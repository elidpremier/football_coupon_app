"""Tests : sélection individuelle — règles dures et grille de score."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from football.analysis.base import AiPart
from football.features import FeatureBundle
from football.normalize import make_fixture_key
from football.probabilities import MARKET_MODEL_VERSION
from football.quality import QualityResult
from football.selection import Selection, evaluate_fixture
from football.utils import to_decimal

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
KICK = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def make_bundle(odds_1x2=("2.10", "3.40", "3.60"), odds_ou=("1.95", "1.85"),
                age_hours: float = 1.0, two_sources: bool = True,
                with_teams: bool = True) -> FeatureBundle:
    observed = NOW - timedelta(hours=age_hours)
    from football.probabilities import derive_double_chance

    odds = {
        "match_winner": {
            "home": {"odds": to_decimal(odds_1x2[0]), "observed_at": observed,
                     "provider": "p"},
            "draw": {"odds": to_decimal(odds_1x2[1]), "observed_at": observed,
                     "provider": "p"},
            "away": {"odds": to_decimal(odds_1x2[2]), "observed_at": observed,
                     "provider": "p"},
        },
        "over_under_2_5": {
            "over": {"odds": to_decimal(odds_ou[0]), "observed_at": observed,
                     "provider": "p"},
            "under": {"odds": to_decimal(odds_ou[1]), "observed_at": observed,
                      "provider": "p"},
        },
    }
    # double chance dérivée de la 1X2 (même référence marché)
    dc = derive_double_chance({k: to_decimal(v) for k, v in zip(
        ("home", "draw", "away"), odds_1x2)})
    odds["double_chance"] = {
        k: {"odds": v, "observed_at": observed, "provider": "p"}
        for k, v in dc.items()
    }
    # observations croisées (stabilité) : source secondaire à +1 %
    def cross(market: str) -> dict:
        out = {}
        for outcome, info in odds[market].items():
            if two_sources:
                out[outcome] = [info["odds"],
                                to_decimal(round(float(info["odds"]) * 1.01, 2))]
            else:
                out[outcome] = [info["odds"]]
        return out

    odds_values = {m: cross(m) for m in ("match_winner", "double_chance",
                                         "over_under_2_5")}
    providers = {"p": {"kickoff": KICK, "id": "1"}}
    if two_sources:
        providers["s"] = {"kickoff": KICK, "id": "2"}
    teams = {}
    if with_teams:
        teams = {"Arsenal": {"position": 1, "played": 8},
                 "Chelsea": {"position": 2, "played": 8}}
    return FeatureBundle(
        fixture_key=make_fixture_key("premier_league", KICK, "Arsenal", "Chelsea"),
        competition_slug="premier_league",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=KICK,
        status="COLLECTED",
        matching_status="OK",
        odds=odds,
        odds_values=odds_values,
        providers=providers,
        teams=teams,
        availability=[],
        computed_at=NOW,
    )


def good_quality() -> QualityResult:
    return QualityResult(
        score=94.0, freshness=0.83, source_agreement=1.0, odds_coverage=1.0,
        team_data_coverage=1.0, availability_confidence=1.0,
        contradictions=0, exclusions=[], notes=[],
    )


def full_analysis() -> tuple[AiPart, AiPart]:
    mk = lambda: AiPart(  # noqa: E731
        summary="Résumé factuel.",
        supporting_factors=[{"fact": "cote 2.10 observée", "source_ref": "p#odds"}],
        risk_factors=[{"fact": "marge du marché", "source_ref": "p#odds"}],
        unknowns=["compositions"],
        recommended_action="watch",
    )
    return mk(), mk()


class TestHardRules:
    def test_hard_exclusion_wins(self, config):
        q = good_quality()
        q.exclusions = ["horaire divergent entre sources (INCOHERENT)"]
        sel = evaluate_fixture("k", make_bundle(), q, config, now=NOW)
        assert sel.status == "exclude"
        assert any("horaire divergent" in r for r in sel.reasons)

    def test_stale_odds_excluded(self, config):
        # 20 h > max 6 h → exclusion dure
        b = make_bundle(age_hours=20)
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        assert sel.status == "exclude"

    def test_below_threshold_probability_never_selected(self, config):
        """Invariant fondamental : la sélection retenue a toujours une
        probabilité (référence marché) ≥ le seuil configuré."""
        b = make_bundle(odds_1x2=("2.50", "3.40", "2.60"),
                        odds_ou=("2.10", "1.80"))
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        if sel.outcome:
            assert sel.probability >= config.quality.min_selection_probability

    def test_invalid_odds_market_never_selected(self, config):
        """Un marché dont les cotes sont invalides (≤ 1) n'est jamais
        retenu, quel que soit le reste du bundle."""
        b = make_bundle()
        b.odds["match_winner"]["home"]["odds"] = Decimal("1.00")
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        if sel.outcome:
            assert sel.market != "match_winner"


class TestGrid:
    def test_eligible_with_full_data_and_explanation(self, config):
        b = make_bundle()
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        analyst, critic = full_analysis()
        sel = evaluate_fixture("k", b, q, config, analyst=analyst,
                               critic=critic, analysis_valid=True, now=NOW)
        assert sel.status == "eligible"
        assert sel.score >= config.quality.eligible_threshold

    def test_template_mode_lower_than_ai_validated(self, config):
        b = make_bundle()
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        analyst, critic = full_analysis()
        with_ai = evaluate_fixture("k", b, q, config, analyst=analyst,
                                   critic=critic, analysis_valid=True, now=NOW)
        without_ai = evaluate_fixture("k", b, q, config, analyst=analyst,
                                      critic=critic, analysis_valid=False,
                                      now=NOW)
        assert with_ai.score > without_ai.score
        delta = with_ai.score - without_ai.score
        assert delta == pytest.approx(15 * (1.0 - 0.90), abs=0.01)

    def test_incomplete_explanation_penalised(self, config):
        b = make_bundle()
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        partial = AiPart(summary="Résumé.", supporting_factors=[],
                         risk_factors=[{"fact": "r", "source_ref": "p#odds"}],
                         unknowns=[], recommended_action="watch")
        good, good_c = full_analysis()
        s_partial = evaluate_fixture("k", b, q, config, analyst=partial,
                                     critic=good_c, analysis_valid=True,
                                     now=NOW)
        s_good = evaluate_fixture("k", b, q, config, analyst=good,
                                  critic=good_c, analysis_valid=True,
                                  now=NOW)
        assert s_partial.score < s_good.score
        assert any("incomplets" in r for r in s_partial.reasons)

    def test_one_selection_per_fixture(self, config):
        b = make_bundle()
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        assert sel is not None
        # même match appelé deux fois → même marché/issue (idempotence métier)
        sel2 = evaluate_fixture("k", b, q, config, now=NOW)
        assert (sel.market, sel.outcome) == (sel2.market, sel2.outcome)

    def test_probability_strictly_between_0_and_1(self, config):
        b = make_bundle()
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        assert 0.0 < sel.probability < 1.0
        assert sel.model_version == MARKET_MODEL_VERSION

    def test_watch_band(self, config):
        # équipe absente du classement + cotes moyennes → pas éligible
        b = make_bundle(with_teams=False)
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        assert sel.status in {"watch", "exclude"}
        if sel.status == "watch":
            assert config.quality.watch_threshold <= sel.score < \
                config.quality.eligible_threshold

    def test_reasons_documented_when_excluded(self, config):
        b = make_bundle(with_teams=False)
        b.providers = {"p": b.providers["p"]}
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        if sel.status == "exclude":
            assert sel.reasons, "un exclusion doit avoir au moins une raison"

    def test_odds_recorded_with_timestamp(self, config):
        b = make_bundle()
        from football.quality import compute_quality

        q = compute_quality(b, config, NOW)
        sel = evaluate_fixture("k", b, q, config, now=NOW)
        assert sel.odds_observed_at == NOW - timedelta(hours=1)
        assert sel.odds > Decimal("1")
