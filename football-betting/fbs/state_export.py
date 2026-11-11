"""state.json for the iPhone app: everything the app shows, nothing secret."""
from __future__ import annotations

import json
import os

from .util import iso


def write_state(e) -> str:
    s, bank = e.store, e.bank

    def rows(sql, args=()):
        return [dict(r) for r in s.q(sql, args)]

    bets = rows("SELECT * FROM bets ORDER BY id DESC LIMIT 200")
    legs = rows("SELECT * FROM legs ORDER BY id")
    for b in bets:
        b["legs"] = [l for l in legs if l["bet_id"] == b["id"]]
        b["verified"] = all(l.get("source") not in ("manual",) for l in b["legs"]) and b.get("settle_source") != "manual"
    opps = rows("SELECT * FROM opportunities ORDER BY created_at DESC LIMIT 60")
    for o in opps:
        o["legs"] = json.loads(o["legs"])
        o["quality"] = json.loads(o["quality"] or "{}")
    shadow = rows("SELECT * FROM shadow_entries ORDER BY id DESC LIMIT 40")
    for x in shadow:
        x["legs"] = json.loads(x["legs"])
    settled = [b for b in bets if b["status"] in ("WON", "LOST", "VOID")]
    staked = sum(b["stake_cents"] for b in settled)
    returned = sum(b["return_cents"] or 0 for b in settled)
    gate = s.get("gate") or {}
    last_run = s.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    state = {
        "generated_at": iso(e.now),
        "mode": e.cfg.execution_mode,
        "ai_enabled": e.ai_enabled,
        "status": bank.status,
        "bankroll_cents": bank.equity(),
        "cash_cents": bank.balance,
        "initial_cents": bank.initial,
        "target_cents": e.cfg.target_stop_cents,
        "max_per_day": e.cfg.max_bets_per_day,
        "odds_range": [e.cfg.target_odds_min, e.cfg.target_odds_max],
        "stats": {"bets": len(settled), "won": sum(b["status"] == "WON" for b in settled),
                  "lost": sum(b["status"] == "LOST" for b in settled), "staked_cents": staked,
                  "returned_cents": returned,
                  "roi": (returned - staked) / staked if staked else None},
        "opportunities": opps,
        "bets": bets,
        "shadow_entries": shadow,
        "decisions": rows("SELECT ts, local_date, outcome, reason FROM decisions ORDER BY id DESC LIMIT 20"),
        "notifications": rows("SELECT ts, title, body, priority, sent FROM notifications ORDER BY id DESC LIMIT 40"),
        "ledger": rows("SELECT ts, kind, amount_cents, balance_cents, note FROM ledger ORDER BY id DESC LIMIT 100"),
        "providers": rows("SELECT * FROM provider_status ORDER BY provider"),
        "last_run": dict(last_run) if last_run else None,
        "gate": {f: v.get("status") for f, v in gate.get("markets", {}).items()},
        "gate_generated": gate.get("generated"),
        "apif_requests_today": s.get(f"apif_used:{e.now.date().isoformat()}", 0),
    }
    path = os.path.join(e.dir, "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"), default=str)
    os.replace(tmp, path)
    return path
