"""Score de qualité des données + règles d'exclusion dures.

Calculé hors IA (règle §7.3). Toute exclusion dure est documentée avec
une raison lisible ; un match contradictoire (horaire) est exclu
d'office, jamais retenu.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import AppConfig
from .features import FeatureBundle, consistency_problems, newest_odds_age_hours
from .utils import clamp, clamp01


@dataclass
class QualityResult:
    score: float                      # 0..100
    freshness: float                  # 0..1
    source_agreement: float           # 0..1
    odds_coverage: float              # 0..1
    team_data_coverage: float         # 0..1
    availability_confidence: float    # 0..1
    contradictions: int
    exclusions: list[str]             # raisons dures (si non vide → exclu)
    notes: list[str]


def _freshness(bundle: FeatureBundle, cfg: AppConfig, now: datetime) -> tuple[float, list[str]]:
    age = newest_odds_age_hours(bundle, now)
    if age is None:
        return 0.0, ["aucune cote observée"]
    if age > cfg.quality.max_odds_age_hours:
        return 0.0, [
            f"cote la plus récente vieille de {age:.1f} h "
            f"(max {cfg.quality.max_odds_age_hours} h)"
        ]
    return clamp01(1.0 - age / cfg.quality.max_odds_age_hours), []


def _agreement(bundle: FeatureBundle, cfg: AppConfig,
               contradictions: list[str]) -> float:
    n_sources = len(bundle.providers)
    has_kickoff_conflict = any("horaire divergent" in c for c in contradictions)
    if has_kickoff_conflict:
        return 0.0
    if n_sources >= 2:
        return 1.0
    if n_sources == 1:
        return 0.6  # contrôle secondaire indisponible : confiance réduite
    return 0.0


def _odds_coverage(bundle: FeatureBundle, cfg: AppConfig) -> float:
    if not cfg.markets:
        return 0.0
    covered = 0
    for market in cfg.markets:
        outcomes = bundle.odds.get(market, {})
        if outcomes:
            covered += 1
    return covered / len(cfg.markets)


def _team_coverage(bundle: FeatureBundle) -> float:
    """Fraction des deux équipes pour lesquelles des données existent."""
    names = bundle.team_names
    if not names:
        return 0.0
    present = sum(1 for t in names if t in bundle.teams)
    return present / len(names)


def _availability(bundle: FeatureBundle) -> tuple[float, list[str]]:
    notes = []
    if not bundle.availability:
        return 1.0, notes
    unconfirmed = [a for a in bundle.availability if a["status"] == "unconfirmed"]
    if unconfirmed:
        names = ", ".join(sorted({a["player"] for a in unconfirmed}))
        notes.append(f"absence(s) non confirmée(s) : {names} — risque mentionné, "
                     "aucune correction quantitative arbitraire")
        return 0.75, notes
    confirmed = ", ".join(sorted({a["player"] for a in bundle.availability}))
    notes.append(f"absence(s) confirmée(s) : {confirmed}")
    return 1.0, notes


def hard_exclusions(bundle: FeatureBundle, cfg: AppConfig,
                    now: datetime) -> list[str]:
    """Règles dures : si une seule est violée, le match est exclu."""
    exclusions: list[str] = []
    if bundle.matching_status != "OK":
        exclusions.append(f"correspondance équipe non confirmée ({bundle.matching_status})")
    contradictions = consistency_problems(bundle, cfg)
    if any("horaire divergent" in c for c in contradictions):
        exclusions.append("horaire divergent entre sources (INCOHERENT)")
    age_excl = [e for e in _freshness(bundle, cfg, now)[1]]
    if age_excl:
        exclusions.extend(age_excl)
    if not bundle.odds:
        exclusions.append("aucune cote sur aucun marché")
    return exclusions


def compute_quality(bundle: FeatureBundle, cfg: AppConfig,
                    now: datetime) -> QualityResult:
    contradictions = consistency_problems(bundle, cfg)
    exclusions = hard_exclusions(bundle, cfg, now)

    freshness, age_notes = _freshness(bundle, cfg, now)
    agreement = _agreement(bundle, cfg, contradictions)
    odds_cov = _odds_coverage(bundle, cfg)
    team_cov = _team_coverage(bundle)
    avail, avail_notes = _availability(bundle)

    w = cfg.quality.weights
    base = (
        w["freshness"] * freshness
        + w["source_agreement"] * agreement
        + w["odds_coverage"] * odds_cov
        + w["team_data_coverage"] * team_cov
        + w["availability_confidence"] * avail
    )
    score = 100.0 * base
    score -= cfg.quality.contradiction_penalty * (
        len([c for c in contradictions if "horaire" not in c])
    )
    score = clamp(score, 0.0, 100.0)

    notes = list(age_notes) + list(avail_notes)
    if exclusions:
        score = 0.0
    return QualityResult(
        score=score,
        freshness=freshness,
        source_agreement=agreement,
        odds_coverage=odds_cov,
        team_data_coverage=team_cov,
        availability_confidence=avail,
        contradictions=len(contradictions),
        exclusions=exclusions,
        notes=notes,
    )
