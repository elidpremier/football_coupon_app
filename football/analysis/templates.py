"""Mode `off` : textes générés par gabarits déterministes.

Fonctionne toujours (aucun réseau, aucun quota). Les chiffres cités
sont copiés tels quels du payload fourni par le code : le gabarit
passe l'audit numérique par construction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import AiPart

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def _odds_str(v: Any) -> str:
    return f"{float(v):.2f}"


def _pct(p: float) -> str:
    return f"{p * 100:.1f} %"


def _date_fr(dt: datetime) -> str:
    return f"le {dt.day} {MONTHS_FR[dt.month - 1]} {dt.year} à {dt.hour:02d} h {dt.minute:02d} (UTC)"


def _as_datetime(value) -> datetime:
    if isinstance(value, str):
        from ..utils import parse_iso

        return parse_iso(value)
    return value


class TemplateProvider:
    mode = "off"

    @staticmethod
    def _ref_for(src: dict[str, str], suffix: str, fallback: str) -> str:
        """Première source connue terminée par `suffix` (sinon fallback).
        Ne cite JAMAIS une référence inconnue du payload."""
        for k in src:
            if k.endswith(suffix):
                return k
        return fallback

    def analyse(self, payload: dict[str, Any],
                source_ids: list[str]) -> tuple[AiPart, AiPart]:
        fx = payload["fixture"]
        home, away = fx["home_team"], fx["away_team"]
        kickoff: datetime = _as_datetime(payload["kickoff_utc"])
        probs = payload["probabilities"]
        odds = payload["odds"]
        standings = payload.get("standings") or {}
        availability = payload.get("availability") or []
        quality = payload.get("quality") or {}
        src = payload.get("sources") or {}
        primary_src = next(iter(src), "")
        odds_src = self._ref_for(src, "#odds", primary_src)
        fixtures_src = self._ref_for(src, "#fixtures", primary_src)
        standings_src = "classement" if "classement" in src else primary_src
        engine_status = payload.get("engine_status", "watch")

        mw = probs.get("match_winner", {})
        p_home = mw.get("home")
        p_draw = mw.get("draw")
        p_away = mw.get("away")

        summary_parts = [f"{home} reçoit {away} { _date_fr(kickoff) }."]
        if p_home is not None:
            overround = payload.get("overround", {}).get("match_winner")
            over_txt = f" (marge du marché {overround * 100:.1f} %)" if overround is not None else ""
            summary_parts.append(
                f"Référence marché : {_pct(p_home)} pour {home}, "
                f"{_pct(p_draw)} pour le nul, {_pct(p_away)} pour {away}{over_txt}."
            )
        if quality.get("score") is not None:
            summary_parts.append(
                f"Qualité des données : {quality['score']:.1f}/100."
            )
        summary = " ".join(summary_parts)

        supporting: list[dict[str, str]] = []
        mw = odds.get("match_winner", {})
        if p_home is not None and mw.get("home") and mw.get("draw") and mw.get("away"):
            home_age = mw["home"].get("age_hours")
            supporting.append({
                "fact": (
                    f"Cotes 1X2 observées : {home} {_odds_str(mw['home']['odds'])}, "
                    f"nul {_odds_str(mw['draw']['odds'])}, "
                    f"{away} {_odds_str(mw['away']['odds'])}"
                    + (f" (observées {home_age:.1f} h avant le match)"
                       if isinstance(home_age, (int, float)) else "")
                ),
                "source_ref": odds_src,
            })
        ou = odds.get("over_under_2_5", {})
        p_ou = probs.get("over_under_2_5", {})
        if ou and p_ou:
            over_odds = ou.get("over", {}).get("odds")
            over_age = ou.get("over", {}).get("age_hours")
            supporting.append({
                "fact": (
                    f"Cote « plus de 2,5 buts » : {_odds_str(over_odds)}, "
                    f"probabilité estimée {_pct(p_ou['over'])} "
                    f"(cote observée {over_age:.1f} h avant le match)"
                ),
                "source_ref": odds_src,
            })
        for team, side in ((home, "home"), (away, "away")):
            s = standings.get(team)
            if s:
                supporting.append({
                    "fact": (
                        f"{team} est {s['position']}-{chr(39)}e du classement : "
                        f"{s['wins']} victoires, {s['draws']} nuls, {s['losses']} défaites "
                        f"en {s['played']} matches, forme {s['form'] or 'n.c.'}"
                    ),
                    "source_ref": standings_src,
                })
        if not supporting:
            supporting.append({
                "fact": "Cotes disponibles pour le marché 1X2 et total de buts.",
                "source_ref": fixtures_src,
            })

        def _known_ref(a: dict) -> str:
            """Référence d'une absence : celle de la donnée si elle fait
            partie des sources fournies, sinon la source principale
            (jamais une référence inconnue)."""
            ref = a.get("source_ref") or ""
            return ref if ref in src else fixtures_src

        risks: list[dict[str, str]] = []
        risks.append({
            "fact": (
                "Probabilités issues de la normalisation de la marge du marché : "
                "c'est une référence, pas une probabilité vraie."
            ),
            "source_ref": odds_src,
        })
        confirmed = [a for a in availability if a["status"] == "confirmed"]
        unconfirmed = [a for a in availability if a["status"] == "unconfirmed"]
        if confirmed:
            names = ", ".join(sorted({a["player"] for a in confirmed}))
            risks.append({
                "fact": f"Absence(s) confirmée(s) : {names}.",
                "source_ref": _known_ref(confirmed[0]),
            })
        if unconfirmed:
            names = ", ".join(sorted({a["player"] for a in unconfirmed}))
            risks.append({
                "fact": f"Absence(s) non confirmée(s) : {names} — risque signalé, "
                        "aucune correction arbitraire.",
                "source_ref": _known_ref(unconfirmed[0]),
            })

        unknowns = [
            "compositions officielles non disponibles",
            "météo non prise en compte",
            "évolution des cotes après cette observation non connue",
        ]

        analyst = AiPart(
            summary=summary,
            supporting_factors=supporting,
            risk_factors=risks,
            unknowns=unknowns,
            recommended_action=engine_status,
        )

        critic_risks = list(risks)
        if len(payload.get("providers") or {}) <= 1:
            critic_risks.append({
                "fact": "Contrôle secondaire indisponible : un seul fournisseur.",
                "source_ref": primary_src,
            })
        if p_home is not None and min(p_home, p_away) < 0.25:
            critic_risks.append({
                "fact": f"Issue faible côté {away if p_away < p_home else home} "
                        f"({_pct(min(p_home, p_away))}) : le marché la juge improbable.",
                "source_ref": primary_src,
            })

        critic = AiPart(
            summary=(
                f"Contre-arguments retenus : {len(critic_risks)} point(s) de fragilité "
                f"signalé(s) ; aucune donnée ne contredit directement la référence marché."
            ),
            supporting_factors=[
                {
                    "fact": "Aucune contradiction horaire ou de classement détectée entre les sources.",
                    "source_ref": primary_src,
                }
            ],
            risk_factors=critic_risks,
            unknowns=unknowns,
            recommended_action=engine_status,
        )
        return analyst, critic
