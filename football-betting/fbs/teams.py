"""Team-name matching across providers.

Each provider spells clubs differently ("Sp Lisbon", "Sporting Lisbon",
"Sporting Clube de Portugal"). A name that cannot be matched with confidence
is left unmatched; the match is then skipped rather than guessed.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from .config import FAVORITE_TEAMS

_STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ud", "sd", "rc", "club", "de", "sv", "ss",
         "as", "cp", "cr", "ec", "se", "fk", "sk", "if", "bk", "ca", "fsv", "vfb", "vfl",
         "tsg", "rcd", "ssc", "acf", "us", "sl", "the", "rj"}

# Exact (normalized) spelling -> canonical name. Checked before and after stop-word removal.
ALIASES = {
    "man united": "manchester united", "man utd": "manchester united", "manchester utd": "manchester united",
    "man city": "manchester city", "nott m forest": "nottingham forest", "nottm forest": "nottingham forest",
    "wolves": "wolverhampton wanderers", "wolverhampton": "wolverhampton wanderers",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur", "newcastle": "newcastle united",
    "west ham": "west ham united", "brighton": "brighton and hove albion",
    "brighton hove albion": "brighton and hove albion", "leicester": "leicester city",
    "leeds": "leeds united", "sheffield utd": "sheffield united", "sheffield weds": "sheffield wednesday",
    "sp lisbon": "sporting", "sporting lisbon": "sporting", "sporting clube portugal": "sporting",
    "sporting portugal": "sporting", "sporting cp": "sporting",
    "sport lisboa e benfica": "benfica", "sl benfica": "benfica",
    "sp braga": "braga", "sporting braga": "braga", "sc braga": "braga",
    "vitoria guimaraes": "guimaraes",
    "fc porto": "porto",
    "paris sg": "paris saint germain", "psg": "paris saint germain", "paris saint germain fc": "paris saint germain",
    "paris fc": "paris fc",
    "bayern munchen": "bayern munich", "bayern": "bayern munich", "fc bayern munchen": "bayern munich",
    "ath madrid": "atletico madrid", "atl madrid": "atletico madrid", "atletico de madrid": "atletico madrid",
    "ath bilbao": "athletic bilbao", "athletic": "athletic bilbao", "athletic club": "athletic bilbao",
    "sociedad": "real sociedad", "betis": "real betis", "celta": "celta vigo", "espanol": "espanyol",
    "vallecano": "rayo vallecano", "alaves": "alaves", "fc barcelona": "barcelona",
    "real madrid cf": "real madrid",
    "inter": "inter", "inter milan": "inter", "internazionale": "inter", "milan": "milan", "ac milan": "milan",
    "m gladbach": "borussia monchengladbach", "monchengladbach": "borussia monchengladbach",
    "dortmund": "borussia dortmund", "leverkusen": "bayer leverkusen", "ein frankfurt": "eintracht frankfurt",
    "flamengo rj": "flamengo", "cr flamengo": "flamengo", "athletico pr": "athletico paranaense",
    "atletico pr": "athletico paranaense", "atletico mg": "atletico mineiro", "botafogo rj": "botafogo",
    "vasco": "vasco da gama", "sao paulo": "sao paulo",
}


def _normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canonical(name: str) -> str:
    s = _normalize(name)
    if s in ALIASES:
        return ALIASES[s]
    tokens = [t for t in s.split() if t not in _STOP and not t.isdigit()]
    stripped = " ".join(tokens) or s
    return ALIASES.get(stripped, stripped)


def is_favorite(name: str) -> bool:
    return canonical(name) in FAVORITE_TEAMS


def match_team(name: str, candidates: set[str] | list[str], cutoff: float = 0.86) -> str | None:
    """Map `name` to one of `candidates` (canonical names). None when unsure."""
    target = canonical(name)
    pool = set(candidates)
    if target in pool:
        return target
    scored = sorted(((difflib.SequenceMatcher(None, target, c).ratio(), c) for c in pool), reverse=True)
    if not scored or scored[0][0] < cutoff:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None  # ambiguous
    return scored[0][1]
