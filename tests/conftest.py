"""Fixtures partagées : base temporaire, config du dépôt, horloge gelée,
fournisseurs démo, transport HTTP factice, réponses API enregistrées."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from football.config import Secrets, load_config
from football.http import HttpResponse, TransportError
from football.providers.demo import DemoProvider
from football.storage import Database
from football.utils import utcnow

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Horloge de référence des tests (deterministe)
NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
DAY = date(2026, 9, 20)


class FrozenClock:
    """Horloge mutable partagée (les tests font avancer le temps)."""

    def __init__(self, start: datetime = NOW):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, hours: float = 0, minutes: float = 0) -> None:
        from datetime import timedelta

        self.t = self.t + timedelta(hours=hours, minutes=minutes)

    def set(self, t: datetime) -> None:
        self.t = t


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def config():
    return load_config(ROOT / "config" / "football.yaml")


@pytest.fixture
def secrets() -> Secrets:
    # Aucune clé → mode démo / publication en mode sec
    return Secrets(
        api_football_key="",
        football_data_token="",
        telegram_bot_token="",
        telegram_test_channel_id="",
        db_path=":memory:",
        outbox_dir=str(Path(__file__).parent / "outbox_test"),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def demo_providers(clock) -> dict[str, DemoProvider]:
    return {
        "demo_primary": DemoProvider("demo_primary", day=DAY, now=clock.now()),
        "demo_secondary": DemoProvider("demo_secondary", day=DAY, now=clock.now()),
    }


@pytest.fixture
def demo_pipeline(db, config, secrets, demo_providers, clock):
    from football.pipeline import Pipeline

    return Pipeline(
        db, config, secrets,
        providers=demo_providers,
        now_fn=clock.now,
    )


class FakeTransport:
    """Transport HTTP factice : aucune sortie réseau.

    `handler(method, url, headers, params, body) -> HttpResponse | Exception`
    (on peut aussi retourner un dict {"status":..., "json":...} ou des
    listes de réponses pour séquences).
    """

    def __init__(self, handler: Callable[..., Any]):
        self.handler = handler
        self.calls: list[dict] = []
        self._queues: dict[str, list] = {}

    def send(self, method: str, url: str, *, headers=None, params=None,
             body: bytes | None = None, timeout: float = 25.0) -> HttpResponse:
        self.calls.append({
            "method": method, "url": url, "headers": headers or {},
            "params": params or {}, "body": body,
        })
        key = f"{method} {url}"
        result = self.handler(method, url, headers or {}, params or {}, body)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, list):  # séquence de réponses
            if not self._queues.get(key):
                raise AssertionError(f"séquence épuisée pour {key}")
            result = self._queues[key].pop(0)
        if isinstance(result, tuple):
            status, payload = result
            return HttpResponse(status=status, headers={},
                                body=json.dumps(payload).encode())
        if isinstance(result, HttpResponse):
            return result
        return HttpResponse(status=200, headers={},
                            body=json.dumps(result).encode())


def load_recorded(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def recorded() -> dict[str, Any]:
    return {
        "aff_fixtures": load_recorded("api_football_fixtures.json"),
        "aff_odds": load_recorded("api_football_odds.json"),
        "aff_standings": load_recorded("api_football_standings.json"),
        "aff_injuries": load_recorded("api_football_injuries.json"),
        "fd_matches": load_recorded("football_data_matches.json"),
        "fd_standings": load_recorded("football_data_standings.json"),
    }
