"""Tests : publication Telegram simulée — état requis, anti-doublon,
mode sec, limites de taille, multipart, échecs réseau."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from football.render import render_coupon_png
from football.storage import Database
from football.telegram import (
    PublishBlocked,
    PublishError,
    TelegramPublisher,
    _multipart,
)
from tests.conftest import FakeTransport, NOW, DAY
from football.utils import iso, utcnow


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "tg.db"))
    yield d
    d.close()


@pytest.fixture
def secrets_with_token():
    from football.config import Secrets

    return Secrets(telegram_bot_token="TOKEN-TEST",
                   telegram_test_channel_id="-1001234567890",
                   outbox_dir="/tmp/fbca-outbox")


@pytest.fixture
def clock_state():
    return {"t": NOW}


@pytest.fixture
def publisher(db, config, secrets, clock_state, tmp_path):
    """Éditeur en MODE SEC (aucun token) — écrit dans l'outbox."""
    return TelegramPublisher(db, config, secrets,
                             outbox_dir=str(tmp_path / "outbox"),
                             now_fn=lambda: clock_state["t"])


def make_coupon_row(db, status="APPROVED", coupon_id=None):
    cid = coupon_id or str(uuid.uuid4())
    db.create_coupon({
        "coupon_id": cid,
        "kind": "cautious",
        "selections": [
            {"fixture_key": "k1", "market": "match_winner", "outcome": "home",
             "odds": "2.10", "odds_observed_at_utc": iso(NOW),
             "probability": 0.47, "score": 84},
            {"fixture_key": "k2", "market": "over_under_2_5", "outcome": "over",
             "odds": "1.95", "odds_observed_at_utc": iso(NOW),
             "probability": 0.51, "score": 83},
        ],
        "combined_odds": "4.09",
        "combined_probability": 0.24,
        "score": 84.0,
        "status": "REVIEW_REQUIRED",
        "version": 1,
        "constraints": ["2 sélections"],
        "notice": "Estimation probabiliste — aucun résultat garanti.",
    }, NOW)
    if status != "REVIEW_REQUIRED":
        db.set_coupon_status(cid, status, NOW,
                             approved_by="admin" if status == "APPROVED" else None)
    return cid


def make_render(tmp_path):
    from football.config import load_config

    # tolère un chemin de dossier ou un chemin .png explicite
    target = (Path(tmp_path) / "r.png") if not str(tmp_path).endswith(".png") \
        else Path(tmp_path)
    cfg = load_config(Path(__file__).resolve().parent.parent / "config" / "football.yaml")
    coupon = {
        "coupon_id": "x",
        "selections": [
            {"fixture_key": "k1", "market": "match_winner", "outcome": "home",
             "odds": "2.10", "odds_observed_at_utc": iso(NOW),
             "odds_observed_label": "20-09 12:00", "probability": 0.47,
             "score": 84, "quality_label": "84/100"},
        ],
        "combined_odds": "2.10",
        "combined_probability": 0.47,
        "kind_label": "Coupon prudent",
        "date_label": "2026-09-20",
        "model_version": "m",
        "version": 1,
        "risks": ["un risque"],
        "risks_summary": "un risque",
    }
    fixtures = {
        "k1": {"home_team": "Home", "away_team": "Away",
               "kickoff_label": "20/09 19:00",
               "competition_label": "Premier League"},
    }
    return render_coupon_png(coupon, cfg, target, fixtures)


class TestStateChecks:
    def test_unapproved_coupon_blocked(self, publisher, db, config, tmp_path):
        cid = make_coupon_row(db, status="REVIEW_REQUIRED")
        render = make_render(tmp_path)
        with pytest.raises(PublishBlocked, match="approuvé"):
            publisher.publish(cid, render)

    def test_require_human_approval_cannot_be_bypassed(self, publisher, db, tmp_path):
        # même si on force APPROVED manuellement en base, la re-vérification
        # d'état passe ; et PUBLISHED est bloqué
        cid = make_coupon_row(db, status="APPROVED")
        db.set_coupon_status(cid, "PUBLISHED", NOW)
        render = make_render(tmp_path)
        with pytest.raises(PublishBlocked):
            publisher.publish(cid, render)

    def test_unknown_coupon(self, publisher, db, tmp_path):
        with pytest.raises(PublishError, match="inconnu"):
            publisher.publish("inexistant", make_render(tmp_path))


class TestAntiDoublePublish:
    def test_second_publish_blocked(self, publisher, db, tmp_path):
        cid = make_coupon_row(db, status="APPROVED")
        render = make_render(tmp_path)
        res = publisher.publish(cid, render)
        assert res.dry_run is True
        with pytest.raises(PublishBlocked, match="anti-doublon"):
            publisher.publish(cid, render)

    def test_blocked_even_after_reopen(self, db, config, secrets, tmp_path):
        p1 = TelegramPublisher(db, config, secrets,
                               outbox_dir=str(tmp_path / "o1"), now_fn=lambda: NOW)
        cid = make_coupon_row(db, status="APPROVED")
        p1.publish(cid, make_render(tmp_path))
        # « redémarrage » : nouvelle instance, même base
        p2 = TelegramPublisher(db, config, secrets,
                               outbox_dir=str(tmp_path / "o2"), now_fn=lambda: NOW)
        with pytest.raises(PublishBlocked):
            p2.publish(cid, make_render(tmp_path / "r3.png"))

    def test_db_constraint_backstop(self, db, config, secrets, tmp_path,
                                    monkeypatch):
        """Même si le contrôle applicatif est contourné (état corrompu,
        course), la contrainte UNIQUE de la base bloque quand même
        (filet final, double publication impossible)."""
        p = TelegramPublisher(db, config, secrets,
                              outbox_dir=str(tmp_path / "o"), now_fn=lambda: NOW)
        cid = make_coupon_row(db, status="APPROVED")
        # insertion directe de l'événement (simulation d'un état corrompu)
        assert db.record_publish({
            "coupon_id": cid, "channel": "x", "dry_run": True,
            "telegram_message_id": "m", "png_hash": "h", "caption": "c",
        }, NOW)
        # on contourne volontairement la vérification applicative :
        # seule la contrainte de base doit maintenant bloquer
        monkeypatch.setattr(p, "_check_state", lambda _cid: None)
        with pytest.raises(PublishBlocked, match="contrainte base"):
            p.publish(cid, make_render(tmp_path))
        # et l'événement n'a pas été dupliqué
        events = db.conn.execute(
            "SELECT COUNT(*) FROM published_events WHERE coupon_id = ?",
            (cid,)).fetchone()[0]
        assert events == 1


class TestDailyCap:
    def test_max_one_per_day(self, publisher, db, tmp_path):
        cid1 = make_coupon_row(db, status="APPROVED")
        cid2 = make_coupon_row(db, status="APPROVED")
        publisher.publish(cid1, make_render(tmp_path))
        with pytest.raises(PublishBlocked, match="maximum"):
            publisher.publish(cid2, make_render(tmp_path / "r4.png"))


class TestDryRun:
    def test_dry_run_writes_outbox(self, publisher, db, tmp_path):
        cid = make_coupon_row(db, status="APPROVED")
        res = publisher.publish(cid, make_render(tmp_path))
        outbox = Path(publisher.outbox)
        assert (outbox / f"{cid}.png").exists()
        assert (outbox / f"{cid}.caption.txt").exists()
        assert res.dry_run is True
        assert res.telegram_message_id.startswith("dry-run-")

    def test_dry_run_records_event(self, publisher, db, tmp_path):
        cid = make_coupon_row(db, status="APPROVED")
        publisher.publish(cid, make_render(tmp_path))
        assert db.is_published(cid)
        events = db.published_events()
        assert events[0]["dry_run"] == 1
        assert db.get_coupon(cid)["status"] == "PUBLISHED"


class TestRealSend:
    def _publisher_with_token(self, db, config, secrets_with_token,
                              transport, tmp_path):
        return TelegramPublisher(db, config, secrets_with_token,
                                 transport=transport,
                                 outbox_dir=str(tmp_path / "o"),
                                 now_fn=lambda: NOW)

    def test_sends_multipart_photo(self, db, config, secrets_with_token,
                                   tmp_path):
        captured = {}

        def handler(method, url, headers, params, body):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = body
            return {"ok": True, "result": {"message_id": 42}}

        transport = FakeTransport(handler)
        p = self._publisher_with_token(db, config, secrets_with_token,
                                       transport, tmp_path)
        cid = make_coupon_row(db, status="APPROVED")
        res = p.publish(cid, make_render(tmp_path))
        assert res.dry_run is False
        assert res.telegram_message_id == "42"
        assert captured["url"].startswith(
            "https://api.telegram.org/botTOKEN-TEST/sendPhoto")
        assert captured["url"].endswith("/sendPhoto")
        assert "multipart/form-data" in captured["headers"]["Content-Type"]
        assert b"chat_id" in captured["body"]
        assert b"-1001234567890" in captured["body"]
        assert b"image/png" in captured["body"]
        assert b"photo" in captured["body"]

    def test_http_error_sets_publish_failed(self, db, config,
                                            secrets_with_token, tmp_path):
        def handler(method, url, headers, params, body):
            return (429, {"ok": False, "description": "Too Many Requests"})

        transport = FakeTransport(handler)
        p = self._publisher_with_token(db, config, secrets_with_token,
                                       transport, tmp_path)
        cid = make_coupon_row(db, status="APPROVED")
        with pytest.raises(PublishError, match="429"):
            p.publish(cid, make_render(tmp_path))
        assert db.get_coupon(cid)["status"] == "PUBLISH_FAILED"
        assert not db.is_published(cid)

    def test_network_failure_sets_publish_failed(self, db, config,
                                                 secrets_with_token, tmp_path):
        def handler(method, url, headers, params, body):
            raise ConnectionError("refusé")

        transport = FakeTransport(handler)
        p = self._publisher_with_token(db, config, secrets_with_token,
                                       transport, tmp_path)
        cid = make_coupon_row(db, status="APPROVED")
        with pytest.raises(PublishError):
            p.publish(cid, make_render(tmp_path))
        assert db.get_coupon(cid)["status"] == "PUBLISH_FAILED"

    def test_telegram_not_ok_payload(self, db, config, secrets_with_token,
                                     tmp_path):
        def handler(method, url, headers, params, body):
            return {"ok": False, "description": "chat not found"}

        transport = FakeTransport(handler)
        p = self._publisher_with_token(db, config, secrets_with_token,
                                       transport, tmp_path)
        cid = make_coupon_row(db, status="APPROVED")
        with pytest.raises(PublishError, match="chat not found"):
            p.publish(cid, make_render(tmp_path))
        assert not db.is_published(cid)

    def test_fails_then_retry_possible(self, db, config, secrets_with_token,
                                       tmp_path):
        """Un PUBLISH_FAILED ne doit pas bloquer une nouvelle tentative
        (le coupon retourne en APPROVED pour l'opérateur)."""
        def handler(method, url, headers, params, body):
            return (500, {"ok": False, "description": "boom"})

        transport = FakeTransport(handler)
        p = self._publisher_with_token(db, config, secrets_with_token,
                                       transport, tmp_path)
        cid = make_coupon_row(db, status="APPROVED")
        with pytest.raises(PublishError):
            p.publish(cid, make_render(tmp_path))
        db.set_coupon_status(cid, "APPROVED", NOW, approved_by="admin")
        transport.handler = lambda *a: {"ok": True, "result": {"message_id": 7}}
        res = p.publish(cid, make_render(tmp_path / "retry.png"))
        assert res.telegram_message_id == "7"


class TestSizeLimits:
    def test_caption_too_long_rejected_before_send(self, publisher, db, tmp_path):
        cid = make_coupon_row(db, status="APPROVED")
        render = make_render(tmp_path)
        long_caption = "x" * (publisher.config.max_caption_chars + 10)
        from football.render import RenderResult

        bad = RenderResult(path=render.path, width=render.width,
                           height=render.height, png_hash=render.png_hash,
                           size_bytes=render.size_bytes, caption=long_caption)
        with pytest.raises(PublishError, match="légende"):
            publisher.publish(cid, bad)

    def test_oversized_png_rejected(self, publisher, db, tmp_path):
        cid = make_coupon_row(db, status="APPROVED")
        render = make_render(tmp_path)
        from football.render import RenderResult

        bad = RenderResult(path=render.path, width=render.width,
                           height=render.height, png_hash=render.png_hash,
                           size_bytes=publisher.config.max_png_bytes + 1,
                           caption=render.caption)
        with pytest.raises(PublishError, match="dépasse"):
            publisher.publish(cid, bad)


class TestMultipart:
    def test_structure_and_boundary(self):
        body, ctype = _multipart({"chat_id": "-100", "caption": "texte"},
                                 "photo", "c.png", b"\x89PNG", "image/png")
        boundary = ctype.split("boundary=")[1]
        assert body.startswith(f"--{boundary}".encode())
        assert body.rstrip(b"\r\n").endswith(f"--{boundary}--".encode())
        assert b'name="chat_id"' in body
        assert b"-100" in body
        assert b"\x89PNG" in body
        assert b"filename=\"c.png\"" in body
        # les champs textuels sont encodés en UTF-8
        body2, _ = _multipart({"caption": "Probabilité ≈ 45 %"},
                              "photo", "c.png", b"\x89PNG", "image/png")
        assert "Probabilité ≈ 45 %".encode("utf-8") in body2
