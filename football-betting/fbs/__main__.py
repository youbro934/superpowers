"""Command line: python -m fbs [--data-dir DIR] COMMAND

  run                 one autonomous cycle (what the scheduler calls)
  status              print bankroll, state and open bets
  continue CONTINUE   resume after the EUR 500 stop (the word must be exact)
  settle LEG_ID void|won|lost   manual settlement for a postponed/unsettleable selection
  backtest [DAYS]     walk-forward backtest on historical closing odds (goal model only)
  research [MONTHS]   per-market chronological backtests + promotion gate (needs numpy)
  notify-test         send a test push notification
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Settings
from .engine import Engine
from .util import eur, iso, utcnow


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fbs")
    p.add_argument("--data-dir", default=os.environ.get("FBS_DATA_DIR", "data"))
    p.add_argument("command")
    p.add_argument("args", nargs="*")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Settings.from_env()
    eng = Engine(cfg, a.data_dir)
    try:
        return _dispatch(eng, a.command, a.args)
    finally:
        eng.close()


def _dispatch(eng: Engine, cmd: str, args: list[str]) -> int:
    bank, store = eng.bank, eng.store
    if cmd == "run":
        out = eng.run()
        print(json.dumps({k: out[k] for k in ("status", "state")} | {"bankroll": eur(out["bankroll"])}))
        print("\n".join(out["log"]))
        return 0
    if cmd == "status":
        bank.init()
        store.commit()
        print(f"state: {bank.status}  cash: {eur(bank.balance)}  at risk: {eur(bank.exposure())}  "
              f"mode: {eng.cfg.execution_mode}")
        for b in bank.open_bets():
            print(f"open bet {b['id']} {b['client_ref']}: {eur(b['stake_cents'])} @ {b['odds']:.2f}")
            for l in store.q("SELECT * FROM legs WHERE bet_id=?", (b["id"],)):
                print(f"   leg {l['id']}: {l['home']} v {l['away']} {l['market']} {l['selection']} "
                      f"{l['line'] or ''} @ {l['odds']} [{l['result']}]")
        return 0
    if cmd == "continue":
        word = args[0] if args else ""
        if bank.resume(word):
            store.commit()
            eng.notifier.queue("Betting resumed", f"CONTINUE received. Bankroll {eur(bank.balance)}.",
                               tags="arrow_forward")
            store.commit()
            eng.notifier.flush()
            store.commit()
            print("resumed")
            return 0
        print(f"not resumed (state {bank.status}; the command must be exactly CONTINUE)")
        return 1
    if cmd == "settle":
        if len(args) != 2 or args[1] not in ("void", "won", "lost"):
            print("usage: settle LEG_ID void|won|lost")
            return 2
        leg = store.one("SELECT * FROM legs WHERE id=?", (int(args[0]),))
        if not leg or leg["result"] != "pending":
            print("leg not found or already settled")
            return 1
        store.x("UPDATE legs SET result=?, settled_at=?, source='manual' WHERE id=?",
                (args[1], iso(utcnow()), leg["id"]))
        store.commit()
        eng.settle_open()
        store.commit()
        eng.notifier.flush()
        store.commit()
        print(f"leg {leg['id']} marked {args[1]}; bankroll {eur(bank.balance)} state {bank.status}")
        return 0
    if cmd == "backtest":
        from .backtest import model_beats_market, run_backtest, summary_text
        days = int(args[0]) if args else 365
        r = run_backtest(eng.history, eng.cfg, utcnow().date(), days)
        with open(os.path.join(eng.dir, "backtest.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, indent=1)
        text = summary_text(r)
        print(text)
        eng.notifier.queue("Backtest finished", text, tags="bar_chart")
        store.set("backtest", {k: r[k] for k in ("period", "bets", "brier_1x2", "value_legs",
                                                 "brier_by_model_weight", "flat_stake_roi")})
        if model_beats_market(r) is False:
            eng.notifier.queue("Model reliability warning",
                               "On historical data the model is less accurate than the bookmakers' closing odds, "
                               "so its 'value' signals are not evidence of a real edge. Expect NO BET most days.",
                               priority="high", tags="warning")
        store.commit()
        eng.notifier.flush()
        store.commit()
        return 0
    if cmd == "research":
        return _research(eng, args)
    if cmd == "notify-test":
        eng.notifier.queue("Test notification", "If you can read this on your iPhone, push alerts work.",
                           tags="bell")
        store.commit()
        sent, failed = eng.notifier.flush()
        store.commit()
        print(f"sent {sent}, failed {failed}, channels {eng.notifier.channels or 'none configured'}")
        return 0 if sent else 1
    print(__doc__)
    return 2


def _research(eng: Engine, args: list[str]) -> int:
    """Per-market chronological backtests + promotion gate (needs numpy)."""
    from .research.backtest import Research
    from .research.data import ResearchData
    from .research.gate import evaluate_gate
    from .research.report import render
    months = int(args[0]) if args else 24
    data = ResearchData(os.path.join(eng.cache_dir, "research"))
    report = Research(data, utcnow().date(), seasons=4, test_months=months,
                      archive_dir=os.path.join(eng.dir, "archive", "api-football")).run()
    gate = evaluate_gate(report)
    with open(os.path.join(eng.dir, "research.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    with open(os.path.join(eng.dir, "gate.json"), "w", encoding="utf-8") as f:
        json.dump(gate, f, indent=1)
    with open(os.path.join(eng.dir, "research.html"), "w", encoding="utf-8") as f:
        f.write(render(report, gate))
    eng.store.set("gate", gate)
    counts = {}
    for fam, g in gate["markets"].items():
        counts.setdefault(g["status"], []).append(fam)
    text = "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in sorted(counts.items()))
    print(text)
    eng.notifier.queue("Market research finished", f"{report['test_period']}\n{text}\n"
                       "Only PASSED markets can be used by the paper bankroll.", tags="bar_chart")
    if not counts.get("PASSED"):
        eng.notifier.queue("Model reliability: no market validated",
                           "No market showed a statistically reliable edge at historical prices. "
                           "The bankroll stays in NO BET; shadow markets are tracked forward without stakes.",
                           priority="high", tags="warning", dedupe_key="gate:none", dedupe_hours=24 * 6)
    eng.store.commit()
    eng.notifier.flush()
    eng.store.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
