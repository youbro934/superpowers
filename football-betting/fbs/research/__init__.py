"""Multi-market research: per-market models, chronological backtests, promotion gate.

Nothing in this package can place a bet. Production only uses a market after
`gate.py` marks it PASSED from out-of-sample evidence.
"""
