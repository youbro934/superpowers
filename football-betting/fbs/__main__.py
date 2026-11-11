"""Command line: python -m fbs [--data-dir DIR] COMMAND

  run                 one autonomous cycle (what the scheduler calls)
  status              print bankroll, state and open bets
  continue CONTINUE   resume after the EUR 500 stop (the word must be exact)
  settle LEG_ID void|won|lost   manual settlement for a postponed/unsettleable selection
  backtest [DAYS]     walk-forward backtest on historical closing odds (goal model only)
  research [MONTHS]   per-market chronological backtests + promotion gate (needs numpy)
  notify-test         send a test push notification
  start | stop        resume / pause issuing opportunities (settlement continues)
  confirm JSON        record a bet the owner placed: {"id","stake","odds","bookmaker","reference"}
  reject JSON         decline an opportunity: {"id"}
  settle-bet JSON     manual settlement when no verified result exists: {"bet_id","result"}
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
    if cmd in ("start", "stop"):
        eng.store.set("ai_enabled", cmd == "start")
        eng.notifier.queue("AI started" if cmd == "start" else "AI stopped",
                           "Scanning for opportunities resumed." if cmd == "start" else
                           "No new opportunities will be issued. Open bets are still tracked and settled.",
                           tags="arrow_forward" if cmd == "start" else "stop_button")
        return _finish(eng, f"AI {'started' if cmd == 'start' else 'stopped'}")
    if cmd in ("confirm", "reject"):
        try:
            payload = json.loads(args[0]) if args else {}
        except ValueError:
            print("payload must be JSON")
            return 2
        ok, msg = (eng.opps.confirm if cmd == "confirm" else eng.opps.reject)(payload)
        if not ok:
            # an input the owner can correct is not a system failure: tell the phone, exit cleanly
            eng.notifier.queue(f"{cmd.title()} not recorded", msg, tags="warning")
        return _finish(eng, msg, 0)
    if cmd == "settle-bet":
        # manual settlement of a whole bet when no verified result can be obtained
        try:
            payload = json.loads(args[0])
            bet = store.one("SELECT * FROM bets WHERE id=?", (int(payload["bet_id"]),))
            res = payload["result"]
        except (ValueError, KeyError, IndexError, TypeError):
            print('usage: settle-bet \'{"bet_id": 1, "result": "won|lost|void"}\'')
            return 2
        if bet is None or bet["status"] != "OPEN" or res not in ("won", "lost", "void"):
            return _finish(eng, "bet not open or bad result", 1)
        store.x("UPDATE legs SET result=?, settled_at=?, source='manual' WHERE bet_id=? AND result='pending'",
                (res if res != "won" else "won", iso(utcnow()), bet["id"]))
        store.x("UPDATE bets SET settle_source='manual' WHERE id=?", (bet["id"],))
        store.commit()
        eng.settle_open()
        return _finish(eng, f"bet {bet['id']} settled manually as {res}")
    if cmd == "research":
        return _research(eng, args)
    if cmd == "issue-command":
        return _issue_command(eng)
    if cmd == "verify-apif":
        return _verify_apif(eng)
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


def _issue_command(eng: Engine) -> int:
    """Owner command from a GitHub issue. Everything arrives via environment variables."""
    from .commands import CommandError, authorize, parse
    env = os.environ
    result_path = env.get("FBS_RESULT_FILE", "command-result.txt")
    try:
        authorize(env.get("REPO_PRIVATE", ""), env.get("ISSUE_AUTHOR", ""), env.get("REPO_OWNER", ""),
                  env.get("ISSUE_ASSOCIATION", ""))
        cmd, payload = parse(env.get("ISSUE_BODY", ""))
    except CommandError as e:
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(f"Not executed: {e}")
        print(f"refused: {e}")
        return 0
    if cmd == "continue":
        args = [payload["confirm"]]
    elif cmd in ("confirm", "reject", "settle-bet"):
        args = [json.dumps(payload)]
    else:
        args = []
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _dispatch(eng, cmd, args)
    out = buf.getvalue().strip().splitlines()
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(f"`{cmd}` processed: {out[-1] if out else 'done'}")
    print(f"{cmd}: {out[-1] if out else 'done'}")
    return 0 if code in (0, 1) else code


def _verify_apif(eng: Engine) -> int:
    """Check the real API-Football account: plan, quota, and whether current fixtures/odds are served."""
    from .util import ProviderError, http_request
    key = eng.cfg.api_football_key
    if not key:
        print("API_FOOTBALL_KEY not set")
        return 1
    hdr = {"x-apisports-key": key}
    report = {}
    try:
        st = http_request("https://v3.football.api-sports.io/status", headers=hdr, provider="api-football").json()
        r = st.get("response") or {}
        report["account_plan"] = (r.get("subscription") or {}).get("plan")
        report["requests_today"] = (r.get("requests") or {}).get("current")
        report["requests_limit_day"] = (r.get("requests") or {}).get("limit_day")
        report["status_errors"] = st.get("errors")
        day = utcnow().date().isoformat()
        fx = http_request("https://v3.football.api-sports.io/fixtures", params={"date": day}, headers=hdr,
                          provider="api-football").json()
        report["fixtures_errors"] = fx.get("errors")
        resp = fx.get("response") or []
        from .research.collector import PRIORITY_LEAGUES
        pri = [f for f in resp if (f.get("league") or {}).get("id") in PRIORITY_LEAGUES]
        report["fixtures_today_total"] = len(resp)
        report["fixtures_today_priority"] = len(pri)
        report["seasons_seen"] = sorted({(f.get("league") or {}).get("season") for f in pri if f.get("league")})
        if pri:
            f0 = pri[0]
            od = http_request("https://v3.football.api-sports.io/odds", params={
                "fixture": f0["fixture"]["id"]}, headers=hdr, provider="api-football").json()
            report["odds_errors"] = od.get("errors")
            books = (od.get("response") or [{}])[0].get("bookmakers", []) if od.get("response") else []
            report["odds_bookmakers"] = len(books)
            report["odds_bet_names"] = sorted({b.get("name") for bk in books for b in bk.get("bets", [])})[:40]
    except ProviderError as e:
        report["error"] = str(e)
    with open(os.path.join(eng.dir, "apif-verification.json"), "w", encoding="utf-8") as f:
        json.dump(report | {"checked_at": iso(utcnow())}, f, indent=1)
    print(json.dumps({k: v for k, v in report.items() if k != "odds_bet_names"}))
    eng.store.set("apif_verification", report | {"checked_at": iso(utcnow())})
    eng.store.commit()
    return 0


def _finish(eng: Engine, msg: str, code: int = 0) -> int:
    from . import dashboard
    from .state_export import write_state
    eng.store.commit()
    eng.notifier.flush()
    eng.store.commit()
    dashboard.write(eng)
    write_state(eng)
    print(msg)
    return code


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
