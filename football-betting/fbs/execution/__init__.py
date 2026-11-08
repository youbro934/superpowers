"""Bet execution adapters.

Only paper trading is implemented. A real-money adapter must implement
`Executor` against an officially documented betting API (for example the
Betfair Exchange API, where it is legal for the account holder) and must:

  * use `client_ref` as the bookmaker's idempotency key / customer reference,
  * implement `find(client_ref)` so a crashed run never places the same bet twice,
  * never scrape websites or work around authentication or anti-bot controls.
"""
from __future__ import annotations

from ..config import LIVE_CONFIRMATION_PHRASE, Settings


class ExecutionError(Exception):
    pass


class Executor:
    mode = "abstract"
    is_real_money = False

    def find(self, client_ref: str) -> str | None:
        """External reference of an already-placed bet with this client_ref, else None."""
        raise NotImplementedError

    def place(self, client_ref: str, stake_cents: int, legs: list, odds: float) -> str:
        raise NotImplementedError


class PaperExecutor(Executor):
    """Simulated placement at the quoted price. No money moves anywhere."""
    mode = "paper"

    def __init__(self, store):
        self.store = store

    def find(self, client_ref: str) -> str | None:
        r = self.store.one("SELECT external_ref FROM bets WHERE client_ref=? AND external_ref IS NOT NULL",
                           (client_ref,))
        return r[0] if r else None

    def place(self, client_ref: str, stake_cents: int, legs: list, odds: float) -> str:
        return f"PAPER-{client_ref}"


def build_executor(cfg: Settings, store) -> Executor:
    if cfg.execution_mode == "paper":
        return PaperExecutor(store)
    if cfg.execution_mode == "live":
        if cfg.live_confirmation != LIVE_CONFIRMATION_PHRASE:
            raise ExecutionError("live mode requested without FBS_LIVE_CONFIRMATION; refusing")
        raise ExecutionError("live mode requested but no authorized bookmaker adapter is installed; "
                             "the system stays in paper mode until one is implemented and tested")
    raise ExecutionError(f"unknown execution mode {cfg.execution_mode!r}")
