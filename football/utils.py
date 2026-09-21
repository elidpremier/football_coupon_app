"""Utilitaires transversaux : dates UTC, hachage, Decimal, JSON."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    """Horloge de référence : UTC, seconde de précision (stable en base)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_utc(dt: datetime) -> datetime:
    """Rend un datetime conscient du fuseau et converti en UTC.

    Les naifs sont rejetés : c'est une source d'erreurs classiques.
    """
    if dt.tzinfo is None:
        raise ValueError(f"datetime naïf interdit (fuseau obligatoire) : {dt!r}")
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def parse_iso(value: str) -> datetime:
    """Parse une date ISO 8601 (Z ou +00:00)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_utc(dt)


def iso(dt: datetime) -> str:
    return to_utc(dt).strftime(ISO_FMT)


def today_utc(now: datetime | None = None) -> str:
    return to_utc(now or utcnow()).strftime("%Y-%m-%d")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise ValueError(f"valeur décimale invalide : {value!r}") from exc


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def geometric_mean(values: Iterable[float]) -> float:
    vals = [max(1e-9, float(v)) for v in values]
    if not vals:
        return 0.0
    import math

    return math.exp(sum(math.log(v) for v in vals) / len(vals))


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def jload(text: str | None):
    if text is None or text == "":
        return None
    return json.loads(text)


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def extract_numbers(text: str) -> list[Decimal]:
    """Extrait tous les nombres décimaux d'un texte (pour l'audit IA)."""
    out: list[Decimal] = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(Decimal(m.group(0).replace(",", ".")))
        except InvalidOperation:
            continue
    return out
