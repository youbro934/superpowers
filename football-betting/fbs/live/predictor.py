"""Live predictor: fits the research models on all history up to today and
produces parameters for upcoming fixtures. Same features and models the
research backtest validates; nothing is filled in where history is missing."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

from ..config import COMPETITION_BY_CODE, COMPETITIONS
from ..models import MatchRecord, fit_league
from ..research.data import APIF_LEAGUES, Match, ResearchData, load_archive, merge
from ..research.features import TARGET_INPUTS, FeatureBuilder, design_row
from ..research.glm import CountModel
from ..teams import canonical, match_team

MIN_HISTORY = 5


def _usable(sf, tgt):
    return (sf.counts.get(f"{tgt}_for_5", 0) >= MIN_HISTORY and
            sf.counts.get(f"{tgt}_opp_against_5", 0) >= MIN_HISTORY and sf.values.get(f"{tgt}_league_mean"))


class LivePredictor:
    def __init__(self, data: ResearchData, archive_dir: str | None, today: date, seasons: int = 3):
        self.data = data
        self.archive_dir = archive_dir
        self.today = today
        self.seasons = seasons
        self.version = f"live-{today.isoformat()}"

    def build(self) -> "LivePredictor":
        matches: list[Match] = []
        for comp in COMPETITIONS:
            try:
                matches.extend(self.data.load(comp, self.today, self.seasons))
            except Exception:
                continue      # a missing league just has no predictions
        if self.archive_dir:
            matches = merge(matches, load_archive(self.archive_dir))
        matches = [m for m in matches if m.day < self.today]
        matches.sort(key=lambda m: (m.day, m.league, m.home))
        self.fb = FeatureBuilder()
        rows = defaultdict(lambda: ([], [], []))
        lo = self.today - timedelta(days=3 * 365)
        self.league_teams = defaultdict(set)
        by_league = defaultdict(list)
        for m in matches:
            sfs = self.fb.features(m)
            if m.day >= lo:
                for tgt in TARGET_INPUTS:
                    if tgt not in m.stats:
                        continue
                    for side, sf in enumerate(sfs):
                        if _usable(sf, tgt):
                            X, y, off = rows[tgt]
                            X.append(design_row(sf, tgt)[0])
                            y.append(m.stats[tgt][side])
                            off.append(math.log(max(sf.values[f"{tgt}_league_mean"], 1e-3)))
            self.fb.update(m)
            self.league_teams[m.league].update((m.home, m.away))
            by_league[m.league].append(m)
        self.models = {}
        for tgt, (X, y, off) in rows.items():
            if len(y) >= 400:
                self.models[tgt] = CountModel(ridge=5.0 if tgt == "reds" else 2.0).fit(
                    np.array(X, float), np.array(y, float), np.array(off))
        self.dc = {}
        for lg, ms in by_league.items():
            recs = [MatchRecord(m.day, m.home, m.away, *m.stats["goals"]) for m in ms if m.day >= lo]
            self.dc[lg] = fit_league(recs, self.today)
        self.n_matches = len(matches)
        return self

    def predict(self, fixture: dict) -> dict | None:
        """fixture: API-Football fixture dict (league id, home, away, kickoff, referee)."""
        league = APIF_LEAGUES.get(fixture["league"])
        if not league or league not in self.league_teams:
            return None    # cups / cross-league ties: team levels from different leagues are not comparable
        teams = self.league_teams[league]
        home = match_team(fixture["home"], teams)
        away = match_team(fixture["away"], teams)
        if not home or not away or home == away:
            return None
        day = datetime.fromisoformat(fixture["kickoff"].replace("Z", "+00:00")).date()
        pseudo = Match(league, day, home, away, {}, fixture.get("referee"), {}, "upcoming", "")
        sfs = self.fb.features(pseudo)
        params = {"glm": {}, "dc": None, "teams": [home, away], "league": league,
                  "history": {"home": sfs[0].counts.get("goals_for_15", 0), "away": sfs[1].counts.get("goals_for_15", 0)}}
        for tgt, cm in self.models.items():
            if not all(_usable(sf, tgt) for sf in sfs):
                continue
            X = np.array([design_row(sf, tgt)[0] for sf in sfs])
            off = np.array([math.log(max(sf.values[f"{tgt}_league_mean"], 1e-3)) for sf in sfs])
            mu = cm.predict(X, off)
            params["glm"][tgt] = [float(mu[0]), float(mu[1]), float(cm.alpha)]
        dcm = self.dc.get(league)
        if dcm and dcm.goals.knows(home, 8) and dcm.goals.knows(away, 8):
            lam, mu = dcm.goals.expected(home, away)
            params["dc"] = [lam, mu, dcm.rho]
        if not params["glm"] and not params["dc"]:
            return None
        return params
