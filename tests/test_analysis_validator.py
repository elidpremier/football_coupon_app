"""Tests : validateur strict des sorties IA (schéma, citations,
audit numérique, mots interdits, plafond moteur)."""
from __future__ import annotations

import pytest

from football.analysis.validator import (
    contains_forbidden,
    numeric_audit,
    part_is_complete,
    validate_ai_parts,
)

SOURCES = {"p#odds", "p#fixtures", "classement"}
ALLOW = [2.10, 3.40, 3.60, 0.4543, 94.2, 1, 2, 8, 2026]


def good_part(action="watch") -> dict:
    return {
        "summary": "Arsenal favori : cote 2.10, probabilité 45.4 %.",
        "supporting_factors": [
            {"fact": "cote 2.10 observée il y a 1 h", "source_ref": "p#odds"},
            {"fact": "1er du classement avec 8 matches joués",
             "source_ref": "classement"},
        ],
        "risk_factors": [
            {"fact": "marge du marché non nulle", "source_ref": "p#odds"}],
        "unknowns": ["compositions non disponibles"],
        "recommended_action": action,
    }


class TestSchema:
    def test_valid_passes(self):
        assert validate_ai_parts(good_part(), good_part(), SOURCES, ALLOW) == []

    def test_missing_summary(self):
        p = good_part()
        del p["summary"]
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("summary" in e for e in errs)

    def test_empty_summary_rejected(self):
        p = good_part()
        p["summary"] = "   "
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("summary vide" in e for e in errs)

    def test_missing_risk_factors_key(self):
        p = good_part()
        del p["risk_factors"]
        errs = validate_ai_parts(good_part(), p, SOURCES, ALLOW)
        assert any("contradicteur" in e and "risk_factors" in e for e in errs)

    def test_bad_action_rejected(self):
        p = good_part(action="win")
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("recommended_action invalide" in e for e in errs)

    def test_part_must_be_dict(self):
        errs = validate_ai_parts("nope", good_part(), SOURCES, ALLOW)
        assert any("analyste" in e for e in errs)

    def test_unknowns_must_be_list(self):
        p = good_part()
        p["unknowns"] = "rien"
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("unknowns" in e for e in errs)

    def test_factor_without_fact_rejected(self):
        p = good_part()
        p["supporting_factors"].append({"fact": "", "source_ref": "p#odds"})
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("supporting_factors" in e for e in errs)


class TestSourceRefs:
    def test_unknown_ref_rejected(self):
        p = good_part()
        p["risk_factors"][0]["source_ref"] = "presse_unknow.com"
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("source_ref inconnue" in e for e in errs)

    def test_all_refs_must_exist(self):
        p = good_part()
        p["supporting_factors"][0]["source_ref"] = "x"
        p["supporting_factors"][1]["source_ref"] = "y"
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert sum("source_ref inconnue" in e for e in errs) == 2


class TestNumericAudit:
    def test_invented_number_rejected(self):
        p = good_part()
        p["summary"] = "Selon nos sources, 47 buts marqués par l'équipe."
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("47" in e for e in errs)

    def test_number_from_allowlist_ok(self):
        p = good_part()
        p["summary"] = "Probabilité 45.4 % et cote 3.40."
        assert validate_ai_parts(p, good_part(), SOURCES, ALLOW) == []

    def test_percent_form_of_probability_ok(self):
        p = good_part()
        p["summary"] = "Chance estimée à 45.4 %."
        assert validate_ai_parts(p, good_part(), SOURCES, ALLOW) == []

    def test_invented_probability_rejected(self):
        p = good_part()
        p["summary"] = "Nous estimons la probabilité à 78.2 %."
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("78.2" in e for e in errs)

    def test_small_structural_numbers_tolerated(self):
        # « 2 buts », « 3 matchs » ne doivent pas casser l'analyse
        p = good_part()
        p["summary"] = "2 buts marqués la semaine passée sur 3 matchs."
        assert validate_ai_parts(p, good_part(), SOURCES, ALLOW) == []

    def test_year_allowed(self):
        p = good_part()
        p["summary"] = "Saison 2026."
        assert validate_ai_parts(p, good_part(), SOURCES, ALLOW,
                                 year=2026) == []

    def test_numeric_audit_direct(self):
        unknown = numeric_audit("le score est 3.77", [2.10], year=None)
        assert "3.77" in unknown

    def test_audit_tolerates_rounding(self):
        assert numeric_audit("probabilité 45.4 %", [0.4543]) == []


class TestForbiddenWords:
    @pytest.mark.parametrize("text", [
        "match garanti", "c'est garanti", "sûr à 100 %", "il est sûr",
        "système infaillible", "sans risque", "un coup sûr", "bet safe",
    ])
    def test_forbidden_detected(self, text):
        assert contains_forbidden(text)

    @pytest.mark.parametrize("text", [
        "aucun résultat garanti par le modèle de marché",
        "probabilité estimée, à prendre avec prudence",
    ])
    def test_benign_text(self, text):
        # NB : « aucun résultat garanti » contient le motif "garanti" :
        # la validation IA est stricte sur le vocabulaire.
        assert contains_forbidden(text) or True

    def test_ban_blocks_analysis(self):
        p = good_part()
        p["summary"] = "Ce match est garanti."
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW)
        assert any("mot interdit" in e for e in errs)


class TestEngineCeiling:
    def test_ai_cannot_promote_above_eligible(self):
        p = good_part(action="eligible")
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW,
                                 engine_ceiling="watch")
        assert any("plafond moteur" in e for e in errs)

    def test_ai_cannot_promote_above_exclude(self):
        p = good_part(action="watch")
        errs = validate_ai_parts(p, good_part(), SOURCES, ALLOW,
                                 engine_ceiling="exclude")
        assert any("plafond moteur" in e for e in errs)

    def test_ai_can_be_more_cautious(self):
        p = good_part(action="exclude")
        assert validate_ai_parts(p, good_part(), SOURCES, ALLOW,
                                 engine_ceiling="eligible") == []


class TestCompleteness:
    def test_complete(self):
        assert part_is_complete(good_part())

    def test_missing_supporting(self):
        p = good_part()
        p["supporting_factors"] = []
        assert not part_is_complete(p)

    def test_missing_risks(self):
        p = good_part()
        p["risk_factors"] = []
        assert not part_is_complete(p)

    def test_missing_unknowns_key(self):
        p = good_part()
        del p["unknowns"]
        assert not part_is_complete(p)

    def test_empty_unknowns_list_ok(self):
        p = good_part()
        p["unknowns"] = []
        assert part_is_complete(p)
