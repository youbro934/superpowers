"""Research pipeline on SYNTHETIC data where the bookmaker prices are the true
probabilities plus a 5% margin. Any 'edge' found here would be a bug or luck."""
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from fbs.config import COMPETITION_BY_CODE
from fbs.research.backtest import Research
from fbs.research.data import ResearchData, parse
from fbs.research.evaluate import mean_ci, wilson
from fbs.research.gate import evaluate_gate
from fbs.research.glm import CountModel, count_pmf, p_over
from fbs.research.joint import joint_probability
from fbs.research.markets import ah_payout
from fbs.util import Response
from tests.fakes import synthetic_full_csv

TODAY = date(2026, 11, 7)


class FullWorld:
    def __init__(self):
        self.csv = synthetic_full_csv(TODAY)

    def __call__(self, url, **kw):
        from fbs.util import ProviderError
        if url.endswith("/E0.csv"):
            return Response(200, {}, self.csv.encode())
        raise ProviderError("x", "404", 404)


class PieceTests(unittest.TestCase):
    def test_parse_keeps_provenance_and_missing(self):
        ms = parse(synthetic_full_csv(TODAY, seasons=1), "PL", "https://example/E0.csv", "2026-11-07T00:00:00Z", False)
        self.assertTrue(ms[0].source.endswith("E0.csv"))
        self.assertIn("corners", ms[0].stats)
        self.assertIn("1x2_taken", ms[0].odds)
        self.assertEqual(ms[0].odds["ah_taken"][2], -0.5)
        text = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nX,01/01/2026,A,B,1,0\n"
        m = parse(text, "PL", "s", "t", False)[0]
        self.assertNotIn("corners", m.stats)        # missing stays missing

    def test_glm_recovers_rate(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(4000, 2))
        y = rng.poisson(np.exp(0.5 + 0.3 * X[:, 0]))
        cm = CountModel(ridge=0.1).fit(X, y.astype(float))
        self.assertAlmostEqual(cm.beta[1], 0.3, delta=0.05)
        self.assertLess(cm.alpha, 0.05)

    def test_distributions(self):
        pmf = count_pmf(9.8, 0.05)
        self.assertAlmostEqual(pmf.sum(), 1, 9)
        self.assertTrue(0.3 < p_over(pmf, 10.5) < 0.6)

    def test_asian_handicap_quarter_lines(self):
        self.assertEqual(ah_payout(1, -0.75, "home"), 0.5)     # win half
        self.assertEqual(ah_payout(0, -0.25, "home"), -0.5)    # lose half
        self.assertEqual(ah_payout(0, -0.25, "away"), 0.5)
        self.assertEqual(ah_payout(2, -1.0, "home"), 1.0)
        self.assertEqual(ah_payout(1, -1.0, "home"), 0.0)      # push

    def test_stats_helpers(self):
        lo, hi = wilson(50, 100)
        self.assertTrue(lo < 0.5 < hi)
        m, lo, hi = mean_ci([1, -1] * 50)
        self.assertTrue(lo < 0 < hi)

    def test_joint_probability_handles_correlation(self):
        from fbs.models import score_matrix
        mat = np.array(score_matrix(1.8, 1.0, -0.05))
        legs = [("e1", "result", "home", None), ("e1", "goals_total", "over", 2.5)]
        joint = joint_probability(legs, {"e1": mat}, {})
        i, j = np.indices(mat.shape)
        ph = float(mat[i > j].sum())
        po = float(mat[(i + j) > 2.5].sum())
        self.assertAlmostEqual(joint, float(mat[(i > j) & ((i + j) > 2.5)].sum()), 9)
        self.assertNotAlmostEqual(joint, ph * po, 3)          # naive product is wrong here
        # mixed families in one match have no joint model: refused
        self.assertIsNone(joint_probability([("e1", "result", "home", None), ("e1", "corners_total", "over", 9.5)],
                                            {"e1": mat}, {("e1", "corners_total", "over", 9.5): 0.5}))
        # different matches: independent
        p = joint_probability([("e1", "result", "home", None), ("e2", "corners_total", "over", 9.5)],
                              {"e1": mat}, {("e2", "corners_total", "over", 9.5): 0.5})
        self.assertAlmostEqual(p, ph * 0.5, 9)


class TwoLeagueWorld:
    def __init__(self):
        self.files = {"E0.csv": synthetic_full_csv(TODAY), "SP1.csv": synthetic_full_csv(TODAY, seed=5, foul_scale=1.3, prefix="Zeta ")}

    def __call__(self, url, **kw):
        from fbs.util import ProviderError
        name = url.rsplit("/", 1)[-1]
        if name in self.files:
            return Response(200, {}, self.files[name].encode())
        raise ProviderError("x", "404", 404)


class LeagueLevelTests(unittest.TestCase):
    def test_each_league_keeps_its_own_level(self):
        data = ResearchData(tempfile.mkdtemp(), http=TwoLeagueWorld())
        rep = Research(data, TODAY, seasons=4, test_months=8,
                       comps=[COMPETITION_BY_CODE["PL"], COMPETITION_BY_CODE["PD"]], log=lambda *_: None).run()
        lv = rep["level_check"]
        for lg in ("PL", "PD"):
            # a pooled model without league offsets is ~10% off in each league here
            self.assertLess(abs(lv[f"{lg}|fouls"]["bias"]), 0.04, lv[f"{lg}|fouls"])


class GateSelectionTests(unittest.TestCase):
    def test_unpriced_line_can_shadow_while_priced_line_rejected(self):
        from fbs.research.gate import selection_status
        good = {"n": 900, "ece": 0.01, "skill_vs_base_rate": 0.02, "reference": "base_rate"}
        rep = {"markets": {"goals_total": {
            "selections": {"goals_total:over@0.5|glm": good,
                           "goals_total:over@2.5|glm": {"n": 900, "ece": 0.01, "reference": "market",
                                                        "skill_vs_market": -0.01}},
            "bets": {"goals_total:over@2.5|glm|all": {"n": 900, "roi": -0.05, "roi_ci95": [-0.09, -0.01],
                                                      "clv_mean": -0.04, "clv_ci95": [-0.05, -0.03]}}}}}
        gate = evaluate_gate(rep)
        self.assertEqual(selection_status(gate, "goals_total", "over", 0.5, "glm"), "SHADOW")
        self.assertEqual(selection_status(gate, "goals_total", "over", 2.5, "glm"), "REJECTED")
        self.assertEqual(selection_status(gate, "goals_total", "over", 1.5, "glm"), "REJECTED")  # untested line


class ArchiveTests(unittest.TestCase):
    def test_archive_matches_and_prices(self):
        import gzip, json
        from fbs.research.data import Match, load_archive, merge
        from fbs.research.markets import Pred, attach_archive_prices
        d = tempfile.mkdtemp()
        base = {"source": "api-football", "fetched_at": "2026-11-07T10:00:00Z", "fixture": 9, "league": 71,
                "home": "flamengo", "away": "palmeiras", "kickoff": "2026-11-07T23:30:00+00:00"}
        recs = [base | {"kind": "stats", "referee": "R. Claus", "goals": {"home": 2, "away": 1},
                        "ht": {"home": 1, "away": 0},
                        "stats": {"home_corners": 7, "away_corners": 4, "home_yellows": 2, "away_yellows": 3,
                                  "home_xg": 1.9, "away_xg": 0.8}}]
        for snap, o, u in (("open", 1.90, 1.90), ("close", 1.80, 2.00)):
            for book in ("A", "B"):
                recs.append(base | {"kind": "odds", "snapshot": snap, "bookmaker": book,
                                    "bet": "Corners Over Under", "value": "Over 9.5", "odd": o})
                recs.append(base | {"kind": "odds", "snapshot": snap, "bookmaker": book,
                                    "bet": "Corners Over Under", "value": "Under 9.5", "odd": u})
        with gzip.open(os.path.join(d, "2026-11-07.jsonl.gz"), "wt") as f:
            f.write("\n".join(json.dumps(r) for r in recs))
        ms = load_archive(d)
        self.assertEqual(len(ms), 1)
        m = ms[0]
        self.assertEqual(m.league, "BSA")
        self.assertEqual(m.stats["corners"], (7, 4))
        self.assertEqual(m.stats["reds"], (0, 0))
        self.assertNotIn("fouls", m.stats)
        p = Pred("corners_total", "over", 9.5, "glm", 0.55, 1)
        attach_archive_prices([p], m)
        self.assertEqual(p.price, 1.90)
        self.assertAlmostEqual(p.close_p, (1 / 1.8) / (1 / 1.8 + 1 / 2.0), 9)
        # merging onto a results-only row a day earlier (time zones) enriches instead of duplicating
        older = Match("BSA", m.day.replace(day=6), "flamengo", "palmeiras", {"goals": (2, 1)}, None, {}, "couk", "")
        merged = merge([older], ms)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].stats["corners"], (7, 4))


class ResearchRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = ResearchData(tempfile.mkdtemp(), http=FullWorld())
        cls.report = Research(data, TODAY, seasons=4, test_months=12, comps=[COMPETITION_BY_CODE["PL"]],
                              log=lambda *_: None).run()

    def test_every_market_family_scored(self):
        fams = set(self.report["markets"])
        for f in ("result", "double_chance", "btts", "goals_total", "team_goals", "ht_goals_total",
                  "corners_total", "team_corners", "fouls_total", "team_fouls", "yellows_total",
                  "team_yellows", "red_card", "cards_total"):
            self.assertIn(f, fams, f)

    def test_calibrated_on_known_process(self):
        sel = self.report["markets"]["corners_total"]["selections"]
        s = sel["corners_total:over@9.5|glm"]
        self.assertGreater(s["n"], 100)
        self.assertLess(abs(s["predicted_rate"] - s["actual_rate"]), 0.06)

    def test_no_fake_edge_against_fair_plus_margin(self):
        gate = evaluate_gate(self.report)
        self.assertFalse(any(v["status"] == "PASSED" for v in gate["markets"].values()), gate)
        # markets without historical prices cannot pass, only shadow
        self.assertNotEqual(gate["markets"]["corners_total"]["status"], "PASSED")

    def test_duplicate_rows_removed(self):
        data = ResearchData(tempfile.mkdtemp(), http=FullWorld())
        ms = data.load(COMPETITION_BY_CODE["PL"], TODAY, 4)     # fake serves one file for every season
        self.assertEqual(len(ms), len({(m.day, m.home, m.away) for m in ms}))

    def test_cannot_beat_true_probabilities(self):
        # prices are the TRUE probabilities plus margin: the model must not look better than the market
        s = self.report["markets"]["result"]["selections"]
        for key, v in s.items():
            self.assertGreaterEqual(v["brier_model_same_rows"], v["brier_market"] - 0.003, key)

    def test_referee_feature_used_when_available(self):
        self.assertEqual(self.report["coverage"]["PL"]["referee"], 1.0)


if __name__ == "__main__":
    unittest.main()
