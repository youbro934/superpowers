import os
import sys
import tempfile
import time
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbs.backtest import run_backtest, summary_text
from fbs.config import COMPETITION_BY_CODE, Settings
from fbs.providers.history import HistoryProvider
from tests.fakes import FakeWorld


class BacktestTests(unittest.TestCase):
    def test_runs_walk_forward_on_synthetic_league(self):
        world = FakeWorld(date(2026, 11, 7))
        hist = HistoryProvider(tempfile.mkdtemp(), http=world)
        t = time.time()
        r = run_backtest(hist, Settings(), date(2026, 11, 7), test_days=120, comps=[COMPETITION_BY_CODE["PL"]])
        self.assertLess(time.time() - t, 60)
        self.assertGreater(r["brier_1x2"]["matches"], 50)
        self.assertIn("Backtest", summary_text(r))
        self.assertEqual(r["bets"], len(r["bets_detail"]))


if __name__ == "__main__":
    unittest.main()
