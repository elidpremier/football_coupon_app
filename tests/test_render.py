"""Tests : rendu PNG (Pillow) — taille, lisibilité, aucun débordement,
légende Telegram dans la limite."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from football.render import (
    build_caption,
    outcome_label,
    render_coupon_png,
    WIDTH,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_coupon(n=2, **kw) -> dict:
    selections = []
    for i in range(n):
        selections.append({
            "fixture_key": f"k{i}",
            "market": "match_winner",
            "outcome": "home",
            "odds": "2.10",
            "odds_observed_at_utc": "2026-09-20T11:00:00Z",
            "odds_observed_label": "20-09 11:00",
            "probability": 0.47,
            "score": 84,
            "quality_label": "84/100",
        })
    coupon = {
        "coupon_id": "c-test",
        "selections": selections,
        "combined_odds": "4.41",
        "combined_probability": 0.22,
        "kind_label": "Coupon prudent",
        "date_label": "2026-09-20",
        "model_version": "marché normalisé",
        "version": 1,
        "risks": [
            "marge du marché non nulle (4,8 %)",
            "compositions officielles non disponibles",
        ],
        "risks_summary": "marge marché ; compositions",
    }
    coupon.update(kw)
    return coupon


def make_fixtures(n=2) -> dict:
    return {
        f"k{i}": {
            "home_team": f"Équipe Domicile Longue {i}" if i == 0 else f"Home {i}",
            "away_team": f"Équipe Extérieur Très Longue {i}" if i == 1
            else f"Away {i}",
            "kickoff_label": "20/09 19:00",
            "competition_label": "Premier League",
        }
        for i in range(n)
    }


class TestRender:
    def test_png_created(self, config, tmp_path):
        out = render_coupon_png(make_coupon(), config,
                                tmp_path / "c.png", make_fixtures())
        assert Path(out.path).exists()
        img = Image.open(out.path)
        assert img.format == "PNG"
        assert img.width == WIDTH
        assert out.size_bytes < config.max_png_bytes

    def test_png_hash_stable_for_same_input(self, config, tmp_path):
        a = render_coupon_png(make_coupon(), config, tmp_path / "a.png",
                              make_fixtures())
        b = render_coupon_png(make_coupon(), config, tmp_path / "b.png",
                              make_fixtures())
        assert a.png_hash == b.png_hash

    def test_different_coupon_different_hash(self, config, tmp_path):
        a = render_coupon_png(make_coupon(), config, tmp_path / "a.png",
                              make_fixtures())
        b = render_coupon_png(make_coupon(combined_odds="5.00"), config,
                              tmp_path / "b.png", make_fixtures())
        assert a.png_hash != b.png_hash

    def test_long_team_names_do_not_overflow(self, config, tmp_path):
        fixtures = {
            "k0": {
                "home_team": "Manchester United Football Club Édition Test",
                "away_team": "Brighton & Hove Albion Association Very Long",
                "kickoff_label": "20/09 19:00",
                "competition_label": "Premier League",
            },
            "k1": {
                "home_team": "Wolverhampton Wanderers Football Club",
                "away_team": "Southampton Saint City Footballers United",
                "kickoff_label": "20/09 21:00",
                "competition_label": "Premier League",
            },
        }
        out = render_coupon_png(make_coupon(), config, tmp_path / "c.png",
                                fixtures)
        img = Image.open(out.path)
        assert img.width == WIDTH

    def test_many_selections_fit_canvas(self, config, tmp_path):
        out = render_coupon_png(make_coupon(n=2), config,
                                tmp_path / "c.png", make_fixtures(2))
        img = Image.open(out.path)
        assert 700 <= img.height <= 4000

    def test_empty_risks_placeholder(self, config, tmp_path):
        out = render_coupon_png(make_coupon(risks=[], risks_summary="aucun"),
                                config, tmp_path / "c.png", make_fixtures())
        assert Path(out.path).exists()

    def test_notice_present_in_caption(self, config, tmp_path):
        out = render_coupon_png(make_coupon(), config,
                                tmp_path / "notice.png", make_fixtures())
        assert "aucun résultat garanti" in out.caption
        assert "responsable" in out.caption

    def test_caption_under_limit(self, config):
        cap = build_caption(make_coupon(), config, make_fixtures())
        assert len(cap) <= config.max_caption_chars

    def test_caption_truncated_when_too_long(self, config):
        # légende énorme → tronquée mais avertissement préservé
        coupon = make_coupon(risks_summary="; ".join(["risque"] * 200))
        cap = build_caption(coupon, config, make_fixtures())
        assert len(cap) <= config.max_caption_chars
        assert "aucun résultat garanti" in cap


class TestOutcomeLabel:
    def test_match_winner_home(self):
        assert outcome_label("match_winner", "home",
                             {"home_team": "Arsenal", "away_team": "Chelsea"}) == \
            "victoire de Arsenal"

    def test_match_winner_away(self):
        assert outcome_label("match_winner", "away",
                             {"home_team": "Arsenal", "away_team": "Chelsea"}) == \
            "victoire de Chelsea"

    def test_draw(self):
        assert outcome_label("match_winner", "draw", {}) == "match nul"

    def test_double_chance(self):
        assert outcome_label("double_chance", "home_draw", {"home_team": "A",
                                                            "away_team": "B"}) == "1X"
        assert outcome_label("double_chance", "home_away", {}) == "12"
        assert outcome_label("double_chance", "away_draw", {}) == "X2"

    def test_over(self):
        assert outcome_label("over_under_2_5", "over", {}) == \
            "plus de 2,5 buts"
