"""Couche de stockage SQLite : migrations versionnées, accès transactionnels.

Garanties clés :
- toutes les dates sont stockées en UTC (ISO 8601, suffixe Z) ;
- un coupon ne peut être publié qu'une fois : contrainte UNIQUE + contrôle
  applicatif (idempotence, même après redémarrage) ;
- aucun effacement automatique des analyses publiées.
"""
from __future__ import annotations

import functools
import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .utils import iso, jdump, jload, utcnow

SCHEMA_VERSION = 3

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS fixtures(
            fixture_key TEXT PRIMARY KEY,
            competition_slug TEXT NOT NULL,
            competition_name TEXT,
            kickoff_utc TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            venue TEXT,
            status TEXT NOT NULL DEFAULT 'COLLECTED',
            matching_status TEXT NOT NULL DEFAULT 'OK',
            primary_provider TEXT,
            primary_fixture_id TEXT,
            secondary_provider TEXT,
            secondary_fixture_id TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS fixtures_providers(
            fixture_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_fixture_id TEXT,
            raw_json TEXT,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY(fixture_key, provider)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS odds_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            bookmaker TEXT,
            market TEXT NOT NULL,
            outcome TEXT NOT NULL,
            odds TEXT NOT NULL,
            observed_at_utc TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            UNIQUE(provider, bookmaker, market, outcome, observed_at_utc)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS team_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            team TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('home','away')),
            competition_slug TEXT,
            position INTEGER,
            played INTEGER, wins INTEGER, draws INTEGER, losses INTEGER,
            goals_for INTEGER, goals_against INTEGER,
            form TEXT,
            observed_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS availability_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            team TEXT NOT NULL,
            player TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('confirmed','unconfirmed')),
            source_ref TEXT,
            observed_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_observations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            logical_url TEXT NOT NULL,
            http_url TEXT,
            fetched_at_utc TEXT NOT NULL,
            http_status INTEGER,
            content_hash TEXT,
            reliability TEXT NOT NULL DEFAULT 'ok'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS http_cache(
            cache_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            logical_url TEXT NOT NULL,
            http_status INTEGER NOT NULL,
            payload TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS api_usage(
            provider TEXT NOT NULL,
            day TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            budget INTEGER NOT NULL,
            errors INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(provider, day)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS match_features(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            model_version TEXT NOT NULL,
            features_json TEXT NOT NULL,
            quality_score REAL,
            computed_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS analyses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            analyst_json TEXT,
            critic_json TEXT,
            validated INTEGER NOT NULL DEFAULT 0,
            source_refs TEXT,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS selections(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            market TEXT NOT NULL,
            outcome TEXT NOT NULL,
            odds TEXT NOT NULL,
            odds_observed_at_utc TEXT NOT NULL,
            probability REAL NOT NULL,
            model_version TEXT NOT NULL,
            score REAL,
            status TEXT NOT NULL,
            reasons TEXT,
            justification TEXT,
            source_refs TEXT,
            created_at_utc TEXT NOT NULL,
            UNIQUE(fixture_key, market, outcome)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS coupons(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coupon_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'cautious',
            selections_json TEXT NOT NULL,
            combined_odds TEXT NOT NULL,
            combined_probability REAL NOT NULL,
            score REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            version INTEGER NOT NULL DEFAULT 1,
            constraints_json TEXT NOT NULL,
            notice TEXT NOT NULL,
            approved_by TEXT,
            approved_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            published_at_utc TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS published_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coupon_id TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            telegram_message_id TEXT,
            png_hash TEXT NOT NULL,
            caption TEXT NOT NULL,
            published_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS results(
            fixture_key TEXT PRIMARY KEY,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT,
            settled_at_utc TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS selection_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            selection_id INTEGER NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('won','lost','void')),
            settled_at_utc TEXT NOT NULL,
            UNIQUE(selection_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS coupon_results(
            coupon_id TEXT PRIMARY KEY,
            overall TEXT NOT NULL CHECK(overall IN ('won','lost','partial','void')),
            settled_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS calibration_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            market TEXT NOT NULL,
            outcome TEXT NOT NULL,
            predicted_probability REAL NOT NULL,
            observed INTEGER NOT NULL CHECK(observed IN (0,1)),
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS operator_actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload TEXT,
            at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS run_log(
            run_id TEXT PRIMARY KEY,
            job TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            summary TEXT,
            errors TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_odds_fixture ON odds_snapshots(fixture_key)",
        "CREATE INDEX IF NOT EXISTS idx_team_snap_fixture ON team_snapshots(fixture_key)",
        "CREATE INDEX IF NOT EXISTS idx_avail_fixture ON availability_snapshots(fixture_key)",
        "CREATE INDEX IF NOT EXISTS idx_selections_status ON selections(status)",
        "CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff ON fixtures(kickoff_utc)",
    ],    2: [
        "ALTER TABLE selections ADD COLUMN quality REAL",
    ],
    3: [
        # la contrainte UNIQUE des cotes omettait fixture_key : deux
        # matchs au même horodatage se cachaient mutuellement
        """
        CREATE TABLE IF NOT EXISTS odds_snapshots_v3(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            bookmaker TEXT,
            market TEXT NOT NULL,
            outcome TEXT NOT NULL,
            odds TEXT NOT NULL,
            observed_at_utc TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            UNIQUE(provider, fixture_key, bookmaker, market, outcome,
                   observed_at_utc)
        )
        """,
        "INSERT INTO odds_snapshots_v3(fixture_key, provider, bookmaker, market,"
        " outcome, odds, observed_at_utc, payload_hash) "
        "SELECT fixture_key, provider, bookmaker, market, outcome, odds, "
        "observed_at_utc, payload_hash FROM odds_snapshots",
        "DROP TABLE odds_snapshots",
        "ALTER TABLE odds_snapshots_v3 RENAME TO odds_snapshots",
        "CREATE INDEX IF NOT EXISTS idx_odds_fixture_v3 ON odds_snapshots(fixture_key)",
    ],


}


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False : Streamlit exécute le script dans un
        # thread différent à chaque session/rerun (st.cache_resource garde
        # cette instance en vie) — la connexion est donc partagée entre
        # threads et TOUTE passe par le verrou self._lock (RLock,
        # réentrant). timeout=30 : en écriture concurrente, on attend la
        # transaction rivale au lieu d'échouer d'emblée.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Exécution directe sous verrou (remplace l'accès brut db.conn
        dans le reste du code)."""
        with self._lock:
            return self.conn.execute(sql, params)

    # ---------------- migrations ----------------
    def _schema_version(self) -> int:
        try:
            row = self.conn.execute(
                "SELECT MAX(version) v FROM schema_migrations"
            ).fetchone()
            return int(row["v"] or 0)
        except sqlite3.OperationalError:
            return 0

    def migrate(self) -> int:
        """Applique les migrations manquantes (idempotent)."""
        current = self._schema_version()
        with self.conn:
            if current == 0:
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
            for version in sorted(MIGRATIONS):
                if version <= current:
                    continue
                for stmt in MIGRATIONS[version]:
                    self.conn.execute(stmt)
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (version, iso(utcnow())),
                )
        return self._schema_version()

    def schema_version(self) -> int:
        return self._schema_version()

    def backup(self, target: str | Path) -> str:
        """Copie cohérente de la base (API de backup SQLite)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(target))
        try:
            with dst:
                self.conn.backup(dst)
        finally:
            dst.close()
        return str(target)

    def close(self) -> None:
        self.conn.close()

    # ---------------- fixtures ----------------
    def upsert_fixture(self, fx: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO fixtures(fixture_key, competition_slug, competition_name,
                    kickoff_utc, home_team, away_team, venue, status, matching_status,
                    primary_provider, primary_fixture_id, secondary_provider,
                    secondary_fixture_id, updated_at)
                VALUES(:fixture_key, :competition_slug, :competition_name, :kickoff_utc,
                    :home_team, :away_team, :venue, :status, :matching_status,
                    :primary_provider, :primary_fixture_id, :secondary_provider,
                    :secondary_fixture_id, :updated_at)
                ON CONFLICT(fixture_key) DO UPDATE SET
                    status = excluded.status,
                    matching_status = excluded.matching_status,
                    venue = COALESCE(excluded.venue, fixtures.venue),
                    primary_provider = COALESCE(excluded.primary_provider, fixtures.primary_provider),
                    primary_fixture_id = COALESCE(excluded.primary_fixture_id, fixtures.primary_fixture_id),
                    secondary_provider = COALESCE(excluded.secondary_provider, fixtures.secondary_provider),
                    secondary_fixture_id = COALESCE(excluded.secondary_fixture_id, fixtures.secondary_fixture_id),
                    updated_at = excluded.updated_at
                """,
                {
                    "fixture_key": fx["fixture_key"],
                    "competition_slug": fx["competition_slug"],
                    "competition_name": fx.get("competition_name"),
                    "kickoff_utc": fx["kickoff_utc"],
                    "home_team": fx["home_team"],
                    "away_team": fx["away_team"],
                    "venue": fx.get("venue"),
                    "status": fx.get("status", "COLLECTED"),
                    "matching_status": fx.get("matching_status", "OK"),
                    "primary_provider": fx.get("primary_provider"),
                    "primary_fixture_id": fx.get("primary_fixture_id"),
                    "secondary_provider": fx.get("secondary_provider"),
                    "secondary_fixture_id": fx.get("secondary_fixture_id"),
                    "updated_at": iso(now),
                },
            )

    def add_provider_fixture(self, fx: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO fixtures_providers(fixture_key, provider, provider_fixture_id,
                    raw_json, fetched_at_utc)
                VALUES(?,?,?,?,?)
                ON CONFLICT(fixture_key, provider) DO UPDATE SET
                    provider_fixture_id = excluded.provider_fixture_id,
                    raw_json = excluded.raw_json,
                    fetched_at_utc = excluded.fetched_at_utc
                """,
                (
                    fx["fixture_key"],
                    fx["provider"],
                    fx.get("provider_fixture_id"),
                    jdump(fx.get("raw")),
                    iso(now),
                ),
            )

    def get_fixture(self, fixture_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fixtures WHERE fixture_key = ?", (fixture_key,)
        ).fetchone()

    def fixtures_between(self, start_utc: str, end_utc: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM fixtures
            WHERE kickoff_utc >= ? AND kickoff_utc < ?
            ORDER BY kickoff_utc
            """,
            (start_utc, end_utc),
        ).fetchall()

    def set_fixture_status(self, fixture_key: str, status: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE fixtures SET status = ?, updated_at = ? WHERE fixture_key = ?",
                (status, iso(now), fixture_key),
            )

    def set_fixture_matching(self, fixture_key: str, matching_status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE fixtures SET matching_status = ? WHERE fixture_key = ?",
                (matching_status, fixture_key),
            )

    # ---------------- cotes ----------------
    def upsert_odds(self, snaps: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.conn:
            for s in snaps:
                cur = self.conn.execute(
                    """
                    INSERT INTO odds_snapshots(fixture_key, provider, bookmaker, market,
                        outcome, odds, observed_at_utc, payload_hash)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider, fixture_key, bookmaker, market, outcome,
                    observed_at_utc)
                    DO NOTHING
                    """,
                    (
                        s["fixture_key"],
                        s["provider"],
                        s.get("bookmaker"),
                        s["market"],
                        s["outcome"],
                        str(s["odds"]),
                        s["observed_at_utc"],
                        s["payload_hash"],
                    ),
                )
                n += cur.rowcount if cur.rowcount > 0 else 0
        return n

    def latest_odds(self, fixture_key: str) -> list[sqlite3.Row]:
        """Dernière cote par (provider, market, outcome) pour un match."""
        return self.conn.execute(
            """
            SELECT o.* FROM odds_snapshots o
            JOIN (
                SELECT provider, market, outcome, MAX(observed_at_utc) mx
                FROM odds_snapshots WHERE fixture_key = ?
                GROUP BY provider, market, outcome
            ) m
            ON o.provider = m.provider AND o.market = m.market
            AND o.outcome = m.outcome AND o.observed_at_utc = m.mx
            WHERE o.fixture_key = ?
            ORDER BY o.market, o.outcome, o.provider
            """,
            (fixture_key, fixture_key),
        ).fetchall()

    def all_odds(self, fixture_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM odds_snapshots WHERE fixture_key = ? ORDER BY observed_at_utc",
            (fixture_key,),
        ).fetchall()

    # ---------------- équipes / indisponibilités ----------------
    def upsert_team_snapshot(self, t: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO team_snapshots(fixture_key, team, side, competition_slug,
                    position, played, wins, draws, losses, goals_for, goals_against,
                    form, observed_at_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    t["fixture_key"], t["team"], t["side"], t.get("competition_slug"),
                    t.get("position"), t.get("played"), t.get("wins"), t.get("draws"),
                    t.get("losses"), t.get("goals_for"), t.get("goals_against"),
                    t.get("form"), iso(now),
                ),
            )

    def team_snapshots(self, fixture_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM team_snapshots WHERE fixture_key = ? ORDER BY side",
            (fixture_key,),
        ).fetchall()

    def upsert_availability(self, a: dict[str, Any], now: datetime) -> None:
        """Insère une observation d'absence ; idempotent par
        (fixture_key, team, player, reason) : les statuts ultérieurs
        (confirmée/non confirmée) remplacent l'observation précédente."""
        with self.conn:
            row = self.conn.execute(
                "SELECT id, status, source_ref FROM availability_snapshots "
                "WHERE fixture_key = ? AND team = ? AND player = ? AND reason = ?",
                (a["fixture_key"], a["team"], a["player"], a["reason"]),
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE availability_snapshots "
                    "SET status = ?, source_ref = ?, observed_at_utc = ? WHERE id = ?",
                    (a["status"], a.get("source_ref"), iso(now), row["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO availability_snapshots(fixture_key, team, player,"
                    " reason, status, source_ref, observed_at_utc) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        a["fixture_key"], a["team"], a["player"], a["reason"],
                        a["status"], a.get("source_ref"), iso(now),
                    ),
                )

    def availability(self, fixture_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM availability_snapshots WHERE fixture_key = ?", (fixture_key,)
        ).fetchall()

    # ---------------- sources / cache / quota ----------------
    def log_source_observation(self, obs: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO source_observations(provider, logical_url, http_url,
                    fetched_at_utc, http_status, content_hash, reliability)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    obs["provider"], obs["logical_url"], obs.get("http_url"),
                    iso(now), obs.get("http_status"), obs.get("content_hash"),
                    obs.get("reliability", "ok"),
                ),
            )

    def cache_get(self, key: str, now: datetime) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM http_cache WHERE cache_key = ?", (key,)
        ).fetchone()

    def cache_put(self, entry: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO http_cache(cache_key, provider, logical_url, http_status,
                    payload, fetched_at_utc, ttl_seconds)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    http_status = excluded.http_status,
                    payload = excluded.payload,
                    fetched_at_utc = excluded.fetched_at_utc,
                    ttl_seconds = excluded.ttl_seconds
                """,
                (
                    entry["cache_key"], entry["provider"], entry["logical_url"],
                    entry["http_status"], entry["payload"], iso(now),
                    int(entry["ttl_seconds"]),
                ),
            )

    def usage_consume(self, provider: str, day: str, limit: int, now: datetime,
                      error: bool = False) -> int:
        """Compteur du jour (atomique) ; renvoie le total de `requests`.

        `error=True` : l'erreur est comptée SANS consommer le budget
        (les échecs réseau ne doivent pas griller le quota journalier).
        """
        with self.conn:
            if error:
                self.conn.execute(
                    "INSERT INTO api_usage(provider, day, requests, budget, errors) "
                    "VALUES(?,?,0,?,1) "
                    "ON CONFLICT(provider, day) DO UPDATE SET errors = errors + 1, "
                    "budget = MAX(budget, excluded.budget)",
                    (provider, day, limit),
                )
            else:
                self.conn.execute(
                    "INSERT INTO api_usage(provider, day, requests, budget) "
                    "VALUES(?,?,1,?) "
                    "ON CONFLICT(provider, day) DO UPDATE SET "
                    "requests = requests + 1, budget = MAX(budget, excluded.budget)",
                    (provider, day, limit),
                )
            row = self.conn.execute(
                "SELECT requests FROM api_usage WHERE provider = ? AND day = ?",
                (provider, day),
            ).fetchone()
        return int(row["requests"])

    def usage_reserve(self, provider: str, day: str, limit: int) -> int:
        """Réservation atomique : incrémente `requests`, renvoie le total."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_usage(provider, day, requests, budget) "
                "VALUES(?,?,1,?) "
                "ON CONFLICT(provider, day) DO UPDATE SET requests = requests + 1",
                (provider, day, limit),
            )
            row = self.conn.execute(
                "SELECT requests FROM api_usage WHERE provider = ? AND day = ?",
                (provider, day),
            ).fetchone()
        return int(row["requests"])

    def usage_release(self, provider: str, day: str) -> None:
        """Annule une réservation (compteur non-négatif)."""
        with self.conn:
            self.conn.execute(
                "UPDATE api_usage SET requests = MAX(requests - 1, 0) "
                "WHERE provider = ? AND day = ?",
                (provider, day),
            )

    def usage_add_error(self, provider: str, day: str, limit: int) -> None:
        """Compte une erreur SANS la faire consommer du budget."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_usage(provider, day, requests, budget, errors) "
                "VALUES(?,?,0,?,1) "
                "ON CONFLICT(provider, day) DO UPDATE SET errors = errors + 1, "
                "budget = MAX(budget, excluded.budget)",
                (provider, day, limit),
            )

    def usage(self, provider: str, day: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT requests, errors, budget FROM api_usage WHERE provider = ? AND day = ?",
            (provider, day),
        ).fetchone()
        if not row:
            return {"requests": 0, "errors": 0, "limit": 0}
        return {"requests": row["requests"], "errors": row["errors"], "limit": row["budget"]}

    def usage_all(self, day: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM api_usage WHERE day = ? ORDER BY provider", (day,)
        ).fetchall()

    # ---------------- features / analyses ----------------
    def save_features(self, features: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO match_features(fixture_key, model_version, features_json,
                    quality_score, computed_at_utc)
                VALUES(?,?,?,?,?)
                """,
                (
                    features["fixture_key"], features["model_version"],
                    jdump(features["features"]), features.get("quality_score"),
                    iso(now),
                ),
            )

    def latest_features(self, fixture_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM match_features WHERE fixture_key = ? ORDER BY id DESC LIMIT 1",
            (fixture_key,),
        ).fetchone()

    def save_analysis(self, a: dict[str, Any], now: datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO analyses(fixture_key, mode, payload_json, analyst_json,
                    critic_json, validated, source_refs, created_at_utc)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    a["fixture_key"], a["mode"], jdump(a["payload"]),
                    jdump(a.get("analyst")), jdump(a.get("critic")),
                    1 if a.get("validated") else 0, jdump(a.get("source_refs") or []),
                    iso(now),
                ),
            )
            return int(cur.lastrowid)

    def latest_analysis(self, fixture_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM analyses WHERE fixture_key = ? ORDER BY id DESC LIMIT 1",
            (fixture_key,),
        ).fetchone()

    # ---------------- sélections ----------------
    def upsert_selection(self, s: dict[str, Any], now: datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO selections(fixture_key, market, outcome, odds,
                    odds_observed_at_utc, probability, model_version, score, quality,
                    status, reasons, justification, source_refs, created_at_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fixture_key, market, outcome) DO UPDATE SET
                    odds = excluded.odds,
                    odds_observed_at_utc = excluded.odds_observed_at_utc,
                    probability = excluded.probability,
                    model_version = excluded.model_version,
                    score = excluded.score,
                    quality = excluded.quality,
                    status = excluded.status,
                    reasons = excluded.reasons,
                    justification = excluded.justification,
                    source_refs = excluded.source_refs,
                    created_at_utc = excluded.created_at_utc
                """,
                (
                    s["fixture_key"], s["market"], s["outcome"], str(s["odds"]),
                    s["odds_observed_at_utc"], s["probability"], s["model_version"],
                    s.get("score"), s.get("quality"), s["status"],
                    jdump(s.get("reasons") or []),
                    s.get("justification"), jdump(s.get("source_refs") or []),
                    iso(now),
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM selections WHERE fixture_key=? AND market=? AND outcome=?",
                (s["fixture_key"], s["market"], s["outcome"]),
            ).fetchone()
            return int(row["id"])

    def selection_by_id(self, sel_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM selections WHERE id = ?", (sel_id,)
        ).fetchone()

    def selections_by_status(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM selections WHERE status = ? ORDER BY score DESC", (status,)
        ).fetchall()

    def set_selection_status(self, sel_id: int, status: str, now: datetime,
                             actor: str = "system", comment: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE selections SET status = ? WHERE id = ?", (status, sel_id)
            )
            self.operator_action(actor, "selection_status", "selection", str(sel_id),
                                 {"to": status, "comment": comment}, now)

    # ---------------- coupons ----------------
    def create_coupon(self, c: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO coupons(coupon_id, kind, selections_json, combined_odds,
                    combined_probability, score, status, version, constraints_json,
                    notice, created_at_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    c["coupon_id"], c.get("kind", "cautious"), jdump(c["selections"]),
                    str(c["combined_odds"]), c["combined_probability"], c["score"],
                    c.get("status", "DRAFT"), int(c.get("version", 1)),
                    jdump(c.get("constraints") or []), c["notice"], iso(now),
                ),
            )

    def get_coupon(self, coupon_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM coupons WHERE coupon_id = ?", (coupon_id,)
        ).fetchone()

    def coupons_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        if not statuses:
            return []
        qs = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM coupons WHERE status IN ({qs}) ORDER BY score DESC",
            statuses,
        ).fetchall()

    def all_coupons(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM coupons ORDER BY created_at_utc DESC LIMIT ?", (limit,)
        ).fetchall()

    def set_coupon_status(self, coupon_id: str, status: str, now: datetime,
                          approved_by: str | None = None,
                          comment: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE coupons SET status = ?,
                    approved_by = COALESCE(?, approved_by),
                    approved_at_utc = CASE WHEN ? = 'APPROVED' THEN ? ELSE approved_at_utc END,
                    published_at_utc = CASE WHEN ? = 'PUBLISHED' THEN ? ELSE published_at_utc END
                WHERE coupon_id = ?
                """,
                (status, approved_by, status, iso(now), status, iso(now), coupon_id),
            )
            self.operator_action("system", "coupon_status", "coupon", coupon_id,
                                 {"to": status, "comment": comment}, now)

    # ---------------- publication (anti-doublon) ----------------
    def is_published(self, coupon_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM published_events WHERE coupon_id = ?", (coupon_id,)
        ).fetchone()
        return row is not None

    def published_count_for_day(self, day: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) n FROM published_events WHERE substr(published_at_utc,1,10) = ?",
                (day,),
            ).fetchone()["n"]
        )

    def record_publish(self, p: dict[str, Any], now: datetime) -> bool:
        """Enregistre l'événement de publication.

        Retourne False si le coupon était déjà publié (idempotence :
        la contrainte UNIQUE est le filet de sécurité final).
        """
        with self.conn:
            try:
                self.conn.execute(
                    """
                    INSERT INTO published_events(coupon_id, channel, dry_run,
                        telegram_message_id, png_hash, caption, published_at_utc)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        p["coupon_id"], p["channel"], 1 if p.get("dry_run") else 0,
                        p.get("telegram_message_id"), p["png_hash"], p["caption"],
                        iso(now),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def published_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM published_events ORDER BY published_at_utc DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---------------- résultats ----------------
    def upsert_result(self, fixture_key: str, home_score: int, away_score: int,
                      status: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO results(fixture_key, home_score, away_score, status, settled_at_utc)
                VALUES(?,?,?,?,?)
                ON CONFLICT(fixture_key) DO UPDATE SET
                    home_score = excluded.home_score,
                    away_score = excluded.away_score,
                    status = excluded.status,
                    settled_at_utc = excluded.settled_at_utc
                """,
                (fixture_key, home_score, away_score, status, iso(now)),
            )

    def result_for(self, fixture_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM results WHERE fixture_key = ?", (fixture_key,)
        ).fetchone()

    def unsettled_fixtures(self, before_utc: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT f.* FROM fixtures f
            LEFT JOIN results r ON r.fixture_key = f.fixture_key
            WHERE f.kickoff_utc < ? AND r.fixture_key IS NULL
            ORDER BY f.kickoff_utc
            """,
            (before_utc,),
        ).fetchall()

    def record_selection_result(self, sel_id: int, outcome: str, now: datetime) -> bool:
        with self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO selection_results(selection_id, outcome, settled_at_utc) "
                    "VALUES(?,?,?)",
                    (sel_id, outcome, iso(now)),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def selection_results(self, selection_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM selection_results WHERE selection_id = ?", (selection_id,)
        ).fetchall()

    def record_coupon_result(self, coupon_id: str, overall: str, now: datetime) -> bool:
        with self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO coupon_results(coupon_id, overall, settled_at_utc) "
                    "VALUES(?,?,?)",
                    (coupon_id, overall, iso(now)),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def coupon_result(self, coupon_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM coupon_results WHERE coupon_id = ?", (coupon_id,)
        ).fetchone()

    def calibration_records(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM calibration_records ORDER BY id"
        ).fetchall()

    def add_calibration_record(self, rec: dict[str, Any], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO calibration_records(fixture_key, market, outcome,
                    predicted_probability, observed, created_at_utc)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    rec["fixture_key"], rec["market"], rec["outcome"],
                    rec["predicted_probability"], rec["observed"], iso(now),
                ),
            )

    # ---------------- audit / runs ----------------
    def operator_action(self, actor: str, action: str, entity: str, entity_id: str,
                        payload: Any, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO operator_actions(actor, action, entity, entity_id,
                    payload, at_utc) VALUES(?,?,?,?,?,?)
                """,
                (actor, action, entity, entity_id, jdump(payload) if payload is not None else None,
                 iso(now)),
            )

    def operator_actions(self, entity: str | None = None,
                         entity_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM operator_actions WHERE 1=1"
        args: list[Any] = []
        if entity:
            sql += " AND entity = ?"
            args.append(entity)
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        sql += " ORDER BY id DESC LIMIT 200"
        return self.conn.execute(sql, args).fetchall()

    def run_start(self, run_id: str, job: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO run_log(run_id, job, started_at_utc, status) VALUES(?,?,?,'running')",
                (run_id, job, iso(now)),
            )

    def run_finish(self, run_id: str, status: str, summary: dict, errors: list[str],
                   now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE run_log SET finished_at_utc = ?, status = ?, summary = ?, errors = ?
                WHERE run_id = ?
                """,
                (iso(now), status, jdump(summary), jdump(errors), run_id),
            )

    def last_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM run_log ORDER BY started_at_utc DESC LIMIT ?", (limit,)
        ).fetchall()

    def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) n FROM fixtures GROUP BY status"
        ):
            out[f"fixture:{row['status']}"] = row["n"]
        for row in self.conn.execute(
            "SELECT status, COUNT(*) n FROM selections GROUP BY status"
        ):
            out[f"selection:{row['status']}"] = row["n"]
        for row in self.conn.execute(
            "SELECT status, COUNT(*) n FROM coupons GROUP BY status"
        ):
            out[f"coupon:{row['status']}"] = row["n"]
        return out

    def last_covered_date(self, competition_slug: str) -> Optional[date]:
        from datetime import date
        row = self.conn.execute(
            "SELECT kickoff_utc FROM fixtures WHERE competition_slug = ? ORDER BY kickoff_utc DESC LIMIT 1",
            (competition_slug,),
        ).fetchone()
        if row and row["kickoff_utc"]:
            try:
                # ISO string like '2026-09-22T20:00:00Z'
                return date.fromisoformat(row["kickoff_utc"].split("T")[0])
            except Exception:
                return None
        return None

    def count_fixtures_for_day(self, competition_slug: str, day: date) -> int:
        day_str = day.isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM fixtures WHERE competition_slug = ? AND kickoff_utc LIKE ?",
            (competition_slug, f"{day_str}%"),
        ).fetchone()
        return row["n"] if row else 0



def _wrap_with_db_lock(cls):
    """Entoure chaque méthode publique de `with self._lock:`.

    `__init__` est exclu : il *crée* le verrou. Le RLock est réentrant,
    donc les appels internes (méthode → méthode, `execute` → …) sont sans
    danger. Résultat : un objet Database survit aux changements de thread
    de Streamlit et reste cohérent sous accès concurrent.
    """
    for name in list(vars(cls)):
        if name.startswith("_") or not callable(vars(cls)[name]):
            continue
        original = vars(cls)[name]

        @functools.wraps(original)
        def wrapper(self, *args, _m=original, **kwargs):
            with self._lock:
                return _m(self, *args, **kwargs)

        setattr(cls, name, wrapper)
    return cls


Database = _wrap_with_db_lock(Database)
