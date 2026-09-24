"""Tests du module de rotation des compétitions (football/rotation.py)."""
from __future__ import annotations

from datetime import date
import pytest

from football.config import load_config
from football.rotation import CompetitionRotator, CompetitionStatus, build_rotator, PRESTIGE_SCORES, ESTIMATED_COST


def test_rotation_disabled_returns_forced(tmp_path):
    # Charger la config par défaut et tester rotator sans storage
    cfg = load_config("config/football.yaml")
    rotator = build_rotator(cfg)
    active = rotator.get_active_competitions(date.today())
    # Par défaut, les compétitions forcées sont présentes
    for comp in cfg.competitions:
        assert comp in active


def test_rotation_priority_score():
    status = CompetitionStatus(
        slug="primeira_liga",
        last_covered_date=None,
        has_fixtures_today=True,
        estimated_api_cost=12,
        prestige_score=60,
        is_forced=False,
        is_in_pool=True,
    )
    score = status.priority_score(min_coverage_days=2)
    # 100 (fixtures today) + 12 (prestige) + 30 (never covered bonus) = 142.0
    assert score > 100.0


def test_coverage_plan():
    cfg = load_config("config/football.yaml")
    rotator = build_rotator(cfg)
    plan = rotator.get_coverage_plan(days=3)
    assert len(plan) == 3
    for day_plan in plan:
        assert "date" in day_plan
        assert "competitions" in day_plan
        assert day_plan["estimated_budget_used"] <= cfg.api_football_daily_budget


def test_catalogue_contains_all_competitions():
    cfg = load_config("config/football.yaml")
    rotator = build_rotator(cfg)
    catalogue = rotator.get_catalogue()
    assert len(catalogue) >= 20
    slugs = [c.slug for c in catalogue]
    assert "primeira_liga" in slugs
    assert "championship" in slugs
    assert "afcon" in slugs
