"""Rendu PNG du coupon (Pillow) + légende Telegram.

Règles visuelles (§13) : typographie uniquement, AUCUN logo d'équipe ;
texte jamais tronqué par débordement (wrapping + troncature contrôlée) ;
légende sous la limite Telegram ; avertissement d'absence de garantie
toujours présent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont
from zoneinfo import ZoneInfo

from .config import AppConfig
from .normalize import Market
from .utils import iso, sha256_bytes, to_decimal, utcnow

WIDTH = 1200
MARGIN = 64
BG = (15, 23, 42)
PANEL = (30, 41, 59)
ACCENT = (251, 191, 36)
TEXT = (248, 250, 252)
MUTED = (148, 163, 184)
RISK = (248, 113, 113)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/DejaVu.ttf",
    "C:/Windows/Fonts/dejavu.ttf",
]

MARKET_FR = {
    "match_winner": "Vainqueur du match (1X2)",
    "double_chance": "Double chance",
    "over_under_2_5": "Total de buts (2,5)",
}
OUTCOME_FR = {
    ("match_winner", "home"): "victoire de",
    ("match_winner", "draw"): "match nul",
    ("match_winner", "away"): "victoire de",
    ("double_chance", "home_draw"): "1X",
    ("double_chance", "home_away"): "12",
    ("double_chance", "away_draw"): "X2",
    ("over_under_2_5", "over"): "plus de 2,5 buts",
    ("over_under_2_5", "under"): "moins de 2,5 buts",
}


def outcome_label(market: str, outcome: str,
                  fixture: dict) -> str:
    label = OUTCOME_FR.get((market, outcome), outcome)
    if market == "match_winner" and outcome in ("home", "away"):
        team = fixture.get("home_team") if outcome == "home" else fixture.get("away_team")
        return f"{label} {team}"
    return label


@dataclass
class RenderResult:
    path: str
    width: int
    height: int
    png_hash: str
    size_bytes: int
    caption: str


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for cand in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # très vieilles versions de Pillow
        return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Troncature contrôlée (guillemet…) pour une seule ligne."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = (text or "").split()
    lines: list[str] = []
    line = ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            # mot plus large que la ligne : troncature contrôlée
            if draw.textlength(w, font=font) > max_width:
                while w and draw.textlength(w + "…", font=font) > max_width:
                    w = w[:-1]
                lines.append(w + "…")
                line = ""
            else:
                line = w
    if line:
        lines.append(line)
    return lines or [""]


def build_caption(coupon: dict, cfg: AppConfig, fixture_by_key: dict[str, dict]) -> str:
    """Légende Telegram courte, dans la limite, avec l'avertissement."""
    lines = [f"🎟 {coupon.get('kind_label', 'Coupon prudent')} — {coupon.get('date_label', '')}"]
    for s in coupon["selections"]:
        fx = fixture_by_key.get(s["fixture_key"], {})
        label = outcome_label(s["market"], s["outcome"], fx)
        lines.append(
            f"• {fx.get('home_team', '?')} vs {fx.get('away_team', '?')} "
            f"({fx.get('kickoff_label', '')}) — {label} @ {s['odds']}"
        )
    lines.append(
        f"Cote combinée : {coupon['combined_odds']} — probabilité ≈ "
        f"{coupon['combined_probability'] * 100:.0f} % (approximation d'indépendance)"
    )
    lines.append(f"Risques majeurs : {coupon.get('risks_summary', 'voir fiche')}")
    lines.append(cfg.responsible_gambling_notice)
    caption = "\n".join(lines)
    if len(caption) > cfg.max_caption_chars:
        notice = cfg.responsible_gambling_notice
        caption = caption[: cfg.max_caption_chars - len(notice) - 2].rstrip() + "\n" + notice
    return caption


def render_coupon_png(coupon: dict, cfg: AppConfig, path: str | Path,
                      fixture_by_key: dict[str, dict]) -> RenderResult:
    """Génère le PNG. `coupon` : dict de type CouponCandidate.to_dict()
    enrichi de 'kind_label', 'date_label', 'risks' (list[str])."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    f_title = _load_font(44)
    f_h2 = _load_font(30)
    f_body = _load_font(24)
    f_small = _load_font(20)
    f_notice = _load_font(22)

    tmp = Image.new("RGB", (WIDTH, 400), BG)
    draw = ImageDraw.Draw(tmp)
    inner_w = WIDTH - 2 * MARGIN

    # ---- calcul de la hauteur nécessaire ----
    def measure() -> int:
        y = MARGIN
        y += 60                      # titre
        y += 34                      # date/version
        y += 18                      # séparateur
        n_sel = len(coupon["selections"])
        for _ in range(n_sel):
            y += 44 + 40 + 40 + 40 + 26   # bloc sélection
            y += 18                      # espacement
        y += 44 + 40 + 30               # bloc combiné
        y += 18
        y += 40                         # titre risques
        for r in coupon.get("risks", ["(aucun risque additionnel déclaré)"]):
            y += 34 * max(1, len(_wrap(draw, r, f_small, inner_w))) + 8
        y += 20
        notice_lines = _wrap(draw, cfg.responsible_gambling_notice, f_notice, inner_w)
        y += 36 * len(notice_lines) + 30
        return y + MARGIN

    height = max(700, min(measure(), 4000))
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    y = MARGIN

    d.text((MARGIN, y), coupon.get("kind_label", "Coupon prudent").upper(),
           font=f_title, fill=ACCENT)
    y += 60
    d.text((MARGIN, y),
           f"{coupon.get('date_label', '')}  ·  modèle {coupon.get('model_version', 'n.c.')}  ·  v{coupon.get('version', 1)}",
           font=f_small, fill=MUTED)
    y += 34
    d.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=PANEL, width=3)
    y += 18

    for s in coupon["selections"]:
        fx = fixture_by_key.get(s["fixture_key"], {})
        d.rounded_rectangle(
            [(MARGIN, y), (WIDTH - MARGIN, y + 160)], radius=14, fill=PANEL
        )
        x = MARGIN + 24
        y0 = y + 16
        d.text((x, y0), _fit(d,
               f"{fx.get('home_team', '?')}  vs  {fx.get('away_team', '?')}",
               f_h2, inner_w - 48), font=f_h2, fill=TEXT)
        y0 += 44
        d.text((x, y0), _fit(d,
               f"{fx.get('competition_label', s['market'])}  ·  {fx.get('kickoff_label', '')}",
               f_small, inner_w - 48), font=f_small, fill=MUTED)
        y0 += 34
        label = outcome_label(s["market"], s["outcome"], fx)
        d.text((x, y0), _fit(d, f"{label}", f_body, inner_w // 2 - 40), font=f_body, fill=TEXT)
        d.text((x + inner_w // 2 + 20, y0), _fit(d,
               f"cote {s['odds']}   ·   p ≈ {s['probability'] * 100:.1f} %",
               f_body, inner_w // 2 - 60), font=f_body, fill=ACCENT)
        y0 += 40
        odds_at = s.get("odds_observed_label", "")
        d.text((x, y0), _fit(d,
               f"cote observée {odds_at}  ·  qualité {s.get('quality_label', 'n.c.')}",
               f_small, inner_w - 48), font=f_small, fill=MUTED)
        y += 160 + 18

    # ---- bloc combiné ----
    d.rounded_rectangle([(MARGIN, y), (WIDTH - MARGIN, y + 114)], radius=14, fill=PANEL)
    x = MARGIN + 24
    d.text((x, y + 16), "COMBINÉ", font=f_h2, fill=ACCENT)
    d.text((x, y + 58),
           f"cote combinée {coupon['combined_odds']}   ·   probabilité ≈ "
           f"{coupon['combined_probability'] * 100:.1f} % (indépendance supposée)",
           font=f_small, fill=TEXT)
    y += 114 + 18

    # ---- risques ----
    d.text((MARGIN, y), "RISQUES MAJEURS", font=f_h2, fill=RISK)
    y += 44
    for r in coupon.get("risks", ["(aucun risque additionnel déclaré)"]):
        for line in _wrap(d, r, f_small, inner_w):
            d.text((MARGIN + 8, y), "•  " + line, font=f_small, fill=MUTED)
            y += 34
        y += 8
    y += 12

    # ---- avertissement ----
    for line in _wrap(d, cfg.responsible_gambling_notice, f_notice, inner_w):
        d.text((MARGIN, y), line, font=f_notice, fill=ACCENT)
        y += 36

    img.save(path, "PNG", optimize=True)
    data = path.read_bytes()

    caption = build_caption(coupon, cfg, fixture_by_key)
    return RenderResult(
        path=str(path),
        width=WIDTH,
        height=height,
        png_hash=sha256_bytes(data),
        size_bytes=len(data),
        caption=caption,
    )
