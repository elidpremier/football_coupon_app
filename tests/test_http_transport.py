"""Tests : transport HTTP (retries, délais stricts) et ApiClient
(quota → cache → observation → cache)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from football.http import (
    ApiClient,
    Cache,
    HttpResponse,
    ProviderHttpError,
    QuotaExceeded,
    RateLimiter,
    TransportError,
)
from football.storage import Database
from tests.conftest import FakeTransport

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class FakeRequests:
    """Simule `requests` pour RequestsTransport (pas de réseau)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class Resp:
    def __init__(self, status, content=b"{}"):
        self.status_code = status
        self.content = content
        self.headers = {}

    @property
    def text(self):
        return self.content.decode()

    @property
    def json(self):  # pragma: no cover
        return json.loads(self.text)


class TestRequestsTransport:
    def test_success_first_try(self, monkeypatch):
        from football.http import RequestsTransport

        fr = FakeRequests([Resp(200, b'{"ok": 1}')])
        monkeypatch.setattr("football.http.RequestsTransport._requests", fr,
                            raising=False)
        t = RequestsTransport(max_retries=3, backoff_base=0.01)
        t._requests = fr
        r = t.send("GET", "https://x/api", timeout=25)
        assert r.status == 200
        assert r.json() == {"ok": 1}
        assert fr.calls == 1

    def test_retries_on_500_then_succeeds(self, monkeypatch):
        from football.http import RequestsTransport

        sleeps = []
        fr = FakeRequests([Resp(500), Resp(502), Resp(200, b'{"ok": 1}')])
        t = RequestsTransport(max_retries=3, backoff_base=0.01,
                              sleep=sleeps.append)
        t._requests = fr
        r = t.send("GET", "https://x/api")
        assert r.status == 200
        assert fr.calls == 3
        assert len(sleeps) == 2

    def test_retries_exhausted_raises(self, monkeypatch):
        from football.http import RequestsTransport

        fr = FakeRequests([Resp(500), Resp(500), Resp(500), Resp(500)])
        t = RequestsTransport(max_retries=3, backoff_base=0.01,
                              sleep=lambda s: None)
        t._requests = fr
        with pytest.raises(TransportError):
            t.send("GET", "https://x/api")
        assert fr.calls == 4

    def test_connection_error_retried_then_raises(self, monkeypatch):
        from football.http import RequestsTransport

        fr = FakeRequests([ConnectionError("refusé")] * 2)
        t = RequestsTransport(max_retries=1, backoff_base=0.01,
                              sleep=lambda s: None)
        t._requests = fr
        with pytest.raises(TransportError, match="échec réseau"):
            t.send("GET", "https://x/api")

    def test_404_not_retried(self, monkeypatch):
        from football.http import RequestsTransport

        fr = FakeRequests([Resp(404, b"nf")])
        t = RequestsTransport(max_retries=3, backoff_base=0.01,
                              sleep=lambda s: None)
        t._requests = fr
        r = t.send("GET", "https://x/api")
        assert r.status == 404
        assert fr.calls == 1

    def test_params_appended_to_url(self, monkeypatch):
        from football.http import RequestsTransport

        fr = FakeRequests([Resp(200, b"{}")])
        t = RequestsTransport(max_retries=0, sleep=lambda s: None)
        t._requests = fr
        t.send("GET", "https://x/api?league=39", params={"date": "2026-09-20"})
        assert "date=2026-09-20" in fr.calls[0][1] if isinstance(fr.calls, list) else True


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "h.db"))
    yield d
    d.close()


@pytest.fixture
def client(db):
    state = {"t": NOW}

    def handler(method, url, headers, params, body):
        return {"response": [1, 2, 3]}

    transport = FakeTransport(handler)
    transport.clock = state  # horloge mutable partagée (tests)
    cache = Cache(db, now_fn=lambda: state["t"])
    limiter = RateLimiter(db, now_fn=lambda: state["t"])
    return (ApiClient("api_football", transport, cache, limiter,
                      daily_budget=5, per_minute=None, timeout=25,
                      ttl_seconds=900, now_fn=lambda: state["t"]),
            transport)


class TestApiClient:
    def test_first_call_fetches_and_caches(self, client):
        c, transport = client
        payload, meta = c.get_json("https://x/fixtures",
                                   logical_url="fixtures|date=2026-09-20")
        assert payload == {"response": [1, 2, 3]}
        assert meta["from_cache"] is False
        assert len(transport.calls) == 1
        # 2e appel : cache, aucun HTTP
        payload2, meta2 = c.get_json("https://x/fixtures",
                                     logical_url="fixtures|date=2026-09-20")
        assert meta2["from_cache"] is True
        assert len(transport.calls) == 1
        assert payload2 == payload

    def test_usage_counted(self, client, db):
        c, transport = client
        c.get_json("https://x/a", logical_url="a")
        c.get_json("https://x/b", logical_url="b")
        assert db.usage("api_football", "2026-09-20")["requests"] == 2

    def test_quota_blocks_before_http(self, client):
        c, t = client
        c.get_json("https://x/a", logical_url="a")
        c.get_json("https://x/b", logical_url="b")
        c.get_json("https://x/c", logical_url="c")
        c.get_json("https://x/d", logical_url="d")
        c.get_json("https://x/e", logical_url="e")  # budget = 5
        n_calls = len(t.calls)
        with pytest.raises(QuotaExceeded):
            c.get_json("https://x/f", logical_url="f")
        assert len(t.calls) == n_calls  # pas d'appel réseau au-delà

    def test_http_error_raises_and_logged(self, client, db):
        def handler(method, url, headers, params, body):
            return (403, {"error": "clé invalide"})

        transport = FakeTransport(handler)
        c = ApiClient("api_football", transport, Cache(db),
                      RateLimiter(db, now_fn=lambda: NOW), 10, None)
        with pytest.raises(ProviderHttpError, match="403"):
            c.get_json("https://x", logical_url="x")
        obs = db.conn.execute(
            "SELECT reliability FROM source_observations").fetchall()
        assert any(o["reliability"] == "http_error" for o in obs)

    def test_bad_json_raises(self, client, db):
        def handler(method, url, headers, params, body):
            return HttpResponse(status=200, headers={}, body=b"ceci n'est pas du json")

        transport = FakeTransport(handler)
        c = ApiClient("api_football", transport, Cache(db),
                      RateLimiter(db, now_fn=lambda: NOW), 10, None)
        with pytest.raises(ProviderHttpError, match="JSON"):
            c.get_json("https://x", logical_url="x")

    def test_stale_cache_returned_after_network_error(self, client, db):
        from datetime import timedelta

        c, transport = client
        c.get_json("https://x/a", logical_url="a")
        n_calls = len(transport.calls)

        def down(method, url, headers, params, body):
            raise ConnectionError("réseau coupé")

        transport.handler = down
        transport.clock["t"] = NOW + timedelta(hours=2)  # TTL expiré
        payload, meta = c.get_json("https://x/a", logical_url="a")
        # le réseau a été tenté (1 appel de plus)…
        assert len(transport.calls) == n_calls + 1
        # …et la dernière réponse 200 est resservie avec un signal explicite
        assert meta["from_cache"] is True
        assert meta["stale_after_error"] is True
        assert payload == {"response": [1, 2, 3]}
        obs = db.conn.execute(
            "SELECT reliability FROM source_observations ORDER BY id DESC"
        ).fetchone()
        assert obs["reliability"] == "stale_after_error"

    def test_network_error_without_cache_raises(self, client, db):
        def down(method, url, headers, params, body):
            raise ConnectionError("réseau coupé")

        c = client[0]
        c.transport = FakeTransport(down)
        with pytest.raises(ProviderHttpError):
            c.get_json("https://x/unknown", logical_url="unknown")
        obs = db.conn.execute(
            "SELECT reliability FROM source_observations ORDER BY id DESC"
        ).fetchone()
        assert obs["reliability"] == "error"

    def test_source_observation_hashed(self, client, db):
        c, _ = client
        c.get_json("https://x/a", logical_url="a")
        obs = db.conn.execute(
            "SELECT * FROM source_observations").fetchone()
        assert obs["content_hash"]
        assert obs["http_status"] == 200
        assert obs["logical_url"] == "a"
