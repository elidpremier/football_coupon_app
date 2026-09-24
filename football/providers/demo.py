"""Fournisseur DÉMO : données déterministes, sans réseau.

Utilisation :
- mode sans clé API : l'application reste utilisable de bout en bout ;
- tests : jour et horloge explicites → sorties 100 % reproductibles.

La table est ancrée sur un « jour démo » : aujourd'hui si 21 h UTC est
encore à venir, sinon demain — les matchs sont donc toujours « à venir ».
Deux variantes (primary/secondary) simulent un contrôle croisé :
légères différences de cotes, et une divergence d'horaire volontaire
sur un match (pour exercer l'exclusion INCOHERENT).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from ..probabilities import derive_double_chance
from ..utils import to_decimal, utcnow
from .base import BaseProvider, PAvailability, PFixture, POdds, PResult, PStanding

COMPETITION = "premier_league"
SEASON_ANCHOR = 2026

# (heure UTC, domicile, extérieur, cotes 1X2 primaire, cotes 1X2 secondaire,
#  OU25 primaire [over, under], OU25 secondaire, index)
_FIXTURES: list[tuple] = [
    (time(18, 0), "Arsenal", "Chelsea",
     (2.10, 3.40, 3.60), (2.12, 3.45, 3.55), (1.95, 1.85), (1.97, 1.83), 0),
    (time(18, 30), "Manchester City", "Liverpool",
     (2.30, 3.80, 2.80), (2.28, 3.85, 2.85), (1.80, 2.05), (1.82, 2.03), 1),
    (time(20, 0), "Tottenham", "Newcastle",
     (2.60, 3.50, 2.60), (2.58, 3.52, 2.62), (1.90, 1.90), (1.88, 1.92), 2),
    (time(20, 0), "Manchester United", "Brighton",
     (1.90, 3.90, 4.20), (1.92, 3.85, 4.10), (1.75, 2.10), (1.77, 2.08), 3),
    (time(20, 15), "Aston Villa", "West Ham",
     (1.85, 3.80, 4.50), (1.85, 3.85, 4.40), (1.80, 2.05), (1.78, 2.07), 4),
    (time(20, 30), "Everton", "Fulham",
     (2.20, 3.30, 3.50), (2.18, 3.35, 3.55), (1.90, 1.95), (1.92, 1.93), 5),
    # Divergence d'horaire volontaire (16:00 vs 16:15) → INCOHERENT
    (time(16, 0), "Brentford", "Crystal Palace",
     (2.40, 3.40, 3.00), (2.42, 3.42, 3.02), (1.95, 1.85), (1.95, 1.85), 6),
    # Cotes « périmées » (observées il y a 20 h) → pénalité forte
    (time(21, 0), "Wolverhampton", "Southampton",
     (2.00, 3.50, 4.00), (2.05, 3.45, 3.95), (1.90, 1.95), (1.90, 1.95), 7),
]

_STANDINGS: list[tuple] = [
    ("Arsenal", 1, 8, 6, 1, 1, 18, 7, "WWDWW"),
    ("Manchester City", 2, 8, 6, 1, 1, 20, 9, "WWWDW"),
    ("Aston Villa", 3, 8, 5, 2, 1, 15, 8, "WDWWL"),
    ("Liverpool", 4, 8, 5, 1, 2, 16, 10, "LWWWD"),
    ("Chelsea", 5, 8, 4, 2, 2, 14, 9, "DWLWW"),
    ("Manchester United", 6, 8, 4, 1, 3, 12, 11, "WLLWW"),
    ("Tottenham", 7, 8, 3, 3, 2, 13, 12, "DWDWW"),
    ("Newcastle", 8, 8, 3, 2, 3, 12, 12, "LWWDW"),
    ("Everton", 9, 8, 2, 3, 3, 9, 11, "DLWDW"),
    ("Fulham", 10, 8, 2, 2, 4, 8, 12, "LWLDW"),
    ("Brighton", 11, 8, 2, 2, 4, 10, 13, "WLLDW"),
    ("West Ham", 12, 8, 2, 1, 5, 9, 15, "LLWLD"),
    ("Brentford", 13, 8, 2, 1, 5, 10, 14, "LWLLD"),
    ("Crystal Palace", 14, 8, 1, 3, 4, 7, 12, "DLWDW"),
    ("Wolverhampton", 15, 8, 1, 2, 5, 8, 14, "LDLLW"),
    ("Southampton", 16, 8, 1, 1, 6, 6, 16, "LWLLD"),
]

# Résultats déterministes pour la veille (score domicile/extérieur)
_YESTERDAY_RESULTS: dict[int, tuple[int, int]] = {
    0: (2, 1), 1: (1, 1), 2: (0, 2), 3: (3, 0), 4: (1, 0), 5: (2, 2),
    6: (1, 2), 7: (2, 0),
}


def demo_day_for(now: datetime) -> date:
    now = now.astimezone(timezone.utc)
    today = now.date()
    cutoff = datetime.combine(today, time(18, 0), tzinfo=timezone.utc)
    return today if now < cutoff else today + timedelta(days=1)


class DemoProvider(BaseProvider):
    """`name` ∈ {"demo_primary", "demo_secondary"} ; `day` et `now`
    explicites pour la reproductibilité (défaut : valeurs réelles)."""

    def __init__(self, name: str, day: date | None = None,
                 now: datetime | None = None):
        assert name in {"demo_primary", "demo_secondary"}
        self.name = name
        self._now = now or utcnow()
        # jour ancré à la construction (« aujourd'hui » de la collecte)
        self._anchor_day = day or demo_day_for(self._now)

    @property
    def day(self) -> date:
        """Jour collecté comme « à venir » (sans mutation d'état)."""
        return self._anchor_day

    def _is_secondary(self) -> bool:
        return self.name == "demo_secondary"

    def _kickoff(self, idx: int, t: time, day: date) -> datetime:
        kickoff = datetime.combine(day, t, tzinfo=timezone.utc)
        if self._is_secondary() and idx == 6:
            kickoff += timedelta(minutes=15)  # divergence volontaire
        return kickoff

    def _pid(self, idx: int) -> str:
        return f"demo-{self.name[-1]}-{idx}"

    def fetch_fixtures(self, day: date, competition_slug: str) -> list[PFixture]:
        if competition_slug != COMPETITION:
            return []
        out = []
        for idx, (t, home, away, *_rest) in enumerate(_FIXTURES):
            out.append(PFixture(
                provider=self.name,
                provider_fixture_id=self._pid(idx),
                competition_slug=COMPETITION,
                competition_name="Premier League (DÉMO)",
                home_team=home,
                away_team=away,
                kickoff_utc=self._kickoff(idx, t, day),
                status="NS",
                venue="Stade démo",
            ))
        return out

    def _odds_row(self, idx: int, rows: list[tuple]) -> list[POdds]:
        _t, _home, _away, x12a, x12b, oua, oub, _idx = rows[idx]
        x12 = x12b if self._is_secondary() else x12a
        ou = oub if self._is_secondary() else oua
        age = timedelta(hours=20) if idx == 7 else timedelta(hours=1)
        observed = self._now - age
        rows: list[POdds] = []
        for outcome, value in zip(("home", "draw", "away"), x12):
            rows.append(POdds(
                provider=self.name,
                provider_fixture_id=self._pid(idx),
                market="match_winner",
                outcome=outcome,
                odds=to_decimal(value),
                bookmaker=f"demo-bookmaker-{self.name[-1]}",
                observed_at_utc=observed,
            ))
        for outcome, value in zip(("over", "under"), ou):
            rows.append(POdds(
                provider=self.name,
                provider_fixture_id=self._pid(idx),
                market="over_under_2_5",
                outcome=outcome,
                odds=to_decimal(value),
                bookmaker=f"demo-bookmaker-{self.name[-1]}",
                observed_at_utc=observed,
            ))
        # double chance dérivée de la 1X2 (marge 2 %)
        dc = derive_double_chance({
            "home": to_decimal(x12[0]), "draw": to_decimal(x12[1]),
            "away": to_decimal(x12[2]),
        })
        for outcome, value in dc.items():
            rows.append(POdds(
                provider=self.name,
                provider_fixture_id=self._pid(idx),
                market="double_chance",
                outcome=outcome,
                odds=value,
                bookmaker=f"demo-bookmaker-{self.name[-1]}",
                observed_at_utc=observed,
            ))
        return rows

    def fetch_odds(self, day: date, competition_slug: str) -> list[POdds]:
        if competition_slug != COMPETITION:
            return []
        out: list[POdds] = []
        for idx, row in enumerate(_FIXTURES):
            out.extend(self._odds_row(idx, _FIXTURES))
        return out

    def fetch_standings(self, competition_slug: str, season: int) -> list[PStanding]:
        if competition_slug != COMPETITION:
            return []
        return [
            PStanding(
                provider=self.name,
                competition_slug=COMPETITION,
                team=team,
                position=pos,
                played=played, wins=w, draws=d, losses=l,
                goals_for=gf, goals_against=ga,
                form=form,
            )
            for (team, pos, played, w, d, l, gf, ga, form) in _STANDINGS
        ]

    def fetch_results(self, day: date, competition_slug: str) -> list[PResult]:
        """Résultats de la journée `day`."""
        if competition_slug != COMPETITION:
            return []
        if day > self._anchor_day:
            return []
        if day == self._anchor_day:
            end = datetime.combine(self._anchor_day, time(23, 50),
                                   tzinfo=timezone.utc)
            if self._now < end:
                return []
        fixtures = self.fetch_fixtures(day, competition_slug)
        out: list[PResult] = []
        for fx in fixtures:
            idx = int(fx.provider_fixture_id.rsplit("-", 1)[1])
            if idx in _YESTERDAY_RESULTS:
                h, a = _YESTERDAY_RESULTS[idx]
                out.append(PResult(
                    provider=self.name,
                    provider_fixture_id=fx.provider_fixture_id,
                    kickoff_utc=fx.kickoff_utc,
                    home_team=fx.home_team,
                    away_team=fx.away_team,
                    home_score=h,
                    away_score=a,
                    status="FT",
                ))
        return out

    def fetch_availability(self, day: date, competition_slug: str) -> list[PAvailability]:
        if competition_slug != COMPETITION:
            return []
        ref = f"demo#availability#{self._anchor_day.isoformat()}"
        return [
            PAvailability(
                provider=self.name,
                provider_fixture_id=self._pid(0),
                team="Chelsea",
                player="M. Silva",
                reason="injury",
                status="confirmed",
                source_ref=ref,
            ),
            PAvailability(
                provider=self.name,
                provider_fixture_id=self._pid(1),
                team="Liverpool",
                player="J. Turner",
                reason="doubt",
                status="unconfirmed",
                source_ref=ref,
            ),
        ]
