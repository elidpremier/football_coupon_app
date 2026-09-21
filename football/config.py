"""Chargement et validation de la configuration (YAML) et des secrets (.env).

Règle : la configuration est versionnée (hash du fichier) et validée au
chargement ; une config invalide empêche tout run (fail fast).
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

from .utils import sha256_text

KNOWN_COMPETITIONS = {
    "premier_league",
    "la_liga",
    "serie_a",
    "bundesliga",
    "ligue_1",
    "eredivisie",
    "champions_league",
    "europe_league",
    "coppa_italia",
    "premier_league_cup",
}
# Compétitions présentes dans les deux fournisseurs utilisés par défaut.
# Les coupes exclusivement disponibles via API-Football sont volontairement
# exclues du repli : une analyse sans contrôle croisé ne doit pas être forcée.
COMMON_PROVIDER_COMPETITIONS = {
    "premier_league", "la_liga", "serie_a", "bundesliga", "ligue_1",
    "eredivisie", "champions_league", "europe_league",
}

KNOWN_MARKETS = {"match_winner", "double_chance", "over_under_2_5"}
KNOWN_AI_MODES = {"off", "gemini_free", "ollama_local"}
KNOWN_PROB_SOURCES = {"market", "poisson"}
REQUIRED_WEIGHT_KEYS = (
    "freshness",
    "source_agreement",
    "odds_coverage",
    "team_data_coverage",
    "availability_confidence",
)


class ConfigError(Exception):
    """Configuration absente ou invalide."""


@dataclass(frozen=True)
class QualityConfig:
    eligible_threshold: float
    watch_threshold: float
    max_odds_age_hours: float
    kickoff_tolerance_minutes: int
    min_selection_probability: float
    weights: dict[str, float]
    contradiction_penalty: float


@dataclass(frozen=True)
class SelectionConfig:
    weight_data_quality: int
    weight_source_agreement: int
    weight_odds_stability: int
    weight_probability: int
    weight_explanation: int
    explanation_score_ai_validated: float
    explanation_score_template: float


@dataclass(frozen=True)
class CouponConfig:
    pilot_max_selections: int
    public_max_selections: int
    max_published_per_day: int
    prohibit_same_fixture: bool
    prohibit_same_team: bool
    same_context_minutes: int
    max_candidates_displayed: int


@dataclass(frozen=True)
class AppConfig:
    raw_path: str
    config_hash: str
    timezone: str
    mode: str
    require_human_approval: bool
    competitions: tuple[str, ...]
    fallback_competitions: tuple[str, ...]
    markets: tuple[str, ...]
    primary_provider: str
    secondary_provider: str
    api_football_daily_budget: int
    api_football_per_minute: Optional[int]
    football_data_daily_budget: int
    football_data_per_minute: int
    http_timeout_seconds: int
    max_retries: int
    quality: QualityConfig
    selection: SelectionConfig
    coupons: CouponConfig
    ai_mode: str
    ai_require_valid_source_refs: bool
    ai_enable_web_search: bool
    ai_prompt_version: str
    telegram_test_channel_env: str
    max_caption_chars: int
    max_png_bytes: int
    responsible_gambling_notice: str
    prob_source: str
    prob_model_version: str
    poisson_max_goals: int
    refresh_window_minutes: int

    def max_selections(self) -> int:
        if self.mode == "public":
            return self.coupons.public_max_selections
        return self.coupons.pilot_max_selections

    def budget_for(self, provider: str) -> int:
        if provider == "api_football":
            return self.api_football_daily_budget
        if provider == "football_data":
            return self.football_data_daily_budget
        return 500


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"config : champ manquant {ctx}.{key}")
    return d[key]


def _validate_tz(tz: str) -> str:
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(tz)
    except Exception as exc:
        raise ConfigError(f"timezone invalide : {tz!r}") from exc
    return tz


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"fichier de configuration introuvable : {p}")
    text = p.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise ConfigError("config : le YAML racine doit être un mapping")

    project = _require(cfg, "project", "racine")
    timezone = _validate_tz(_require(project, "timezone", "project"))
    mode = _require(project, "mode", "project")
    if mode not in {"pilot", "public"}:
        raise ConfigError(f"project.mode invalide : {mode!r}")
    require_human = bool(project.get("require_human_approval", True))

    competitions = cfg.get("competitions") or []
    # API-Football demande les cotes et indisponibilités par match. Trois
    # championnats majeurs conservent une marge sous le budget quotidien de
    # 85 appels, là où cinq championnats pourraient l'épuiser un week-end.
    if not (1 <= len(competitions) <= 3):
        raise ConfigError("competitions : 1 à 3 compétitions au maximum")
    unknown = set(competitions) - KNOWN_COMPETITIONS
    if unknown:
        raise ConfigError(f"competitions inconnues : {sorted(unknown)}")

    fallback_competitions = cfg.get("fallback_competitions") or []
    if not isinstance(fallback_competitions, list):
        raise ConfigError("fallback_competitions : une liste est attendue")
    fallback_unknown = set(fallback_competitions) - COMMON_PROVIDER_COMPETITIONS
    if fallback_unknown:
        raise ConfigError(
            "fallback_competitions : compétitions non couvertes par les deux "
            f"fournisseurs : {sorted(fallback_unknown)}"
        )
    overlap = set(competitions) & set(fallback_competitions)
    if overlap:
        raise ConfigError(
            "fallback_competitions : déjà présente(s) dans competitions : "
            f"{sorted(overlap)}"
        )

    markets = cfg.get("markets") or []
    if not markets or not set(markets) <= KNOWN_MARKETS:
        raise ConfigError(
            f"markets invalides : {sorted(set(markets) - KNOWN_MARKETS)} "
            f"(autorisées : {sorted(KNOWN_MARKETS)})"
        )

    prov = _require(cfg, "providers", "racine")
    primary = _require(prov, "primary", "providers")
    secondary = _require(prov, "secondary", "providers")
    if primary == secondary:
        raise ConfigError("providers : primaire et secondaire doivent différer")
    aff_budget = int(_require(prov, "api_football_daily_budget", "providers"))
    if not (0 < aff_budget <= 100):
        raise ConfigError("providers.api_football_daily_budget doit être dans 1..100")
    aff_pm = prov.get("api_football_per_minute")
    fd_budget = int(prov.get("football_data_daily_budget", 200))
    fd_pm = int(prov.get("football_data_per_minute", 10))
    timeout = int(prov.get("http_timeout_seconds", 25))
    retries = int(prov.get("max_retries", 3))
    if not (1 <= timeout <= 300) or not (0 <= retries <= 8):
        raise ConfigError("providers : http_timeout_seconds/max_retries hors bornes")

    q = _require(cfg, "quality", "racine")
    eligible = float(_require(q, "eligible_threshold", "quality"))
    watch = float(_require(q, "watch_threshold", "quality"))
    max_age = float(_require(q, "max_odds_age_hours", "quality"))
    tol = int(q.get("kickoff_tolerance_minutes", 5))
    min_p = float(q.get("min_selection_probability", 0.45))
    if not (0 < watch < eligible <= 100):
        raise ConfigError("quality : il faut 0 < watch_threshold < eligible_threshold <= 100")
    if not (0 < max_age <= 72):
        raise ConfigError("quality.max_odds_age_hours doit être dans 0..72")
    if not (0.3 <= min_p < 1.0):
        raise ConfigError("quality.min_selection_probability doit être dans [0.3, 1)")
    if tol < 0:
        raise ConfigError("quality.kickoff_tolerance_minutes >= 0")
    weights = _require(q, "weights", "quality")
    missing = set(REQUIRED_WEIGHT_KEYS) - set(weights)
    if missing:
        raise ConfigError(f"quality.weights : champs manquants {sorted(missing)}")
    wsum = sum(float(weights[k]) for k in REQUIRED_WEIGHT_KEYS)
    if abs(wsum - 1.0) > 1e-6:
        raise ConfigError(f"quality.weights : la somme doit valoir 1.0 (actuel {wsum})")

    s = _require(cfg, "selection", "racine")
    w_dq = int(_require(s, "weight_data_quality", "selection"))
    w_sa = int(_require(s, "weight_source_agreement", "selection"))
    w_os = int(_require(s, "weight_odds_stability", "selection"))
    w_p = int(_require(s, "weight_probability", "selection"))
    w_e = int(_require(s, "weight_explanation", "selection"))
    if w_dq + w_sa + w_os + w_p + w_e != 100:
        raise ConfigError("selection : la somme des poids doit valoir 100")
    exp_ai = float(s.get("explanation_score_ai_validated", 1.0))
    exp_tpl = float(s.get("explanation_score_template", 0.90))
    if not (0 <= exp_tpl <= exp_ai <= 1):
        raise ConfigError("selection : 0 <= gabarit <= IA validée <= 1")

    c = _require(cfg, "coupons", "racine")
    pilot = int(_require(c, "pilot_max_selections", "coupons"))
    pub = int(_require(c, "public_max_selections", "coupons"))
    per_day = int(_require(c, "max_published_per_day", "coupons"))
    if not (2 <= pilot <= 3) or not (pilot <= pub <= 5):
        raise ConfigError("coupons : 2 <= pilot_max <= public_max <= 5")
    if per_day < 1:
        raise ConfigError("coupons.max_published_per_day >= 1")

    ai = _require(cfg, "ai", "racine")
    ai_mode = _require(ai, "mode", "ai")
    if isinstance(ai_mode, bool):
        # piège YAML : `mode: off` sans guillemets devient False
        raise ConfigError(
            "ai.mode : le mot doit être entre guillemets (ex. mode: \"off\")"
        )
    if ai_mode not in KNOWN_AI_MODES:
        raise ConfigError(f"ai.mode invalide : {ai_mode!r}")
    prompt_version = str(ai.get("prompt_version", "v1"))

    pub_cfg = _require(cfg, "publishing", "racine")
    notice = str(pub_cfg.get("responsible_gambling_notice", ""))
    if "garanti" not in notice.lower() and "aucun" not in notice.lower():
        raise ConfigError(
            "publishing.responsible_gambling_notice : l'avertissement d'absence "
            "de garantie est obligatoire"
        )
    max_caption = int(pub_cfg.get("max_caption_chars", 1024))
    max_png = int(pub_cfg.get("max_png_bytes", 10_000_000))
    if not (0 < max_caption <= 4096) or not (0 < max_png <= 50_000_000):
        raise ConfigError("publishing : limites de sortie hors bornes")

    probs = cfg.get("probabilities") or {}
    prob_source = str(probs.get("source", "market"))
    if prob_source not in KNOWN_PROB_SOURCES:
        raise ConfigError(f"probabilities.source invalide : {prob_source!r}")
    if prob_source == "poisson" and mode != "public":
        raise ConfigError(
            "probabilities.source=poisson est interdit pendant le pilote "
            "(validation chronologique requise)"
        )

    sched = cfg.get("scheduler") or {}
    refresh_win = int(sched.get("refresh_window_minutes", 180))
    if not (30 <= refresh_win <= 720):
        raise ConfigError("scheduler.refresh_window_minutes dans [30, 720]")

    return AppConfig(
        raw_path=str(p),
        config_hash=sha256_text(text),
        timezone=timezone,
        mode=mode,
        require_human_approval=require_human,
        competitions=tuple(competitions),
        fallback_competitions=tuple(fallback_competitions),
        markets=tuple(markets),
        primary_provider=primary,
        secondary_provider=secondary,
        api_football_daily_budget=aff_budget,
        api_football_per_minute=(int(aff_pm) if aff_pm else None),
        football_data_daily_budget=fd_budget,
        football_data_per_minute=fd_pm,
        http_timeout_seconds=timeout,
        max_retries=retries,
        quality=QualityConfig(
            eligible_threshold=eligible,
            watch_threshold=watch,
            max_odds_age_hours=max_age,
            kickoff_tolerance_minutes=tol,
            min_selection_probability=min_p,
            weights={k: float(weights[k]) for k in REQUIRED_WEIGHT_KEYS},
            contradiction_penalty=float(q.get("contradiction_penalty", 25)),
        ),
        selection=SelectionConfig(
            weight_data_quality=w_dq,
            weight_source_agreement=w_sa,
            weight_odds_stability=w_os,
            weight_probability=w_p,
            weight_explanation=w_e,
            explanation_score_ai_validated=exp_ai,
            explanation_score_template=exp_tpl,
        ),
        coupons=CouponConfig(
            pilot_max_selections=pilot,
            public_max_selections=pub,
            max_published_per_day=per_day,
            prohibit_same_fixture=bool(c.get("prohibit_same_fixture", True)),
            prohibit_same_team=bool(c.get("prohibit_same_team", True)),
            same_context_minutes=int(c.get("same_context_minutes", 90)),
            max_candidates_displayed=int(c.get("max_candidates_displayed", 3)),
        ),
        ai_mode=ai_mode,
        ai_require_valid_source_refs=bool(ai.get("require_valid_source_refs", True)),
        ai_enable_web_search=bool(ai.get("enable_web_search", False)),
        ai_prompt_version=prompt_version,
        telegram_test_channel_env=str(pub_cfg.get("telegram_test_channel_env", "TELEGRAM_TEST_CHANNEL_ID")),
        max_caption_chars=max_caption,
        max_png_bytes=max_png,
        responsible_gambling_notice=notice,
        prob_source=prob_source,
        prob_model_version=str(probs.get("model_version", "market_normalized_v1")),
        poisson_max_goals=int(probs.get("poisson_max_goals", 10)),
        refresh_window_minutes=refresh_win,
    )


def write_config(path: str | Path, raw: dict[str, Any]) -> AppConfig:
    """Valide puis remplace atomiquement une configuration YAML.

    Utilisé par l'écran Paramètres : une saisie invalide ne peut jamais
    laisser le fichier de configuration partiellement écrit.
    """
    p = Path(path)
    if not isinstance(raw, dict):
        raise ConfigError("config : le YAML racine doit être un mapping")
    rendered = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{p.stem}-", suffix=".yaml", dir=p.parent)
    candidate = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        config = load_config(candidate)
        os.replace(candidate, p)
        return config
    finally:
        if candidate.exists():
            candidate.unlink()


@dataclass(frozen=True)
class Secrets:
    api_football_key: str = ""
    football_data_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    telegram_bot_token: str = ""
    telegram_test_channel_id: str = ""
    db_path: str = "data/football.db"
    outbox_dir: str = "data/outbox"

    @property
    def has_api_football(self) -> bool:
        return bool(self.api_football_key)

    @property
    def has_football_data(self) -> bool:
        return bool(self.football_data_token)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_test_channel_id)

    def masked(self) -> dict[str, str]:
        """Version affichable : aucune clé n'est révélée."""

        def m(v: str) -> str:
            if not v:
                return "(absente)"
            return v[:4] + "…" + f"({len(v)} car.)"

        return {
            "API_FOOTBALL_KEY": m(self.api_football_key),
            "FOOTBALL_DATA_TOKEN": m(self.football_data_token),
            "GEMINI_API_KEY": m(self.gemini_api_key),
            "TELEGRAM_BOT_TOKEN": m(self.telegram_bot_token),
            "TELEGRAM_TEST_CHANNEL_ID": m(self.telegram_test_channel_id),
        }


def load_secrets(env_file: str | Path | None = None) -> Secrets:
    """Charge .env s'il existe (jamais obligatoire : mode démo sinon)."""
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)

    def g(k: str) -> str:
        return os.environ.get(k, "").strip()

    return Secrets(
        api_football_key=g("API_FOOTBALL_KEY"),
        football_data_token=g("FOOTBALL_DATA_TOKEN"),
        gemini_api_key=g("GEMINI_API_KEY"),
        gemini_model=g("GEMINI_MODEL") or "gemini-2.0-flash",
        ollama_base_url=g("OLLAMA_BASE_URL") or "http://localhost:11434",
        ollama_model=g("OLLAMA_MODEL") or "llama3.1",
        telegram_bot_token=g("TELEGRAM_BOT_TOKEN"),
        telegram_test_channel_id=g("TELEGRAM_TEST_CHANNEL_ID"),
        db_path=g("DB_PATH") or "data/football.db",
        outbox_dir=g("OUTBOX_DIR") or "data/outbox",
    )
