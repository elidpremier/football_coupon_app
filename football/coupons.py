"""Constructeur de coupons sous contraintes.

Rigueur avant volume :
- uniquement des sélections `eligible` ;
- 1 sélection par match, jamais la même équipe dans un coupon ;
- jamais deux matchs de même compétition dont le coup d'envoi est trop
  proche (« contexte identique ») ;
- si les contraintes ne sont pas satisfaites → AUCUN coupon ;
- la probabilité combinée est un produit d'indépendance : approximation
  affichée comme telle, jamais une garantie.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Any, Sequence

from .config import AppConfig
from .normalize import make_fixture_key
from .selection import Selection
from .utils import geometric_mean, to_decimal, to_utc


@dataclass
class FixtureInfo:
    fixture_key: str
    home_team: str
    away_team: str
    competition_slug: str
    kickoff_utc: datetime

    @property
    def teams(self) -> set[str]:
        return {self.home_team, self.away_team}


@dataclass
class CouponCandidate:
    coupon_id: str
    selections: list[Selection]
    combined_odds: Decimal
    combined_probability: float
    score: float
    diversity_factor: float
    uncertainty_penalty: float
    constraints: list[str] = field(default_factory=list)
    rejected_combos: list[tuple[str, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coupon_id": self.coupon_id,
            "selections": [
                {
                    "fixture_key": s.fixture_key,
                    "market": s.market,
                    "outcome": s.outcome,
                    "odds": str(s.odds),
                    "odds_observed_at_utc": s.odds_observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "probability": s.probability,
                    "score": s.score,
                }
                for s in self.selections
            ],
            "combined_odds": str(self.combined_odds),
            "combined_probability": self.combined_probability,
            "score": self.score,
            "diversity_factor": self.diversity_factor,
            "uncertainty_penalty": self.uncertainty_penalty,
            "constraints": self.constraints,
        }


def _pair_rejection(a: Selection, b: Selection, fa: FixtureInfo, fb: FixtureInfo,
                    cfg: AppConfig) -> str | None:
    """Raison de rejet d'une paire, ou None si la paire est admise."""
    if cfg.coupons.prohibit_same_fixture and a.fixture_key == b.fixture_key:
        return "même match"
    if cfg.coupons.prohibit_same_team and fa.teams & fb.teams:
        return "équipe commune"
    if (fa.competition_slug == fb.competition_slug
            and abs((to_utc(fa.kickoff_utc) - to_utc(fb.kickoff_utc)).total_seconds())
            < cfg.coupons.same_context_minutes * 60):
        return f"même compétition, écart < {cfg.coupons.same_context_minutes} min"
    return None


def _diversity(a: Selection, b: Selection, fa: FixtureInfo, fb: FixtureInfo,
               cfg: AppConfig) -> float:
    factor = 1.0
    if fa.competition_slug == fb.competition_slug:
        factor *= 0.9
    dt = abs((to_utc(fa.kickoff_utc) - to_utc(fb.kickoff_utc)).total_seconds()) / 60.0
    if dt < 60:
        factor *= 0.85
    return factor


def _uncertainty_penalty(joint: float) -> float:
    """Pénalité d'incertitude : plus la probabilité conjointe est faible,
    plus le coupon est incertain → score réduit.

    paliers : ≥ 50 % → 1.0 ; 25–50 % → 0.75 ; < 25 % → 0.5 (plancher).
    """
    if joint >= 0.5:
        return 1.0
    if joint >= 0.25:
        return 0.75
    return 0.5


def build_candidates(selections: Sequence[Selection],
                     fixtures: dict[str, FixtureInfo],
                     cfg: AppConfig,
                     now: datetime | None = None,
                     rng_id: str | None = None) -> tuple[list[CouponCandidate], list[tuple[str, str, str]]]:
    """Génère les combinaisons admissibles (paires, puis triplets si la
    config l'autorise) et les classe. Retourne (candidates, rejets)."""
    eligible = [s for s in selections if s.status == "eligible"]
    rejections: list[tuple[str, str, str]] = []
    for s in selections:
        if s.status != "eligible":
            rejections.append((s.fixture_key, s.market, s.status))

    # une seule sélection par match : si deux issues du même match sont
    # éligibles, on conserve la meilleure et on documente le rejet
    by_fixture: dict[str, Selection] = {}
    for s in eligible:
        if s.fixture_key not in by_fixture:
            by_fixture[s.fixture_key] = s
        else:
            rejections.append((s.fixture_key, s.fixture_key, "même match"))
            if s.score > by_fixture[s.fixture_key].score:
                by_fixture[s.fixture_key] = s
    eligible = list(by_fixture.values())

    info = {s.fixture_key: fixtures[s.fixture_key] for s in eligible
            if s.fixture_key in fixtures}
    if len(info) < 2:
        return [], rejections

    max_n = cfg.max_selections()
    by_size: dict[int, list[CouponCandidate]] = {}

    for size in range(2, max_n + 1):
        by_size[size] = []
        for combo in combinations(sorted(eligible, key=lambda s: s.fixture_key), size):
            combo_keys = [s.fixture_key for s in combo]
            rejected_here = False
            for i in range(len(combo)):
                for j in range(i + 1, len(combo)):
                    reason = _pair_rejection(combo[i], combo[j],
                                             info[combo[i].fixture_key],
                                             info[combo[j].fixture_key], cfg)
                    if reason:
                        rejections.append((combo[i].fixture_key,
                                           combo[j].fixture_key, reason))
                        rejected_here = True
            if rejected_here:
                continue

            combined_odds = Decimal("1")
            joint = 1.0
            for s in combo:
                combined_odds *= to_decimal(s.odds)
                joint *= s.probability
            diversity = 1.0
            for i in range(len(combo)):
                for j in range(i + 1, len(combo)):
                    diversity *= _diversity(combo[i], combo[j],
                                            info[combo[i].fixture_key],
                                            info[combo[j].fixture_key], cfg)
            penalty = _uncertainty_penalty(joint)
            quality_mean = geometric_mean([s.score / 100.0 for s in combo])
            confidence_mean = geometric_mean([s.probability for s in combo])
            score = 100.0 * (quality_mean ** 0.5 * confidence_mean ** 0.5
                             * diversity * penalty)
            cid = rng_id or str(uuid.uuid4())
            by_size[size].append(CouponCandidate(
                coupon_id=cid,
                selections=list(combo),
                combined_odds=combined_odds,
                combined_probability=round(joint, 4),
                score=round(score, 2),
                diversity_factor=round(diversity, 4),
                uncertainty_penalty=round(penalty, 4),
                constraints=[
                    f"{len(combo)} sélections (max {max_n} en mode {cfg.mode})",
                    "une sélection par match",
                    "aucune équipe commune",
                    "aucun contexte identique (< "
                    f"{cfg.coupons.same_context_minutes} min même compétition)",
                    "probabilité conjointe = produit (approximation d'indépendance)",
                ],
            ))

    # Affichage : le meilleur coupon de CHAQUE taille autorisée est toujours
    # visible (l'opérateur compare pair/triplet), complété par les meilleurs
    # restants jusqu'au plafond d'affichage.
    display = cfg.coupons.max_candidates_displayed
    per_size = {sz: sorted(cs, key=lambda c: c.score, reverse=True)
                for sz, cs in by_size.items() if cs}
    final: list[CouponCandidate] = []
    for sz in sorted(per_size):
        final.append(per_size[sz][0])
    extras = [c for sz in sorted(per_size) for c in per_size[sz][1:]]
    extras.sort(key=lambda c: c.score, reverse=True)
    for c in extras:
        if len(final) >= display:
            break
        final.append(c)
    final.sort(key=lambda c: c.score, reverse=True)
    return final[:display], rejections
