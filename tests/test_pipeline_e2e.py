"""Tests d'intégration : parcours complet sur données démo.

collecte → analyse → coupons → validation → rendu → publication simulée
→ règlement → métriques. Plus : panne fournisseur, reprise sans
doublon, quota de sécurité.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from football.config import Secrets
from football.pipeline import Pipeline
from football.providers.base import ProviderError
from football.providers.base import BaseProvider, PFixture
from football.providers.demo import DemoProvider
from football.render import render_coupon_png
from football.storage import Database
from football.telegram import (
    PublishBlocked,
    TelegramPublisher,
)
from tests.conftest import NOW, DAY

UTC = timezone.utc


@pytest.fixture
def env(tmp_path, config):
    db = Database(str(tmp_path / "e2e.db"))
    now_box = {"t": datetime(2026, 9, 19, 8, 0, tzinfo=UTC)}
    secrets = Secrets(db_path=str(tmp_path / "e2e.db"),
                      outbox_dir=str(tmp_path / "outbox"))

    def providers():
        # journée démo = jour courant : les coups d'envoi (16 h–21 h UTC)
        # sont dans le futur vu de 08 h 00 UTC → analyser est légitime
        d = now_box["t"].date()
        return {
            "demo_primary": DemoProvider("demo_primary", day=d, now=now_box["t"]),
            "demo_secondary": DemoProvider("demo_secondary", day=d,
                                           now=now_box["t"]),
        }

    pipe = Pipeline(db, config, secrets, providers=providers(),
                    now_fn=lambda: now_box["t"])
    yield {"db": db, "pipe": pipe, "secrets": secrets, "config": config,
           "tmp": tmp_path, "now": now_box, "provs": pipe.providers}
    db.close()


def advance_past_day(env, day) -> None:
    """Fait passer le temps après la fin de `day` (pour le règlement),
    y compris dans l'horloge interne des fournisseurs démo."""
    t = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1, hours=8)
    env["now"]["t"] = t
    for p in env["provs"].values():
        p._now = t


class TestFullJourney:
    def test_morning_chain(self, env):
        pipe = env["pipe"]
        reports = pipe.run_morning()
        assert reports["collect"].status == "ok"
        assert reports["analyse"].status in ("ok", "partial")
        assert reports["coupons"].status == "ok"
        c = reports["collect"].summary
        assert c["fixtures"] == 8
        assert c["incoherent"] == 1          # Brentford (divergence 15 min)
        # 2 sources × 8 issues/match (3×1X2 + 3×DC + 2×OU) × 8 matchs
        assert c["odds"] >= 8 * 8
        assert c["standings"] >= 16          # 8 matchs × 2 équipes
        assert reports["coupons"].summary["coupons"] >= 1

    def test_incoherent_fixture_never_selected(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        row = db.conn.execute(
            "SELECT * FROM fixtures WHERE matching_status='INCOHERENT'"
        ).fetchone()
        assert row is not None
        sels = db.conn.execute(
            "SELECT * FROM selections WHERE fixture_key=? AND status='eligible'",
            (row["fixture_key"],),
        ).fetchall()
        assert sels == []  # jamais éligible

    def test_stale_fixture_excluded(self, env):
        from football.utils import jload

        env["pipe"].run_morning()
        db = env["db"]
        # le match aux cotes de 20 h est exclu avec une raison documentée
        rows = db.conn.execute(
            "SELECT * FROM selections WHERE status='exclude'"
        ).fetchall()
        assert rows
        stale = [
            r for r in rows
            if any("vieille de 20" in (x or "") for x in jload(r["reasons"] or "[]"))
        ]
        assert stale, "le match à cotes périmées (20 h) doit être exclu"

    def test_eligible_selections_have_data(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        el = db.selections_by_status("eligible")
        assert len(el) >= 2
        for s in el:
            assert 0.0 < s["probability"] < 1.0
            assert Decimal(s["odds"]) > Decimal("1")
            assert s["odds_observed_at_utc"], "cote horodatée obligatoire"
            assert s["model_version"] == "market_normalized_v1"
            assert s["score"] >= env["config"].quality.eligible_threshold

    def test_coupon_candidates_respect_constraints(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        coupons = db.coupons_by_status("REVIEW_REQUIRED")
        assert coupons
        import json

        for c in coupons:
            sels = json.loads(c["selections_json"])
            assert len(sels) == 2  # mode pilote : 2 max
            keys = {s["fixture_key"] for s in sels}
            assert len(keys) == 2  # une par match
            # aucune équipe commune
            teams = set()
            for s in sels:
                fx = db.get_fixture(s["fixture_key"])
                assert fx["home_team"] not in teams
                assert fx["away_team"] not in teams
                teams.add(fx["home_team"])
                teams.add(fx["away_team"])
            # écart ≥ 90 min (même compétition)
            from football.utils import parse_iso

            kocks = [parse_iso(db.get_fixture(k)["kickoff_utc"]) for k in keys]
            assert abs((kocks[0] - kocks[1]).total_seconds()) >= 90 * 60
            joint = 1.0
            for s in sels:
                joint *= s["probability"]
            assert c["combined_probability"] == pytest.approx(joint, abs=1e-4)
            assert float(c["combined_odds"]) > 1

    def test_approve_render_publish_dry(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        config = env["config"]
        coupons = db.coupons_by_status("REVIEW_REQUIRED")
        c = coupons[0]
        db.set_coupon_status(c["coupon_id"], "APPROVED",
                             env["now"]["t"], approved_by="admin")

        # rendu
        import json

        sels = json.loads(c["selections_json"])
        fx_by_key = {}
        disp_sels = []
        for s in sels:
            fx = db.get_fixture(s["fixture_key"])
            fx_by_key[s["fixture_key"]] = {
                "home_team": fx["home_team"], "away_team": fx["away_team"],
                "kickoff_label": "19/09 19:00",
                "competition_label": fx["competition_slug"],
            }
            disp_sels.append({**s,
                              "odds_observed_label": "19/09 07:00",
                              "quality_label": f"{s.get('score', 'n.c.')}"})
        disp = {"selections": disp_sels,
                "combined_odds": c["combined_odds"],
                "combined_probability": c["combined_probability"],
                "kind_label": "Coupon prudent",
                "date_label": "2026-09-19",
                "model_version": "marché", "version": 1,
                "risks": ["risque a", "risque b"], "risks_summary": "risque a"}
        render = render_coupon_png(disp, config,
                                   env["tmp"] / "c.png", fx_by_key)
        assert render.size_bytes < config.max_png_bytes
        assert "aucun résultat garanti" in render.caption

        # publication (mode sec)
        pub = TelegramPublisher(db, config, env["secrets"],
                                outbox_dir=str(env["tmp"] / "outbox"),
                                now_fn=lambda: env["now"]["t"])
        res = pub.publish(c["coupon_id"], render)
        assert res.dry_run is True
        assert db.is_published(c["coupon_id"])
        with pytest.raises(PublishBlocked):
            pub.publish(c["coupon_id"], render)

    def test_publish_requires_approval(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        c = db.coupons_by_status("REVIEW_REQUIRED")[0]
        pub = TelegramPublisher(db, env["config"], env["secrets"],
                                outbox_dir=str(env["tmp"] / "o"),
                                now_fn=lambda: env["now"]["t"])
        from football.render import RenderResult

        fake = RenderResult(path=str(env["tmp"] / "x.png"), width=100,
                            height=100, png_hash="h", size_bytes=10,
                            caption="ok")
        with pytest.raises(PublishBlocked, match="approuvé"):
            pub.publish(c["coupon_id"], fake)

    def test_settle_updates_results_and_metrics(self, env):
        from datetime import date as _date

        db, pipe = env["db"], env["pipe"]
        pipe.run_morning()
        # le lendemain matin : règlement (journée terminée)
        advance_past_day(env, _date(2026, 9, 19))
        rep = pipe.run_settle()
        assert rep.status in ("ok", "partial")
        assert rep.summary["results"] == 8
        res = db.conn.execute("SELECT COUNT(*) n FROM results").fetchone()
        assert res["n"] == 8
        calib = db.calibration_records()
        assert len(calib) > 0
        for r in calib:
            assert r["observed"] in (0, 1)
            assert 0.0 < r["predicted_probability"] < 1.0

    def test_settle_idempotent(self, env):
        from datetime import date as _date

        env["pipe"].run_morning()
        advance_past_day(env, _date(2026, 9, 19))
        env["pipe"].run_settle()
        n1 = len(env["db"].calibration_records())
        env["pipe"].run_settle()  # 2e fois : pas de doublon
        n2 = len(env["db"].calibration_records())
        assert n1 == n2
        sr = env["db"].conn.execute(
            "SELECT COUNT(*) n FROM selection_results").fetchone()
        sels = env["db"].conn.execute(
            "SELECT COUNT(*) n FROM selections").fetchone()
        # chaque sélection résolue une seule fois
        assert sr["n"] <= sels["n"]

    def test_coupon_settled_with_result(self, env):
        from datetime import date as _date

        env["pipe"].run_morning()
        db = env["db"]
        c = db.coupons_by_status("REVIEW_REQUIRED")[0]
        db.set_coupon_status(c["coupon_id"], "APPROVED",
                             env["now"]["t"], approved_by="admin")
        advance_past_day(env, _date(2026, 9, 19))
        env["pipe"].run_settle()
        row = db.coupon_result(c["coupon_id"])
        assert row is not None
        assert row["overall"] in ("won", "lost", "partial")
        assert db.get_coupon(c["coupon_id"])["status"] == "SETTLED"


class TestRecoveryAndQuota:
    def test_fallback_competitions_are_used_when_priority_is_empty(self, env):
        """Une journée sans match prioritaire doit rester analysable grâce
        aux compétitions de repli couvertes par les deux fournisseurs."""
        from dataclasses import replace

        class FallbackProvider(BaseProvider):
            def __init__(self, name):
                self.name = name

            def fetch_fixtures(self, day, competition):
                if competition != "bundesliga":
                    return []
                return [PFixture(
                    provider=self.name, provider_fixture_id=f"{self.name}-1",
                    competition_slug=competition, competition_name="Bundesliga",
                    home_team="Bayern", away_team="Dortmund",
                    kickoff_utc=datetime(2026, 9, 19, 18, 0, tzinfo=UTC),
                    status="NS", venue="Teststadion",
                )]

        config = replace(
            env["config"], primary_provider="custom_primary",
            secondary_provider="custom_secondary",
            competitions=("premier_league",),
            fallback_competitions=("bundesliga",),
        )
        pipe = Pipeline(
            env["db"], config, env["secrets"],
            providers={
                "custom_primary": FallbackProvider("custom_primary"),
                "custom_secondary": FallbackProvider("custom_secondary"),
            },
            now_fn=lambda: env["now"]["t"],
        )

        report = pipe.run_collect(env["now"]["t"].date())

        assert report.status == "ok"
        assert report.summary["fallback_used"] == 1
        assert report.summary["fixtures"] == 1
        assert env["db"].get_fixture(
            "bundesliga|2026-09-19T18:00:00Z|bayern|dortmund"
        ) is not None

    def test_secondary_provider_down_degrades_gracefully(self, env):
        """Panne du fournisseur secondaire : collecte partielle (1 source),
        aucune donnée inventée, matchs toujours analysables."""
        db, pipe = env["db"], env["pipe"]

        class BrokenSecondary(DemoProvider):
            def fetch_fixtures(self, day, comp):
                raise ProviderError("secondaire indisponible")

            def fetch_odds(self, day, comp):
                raise ProviderError("secondaire indisponible")

            def fetch_standings(self, comp, season):
                raise ProviderError("secondaire indisponible")

            def fetch_availability(self, day, comp):
                raise ProviderError("secondaire indisponible")

            def fetch_results(self, day, comp):
                raise ProviderError("secondaire indisponible")

        pipe.providers["demo_secondary"] = BrokenSecondary(
            "demo_secondary", day=(NOW - timedelta(days=1)).date(),
            now=env["now"]["t"])
        reports = pipe.run_morning()
        assert reports["collect"].status == "partial"
        assert any("secondaire" in e for e in reports["collect"].errors)
        # les matchs sont collectés (source primaire seule)
        assert reports["collect"].summary["fixtures"] == 8
        # analyse : accord réduit (0.6) mais pas d'exclusion pour ça
        fxs = db.conn.execute("SELECT * FROM fixtures").fetchall()
        for fx in fxs:
            assert fx["secondary_provider"] is None

    def test_primary_provider_down_fails_loudly(self, env):
        db, pipe = env["db"], env["pipe"]

        class BrokenPrimary(DemoProvider):
            def fetch_fixtures(self, day, comp):
                raise ProviderError("primaire en panne")

        pipe.providers["demo_primary"] = BrokenPrimary(
            "demo_primary", day=(NOW - timedelta(days=1)).date(),
            now=env["now"]["t"])
        reports = pipe.run_morning()
        # la chaîne s'arrête après la collecte (pas d'analyse à vide)
        assert reports["collect"].status == "partial"
        assert "analyse" not in reports

    def test_quota_exceeded_reported_with_message(self, env, config):
        """Budget à 1 → le 2e appel API déclenche le message de
        report, sans dégradation silencieuse."""
        from football.config import Secrets as S

        db = Database(str(env["tmp"] / "quota.db"))
        now_box = env["now"]
        d = (now_box["t"] - timedelta(days=1)).date()

        # provider « primaire » dont chaque appel consomme le budget d'un
        # client partagé de budget 1
        from football.http import ApiClient, Cache, RateLimiter
        from tests.conftest import FakeTransport

        calls = {"n": 0}

        def handler(method, url, headers, params, body):
            calls["n"] += 1
            return {"response": []}

        transport = FakeTransport(handler)
        shared_limiter = RateLimiter(db, now_fn=lambda: now_box["t"])

        class BudgetedDemo(DemoProvider):
            def _acquire(self):
                shared_limiter.acquire("demo", 1)
                shared_limiter.record("demo", 1)

            def fetch_fixtures(self, day, comp):
                self._acquire()
                return super().fetch_fixtures(day, comp)

            def fetch_odds(self, day, comp):
                self._acquire()
                return super().fetch_odds(day, comp)

            def fetch_standings(self, comp, season):
                self._acquire()
                return super().fetch_standings(comp, season)

            def fetch_availability(self, day, comp):
                self._acquire()
                return super().fetch_availability(day, comp)

        provs = {
            "demo_primary": BudgetedDemo("demo_primary", day=d,
                                         now=now_box["t"]),
            "demo_secondary": DemoProvider("demo_secondary", day=d,
                                           now=now_box["t"]),
        }
        pipe = Pipeline(db, config, env["secrets"], providers=provs,
                        now_fn=lambda: now_box["t"])
        reports = pipe.run_morning()
        assert reports["collect"].status == "quota_exceeded"
        assert reports["collect"].message == (
            "Collecte reportée : quota de sécurité atteint")
        assert "analyse" not in reports
        db.close()

    def test_no_duplicate_fixtures_on_recollect(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        n1 = db.conn.execute("SELECT COUNT(*) n FROM fixtures").fetchone()["n"]
        n_odds1 = db.conn.execute(
            "SELECT COUNT(*) n FROM odds_snapshots").fetchone()["n"]
        # re-collecte le même jour (redémarrage / rattrapage)
        env["pipe"].run_collect(env["now"]["t"].date())
        n2 = db.conn.execute("SELECT COUNT(*) n FROM fixtures").fetchone()["n"]
        n_odds2 = db.conn.execute(
            "SELECT COUNT(*) n FROM odds_snapshots").fetchone()["n"]
        assert n1 == n2 == 8
        assert n_odds1 == n_odds2  # pas de doublon de cotes

    def test_recollect_after_restart_no_duplicate_coupons(self, env):
        pipe, db = env["pipe"], env["db"]
        pipe.run_morning()
        n1 = db.conn.execute("SELECT COUNT(*) n FROM coupons").fetchone()["n"]
        # nouvelle chaîne complète (ex. machine rallumée)
        pipe.run_morning()
        n2 = db.conn.execute("SELECT COUNT(*) n FROM coupons").fetchone()["n"]
        assert n1 == n2, "les coupons équivalents ne doivent pas se dupliquer"

    def test_run_log_records_every_job(self, env):
        env["pipe"].run_morning()
        env["now"]["t"] = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
        env["pipe"].run_settle()
        jobs = {r["job"] for r in env["db"].last_runs(10)}
        assert {"collect", "analyse", "coupons", "settle"} <= jobs

    def test_all_critical_data_timestamped(self, env):
        env["pipe"].run_morning()
        db = env["db"]
        rows = db.conn.execute(
            "SELECT observed_at_utc FROM odds_snapshots"
        ).fetchall()
        assert rows
        assert all(r["observed_at_utc"] for r in rows)
        obs = db.conn.execute(
            "SELECT fetched_at_utc FROM source_observations"
        ).fetchall()
        # chaque observation source est horodatée
        assert all(o["fetched_at_utc"] for o in obs)

    def test_no_forbidden_promises_in_any_output(self, env):
        """Aucune sortie publiée ne promet un gain (règle absolue)."""
        import json
        import re

        env["pipe"].run_morning()
        db = env["db"]
        patterns = [r"\bgaranti", r"\binfaillible", r"\bcoup\s+sûr",
                    r"\bbet\s+safe"]
        for c in db.all_coupons(50):
            assert c["notice"], "l'avertissement est obligatoire"
            assert any(p in c["notice"].lower() for p in ("garanti",))
        for a in db.conn.execute("SELECT * FROM analyses").fetchall():
            for col in ("analyst_json", "critic_json"):
                text = a[col] or ""
                for pat in patterns:
                    assert not re.search(pat, text, re.IGNORECASE), \
                        f"mot interdit dans {col}"
