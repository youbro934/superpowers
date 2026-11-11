"""Rich historical match data with provenance.

Source: football-data.co.uk CSVs (free). Every match keeps the file URL it came
from and when that file was fetched. Statistics that a file does not carry stay
None; nothing is filled in or estimated.
"""
from __future__ import annotations

import csv
import io
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..config import Competition
from ..providers.history import BASE, _extra_season_names, _parse_date, season_codes
from ..teams import canonical
from ..util import ProviderError, http_request

STAT_COLUMNS = {
    "goals": ("FTHG", "FTAG"), "ht_goals": ("HTHG", "HTAG"), "shots": ("HS", "AS"),
    "sot": ("HST", "AST"), "corners": ("HC", "AC"), "fouls": ("HF", "AF"),
    "yellows": ("HY", "AY"), "reds": ("HR", "AR"),
}
EXTRA_GOALS = ("HG", "AG")

# bookmaker column prefixes, most realistic first. "taken" = a price you could
# have bet before kick-off; "closing" = reference for closing-line value.
TAKEN_1X2 = ("B365", "Avg", "BbAv", "PS", "P")
CLOSE_1X2 = ("PSC", "AvgC", "B365C")
TAKEN_OU = ("B365", "Avg", "BbAv", "P")
CLOSE_OU = ("PC", "AvgC", "B365C")
TAKEN_AH = ("B365", "Avg", "BbAv", "P")
CLOSE_AH = ("PC", "AvgC", "B365C")


@dataclass
class Match:
    league: str
    day: date
    home: str
    away: str
    stats: dict              # stat -> (home, away) or missing
    referee: str | None
    odds: dict               # see _odds()
    source: str
    fetched_at: str
    idx: int = 0
    extra: dict = field(default_factory=dict)

    def has(self, stat: str) -> bool:
        return stat in self.stats


def _num(row: dict, key: str) -> float | None:
    v = (row.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _group(row: dict, prefixes, suffixes) -> tuple[str, tuple] | None:
    for p in prefixes:
        vals = [_num(row, p + s) for s in suffixes]
        if all(v is not None and v > 1.0 for v in vals):
            return p, tuple(vals)
    return None


def _odds(row: dict) -> dict:
    out = {}
    g = _group(row, TAKEN_1X2, ("H", "D", "A"))
    if g:
        out["1x2_taken"] = g
    g = _group(row, CLOSE_1X2, ("H", "D", "A"))
    if g:
        out["1x2_close"] = g
    g = _group(row, TAKEN_OU, (">2.5", "<2.5"))
    if g:
        out["ou25_taken"] = g
    g = _group(row, CLOSE_OU, (">2.5", "<2.5"))
    if g:
        out["ou25_close"] = g
    line = _num(row, "AHh") if _num(row, "AHh") is not None else _num(row, "BbAHh")
    g = _group(row, TAKEN_AH, ("AHH", "AHA"))
    if g and line is not None:
        out["ah_taken"] = (g[0], g[1], line)
    cline = _num(row, "AHCh")
    g = _group(row, CLOSE_AH, ("AHH", "AHA"))
    if g and cline is not None:
        out["ah_close"] = (g[0], g[1], cline)
    return out


def parse(text: str, league: str, source: str, fetched_at: str, extra: bool,
          season_filter: set[str] | None = None) -> list[Match]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if extra:
            if season_filter and (row.get("Season") or "").strip() not in season_filter:
                continue
            home, away = row.get("Home"), row.get("Away")
        else:
            home, away = row.get("HomeTeam"), row.get("AwayTeam")
        d = _parse_date(row.get("Date") or "")
        if not home or not away or d is None:
            continue
        stats = {}
        cols = dict(STAT_COLUMNS)
        if extra:
            cols["goals"] = EXTRA_GOALS
        for stat, (hc, ac) in cols.items():
            h, a = _num(row, hc), _num(row, ac)
            if h is not None and a is not None and h >= 0 and a >= 0:
                stats[stat] = (int(h), int(a))
        if "goals" not in stats:
            continue
        ref = (row.get("Referee") or "").strip() or None
        out.append(Match(league, d, canonical(home), canonical(away), stats, ref, _odds(row), source, fetched_at))
    return out


class ResearchData:
    def __init__(self, cache_dir: str, http=http_request, max_age_hours: float = 20):
        self.cache_dir = cache_dir
        self.http = http
        self.max_age = max_age_hours * 3600
        os.makedirs(cache_dir, exist_ok=True)

    def _fetch(self, path: str) -> tuple[str | None, str]:
        fn = os.path.join(self.cache_dir, path.replace("/", "_"))
        if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < self.max_age:
            with open(fn, encoding="utf-8") as f:
                return f.read(), datetime.fromtimestamp(os.path.getmtime(fn), timezone.utc).isoformat()
        try:
            resp = self.http(f"{BASE}/{path}", provider="football-data.co.uk")
        except ProviderError as e:
            if e.status == 404:
                return None, ""
            if os.path.exists(fn):
                with open(fn, encoding="utf-8") as f:
                    return f.read(), datetime.fromtimestamp(os.path.getmtime(fn), timezone.utc).isoformat()
            raise
        text = resp.text()
        with open(fn, "w", encoding="utf-8") as f:
            f.write(text)
        return text, datetime.now(timezone.utc).isoformat()

    def load(self, comp: Competition, today: date, seasons: int) -> list[Match]:
        if not comp.couk:
            return []
        out: list[Match] = []
        if comp.couk_extra:
            path = f"new/{comp.couk}.csv"
            text, ts = self._fetch(path)
            if text:
                out = parse(text, comp.code, f"{BASE}/{path}", ts, True, _extra_season_names(today, seasons))
        else:
            for code in reversed(season_codes(today, seasons)):
                path = f"mmz4281/{code}/{comp.couk}.csv"
                text, ts = self._fetch(path)
                if text:
                    out.extend(parse(text, comp.code, f"{BASE}/{path}", ts, False))
        # a fixture can only be played once per day: duplicates would leak results into "past" form
        seen, uniq = set(), []
        for m in out:
            k = (m.day, m.home, m.away)
            if k not in seen:
                seen.add(k)
                uniq.append(m)
        out = sorted(uniq, key=lambda m: m.day)
        for i, m in enumerate(out):
            m.idx = i
        return out


def coverage(matches: list[Match]) -> dict:
    """Share of matches that carry each statistic / odds group (data reliability)."""
    n = len(matches)
    if not n:
        return {"matches": 0}
    keys = list(STAT_COLUMNS) + ["referee", "1x2_taken", "1x2_close", "ou25_taken", "ou25_close", "ah_taken",
                                 "ah_close"]
    cov = {"matches": n, "from": matches[0].day.isoformat(), "to": matches[-1].day.isoformat()}
    for k in keys:
        if k in STAT_COLUMNS:
            c = sum(1 for m in matches if k in m.stats)
        elif k == "referee":
            c = sum(1 for m in matches if m.referee)
        else:
            c = sum(1 for m in matches if k in m.odds)
        cov[k] = round(c / n, 3)
    return cov
