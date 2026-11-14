"""G2G webhook inbox: checks each event's signature and keeps it on disk.

G2G signs webhooks with HMAC-SHA256(webhook URL + user ID + timestamp, webhook secret). The body itself is not
signed, so an event is a hint: anything that acts on it must confirm the order through G2G's API first.
For now the inbox only stores events, so we can see what G2G sends for game coins and items.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import deque

log = logging.getLogger("sellerdesk")

MAX_EVENTS = 2000         # kept in the inbox file; the oldest go first
MAX_REJECTED = 50         # unsigned or badly signed requests, kept apart for debugging
MAX_SKEW_MS = 15 * 60 * 1000


class G2GInbox:
    def __init__(self, path: str, webhook_url: str, user_id: str, secret: str, clock=time.time):
        self.path = path
        self.rejected_path = os.path.join(os.path.dirname(path) or ".", "g2g-rejected.jsonl")
        self.webhook_url = webhook_url
        self.user_id = user_id
        self.secret = secret
        self.clock = clock
        self._lock = threading.Lock()
        self._events = self._load(self.path, MAX_EVENTS)
        self._seen = {e.get("event", {}).get("id") for e in self._events} - {None}

    @staticmethod
    def _load(path, limit) -> deque:
        items = deque(maxlen=limit)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        items.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        return items

    def signature_ok(self, signature: str, timestamp: str) -> bool:
        if not signature or not timestamp.isdigit():
            return False
        if abs(self.clock() * 1000 - int(timestamp)) > MAX_SKEW_MS:
            return False
        expected = hmac.new(self.secret.encode(), (self.webhook_url + self.user_id + timestamp).encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())

    def receive(self, signature: str, timestamp: str, raw: bytes) -> int:
        """Store one webhook call and return the HTTP status to answer with."""
        now = int(self.clock() * 1000)
        try:
            event = json.loads(raw) if raw.strip() else {}
        except ValueError:
            event = None
        if not self.signature_ok(signature, timestamp):
            # 401 makes G2G resend for about 16 hours, which leaves time to fix a wrong secret or URL.
            self._reject({"received_at": now, "timestamp": timestamp, "reason": "signature",
                          "body": raw[:4096].decode("utf-8", "replace")})
            return 401
        if not isinstance(event, dict):
            self._reject({"received_at": now, "timestamp": timestamp, "reason": "not a JSON object",
                          "body": raw[:4096].decode("utf-8", "replace")})
            return 400
        with self._lock:
            event_id = event.get("id")
            if event_id and event_id in self._seen:
                return 200  # G2G may send an event more than once
            if event_id:
                self._seen.add(event_id)
            self._events.append({"received_at": now, "event": event})
            self._write(self.path, self._events)
        log.info("G2G %s for order %s", event.get("event_type", "?"),
                 (event.get("payload") or {}).get("order_id", "?"))
        return 200

    def _reject(self, entry):
        log.warning("G2G webhook refused: %s", entry["reason"])
        with self._lock:
            rejected = self._load(self.rejected_path, MAX_REJECTED)
            rejected.append(entry)
            self._write(self.rejected_path, rejected)

    @staticmethod
    def _write(path, items):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def events(self) -> list:
        with self._lock:
            return list(self._events)
