import json
import os
import tempfile
import unittest

from sellerdesk.market import MarketError, Offer
from sellerdesk.requests import (RequestDesk, budget_of, clean_settings, coins_of, default_settings, delivery_for,
                                 item_name, level_price, match_items, quote)


def offer(oid, title, price, quantity=10, state="Active"):
    return Offer(id=oid, title=title, price=price, quantity=quantity, state=state, game_id="319", category="CustomItem")


OFFERS = [
    offer("snap", "Snap Hook x1 | ARC Raiders Quick Use | Fast Delivery", 0.36, 20),
    offer("anvil", "Anvil IV x1 | ARC Raiders Weapons | Fast Delivery", 0.54, 12),
    offer("tempest", "Tempest IV x1 | ARC Raiders Weapons", 1.10, 30),
    offer("seeds", "100x Seeds | ARC Raiders", 0.50, 50),
]


def request(category, fields, rid="r1", **extra):
    row = {"id": rid, "category": category, "fields": fields, "buyer": "amy", "text": "", "platform": "",
           "state": "Active", "mine": {}, "spam": False, "blocked": False}
    row.update(extra)
    return row


class Parsing(unittest.TestCase):
    def test_item_name(self):
        self.assertEqual(item_name("Snap Hook x1 | ARC Raiders Quick Use | Fast Delivery"), "Snap Hook")
        self.assertEqual(item_name("Looting Mk. 3 (Survivor) Augment x1 | ARC"), "Looting Mk. 3 Survivor Augment")

    def test_match_items_reads_counts(self):
        found, maybe = match_items("10 SnapHook less than 3", OFFERS)
        self.assertEqual([(f["offer"], f["count"]) for f in found], [("snap", 10)])
        found, _ = match_items("12 Tempest IV Best attachment", OFFERS)
        self.assertEqual([(f["offer"], f["count"]) for f in found], [("tempest", 12)])
        found, _ = match_items("snap hooks x5", OFFERS)
        self.assertEqual(found[0]["count"], 5)

    def test_tier_missing_is_a_near_miss(self):
        found, maybe = match_items("25 Anvils", OFFERS)
        self.assertEqual(found, [])
        self.assertEqual(maybe, ["Anvil IV"])

    def test_bundle_price_is_per_item(self):
        found, _ = match_items("300 seeds", OFFERS)
        self.assertEqual(found[0]["each"], 0.005)
        self.assertEqual(found[0]["stock"], 5000)

    def test_budget_and_coins(self):
        self.assertEqual(budget_of("10 SnapHook less than 3"), 3)
        self.assertEqual(budget_of("need 4 snaphooks"), None)
        self.assertEqual(budget_of("100 herbal 1$ = good review"), 1)
        self.assertEqual(coins_of("I need 1 mil coins"), 1_000_000)
        self.assertEqual(coins_of("500k"), 500_000)
        self.assertEqual(coins_of("1,000,000 coins please"), 1_000_000)
        self.assertIsNone(coins_of("10 turbine"))

    def test_level_price(self):
        bands = [{"upTo": 20, "each": 0.02}, {"upTo": 75, "each": 0.04}]
        self.assertAlmostEqual(level_price(bands, 1, 75), 19 * 0.02 + 55 * 0.04)
        self.assertIsNone(level_price(bands, 70, 80))   # level 76+ has no price
        self.assertIsNone(level_price([], 1, 10))


class Quotes(unittest.TestCase):
    def setUp(self):
        self.settings = clean_settings({"minPrice": 0.5, "categories": {
            "Power Leveling": {"bands": [{"upTo": 75, "each": 0.0324}]},
            "Coins Farm": {"perMillion": 2.5},
        }})

    def test_levels(self):
        q = quote(request("Power Leveling", [["Current level", "1"], ["Desired level", "75"], ["Platform", "PC"]]),
                  self.settings, OFFERS)
        self.assertEqual(q["price"], 2.4)
        self.assertTrue(q["sure"])

    def test_levels_without_price_waits_for_you(self):
        q = quote(request("Stash Upgrade", [["Current level", "1"], ["Desired level", "10"]]), self.settings, OFFERS)
        self.assertIsNone(q["price"])
        self.assertIn("Add them in Prices", q["why"])

    def test_coins(self):
        q = quote(request("Coins Farm", [["Request description", "I need 2M coins"]]), self.settings, OFFERS)
        self.assertEqual(q["price"], 5.0)

    def test_items_sure_only_when_plain(self):
        q = quote(request("Custom Request", [["Platform", "PC"], ["Request description", "12 Tempest IV"]]),
                  self.settings, OFFERS)
        self.assertEqual((q["price"], q["sure"]), (13.2, True))
        q = quote(request("Custom Request", [["Request description", "10 SnapHook less than 3"]]), self.settings, OFFERS)
        self.assertEqual(q["price"], 3.6)
        self.assertFalse(q["sure"])
        self.assertIn("under $3", q["why"])
        q = quote(request("Custom Request", [["Request description", "12 Tempest IV and 3 Ferro"]]), self.settings, OFFERS)
        self.assertFalse(q["sure"])   # "3 Ferro" isn't yours: a person checks
        q = quote(request("Custom Request", [["Request description", "40 tempest iv"]]), self.settings, OFFERS)
        self.assertFalse(q["sure"])
        self.assertIn("stock", q["why"])

    def test_min_price(self):
        q = quote(request("Custom Request", [["Request description", "1 snap hook"]]), self.settings, OFFERS)
        self.assertEqual(q["price"], 0.5)

    def test_review_kinds(self):
        q = quote(request("Trials", [["Request description", "do my trials"]]), self.settings, OFFERS)
        self.assertIsNone(q["price"])

    def test_delivery_fixed_or_by_job_size(self):
        levels = request("Power Leveling", [["Current level", "1"], ["Desired level", "75"]])
        small = request("Power Leveling", [["Current level", "1"], ["Desired level", "20"]])
        coins = request("Coins Farm", [["Request description", "I need 2M coins"]])
        settings = clean_settings({"delivery": "Hour1", "categories": self.settings["categories"]})
        self.assertEqual(quote(levels, settings, OFFERS)["delivery"], "Hour1")            # fixed: always yours
        settings = clean_settings({"deliveryMode": "auto"}, settings)
        self.assertEqual(quote(small, settings, OFFERS)["delivery"], "Hour1")             # one job
        self.assertEqual(quote(levels, settings, OFFERS)["delivery"], "Hour3")            # 74 levels = 3 jobs
        self.assertEqual(quote(coins, settings, OFFERS)["delivery"], "Hour2")             # 2M = 2 jobs
        settings["categories"]["Power Leveling"]["delivery"] = "Day1"                     # the kind's own time wins
        self.assertEqual(delivery_for("Power Leveling", settings, 3), "Day1")
        with self.assertRaises(ValueError):
            clean_settings({"deliveryMode": "fast"})


class Settings(unittest.TestCase):
    def test_refuses_bad_values(self):
        for bad in ({"mode": "on"}, {"delivery": "Hour4"}, {"minPrice": -1}, {"platforms": ["Switch"]},
                    {"categories": {"Power Leveling": {"bands": [{"upTo": 0, "each": 1}]}}},
                    {"categories": {"Power Leveling": {"auto": "yes"}}}, {"message": "x" * 600}):
            with self.assertRaises(ValueError, msg=bad):
                clean_settings(bad)

    def test_defaults_are_off(self):
        settings = default_settings()
        self.assertEqual(settings["mode"], "off")
        self.assertFalse(any(rule["auto"] for rule in settings["categories"].values()))


class FakeApi:
    def __init__(self):
        self.requests = {}
        self.writes = []
        self.offer_ids = {}

    def offers(self):
        return OFFERS

    def signed_read(self, path, query=None):
        if path.endswith("/received"):
            return {"results": [{"id": rid, "createdDate": r["createdDate"], "buyerUsername": "amy"}
                                for rid, r in self.requests.items()]}
        rid = path.rsplit("/", 1)[-1]
        if rid not in self.requests:
            raise MarketError("gone", 404)
        raw = dict(self.requests[rid])
        if rid in self.offer_ids:
            raw["offers"] = [{"id": self.offer_ids[rid], "pricing": {"pricePerUnit": {"amount": 2.4}},
                              "offerDetails": {"guaranteedDeliveryTime": "Minute20"}}]
            raw["sellerDetails"] = {"boostingRequestSellerState": "OfferPlaced"}
        return raw

    def signed_write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if method == "POST":
            rid = body["details"]["boostingRequestId"]
            self.offer_ids[rid] = "o-" + rid
        if method == "DELETE":
            self.offer_ids = {k: v for k, v in self.offer_ids.items() if not path.startswith(f"/api/boostingOffers/{v}/")}
        return "{}"


def raw_request(category, values, state="Active"):
    return {"gameName": "Arc Raiders", "gameId": "319", "gameCategoryTitle": category, "state": state,
            "createdDate": "2099-01-01T00:00:00Z", "availableSellersCount": 770, "viewersCount": 5,
            "buyerInfo": {"user": {"username": "amy", "id": "b1"}, "userOrderInfo": {"ratingCount": 3}},
            "sellerDetails": {"boostingRequestSellerState": "WaitingForOffer"},
            "boostingRequestDetails": {"descriptionValues": [{"label": l, "value": v} for l, v in values]}}


class Desk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "requests.json")
        self.api = FakeApi()
        self.desk = RequestDesk(self.api, self.path, clock=lambda: 4_070_908_800.0 + 60)  # 2099-01-01 + 1 min
        self.desk.set_settings({"categories": {"Power Leveling": {"auto": True, "bands": [{"upTo": 75, "each": 0.0324}]}},
                                "message": "Hi {buyer}, {price} in {delivery}."})
        self.api.requests["pl1"] = raw_request("Power Leveling", [("Current level", "1"), ("Desired level", "75"),
                                                                  ("Platform", "PC")])
        self.api.requests["spam"] = raw_request("Custom Request", [("Request description", "SnapBid automates your requests, snapbid.net")])

    def tearDown(self):
        self.dir.cleanup()

    def test_off_sends_nothing(self):
        self.desk.poll()
        self.assertEqual(self.api.writes, [])
        rows = {r["id"]: r for r in self.desk.snapshot()["requests"]}
        self.assertEqual(rows["pl1"]["quote"]["price"], 2.4)
        self.assertTrue(rows["spam"]["spam"])

    def test_simulate_marks_without_sending(self):
        self.desk.set_settings({"mode": "simulate"})
        self.desk.poll()
        self.assertEqual(self.api.writes, [])
        snap = self.desk.snapshot()
        self.assertEqual({r["id"]: r["mine"].get("by") for r in snap["requests"]}["pl1"], "simulate")
        self.assertEqual(snap["stats"]["simulated"], 1)

    def test_live_sends_once_with_the_site_body(self):
        self.desk.set_settings({"mode": "live"})
        self.desk.poll()
        self.desk.poll()
        self.assertEqual(len(self.api.writes), 1)
        method, path, body = self.api.writes[0]
        self.assertEqual((method, path), ("POST", "/api/boostingOffers"))
        self.assertEqual(list(body), ["details"])
        body = body["details"]
        self.assertEqual(body["boostingRequestId"], "pl1")
        self.assertEqual(body["pricing"]["pricePerUnit"], {"amount": 2.4, "currency": "USD"})
        self.assertEqual(body["guaranteedDeliveryTime"], "Minute20")
        self.assertEqual(body["message"], "Hi amy, $2.4 in 20 minutes.")
        row = {r["id"]: r for r in self.desk.snapshot()["requests"]}["pl1"]
        self.assertEqual((row["mine"]["offerId"], row["mine"]["by"]), ("o-pl1", "auto"))

    def test_category_switch_and_platform_filter(self):
        self.desk.set_settings({"mode": "live", "platforms": ["PlayStation"]})
        self.desk.poll()
        self.assertEqual(self.api.writes, [])

    def test_manual_send_change_and_withdraw(self):
        self.desk.poll()
        self.desk.send("pl1", 3, "Hour1", "")
        self.desk.send("pl1", 2.8, "Hour1", "")
        self.assertEqual([w[0] for w in self.api.writes], ["POST", "PUT"])
        self.assertEqual(self.api.writes[1][1], "/api/boostingOffers/o-pl1/update")
        row = self.desk.withdraw("pl1")
        self.assertEqual(self.api.writes[2][:2], ("DELETE", "/api/boostingOffers/o-pl1/delete"))
        self.assertNotIn("offerId", row["mine"])
        with self.assertRaises(ValueError):
            self.desk.withdraw("pl1")
        with self.assertRaises(KeyError):
            self.desk.send("nope", 1)

    def test_bought_request_leaves_the_open_list(self):
        self.desk.poll()
        self.api.requests["pl1"]["state"] = "Bought"

        def refuse(method, path, body=None):
            raise MarketError("Eldorado said: Cannot perform this action on boosting request in state: Bought.", 400)

        self.api.signed_write = refuse
        with self.assertRaises(MarketError) as caught:
            self.desk.send("pl1", 3, "Hour1", "")
        self.assertIn("already bought", str(caught.exception))
        row = {r["id"]: r for r in self.desk.snapshot()["requests"]}["pl1"]
        self.assertEqual(row["state"], "Bought")

    def test_other_refusals_pass_through(self):
        self.desk.poll()

        def refuse(method, path, body=None):
            raise MarketError("price too low", 400)

        self.api.signed_write = refuse
        with self.assertRaises(MarketError) as caught:
            self.desk.send("pl1", 3, "Hour1", "")
        self.assertEqual(str(caught.exception), "price too low")

    def test_old_open_requests_are_read_again(self):
        self.desk.poll()
        self.api.requests["pl1"]["state"] = "Bought"
        self.desk.clock = lambda: 4_070_908_800.0 + 7200        # two hours on: past the young window
        self.desk.poll()
        row = {r["id"]: r for r in self.desk.snapshot()["requests"]}["pl1"]
        self.assertEqual(row["state"], "Bought")

    def test_skip_keeps_auto_away_and_survives_reload(self):
        self.desk.poll()
        self.desk.skip("pl1")
        row = {r["id"]: r for r in self.desk.snapshot()["requests"]}["pl1"]
        self.assertTrue(row["skipped"])
        self.desk.set_settings({"mode": "live"})
        self.assertEqual(self.desk._decide(dict(row, mine={"state": ""})), None)
        self.desk.skip("pl1", False)
        self.assertEqual(self.desk._decide(dict(row, mine={"state": ""})), "send")
        self.desk.skip("pl1")
        again = RequestDesk(self.api, self.path, clock=lambda: 4_070_908_800.0 + 60)
        self.assertTrue({r["id"]: r for r in again.snapshot()["requests"]}["pl1"]["skipped"])
        self.desk.skip("pl1", False)
        self.assertFalse({r["id"]: r for r in self.desk.snapshot()["requests"]}["pl1"]["skipped"])

    def test_saved_and_reloaded(self):
        self.desk.set_settings({"mode": "simulate"})
        self.desk.poll()
        again = RequestDesk(self.api, self.path, clock=lambda: 4_070_908_800.0 + 60)
        self.assertEqual(again.snapshot()["settings"]["mode"], "simulate")
        self.assertEqual(len(again.snapshot()["requests"]), 2)
        with open(self.path, encoding="utf-8") as file:
            self.assertIn("settings", json.load(file))


if __name__ == "__main__":
    unittest.main()
