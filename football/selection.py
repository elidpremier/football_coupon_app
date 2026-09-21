"""Sélection individuelle : règles déterministes + grille de score.

Une sélection n'est **candidate** que si toutes les conditions dures sont
remplies ; le score final est la grille visible du dossier (§10).
Aucune prose IA ne peut, à elle seule, rendre une sélection éligible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from .analysis.base import AiPart
from .config import AppConfig
from .features import FeatureBundle, odds_stability
from .normalize import MARKET_OUTCOMES
from .probabilities import (
    MARKET_MODEL_VERSION,
    implied_probabilities,
    market_model_probability,
)
from .quality import QualityResult
from .utils import to_decimal

MIN_PROB_STRICT = 0.0
MAX_PROB_STRICT = 1.0


@dataclass
class Selection:
    fixture_key: str
    market: str
    outcome: str
    odds: Decimal
    odds_observed_at: datetime
    probability: float
    model_version: str
    score: float
    status: str                      # eligible | watch | exclude | manual_exclude
    reasons: list[str] = field(default_factory=list)
    justification: str = ""
    source_refs: list[str] = field(default_factory=list)
    odds_stability: Optional[float] = None
    quality: Optional[float] = None


def _best_outcome(bundle: FeatureBundle, market: str,
                  min_prob: float) -> Optional[dict[str, Any]]:
    """Meilleure issue d'un marché : probabilité max ≥ seuil, cote valide."""
    rows = bundle.odds.get(market)
    if not rows:
        return None
    market_odds = {o: info["odds"] for o, info in rows.items()}
    try:
        probs, _ = implied_probabilities(market_odds)
    except Exception:
        return None
    best = max(probs.items(), key=lambda kv: kv[1])
    outcome, p = best
    if p < min_prob or not (MIN_PROB_STRICT < p < MAX_PROB_STRICT):
        return None
    info = rows[outcome]
    return {
        "outcome": outcome,
        "probability": p,
        "odds": info["odds"],
        "observed_at": info["observed_at"],
        "bookmaker": info.get("bookmaker", ""),
        "provider": info.get("provider", ""),
    }


def _prob_signal(p: float, min_prob: float) -> float:
    """Cartographie douce : min_prob → 0, 0.75 → 1 (plafonné)."""
    span = max(0.05, 0.75 - min_prob)
    return max(0.0, min(1.0, (p - min_prob) / span))


def _explanation_score(analysis_valid: bool, complete: bool,
                       cfg: AppConfig) -> float:
    if not complete:
        return 0.5
    if analysis_valid:
        return cfg.selection.explanation_score_ai_validated
    return cfg.selection.explanation_score_template


def evaluate_fixture(fixture_key: str,
                     bundle: FeatureBundle,
                     quality: QualityResult,
                     cfg: AppConfig,
                     analyst: Optional[AiPart] = None,
                     critic: Optional[AiPart] = None,
                     analysis_valid: bool = False,
                     now: datetime | None = None) -> Optional[Selection]:
    """Évalue un match : renvoie la meilleure sélection (0 ou 1 par match).

    `quality.exclusions` non vide → sélection exclue, raisons documentées.
    """
    from .analysis.validator import part_is_complete

    now = now or bundle.computed_at
    reasons: list[str] = list(quality.exclusions)
    complete = bool(
        analyst is not None and critic is not None
        and part_is_complete(analyst.to_dict()) and part_is_complete(critic.to_dict())
    )

    candidate = None
    if not reasons:
        for market in cfg.markets:
            cand = _best_outcome(bundle, market, cfg.quality.min_selection_probability)
            if cand is None:
                continue
            if candidate is None or cand["probability"] > candidate["probability"]:
                candidate = {**cand, "market": market}
        if candidate is None:
            reasons.append(
                "aucune issue d'un marché autorisé au-dessus du seuil de "
                f"probabilité ({cfg.quality.min_selection_probability})"
            )

    if candidate is None:
        return Selection(
            fixture_key=fixture_key,
            market=cfg.markets[0] if cfg.markets else "match_winner",
            outcome="",
            odds=Decimal("1.01"),
            odds_observed_at=now,
            probability=0.0,
            model_version=MARKET_MODEL_VERSION,
            score=0.0,
            status="exclude",
            reasons=reasons or ["match non analysable"],
            quality=quality.score,
        )

    market = candidate["market"]
    stability = odds_stability(bundle, market)
    if stability is None:
        stability = 0.0
        reasons.append("stabilité de cote non évaluable")
    if not complete:
        reasons.append("explication/risques incomplets (pas de bonus de confiance)")

    s = cfg.selection
    score = (
        s.weight_data_quality * (quality.score / 100.0)
        + s.weight_source_agreement * quality.source_agreement
        + s.weight_odds_stability * stability
        + s.weight_probability * _prob_signal(candidate["probability"],
                                              cfg.quality.min_selection_probability)
        + s.weight_explanation * _explanation_score(analysis_valid, complete, cfg)
    )
    score = round(score, 2)

    if score >= cfg.quality.eligible_threshold:
        status = "eligible"
    elif score >= cfg.quality.watch_threshold:
        status = "watch"
    else:
        status = "exclude"
        if not reasons:
            reasons.append(f"score {score} sous le seuil d'observation")

    just = (
        f"Marché {market}, issue {candidate['outcome']} : probabilité "
        f"{candidate['probability']:.1%} (référence marché, "
        f"{cfg.prob_model_version}), cote {candidate['odds']} "
        f"observée {candidate['observed_at'].strftime('%Y-%m-%d %H:%M')} UTC. "
        f"Qualité données {quality.score:.0f}/100."
    )
    return Selection(
        fixture_key=fixture_key,
        market=market,
        outcome=candidate["outcome"],
        odds=to_decimal(candidate["odds"]),
        odds_observed_at=candidate["observed_at"],
        probability=candidate["probability"],
        model_version=MARKET_MODEL_VERSION,
        score=score,
        status=status,
        reasons=reasons,
        justification=just,
        source_refs=sorted({info["provider"] for mkt in bundle.odds.values()
                            for info in mkt.values()}),
        odds_stability=stability,
        quality=quality.score,
    )
