"""Probability of any supported selection, for any line, from stored model parameters.

params = {"dc": [lam, mu, rho] | None,
          "glm": {target: [mu_home, mu_away, alpha]}}   targets: goals, ht_goals, corners, fouls, yellows, reds
The model used per family matches the one the research gate validates.
"""
from __future__ import annotations

import numpy as np

from ..models import score_matrix
from ..research.glm import convolve, count_pmf, p_over

FAMILY_MODEL = {"result": "dc", "double_chance": "dc", "btts": "dc", "asian_handicap": "dc"}
COUNT_FAMILIES = {
    "goals_total": ("goals", None), "team_goals": ("goals", "team"),
    "ht_goals_total": ("ht_goals", None), "ht_team_goals": ("ht_goals", "team"),
    "corners_total": ("corners", None), "team_corners": ("corners", "team"),
    "fouls_total": ("fouls", None), "team_fouls": ("fouls", "team"),
    "yellows_total": ("yellows", None), "team_yellows": ("yellows", "team"),
}


def model_for(family: str) -> str:
    return FAMILY_MODEL.get(family, "glm")


def _side_pmf(params, target, k):
    g = params.get("glm", {}).get(target)
    if not g:
        return None
    return count_pmf(g[k], 0.0 if target == "reds" else g[2])


def _cards_pmf(params, k):
    y, r = _side_pmf(params, "yellows", k), _side_pmf(params, "reds", k)
    if y is None or r is None:
        return None
    return convolve(y, r)


def _ou(pmf, sel: str, line: float):
    po = p_over(pmf, line)
    return po if sel.endswith("over") else 1 - po


def _matrix(params):
    dc = params.get("dc")
    if not dc:
        return None
    return np.array(score_matrix(dc[0], dc[1], dc[2], 10))


def probability(params: dict, family: str, sel: str, line: float | None) -> float | None:
    """None when the needed model or data is missing (never guessed)."""
    if family in ("result", "double_chance", "btts"):
        mat = _matrix(params)
        if mat is None:
            return None
        i, j = np.indices(mat.shape)
        masks = {("result", "home"): i > j, ("result", "draw"): i == j, ("result", "away"): i < j,
                 ("double_chance", "1X"): i >= j, ("double_chance", "X2"): i <= j, ("double_chance", "12"): i != j,
                 ("btts", "yes"): (i > 0) & (j > 0), ("btts", "no"): ~((i > 0) & (j > 0))}
        m = masks.get((family, sel))
        return float(mat[m].sum()) if m is not None else None
    if line is None:
        return None
    if family in ("cards_total", "team_cards"):
        if family == "cards_total":
            a, b = _cards_pmf(params, 0), _cards_pmf(params, 1)
            return _ou(convolve(a, b), sel, line) if a is not None and b is not None else None
        side = 0 if sel.startswith("home") else 1
        pmf = _cards_pmf(params, side)
        return _ou(pmf, sel, line) if pmf is not None else None
    if family == "red_card":
        a, b = _side_pmf(params, "reds", 0), _side_pmf(params, "reds", 1)
        if a is None or b is None:
            return None
        p = p_over(convolve(a, b), 0.5)
        return p if sel == "yes" else 1 - p
    spec = COUNT_FAMILIES.get(family)
    if not spec:
        return None
    target, kind = spec
    if kind == "team":
        side = 0 if sel.startswith("home") else 1
        pmf = _side_pmf(params, target, side)
        return _ou(pmf, sel, line) if pmf is not None else None
    a, b = _side_pmf(params, target, 0), _side_pmf(params, target, 1)
    if a is None or b is None:
        return None
    return _ou(convolve(a, b), sel, line)
