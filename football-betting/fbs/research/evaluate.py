"""Scoring: Brier, log loss, calibration, confidence intervals, ROI, CLV."""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mean_ci(xs: list[float], z: float = 1.96) -> tuple[float, float, float]:
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    m = sum(xs) / n
    if n < 2:
        return (m, -math.inf, math.inf)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    se = sd / math.sqrt(n)
    return (m, m - z * se, m + z * se)


class ProbScore:
    """Accumulates probability forecasts for one market key."""

    def __init__(self):
        self.n = 0
        self.brier = 0.0
        self.brier_ref = 0.0
        self.n_ref = 0
        self.brier_ref_model = 0.0
        self.logloss = 0.0
        self.sum_p = 0.0
        self.wins = 0
        self.bins = [[0, 0.0, 0] for _ in range(10)]   # n, sum p, wins

    def add(self, p: float, won: int, ref: float | None):
        p = min(max(p, 1e-6), 1 - 1e-6)
        self.n += 1
        self.brier += (p - won) ** 2
        self.logloss += -math.log(p if won else 1 - p)
        self.sum_p += p
        self.wins += won
        b = self.bins[min(9, int(p * 10))]
        b[0] += 1
        b[1] += p
        b[2] += won
        if ref is not None:
            self.n_ref += 1
            self.brier_ref += (ref - won) ** 2
            self.brier_ref_model += (p - won) ** 2

    def summary(self, ref_name: str) -> dict:
        if not self.n:
            return {"n": 0}
        ece = sum(abs(b[1] - b[2]) for b in self.bins if b[0]) / self.n
        out = {"n": self.n, "brier": self.brier / self.n, "logloss": self.logloss / self.n,
               "predicted_rate": self.sum_p / self.n, "actual_rate": self.wins / self.n,
               "actual_rate_ci": wilson(self.wins, self.n), "ece": ece,
               "calibration": [{"bin": f"{i / 10:.1f}-{(i + 1) / 10:.1f}", "n": b[0],
                                "predicted": b[1] / b[0], "actual": b[2] / b[0]}
                               for i, b in enumerate(self.bins) if b[0] >= 20]}
        if self.n_ref:
            out[f"brier_{ref_name}"] = self.brier_ref / self.n_ref
            out["brier_model_same_rows"] = self.brier_ref_model / self.n_ref
            out["n_compared"] = self.n_ref
            out["skill_vs_" + ref_name] = 1 - (self.brier_ref_model / self.brier_ref) if self.brier_ref else None
        return out


class BetLog:
    """Value bets placed at historical prices: ROI with uncertainty and CLV."""

    def __init__(self):
        self.profits: list[float] = []
        self.close_profits: list[float] = []
        self.clv: list[float] = []
        self.p: list[float] = []
        self.wins = 0
        self.by_league: dict[str, list[float]] = {}

    def add(self, profit: float, p: float, won: bool, league: str, clv: float | None,
            close_profit: float | None):
        self.profits.append(profit)
        self.p.append(p)
        self.wins += int(won)
        self.by_league.setdefault(league, []).append(profit)
        if clv is not None:
            self.clv.append(clv)
        if close_profit is not None:
            self.close_profits.append(close_profit)

    def summary(self) -> dict:
        n = len(self.profits)
        if not n:
            return {"n": 0}
        roi, lo, hi = mean_ci(self.profits)
        clv, clo, chi = mean_ci(self.clv) if self.clv else (None, None, None)
        out = {"n": n, "roi": roi, "roi_ci95": [lo, hi], "predicted_win_rate": sum(self.p) / n,
               "actual_win_rate": self.wins / n, "actual_win_rate_ci95": wilson(self.wins, n),
               "clv_mean": clv, "clv_ci95": [clo, chi] if clv is not None else None, "clv_n": len(self.clv),
               "roi_at_closing_prices": (sum(self.close_profits) / len(self.close_profits)
                                         if self.close_profits else None),
               "by_league": {lg: {"n": len(v), "roi": sum(v) / len(v)}
                             for lg, v in sorted(self.by_league.items())}}
        # crude guard against lucky streaks: how big must n be for this ROI to be 2 SE from 0
        if n > 1 and roi != 0:
            sd = (hi - lo) / (2 * 1.96) * math.sqrt(n)
            out["n_needed_for_significance"] = int(math.ceil((2 * sd / abs(roi)) ** 2))
        return out
