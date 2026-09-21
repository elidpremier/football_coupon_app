"""Normalisation : équipes, compétitions, dates, marchés, identifiant stable.

Règle absolue : une correspondance d'équipes incertaine n'est JAMAIS
automatisée — elle produit un état MATCHING_REVIEW_REQUIRED.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .utils import strip_accents, to_utc

# Suffixes juridiques/sociétaires supprimés pour la comparaison.
# "Real" est conservé (partie du nom commun), les acronymes sont jetés.
TEAM_SUFFIXES = {
    "fc", "cf", "sc", "ac", "afc", "cfc", "ssc", "ss", "us", "as",
    "rc", "cd", "sd", "ud", "ca", "cp", "bk", "if", "f", "c",
}

# ---------------------------------------------------------------- équipes
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_SPACE_RE = re.compile(r"\s+")


def normalize_team_name(name: str) -> str:
    """Forme canonique d'un nom d'équipe : minuscules, sans accents,
    sans ponctuation, suffixes juridiques retirés si un reste de nom
    subsiste, espaces réduits."""
    if not name or not name.strip():
        raise ValueError("nom d'équipe vide")
    text = strip_accents(name).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    tokens = text.split(" ")
    while len(tokens) > 1 and tokens[-1] in TEAM_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True)
class TeamMatch:
    """Résultat de la correspondance de deux noms d'équipe."""

    status: str  # "matched" | "review_required" | "mismatch"
    ratio: float


# ≥ 0.92 : variantes quasi identiques (typo d'une lettre) — fusion OK.
# 0.78–0.92 : douteux → REVUE HUMaine obligatoire, jamais de fusion.
# < 0.78 : mismatch.
FUZZY_MATCH = 0.92
FUZZY_REVIEW = 0.78


def match_team_names(name_a: str, name_b: str) -> TeamMatch:
    a, b = normalize_team_name(name_a), normalize_team_name(name_b)
    if a == b:
        return TeamMatch("matched", 1.0)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= FUZZY_MATCH:
        return TeamMatch("matched", ratio)
    if ratio >= FUZZY_REVIEW:
        return TeamMatch("review_required", ratio)
    return TeamMatch("mismatch", ratio)


# ---------------------------------------------------------------- dates
def kickoff_agrees(a: datetime, b: datetime, tolerance_minutes: int = 5) -> bool:
    diff = abs((to_utc(a) - to_utc(b)).total_seconds())
    return diff <= tolerance_minutes * 60


def make_fixture_key(competition_slug: str, kickoff_utc: datetime,
                     home_team: str, away_team: str) -> str:
    """Identifiant interne stable, indépendant des identifiants fournisseur.

    competition_slug | kickoff_utc | home_normalisé | away_normalisé
    """
    k = to_utc(kickoff_utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{competition_slug}|{k}|{normalize_team_name(home_team)}|{normalize_team_name(away_team)}"


# ---------------------------------------------------------------- marchés
class Market(str, Enum):
    MATCH_WINNER = "match_winner"
    DOUBLE_CHANCE = "double_chance"
    OVER_UNDER_2_5 = "over_under_2_5"


MARKET_OUTCOMES: dict[str, tuple[str, ...]] = {
    "match_winner": ("home", "draw", "away"),
    "double_chance": ("home_draw", "home_away", "away_draw"),
    "over_under_2_5": ("over", "under"),
}


def normalize_market(raw: str) -> str:
    """Mappe les libellés fournisseur vers les marchés internes."""
    t = re.sub(r"[\s_/.]+", "", (raw or "").lower()).replace("-", "")
    aliases = {
        "1x2": "match_winner",
        "matchwinner": "match_winner",
        "winner": "match_winner",
        "doublechance": "double_chance",
        "dc": "double_chance",
        "ou25": "over_under_2_5",
        "overunder25": "over_under_2_5",
        "totalgoals25": "over_under_2_5",
    }
    if t in aliases:
        return aliases[t]
    raise ValueError(f"marché non autorisé : {raw!r}")


def normalize_outcome(market: str, raw: str) -> str:
    """Valide un issue d'un marché ; lève ValueError si inconnu."""
    if market not in MARKET_OUTCOMES:
        raise ValueError(f"marché inconnu : {market!r}")
    t = re.sub(r"[\s_/.]+", "", (raw or "").lower()).replace("-", "")
    aliases = {
        ("match_winner", "home"): "home",
        ("match_winner", "1"): "home",
        ("match_winner", "hometeam"): "home",
        ("match_winner", "draw"): "draw",
        ("match_winner", "x"): "draw",
        ("match_winner", "away"): "away",
        ("match_winner", "2"): "away",
        ("match_winner", "awayteam"): "away",
        ("double_chance", "homdraw"): "home_draw",
        ("double_chance", "1x"): "home_draw",
        ("double_chance", "homeaway"): "home_away",
        ("double_chance", "12"): "home_away",
        ("double_chance", "awaydraw"): "away_draw",
        ("double_chance", "x2"): "away_draw",
        ("over_under_2_5", "over"): "over",
        ("over_under_2_5", "over25"): "over",
        ("over_under_2_5", "under"): "under",
        ("over_under_2_5", "under25"): "under",
    }
    key = (market, t)
    if key not in aliases:
        raise ValueError(f"issue inconnue pour {market} : {raw!r}")
    return aliases[key]


def fixture_status_ok(status: str) -> bool:
    """Un match est analysable uniquement s'il n'est ni annulé ni commencé."""
    s = (status or "").upper()
    return s not in {"CANCELED", "CANCELLED", "POSTPONED", "SUSPENDED",
                     "FINISHED", "FT", "INPLAY", "INT", "AET", "PEN", "AFTER"}
