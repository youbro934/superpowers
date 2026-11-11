"""Static HTML page for the multi-market research report."""
from __future__ import annotations

import html

from ..dashboard import CSS


def _f(x, pct=False, d=3):
    if x is None:
        return "—"
    return f"{x:+.1%}" if pct else f"{x:.{d}f}"


def render(report: dict, gate: dict) -> str:
    e = html.escape
    parts = [f"<h1>Market Research</h1><div class='muted'>Out-of-sample test period {e(str(report['test_period']))}. "
             f"{e(report['method'])}.</div>"]
    parts.append("<div class='banner'><b>How to read this.</b> Brier: lower is better. ECE: average calibration "
                 "error. ROI is per unit staked at the pre-match price, with a 95% interval; an interval that "
                 "includes 0 is not evidence of an edge. CLV &gt; 0 means the price taken beat the closing line.</div>")
    rows = []
    for fam, g in sorted(gate["markets"].items()):
        data = report["markets"].get(fam, {})
        sels = data.get("selections", {})
        best = None
        for k, s in sels.items():
            if s.get("n", 0) >= 200 and (best is None or s.get("ece", 1) < best[1].get("ece", 1)):
                best = (k, s)
        bets = [(k, b) for k, b in data.get("bets", {}).items() if b.get("n")]
        bets.sort(key=lambda kb: -kb[1]["n"])
        bk, bb = bets[0] if bets else (None, {})
        ref = best[1].get("reference") if best else None
        rows.append(
            f"<tr><td>{e(fam)}</td><td><b>{e(g['status'])}</b></td>"
            f"<td>{e(best[0]) if best else '—'}</td><td>{best[1]['n'] if best else '—'}</td>"
            f"<td>{_f(best[1].get('brier_model_same_rows')) if best else '—'}</td>"
            f"<td>{_f(best[1].get('brier_' + ref)) if best and ref else '—'} {e(ref or '')}</td>"
            f"<td>{_f(best[1].get('ece')) if best else '—'}</td>"
            f"<td>{(_f(best[1]['predicted_rate'], True) + ' / ' + _f(best[1]['actual_rate'], True)) if best else '—'}</td>"
            f"<td>{bb.get('n', '—')}</td>"
            f"<td>{(_f(bb['roi'], True) + ' [' + _f(bb['roi_ci95'][0], True) + ', ' + _f(bb['roi_ci95'][1], True) + ']') if bb else 'no historical prices'}</td>"
            f"<td>{_f(bb.get('clv_mean'), True) if bb else '—'}</td>"
            f"<td class='wrap'>{e(g['reason'])}</td></tr>")
    parts.append("<h2>Markets</h2><div class='scroll'><table><thead><tr><th>Market</th><th>Status</th>"
                 "<th>Best-calibrated selection</th><th>n</th><th>Brier model</th><th>Brier reference</th>"
                 "<th>ECE</th><th>Pred / actual</th><th>Value bets</th><th>ROI [95%]</th><th>CLV</th>"
                 "<th>Gate reason</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    cov_rows = []
    for lg, c in sorted(report["coverage"].items()):
        if "error" in c:
            cov_rows.append(f"<tr><td>{e(lg)}</td><td colspan='9'>{e(c['error'])}</td></tr>")
            continue
        cells = "".join(f"<td>{c.get(k, 0):.0%}</td>" for k in ("corners", "fouls", "yellows", "sot", "referee",
                                                                 "1x2_taken", "ou25_taken", "ah_taken"))
        cov_rows.append(f"<tr><td>{e(lg)}</td><td>{c.get('matches', 0)}</td>{cells}</tr>")
    parts.append("<h2>Data coverage (share of matches with each item)</h2><div class='scroll'><table><thead><tr>"
                 "<th>League</th><th>Matches</th><th>Corners</th><th>Fouls</th><th>Yellows</th><th>Shots on target</th>"
                 "<th>Referee</th><th>1X2 odds</th><th>O/U 2.5 odds</th><th>AH odds</th></tr></thead><tbody>"
                 + "".join(cov_rows) + "</tbody></table></div>")
    corr = report.get("correlations", {})
    if corr.get("totals"):
        items = "".join(f"<tr><td>{e(k)}</td><td>{v:+.3f}</td></tr>" for k, v in corr["totals"].items())
        parts.append(f"<h2>Same-match correlations (n={corr['n']})</h2><div class='muted'>Why same-match legs are "
                     f"never multiplied blindly. Home win ~ over 2.5 phi: {corr.get('home_win~over2.5_phi')}</div>"
                     f"<div class='scroll'><table><tbody>{items}</tbody></table></div>")
    parts.append("<h2>Not available</h2><div class='card'>" + "<br>".join(
        f"<b>{e(k)}</b>: {e(v)}" for k, v in report.get("unavailable", {}).items()) + "</div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Market Research</title><style>{CSS}</style>"
            f"</head><body><main>{''.join(parts)}</main></body></html>")
