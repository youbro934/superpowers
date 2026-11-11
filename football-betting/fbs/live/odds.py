"""Current bookmaker prices from API-Football snapshots.

Snapshot format (compact, one per fixture):
  {"fixture": id, "fetched_at": iso, "books": {bookmaker: {bet_name: {value: odd}}}}
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

_EXACT = {
    "match winner": "result", "double chance": "double_chance", "goals over/under": "goals_total",
    "both teams score": "btts", "goals over/under first half": "ht_goals_total",
    "total - home": "team_goals:home", "total - away": "team_goals:away",
    "corners over under": "corners_total", "corners over/under": "corners_total",
    "total corners": "corners_total", "home corners over/under": "team_corners:home",
    "away corners over/under": "team_corners:away", "cards over/under": "cards_total",
    "total cards": "cards_total", "home team total cards": "team_cards:home",
    "away team total cards": "team_cards:away", "fouls over/under": "fouls_total",
    "total fouls": "fouls_total", "home team fouls": "team_fouls:home", "away team fouls": "team_fouls:away",
    "yellow cards over/under": "yellows_total", "red card": "red_card",
}
_VALUES = {"home": "home", "draw": "draw", "away": "away", "yes": "yes", "no": "no",
           "home/draw": "1X", "draw/away": "X2", "home/away": "12", "1x": "1X", "x2": "X2", "12": "12"}
# names that look relevant but whose settlement rules are ambiguous: never mapped
_EXCLUDE = re.compile(r"(1x2|asian|handicap|exact|first|second|race|odd/even|ht/ft|3 way|bookings|booking points)",
                      re.I)


def map_bet(bet: str, value: str):
    """(family, selection, line) or None. Conservative: unknown or ambiguous names are skipped."""
    name = bet.strip().lower()
    fam = _EXACT.get(name)
    if fam is None:
        if _EXCLUDE.search(name):
            return None
        # e.g. "Home Team Corners Over/Under", "Away Total Cards"
        side = "home" if re.search(r"\bhome\b", name) else "away" if re.search(r"\baway\b", name) else None
        for key, base in (("corner", "corners"), ("card", "cards"), ("foul", "fouls")):
            if key in name and ("over" in name or "total" in name):
                fam = f"team_{base}:{side}" if side else f"{base}_total"
                break
        if fam is None:
            return None
    side = None
    if ":" in fam:
        fam, side = fam.split(":")
    v = str(value).strip()
    parts = v.split()
    if parts and parts[0].lower() in ("over", "under"):
        try:
            line = float(parts[1])
        except (IndexError, ValueError):
            return None
        if abs(line % 1 - 0.5) > 1e-9:
            return None              # whole/quarter lines can push or split: not traded
        ou = parts[0].lower()
        return fam, (f"{side}_{ou}" if side else ou), line
    if fam == "red_card":
        sel = _VALUES.get(v.lower())
        return (fam, sel, 0.5) if sel in ("yes", "no") else None
    sel = _VALUES.get(v.lower())
    if sel is None or side is not None:
        return None
    if fam in ("result",) and sel not in ("home", "draw", "away"):
        return None
    if fam == "double_chance" and sel not in ("1X", "X2", "12"):
        return None
    if fam == "btts" and sel not in ("yes", "no"):
        return None
    return fam, sel, None


@dataclass
class Price:
    family: str
    sel: str
    line: float | None
    best: float
    bookmaker: str
    fair_p: float | None       # de-vigged consensus probability
    n_books: int
    fetched_at: str


def _partners(fam, sel, line):
    if fam == "result":
        return [(fam, s, None) for s in ("home", "draw", "away")]
    if fam == "btts":
        return [(fam, s, None) for s in ("yes", "no")]
    if fam == "red_card":
        return [(fam, s, line) for s in ("yes", "no")]
    if fam == "double_chance":
        return None
    base = sel.rsplit("_", 1)[0] + "_" if "_" in sel else ""
    return [(fam, base + "over", line), (fam, base + "under", line)]


def prices(snapshot: dict, allowed_books: list[str] | None = None) -> tuple[dict, dict]:
    """Returns ({(family, sel, line): Price}, {unmapped bet name: count})."""
    per_book = defaultdict(dict)
    unmapped = defaultdict(int)
    for book, bets in snapshot.get("books", {}).items():
        for bet, values in bets.items():
            for value, odd in values.items():
                k = map_bet(bet, value)
                if k is None:
                    unmapped[bet] += 1
                    continue
                if odd and odd > 1.0:
                    per_book[book][k] = odd
    best: dict = {}
    for book, sels in per_book.items():
        if allowed_books and book not in allowed_books:
            continue
        for k, odd in sels.items():
            if k not in best or odd > best[k][0]:
                best[k] = (odd, book)
    fair = defaultdict(list)
    for book, sels in per_book.items():
        done = set()
        for k in sels:
            group = _partners(*k)
            if not group or tuple(group) in done or any(g not in sels for g in group):
                continue
            done.add(tuple(group))
            inv = {g: 1 / sels[g] for g in group}
            t = sum(inv.values())
            for g in group:
                fair[g].append(inv[g] / t)
    # double chance fair value from fair 1X2
    h, d, a = (fair.get(("result", s, None)) for s in ("home", "draw", "away"))
    if h and d and a:
        H, D, A = (sum(x) / len(x) for x in (h, d, a))
        for s, v in (("1X", H + D), ("X2", D + A), ("12", H + A)):
            fair[("double_chance", s, None)] = [v]
    out = {}
    for k, (odd, book) in best.items():
        fp = fair.get(k)
        out[k] = Price(k[0], k[1], k[2], odd, book, sum(fp) / len(fp) if fp else None,
                       sum(1 for b in per_book.values() if k in b), snapshot.get("fetched_at", ""))
    return out, dict(unmapped)
