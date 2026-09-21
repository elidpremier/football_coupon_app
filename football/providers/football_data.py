"""Adaptateur football-data.org (offre gratuite, v4).

Rôle : contrôle secondaire (existence, statut, horaire, classement).
10 appels/minute → le RateLimiter applique la fenêtre glissante.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..normalize import normalize_outcome
from ..utils import parse_iso, to_decimal, utcnow
from .base import (
    BaseProvider,
    PFixture,
    POdds,
    PResult,
    PStanding,
    ProviderError,
    competition_to_provider_code,
)

BASE_URL = "https://api.football-data.org/v4"


class FootballDataOrgProvider(BaseProvider):
    name = "football_data"

    def __init__(self, client, token: str):
        self.client = client
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.token}

    def fetch_fixtures(self, day: date, competition_slug: str) -> list[PFixture]:
        code = competition_to_provider_code("football_data", competition_slug)
        url = f"{BASE_URL}/matches"
        logical = f"matches|date={day.isoformat()}|comp={code}"
        params = {"date": day.isoformat(), "competitions": code}
        try:
            payload, _ = self.client.get_json(url, logical_url=logical, params=params,
                                              headers=self._headers())
        except Exception as exc:
            raise ProviderError(f"football_data.matches : {exc}") from exc
        out = []
        for m in ((payload or {}).get("response") or {}).get("matches") or []:
            try:
                home = (m.get("homeTeam") or {}).get("name")
                away = (m.get("awayTeam") or {}).get("name")
                utc_date = m.get("utcDate")
                if not (home and away and utc_date):
                    continue
                out.append(PFixture(
                    provider=self.name,
                    provider_fixture_id=str(m.get("id")),
                    competition_slug=competition_slug,
                    competition_name=(m.get("competition") or {}).get("name", ""),
                    home_team=home,
                    away_team=away,
                    kickoff_utc=parse_iso(utc_date),
                    status=m.get("status", "SCHEDULED"),
                    venue=(m.get("venue") or ""),
                ))
            except (ValueError, TypeError, KeyError) as exc:
                raise ProviderError(f"football_data : match invalide : {exc}") from exc
        return out

    def fetch_odds(self, day: date, competition_slug: str) -> list[POdds]:
        """Les cotes 1X2 sont fournies par le même appel /matches (pas
        d'appel supplémentaire → économie de quota)."""
        out: list[POdds] = []
        for m in ((self._matches_payload(day, competition_slug) or {})
                  .get("response") or {}).get("matches") or []:
            odds = (m.get("odds") or {}).get("win") or {}
            pid = str(m.get("id"))
            for raw, internal in (("home", "home"), ("draw", "draw"), ("away", "away")):
                if odds.get(raw) is not None:
                    try:
                        # L'API ne fournit pas d'horodatage de cote : on
                        # horodate l'instant d'observation (le plus honnête).
                        out.append(POdds(
                            provider=self.name,
                            provider_fixture_id=pid,
                            market="match_winner",
                            outcome=internal,
                            odds=to_decimal(odds[raw]),
                            bookmaker="football-data.org",
                            observed_at_utc=utcnow(),
                        ))
                    except (ValueError, TypeError):
                        continue
        return out

    def _matches_payload(self, day: date, competition_slug: str):
        code = competition_to_provider_code("football_data", competition_slug)
        url = f"{BASE_URL}/matches"
        logical = f"matches|date={day.isoformat()}|comp={code}"
        params = {"date": day.isoformat(), "competitions": code}
        payload, _ = self.client.get_json(url, logical_url=logical, params=params,
                                          headers=self._headers())
        return payload

    def fetch_standings(self, competition_slug: str, season: int) -> list[PStanding]:
        code = competition_to_provider_code("football_data", competition_slug)
        url = f"{BASE_URL}/competitions/{code}/standings"
        logical = f"standings|comp={code}"
        try:
            payload, _ = self.client.get_json(url, logical_url=logical,
                                              headers=self._headers(), ttl_seconds=21600)
        except Exception as exc:
            raise ProviderError(f"football_data.standings : {exc}") from exc
        out: list[PStanding] = []
        for block in (payload or {}).get("response") or []:
            if block.get("type") not in (None, "TOTAL"):
                continue
            entries = []
            for part in block.get("standings") or []:
                # v4 : liste de groupes (listes) ; certains exports : liste plate
                if isinstance(part, list):
                    entries.extend(part)
                else:
                    entries.append(part)
            for t in entries:
                try:
                    name = (t.get("team") or {}).get("name")
                    if not name:
                        continue
                    out.append(PStanding(
                        provider=self.name,
                        competition_slug=competition_slug,
                        team=name,
                        position=int(t.get("ranking", 0)),
                        played=int(t.get("played", 0)),
                        wins=int(t.get("win", 0)),
                        draws=int(t.get("draw", 0)),
                        losses=int(t.get("lose", 0)),
                        goals_for=int(t.get("goalsFor", 0)),
                        goals_against=int(t.get("goalsAgainst", 0)),
                        form=t.get("form") or "",
                    ))
                except (ValueError, TypeError):
                    continue
        return out

    def fetch_results(self, day: date, competition_slug: str) -> list[PResult]:
        out: list[PResult] = []
        for m in ((self._matches_payload(day, competition_slug) or {})
                  .get("response") or {}).get("matches") or []:
            if m.get("status") not in {"FINISHED", "AFTER_EXTRA_TIME",
                                       "AFTER_PENALTIES", "AWARDED"}:
                continue
            score = (m.get("score") or {}).get("fullTime") or {}
            try:
                out.append(PResult(
                    provider=self.name,
                    provider_fixture_id=str(m.get("id")),
                    kickoff_utc=parse_iso(m["utcDate"]),
                    home_team=(m.get("homeTeam") or {}).get("name"),
                    away_team=(m.get("awayTeam") or {}).get("name"),
                    home_score=int(score.get("home", 0)),
                    away_score=int(score.get("away", 0)),
                    status=m.get("status"),
                ))
            except (ValueError, TypeError, KeyError):
                continue
        return out
