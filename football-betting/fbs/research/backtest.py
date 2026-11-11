"""Chronological, out-of-sample, per-market backtest.

* Features for a match are computed from strictly earlier matches.
* Models are refitted at each month boundary on matches before that boundary,
  then predict the following month only.
* Every selection of every market is scored (Brier, log loss, calibration)
  against the de-vigged closing odds where historical prices exist, otherwise
  against the league's historical base rate.
* Where prices exist, value bets (EV >= edge at the pre-match price) give ROI
  with a 95% interval, actual vs predicted win rate, and closing-line value.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

from ..config import COMPETITIONS
from ..models import MatchRecord, fit_league, score_matrix as dc_matrix
from .data import Match, ResearchData, coverage, load_archive, merge
from .evaluate import BetLog, ProbScore
from .features import TARGET_INPUTS, FeatureBuilder, design_row
from .glm import CountModel, convolve, count_pmf, p_over
from .markets import (CARD_LINES, CORNER_LINES, FOUL_LINES, GOAL_LINES, HT_LINES, PRICED, TEAM_CORNER_LINES,
                      TEAM_FOUL_LINES, TEAM_GOAL_LINES, TEAM_YELLOW_LINES, UNAVAILABLE, YELLOW_LINES, Pred,
                      ah_expected, ah_payout, attach_archive_prices, attach_ou25_prices, count_preds, devig,
                      result_preds)

TARGETS = tuple(TARGET_INPUTS)
MIN_HISTORY = 5
EDGE = 0.02


def _usable(sf, target: str) -> bool:
    return sf.counts.get(f"{target}_for_5", 0) >= MIN_HISTORY and \
        sf.counts.get(f"{target}_opp_against_5", 0) >= MIN_HISTORY


def _side_matrix(mu_h: float, mu_a: float, alpha: float, size: int = 11) -> np.ndarray:
    ph = count_pmf(mu_h, alpha, size - 1)
    pa = count_pmf(mu_a, alpha, size - 1)
    return np.outer(ph, pa)


def _months(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        yield cur, nxt
        cur = nxt


class Research:
    def __init__(self, data: ResearchData, today: date, seasons: int = 4, test_months: int = 24,
                 comps=None, log=print, archive_dir: str | None = None):
        self.archive_dir = archive_dir
        self.data = data
        self.today = today
        self.seasons = seasons
        self.test_months = test_months
        self.comps = comps or COMPETITIONS
        self.log = log

    def run(self) -> dict:
        matches: list[Match] = []
        cov = {}
        for comp in self.comps:
            try:
                ms = self.data.load(comp, self.today, self.seasons)
            except Exception as e:
                cov[comp.code] = {"error": str(e)[:200]}
                continue
            cov[comp.code] = coverage(ms) | {"source": ms[0].source.rsplit("/", 2)[0] if ms else None}
            matches.extend(ms)
        if self.archive_dir:
            arch = load_archive(self.archive_dir)
            cov["forward_archive"] = {"matches": len(arch), "with_odds": sum(1 for a in arch if a.extra.get("apif_odds")),
                                      "source": "api-football (collected by this system)"}
            matches = merge(matches, arch)
        matches.sort(key=lambda m: (m.day, m.league, m.home))
        self.log(f"loaded {len(matches)} matches")

        fb = FeatureBuilder()
        feats = {}
        for m in matches:
            feats[id(m)] = fb.features(m)
            fb.update(m)
        self._build_design(matches, feats)
        self.level = defaultdict(lambda: [0.0, 0, 0])

        test_start = self.today - timedelta(days=30 * self.test_months)
        scores: dict[tuple, ProbScore] = defaultdict(ProbScore)
        league_scores: dict[tuple, ProbScore] = defaultdict(ProbScore)
        bets: dict[tuple, BetLog] = defaultdict(BetLog)
        base = defaultdict(lambda: [0, 0])     # (league, key) -> wins, n  from earlier matches only
        dispersion = defaultdict(list)
        by_league_records = defaultdict(list)
        for m in matches:
            by_league_records[m.league].append(m)

        # seed base rates with pre-test history
        for m in matches:
            if m.day >= test_start:
                break
            for p in self._outcomes_only(m):
                b = base[(m.league, p.key)]
                b[0] += p.won
                b[1] += 1

        for t0, t1 in _months(test_start, self.today):
            period = [m for m in matches if t0 <= m.day < t1]
            if not period:
                continue
            models = self._fit_glms(t0)
            for tgt, cm in models.items():
                dispersion[tgt].append(cm.alpha)
            dc = {}
            for lg in {m.league for m in period}:
                recs = [MatchRecord(x.day, x.home, x.away, *x.stats["goals"]) for x in by_league_records[lg]
                        if x.day < t0 and x.day >= t0 - timedelta(days=3 * 365)]
                dc[lg] = fit_league(recs, t0)
            for m in period:
                preds = self._predict(m, feats[id(m)], models, dc.get(m.league))
                self._score(m, preds, scores, league_scores, bets, base)
            self.log(f"{t0}: {len(period)} matches")

        return self._report(cov, scores, league_scores, bets, dispersion, matches, test_start)

    # ------------------------------------------------------------------ models
    def _build_design(self, matches, feats):
        """One design matrix per target, built once; rows are (match, side)."""
        self.design = {}
        for tgt in TARGETS:
            X, y, days, off, index = [], [], [], [], {}
            for m in matches:
                for side, sf in enumerate(feats[id(m)]):
                    if not _usable(sf, tgt) or not sf.values.get(f"{tgt}_league_mean"):
                        continue
                    index[(id(m), side)] = len(X)
                    X.append(design_row(sf, tgt)[0])
                    y.append(m.stats[tgt][side] if tgt in m.stats else np.nan)
                    days.append(m.day.toordinal())
                    # league average for this side at that date: each league keeps its own level
                    off.append(math.log(max(sf.values[f"{tgt}_league_mean"], 1e-3)))
            self.design[tgt] = (np.array(X, float), np.array(y, float), np.array(days), index, np.array(off))

    def _fit_glms(self, t0: date) -> dict:
        out = {}
        lo, hi = (t0 - timedelta(days=3 * 365)).toordinal(), t0.toordinal()
        for tgt in TARGETS:
            X, y, days, _, off = self.design[tgt]
            if not len(y):
                continue
            mask = (days >= lo) & (days < hi) & ~np.isnan(y)
            if mask.sum() < 400:
                continue
            out[tgt] = CountModel(ridge=5.0 if tgt == "reds" else 2.0).fit(X[mask], y[mask], off[mask])
        return out

    def _side_mus(self, m, sfs, models, tgt):
        cm = models.get(tgt)
        if cm is None:
            return None
        X, _, _, index, off = self.design[tgt]
        try:
            ix = [index[(id(m), 0)], index[(id(m), 1)]]
        except KeyError:
            return None      # not enough history for one of the teams
        mu = cm.predict(X[ix], off[ix])
        if tgt in m.stats:      # level check: average predicted vs actual count per league
            lv = self.level[(m.league, tgt)]
            lv[0] += float(mu[0] + mu[1])
            lv[1] += sum(m.stats[tgt])
            lv[2] += 1
        return float(mu[0]), float(mu[1]), cm.alpha

    def _predict(self, m: Match, sfs, models, dcm) -> list[Pred]:
        preds: list[Pred] = []
        self._ah = None
        # goals: Dixon-Coles ratings (joint score matrix)
        if dcm and m.home in dcm.goals.attack and m.away in dcm.goals.attack \
                and dcm.goals.knows(m.home, 8) and dcm.goals.knows(m.away, 8):
            mat = np.array(dcm.score_matrix(m.home, m.away, 10))
            preds += result_preds(mat, None, m, "dc")
            tot = np.array([mat[np.add.outer(range(11), range(11)) == k].sum() for k in range(21)])
            preds += count_preds("goals_total", "team_goals", "goals", GOAL_LINES, TEAM_GOAL_LINES,
                                 mat.sum(1), mat.sum(0), m, "dc", total_pmf=tot)
            self._ah = mat
        # goals: feature GLM (form, shots on target as xG proxy, h2h, rest, venue)
        g = self._side_mus(m, sfs, models, "goals")
        if g:
            mat = _side_matrix(*g)
            preds += result_preds(mat, None, m, "glm")
            preds += count_preds("goals_total", "team_goals", "goals", GOAL_LINES, TEAM_GOAL_LINES,
                                 count_pmf(g[0], g[2]), count_pmf(g[1], g[2]), m, "glm")
        attach_ou25_prices(preds, m)
        h = self._side_mus(m, sfs, models, "ht_goals")
        if h:
            preds += count_preds("ht_goals_total", "ht_team_goals", "ht_goals", HT_LINES, (0.5,),
                                 count_pmf(h[0], h[2]), count_pmf(h[1], h[2]), m, "glm")
        for tgt, fam, tfam, lines, tlines in (
                ("corners", "corners_total", "team_corners", CORNER_LINES, TEAM_CORNER_LINES),
                ("fouls", "fouls_total", "team_fouls", FOUL_LINES, TEAM_FOUL_LINES),
                ("yellows", "yellows_total", "team_yellows", YELLOW_LINES, TEAM_YELLOW_LINES)):
            c = self._side_mus(m, sfs, models, tgt)
            if c:
                preds += count_preds(fam, tfam, tgt, lines, tlines, count_pmf(c[0], c[2]), count_pmf(c[1], c[2]),
                                     m, "glm")
        y = self._side_mus(m, sfs, models, "yellows")
        r = self._side_mus(m, sfs, models, "reds")
        if r:
            rt = convolve(count_pmf(r[0], 0.0, 6), count_pmf(r[1], 0.0, 6), 6)
            have = "reds" in m.stats
            won = int(sum(m.stats["reds"]) > 0) if have else None
            preds.append(Pred("red_card", "yes", 0.5, "glm", p_over(rt, 0.5), won))
            preds.append(Pred("red_card", "no", 0.5, "glm", 1 - p_over(rt, 0.5), None if won is None else 1 - won))
            if y:
                yt = convolve(count_pmf(y[0], y[2]), count_pmf(y[1], y[2]))
                ct = convolve(yt, np.pad(rt, (0, len(yt) - len(rt))))
                have = "reds" in m.stats and "yellows" in m.stats
                total = sum(m.stats["reds"]) + sum(m.stats["yellows"]) if have else None
                for l in CARD_LINES:
                    po = p_over(ct, l)
                    won = int(total > l) if have else None
                    preds.append(Pred("cards_total", "over", l, "glm", po, won))
                    preds.append(Pred("cards_total", "under", l, "glm", 1 - po, None if won is None else 1 - won))
        attach_archive_prices(preds, m)
        return preds

    # ----------------------------------------------------------------- scoring
    def _outcomes_only(self, m: Match) -> list[Pred]:
        """Outcomes for base rates (probability unused)."""
        out = []
        hg, ag = m.stats["goals"]
        actual = "home" if hg > ag else "away" if ag > hg else "draw"
        for s in ("home", "draw", "away"):
            out.append(Pred("result", s, None, "", 0, int(actual == s)))
        for s, cov in (("1X", ("home", "draw")), ("X2", ("draw", "away")), ("12", ("home", "away"))):
            out.append(Pred("double_chance", s, None, "", 0, int(actual in cov)))
        out.append(Pred("btts", "yes", None, "", 0, int(hg > 0 and ag > 0)))
        out.append(Pred("btts", "no", None, "", 0, int(not (hg > 0 and ag > 0))))
        for fam, tfam, stat, lines, tlines in (
                ("goals_total", "team_goals", "goals", GOAL_LINES, TEAM_GOAL_LINES),
                ("ht_goals_total", "ht_team_goals", "ht_goals", HT_LINES, (0.5,)),
                ("corners_total", "team_corners", "corners", CORNER_LINES, TEAM_CORNER_LINES),
                ("fouls_total", "team_fouls", "fouls", FOUL_LINES, TEAM_FOUL_LINES),
                ("yellows_total", "team_yellows", "yellows", YELLOW_LINES, TEAM_YELLOW_LINES)):
            if stat not in m.stats:
                continue
            h, a = m.stats[stat]
            for l in lines:
                out += [Pred(fam, "over", l, "", 0, int(h + a > l)), Pred(fam, "under", l, "", 0, int(h + a < l))]
            for side, v in (("home", h), ("away", a)):
                for l in tlines:
                    out += [Pred(tfam, f"{side}_over", l, "", 0, int(v > l)),
                            Pred(tfam, f"{side}_under", l, "", 0, int(v < l))]
        if "reds" in m.stats:
            r = int(sum(m.stats["reds"]) > 0)
            out += [Pred("red_card", "yes", 0.5, "", 0, r), Pred("red_card", "no", 0.5, "", 0, 1 - r)]
            if "yellows" in m.stats:
                t = sum(m.stats["reds"]) + sum(m.stats["yellows"])
                for l in CARD_LINES:
                    out += [Pred("cards_total", "over", l, "", 0, int(t > l)),
                            Pred("cards_total", "under", l, "", 0, int(t < l))]
        return out

    def _score(self, m: Match, preds, scores, league_scores, bets, base):
        for p in preds:
            if p.won is None:
                continue
            b = base[(m.league, p.key)]
            ref = p.close_p if p.close_p is not None else (b[0] / b[1] if b[1] >= 30 else None)
            ref_kind = "market" if p.close_p is not None else "base_rate"
            scores[(p.family, p.key, p.model, ref_kind)].add(p.p, p.won, ref)
            league_scores[(p.family, p.model, m.league)].add(p.p, p.won, None)
            if p.price:
                ev = p.p * p.price - 1
                if ev >= EDGE:
                    profit = p.price - 1 if p.won else -1.0
                    clv = p.price * p.close_p - 1 if p.close_p is not None else None
                    cprofit = (p.close_price - 1 if p.won else -1.0) if p.close_price else None
                    bets[(p.family, p.key, p.model, "all")].add(profit, p.p, bool(p.won), m.league, clv, cprofit)
                    if p.price <= 1.40 and p.p >= 0.60:
                        bets[(p.family, p.key, p.model, "entry_range")].add(profit, p.p, bool(p.won), m.league,
                                                                             clv, cprofit)
        # Asian handicap (expected-return based; quarter lines handled)
        ah = m.odds.get("ah_taken")
        if self._ah is not None and ah:
            _, (ph, pa), line = ah
            close = m.odds.get("ah_close")
            hg, ag = m.stats["goals"]
            for side, price, k in (("home", ph, 0), ("away", pa, 1)):
                ev = ah_expected(self._ah, line, side, price)
                if ev < EDGE:
                    continue
                r = ah_payout(hg - ag, line, side)
                profit = r * (price - 1) if r > 0 else r
                clv = None
                if close and close[2] == line:
                    fair = devig(close[1])[k]
                    clv = price * fair - 1
                bets[("asian_handicap", f"asian_handicap:{side}", "dc", "all")].add(
                    profit, 0.5 + ev / 2, profit > 0, m.league, clv, None)
        for p in self._outcomes_only(m):
            b = base[(m.league, p.key)]
            b[0] += p.won
            b[1] += 1

    # ------------------------------------------------------------------ report
    def _report(self, cov, scores, league_scores, bets, dispersion, matches, test_start) -> dict:
        markets = defaultdict(lambda: {"selections": {}, "bets": {}, "by_league": {}})
        for (fam, key, model, ref_kind), sc in scores.items():
            markets[fam]["selections"][f"{key}|{model}"] = sc.summary(ref_kind) | {"reference": ref_kind}
        for (fam, key, model, subset), bl in bets.items():
            markets[fam]["bets"][f"{key}|{model}|{subset}"] = bl.summary()
        for (fam, model, lg), sc in league_scores.items():
            s = sc.summary("none")
            markets[fam]["by_league"][f"{lg}|{model}"] = {k: s.get(k) for k in ("n", "brier", "ece")}
        for fam in markets:
            markets[fam]["historical_prices"] = any(k.startswith(fam) for k in PRICED) or \
                any(b.get("n") for b in markets[fam]["bets"].values())
        return {
            "test_period": f"{test_start.isoformat()}..{self.today.isoformat()}",
            "method": "monthly walk-forward refit; features from earlier matches only; "
                      "value bet = EV >= 2% at the pre-match price",
            "coverage": cov,
            "markets": dict(markets),
            "dispersion_alpha": {k: (sum(v) / len(v) if v else None) for k, v in dispersion.items()},
            "level_check": {f"{lg}|{t}": {"n": n, "predicted_mean": ps / n, "actual_mean": a / n,
                                          "bias": ps / a - 1 if a else None}
                            for (lg, t), (ps, a, n) in sorted(self.level.items()) if n},
            "correlations": correlations([m for m in matches if m.day >= test_start]),
            "unavailable": UNAVAILABLE,
        }


def correlations(matches: list[Match]) -> dict:
    """Pearson correlation of match totals; shows which same-match legs are dependent."""
    stats = ["goals", "corners", "shots", "sot", "fouls", "yellows", "reds"]
    rows = [[sum(m.stats[s]) for s in stats] for m in matches if all(s in m.stats for s in stats)]
    if len(rows) < 100:
        return {"n": len(rows)}
    a = np.array(rows, float)
    c = np.corrcoef(a.T)
    out = {"n": len(rows), "totals": {f"{stats[i]}~{stats[j]}": round(float(c[i, j]), 3)
                                      for i in range(len(stats)) for j in range(i + 1, len(stats))}}
    # home win vs over 2.5 goals, the classic same-match pair
    hw = np.array([m.stats["goals"][0] > m.stats["goals"][1] for m in matches], float)
    o25 = np.array([sum(m.stats["goals"]) > 2.5 for m in matches], float)
    out["home_win~over2.5_phi"] = round(float(np.corrcoef(hw, o25)[0, 1]), 3)
    return out
