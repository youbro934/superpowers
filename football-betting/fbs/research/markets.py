"""Market catalogue: model probability, historical price (if any) and outcome for
each selection of each market family. One entry = one binary selection."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .glm import convolve, p_over

GOAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)
TEAM_GOAL_LINES = (0.5, 1.5, 2.5)
HT_LINES = (0.5, 1.5)
CORNER_LINES = (7.5, 8.5, 9.5, 10.5, 11.5, 12.5)
TEAM_CORNER_LINES = (2.5, 3.5, 4.5, 5.5, 6.5)
FOUL_LINES = (18.5, 20.5, 22.5, 24.5, 26.5)
TEAM_FOUL_LINES = (8.5, 9.5, 10.5, 11.5, 12.5, 13.5)
YELLOW_LINES = (2.5, 3.5, 4.5, 5.5)
TEAM_YELLOW_LINES = (0.5, 1.5, 2.5)
CARD_LINES = (2.5, 3.5, 4.5, 5.5, 6.5)

# research family -> production market key used by the live decision engine
PRODUCTION_KEY = {
    "result": "h2h", "double_chance": "double_chance", "goals_total": "totals", "btts": "btts",
    "corners_total": "corners_totals", "cards_total": "cards_totals",
}

# markets the bookmaker data gives historical prices for
PRICED = {"result", "double_chance", "goals_total@2.5", "asian_handicap"}

UNAVAILABLE = {
    "corners_halves": "no source gives corners per half (historically or live on the free plans)",
    "fouls_suffered_by_player": "player-level fouls need a paid feed",
    "xg": "xG is not in the free historical files; API-Football provides it forward for some leagues",
}


@dataclass
class Pred:
    family: str
    sel: str
    line: float | None
    model: str
    p: float
    won: int | None            # None when the statistic is missing
    price: float | None = None     # price you could have taken
    close_p: float | None = None   # de-vigged closing probability (for CLV)
    close_price: float | None = None

    @property
    def key(self) -> str:
        return f"{self.family}:{self.sel}" + (f"@{self.line:g}" if self.line is not None else "")


def devig(prices) -> list[float]:
    inv = [1 / p for p in prices]
    t = sum(inv)
    return [x / t for x in inv]


def result_preds(matrix: np.ndarray | None, side_goals, m, model: str) -> list[Pred]:
    """1X2, double chance, BTTS from a joint score distribution."""
    if matrix is None:
        return []
    n = matrix.shape[0]
    i, j = np.indices(matrix.shape)
    ph, pd, pa = float(matrix[i > j].sum()), float(np.trace(matrix)), float(matrix[i < j].sum())
    btts = float(matrix[1:, 1:].sum())
    hg, ag = m.stats["goals"]
    actual = "home" if hg > ag else "away" if ag > hg else "draw"
    taken = m.odds.get("1x2_taken")
    close = m.odds.get("1x2_close")
    cp = devig(close[1]) if close else None
    tp = taken[1] if taken else None
    out = []
    for k, sel in enumerate(("home", "draw", "away")):
        out.append(Pred("result", sel, None, model, (ph, pd, pa)[k], int(actual == sel),
                        tp[k] if tp else None, cp[k] if cp else None, close[1][k] if close else None))
    dc = {"1X": (0, 1), "X2": (1, 2), "12": (0, 2)}
    for sel, (a, b) in dc.items():
        p = (ph, pd, pa)[a] + (ph, pd, pa)[b]
        won = int(actual in {("home", "draw", "away")[a], ("home", "draw", "away")[b]})
        # dutching both outcomes at the taken prices gives exactly this combined price
        price = 1 / (1 / tp[a] + 1 / tp[b]) if tp else None
        cprice = 1 / (1 / close[1][a] + 1 / close[1][b]) if close else None
        out.append(Pred("double_chance", sel, None, model, p, won, price,
                        (cp[a] + cp[b]) if cp else None, cprice))
    yes = int(hg > 0 and ag > 0)
    out.append(Pred("btts", "yes", None, model, btts, yes))
    out.append(Pred("btts", "no", None, model, 1 - btts, 1 - yes))
    return out


def ah_payout(diff: int, line: float, side: str) -> float:
    """Profit multiplier per unit stake at decimal odds 2.0-equivalent units:
    returns the fraction of the stake that wins (1), pushes (0) or loses (-1),
    with quarter lines split in halves."""
    if side == "away":
        diff, line = -diff, -line
    q = round(line * 4) % 2 == 1          # quarter line
    lines = (line - 0.25, line + 0.25) if q else (line,)
    total = 0.0
    for l in lines:
        v = diff + l
        total += 1.0 if v > 0 else (0.0 if v == 0 else -1.0)
    return total / len(lines)


def ah_expected(matrix: np.ndarray, line: float, side: str, price: float) -> float:
    """Expected profit per unit stake at `price` (decimal)."""
    i, j = np.indices(matrix.shape)
    ev = 0.0
    for d in range(-matrix.shape[0] + 1, matrix.shape[0]):
        pdiff = float(matrix[(i - j) == d].sum())
        if pdiff == 0:
            continue
        r = ah_payout(d, line, side)
        ev += pdiff * (r * (price - 1) if r > 0 else r)
    return ev


def count_preds(family: str, team_family: str, stat: str, lines, team_lines, home_pmf, away_pmf, m,
                model: str, total_pmf: np.ndarray | None = None) -> list[Pred]:
    have = stat in m.stats
    h, a = m.stats[stat] if have else (None, None)
    total = total_pmf if total_pmf is not None else convolve(home_pmf, away_pmf)
    out = []
    for l in lines:
        po = p_over(total, l)
        won = int(h + a > l) if have else None
        out.append(Pred(family, "over", l, model, po, won))
        out.append(Pred(family, "under", l, model, 1 - po, None if won is None else 1 - won))
    for side, pmf, val in (("home", home_pmf, h), ("away", away_pmf, a)):
        for l in team_lines:
            po = p_over(pmf, l)
            won = int(val > l) if have else None
            out.append(Pred(team_family, f"{side}_over", l, model, po, won))
            out.append(Pred(team_family, f"{side}_under", l, model, 1 - po, None if won is None else 1 - won))
    return out


def attach_ou25_prices(preds: list[Pred], m):
    taken, close = m.odds.get("ou25_taken"), m.odds.get("ou25_close")
    cp = devig(close[1]) if close else None
    for p in preds:
        if p.family == "goals_total" and p.line == 2.5:
            k = 0 if p.sel == "over" else 1
            p.price = taken[1][k] if taken else None
            p.close_p = cp[k] if cp else None
            p.close_price = close[1][k] if close else None
