"""Tests : probabilités implicites, correction de marge, edge, Poisson."""
from __future__ import annotations

from decimal import Decimal

import pytest

from football.probabilities import (
    MARKET_MODEL_VERSION,
    ProbabilityError,
    edge,
    implied_probabilities,
    is_value_bet,
    market_consistency_check,
    market_model_probability,
    overround,
    poisson_model,
    poisson_pmf,
    probabilities_1x2,
    score_matrix,
    validate_odds,
)


class TestImpliedProbabilities:
    def test_sum_to_one(self):
        odds = {"home": Decimal("2.00"), "draw": Decimal("3.50"),
                "away": Decimal("3.50")}
        probs, total = implied_probabilities(odds)
        assert abs(sum(probs.values()) - 1.0) < 1e-12
        assert total > 1.0  # marge du marché

    def test_overround_positive_for_real_odds(self):
        odds = {"home": Decimal("2.10"), "draw": Decimal("3.40"),
                "away": Decimal("3.60")}
        assert 0.0 < overround(odds) < 0.20

    def test_fair_odds_have_zero_overround(self):
        odds = {"home": Decimal("2.00"), "draw": Decimal("3.00"),
                "away": Decimal("6.00")}
        assert abs(overround(odds)) < 1e-9

    def test_all_outcomes_between_0_and_1(self):
        probs, _ = implied_probabilities(
            {"home": Decimal("1.20"), "away": Decimal("9.00")})
        for p in probs.values():
            assert 0.0 < p < 1.0

    def test_favorite_has_highest_probability(self):
        probs, _ = implied_probabilities(
            {"home": Decimal("1.40"), "draw": Decimal("5.00"),
             "away": Decimal("8.00")})
        assert probs["home"] == max(probs.values())


class TestValidation:
    def test_odds_equal_to_one_rejected(self):
        with pytest.raises(ProbabilityError):
            validate_odds({"home": Decimal("1.00")})

    def test_odds_below_one_rejected(self):
        with pytest.raises(ProbabilityError):
            validate_odds({"home": Decimal("0.98")})

    def test_empty_rejected(self):
        with pytest.raises(ProbabilityError):
            validate_odds({})

    def test_garbage_value_rejected(self):
        with pytest.raises((ProbabilityError, ValueError)):
            validate_odds({"home": "abc"})

    def test_huge_odds_rejected(self):
        with pytest.raises(ProbabilityError):
            validate_odds({"home": Decimal("10000")})


class TestMarketModel:
    def test_probability_in_strict_bounds(self):
        p = market_model_probability(
            "home", {"home": Decimal("2.10"), "draw": Decimal("3.40"),
                     "away": Decimal("3.60")})
        assert 0.0 < p < 1.0

    def test_unknown_outcome_raises(self):
        with pytest.raises(ProbabilityError):
            market_model_probability("draw", {"home": Decimal("2.00"),
                                              "away": Decimal("3.00")})

    def test_edge_is_zero_by_construction_in_market_mode(self):
        """Règle §8.1 : avec le modèle de marché, la value est nulle —
        simuler une value est interdit (is_value_bet → False)."""
        odds = {"home": Decimal("2.10"), "draw": Decimal("3.40"),
                "away": Decimal("3.60")}
        p = market_model_probability("home", odds)
        e = edge(p, odds["home"])
        # e = 1/S - 1 : négatif ou nul, jamais de value
        assert e <= 1e-12
        assert is_value_bet(MARKET_MODEL_VERSION, p, odds["home"], 0.01) is False

    def test_value_reported_only_with_validated_model(self):
        assert is_value_bet("poisson_baseline_v0", 0.60, Decimal("2.00"),
                            threshold=0.05) is True
        assert is_value_bet("poisson_baseline_v0", 0.50, Decimal("1.90"),
                            threshold=0.05) is False

    def test_edge_value(self):
        assert abs(edge(0.5, Decimal("2.00")) - 0.0) < 1e-12
        assert abs(edge(0.6, Decimal("2.00")) - 0.2) < 1e-12


class TestConsistency:
    def test_all_markets_covered(self):
        probs = {
            "match_winner": {"home": Decimal("2.00"), "draw": Decimal("3.00"),
                             "away": Decimal("3.50")},
            "over_under_2_5": {"over": Decimal("1.90"), "under": Decimal("1.95")},
        }
        assert market_consistency_check(probs, ["match_winner",
                                                "over_under_2_5"]) == []

    def test_missing_market_reported(self):
        probs = {"match_winner": {"home": Decimal("2.00"),
                                  "draw": Decimal("3.00"),
                                  "away": Decimal("3.50")}}
        problems = market_consistency_check(
            probs, ["match_winner", "double_chance", "over_under_2_5"])
        assert any("double_chance" in p for p in problems)
        assert any("over_under_2_5" in p for p in problems)

    def test_missing_outcome_reported(self):
        probs = {"match_winner": {"home": Decimal("2.00"),
                                  "draw": Decimal("3.00")}}
        problems = market_consistency_check(probs, ["match_winner"])
        assert any("away" in p for p in problems)

    def test_unknown_market_reported(self):
        assert market_consistency_check({}, ["exact_score"])


class TestPoisson:
    def test_pmf_sums_to_one(self):
        total = sum(poisson_pmf(1.5, k) for k in range(20))
        assert abs(total - 1.0) < 1e-9

    def test_pmf_rejects_negative_lambda(self):
        with pytest.raises(ProbabilityError):
            poisson_pmf(-0.5, 0)

    def test_matrix_dimensions(self):
        m = score_matrix(1.5, 1.1, max_goals=8)
        assert len(m) == 9 and len(m[0]) == 9

    def test_1x2_sums_to_one(self):
        probs = probabilities_1x2(score_matrix(1.6, 1.0))
        assert abs(sum(probs.values()) - 1.0) < 1e-9

    def test_home_advantage_shifts_probability(self):
        base = poisson_model(1.0, 1.0, 1.0, 1.0, home_advantage=1.0)
        adv = poisson_model(1.0, 1.0, 1.0, 1.0, home_advantage=1.3)
        assert adv["match_winner"]["home"] > base["match_winner"]["home"]

    def test_stronger_team_more_likely_to_win(self):
        res = poisson_model(attack_home=1.4, defense_home=0.8,
                            attack_away=0.8, defense_away=1.2)
        assert res["match_winner"]["home"] > res["match_winner"]["away"]

    def test_over_under_complement(self):
        res = poisson_model(1.2, 1.0, 1.0, 1.1)
        ou = res["over_under_2_5"]
        assert abs(ou["over"] + ou["under"] - 1.0) < 1e-9

    def test_rejects_negative_strengths(self):
        with pytest.raises(ProbabilityError):
            poisson_model(-1.0, 1.0, 1.0, 1.0)
