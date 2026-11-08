"""Static HTML dashboard + CSV exports, regenerated every run."""
from __future__ import annotations

import csv
import html
import json
import os

from .util import eur

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6470;--line:#e2e5ea;--good:#137a3f;--bad:#b42318;--warn:#a15c00;--accent:#1f5fbf}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--text:#e7eaee;--muted:#9aa3ad;--line:#2a3038;--good:#4cc38a;--bad:#ff7a6b;--warn:#f2b45a;--accent:#7fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 2px}h2{font-size:16px;margin:24px 0 8px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.v{font-size:22px;font-weight:600;margin-top:2px}
.banner{border-radius:10px;padding:10px 12px;margin:12px 0;border:1px solid var(--line);background:var(--card)}
.warn{border-color:var(--warn)}.bad{color:var(--bad)}.good{color:var(--good)}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}td.wrap{white-space:normal;min-width:240px}
"""


def _t(rows, cols, wrap=()):
    head = "".join(f"<th>{html.escape(c)}</th>" for c, _ in cols)
    body = []
    for r in rows:
        cells = []
        for c, fn in cols:
            val = fn(r)
            cls = ' class="wrap"' if c in wrap else ""
            cells.append(f"<td{cls}>{val}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body = [f'<tr><td colspan="{len(cols)}" class="muted">Nothing yet</td></tr>']
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def write(engine) -> str:
    s, bank, cfg = engine.store, engine.bank, engine.cfg
    bets = s.q("SELECT * FROM bets ORDER BY id DESC LIMIT 200")
    legs_by_bet: dict[int, list] = {}
    for l in s.q("SELECT * FROM legs ORDER BY id"):
        legs_by_bet.setdefault(l["bet_id"], []).append(l)
    settled = [b for b in bets if b["status"] in ("WON", "LOST", "VOID")]
    won = sum(1 for b in settled if b["status"] == "WON")
    lost = sum(1 for b in settled if b["status"] == "LOST")
    equity = bank.equity()
    initial = bank.initial
    status = bank.status
    calib = s.get("calibration")
    disc = s.get("last_discovery") or {}
    e = html.escape

    status_txt = {"ACTIVE": "Active", "STOPPED_TARGET": "STOPPED at €500 — waiting for CONTINUE",
                  "BUSTED": "Bankroll lost — stopped"}.get(status, status)
    mode = cfg.execution_mode.upper()
    cards = [("Bankroll", eur(equity)), ("Net result", eur(equity - initial)), ("Status", status_txt),
             ("Mode", "PAPER (simulated)" if mode == "PAPER" else mode),
             ("Bets won / lost", f"{won} / {lost}"), ("Wins in a row needed", _wins_needed(equity, cfg))]
    grid = "".join(f'<div class="card"><div class="k">{e(k)}</div><div class="v">{e(str(v))}</div></div>'
                   for k, v in cards)

    def legs_txt(b):
        return "<br>".join(e(f"{l['home']} v {l['away']} — {l['market']} {l['selection']}"
                             f"{'' if l['line'] is None else ' ' + format(l['line'], 'g')} @ {l['odds']:.2f}"
                             f" [{l['result']}]") for l in legs_by_bet.get(b["id"], []))

    bets_tbl = _t(bets, [
        ("Date", lambda b: e(b["local_date"])), ("#", lambda b: b["slot"]), ("Mode", lambda b: e(b["mode"])),
        ("Status", lambda b: e(b["status"])), ("Stake", lambda b: eur(b["stake_cents"])),
        ("Odds", lambda b: f"{b['odds']:.2f}"), ("p", lambda b: f"{b['prob']:.0%}"),
        ("EV", lambda b: f"{b['ev']:+.1%}"),
        ("Return", lambda b: "" if b["return_cents"] is None else eur(b["return_cents"])),
        ("Selections", legs_txt)], wrap=("Selections",))
    ledger = s.q("SELECT * FROM ledger ORDER BY id DESC LIMIT 200")
    ledger_tbl = _t(ledger, [("Time (UTC)", lambda r: e(r["ts"][:16].replace("T", " "))),
                             ("Type", lambda r: e(r["kind"])), ("Amount", lambda r: eur(r["amount_cents"])),
                             ("Cash balance", lambda r: eur(r["balance_cents"])), ("Note", lambda r: e(r["note"] or ""))])
    decisions = s.q("SELECT * FROM decisions ORDER BY id DESC LIMIT 30")
    dec_tbl = _t(decisions, [("Date", lambda r: e(r["local_date"])), ("Outcome", lambda r: e(r["outcome"])),
                             ("Reason", lambda r: e(r["reason"] or ""))], wrap=("Reason",))
    prov = s.q("SELECT * FROM provider_status ORDER BY provider")
    prov_tbl = _t(prov, [("Provider", lambda r: e(r["provider"])), ("Last OK", lambda r: e((r["last_ok"] or "")[:16])),
                         ("Failures in a row", lambda r: r["consecutive_failures"]),
                         ("Last error", lambda r: e((r["last_error"] or "")[:120]))], wrap=("Last error",))
    runs = s.q("SELECT * FROM runs ORDER BY id DESC LIMIT 12")
    runs_tbl = _t(runs, [("Started (UTC)", lambda r: e((r["started_at"] or "")[:16])),
                         ("Status", lambda r: e(r["status"] or "")),
                         ("Summary", lambda r: e((r["summary"] or "").splitlines()[-1] if r["summary"] else ""))],
                  wrap=("Summary",))
    favs = disc.get("favorites") or []
    fav_html = "<br>".join(e(f) for f in favs) or '<span class="muted">No favorite-team match in the next 36 h</span>'
    calib_html = (f"Model Brier {calib['brier_model']:.3f} vs bookmaker {calib['brier_market']:.3f} "
                  f"on {calib['n']} settled selections (lower is better)." if calib
                  else "Needs 30 settled selections before calibration can be measured.")

    banner = ""
    if mode == "PAPER":
        banner = ('<div class="banner warn"><b>Paper trading.</b> No real money is staked. '
                  'Every bet is simulated at the quoted bookmaker price.</div>')
    if status == "STOPPED_TARGET":
        banner += '<div class="banner warn"><b>€500 reached.</b> New bets are blocked until you send CONTINUE.</div>'
    if status == "BUSTED":
        banner += '<div class="banner warn bad"><b>Bankroll lost.</b> No deposits are allowed; betting stopped.</div>'

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Football Bet Tracker</title>
<style>{CSS}</style></head><body><main>
<h1>Football Bet Tracker</h1>
<div class="muted">Updated {e(engine.now.strftime('%Y-%m-%d %H:%M UTC'))} · {disc.get('events', 0)} upcoming matches seen in last scan</div>
{banner}
<div class="grid" style="margin-top:12px">{grid}</div>
<div class="banner"><b>Risk.</b> Each bet stakes the whole bankroll. One loss ends the run at €0, and no
deposits are allowed. Reaching €500 from €10 at odds 1.30–1.40 needs 12–15 wins in a row.</div>
<h2>Favorite teams — upcoming</h2><div class="card">{fav_html}</div>
<h2>Bets</h2>{bets_tbl}
<h2>Decisions</h2>{dec_tbl}
<h2>Ledger</h2>{ledger_tbl}
<h2>Model reliability</h2><div class="card">{e(calib_html)}</div>
<h2>Data providers</h2>{prov_tbl}
<h2>Recent runs</h2>{runs_tbl}
</main></body></html>"""
    path = os.path.join(engine.dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    _export_csv(engine)
    return path


def _wins_needed(equity: int, cfg) -> str:
    import math
    if equity <= 0:
        return "—"
    if equity >= cfg.target_stop_cents:
        return "0"
    mid = (cfg.target_odds_min + cfg.target_odds_max) / 2
    return str(math.ceil(math.log(cfg.target_stop_cents / equity) / math.log(mid)))


def _export_csv(engine):
    s = engine.store
    for table in ("bets", "legs", "ledger"):
        rows = s.q(f"SELECT * FROM {table} ORDER BY id")
        path = os.path.join(engine.dir, f"{table}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if rows:
                w.writerow(rows[0].keys())
                w.writerows([tuple(r) for r in rows])
