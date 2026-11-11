"""Configuration. Every tunable lives here; secrets come only from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


# Favorite teams, written as canonical names (see fbs.teams.canonical).
FAVORITE_TEAMS = [
    "barcelona",
    "real madrid",
    "bayern munich",
    "paris saint germain",
    "benfica",
    "sporting",
    "porto",
    "flamengo",
]


@dataclass(frozen=True)
class Competition:
    code: str              # internal code
    name: str
    odds_sport: str        # The Odds API sport key
    couk: str | None       # football-data.co.uk file code (history for the model)
    couk_extra: bool       # True: file lives under /new/ (one file, all seasons)
    fdorg: str | None      # football-data.org competition code (results)
    has_corners: bool      # football-data.co.uk file carries corners/cards


COMPETITIONS: list[Competition] = [
    Competition("PL", "Premier League", "soccer_epl", "E0", False, "PL", True),
    Competition("ELC", "Championship", "soccer_efl_champ", "E1", False, "ELC", True),
    Competition("PD", "La Liga", "soccer_spain_la_liga", "SP1", False, "PD", True),
    Competition("BL1", "Bundesliga", "soccer_germany_bundesliga", "D1", False, "BL1", True),
    Competition("SA", "Serie A", "soccer_italy_serie_a", "I1", False, "SA", True),
    Competition("FL1", "Ligue 1", "soccer_france_ligue_one", "F1", False, "FL1", True),
    Competition("PPL", "Primeira Liga", "soccer_portugal_primeira_liga", "P1", False, "PPL", True),
    Competition("DED", "Eredivisie", "soccer_netherlands_eredivisie", "N1", False, "DED", True),
    Competition("BEL", "Belgian Pro League", "soccer_belgium_first_div", "B1", False, None, True),
    Competition("SPL", "Scottish Premiership", "soccer_spl", "SC0", False, None, True),
    Competition("TUR", "Turkish Super Lig", "soccer_turkey_super_league", "T1", False, None, True),
    Competition("GRE", "Greek Super League", "soccer_greece_super_league", "G1", False, None, True),
    Competition("BSA", "Brasileirao Serie A", "soccer_brazil_campeonato", "BRA", True, "BSA", False),
    Competition("ARG", "Argentina Primera", "soccer_argentina_primera_division", "ARG", True, None, False),
    Competition("MLS", "MLS", "soccer_usa_mls", "USA", True, None, False),
    Competition("MEX", "Liga MX", "soccer_mexico_ligamx", "MEX", True, None, False),
    Competition("JPN", "J1 League", "soccer_japan_j_league", "JPN", True, None, False),
]

COMPETITION_BY_SPORT = {c.odds_sport: c for c in COMPETITIONS}
COMPETITION_BY_CODE = {c.code: c for c in COMPETITIONS}

LIVE_CONFIRMATION_PHRASE = "I AUTHORIZE REAL MONEY BETTING"


@dataclass
class Settings:
    # --- bankroll rules (fixed by the owner) ---
    initial_bankroll_cents: int = 1000          # EUR 10.00; hard-capped at 1000 below
    target_stop_cents: int = 50000              # EUR 500 automatic stop
    min_stake_cents: int = 10                   # below this the bankroll is busted
    max_bets_per_day: int = 2
    target_odds_min: float = 1.30
    target_odds_max: float = 1.40
    timezone: str = "Europe/Lisbon"

    # --- decision engine ---
    model_weight: float = 0.5        # blend of model vs de-vigged market probability
    min_leg_edge: float = 0.02       # EV a single selection must clear to be used anywhere
    min_bet_edge: float = 0.03       # EV the final bet (single or combo) must clear
    min_leg_prob: float = 0.60
    max_model_market_gap: float = 0.15   # bigger disagreement = model probably missing info
    min_leg_odds: float = 1.05
    secondary_market_extra_edge: float = 0.03   # corners / cards are noisier
    injury_extra_edge: float = 0.02
    injury_alert_count: int = 3
    max_combo_legs: int = 3
    max_entry_legs: int = 4          # 4th leg only when no 1-3 leg entry qualifies
    apif_odds_max_age_minutes: int = 240   # free plan budget: prices refreshed every few hours
    apif_odds_refresh_hours: float = 3.0
    combo_pool_size: int = 25
    min_minutes_to_kickoff: int = 60
    lookahead_hours: int = 36          # discovery window (free event listing)
    decision_window_hours: float = 8.0  # odds are only bought for matches this close
    max_odds_age_minutes: int = 120

    # --- model ---
    time_decay_xi: float = 0.0019    # per day (~1 year half-life)
    min_team_eff_matches: float = 8.0
    ratings_max_age_hours: float = 24.0
    history_seasons: int = 2

    # --- data budget ---
    odds_regions: str = "eu"
    odds_markets: str = "h2h,totals"
    favorite_extra_markets: str = "btts,double_chance"
    secondary_markets: str = ""      # e.g. "alternate_totals_corners,alternate_totals_cards" if your plan has them
    league_odds_refresh_hours: float = 12.0
    odds_credit_reserve: int = 60    # below this only favorites' leagues are scanned
    allowed_bookmakers: list[str] = field(default_factory=list)   # empty = any returned

    # --- scheduling / notifications ---
    no_bet_notice_hour: int = 22
    settlement_wait_hours: float = 2.25
    settlement_alert_hours: float = 12.0
    alert_dedupe_hours: float = 6.0

    # --- execution ---
    execution_mode: str = "manual"   # manual: cards to the iPhone, owner places bets; paper: simulated
    live_confirmation: str = ""

    # --- secrets ---
    odds_api_key: str = ""
    football_data_org_key: str = ""
    api_football_key: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    healthcheck_url: str = ""
    dashboard_url: str = ""
    app_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.initial_bankroll_cents = min(1000, _int("FBS_INITIAL_BANKROLL_CENTS", 1000))
        s.timezone = _env("FBS_TIMEZONE", s.timezone)
        s.model_weight = _float("FBS_MODEL_WEIGHT", s.model_weight)
        s.min_leg_edge = _float("FBS_MIN_LEG_EDGE", s.min_leg_edge)
        s.min_bet_edge = _float("FBS_MIN_BET_EDGE", s.min_bet_edge)
        s.min_leg_prob = _float("FBS_MIN_LEG_PROB", s.min_leg_prob)
        s.odds_regions = _env("FBS_ODDS_REGIONS", s.odds_regions)
        s.odds_markets = _env("FBS_ODDS_MARKETS", s.odds_markets)
        s.favorite_extra_markets = _env("FBS_FAVORITE_EXTRA_MARKETS", s.favorite_extra_markets)
        s.secondary_markets = _env("FBS_SECONDARY_MARKETS", s.secondary_markets)
        s.odds_credit_reserve = _int("FBS_ODDS_CREDIT_RESERVE", s.odds_credit_reserve)
        books = _env("FBS_ALLOWED_BOOKMAKERS")
        s.allowed_bookmakers = [b.strip() for b in books.split(",") if b.strip()]
        s.execution_mode = _env("FBS_EXECUTION_MODE", "manual").lower() or "manual"
        s.live_confirmation = _env("FBS_LIVE_CONFIRMATION")
        s.odds_api_key = _env("ODDS_API_KEY")
        s.football_data_org_key = _env("FOOTBALL_DATA_ORG_KEY")
        s.api_football_key = _env("API_FOOTBALL_KEY")
        s.ntfy_topic = _env("NTFY_TOPIC")
        s.ntfy_server = _env("NTFY_SERVER", s.ntfy_server).rstrip("/")
        s.ntfy_token = _env("NTFY_TOKEN")
        s.telegram_bot_token = _env("TELEGRAM_BOT_TOKEN")
        s.telegram_chat_id = _env("TELEGRAM_CHAT_ID")
        s.healthcheck_url = _env("HEALTHCHECK_URL")
        s.dashboard_url = _env("FBS_DASHBOARD_URL")
        s.app_url = _env("FBS_APP_URL")
        return s
