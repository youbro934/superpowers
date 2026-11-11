"""SYNTHETIC test data and a fake HTTP layer. Nothing here is real football data."""
from __future__ import annotations

import json
import math
import random
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from fbs.util import ProviderError, Response

TEAMS = ["Alpha FC", "Bravo United", "Charlie City", "Delta Rovers", "Echo Athletic", "Foxtrot Town",
         "Golf Wanderers", "Hotel Albion", "India Park", "Juliet Rangers", "Kilo Borough", "Lima County"]
STRENGTH = {t: (1.6 - 0.09 * i, 0.7 + 0.06 * i) for i, t in enumerate(TEAMS)}   # attack, weakness


def _poisson(rng, lam):
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def synthetic_csv(end: date, seasons: int = 2, seed: int = 7, home_adv: float = 1.25) -> str:
    rng = random.Random(seed)
    rows = ["Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC,HY,AY,HR,AR,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5"]
    day = end - timedelta(days=330 * seasons)
    while day < end:
        teams = TEAMS[:]
        rng.shuffle(teams)
        for i in range(0, len(teams), 2):
            h, a = teams[i], teams[i + 1]
            lam = STRENGTH[h][0] * STRENGTH[a][1] * home_adv
            mu = STRENGTH[a][0] * STRENGTH[h][1]
            hg, ag = _poisson(rng, lam), _poisson(rng, mu)
            hc, ac = _poisson(rng, 5.5), _poisson(rng, 4.5)
            rows.append(f"X1,{day.strftime('%d/%m/%Y')},{h},{a},{hg},{ag},{hc},{ac},"
                        f"{_poisson(rng, 1.8)},{_poisson(rng, 2.0)},0,0,2.0,3.4,3.8,1.9,1.9")
        day += timedelta(days=7)
    return "\n".join(rows) + "\n"


class FakeWorld:
    """Routes provider URLs to in-memory data and records outgoing notifications."""

    def __init__(self, today: date):
        self.csv = synthetic_csv(today)
        self.events: list[dict] = []       # Odds API event dicts with bookmakers
        self.scores: dict[str, tuple[int, int]] = {}
        self.pushes: list[dict] = []
        self.remaining = 480
        self.fail_odds = False
        self.calls: list[str] = []

    def add_event(self, eid, home, away, kickoff: datetime, h2h=(1.30, 5.5, 9.0), totals=None):
        books = []
        for name, bump in (("bookA", 0.0), ("bookB", 0.02)):
            markets = [{"key": "h2h", "outcomes": [
                {"name": home, "price": h2h[0] + bump}, {"name": "Draw", "price": h2h[1]},
                {"name": away, "price": h2h[2]}]}]
            if totals:
                markets.append({"key": "totals", "outcomes": [
                    {"name": "Over", "price": totals[0], "point": 2.5},
                    {"name": "Under", "price": totals[1], "point": 2.5}]})
            books.append({"key": name, "markets": markets})
        self.events.append({"id": eid, "sport_key": "soccer_epl", "home_team": home, "away_team": away,
                            "commence_time": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"), "bookmakers": books})

    def __call__(self, url, *, method="GET", params=None, headers=None, data=None, provider="http", **kw):
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        self.calls.append(full)
        u = urllib.parse.urlparse(url)
        if "football-data.co.uk" in u.netloc:
            if u.path.endswith("/E0.csv"):
                return Response(200, {}, self.csv.encode())
            raise ProviderError(provider, "HTTP 404", 404)
        if "the-odds-api" in u.netloc:
            parts = u.path.split("/")
            sport = parts[3]
            if sport != "soccer_epl":
                return Response(200, {"x-requests-remaining": str(self.remaining)}, b"[]")
            if parts[-1] == "events":
                body = [{k: v for k, v in e.items() if k != "bookmakers"} for e in self.events]
                return Response(200, {"x-requests-remaining": str(self.remaining), "x-requests-last": "0"},
                                json.dumps(body).encode())
            if parts[-1] == "odds" and len(parts) == 5:
                if self.fail_odds:
                    raise ProviderError(provider, "HTTP 503", 503)
                self.remaining -= 2
                return Response(200, {"x-requests-remaining": str(self.remaining), "x-requests-last": "2"},
                                json.dumps(self.events).encode())
            if parts[-1] == "odds":   # event odds (extra markets)
                raise ProviderError(provider, "HTTP 422", 422)
            if parts[-1] == "scores":
                out = []
                for e in self.events:
                    if e["id"] in self.scores:
                        hg, ag = self.scores[e["id"]]
                        out.append({"id": e["id"], "completed": True, "home_team": e["home_team"],
                                    "away_team": e["away_team"],
                                    "scores": [{"name": e["home_team"], "score": str(hg)},
                                               {"name": e["away_team"], "score": str(ag)}]})
                return Response(200, {"x-requests-remaining": str(self.remaining), "x-requests-last": "2"},
                                json.dumps(out).encode())
        if "ntfy" in u.netloc:
            msg = json.loads(data.decode())
            prio = {1: "min", 2: "low", 3: "default", 4: "high", 5: "urgent"}[msg["priority"]]
            self.pushes.append({"title": msg["title"], "body": msg["message"], "priority": prio})
            return Response(200, {}, b"{}")
        raise ProviderError(provider, f"unexpected url {url}", 404)


def synthetic_full_csv(end: date, seasons: int = 3, seed: int = 11, margin: float = 0.05,
                       foul_scale: float = 1.0, prefix: str = "") -> str:
    """SYNTHETIC league with every statistic and bookmaker prices built from the TRUE
    probabilities plus a margin: a correct backtest must find no edge here."""
    from fbs.models import goal_market_probs, score_matrix
    rng = random.Random(seed)
    refs = [prefix + r for r in ("Ref A", "Ref B", "Ref C", "Ref D")]
    ref_card = {prefix + k: v for k, v in {"Ref A": 0.7, "Ref B": 1.0, "Ref C": 1.2, "Ref D": 1.5}.items()}
    cols = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HTHG,HTAG,Referee,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR,"
            "B365H,B365D,B365A,PSCH,PSCD,PSCA,B365>2.5,B365<2.5,PC>2.5,PC<2.5,AHh,B365AHH,B365AHA")
    rows = [cols]
    day = end - timedelta(days=330 * seasons)
    while day < end:
        teams = TEAMS[:]
        rng.shuffle(teams)
        for i in range(0, len(teams), 2):
            h, a = teams[i], teams[i + 1]
            lam = STRENGTH[h][0] * STRENGTH[a][1] * 1.25
            mu = STRENGTH[a][0] * STRENGTH[h][1]
            hg, ag = _poisson(rng, lam), _poisson(rng, mu)
            hth, hta = min(hg, _poisson(rng, lam * 0.45)), min(ag, _poisson(rng, mu * 0.45))
            ref = rng.choice(refs)
            hs, as_ = _poisson(rng, 6 * lam + 4), _poisson(rng, 6 * mu + 3)
            hst, ast = min(hs, _poisson(rng, 2.5 * lam + 1)), min(as_, _poisson(rng, 2.5 * mu + 1))
            hf, af = _poisson(rng, 11 * foul_scale), _poisson(rng, 12 * foul_scale)
            hc, ac = _poisson(rng, 3 + 1.5 * lam), _poisson(rng, 2.5 + 1.5 * mu)
            hy, ay = _poisson(rng, 1.6 * ref_card[ref]), _poisson(rng, 1.9 * ref_card[ref])
            hr, ar = int(rng.random() < 0.05), int(rng.random() < 0.06)
            p = goal_market_probs(score_matrix(lam, mu, 0.0))
            ph, pd, pa = p[("h2h", "home", None)], p[("h2h", "draw", None)], p[("h2h", "away", None)]
            po = p[("totals", "over", 2.5)]
            k = 1 + margin
            o = lambda q: round(1 / (q * k), 2)
            rows.append(",".join(map(str, [
                "X1", day.strftime("%d/%m/%Y"), prefix + h, prefix + a, hg, ag, hth, hta, ref, hs, as_, hst, ast, hf, af, hc, ac,
                hy, ay, hr, ar, o(ph), o(pd), o(pa), o(ph), o(pd), o(pa), o(po), o(1 - po), o(po), o(1 - po),
                -0.5, o(ph), o(1 - ph)])))
        day += timedelta(days=7)
    return "\n".join(rows) + "\n"
