"""Sélection quotidienne intelligente des compétitions — système de rotation.

Ce module décide chaque jour quelles compétitions du pool de rotation sont
analysées, en tenant compte de :
  1. Matchs imminents (priorité maximale)
  2. Ancienneté depuis la dernière couverture (fairness)
  3. Prestige de la compétition (Champions League, etc.)
  4. Budget API disponible (protection du quota journalier)

Il est appelé par le pipeline principal avant le run d'analyse et expose
également les données nécessaires à l'affichage dans l'UI Streamlit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig

log = logging.getLogger(__name__)

# Score de prestige par compétition (plus haut = plus prioritaire à égalité)
PRESTIGE_SCORES: dict[str, int] = {
    "champions_league": 100,
    "europe_league": 85,
    "conference_league": 70,
    "premier_league": 90,
    "la_liga": 88,
    "serie_a": 82,
    "bundesliga": 80,
    "ligue_1": 75,
    "eredivisie": 65,
    "primeira_liga": 60,
    "championship": 55,
    "brasileirao": 50,
    "mls": 45,
    "ligue_2": 40,
    "serie_b": 38,
    "segunda_division": 38,
    "afcon": 70,
    "can_qualif": 55,
    "coppa_italia": 60,
    "premier_league_cup": 50,
}

# Coût budgétaire estimé par compétition en haute saison (nb requêtes API)
# Coût budgétaire moyen estimé par compétition en jour d'analyse (nb requêtes API)
# fixtures(1) + odds(1 à 3 par match en moyenne) + injuries(1 par match)
# Avec le cache et la recherche ciblée, le coût moyen par compétition est de 4 à 8 requêtes.
ESTIMATED_COST: dict[str, int] = {
    "premier_league": 8,
    "la_liga": 8,
    "serie_a": 8,
    "bundesliga": 7,
    "ligue_1": 7,
    "eredivisie": 6,
    "champions_league": 6,
    "europe_league": 6,
    "conference_league": 5,
    "primeira_liga": 6,
    "championship": 8,
    "brasileirao": 6,
    "mls": 6,
    "ligue_2": 6,
    "serie_b": 6,
    "segunda_division": 6,
    "coppa_italia": 4,
    "premier_league_cup": 4,
    "afcon": 5,
    "can_qualif": 4,
}


@dataclass
class CompetitionStatus:
    """État d'une compétition pour la prise de décision quotidienne."""
    slug: str
    last_covered_date: date | None        # dernier jour où elle a été analysée
    has_fixtures_today: bool              # matchs détectés aujourd'hui
    estimated_api_cost: int               # requêtes API estimées
    prestige_score: int                   # score de prestige (0-100)
    is_forced: bool                       # dans competitions[] (toujours incluse)
    is_in_pool: bool                      # dans rotation.pool
    days_since_covered: int = field(init=False)

    def __post_init__(self) -> None:
        today = date.today()
        if self.last_covered_date:
            self.days_since_covered = (today - self.last_covered_date).days
        else:
            self.days_since_covered = 999  # jamais couverte = priorité haute

    def priority_score(self, min_coverage_days: int, target_day: date | None = None) -> float:
        """Score composite déterminant la priorité d'inclusion dans la rotation.

        Formule :
          - matchs aujourd'hui : +100 pts
          - affinité jour de semaine (UEFA mardi-jeudi, ligues vendredi-lundi) : +30-40 pts
          - prestige : 0-20 pts
          - ancienneté : 0-30 pts
          - variation déterministe par jour : 0-15 pts
        """
        if target_day is None:
            target_day = date.today()
        weekday = target_day.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

        score = 0.0
        if self.has_fixtures_today:
            score += 100.0

        # Affinité par jour de la semaine
        midweek_comps = {"champions_league", "europe_league", "conference_league", "coppa_italia", "premier_league_cup"}
        if self.slug in midweek_comps:
            if weekday in (1, 2, 3):  # Tue, Wed, Thu
                score += 40.0
        else:
            if weekday in (4, 5, 6, 0):  # Fri, Sat, Sun, Mon
                score += 30.0

        # Prestige : 0 → 20 pts
        score += (self.prestige_score / 100.0) * 20.0

        # Ancienneté : bonus progressif après le seuil minimal
        if self.days_since_covered >= min_coverage_days:
            bonus = min(30.0, (self.days_since_covered - min_coverage_days + 1) * 5.0)
            score += bonus

        # Variabilité déterministe quotidienne (rotation équitable du pool)
        day_hash = (target_day.toordinal() * 17 + abs(hash(self.slug))) % 15
        score += day_hash

        return score


from .config import RotationConfig

DEFAULT_ROTATION = RotationConfig(
    enabled=False,
    pool=(),
    max_active=8,
    min_coverage_days=2,
    budget_safety_pct=80.0,
)


def _get_rotation(cfg: "AppConfig") -> RotationConfig:
    return getattr(cfg, "rotation", DEFAULT_ROTATION)


class CompetitionRotator:
    """Sélectionne chaque jour les compétitions à analyser.

    Usage typique (dans le pipeline) :
        rotator = CompetitionRotator(config, db_storage)
        active = rotator.get_active_competitions(date.today())
        # active = compétitions forcées + meilleures du pool de rotation
    """

    def __init__(self, config: "AppConfig", storage=None) -> None:
        self._config = config
        self._storage = storage  # instance de football.storage.Storage (optionnel)

    # ------------------------------------------------------------------
    # API principale
    # ------------------------------------------------------------------

    def get_active_competitions(self, day: date | None = None) -> list[str]:
        """Retourne la liste des compétitions à analyser pour `day`.

        Les compétitions forcées (competitions[]) sont toujours présentes.
        Le pool de rotation est trié par priorité et complète jusqu'à
        `rotation.max_active` ou jusqu'à saturation du budget estimé.
        """
        if day is None:
            day = date.today()

        cfg = self._config
        forced = list(cfg.competitions)
        rot = _get_rotation(cfg)

        if not rot.enabled or not rot.pool:
            log.info("Rotation désactivée — compétitions forcées uniquement : %s", forced)
            return forced

        # Budget disponible (total journalier - coût des compétitions forcées)
        budget_total = int(cfg.api_football_daily_budget * rot.budget_safety_pct / 100)
        budget_used = sum(ESTIMATED_COST.get(c, 10) for c in forced)
        budget_remaining = max(0, budget_total - budget_used)

        # Construire les statuts du pool
        statuses = [
            self._build_status(slug, day)
            for slug in rot.pool
        ]

        # Trier par priorité décroissante
        statuses.sort(key=lambda s: s.priority_score(rot.min_coverage_days, day),
                      reverse=True)

        selected_from_pool: list[str] = []
        max_pool = rot.max_active - len(forced)

        for status in statuses:
            if len(selected_from_pool) >= max_pool:
                break
            cost = status.estimated_api_cost
            if cost > budget_remaining:
                log.info(
                    "Rotation : %s ignorée (coût estimé %d req > budget restant %d)",
                    status.slug, cost, budget_remaining,
                )
                continue
            selected_from_pool.append(status.slug)
            budget_remaining -= cost
            log.info(
                "Rotation : %s incluse (priorité=%.1f, coût=%d req, budget restant=%d)",
                status.slug,
                status.priority_score(rot.min_coverage_days, day),
                cost,
                budget_remaining,
            )

        active = forced + selected_from_pool
        log.info("Compétitions actives du jour (%s) : %s", day, active)
        return active

    def get_coverage_plan(self, days: int = 7) -> list[dict]:
        """Retourne un plan de couverture prévisionnel sur `days` jours.

        Utilisé par l'UI Streamlit pour afficher le planning hebdomadaire.
        Retourne une liste de dicts :
          [{date, competitions, estimated_budget_used}, ...]
        """
        today = date.today()
        plan = []
        for i in range(days):
            day = today + timedelta(days=i)
            active = self.get_active_competitions(day)
            total_cost = sum(ESTIMATED_COST.get(c, 10) for c in active)
            plan.append({
                "date": day,
                "competitions": active,
                "estimated_budget_used": total_cost,
                "budget_total": self._config.api_football_daily_budget,
                "budget_pct": round(total_cost / self._config.api_football_daily_budget * 100, 1),
            })
        return plan

    def get_catalogue(self) -> list[CompetitionStatus]:
        """Retourne l'état de toutes les compétitions connues (UI catalogue)."""
        from .config import KNOWN_COMPETITIONS
        today = date.today()
        result = []
        forced_set = set(self._config.competitions)
        rot = _get_rotation(self._config)
        pool_set = set(rot.pool)
        for slug in sorted(KNOWN_COMPETITIONS):
            status = self._build_status(slug, today)
            status_with_flags = CompetitionStatus(
                slug=slug,
                last_covered_date=status.last_covered_date,
                has_fixtures_today=status.has_fixtures_today,
                estimated_api_cost=status.estimated_api_cost,
                prestige_score=status.prestige_score,
                is_forced=(slug in forced_set),
                is_in_pool=(slug in pool_set),
            )
            result.append(status_with_flags)
        return result

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def _build_status(self, slug: str, day: date) -> CompetitionStatus:
        """Construit un CompetitionStatus pour un slug et une date donnés."""
        last_covered = self._get_last_covered(slug)
        has_fixtures = self._has_fixtures_today(slug, day)
        rot = _get_rotation(self._config)
        return CompetitionStatus(
            slug=slug,
            last_covered_date=last_covered,
            has_fixtures_today=has_fixtures,
            estimated_api_cost=ESTIMATED_COST.get(slug, 10),
            prestige_score=PRESTIGE_SCORES.get(slug, 50),
            is_forced=(slug in self._config.competitions),
            is_in_pool=(slug in rot.pool),
        )

    def _get_last_covered(self, slug: str) -> date | None:
        """Récupère la dernière date de couverture d'une compétition via le storage."""
        if self._storage is None:
            return None
        try:
            # Interroge la DB pour trouver la date du dernier fixture stocké
            row = self._storage.last_covered_date(slug)
            return row
        except Exception:
            return None

    def _has_fixtures_today(self, slug: str, day: date) -> bool:
        """Vérifie si des matchs sont stockés/prévisibles pour ce jour.

        En l'absence de storage, retourne False (la compétition sera
        sélectionnée par ancienneté si elle est dans le pool).
        """
        if self._storage is None:
            return False
        try:
            count = self._storage.count_fixtures_for_day(slug, day)
            return count > 0
        except Exception:
            return False


def build_rotator(config: "AppConfig", storage=None) -> CompetitionRotator:
    """Fabrique un CompetitionRotator configuré. Point d'entrée public."""
    return CompetitionRotator(config, storage)
