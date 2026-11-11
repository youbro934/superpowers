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
                 now: datetime | None = None, cache_dir: str | None = None, refresh_hours: float = 3.0):
        self.key = key
        self.dir = archive_dir
        self.cache_dir = cache_dir or os.path.join(archive_dir, "..", "current-odds")
        self.refresh_hours = refresh_hours
        os.makedirs(self.cache_dir, exist_ok=True)
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

    def _get_full(self, path: str, params: dict) -> dict:
        if self.remaining() <= 0:
            raise ProviderError(self.name, "daily request budget used")
        self.store.set(self._used_key(), self.store.get(self._used_key(), 0) + 1)
        resp = self.http(f"{BASE}{path}", params=params, headers={"x-apisports-key": self.key},
                         provider=self.name)
        data = resp.json()
        if data.get("errors"):
            # e.g. free-plan season restrictions: reported, never worked around
            raise ProviderError(self.name, str(data["errors"])[:300])
        return data

    def _get(self, path: str, params: dict) -> list:
        return self._get_full(path, params).get("response", [])

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
            score = f.get("score") or {}
            ft = score.get("fulltime") or {}
            out.append({"id": fx["id"], "league": lg, "season": f.get("league", {}).get("season"),
                        "kickoff": fx["date"], "referee": fx.get("referee"),
                        "status": fx.get("status", {}).get("short"), "home": t["home"]["name"],
                        "away": t["away"]["name"],
                        # settle on 90 minutes: score.fulltime excludes extra time
                        "goals": ft if ft.get("home") is not None else f.get("goals"),
                        "ht": score.get("halftime")})
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

    def odds_league_day(self, league: int, season, day: str) -> dict:
        """All fixtures' odds for one league and date (10 fixtures per request)."""
        snaps, page = {}, 1
        while True:
            params = {"league": league, "date": day, "page": page}
            if season:
                params["season"] = season
            data = self._get_full("/odds", params)
            for item in data.get("response", []):
                fid = item.get("fixture", {}).get("id")
                books = {}
                for bm in item.get("bookmakers", []):
                    bets = books.setdefault(bm.get("name", "?"), {})
                    for bet in bm.get("bets", []):
                        vals = bets.setdefault(bet.get("name", "?"), {})
                        for v in bet.get("values", []):
                            try:
                                vals[str(v.get("value"))] = float(v.get("odd"))
                            except (TypeError, ValueError):
                                continue
                if fid:
                    snaps[fid] = {"fixture": fid, "fetched_at": iso(self.now), "books": books}
            paging = data.get("paging") or {}
            if page >= int(paging.get("total") or 1) or self.remaining() <= 0:
                break
            page += 1
        return snaps

    def current(self, fixture_id) -> dict | None:
        fn = os.path.join(self.cache_dir, f"{fixture_id}.json")
        if not os.path.exists(fn):
            return None
        with open(fn, encoding="utf-8") as f:
            return json.load(f)

    def _save_current(self, fx: dict, snap: dict):
        with open(os.path.join(self.cache_dir, f"{fx['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(snap, f)
        ko = parse_iso(fx["kickoff"])
        label = None
        if not self.store.get(f"apif_odds:{fx['id']}:open"):
            label = "open"
        elif ko - self.now <= timedelta(hours=2, minutes=30):
            label = "close"           # the last one before kick-off wins when loading
        if label:
            self._write([self._meta("odds_snapshot") | {
                "fixture": fx["id"], "league": fx["league"], "home": canonical(fx["home"]),
                "away": canonical(fx["away"]), "kickoff": fx["kickoff"], "snapshot": label,
                "books": snap["books"]}])
            self.store.set(f"apif_odds:{fx['id']}:{label}", iso(self.now))

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
        g, ht = fx.get("goals") or {}, fx.get("ht") or {}
        self.store.x("INSERT INTO fixture_results(fixture_id,home_goals,away_goals,ht_home,ht_away,stats,fetched_at) "
                     "VALUES(?,?,?,?,?,?,?) ON CONFLICT(fixture_id) DO UPDATE SET home_goals=excluded.home_goals, "
                     "away_goals=excluded.away_goals, ht_home=excluded.ht_home, ht_away=excluded.ht_away, "
                     "stats=excluded.stats, fetched_at=excluded.fetched_at",
                     (fx["id"], g.get("home"), g.get("away"), ht.get("home"), ht.get("away"), json.dumps(stats),
                      iso(self.now)))

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
    def run(self, priority_ids: set | None = None) -> dict:
        """One collection pass within the daily budget. Returns counters."""
        priority_ids = priority_ids or set()
        done = {"odds_requests": 0, "fixtures_priced": 0, "stats": 0, "news": 0}
        today = self.now.date()
        upcoming = [f for f in self.fixtures(today) + self.fixtures(today + timedelta(days=1))
                    if parse_iso(f["kickoff"]) > self.now]
        finished = self.fixtures(today - timedelta(days=1)) + [
            f for f in self.fixtures(today) if f["status"] in ("FT", "AET", "PEN")]
        # 1) results for fixtures with open entries come first (settlement)
        finished.sort(key=lambda f: (f["id"] not in priority_ids,) + self.priority(f))
        stats_reserve = 20
        for fx in finished:
            if fx["id"] in priority_ids and self.remaining() > 1 and not self.store.get(f"apif_stats:{fx['id']}"):
                self.statistics(fx)
                done["stats"] += 1
        # 2) current odds, by league and date, favorites / Brazil first
        groups: dict = {}
        for fx in sorted(upcoming, key=self.priority):
            if parse_iso(fx["kickoff"]) - self.now > timedelta(hours=36):
                continue
            groups.setdefault((fx["league"], fx.get("season"), fx["kickoff"][:10]), []).append(fx)
        for (league, season, day), fxs in groups.items():
            if self.remaining() <= stats_reserve:
                break
            key = f"apif_oddsq:{league}:{day}"
            last = self.store.get(key)
            soon = min(parse_iso(f["kickoff"]) for f in fxs) - self.now
            due = (last is None or self.now - parse_iso(last) >= timedelta(hours=self.refresh_hours)
                   or (soon <= timedelta(hours=2, minutes=30) and self.now - parse_iso(last) >= timedelta(hours=1)))
            if not due:
                continue
            before = self.remaining()
            snaps = self.odds_league_day(league, season, day)
            done["odds_requests"] += before - self.remaining()
            self.store.set(key, iso(self.now))
            for fx in fxs:
                if fx["id"] in snaps:
                    self._save_current(fx, snaps[fx["id"]])
                    done["fixtures_priced"] += 1
        # 3) team news for favorites close to kick-off
        for fx in upcoming:
            ko = parse_iso(fx["kickoff"])
            if (is_favorite(fx["home"]) or is_favorite(fx["away"])) and ko - self.now <= timedelta(minutes=75) \
                    and not self.store.get(f"apif_news:{fx['id']}") and self.remaining() > 4:
                self.team_news(fx)
                done["news"] += 1
        # 4) statistics for the rest of the finished priority fixtures (model history)
        for fx in finished:
            if self.remaining() <= 2:
                break
            if fx["status"] in ("FT", "AET", "PEN") and not self.store.get(f"apif_stats:{fx['id']}"):
                self.statistics(fx)
                done["stats"] += 1
        return done

    def upcoming(self) -> list[dict]:
        today = self.now.date()
        out = []
        for d in (today, today + timedelta(days=1)):
            c = self.store.get(f"apif_fixtures:{d.isoformat()}")
            if c:
                out += [f for f in c["fixtures"] if parse_iso(f["kickoff"]) > self.now]
        return out
