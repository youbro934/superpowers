"""Settle any supported selection from a fixture_results row (90-minute score + statistics)."""
from __future__ import annotations

import json

STAT_FAMILIES = {"corners_total": "corners", "team_corners": "corners", "fouls_total": "fouls",
                 "team_fouls": "fouls", "yellows_total": "yellows", "team_yellows": "yellows"}


def settle_leg(family: str, sel: str, line, r) -> str | None:
    """'won' / 'lost', or None if the needed number is not published (never assumed)."""
    hg, ag = r["home_goals"], r["away_goals"]
    st = json.loads(r["stats"] or "{}")
    actual = "home" if hg > ag else "away" if ag > hg else "draw"
    if family == "result":
        return "won" if sel == actual else "lost"
    if family == "double_chance":
        return "won" if actual in {"1X": ("home", "draw"), "X2": ("draw", "away"), "12": ("home", "away")}[sel] \
            else "lost"
    if family == "btts":
        return "won" if (sel == "yes") == (hg > 0 and ag > 0) else "lost"

    def ou(value):
        if value is None:
            return None
        over = value > line
        return "won" if sel.endswith("over") == over else "lost"

    def side_val(key, side):
        v = st.get(f"{side}_{key}")
        return None if v is None else float(v)

    if family == "goals_total":
        return ou(hg + ag)
    if family == "team_goals":
        return ou(hg if sel.startswith("home") else ag)
    if family in ("ht_goals_total", "ht_team_goals"):
        if r["ht_home"] is None or r["ht_away"] is None:
            return None
        if family == "ht_goals_total":
            return ou(r["ht_home"] + r["ht_away"])
        return ou(r["ht_home"] if sel.startswith("home") else r["ht_away"])
    if family == "red_card":
        h, a = side_val("reds", "home"), side_val("reds", "away")
        y = side_val("yellows", "home")
        # API-Football omits the red-card row when there were none; trust that only if cards were reported
        if h is None and a is None and y is not None:
            h = a = 0.0
        if h is None or a is None:
            return None
        return "won" if (sel == "yes") == (h + a > 0) else "lost"
    if family in ("cards_total", "team_cards"):
        def cards(side):
            y, rd = side_val("yellows", side), side_val("reds", side)
            if y is None:
                return None
            return y + (rd or 0.0)
        if family == "cards_total":
            a, b = cards("home"), cards("away")
            return ou(None if a is None or b is None else a + b)
        return ou(cards("home" if sel.startswith("home") else "away"))
    key = STAT_FAMILIES.get(family)
    if key:
        if family.startswith("team_"):
            return ou(side_val(key, "home" if sel.startswith("home") else "away"))
        a, b = side_val(key, "home"), side_val(key, "away")
        return ou(None if a is None or b is None else a + b)
    return None
