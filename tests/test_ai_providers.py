"""Tests : providers Gemini & Ollama (transport factice) + repli gabarit."""
from __future__ import annotations

import json

import pytest

from football.analysis.base import (
    AnalysisError,
    AiPart,
    build_analysis_provider,
)
from football.analysis.ollama import OllamaProvider
from football.analysis.gemini import GeminiProvider
from football.analysis.templates import TemplateProvider
from tests.conftest import FakeTransport

VALID = {
    "analyst": {
        "summary": "Résumé factuel.",
        "supporting_factors": [{"fact": "cote 2.10", "source_ref": "p#odds"}],
        "risk_factors": [{"fact": "marge", "source_ref": "p#odds"}],
        "unknowns": ["compositions"],
        "recommended_action": "watch",
    },
    "critic": {
        "summary": "Contre-arguments.",
        "supporting_factors": [{"fact": "aucune contradiction",
                                "source_ref": "p#odds"}],
        "risk_factors": [{"fact": "données limitées", "source_ref": "p#odds"}],
        "unknowns": [],
        "recommended_action": "watch",
    },
}


def payload():
    return {"fixture": {"home_team": "A", "away_team": "B",
                        "competition_slug": "premier_league", "venue": ""},
            "kickoff_utc": "2026-09-20T18:00:00Z",
            "odds": {}, "probabilities": {}, "overround": {},
            "standings": {}, "availability": [], "quality": {"score": 80.0,
                                                             "notes": []},
            "providers": {"p": {"id": "1"}},
            "sources": {"p#odds": "cotes"}, "engine_status": "watch"}


class TestGemini:
    def test_requires_key(self):
        with pytest.raises(AnalysisError, match="GEMINI_API_KEY"):
            GeminiProvider("", "model")

    def test_success(self):
        def handler(method, url, headers, params, body):
            assert "generateContent" in url
            assert headers["x-goog-api-key"] == "GKEY"
            sent = json.loads(body)
            assert sent["generationConfig"]["responseMimeType"] == "application/json"
            return (200, {"candidates": [{"content": {"parts": [
                {"text": json.dumps(VALID, ensure_ascii=False)}]}}]})

        p = GeminiProvider("GKEY", "gemini-2.0-flash", FakeTransport(handler))
        a, c = p.analyse(payload(), ["p#odds"])
        assert a.recommended_action == "watch"
        assert c.summary

    def test_web_search_tool_is_opt_in(self):
        def handler(method, url, headers, params, body):
            sent = json.loads(body)
            assert sent["tools"] == [{"google_search": {}}]
            assert "recherche Web est activée" in sent["system_instruction"]["parts"][0]["text"]
            return (200, {"candidates": [{"content": {"parts": [
                {"text": json.dumps(VALID, ensure_ascii=False)}]}}]})

        p = GeminiProvider("GKEY", "gemini-2.0-flash", FakeTransport(handler),
                           enable_web_search=True)
        p.analyse(payload(), ["p#odds", "web_search"])

    def test_http_error(self):
        def handler(method, url, headers, params, body):
            return (429, {"error": {"message": "quota"}})

        p = GeminiProvider("GKEY", "m", FakeTransport(handler))
        with pytest.raises(AnalysisError, match="429"):
            p.analyse(payload(), ["p#odds"])

    def test_unparseable_response(self):
        def handler(method, url, headers, params, body):
            return (200, {"candidates": [{"content": {"parts": [
                {"text": "ceci n'est pas du json"}]}}]})

        p = GeminiProvider("GKEY", "m", FakeTransport(handler))
        with pytest.raises(AnalysisError, match="ininterprétable"):
            p.analyse(payload(), ["p#odds"])

    def test_missing_keys(self):
        def handler(method, url, headers, params, body):
            return (200, {"candidates": [{"content": {"parts": [
                {"text": json.dumps({"analyst": VALID["analyst"]})}]}}]})

        p = GeminiProvider("GKEY", "m", FakeTransport(handler))
        with pytest.raises(AnalysisError, match="critic"):
            p.analyse(payload(), ["p#odds"])

    def test_network_error(self):
        def handler(method, url, headers, params, body):
            raise ConnectionError("down")

        p = GeminiProvider("GKEY", "m", FakeTransport(handler))
        with pytest.raises(AnalysisError, match="injoignable"):
            p.analyse(payload(), ["p#odds"])

    def test_no_sensitive_data_in_prompt(self):
        """Le prompt contient les données du match mais jamais de secret
        (le token n'est que dans l'en-tête)."""
        captured = {}

        def handler(method, url, headers, params, body):
            captured["body"] = body
            return (200, {"candidates": [{"content": {"parts": [
                {"text": json.dumps(VALID)}]}}]})

        p = GeminiProvider("GKEY-SECRET-TOKEN", "m", FakeTransport(handler))
        p.analyse(payload(), ["p#odds"])
        assert b"GKEY-SECRET-TOKEN" not in captured["body"]
        assert captured["body"].decode("utf-8").count("home_team") >= 1


class TestOllama:
    def test_success(self):
        def handler(method, url, headers, params, body):
            assert url == "http://localhost:11434/api/chat"
            sent = json.loads(body)
            assert sent["format"] == "json"
            assert sent["model"] == "llama3.1"
            return (200, {"message": {"content": json.dumps(VALID)}})

        p = OllamaProvider("http://localhost:11434", "llama3.1",
                           FakeTransport(handler))
        a, c = p.analyse(payload(), ["p#odds"])
        assert a.supporting_factors[0]["source_ref"] == "p#odds"

    def test_unreachable(self):
        def handler(method, url, headers, params, body):
            raise ConnectionError("refusé")

        p = OllamaProvider("http://localhost:11434", "m", FakeTransport(handler))
        with pytest.raises(AnalysisError, match="injoignable"):
            p.analyse(payload(), ["p#odds"])

    def test_bad_shape(self):
        def handler(method, url, headers, params, body):
            return (200, {"message": {"content": json.dumps({"x": 1})}})

        p = OllamaProvider("http://localhost:11434", "m", FakeTransport(handler))
        with pytest.raises(AnalysisError):
            p.analyse(payload(), ["p#odds"])


class TestBuilder:
    def test_off_mode(self):
        p = build_analysis_provider("off")
        assert isinstance(p, TemplateProvider)

    def test_unknown_mode(self):
        with pytest.raises(AnalysisError):
            build_analysis_provider("magic")

    def test_gemini_mode(self):
        p = build_analysis_provider("gemini_free", gemini_key="k")
        assert isinstance(p, GeminiProvider)

    def test_ollama_mode(self):
        p = build_analysis_provider("ollama_local")
        assert isinstance(p, OllamaProvider)


class TestFallbackBehavior:
    def _simulate_pipeline_fallback(self, payload, source_ids, allow):
        """Comporte le pipeline : IA → échec → gabarit → validation."""
        from football.analysis.validator import validate_ai_parts

        ai = OllamaProvider("http://localhost:11434", "m",
                            FakeTransport(lambda *a: ConnectionError("down")))
        try:
            analyst, critic = ai.analyse(payload, source_ids)
            valid = not validate_ai_parts(analyst.to_dict(), critic.to_dict(),
                                          source_ids, allow,
                                          engine_ceiling="eligible")
        except AnalysisError:
            tpl = TemplateProvider()
            analyst, critic = tpl.analyse(payload, source_ids)
            valid = not validate_ai_parts(analyst.to_dict(), critic.to_dict(),
                                          source_ids, allow,
                                          engine_ceiling="eligible")
        return analyst, critic, valid

    def test_fallback_to_template_on_ai_failure(self):
        p = payload()
        # même allowlist que le pipeline (dont 100 : dénominateur /100)
        allow = [2.10, 3.40, 3.60, 0.4543, 80.0, 20, 9, 2026, 18, 0, 100]
        a, c, valid = self._simulate_pipeline_fallback(
            p, list(p["sources"]), allow)
        assert valid is True  # le gabarit passe le validateur
        assert a.recommended_action == p["engine_status"]
