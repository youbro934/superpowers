"""Forward data collection from API-Football (free plan: 100 requests/day).

Builds the evidence that free historical data lacks: bookmaker prices for
corners, cards, fouls, team goals, BTTS, double chance and half markets, plus
xG, shots, possession, referees, lineups and injuries. Records are appended
to dated gzip JSON-lines files with source and fetch time; nothing is
estimated. Priority: favorite teams, then Brazilian competitions, then the
other priority leagues.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta

from ..teams import canonical, is_favorite
from ..util import ProviderError, http_request, iso, parse_iso, utcnow

BASE = "https://v3.football.api-sports.io"

# API-Football league ids, highest priority first (Brazil first, as requested)
PRIORITY_LEAGUES = {
    71: "Brasileirao Serie A", 73: "Copa do Brasil", 13: "Copa Libertadores", 72: "Brasileirao Serie B",
    94: "Primeira Liga", 140: "La Liga", 61: "Ligue 1", 78: "Bundesliga", 2: "UEFA Champions League",
    39: "Premier League", 135: "Serie A", 3: "UEFA Europa League",
}
STAT_NAMES = {
    "Shots on Goal": "sot", "Total Shots": "shots", "Fouls": "fouls", "Corner Kicks": "corners",
    "Yellow Cards": "yellows", "Red Cards": "reds", "Ball Possession": "possession",
    "expected_goals": "xg", "Offsides": "offsides", "Goalkeeper Saves": "saves",
}


class Collector:
    name = "api-football"

    def __init__(self, key: str, archive_dir: str, store, http=http_request, daily_budget: int = 90,
                 now: datetime | None = None):
        self.key = key
        self.dir = archive_dir
        self.store = store
        self.http = http
        self.budget = daily_budget
        self._now = now
        os.makedirs(archive_dir, exist_ok=True)

    @property
    def now(self) -> datetime:
        return self._now or utcnow()

    # ---------------------------------------------------------------- plumbing
    def _used_key(self) -> str:
        return f"apif_used:{self.now.date().isoformat()}"

    def remaining(self) -> int:
        return self.budget - self.store.get(self._used_key(), 0)

    def _get(self, path: str, params: dict) -> list:
        if self.remaining() <= 0:
            raise ProviderError(self.name, "daily request budget used")
        self.store.set(self._used_key(), self.store.get(self._used_key(), 0) + 1)
        resp = self.http(f"{BASE}{path}", params=params, headers={"x-apisports-key": self.key},
                         provider=self.name)
        data = resp.json()
        if data.get("errors"):
            raise ProviderError(self.name, str(data["errors"])[:300])
        return data.get("response", [])

    def _write(self, records: list[dict]):
        if not records:
            return
        path = os.path.join(self.dir, f"{self.now.date().isoformat()}.jsonl.gz")
        with gzip.open(path, "at", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    def _meta(self, kind: str) -> dict:
        return {"kind": kind, "source": self.name, "fetched_at": iso(self.now)}

    # ----------------------------------------------------------------- steps
    def fixtures(self, day) -> list[dict]:
        cache = self.store.get(f"apif_fixtures:{day.isoformat()}")
        if cache is not None:
            if cache.get("final"):
                return cache["fixtures"]            # past day already fetched after it ended
            if day >= self.now.date() and self.now - parse_iso(cache["at"]) < timedelta(hours=12):
                return cache["fixtures"]
        out = []
        for f in self._get("/fixtures", {"date": day.isoformat()}):
            lg = f.get("league", {}).get("id")
            if lg not in PRIORITY_LEAGUES:
                continue
            fx, t = f["fixture"], f["teams"]
            out.append({"id": fx["id"], "league": lg, "kickoff": fx["date"], "referee": fx.get("referee"),
                        "status": fx.get("status", {}).get("short"), "home": t["home"]["name"],
                        "away": t["away"]["name"], "goals": f.get("goals"),
                        "ht": (f.get("score") or {}).get("halftime")})
        self.store.set(f"apif_fixtures:{day.isoformat()}",
                       {"at": iso(self.now), "final": day < self.now.date(), "fixtures": out})
        self._write([self._meta("fixture") | fx for fx in out])
        return out

    @staticmethod
    def priority(fx: dict) -> tuple:
        fav = is_favorite(fx["home"]) or is_favorite(fx["away"])
        brazil = fx["league"] in (71, 72, 73, 13)
        order = list(PRIORITY_LEAGUES).index(fx["league"])
        return (not fav, not brazil, order)

    def odds_snapshot(self, fx: dict, label: str):
        recs = []
        for item in self._get("/odds", {"fixture": fx["id"]}):
            for bm in item.get("bookmakers", []):
                for bet in bm.get("bets", []):
                    for v in bet.get("values", []):
                        try:
                            odd = float(v.get("odd"))
                        except (TypeError, ValueError):
                            continue
                        recs.append(self._meta("odds") | {
                            "fixture": fx["id"], "league": fx["league"], "home": canonical(fx["home"]),
                            "away": canonical(fx["away"]), "kickoff": fx["kickoff"], "snapshot": label,
                            "bookmaker": bm.get("name"), "bet": bet.get("name"), "value": str(v.get("value")),
                            "odd": odd})
        self._write(recs)
        self.store.set(f"apif_odds:{fx['id']}:{label}", iso(self.now))
        return len(recs)

    def statistics(self, fx: dict):
        stats = {}
        for team in self._get("/fixtures/statistics", {"fixture": fx["id"]}):
            side = "home" if canonical(team.get("team", {}).get("name", "")) == canonical(fx["home"]) else "away"
            for s in team.get("statistics", []):
                key = STAT_NAMES.get(s.get("type"))
                val = s.get("value")
                if key is None or val is None:
                    continue          # missing stays missing
                if isinstance(val, str) and val.endswith("%"):
                    val = float(val[:-1])
                try:
                    stats[f"{side}_{key}"] = float(val)
                except (TypeError, ValueError):
                    continue
        self._write([self._meta("stats") | {"fixture": fx["id"], "league": fx["league"],
                                            "home": canonical(fx["home"]), "away": canonical(fx["away"]),
                                            "kickoff": fx["kickoff"], "referee": fx.get("referee"),
                                            "goals": fx.get("goals"), "ht": fx.get("ht"), "stats": stats}])
        self.store.set(f"apif_stats:{fx['id']}", iso(self.now))

    def team_news(self, fx: dict):
        lineups = self._get("/fixtures/lineups", {"fixture": fx["id"]})
        injuries = self._get("/injuries", {"fixture": fx["id"]})
        self._write([self._meta("team_news") | {
            "fixture": fx["id"], "home": canonical(fx["home"]), "away": canonical(fx["away"]),
            "lineups": [{"team": canonical(l.get("team", {}).get("name", "")), "formation": l.get("formation"),
                         "start_xi": [p.get("player", {}).get("name") for p in l.get("startXI", [])]}
                        for l in lineups],
            "injuries": [{"team": canonical(i.get("team", {}).get("name", "")),
                          "player": i.get("player", {}).get("name"), "type": i.get("player", {}).get("type"),
                          "reason": i.get("player", {}).get("reason")} for i in injuries]}])
        self.store.set(f"apif_news:{fx['id']}", iso(self.now))

    # ------------------------------------------------------------------ cycle
    def run(self) -> dict:
        """One collection pass within the daily budget. Returns counters."""
        done = {"odds_open": 0, "odds_close": 0, "stats": 0, "news": 0}
        today = self.now.date()
        upcoming = self.fixtures(today) + self.fixtures(today + timedelta(days=1))
        finished = self.fixtures(today - timedelta(days=1)) + [f for f in upcoming if f["status"] == "FT"]
        upcoming.sort(key=self.priority)
        for fx in upcoming:
            if self.remaining() <= 5:
                break
            ko = parse_iso(fx["kickoff"])
            if ko <= self.now:
                continue
            if not self.store.get(f"apif_odds:{fx['id']}:open"):
                self.odds_snapshot(fx, "open")
                done["odds_open"] += 1
            elif ko - self.now <= timedelta(hours=2) and not self.store.get(f"apif_odds:{fx['id']}:close"):
                self.odds_snapshot(fx, "close")
                done["odds_close"] += 1
            if (is_favorite(fx["home"]) or is_favorite(fx["away"])) and ko - self.now <= timedelta(minutes=75) \
                    and not self.store.get(f"apif_news:{fx['id']}"):
                self.team_news(fx)
                done["news"] += 1
        finished.sort(key=self.priority)
        for fx in finished:
            if self.remaining() <= 2:
                break
            if fx["status"] in ("FT", "AET", "PEN") and not self.store.get(f"apif_stats:{fx['id']}"):
                self.statistics(fx)
                done["stats"] += 1
        return done
