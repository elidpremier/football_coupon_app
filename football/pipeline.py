"""Orchestration : collecte → normalisation → analyse → coupons →
règlement. Chaque job est journalisé (run_log) et retourne un rapport
lisible ; une panne d'un fournisseur n'empêche pas l'autre, et le
quota dépassé produit un message explicite (« collecte reportée »).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from .analysis.base import AiPart, AnalysisError, build_analysis_provider
from .analysis.validator import validate_ai_parts
from .config import AppConfig, Secrets
from .features import FeatureBundle, load_bundle
from .http import QuotaExceeded
from .normalize import (
    fixture_status_ok,
    make_fixture_key,
    match_team_names,
    normalize_team_name,
)
from .providers.base import (
    BaseProvider,
    ProviderError,
    build_providers,
)
from .probabilities import implied_probabilities, overround
from .quality import compute_quality
from .coupons import FixtureInfo, build_candidates
from .selection import Selection, evaluate_fixture
from .storage import Database
from .utils import iso, parse_iso, sha256_text, to_decimal, to_utc, utcnow

QUOTA_MESSAGE = "Collecte reportée : quota de sécurité atteint"


@dataclass
class StepReport:
    job: str
    status: str                      # ok | partial | failed | quota_exceeded
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    message: str = ""

    def ok(self) -> bool:
        return self.status in {"ok", "partial"}


class Pipeline:
    def __init__(self, db: Database, config: AppConfig, secrets: Secrets,
                 providers: Optional[dict[str, BaseProvider]] = None,
                 ai=None, transport=None,
                 now_fn: Callable[[], datetime] | None = None):
        self.db = db
        self.config = config
        self.secrets = secrets
        self.now_fn = now_fn or utcnow
        self.providers = providers or build_providers(
            config, secrets, db, transport
        )
        self.ai = ai
        if self.ai is None:
            try:
                self.ai = build_analysis_provider(
                    config.ai_mode,
                    gemini_key=secrets.gemini_api_key,
                    gemini_model=secrets.gemini_model,
                    enable_web_search=config.ai_enable_web_search,
                    ollama_base_url=secrets.ollama_base_url,
                    ollama_model=secrets.ollama_model,
                    transport=transport,
                )
            except AnalysisError:
                from .analysis.templates import TemplateProvider

                self.ai = TemplateProvider()
        self.is_demo = config.primary_provider == "api_football" and not secrets.has_api_football

    # ------------------------------------------------------------- collecte
    def _primary(self) -> BaseProvider:
        name = "demo_primary" if self.is_demo else self.config.primary_provider
        return self.providers[name]

    def _secondary(self) -> Optional[BaseProvider]:
        if self.is_demo:
            return self.providers.get("demo_secondary")
        name = self.config.secondary_provider
        return self.providers.get(name)

    def run_collect(self, day: Optional[date] = None) -> StepReport:
        now = self.now_fn()
        if day is None:
            p = self._primary()
            day = p.day if hasattr(p, "day") else now.astimezone(timezone.utc).date()
        errors: list[str] = []
        counters: dict[str, int] = {"fixtures": 0, "odds": 0, "standings": 0,
                                    "availability": 0, "incoherent": 0,
                                    "review_required": 0, "primary_ok": 1}
        run_id = f"collect-{uuid.uuid4().hex[:10]}"
        self.db.run_start(run_id, "collect", now)
        primary = self._primary()
        secondary = self._secondary()
        season = now.year
        quota_hit = False
        fallback_scanned = 0

        # On sonde d'abord les compétitions prioritaires. S'il n'y a aucun
        # match, on balaie les compétitions de repli compatibles avec les
        # deux sources. Cela garantit une analyse quotidienne lorsqu'une
        # journée est creuse dans les championnats principaux, tout en évitant
        # des appels de cotes/indisponibilités inutiles pour des ligues vides.
        prefetched_fixtures: dict[str, list] = {}
        selected_competitions: list[str] = []

        def fetch_primary_fixtures(competition: str) -> None:
            nonlocal quota_hit
            try:
                fixtures = primary.fetch_fixtures(day, competition)
            except QuotaExceeded as exc:
                quota_hit = True
                errors.append(f"fixtures {competition} : {exc}")
                return
            except ProviderError as exc:
                counters["primary_ok"] = 0
                errors.append(f"fixtures {competition} (primaire) : {exc}")
                return
            if fixtures:
                prefetched_fixtures[competition] = fixtures
                selected_competitions.append(competition)

        from .rotation import build_rotator
        rotator = build_rotator(self.config, self.db)
        target_competitions = rotator.get_active_competitions(day)

        for competition in target_competitions:
            fetch_primary_fixtures(competition)
            if quota_hit:
                break
        if not selected_competitions and not quota_hit:
            for competition in self.config.fallback_competitions:
                fallback_scanned += 1
                fetch_primary_fixtures(competition)
                if quota_hit:
                    break

        if not selected_competitions and self.config.fallback_competitions:
            counters["fallback_used"] = int(not quota_hit)
        elif selected_competitions and any(
            competition in self.config.fallback_competitions
            for competition in selected_competitions
        ):
            counters["fallback_used"] = 1
        counters["fallback_scanned"] = fallback_scanned
        counters["competitions_with_matches"] = len(selected_competitions)

        for competition in selected_competitions:
            key_map: dict[str, str] = {}
            primary_fixtures = prefetched_fixtures[competition]

            secondary_fixtures: list = []
            if secondary is not None:
                try:
                    secondary_fixtures = secondary.fetch_fixtures(day, competition)
                except QuotaExceeded as exc:
                    quota_hit = True
                    errors.append(f"fixtures {competition} (secondaire) : {exc}")
                except ProviderError as exc:
                    errors.append(f"fixtures {competition} (secondaire) : {exc}")

            sec_by_names: dict[tuple, Any] = {}
            for sf in secondary_fixtures:
                sec_by_names.setdefault(
                    (normalize_team_name(sf.home_team),
                     normalize_team_name(sf.away_team)), sf)

            for pf in primary_fixtures:
                h, a = normalize_team_name(pf.home_team), normalize_team_name(pf.away_team)
                key = make_fixture_key(competition, pf.kickoff_utc,
                                       pf.home_team, pf.away_team)
                matching = "OK"
                sec_fixture = sec_by_names.get((h, a))
                if sec_fixture is not None:
                    mt = match_team_names(pf.home_team, sec_fixture.home_team)
                    if mt.status == "review_required":
                        matching = "MATCHING_REVIEW_REQUIRED"
                    elif mt.status == "mismatch":
                        matching = "MATCHING_REVIEW_REQUIRED"
                    elif abs((pf.kickoff_utc - sec_fixture.kickoff_utc).total_seconds()) \
                            > self.config.quality.kickoff_tolerance_minutes * 60:
                        matching = "INCOHERENT"

                self.db.upsert_fixture({
                    "fixture_key": key,
                    "competition_slug": competition,
                    "competition_name": pf.competition_name,
                    "kickoff_utc": iso(pf.kickoff_utc),
                    "home_team": pf.home_team,
                    "away_team": pf.away_team,
                    "venue": pf.venue,
                    "status": "COLLECTED",
                    "matching_status": matching,
                    "primary_provider": pf.provider,
                    "primary_fixture_id": pf.provider_fixture_id,
                    "secondary_provider": (sec_fixture.provider if sec_fixture else None),
                    "secondary_fixture_id": (sec_fixture.provider_fixture_id
                                             if sec_fixture else None),
                }, now)
                self.db.add_provider_fixture({
                    "fixture_key": key, "provider": pf.provider,
                    "provider_fixture_id": pf.provider_fixture_id,
                    "raw": {"status": pf.status, "kickoff": iso(pf.kickoff_utc)},
                }, now)
                key_map[f"{pf.provider}|{pf.provider_fixture_id}"] = key
                if sec_fixture is not None:
                    self.db.add_provider_fixture({
                        "fixture_key": key, "provider": sec_fixture.provider,
                        "provider_fixture_id": sec_fixture.provider_fixture_id,
                        "raw": {"status": sec_fixture.status,
                                "kickoff": iso(sec_fixture.kickoff_utc)},
                    }, now)
                    key_map[f"{sec_fixture.provider}|{sec_fixture.provider_fixture_id}"] = key
                counters["fixtures"] += 1
                if matching == "INCOHERENT":
                    counters["incoherent"] += 1
                elif matching == "MATCHING_REVIEW_REQUIRED":
                    counters["review_required"] += 1

            # cotes (les deux sources pour la stabilité)
            for provider in (p for p in (primary, secondary) if p is not None):
                try:
                    odds = provider.fetch_odds(day, competition)
                    counters["odds"] += self._store_odds(odds, key_map, now)
                except QuotaExceeded as exc:
                    quota_hit = True
                    errors.append(f"odds {competition} ({provider.name}) : {exc}")
                except ProviderError as exc:
                    errors.append(f"odds {competition} ({provider.name}) : {exc}")

            # classements → snapshots d'équipe
            standings: list = []
            try:
                standings = primary.fetch_standings(competition, season)
            except QuotaExceeded as exc:
                quota_hit = True
                errors.append(f"standings {competition} : {exc}")
            except ProviderError as exc:
                errors.append(f"standings {competition} : {exc}")
            counters["standings"] += self._store_standings(standings, competition, now)

            # indisponibilités
            try:
                avail = primary.fetch_availability(day, competition)
                counters["availability"] += self._store_availability(avail, key_map, now)
            except QuotaExceeded as exc:
                quota_hit = True
                errors.append(f"availability {competition} : {exc}")
            except ProviderError as exc:
                errors.append(f"availability {competition} : {exc}")

        if quota_hit:
            status = "quota_exceeded"
            self._finish(run_id, status, counters, errors, "collect")
            return StepReport("collect", status, summary=counters, errors=errors,
                              message=QUOTA_MESSAGE)
        status = "ok" if not errors else "partial"
        self._finish(run_id, status, counters, errors, "collect")
        source_label = ""
        if counters.get("fallback_used"):
            source_label = " — repli de compétitions activé"
        if not counters["fixtures"] and not errors:
            source_label = " — aucun match trouvé, y compris après le repli"
        return StepReport("collect", status, summary=counters, errors=errors,
                          message=f"{counters['fixtures']} match(s) collecté(s), "
                                  f"{counters['odds']} cote(s) horodatée(s)"
                                  f"{source_label}")

    # ------------------------------------------------------------------
    def _store_odds(self, odds, key_map: dict[str, str], now: datetime) -> int:
        snaps = []
        for o in odds:
            fkey = key_map.get(f"{o.provider}|{o.provider_fixture_id}")
            if fkey is None:
                continue
            snaps.append({
                "fixture_key": fkey,
                "provider": o.provider,
                "bookmaker": o.bookmaker,
                "market": o.market,
                "outcome": o.outcome,
                "odds": o.odds,
                "observed_at_utc": iso(to_utc(o.observed_at_utc)),
                "payload_hash": sha256_text(
                    f"{o.provider}|{o.provider_fixture_id}|{o.market}|{o.outcome}|{o.odds}"
                ),
            })
        return self.db.upsert_odds(snaps)

    def _store_standings(self, standings, competition: str, now: datetime) -> int:
        if not standings:
            return 0
        table = {
            normalize_team_name(s.team): s
            for s in standings
        }
        n = 0
        for fx in self.db.fixtures_between("1970-01-01T00:00:00Z", "2999-01-01T00:00:00Z"):
            if fx["competition_slug"] != competition:
                continue
            for side, team_name in (("home", fx["home_team"]), ("away", fx["away_team"])):
                s = table.get(normalize_team_name(team_name))
                if s is None:
                    continue
                self.db.upsert_team_snapshot({
                    "fixture_key": fx["fixture_key"],
                    "team": team_name,
                    "side": side,
                    "competition_slug": competition,
                    "position": s.position,
                    "played": s.played, "wins": s.wins, "draws": s.draws,
                    "losses": s.losses, "goals_for": s.goals_for,
                    "goals_against": s.goals_against, "form": s.form,
                }, now)
                n += 1
        return n

    def _store_availability(self, avail, key_map: dict[str, str],
                            now: datetime) -> int:
        n = 0
        for a in avail:
            fkey = key_map.get(f"{a.provider}|{a.provider_fixture_id}")
            if fkey is None:
                continue
            self.db.upsert_availability({
                "fixture_key": fkey,
                "team": a.team,
                "player": a.player,
                "reason": a.reason,
                "status": a.status,
                "source_ref": a.source_ref,
            }, now)
            n += 1
        return n

    def _finish(self, run_id: str, status: str, summary: dict, errors: list[str],
                job: str) -> None:
        self.db.run_finish(run_id, status, summary, errors, self.now_fn())

    # --------------------------------------------------------------- analyse
    def _build_payload(self, fx_row, bundle: FeatureBundle,
                       quality, engine_status: str) -> tuple[dict, list[str]]:
        from .utils import parse_iso as _pi

        sources: dict[str, str] = {}
        for prov in bundle.providers:
            sources[f"{prov}#fixtures"] = f"rencontres fournies par {prov}"
        if bundle.odds:
            provs = sorted({info["provider"] for m in bundle.odds.values()
                            for info in m.values()})
            for p in provs:
                sources[f"{p}#odds"] = f"cotes horodatées fournies par {p}"
        if bundle.teams:
            sources["classement"] = "classement/fournis par les sources primaires"
        for a in bundle.availability:
            ref = a.get("source_ref") or ""
            if ref:
                sources[ref] = "absence/information de composition " \
                               f"({a.get('team', '')}, {a.get('player', '')})"
        if self.config.ai_enable_web_search and self.config.ai_mode == "gemini_free":
            sources["web_search"] = (
                "recherche Web Gemini, limitée aux risques et inconnues ; "
                "jamais une source de chiffres de décision"
            )

        odds_payload: dict[str, dict] = {}
        allow: list[float] = []
        for market, outcomes in bundle.odds.items():
            mp: dict[str, dict] = {}
            for outcome, info in outcomes.items():
                age_h = max(
                    0.0,
                    (to_utc(bundle.computed_at) - to_utc(info["observed_at"])
                     ).total_seconds() / 3600.0,
                )
                mp[outcome] = {
                    "odds": str(info["odds"]),
                    "observed_at_utc": iso(info["observed_at"]),
                    "age_hours": round(age_h, 2),
                    "provider": info["provider"],
                    "bookmaker": info.get("bookmaker", ""),
                }
                allow.append(float(info["odds"]))
                allow.append(round(age_h, 2))
                allow.append(float(int(round(age_h))))
            probs = self._market_probs(bundle, market)
            odds_payload[market] = mp
            for p in probs.values():
                allow.append(p)
                allow.append(round(p * 100, 1))
                allow.append(float(int(round(p * 100))))
        for ov in self._market_overrounds(bundle).values():
            allow.append(ov)
            allow.append(round(ov * 100, 1))

        standings_payload = {}
        for team, s in bundle.teams.items():
            standings_payload[team] = {
                "position": s["position"], "played": s["played"],
                "wins": s["wins"], "draws": s["draws"], "losses": s["losses"],
                "goals_for": s["goals_for"], "goals_against": s["goals_against"],
                "form": s["form"] or "",
            }
            for k in ("position", "played", "wins", "draws", "losses",
                      "goals_for", "goals_against"):
                if s[k] is not None:
                    allow.append(float(s[k]))

        kickoff = to_utc(bundle.kickoff_utc)
        allow += [float(kickoff.day), float(kickoff.month), float(kickoff.year),
                  float(kickoff.hour), float(kickoff.minute)]
        allow.append(round(quality.score, 1))
        allow.append(float(int(round(quality.score))))
        allow.append(100.0)  # dénominateur de l'échelle de qualité

        payload = {
            "fixture": {
                "home_team": bundle.home_team,
                "away_team": bundle.away_team,
                "competition_slug": bundle.competition_slug,
                "venue": "",
            },
            "kickoff_utc": iso(kickoff),
            "odds": odds_payload,
            "probabilities": {m: self._market_probs(bundle, m) for m in bundle.odds},
            "overround": self._market_overrounds(bundle),
            "standings": standings_payload,
            "availability": list(bundle.availability),
            "quality": {"score": round(quality.score, 1),
                        "notes": list(quality.notes)},
            "providers": {p: {"id": d.get("id")} for p, d in bundle.providers.items()},
            "sources": sources,
            "engine_status": engine_status,
        }
        return payload, sorted(allow)

    def _market_probs(self, bundle: FeatureBundle, market: str) -> dict[str, float]:
        try:
            probs, _ = implied_probabilities({o: to_decimal(v["odds"])
                                              for o, v in bundle.odds[market].items()})
            return probs
        except Exception:
            return {}

    def _market_overrounds(self, bundle: FeatureBundle) -> dict[str, float]:
        out = {}
        for market, outcomes in bundle.odds.items():
            try:
                out[market] = round(overround({o: to_decimal(v["odds"])
                                               for o, v in outcomes.items()}), 4)
            except Exception:
                continue
        return out

    def _engine_ceiling(self, fixture_key: str, bundle: FeatureBundle,
                        quality) -> str:
        """Plafond optimiste du moteur (sans le composant explication) :
        l'IA ne peut pas le dépasser."""
        s = self.config.selection
        score_wo_exp = (
            s.weight_data_quality * (quality.score / 100.0)
            + s.weight_source_agreement * quality.source_agreement
            + s.weight_odds_stability * (0.8 if bundle.odds else 0.0)
            + s.weight_probability * 1.0
        )
        optimistic = score_wo_exp + s.weight_explanation * s.explanation_score_ai_validated
        if optimistic >= self.config.quality.eligible_threshold:
            return "eligible"
        if optimistic >= self.config.quality.watch_threshold:
            return "watch"
        return "exclude"

    def analyse_fixture(self, fixture_key: str) -> Optional[dict[str, Any]]:
        now = self.now_fn()
        bundle = load_bundle(self.db, fixture_key, computed_at=now)
        if bundle is None:
            return None
        fx_row = self.db.get_fixture(fixture_key)
        if fx_row["status"] == "SETTLED":
            return {"fixture_key": fixture_key, "skipped": "déjà réglé"}
        kickoff = to_utc(bundle.kickoff_utc)
        if kickoff <= now and (now - kickoff).total_seconds() > 900:
            return {"fixture_key": fixture_key, "skipped": "match déjà commencé"}
        quality = compute_quality(bundle, self.config, now)

        engine_status = "exclude"
        if not quality.exclusions:
            ceiling = self._engine_ceiling(fixture_key, bundle, quality)
            engine_status = ceiling

        analyst: Optional[AiPart] = None
        critic: Optional[AiPart] = None
        analysis_valid = False
        payload, allowlist = self._build_payload(fx_row, bundle, quality,
                                                 engine_status)
        source_ids = list(payload["sources"].keys())
        mode = self.ai.mode
        if mode == "off":
            try:
                analyst, critic = self.ai.analyse(payload, source_ids)
                errors = validate_ai_parts(
                    analyst.to_dict(), critic.to_dict(), source_ids, allowlist,
                    engine_ceiling=engine_status,
                    year=kickoff.year,
                )
                if errors:
                    raise AnalysisError(f"validation gabarit : {errors[:3]}")
                analysis_valid = True
            except AnalysisError:
                pass  # le gabarit ne devrait pas échouer ; conservé par prudence
        else:
            try:
                analyst, critic = self.ai.analyse(payload, source_ids)
                errors = validate_ai_parts(
                    analyst.to_dict(), critic.to_dict(), source_ids, allowlist,
                    engine_ceiling=engine_status,
                    year=kickoff.year,
                )
                if errors:
                    raise AnalysisError(f"validation IA : {errors[:3]}")
                analysis_valid = True
            except AnalysisError:
                # repli automatique sur le gabarit (sans bonus de confiance)
                try:
                    from .analysis.templates import TemplateProvider

                    tpl = TemplateProvider()
                    analyst, critic = tpl.analyse(payload, source_ids)
                    errors = validate_ai_parts(
                        analyst.to_dict(), critic.to_dict(), source_ids, allowlist,
                        engine_ceiling=engine_status,
                        year=kickoff.year,
                    )
                    analysis_valid = not errors
                except AnalysisError:
                    analyst, critic = None, None

        self.db.save_analysis({
            "fixture_key": fixture_key,
            "mode": mode,
            "payload": payload,
            "analyst": analyst.to_dict() if analyst else None,
            "critic": critic.to_dict() if critic else None,
            "validated": analysis_valid,
            "source_refs": source_ids,
        }, now)
        self.db.save_features({
            "fixture_key": fixture_key,
            "model_version": self.config.prob_model_version,
            "features": {"quality": quality.to_dict() if hasattr(quality, "to_dict")
                         else vars(quality)},
            "quality_score": quality.score,
        }, now)

        selection = evaluate_fixture(
            fixture_key, bundle, quality, self.config,
            analyst=analyst, critic=critic,
            analysis_valid=analysis_valid and mode != "off",
            now=now,
        )
        if selection is not None:
            self.db.upsert_selection({
                "fixture_key": fixture_key,
                "market": selection.market,
                "outcome": selection.outcome,
                "odds": selection.odds,
                "odds_observed_at_utc": iso(selection.odds_observed_at),
                "probability": selection.probability,
                "model_version": selection.model_version,
                "score": selection.score,
                "quality": selection.quality,
                "status": selection.status,
                "reasons": selection.reasons,
                "justification": selection.justification,
                "source_refs": selection.source_refs,
            }, now)
        self.db.set_fixture_status(fixture_key, "ANALYZED", now)
        return {
            "fixture_key": fixture_key,
            "quality": quality.score,
            "exclusions": quality.exclusions,
            "selection": selection.status if selection else None,
            "selection_score": selection.score if selection else None,
            "analysis_valid": analysis_valid,
            "ai_mode": mode,
        }

    def run_analyse(self) -> StepReport:
        now = self.now_fn()
        errors: list[str] = []
        counters: dict[str, int] = {"analysed": 0, "eligible": 0, "watch": 0,
                                    "excluded": 0}
        run_id = f"analyse-{uuid.uuid4().hex[:10]}"
        self.db.run_start(run_id, "analyse", now)
        window_start = "1970-01-01T00:00:00Z"
        window_end = "2999-01-01T00:00:00Z"
        for fx in self.db.fixtures_between(window_start, window_end):
            if fx["status"] in ("SETTLED",):
                continue
            try:
                out = self.analyse_fixture(fx["fixture_key"])
            except Exception as exc:
                errors.append(f"{fx['fixture_key']} : {exc}")
                continue
            if out is None:
                continue
            if out.get("skipped"):
                continue
            counters["analysed"] += 1
            if out.get("selection") == "eligible":
                counters["eligible"] += 1
            elif out.get("selection") == "watch":
                counters["watch"] += 1
            else:
                counters["excluded"] += 1
        status = "ok" if not errors else "partial"
        self._finish(run_id, status, counters, errors, "analyse")
        return StepReport("analyse", status, summary=counters, errors=errors,
                          message=f"{counters['analysed']} match(s) analysé(s), "
                                  f"{counters['eligible']} candidate(s) éligible(s)")

    # --------------------------------------------------------------- coupons
    def _fixture_info(self, fx_row) -> FixtureInfo:
        return FixtureInfo(
            fixture_key=fx_row["fixture_key"],
            home_team=fx_row["home_team"],
            away_team=fx_row["away_team"],
            competition_slug=fx_row["competition_slug"],
            kickoff_utc=parse_iso(fx_row["kickoff_utc"]),
        )

    def run_build_coupons(self) -> StepReport:
        now = self.now_fn()
        errors: list[str] = []
        counters: dict[str, int] = {"eligible": 0, "coupons": 0}
        run_id = f"coupons-{uuid.uuid4().hex[:10]}"
        self.db.run_start(run_id, "coupons", now)
        try:
            rows = self.db.selections_by_status("eligible")
            fixtures = {}
            selections: list[Selection] = []
            for r in rows:
                fx_row = self.db.get_fixture(r["fixture_key"])
                if fx_row is None:
                    continue
                fixtures[r["fixture_key"]] = self._fixture_info(fx_row)
                selections.append(Selection(
                    fixture_key=r["fixture_key"],
                    market=r["market"],
                    outcome=r["outcome"],
                    odds=to_decimal(r["odds"]),
                    odds_observed_at=parse_iso(r["odds_observed_at_utc"]),
                    probability=r["probability"],
                    model_version=r["model_version"],
                    score=r["score"] or 0.0,
                    status=r["status"],
                    reasons=[],
                    justification=r["justification"] or "",
                ))
            counters["eligible"] = len(selections)
            candidates, rejections = build_candidates(selections, fixtures,
                                                      self.config, now)
            # Anti-duplication : un coupon équivalent (mêmes sélections)
            # déjà existant et non rejeté n'est pas recréé.
            import json as _json

            def already_exists(c) -> bool:
                sig = {
                    (s.fixture_key, s.market, s.outcome)
                    for s in c.selections
                }
                for existing in self.db.all_coupons(500):
                    if existing["status"] == "REJECTED":
                        continue
                    if {
                        (x["fixture_key"], x["market"], x["outcome"])
                        for x in _json.loads(existing["selections_json"])
                    } == sig:
                        return True
                return False

            for c in candidates:
                if already_exists(c):
                    continue
                self.db.create_coupon({
                    "coupon_id": c.coupon_id,
                    "kind": "cautious",
                    "selections": c.to_dict()["selections"],
                    "combined_odds": c.combined_odds,
                    "combined_probability": c.combined_probability,
                    "score": c.score,
                    "status": "REVIEW_REQUIRED",
                    "version": 1,
                    "constraints": c.constraints,
                    "notice": self.config.responsible_gambling_notice,
                }, now)
                counters["coupons"] += 1
            status = "ok"
            self._finish(run_id, status, {**counters, "rejections": len(rejections)},
                         errors, "coupons")
            return StepReport("coupons", status,
                              summary={**counters, "rejections": len(rejections)},
                              errors=errors,
                              message=f"{counters['coupons']} coupon(s) candidat(s) "
                                      f"proposé(s) à la validation humaine")
        except Exception as exc:
            errors.append(str(exc))
            self._finish(run_id, "failed", counters, errors, "coupons")
            return StepReport("coupons", "failed", summary=counters,
                              errors=errors, message="échec de la construction des coupons")

    # ------------------------------------------------------------- règlement
    def run_settle(self) -> StepReport:
        now = self.now_fn()
        errors: list[str] = []
        counters: dict[str, int] = {"results": 0, "selections": 0, "coupons": 0}
        run_id = f"settle-{uuid.uuid4().hex[:10]}"
        self.db.run_start(run_id, "settle", now)
        try:
            primary = self._primary()
            # matchs joués (kickoff avant maintenant - 2 h) sans résultat
            candidates = self.db.unsettled_fixtures(iso(now - timedelta(hours=2)))
            # regrouper par jour UTC
            by_day: dict[str, list] = {}
            for fx in candidates:
                d = fx["kickoff_utc"][:10]
                by_day.setdefault(d, []).append(fx)
            for day_str, fxs in by_day.items():
                day = parse_iso(fxs[0]["kickoff_utc"]).date()
                results = {}
                try:
                    for r in primary.fetch_results(day, fxs[0]["competition_slug"]):
                        results[make_fixture_key(
                            fxs[0]["competition_slug"], r.kickoff_utc,
                            r.home_team, r.away_team)] = r
                except (ProviderError, QuotaExceeded) as exc:
                    errors.append(f"results {day_str} : {exc}")
                    continue
                for fx in fxs:
                    r = results.get(fx["fixture_key"])
                    if r is None:
                        continue
                    self.db.upsert_result(fx["fixture_key"], r.home_score,
                                          r.away_score, r.status, now)
                    counters["results"] += 1
                    self.db.set_fixture_status(fx["fixture_key"], "SETTLED", now)
                    self._settle_fixture(fx["fixture_key"], r.home_score,
                                         r.away_score, now)
            status = "ok" if not errors else "partial"
            self._finish(run_id, status, counters, errors, "settle")
            return StepReport("settle", status, summary=counters, errors=errors,
                              message=f"{counters['results']} résultat(s) archivé(s), "
                                      f"{counters['selections']} sélection(s) résolue(s)")
        except Exception as exc:
            errors.append(str(exc))
            self._finish(run_id, "failed", counters, errors, "settle")
            return StepReport("settle", "failed", summary=counters, errors=errors,
                              message="échec du règlement")

    def _settle_fixture(self, fixture_key: str, home_score: int, away_score: int,
                        now: datetime) -> None:
        # sélections de ce match
        srows = self.db.execute(
            "SELECT * FROM selections WHERE fixture_key = ?", (fixture_key,)
        ).fetchall()
        for s in srows:
            outcome = self._selection_outcome(s["market"], s["outcome"],
                                              home_score, away_score)
            if outcome is None:
                continue
            if self.db.record_selection_result(s["id"], outcome, now):
                # la calibration ne retient que de vraies prédictions
                # (les sélections exclues ont probability = 0.0)
                if 0.0 < float(s["probability"] or 0.0) < 1.0:
                    self.db.add_calibration_record({
                        "fixture_key": fixture_key,
                        "market": s["market"],
                        "outcome": s["outcome"],
                        "predicted_probability": s["probability"],
                        "observed": 1 if outcome == "won" else 0,
                    }, now)
        # coupons concernés (reliés par fixture_key + marché + issue)
        for coupon in self.db.coupons_by_status("PUBLISHED", "APPROVED",
                                                 "RENDERED", "REVIEW_REQUIRED"):
            import json as _json

            coupon_sels = _json.loads(coupon["selections_json"])
            keys = {(x["fixture_key"], x["market"], x["outcome"])
                    for x in coupon_sels}
            fixture_keys = {x["fixture_key"] for x in coupon_sels}
            if fixture_key not in fixture_keys:
                continue
            all_sels = self.db.execute(
                "SELECT * FROM selections WHERE fixture_key IN (%s)" %
                ",".join("?" * len(fixture_keys)), tuple(fixture_keys)
            ).fetchall()
            outcomes: list[str] = []
            for sel in all_sels:
                if (sel["fixture_key"], sel["market"], sel["outcome"]) not in keys:
                    continue
                res = self.db.selection_results(sel["id"])
                if res:
                    outcomes.append(res[0]["outcome"])
            if not outcomes:
                continue
            if any(o == "lost" for o in outcomes):
                overall = "lost"
            elif len(outcomes) == len(keys):
                overall = "won" if all(o == "won" for o in outcomes) else "partial"
            else:
                overall = "partial"
            if self.db.record_coupon_result(coupon["coupon_id"], overall, now):
                self.db.set_coupon_status(coupon["coupon_id"], "SETTLED", now)

    @staticmethod
    def _selection_outcome(market: str, outcome: str, hs: int,
                           as_: int) -> Optional[str]:
        if market == "match_winner":
            if outcome == "home":
                return "won" if hs > as_ else "lost"
            if outcome == "draw":
                return "won" if hs == as_ else "lost"
            return "won" if as_ > hs else "lost"
        if market == "double_chance":
            home_w = hs > as_
            away_w = as_ > hs
            draw = hs == as_
            if outcome == "home_draw":
                return "won" if (home_w or draw) else "lost"
            if outcome == "home_away":
                return "won" if (home_w or away_w) else "lost"
            if outcome == "away_draw":
                return "won" if (away_w or draw) else "lost"
        if market == "over_under_2_5":
            total = hs + as_
            if outcome == "over":
                return "won" if total > 2.5 else "lost"
            if outcome == "under":
                return "won" if total < 2.5 else "lost"
        return None

    # ----------------------------------------------------------------- runs
    def run_morning(self, day: Optional[date] = None) -> dict[str, StepReport]:
        reports = {}
        reports["collect"] = self.run_collect(day)
        # On n'analyse que si le fournisseur primaire a livré des données
        # (panne primaire → pas d'analyse de données potentiellement
        # périmées).
        collect_ok = reports["collect"].status in ("ok", "partial")
        primary_ok = reports["collect"].summary.get("primary_ok", 1) == 1
        if collect_ok and primary_ok:
            reports["analyse"] = self.run_analyse()
            reports["coupons"] = self.run_build_coupons()
        return reports

    def run_refresh(self) -> StepReport:
        """Rafraîchissement ciblé 2–3 h avant match : uniquement les
        candidats. Réutilise la collecte (cache TTL → peu d'appels)."""
        now = self.now_fn()
        window = timedelta(minutes=self.config.refresh_window_minutes)
        run_id = f"refresh-{uuid.uuid4().hex[:10]}"
        self.db.run_start(run_id, "refresh", now)
        errors: list[str] = []
        refreshed = 0
        target_keys = set()
        for fx in self.db.fixtures_between(iso(now), iso(now + window + timedelta(hours=4))):
            if fx["status"] in ("COLLECTED", "ANALYZED") and fx["matching_status"] == "OK":
                target_keys.add(fx["fixture_key"])
        if target_keys:
            collect = self.run_collect(
                now.astimezone(timezone.utc).date())
            if collect.status not in ("ok", "partial"):
                errors.extend(collect.errors)
            for k in target_keys:
                try:
                    self.analyse_fixture(k)
                    refreshed += 1
                except Exception as exc:
                    errors.append(f"{k} : {exc}")
        status = "ok" if not errors else "partial"
        self._finish(run_id, status, {"refreshed": refreshed}, errors, "refresh")
        return StepReport("refresh", status, summary={"refreshed": refreshed},
                          errors=errors,
                          message=f"{refreshed} match(s) rafraîchi(s) avant coup d'envoi")
