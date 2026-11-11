"""The Odds API v4 client (https://the-odds-api.com). Free plan: 500 credits/month.

/events is free; /odds costs markets x regions credits; /scores costs 1 (2 with daysFrom).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..teams import canonical
from ..util import ProviderError, http_request, iso, parse_iso, utcnow

BASE = "https://api.the-odds-api.com/v4"


@dataclass
class Quote:
    market: str          # h2h | totals | btts | double_chance | corners_totals | cards_totals
    selection: str       # home/draw/away, over/under, yes/no, home_draw/draw_away/home_away
    line: float | None
    bookmaker: str
    price: float
    last_update: datetime | None = None


@dataclass
class Event:
    event_id: str
    sport_key: str
    home: str            # canonical
    away: str
    home_raw: str
    away_raw: str
    kickoff: datetime
    quotes: list[Quote] = field(default_factory=list)
    fetched_at: datetime | None = None


def _side(name: str, home_raw: str, away_raw: str) -> str | None:
    n = canonical(name)
    if n == "draw":
        return "draw"
    if n == canonical(home_raw):
        return "home"
    if n == canonical(away_raw):
        return "away"
    return None


_DC = {frozenset({"home", "draw"}): "home_draw", frozenset({"draw", "away"}): "draw_away",
       frozenset({"home", "away"}): "home_away"}


def _market_key(key: str) -> str | None:
    if key in ("h2h", "h2h_3_way"):
        return "h2h"
    if key in ("totals", "alternate_totals"):
        return "totals"
    if key in ("btts", "double_chance"):
        return key
    if "corner" in key and "total" in key:
        return "corners_totals"
    if "card" in key and "total" in key:
        return "cards_totals"
    return None


def parse_event(raw: dict, sport_key: str) -> Event:
    home_raw, away_raw = raw["home_team"], raw["away_team"]
    ev = Event(raw["id"], sport_key, canonical(home_raw), canonical(away_raw), home_raw, away_raw,
               parse_iso(raw["commence_time"]))
    for bm in raw.get("bookmakers", []):
        for mk in bm.get("markets", []):
            market = _market_key(mk.get("key", ""))
            if market is None:
                continue
            upd = mk.get("last_update") or bm.get("last_update")
            upd_dt = parse_iso(upd) if upd else None
            for oc in mk.get("outcomes", []):
                price = oc.get("price")
                if not isinstance(price, (int, float)) or price <= 1.0:
                    continue
                name = str(oc.get("name", ""))
                line = oc.get("point")
                if market == "h2h":
                    sel = _side(name, home_raw, away_raw)
                elif market in ("totals", "corners_totals", "cards_totals"):
                    sel = name.lower() if name.lower() in ("over", "under") else None
                    if line is None or abs((float(line) % 1) - 0.5) > 1e-9:
                        sel = None   # only half lines: no pushes, no quarter-line splits
                elif market == "btts":
                    sel = name.lower() if name.lower() in ("yes", "no") else None
                    line = None
                else:  # double chance
                    parts = [p.strip() for p in name.replace(" or ", "/").split("/")]
                    sides = frozenset(filter(None, (_side(p, home_raw, away_raw) for p in parts)))
                    sel = _DC.get(sides) if len(parts) == 2 else None
                    line = None
                if sel is None:
                    continue
                ev.quotes.append(Quote(market, sel, float(line) if line is not None else None,
                                       bm.get("key", "?"), float(price), upd_dt))
    return ev


class OddsAPI:
    name = "the-odds-api"

    def __init__(self, api_key: str, http=http_request):
        self.key = api_key
        self.http = http
        self.remaining: int | None = None
        self.used_this_run = 0

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: dict):
        if not self.key:
            raise ProviderError(self.name, "ODDS_API_KEY not configured")
        resp = self.http(f"{BASE}{path}", params={"apiKey": self.key, **params}, provider=self.name)
        rem = resp.headers.get("x-requests-remaining")
        if rem is not None:
            try:
                self.remaining = int(float(rem))
            except ValueError:
                pass
        last = resp.headers.get("x-requests-last")
        if last:
            try:
                self.used_this_run += int(float(last))
            except ValueError:
                pass
        return resp.json()

    def events(self, sport: str, start: datetime, end: datetime) -> list[Event]:
        data = self._get(f"/sports/{sport}/events",
                         {"commenceTimeFrom": iso(start), "commenceTimeTo": iso(end)})
        return [parse_event(e, sport) for e in data]

    def odds(self, sport: str, markets: str, regions: str, start: datetime, end: datetime) -> list[Event]:
        data = self._get(f"/sports/{sport}/odds", {
            "regions": regions, "markets": markets, "oddsFormat": "decimal",
            "commenceTimeFrom": iso(start), "commenceTimeTo": iso(end)})
        now = utcnow()
        out = [parse_event(e, sport) for e in data]
        for e in out:
            e.fetched_at = now
        return out

    def event_odds(self, sport: str, event_id: str, markets: str, regions: str) -> Event:
        data = self._get(f"/sports/{sport}/events/{event_id}/odds",
                         {"regions": regions, "markets": markets, "oddsFormat": "decimal"})
        ev = parse_event(data, sport)
        ev.fetched_at = utcnow()
        return ev

    def scores(self, sport: str, days_from: int = 3) -> dict[str, tuple[int, int]]:
        """event_id -> (home_goals, away_goals) for completed events."""
        data = self._get(f"/sports/{sport}/scores", {"daysFrom": days_from})
        out = {}
        for e in data:
            if not e.get("completed") or not e.get("scores"):
                continue
            by_name = {canonical(s["name"]): s["score"] for s in e["scores"]}
            try:
                out[e["id"]] = (int(by_name[canonical(e["home_team"])]), int(by_name[canonical(e["away_team"])]))
            except (KeyError, ValueError, TypeError):
                continue
        return out
