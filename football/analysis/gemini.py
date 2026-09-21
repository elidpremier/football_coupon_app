"""Mode `gemini_free` : synthèse JSON via l'API Gemini (palier gratuit).

Bonne pratique : aucun champ sensible n'est transmis ; l'IA est un
agrégateur des faits fournis, jamais une source. En cas d'échec →
AnalysisError → repli automatique sur le gabarit.
"""
from __future__ import annotations

import json
from typing import Any

from ..http import HttpTransport, RequestsTransport, TransportError
from .base import AiPart, AnalysisError

SYSTEM_INSTRUCTION = (
    "Tu es un analyste football factuel, en français. Tu ne reçois qu'un "
    "objet JSON de données. Tu dois : 1) expliquer les facteurs favorables, "
    "2) chercher activement les arguments contraires et les données "
    "manquantes, 3) n'utiliser que des chiffres présents dans le JSON, "
    "4) citer source_ref (une clé du dictionnaire 'sources') pour chaque "
    "fait, 5) ne jamais promettre un résultat (interdits : garanti, sûr, "
    "infaillible, sans risque). Réponds UNIQUEMENT avec un objet JSON : "
    "{\"analyst\": {...}, \"critic\": {...}} où chaque partie a les champs "
    "summary, supporting_factors [{fact, source_ref}], risk_factors "
    "[{fact, source_ref}], unknowns [str], recommended_action "
    "(eligible|watch|exclude)."
)

_USER_TEMPLATE = (
    "Données du match (seules sources autorisées) :\n{data}\n\n"
    "Rappelle-toi : tu ne decides pas de l'éligibilité, tu éclaires une "
    "décision humaine. Réponds en JSON uniquement."
)


class GeminiProvider:
    mode = "gemini_free"

    def __init__(self, api_key: str, model: str, transport: HttpTransport | None = None,
                 enable_web_search: bool = False):
        if not api_key:
            raise AnalysisError("GEMINI_API_KEY absente : mode gemini_free indisponible")
        self.api_key = api_key
        self.model = model
        self.transport = transport or RequestsTransport(max_retries=2)
        self.enable_web_search = enable_web_search

    def analyse(self, payload: dict[str, Any],
                source_ids: list[str]) -> tuple[AiPart, AiPart]:
        data = {k: payload[k] for k in
                ("fixture", "kickoff_utc", "odds", "probabilities", "overround",
                 "standings", "availability", "quality", "sources",
                 "engine_status", "providers")
                if k in payload}
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        system_instruction = SYSTEM_INSTRUCTION
        if self.enable_web_search:
            system_instruction += (
                " La recherche Web est activée : elle sert uniquement à "
                "signaler des risques ou inconnues. Cite alors la source "
                "'web_search', mais n'ajoute aucun chiffre ou fait de "
                "décision absent des données reçues."
            )
        request = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{
                "role": "user",
                "parts": [{"text": _USER_TEMPLATE.format(
                    data=json.dumps(data, ensure_ascii=False, default=str))}],
            }],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        if self.enable_web_search:
            # Outil intégré Gemini : aucune clé ni service tiers supplémentaire.
            request["tools"] = [{"google_search": {}}]
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        try:
            resp = self.transport.send(
                "POST", url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                body=body,
                timeout=45.0,
            )
        except Exception as exc:
            # tout échec réseau → repli gabarit (jamais de fuite d'erreur)
            raise AnalysisError(f"Gemini injoignable : {exc}") from exc
        if resp.status >= 400:
            raise AnalysisError(f"Gemini HTTP {resp.status} : {resp.text[:200]}")
        try:
            data_out = resp.json()
            text = data_out["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except Exception as exc:
            raise AnalysisError(f"réponse Gemini ininterprétable : {exc}") from exc
        if not isinstance(parsed, dict) or "analyst" not in parsed or "critic" not in parsed:
            raise AnalysisError("JSON Gemini : clés analyst/critic absentes")
        return AiPart.from_dict(parsed["analyst"]), AiPart.from_dict(parsed["critic"])
