"""Joint probability of a multi-selection entry, without blind multiplication.

* Selections from different matches: independent, so probabilities multiply.
* Selections from the same match that all depend on the score: evaluated
  together on the joint score distribution (e.g. home win AND over 2.5).
* Same-match selections mixing families with no joint model (goals + corners,
  cards + result, ...): refused (None). Corners, cards and goals are measurably
  correlated within a match (see the research report), so multiplying them
  would overstate or understate the true probability.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

GOAL_FAMILIES = {"result", "h2h", "double_chance", "goals_total", "totals", "team_goals", "btts"}


def _mask(mat: np.ndarray, family: str, sel: str, line):
    i, j = np.indices(mat.shape)
    if family in ("result", "h2h"):
        return {"home": i > j, "draw": i == j, "away": i < j}[sel]
    if family == "double_chance":
        return {"1X": i >= j, "home_draw": i >= j, "X2": i <= j, "draw_away": i <= j,
                "12": i != j, "home_away": i != j}[sel]
    if family in ("goals_total", "totals"):
        return (i + j > line) if sel == "over" else (i + j < line)
    if family == "team_goals":
        side, ou = sel.split("_")
        g = i if side == "home" else j
        return (g > line) if ou == "over" else (g < line)
    if family == "btts":
        return ((i > 0) & (j > 0)) if sel == "yes" else ~((i > 0) & (j > 0))
    raise KeyError(family)


def joint_probability(legs, matrices: dict, singles: dict) -> float | None:
    """legs: (event_id, family, selection, line). matrices: event_id -> score matrix.
    singles: leg tuple -> probability for non-goal selections."""
    by_event = defaultdict(list)
    for leg in legs:
        by_event[leg[0]].append(leg)
    total = 1.0
    for ev, group in by_event.items():
        mat = matrices.get(ev)
        if all(l[1] in GOAL_FAMILIES for l in group) and mat is not None:
            mask = np.ones(mat.shape, bool)
            for _, fam, sel, line in group:
                mask &= _mask(mat, fam, sel, line)
            total *= float(mat[mask].sum())
        elif len(group) == 1 and group[0] in singles:
            total *= singles[group[0]]
        else:
            return None
    return total
