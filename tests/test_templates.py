"""Tests : gabarits déterministes (mode off)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from football.analysis.templates import TemplateProvider
from football.analysis.validator import contains_forbidden, validate_ai_parts


def make_payload(**kw) -> dict:
    base = {
        "fixture": {"home_team": "Arsenal", "away_team": "Chelsea",
                    "competition_slug": "premier_league", "venue": ""},
        "kickoff_utc": "2026-09-20T18:00:00Z",
        "odds": {
            "match_winner": {
                "home": {"odds": "2.10", "observed_at_utc": "2026-09-20T11:00:00Z",
                         "age_hours": 1.0, "provider": "p", "bookmaker": "b"},
                "draw": {"odds": "3.40", "observed_at_utc": "2026-09-20T11:00:00Z",
                         "age_hours": 1.0, "provider": "p", "bookmaker": "b"},
                "away": {"odds": "3.60", "observed_at_utc": "2026-09-20T11:00:00Z",
                         "age_hours": 1.0, "provider": "p", "bookmaker": "b"},
            },
            "over_under_2_5": {
                "over": {"odds": "1.95", "observed_at_utc": "2026-09-20T11:00:00Z",
                         "age_hours": 1.0, "provider": "p", "bookmaker": "b"},
                "under": {"odds": "1.85", "observed_at_utc": "2026-09-20T11:00:00Z",
                          "age_hours": 1.0, "provider": "p", "bookmaker": "b"},
            },
        },
        "probabilities": {
            "match_winner": {"home": 0.4543, "draw": 0.2806, "away": 0.2651},
            "over_under_2_5": {"over": 0.5128, "under": 0.4872},
        },
        "overround": {"match_winner": 0.0481, "over_under_2_5": 0.0391},
        "standings": {
            "Arsenal": {"position": 1, "played": 8, "wins": 6, "draws": 1,
                        "losses": 1, "goals_for": 18, "goals_against": 7,
                        "form": "WWDWW"},
            "Chelsea": {"position": 2, "played": 8, "wins": 4, "draws": 2,
                        "losses": 2, "goals_for": 14, "goals_against": 9,
                        "form": "DWLWW"},
        },
        "availability": [
            {"team": "Chelsea", "player": "M. Silva", "reason": "injury",
             "status": "confirmed", "source_ref": "p#injuries"},
            {"team": "Chelsea", "player": "J. Turner", "reason": "doubt",
             "status": "unconfirmed", "source_ref": "p#injuries"},
        ],
        "quality": {"score": 94.2, "notes": []},
        "providers": {"p": {"id": "1"}},
        "sources": {"p#fixtures": "rencontres", "p#odds": "cotes",
                    "classement": "classement"},
        "engine_status": "watch",
    }
    base.update(kw)
    return base


def allowlist_from(payload: dict) -> list[float]:
    allow: list[float] = []
    for mkt in payload["odds"].values():
        for info in mkt.values():
            allow += [float(info["odds"]), info["age_hours"]]
    for mkt in payload["probabilities"].values():
        for p in mkt.values():
            allow += [p, round(p * 100, 1)]
    for s in payload["standings"].values():
        for k in ("position", "played", "wins", "draws", "losses",
                  "goals_for", "goals_against"):
            allow.append(float(s[k]))
    allow += [20, 9, 2026, 18, 0, 100, round(payload["quality"]["score"], 1)]
    for ov in payload["overround"].values():
        allow += [ov, round(ov * 100, 1)]
    return allow


class TestTemplateProvider:
    def test_deterministic(self):
        p = TemplateProvider()
        payload = make_payload()
        src = list(payload["sources"])
        a1, c1 = p.analyse(payload, src)
        a2, c2 = p.analyse(payload, src)
        assert a1.to_dict() == a2.to_dict()
        assert c1.to_dict() == c2.to_dict()

    def test_no_forbidden_words(self):
        p = TemplateProvider()
        payload = make_payload()
        a, c = p.analyse(payload, list(payload["sources"]))
        for part in (a, c):
            for text in part.texts():
                assert not contains_forbidden(text), text

    def test_passes_strict_validator(self):
        p = TemplateProvider()
        payload = make_payload()
        src = list(payload["sources"])
        a, c = p.analyse(payload, src)
        errs = validate_ai_parts(a.to_dict(), c.to_dict(), src,
                                 allowlist_from(payload),
                                 engine_ceiling="eligible", year=2026)
        assert errs == []

    def test_cites_only_known_sources(self):
        p = TemplateProvider()
        payload = make_payload()
        a, c = p.analyse(payload, list(payload["sources"]))
        for ref in a.all_source_refs() + c.all_source_refs():
            assert ref in payload["sources"]

    def test_quotes_odds_verbatim(self):
        p = TemplateProvider()
        payload = make_payload()
        a, _ = p.analyse(payload, list(payload["sources"]))
        assert "2.10" in a.summary or any("2.10" in f["fact"]
                                          for f in a.supporting_factors)

    def test_unconfirmed_absence_mentioned_as_risk(self):
        p = TemplateProvider()
        payload = make_payload()
        a, c = p.analyse(payload, list(payload["sources"]))
        all_risks = [f["fact"] for f in a.risk_factors] + \
                    [f["fact"] for f in c.risk_factors]
        assert any("non confirmée" in r for r in all_risks)

    def test_single_source_flagged_by_critic(self):
        p = TemplateProvider()
        payload = make_payload(providers={"p": {"id": "1"}})
        _, c = p.analyse(payload, list(payload["sources"]))
        assert any("seul fournisseur" in f["fact"]
                   for f in c.risk_factors)

    def test_engine_status_forwarded(self):
        p = TemplateProvider()
        payload = make_payload(engine_status="exclude")
        a, c = p.analyse(payload, list(payload["sources"]))
        assert a.recommended_action == "exclude"
        assert c.recommended_action == "exclude"

    def test_french_months(self):
        p = TemplateProvider()
        payload = make_payload()
        a, _ = p.analyse(payload, list(payload["sources"]))
        assert "septembre" in a.summary

    def test_works_with_minimal_payload(self):
        p = TemplateProvider()
        payload = make_payload()
        payload["standings"] = {}
        payload["availability"] = []
        a, c = p.analyse(payload, list(payload["sources"]))
        assert a.summary
        assert c.summary
