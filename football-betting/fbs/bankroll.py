"""Compound (all-in) bankroll manager and ledger.

Rules enforced here, not in the caller:
  * one deposit ever, at most EUR 10
  * every stake is the whole bankroll (100% of the previous gross return)
  * only one open bet at a time, so the two daily bets are sequential
  * at most two bets per local calendar day
  * at >= EUR 500 betting is blocked until the exact command CONTINUE
  * below the minimum stake the bankroll is busted and betting stops for good
"""
from __future__ import annotations

import math

from .config import Settings
from .db import Store
from .util import iso, utcnow

ACTIVE = "ACTIVE"
STOPPED_TARGET = "STOPPED_TARGET"
BUSTED = "BUSTED"


class BankrollError(Exception):
    pass


class Bankroll:
    def __init__(self, store: Store, cfg: Settings):
        self.s = store
        self.cfg = cfg

    def init(self):
        if self.s.one("SELECT 1 FROM ledger LIMIT 1"):
            return False
        amount = min(self.cfg.initial_bankroll_cents, 1000)
        self._ledger("DEPOSIT", amount, None, "initial bankroll (the only deposit ever allowed)")
        self.s.set("status", ACTIVE)
        self.s.set("initial_cents", amount)
        return True

    # --- reads ---
    @property
    def balance(self) -> int:
        r = self.s.one("SELECT balance_cents FROM ledger ORDER BY id DESC LIMIT 1")
        return r[0] if r else 0

    @property
    def status(self) -> str:
        return self.s.get("status", ACTIVE)

    @property
    def initial(self) -> int:
        return self.s.get("initial_cents", self.cfg.initial_bankroll_cents)

    def open_bets(self):
        return self.s.q("SELECT * FROM bets WHERE status IN ('PLACING','OPEN') ORDER BY id")

    def bets_on(self, local_date: str) -> int:
        return self.s.one("SELECT COUNT(*) FROM bets WHERE local_date=? AND status!='FAILED'", (local_date,))[0]

    def exposure(self) -> int:
        r = self.s.one("SELECT COALESCE(SUM(stake_cents),0) FROM bets WHERE status IN ('PLACING','OPEN')")
        return r[0]

    def equity(self) -> int:
        """Cash plus stakes currently at risk (what the bankroll is 'worth' before settlement)."""
        return self.balance + self.exposure()

    def can_bet(self, local_date: str) -> tuple[bool, str]:
        if self.status == STOPPED_TARGET:
            return False, "stopped at target; waiting for CONTINUE"
        if self.status == BUSTED:
            return False, "bankroll lost"
        if self.open_bets():
            return False, "a bet is still open (bets are sequential)"
        if self.bets_on(local_date) >= self.cfg.max_bets_per_day:
            return False, f"daily limit of {self.cfg.max_bets_per_day} bets reached"
        if self.balance < self.cfg.min_stake_cents:
            return False, "bankroll below minimum stake"
        return True, "ok"

    # --- writes ---
    def _ledger(self, kind: str, amount: int, bet_id, note: str):
        bal = self.balance + amount
        if bal < 0:
            raise BankrollError("ledger would go negative")
        self.s.x("INSERT INTO ledger(ts,kind,amount_cents,balance_cents,bet_id,note) VALUES(?,?,?,?,?,?)",
                 (iso(utcnow()), kind, amount, bal, bet_id, note))

    def reserve(self, client_ref: str, local_date: str, slot: int, mode: str, odds: float,
                prob: float, ev: float) -> int:
        """Create the bet row in PLACING state and move the whole bankroll into it."""
        ok, why = self.can_bet(local_date)
        if not ok:
            raise BankrollError(why)
        stake = self.balance
        bet_id = self.s.x(
            "INSERT INTO bets(client_ref,created_at,local_date,slot,mode,status,stake_cents,odds,prob,ev) "
            "VALUES(?,?,?,?,?,'PLACING',?,?,?,?)",
            (client_ref, iso(utcnow()), local_date, slot, mode, stake, odds, prob, ev))
        self._ledger("STAKE", -stake, bet_id, f"all-in stake @ {odds:.2f}")
        return bet_id

    def confirm(self, bet_id: int, external_ref: str):
        self.s.x("UPDATE bets SET status='OPEN', external_ref=? WHERE id=?", (external_ref, bet_id))

    def fail(self, bet_id: int, note: str):
        bet = self.s.one("SELECT * FROM bets WHERE id=?", (bet_id,))
        if bet["status"] != "PLACING":
            return
        self.s.x("UPDATE bets SET status='FAILED', note=? WHERE id=?", (note[:300], bet_id))
        self._ledger("REFUND", bet["stake_cents"], bet_id, "placement failed, stake returned")

    def settle(self, bet_id: int, outcome: str, effective_odds: float) -> int:
        """outcome WON / LOST / VOID. Returns the gross return in cents."""
        bet = self.s.one("SELECT * FROM bets WHERE id=?", (bet_id,))
        if bet["status"] != "OPEN":
            raise BankrollError(f"bet {bet_id} is {bet['status']}, not OPEN")
        stake = bet["stake_cents"]
        if outcome == "WON":
            ret = math.floor(stake * effective_odds + 1e-6)
        elif outcome == "VOID":
            ret = stake
        else:
            ret = 0
        self.s.x("UPDATE bets SET status=?, settled_at=?, return_cents=? WHERE id=?",
                 (outcome, iso(utcnow()), ret, bet_id))
        if ret:
            self._ledger("RETURN", ret, bet_id, f"{outcome.lower()} return")
        else:
            self._ledger("RETURN", 0, bet_id, "lost")
        self._check_thresholds()
        return ret

    def _check_thresholds(self):
        bal = self.balance
        if self.status == ACTIVE and bal >= self.cfg.target_stop_cents and not self.s.get("target_acknowledged"):
            self.s.set("status", STOPPED_TARGET)
        if bal < self.cfg.target_stop_cents and self.s.get("target_acknowledged"):
            self.s.set("target_acknowledged", False)   # re-arm for the next crossing
        if self.status == ACTIVE and bal < self.cfg.min_stake_cents and not self.open_bets():
            self.s.set("status", BUSTED)

    def resume(self, command: str) -> bool:
        if command != "CONTINUE":
            return False
        if self.status != STOPPED_TARGET:
            return False
        self.s.set("status", ACTIVE)
        self.s.set("target_acknowledged", True)
        return True
