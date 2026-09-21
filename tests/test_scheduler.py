"""Tests : CLI du scheduler (jobs, codes de retour, idempotence)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from football.scheduler import main as sched_main
from tests.conftest import NOW


@pytest.fixture
def workspace(tmp_path, config, monkeypatch):
    """Réplique l'arborescence attendue par le scheduler dans un tmpdir."""
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "football.yaml").write_text(
        (tmp_path / "_cfg_src").read_text() if (tmp_path / "_cfg_src").exists()
        else "", encoding="utf-8")
    # copie de la config du dépôt
    from pathlib import Path

    repo_cfg = Path(__file__).resolve().parent.parent / "config" / "football.yaml"
    (root / "config" / "football.yaml").write_text(
        repo_cfg.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "data").mkdir()
    db = str(root / "data" / "test.db")
    monkeypatch.setenv("DB_PATH", db)
    monkeypatch.setenv("OUTBOX_DIR", str(root / "data" / "outbox"))
    for k in ("API_FOOTBALL_KEY", "FOOTBALL_DATA_TOKEN", "GEMINI_API_KEY",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_TEST_CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)
    return root, db


def test_status_empty_workspace(workspace):
    root, db = workspace
    rc = sched_main(["status", "--config", str(root / "config" / "football.yaml")])
    assert rc == 0


def test_morning_job_runs_and_idempotent(workspace):
    root, db = workspace
    cfg = str(root / "config" / "football.yaml")
    rc1 = sched_main(["morning", "--config", cfg])
    assert rc1 == 0
    import sqlite3

    con = sqlite3.connect(db)
    n1 = con.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
    n_coupons1 = con.execute("SELECT COUNT(*) FROM coupons").fetchone()[0]
    con.close()
    rc2 = sched_main(["morning", "--config", cfg])
    assert rc2 == 0
    con = sqlite3.connect(db)
    n2 = con.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
    n_coupons2 = con.execute("SELECT COUNT(*) FROM coupons").fetchone()[0]
    con.close()
    assert n1 == n2  # pas de doublon de matchs
    assert n_coupons1 == n_coupons2  # pas de doublon de coupons


def test_morning_creates_backup(workspace):
    root, db = workspace
    cfg = str(root / "config" / "football.yaml")
    sched_main(["morning", "--config", cfg])
    backups = list((root / "data" / "backups").glob("football-*.db"))
    assert backups, "une sauvegarde quotidienne doit être créée"


def test_settle_job_runs(workspace):
    root, db = workspace
    cfg = str(root / "config" / "football.yaml")
    sched_main(["morning", "--config", cfg])
    rc = sched_main(["settle", "--config", cfg])
    assert rc == 0  # rien à régler (matchs à venir) → ok


def test_bad_config_returns_2(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("project: {mode: pilot}\n", encoding="utf-8")
    monkeypatch.delenv("DB_PATH", raising=False)
    rc = sched_main(["status", "--config", str(bad)])
    assert rc == 2


def test_refresh_job_runs(workspace):
    root, db = workspace
    cfg = str(root / "config" / "football.yaml")
    sched_main(["morning", "--config", cfg])
    rc = sched_main(["refresh", "--config", cfg])
    assert rc == 0
