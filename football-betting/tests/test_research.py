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
