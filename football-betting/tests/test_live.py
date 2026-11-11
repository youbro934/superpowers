"""Live multi-market path on SYNTHETIC history and fake API-Football responses."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbs.config import Settings
from fbs.decision import Leg
from fbs.engine import Engine
from fbs.live.odds import map_bet, prices
from fbs.live.optimizer import optimize
from fbs.live.probs import probability
from fbs.live.settle import settle_leg
from fbs.util import ProviderError, Response
from tests.fakes import synthetic_full_csv

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


class UnitTests(unittest.TestCase):
    def test_bet_name_mapping_is_conservative(self):
        self.assertEqual(map_bet("Goals Over/Under", "Over 0.5"), ("goals_total", "over", 0.5))
        self.assertEqual(map_bet("Corners Over Under", "Over 2.5"), ("corners_total", "over", 2.5))
        self.assertEqual(map_bet("Home Team Total Cards", "Over 0.5"), ("team_cards", "home_over", 0.5))
        self.assertEqual(map_bet("Double Chance", "Home/Draw"), ("double_chance", "1X", None))
        self.assertEqual(map_bet("Total - Away", "Under 1.5"), ("team_goals", "away_under", 1.5))
        self.assertIsNone(map_bet("Goals Over/Under", "Over 2"))          # whole line can push
        self.assertIsNone(map_bet("Asian Handicap", "Home -1"))
        self.assertIsNone(map_bet("Corners 1x2", "Home"))
        self.assertIsNone(map_bet("Some New Market", "Yes"))

    def test_prices_best_and_devig_with_timestamp(self):
        snap = {"fetched_at": "2026-11-07T09:00:00Z", "books": {
            "A": {"Goals Over/Under": {"Over 2.5": 1.9, "Under 2.5": 1.9}},
            "B": {"Goals Over/Under": {"Over 2.5": 2.0, "Under 2.5": 1.8}, "Weird": {"X": 3.0}}}}
        q, unmapped = prices(snap)
        p = q[("goals_total", "over", 2.5)]
        self.assertEqual((p.best, p.bookmaker, p.n_books, p.fetched_at), (2.0, "B", 2, "2026-11-07T09:00:00Z"))
        self.assertAlmostEqual(p.fair_p, (0.5 + (1 / 2.0) / (1 / 2.0 + 1 / 1.8)) / 2, 9)
        self.assertEqual(unmapped, {"Weird": 1})

    def test_probabilities_cover_markets(self):
        params = {"dc": [1.6, 1.1, -0.05], "glm": {"goals": [1.6, 1.1, 0.0], "corners": [5.5, 4.4, 0.08],
                                                   "yellows": [1.8, 2.1, 0.0], "reds": [0.08, 0.1, 0.0],
                                                   "fouls": [11, 12, 0.02], "ht_goals": [0.7, 0.5, 0.0]}}
        self.assertGreater(probability(params, "goals_total", "over", 0.5), 0.9)
        self.assertGreater(probability(params, "corners_total", "over", 2.5), 0.98)
        a = probability(params, "team_cards", "home_over", 0.5)
        self.assertTrue(0.8 < a < 0.9)
        dc = probability(params, "double_chance", "1X", None)
        self.assertAlmostEqual(dc, probability(params, "result", "home", None) +
                               probability(params, "result", "draw", None), 9)
        self.assertIsNone(probability({"glm": {}}, "corners_total", "over", 9.5))   # no model: no number

    def test_settlement_needs_published_stats(self):
        r = {"home_goals": 2, "away_goals": 0, "ht_home": 1, "ht_away": 0,
             "stats": json.dumps({"home_corners": 6, "away_corners": 3, "home_yellows": 1, "away_yellows": 2})}
        self.assertEqual(settle_leg("corners_total", "over", 8.5, r), "won")
        self.assertEqual(settle_leg("team_cards", "home_over", 0.5, r), "won")
        self.assertEqual(settle_leg("double_chance", "X2", None, r), "lost")
        self.assertEqual(settle_leg("btts", "no", None, r), "won")
        self.assertIsNone(settle_leg("fouls_total", "over", 20.5, r))   # fouls not published: wait

    def _leg(self, ev_id, fam, odds, p, status="PASSED"):
        return Leg(ev_id, "", "PL", "a", "b", "2026-11-07T14:00:00+00:00", fam, "over", 0.5, odds, "bk",
                   p, None, p, p * odds - 1, False, status)

    def test_optimizer_cross_match_cross_market(self):
        cfg = Settings()
        legs = [self._leg("apif:1", "goals_total", 1.16, 0.93), self._leg("apif:2", "corners_total", 1.17, 0.92),
                self._leg("apif:1", "team_cards", 1.17, 0.93)]
        res = optimize(legs, cfg)
        self.assertIsNotNone(res.staked)
        self.assertEqual(len({l.event_id for l in res.staked.legs}), len(res.staked.legs))   # one per match
        self.assertTrue(1.30 <= res.staked.odds <= 1.40)
        self.assertAlmostEqual(res.staked.prob, res.staked.legs[0].p_used * res.staked.legs[1].p_used, 3)

    def test_fourth_leg_only_when_needed(self):
        cfg = Settings()
        legs = [self._leg(f"apif:{i}", "goals_total", 1.08, 0.96) for i in range(4)]   # 3 legs = 1.26, 4 = 1.36
        res = optimize(legs, cfg)
        self.assertEqual(len(res.staked.legs), 4)
        legs.append(self._leg("apif:9", "corners_total", 1.17, 0.93))
        res = optimize(legs, cfg)
        self.assertLessEqual(len(res.staked.legs), 3)

    def test_shadow_legs_never_staked(self):
        cfg = Settings()
        legs = [self._leg("apif:1", "goals_total", 1.16, 0.93), self._leg("apif:2", "corners_total", 1.17, 0.92, "SHADOW")]
        res = optimize(legs, cfg)
        self.assertIsNone(res.staked)
        self.assertIsNotNone(res.shadow)
        self.assertIn("SHADOW", res.reason)


class LiveWorld:
    """football-data.co.uk history (synthetic) + API-Football fake with today's fixtures and odds."""

    def __init__(self, odds_by_fixture):
        self.csv = synthetic_full_csv(NOW.date(), seasons=3)
        self.odds = odds_by_fixture
        self.final = False
        self.pushes = []

    def fixtures(self, d):
        out = []
        if d == "2026-11-07":
            for fid, (h, a, hh) in {1: ("Alpha FC", "Lima County", 14), 2: ("Bravo United", "Kilo Borough", 15)}.items():
                done = self.final
                out.append({"fixture": {"id": fid, "date": f"{d}T{hh}:00:00+00:00", "referee": None,
                                        "status": {"short": "FT" if done else "NS"}},
                            "league": {"id": 39, "season": 2026}, "teams": {"home": {"name": h}, "away": {"name": a}},
                            "goals": {"home": 2 if done else None, "away": 1 if done else None},
                            "score": {"halftime": {"home": 1, "away": 0} if done else {},
                                      "fulltime": {"home": 2, "away": 1} if done else {}}})
        return out

    def __call__(self, url, *, params=None, headers=None, data=None, method="GET", provider="", **kw):
        if "football-data.co.uk" in url:
            if url.endswith("/E0.csv"):
                return Response(200, {}, self.csv.encode())
            raise ProviderError(provider, "404", 404)
        if "api-sports" in url:
            if url.endswith("/fixtures"):
                body = self.fixtures(params["date"])
            elif url.endswith("/odds"):
                body = [{"fixture": {"id": fid}, "bookmakers": books} for fid, books in self.odds.items()] \
                    if params["date"] == "2026-11-07" else []
            elif url.endswith("/statistics"):
                body = [{"team": {"name": "Alpha FC" if params["fixture"] == 1 else "Bravo United"},
                         "statistics": [{"type": "Corner Kicks", "value": 6}, {"type": "Yellow Cards", "value": 2}]},
                        {"team": {"name": "Lima County" if params["fixture"] == 1 else "Kilo Borough"},
                         "statistics": [{"type": "Corner Kicks", "value": 2}, {"type": "Yellow Cards", "value": 1}]}]
            else:
                body = []
            return Response(200, {}, json.dumps({"errors": [], "paging": {"current": 1, "total": 1},
                                                 "response": body}).encode())
        if "ntfy" in url:
            m = json.loads(data.decode())
            self.pushes.append(m["title"])
            return Response(200, {}, b"{}")
        raise ProviderError(provider, f"unexpected {url}", 404)


class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = Settings()
        self.cfg.api_football_key = "k"
        self.cfg.ntfy_topic = "t"
        self.cfg.execution_mode = "paper"

    def _prepare(self, statuses):
        """Model the two fixtures, then price chosen selections ~8% above the model probability."""
        world = LiveWorld({})
        eng = Engine(self.cfg, self.dir, http=world, now=NOW)
        eng.collect()
        eng.refresh_predictions()
        params = {r["fixture_id"]: json.loads(r["params"]) for r in eng.store.q("SELECT * FROM predictions")}
        self.assertEqual(set(params), {1, 2})
        picks = {}
        for fid, fam, bet in ((1, "goals_total", "Goals Over/Under"), (2, "corners_total", "Corners Over Under")):
            for line in [x + 0.5 for x in range(0, 14)]:
                p = probability(params[fid], fam, "over", line)
                if p is not None and 0.88 <= p <= 0.95:
                    picks[fid] = (fam, bet, line, p)
                    break
        books = {}
        for fid, (fam, bet, line, p) in picks.items():
            price = round(1.08 / p + 0.01, 2)
            books[fid] = [{"name": n, "bets": [{"name": bet, "values": [
                {"value": f"Over {line}", "odd": str(price if n == "A" else price - 0.02)},
                {"value": f"Under {line}", "odd": str(round(1 / (1 - p) * 0.92, 2))}]}]} for n in ("A", "B")]
        gate = {"markets": {fam: {"status": statuses[i], "passed": [["over", line, "glm"]] if statuses[i] == "PASSED" else [],
                                  "shadow": [["over", line, "glm"]] if statuses[i] == "SHADOW" else []}
                            for i, (fid, (fam, bet, line, p)) in enumerate(sorted(picks.items()))}}
        eng.store.set("gate", gate)
        eng.store.commit()
        eng.close()
        world.odds = books
        self.store_keys_reset()
        return world, picks

    def store_keys_reset(self):
        e = Engine(self.cfg, self.dir, now=NOW)
        e.store.x("DELETE FROM meta WHERE key LIKE 'apif_oddsq:%'")    # force a fresh odds pull
        e.store.commit()
        e.close()

    def test_full_day_cross_match_entry_staked_then_settled(self):
        world, picks = self._prepare(["PASSED", "PASSED"])
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=5))
        out = eng.run()
        bets = eng.store.q("SELECT * FROM bets")
        legs = eng.store.q("SELECT * FROM legs")
        eng.close()
        self.assertEqual(len(bets), 1, out["log"])
        self.assertEqual({l["event_id"] for l in legs}, {"apif:1", "apif:2"})
        self.assertEqual({l["market"] for l in legs}, {"goals_total", "corners_total"})
        self.assertTrue(1.30 <= bets[0]["odds"] <= 1.40)
        self.assertTrue(any(t.startswith("PAPER BET") for t in world.pushes), world.pushes)
        self.assertTrue(os.listdir(os.path.join(self.dir, "archive", "observations")))
        # after the matches: fixtures final, statistics published -> entry settled
        world.final = True
        e = Engine(self.cfg, self.dir, http=world, now=NOW)
        e.store.x("DELETE FROM meta WHERE key LIKE 'apif_fixtures:%'")
        e.store.commit()
        e.close()
        eng = Engine(self.cfg, self.dir, http=world, now=datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc))
        out = eng.run()
        bet = eng.store.one("SELECT * FROM bets")
        eng.close()
        corners_line = picks[2][2]
        expected = "WON" if (2 + 1 > picks[1][2]) and (6 + 2 > corners_line) else "LOST"
        self.assertEqual(bet["status"], expected, out["log"])

    def test_shadow_market_logged_not_staked(self):
        world, _ = self._prepare(["PASSED", "SHADOW"])
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=5))
        out = eng.run()
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM bets")[0], 0, out["log"])
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM shadow_entries")[0], 1)
        self.assertEqual(out["bankroll"], 1000)
        eng.close()
        self.assertTrue(any(t.startswith("SHADOW entry") for t in world.pushes), world.pushes)


if __name__ == "__main__":
    unittest.main()


class ManualFlowTests(LiveEngineTests):
    """iPhone flow: opportunity card -> owner places bet at Bet365 -> confirms -> settled -> bankroll."""

    test_full_day_cross_match_entry_staked_then_settled = None   # paper-mode test, covered above

    def setUp(self):
        super().setUp()
        self.cfg.execution_mode = "manual"
        self.cfg.app_url = "https://example.github.io/superpowers/app/"

    def _issue(self):
        world, picks = self._prepare(["PASSED", "PASSED"])
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=5))
        out = eng.run()
        opp = eng.store.one("SELECT * FROM opportunities")
        eng.close()
        return world, picks, opp, out

    def test_card_issued_not_a_bet(self):
        world, picks, opp, out = self._issue()
        self.assertIsNotNone(opp, out["log"])
        self.assertEqual(opp["status"], "OPEN")
        self.assertEqual(opp["stake_cents"], 1000)
        self.assertEqual(out["bankroll"], 1000)                     # nothing staked until the owner confirms
        eng = Engine(self.cfg, self.dir, now=NOW)
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM bets")[0], 0)
        n = eng.store.one("SELECT * FROM notifications WHERE title LIKE 'NEW OPPORTUNITY%'")
        eng.close()
        self.assertIn("potential return", n["body"])
        self.assertIn("Odds verified", n["body"])
        extra = json.loads(n["extra"])
        self.assertEqual(extra["actions"][0]["url"], "https://www.bet365.com/")
        self.assertTrue(extra["click"].endswith("#opp=" + opp["id"]))
        state = json.load(open(os.path.join(self.dir, "state.json")))
        self.assertEqual(state["opportunities"][0]["id"], opp["id"])
        self.assertTrue(all(l["label"] and l["why"] for l in state["opportunities"][0]["legs"]))

    def test_no_duplicate_and_one_at_a_time(self):
        world, picks, opp, out = self._issue()
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=20))
        out = eng.run()
        n = eng.store.one("SELECT COUNT(*) FROM opportunities")[0]
        eng.close()
        self.assertEqual(n, 1)
        self.assertIn("not scanning: an opportunity is waiting for your decision", out["log"])

    def test_confirm_settle_and_reinvest(self):
        from fbs.__main__ import _dispatch
        world, picks, opp, out = self._issue()
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=30))
        bad = {"id": opp["id"], "stake": 25, "odds": 1.35}
        _dispatch(eng, "confirm", [json.dumps(bad)])                                 # beyond bankroll: refused
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM bets")[0], 0)
        self.assertTrue(eng.store.one("SELECT 1 FROM notifications WHERE title='Confirm not recorded'"))
        good = {"id": opp["id"], "stake": 10, "odds": 1.33, "bookmaker": "Bet365", "reference": "ABC123"}
        self.assertEqual(_dispatch(eng, "confirm", [json.dumps(good)]), 0)
        _dispatch(eng, "confirm", [json.dumps(good)])                                # duplicate ignored
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM bets")[0], 1)
        bet = eng.store.one("SELECT * FROM bets")
        self.assertEqual((bet["mode"], bet["stake_cents"], bet["odds"], bet["reference"]), ("manual", 1000, 1.33, "ABC123"))
        eng.close()
        world.final = True
        e = Engine(self.cfg, self.dir, http=world, now=NOW)
        e.store.x("DELETE FROM meta WHERE key LIKE 'apif_fixtures:%'")
        e.store.commit()
        e.close()
        eng = Engine(self.cfg, self.dir, http=world, now=datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc))
        out = eng.run()
        bet = eng.store.one("SELECT * FROM bets")
        eng.close()
        won = (2 + 1 > picks[1][2]) and (6 + 2 > picks[2][2])
        self.assertEqual(bet["status"], "WON" if won else "LOST", out["log"])
        if won:
            self.assertEqual(bet["return_cents"], 1330)        # paid at the odds actually accepted
            self.assertEqual(out["bankroll"], 1330)             # 100% reinvested: next suggested stake

    def test_expiry_and_odds_change(self):
        world, picks, opp, out = self._issue()
        # odds move down: the card is withdrawn
        for fid in world.odds:
            for bm in world.odds[fid]:
                for v in bm["bets"][0]["values"]:
                    if v["value"].startswith("Over"):
                        v["odd"] = "1.03"
        e = Engine(self.cfg, self.dir, now=NOW)
        e.store.x("DELETE FROM meta WHERE key LIKE 'apif_oddsq:%'")
        e.store.commit()
        e.close()
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(hours=1, minutes=5))
        eng.run()
        o = eng.store.one("SELECT * FROM opportunities WHERE id=?", (opp["id"],))
        eng.close()
        self.assertEqual(o["status"], "CHANGED")
        self.assertTrue(any("withdrawn" in t for t in world.pushes), world.pushes)

    def test_expires_at_kickoff(self):
        world, picks, opp, out = self._issue()
        eng = Engine(self.cfg, self.dir, http=world, now=datetime(2026, 11, 7, 13, 55, tzinfo=timezone.utc))
        eng.run()
        o = eng.store.one("SELECT * FROM opportunities")
        eng.close()
        self.assertEqual(o["status"], "EXPIRED")

    def test_stop_and_start(self):
        from fbs.__main__ import _dispatch
        world, picks = self._prepare(["PASSED", "PASSED"])
        eng = Engine(self.cfg, self.dir, http=world, now=NOW)
        _dispatch(eng, "stop", [])
        eng.close()
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=5))
        out = eng.run()
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM opportunities")[0], 0)
        self.assertIn("not scanning: AI stopped from the app (press START to resume)", out["log"])
        _dispatch(eng, "start", [])
        eng.close()
        eng = Engine(self.cfg, self.dir, http=world, now=NOW + timedelta(minutes=10))
        eng.run()
        self.assertEqual(eng.store.one("SELECT COUNT(*) FROM opportunities")[0], 1)
        eng.close()
