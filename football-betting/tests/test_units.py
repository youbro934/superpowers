import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbs.bankroll import ACTIVE, BUSTED, STOPPED_TARGET, Bankroll, BankrollError
from fbs.config import Settings
from fbs.db import Store
from fbs.decision import Leg, consensus_probs, decide
from fbs.models import fit_league, goal_market_probs, score_matrix
from fbs.providers.history import parse_csv, season_codes
from fbs.providers.odds_api import Quote, parse_event
from fbs.settlement import bet_outcome, leg_result
from fbs.teams import canonical, is_favorite, match_team
from tests.fakes import STRENGTH, synthetic_csv


class TeamTests(unittest.TestCase):
    def test_aliases_across_providers(self):
        for a, b in [("Sp Lisbon", "Sporting CP"), ("Sporting Clube de Portugal", "Sporting Lisbon"),
                     ("Sport Lisboa e Benfica", "Benfica"), ("FC Porto", "Porto"), ("Paris SG", "Paris Saint-Germain"),
                     ("FC Bayern München", "Bayern Munich"), ("FC Barcelona", "Barcelona"),
                     ("Real Madrid CF", "Real Madrid"), ("CR Flamengo", "Flamengo"), ("Flamengo RJ", "Flamengo"),
                     ("Man United", "Manchester United")]:
            self.assertEqual(canonical(a), canonical(b), (a, b))

    def test_distinct_clubs_stay_distinct(self):
        self.assertNotEqual(canonical("Sporting Braga"), canonical("Sporting CP"))
        self.assertNotEqual(canonical("Paris FC"), canonical("Paris SG"))
        self.assertNotEqual(canonical("Real Madrid"), canonical("Real Sociedad"))

    def test_favorites(self):
        for t in ["FC Barcelona", "Real Madrid", "Bayern Munich", "PSG", "Benfica", "Sporting CP", "FC Porto",
                  "Flamengo"]:
            self.assertTrue(is_favorite(t), t)
        self.assertFalse(is_favorite("Getafe"))

    def test_match_refuses_ambiguous(self):
        self.assertIsNone(match_team("Unknown Club", {"alpha", "bravo"}))
        self.assertEqual(match_team("Bravo United FC", {"alpha", "bravo united"}), "bravo united")


class ModelTests(unittest.TestCase):
    def test_score_matrix_is_a_distribution(self):
        m = score_matrix(1.6, 1.1, -0.08)
        self.assertAlmostEqual(sum(map(sum, m)), 1.0, places=9)
        p = goal_market_probs(m)
        self.assertAlmostEqual(p[("h2h", "home", None)] + p[("h2h", "draw", None)] + p[("h2h", "away", None)], 1, 9)
        self.assertAlmostEqual(p[("totals", "over", 2.5)] + p[("totals", "under", 2.5)], 1, 9)
        self.assertGreater(p[("double_chance", "home_draw", None)], p[("h2h", "home", None)])

    def test_fit_recovers_synthetic_strengths(self):
        today = date(2026, 11, 1)
        recs = parse_csv(synthetic_csv(today, seasons=3), extra=False)
        model = fit_league(recs, today, xi=0.0005)
        self.assertIsNotNone(model)
        self.assertAlmostEqual(model.goals.home, 1.25, delta=0.12)
        # strongest synthetic attack should rate above the weakest
        best, worst = canonical("Alpha FC"), canonical("Lima County")
        self.assertGreater(model.goals.attack[best], model.goals.attack[worst] * 1.4)
        self.assertLess(model.goals.weakness[best], model.goals.weakness[worst])
        self.assertIsNotNone(model.corners)

    def test_season_codes(self):
        self.assertEqual(season_codes(date(2026, 11, 8), 2), ["2627", "2526"])
        self.assertEqual(season_codes(date(2026, 3, 1), 1), ["2526"])


class OddsParseTests(unittest.TestCase):
    def test_parse_markets(self):
        raw = {"id": "e1", "home_team": "Benfica", "away_team": "FC Porto", "commence_time": "2026-11-09T20:00:00Z",
               "bookmakers": [{"key": "b", "markets": [
                   {"key": "h2h", "outcomes": [{"name": "Benfica", "price": 2.1}, {"name": "Draw", "price": 3.3},
                                               {"name": "FC Porto", "price": 3.6}]},
                   {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 2.5},
                                                  {"name": "Under", "price": 1.9, "point": 2.5},
                                                  {"name": "Over", "price": 1.5, "point": 2.0}]},
                   {"key": "double_chance", "outcomes": [{"name": "Benfica/Draw", "price": 1.3}]},
                   {"key": "btts", "outcomes": [{"name": "Yes", "price": 1.8}]}]}]}
        ev = parse_event(raw, "soccer_portugal_primeira_liga")
        got = {(q.market, q.selection, q.line) for q in ev.quotes}
        self.assertIn(("h2h", "away", None), got)
        self.assertIn(("double_chance", "home_draw", None), got)
        self.assertIn(("btts", "yes", None), got)
        self.assertNotIn(("totals", "over", 2.0), got)   # integer line skipped

    def test_devig(self):
        qs = [Quote("h2h", "home", None, "b", 2.0), Quote("h2h", "draw", None, "b", 3.5),
              Quote("h2h", "away", None, "b", 4.0)]
        p = consensus_probs(qs)
        self.assertAlmostEqual(p[("h2h", "home", None)] + p[("h2h", "draw", None)] + p[("h2h", "away", None)], 1)
        self.assertAlmostEqual(p[("double_chance", "home_draw", None)],
                               p[("h2h", "home", None)] + p[("h2h", "draw", None)])


def _leg(eid, odds, p, ev=None, market="h2h", sel="home"):
    ev = ev if ev is not None else p * odds - 1
    return Leg(eid, "s", "PL", "a", "b", "2026-11-09T20:00:00+00:00", market, sel, None, odds, "bk",
               p, p, p, ev, False)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()

    def test_single_in_range(self):
        d = decide([_leg("e1", 1.35, 0.79)], self.cfg)
        self.assertIsNotNone(d.candidate)
        self.assertEqual(len(d.candidate.legs), 1)

    def test_combo_reaches_target_range(self):
        d = decide([_leg("e1", 1.16, 0.91), _leg("e2", 1.17, 0.90)], self.cfg)
        self.assertIsNotNone(d.candidate)
        self.assertEqual(d.candidate.kind, "combo2")
        self.assertTrue(1.30 <= d.candidate.odds <= 1.40)

    def test_no_same_match_combo(self):
        d = decide([_leg("e1", 1.16, 0.91), _leg("e1", 1.17, 0.90, market="totals", sel="over")], self.cfg)
        self.assertIsNone(d.candidate)

    def test_no_bet_when_out_of_range(self):
        d = decide([_leg("e1", 1.9, 0.6)], self.cfg)
        self.assertIsNone(d.candidate)
        self.assertTrue(d.reason.startswith("NO BET"))

    def test_no_bet_without_edge(self):
        self.assertIsNone(decide([], self.cfg).candidate)
        # combined edge (about +2.1%) below the 3% bet threshold
        d = decide([_leg("e1", 1.16, 0.87), _leg("e2", 1.17, 0.865)], self.cfg)
        self.assertIsNone(d.candidate)


class SettlementTests(unittest.TestCase):
    def test_markets(self):
        self.assertEqual(leg_result("h2h", "home", None, 2, 1), "won")
        self.assertEqual(leg_result("h2h", "draw", None, 2, 1), "lost")
        self.assertEqual(leg_result("double_chance", "draw_away", None, 1, 1), "won")
        self.assertEqual(leg_result("totals", "over", 2.5, 2, 1), "won")
        self.assertEqual(leg_result("totals", "under", 2.5, 2, 1), "lost")
        self.assertEqual(leg_result("btts", "no", None, 0, 3), "won")
        self.assertIsNone(leg_result("corners_totals", "over", 9.5, 1, 0, None))
        self.assertEqual(leg_result("corners_totals", "over", 9.5, 1, 0, 11), "won")

    def test_bet_outcome(self):
        self.assertEqual(bet_outcome([{"result": "won", "odds": 1.2}, {"result": "pending", "odds": 1.1}])[0], None)
        self.assertEqual(bet_outcome([{"result": "lost", "odds": 1.2}, {"result": "pending", "odds": 1.1}])[0], "LOST")
        o, odds = bet_outcome([{"result": "won", "odds": 1.2}, {"result": "void", "odds": 1.1}])
        self.assertEqual(o, "WON")
        self.assertAlmostEqual(odds, 1.2)


class BankrollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(self.tmp)
        self.bank = Bankroll(self.store, Settings())
        self.bank.init()

    def tearDown(self):
        self.store.close()

    def _bet(self, day, slot, odds, outcome):
        bid = self.bank.reserve(f"{day}-s{slot}", day, slot, "paper", odds, 0.75, 0.04)
        self.bank.confirm(bid, "x")
        return self.bank.settle(bid, outcome, odds)

    def test_full_return_reinvestment(self):
        self.assertEqual(self.bank.balance, 1000)
        self._bet("2026-11-01", 1, 1.40, "WON")
        self.assertEqual(self.bank.balance, 1400)
        bid = self.bank.reserve("2026-11-01-s2", "2026-11-01", 2, "paper", 1.40, 0.75, 0.04)
        self.assertEqual(self.store.one("SELECT stake_cents FROM bets WHERE id=?", (bid,))[0], 1400)
        self.bank.confirm(bid, "x")
        self.bank.settle(bid, "WON", 1.40)
        self.assertEqual(self.bank.balance, 1960)

    def test_only_one_deposit_ever(self):
        self.assertFalse(self.bank.init())
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM ledger WHERE kind='DEPOSIT'")[0], 1)
        cfg = Settings()
        cfg.initial_bankroll_cents = 5000
        s2 = Store(tempfile.mkdtemp())
        b2 = Bankroll(s2, cfg)
        b2.init()
        self.assertEqual(b2.balance, 1000)   # capped at EUR 10
        s2.close()

    def test_sequential_and_daily_limit(self):
        bid = self.bank.reserve("d-s1", "2026-11-01", 1, "paper", 1.35, 0.75, 0.04)
        self.bank.confirm(bid, "x")
        with self.assertRaises(BankrollError):
            self.bank.reserve("d-s2", "2026-11-01", 2, "paper", 1.35, 0.75, 0.04)
        self.bank.settle(bid, "WON", 1.35)
        self._bet("2026-11-01", 2, 1.35, "WON")
        ok, why = self.bank.can_bet("2026-11-01")
        self.assertFalse(ok)
        self.assertIn("daily limit", why)
        self.assertTrue(self.bank.can_bet("2026-11-02")[0])

    def test_loss_busts(self):
        self._bet("2026-11-01", 1, 1.35, "LOST")
        self.assertEqual(self.bank.balance, 0)
        self.assertEqual(self.bank.status, BUSTED)
        self.assertFalse(self.bank.can_bet("2026-11-02")[0])

    def test_target_stop_and_exact_continue(self):
        day = date(2026, 1, 1)
        while self.bank.status == ACTIVE:
            self._bet(day.isoformat(), 1, 1.40, "WON")
            day += timedelta(days=1)
        self.assertEqual(self.bank.status, STOPPED_TARGET)
        self.assertGreaterEqual(self.bank.balance, 50000)
        self.assertFalse(self.bank.can_bet(day.isoformat())[0])
        self.assertFalse(self.bank.resume("continue"))
        self.assertFalse(self.bank.resume("CONTINUE please"))
        self.assertEqual(self.bank.status, STOPPED_TARGET)
        self.assertTrue(self.bank.resume("CONTINUE"))
        self.assertEqual(self.bank.status, ACTIVE)
        self._bet(day.isoformat(), 1, 1.40, "WON")   # does not re-stop for the same crossing
        self.assertEqual(self.bank.status, ACTIVE)

    def test_failed_placement_refunds(self):
        bid = self.bank.reserve("d-s1", "2026-11-01", 1, "paper", 1.35, 0.75, 0.04)
        self.assertEqual(self.bank.balance, 0)
        self.bank.fail(bid, "boom")
        self.assertEqual(self.bank.balance, 1000)
        self.assertTrue(self.bank.can_bet("2026-11-01")[0])

    def test_state_survives_sql_dump(self):
        self._bet("2026-11-01", 1, 1.40, "WON")
        self.store.commit()
        self.store.close()
        self.store = Store(self.tmp)
        again = Bankroll(self.store, Settings())
        self.assertEqual(again.balance, 1400)


if __name__ == "__main__":
    unittest.main()
