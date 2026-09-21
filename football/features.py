"""Construction des variables de match (features) et du bundle de données.

Tout ici est calculable à partir des seules données horodatées présentes
en base ; aucune variable « future » n'est utilisée.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from .config import AppConfig
from .probabilities import market_consistency_check
from .storage import Database
from .utils import to_decimal, to_utc, utcnow


def _utcnow() -> datetime:
    return utcnow()


@dataclass
class FeatureBundle:
    """Toutes les données connues pour un match à un instant T."""

    fixture_key: str
    competition_slug: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: str
    matching_status: str
    # {market: {outcome: {"odds": Decimal, "observed_at": datetime, "provider": str}}}
    # une seule cote retenue par issue : la plus récente.
    odds: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # {market: {outcome: [Decimal, ...]}} — TOUTES les observations
    # (toutes sources) : sert au calcul de stabilité de cote.
    odds_values: dict[str, dict[str, list]] = field(default_factory=dict)
    # {provider: {"kickoff": datetime, "id": str}}
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {team: {position, played, ...}}
    teams: dict[str, dict[str, Any]] = field(default_factory=dict)
    # [{"team","player","reason","status","source_ref"}]
    availability: list[dict[str, Any]] = field(default_factory=list)
    computed_at: datetime = field(default_factory=_utcnow)

    @property
    def team_names(self) -> set[str]:
        return {self.home_team, self.away_team}


def load_bundle(db: Database, fixture_key: str,
                computed_at: datetime | None = None) -> Optional[FeatureBundle]:
    """`computed_at` : « maintenant » de référence pour les calculs de
    fraîcheur (injectable : horloge réelle en production, horloge gelée
    dans les tests)."""
    row = db.get_fixture(fixture_key)
    if not row:
        return None
    from .utils import parse_iso

    kickoff = parse_iso(row["kickoff_utc"])
    odds: dict[str, dict[str, dict[str, Any]]] = {}
    odds_values: dict[str, dict[str, list]] = {}
    for o in db.all_odds(fixture_key):
        odds_values.setdefault(o["market"], {}).setdefault(
            o["outcome"], []).append(to_decimal(o["odds"]))
    for o in db.latest_odds(fixture_key):
        slot = odds.setdefault(o["market"], {})
        info = {
            "odds": to_decimal(o["odds"]),
            "observed_at": parse_iso(o["observed_at_utc"]),
            "provider": o["provider"],
            "bookmaker": o["bookmaker"],
        }
        prev = slot.get(o["outcome"])
        if prev is None or info["observed_at"] > prev["observed_at"]:
            slot[o["outcome"]] = info
    teams: dict[str, dict[str, Any]] = {}
    for t in db.team_snapshots(fixture_key):
        teams[t["team"]] = {
            "side": t["side"],
            "position": t["position"],
            "played": t["played"],
            "wins": t["wins"],
            "draws": t["draws"],
            "losses": t["losses"],
            "goals_for": t["goals_for"],
            "goals_against": t["goals_against"],
            "form": t["form"],
        }
    availability = [
        {
            "team": a["team"],
            "player": a["player"],
            "reason": a["reason"],
            "status": a["status"],
            "source_ref": a["source_ref"],
        }
        for a in db.availability(fixture_key)
    ]
    providers: dict[str, dict[str, Any]] = {}
    prow = row
    if prow["primary_provider"]:
        providers[prow["primary_provider"]] = {
            "kickoff": kickoff, "id": prow["primary_fixture_id"],
        }
    if prow["secondary_provider"]:
        providers[prow["secondary_provider"]] = {
            "kickoff": kickoff, "id": prow["secondary_fixture_id"],
        }
    return FeatureBundle(
        fixture_key=fixture_key,
        competition_slug=row["competition_slug"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        kickoff_utc=kickoff,
        status=row["status"],
        matching_status=row["matching_status"],
        odds=odds,
        odds_values=odds_values,
        providers=providers,
        teams=teams,
        availability=availability,
        computed_at=computed_at or _utcnow(),
    )


def market_odds_rows(bundle: FeatureBundle, market: str) -> dict[str, Decimal]:
    """Cotes {issue: cote} pour un marché (cote la plus récente par issue)."""
    return {
        outcome: info["odds"]
        for outcome, info in bundle.odds.get(market, {}).items()
    }


def newest_odds_age_hours(bundle: FeatureBundle, now: datetime) -> Optional[float]:
    """Âge (h) de la cote la plus récente de tous les marchés."""
    times = [
        info["observed_at"]
        for mkt in bundle.odds.values()
        for info in mkt.values()
    ]
    if not times:
        return None
    newest = max(times)
    return max(0.0, (to_utc(now) - newest).total_seconds() / 3600.0)


def odds_stability(bundle: FeatureBundle, market: str) -> Optional[float]:
    """Stabilité de cote : 1 − amplitude relative, par issue, sur TOUTES
    les observations (toutes sources confondues), puis moyenne.

    Comparer des cotes de DIFFÉRENTES issues n'a aucun sens (2.10 pour
    la victoire vs 3.40 pour le nul) : seule l'amplitude d'une même issue
    porte de l'information.

    Une seule observation par issue : 0.8 (ni confirmée ni démentie).
    """
    values_map = bundle.odds_values.get(market) or {
        outcome: [info["odds"]] for outcome, info in bundle.odds.get(market, {}).items()
    }
    ratios: list[float] = []
    for vals in values_map.values():
        vals = [v for v in vals if v is not None and v > 0]
        if len(vals) >= 2:
            lo, hi = min(vals), max(vals)
            if hi > 0:
                ratios.append(max(0.0, 1.0 - float((hi - lo) / hi)))
    if not ratios:
        if any(len(v) == 1 for v in values_map.values()):
            return 0.8
        return None
    return sum(ratios) / len(ratios)


def consistency_problems(bundle: FeatureBundle, config: AppConfig) -> list[str]:
    """Contradictions critiques : divergence d'horaire entre sources,
    marchés incomplets hors des seuils autorisés."""
    problems = []
    kickoffs = [p["kickoff"] for p in bundle.providers.values()]
    if len(kickoffs) >= 2:
        tol = config.quality.kickoff_tolerance_minutes
        for i in range(len(kickoffs)):
            for j in range(i + 1, len(kickoffs)):
                if abs((kickoffs[i] - kickoffs[j]).total_seconds()) > tol * 60:
                    problems.append(
                        f"horaire divergent entre sources ({tol} min de tolérance)"
                    )
    problems.extend(market_consistency_check(
        {m: {o: i["odds"] for o, i in outcomes.items()} for m, outcomes in bundle.odds.items()},
        config.markets,
    ))
    return problems
