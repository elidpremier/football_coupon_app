"""Métriques de performance calculées sur l'historique archivé.

Aucune garantie de résultat : ces chiffres mesurent la qualité réelle
du système (calibration, Brier, log loss, réussite par marché).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CalibRecord:
    predicted_probability: float
    observed: int  # 0 ou 1



def _pairs(records: Sequence) -> list[tuple[float, int]]:
    """Normalise les enregistrements : CalibRecord ou paire (p, o)."""
    out: list[tuple[float, int]] = []
    for r in records:
        if isinstance(r, CalibRecord):
            out.append((r.predicted_probability, r.observed))
        else:
            p, o = r
            out.append((float(p), int(o)))
    return out


def _check(p: float, o: int) -> None:
    if not (0.0 < p < 1.0):
        raise ValueError(f"probabilité hors bornes strictes : {p}")
    if o not in (0, 1):
        raise ValueError(f"observé invalide : {o}")


def brier_score(records: Sequence) -> float | None:
    pairs = _pairs(records)
    if not pairs:
        return None
    for p, o in pairs:
        _check(p, o)
    return float(sum((p - o) ** 2 for p, o in pairs) / len(pairs))


def log_loss(records: Sequence, eps: float = 1e-12) -> float | None:
    pairs = _pairs(records)
    if not pairs:
        return None
    total = 0.0
    for p, o in pairs:
        _check(p, o)
        q = min(max(p, eps), 1 - eps)
        total += -(math.log(q) if o == 1 else math.log(1 - q))
    return float(total / len(pairs))


def hit_rate(records: Sequence) -> float | None:
    pairs = _pairs(records)
    if not pairs:
        return None
    hits = sum(1 for p, o in pairs if (o == 1 and p >= 0.5) or (o == 0 and p < 0.5))
    return hits / len(pairs)


def calibration_bins(records: Sequence[CalibRecord],
                     bins: int = 10) -> list[dict]:
    """Courbe de calibration : par tranche de probabilité prédite, la
    fréquence observée. Vide si pas de données."""
    pairs = _pairs(records)
    if not pairs:
        return []
    out: list[dict] = []
    for b in range(bins):
        lo = b / bins
        hi = (b + 1) / bins
        in_bin = [
            (p, o) for p, o in pairs
            if (lo <= p < hi) or (b == bins - 1 and p == hi)
        ]
        if not in_bin:
            out.append({"bin_lo": lo, "bin_hi": hi, "n": 0, "avg_p": None, "hit_rate": None})
            continue
        avg_p = sum(p for p, _ in in_bin) / len(in_bin)
        freq = sum(o for _, o in in_bin) / len(in_bin)
        out.append({"bin_lo": lo, "bin_hi": hi, "n": len(in_bin),
                    "avg_p": avg_p, "hit_rate": freq})
    return out


def per_market(records_by_market: dict[str, Sequence]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for market, recs in records_by_market.items():
        out[market] = {
            "n": len(recs),
            "brier": brier_score(recs),
            "log_loss": log_loss(recs),
            "hit_rate": hit_rate(recs),
        }
    return out


def summarize(records: Sequence[CalibRecord]) -> dict:
    return {
        "n": len(records),
        "brier": brier_score(records),
        "log_loss": log_loss(records),
        "hit_rate": hit_rate(records),
        "calibration": calibration_bins(records),
    }
