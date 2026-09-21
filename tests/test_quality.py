"""Tests : score de qualité, fraîcheur, exclusions dures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from football.features import FeatureBundle
from football.quality import compute_quality, hard_exclusions
from football.utils import to_decimal

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_bundle(**kw) -> FeatureBundle:
    base = dict(
        fixture_key="premier_league|2026-09-20T18:00:00Z|arsenal|chelsea",
        competition_slug="premier_league",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc),
        status="COLLECTED",
        matching_status="OK",
        odds={
            "match_winner": {
                "home": {"odds": to_decimal("2.10"),
                         "observed_at": NOW - timedelta(hours=1),
                         "provider": "p"},
                "draw": {"odds": to_decimal("3.40"),
                         "observed_at": NOW - timedelta(hours=1),
                         "provider": "p"},
                "away": {"odds": to_decimal("3.60"),
                         "observed_at": NOW - timedelta(hours=1),
                         "provider": "p"},
            },
            "over_under_2_5": {
                "over": {"odds": to_decimal("1.95"),
                         "observed_at": NOW - timedelta(hours=1),
                         "provider": "p"},
                "under": {"odds": to_decimal("1.85"),
                          "observed_at": NOW - timedelta(hours=1),
                          "provider": "p"},
            },
        },
        providers={"p": {"kickoff": datetime(2026, 9, 20, 18, 0,
                                             tzinfo=timezone.utc), "id": "1"},
                   "s": {"kickoff": datetime(2026, 9, 20, 18, 0,
                                             tzinfo=timezone.utc), "id": "2"}},
        teams={"Arsenal": {"position": 1, "played": 8, "form": "WWDWW"},
               "Chelsea": {"position": 2, "played": 8, "form": "DWLWW"}},
        availability=[],
        computed_at=NOW,
    )
    # double chance dérivée de la 1X2 (3e marché configuré)
    from football.probabilities import derive_double_chance

    x12 = {o: info["odds"] for o, info in base["odds"]["match_winner"].items()}
    dc = derive_double_chance(x12)
    base["odds"]["double_chance"] = {
        k: {"odds": v,
            "observed_at": base["odds"]["match_winner"]["home"]["observed_at"],
            "provider": "p"}
        for k, v in dc.items()
    }
    base.update(kw)
    return FeatureBundle(**base)


class TestQualityScore:
    def test_full_data_high_score(self, config):
        q = compute_quality(make_bundle(), config, NOW)
        assert 85 <= q.score <= 100
        assert q.exclusions == []

    def test_stale_odds_strong_penalty_and_exclusion(self, config):
        b = make_bundle()
        for m in b.odds.values():
            for i in m.values():
                i["observed_at"] = NOW - timedelta(hours=20)
        q = compute_quality(b, config, NOW)
        assert q.exclusions, "cotes de 20 h doivent déclencher une exclusion dure"
        assert q.score == 0.0

    def test_freshness_decreases_with_age(self, config):
        fresh = compute_quality(make_bundle(), config, NOW)
        b = make_bundle()
        for m in b.odds.values():
            for i in m.values():
                i["observed_at"] = NOW - timedelta(hours=3)
        aged = compute_quality(b, config, NOW)
        assert aged.freshness < fresh.freshness
        assert aged.score < fresh.score

    def test_single_source_reduces_agreement(self, config):
        b = make_bundle()
        b.providers = {"p": list(b.providers.values())[0]}
        q = compute_quality(b, config, NOW)
        assert q.source_agreement == pytest.approx(0.6)

    def test_two_sources_full_agreement(self, config):
        q = compute_quality(make_bundle(), config, NOW)
        assert q.source_agreement == 1.0

    def test_missing_team_reduces_coverage(self, config):
        b = make_bundle()
        del b.teams["Chelsea"]
        q = compute_quality(b, config, NOW)
        assert q.team_data_coverage == pytest.approx(0.5)

    def test_unconfirmed_availability_mentioned_not_fatal(self, config):
        b = make_bundle(availability=[
            {"team": "Chelsea", "player": "M. X", "reason": "injury",
             "status": "unconfirmed", "source_ref": "s#injuries"}])
        q = compute_quality(b, config, NOW)
        assert q.availability_confidence == pytest.approx(0.75)
        assert any("non confirmée" in n for n in q.notes)
        assert q.exclusions == []

    def test_confirmed_availability_full_confidence(self, config):
        b = make_bundle(availability=[
            {"team": "Chelsea", "player": "M. X", "reason": "injury",
             "status": "confirmed", "source_ref": "s#injuries"}])
        q = compute_quality(b, config, NOW)
        assert q.availability_confidence == 1.0
        assert any("confirmée" in n for n in q.notes)

    def test_no_odds_is_exclusion(self, config):
        b = make_bundle(odds={})
        q = compute_quality(b, config, NOW)
        assert q.exclusions
        assert q.score == 0.0

    def test_score_bounded(self, config):
        b = make_bundle()
        for m in b.odds.values():
            for i in m.values():
                i["observed_at"] = NOW - timedelta(minutes=1)
        q = compute_quality(b, config, NOW)
        assert 0.0 <= q.score <= 100.0


class TestHardExclusions:
    def test_incoherent_kickoff_excluded(self, config):
        b = make_bundle()
        b.providers["s"]["kickoff"] = b.providers["s"]["kickoff"] + timedelta(minutes=15)
        q = compute_quality(b, config, NOW)
        assert any("horaire divergent" in e for e in q.exclusions)
        assert q.source_agreement == 0.0

    def test_matching_review_required_excluded(self, config):
        b = make_bundle(matching_status="MATCHING_REVIEW_REQUIRED")
        q = compute_quality(b, config, NOW)
        assert any("correspondance" in e for e in q.exclusions)

    def test_ok_matching_not_excluded(self, config):
        b = make_bundle(matching_status="OK")
        assert hard_exclusions(b, config, NOW) == []

    def test_kickoff_within_tolerance_ok(self, config):
        b = make_bundle()
        b.providers["s"]["kickoff"] = b.providers["s"]["kickoff"] + timedelta(minutes=4)
        assert hard_exclusions(b, config, NOW) == []
