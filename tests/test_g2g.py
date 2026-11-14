import hashlib
import hmac
import http.client
import json
import os
import tempfile
import unittest

from sellerdesk import dashboard
from sellerdesk.g2g import G2GInbox

URL = "https://example.com/manager/api/g2g/webhook"
USER = "100000"
SECRET = "webhook-secret"
NOW_MS = 1_800_000_000_000


def sign(timestamp, url=URL, secret=SECRET):
    return hmac.new(secret.encode(), (url + USER + timestamp).encode(), hashlib.sha256).hexdigest()


class FakeEngine:
    snapshot = {"orders": []}


class Signature(unittest.TestCase):
    def test_matches_the_example_in_g2g_docs(self):
        inbox = G2GInbox(os.devnull, "https://www.your_webhook_url.com", "100000", "gBW1wXgBNnA",
                         clock=lambda: 1653278884)
        sig = "645343db00f974f5dab1f9e9ed8d4846a265e150291009fe40bfbdf437cfa16a"
        self.assertTrue(inbox.signature_ok(sig, "1653278884000"))
        self.assertTrue(inbox.signature_ok(sig.upper(), "1653278884000"))
        self.assertFalse(inbox.signature_ok(sig, "1653278884001"))

    def test_old_timestamp_is_refused(self):
        inbox = G2GInbox(os.devnull, URL, USER, SECRET, clock=lambda: NOW_MS / 1000)
        old = str(NOW_MS - 16 * 60 * 1000)
        self.assertFalse(inbox.signature_ok(sign(old), old))
        self.assertTrue(inbox.signature_ok(sign(str(NOW_MS)), str(NOW_MS)))


class Webhook(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "g2g-events.jsonl")
        self.inbox = G2GInbox(self.path, URL, USER, SECRET, clock=lambda: NOW_MS / 1000)
        self.server = dashboard.start(FakeEngine(), "127.0.0.1", 0, "sekrit", g2g=self.inbox)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def post(self, body, signature=None, timestamp=str(NOW_MS)):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw)),
                   "g2g-timestamp": timestamp, "g2g-signature": signature or sign(timestamp)}
        conn.request("POST", "/api/g2g/webhook", raw, headers)
        status = conn.getresponse().status
        conn.close()
        return status

    def event(self, event_id="e1"):
        return {"id": event_id, "event_type": "order.created", "event_happened_at": NOW_MS,
                "payload": {"order_id": "1637925438960TE5T-1", "offer_id": "G1", "purchased_qty": 300}}

    def test_signed_event_is_stored_without_the_dashboard_token(self):
        self.assertEqual(self.post(self.event()), 200)
        with open(self.path, encoding="utf-8") as f:
            stored = [json.loads(line) for line in f]
        self.assertEqual(stored[0]["event"]["payload"]["order_id"], "1637925438960TE5T-1")

    def test_same_event_twice_is_stored_once(self):
        self.assertEqual(self.post(self.event()), 200)
        self.assertEqual(self.post(self.event()), 200)
        self.assertEqual(len(self.inbox.events()), 1)
        reopened = G2GInbox(self.path, URL, USER, SECRET, clock=lambda: NOW_MS / 1000)
        self.assertEqual(reopened.receive(sign(str(NOW_MS)), str(NOW_MS), json.dumps(self.event()).encode()), 200)
        self.assertEqual(len(reopened.events()), 1)

    def test_bad_signature_is_refused_and_kept_apart(self):
        self.assertEqual(self.post(self.event(), signature=sign(str(NOW_MS), secret="wrong")), 401)
        self.assertEqual(self.inbox.events(), [])
        with open(os.path.join(self.dir.name, "g2g-rejected.jsonl"), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["reason"], "signature")

    def test_route_is_absent_when_g2g_is_not_configured(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = dashboard.start(FakeEngine(), "127.0.0.1", 0, "sekrit")
        self.port = self.server.server_address[1]
        self.assertEqual(self.post(self.event()), 404)


if __name__ == "__main__":
    unittest.main()
