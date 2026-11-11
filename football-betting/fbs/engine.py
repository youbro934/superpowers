"""One autonomous cycle: recover -> settle -> scan -> decide -> execute -> report.

Runs hourly from GitHub Actions. Every step is isolated: a failing provider is
recorded and reported, and the rest of the cycle continues.
"""
from __future__ import annotations

import json
import logging
import os
import traceback
from dataclasses import asdict
from datetime import date, datetime, timedelta

from . import dashboard
from .bankroll import ACTIVE, BUSTED, STOPPED_TARGET, Bankroll
from .config import COMPETITION_BY_CODE, COMPETITION_BY_SPORT, COMPETITIONS, Competition, Settings
from .db import Store
from .decision import Leg, decide, evaluate_event
from .execution import ExecutionError, build_executor
from .models import LeagueModel, fit_league
from .notify import Notifier
from .providers.history import HistoryProvider
from .providers.odds_api import Event, OddsAPI, Quote
from .providers.results import APIFootball, FootballDataOrg, find_result
from .settlement import bet_outcome, leg_result
from .teams import canonical, is_favorite, match_team
from .util import ProviderError, eur, get_tz, http_request, iso, parse_iso, utcnow

log = logging.getLogger("fbs")

RISK_LINE = ("All-in compounding: one losing bet loses the entire bankroll. "
             "Past results do not guarantee future results.")


def _event_to_json(e: Event) -> dict:
    d = asdict(e)
    d["kickoff"] = iso(e.kickoff)
    d["fetched_at"] = iso(e.fetched_at) if e.fetched_at else None
    for q in d["quotes"]:
        q["last_update"] = iso(q["last_update"]) if q["last_update"] else None
    return d


def _event_from_json(d: dict) -> Event:
    quotes = [Quote(q["market"], q["selection"], q["line"], q["bookmaker"], q["price"],
                    parse_iso(q["last_update"]) if q["last_update"] else None) for q in d["quotes"]]
    return Event(d["event_id"], d["sport_key"], d["home"], d["away"], d["home_raw"], d["away_raw"],
                 parse_iso(d["kickoff"]), quotes, parse_iso(d["fetched_at"]) if d.get("fetched_at") else None)


class Engine:
    def __init__(self, cfg: Settings, data_dir: str, http=http_request, now: datetime | None = None,
                 history: HistoryProvider | None = None):
        self.cfg = cfg
        self.dir = data_dir
        self.http = http
        self._now = now
        self.store = Store(data_dir)
        self.bank = Bankroll(self.store, cfg)
        self.notifier = Notifier(self.store, cfg, http)
        self.odds = OddsAPI(cfg.odds_api_key, http)
        self.fdorg = FootballDataOrg(cfg.football_data_org_key, http)
        self.apif = APIFootball(cfg.api_football_key, http)
        self.cache_dir = os.path.join(data_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.history = history or HistoryProvider(os.path.join(self.cache_dir, "history"), http)
        self.tz = get_tz(cfg.timezone)
        self.log: list[str] = []
        self._results_cache: dict = {}

    # ------------------------------------------------------------------ utils
    @property
    def now(self) -> datetime:
        return self._now or utcnow()

    @property
    def local_now(self) -> datetime:
        return self.now.astimezone(self.tz)

    @property
    def today(self) -> str:
        return self.local_now.date().isoformat()

    def note(self, msg: str):
        log.info(msg)
        self.log.append(msg)

    def provider_error(self, provider: str, err: Exception):
        n = self.store.provider_failed(provider, str(err))
        self.note(f"provider error {provider}: {err}")
        if n >= 2:
            self.notifier.queue(f"Data failure: {provider}",
                                f"{provider} failed {n} times in a row.\nLast error: {str(err)[:300]}",
                                priority="high", tags="warning", dedupe_key=f"provider:{provider}",
                                dedupe_hours=self.cfg.alert_dedupe_hours)

    # ------------------------------------------------------------------ cycle
    def run(self) -> dict:
        run_id = self.store.x("INSERT INTO runs(started_at,status) VALUES(?,?)", (iso(self.now), "running"))
        self.store.commit()
        status = "ok"
        try:
            self._startup()
            for step in (self.recover, self.settle_open, self.settle_shadow, self.scan_and_bet,
                         self.daily_notices, self.reliability_checks):
                try:
                    step()
                except Exception as e:   # keep going; report the failure
                    status = "degraded"
                    self.note(f"step {step.__name__} failed: {e}")
                    log.error(traceback.format_exc())
                    self.notifier.queue("System error", f"{step.__name__}: {str(e)[:400]}", priority="high",
                                        tags="rotating_light", dedupe_key=f"step:{step.__name__}",
                                        dedupe_hours=self.cfg.alert_dedupe_hours)
                self.store.commit()
        finally:
            self.store.x("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                         (iso(utcnow()), status, "\n".join(self.log)[-4000:], run_id))
            self.store.prune()
            self.store.commit()
            try:
                self.notifier.flush()
            except Exception as e:
                log.error("notification flush failed: %s", e)
            self.store.commit()
            self.heartbeat(status)
            dashboard.write(self)
        return {"status": status, "log": self.log, "bankroll": self.bank.balance,
                "state": self.bank.status}

    def _startup(self):
        if self.bank.init():
            mode = "PAPER TRADING (no real money)" if self.cfg.execution_mode == "paper" else self.cfg.execution_mode
            self.notifier.queue("Betting system started",
                                f"Mode: {mode}\nBankroll: {eur(self.bank.balance)}\n"
                                f"Stop at {eur(self.cfg.target_stop_cents)}.\n{RISK_LINE}",
                                tags="soccer", dedupe_key="startup")
        missing = [k for k, v in (("ODDS_API_KEY", self.cfg.odds_api_key),
                                  ("FOOTBALL_DATA_ORG_KEY", self.cfg.football_data_org_key)) if not v]
        if missing:
            self.note(f"missing secrets: {', '.join(missing)}")
            self.notifier.queue("Setup incomplete", f"Missing secrets: {', '.join(missing)}. "
                                "Scanning/settlement are limited until they are added.",
                                priority="high", tags="warning", dedupe_key="config:missing", dedupe_hours=24)

    # --------------------------------------------------------------- execution
    def executor(self):
        return build_executor(self.cfg, self.store)

    def recover(self):
        """Resolve bets left in PLACING by a crashed run, without placing twice."""
        stuck = self.store.q("SELECT * FROM bets WHERE status='PLACING'")
        if not stuck:
            return
        ex = self.executor()
        for b in stuck:
            ref = ex.find(b["client_ref"])
            if ref:
                self.bank.confirm(b["id"], ref)
                self.note(f"recovered bet {b['id']} as placed ({ref})")
            else:
                self.bank.fail(b["id"], "not found after interrupted run; stake returned")
                self.note(f"recovered bet {b['id']} as not placed")

    # -------------------------------------------------------------- settlement
    def _fdorg_results(self, comp: Competition, day: date):
        key = ("fd", comp.code, day)
        if key not in self._results_cache:
            self._results_cache[key] = self.fdorg.finished([comp.fdorg], day - timedelta(days=1),
                                                           day + timedelta(days=1))
            self.store.provider_ok(self.fdorg.name)
        return self._results_cache[key]

    def _odds_scores(self, sport: str):
        key = ("oa", sport)
        if key not in self._results_cache:
            self._results_cache[key] = self.odds.scores(sport, 3)
            self.store.provider_ok(f"{self.odds.name}:scores")
        return self._results_cache[key]

    def _final_score(self, leg) -> tuple[int, int, str] | None:
        comp = COMPETITION_BY_CODE.get(leg["competition"])
        kick = parse_iso(leg["kickoff"])
        if comp and comp.fdorg and self.fdorg.enabled:
            try:
                r = find_result(self._fdorg_results(comp, kick.date()), leg["home"], leg["away"], kick.date())
                if r:
                    return r.hg, r.ag, self.fdorg.name
            except ProviderError as e:
                self.provider_error(self.fdorg.name, e)
        if self.odds.enabled and leg["sport_key"]:
            try:
                s = self._odds_scores(leg["sport_key"]).get(leg["event_id"])
                if s:
                    return s[0], s[1], self.odds.name
            except ProviderError as e:
                self.provider_error(f"{self.odds.name}:scores", e)
        return None

    def _stat_total(self, leg) -> int | None:
        kick = parse_iso(leg["kickoff"])
        want_corners = leg["market"] == "corners_totals"
        if self.apif.enabled:
            try:
                fid = self.apif.fixture_id(leg["home"], leg["away"], kick.date())
                if fid:
                    corners, cards = self.apif.statistics(fid)
                    self.store.provider_ok(self.apif.name)
                    val = corners if want_corners else cards
                    if val is not None:
                        return val
            except ProviderError as e:
                self.provider_error(self.apif.name, e)
        comp = COMPETITION_BY_CODE.get(leg["competition"])
        if comp and comp.has_corners:
            try:
                for r in self.history.load(comp, kick.date(), 1):
                    if r.home == leg["home"] and r.away == leg["away"] and abs((r.day - kick.date()).days) <= 1:
                        if want_corners and r.hc is not None:
                            return r.hc + r.ac
                        if not want_corners and r.hcards is not None:
                            return r.hcards + r.acards
            except ProviderError as e:
                self.provider_error("football-data.co.uk", e)
        return None

    def settle_open(self):
        for bet in self.store.q("SELECT * FROM bets WHERE status='OPEN'"):
            legs = self.store.q("SELECT * FROM legs WHERE bet_id=?", (bet["id"],))
            for leg in legs:
                if leg["result"] != "pending":
                    continue
                kick = parse_iso(leg["kickoff"])
                if self.now < kick + timedelta(hours=self.cfg.settlement_wait_hours):
                    continue
                score = self._final_score(leg)
                result = None
                stat = None
                if score:
                    hg, ag, src = score
                    if leg["market"] in ("corners_totals", "cards_totals"):
                        stat = self._stat_total(leg)
                    result = leg_result(leg["market"], leg["selection"], leg["line"], hg, ag, stat)
                if result:
                    self.store.x("UPDATE legs SET result=?, home_goals=?, away_goals=?, stat_total=?, "
                                 "settled_at=?, source=? WHERE id=?",
                                 (result, hg, ag, stat, iso(self.now), src, leg["id"]))
                    self.note(f"leg {leg['id']} {leg['home']} v {leg['away']} {hg}-{ag}: {result}")
                elif self.now > kick + timedelta(hours=self.cfg.settlement_alert_hours):
                    self.notifier.queue(
                        "Settlement needs attention",
                        f"No result yet for {leg['home']} v {leg['away']} ({leg['kickoff'][:16]}). "
                        f"If postponed/abandoned, void it with: settle {leg['id']} void",
                        priority="high", tags="hourglass", dedupe_key=f"unsettled:{leg['id']}", dedupe_hours=24)
            self.store.commit()
            legs = [dict(l) for l in self.store.q("SELECT * FROM legs WHERE bet_id=?", (bet["id"],))]
            outcome, eff_odds = bet_outcome(legs)
            if outcome:
                self._finish_bet(bet, outcome, eff_odds, legs)

    def _finish_bet(self, bet, outcome: str, eff_odds: float, legs: list[dict]):
        before = bet["stake_cents"]
        ret = self.bank.settle(bet["id"], outcome, eff_odds)
        self.store.commit()
        bal = self.bank.balance
        profit = bal - self.bank.initial
        lines = "\n".join(f"- {l['home']} v {l['away']} {l['home_goals']}-{l['away_goals']}: "
                          f"{l['market']} {l['selection']} {l['result']}" for l in legs)
        mode = "PAPER " if bet["mode"] == "paper" else ""
        if outcome == "WON":
            title = f"{mode}WIN +{eur(ret - before)} | bankroll {eur(bal)}"
            tags = "white_check_mark"
        elif outcome == "VOID":
            title = f"{mode}VOID, stake returned | bankroll {eur(bal)}"
            tags = "leftwards_arrow_with_hook"
        else:
            title = f"{mode}LOSS -{eur(before)} | bankroll {eur(bal)}"
            tags = "x"
        self.notifier.queue(title, f"{lines}\n\nBankroll: {eur(bal)} (net {eur(profit)})", tags=tags,
                            priority="high")
        if self.bank.status == STOPPED_TARGET:
            self.notifier.queue(f"{eur(self.cfg.target_stop_cents)} TARGET REACHED: {eur(bal)}",
                                f"Betting is BLOCKED.\nBankroll: {eur(bal)}\nNet profit: {eur(profit)}\n"
                                "All records preserved. Reply with the command CONTINUE "
                                "(Actions > Football Betting System > Run workflow) to resume.",
                                priority="urgent", tags="trophy,rotating_light", dedupe_key=f"target:{bet['id']}")
        if self.bank.status == BUSTED:
            self.notifier.queue("Bankroll lost", f"The all-in bet lost. Bankroll: {eur(bal)}. "
                                "No deposits are allowed, so betting has stopped permanently. "
                                "Records preserved on the dashboard.", priority="urgent", tags="skull",
                                dedupe_key="busted")

    # ------------------------------------------------------------- scanning
    def _ratings(self, comp: Competition) -> LeagueModel | None:
        r = self.store.one("SELECT * FROM ratings WHERE competition=?", (comp.code,))
        if r and self.now - parse_iso(r["fitted_at"]) < timedelta(hours=self.cfg.ratings_max_age_hours):
            return LeagueModel.from_dict(json.loads(r["model"]))
        try:
            records = self.history.load(comp, self.now.date(), self.cfg.history_seasons)
            self.store.provider_ok("football-data.co.uk")
        except ProviderError as e:
            self.provider_error("football-data.co.uk", e)
            return LeagueModel.from_dict(json.loads(r["model"])) if r else None
        model = fit_league(records, self.now.date(), self.cfg.time_decay_xi)
        if model is None:
            self.note(f"{comp.code}: not enough history to fit a model ({len(records)} matches)")
            return None
        self.store.x("INSERT INTO ratings(competition,fitted_at,model) VALUES(?,?,?) ON CONFLICT(competition) "
                     "DO UPDATE SET fitted_at=excluded.fitted_at, model=excluded.model",
                     (comp.code, iso(self.now), json.dumps(model.to_dict())))
        self.note(f"{comp.code}: model fitted on {model.n_matches} matches")
        return model

    def _odds_cache_path(self, sport: str) -> str:
        return os.path.join(self.cache_dir, f"odds_{sport}.json")

    def _load_cached_odds(self, sport: str) -> list[Event]:
        p = self._odds_cache_path(sport)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [_event_from_json(d) for d in json.load(f)]

    def _save_cached_odds(self, sport: str, events: list[Event]):
        with open(self._odds_cache_path(sport), "w", encoding="utf-8") as f:
            json.dump([_event_to_json(e) for e in events], f)

    def _credit_budget(self) -> int:
        """Odds API credits this run may spend, spreading the month's quota evenly."""
        remaining = self.odds.remaining if self.odds.remaining is not None else self.store.get("odds_remaining")
        if remaining is None:
            return 10
        today = self.now.date()
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        days_left = max(1, (nxt - today).days)
        used_key = f"odds_used:{today.isoformat()}"
        used_today = self.store.get(used_key, 0)
        allowance = max(0, remaining - self.cfg.odds_credit_reserve) / days_left
        return max(0, int(allowance - used_today)) if remaining > self.cfg.odds_credit_reserve else 0

    def _spend(self, before: int):
        spent = self.odds.used_this_run - before
        if spent:
            k = f"odds_used:{self.now.date().isoformat()}"
            self.store.set(k, self.store.get(k, 0) + spent)
        if self.odds.remaining is not None:
            self.store.set("odds_remaining", self.odds.remaining)

    def scan_and_bet(self):
        ok, why = self.bank.can_bet(self.today)
        if not ok:
            self.note(f"not scanning: {why}")
            return
        if not self.odds.enabled:
            self.note("not scanning: ODDS_API_KEY missing")
            return
        start = self.now + timedelta(minutes=self.cfg.min_minutes_to_kickoff)
        window_end = self.now + timedelta(hours=self.cfg.decision_window_hours)
        # 1) free discovery
        upcoming: dict[str, list[Event]] = {}
        for comp in COMPETITIONS:
            try:
                evs = self.odds.events(comp.odds_sport, start, self.now + timedelta(hours=self.cfg.lookahead_hours))
                self.store.provider_ok(f"{self.odds.name}:events")
            except ProviderError as e:
                if e.status in (404, 422):
                    continue   # league not offered right now
                self.provider_error(f"{self.odds.name}:events", e)
                continue
            if evs:
                upcoming[comp.odds_sport] = evs
        self._spend(0)
        self.store.set("last_discovery", {"at": iso(self.now), "events": sum(map(len, upcoming.values())),
                                          "favorites": [f"{e.home_raw} v {e.away_raw} {iso(e.kickoff)}"
                                                        for evs in upcoming.values() for e in evs
                                                        if is_favorite(e.home) or is_favorite(e.away)]})
        # 2) buy odds for leagues with matches inside the decision window; favorites first
        def priority(item):
            sport, evs = item
            soon = [e for e in evs if e.kickoff <= window_end]
            fav = any(is_favorite(e.home) or is_favorite(e.away) for e in soon)
            return (not fav, -len(soon))
        mk_cost = len(self.cfg.odds_markets.split(",")) * len(self.cfg.odds_regions.split(","))
        legs: list[Leg] = []
        rejected: dict = {}
        scanned = 0
        for sport, evs in sorted(upcoming.items(), key=priority):
            soon = [e for e in evs if e.kickoff <= window_end]
            if not soon:
                continue
            comp = COMPETITION_BY_SPORT[sport]
            model = self._ratings(comp)
            if model is None:
                rejected["no model for league"] = rejected.get("no model for league", 0) + len(soon)
                continue
            cached = self._load_cached_odds(sport)
            fresh_enough = cached and all(e.fetched_at and self.now - e.fetched_at <
                                          timedelta(minutes=self.cfg.max_odds_age_minutes - 15) for e in cached)
            if fresh_enough:
                events = cached
            else:
                if self._credit_budget() < mk_cost:
                    rejected["odds budget exhausted for today"] = rejected.get("odds budget exhausted for today", 0) + 1
                    continue
                before = self.odds.used_this_run
                try:
                    events = self.odds.odds(sport, self.cfg.odds_markets, self.cfg.odds_regions, start, window_end)
                    self.store.provider_ok(f"{self.odds.name}:odds")
                except ProviderError as e:
                    self.provider_error(f"{self.odds.name}:odds", e)
                    continue
                finally:
                    self._spend(before)
                for e in events:
                    e.fetched_at = self.now
                events = self._extra_markets(sport, events)
                self._save_cached_odds(sport, events)
            scanned += 1
            for ev in events:
                if ev.kickoff > window_end:
                    continue
                legs.extend(evaluate_event(ev, model, comp.code, self.cfg, self.now, None, rejected))
        legs = self._injury_review(legs, rejected)
        legs = self._gate(legs, rejected)
        decision = decide(legs, self.cfg, rejected)
        self.note(f"scanned {scanned} league(s), {len(legs)} value leg(s): {decision.reason}")
        detail = json.dumps({"rejected": decision.rejected, "qualifying_legs": decision.qualifying_legs,
                             "candidates": decision.candidates,
                             "top_legs": [asdict(l) for l in sorted(legs, key=lambda l: -l.ev)[:8]]})
        if decision.candidate is None:
            row = self.store.one("SELECT id FROM decisions WHERE local_date=? AND outcome='NO_BET'", (self.today,))
            if row:
                self.store.x("UPDATE decisions SET ts=?, reason=?, detail=? WHERE id=?",
                             (iso(self.now), decision.reason, detail, row["id"]))
            else:
                self.store.x("INSERT INTO decisions(ts,local_date,outcome,reason,detail) VALUES(?,?,?,?,?)",
                             (iso(self.now), self.today, "NO_BET", decision.reason, detail))
            return
        self.store.x("INSERT INTO decisions(ts,local_date,outcome,reason,detail) VALUES(?,?,?,?,?)",
                     (iso(self.now), self.today, "BET", decision.reason, detail))
        self.place(decision.candidate)

    def _gate(self, legs: list[Leg], rejected: dict) -> list[Leg]:
        """Only markets that passed out-of-sample validation may be staked; the rest are logged as shadow."""
        from .research.gate import allowed
        gate = self.store.get("gate")
        if gate is None:
            self.notifier.queue("No validated markets yet",
                                "The market research has not run yet, so no market is cleared for staking. "
                                "Run the 'research' command (it also runs weekly). Opportunities are logged as shadow.",
                                tags="hourglass", dedupe_key="gate:missing", dedupe_hours=24)
        kept = []
        for l in legs:
            if allowed(gate, l.market, l.selection, l.line):
                kept.append(l)
                continue
            rejected["market not validated (shadow-logged)"] = rejected.get("market not validated (shadow-logged)", 0) + 1
            self.store.x(
                "INSERT INTO shadow(first_seen,last_seen,local_date,event_id,sport_key,competition,home,away,kickoff,"
                "market,selection,line,first_odds,last_odds,bookmaker,p_model,p_market,p_used,ev,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id,market,selection,line) "
                "DO UPDATE SET last_seen=excluded.last_seen, last_odds=excluded.last_odds",
                (iso(self.now), iso(self.now), self.today, l.event_id, l.sport_key, l.competition, l.home, l.away,
                 l.kickoff, l.market, l.selection, l.line, l.odds, l.odds, l.bookmaker, l.p_model, l.p_market,
                 l.p_used, l.ev, "market not validated"))
        return kept

    def settle_shadow(self):
        """Settle shadow selections with free result sources so forward evidence accumulates."""
        rows = self.store.q("SELECT * FROM shadow WHERE result IS NULL AND kickoff < ? ORDER BY id LIMIT 60",
                            (iso(self.now - timedelta(hours=self.cfg.settlement_wait_hours)),))
        for r in rows:
            comp = COMPETITION_BY_CODE.get(r["competition"])
            kick = parse_iso(r["kickoff"])
            score = None
            if comp and comp.fdorg and self.fdorg.enabled:
                try:
                    res = find_result(self._fdorg_results(comp, kick.date()), r["home"], r["away"], kick.date())
                    score = (res.hg, res.ag) if res else None
                except ProviderError as e:
                    self.provider_error(self.fdorg.name, e)
            if score is None:
                if self.now - kick > timedelta(days=10):
                    self.store.x("UPDATE shadow SET result='unsettled', settled_at=? WHERE id=?",
                                 (iso(self.now), r["id"]))
                continue
            stat = None
            if r["market"] in ("corners_totals", "cards_totals"):
                stat = self._stat_total(r)
            res = leg_result(r["market"], r["selection"], r["line"], score[0], score[1], stat)
            if res:
                self.store.x("UPDATE shadow SET result=?, settled_at=? WHERE id=?", (res, iso(self.now), r["id"]))

    def _extra_markets(self, sport: str, events: list[Event]) -> list[Event]:
        """BTTS / double chance (and optional corners/cards) for favorites' matches only."""
        markets = ",".join(m for m in (self.cfg.favorite_extra_markets, self.cfg.secondary_markets) if m)
        if not markets:
            return events
        cost = len(markets.split(",")) * len(self.cfg.odds_regions.split(","))
        for ev in events:
            if not (is_favorite(ev.home) or is_favorite(ev.away)):
                continue
            if self._credit_budget() < cost:
                break
            before = self.odds.used_this_run
            try:
                extra = self.odds.event_odds(sport, ev.event_id, markets, self.cfg.odds_regions)
                ev.quotes.extend(extra.quotes)
                self.store.provider_ok(f"{self.odds.name}:odds")
            except ProviderError as e:
                if e.status not in (404, 422):
                    self.provider_error(f"{self.odds.name}:odds", e)
            finally:
                self._spend(before)
        return events

    def _injury_review(self, legs: list[Leg], rejected: dict) -> list[Leg]:
        if not legs or not self.apif.enabled:
            return legs
        out = []
        checked: dict[str, dict] = {}
        top_events = []
        for l in sorted(legs, key=lambda l: -l.ev):
            if l.event_id not in top_events:
                top_events.append(l.event_id)
        top_events = top_events[:5]
        for l in legs:
            if l.event_id not in top_events:
                out.append(l)
                continue
            if l.event_id not in checked:
                try:
                    fid = self.apif.fixture_id(l.home, l.away, parse_iso(l.kickoff).date())
                    checked[l.event_id] = self.apif.injuries(fid) if fid else {}
                    self.store.provider_ok(self.apif.name)
                except ProviderError as e:
                    self.provider_error(self.apif.name, e)
                    checked[l.event_id] = {}
            inj = checked[l.event_id]
            backed = {"home": l.home, "away": l.away}
            from .decision import backs
            heavy = [backed[s] for s in backs(l.selection, l.market) if inj.get(backed[s], 0) >= self.cfg.injury_alert_count]
            if heavy and l.ev < self.cfg.min_leg_edge + self.cfg.injury_extra_edge:
                rejected["injury list too long"] = rejected.get("injury list too long", 0) + 1
                continue
            out.append(l)
        return out

    def place(self, cand):
        slot = self.bank.bets_on(self.today) + 1
        attempt = self.store.one("SELECT COUNT(*) FROM bets WHERE local_date=? AND slot=?", (self.today, slot))[0] + 1
        client_ref = f"{self.today}-s{slot}-a{attempt}"
        try:
            ex = self.executor()
        except ExecutionError as e:
            self.notifier.queue("Execution blocked", str(e), priority="high", tags="no_entry",
                                dedupe_key="exec:blocked", dedupe_hours=24)
            self.note(f"execution blocked: {e}")
            return
        bet_id = self.bank.reserve(client_ref, self.today, slot, ex.mode, cand.odds, cand.prob, cand.ev)
        for l in cand.legs:
            self.store.x("INSERT INTO legs(bet_id,event_id,sport_key,competition,home,away,kickoff,market,selection,"
                         "line,odds,bookmaker,p_model,p_market,p_used,ev) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (bet_id, l.event_id, l.sport_key, l.competition, l.home, l.away, l.kickoff, l.market,
                          l.selection, l.line, l.odds, l.bookmaker, l.p_model, l.p_market, l.p_used, l.ev))
        self.store.commit()   # intent is durable before anything is sent to a bookmaker
        stake = self.store.one("SELECT stake_cents FROM bets WHERE id=?", (bet_id,))[0]
        try:
            ref = ex.find(client_ref) or ex.place(client_ref, stake, cand.legs, cand.odds)
        except Exception as e:
            self.bank.fail(bet_id, str(e))
            self.store.commit()
            self.notifier.queue("Bet placement failed", f"{e}\nStake returned to bankroll.", priority="high",
                                tags="warning")
            return
        self.bank.confirm(bet_id, ref)
        self.store.commit()
        label = "PAPER BET (simulated, no real money)" if ex.mode == "paper" else "BET PLACED"
        legs_txt = "\n".join(f"- {l.label()} [{l.bookmaker}] p={l.p_used:.0%}" for l in cand.legs)
        self.notifier.queue(f"{label} #{slot} today: {eur(stake)} @ {cand.odds:.2f}",
                            f"{legs_txt}\n\nWin probability {cand.prob:.1%}, EV {cand.ev:+.1%}.\n"
                            f"Potential return {eur(int(stake * cand.odds))}.\n{RISK_LINE}",
                            priority="high", tags="soccer,moneybag")
        self.note(f"placed {client_ref}: {eur(stake)} @ {cand.odds}")

    # ---------------------------------------------------------------- notices
    def daily_notices(self):
        if self.bank.status != ACTIVE or self.local_now.hour < self.cfg.no_bet_notice_hour:
            return
        if self.bank.bets_on(self.today) == 0 and not self.bank.open_bets():
            d = self.store.one("SELECT reason FROM decisions WHERE local_date=? AND outcome='NO_BET'", (self.today,))
            reason = d["reason"] if d else "no scan completed today"
            self.notifier.queue(f"NO BET today ({self.today})",
                                f"{reason}\nBankroll unchanged: {eur(self.bank.balance)}", tags="zzz",
                                priority="low", dedupe_key=f"nobet:{self.today}")

    def reliability_checks(self):
        rows = self.store.q("SELECT p_model, p_market, result FROM legs WHERE result IN ('won','lost')")
        if len(rows) < 30:
            return
        bm = sum((r["p_model"] - (r["result"] == "won")) ** 2 for r in rows) / len(rows)
        bk = sum((r["p_market"] - (r["result"] == "won")) ** 2 for r in rows) / len(rows)
        self.store.set("calibration", {"n": len(rows), "brier_model": bm, "brier_market": bk})
        if bm > bk + 0.01:
            self.notifier.queue("Model reliability warning",
                                f"On {len(rows)} settled selections the model (Brier {bm:.3f}) is worse than "
                                f"the bookmakers' implied odds (Brier {bk:.3f}). Its edge is not real.",
                                priority="high", tags="warning", dedupe_key="reliability", dedupe_hours=24 * 7)

    def heartbeat(self, status: str):
        if not self.cfg.healthcheck_url:
            return
        url = self.cfg.healthcheck_url + ("" if status == "ok" else "/fail")
        try:
            self.http(url, provider="healthcheck", retries=1)
        except ProviderError as e:
            log.warning("heartbeat failed: %s", e)

    def close(self):
        self.store.close()
