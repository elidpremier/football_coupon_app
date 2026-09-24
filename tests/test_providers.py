"""Tests : adaptateurs API-Football & football-data.org (réponses
enregistrées comme fixtures), erreurs réseau, formats invalides."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from football.http import (
    ApiClient,
    Cache,
    HttpResponse,
    ProviderHttpError,
    QuotaExceeded,
    RateLimiter,
)
from football.providers.api_football import ApiFootballProvider
from football.providers.base import ProviderError, build_providers, competition_to_provider_code
from football.providers.demo import DemoProvider, demo_day_for
from football.providers.football_data import FootballDataOrgProvider
from football.storage import Database
from football.utils import to_decimal, utcnow
from tests.conftest import FakeTransport, NOW, DAY


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "p.db"))
    yield d
    d.close()


def make_client(db, handler, name="api_football", budget=50, per_minute=None):
    transport = FakeTransport(handler)
    return ApiClient(
        name, transport, Cache(db, now_fn=lambda: NOW),
        RateLimiter(db, now_fn=lambda: NOW), budget, per_minute,
        now_fn=lambda: NOW,
    ), transport


class TestApiFootball:
    def _provider(self, db, recorded, name="api_football"):
        def handler(method, url, headers, params, body):
            if "/fixtures" in url and "injuries" not in url and "league" in (params or {}):
                return recorded["aff_fixtures"]
            if "/odds" in url:
                return recorded["aff_odds"]
            if "/standings" in url:
                return recorded["aff_standings"]
            if "injuries" in url:
                return recorded["aff_injuries"]
            return (404, {"error": "nf"})

        client, transport = make_client(db, handler)
        return ApiFootballProvider(client, "token-test"), transport

    def test_parse_fixtures(self, db, recorded):
        p, _ = self._provider(db, recorded)
        fxs = p.fetch_fixtures(DAY, "premier_league")
        assert len(fxs) == 2
        f = fxs[0]
        assert f.provider_fixture_id == "1101"
        assert f.home_team == "Arsenal FC"
        assert f.kickoff_utc == datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
        assert f.status == "NS"
        assert f.competition_slug == "premier_league"

    def test_parse_odds_with_timestamps(self, db, recorded):
        p, _ = self._provider(db, recorded)
        odds = p.fetch_odds(DAY, "premier_league")
        by = {(o.provider_fixture_id, o.market, o.outcome): o for o in odds}
        assert by[("1101", "match_winner", "home")].odds == to_decimal("2.10")
        assert by[("1101", "match_winner", "draw")].odds == to_decimal("3.40")
        assert by[("1101", "match_winner", "away")].odds == to_decimal("3.60")
        assert by[("1101", "over_under_2_5", "over")].odds == to_decimal("1.95")
        assert by[("1102", "match_winner", "home")].odds == to_decimal("1.90")
        # horodatage respecté (lastUpdateTime)
        assert by[("1101", "match_winner", "home")].observed_at_utc == \
            datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc)
        assert by[("1101", "match_winner", "home")].bookmaker == "Betway"

    def test_parse_standings(self, db, recorded):
        p, _ = self._provider(db, recorded)
        rows = p.fetch_standings("premier_league", 2026)
        by = {r.team: r for r in rows}
        assert by["Arsenal FC"].position == 1
        assert by["Arsenal FC"].played == 8
        assert by["Brighton & Hove Albion"].position == 4

    def test_parse_injuries(self, db, recorded):
        p, transport = self._provider(db, recorded)
        avail = p.fetch_availability(DAY, "premier_league")
        by = {(a.team, a.player): a for a in avail}
        assert by[("Arsenal", "M. Silva")].status == "confirmed"
        assert by[("Arsenal", "M. Silva")].reason == "injury"
        assert by[("Chelsea", "J. Turner")].status == "unconfirmed"

        # Vérifie que l'URL /injuries avec paramètre fixture est bien utilisée
        inj_calls = [c for c in transport.calls if "/injuries" in c["url"]]
        assert len(inj_calls) > 0
        assert inj_calls[0]["params"] == {"fixture": "1101"}

    def test_parse_odds_v3_bets_values_schema(self, db):
        recorded_v3 = {
            "aff_fixtures": {
                "response": [
                    {
                        "fixture": {"id": 1101, "date": "2026-09-20T18:00:00+00:00", "status": {"short": "NS"}},
                        "league": {"id": 39, "name": "Premier League"},
                        "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}}
                    }
                ]
            },
            "aff_odds": {
                "response": [
                    {
                        "fixture": {"id": 1101},
                        "update": "2026-09-20T11:00:00+00:00",
                        "bookmakers": [
                            {
                                "id": 6,
                                "name": "Bwin",
                                "bets": [
                                    {
                                        "id": 1,
                                        "name": "Match Winner",
                                        "values": [
                                            {"value": "Home", "odd": "1.85"},
                                            {"value": "Draw", "odd": "3.50"},
                                            {"value": "Away", "odd": "4.20"}
                                        ]
                                    },
                                    {
                                        "id": 5,
                                        "name": "Goals Over/Under",
                                        "values": [
                                            {"value": "Over 2.5", "odd": "1.95"},
                                            {"value": "Under 2.5", "odd": "1.85"}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        def handler(method, url, headers, params, body):
            if "/fixtures" in url:
                return recorded_v3["aff_fixtures"]
            if "/odds" in url:
                return recorded_v3["aff_odds"]
            return (404, {"error": "nf"})

        client, _ = make_client(db, handler)
        p = ApiFootballProvider(client, "token-test")
        odds = p.fetch_odds(DAY, "premier_league")
        assert len(odds) == 5
        by = {(o.market, o.outcome): o.odds for o in odds}
        assert by[("match_winner", "home")] == to_decimal("1.85")
        assert by[("match_winner", "draw")] == to_decimal("3.50")
        assert by[("match_winner", "away")] == to_decimal("4.20")
        assert by[("over_under_2_5", "over")] == to_decimal("1.95")
        assert by[("over_under_2_5", "under")] == to_decimal("1.85")

    def test_http_error_wrapped(self, db):
        def handler(method, url, headers, params, body):
            return (500, {"error": "boom"})

        client, _ = make_client(db, handler)
        p = ApiFootballProvider(client, "t")
        with pytest.raises(ProviderError):
            p.fetch_fixtures(DAY, "premier_league")

    def test_quota_wrapped(self, db):
        def handler(method, url, headers, params, body):
            return (200, {"response": []})

        client, _ = make_client(db, handler, budget=0)
        p = ApiFootballProvider(client, "t")
        with pytest.raises(ProviderError, match="quota"):
            p.fetch_fixtures(DAY, "premier_league")

    def test_uncovered_competition(self):
        with pytest.raises(ProviderError):
            competition_to_provider_code("api_football", "mars_league")


class TestFootballData:
    def _provider(self, db, recorded):
        def handler(method, url, headers, params, body):
            assert headers.get("X-Auth-Token") == "token-fd"
            if "/matches" in url:
                return recorded["fd_matches"]
            if "/standings" in url:
                return recorded["fd_standings"]
            return (404, {"error": "nf"})

        client, transport = make_client(db, handler, name="football_data",
                                        per_minute=10)
        return FootballDataOrgProvider(client, "token-fd"), transport

    def test_parse_matches(self, db, recorded):
        p, _ = self._provider(db, recorded)
        fxs = p.fetch_fixtures(DAY, "premier_league")
        assert len(fxs) == 2
        assert fxs[0].kickoff_utc == datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
        assert fxs[0].status == "SCHEDULED"

    def test_parse_odds_from_same_call(self, db, recorded):
        p, _ = self._provider(db, recorded)
        odds = p.fetch_odds(DAY, "premier_league")
        by = {(o.provider_fixture_id, o.outcome): o for o in odds}
        assert by[("2101", "home")].odds == to_decimal("2.12")
        assert by[("2102", "away")].odds == to_decimal("4.10")
        assert all(o.market == "match_winner" for o in odds)

    def test_odds_call_cached_no_extra_http(self, db, recorded):
        p, transport = self._provider(db, recorded)
        p.fetch_fixtures(DAY, "premier_league")
        n = len(transport.calls)
        p.fetch_odds(DAY, "premier_league")
        # même URL logique → servi par le cache, pas d'appel supplémentaire
        assert len(transport.calls) == n

    def test_parse_standings(self, db, recorded):
        p, _ = self._provider(db, recorded)
        rows = p.fetch_standings("premier_league", 2026)
        by = {r.team: r for r in rows}
        assert by["Arsenal FC"].form == "WWDWW"
        assert by["Arsenal FC"].position == 1

    def test_http_error_wrapped(self, db):
        def handler(method, url, headers, params, body):
            return (403, {"message": "forbidden"})

        client, _ = make_client(db, handler, name="football_data")
        p = FootballDataOrgProvider(client, "t")
        with pytest.raises(ProviderError):
            p.fetch_fixtures(DAY, "premier_league")

    def test_unsupported_competition(self):
        with pytest.raises(ProviderError):
            competition_to_provider_code("football_data", "coppa_italia")


class TestBuildProviders:
    def test_no_keys_gives_demo(self, db, config, secrets):
        providers = build_providers(config, secrets, db)
        assert set(providers) == {"demo_primary", "demo_secondary"}
        assert isinstance(providers["demo_primary"], DemoProvider)

    def test_aff_key_builds_api_football(self, db, config, secrets):
        s = secrets.__class__(**{**secrets.__dict__, "api_football_key": "k"})
        providers = build_providers(config, s, db,
                                    transport=FakeTransport(
                                        lambda *a: {"response": []}))
        assert "api_football" in providers
        assert isinstance(providers["api_football"], ApiFootballProvider)

    def test_aff_without_fd_token(self, db, config, secrets):
        s = secrets.__class__(**{**secrets.__dict__, "api_football_key": "k"})
        providers = build_providers(config, s, db,
                                    transport=FakeTransport(
                                        lambda *a: {"response": []}))
        assert "football_data" not in providers


class TestDemoProvider:
    def test_fixture_count_and_kickoffs(self):
        p = DemoProvider("demo_primary", day=DAY, now=NOW)
        fxs = p.fetch_fixtures(DAY, "premier_league")
        assert len(fxs) == 8
        assert all(f.status == "NS" for f in fxs)

    def test_secondary_kickoff_divergence(self):
        p1 = DemoProvider("demo_primary", day=DAY, now=NOW)
        p2 = DemoProvider("demo_secondary", day=DAY, now=NOW)
        f1 = {f.provider_fixture_id.rsplit("-", 1)[1]: f
              for f in p1.fetch_fixtures(DAY, "premier_league")}
        f2 = {f.provider_fixture_id.rsplit("-", 1)[1]: f
              for f in p2.fetch_fixtures(DAY, "premier_league")}
        diff = (f2["6"].kickoff_utc - f1["6"].kickoff_utc).total_seconds()
        assert diff == 15 * 60
        assert f1["0"].kickoff_utc == f2["0"].kickoff_utc

    def test_stale_fixture_odds_age(self):
        p = DemoProvider("demo_primary", day=DAY, now=NOW)
        odds = p.fetch_odds(DAY, "premier_league")
        stale = [o for o in odds if o.provider_fixture_id.endswith("-7")]
        fresh = [o for o in odds if o.provider_fixture_id.endswith("-0")]
        assert stale and fresh
        stale_age = (NOW - stale[0].observed_at_utc).total_seconds() / 3600
        fresh_age = (NOW - fresh[0].observed_at_utc).total_seconds() / 3600
        assert stale_age == pytest.approx(20.0)
        assert fresh_age == pytest.approx(1.0)

    def test_results_before_anchor_day_empty(self):
        p = DemoProvider("demo_primary", day=DAY, now=NOW)
        assert p.fetch_results(DAY, "premier_league") == []

    def test_results_for_past_day(self):
        p = DemoProvider("demo_primary", day=DAY, now=NOW)
        results = p.fetch_results(DAY.replace(day=19), "premier_league")
        assert len(results) == 8
        assert all(r.status == "FT" for r in results)
        scores = {(r.home_team, r.away_team): (r.home_score, r.away_score)
                  for r in results}
        assert scores[("Arsenal", "Chelsea")] == (2, 1)

    def test_unsupported_competition(self):
        p = DemoProvider("demo_primary", day=DAY, now=NOW)
        assert p.fetch_fixtures(DAY, "ligue_1") == []

    def test_demo_day_anchor(self):
        from datetime import datetime as dt, timezone as tz

        before = demo_day_for(dt(2026, 9, 20, 10, 0, tzinfo=tz.utc))
        after = demo_day_for(dt(2026, 9, 20, 21, 30, tzinfo=tz.utc))
        assert before.day == 20
        assert after.day == 21
