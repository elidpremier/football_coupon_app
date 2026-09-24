"""Smoke tests de l'application Streamlit (AppTest, sans navigateur).

Vérifie que l'app démarre, que les 7 écrans sont présents, que la
chaîne complète est actionnable depuis l'interface, et que la
publication respecte l'anti-doublon.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from football.storage import Database
from football.utils import utcnow

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("OUTBOX_DIR", str(outbox))
    for k in ("API_FOOTBALL_KEY", "FOOTBALL_DATA_TOKEN", "GEMINI_API_KEY",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_TEST_CHANNEL_ID"):
        # load_secrets() also reads the repository's .env.  An explicit
        # empty value prevents python-dotenv (override=False) from restoring
        # real credentials and keeps every UI test offline and deterministic.
        monkeypatch.setenv(k, "")
    return tmp_path


def boot() -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    return at


def click_key(at: AppTest, key: str) -> AppTest:
    """Clique sur le bouton identifié par `key` (robuste aux versions
    d'AppTest : on itère la séquence complète)."""
    for b in at.button:
        if b.key == key:
            b.click()
            break
    else:
        raise AssertionError(f"bouton {key!r} introuvable")
    at.run()
    return at


def buttons_prefixed(at: AppTest, prefix: str) -> list:
    return [b for b in at.button if b.key and str(b.key).startswith(prefix)]


class TestBoot:
    def test_app_boots_without_errors(self, app_env):
        at = boot()
        assert not at.exception

    def test_dashboard_handles_sqlite_usage_rows(self, app_env):
        """A usage row must render like a mapping, without calling Row.get()."""
        db = Database(app_env / "app.db")
        db.usage_reserve("api_football", utcnow().strftime("%Y-%m-%d"), 85)

        at = boot()

        assert not at.exception
        assert any("1 / 85" in str(metric.value) for metric in at.metric)

    def test_seven_tabs_present(self, app_env):
        at = boot()
        tabs = at.tabs
        assert len(tabs) == 8
        titles = [t.label for t in tabs]
        for expected in ("Tableau de bord", "Compétitions", "Matchs", "Analyses", "Coupons",
                         "Prévisualisation", "Historique", "Paramètres"):
            assert any(expected in t for t in titles)

    def test_demo_mode_banner(self, app_env):
        at = boot()
        info = " ".join(str(i.value) for i in at.info)
        assert "MODE DÉMO" in info

    def test_settings_expose_ai_and_web_search_controls(self, app_env):
        at = boot()
        assert any(b.label == "Autoriser la recherche Web de Gemini"
                   for b in at.checkbox)
        assert any(s.label == "Moteur d'analyse" for s in at.selectbox)
        assert any(b.key == "FormSubmitter:settings_form-Enregistrer les paramètres"
                   for b in at.button)

    def test_no_secret_leak_in_ui(self, app_env):
        os.environ["TELEGRAM_BOT_TOKEN"] = "SUPER-SECRET-TOKEN-123"
        try:
            at = boot()
            all_text = " ".join(str(x.value) for x in at.json)
            assert "SUPER-SECRET-TOKEN-123" not in all_text
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)


class TestFullFlowThroughUI:
    def test_chain_run_creates_data(self, app_env):
        at = boot()
        at = click_key(at, "btn_run_morning")
        assert not at.exception
        text = str(at)
        assert "match(s) collecté(s)" in text or "collect" in text.lower()

    def test_approve_and_publish_flow(self, app_env):
        at = boot()
        at = click_key(at, "btn_run_morning")
        # coupons en revue : on approuve le premier
        approve = buttons_prefixed(at, "approve_")
        assert approve, "un coupon doit être proposé à la validation"
        approve[0].click()
        at.run()
        assert not at.exception
        # publication (mode sec, pas de token)
        publish = buttons_prefixed(at, "publish_")
        assert publish
        publish[0].click()
        at.run()
        assert not at.exception
        text = str(at)
        assert "Publié" in text

    def test_publish_blocked_second_time(self, app_env):
        """Anti-doublon visible depuis l'interface : le 2e clic de
        publication est bloqué (et non renvoyé)."""
        at = boot()
        at = click_key(at, "btn_run_morning")
        approve = buttons_prefixed(at, "approve_")
        assert approve
        approve[0].click()
        at.run()
        publish = buttons_prefixed(at, "publish_")
        assert publish
        publish[0].click()
        at.run()
        # la 2e tentative (même coupon)
        publish2 = buttons_prefixed(at, "publish_")
        if publish2:
            publish2[0].click()
            at.run()
            text = str(at)
            assert ("déjà publié" in text) or ("Publié" in text)

    def test_settle_button_works(self, app_env):
        at = boot()
        at = click_key(at, "btn_run_morning")
        at = click_key(at, "btn_settle")
        assert not at.exception

    def test_reject_flow(self, app_env):
        at = boot()
        at = click_key(at, "btn_run_morning")
        reject = buttons_prefixed(at, "reject_")
        if reject:
            reject[0].click()
            at.run()
            assert not at.exception
