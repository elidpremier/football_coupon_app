"""Publication Telegram : canal de test uniquement pendant le pilote.

Sécurité :
- le coupon doit être APPROVED (validation humaine) avant toute envoi ;
- anti-doublon : un coupon publié est bloqué par contrôle applicatif +
  contrainte UNIQUE en base (idempotent même après redémarrage) ;
- max N publications par jour (config) ;
- mode sec (dry-run) si pas de token : le PNG + la légende sont écrits
  dans le répertoire de sortie, aucun réseau ;
- taille du PNG et longueur de la légende vérifiées avant l'envoi.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import AppConfig, Secrets
from .http import HttpTransport, RequestsTransport, TransportError
from .render import RenderResult
from .storage import Database
from .utils import today_utc, utcnow


class PublishError(Exception):
    pass


class PublishBlocked(PublishError):
    """Refus de publication (déjà publié, non approuvé, quota journalier)."""


@dataclass
class PublishResult:
    coupon_id: str
    channel: str
    dry_run: bool
    telegram_message_id: Optional[str]
    png_hash: str
    caption: str
    published_at_utc: str


def _multipart(fields: dict[str, str], file_field: str, filename: str,
               file_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----fbcoupon" + uuid.uuid4().hex
    lines: list[bytes] = []
    for k, v in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{k}"'.encode())
        lines.append(b"")
        lines.append(v.encode("utf-8"))
    lines.append(f"--{boundary}".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
    )
    lines.append(f"Content-Type: {content_type}".encode())
    lines.append(b"")
    lines.append(file_bytes)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


class TelegramPublisher:
    def __init__(self, db: Database, config: AppConfig, secrets: Secrets,
                 transport: HttpTransport | None = None,
                 outbox_dir: str = "data/outbox",
                 now_fn=None):
        self.db = db
        self.config = config
        self.secrets = secrets
        self.transport = transport
        self.outbox = Path(outbox_dir)
        self.now_fn = now_fn or utcnow

    # ------------------------------------------------------------ contrôles
    def _check_state(self, coupon_id: str) -> None:
        if self.db.is_published(coupon_id):
            raise PublishBlocked(
                f"coupon {coupon_id} déjà publié (anti-doublon)"
            )
        coupon = self.db.get_coupon(coupon_id)
        if coupon is None:
            raise PublishError(f"coupon inconnu : {coupon_id}")
        if self.config.require_human_approval and coupon["status"] != "APPROVED":
            raise PublishBlocked(
                f"coupon {coupon_id} non approuvé (statut {coupon['status']}) — "
                "validation humaine requise"
            )
        if not self.db.is_published(coupon_id) and coupon["status"] in (
                "PUBLISHED", "SETTLED"):
            raise PublishBlocked("coupon déjà en état publié")
        day = today_utc(self.now_fn())
        if self.db.published_count_for_day(day) >= self.config.coupons.max_published_per_day:
            raise PublishBlocked(
                f"maximum {self.config.coupons.max_published_per_day} coupon(s) "
                f"publié(s) aujourd'hui ({day})"
            )

    # ------------------------------------------------------------ publication
    def publish(self, coupon_id: str, render: RenderResult,
                channel: str | None = None) -> PublishResult:
        now = self.now_fn()
        self._check_state(coupon_id)

        if render.size_bytes > self.config.max_png_bytes:
            raise PublishError(
                f"PNG de {render.size_bytes} o dépasse la limite "
                f"({self.config.max_png_bytes} o)"
            )
        if len(render.caption) > self.config.max_caption_chars:
            raise PublishError("légende trop longue pour Telegram")

        dry_run = not (self.secrets.has_telegram)
        if dry_run:
            self.outbox.mkdir(parents=True, exist_ok=True)
            dst = self.outbox / f"{coupon_id}.png"
            dst.write_bytes(Path(render.path).read_bytes())
            (self.outbox / f"{coupon_id}.caption.txt").write_text(
                render.caption, encoding="utf-8"
            )
            message_id = f"dry-run-{uuid.uuid4().hex[:10]}"
            channel = channel or "dry-run"
        else:
            channel = channel or self.secrets.telegram_test_channel_id
            message_id = self._send_photo(coupon_id, render.path,
                                          render.caption, str(channel))

        ok = self.db.record_publish(
            {
                "coupon_id": coupon_id,
                "channel": channel,
                "dry_run": dry_run,
                "telegram_message_id": message_id,
                "png_hash": render.png_hash,
                "caption": render.caption,
            },
            now,
        )
        if not ok:
            # Filet de sécurité final (contrainte UNIQUE)
            raise PublishBlocked(f"coupon {coupon_id} déjà publié (contrainte base)")
        self.db.set_coupon_status(coupon_id, "PUBLISHED", now,
                                  approved_by="publisher")
        self.db.operator_action(
            "publisher", "publish", "coupon", coupon_id,
            {"channel": channel, "dry_run": dry_run,
             "telegram_message_id": message_id, "png_hash": render.png_hash},
            now,
        )
        return PublishResult(
            coupon_id=coupon_id,
            channel=channel,
            dry_run=dry_run,
            telegram_message_id=message_id,
            png_hash=render.png_hash,
            caption=render.caption,
            published_at_utc=str(now),
        )

    def _send_photo(self, coupon_id: str, png_path: str, caption: str,
                    channel: str) -> str:
        if self.transport is None:
            self.transport = RequestsTransport(max_retries=2)
        url = f"https://api.telegram.org/bot{self.secrets.telegram_bot_token}/sendPhoto"
        body, ctype = _multipart(
            {"chat_id": channel, "caption": caption},
            file_field="photo",
            filename=Path(png_path).name,
            file_bytes=Path(png_path).read_bytes(),
            content_type="image/png",
        )
        try:
            resp = self.transport.send(
                "POST", url,
                headers={"Content-Type": ctype},
                body=body,
                timeout=45.0,
            )
        except Exception as exc:  # transport : timeout, connexion, retries épuisés
            self.db.set_coupon_status(coupon_id, "PUBLISH_FAILED",
                                      self.now_fn(), comment=str(exc)[:300])
            raise PublishError(f"envoi Telegram impossible : {exc}") from exc
        try:
            data = resp.json()
        except Exception as exc:
            self.db.set_coupon_status(coupon_id, "PUBLISH_FAILED",
                                      self.now_fn(),
                                      comment=f"JSON invalide (HTTP {resp.status})")
            raise PublishError(
                f"réponse Telegram illisible (HTTP {resp.status}) : {exc}") from exc
        if resp.status >= 400:
            self.db.set_coupon_status(coupon_id, "PUBLISH_FAILED",
                                      self.now_fn(), comment=resp.text[:300])
            raise PublishError(f"Telegram HTTP {resp.status} : {resp.text[:300]}")
        if not data.get("ok"):
            self.db.set_coupon_status(coupon_id, "PUBLISH_FAILED",
                                      self.now_fn(), comment=str(data)[:300])
            raise PublishError(f"Telegram : {str(data)[:300]}")
        return str(data["result"]["message_id"])
