"""Final results from football-data.org (free key, 10 req/min) and match
statistics / injuries from API-Football (optional, free 100 req/day)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..teams import canonical, match_team
from ..util import ProviderError, http_request

FDORG = "https://api.football-data.org/v4"
APIF = "https://v3.football.api-sports.io"


@dataclass
class FinalResult:
    home: str
    away: str
    day: date
    hg: int
    ag: int
    corners: int | None = None
    cards: int | None = None


class FootballDataOrg:
    name = "football-data.org"

    def __init__(self, key: str, http=http_request):
        self.key = key
        self.http = http

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def finished(self, competitions: list[str], start: date, end: date) -> list[FinalResult]:
        if not self.key:
            raise ProviderError(self.name, "FOOTBALL_DATA_ORG_KEY not configured")
        resp = self.http(f"{FDORG}/matches", params={
            "competitions": ",".join(competitions), "status": "FINISHED",
            "dateFrom": start.isoformat(), "dateTo": end.isoformat()},
            headers={"X-Auth-Token": self.key}, provider=self.name)
        out = []
        for m in resp.json().get("matches", []):
            score = m.get("score") or {}
            # settle on 90 minutes: regularTime exists when extra time was played
            ft = score.get("regularTime") or score.get("fullTime") or {}
            if ft.get("home") is None or ft.get("away") is None:
                continue
            out.append(FinalResult(canonical(m["homeTeam"]["name"]), canonical(m["awayTeam"]["name"]),
                                   date.fromisoformat(m["utcDate"][:10]), int(ft["home"]), int(ft["away"])))
        return out


def find_result(results: list[FinalResult], home: str, away: str, day: date) -> FinalResult | None:
    homes = {r.home for r in results}
    aways = {r.away for r in results}
    h = match_team(home, homes)
    a = match_team(away, aways)
    if not h or not a:
        return None
    for r in results:
        if r.home == h and r.away == a and abs((r.day - day).days) <= 1:
            return r
    return None


class APIFootball:
    name = "api-football"

    def __init__(self, key: str, http=http_request):
        self.key = key
        self.http = http

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: dict) -> list:
        if not self.key:
            raise ProviderError(self.name, "API_FOOTBALL_KEY not configured")
        data = self.http(f"{APIF}{path}", params=params, headers={"x-apisports-key": self.key},
                         provider=self.name).json()
        if data.get("errors"):
            raise ProviderError(self.name, str(data["errors"]))
        return data.get("response", [])

    def fixture_id(self, home: str, away: str, day: date) -> int | None:
        for d in (day, day - timedelta(days=1), day + timedelta(days=1)):
            fixtures = self._get("/fixtures", {"date": d.isoformat()})
            for f in fixtures:
                t = f.get("teams", {})
                if (canonical(t.get("home", {}).get("name", "")) == home
                        and canonical(t.get("away", {}).get("name", "")) == away):
                    return f["fixture"]["id"]
        return None

    def injuries(self, fixture_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._get("/injuries", {"fixture": fixture_id}):
            team = canonical(item.get("team", {}).get("name", ""))
            counts[team] = counts.get(team, 0) + 1
        return counts

    def statistics(self, fixture_id: int) -> tuple[int | None, int | None]:
        """(total corners, total cards) or Nones when not published."""
        corners = cards = 0
        got_c = got_k = False
        for team in self._get("/fixtures/statistics", {"fixture": fixture_id}):
            for s in team.get("statistics", []):
                val = s.get("value")
                if not isinstance(val, int):
                    continue
                if s.get("type") == "Corner Kicks":
                    corners += val
                    got_c = True
                elif s.get("type") in ("Yellow Cards", "Red Cards"):
                    cards += val
                    got_k = True
        return (corners if got_c else None, cards if got_k else None)
