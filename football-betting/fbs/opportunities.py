"""Opportunity cards for manual betting (iPhone flow).

The engine never places real bets. A qualifying entry becomes an OPEN
opportunity card, pushed to the phone. The owner places the bet personally at
the bookmaker and confirms it in the app; only then is it recorded as a bet,
with the stake and odds the bookmaker actually accepted.

Lifecycle: OPEN -> CONFIRMED (owner placed it) | EXPIRED (first kick-off
minus 10 min, or never confirmed) | CHANGED (current odds no longer qualify)
| REJECTED (owner declined). Only one opportunity or open bet at a time;
at most two opportunities per day.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from .util import eur, iso, parse_iso

BET365_URL = "https://www.bet365.com/"

FAMILY_LABEL = {
    "result": "Match result", "double_chance": "Double chance", "btts": "Both teams to score",
    "goals_total": "Total goals", "team_goals": "Team goals", "ht_goals_total": "1st-half goals",
    "ht_team_goals": "1st-half team goals", "corners_total": "Total corners", "team_corners": "Team corners",
    "fouls_total": "Total fouls", "team_fouls": "Team fouls", "yellows_total": "Total yellow cards",
    "team_yellows": "Team yellow cards", "cards_total": "Total cards", "team_cards": "Team cards",
    "red_card": "Red card shown", "h2h": "Match result", "totals": "Total goals",
}


def selection_label(leg: dict) -> str:
    home, away = leg["home"].title(), leg["away"].title()
    fam, sel, line = leg["market"], leg["selection"], leg["line"]
    lt = "" if line is None else f" {line:g}"
    if fam in ("result", "h2h"):
        return {"home": f"{home} to win", "away": f"{away} to win", "draw": "Draw"}.get(sel, sel)
    if fam == "double_chance":
        return {"1X": f"{home} or draw (1X)", "X2": f"{away} or draw (X2)", "12": f"{home} or {away} (12)",
                "home_draw": f"{home} or draw (1X)", "draw_away": f"{away} or draw (X2)",
                "home_away": f"{home} or {away} (12)"}.get(sel, sel)
    if fam == "btts":
        return f"Both teams to score: {sel.title()}"
    if fam == "red_card":
        return f"Red card shown: {sel.title()}"
    if sel.startswith(("home_", "away_")):
        side, ou = sel.split("_")
        team = home if side == "home" else away
        return f"{team} {FAMILY_LABEL.get(fam, fam).lower().replace('team ', '')}: {ou.title()}{lt}"
    return f"{FAMILY_LABEL.get(fam, fam)}: {sel.title()}{lt}"


def explain(leg: dict, params: dict | None) -> str:
    """One line built only from model numbers (never invented)."""
    if not params:
        return f"model {leg['p_model']:.0%} vs market {leg['p_market']:.0%}" if leg.get("p_market") else \
            f"model probability {leg['p_model']:.0%}"
    glm = params.get("glm", {})
    fam = leg["market"]
    target = {"goals_total": "goals", "team_goals": "goals", "ht_goals_total": "ht_goals", "ht_team_goals": "ht_goals",
              "corners_total": "corners", "team_corners": "corners", "fouls_total": "fouls", "team_fouls": "fouls",
              "yellows_total": "yellows", "team_yellows": "yellows", "cards_total": "yellows",
              "team_cards": "yellows"}.get(fam)
    parts = []
    if target and target in glm:
        h, a = glm[target][0], glm[target][1]
        if fam in ("cards_total", "team_cards") and "reds" in glm:
            h += glm["reds"][0]
            a += glm["reds"][1]
        name = {"ht_goals": "1st-half goals", "yellows": "cards"}.get(target, target)
        parts.append(f"expected {name}: {leg['home'].title()} {h:.1f}, {leg['away'].title()} {a:.1f} "
                     f"(total {h + a:.1f})")
    elif params.get("dc"):
        lam, mu = params["dc"][0], params["dc"][1]
        parts.append(f"expected goals {leg['home'].title()} {lam:.2f} - {mu:.2f} {leg['away'].title()}")
    parts.append(f"model {leg['p_model']:.0%}" + (f" vs bookmakers {leg['p_market']:.0%}" if leg.get("p_market") else ""))
    return "; ".join(parts)


class Opportunities:
    def __init__(self, engine):
        self.e = engine
        self.s = engine.store

    # -------------------------------------------------------------- creation
    def blocking(self) -> str | None:
        """Why no new opportunity may be issued now (None = allowed)."""
        if self.s.one("SELECT 1 FROM opportunities WHERE status='OPEN' AND kind='REAL'"):
            return "an opportunity is waiting for your decision"
        n = self.s.one("SELECT COUNT(*) FROM opportunities WHERE local_date=? AND kind='REAL'", (self.e.today,))[0]
        if n >= self.e.cfg.max_bets_per_day:
            return f"{n} opportunities already issued today (max {self.e.cfg.max_bets_per_day})"
        return None

    def create(self, cand) -> str | None:
        legs = []
        params_by_fixture = {}
        for l in cand.legs:
            d = {k: getattr(l, k) for k in ("event_id", "competition", "home", "away", "kickoff", "market",
                                            "selection", "line", "odds", "bookmaker", "p_model", "p_market",
                                            "p_used", "ev", "status", "priced_at")}
            if str(l.event_id).startswith("apif:"):
                r = self.s.one("SELECT params FROM predictions WHERE fixture_id=?", (int(l.event_id.split(":")[1]),))
                params_by_fixture[l.event_id] = json.loads(r[0]) if r else None
            d["label"] = selection_label(d)
            d["why"] = explain(d, params_by_fixture.get(l.event_id))
            legs.append(d)
        sig = hashlib.sha1(json.dumps(sorted((l["event_id"], l["market"], l["selection"], l["line"]) for l in legs))
                           .encode()).hexdigest()[:12]
        if self.s.one("SELECT 1 FROM opportunities WHERE signature=? AND local_date=?", (sig, self.e.today)):
            return None     # the same entry is never issued twice
        n = self.s.one("SELECT COUNT(*) FROM opportunities WHERE local_date=?", (self.e.today,))[0] + 1
        oid = f"OPP-{self.e.today.replace('-', '')}-{n}"
        first_ko = min(parse_iso(l["kickoff"]) for l in legs)
        expires = first_ko - timedelta(minutes=10)
        stake = self.e.bank.balance                  # 100% of the bankroll (all-in rule)
        verified = min(l["priced_at"] for l in legs if l.get("priced_at")) if any(l.get("priced_at") for l in legs) \
            else iso(self.e.now)
        quality = {
            "gate": sorted({l["status"] for l in legs}),
            "history_matches": {l["event_id"]: (params_by_fixture.get(l["event_id"]) or {}).get("history")
                                for l in legs},
            "odds_age_minutes": int((self.e.now - parse_iso(verified)).total_seconds() // 60),
        }
        self.s.x("INSERT INTO opportunities(id,created_at,local_date,expires_at,status,kind,odds,prob,ev,stake_cents,"
                 "legs,quality,verified_at,explanation,signature,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, iso(self.e.now), self.e.today, iso(expires), "OPEN", "REAL", cand.odds, cand.prob, cand.ev,
                  stake, json.dumps(legs), json.dumps(quality), verified,
                  " | ".join(l["why"] for l in legs), sig, iso(self.e.now)))
        self.notify_new(oid)
        return oid

    def card_text(self, o) -> str:
        legs = json.loads(o["legs"])
        lines = []
        for l in legs:
            ko = parse_iso(l["kickoff"]).astimezone(self.e.tz).strftime("%a %H:%M")
            lines.append(f"• {l['home'].title()} v {l['away'].title()} ({ko})\n  {l['label']} @ {l['odds']:.2f}")
        stake = o["stake_cents"]
        exp = parse_iso(o["expires_at"]).astimezone(self.e.tz).strftime("%H:%M")
        ver = parse_iso(o["verified_at"]).astimezone(self.e.tz).strftime("%H:%M")
        return ("\n".join(lines) +
                f"\n\nCombined odds {o['odds']:.2f} · {len(legs)} selection(s)\n"
                f"Stake {eur(stake)} → potential return {eur(int(stake * o['odds']))}\n"
                f"Probability {o['prob']:.0%} · edge {o['ev']:+.1%}\n"
                f"Odds verified {ver} · expires {exp}\n"
                "Place it yourself at Bet365, then tap 'I placed this bet' in the app.")

    def notify_new(self, oid: str):
        o = self.s.one("SELECT * FROM opportunities WHERE id=?", (oid,))
        self.e.notifier.queue(f"NEW OPPORTUNITY {oid} @ {o['odds']:.2f}", self.card_text(o), priority="high",
                              tags="soccer,moneybag", dedupe_key=f"opp:new:{oid}",
                              click=self.e.app_link(oid),
                              actions=[{"action": "view", "label": "Open Bet365", "url": BET365_URL},
                                       {"action": "view", "label": "Open card", "url": self.e.app_link(oid)}])

    # ------------------------------------------------------------ revalidate
    def revalidate(self):
        """Expire stale cards and re-check every OPEN card against the current odds."""
        from .live.odds import prices
        cfg = self.e.cfg
        for o in self.s.q("SELECT * FROM opportunities WHERE status='OPEN'"):
            if self.e.now >= parse_iso(o["expires_at"]):
                self._close(o, "EXPIRED", "not confirmed before the first kick-off")
                self.e.notifier.queue(f"Opportunity {o['id']} expired", "It was not confirmed before kick-off. "
                                      "No bet was recorded.", tags="hourglass", dedupe_key=f"opp:exp:{o['id']}")
                continue
            legs = json.loads(o["legs"])
            col = self.e._collector() if self.e.apif.enabled else None
            changed, missing = [], []
            for l in legs:
                if not str(l["event_id"]).startswith("apif:") or col is None:
                    continue
                snap = col.current(int(l["event_id"].split(":")[1]))
                if snap is None or snap.get("fetched_at", "") <= o["verified_at"]:
                    continue          # no newer prices yet
                quotes, _ = prices(snap, cfg.allowed_bookmakers)
                q = quotes.get((l["market"], l["selection"], l["line"]))
                if q is None:
                    missing.append(l["label"])
                    continue
                if abs(q.best - l["odds"]) >= 0.01:
                    changed.append((l["label"], l["odds"], q.best))
                    l["odds"], l["bookmaker"], l["priced_at"] = q.best, q.bookmaker, q.fetched_at
                    l["ev"] = round(l["p_used"] * q.best - 1, 4)
            if missing:
                self._close(o, "CHANGED", "selection no longer offered: " + ", ".join(missing))
                self.e.notifier.queue(f"Opportunity {o['id']} withdrawn", "No longer offered: " + ", ".join(missing)
                                      + ". Do not place it.", priority="high", tags="no_entry",
                                      dedupe_key=f"opp:chg:{o['id']}")
                continue
            if not changed:
                continue
            odds = 1.0
            for l in legs:
                odds *= l["odds"]
            ev = o["prob"] * odds - 1
            newest = max(l.get("priced_at") or o["verified_at"] for l in legs)
            txt = "\n".join(f"• {lab}: {a:.2f} → {b:.2f}" for lab, a, b in changed)
            if not (cfg.target_odds_min <= odds <= cfg.target_odds_max) or ev < cfg.min_bet_edge or \
                    any(l["ev"] < cfg.min_leg_edge for l in legs):
                self._close(o, "CHANGED", f"odds moved: combined {odds:.2f}, EV {ev:+.1%}")
                self.e.notifier.queue(f"Opportunity {o['id']} withdrawn (odds changed)",
                                      f"{txt}\nCombined {odds:.2f}, EV {ev:+.1%}: no longer qualifies. Do not place it.",
                                      priority="high", tags="no_entry", dedupe_key=f"opp:chg:{o['id']}")
                continue
            self.s.x("UPDATE opportunities SET legs=?, odds=?, ev=?, verified_at=?, updated_at=? WHERE id=?",
                     (json.dumps(legs), round(odds, 4), round(ev, 4), newest, iso(self.e.now), o["id"]))
            self.e.notifier.queue(f"Odds changed {o['id']}: now {odds:.2f}", f"{txt}\nStill qualifies "
                                  f"(EV {ev:+.1%}). Re-check the price at Bet365.", tags="chart_with_upwards_trend",
                                  dedupe_key=f"opp:odds:{o['id']}:{newest}", click=self.e.app_link(o["id"]))

    def _close(self, o, status: str, note: str):
        self.s.x("UPDATE opportunities SET status=?, note=?, updated_at=? WHERE id=?",
                 (status, note, iso(self.e.now), o["id"]))

    # ------------------------------------------------------------ owner input
    def confirm(self, payload: dict) -> tuple[bool, str]:
        """The owner placed the bet. payload: id, stake (EUR), odds (accepted), bookmaker, reference, placed_at."""
        o = self.s.one("SELECT * FROM opportunities WHERE id=?", (payload.get("id"),))
        if o is None:
            return False, "unknown opportunity"
        if o["status"] == "CONFIRMED":
            return False, f"already confirmed as bet {o['bet_id']} (duplicate ignored)"
        if o["status"] not in ("OPEN", "EXPIRED", "CHANGED"):
            return False, f"opportunity is {o['status']}"
        try:
            stake = int(round(float(payload["stake"]) * 100))
            odds = float(payload["odds"])
        except (KeyError, TypeError, ValueError):
            return False, "stake and accepted odds are required"
        if stake <= 0 or odds <= 1.0:
            return False, "stake must be > 0 and odds > 1"
        bal = self.e.bank.balance
        if stake > bal:
            return False, f"stake {eur(stake)} exceeds the bankroll {eur(bal)} (no deposits allowed)"
        ok, why = self.e.bank.can_bet(self.e.today)
        if not ok:
            return False, why
        legs = json.loads(o["legs"])
        n = self.s.one("SELECT COUNT(*) FROM bets WHERE local_date=? AND status!='FAILED'", (self.e.today,))[0] + 1
        bet_id = self.e.bank.reserve_amount(f"{o['id']}", self.e.today, n, "manual", odds, o["prob"],
                                            o["prob"] * odds - 1, stake)
        self.s.x("UPDATE bets SET status='OPEN', external_ref=?, opportunity_id=?, bookmaker=?, reference=?, "
                 "placed_at=?, suggested_stake_cents=? WHERE id=?",
                 (payload.get("reference") or "", o["id"], payload.get("bookmaker") or "Bet365",
                  payload.get("reference") or "", payload.get("placed_at") or iso(self.e.now), o["stake_cents"],
                  bet_id))
        for l in legs:
            self.s.x("INSERT INTO legs(bet_id,event_id,sport_key,competition,home,away,kickoff,market,selection,line,"
                     "odds,bookmaker,p_model,p_market,p_used,ev) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (bet_id, l["event_id"], "", l["competition"], l["home"], l["away"], l["kickoff"], l["market"],
                      l["selection"], l["line"], l["odds"], payload.get("bookmaker") or "Bet365", l["p_model"],
                      l["p_market"], l["p_used"], l["ev"]))
        self.s.x("UPDATE opportunities SET status='CONFIRMED', bet_id=?, updated_at=? WHERE id=?",
                 (bet_id, iso(self.e.now), o["id"]))
        warn = "" if stake == o["stake_cents"] else \
            f" (suggested {eur(o['stake_cents'])}; you staked {eur(stake)})"
        self.e.notifier.queue(f"Bet recorded: {eur(stake)} @ {odds:.2f}",
                              f"{o['id']} confirmed by you{warn}. Potential return {eur(int(stake * odds))}. "
                              "Results are tracked automatically.", tags="memo", dedupe_key=f"opp:conf:{o['id']}")
        return True, f"bet {bet_id} recorded"

    def reject(self, payload: dict) -> tuple[bool, str]:
        o = self.s.one("SELECT * FROM opportunities WHERE id=?", (payload.get("id"),))
        if o is None or o["status"] != "OPEN":
            return False, "no open opportunity with that id"
        self._close(o, "REJECTED", "declined by owner")
        return True, "declined"
