"""Tests : SQLite — migrations, idempotence, anti-doublon publication,
sauvegarde."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from football.storage import Database, SCHEMA_VERSION
from football.utils import iso, utcnow

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    yield d
    d.close()


class TestMigrations:
    def test_schema_version_after_migrate(self, db):
        assert db.schema_version() == SCHEMA_VERSION

    def test_migrate_idempotent(self, db):
        v1 = db.schema_version()
        db.migrate()
        db.migrate()
        assert db.schema_version() == v1

    def test_all_expected_tables_exist(self, db):
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("fixtures", "odds_snapshots", "team_snapshots",
                  "availability_snapshots", "source_observations",
                  "match_features", "analyses", "selections", "coupons",
                  "results", "api_usage", "published_events", "run_log",
                  "operator_actions", "calibration_records"):
            assert t in tables

    def test_fresh_db_from_file(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        d1 = Database(path)
        d1.close()
        d2 = Database(path)  # réouverture : pas de re-migration cassée
        assert d2.schema_version() == SCHEMA_VERSION
        d2.close()


class TestFixtures:
    def test_upsert_fixture_and_get(self, db):
        fx = {
            "fixture_key": "premier_league|2026-09-20T18:00:00Z|a|b",
            "competition_slug": "premier_league",
            "competition_name": "PL",
            "kickoff_utc": iso(NOW),
            "home_team": "A", "away_team": "B",
            "status": "COLLECTED", "matching_status": "OK",
            "primary_provider": "p", "primary_fixture_id": "1",
        }
        db.upsert_fixture(fx, NOW)
        row = db.get_fixture(fx["fixture_key"])
        assert row is not None
        assert row["home_team"] == "A"

    def test_upsert_updates_status(self, db):
        fx = {
            "fixture_key": "k", "competition_slug": "premier_league",
            "kickoff_utc": iso(NOW), "home_team": "A", "away_team": "B",
            "status": "COLLECTED", "matching_status": "OK",
        }
        db.upsert_fixture(fx, NOW)
        db.set_fixture_status("k", "ANALYZED", NOW)
        assert db.get_fixture("k")["status"] == "ANALYZED"

    def test_fixtures_between(self, db):
        for i, h in enumerate((18, 19, 20)):
            db.upsert_fixture({
                "fixture_key": f"k{i}", "competition_slug": "premier_league",
                "kickoff_utc": f"2026-09-20T{h:02d}:00:00Z",
                "home_team": f"H{i}", "away_team": f"A{i}",
                "status": "COLLECTED", "matching_status": "OK",
            }, NOW)
        rows = db.fixtures_between("2026-09-20T18:30:00Z", "2026-09-20T20:30:00Z")
        assert [r["fixture_key"] for r in rows] == ["k1", "k2"]


class TestOdds:
    def test_upsert_odds_idempotent(self, db):
        snap = {
            "fixture_key": "k", "provider": "p", "bookmaker": "b",
            "market": "match_winner", "outcome": "home", "odds": "2.10",
            "observed_at_utc": iso(NOW), "payload_hash": "h",
        }
        assert db.upsert_odds([snap]) >= 1
        assert db.upsert_odds([snap]) == 0  # doublon exact → ignoré
        rows = db.latest_odds("k")
        assert len(rows) == 1

    def test_latest_odds_picks_newest(self, db):
        db.upsert_odds([{
            "fixture_key": "k", "provider": "p", "bookmaker": "b",
            "market": "match_winner", "outcome": "home", "odds": "2.10",
            "observed_at_utc": "2026-09-20T10:00:00Z", "payload_hash": "h1",
        }])
        db.upsert_odds([{
            "fixture_key": "k", "provider": "p", "bookmaker": "b",
            "market": "match_winner", "outcome": "home", "odds": "2.05",
            "observed_at_utc": "2026-09-20T11:00:00Z", "payload_hash": "h2",
        }])
        latest = db.latest_odds("k")
        assert len(latest) == 1
        assert latest[0]["odds"] == "2.05"
        assert len(db.all_odds("k")) == 2


class TestPublishIdempotency:
    def test_publish_once_then_blocked(self, db):
        ok = db.record_publish({
            "coupon_id": "c1", "channel": "test", "dry_run": True,
            "telegram_message_id": "m1", "png_hash": "h", "caption": "x",
        }, NOW)
        assert ok is True
        assert db.is_published("c1")
        assert db.record_publish({
            "coupon_id": "c1", "channel": "test", "dry_run": True,
            "telegram_message_id": "m2", "png_hash": "h", "caption": "x",
        }, NOW) is False
        # même après « redémarrage » (nouvelle connexion)
        db2 = Database(db.path)
        assert db2.is_published("c1")
        assert db2.record_publish({
            "coupon_id": "c1", "channel": "test", "dry_run": True,
            "telegram_message_id": "m3", "png_hash": "h", "caption": "x",
        }, NOW) is False
        db2.close()

    def test_two_coupons_both_publishable(self, db):
        for cid in ("c1", "c2"):
            assert db.record_publish({
                "coupon_id": cid, "channel": "test", "dry_run": True,
                "telegram_message_id": cid, "png_hash": "h", "caption": "x",
            }, NOW)
        assert db.published_count_for_day("2026-09-20") == 2


class TestSelections:
    def test_one_selection_per_market_outcome(self, db):
        s = {
            "fixture_key": "k", "market": "match_winner", "outcome": "home",
            "odds": "2.10", "odds_observed_at_utc": iso(NOW),
            "probability": 0.45, "model_version": "m", "score": 80,
            "status": "eligible", "reasons": [], "justification": "j",
            "source_refs": ["p"],
        }
        sid = db.upsert_selection(s, NOW)
        sid2 = db.upsert_selection({**s, "odds": "2.05", "score": 82}, NOW)
        assert sid == sid2  # même ligne, mise à jour
        assert len(db.conn.execute(
            "SELECT * FROM selections WHERE fixture_key='k'").fetchall()) == 1
        row = db.selection_by_id(sid)
        assert row["odds"] == "2.05"


class TestUsage:
    def test_usage_consume_accumulates(self, db):
        assert db.usage_consume("api_football", "2026-09-20", 85, NOW) == 1
        assert db.usage_consume("api_football", "2026-09-20", 85, NOW) == 2
        assert db.usage("api_football", "2026-09-20")["requests"] == 2

    def test_errors_counted_separately(self, db):
        db.usage_consume("p", "2026-09-20", 85, NOW, error=True)
        u = db.usage("p", "2026-09-20")
        assert u["errors"] == 1
        assert u["requests"] == 0

    def test_new_day_reset(self, db):
        db.usage_consume("p", "2026-09-19", 85, NOW)
        assert db.usage("p", "2026-09-20")["requests"] == 0


class TestBackupAndAudit:
    def test_backup_creates_consistent_copy(self, db, tmp_path):
        db.upsert_fixture({
            "fixture_key": "k", "competition_slug": "premier_league",
            "kickoff_utc": iso(NOW), "home_team": "A", "away_team": "B",
            "status": "COLLECTED", "matching_status": "OK",
        }, NOW)
        target = tmp_path / "backups" / "b.db"
        db.backup(target)
        assert target.exists()
        import sqlite3

        con = sqlite3.connect(str(target))
        n = con.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
        con.close()
        assert n == 1

    def test_operator_action_logged(self, db):
        db.operator_action("admin", "approve", "coupon", "c1",
                           {"note": "ok"}, NOW)
        rows = db.operator_actions(entity="coupon", entity_id="c1")
        assert len(rows) == 1
        assert rows[0]["actor"] == "admin"

    def test_run_log_lifecycle(self, db):
        db.run_start("r1", "collect", NOW)
        db.run_finish("r1", "ok", {"a": 1}, [], NOW)
        runs = db.last_runs()
        assert runs[0]["status"] == "ok"
        assert runs[0]["summary"] is not None


class TestThreading:
    """Régression : Streamlit réexécute le script dans un thread différent
    à chaque session/rerun, alors que l'instance Database (st.cache_resource)
    est conservée. La connexion doit survivre au changement de thread et
    rester cohérente sous accès concurrent."""

    @pytest.fixture
    def thread_db(self, tmp_path):
        d = Database(str(tmp_path / "t.db"))
        yield d
        d.close()

    def test_created_in_one_thread_used_in_another(self, thread_db):
        db = thread_db
        db.upsert_fixture({
            "fixture_key": "k1", "competition_slug": "premier_league",
            "kickoff_utc": iso(NOW), "home_team": "A", "away_team": "B",
            "status": "COLLECTED", "matching_status": "OK",
        }, NOW)
        errors: list[BaseException] = []
        inserted = []
        ins_lock = __import__("threading").Lock()

        def worker():
            try:
                rows = db.fixtures_between("1970-01-01T00:00:00Z",
                                           "2999-01-01T00:00:00Z")
                assert len(rows) == 1
                assert db.usage("api_football", "2026-09-20")["requests"] == 0
                n = db.upsert_odds([{
                    "fixture_key": "k1", "provider": "p",
                    "bookmaker": "b", "market": "match_winner",
                    "outcome": "home", "odds": "2.10",
                    "observed_at_utc": iso(NOW),
                    "payload_hash": "h1",
                }])
                assert n in (0, 1)
                with ins_lock:
                    if n == 1:
                        inserted.append(1)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [__import__("threading").Thread(target=worker)
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len(inserted) == 1  # un seul insert effectif…
        # …les 7 autres sont dédupliqués par la contrainte UNIQUE
        assert len(db.latest_odds("k1")) == 1

    def test_concurrent_writers_no_lost_update(self, thread_db):
        db = thread_db
        import threading as _th

        def writer(i: int):
            for j in range(25):
                db.upsert_team_snapshot({
                    "fixture_key": f"fx{i}", "team": f"T{i}-{j}",
                    "side": "home", "position": j, "points": j,
                    "played": j, "wins": 0, "draws": 0, "losses": j,
                    "goals_for": j, "goals_against": 0,
                    "form_last5": "",
                }, NOW)

        barrier = _th.Barrier(4)
        errors: list[BaseException] = []

        def guarded(i: int):
            barrier.wait()
            try:
                writer(i)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [_th.Thread(target=guarded, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        total = sum(len(db.team_snapshots(f"fx{i}")) for i in range(4))
        assert total == 100
