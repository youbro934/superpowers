"""Probability models: time-weighted Poisson ratings with the Dixon-Coles low-score correction.

Ratings are fitted by iterative proportional fitting of the Poisson likelihood
(attack_i * weakness_j * home), with shrinkage toward the league average so
teams with few matches are not given extreme ratings. The Dixon-Coles rho is
then fitted by maximizing the weighted tau term of the likelihood.

The same rating machinery fits corners and cards counts (plain Poisson, no rho).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass
class MatchRecord:
    day: date
    home: str
    away: str
    hg: int
    ag: int
    hc: int | None = None   # corners
    ac: int | None = None
    hcards: int | None = None   # yellow + red
    acards: int | None = None
    odds: dict = field(default_factory=dict)   # closing odds for backtests


@dataclass
class Ratings:
    attack: dict[str, float]
    weakness: dict[str, float]
    home: float
    eff_matches: dict[str, float]
    n_matches: int

    def expected(self, home: str, away: str) -> tuple[float, float]:
        lam = self.attack[home] * self.weakness[away] * self.home
        mu = self.attack[away] * self.weakness[home]
        return lam, mu

    def knows(self, team: str, min_eff: float) -> bool:
        return team in self.attack and self.eff_matches.get(team, 0.0) >= min_eff

    def to_dict(self) -> dict:
        return {"attack": self.attack, "weakness": self.weakness, "home": self.home,
                "eff": self.eff_matches, "n": self.n_matches}

    @classmethod
    def from_dict(cls, d: dict) -> "Ratings":
        return cls(d["attack"], d["weakness"], d["home"], d["eff"], d["n"])


def fit_ratings(rows: list[tuple[str, str, float, float, float]], iters: int = 150,
                prior: float = 3.0) -> Ratings | None:
    """rows: (home, away, home_count, away_count, weight)."""
    rows = [r for r in rows if r[4] > 1e-6]
    if len(rows) < 20:
        return None
    teams = sorted({r[0] for r in rows} | {r[1] for r in rows})
    a = {t: 1.0 for t in teams}
    total_w = sum(r[4] for r in rows)
    mean_count = sum(r[4] * (r[2] + r[3]) for r in rows) / (2 * total_w)
    b = {t: mean_count for t in teams}
    g = 1.0
    eff = {t: 0.0 for t in teams}
    for h, aw, _, _, w in rows:
        eff[h] += w
        eff[aw] += w
    for _ in range(iters):
        scored = {t: 0.0 for t in teams}
        exp_a = {t: 0.0 for t in teams}
        for h, aw, hg, ag, w in rows:
            scored[h] += w * hg
            scored[aw] += w * ag
            exp_a[h] += w * b[aw] * g
            exp_a[aw] += w * b[h]
        a = {t: (scored[t] + prior) / (exp_a[t] + prior) for t in teams}
        conceded = {t: 0.0 for t in teams}
        exp_b = {t: 0.0 for t in teams}
        for h, aw, hg, ag, w in rows:
            conceded[aw] += w * hg
            conceded[h] += w * ag
            exp_b[aw] += w * a[h] * g
            exp_b[h] += w * a[aw]
        b_mean = sum(b.values()) / len(b)
        b = {t: (conceded[t] + prior * b_mean) / (exp_b[t] + prior) for t in teams}
        num = sum(w * hg for _, _, hg, _, w in rows)
        den = sum(w * a[h] * b[aw] for h, aw, _, _, w in rows)
        g = num / den if den > 0 else 1.0
        # fix the scale: geometric mean of attack = 1
        gm = math.exp(sum(math.log(max(v, 1e-9)) for v in a.values()) / len(a))
        a = {t: v / gm for t, v in a.items()}
        b = {t: v * gm for t, v in b.items()}
    return Ratings(a, b, g, eff, len(rows))


def tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    if x == 0 and y == 1:
        return 1 + lam * rho
    if x == 1 and y == 0:
        return 1 + mu * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def fit_rho(ratings: Ratings, rows) -> float:
    best, best_ll = 0.0, -math.inf
    for k in range(-50, 31):
        rho = k * 0.005
        ll = 0.0
        ok = True
        for h, aw, hg, ag, w in rows:
            if hg > 1 or ag > 1:
                continue
            lam, mu = ratings.expected(h, aw)
            t = tau(int(hg), int(ag), lam, mu, rho)
            if t <= 0:
                ok = False
                break
            ll += w * math.log(t)
        if ok and ll > best_ll:
            best, best_ll = rho, ll
    return best


def _weights(records: list[MatchRecord], ref: date, xi: float):
    return [math.exp(-xi * max(0, (ref - r.day).days)) for r in records]


@dataclass
class LeagueModel:
    goals: Ratings
    rho: float
    corners: Ratings | None
    cards: Ratings | None
    fitted_on: str
    n_matches: int

    def to_dict(self) -> dict:
        return {"goals": self.goals.to_dict(), "rho": self.rho,
                "corners": self.corners.to_dict() if self.corners else None,
                "cards": self.cards.to_dict() if self.cards else None,
                "fitted_on": self.fitted_on, "n": self.n_matches}

    @classmethod
    def from_dict(cls, d: dict) -> "LeagueModel":
        return cls(Ratings.from_dict(d["goals"]), d["rho"],
                   Ratings.from_dict(d["corners"]) if d.get("corners") else None,
                   Ratings.from_dict(d["cards"]) if d.get("cards") else None,
                   d["fitted_on"], d["n"])

    def score_matrix(self, home: str, away: str, max_goals: int = 10) -> list[list[float]]:
        lam, mu = self.goals.expected(home, away)
        return score_matrix(lam, mu, self.rho, max_goals)


def fit_league(records: list[MatchRecord], ref: date, xi: float = 0.0019) -> LeagueModel | None:
    records = [r for r in records if r.day < ref]
    if len(records) < 40:
        return None
    w = _weights(records, ref, xi)
    rows = [(r.home, r.away, r.hg, r.ag, wi) for r, wi in zip(records, w)]
    goals = fit_ratings(rows)
    if goals is None:
        return None
    rho = fit_rho(goals, rows)
    crow = [(r.home, r.away, r.hc, r.ac, wi) for r, wi in zip(records, w) if r.hc is not None and r.ac is not None]
    krow = [(r.home, r.away, r.hcards, r.acards, wi) for r, wi in zip(records, w)
            if r.hcards is not None and r.acards is not None]
    corners = fit_ratings(crow, iters=60) if len(crow) >= 100 else None
    cards = fit_ratings(krow, iters=60) if len(krow) >= 100 else None
    return LeagueModel(goals, rho, corners, cards, ref.isoformat(), len(records))


def poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1)) if lam > 0 else (1.0 if k == 0 else 0.0)


def score_matrix(lam: float, mu: float, rho: float, max_goals: int = 10) -> list[list[float]]:
    ph = [poisson_pmf(i, lam) for i in range(max_goals + 1)]
    pa = [poisson_pmf(j, mu) for j in range(max_goals + 1)]
    m = [[ph[i] * pa[j] * tau(i, j, lam, mu, rho) for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    total = sum(map(sum, m))
    return [[v / total for v in row] for row in m]


def goal_market_probs(m: list[list[float]]) -> dict:
    """Probabilities for every goal-based market the engine trades."""
    n = len(m)
    home = sum(m[i][j] for i in range(n) for j in range(n) if i > j)
    draw = sum(m[i][i] for i in range(n))
    away = 1 - home - draw
    btts = sum(m[i][j] for i in range(1, n) for j in range(1, n))
    totals = {}
    for line in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
        over = sum(m[i][j] for i in range(n) for j in range(n) if i + j > line)
        totals[line] = over
    return {
        ("h2h", "home", None): home, ("h2h", "draw", None): draw, ("h2h", "away", None): away,
        ("double_chance", "home_draw", None): home + draw,
        ("double_chance", "draw_away", None): draw + away,
        ("double_chance", "home_away", None): home + away,
        ("btts", "yes", None): btts, ("btts", "no", None): 1 - btts,
        **{("totals", "over", l): p for l, p in totals.items()},
        **{("totals", "under", l): 1 - p for l, p in totals.items()},
    }


def poisson_over(total_lambda: float, line: float) -> float:
    k_max = math.floor(line)
    return 1 - sum(poisson_pmf(k, total_lambda) for k in range(k_max + 1))
