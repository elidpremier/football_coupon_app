"""Adaptateur API-Football (offre gratuite, v3).

Endpoints utilisés : fixtures, odds, standings, injuries.
Toutes les réponses passent par l'ApiClient (cache TTL, quota, hash).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..normalize import normalize_market, normalize_outcome
from ..utils import parse_iso, to_decimal
from .base import (
    BaseProvider,
    PAvailability,
    PFixture,
    POdds,
    PResult,
    PStanding,
    ProviderError,
    competition_to_provider_code,
)

BASE_URL = "https://v3.football.api-sports.io"

LEAGUE_NAMES = {
    39: "premier_league", 140: "la_liga", 135: "serie_a", 132: "bundesliga",
    61: "ligue_1", 107: "eredivisie", 2: "champions_league", 14: "europe_league",
    79: "coppa_italia", 131: "premier_league_cup", 94: "primeira_liga",
    40: "championship", 848: "conference_league", 62: "ligue_2",
    136: "serie_b", 141: "segunda_division", 71: "brasileirao",
    253: "mls", 6: "afcon", 30: "can_qualif",
}


class ApiFootballProvider(BaseProvider):
    name = "api_football"

    def __init__(self, client, token: str = ""):
        self.client = client
        self._token = token

    def fetch_fixtures(self, day: date, competition_slug: str) -> list[PFixture]:
        league = competition_to_provider_code("api_football", competition_slug)
        url = f"{BASE_URL}/fixtures"
        logical = f"fixtures|date={day.isoformat()}|league={league}"
        params = {"date": day.isoformat(), "league": league}
        try:
            payload, _ = self.client.get_json(url, logical_url=logical, params=params,
                                              headers={"x-apisports-key": self._token})
        except Exception as exc:
            raise ProviderError(f"api_football.fixtures : {exc}") from exc
        items = (payload or {}).get("response") or []
        out = []
        for it in items:
            try:
                fx = it.get("fixture") or {}
                league_id = (it.get("league") or {}).get("id")
                home = ((it.get("teams") or {}).get("home") or {}).get("name")
                away = ((it.get("teams") or {}).get("away") or {}).get("name")
                date_iso = fx.get("date")
                if not (home and away and date_iso):
                    continue
                out.append(PFixture(
                    provider=self.name,
                    provider_fixture_id=str(fx.get("id")),
                    competition_slug=LEAGUE_NAMES.get(league_id, competition_slug),
                    competition_name=(it.get("league") or {}).get("name", ""),
                    home_team=home,
                    away_team=away,
                    kickoff_utc=parse_iso(date_iso),
                    status=(fx.get("status") or {}).get("short", "NS"),
                    venue=((fx.get("venue") or {}).get("name") or ""),
                ))
            except (ValueError, TypeError, KeyError) as exc:
                raise ProviderError(f"api_football : fixture invalide : {exc}") from exc
        return out

    def fetch_odds(self, day: date, competition_slug: str) -> list[POdds]:
        """Un appel /odds?fixture= par match du jour (budget mesuré)."""
        fixtures = self.fetch_fixtures(day, competition_slug)
        out: list[POdds] = []
        for fx in fixtures:
            url = f"{BASE_URL}/odds"
            logical = f"odds|fixture={fx.provider_fixture_id}"
            params = {"fixture": fx.provider_fixture_id, "live": 0}
            try:
                payload, _ = self.client.get_json(url, logical_url=logical,
                                                  params=params,
                                                  headers={"x-apisports-key": self._token},
                                                  ttl_seconds=1800)
            except Exception as exc:
                raise ProviderError(f"api_football.odds({fx.provider_fixture_id}) : {exc}") from exc
            for entry in (payload or {}).get("response") or []:
                # la réponse peut contenir plusieurs matchs : on ne garde
                # que ceux de l'appel courant
                entry_fid = str(((entry.get("fixture") or {}).get("id"))
                                or fx.provider_fixture_id)
                if entry_fid != fx.provider_fixture_id:
                    continue
                for bm in entry.get("bookmakers") or []:
                    bm_name = bm.get("bookmaker") or bm.get("name") or ""
                    update_time = bm.get("lastUpdateTime") or bm.get("update") or entry.get("update")
                    observed = (parse_iso(update_time)
                                if update_time else datetime.now(timezone.utc))

                    bets = bm.get("bets") or bm.get("markets") or []
                    for bet in bets:
                        bet_name = bet.get("name", "")
                        try:
                            market_internal = normalize_market(bet_name)
                        except ValueError:
                            continue

                        # Format officiel API-Football v3: "values" array
                        values = bet.get("values")
                        if values is not None:
                            for val in values:
                                val_name = str(val.get("value", ""))
                                val_odd = val.get("odd")
                                if not val_odd:
                                    continue
                                try:
                                    outcome_internal = normalize_outcome(market_internal, val_name)
                                except ValueError:
                                    continue
                                try:
                                    out.append(POdds(
                                        provider=self.name,
                                        provider_fixture_id=fx.provider_fixture_id,
                                        market=market_internal,
                                        outcome=outcome_internal,
                                        odds=to_decimal(val_odd),
                                        bookmaker=bm_name,
                                        observed_at_utc=observed,
                                    ))
                                except (ValueError, TypeError) as exc:
                                    raise ProviderError(
                                        f"api_football : cote invalide "
                                        f"{bet_name}/{val_name} : {exc}"
                                    ) from exc

                        # Format test fixture / répliques alternatives
                        obms = bet.get("bookmakers") or []
                        for obm in obms:
                            outcome_raw = str(obm.get("name", ""))
                            mapping = {
                                "match_winner": {"1": "home", "X": "draw", "2": "away", "Home": "home", "Draw": "draw", "Away": "away"},
                                "double_chance": {"1X": "home_draw", "12": "home_away", "X2": "away_draw",
                                                  "Home/Draw": "home_draw", "Home/Away": "home_away", "Draw/Away": "away_draw"},
                                "over_under_2_5": {"Over": "over", "Under": "under"},
                            }.get(market_internal, {})
                            if outcome_raw not in mapping:
                                continue
                            raw_vals = obm.get("123", obm.get("Over/Under"))
                            if isinstance(raw_vals, list):
                                if not raw_vals:
                                    continue
                                value = raw_vals[0]
                            elif isinstance(raw_vals, dict):
                                if outcome_raw not in raw_vals:
                                    continue
                                value = raw_vals[outcome_raw]
                            else:
                                value = raw_vals
                            if value is None:
                                continue
                            last_update = obm.get("lastUpdateTime") or update_time
                            obs_time = (parse_iso(last_update)
                                        if last_update else observed)
                            try:
                                out.append(POdds(
                                    provider=self.name,
                                    provider_fixture_id=fx.provider_fixture_id,
                                    market=market_internal,
                                    outcome=mapping[outcome_raw],
                                    odds=to_decimal(value),
                                    bookmaker=bm_name,
                                    observed_at_utc=obs_time,
                                ))
                            except (ValueError, TypeError) as exc:
                                raise ProviderError(
                                    f"api_football : cote invalide "
                                    f"{bet_name}/{outcome_raw} : {exc}"
                                ) from exc
        return out

    def fetch_standings(self, competition_slug: str, season: int) -> list[PStanding]:
        league = competition_to_provider_code("api_football", competition_slug)
        url = f"{BASE_URL}/standings"
        logical = f"standings|league={league}|season={season}"
        params = {"league": league, "season": season}
        try:
            payload, _ = self.client.get_json(url, logical_url=logical, params=params,
                                              headers={"x-apisports-key": self._token},
                                              ttl_seconds=21600)
        except Exception as exc:
            raise ProviderError(f"api_football.standings : {exc}") from exc
        out: list[PStanding] = []
        for grp in (payload or {}).get("response") or []:
            for group in grp.get("groups") or []:
                # v3 : groups est une liste de listes d'entrées
                for t in group or []:
                    try:
                        name = (t.get("team") or {}).get("name")
                        if not name:
                            continue
                        goals = t.get("goals") or {}
                        out.append(PStanding(
                            provider=self.name,
                            competition_slug=competition_slug,
                            team=name,
                            position=int(t.get("rank", 0)),
                            played=int(t.get("played", 0)),
                            wins=int(t.get("win", 0)),
                            draws=int(t.get("draw", 0)),
                            losses=int(t.get("lose", 0)),
                            goals_for=int(goals.get("for", 0)),
                            goals_against=int(goals.get("against", 0)),
                        ))
                    except (ValueError, TypeError) as exc:
                        raise ProviderError(f"api_football : ligne de classement invalide : {exc}") from exc
        return out

    def fetch_results(self, day: date, competition_slug: str) -> list[PResult]:
        fixtures = self.fetch_fixtures(day, competition_slug)
        out: list[PResult] = []
        for fx in fixtures:
            if fx.status not in {"FT", "AET", "PEN"}:
                continue
            score = self._score(fx.provider_fixture_id)
            if score is None:
                continue
            out.append(PResult(
                provider=self.name,
                provider_fixture_id=fx.provider_fixture_id,
                kickoff_utc=fx.kickoff_utc,
                home_team=fx.home_team,
                away_team=fx.away_team,
                home_score=score[0],
                away_score=score[1],
                status=fx.status,
            ))
        return out

    def _score(self, fixture_id: str) -> tuple[int, int] | None:
        url = f"{BASE_URL}/fixtures"
        logical = f"score|fixture={fixture_id}"
        try:
            payload, _ = self.client.get_json(url, logical_url=logical,
                                              params={"fixture": fixture_id},
                                              headers={"x-apisports-key": self._token},
                                              ttl_seconds=21600)
        except Exception:
            return None
        for it in (payload or {}).get("response") or []:
            score = ((it.get("fixture") or {}).get("goals") or {})
            try:
                return int(score.get("home")), int(score.get("away"))
            except (TypeError, ValueError):
                return None
        return None

    def fetch_availability(self, day: date, competition_slug: str) -> list[PAvailability]:
        fixtures = self.fetch_fixtures(day, competition_slug)
        out: list[PAvailability] = []
        for fx in fixtures:
            url = f"{BASE_URL}/injuries"
            logical = f"injuries|fixture={fx.provider_fixture_id}"
            params = {"fixture": fx.provider_fixture_id}
            try:
                payload, _ = self.client.get_json(url, logical_url=logical, params=params,
                                                  headers={"x-apisports-key": self._token},
                                                  ttl_seconds=3600)
            except Exception as exc:
                raise ProviderError(f"api_football.injuries({fx.provider_fixture_id}) : {exc}") from exc
            out_items = (payload or {}).get("response") or []
            if isinstance(out_items, dict):
                out_items = [out_items]
            for item in out_items:
                # 1) Format officiel API-Football v3 (/injuries?fixture=...)
                player_obj = item.get("player") or {}
                if isinstance(player_obj, dict) and player_obj.get("name"):
                    player_name = player_obj.get("name")
                    team_name = ((item.get("team") or {}).get("name") or fx.home_team)
                    reason = item.get("reason") or "injury"
                    out.append(PAvailability(
                        provider=self.name,
                        provider_fixture_id=fx.provider_fixture_id,
                        team=team_name,
                        player=player_name,
                        reason=str(reason).lower(),
                        status="confirmed",
                        source_ref=f"api_football#injuries#{fx.provider_fixture_id}",
                    ))
                    continue

                # 2) Format structure groupée (home/away)
                fx_data = item.get("fixture") or {}
                for side in ("home", "away"):
                    team_name = ((fx_data.get(side) or {}).get("name")
                                 or (fx.home_team if side == "home" else fx.away_team))
                    data = item.get(side) or {}
                    for list_name, status, reason in (
                        ("injured", "confirmed", "injury"),
                        ("suspected", "unconfirmed", "injury"),
                        ("suspended", "confirmed", "suspension"),
                    ):
                        for p in data.get(list_name) or []:
                            name = (p.get("player") or {}).get("name")
                            if name:
                                out.append(PAvailability(
                                    provider=self.name,
                                    provider_fixture_id=fx.provider_fixture_id,
                                    team=team_name,
                                    player=name,
                                    reason=reason,
                                    status=status,
                                    source_ref=f"api_football#injuries#{fx.provider_fixture_id}",
                                ))
        return out
