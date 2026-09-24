"""Interface commune des fournisseurs et données brutes normalisées
avant entrée en base.

Les adaptateurs ne font AUCUN pronostic : ils téléchargent, authentifient
et limitent les requêtes (règle §5.1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from ..config import AppConfig, Secrets


class ProviderError(Exception):
    """Échec d'un fournisseur (réseau, quota, format). Journalisé,
    jamais fatal pour l'autre fournisseur."""


@dataclass(frozen=True)
class PFixture:
    provider: str
    provider_fixture_id: str
    competition_slug: str
    competition_name: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: str
    venue: str = ""


@dataclass(frozen=True)
class POdds:
    provider: str
    provider_fixture_id: str
    market: str          # marché interne (normalisé)
    outcome: str         # issue interne (normalisée)
    odds: Decimal
    bookmaker: str = ""
    observed_at_utc: datetime = field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc))


@dataclass(frozen=True)
class PStanding:
    provider: str
    competition_slug: str
    team: str
    position: int
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    form: str = ""


@dataclass(frozen=True)
class PResult:
    provider: str
    provider_fixture_id: str
    kickoff_utc: datetime
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    status: str = "FT"


@dataclass(frozen=True)
class PAvailability:
    provider: str
    provider_fixture_id: str
    team: str
    player: str
    reason: str          # injury | suspension | doubt
    status: str          # confirmed | unconfirmed
    source_ref: str = ""


class BaseProvider:
    name = "base"

    def fetch_fixtures(self, day: date, competition_slug: str) -> list[PFixture]:
        raise NotImplementedError

    def fetch_odds(self, day: date, competition_slug: str) -> list[POdds]:
        return []

    def fetch_standings(self, competition_slug: str, season: int) -> list[PStanding]:
        return []

    def fetch_results(self, day: date, competition_slug: str) -> list[PResult]:
        return []

    def fetch_availability(self, day: date, competition_slug: str) -> list[PAvailability]:
        return []


def competition_to_provider_code(provider: str, competition_slug: str) -> str:
    """Cartographie compétition interne → code fournisseur.

    API-Football donne accès à 1 200+ compétitions sur l'offre gratuite.
    football-data.org couvre 12 compétitions sur l'offre gratuite.
    Les compétitions marquées *(api_football only)* ne sont pas disponibles
    via football-data.org (contrôle croisé impossible : qualité réduite).
    """
    table = {
        "api_football": {
            # === Ligues européennes majeures ===
            "premier_league": 39,
            "la_liga": 140,
            "serie_a": 135,
            "bundesliga": 132,
            "ligue_1": 61,
            "eredivisie": 107,
            "primeira_liga": 94,       # Portugal
            "championship": 40,        # Angleterre D2
            # === Coupes européennes ===
            "champions_league": 2,
            "europe_league": 14,
            "conference_league": 848,  # UEFA Conference League
            # === Coupes nationales ===
            "coppa_italia": 79,
            "premier_league_cup": 131,
            # === Divisions 2 européennes ===
            "ligue_2": 62,             # France D2
            "serie_b": 136,            # Italie D2
            "segunda_division": 141,   # Espagne D2
            # === Amériques ===
            "brasileirao": 71,         # Brésil Série A
            "mls": 253,                # Major League Soccer (USA/Canada)
            # === Afrique ===
            "afcon": 6,                # Coupe d'Afrique des Nations
            "can_qualif": 30,          # CAN Qualifications
        },
        "football_data": {
            # 12 compétitions disponibles sur l'offre gratuite
            "premier_league": "PL",
            "la_liga": "PD",
            "serie_a": "SA",
            "bundesliga": "BL1",
            "ligue_1": "FL1",
            "eredivisie": "DED",
            "primeira_liga": "PPL",    # Portugal — nouvellement mappé
            "championship": "ELC",    # Angleterre D2 — nouvellement mappé
            "champions_league": "CL",
            "europe_league": "EL",
            "brasileirao": "BSA",     # Brésil — nouvellement mappé
            # conference_league, ligue_2, serie_b, segunda_division,
            # mls, afcon, can_qualif : non couverts par football-data.org
        },
    }
    try:
        return table[provider][competition_slug]
    except KeyError as exc:
        raise ProviderError(
            f"compétition {competition_slug!r} non couverte par {provider}"
        ) from exc


def build_providers(config: AppConfig, secrets: Secrets, db,
                    transport=None) -> dict[str, BaseProvider]:
    """Fabrique les fournisseurs selon les clés disponibles.

    - sans clé API-Football : MODE DÉMO — deux variantes démo
      déterministes (l'application reste utilisable, étiquetée DÉMO) ;
    - football-data sans token : absent (le contrôle secondaire manque,
      ce qui est visible dans la qualité).
    """
    from ..http import Cache, RateLimiter, RequestsTransport
    from .api_football import ApiFootballProvider
    from .demo import DemoProvider
    from .football_data import FootballDataOrgProvider

    out: dict[str, BaseProvider] = {}
    real = config.primary_provider in {"api_football", "football_data"}
    if not real or not secrets.has_api_football:
        out["demo_primary"] = DemoProvider("demo_primary")
        out["demo_secondary"] = DemoProvider("demo_secondary")
        return out

    if transport is None:
        transport = RequestsTransport(max_retries=config.max_retries)

    wanted = {config.primary_provider, config.secondary_provider}
    if "api_football" in wanted:
        client = _make_client("api_football", db, transport,
                              config.api_football_daily_budget,
                              config.api_football_per_minute,
                              config.http_timeout_seconds, ttl=900)
        out["api_football"] = ApiFootballProvider(client, secrets.api_football_key)
    if "football_data" in wanted and secrets.has_football_data:
        client = _make_client("football_data", db, transport,
                              config.football_data_daily_budget,
                              config.football_data_per_minute,
                              config.http_timeout_seconds, ttl=600)
        out["football_data"] = FootballDataOrgProvider(client, secrets.football_data_token)
    if not out:
        out["demo_primary"] = DemoProvider("demo_primary")
        out["demo_secondary"] = DemoProvider("demo_secondary")
    return out


def _make_client(name: str, db, transport, budget: int, per_minute,
                 timeout: float, ttl: int):
    from ..http import ApiClient, Cache, RateLimiter

    return ApiClient(
        provider=name,
        transport=transport,
        cache=Cache(db),
        rate_limiter=RateLimiter(db),
        daily_budget=budget,
        per_minute=per_minute,
        timeout=timeout,
        ttl_seconds=ttl,
    )


__all__ = [
    "BaseProvider", "PAvailability", "PFixture", "POdds", "PResult", "PStanding",
    "ProviderError", "competition_to_provider_code", "build_providers",
]
