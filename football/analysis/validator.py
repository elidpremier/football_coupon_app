"""Validation stricte des sorties IA.

Un rejet a un coût (repli gabarit) mais jamais une publication :
- schéma JSON exact (champs obligatoires, types) ;
- `recommended_action` ∈ {eligible, watch, exclude} ;
- toute `source_ref` doit exister dans les sources fournies ;
- audit numérique : aucun chiffre hors de l'allowlist fournie
  (cotes, probabilités, classement, formes, dates, petits entiers
  structurels 0..20) ;
- aucun mot interdit (« garanti », « sûr », « infaillible »…) ;
- plafond : l'IA ne peut pas proposer un action plus favorable que
  le statut d'éligibilité calculé par le moteur.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable, Sequence

from ..utils import extract_numbers
from .base import AiPart, VALID_ACTIONS

_FORBIDDEN = [
    r"\bgaranti(?:s)?\b",
    r"\bgarant[ée]e[ds]?\b",
    r"\binfaillible\b",
    r"\bsûr\b", r"\bsurs\b", r"\bsûrs\b",
    r"\bsans\s+risque\b",
    r"\bcoup\s+sûr\b",
    r"\b100\s*%\s+(?:certain|sûr|sûre)\b",
    r"\bbet\s+safe\b",
    r"\bargent\s+sûr\b",
]
_FORBIDDEN_RES = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN]

_ACTION_ORDER = {"exclude": 0, "watch": 1, "eligible": 2}
_ALLOWED_STRUCTURAL_MAX = Decimal(20)  # entiers « structurels » tolérés


def contains_forbidden(text: str) -> list[str]:
    return [r.pattern for r in _FORBIDDEN_RES if r.search(text or "")]


def _allowed_numbers(allowlist: Sequence[float | Decimal],
                     year: int | None = None) -> set[Decimal]:
    allowed: set[Decimal] = set()
    for v in allowlist:
        d = Decimal(str(v))
        allowed.add(d)
        allowed.update({
            (d * 100).quantize(Decimal("0.1")),   # probabilité en % (1 décimale)
            d.quantize(Decimal("0.01")),
            (d * 100).quantize(Decimal("1")),
        })
        if d == d.to_integral_value():
            allowed.add(d)
    for i in range(0, int(_ALLOWED_STRUCTURAL_MAX) + 1):
        allowed.add(Decimal(i))
    # seuil structurel du marché « total de buts » (libellé « 2,5 buts »)
    allowed.add(Decimal("2.5"))
    if year:
        allowed.add(Decimal(year))
    return allowed


def numeric_audit(text: str, allowlist: Sequence[float | Decimal],
                  year: int | None = None) -> list[str]:
    """Retourne les nombres du texte absents de l'allowlist."""
    allowed = _allowed_numbers(allowlist, year)
    unknown = []
    for num in extract_numbers(text):
        if num not in allowed:
            # tolérance de 0.05 sur les décimales (arrondis d'affichage)
            close = any(abs(num - a) <= Decimal("0.05") for a in allowed)
            if not close:
                unknown.append(str(num))
    return unknown


def _check_part(part: dict, errors: list[str], ctx: str, source_ids: set[str],
                allowlist: Sequence[float | Decimal], year: int | None) -> None:
    if not isinstance(part, dict):
        errors.append(f"{ctx} : doit être un objet JSON")
        return
    for key in ("summary", "supporting_factors", "risk_factors",
                "unknowns", "recommended_action"):
        if key not in part:
            errors.append(f"{ctx} : champ manquant '{key}'")
    if not str(part.get("summary", "")).strip():
        errors.append(f"{ctx} : summary vide")
    action = part.get("recommended_action")
    if action not in VALID_ACTIONS:
        errors.append(f"{ctx} : recommended_action invalide : {action!r}")
    for i, f in enumerate(part.get("supporting_factors") or []):
        if not isinstance(f, dict) or not str(f.get("fact", "")).strip():
            errors.append(f"{ctx} : supporting_factors[{i}] sans fact")
        else:
            ref = str(f.get("source_ref", ""))
            if ref and ref not in source_ids:
                errors.append(f"{ctx} : source_ref inconnue {ref!r}")
    for i, f in enumerate(part.get("risk_factors") or []):
        if not isinstance(f, dict) or not str(f.get("fact", "")).strip():
            errors.append(f"{ctx} : risk_factors[{i}] sans fact")
        else:
            ref = str(f.get("source_ref", ""))
            if ref and ref not in source_ids:
                errors.append(f"{ctx} : source_ref inconnue {ref!r}")
    unknowns = part.get("unknowns")
    if unknowns is not None and not isinstance(unknowns, list):
        errors.append(f"{ctx} : unknowns doit être une liste")

    text = " ".join(_part_texts(part))
    for word in contains_forbidden(text):
        errors.append(f"{ctx} : mot interdit détecté (motif : {word})")
    for num in numeric_audit(text, allowlist, year):
        errors.append(f"{ctx} : nombre {num} non issu des données fournies")


def _part_texts(part: dict) -> list[str]:
    texts = [str(part.get("summary", ""))]
    texts += [str(f.get("fact", "")) for f in part.get("supporting_factors") or []]
    texts += [str(f.get("fact", "")) for f in part.get("risk_factors") or []]
    texts += [str(u) for u in part.get("unknowns") or []]
    return texts


def validate_ai_parts(analyst: dict, critic: dict,
                      source_ids: Iterable[str],
                      allowlist: Sequence[float | Decimal],
                      *,
                      engine_ceiling: str = "eligible",
                      year: int | None = None) -> list[str]:
    """Valide analyste + contradicteur. Retourne la liste des erreurs
    (vide = validé). `engine_ceiling` : action maximale que le moteur
    autorise (l'IA ne peut pas la dépasser) ; la chaîne de production
    le passe TOUJOURS explicitement."""
    errors: list[str] = []
    ids = set(source_ids)
    _check_part(analyst, errors, "analyste", ids, allowlist, year)
    _check_part(critic, errors, "contradicteur", ids, allowlist, year)

    action = analyst.get("recommended_action") if isinstance(analyst, dict) else None
    if action in VALID_ACTIONS:
        if _ACTION_ORDER[action] > _ACTION_ORDER[engine_ceiling]:
            errors.append(
                f"analyste : action {action!r} au-dessus du plafond moteur "
                f"{engine_ceiling!r} (l'IA ne décide pas)"
            )
    return errors


def part_is_complete(part: dict) -> bool:
    """Explication complète : au moins un facteur favorable, un risque,
    et les inconnues traitées (liste ou déclaration explicite)."""
    if not isinstance(part, dict):
        return False
    if not str(part.get("summary", "")).strip():
        return False
    if not part.get("supporting_factors"):
        return False
    if not part.get("risk_factors"):
        return False
    unknowns = part.get("unknowns")
    if unknowns is None:
        return False
    if not isinstance(unknowns, list):
        return False
    return True
