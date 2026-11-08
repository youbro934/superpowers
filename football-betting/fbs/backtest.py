"""Walk-forward backtest on football-data.co.uk history with closing average odds.

The model is refitted weekly using only matches played before that week, and
the live decision rules are applied to the closing 1X2 and over/under 2.5
prices. This is how profitability is measured instead of assumed.

Limits, stated plainly: only 1X2 / double chance (derived) / over-under 2.5
are testable from this data; average closing odds are not always obtainable;
no injury information is used.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

from .config import COMPETITIONS, Settings
from .decision import decide, evaluate_event
from .models import MatchRecord, fit_league, goal_market_probs
from .providers.odds_api import Event, Quote
from .settlement import bet_outcome, leg_result


def _event(r: MatchRecord, sport: str, idx: int) -> Event | None:
    o = r.odds
    if not all(k in o for k in ("H", "D", "A")):
        return None
    ko = datetime.combine(r.day, time(15, 0), tzinfo=timezone.utc)
    quotes = [Quote("h2h", "home", None, "avg", o["H"]), Quote("h2h", "draw", None, "avg", o["D"]),
              Quote("h2h", "away", None, "avg", o["A"])]
    if "O25" in o and "U25" in o:
        quotes += [Quote("totals", "over", 2.5, "avg", o["O25"]), Quote("totals", "under", 2.5, "avg", o["U25"])]
    return Event(f"bt{idx}", sport, r.home, r.away, r.home, r.away, ko, quotes, ko - timedelta(hours=2))


def run_backtest(history, cfg: Settings, today: date, test_days: int = 365, comps=None) -> dict:
    cfg = replace(cfg)
    comps = comps or COMPETITIONS
    test_start = today - timedelta(days=test_days)
    legs_by_day = defaultdict(list)
    results: dict[str, MatchRecord] = {}
    brier_model = brier_market = 0.0
    n_brier = 0
    per_league = {}
    idx = 0
    for comp in comps:
        try:
            records = history.load(comp, today, cfg.history_seasons + 1)
        except Exception as e:  # provider failure for one league should not stop the test
            per_league[comp.code] = f"error: {e}"
            continue
        records.sort(key=lambda r: r.day)
        test = [r for r in records if r.day >= test_start]
        if not test:
            per_league[comp.code] = "no data"
            continue
        n_league = 0
        week_start = test_start
        while week_start <= today:
            week_end = week_start + timedelta(days=7)
            week = [r for r in test if week_start <= r.day < week_end]
            if week:
                model = fit_league(records, week_start, cfg.time_decay_xi)
                if model:
                    for r in week:
                        ev = _event(r, comp.odds_sport, idx)
                        idx += 1
                        if ev is None:
                            continue
                        results[ev.event_id] = r
                        if r.home in model.goals.attack and r.away in model.goals.attack:
                            pm = goal_market_probs(model.score_matrix(r.home, r.away))
                            inv = [1 / r.odds[k] for k in ("H", "D", "A")]
                            mk = [x / sum(inv) for x in inv]
                            actual = [r.hg > r.ag, r.hg == r.ag, r.hg < r.ag]
                            brier_model += sum((p - a) ** 2 for p, a in zip(
                                (pm[("h2h", "home", None)], pm[("h2h", "draw", None)], pm[("h2h", "away", None)]),
                                actual))
                            brier_market += sum((p - a) ** 2 for p, a in zip(mk, actual))
                            n_brier += 1
                        legs = evaluate_event(ev, model, comp.code, cfg, ev.fetched_at)
                        legs_by_day[r.day].extend(legs)
                        n_league += len(legs)
            week_start = week_end
        per_league[comp.code] = n_league

    bets = []
    for day in sorted(legs_by_day):
        d = decide(legs_by_day[day], cfg)
        if not d.candidate:
            continue
        legs = []
        for l in d.candidate.legs:
            r = results[l.event_id]
            legs.append({"result": leg_result(l.market, l.selection, l.line, r.hg, r.ag), "odds": l.odds})
        outcome, eff = bet_outcome(legs)
        bets.append({"day": day.isoformat(), "odds": d.candidate.odds, "prob": d.candidate.prob,
                     "ev": d.candidate.ev, "legs": len(legs), "won": outcome == "WON"})

    n = len(bets)
    wins = sum(b["won"] for b in bets)
    flat_profit = sum((b["odds"] - 1) if b["won"] else -1 for b in bets)
    # compounding simulation: restart from EUR 10 after each bust or each EUR 500 target
    bank, busts, targets, best_run, run = 10.0, 0, 0, 0, 0
    for b in bets:
        if b["won"]:
            bank *= b["odds"]
            run += 1
            best_run = max(best_run, run)
            if bank >= 500:
                targets += 1
                bank, run = 10.0, 0
        else:
            busts += 1
            bank, run = 10.0, 0
    return {
        "period": f"{test_start.isoformat()}..{today.isoformat()}",
        "bets": n, "wins": wins, "hit_rate": wins / n if n else None,
        "avg_odds": sum(b["odds"] for b in bets) / n if n else None,
        "avg_claimed_ev": sum(b["ev"] for b in bets) / n if n else None,
        "flat_stake_roi": flat_profit / n if n else None,
        "compound_sim": {"busts": busts, "targets_500": targets, "longest_win_streak": best_run,
                         "final_bank_of_current_run": round(bank, 2)},
        "brier_1x2": {"model": brier_model / n_brier if n_brier else None,
                      "market": brier_market / n_brier if n_brier else None, "matches": n_brier},
        "legs_per_league": per_league,
        "bets_detail": bets[-200:],
    }


def summary_text(r: dict) -> str:
    if not r["bets"]:
        return f"Backtest {r['period']}: 0 qualifying bets. Brier model/market: {r['brier_1x2']}"
    b = r["brier_1x2"]
    verdict = "PROFITABLE" if r["flat_stake_roi"] > 0 else "NOT profitable"
    return (f"Backtest {r['period']}: {r['bets']} bets, hit rate {r['hit_rate']:.1%}, avg odds {r['avg_odds']:.2f}, "
            f"flat-stake ROI {r['flat_stake_roi']:+.1%} ({verdict}). "
            f"All-in sim from EUR 10: {r['compound_sim']['busts']} busts, "
            f"{r['compound_sim']['targets_500']} reached EUR 500, longest streak "
            f"{r['compound_sim']['longest_win_streak']}. "
            f"1X2 Brier model {b['model']:.4f} vs market {b['market']:.4f} over {b['matches']} matches.")
