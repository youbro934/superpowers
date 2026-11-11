"""Historical results (goals, corners, cards, closing odds) from football-data.co.uk.

Free, no key. Used to fit the models and to backtest. Files are cached on disk.
"""
from __future__ import annotations

import csv
import io
import os
import time
from datetime import date, datetime

from ..config import Competition
from ..models import MatchRecord
from ..teams import canonical
from ..util import ProviderError, http_request

BASE = "https://www.football-data.co.uk"


def season_codes(today: date, n: int) -> list[str]:
    start = today.year if today.month >= 7 else today.year - 1
    return [f"{(start - k) % 100:02d}{(start - k + 1) % 100:02d}" for k in range(n)]


def _parse_date(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _int(row: dict, *keys) -> int | None:
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            try:
                return int(float(v))
            except ValueError:
                return None
    return None


def _float(row: dict, *keys) -> float | None:
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            try:
                f = float(v)
                return f if f > 1.0 else None
            except ValueError:
                continue
    return None


def parse_csv(text: str, extra: bool, season_filter: set[str] | None = None) -> list[MatchRecord]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if extra:
            if season_filter and (row.get("Season") or "").strip() not in season_filter:
                continue
            home, away = row.get("Home"), row.get("Away")
            hg, ag = _int(row, "HG"), _int(row, "AG")
        else:
            home, away = row.get("HomeTeam"), row.get("AwayTeam")
            hg, ag = _int(row, "FTHG"), _int(row, "FTAG")
        d = _parse_date(row.get("Date") or "")
        if not home or not away or hg is None or ag is None or d is None:
            continue
        hy, ay, hr, ar = (_int(row, k) for k in ("HY", "AY", "HR", "AR"))
        odds = {
            "H": _float(row, "AvgCH", "AvgH", "PSCH", "B365H", "BbAvH"),
            "D": _float(row, "AvgCD", "AvgD", "PSCD", "B365D", "BbAvD"),
            "A": _float(row, "AvgCA", "AvgA", "PSCA", "B365A", "BbAvA"),
            "O25": _float(row, "AvgC>2.5", "Avg>2.5", "B365>2.5", "BbAv>2.5"),
            "U25": _float(row, "AvgC<2.5", "Avg<2.5", "B365<2.5", "BbAv<2.5"),
        }
        out.append(MatchRecord(
            day=d, home=canonical(home), away=canonical(away), hg=hg, ag=ag,
            hc=_int(row, "HC"), ac=_int(row, "AC"),
            hcards=(hy + (hr or 0)) if hy is not None else None,
            acards=(ay + (ar or 0)) if ay is not None else None,
            odds={k: v for k, v in odds.items() if v},
        ))
    return out


def _extra_season_names(today: date, n: int) -> set[str]:
    # extra-league files label seasons "2025" (calendar) or "2025/2026"
    names = set()
    for k in range(n + 1):
        y = today.year - k
        names.add(str(y))
        names.add(f"{y - 1}/{y}")
        names.add(f"{y}/{y + 1}")
    return names


class HistoryProvider:
    def __init__(self, cache_dir: str, http=http_request, max_age_hours: float = 20):
        self.cache_dir = cache_dir
        self.http = http
        self.max_age = max_age_hours * 3600
        os.makedirs(cache_dir, exist_ok=True)

    def _fetch(self, path: str) -> str | None:
        fn = os.path.join(self.cache_dir, path.replace("/", "_"))
        if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < self.max_age:
            with open(fn, encoding="utf-8") as f:
                return f.read()
        try:
            resp = self.http(f"{BASE}/{path}", provider="football-data.co.uk")
        except ProviderError as e:
            if e.status == 404:
                return None
            if os.path.exists(fn):  # stale cache beats nothing
                with open(fn, encoding="utf-8") as f:
                    return f.read()
            raise
        text = resp.text()
        with open(fn, "w", encoding="utf-8") as f:
            f.write(text)
        return text

    def load(self, comp: Competition, today: date, seasons: int = 2) -> list[MatchRecord]:
        if not comp.couk:
            return []
        if comp.couk_extra:
            text = self._fetch(f"new/{comp.couk}.csv")
            return parse_csv(text, True, _extra_season_names(today, seasons)) if text else []
        records: list[MatchRecord] = []
        for code in season_codes(today, seasons + 1):   # +1 covers the early weeks of a season
            text = self._fetch(f"mmz4281/{code}/{comp.couk}.csv")
            if text:
                records.extend(parse_csv(text, False))
        return records
