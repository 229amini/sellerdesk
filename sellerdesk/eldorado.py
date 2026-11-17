"""Client for the Eldorado Seller API, plus the Order model.

API reference: https://www.eldorado.gg/swagger/seller/index.html (requires a seller login).
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .deadline import GUARANTEE_SECONDS, parse_time
from .net import HttpError, request

BOM = chr(0xFEFF)
PLATFORMS = {"pc", "playstation", "xbox", "switch", "mobile", "android", "ios"}


def format_request(fields) -> str:
    """["PC", "100 herbals"] -> "100 herbals | PC"; ["1", "75", "PC"] -> "1 → 75 | PC"."""
    clean = [re.sub(r"\s+", " ", str(f or "")).strip() for f in fields or []]
    clean = [f for f in clean if f]
    platforms = [f for f in clean if f.lower() in PLATFORMS]
    rest = [f for f in clean if f.lower() not in PLATFORMS]
    if len(rest) >= 2 and rest[0].isdigit() and rest[1].isdigit():
        rest[:2] = [f"{rest[0]} → {rest[1]}"]
    line = " | ".join(rest + platforms)
    return line if len(line) <= 240 else line[:239] + "…"


@dataclass(frozen=True)
class Order:
    id: str
    title: str
    quantity: int
    price: float | None
    currency: str
    buyer: str
    url: str
    started_at: float | None
    window: int | None  # guaranteed delivery time in seconds, None if the offer has none
    kind: str = ""        # Eldorado's category title, e.g. "Custom Request" on a request order
    request_id: str = ""  # boosting and custom requests: the buyer's request behind the order

    def with_request(self, asked: str) -> "Order":
        """A request order names what the buyer asked for: "Custom Request | 100 herbals | PC"."""
        if not asked:
            return self
        return replace(self, title=f"{self.kind or 'Custom Request'} | {asked}")

    @property
    def deadline(self) -> float | None:
        if self.started_at is None or self.window is None:
            return None
        return self.started_at + self.window

    @property
    def summary(self) -> str:
        parts = [self.title]
        if self.quantity > 1:
            parts.append(f"x{self.quantity}")
        if self.price is not None:
            parts.append(f"{self.price:.2f} {self.currency}")
        if self.buyer:
            parts.append(f"buyer {self.buyer}")
        return ", ".join(parts)

    @classmethod
    def from_api(cls, raw: dict, base_url: str) -> "Order":
        offer = raw.get("orderOfferDetails") or {}
        money = raw.get("totalPrice") or {}
        return cls(
            id=raw["id"],
            title=offer.get("offerTitle") or offer.get("gameCategoryTitle") or "Order",
            quantity=raw.get("purchaseQuantity") or 1,
            price=money.get("amount"),
            currency=money.get("currency") or "USD",
            buyer=raw.get("buyerUsername") or "",
            url=f"{base_url}/order/{raw['id']}",
            started_at=parse_time(raw.get("deliveryStartedDate") or raw.get("createdDate")),
            window=GUARANTEE_SECONDS.get(offer.get("guaranteedDeliveryTime")),
            kind=offer.get("gameCategoryTitle") or "",
            request_id=offer.get("boostingRequestId") or "",
        )


@dataclass(frozen=True)
class Notice:
    id: str
    event: str
    title: str
    text: str
    url: str | None
    request_id: str = ""  # BoostingRequestCreated: the request an order will later point to
    request: str = ""     # what the buyer wrote, formatted by format_request

    @classmethod
    def from_api(cls, raw: dict) -> "Notice | None":
        custom = raw.get("customNotification")
        if custom:
            return cls(custom.get("id"), "Custom", custom.get("subject") or "Eldorado",
                       custom.get("text") or "", custom.get("redirectUrl"))
        plain = raw.get("notification") or {}
        if not plain.get("id"):
            return None
        details = plain.get("details") or {}
        data = plain.get("customNotificationData") or {}
        asked = format_request(data.get("requestFields")) if isinstance(data, dict) else ""
        category = details.get("gameCategoryTitle")
        if asked:
            category = f"{category or 'Request'}: {asked}"
        bits = [details.get("title"), category, details.get("buyerUsername")]
        event = plain.get("event") or "Unknown"
        return cls(plain["id"], event, event, ", ".join(b for b in bits if b), None,
                   (details.get("detailsId") or "") if asked else "", asked)


class EldoradoClient:
    def __init__(self, base_url, client_id, client_secret, transport=request, clock=time.time):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport
        self._clock = clock
        self._token = None
        self._expires_at = 0.0

    def _authenticate(self):
        if self._token and self._clock() < self._expires_at - 60:
            return
        raw = self._transport("POST", f"{self.base_url}/api/authentication/seller/token",
                              json_body={"clientId": self.client_id, "clientSecret": self.client_secret})
        reply = json.loads(raw)
        self._token = reply["accessToken"]
        self._expires_at = self._clock() + int(reply.get("expiresIn", 900))

    def _call(self, method, path, params=None) -> str:
        self._authenticate()
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        try:
            return self._transport(method, url, headers={"Authorization": f"Bearer {self._token}"})
        except HttpError as error:
            if error.status != 401:
                raise
            self._token = None  # revoked or expired early: retry once with a fresh token
            self._authenticate()
            return self._transport(method, url, headers={"Authorization": f"Bearer {self._token}"})

    def orders(self, state: str) -> list[Order]:
        reply = json.loads(self._call("GET", "/api/v1/orders/me/seller/orders",
                                      {"orderState": state, "pageSize": 50}) or "{}")
        return [Order.from_api(raw, self.base_url) for raw in reply.get("results") or []]

    def find_requests(self, request_ids, pages: int = 4) -> dict[str, str]:
        """Pages read and unread notifications for the given requests (one status per call)."""
        wanted, found = set(request_ids), {}
        for status in ("IsUnread", "IsRead"):
            cursor = ""
            for _ in range(pages):
                if not wanted - set(found):
                    return found
                params = {"notificationReadStatuses": status, "pageSize": 50}
                if cursor:
                    params.update(pageDirection="Next", cursorValue=cursor)
                reply = json.loads(self._call("GET", "/api/notifications/me", params) or "{}")
                rows = reply.get("results") or []
                for notice in filter(None, (Notice.from_api(raw) for raw in rows)):
                    if notice.request_id in wanted:
                        found[notice.request_id] = notice.request
                cursor = reply.get("nextPageCursor") or ""
                if not rows or not cursor:
                    break
        return found

    def unread_notices(self) -> list[Notice]:
        reply = json.loads(self._call("GET", "/api/notifications/me",
                                      {"notificationReadStatuses": "IsUnread", "pageSize": 30}) or "{}")
        notices = (Notice.from_api(raw) for raw in reply.get("results") or [])
        return [notice for notice in notices if notice]

    def sales_since(self, seconds_ago: float) -> list[tuple[float, float]]:
        """Return (timestamp, net income) rows. The endpoint answers with CSV, not JSON."""
        now = self._clock()

        def iso(ts):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        text = self._call("GET", "/api/v1/orders/me/sold-order-data",
                          {"fromDate": iso(now - seconds_ago), "untilDate": iso(now)})
        rows = []
        for row in csv.DictReader(io.StringIO(text.lstrip(BOM))):
            when = datetime.strptime(row["Order Date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            rows.append((when.timestamp(), float(row["Net Income $"])))
        return rows

    def switch_offline(self):
        self._call("PUT", "/api/offerUser/me/switchOffline")

    def switch_online(self):
        self._call("PUT", "/api/offerUser/me/switchOnline")
