"""Tests : constructeur de coupons — contraintes, corrélations, scores."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from football.coupons import FixtureInfo, Selection, build_candidates
from football.utils import to_decimal

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
K = lambda h, m: datetime(2026, 9, 20, h, m, tzinfo=timezone.utc)  # noqa: E731


def sel(fixture_key: str, market: str, outcome: str, odds: str, p: float,
        status: str = "eligible", score: float = 85.0) -> Selection:
    return Selection(
        fixture_key=fixture_key, market=market, outcome=outcome,
        odds=to_decimal(odds), odds_observed_at=NOW - timedelta(hours=1),
        probability=p, model_version="market_normalized_v1", score=score,
        status=status, reasons=[], justification="t",
    )


def fixtures(**specs) -> dict[str, FixtureInfo]:
    out = {}
    for key, (home, away, comp, kick) in specs.items():
        out[key] = FixtureInfo(fixture_key=key, home_team=home, away_team=away,
                               competition_slug=comp, kickoff_utc=kick)
    return out


class TestBasic:
    def test_two_eligible_make_one_coupon(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k2", "match_winner", "home", "2.10", 0.48)]
        f = fixtures(k1=("Arsenal", "Chelsea", "premier_league", K(18, 0)),
                     k2=("Everton", "Fulham", "premier_league", K(20, 30)))
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert len(cands) == 1
        c = cands[0]
        assert len(c.selections) == 2
        assert c.combined_odds == to_decimal("4.20")
        assert c.combined_probability == pytest.approx(0.50 * 0.48, abs=1e-4)

    def test_combined_probability_is_product_of_independence(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.70),
             sel("k2", "match_winner", "home", "2.20", 0.65)]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)),
                     k2=("C", "D", "premier_league", K(21, 0)))
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert cands[0].combined_probability == pytest.approx(0.70 * 0.65, abs=1e-4)

    def test_no_coupon_with_one_eligible(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50)]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)))
        cands, rejs = build_candidates(s, f, config, now=NOW)
        assert cands == []
        assert rejs == []

    def test_no_coupon_with_zero_eligible(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50, status="watch"),
             sel("k2", "match_winner", "home", "2.10", 0.48, status="watch")]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)),
                     k2=("C", "D", "premier_league", K(21, 0)))
        cands, rejs = build_candidates(s, f, config, now=NOW)
        assert cands == []
        assert len(rejs) == 2  # documentées comme non éligibles

    def test_watch_never_in_coupon_even_if_mixed(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50, status="eligible"),
             sel("k2", "match_winner", "home", "2.10", 0.48, status="watch"),
             sel("k3", "match_winner", "home", "2.20", 0.47, status="watch")]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)),
                     k2=("C", "D", "premier_league", K(21, 0)),
                     k3=("E", "F", "premier_league", K(23, 0)))
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert cands == []  # une seule eligible → pas de coupon


class TestConstraints:
    def test_same_fixture_rejected(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k1", "over_under_2_5", "over", "1.90", 0.52)]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)))
        cands, rejs = build_candidates(s, f, config, now=NOW)
        assert cands == []
        assert any(r[2] == "même match" for r in rejs)

    def test_same_team_rejected(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k2", "match_winner", "away", "2.10", 0.48)]
        f = fixtures(k1=("Arsenal", "Chelsea", "premier_league", K(18, 0)),
                     k2=("Chelsea", "Arsenal", "premier_league", K(21, 0)))
        cands, rejs = build_candidates(s, f, config, now=NOW)
        assert cands == []
        assert any(r[2] == "équipe commune" for r in rejs)

    def test_same_context_close_kickoff_rejected(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k2", "match_winner", "home", "2.10", 0.48)]
        f = fixtures(k1=("A", "B", "premier_league", K(20, 0)),
                     k2=("C", "D", "premier_league", K(20, 30)))
        cands, rejs = build_candidates(s, f, config, now=NOW)
        assert cands == []
        assert any("même compétition" in r[2] for r in rejs)

    def test_different_competitions_allowed_close_kickoff(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k2", "match_winner", "home", "2.10", 0.48)]
        f = fixtures(k1=("A", "B", "premier_league", K(20, 0)),
                     k2=("C", "D", "ligue_1", K(20, 30)))
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert len(cands) == 1

    def test_same_competition_far_apart_allowed(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k2", "match_winner", "home", "2.10", 0.48)]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)),
                     k2=("C", "D", "premier_league", K(21, 0)))
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert len(cands) == 1


class TestScoringAndLimits:
    def test_max_three_candidates(self, config):
        s = [sel(f"k{i}", "match_winner", "home", "2.00", 0.50 + 0.001 * i,
                 score=80 + i) for i in range(6)]
        f = {f"k{i}": FixtureInfo(f"k{i}", f"H{i}", f"A{i}", "premier_league",
                                  datetime(2026, 9, 20, 12 + i, tzinfo=timezone.utc))
             for i in range(6)}
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert len(cands) <= config.coupons.max_candidates_displayed

    def test_sorted_by_score_desc(self, config):
        s = [sel("k1", "match_winner", "home", "2.50", 0.46, score=78),
             sel("k2", "match_winner", "home", "1.90", 0.52, score=90),
             sel("k3", "match_winner", "home", "2.00", 0.50, score=84),
             sel("k4", "match_winner", "home", "1.85", 0.53, score=92)]
        f = {f"k{i}": FixtureInfo(f"k{i}", f"H{i}", f"A{i}", "premier_league",
                                  datetime(2026, 9, 20, 12 + i, tzinfo=timezone.utc))
             for i in range(1, 5)}
        cands, _ = build_candidates(s, f, config, now=NOW)
        scores = [c.score for c in cands]
        assert scores == sorted(scores, reverse=True)

    def test_higher_quality_coupons_score_higher(self, config):
        good = [sel("k1", "match_winner", "home", "2.00", 0.55, score=95),
                sel("k2", "match_winner", "home", "2.10", 0.53, score=94)]
        weak = [sel("k3", "match_winner", "home", "2.00", 0.55, score=81),
                sel("k4", "match_winner", "home", "2.10", 0.53, score=80)]
        f = {f"k{i}": FixtureInfo(f"k{i}", f"H{i}", f"A{i}", "premier_league",
                                  datetime(2026, 9, 20, 6 + i * 3,
                                           tzinfo=timezone.utc))
             for i in range(1, 5)}
        cands, _ = build_candidates(good + weak, f, config, now=NOW)
        by_key = {tuple(c.coupon_id for c in [c]): c for c in cands}
        # le coupon formé des deux meilleures sélections doit être en tête
        top = cands[0]
        assert min(s.score for s in top.selections) >= 94

    def test_pilot_mode_limits_to_two_selections(self, config):
        # 4 éligibles bien espacées : pas de triplet en mode pilote
        s = [sel(f"k{i}", "match_winner", "home", "2.00", 0.50) for i in range(4)]
        f = {f"k{i}": FixtureInfo(f"k{i}", f"H{i}", f"A{i}", "premier_league",
                                  datetime(2026, 9, 20, 12 + i * 3,
                                           tzinfo=timezone.utc))
             for i in range(4)}
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert all(len(c.selections) == 2 for c in cands)

    def test_public_mode_allows_three(self, config):
        pub = config.__class__(**{**config.__dict__, "mode": "public"})
        s = [sel(f"k{i}", "match_winner", "home", "2.00", 0.50) for i in range(4)]
        f = {f"k{i}": FixtureInfo(f"k{i}", f"H{i}", f"A{i}", "premier_league",
                                  datetime(2026, 9, 20, 12 + i * 3,
                                           tzinfo=timezone.utc))
             for i in range(4)}
        cands, _ = build_candidates(s, f, pub, now=NOW)
        assert any(len(c.selections) == 3 for c in cands)

    def test_uncertainty_penalty_decreases_with_joint_probability(self, config):
        from football.coupons import _uncertainty_penalty

        assert _uncertainty_penalty(0.5) == pytest.approx(1.0)
        assert _uncertainty_penalty(0.25) == pytest.approx(0.75)
        assert _uncertainty_penalty(0.05) == 0.5  # plancher

    def test_constraints_listed(self, config):
        s = [sel("k1", "match_winner", "home", "2.00", 0.50),
             sel("k2", "match_winner", "home", "2.10", 0.48)]
        f = fixtures(k1=("A", "B", "premier_league", K(18, 0)),
                     k2=("C", "D", "premier_league", K(21, 0)))
        cands, _ = build_candidates(s, f, config, now=NOW)
        assert any("indépendance" in c for c in cands[0].constraints)
        assert any("une sélection par match" in c for c in cands[0].constraints)
