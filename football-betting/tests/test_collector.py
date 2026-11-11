"""API-Football collector against a fake API (SYNTHETIC responses)."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbs.db import Store
from fbs.research.collector import Collector
from fbs.util import Response

NOW = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


class FakeAPIF:
    def __init__(self):
        self.calls = []

    def __call__(self, url, params=None, headers=None, provider="", **kw):
        self.calls.append((url.rsplit("/", 1)[-1] if "statistics" not in url else "statistics", dict(params or {})))
        if url.endswith("/fixtures"):
            d = params["date"]
            fx = [
                {"fixture": {"id": 1, "date": f"{d}T19:00:00+00:00", "referee": "R. Claus",
                             "status": {"short": "FT" if d < "2026-11-07" else "NS"}},
                 "league": {"id": 71}, "teams": {"home": {"name": "Flamengo"}, "away": {"name": "Palmeiras"}},
                 "goals": {"home": 2, "away": 1}, "score": {"halftime": {"home": 1, "away": 0}}},
                {"fixture": {"id": 2, "date": f"{d}T20:00:00+00:00", "referee": None, "status": {"short": "NS"}},
                 "league": {"id": 999}, "teams": {"home": {"name": "X"}, "away": {"name": "Y"}}},
            ]
            for f in fx:
                f["fixture"]["id"] += {"2026-11-06": 100, "2026-11-07": 0, "2026-11-08": 200}[d]
            return Response(200, {}, json.dumps({"errors": [], "response": fx}).encode())
        if url.endswith("/odds"):
            return Response(200, {}, json.dumps({"errors": [], "response": [{"bookmakers": [{"name": "Bet365", "bets": [
                {"name": "Corners Over Under", "values": [{"value": "Over 9.5", "odd": "1.85"}]},
                {"name": "Cards Over/Under", "values": [{"value": "Over 4.5", "odd": "2.10"}]}]}]}]}).encode())
        if url.endswith("/fixtures/statistics"):
            return Response(200, {}, json.dumps({"errors": [], "response": [
                {"team": {"name": "Flamengo"}, "statistics": [{"type": "Corner Kicks", "value": 7},
                                                              {"type": "Fouls", "value": None},
                                                              {"type": "Ball Possession", "value": "58%"},
                                                              {"type": "expected_goals", "value": "1.92"}]},
                {"team": {"name": "Palmeiras"}, "statistics": [{"type": "Corner Kicks", "value": 3}]}]}).encode())
        return Response(200, {}, json.dumps({"errors": [], "response": []}).encode())


class CollectorTests(unittest.TestCase):
    def test_collects_priority_odds_and_stats_with_provenance(self):
        d = tempfile.mkdtemp()
        store = Store(d)
        api = FakeAPIF()
        c = Collector("k", os.path.join(d, "arch"), store, http=api, now=NOW)
        done = c.run()
        self.assertEqual(done["stats"], 1)          # yesterday's Flamengo match
        self.assertGreaterEqual(done["odds_open"], 1)
        with gzip.open(os.path.join(d, "arch", "2026-11-07.jsonl.gz"), "rt") as f:
            recs = [json.loads(l) for l in f]
        odds = [r for r in recs if r["kind"] == "odds"]
        self.assertTrue(any(r["bet"] == "Corners Over Under" and r["odd"] == 1.85 for r in odds))
        self.assertTrue(all(r["source"] == "api-football" and r["fetched_at"] for r in recs))
        st = [r for r in recs if r["kind"] == "stats"][0]
        self.assertEqual(st["stats"]["home_corners"], 7.0)
        self.assertEqual(st["stats"]["home_possession"], 58.0)
        self.assertNotIn("home_fouls", st["stats"])     # missing is never filled in
        self.assertFalse(any(r.get("league") == 999 for r in recs))   # non-priority league skipped
        # second pass: no second "open" snapshot; kick-off is 1 h away so the "close" snapshot is due
        n_calls = len(api.calls)
        c.run()
        self.assertEqual(sum(1 for k, _ in api.calls[n_calls:] if k == "odds"), 1)
        self.assertTrue(store.get("apif_odds:1:close"))
        n_calls = len(api.calls)
        c.run()
        self.assertEqual(sum(1 for k, _ in api.calls[n_calls:] if k == "odds"), 0)
        store.close()

    def test_budget_stops_requests(self):
        d = tempfile.mkdtemp()
        store = Store(d)
        api = FakeAPIF()
        c = Collector("k", os.path.join(d, "arch"), store, http=api, now=NOW, daily_budget=2)
        try:
            c.run()
        except Exception:
            pass
        self.assertLessEqual(len(api.calls), 2)
        store.close()


if __name__ == "__main__":
    unittest.main()
