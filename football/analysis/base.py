"""Contrat de la couche IA.

L'IA reçoit uniquement un objet JSON construit par le code (probabilités,
cotes horodatées, formes, classements, absences, sources). Elle renvoie
deux passages (analyste + contradicteur) qui doivent valider un schéma
strict. L'IA ne crée ni ne modifie de valeur numérique.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

VALID_ACTIONS = ("eligible", "watch", "exclude")


class AnalysisError(Exception):
    """Échec de l'IA (quota, réseau, JSON invalide) → repli gabarit."""


@dataclass
class AiPart:
    summary: str
    supporting_factors: list[dict[str, str]] = field(default_factory=list)
    risk_factors: list[dict[str, str]] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    recommended_action: str = "watch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "supporting_factors": list(self.supporting_factors),
            "risk_factors": list(self.risk_factors),
            "unknowns": list(self.unknowns),
            "recommended_action": self.recommended_action,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AiPart":
        return cls(
            summary=str(d.get("summary", "")),
            supporting_factors=[
                {"fact": str(f.get("fact", "")), "source_ref": str(f.get("source_ref", ""))}
                for f in d.get("supporting_factors", []) or []
            ],
            risk_factors=[
                {"fact": str(f.get("fact", "")), "source_ref": str(f.get("source_ref", ""))}
                for f in d.get("risk_factors", []) or []
            ],
            unknowns=[str(u) for u in d.get("unknowns", []) or []],
            recommended_action=str(d.get("recommended_action", "watch")),
        )

    def texts(self) -> list[str]:
        parts = [self.summary]
        parts += [f["fact"] for f in self.supporting_factors]
        parts += [f["fact"] for f in self.risk_factors]
        parts += list(self.unknowns)
        return parts

    def all_source_refs(self) -> list[str]:
        refs = [f["source_ref"] for f in self.supporting_factors]
        refs += [f["source_ref"] for f in self.risk_factors]
        return refs


class AnalysisProvider(Protocol):
    mode: str

    def analyse(self, payload: dict[str, Any],
                source_ids: list[str]) -> tuple[AiPart, AiPart]:
        """Renvoie (analyste, contradicteur). Lève AnalysisError en cas
        d'échec : le pipeline bascule alors sur le gabarit déterministe."""
        ...


def build_analysis_provider(mode: str, *, gemini_key: str = "",
                            gemini_model: str = "gemini-2.0-flash",
                            enable_web_search: bool = False,
                            ollama_base_url: str = "http://localhost:11434",
                            ollama_model: str = "llama3.1",
                            transport=None) -> AnalysisProvider:
    from .templates import TemplateProvider

    if mode == "off":
        return TemplateProvider()
    if mode == "gemini_free":
        from .gemini import GeminiProvider

        return GeminiProvider(gemini_key, gemini_model, transport,
                              enable_web_search=enable_web_search)
    if mode == "ollama_local":
        from .ollama import OllamaProvider

        return OllamaProvider(ollama_base_url, ollama_model, transport)
    raise AnalysisError(f"mode IA inconnu : {mode!r}")
