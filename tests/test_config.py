"""Tests : chargement et validation de la configuration."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from football.config import (
    ConfigError,
    Secrets,
    load_config,
    load_secrets,
    write_config,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def valid_yaml(tmp_path) -> Path:
    base = yaml.safe_load((ROOT / "config" / "football.yaml").read_text())
    p = tmp_path / "football.yaml"
    p.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return p


def write_cfg(tmp_path, d) -> Path:
    p = tmp_path / "football.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return p


class TestLoad:
    def test_repo_config_is_valid(self):
        cfg = load_config(ROOT / "config" / "football.yaml")
        assert cfg.timezone == "Africa/Ouagadougou"
        assert cfg.mode == "pilot"
        assert cfg.require_human_approval is True
        assert cfg.competitions == ("premier_league", "la_liga", "serie_a")
        assert cfg.fallback_competitions == (
            "bundesliga", "ligue_1", "eredivisie", "champions_league",
            "europe_league",
        )
        assert cfg.ai_mode == "gemini_free"

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "nope.yaml")

    def test_config_hash_changes_with_content(self, valid_yaml, tmp_path):
        c1 = load_config(valid_yaml)
        text = valid_yaml.read_text() + "\n# commentaire"
        p = tmp_path / "v2.yaml"
        p.write_text(text, encoding="utf-8")
        c2 = load_config(p)
        assert c1.config_hash != c2.config_hash

    def test_pilot_max_selections_two(self):
        cfg = load_config(ROOT / "config" / "football.yaml")
        assert cfg.max_selections() == 2


class TestValidationErrors:
    def test_bad_timezone(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["project"]["timezone"] = "Mars/Olympus"
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_four_competitions(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["competitions"] = ["premier_league", "la_liga", "serie_a", "ligue_1"]
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_unknown_competition(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["competitions"] = ["mars_league"]
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_fallback_must_be_cross_provider_compatible(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["fallback_competitions"] = ["coppa_italia"]
        with pytest.raises(ConfigError, match="deux fournisseurs"):
            load_config(write_cfg(tmp_path, d))

    def test_unknown_market(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["markets"] = ["corners"]
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_budget_over_hundred(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["providers"]["api_football_daily_budget"] = 101
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_same_primary_secondary(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["providers"]["secondary"] = "api_football"
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_inverted_thresholds(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["quality"]["eligible_threshold"] = 60
        d["quality"]["watch_threshold"] = 70
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_weights_sum_not_one(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["quality"]["weights"]["freshness"] = 0.5
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_selection_weights_not_100(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["selection"]["weight_data_quality"] = 40
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_web_search_is_configurable(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["ai"]["enable_web_search"] = True
        assert load_config(write_cfg(tmp_path, d)).ai_enable_web_search is True

    def test_write_config_is_validated_and_atomic(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["ai"]["mode"] = "off"
        config = write_config(valid_yaml, d)
        assert config.ai_mode == "off"
        assert load_config(valid_yaml).ai_mode == "off"

    def test_bad_ai_mode(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["ai"]["mode"] = "gpt-5"
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_notice_mandatory(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["publishing"]["responsible_gambling_notice"] = "Bonne chance !"
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_poisson_forbidden_in_pilot(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["probabilities"]["source"] = "poisson"
        with pytest.raises(ConfigError, match="pilote"):
            load_config(write_cfg(tmp_path, d))

    def test_coupons_bounds(self, tmp_path, valid_yaml):
        d = yaml.safe_load(valid_yaml.read_text())
        d["coupons"]["pilot_max_selections"] = 5
        with pytest.raises(ConfigError):
            load_config(write_cfg(tmp_path, d))

    def test_non_mapping_root(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(p)


class TestSecrets:
    def test_masking_never_reveals_full_key(self):
        s = Secrets(api_football_key="1234567890abcdef")
        masked = s.masked()
        assert "1234567890abcdef" not in masked["API_FOOTBALL_KEY"]
        assert "1234" in masked["API_FOOTBALL_KEY"]

    def test_masking_absent_key(self):
        s = Secrets()
        assert s.masked()["TELEGRAM_BOT_TOKEN"] == "(absente)"

    def test_capability_flags(self):
        s = Secrets(api_football_key="x", telegram_bot_token="y",
                    telegram_test_channel_id="-100123")
        assert s.has_api_football
        assert s.has_telegram
        assert not s.has_football_data
        assert not s.has_gemini
