"""Tests : features — stabilité de cote (amplitude par issue, multi-
sources), fraîcheur, cohérence inter-sources."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from football.features import (
    FeatureBundle,
    consistency_problems,
    market_odds_rows,
    newest_odds_age_hours,
    odds_stability,
)
from football.utils import to_decimal

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
KICK = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def bundle_with(
    odds=None,
    odds_values=None,
    providers=None,
    matching_status="OK",
) -> FeatureBundle:
    default_odds = {
        "match_winner": {
            "home": {"odds": to_decimal("2.10"), "observed_at": NOW, "provider": "p"},
            "draw": {"odds": to_decimal("3.40"), "observed_at": NOW, "provider": "p"},
            "away": {"odds": to_decimal("3.60"), "observed_at": NOW, "provider": "p"},
        }
    }
    default_values = {
        "match_winner": {
            "home": [to_decimal("2.10")],
            "draw": [to_decimal("3.40")],
            "away": [to_decimal("3.60")],
        }
    }
    return FeatureBundle(
        fixture_key="k", competition_slug="premier_league",
        home_team="A", away_team="B", kickoff_utc=KICK, status="COLLECTED",
        matching_status=matching_status,
        odds=odds if odds is not None else default_odds,
        odds_values=odds_values if odds_values is not None else default_values,
        providers=providers if providers is not None
        else {"p": {"kickoff": KICK, "id": "1"}},
        teams={}, availability=[], computed_at=NOW,
    )


class TestOddsStability:
    def test_same_outcome_across_sources(self):
        b = bundle_with(odds_values={
            "match_winner": {
                "home": [to_decimal("2.10"), to_decimal("2.12")],
                "draw": [to_decimal("3.40"), to_decimal("3.45")],
                "away": [to_decimal("3.60"), to_decimal("3.55")],
            }
        })
        s = odds_stability(b, "match_winner")
        assert s is not None
        assert s > 0.98  # cotes stables

    def test_not_confusing_outcomes(self):
        """Piège classique : comparer la cote « home » (2.10) à celle du
        « nul » (3.40) donnerait une stabilité faussement faible. La
        stabilité ne doit PAS baisser quand les issues ont des cotes
        différentes."""
        b = bundle_with(odds_values={
            "match_winner": {
                "home": [to_decimal("2.10")],
                "draw": [to_decimal("3.40")],
                "away": [to_decimal("3.60")],
            }
        })
        s = odds_stability(b, "match_winner")
        assert s == pytest.approx(0.8)  # 1 observation/issue : neutre

    def test_volatile_market_detected(self):
        b = bundle_with(odds_values={
            "match_winner": {
                "home": [to_decimal("1.90"), to_decimal("2.40")],
                "draw": [to_decimal("3.40"), to_decimal("3.40")],
                "away": [to_decimal("3.60"), to_decimal("3.60")],
            }
        })
        s = odds_stability(b, "match_winner")
        assert s is not None
        # home : 1 - 0.5/2.40 = 0.7917 ; mean = (0.7917+1+1)/3 ≈ 0.93
        assert s == pytest.approx((1 - 0.5 / 2.40 + 1 + 1) / 3, abs=1e-6)

    def test_single_observation_neutral(self):
        b = bundle_with()
        assert odds_stability(b, "match_winner") == pytest.approx(0.8)

    def test_missing_market_none(self):
        b = bundle_with()
        assert odds_stability(b, "over_under_2_5") is None

    def test_zero_odds_ignored(self):
        b = bundle_with(odds_values={
            "match_winner": {"home": [to_decimal("0"), to_decimal("2.10")]}
        })
        s = odds_stability(b, "match_winner")
        assert s is None  # pas de valeur exploitable


class TestFreshness:
    def test_age_computed_from_newest(self):
        b = bundle_with()
        b.odds["match_winner"]["home"]["observed_at"] = NOW - timedelta(hours=1)
        b.odds["match_winner"]["draw"]["observed_at"] = NOW - timedelta(hours=2)
        b.odds["match_winner"]["away"]["observed_at"] = NOW - timedelta(hours=3)
        age = newest_odds_age_hours(b, NOW)
        assert age == pytest.approx(1.0)  # la plus récente prime

    def test_no_odds_none(self):
        b = bundle_with(odds={})
        assert newest_odds_age_hours(b, NOW) is None

    def test_future_observed_clamped_zero(self):
        b = bundle_with()
        b.odds["match_winner"]["home"]["observed_at"] = NOW + timedelta(hours=1)
        assert newest_odds_age_hours(b, NOW) == 0.0


class TestConsistency:
    def test_kickoff_divergence_detected(self, config):
        b = bundle_with(providers={
            "p": {"kickoff": KICK, "id": "1"},
            "s": {"kickoff": KICK + timedelta(minutes=15), "id": "2"},
        })
        problems = consistency_problems(b, config)
        assert any("horaire divergent" in p for p in problems)

    def test_no_divergence_within_tolerance(self, config):
        b = bundle_with(providers={
            "p": {"kickoff": KICK, "id": "1"},
            "s": {"kickoff": KICK + timedelta(minutes=3), "id": "2"},
        })
        problems = consistency_problems(b, config)
        assert not any("horaire divergent" in p for p in problems)

    def test_market_consistency_reported(self, config):
        b = bundle_with()  # seulement match_winner présent
        problems = consistency_problems(b, config)
        assert any("double_chance" in p for p in problems)
        assert any("over_under_2_5" in p for p in problems)


class TestMarketRows:
    def test_rows_returned(self):
        b = bundle_with()
        rows = market_odds_rows(b, "match_winner")
        assert rows == {"home": to_decimal("2.10"),
                        "draw": to_decimal("3.40"),
                        "away": to_decimal("3.60")}
        assert market_odds_rows(b, "over_under_2_5") == {}
