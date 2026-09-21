"""Tâche planifiée locale (cron / Task Scheduler / manuelle).

Usage :
    python -m football.scheduler morning     # collecte + analyse + coupons
    python -m football.scheduler refresh     # rafraîchissement ciblé
    python -m football.scheduler settle      # résultats + métriques
    python -m football.scheduler all         # morning puis settle
    python -m football.scheduler status      # état en bref

Le planning est exprimé dans le fuseau du projet ; les données restent
en UTC. Exemple de crontab (fuseau UTC) :
    15 8 * * *  cd /chemin/football_coupon_app && python -m football.scheduler morning >> data/cron.log 2>&1
    45 15 * * * cd /chemin/football_coupon_app && python -m football.scheduler refresh >> data/cron.log 2>&1
    30 23 * * * cd /chemin/football_coupon_app && python -m football.scheduler settle >> data/cron.log 2>&1
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config, load_secrets
from .pipeline import Pipeline
from .storage import Database


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load(args) -> tuple:
    cfg_path = Path(args.config) if args.config else _root() / "config" / "football.yaml"
    config = load_config(cfg_path)
    secrets = load_secrets(_root() / ".env")
    return config, secrets


def _banner(report) -> None:
    print(f"[{report.job}] {report.status.upper()} — {report.message}")
    for e in report.errors[:10]:
        print(f"  ! {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="football-scheduler")
    parser.add_argument("job", choices=["morning", "refresh", "settle", "all", "status"])
    parser.add_argument("--config", default=None, help="chemin du YAML (défaut config/football.yaml)")
    args = parser.parse_args(argv)

    try:
        config, secrets = _load(args)
    except ConfigError as exc:
        print(f"CONFIGURATION INVALIDE : {exc}", file=sys.stderr)
        return 2

    db_path = Path(secrets.db_path)
    if not db_path.is_absolute():
        db_path = _root() / db_path
    db = Database(db_path)

    if args.job == "status":
        counts = db.counts_by_status()
        from .utils import today_utc

        usage = db.usage_all(today_utc())
        print("Compteurs :", counts or "(aucune donnée)")
        for row in usage:
            print(f"Quota {row['provider']} : {row['requests']}/{row['budget']} "
                  f"(erreurs : {row['errors']})")
        last = db.last_runs(3)
        for r in last:
            print(f"Dernier run : {r['job']} {r['status']} à {r['started_at_utc']}")
        db.close()
        return 0

    pipeline = Pipeline(db, config, secrets)

    if args.job == "morning":
        reports = pipeline.run_morning()
        bad = 0
        for name in ("collect", "analyse", "coupons"):
            if name in reports:
                _banner(reports[name])
                if reports[name].status in ("failed", "quota_exceeded"):
                    bad = 2 if reports[name].status == "quota_exceeded" else 1
        db.backup(db_path.parent / "backups" / f"football-{datetime.now():%Y%m%d}.db")
        db.close()
        return bad

    if args.job == "refresh":
        report = pipeline.run_refresh()
        _banner(report)
        db.close()
        return 0 if report.ok() else 1

    if args.job == "settle":
        report = pipeline.run_settle()
        _banner(report)
        db.backup(db_path.parent / "backups" / f"football-{datetime.now():%Y%m%d}.db")
        db.close()
        return 0 if report.ok() else 1

    if args.job == "all":
        rc = 0
        reports = pipeline.run_morning()
        for name in ("collect", "analyse", "coupons"):
            if name in reports:
                _banner(reports[name])
                rc = max(rc, 2 if reports[name].status == "quota_exceeded"
                         else (1 if reports[name].status == "failed" else 0))
        report = pipeline.run_settle()
        _banner(report)
        db.backup(db_path.parent / "backups" / f"football-{datetime.now():%Y%m%d}.db")
        db.close()
        return rc

    parser.error("job inconnu")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
