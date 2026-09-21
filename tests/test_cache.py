"""Tests : cache horodaté (TTL, stale, hash, isolation des clés)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from football.http import Cache
from football.storage import Database
from football.utils import sha256_text, utcnow

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "c.db"))
    yield d
    d.close()


@pytest.fixture
def clock_state():
    return {"t": NOW}


@pytest.fixture
def cache(db, clock_state):
    return Cache(db, now_fn=lambda: clock_state["t"])


class TestCache:
    def test_miss_then_hit(self, cache, clock_state):
        assert cache.get("p", "u1", 900)[0] is None
        cache.put("p", "u1", 200, {"a": 1}, 900)
        row, fresh = cache.get("p", "u1", 900)
        assert fresh is True
        assert "a" in row["payload"]

    def test_ttl_expiry(self, cache, clock_state):
        cache.put("p", "u1", 200, {"a": 1}, 60)
        clock_state["t"] = NOW + timedelta(seconds=61)
        row, fresh = cache.get("p", "u1", 60)
        assert row is None  # expiré → cache manqué

    def test_stale_returned_when_allowed(self, cache, clock_state):
        cache.put("p", "u1", 200, {"a": 1}, 60)
        clock_state["t"] = NOW + timedelta(hours=2)
        row, fresh = cache.get("p", "u1", 60, allow_stale=True)
        assert row is not None
        assert fresh is False  # signal explicite de péremption

    def test_stale_not_returned_when_disallowed(self, cache, clock_state):
        cache.put("p", "u1", 200, {"a": 1}, 60)
        clock_state["t"] = NOW + timedelta(hours=2)
        row, _ = cache.get("p", "u1", 60, allow_stale=False)
        assert row is None

    def test_error_response_not_served_as_stale(self, cache):
        cache.put("p", "u1", 500, {}, 60)
        clock_t = cache.now_fn
        # la réponse 500 ne doit jamais être resservie
        row, _ = cache.get("p", "u1", 1, allow_stale=True)
        assert row is None

    def test_keys_isolated_by_provider(self, cache):
        cache.put("p1", "u1", 200, {"v": "one"}, 900)
        row, _ = cache.get("p2", "u1", 900)
        assert row is None

    def test_keys_isolated_by_url(self, cache):
        cache.put("p", "u1", 200, {"v": "one"}, 900)
        row, _ = cache.get("p", "u2", 900)
        assert row is None

    def test_key_is_stable_hash(self):
        k1 = Cache.key("p", "fixtures|date=2026-09-20")
        k2 = Cache.key("p", "fixtures|date=2026-09-20")
        k3 = Cache.key("p", "fixtures|date=2026-09-21")
        assert k1 == k2 != k3
        assert k1 == sha256_text("p|fixtures|date=2026-09-20")

    def test_overwrite_updates_payload(self, cache):
        cache.put("p", "u1", 200, {"v": 1}, 900)
        cache.put("p", "u1", 200, {"v": 2}, 900)
        row, _ = cache.get("p", "u1", 900)
        assert '"v": 2' in row["payload"]
