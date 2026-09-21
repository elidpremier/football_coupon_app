"""Tests : normalisation des équipes, dates, marchés, identifiant stable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from football.normalize import (
    Market,
    fixture_status_ok,
    kickoff_agrees,
    make_fixture_key,
    match_team_names,
    normalize_market,
    normalize_outcome,
    normalize_team_name,
)
from football.utils import parse_iso, to_utc

KICK = datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc)


class TestNormalizeTeam:
    def test_accents_and_case(self):
        assert normalize_team_name("  Étoile de Franche-Comté  ") == \
            "etoile de franche comte"

    def test_suffix_stripped_when_rest_remains(self):
        assert normalize_team_name("Arsenal FC") == "arsenal"
        assert normalize_team_name("Olympique de Marseille FC") == \
            "olympique de marseille"

    def test_single_token_suffix_kept(self):
        # "FC" seul ne doit pas devenir la chaîne vide
        assert normalize_team_name("FC") == "fc"

    def test_punctuation_removed(self):
        assert normalize_team_name("Brighton & Hove Albion") == \
            "brighton hove albion"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalize_team_name("   ")

    def test_stability_across_variants(self):
        a = normalize_team_name("Manchester United F.C.")
        b = normalize_team_name("manchester united fc")
        assert a == b == "manchester united"


class TestTeamMatching:
    def test_exact_match(self):
        mt = match_team_names("Arsenal FC", "Arsenal")
        assert mt.status == "matched"
        assert mt.ratio == 1.0

    def test_fuzzy_very_close_is_matched(self):
        mt = match_team_names("Wolverhampton Wanderers", "Wolverhampton Wanders")
        assert mt.status == "matched"

    def test_near_match_goes_to_review(self):
        # similarité partielle : jamais de fusion automatique
        mt = match_team_names("Inter Milan", "Internazionale")
        assert mt.status in {"review_required", "mismatch"}
        if mt.status == "review_required":
            assert mt.ratio < 0.999

    def test_totally_different(self):
        mt = match_team_names("Arsenal", "Bayern Munich")
        assert mt.status == "mismatch"

    def test_review_status_is_never_matched(self):
        mt = match_team_names("Paris Saint Germain", "Paris SG Football")
        assert mt.status != "matched"


class TestFixtureKey:
    def test_shape(self):
        key = make_fixture_key("premier_league", KICK, "Arsenal FC", "Chelsea")
        assert key == "premier_league|2026-09-20T18:00:00Z|arsenal|chelsea"

    def test_stable_across_name_variants(self):
        k1 = make_fixture_key("premier_league", KICK, "Arsenal FC", "Chelsea")
        k2 = make_fixture_key("premier_league", KICK, "arsenal", "Chelsea F.C.")
        assert k1 == k2

    def test_different_kickoff_different_key(self):
        k1 = make_fixture_key("premier_league", KICK, "Arsenal", "Chelsea")
        k2 = make_fixture_key(
            "premier_league", KICK + timedelta(minutes=1), "Arsenal", "Chelsea")
        assert k1 != k2

    def test_different_competition_different_key(self):
        k1 = make_fixture_key("premier_league", KICK, "Arsenal", "Chelsea")
        k2 = make_fixture_key("ligue_1", KICK, "Arsenal", "Chelsea")
        assert k1 != k2

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            make_fixture_key("premier_league",
                             datetime(2026, 9, 20, 18, 0), "A", "B")

    def test_swapped_teams_different_key(self):
        k1 = make_fixture_key("premier_league", KICK, "Arsenal", "Chelsea")
        k2 = make_fixture_key("premier_league", KICK, "Chelsea", "Arsenal")
        assert k1 != k2


class TestDates:
    def test_parse_z_suffix(self):
        dt = parse_iso("2026-09-20T18:00:00Z")
        assert dt.tzinfo is not None
        assert to_utc(dt) == datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)

    def test_parse_offset_converted(self):
        dt = parse_iso("2026-09-20T19:00:00+01:00")
        assert to_utc(dt) == datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)

    def test_kickoff_agrees_within_tolerance(self):
        assert kickoff_agrees(KICK, KICK + timedelta(minutes=4), 5)

    def test_kickoff_disagrees_beyond_tolerance(self):
        assert not kickoff_agrees(KICK, KICK + timedelta(minutes=15), 5)

    def test_kickoff_agrees_across_zones(self):
        local = datetime(2026, 9, 20, 19, 0, tzinfo=timezone(timedelta(hours=1)))
        assert kickoff_agrees(KICK, local, 5)


class TestMarkets:
    def test_known_aliases(self):
        assert normalize_market("1X2") == "match_winner"
        assert normalize_market("Over/Under 2.5") == "over_under_2_5"
        assert normalize_market("Double Chance") == "double_chance"
        assert normalize_market("Doublechance") == "double_chance"

    def test_unknown_market_raises(self):
        with pytest.raises(ValueError):
            normalize_market("Corners Over 8.5")

    def test_outcome_mapping(self):
        assert normalize_outcome("match_winner", "1") == "home"
        assert normalize_outcome("match_winner", "X") == "draw"
        assert normalize_outcome("match_winner", "2") == "away"
        assert normalize_outcome("double_chance", "1X") == "home_draw"
        assert normalize_outcome("double_chance", "12") == "home_away"
        assert normalize_outcome("double_chance", "X2") == "away_draw"
        assert normalize_outcome("over_under_2_5", "Over") == "over"
        assert normalize_outcome("over_under_2_5", "under 2.5") == "under"

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValueError):
            normalize_outcome("match_winner", "triple_chance")

    def test_market_enum_values(self):
        assert {m.value for m in Market} == {
            "match_winner", "double_chance", "over_under_2_5"}


class TestFixtureStatus:
    def test_scheduled_ok(self):
        assert fixture_status_ok("NS")

    def test_canceled_not_ok(self):
        assert not fixture_status_ok("CANCELED")
        assert not fixture_status_ok("POSTPONED")

    def test_finished_not_ok(self):
        assert not fixture_status_ok("FT")
        assert not fixture_status_ok("INPLAY")
