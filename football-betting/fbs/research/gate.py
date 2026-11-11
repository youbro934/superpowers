"""Promotion gate: which markets the paper bankroll may use.

PASSED   historical prices exist AND, out of sample, value bets show
         ROI 95% interval entirely above 0, positive closing-line value with its
         interval above 0, at least 300 bets, and a calibrated model (ECE <= 3%).
SHADOW   no historical prices, but the model is calibrated and beats the base
         rate out of sample on 500+ matches. Predictions and live odds are
         logged forward (no stake) until enough priced evidence exists.
REJECTED everything else.

These bars are fixed here and are not relaxed to create betting opportunities.
"""
from __future__ import annotations

from datetime import datetime, timezone

MIN_BETS = 300
MAX_ECE = 0.03
MIN_SHADOW_N = 500

# families with free historical prices; the rest can only earn evidence forward
HIST_PRICED = {"result", "double_chance", "goals_total", "asian_handicap"}

PROD = {"result": "h2h", "double_chance": "double_chance", "goals_total": "totals", "btts": "btts",
        "corners_total": "corners_totals", "cards_total": "cards_totals"}
PROD_SEL = {"1X": "home_draw", "X2": "draw_away", "12": "home_away"}


def _parse_key(key: str):
    # "family:sel@line|model[|subset]"
    head, *rest = key.split("|")
    fam, sel = head.split(":", 1)
    line = None
    if "@" in sel:
        sel, l = sel.split("@")
        line = float(l)
    return fam, sel, line, rest


def evaluate_gate(report: dict) -> dict:
    out = {"generated": datetime.now(timezone.utc).isoformat(), "test_period": report.get("test_period"),
           "rules": {"min_bets": MIN_BETS, "max_ece": MAX_ECE, "min_shadow_n": MIN_SHADOW_N},
           "markets": {}, "production_allowed": [], "production_shadow": []}
    for fam, data in report.get("markets", {}).items():
        sels = data.get("selections", {})
        passed, shadow, notes = [], [], []
        for key, b in data.get("bets", {}).items():
            fam_, sel, line, (model, subset) = _parse_key(key)
            if b.get("n", 0) < MIN_BETS:
                continue
            ci = b.get("roi_ci95") or [None, None]
            clv_ci = b.get("clv_ci95") or [None, None]
            cal = None
            for skey, s in sels.items():
                f2, s2, l2, (m2,) = _parse_key(skey)
                if s2 == sel and l2 == line and m2 == model:
                    cal = s.get("ece")
            ok = (ci[0] is not None and ci[0] > 0 and clv_ci[0] is not None and clv_ci[0] > 0
                  and cal is not None and cal <= MAX_ECE)
            notes.append(f"{key}: n={b['n']} roi={b['roi']:+.3f} ci=[{ci[0]:+.3f},{ci[1]:+.3f}] "
                         f"clv={b.get('clv_mean')} ece={cal}")
            if ok:
                passed.append((sel, line, model))
        priced = fam in HIST_PRICED
        # selection-level: a selection never priced historically (scored against the base rate,
        # e.g. over 0.5 goals when only 2.5 has prices) can earn SHADOW; a priced one cannot
        for skey, s in sels.items():
            f2, sel, line, (model,) = _parse_key(skey)
            if s.get("reference") != "base_rate":
                continue
            skill = s.get("skill_vs_base_rate")
            if s.get("n", 0) >= MIN_SHADOW_N and s.get("ece", 1) <= MAX_ECE and skill is not None and skill > 0:
                shadow.append((sel, line, model))
        if passed:
            status, reason = "PASSED", f"{len(passed)} selection(s) cleared every bar"
        elif shadow:
            status, reason = "SHADOW", ("no historical prices exist for this market; the model is calibrated and "
                                        f"beats the base rate on {len(shadow)} selection(s), so it is logged forward "
                                        "against live odds without staking")
        elif priced:
            status, reason = "REJECTED", "no selection showed a statistically reliable edge at historical prices"
        else:
            status, reason = "REJECTED", "no historical prices and the model did not beat the base rate reliably"
        out["markets"][fam] = {"status": status, "reason": reason, "passed": passed, "shadow": shadow,
                               "evidence": notes[:20]}
        pk = PROD.get(fam)
        if pk:
            for sel, line, model in passed:
                out["production_allowed"].append([pk, PROD_SEL.get(sel, sel), line])
            for sel, line, model in shadow:
                out["production_shadow"].append([pk, PROD_SEL.get(sel, sel), line])
    return out


def allowed(gate: dict | None, market: str, selection: str, line) -> bool:
    if not gate:
        return False
    for m, s, l in gate.get("production_allowed", []):
        if m == market and s == selection and (l is None or line is None or abs(l - line) < 1e-9):
            return True
    return False


def selection_status(gate: dict | None, family: str, sel: str, line, model: str) -> str:
    """PASSED / SHADOW / REJECTED for one live selection. A line the research did not
    validate can at most be SHADOW, and only inside a SHADOW or PASSED family."""
    if not gate:
        return "UNTESTED"
    g = gate.get("markets", {}).get(family)
    if not g:
        return "UNTESTED"

    def has(lst):
        for s, l, m in lst:
            if s == sel and m == model and ((l is None and line is None) or
                                            (l is not None and line is not None and abs(l - line) < 1e-9)):
                return True
        return False

    if has(g.get("passed", [])):
        return "PASSED"
    if has(g.get("shadow", [])):
        return "SHADOW"
    if g.get("status") in ("PASSED", "SHADOW") and family not in HIST_PRICED:
        return "SHADOW"
    return "REJECTED"
