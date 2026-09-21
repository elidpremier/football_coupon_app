"""Moteur de probabilités déterministe.

Étape 1 (obligatoire au départ) : référence de marché.
    p_brute = 1 / cote ; p_normalisée = p_brute / Σ p_brute
Aucune « value » n'est signalée quand le modèle est la référence de
marché : l'edge est nul par construction et simuler une value est
interdit (règle §8.1 du dossier).

Étape 2 (interdite avant validation chronologique) : Poisson simple,
fourni mais désactivé par défaut.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Iterable

from .normalize import MARKET_OUTCOMES
from .utils import clamp01, to_decimal

MARKET_MODEL_VERSION = "market_normalized_v1"
POISSON_MODEL_VERSION = "poisson_baseline_v0"


class ProbabilityError(ValueError):
    pass


def validate_odds(odds: dict[str, Decimal | float | str]) -> dict[str, Decimal]:
    """Toutes les cotes doivent être > 1 et finies (cote décimale)."""
    out: dict[str, Decimal] = {}
    for k, v in odds.items():
        d = to_decimal(v)
        if d <= Decimal("1") or d > Decimal("1000"):
            raise ProbabilityError(f"cote invalide {k}={d} (doit être dans ]1, 1000])")
        out[k] = d
    if not out:
        raise ProbabilityError("aucune cote fournie")
    return out


def implied_probabilities(odds: dict[str, Decimal]) -> tuple[dict[str, float], float]:
    """(probabilités normalisées, surcote totale S = Σ 1/o)."""
    validated = validate_odds(odds)
    raw = {k: (Decimal(1) / v) for k, v in validated.items()}
    total = sum(raw.values())
    probs = {k: float(p / total) for k, p in raw.items()}
    return probs, float(total)


def overround(odds: dict[str, Decimal]) -> float:
    """Marge du marché (ex. 0.0481 = ~4,8 %)."""
    _, s = implied_probabilities(odds)
    return s - 1.0


def market_model_probability(outcome: str, market_odds: dict[str, Decimal]) -> float:
    """Probabilité du modèle de marché pour une issue donnée."""
    probs, _ = implied_probabilities(market_odds)
    if outcome not in probs:
        raise ProbabilityError(f"issue {outcome!r} absente du marché")
    p = probs[outcome]
    if not (0.0 < p < 1.0):
        raise ProbabilityError(f"probabilité hors bornes strictes : {p}")
    return p


def derive_double_chance(odds_1x2: dict[str, Decimal | float | str],
                         spread: float = 0.98) -> dict[str, Decimal]:
    """Cotes double-chance dérivées de la 1X2 (référence marché).

    P(1X) = p1 + px, etc. sur les probabilités normalisées ; `spread`
    (< 1) ajoute la marge du marché. Résultat arrondi à 2 décimales,
    toujours > 1 (sinon la cote dérivée est incohérente → levée d'erreur).
    """
    probs, _ = implied_probabilities(odds_1x2)
    pairs = {
        "home_draw": probs.get("home", 0.0) + probs.get("draw", 0.0),
        "home_away": probs.get("home", 0.0) + probs.get("away", 0.0),
        "away_draw": probs.get("away", 0.0) + probs.get("draw", 0.0),
    }
    out: dict[str, Decimal] = {}
    for k, p in pairs.items():
        if not (0.0 < p < 1.0):
            raise ProbabilityError(f"probabilité {k!r} incohérente : {p}")
        odds = round(float(spread) / p, 2)
        if odds <= 1.0:
            raise ProbabilityError(f"cote double-chance {k!r} ≤ 1 : {odds}")
        out[k] = to_decimal(odds)
    return out


def edge(probability: float, odds: Decimal | float | str) -> float:
    """edge = p × cote − 1."""
    return float(to_decimal(probability) * to_decimal(odds) - 1)


def is_value_bet(model_version: str, probability: float, odds: Decimal | float | str,
                 threshold: float) -> bool:
    """La value n'est autorisée qu'avec un modèle validé (jamais en marché)."""
    if model_version == MARKET_MODEL_VERSION:
        return False  # edge nul par construction : interdit de simuler
    return edge(probability, odds) >= threshold


# ---------------------------------------------------------------- Poisson
def poisson_pmf(lam: float, k: int) -> float:
    if lam < 0:
        raise ProbabilityError("intensité de Poisson négative")
    return math.exp(-lam) * lam ** k / math.factorial(k)


def score_matrix(lam_home: float, lam_away: float, max_goals: int = 10) -> list[list[float]]:
    if lam_home <= 0 or lam_away <= 0:
        raise ProbabilityError("intensités strictement positives requises")
    row_h = [poisson_pmf(lam_home, i) for i in range(max_goals + 1)]
    row_a = [poisson_pmf(lam_away, j) for j in range(max_goals + 1)]
    return [[row_h[i] * row_a[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]


def normalize_matrix(matrix: list[list[float]]) -> list[list[float]]:
    total = sum(sum(r) for r in matrix)
    if total <= 0:
        raise ProbabilityError("matrice de score nulle")
    return [[v / total for v in r] for r in matrix]


def probabilities_1x2(matrix: list[list[float]]) -> dict[str, float]:
    m = normalize_matrix(matrix)
    home = sum(m[i][j] for i in range(len(m)) for j in range(i))
    draw = sum(m[i][i] for i in range(len(m)))
    away = sum(m[i][j] for i in range(len(m)) for j in range(i + 1, len(m)))
    return {"home": clamp01(home), "draw": clamp01(draw), "away": clamp01(away)}


def over_under(matrix: list[list[float]], line: float = 2.5) -> dict[str, float]:
    m = normalize_matrix(matrix)
    over = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j > line)
    under = 1.0 - over
    return {"over": clamp01(over), "under": clamp01(under)}


def poisson_model(attack_home: float, defense_home: float,
                  attack_away: float, defense_away: float,
                  league_avg_home: float = 1.5, league_avg_away: float = 1.1,
                  home_advantage: float = 1.15, max_goals: int = 10
                  ) -> dict:
    """Poisson interprétable : intensités construites depuis des forces
    connues avant le coup d'envoi. Retourne probabilités 1X2 + total 2.5."""
    if any(v < 0 for v in (attack_home, defense_home, attack_away, defense_away)):
        raise ProbabilityError("forces d'équipe négatives")
    lam_home = league_avg_home * attack_home / max(0.05, defense_away) * home_advantage
    lam_away = league_avg_away * attack_away / max(0.05, defense_home)
    m = score_matrix(lam_home, lam_away, max_goals)
    return {
        "model_version": POISSON_MODEL_VERSION,
        "lam_home": round(lam_home, 4),
        "lam_away": round(lam_away, 4),
        "match_winner": probabilities_1x2(m),
        "over_under_2_5": over_under(m, 2.5),
    }


def market_consistency_check(market_odds_by_market: dict[str, dict[str, Decimal]],
                             configured: Iterable[str]) -> list[str]:
    """Contrôle : chaque marché configuré est présent avec toutes ses issues."""
    problems = []
    for market in configured:
        if market not in MARKET_OUTCOMES:
            problems.append(f"marché inconnu : {market}")
            continue
        odds = market_odds_by_market.get(market)
        if not odds:
            problems.append(f"marché absent : {market}")
            continue
        missing = [o for o in MARKET_OUTCOMES[market] if o not in odds]
        if missing:
            problems.append(f"{market} : issues manquantes {missing}")
    return problems
