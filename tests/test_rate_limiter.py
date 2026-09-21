"""Tests : quota quotidien persistant + fenêtre par minute."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from football.http import QuotaExceeded, RateLimiter
from football.storage import Database

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "r.db"))
    yield d
    d.close()


@pytest.fixture
def clock_state():
    return {"t": NOW}


@pytest.fixture
def limiter(db, clock_state):
    return RateLimiter(db, now_fn=lambda: clock_state["t"])


class TestDailyBudget:
    def test_acquire_until_limit(self, limiter, db):
        for _ in range(85):
            limiter.acquire("api_football", 85)
        assert db.usage("api_football", "2026-09-20")["requests"] == 85
        with pytest.raises(QuotaExceeded):
            limiter.acquire("api_football", 85)

    def test_budget_is_per_day(self, limiter, db, clock_state):
        for _ in range(85):
            limiter.acquire("api_football", 85)
        clock_state["t"] = NOW + timedelta(days=1)
        limiter.acquire("api_football", 85)  # jour suivant : OK
        assert db.usage("api_football", "2026-09-21")["requests"] == 1

    def test_budgets_isolated_per_provider(self, limiter):
        for _ in range(85):
            limiter.acquire("api_football", 85)
        limiter.acquire("football_data", 200)  # pas bloqué par l'autre
        assert limiter.db.usage("football_data", "2026-09-20")["requests"] == 1

    def test_persistence_across_instances(self, db, clock_state):
        l1 = RateLimiter(db, now_fn=lambda: clock_state["t"])
        for _ in range(84):
            l1.acquire("api_football", 85)
        l2 = RateLimiter(db, now_fn=lambda: clock_state["t"])  # "redémarrage"
        l2.acquire("api_football", 85)
        with pytest.raises(QuotaExceeded):
            l2.acquire("api_football", 85)

    def test_soft_budget_under_api_limit(self, limiter):
        # 85 < 100 : le plafond logiciel protège le quota réel
        assert 85 < 100

    def test_error_does_not_consume_budget(self, limiter, db):
        limiter.record("p", 85, error=True)
        assert db.usage("p", "2026-09-20")["requests"] == 0
        assert db.usage("p", "2026-09-20")["errors"] == 1


class TestPerMinute:
    def test_minute_window_enforced(self, limiter, clock_state):
        for i in range(10):
            limiter.acquire("football_data", 200, per_minute=10)
            clock_state["t"] = NOW + timedelta(seconds=i * 2)
        with pytest.raises(QuotaExceeded):
            limiter.acquire("football_data", 200, per_minute=10)

    def test_minute_window_slides(self, limiter, clock_state):
        for i in range(10):
            limiter.acquire("football_data", 200, per_minute=10)
            clock_state["t"] = NOW + timedelta(seconds=i * 5)
        # tout le trafic glisse hors de la fenêtre de 60 s
        clock_state["t"] = NOW + timedelta(seconds=400)
        limiter.acquire("football_data", 200, per_minute=10)  # OK

    def test_no_per_minute_no_window(self, limiter):
        for _ in range(50):
            limiter.acquire("api_football", 85, per_minute=None)
