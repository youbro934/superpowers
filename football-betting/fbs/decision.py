"""Expected value and the bet / NO BET decision."""
from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .models import LeagueModel, goal_market_probs, poisson_over
from .providers.odds_api import Event, Quote
from .teams import is_favorite, match_team

# selections in a market that together cover every outcome (for de-vigging)
MARKET_GROUPS = {
    "h2h": ("home", "draw", "away"),
    "totals": ("over", "under"),
    "corners_totals": ("over", "under"),
    "cards_totals": ("over", "under"),
    "btts": ("yes", "no"),
}
SECONDARY = {"corners_totals", "cards_totals"}


@dataclass
class Leg:
    event_id: str
    sport_key: str
    competition: str
    home: str
    away: str
    kickoff: str
    market: str
    selection: str
    line: float | None
    odds: float
    bookmaker: str
    p_model: float
    p_market: float
    p_used: float
    ev: float
    favorite: bool
    status: str = "PASSED"        # promotion-gate status of this selection
    priced_at: str = ""           # timestamp of the price used

    def label(self) -> str:
        line = f" {self.line:g}" if self.line is not None else ""
        names = {"home": self.home, "away": self.away, "draw": "Draw",
                 "home_draw": f"{self.home} or draw", "draw_away": f"{self.away} or draw",
                 "home_away": "No draw"}
        sel = names.get(self.selection, self.selection)
        return f"{self.home} v {self.away}: {self.market} {sel}{line} @ {self.odds:.2f}"


@dataclass
class Candidate:
    legs: list[Leg]
    odds: float
    prob: float
    ev: float

    @property
    def kind(self) -> str:
        return "single" if len(self.legs) == 1 else f"combo{len(self.legs)}"

    def to_dict(self) -> dict:
        return {"odds": self.odds, "prob": self.prob, "ev": self.ev, "legs": [asdict(l) for l in self.legs]}


@dataclass
class Decision:
    candidate: Candidate | None
    reason: str
    qualifying_legs: int = 0
    candidates: int = 0
    rejected: dict = field(default_factory=dict)


def consensus_probs(quotes: list[Quote]) -> dict[tuple, float]:
    """De-vig each bookmaker's complete market (proportional method) and average."""
    by_book = defaultdict(dict)
    for q in quotes:
        by_book[(q.bookmaker, q.market, q.line)][q.selection] = q.price
    acc = defaultdict(list)
    for (book, market, line), sel_prices in by_book.items():
        if market == "double_chance":
            continue
        group = MARKET_GROUPS.get(market)
        if not group or any(s not in sel_prices for s in group):
            continue
        inv = {s: 1 / sel_prices[s] for s in group}
        total = sum(inv.values())
        for s in group:
            acc[(market, s, line)].append(inv[s] / total)
    out = {k: sum(v) / len(v) for k, v in acc.items()}
    # double chance fair probabilities follow from fair 1X2
    h, d, a = (out.get(("h2h", s, None)) for s in ("home", "draw", "away"))
    if h is not None and d is not None and a is not None:
        out[("double_chance", "home_draw", None)] = h + d
        out[("double_chance", "draw_away", None)] = d + a
        out[("double_chance", "home_away", None)] = h + a
    return out


def best_prices(quotes: list[Quote], allowed: list[str]) -> dict[tuple, Quote]:
    best: dict[tuple, Quote] = {}
    for q in quotes:
        if allowed and q.bookmaker not in allowed:
            continue
        k = (q.market, q.selection, q.line)
        if k not in best or q.price > best[k].price:
            best[k] = q
    return best


def model_probs(model: LeagueModel, home: str, away: str) -> dict[tuple, float]:
    probs = goal_market_probs(model.score_matrix(home, away))
    for attr, market in (("corners", "corners_totals"), ("cards", "cards_totals")):
        r = getattr(model, attr)
        if r and home in r.attack and away in r.attack:
            lam, mu = r.expected(home, away)
            for k in range(3, 16):
                line = k + 0.5
                over = poisson_over(lam + mu, line)
                probs[(market, "over", line)] = over
                probs[(market, "under", line)] = 1 - over
    return probs


def backs(leg_selection: str, market: str) -> set[str]:
    if market == "h2h":
        return {leg_selection} - {"draw"}
    if market == "double_chance":
        return {"home_draw": {"home"}, "draw_away": {"away"}, "home_away": {"home", "away"}}[leg_selection]
    return set()


def evaluate_event(ev: Event, model: LeagueModel, comp_code: str, cfg: Settings, now: datetime,
                   injuries: dict[str, int] | None = None, rejected: dict | None = None) -> list[Leg]:
    rejected = rejected if rejected is not None else {}

    def reject(why: str):
        rejected[why] = rejected.get(why, 0) + 1

    teams = set(model.goals.attack)
    home = match_team(ev.home_raw, teams)
    away = match_team(ev.away_raw, teams)
    if not home or not away or home == away:
        reject("team not in model")
        return []
    if not (model.goals.knows(home, cfg.min_team_eff_matches) and model.goals.knows(away, cfg.min_team_eff_matches)):
        reject("too little team history")
        return []
    if ev.kickoff - now < timedelta(minutes=cfg.min_minutes_to_kickoff):
        reject("kickoff too close")
        return []
    if ev.fetched_at is None or now - ev.fetched_at > timedelta(minutes=cfg.max_odds_age_minutes):
        reject("stale odds")
        return []
    fresh = ev.quotes
    if not fresh:
        reject("no odds")
        return []
    pm = model_probs(model, home, away)
    pk = consensus_probs(fresh)
    best = best_prices(fresh, cfg.allowed_bookmakers)
    fav = is_favorite(home) or is_favorite(away)
    legs = []
    for key, q in best.items():
        if key not in pm or key not in pk:
            continue
        p_model, p_market = pm[key], pk[key]
        if abs(p_model - p_market) > cfg.max_model_market_gap:
            reject("model/market disagreement too large")
            continue
        p = cfg.model_weight * p_model + (1 - cfg.model_weight) * p_market
        value = p * q.price - 1
        need = cfg.min_leg_edge
        if q.market in SECONDARY:
            need += cfg.secondary_market_extra_edge
        if injuries:
            backed = {"home": home, "away": away}
            if any(injuries.get(backed[s], 0) >= cfg.injury_alert_count for s in backs(q.selection, q.market)):
                need += cfg.injury_extra_edge
        if q.price < cfg.min_leg_odds:
            reject("odds too short")
            continue
        if p < cfg.min_leg_prob:
            reject("probability too low")
            continue
        if value < need:
            reject("no edge")
            continue
        legs.append(Leg(ev.event_id, ev.sport_key, comp_code, home, away, ev.kickoff.isoformat(),
                        q.market, q.selection, q.line, q.price, q.bookmaker,
                        round(p_model, 4), round(p_market, 4), round(p, 4), round(value, 4), fav))
    return legs


def decide(legs: list[Leg], cfg: Settings, rejected: dict | None = None) -> Decision:
    rejected = dict(rejected or {})
    if not legs:
        return Decision(None, "NO BET: no selection with a positive edge after margins", 0, 0, rejected)
    # combos never take two legs from one match (correlated outcomes)
    pool_all = sorted(legs, key=lambda l: l.ev, reverse=True)
    cands: list[Candidate] = []
    for l in pool_all:
        if cfg.target_odds_min <= l.odds <= cfg.target_odds_max and l.ev >= cfg.min_bet_edge:
            cands.append(Candidate([l], l.odds, l.p_used, l.ev))
    pool = pool_all[: cfg.combo_pool_size]
    for n in range(2, cfg.max_combo_legs + 1):
        for combo in itertools.combinations(pool, n):
            if len({l.event_id for l in combo}) < n:
                continue
            odds = 1.0
            prob = 1.0
            for l in combo:
                odds *= l.odds
                prob *= l.p_used
            if not (cfg.target_odds_min <= odds <= cfg.target_odds_max):
                continue
            value = prob * odds - 1
            if value < cfg.min_bet_edge:
                continue
            cands.append(Candidate(list(combo), round(odds, 4), round(prob, 4), round(value, 4)))
    if not cands:
        return Decision(None, f"NO BET: {len(legs)} value selection(s) but none fit odds "
                              f"{cfg.target_odds_min:.2f}-{cfg.target_odds_max:.2f} with enough edge",
                        len(legs), 0, rejected)
    # highest EV; ties go to fewer legs, then higher win probability
    cands.sort(key=lambda c: (round(c.ev, 4), -len(c.legs), c.prob), reverse=True)
    best = cands[0]
    return Decision(best, f"BET: {best.kind} @ {best.odds:.2f}, p={best.prob:.1%}, EV={best.ev:+.1%}",
                    len(legs), len(cands), rejected)
