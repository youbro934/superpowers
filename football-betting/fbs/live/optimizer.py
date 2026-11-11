"""Multi-match, multi-market entry optimizer.

* At most one selection per match: different matches are treated as
  independent, so the joint probability is the product. Two selections from
  the same match are never combined here (their outcomes are correlated).
* Every selection must clear its own bars (edge, probability, minimum odds,
  agreement with the de-vigged market); weak legs are never added to reach
  the target odds.
* 1-3 selections; a 4th only when no entry of 1-3 qualifies and the 4-leg
  entry clears every bar on its own (cfg.max_entry_legs).
* Staked entries use only PASSED selections. Entries that need a SHADOW
  selection are recorded as shadow entries (no stake) for forward validation.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..config import Settings
from ..decision import Candidate, Leg


@dataclass
class OptimizerResult:
    staked: Candidate | None
    shadow: Candidate | None
    reason: str
    legs_considered: int = 0
    rejected: dict = field(default_factory=dict)


def _search(legs: list[Leg], cfg: Settings, max_legs: int) -> list[Candidate]:
    pool = sorted(legs, key=lambda l: l.ev, reverse=True)[: cfg.combo_pool_size]
    out = []
    for n in range(1, max_legs + 1):
        for combo in itertools.combinations(pool, n):
            if len({l.event_id for l in combo}) < n:
                continue          # one selection per match
            odds = 1.0
            prob = 1.0
            for l in combo:
                odds *= l.odds
                prob *= l.p_used
            if not (cfg.target_odds_min <= odds <= cfg.target_odds_max):
                continue
            ev = prob * odds - 1
            if ev < cfg.min_bet_edge:
                continue
            out.append(Candidate(list(combo), round(odds, 4), round(prob, 4), round(ev, 4)))
    return out


def _best(cands: list[Candidate]) -> Candidate | None:
    if not cands:
        return None
    cands.sort(key=lambda c: (round(c.ev, 4), -len(c.legs), c.prob), reverse=True)
    return cands[0]


def optimize(legs: list[Leg], cfg: Settings, rejected: dict | None = None) -> OptimizerResult:
    rejected = dict(rejected or {})
    passed = [l for l in legs if getattr(l, "status", "PASSED") == "PASSED"]
    shadowable = [l for l in legs if getattr(l, "status", "PASSED") in ("PASSED", "SHADOW")]

    def run(pool):
        best = _best(_search(pool, cfg, 3))
        if best is None and cfg.max_entry_legs > 3:
            best = _best(_search(pool, cfg, cfg.max_entry_legs))
        return best

    staked = run(passed) if passed else None
    shadow = None
    if staked is None and len(shadowable) > len(passed):
        shadow = run(shadowable)
        if shadow is not None and all(getattr(l, "status", "") == "PASSED" for l in shadow.legs):
            shadow = None
    if staked:
        reason = f"BET: {staked.kind} @ {staked.odds:.2f}, p={staked.prob:.1%}, EV={staked.ev:+.1%}"
    elif shadow:
        reason = (f"NO BET (staked): no validated entry. SHADOW entry {shadow.kind} @ {shadow.odds:.2f}, "
                  f"p={shadow.prob:.1%}, EV={shadow.ev:+.1%} logged without stake")
    elif legs:
        reason = (f"NO BET: {len(legs)} value selection(s), none combine into "
                  f"{cfg.target_odds_min:.2f}-{cfg.target_odds_max:.2f} with enough edge")
    else:
        reason = "NO BET: no selection with a positive edge after margins"
    return OptimizerResult(staked, shadow, reason, len(legs), rejected)
