"""The poll loop: turns API state into alerts and keeps the dashboard snapshot fresh."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time

from .channels import Alert, Channel
from .config import Settings
from .deadline import Stage, format_remaining, repeat_interval, stage_for
from .eldorado import Order

log = logging.getLogger("sellerdesk")

# The order polls already cover these two, so the notification feed skips them.
COVERED_EVENTS = {"OrderCreated", "OrderDisputed"}
# The kinds of alert a person can switch on or off, and the ones that start off.
ALERT_KINDS = ("order", "deadline", "dispute", "boosting", "news", "connection", "other")
DEFAULT_OFF = ("boosting", "news", "connection", "other")
# Channels that forward to another alert app, which has its own switches, get every alert.
FORWARDERS = {"webhook"}


def alert_kind(alert: Alert) -> str | None:
    """Which switchable kind an alert is. None for the one alert that must always get through: SellerDesk
    took the account offline, which only the person can undo."""
    if alert.kind == "order":
        return "order" if (alert.headline or alert.title).startswith("New order") else "deadline"
    if alert.kind == "dispute":
        return "dispute"
    if alert.kind == "system":
        return None if alert.title == "SellerDesk took you offline" else "connection"
    event = alert.event or ""
    if "Dispute" in event:
        return "dispute"
    if event == "Custom":
        return "news"
    if event.startswith("Boosting"):
        return "boosting"
    return "other"


def clean_prefs(raw) -> dict:
    """The alert switches as the desktop app sends them. Raises ValueError with a message for the app."""
    if not isinstance(raw, dict):
        raise ValueError("send a JSON object")
    paused = raw.get("paused", False)
    off = raw.get("off", list(DEFAULT_OFF))
    if not isinstance(paused, bool):
        raise ValueError("paused must be true or false")
    if not isinstance(off, list) or any(kind not in ALERT_KINDS for kind in off):
        raise ValueError("off must list kinds from: " + ", ".join(ALERT_KINDS))
    return {"paused": paused, "off": sorted(set(off))}
SALES_REFRESH_SECONDS = 300
DAY = 86400


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(f"{path}.tmp", "w", encoding="utf-8") as file:
        json.dump(state, file)
    os.replace(f"{path}.tmp", path)


class Engine:
    def __init__(self, api, channels: list[Channel], settings: Settings, state=None, clock=time.time):
        self.api = api
        self.channels = channels
        self.settings = settings
        self.clock = clock
        self.state = state if state is not None else {}
        self.state.setdefault("alarms", {})     # order id -> alarm record
        self.state.setdefault("disputes", [])
        self.state.setdefault("notices", [])
        self.state.setdefault("requests", {})   # boosting request id -> what the buyer wrote ("" = not found)
        self.commands: queue.SimpleQueue = queue.SimpleQueue()  # filled by the dashboard thread
        self.wake = threading.Event()
        self.health = {channel.name: {"ok": None, "error": ""} for channel in channels}
        self.recent: list[dict] = []
        self.sales = {"day": None, "week": None, "day_count": 0, "week_count": 0}
        self.sales_checked = 0.0
        self.open_orders: list[Order] = []
        self.last_poll = None
        self.last_error = ""
        self.snapshot: dict = self._build_snapshot()

    # ---- commands from the dashboard ----

    def acknowledge(self, order_id: str) -> None:
        self.commands.put(("ack", order_id))
        self.wake.set()

    def go_online(self) -> None:
        self.commands.put(("online", ""))
        self.wake.set()

    def alert_prefs(self) -> dict:
        """Which alerts reach the person: all of SellerDesk on or off, and each kind on or off."""
        prefs = self.state.get("alert_prefs") or {"paused": False, "off": list(DEFAULT_OFF)}
        return {**prefs, "kinds": list(ALERT_KINDS)}

    def set_alert_prefs(self, raw) -> dict:
        prefs = clean_prefs(raw)
        self.commands.put(("prefs", prefs))     # the poll thread owns the state
        self.wake.set()
        return {**prefs, "kinds": list(ALERT_KINDS)}

    def _wanted(self, kind: str | None) -> bool:
        prefs = self.alert_prefs()
        return not prefs["paused"] and kind not in prefs["off"]

    # ---- one poll ----

    def tick(self) -> None:
        now = self.clock()
        self._take_commands()
        self._take_acks()
        self._watch_notices()   # first, so a request order can name what the buyer asked for
        self._watch_orders(self._with_requests(self.api.orders("Paid")), now)
        self._watch_disputes()
        self._watch_sales(now)
        self._deadman(now)
        self.state["started"] = True
        self.last_poll = now
        self.last_error = ""
        self.snapshot = self._build_snapshot()

    def _take_commands(self):
        while not self.commands.empty():
            command, value = self.commands.get()
            if command == "ack":
                self._ack(value)
            elif command == "online":
                self.api.switch_online()
                self.state["deadman_tripped"] = False
                self._dispatch(Alert("system", 3, "You are online", "SellerDesk switched you back online."))
            elif command == "prefs":
                self.state["alert_prefs"] = value

    def _take_acks(self):
        for channel in self.channels:
            try:
                for order_id in channel.poll_acks():
                    self._ack(order_id)
            except Exception as error:
                log.warning("%s ack poll failed: %s", channel.name, error)

    def _ack(self, order_id):
        alarm = self.state["alarms"].get(order_id)
        if alarm and alarm.get("acked_stage") is None:
            alarm["acked_stage"] = alarm["stage"]
            log.info("order %s acknowledged", order_id)

    def _watch_orders(self, orders: list[Order], now: float):
        settings, alarms = self.settings, self.state["alarms"]
        self.open_orders = orders
        for order in orders:
            remaining = None if order.deadline is None else order.deadline - now
            stage = stage_for(remaining, order.window, settings.hurry_at, settings.critical_at)
            alarm = alarms.get(order.id)
            if alarm is None:
                alarms[order.id] = {"first": now, "last": now, "stage": int(stage), "stage_since": now,
                                    "acked_stage": None}
                log.info("new order %s: %s", order.id, order.summary)
                self._dispatch(self._order_alert(order, "New order", remaining))
                continue

            advanced = stage > alarm["stage"]
            if advanced:
                alarm.update(stage=int(stage), stage_since=now)
            acked = alarm["acked_stage"]
            # An ack buys silence until the order turns critical, then the alarm comes back once.
            if acked is not None:
                if stage >= Stage.CRITICAL and stage > acked:
                    alarm["acked_stage"] = None
                else:
                    continue
            over_budget = now - alarm["stage_since"] >= settings.stage_budget_minutes * 60
            due = now - alarm["last"] >= repeat_interval(stage, settings.repeat_seconds)
            if advanced or (due and not over_budget):
                alarm["last"] = now
                self._dispatch(self._order_alert(order, self._headline(stage), remaining, repeat=not advanced))

        open_ids = {order.id for order in orders}
        for order_id in [known for known in alarms if known not in open_ids]:
            del alarms[order_id]
            self._closed(order_id)

    @staticmethod
    def _headline(stage: Stage) -> str:
        return {Stage.CALM: "Order waiting", Stage.HURRY: "Deliver soon",
                Stage.CRITICAL: "Deadline close", Stage.LATE: "Order is late"}[stage]

    @staticmethod
    def _order_alert(order: Order, headline: str, remaining, repeat=False) -> Alert:
        if remaining is None:
            title = headline
        elif remaining > 0:
            title = f"{headline}: {format_remaining(remaining)} left"
        else:
            title = f"{headline}: {format_remaining(remaining)} over"
        return Alert("order", 5, title, order.summary, order.url, order.id, repeat, order.deadline, order.window,
                     headline)

    def _with_requests(self, orders: list[Order]) -> list[Order]:
        """Request orders carry only "Custom Request"; the buyer's words come from the request's notification."""
        known = self.state["requests"]
        missing = [o.request_id for o in orders if o.request_id and o.request_id not in known]
        if missing:
            try:
                found = self.api.find_requests(missing)
            except Exception as error:  # a title must not stop the alarm
                log.warning("request lookup failed: %s", error)
                found = None
            if found is not None:
                for request_id in missing:
                    known[request_id] = found.get(request_id, "")
                self._trim_requests()
        return [o.with_request(known.get(o.request_id, "")) if o.request_id else o for o in orders]

    def _trim_requests(self, keep: int = 1000):
        known = self.state["requests"]
        for request_id in list(known)[:-keep]:
            del known[request_id]

    def _watch_disputes(self):
        seen = set(self.state["disputes"])
        for order in self._with_requests(self.api.orders("Disputed")):
            if order.id not in seen:
                seen.add(order.id)
                self._dispatch(Alert("dispute", 4, "Order disputed", order.summary, order.url))
        self.state["disputes"] = sorted(seen)[-1000:]

    def _watch_notices(self):
        first_run = not self.state.get("started")
        seen = set(self.state["notices"])
        for notice in self.api.unread_notices():
            if notice.id in seen:
                continue
            seen.add(notice.id)
            if notice.request_id and notice.request:
                self.state["requests"][notice.request_id] = notice.request
                self._trim_requests()
            # The first poll records the backlog without pushing it to your phone.
            if not first_run and notice.event not in COVERED_EVENTS:
                self._dispatch(Alert("notice", 3, notice.title, notice.text or notice.event, notice.url,
                                     event=notice.event))
        self.state["notices"] = sorted(seen)[-2000:]

    def _watch_sales(self, now):
        if now - self.sales_checked < SALES_REFRESH_SECONDS:
            return
        self.sales_checked = now
        try:
            rows = self.api.sales_since(7 * DAY)
        except Exception as error:  # a chart must not stop the alarm
            log.warning("sales refresh failed: %s", error)
            return
        day = [net for when, net in rows if now - when <= DAY]
        self.sales = {"day": round(sum(day), 2), "week": round(sum(net for _, net in rows), 2),
                      "day_count": len(day), "week_count": len(rows)}

    def _deadman(self, now):
        settings = self.settings
        if not settings.deadman_enabled or self.state.get("deadman_tripped"):
            return
        # With order alarms switched off nobody is asked to answer, so silence must not take the account offline.
        if not self._wanted("order") and not self._wanted("deadline"):
            return
        limit = settings.deadman_minutes * 60
        ignored = [a for a in self.state["alarms"].values()
                   if a["acked_stage"] is None and now - a["first"] >= limit]
        if ignored:
            self.api.switch_offline()
            self.state["deadman_tripped"] = True
            self._dispatch(Alert("system", 5, "SellerDesk took you offline",
                                 f"An alarm went unanswered for {settings.deadman_minutes} minutes. "
                                 "Buyers can't place new orders until you go online again."))

    # ---- delivery ----

    def _closed(self, order_id):
        for channel in self.channels:
            try:
                channel.closed(order_id)
            except Exception as error:
                log.warning("%s close failed: %s", channel.name, error)

    def _dispatch(self, alert: Alert):
        wanted = self._wanted(alert_kind(alert))
        self.recent = [{"at": self.clock(), "title": alert.title, "body": alert.body, "kind": alert.kind,
                        "muted": not wanted}] + self.recent[:29]
        for channel in self.channels:
            if alert.repeat and not channel.wants_repeats:
                continue
            if not wanted and channel.name not in FORWARDERS:
                continue
            try:
                channel.send(alert)
                self.health[channel.name] = {"ok": True, "error": ""}
            except Exception as error:  # one broken channel must not silence the others
                self.health[channel.name] = {"ok": False, "error": str(error)[:200]}
                log.warning("%s send failed: %s", channel.name, error)

    def _build_snapshot(self) -> dict:
        alarms = self.state["alarms"]
        orders = []
        for order in self.open_orders:
            alarm = alarms.get(order.id, {})
            orders.append({"id": order.id, "title": order.title, "quantity": order.quantity,
                           "price": order.price, "currency": order.currency, "buyer": order.buyer,
                           "url": order.url, "deadline": order.deadline, "window": order.window,
                           "acked": alarm.get("acked_stage") is not None})
        orders.sort(key=lambda o: (o["deadline"] is None, o["deadline"] or 0))
        return {"now": self.clock(), "last_poll": self.last_poll, "last_error": self.last_error,
                "poll_seconds": self.settings.poll_seconds, "orders": orders, "sales": self.sales,
                "hurry_at": self.settings.hurry_at, "critical_at": self.settings.critical_at,
                "channels": [{"name": name, **status} for name, status in self.health.items()],
                "recent": self.recent[:8],
                "deadman": {"enabled": self.settings.deadman_enabled,
                            "minutes": self.settings.deadman_minutes,
                            "tripped": bool(self.state.get("deadman_tripped"))}}

    # ---- forever ----

    def run(self, save=None) -> None:
        settings = self.settings
        last_ok, warned = self.clock(), False
        self._dispatch(Alert("system", 2, "SellerDesk is on watch", f"Checking orders every {settings.poll_seconds} s."))
        while True:
            try:
                self.tick()
                if save:
                    save(self.state)
                last_ok = self.clock()
                if warned:
                    warned = False
                    self._dispatch(Alert("system", 3, "SellerDesk recovered", "Order checks work again."))
            except Exception as error:
                log.error("poll failed: %s", error)
                self.last_error = str(error)[:300]
                self.snapshot = self._build_snapshot()
                # A silent watcher means missed orders, so say so once, at full volume.
                if not warned and self.clock() - last_ok > settings.unhealthy_after_seconds:
                    warned = True
                    minutes = settings.unhealthy_after_seconds // 60
                    self._dispatch(Alert("system", 5, "SellerDesk can't reach Eldorado",
                                         f"No successful check for {minutes} minutes: {error}"))
            self.wake.wait(settings.poll_seconds)
            self.wake.clear()
