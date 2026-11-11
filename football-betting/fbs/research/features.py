"""Pre-match features built strictly from earlier matches (no leakage).

For every match and side: form over the last 5/10/15 matches, venue-specific
form, head-to-head, rest days, referee tendencies, all relative to the league
average at that moment and shrunk toward it when the sample is small. Each
value carries the number of observations behind it.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass

from .data import STAT_COLUMNS, Match

STATS = list(STAT_COLUMNS)
WINDOWS = (5, 10, 15)
SHRINK = 3.0            # pseudo-observations at the league mean
LEAGUE_MEMORY = 760     # about two seasons of league matches

# which statistics inform each target
TARGET_INPUTS = {
    "goals": ("goals", "sot", "shots"),
    "ht_goals": ("ht_goals", "goals", "sot"),
    "corners": ("corners", "shots"),
    "fouls": ("fouls",),
    "yellows": ("yellows", "fouls"),
    "reds": ("reds", "yellows", "fouls"),
}
REFEREE_TARGETS = ("fouls", "yellows", "reds", "corners")


@dataclass
class SideFeatures:
    values: dict           # feature name -> float (0 = league average)
    counts: dict           # feature name -> observations used


def _rel(x: float | None, n: int, league: float) -> float:
    if x is None or n == 0 or league <= 0:
        return 0.0
    shrunk = (n * x + SHRINK * league) / (n + SHRINK)
    return math.log((shrunk + 0.05) / (league + 0.05))


class FeatureBuilder:
    """Feed matches in date order; call `features(match)` BEFORE `update(match)`."""

    def __init__(self):
        self.team_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
        self.league_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=LEAGUE_MEMORY))
        self.h2h: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=6))
        self.ref_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=60))
        # running sums over the league window: [home_sum, away_sum, count]
        self.lsum: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0, 0])

    # league average per side and stat (home side / away side)
    def league_mean(self, league: str, stat: str, side: int) -> tuple[float, int]:
        s = self.lsum[(league, stat)]
        return (s[side] / s[2], s[2]) if s[2] else (0.0, 0)

    def league_total_mean(self, league: str, stat: str) -> float:
        s = self.lsum[(league, stat)]
        return (s[0] + s[1]) / s[2] if s[2] else 0.0

    def _side(self, m: Match, side: int) -> SideFeatures:
        team = m.home if side == 0 else m.away
        opp = m.away if side == 0 else m.home
        venue = "H" if side == 0 else "A"
        vals, counts = {}, {}
        for stat in STATS:
            lg_for, n_lg = self.league_mean(m.league, stat, side)
            lg_against = lg_for     # what this side's opponent concedes, on average, is the same quantity
            if n_lg < 30:
                continue
            vals[f"{stat}_league_mean"] = lg_for       # baseline (offset), not a model feature
            th = [r for r in self.team_hist[team] if stat in r["stats"]]
            oh = [r for r in self.team_hist[opp] if stat in r["stats"]]
            for k in WINDOWS:
                tf = [r["stats"][stat][r["side"]] for r in list(th)[-k:]]
                oa = [r["stats"][stat][1 - r["side"]] for r in list(oh)[-k:]]
                vals[f"{stat}_for_{k}"] = _rel(sum(tf) / len(tf) if tf else None, len(tf), lg_for)
                counts[f"{stat}_for_{k}"] = len(tf)
                vals[f"{stat}_opp_against_{k}"] = _rel(sum(oa) / len(oa) if oa else None, len(oa), lg_against)
                counts[f"{stat}_opp_against_{k}"] = len(oa)
            tv = [r["stats"][stat][r["side"]] for r in th if r["venue"] == venue][-10:]
            ov = [r["stats"][stat][1 - r["side"]] for r in oh if r["venue"] != venue][-10:]
            vals[f"{stat}_for_venue"] = _rel(sum(tv) / len(tv) if tv else None, len(tv), lg_for)
            counts[f"{stat}_for_venue"] = len(tv)
            vals[f"{stat}_opp_against_venue"] = _rel(sum(ov) / len(ov) if ov else None, len(ov), lg_against)
            counts[f"{stat}_opp_against_venue"] = len(ov)
            hh = [r for r in self.h2h[frozenset((team, opp))] if stat in r.stats]
            hv = [r.stats[stat][0 if r.home == team else 1] for r in hh]
            vals[f"{stat}_h2h"] = _rel(sum(hv) / len(hv) if hv else None, len(hv), lg_for)
            counts[f"{stat}_h2h"] = len(hv)
        last = self.team_hist[team][-1]["day"] if self.team_hist[team] else None
        rest = (m.day - last).days if last else None
        vals["rest"] = math.log(min(max(rest, 2), 21) / 7.0) if rest is not None else 0.0
        counts["rest"] = 1 if rest is not None else 0
        olast = self.team_hist[opp][-1]["day"] if self.team_hist[opp] else None
        orest = (m.day - olast).days if olast else None
        vals["opp_rest"] = math.log(min(max(orest, 2), 21) / 7.0) if orest is not None else 0.0
        counts["opp_rest"] = 1 if orest is not None else 0
        # referee tendency for match totals (only where the source names referees)
        for stat in REFEREE_TARGETS:
            key = f"ref_{stat}"
            if m.referee:
                rh = [sum(r.stats[stat]) for r in self.ref_hist[m.referee] if stat in r.stats]
                lg = self.league_total_mean(m.league, stat)
                vals[key] = _rel(sum(rh) / len(rh) if rh else None, len(rh), lg) if lg else 0.0
                counts[key] = len(rh)
            else:
                vals[key] = 0.0
                counts[key] = 0
        vals["home"] = 1.0 if side == 0 else 0.0
        counts["home"] = 1
        return SideFeatures(vals, counts)

    def features(self, m: Match) -> tuple[SideFeatures, SideFeatures]:
        return self._side(m, 0), self._side(m, 1)

    def update(self, m: Match):
        self.team_hist[m.home].append({"day": m.day, "venue": "H", "side": 0, "stats": m.stats})
        self.team_hist[m.away].append({"day": m.day, "venue": "A", "side": 1, "stats": m.stats})
        lh = self.league_hist[m.league]
        if len(lh) == lh.maxlen:
            old = lh[0]
            for stat, (h, a) in old.stats.items():
                s = self.lsum[(m.league, stat)]
                s[0] -= h
                s[1] -= a
                s[2] -= 1
        lh.append(m)
        for stat, (h, a) in m.stats.items():
            s = self.lsum[(m.league, stat)]
            s[0] += h
            s[1] += a
            s[2] += 1
        self.h2h[frozenset((m.home, m.away))].append(m)
        if m.referee:
            self.ref_hist[m.referee].append(m)


def design_row(sf: SideFeatures, target: str) -> tuple[list[float], list[str]]:
    """Feature vector for one side and target, with missing-data indicators."""
    names, row = [], []
    for stat in TARGET_INPUTS[target]:
        for suffix in [f"for_{k}" for k in WINDOWS] + [f"opp_against_{k}" for k in WINDOWS] + \
                      ["for_venue", "opp_against_venue", "h2h"]:
            key = f"{stat}_{suffix}"
            names.append(key)
            row.append(sf.values.get(key, 0.0))
    for key in ("rest", "opp_rest", "home"):
        names.append(key)
        row.append(sf.values.get(key, 0.0))
    if target in REFEREE_TARGETS:
        names += [f"ref_{target}", "ref_missing"]
        row += [sf.values.get(f"ref_{target}", 0.0), 0.0 if sf.counts.get(f"ref_{target}", 0) >= 5 else 1.0]
    return row, names
