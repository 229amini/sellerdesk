"""The request desk: reads the boosting and custom requests buyers send to every seller, prices them from
your own rules and offers, and sends an offer when you turn that on.

Eldorado's seller key reaches the same calls the site uses:
- GET  /api/boostingOffers/me/boostingRequests/received        the requests you can answer
- GET  /api/boostingOffers/boostingRequests/{id}               one request: state, fields, your offer
- POST /api/boostingOffers                                      send an offer
- PUT  /api/boostingOffers/{offerId}/update                     change it
- DELETE /api/boostingOffers/{offerId}/delete                   withdraw it
Sending is off until you switch it on, and Simulate prices real requests without sending anything.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone

from .market import MarketError, bundle_size

log = logging.getLogger("sellerdesk")

RECEIVED = "/api/boostingOffers/me/boostingRequests/received"
ONE = "/api/boostingOffers/boostingRequests/{}"
OFFERS = "/api/boostingOffers"
KEEP = 300                 # closed requests kept on disk, newest first
KEEP_HOURS = 72
LIFE_HOURS = 7 * 24        # Eldorado keeps a request open for a week
KEEP_OPEN = 1500           # open requests kept, a safety cap only
KEEP_MARKS = 2000          # hidden and skipped ids
FORGET = 5000              # closed requests dropped from disk, remembered so the back read skips them
PAGE = 30                  # the most `received` gives per page
BACKFILL_SECONDS = 600     # how often the whole `received` list is walked for requests the desk never read
BACKFILL_PAGES = 20
BACKLOG_PER_PASS = 4       # of those, read per pass, so new requests stay quick
SHOW_CLOSED = 150          # closed requests sent to the dashboard besides every open one
REFRESH_SECONDS = 60       # how often a request you offered on is read again
CHECK_SECONDS = 180        # how often a young open request is read again, to drop the closed ones
OLD_CHECK_SECONDS = 900    # the same for one over an hour old
REFRESH_PER_PASS = 6
OFFERS_EVERY = 600         # your own item offers, for pricing item requests
MAX_PRICE = 10_000.0
MAX_TEXT = 500

MODES = ("off", "simulate", "live")
DELIVERY_TIMES = ("Minute20", "Hour1", "Hour2", "Hour3", "Hour5", "Hour8", "Hour12", "Day1", "Day2", "Day3",
                  "Day5", "Day7", "Day10", "Day14")
PLATFORMS = ("PC", "PlayStation", "Xbox")
DELIVERY_MODES = ("fixed", "auto")
LEVELS_PER_STEP = 25       # "auto" delivery: every 25 levels or 1 million coins is one job of your base time

# How each Arc Raiders request form is priced. Other games fall back to "review": you read and answer.
KINDS = ("levels", "coins", "items", "flat", "review")
CATEGORY_KINDS = {
    "Power Leveling": "levels",
    "Workshop Leveling": "levels",
    "Stash Upgrade": "levels",
    "Coins Farm": "coins",
    "Blueprint Farm": "items",
    "Custom Request": "items",
    "Quests": "review",
    "Trials": "review",
    "Expedition": "review",
    "Raids": "review",
}

SPAM = re.compile(
    r"https?://|www\.|\b[\w-]+\.(?:com|net|app|xyz|win|io|shop|store)\b|discord|telegram|t\.me/|whatsapp"
    r"|\bbots?\b|automates?|voltstrike|snapbid|firstbid|reviewboost|i'?m a seller|not a buyer|check my store",
    re.I)
BUDGET = re.compile(
    r"(?:less than|under|below|max(?:imum)?|budget|up to|for|at)\s*\$?\s*(\d+(?:[.,]\d+)?)\s*\$?"
    r"|\$\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:\$|(?:usd|dollars?)\b)", re.I)
COINS = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:[.,]\d+)?)\s*(k|m|mil|mill|million|kk)?\b", re.I)
ROMAN = {"i", "ii", "iii", "iv", "v", "vi"}
COMMON = {"blueprint", "augment", "module", "weapon", "raider", "delivery", "shield", "grenade", "recharger",
          "light", "heavy", "medium", "small", "large", "mixed", "bundle"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value) -> float:
    try:
        text = str(value or "")
        if "." in text:  # .NET writes 7 fractional digits
            head, _, rest = text.partition(".")
            digits = re.match(r"\d*", rest).group(0)
            text = head + "." + digits[:6] + rest[len(digits):]
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except ValueError:
        return 0.0


def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def money(value) -> float:
    return round(float(value) + 1e-9, 2)


# ---------- settings ----------


def default_settings() -> dict:
    return {
        "mode": "off",
        "delivery": "Minute20",
        "deliveryMode": "fixed",
        "message": "",
        "minPrice": 1.0,
        "platforms": list(PLATFORMS),
        "blocked": [],
        "categories": {name: {"kind": kind, "auto": False, "bands": [], "perMillion": 0.0, "flat": 0.0,
                              "markup": 0.0, "delivery": ""}
                       for name, kind in CATEGORY_KINDS.items()},
    }


def _price(value, name) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number")
    if value < 0 or value > MAX_PRICE:
        raise ValueError(f"{name} must be between 0 and {MAX_PRICE:g}")
    return round(float(value), 4)


def clean_settings(raw, old: dict | None = None) -> dict:
    """Settings as the desktop app sends them, checked. Unknown keys are dropped. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("send a JSON object")
    out = copy.deepcopy(old or default_settings())
    if "mode" in raw:
        if raw["mode"] not in MODES:
            raise ValueError("mode must be one of " + ", ".join(MODES))
        out["mode"] = raw["mode"]
    if "delivery" in raw:
        if raw["delivery"] not in DELIVERY_TIMES:
            raise ValueError("delivery must be one of " + ", ".join(DELIVERY_TIMES))
        out["delivery"] = raw["delivery"]
    if "deliveryMode" in raw:
        if raw["deliveryMode"] not in DELIVERY_MODES:
            raise ValueError("deliveryMode must be one of " + ", ".join(DELIVERY_MODES))
        out["deliveryMode"] = raw["deliveryMode"]
    if "message" in raw:
        if not isinstance(raw["message"], str) or len(raw["message"]) > MAX_TEXT:
            raise ValueError(f"message must be text under {MAX_TEXT} characters")
        out["message"] = raw["message"].strip()
    if "minPrice" in raw:
        out["minPrice"] = _price(raw["minPrice"], "minPrice")
    if "platforms" in raw:
        if not isinstance(raw["platforms"], list) or any(p not in PLATFORMS for p in raw["platforms"]):
            raise ValueError("platforms must list " + ", ".join(PLATFORMS))
        out["platforms"] = [p for p in PLATFORMS if p in raw["platforms"]]
    if "blocked" in raw:
        if not isinstance(raw["blocked"], list) or not all(isinstance(b, str) for b in raw["blocked"]):
            raise ValueError("blocked must list buyer names")
        out["blocked"] = sorted({clean(b)[:60] for b in raw["blocked"] if clean(b)}, key=str.casefold)[:500]
    for name, given in (raw.get("categories") or {}).items():
        if not isinstance(name, str) or not isinstance(given, dict):
            raise ValueError("categories must map a name to its settings")
        rule = out["categories"].setdefault(clean(name)[:60], {"kind": "review", "auto": False, "bands": [],
                                                               "perMillion": 0.0, "flat": 0.0, "markup": 0.0,
                                                               "delivery": ""})
        if "kind" in given:
            if given["kind"] not in KINDS:
                raise ValueError("kind must be one of " + ", ".join(KINDS))
            rule["kind"] = given["kind"]
        if "auto" in given:
            if not isinstance(given["auto"], bool):
                raise ValueError("auto must be true or false")
            rule["auto"] = given["auto"]
        for key in ("perMillion", "flat"):
            if key in given:
                rule[key] = _price(given[key], key)
        if "markup" in given:
            markup = given["markup"]
            if isinstance(markup, bool) or not isinstance(markup, (int, float)) or not -90 <= markup <= 500:
                raise ValueError("markup must be a percent between -90 and 500")
            rule["markup"] = float(markup)
        if "delivery" in given:
            if given["delivery"] not in ("",) + DELIVERY_TIMES:
                raise ValueError("delivery must be empty or one of " + ", ".join(DELIVERY_TIMES))
            rule["delivery"] = given["delivery"]
        if "bands" in given:
            bands = given["bands"]
            if not isinstance(bands, list) or len(bands) > 30:
                raise ValueError("bands must be a list of up to 30 {upTo, each}")
            clean_bands = []
            for band in bands:
                if not isinstance(band, dict):
                    raise ValueError("each band needs upTo and each")
                up_to = band.get("upTo")
                if isinstance(up_to, bool) or not isinstance(up_to, (int, float)) or up_to != int(up_to) or not 1 <= up_to <= 10_000:
                    raise ValueError("upTo must be a whole level from 1")
                clean_bands.append({"upTo": int(up_to), "each": _price(band.get("each"), "each")})
            rule["bands"] = sorted(clean_bands, key=lambda b: b["upTo"])
    return out


# ---------- reading a request ----------


def field(fields: list, *labels) -> str:
    wanted = {label.casefold() for label in labels}
    for label, value in fields:
        if label.casefold() in wanted:
            return value
    return ""


def words_of(fields: list) -> str:
    """What the buyer typed, without the form's fixed choices."""
    return " | ".join(value for label, value in fields
                      if label.casefold() not in ("platform", "completion method"))


def budget_of(text: str) -> float | None:
    for match in BUDGET.finditer(text or ""):
        raw = next(g for g in match.groups() if g)
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        if 0 < value < MAX_PRICE:
            return value
    return None


def coins_of(text: str) -> int | None:
    best = None
    for match in COINS.finditer(text or ""):
        raw = match.group(1)
        number = float(raw.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw) else raw.replace(",", "."))
        unit = (match.group(2) or "").lower()
        if unit == "k":
            number *= 1_000
        elif unit in ("m", "mil", "mill", "million", "kk"):
            number *= 1_000_000
        if number >= 10_000 and (best is None or number > best):
            best = int(number)
    return best


def _tokens(text: str) -> list[str]:
    out = []
    for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss") and not token.isdigit():
            token = token[:-1]
        out.append(token)
    return out


def item_name(title: str) -> str:
    """'Snap Hook x1 | ARC Raiders Quick Use | Fast Delivery' -> 'Snap Hook'."""
    head = (title or "").split("|")[0]
    head = re.sub(r"\(\s*([^)]*?)\s*\)", r"\1", head)
    head = re.sub(r"(?<![\w.])(?:x\s*\d+|\d+\s*x)(?![\w.])", " ", head, flags=re.I)
    return clean(head)


def _count_near(tokens: list[str], start: int, end: int) -> int | None:
    """The number written next to an item: '12 Tempest IV', '10x Snap Hook', 'Snap Hook x10'."""
    if start > 0:
        before = tokens[start - 1]
        if before.isdigit():
            return int(before)
        if re.fullmatch(r"\d+x", before):
            return int(before[:-1])
        if before == "x" and start > 1 and tokens[start - 2].isdigit():
            return int(tokens[start - 2])
    if end < len(tokens):
        after = tokens[end]
        if re.fullmatch(r"x\d+", after):
            return int(after[1:])
        if after == "x" and end + 1 < len(tokens) and tokens[end + 1].isdigit():
            return int(tokens[end + 1])
    return None


def match_items(text: str, offers: list) -> tuple[list[dict], list[str]]:
    """Your offers named in the request, with the count asked for. Also near misses: an offer whose name
    matches but for its tier ("25 Anvils" vs "Anvil IV"), which a person should look at."""
    tokens = _tokens(text)
    used = [False] * len(tokens)
    found, maybe = [], []
    named = []
    for offer in offers:
        name = item_name(offer.title)
        name_tokens = _tokens(name)
        if name_tokens and len("".join(name_tokens)) >= 4:
            named.append((offer, name, name_tokens))
    named.sort(key=lambda item: -len("".join(item[2])))
    for offer, name, name_tokens in named:
        wanted = "".join(name_tokens)
        for start in range(len(tokens)):
            if used[start]:
                continue
            joined, end = "", start
            while end < len(tokens) and len(joined) < len(wanted):
                joined += tokens[end]
                end += 1
            if joined != wanted or any(used[start:end]):
                continue
            for i in range(start, end):
                used[i] = True
            size = max(1, bundle_size(offer.title, name))
            found.append({"offer": offer.id, "name": name, "count": _count_near(tokens, start, end),
                          "each": round(offer.price / size, 4), "stock": offer.quantity * size,
                          "state": offer.state})
            break
        else:
            base = [t for t in name_tokens if t not in ROMAN]
            if base != name_tokens and base and "".join(base) in "".join(tokens):
                maybe.append(name)
    # Buyers shorten names: "40 Survivor" for "Looting Mk. 3 (Survivor) Augment". A rare word of an offer's
    # name in the request makes it a near miss for a person to look at.
    taken = {f["name"] for f in found}
    counts = {}
    for _, _, name_tokens in named:
        for token in set(name_tokens):
            counts[token] = counts.get(token, 0) + 1
    words = set(tokens)
    for _, name, name_tokens in named:
        if name not in taken and any(len(t) >= 5 and t not in COMMON and counts[t] <= 3 and t in words
                                     for t in name_tokens):
            maybe.append(name)
    return found, sorted(set(maybe))[:6]


def level_price(bands: list, current: int, desired: int) -> float | None:
    if not bands or desired <= current:
        return None
    total = 0.0
    for level in range(current + 1, desired + 1):
        band = next((b for b in bands if level <= b["upTo"]), None)
        if band is None:
            return None
        total += band["each"]
    return total


def rule_for(settings: dict, category: str) -> dict:
    """The category's rule; Eldorado writes "Custom Request" in one game and "Custom request" in another."""
    rules = settings["categories"]
    if category in rules:
        return rules[category]
    folded = (category or "").casefold()
    return next((rule for name, rule in rules.items() if name.casefold() == folded), {"kind": "review", "auto": False})


def quote(request: dict, settings: dict, offers: list) -> dict:
    """Your price for a request, how it was worked out, and whether it may go out by itself.

    sure=False means a person should look first: the auto sender only sends sure quotes."""
    rule = rule_for(settings, request["category"])
    kind = rule.get("kind", "review")
    fields = request["fields"]
    words = words_of(fields)
    result = {"kind": kind, "price": None, "basis": "", "sure": False, "why": "", "items": [], "maybe": [],
              "size": 1}
    if kind == "levels":
        try:
            current = int(float(field(fields, "Current level")))
            desired = int(float(field(fields, "Desired level")))
        except ValueError:
            result["why"] = "No levels in the request."
            return result
        price = level_price(rule.get("bands") or [], current, desired)
        if price is None:
            result["why"] = f"No price for levels {current} to {desired}. Add them in Prices."
            return result
        result.update(price=price, basis=f"levels {current} → {desired}", sure=True,
                      size=max(1, math.ceil((desired - current) / LEVELS_PER_STEP)))
    elif kind == "coins":
        amount = coins_of(words)
        if not amount or not rule.get("perMillion"):
            result["why"] = "Set a price per million coins." if amount else "No coin amount in the request."
            return result
        result.update(price=amount / 1_000_000 * rule["perMillion"], basis=f"{amount:,} coins", sure=True,
                      size=max(1, math.ceil(amount / 1_000_000)))
    elif kind == "items":
        found, maybe = match_items(words, [o for o in offers if not o.is_currency])
        result["items"], result["maybe"] = found, maybe
        if not found:
            result["why"] = ("Not one of your offers. " + ("Close: " + ", ".join(maybe) if maybe else "")).strip()
            return result
        missing = [f["name"] for f in found if not f["count"]]
        if missing:
            result["why"] = "How many? No count next to " + ", ".join(missing) + "."
            return result
        total = sum(f["count"] * f["each"] for f in found)
        markup = rule.get("markup") or 0.0
        result.update(price=total * (1 + markup / 100),
                      basis=" + ".join(f"{f['count']} × {f['name']} {f['each']:g}" for f in found)
                      + (f", {markup:+g}%" if markup else ""))
        closed = [f["name"] for f in found if f["state"] != "Active"]
        short = [f["name"] for f in found if f["count"] > f["stock"]]
        numbers = [int(n) for n in re.findall(r"(?<![\w.$])\d+(?![\w.])", words)]
        unexplained = len(numbers) - len(found) - (1 if budget_of(words) is not None else 0)
        if closed:
            result["why"] = "Your offer is closed or paused: " + ", ".join(closed) + "."
        elif short:
            result["why"] = "Not enough stock: " + ", ".join(short) + "."
        elif maybe or unexplained > 0 or len(found) > 1:
            result["why"] = "Check the request: it may ask for more than this."
        else:
            result["sure"] = True
    elif kind == "flat":
        if not rule.get("flat"):
            result["why"] = "Set a price for this kind of request."
            return result
        result.update(price=rule["flat"], basis="your set price", sure=True)
    else:
        result["why"] = "Read it and send your own price."
        return result
    price = money(max(result["price"], settings.get("minPrice") or 0))
    result["price"] = price
    result["delivery"] = delivery_for(request["category"], settings, result["size"])
    budget = budget_of(words)
    if budget is not None and price > budget:
        result["sure"] = False
        result["why"] = f"The buyer asks for under ${budget:g}."
    return result


# ---------- the desk ----------


class RequestDesk:
    def __init__(self, api, path: str, poll_seconds: int = 10, clock=time.time):
        self.api = api
        self.path = path
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._offers: list = []
        self._offers_read = 0.0
        self._backfilled = 0.0
        self._backlog: list[dict] = []
        self.problem = ""
        self.last_poll = None
        self.data = self._load()

    # --- storage ---

    def _load(self) -> dict:
        data = {"settings": default_settings(), "requests": {}, "hidden": [], "skipped": [], "forgotten": []}
        try:
            with open(self.path, encoding="utf-8") as file:
                saved = json.load(file)
        except FileNotFoundError:
            return data
        except (OSError, ValueError) as error:
            log.error("requests: could not read %s (%s); starting with sending off", self.path, error)
            return data
        if isinstance(saved, dict):
            try:
                data["settings"] = clean_settings(saved.get("settings") or {}, default_settings())
            except ValueError as error:
                log.error("requests: saved settings refused (%s); using the defaults", error)
            if isinstance(saved.get("requests"), dict):
                data["requests"] = {k: v for k, v in saved["requests"].items() if isinstance(v, dict)}
            for key, cap in (("hidden", KEEP_MARKS), ("skipped", KEEP_MARKS), ("forgotten", FORGET)):
                if isinstance(saved.get(key), list):
                    data[key] = [h for h in saved[key] if isinstance(h, str)][-cap:]
        return data

    def _save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fresh = self.path + ".new"
        with open(fresh, "w", encoding="utf-8") as file:
            json.dump(self.data, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(fresh, self.path)

    def _is_open(self, row: dict) -> bool:
        now = self.clock()
        return (row.get("state") == "Active" and row.get("created", 0) >= now - LIFE_HOURS * 3600
                and not (row.get("expires") and row["expires"] <= now))

    def _trim(self) -> None:
        """An open request stays for its whole week; the cap and the 72 hours apply to closed ones only.
        Before, 300 rows were about 22 hours of requests, so a day-old open one fell off the list."""
        cutoff = self.clock() - KEEP_HOURS * 3600
        rows = sorted(self.data["requests"].values(), key=lambda r: r.get("created", 0), reverse=True)
        keep = {r["id"] for r in [r for r in rows if self._is_open(r)][:KEEP_OPEN]}
        keep |= {r["id"] for r in [r for r in rows if not self._is_open(r)][:KEEP] if r.get("created", 0) >= cutoff}
        dropped = [k for k in self.data["requests"] if k not in keep]
        if dropped:
            self.data["forgotten"] = (self.data.setdefault("forgotten", []) + dropped)[-FORGET:]
        self.data["requests"] = {k: v for k, v in self.data["requests"].items() if k in keep}

    # --- reads for the dashboard ---

    def snapshot(self) -> dict:
        with self._lock:
            data = copy.deepcopy(self.data)
        rows = sorted(data["requests"].values(), key=lambda r: r.get("created", 0), reverse=True)
        hidden, skipped = set(data["hidden"]), set(data["skipped"])
        for row in rows:
            row["hidden"] = row["id"] in hidden
            row["skipped"] = row["id"] in skipped
        day = self.clock() - 86400
        today = [r for r in rows if r.get("created", 0) >= day]
        stats = {
            "seen": len(today),
            "spam": sum(1 for r in today if r.get("spam")),
            "sent": sum(1 for r in today if (r.get("mine") or {}).get("offerId")),
            "simulated": sum(1 for r in today if (r.get("mine") or {}).get("by") == "simulate"),
            "won": sum(1 for r in today if (r.get("mine") or {}).get("state") == "OfferWon"),
        }
        shown = {r["id"] for r in rows if self._is_open(r)}
        shown |= {r["id"] for r in [r for r in rows if r["id"] not in shown][:SHOW_CLOSED]}
        return {"settings": data["settings"], "requests": [r for r in rows if r["id"] in shown], "stats": stats,
                "checked": self.last_poll, "problem": self.problem or None,
                "deliveryTimes": list(DELIVERY_TIMES), "kinds": list(KINDS)}

    # --- settings ---

    def set_settings(self, raw: dict) -> dict:
        with self._lock:
            self.data["settings"] = clean_settings(raw, self.data["settings"])
            for row in self.data["requests"].values():
                row["quote"] = quote(row, self.data["settings"], self._offers)
            self._save()
            log.info("requests: settings saved, mode %s", self.data["settings"]["mode"])
            return copy.deepcopy(self.data["settings"])

    def hide(self, request_id: str, hidden: bool = True) -> None:
        self._mark("hidden", request_id, hidden)

    def skip(self, request_id: str, skipped: bool = True) -> None:
        """A request you don't want: it leaves To answer, and the auto sender leaves it alone."""
        self._mark("skipped", request_id, skipped)

    def _mark(self, key: str, request_id: str, on: bool) -> None:
        with self._lock:
            ids = [h for h in self.data[key] if h != request_id]
            if on:
                ids.append(request_id)
            self.data[key] = ids[-KEEP_MARKS:]
            self._save()

    # --- one request ---

    def _read_one(self, request_id: str, listed: dict | None = None) -> dict:
        raw = self.api.signed_read(ONE.format(request_id))
        details = (raw.get("boostingRequestDetails") or {}).get("descriptionValues") or []
        fields = [[clean(v.get("label"))[:80], clean(v.get("value"))[:MAX_TEXT]]
                  for v in details if isinstance(v, dict) and clean(v.get("value"))]
        buyer = (raw.get("buyerInfo") or {}).get("user") or {}
        record = (raw.get("buyerInfo") or {}).get("userOrderInfo") or {}
        mine = raw.get("sellerDetails") or {}
        offers = raw.get("offers") or []
        offer = offers[0] if offers else {}
        price = ((offer.get("pricing") or {}).get("pricePerUnit") or offer.get("pricePerUnit") or {}).get("amount")
        listed = listed or {}
        return {
            "id": request_id,
            "game": clean(raw.get("gameName") or listed.get("gameName")),
            "gameId": str(raw.get("gameId") or listed.get("gameId") or ""),
            "category": clean(raw.get("gameCategoryTitle") or listed.get("boostingCategoryTitle")),
            "categoryId": str(raw.get("boostingCategoryId") or listed.get("boostingCategoryId") or ""),
            "buyer": clean(buyer.get("username") or listed.get("buyerUsername")),
            "buyerId": str(buyer.get("id") or listed.get("buyerId") or ""),
            "buyerOrders": int(record.get("ratingCount") or 0),
            "buyerScore": record.get("feedbackScore"),
            "created": parse_time(raw.get("createdDate") or listed.get("createdDate")),
            "expires": parse_time(raw.get("expireDate")),
            "state": clean(raw.get("state")),
            "sellers": raw.get("availableSellersCount"),
            "viewers": raw.get("viewersCount"),
            "budget": raw.get("desiredPrice"),
            "fields": fields,
            "platform": field(fields, "Platform"),
            "text": words_of(fields),
            "mineState": clean(mine.get("boostingRequestSellerState")),
            "offer": {"id": offer.get("id"), "price": price,
                      "delivery": (offer.get("offerDetails") or {}).get("guaranteedDeliveryTime")
                      or offer.get("guaranteedDeliveryTime")} if offer else None,
            "read": self.clock(),
        }

    def _merge(self, fresh: dict) -> dict:
        """Keep what this desk did with the request (mine) over what was just read."""
        old = self.data["requests"].get(fresh["id"]) or {}
        mine = dict(old.get("mine") or {})
        if fresh.get("offer"):
            mine.update(offerId=fresh["offer"]["id"], price=fresh["offer"]["price"] or mine.get("price"),
                        delivery=fresh["offer"]["delivery"] or mine.get("delivery"))
        mine["state"] = fresh.pop("mineState") or mine.get("state", "")
        fresh["mine"] = mine
        settings = self.data["settings"]
        fresh["spam"] = bool(SPAM.search(fresh["text"]))
        fresh["blocked"] = fresh["buyer"].casefold() in {b.casefold() for b in settings["blocked"]}
        fresh["quote"] = quote(fresh, settings, self._offers)
        self.data["requests"][fresh["id"]] = fresh
        return fresh

    # --- offers ---

    def _body(self, request_id, price, delivery, message) -> dict:
        body = {"boostingRequestId": request_id, "guaranteedDeliveryTime": delivery,
                "pricing": {"quantity": 1, "minQuantity": 1, "volumeDiscounts": None,
                            "pricePerUnit": {"amount": money(price), "currency": "USD"}}}
        if message:
            body["message"] = message
        return body

    def message_for(self, row: dict, price: float, delivery: str) -> str:
        template = self.data["settings"].get("message") or ""
        values = {"buyer": row.get("buyer", ""), "price": f"${money(price):g}", "delivery": delivery_text(delivery),
                  "request": row.get("text", ""), "game": row.get("game", "")}
        return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template).strip()[:MAX_TEXT]

    def send(self, request_id: str, price, delivery: str | None = None, message: str | None = None,
             by: str = "you") -> dict:
        """Send your offer, or change it when one is already out. Raises ValueError or MarketError."""
        price = _price(price, "price")
        if price <= 0:
            raise ValueError("price must be above 0")
        with self._lock:
            row = self.data["requests"].get(request_id)
            if row is None:
                raise KeyError(request_id)
            delivery = delivery or (row.get("quote") or {}).get("delivery") \
                or delivery_for(row["category"], self.data["settings"])
            if delivery not in DELIVERY_TIMES:
                raise ValueError("delivery must be one of " + ", ".join(DELIVERY_TIMES))
            if message is None:
                message = self.message_for(row, price, delivery)
            offer_id = (row.get("mine") or {}).get("offerId")
        body = self._body(request_id, price, delivery, message)
        try:
            if offer_id:
                self.api.signed_write("PUT", f"{OFFERS}/{offer_id}/update", body)
            else:
                # Create wraps the offer in "details"; update takes it bare (site: Dn({details:t}) vs wn(..., t)).
                self.api.signed_write("POST", OFFERS, {"details": body})
        except MarketError as error:
            self._refused(request_id, error)
        log.info("requests: offer %s on %s: $%.2f, %s (%s)", "changed" if offer_id else "sent", request_id, price,
                 delivery, by)
        fresh = self._read_one(request_id)
        with self._lock:
            row = self._merge(fresh)
            row["mine"].update(price=money(price), delivery=delivery, by=by, at=self.clock(), error="")
            self._save()
            return copy.deepcopy(row)

    def withdraw(self, request_id: str) -> dict:
        with self._lock:
            row = self.data["requests"].get(request_id)
            if row is None:
                raise KeyError(request_id)
            offer_id = (row.get("mine") or {}).get("offerId")
        if not offer_id:
            raise ValueError("you have no offer on this request")
        try:
            self.api.signed_write("DELETE", f"{OFFERS}/{offer_id}/delete")
        except MarketError as error:
            self._refused(request_id, error)
        log.info("requests: offer withdrawn on %s", request_id)
        fresh = self._read_one(request_id)
        with self._lock:
            row = self._merge(fresh)
            if not fresh.get("offer"):
                row["mine"] = {"state": row["mine"].get("state", ""), "withdrawn": self.clock()}
            self._save()
            return copy.deepcopy(row)

    def _refused(self, request_id: str, error: MarketError) -> None:
        """Eldorado turned a write down. Read the request again, so one a buyer has already bought ("Cannot
        perform this action on boosting request in state: Bought") leaves the open list. Always raises."""
        try:
            fresh = self._read_one(request_id)
        except MarketError as again:
            if again.status != 404:
                raise error
            fresh = None
        with self._lock:
            if fresh is None:
                row = self.data["requests"].get(request_id) or {}
                row.update(state="Gone", read=self.clock())
            else:
                row = self._merge(fresh)
            self._save()
            state = row.get("state", "")
        if state and state != "Active":
            log.info("requests: %s is %s, write refused: %s", request_id, state, error)
            raise MarketError(closed_text(state), error.status) from error
        raise error

    def _decide(self, row: dict) -> str | None:
        """What the auto sender does with a new request: "simulate", "send" or nothing. Call under the lock."""
        settings = self.data["settings"]
        mode = settings["mode"]
        rule = rule_for(settings, row["category"])
        q = row["quote"]
        if mode == "off" or not rule.get("auto") or not q.get("sure") or q.get("price") is None:
            return None
        if row["spam"] or row["blocked"] or row["state"] != "Active" or row["mine"].get("offerId") \
                or row["id"] in self.data["skipped"]:
            return None
        if row["mine"].get("state") not in ("", "WaitingForOffer"):
            return None
        if row["platform"] and row["platform"] not in settings["platforms"]:
            return None
        if mode == "simulate":
            row["mine"].update(by="simulate", price=q["price"], at=self.clock(),
                               delivery=q.get("delivery") or delivery_for(row["category"], settings))
            return "simulate"
        return "send"

    # --- the loop ---

    def _refresh_offers(self) -> None:
        if self.clock() - self._offers_read < OFFERS_EVERY:
            return
        try:
            # Every offer, sold out and paused too: a request for one says "out of stock" instead of nothing.
            self._offers = self.api.offers()
            self._offers_read = self.clock()
        except MarketError as error:
            log.warning("requests: could not read your offers: %s", error)
            self._offers_read = self.clock() - OFFERS_EVERY + 60

    def _backfill(self) -> None:
        """Walks the whole `received` list (newest first, `cursorValue` pages) for open requests the desk never
        read: the ones sent before it started, or while the server was down. They are read a few per pass."""
        now = self.clock()
        if now - self._backfilled < BACKFILL_SECONDS:
            return
        self._backfilled = now
        with self._lock:
            skip = set(self.data["requests"]) | set(self.data.get("forgotten") or [])
        oldest = now - LIFE_HOURS * 3600
        found, cursor = {}, None
        try:
            for _ in range(BACKFILL_PAGES):
                query = {"pageSize": str(PAGE)}
                if cursor:
                    query["cursorValue"] = cursor
                reply = self.api.signed_read(RECEIVED, query)
                rows = [r for r in (reply.get("results") or []) if isinstance(r, dict) and r.get("id")]
                for row in rows:
                    if row["id"] not in skip and parse_time(row.get("createdDate")) >= oldest:
                        found.setdefault(row["id"], row)
                cursor = reply.get("nextPageCursor")
                if not rows or not cursor or parse_time(rows[-1].get("createdDate")) < oldest:
                    break
        except MarketError as error:
            log.warning("requests: could not walk the request list: %s", error)
            self._backfilled = now - BACKFILL_SECONDS + 120
        if found:
            log.info("requests: %d older requests to read", len(found))
        self._backlog = list(found.values())

    def _read_backlog(self) -> bool:
        """A few of the older requests. They don't go to the auto sender: it answers new requests only."""
        reads = 0
        while self._backlog and reads < BACKLOG_PER_PASS:
            raw = self._backlog.pop(0)
            with self._lock:
                if raw["id"] in self.data["requests"]:
                    continue
            reads += 1
            try:
                fresh = self._read_one(raw["id"], raw)
            except MarketError as error:
                if error.status == 404:
                    with self._lock:
                        self.data["forgotten"] = (self.data.setdefault("forgotten", []) + [raw["id"]])[-FORGET:]
                else:
                    log.warning("requests: could not read %s: %s", raw["id"], error)
                continue
            with self._lock:
                self._merge(fresh)
        return reads > 0

    def poll(self) -> None:
        self._refresh_offers()
        listed = self.api.signed_read(RECEIVED, {"pageSize": str(PAGE)})
        rows = [r for r in (listed.get("results") or []) if isinstance(r, dict) and r.get("id")]
        with self._lock:
            known = set(self.data["requests"])
        changed = False
        for raw in rows:
            if raw["id"] in known:
                continue
            try:
                fresh = self._read_one(raw["id"], raw)
            except MarketError as error:
                log.warning("requests: could not read %s: %s", raw["id"], error)
                continue
            with self._lock:
                row = self._merge(fresh)
                action = self._decide(row)
                price = row["quote"].get("price")
            if action == "send":
                try:
                    self.send(raw["id"], price, by="auto")
                except (MarketError, ValueError) as error:
                    with self._lock:
                        self.data["requests"][raw["id"]]["mine"]["error"] = str(error)[:200]
                    log.warning("requests: auto offer on %s failed: %s", raw["id"], error)
            changed = True
        self._backfill()
        changed = self._read_backlog() or changed
        now = self.clock()
        with self._lock:
            skipped = set(self.data["skipped"]) | set(self.data["hidden"])

            def every(r):
                """How often an open request is read again; a bought one then leaves To answer."""
                if (r.get("mine") or {}).get("offerId"):
                    return REFRESH_SECONDS
                if r["id"] in skipped or r.get("spam"):
                    return None
                return CHECK_SECONDS if now - r.get("created", 0) < 3600 else OLD_CHECK_SECONDS

            due = sorted(
                (r for r in self.data["requests"].values() if r.get("state") == "Active"
                 and every(r) is not None and now - r.get("read", 0) >= every(r)),
                key=lambda r: r.get("read", 0))[:REFRESH_PER_PASS]
        for row in due:
            try:
                fresh = self._read_one(row["id"])
            except MarketError as error:
                if error.status == 404:
                    with self._lock:
                        self.data["requests"][row["id"]]["state"] = "Gone"
                        self.data["requests"][row["id"]]["read"] = now
                    changed = True
                continue
            with self._lock:
                self._merge(fresh)
            changed = True
        with self._lock:
            self._trim()
            if changed:
                self._save()
        self.last_poll = now_iso()
        self.problem = ""

    def start(self) -> threading.Thread:
        def run():
            while not self._stop.is_set():
                try:
                    self.poll()
                except MarketError as error:
                    self.problem = str(error)
                    log.warning("requests: %s", error)
                except Exception as error:  # never take the server down
                    self.problem = "The request desk hit a problem; see the server log."
                    log.exception("requests: %s", error)
                self._stop.wait(self.poll_seconds)

        thread = threading.Thread(target=run, daemon=True, name="requests")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def delivery_minutes(code: str) -> int:
    match = re.fullmatch(r"(Minute|Hour|Day)(\d+)", code or "")
    if not match:
        return 0
    return int(match.group(2)) * {"Minute": 1, "Hour": 60, "Day": 1440}[match.group(1)]


def delivery_for(category: str, settings: dict, size: int = 1) -> str:
    """The delivery time promised for a request: the kind's own time, else your time, and with "auto" a bigger
    job (more levels, more coins) gets the shortest time that covers `size` jobs of your time."""
    rule = rule_for(settings, category)
    if rule.get("delivery"):
        return rule["delivery"]
    base = settings.get("delivery") or "Minute20"
    if settings.get("deliveryMode") != "auto" or size <= 1:
        return base
    want = delivery_minutes(base) * size
    return next((code for code in DELIVERY_TIMES if delivery_minutes(code) >= want), DELIVERY_TIMES[-1])


def closed_text(state: str) -> str:
    if state == "Bought":
        return "A buyer already bought this request, so it is closed. It is gone from To answer."
    if state == "Gone":
        return "This request is gone from Eldorado. It is gone from To answer."
    return f"This request is closed ({state}). It is gone from To answer."


def delivery_text(code: str) -> str:
    match = re.fullmatch(r"(Minute|Hour|Day)(\d+)", code or "")
    if not match:
        return code or ""
    unit, n = match.group(1).lower(), int(match.group(2))
    return f"{n} {unit}{'' if n == 1 else 's'}"
