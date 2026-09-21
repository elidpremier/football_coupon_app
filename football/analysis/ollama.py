"""Mode `ollama_local` : synthèse avec un modèle local (pas de
transmission externe). Dépend du matériel local ; repli gabarit en
cas d'échec.
"""
from __future__ import annotations

import json
from typing import Any

from ..http import HttpTransport, RequestsTransport, TransportError
from .base import AiPart, AnalysisError

_SYSTEM = (
    "Analyste football factuel en français. Réponds UNIQUEMENT en JSON : "
    "{\"analyst\": {summary, supporting_factors:[{fact,source_ref}], "
    "risk_factors:[{fact,source_ref}], unknowns:[str], recommended_action}, "
    "\"critic\": {...mêmes champs...}}. recommended_action ∈ "
    "{eligible, watch, exclude}. N'utilise que les chiffres du JSON fourni. "
    "Interdits : garanti, sûr, infaillible, sans risque. Cite source_ref "
    "(clé du dictionnaire sources) pour chaque fait."
)


class OllamaProvider:
    mode = "ollama_local"

    def __init__(self, base_url: str, model: str,
                 transport: HttpTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport or RequestsTransport(max_retries=1)

    def analyse(self, payload: dict[str, Any],
                source_ids: list[str]) -> tuple[AiPart, AiPart]:
        data = {k: payload[k] for k in
                ("fixture", "kickoff_utc", "odds", "probabilities", "overround",
                 "standings", "availability", "quality", "sources",
                 "engine_status", "providers")
                if k in payload}
        url = f"{self.base_url}/api/chat"
        body = json.dumps({
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content":
                    "Données du match : " + json.dumps(data, ensure_ascii=False, default=str)},
            ],
        }, ensure_ascii=False).encode("utf-8")
        try:
            resp = self.transport.send(
                "POST", url,
                headers={"Content-Type": "application/json"},
                body=body,
                timeout=180.0,
            )
        except Exception as exc:
            # tout échec réseau → repli gabarit (jamais de fuite d'erreur)
            raise AnalysisError(f"Ollama injoignable ({self.base_url}) : {exc}") from exc
        if resp.status >= 400:
            raise AnalysisError(f"Ollama HTTP {resp.status} : {resp.text[:200]}")
        try:
            parsed = json.loads(resp.json()["message"]["content"])
        except Exception as exc:
            raise AnalysisError(f"réponse Ollama ininterprétable : {exc}") from exc
        if not isinstance(parsed, dict) or "analyst" not in parsed or "critic" not in parsed:
            raise AnalysisError("JSON Ollama : clés analyst/critic absentes")
        return AiPart.from_dict(parsed["analyst"]), AiPart.from_dict(parsed["critic"])
