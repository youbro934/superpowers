"""End-to-end cycles against the fake (synthetic) providers. Each Engine instance
is a fresh 'process' over the same data directory, like successive cloud runs."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbs.__main__ import _dispatch
from fbs.bankroll import ACTIVE, BUSTED, STOPPED_TARGET
from fbs.config import Settings
from fbs.engine import Engine
from tests.fakes import FakeWorld

SAT = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def cfg(**kw):
    c = Settings()
    c.odds_api_key = "test"
    c.football_data_org_key = ""        # results come from the Odds API scores fake
    c.ntfy_topic = "test-topic"
    c.min_bet_edge = 0.02               # plumbing test, not a threshold test
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.world = FakeWorld(date(2026, 11, 7))
        # plumbing tests: pretend research cleared home wins (real gate comes from `research`)
        eng = Engine(cfg(), self.dir, http=self.world, now=SAT)
        eng.store.set("gate", {"production_allowed": [["h2h", "home", None]]})
        eng.store.commit()
        eng.close()

    def cycle(self, now, c=None):
        eng = Engine(c or cfg(), self.dir, http=self.world, now=now)
        try:
            out = eng.run()
        finally:
            eng.close()
        return out

    def titles(self):
        return [p["title"] for p in self.world.pushes]

    def test_full_day_compound_then_bust(self):
        self.world.add_event("e1", "Alpha FC", "Lima County", SAT + timedelta(hours=4), h2h=(1.38, 5.2, 10.0))
        out = self.cycle(SAT)
        self.assertEqual(out["bankroll"], 0, out["log"])          # whole bankroll staked
        self.assertTrue(any(t.startswith("Betting system started") for t in self.titles()))
        self.assertTrue(any(t.startswith("PAPER BET (simulated, no real money) #1 today: €10.00 @ 1.40") for t in self.titles()), self.titles())

        # re-run before kickoff: no duplicate bet
        out = self.cycle(SAT + timedelta(minutes=30))
        self.assertIn("not scanning: a bet is still open (bets are sequential)", out["log"])

        # result in; second match of the day available
        self.world.scores["e1"] = (2, 0)
        self.world.add_event("e2", "Charlie City", "Juliet Rangers", SAT + timedelta(hours=10), h2h=(1.38, 5.2, 10.0))
        out = self.cycle(SAT + timedelta(hours=7))
        self.assertTrue(any("WIN" in t and "€14.00" in t for t in self.titles()), self.titles())
        self.assertTrue(any(t.startswith("PAPER BET (simulated, no real money) #2 today: €14.00") for t in self.titles()), self.titles())

        # second bet loses: bankroll gone, system stops for good
        self.world.scores["e2"] = (0, 1)
        out = self.cycle(SAT + timedelta(hours=13))
        self.assertEqual(out["state"], BUSTED)
        self.assertEqual(out["bankroll"], 0)
        self.assertIn("Bankroll lost", self.titles())
        self.assertTrue(os.path.exists(os.path.join(self.dir, "index.html")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "fbs.sql")))

    def test_target_stop_requires_exact_continue(self):
        c = cfg(target_stop_cents=1300)
        self.world.add_event("e1", "Alpha FC", "Lima County", SAT + timedelta(hours=4), h2h=(1.38, 5.2, 10.0))
        self.cycle(SAT, c)
        self.world.scores["e1"] = (3, 1)
        self.world.add_event("e2", "Charlie City", "Juliet Rangers", SAT + timedelta(hours=10), h2h=(1.38, 5.2, 10.0))
        out = self.cycle(SAT + timedelta(hours=7), c)
        self.assertEqual(out["state"], STOPPED_TARGET)
        self.assertEqual(out["bankroll"], 1400)                  # no second bet placed
        urgent = [p for p in self.world.pushes if p["title"].startswith("€13.00 TARGET REACHED: €14.00")]
        self.assertEqual(urgent[0]["priority"], "urgent")
        self.assertIn("Net profit: €4.00", urgent[0]["body"])
        out = self.cycle(SAT + timedelta(hours=8), c)
        self.assertEqual(out["state"], STOPPED_TARGET)            # never resumes by itself

        eng = Engine(c, self.dir, http=self.world, now=SAT + timedelta(hours=8))
        self.assertEqual(_dispatch(eng, "continue", ["continue"]), 1)
        self.assertEqual(_dispatch(eng, "continue", ["CONTINUE"]), 0)
        self.assertEqual(eng.bank.status, ACTIVE)
        eng.close()

    def test_crash_recovery_never_double_places(self):
        eng = Engine(cfg(), self.dir, http=self.world, now=SAT)
        eng.bank.init()
        bid = eng.bank.reserve("2026-11-07-s1-a1", "2026-11-07", 1, "paper", 1.35, 0.75, 0.03)
        eng.store.commit()     # process dies here, before placement was confirmed
        eng.close()
        out = self.cycle(SAT + timedelta(minutes=5))
        self.assertIn(f"recovered bet {bid} as not placed", out["log"])
        self.assertEqual(out["bankroll"], 1000)

    def test_provider_outage_alerts_and_no_bet_notice(self):
        self.world.add_event("e1", "Alpha FC", "Lima County", SAT + timedelta(hours=4))
        self.world.fail_odds = True
        self.cycle(SAT)
        self.cycle(SAT + timedelta(hours=1))
        self.assertIn("Data failure: the-odds-api:odds", self.titles())
        late = datetime(2026, 11, 7, 22, 30, tzinfo=timezone.utc)
        self.cycle(late)
        self.cycle(late + timedelta(minutes=20))
        self.assertEqual(sum(t.startswith("NO BET today") for t in self.titles()), 1)

    def test_unvalidated_market_is_shadow_logged_not_staked(self):
        eng = Engine(cfg(), self.dir, http=self.world, now=SAT)
        eng.store.set("gate", None)
        eng.store.x("DELETE FROM meta WHERE key='gate'")
        eng.store.commit()
        eng.close()
        self.world.add_event("e1", "Alpha FC", "Lima County", SAT + timedelta(hours=4), h2h=(1.38, 5.2, 10.0))
        out = self.cycle(SAT)
        self.assertEqual(out["bankroll"], 1000)                  # nothing staked
        self.assertIn("No validated markets yet", self.titles())
        eng = Engine(cfg(), self.dir, http=self.world, now=SAT)
        rows = eng.store.q("SELECT * FROM shadow")
        eng.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market"], "h2h")

    def test_live_mode_refuses_without_adapter(self):
        self.world.add_event("e1", "Alpha FC", "Lima County", SAT + timedelta(hours=4), h2h=(1.38, 5.2, 10.0))
        out = self.cycle(SAT, cfg(execution_mode="live", live_confirmation="I AUTHORIZE REAL MONEY BETTING"))
        self.assertEqual(out["bankroll"], 1000)
        self.assertIn("Execution blocked", self.titles())


if __name__ == "__main__":
    unittest.main()
