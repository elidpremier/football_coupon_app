"""Couche HTTP : transport injectable, retries avec délai progressif,
délais réseau stricts.

Le transport est injectable (protocole `HttpTransport`) : les tests n'ont
jamais besoin du réseau réel.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .utils import iso, utcnow


class TransportError(Exception):
    """Échec réseau définitif (après les retries)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise TransportError(f"réponse JSON invalide (HTTP {self.status})") from exc


class HttpTransport(Protocol):
    def send(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
             params: Optional[dict[str, Any]] = None, body: Optional[bytes] = None,
             timeout: float = 25.0) -> HttpResponse: ...


class RequestsTransport:
    """Transport par défaut (requests) avec retries + backoff exponentiel."""

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, max_retries: int = 3, backoff_base: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self._sleep = sleep
        try:
            import requests  # import différé : les tests n'ont pas besoin de requests
            self._requests = requests
        except ImportError as exc:  # pragma: no cover
            raise TransportError("la bibliothèque 'requests' est requise") from exc

    def _build_url(self, url: str, params: Optional[dict[str, Any]]) -> str:
        if not params:
            return url
        from urllib.parse import urlencode

        sep = "&" if "?" in url else "?"
        return url + sep + urlencode(params)

    def send(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
             params: Optional[dict[str, Any]] = None, body: Optional[bytes] = None,
             timeout: float = 25.0) -> HttpResponse:
        full_url = self._build_url(url, params)
        last_error: Exception | None = None
        last_status: int | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._requests.request(
                    method, full_url, headers=headers, data=body, timeout=timeout
                )
                last_status = resp.status_code
                if resp.status_code in self.RETRYABLE_STATUS:
                    if attempt < self.max_retries:
                        self._sleep(self.backoff_base ** (attempt + 1))
                        continue
                    break  # tentatives épuisées avec une erreur persistante
                return HttpResponse(
                    status=resp.status_code,
                    headers=dict(resp.headers.items()),
                    body=resp.content,
                )
            except Exception as exc:  # timeout, connexion refusée…
                last_error = exc
                last_status = None
                if attempt < self.max_retries:
                    self._sleep(self.backoff_base ** (attempt + 1))
                    continue
        if last_status in self.RETRYABLE_STATUS:
            raise TransportError(
                f"HTTP {last_status} persistant après {self.max_retries + 1} tentatives "
                f"pour {full_url}"
            )
        raise TransportError(f"échec réseau après {self.max_retries + 1} tentatives : {last_error}")


class QuotaExceeded(Exception):
    """Budget quotidien (ou par minute) atteint — collecte reportée."""


class Cache:
    """Cache horodaté : clé = hash(provider|url logique), TTL, hash de contenu.

    Comportement : réponse fraîche en cache → pas d'appel HTTP.
    Cache expiré + erreur réseau → retourne la réponse la plus récente
    marquée `stale=True` (jamais une donnée silencieusement périmée
    sans signal explicite).
    """

    def __init__(self, db, now_fn: Callable[[], Any] | None = None):
        self.db = db
        self.now_fn = now_fn or utcnow

    @staticmethod
    def key(provider: str, logical_url: str) -> str:
        from .utils import sha256_text

        return sha256_text(f"{provider}|{logical_url}")

    def get(self, provider: str, logical_url: str, ttl_seconds: int,
            allow_stale: bool = False):
        from .utils import parse_iso

        row = self.db.cache_get(self.key(provider, logical_url), self.now_fn())
        if not row:
            return None, False
        fetched = parse_iso(row["fetched_at_utc"])
        age = (self.now_fn() - fetched).total_seconds()
        fresh = age <= ttl_seconds
        if fresh or (allow_stale and row["http_status"] == 200):
            return row, fresh
        return None, False

    def put(self, provider: str, logical_url: str, http_status: int,
            payload: Any, ttl_seconds: int) -> None:
        import json

        from .utils import sha256_text

        if http_status != 200:
            # une réponse d'erreur ne doit jamais être servie plus tard
            # (même « stale ») : on ne la met tout simplement pas en cache.
            return
        self.db.cache_put(
            {
                "cache_key": self.key(provider, logical_url),
                "provider": provider,
                "logical_url": logical_url,
                "http_status": http_status,
                "payload": json.dumps(payload, ensure_ascii=False),
                "ttl_seconds": ttl_seconds,
            },
            self.now_fn(),
        )


class RateLimiter:
    """Plafond quotidien persistant (SQLite) + fenêtre glissante par minute
    (mémoire, réinitialisée au redémarrage — documenté comme tel)."""

    def __init__(self, db, now_fn: Callable[[], Any] | None = None):
        self.db = db
        self.now_fn = now_fn or utcnow
        self._minute_windows: dict[str, list[float]] = {}

    def acquire(self, provider: str, daily_limit: int,
                per_minute: int | None = None) -> None:
        """Réservation ATOMIQUE du budget quotidien (persistante).

        Le compteur est incrémenté avant le contrôle : en cas de dépassement
        il est décrémenté et `QuotaExceeded` est levé. La réservation survit
        à un crash — un appel en cours consomme toujours sa part.
        """
        now = self.now_fn()
        from .utils import today_utc

        day = today_utc(now)
        used = self.db.usage_reserve(provider, day, daily_limit)
        if used > daily_limit:
            self.db.usage_release(provider, day)
            raise QuotaExceeded(
                f"quota quotidien {provider} atteint ({used - 1}/{daily_limit})"
            )
        if per_minute:
            key = f"{provider}|minute"
            now_ts = now.timestamp()
            window = [t for t in self._minute_windows.get(key, []) if now_ts - t < 60]
            if len(window) >= per_minute:
                self.db.usage_release(provider, day)
                raise QuotaExceeded(
                    f"fenêtre par minute {provider} saturée ({per_minute}/min)"
                )
            window.append(now_ts)
            self._minute_windows[key] = window

    def record(self, provider: str, daily_limit: int, error: bool = False) -> int:
        """Journalise le résultat de l'appel réservé par `acquire`.

        En cas d'erreur : la réservation est libérée (les erreurs ne
        consomment pas le budget) et le compteur d'erreurs est incrémenté.
        """
        now = self.now_fn()
        from .utils import today_utc

        day = today_utc(now)
        if error:
            self.db.usage_release(provider, day)
            self.db.usage_add_error(provider, day, daily_limit)
            return self.db.usage(provider, day)["requests"]
        return self.db.usage(provider, day)["requests"]


class ApiClient:
    """Façade : quota → cache → HTTP → observation source → cache.

    Chaque appel est journalisé dans `source_observations` avec l'URL,
    le statut HTTP et le hash du contenu.
    """

    def __init__(self, provider: str, transport: HttpTransport, cache: Cache,
                 rate_limiter: RateLimiter, daily_budget: int,
                 per_minute: int | None = None, timeout: float = 25.0,
                 ttl_seconds: int = 900,
                 now_fn: Callable[[], Any] | None = None):
        self.provider = provider
        self.transport = transport
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.daily_budget = daily_budget
        self.per_minute = per_minute
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self._now_fn = now_fn or utcnow

    def now_fn(self):
        return self._now_fn()

    def get_json(self, url: str, *, logical_url: str,
                 headers: Optional[dict[str, str]] = None,
                 params: Optional[dict[str, Any]] = None,
                 ttl_seconds: int | None = None,
                 allow_stale_on_error: bool = True):
        """Renvoie (payload, meta).

        Logique : réponse FRAÎCHE en cache → pas d'appel HTTP ;
        sinon appel HTTP ; en cas d'échec réseau, la dernière réponse
        200 est resservie avec le signal explicite `stale_after_error`
        (jamais de donnée périmée servie silencieusement).
        """
        import json

        from .utils import parse_iso, sha256_bytes, sha256_text

        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        row = self.cache.db.cache_get(
            self.cache.key(self.provider, logical_url), self.now_fn())
        if row is not None and row["http_status"] == 200:
            fetched = parse_iso(row["fetched_at_utc"])
            age = (self.now_fn() - fetched).total_seconds()
            if age <= ttl:
                self._observe(logical_url, url, 200,
                              sha256_text(row["payload"]), "fresh_cache")
                return json.loads(row["payload"]), {
                    "fresh": True, "from_cache": True, "status": 200,
                }

        self.rate_limiter.acquire(self.provider, self.daily_budget,
                                  self.per_minute)
        try:
            resp = self.transport.send("GET", url, headers=headers,
                                       params=params, timeout=self.timeout)
        except Exception as exc:  # TransportError en prod, brut dans les tests
            self.rate_limiter.record(self.provider, self.daily_budget,
                                     error=True)
            if allow_stale_on_error and row is not None \
                    and row["http_status"] == 200:
                self._observe(logical_url, url, 200,
                              sha256_text(row["payload"]), "stale_after_error")
                return json.loads(row["payload"]), {
                    "fresh": False, "from_cache": True,
                    "stale_after_error": True, "status": 200,
                }
            self._observe(logical_url, url, None, None, "error")
            raise ProviderHttpError(str(exc)) from exc
        self.rate_limiter.record(self.provider, self.daily_budget,
                                 error=resp.status >= 400)
        if resp.status >= 400:
            self._observe(logical_url, url, resp.status,
                          sha256_bytes(resp.body), "http_error")
            raise ProviderHttpError(
                f"HTTP {resp.status} pour {logical_url} "
                f"(corps : {resp.text[:200]!r})"
            )
        try:
            payload = resp.json()
        except TransportError as exc:
            self._observe(logical_url, url, resp.status,
                          sha256_bytes(resp.body), "bad_json")
            raise ProviderHttpError("JSON invalide renvoyé par le fournisseur") from exc
        self._observe(logical_url, url, resp.status, sha256_bytes(resp.body), "ok")
        self.cache.put(self.provider, logical_url, resp.status, payload, ttl)
        return payload, {"fresh": True, "from_cache": False, "status": resp.status}

    def _observe(self, logical_url: str, url: str, status: int | None,
                 content_hash: str | None, reliability: str) -> None:
        self.cache.db.log_source_observation(
            {
                "provider": self.provider,
                "logical_url": logical_url,
                "http_url": url,
                "http_status": status,
                "content_hash": content_hash,
                "reliability": reliability,
            },
            self.now_fn(),
        )


class ProviderHttpError(Exception):
    """Erreur HTTP / JSON d'un fournisseur (distinct d'un quota)."""
