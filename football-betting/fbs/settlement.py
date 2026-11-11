"""Leg and bet settlement (90-minute results)."""
from __future__ import annotations


def leg_result(market: str, selection: str, line: float | None, hg: int, ag: int,
               stat_total: int | None = None) -> str | None:
    """'won' / 'lost', or None when the needed statistic is missing."""
    if market == "h2h":
        actual = "home" if hg > ag else "away" if ag > hg else "draw"
        return "won" if selection == actual else "lost"
    if market == "double_chance":
        actual = "home" if hg > ag else "away" if ag > hg else "draw"
        covered = {"home_draw": {"home", "draw"}, "draw_away": {"draw", "away"},
                   "home_away": {"home", "away"}}[selection]
        return "won" if actual in covered else "lost"
    if market == "btts":
        yes = hg > 0 and ag > 0
        return "won" if (selection == "yes") == yes else "lost"
    if market == "totals":
        total = hg + ag
    elif market in ("corners_totals", "cards_totals"):
        if stat_total is None:
            return None
        total = stat_total
    else:
        raise ValueError(f"unknown market {market}")
    over = total > line
    return "won" if (selection == "over") == over else "lost"


def bet_outcome(legs: list[dict]) -> tuple[str | None, float]:
    """(WON/LOST/VOID or None if still pending, effective odds over non-void legs)."""
    results = [l["result"] for l in legs]
    if "lost" in results:
        return "LOST", 0.0
    if "pending" in results:
        return None, 0.0
    odds = 1.0
    for l in legs:
        if l["result"] == "won":
            odds *= l["odds"]
    if all(r == "void" for r in results):
        return "VOID", 1.0
    return "WON", odds
