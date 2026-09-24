"""Application Streamlit locale de validation des coupons football.

L'IA explique et contredit ; le code contrôle ; une personne valide.
Aucune clé API n'est affichée (masquage), aucune publication sans
validation humaine explicite, avertissement d'absence de garantie
partout.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
import yaml
from zoneinfo import ZoneInfo

from football.config import (
    COMMON_PROVIDER_COMPETITIONS,
    ConfigError,
    KNOWN_COMPETITIONS,
    load_config,
    load_secrets,
    write_config,
)
from football.metrics import (
    CalibRecord,
    brier_score,
    hit_rate,
    log_loss,
    per_market,
)
from football.pipeline import Pipeline
from football.render import render_coupon_png
from football.storage import Database
from football.telegram import PublishBlocked, PublishError, TelegramPublisher
from football.utils import iso, parse_iso, utcnow

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "football.yaml"
TZ = ZoneInfo("Africa/Ouagadougou")  # surchargé par la config après chargement


@st.cache_resource(show_spinner=False)
def get_context(db_path_key: str, outbox_dir_key: str):
    # les variables d'environnement font partie de la clé de cache :
    # changer DB_PATH/OUTBOX_DIR (tests, déploiements) crée un contexte
    # frais au lieu de réutiliser l'ancienne base
    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        config = None
        config_error = str(exc)
    else:
        config_error = None
    secrets = load_secrets(ROOT / ".env")
    db_path = Path(secrets.db_path)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db = Database(db_path)
    pipeline = None
    if config is not None:
        pipeline = Pipeline(db, config, secrets)
    return config, config_error, secrets, db, pipeline


def _fmt_kickoff(dt: datetime) -> str:
    try:
        tz = ZoneInfo(st.session_state.get("tz", "Africa/Ouagadougou"))
        return dt.astimezone(tz).strftime("%d/%m %H:%M")
    except Exception:
        return dt.strftime("%d/%m %H:%M UTC")


def _chip(status: str) -> str:
    colors = {
        "eligible": "🟢 éligible", "watch": "🟡 surveillance",
        "exclude": "⛔ exclu", "manual_exclude": "⛔ exclu (main)",
        "OK": "✅ OK", "INCOHERENT": "🚫 INCOHERENT",
        "MATCHING_REVIEW_REQUIRED": "⚠️ revue requise",
        "COLLECTED": "collected", "ANALYZED": "analysé", "SETTLED": "réglé",
        "REVIEW_REQUIRED": "⏳ à valider", "APPROVED": "✅ approuvé",
        "REJECTED": "❌ rejeté", "PUBLISHED": "📤 publié",
        "PUBLISH_FAILED": "⚠️ échec publication", "DRAFT": "brouillon",
    }
    return colors.get(status, status)


def render_diagnostic_panel(db, config, pipeline) -> None:
    st.markdown("### 🔍 Diagnostic d'analyse et de génération")
    from football.utils import jload

    # 1. État des exécutions
    runs = db.last_runs(5)
    st.markdown("**1. Historique récent de la chaîne :**")
    if not runs:
        st.info("Aucune exécution enregistrée. Lancez la chaîne du jour à l'aide du bouton **▶️ Chaîne du jour**.")
    else:
        for r in runs[:3]:
            st.caption(f"• **`{r['job']}`** — statut `{r['status']}` le `{r['started_at_utc'][:19]}` — {r['summary']}")

    # 2. Matchs collectés et raisons d'exclusion
    fxs = _visible_fixtures(db)
    st.markdown(f"**2. Collecte des matchs (48h) :** `{len(fxs)} match(s) en base`")
    if not fxs:
        st.warning(
            "• **Aucun match en base locale.**\n"
            "• Si vous avez lancé la chaîne mais qu'aucun match n'est apparu, cela signifie que le fournisseur API "
            "n'a renvoyé aucun match programmé pour aujourd'hui dans les compétitions scannées (prioritaires ou repli)."
        )
    else:
        incoherent = [f for f in fxs if f["matching_status"] == "INCOHERENT"]
        review_req = [f for f in fxs if f["matching_status"] == "MATCHING_REVIEW_REQUIRED"]
        if incoherent:
            st.warning(f"• **{len(incoherent)} match(s) exclu(s)** pour heure de coup d'envoi divergente entre sources (> 5 min).")
        if review_req:
            st.warning(f"• **{len(review_req)} match(s) exclu(s)** nécessitant une revue manuelle des noms d'équipes.")

    # 3. Sélections & Qualité
    sels = db.execute("SELECT * FROM selections ORDER BY id DESC LIMIT 100").fetchall()
    eligible_sels = [s for s in sels if s["status"] == "eligible"]
    watch_sels = [s for s in sels if s["status"] == "watch"]
    excl_sels = [s for s in sels if s["status"] in ("exclude", "manual_exclude")]

    st.markdown(f"**3. Éligibilité des sélections :** `{len(eligible_sels)}` éligibles, `{len(watch_sels)}` surveillance, `{len(excl_sels)}` exclues")
    if sels and not eligible_sels:
        st.warning("• **Aucune sélection éligible (Score < 80/100).** Raisons courantes des pénalités :")
        for s in sels[:5]:
            reasons = jload(s["reasons"] or "[]")
            r_str = ", ".join(reasons) if reasons else "cotes périmées (>6h), absence de seconde source ou complétude incomplète"
            st.caption(f"  - `{s['fixture_key']}` ({s['market']}/{s['outcome']}) : Score {s['score']:.0f}/100 — **{r_str}**")

    # 4. Diagnostic coupons
    coupons = db.all_coupons(10)
    min_req = config.coupons.pilot_max_selections if config else 2
    st.markdown(f"**4. Génération des coupons :** `{len(coupons)}` coupon(s) candidat(s)")
    if len(eligible_sels) < min_req:
        st.error(
            f"❌ **Génération de coupon bloquée :** Le mode pilote requiert au moins "
            f"**{min_req} sélections éligibles (score ≥ 80)**. Seulement **{len(eligible_sels)}** est disponible actuellement."
        )


def screen_dashboard(config, secrets, db, pipeline) -> None:
    st.subheader("Tableau de bord")
    today = utcnow().strftime("%Y-%m-%d")
    c1, c2, c3, c4 = st.columns(4)
    # sqlite3.Row supports item access but not dict.get().  Convert the
    # database rows at this UI boundary because the dashboard needs safe
    # defaults when a provider has not yet consumed any quota.
    usage = {row["provider"]: dict(row) for row in db.usage_all(today)}
    if config is not None:
        with c1:
            budget = config.api_football_daily_budget
            used = usage.get("api_football", {}).get("requests", 0)
            st.metric("Quota API-Football (jour)", f"{used} / {budget}")
            st.progress(used / max(1, budget))
        with c2:
            fd_used = usage.get("football_data", {}).get("requests", 0)
            st.metric("Quota football-data (jour)",
                      f"{fd_used} / {config.football_data_daily_budget}")
    with c3:
        st.metric("Mode IA", config.ai_mode if config else "n/a")
    with c4:
        demo = pipeline is not None and pipeline.is_demo
        st.metric("Source de données", "DÉMO (aucune clé API)" if demo else "API réelles")

    counts = db.counts_by_status()
    st.caption(
        "Compteurs — "
        + " · ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        if counts else "Aucune donnée pour l'instant."
    )

    last = db.last_runs(1)
    if last:
        r = last[0]
        st.caption(f"Dernier run : **{r['job']}** — {r['status']} — {r['started_at_utc']}")

    st.divider()
    col_a, col_b, _ = st.columns([1, 1, 2])
    if col_a.button("▶️ Chaîne du jour (collecte → analyse → coupons)", key="btn_run_morning"):
        if pipeline is None:
            st.error("Configuration invalide : chaîne indisponible.")
        else:
            with st.spinner("Exécution de la chaîne…"):
                reports = pipeline.run_morning()
            for name in ("collect", "analyse", "coupons"):
                if name in reports:
                    rep = reports[name]
                    if rep.status == "quota_exceeded":
                        st.warning(rep.message)
                    elif rep.status == "failed":
                        st.error(f"{name} : {rep.message}")
                    else:
                        st.success(f"{name} : {rep.message}")
            st.rerun()
    if col_b.button("⚽ Régler les résultats", key="btn_settle"):
        if pipeline is None:
            st.error("Configuration invalide.")
        else:
            with st.spinner("Règlement des résultats…"):
                rep = pipeline.run_settle()
            if rep.status == "quota_exceeded":
                st.warning(rep.message)
            elif rep.status == "failed":
                st.error(rep.message)
            else:
                st.success(rep.message)
            st.rerun()

    st.divider()
    fxs = _visible_fixtures(db)
    coupons = db.all_coupons(10)
    has_items = bool(fxs and coupons)
    with st.expander("🔍 Panneau de diagnostic détaillé (pourquoi aucun match/coupon ?)", expanded=not has_items):
        render_diagnostic_panel(db, config, pipeline)


def _visible_fixtures(db, now=None):
    now = now or utcnow()
    start = iso(now - timedelta(hours=6))
    end = iso(now + timedelta(hours=48))
    return db.fixtures_between(start, end)


def screen_matches(config, secrets, db, pipeline) -> None:
    st.subheader("Matchs à venir")
    fxs = _visible_fixtures(db)
    if not fxs:
        st.info("Aucun match collecté sur la période. Lancez la chaîne du jour "
                "(tableau de bord) ou attendez la tâche planifiée.")
        render_diagnostic_panel(db, config, pipeline)
        return
    rows = []
    for fx in fxs:
        sel = db.execute(
            "SELECT * FROM selections WHERE fixture_key = ? ORDER BY id DESC LIMIT 1",
            (fx["fixture_key"],),
        ).fetchone()
        qual = sel["quality"] if sel and sel["quality"] is not None else None
        rows.append({
            "Match": f"{fx['home_team']} vs {fx['away_team']}",
            "Coup d'envoi": _fmt_kickoff(parse_iso(fx["kickoff_utc"])),
            "Correspondance": _chip(fx["matching_status"]),
            "États": fx["status"],
            "Sélection": (f"{sel['market']} / {sel['outcome']}" if sel else "—"),
            "Score": (f"{sel['score']:.0f}" if sel and sel["score"] is not None else "—"),
            "Statut": _chip(sel["status"]) if sel else "—",
            "Qualité": (f"{qual:.0f}/100" if qual is not None else "—"),
        })
    st.dataframe(rows, hide_index=True)
    st.caption(
        "Correspondance : ✅ OK = horaires cohérents entre sources ; "
        "🚫 INCOHERENT = exclu ; ⚠️ revue requise = jamais automatisée."
    )


def screen_analyses(config, secrets, db, pipeline) -> None:
    st.subheader("Fiche d'analyse")
    fxs = _visible_fixtures(db)
    if not fxs:
        st.info("Aucun match à afficher.")
        return
    labels = {f"{r['home_team']} vs {r['away_team']} ({_fmt_kickoff(parse_iso(r['kickoff_utc']))})":
              r["fixture_key"] for r in fxs}
    choice = st.selectbox("Match", list(labels.keys()), key="sel_analysis_fixture")
    fx_key = labels[choice]
    fx = db.get_fixture(fx_key)
    sel = db.execute(
        "SELECT * FROM selections WHERE fixture_key = ? ORDER BY id DESC LIMIT 1",
        (fx_key,),
    ).fetchone()
    analysis = db.latest_analysis(fx_key)

    st.write(f"**{fx['home_team']} vs {fx['away_team']}** — "
             f"{_fmt_kickoff(parse_iso(fx['kickoff_utc']))} — "
             f"correspondance : {_chip(fx['matching_status'])}")

    t1, t2, t3 = st.tabs(["Faits & cotes", "Probabilités", "IA (analyste / contradicteur)"])
    with t1:
        bundle_odds = db.latest_odds(fx_key)
        if bundle_odds:
            st.dataframe(
                [
                    {
                        "Source": o["provider"],
                        "Marché": o["market"],
                        "Issue": o["outcome"],
                        "Cote": o["odds"],
                        "Observée (UTC)": o["observed_at_utc"],
                    }
                    for o in bundle_odds
                ],
                hide_index=True,
            )
        else:
            st.warning("Aucune cote horodatée pour ce match.")
        avail = db.availability(fx_key)
        if avail:
            st.markdown("**Indisponibilités**")
            for a in avail:
                tag = "confirmée" if a["status"] == "confirmed" else "NON confirmée (risque signalé)"
                st.caption(f"• {a['player']} ({a['team']}) — {a['reason']} — {tag}")
    with t2:
        if sel is None:
            st.info("Aucune sélection calculée.")
        else:
            st.write(
                f"Marché **{sel['market']}**, issue **{sel['outcome']}** — "
                f"probabilité **{sel['probability']:.1%}** (modèle {sel['model_version']}), "
                f"cote **{sel['odds']}** observée le {sel['odds_observed_at_utc']}, "
                f"score **{sel['score']:.0f}/100** → {_chip(sel['status'])}"
            )
            if sel["reasons"]:
                st.markdown("**Raisons / alertes**")
                for r in __import__("football.utils", fromlist=["jload"]).jload(sel["reasons"] or "[]"):
                    st.caption(f"• {r}")
            st.caption(sel["justification"])
    with t3:
        if analysis is None:
            st.info("Aucune analyse archivée pour ce match.")
        else:
            from football.utils import jload

            analyst = jload(analysis["analyst_json"])
            critic = jload(analysis["critic_json"])
            valid = bool(analysis["validated"])
            st.caption(
                f"Mode : {analysis['mode']} — validation stricte : "
                + ("✅ validée (citations + audit numérique)" if valid
                   else "⚠️ non validée → repli gabarit sans bonus")
            )
            col_a, col_b = st.columns(2)
            for col, part, title in ((col_a, analyst, "🧠 Analyste"),
                                     (col_b, critic, "⚔️ Contradicteur")):
                with col:
                    st.markdown(f"**{title}**")
                    if not part:
                        st.warning("Aucun contenu (IA indisponible).")
                        continue
                    st.write(part["summary"])
                    st.markdown("*Arguments favorables*")
                    for f in part.get("supporting_factors", []):
                        st.caption(f"• {f['fact']} _(source : {f['source_ref']})_")
                    st.markdown("*Risques*")
                    for f in part.get("risk_factors", []):
                        st.caption(f"• {f['fact']} _(source : {f['source_ref']})_")
                    st.markdown("*Inconnues*")
                    for u in part.get("unknowns", []):
                        st.caption(f"• {u}")
                    st.caption(f"Action proposée : **{part.get('recommended_action')}** "
                               "(informationnelle — le moteur décide)")


def screen_coupons(config, secrets, db, pipeline) -> None:
    st.subheader("Coupons — validation humaine")
    coupons = db.coupons_by_status("REVIEW_REQUIRED")
    if not coupons:
        st.info("Aucun coupon à valider. Le constructeur n'impose jamais un "
                "coupon : si les contraintes ne sont pas satisfaites, il n'y "
                "en a aucun.")
        render_diagnostic_panel(db, config, pipeline)
        return
    for c in coupons:
        from football.utils import jload

        sels = jload(c["selections_json"])
        st.markdown(
            f"### Coupon {c['coupon_id'][:8]} — score {c['score']:.0f} — "
            f"cote combinée {c['combined_odds']} — prob. ≈ {c['combined_probability']:.0%}"
        )
        for s in sels:
            fx = db.get_fixture(s["fixture_key"])
            st.write(
                f"• {fx['home_team']} vs {fx['away_team']} — "
                f"{s['market']} / {s['outcome']} @ {s['odds']} "
                f"(p ≈ {s['probability']:.0%}, cote du {s['odds_observed_at_utc']})"
            )
        st.caption("Probabilité conjointe = produit des probabilités "
                   "(approximation d'indépendance, pas une garantie).")
        constraints = jload(c["constraints_json"] or "[]")
        for k in constraints:
            st.caption(f"contrainte : {k}")
        c1, c2, c3 = st.columns([1, 1, 2])
        comment = c3.text_input("Commentaire opérateur (optionnel)", key=f"comment_{c['coupon_id']}")
        if c1.button("✅ Approuver", key=f"approve_{c['coupon_id']}"):
            db.set_coupon_status(c["coupon_id"], "APPROVED", utcnow(),
                                 approved_by="admin", comment=comment)
            st.success("Coupon approuvé — passez à l'écran Prévisualisation.")
            st.rerun()
        if c2.button("❌ Refuser", key=f"reject_{c['coupon_id']}"):
            db.set_coupon_status(c["coupon_id"], "REJECTED", utcnow(),
                                 approved_by="admin", comment=comment)
            st.warning("Coupon rejeté.")
            st.rerun()
        st.divider()
    st.caption(config.responsible_gambling_notice if config else "")


def _coupon_display(c, db):
    from football.utils import jload, parse_iso as pi

    sels = jload(c["selections_json"])
    fx_by_key = {}
    rows = []
    for s in sels:
        fx = db.get_fixture(s["fixture_key"])
        if fx is None:
            continue
        kickoff = pi(fx["kickoff_utc"])
        fx_by_key[s["fixture_key"]] = {
            "home_team": fx["home_team"],
            "away_team": fx["away_team"],
            "kickoff_label": _fmt_kickoff(kickoff),
            "competition_label": fx["competition_slug"],
        }
        rows.append({
            **s,
            "odds_observed_label": s["odds_observed_at_utc"][5:16].replace("T", " "),
            "quality_label": f"{s.get('score', 'n.c.')}",
        })
    analysis_risks = []
    for s in sels:
        a = db.latest_analysis(s["fixture_key"])
        if a:
            analyst = jload(a["analyst_json"])
            if analyst:
                analysis_risks += [f["fact"] for f in analyst.get("risk_factors", [])][:2]
    return {
        "selections": rows,
        "combined_odds": c["combined_odds"],
        "combined_probability": c["combined_probability"],
        "kind_label": "Coupon prudent",
        "date_label": utcnow().strftime("%Y-%m-%d"),
        "model_version": "marché normalisé",
        "version": c["version"],
        "risks": analysis_risks[:4] or ["qualité des données limitée",
                                        "évolution des cotes non suivie après publication"],
        "risks_summary": "; ".join(analysis_risks[:2]) or "voir fiche",
        "fixture_by_key": fx_by_key,
    }


def screen_preview(config, secrets, db, pipeline) -> None:
    st.subheader("Prévisualisation & publication (canal de test)")
    # le message de publication est conservé dans la session : le rerun
    # qui suit la publication ne doit pas le faire disparaître
    last_msg = st.session_state.get("last_publish_msg")
    if last_msg:
        st.success(last_msg)
    approved = db.coupons_by_status("APPROVED")
    if not approved:
        st.info("Aucun coupon approuvé. Approuvez d'abord un coupon dans "
                "l'écran Coupons.")
        return
    for c in approved:
        st.markdown(f"### Coupon {c['coupon_id'][:8]} — cote {c['combined_odds']}")
        col_a, col_b = st.columns([2, 1])
        with col_a:
            if st.button("🖼 Générer l'aperçu", key=f"preview_{c['coupon_id']}"):
                disp = _coupon_display(c, db)
                out = ROOT / "data" / "previews" / f"{c['coupon_id']}.png"
                st.session_state[f"preview_{c['coupon_id']}"] = (
                    render_coupon_png(disp, config, out, disp["fixture_by_key"]),
                    disp,
                )
        with col_b:
            st.write("")
            if st.button("📤 Publier (canal de test)", key=f"publish_{c['coupon_id']}"):
                disp = _coupon_display(c, db)
                out = ROOT / "data" / "previews" / f"{c['coupon_id']}.png"
                render = render_coupon_png(disp, config, out, disp["fixture_by_key"])
                publisher = TelegramPublisher(
                    db, config, secrets,
                    outbox_dir=str(ROOT / secrets.outbox_dir),
                )
                try:
                    res = publisher.publish(c["coupon_id"], render)
                    mode = "MODE SEC (écriture locale, pas de réseau)" if res.dry_run \
                        else "envoyé"
                    msg = (f"Publié ({mode}) — message {res.telegram_message_id} "
                           f"— canal {res.channel}")
                    st.session_state["last_publish_msg"] = msg
                    st.success(msg)
                except (PublishBlocked, PublishError) as exc:
                    st.session_state["last_publish_msg"] = None
                    st.error(str(exc))
                st.rerun()
        pv = st.session_state.get(f"preview_{c['coupon_id']}")
        if pv:
            render, disp = pv
            st.image(render.path)
            st.text_area("Légende Telegram", render.caption, disabled=True,
                         height=140, key=f"caption_{c['coupon_id']}")
            st.caption(f"PNG : {render.width}×{render.height}, "
                       f"{render.size_bytes / 1024:.0f} Ko, hash {render.png_hash[:16]}…")


def screen_history(config, secrets, db, pipeline) -> None:
    st.subheader("Historique & performance")
    recs = db.calibration_records()
    if recs:
        records = [CalibRecord(r["predicted_probability"], r["observed"]) for r in recs]
        c1, c2, c3 = st.columns(3)
        b = brier_score(records)
        l = log_loss(records)
        h = hit_rate(records)
        with c1:
            st.metric("Brier", f"{b:.4f}" if b is not None else "n/a")
        with c2:
            st.metric("Log loss", f"{l:.4f}" if l is not None else "n/a")
        with c3:
            st.metric("Taux de réussite", f"{h:.0%}" if h is not None else "n/a")
        st.caption(f"{len(records)} probabilités archivées. Ces mesures "
                   "évaluent le système ; elles ne prédisent aucun match.")
        by_market: dict = {}
        for r in recs:
            by_market.setdefault(r["market"], []).append(
                CalibRecord(r["predicted_probability"], r["observed"]))
        st.dataframe(
            [
                {"Marché": m, **v}
                for m, v in per_market(by_market).items()
            ],
            hide_index=True,
        )
    else:
        st.info("Aucune probabilité réglée pour l'instant : les métriques "
                "apparaîtront après les premiers matchs archivés.")

    events = db.published_events(50)
    if events:
        st.markdown("**Publications**")
        st.dataframe(
            [
                {
                    "Coupon": e["coupon_id"][:8],
                    "Canal": e["channel"],
                    "Mode": "sec" if e["dry_run"] else "réel",
                    "Message": e["telegram_message_id"],
                    "PNG (hash court)": (e["png_hash"] or "")[:12],
                    "Le (UTC)": e["published_at_utc"],
                }
                for e in events
            ],
            hide_index=True,
        )

    allr = db.execute(
        """
        SELECT f.fixture_key, f.home_team, f.away_team, r.home_score, r.away_score,
               s.market, s.outcome, s.probability, sr.outcome AS result
        FROM fixtures f
        LEFT JOIN results r ON r.fixture_key = f.fixture_key
        LEFT JOIN selections s ON s.fixture_key = f.fixture_key
        LEFT JOIN selection_results sr ON sr.selection_id = s.id
        ORDER BY f.kickoff_utc DESC LIMIT 200
        """
    ).fetchall()
    if allr:
        import csv

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["fixture", "home", "away", "home_score", "away_score",
                         "market", "outcome", "prob", "result"])
        for row in allr:
            writer.writerow([row["fixture_key"], row["home_team"], row["away_team"],
                             row["home_score"], row["away_score"], row["market"],
                             row["outcome"], row["probability"], row["result"]])
        st.download_button("📄 Exporter CSV", buf.getvalue(),
                           file_name="football_history.csv", mime="text/csv",
                           key="btn_export_csv")


def screen_settings(config, secrets, db, pipeline) -> None:
    st.subheader("Paramètres")
    if config is None:
        st.error("Configuration invalide — corrigez config/football.yaml.")
        return
    st.caption("Les réglages ci-dessous sont validés avant enregistrement et "
               "prennent effet à la prochaine exécution de la chaîne.")
    labels = {
        "premier_league": "Premier League", "la_liga": "Liga", "serie_a": "Serie A",
        "bundesliga": "Bundesliga", "ligue_1": "Ligue 1",
        "eredivisie": "Eredivisie", "champions_league": "Ligue des champions",
        "europe_league": "Ligue Europa", "coppa_italia": "Coupe d'Italie",
        "premier_league_cup": "Coupe d'Angleterre", "primeira_liga": "Primeira Liga",
        "championship": "Championship", "conference_league": "Conference League",
        "ligue_2": "Ligue 2", "serie_b": "Serie B", "segunda_division": "Segunda Division",
        "brasileirao": "Brasileirão", "mls": "MLS", "afcon": "CAN", "can_qualif": "CAN Qualifications",
        "uefa_nations_league": "Ligue des nations UEFA", "wc_qualif": "Qualif. Coupe du monde",
    }
    get_label = lambda c: labels.get(c, c.replace("_", " ").title())
    ai_labels = {
        "gemini_free": "Gemini (cloud)", "off": "Désactivée — gabarits",
        "ollama_local": "Ollama (local)",
    }
    all_competitions = sorted(KNOWN_COMPETITIONS)
    fallback_options = sorted(COMMON_PROVIDER_COMPETITIONS)

    with st.form("settings_form"):
        st.markdown("**IA et recherche**")
        ai_mode = st.selectbox(
            "Moteur d'analyse", list(ai_labels),
            index=list(ai_labels).index(config.ai_mode), format_func=ai_labels.get,
            help="Gemini exige une GEMINI_API_KEY dans .env ; Ollama doit être lancé localement.",
        )
        web_search = st.checkbox(
            "Autoriser la recherche Web de Gemini",
            value=config.ai_enable_web_search,
            help=("Gemini peut rechercher des informations récentes pour relever des "
                  "risques ou inconnues. Les chiffres de décision restent ceux des API football."),
        )

        st.markdown("**Collecte quotidienne**")
        priority = st.multiselect(
            "Compétitions prioritaires", all_competitions,
            default=list(config.competitions), max_selections=3,
            format_func=get_label,
            help="L'application cherche ces compétitions en premier.",
        )
        fallback = st.multiselect(
            "Compétitions de repli", fallback_options,
            default=list(config.fallback_competitions), format_func=get_label,
            help="Consultées seulement si aucun match prioritaire n'est disponible."
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            app_mode = st.selectbox("Mode de publication", ["pilot", "public"],
                                    index=(0 if config.mode == "pilot" else 1))
            api_budget = st.number_input("Budget API-Football / jour", 1, 100,
                                         config.api_football_daily_budget)
        with c2:
            refresh_window = st.number_input("Fenêtre de rafraîchissement (min)", 30, 720,
                                             config.refresh_window_minutes, step=30)
            max_publications = st.number_input("Publications maximum / jour", 1, 10,
                                                config.coupons.max_published_per_day)
        with c3:
            pilot_max = st.number_input("Sélections max — pilote", 2, 3,
                                        config.coupons.pilot_max_selections)
            public_max = st.number_input("Sélections max — public", 2, 5,
                                         config.coupons.public_max_selections)

        st.markdown("**Qualité et sélection**")
        q1, q2, q3, q4 = st.columns(4)
        with q1:
            eligible = st.number_input("Seuil éligible", 1.0, 100.0,
                                       float(config.quality.eligible_threshold), step=1.0)
        with q2:
            watch = st.number_input("Seuil surveillance", 1.0, 99.0,
                                    float(config.quality.watch_threshold), step=1.0)
        with q3:
            max_odds_age = st.number_input("Âge max des cotes (heures)", 1.0, 72.0,
                                           float(config.quality.max_odds_age_hours), step=1.0)
        with q4:
            min_probability = st.number_input("Probabilité minimale", 0.30, 0.99,
                                              float(config.quality.min_selection_probability), step=0.01)

        saved = st.form_submit_button("Enregistrer les paramètres", type="primary",
                                      icon=":material/save:")

    if saved:
        try:
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
            raw["project"]["mode"] = app_mode
            raw["competitions"] = priority
            raw["fallback_competitions"] = [c for c in fallback if c not in priority]
            raw["providers"]["api_football_daily_budget"] = int(api_budget)
            raw["quality"].update({
                "eligible_threshold": float(eligible),
                "watch_threshold": float(watch),
                "max_odds_age_hours": float(max_odds_age),
                "min_selection_probability": float(min_probability),
            })
            raw["coupons"].update({
                "pilot_max_selections": int(pilot_max),
                "public_max_selections": int(public_max),
                "max_published_per_day": int(max_publications),
            })
            raw["ai"].update({
                "mode": ai_mode,
                "enable_web_search": bool(web_search),
            })
            raw.setdefault("scheduler", {})["refresh_window_minutes"] = int(refresh_window)
            write_config(CONFIG_PATH, raw)
        except (ConfigError, OSError, TypeError, ValueError) as exc:
            st.error(f"Paramètres non enregistrés : {exc}")
        else:
            get_context.clear()
            st.success("Paramètres enregistrés. La nouvelle configuration est active.")
            st.rerun()

    st.markdown("**Secrets (masqués)**")
    st.json(secrets.masked())
    st.caption("Les clés API restent dans `.env` et ne sont jamais modifiables ni affichées ici.")
    st.warning(config.responsible_gambling_notice)


def screen_competitions(config, secrets, db, pipeline) -> None:
    st.subheader("🌍 Compétitions & Rotation Intelligente")
    if config is None:
        st.error("Configuration invalide.")
        return

    from football.rotation import build_rotator, _get_rotation
    rotator = build_rotator(config, db)
    rot_cfg = _get_rotation(config)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Mode Rotation", "🟢 Activée" if rot_cfg.enabled else "🔴 Désactivée")
    with c2:
        st.metric("Limite active simultanée", f"{rot_cfg.max_active} compétitions")
    with c3:
        st.metric("Seuil de sécurité Budget API", f"{rot_cfg.budget_safety_pct}% ({int(config.api_football_daily_budget * rot_cfg.budget_safety_pct / 100)} req/j)")

    st.markdown("---")
    st.markdown("### 📅 Plan de couverture prévisionnel (7 prochains jours)")
    plan = rotator.get_coverage_plan(days=7)

    labels = {
        "premier_league": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", "la_liga": "🇪🇸 La Liga", "serie_a": "🇮🇹 Serie A",
        "bundesliga": "🇩🇪 Bundesliga", "ligue_1": "🇫🇷 Ligue 1", "eredivisie": "🇳🇱 Eredivisie",
        "primeira_liga": "🇵🇹 Primeira Liga", "championship": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Championship",
        "champions_league": "🏆 Champions League", "europe_league": "🇪🇺 Europa League",
        "conference_league": "🇪🇺 Conference League", "coppa_italia": "🇮🇹 Coppa Italia",
        "premier_league_cup": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 EFL Cup", "ligue_2": "🇫🇷 Ligue 2", "serie_b": "🇮🇹 Serie B",
        "segunda_division": "🇪🇸 Segunda Division", "brasileirao": "🇧🇷 Brasileirão",
        "mls": "🇺🇸 MLS", "afcon": "🌍 CAN", "can_qualif": "🌍 CAN Qualif",
        "uefa_nations_league": "🇪🇺 Ligue des nations", "wc_qualif": "🌍 Qualif. Coupe du monde",
    }
    plan_data = []
    for row in plan:
        comp_names = [labels.get(c, c) for c in row["competitions"]]
        plan_data.append({
            "Date": row["date"].strftime("%A %d/%m"),
            "Compétitions analysées": ", ".join(comp_names),
            "Coût estimé (API)": f"{row['estimated_budget_used']} / {row['budget_total']} req ({row['budget_pct']}%)",
        })
    st.dataframe(plan_data, use_container_width=True)

    st.markdown("---")
    st.markdown("### 📋 Catalogue complet des compétitions")
    catalogue = rotator.get_catalogue()
    cat_data = []
    for s in catalogue:
        status_str = "🔒 Forcée (Toujours active)" if s.is_forced else ("🔄 Pool de rotation" if s.is_in_pool else "⚪ Non activée")
        provider_support = "API-Football & football-data" if s.slug in COMMON_PROVIDER_COMPETITIONS else "API-Football uniquement"

        is_scheduled = getattr(s, "is_scheduled_today", False)
        fixtures_count = getattr(s, "fixtures_today_count", 0)
        sched_str = "Oui 📅 (Analyse prévue)" if is_scheduled else "Non"
        if fixtures_count > 0:
            db_str = f"Oui ⚽ ({fixtures_count} collectés)"
        else:
            db_str = "0 (attente collecte)" if is_scheduled else "0"

        cat_data.append({
            "Compétition": labels.get(s.slug, s.slug),
            "Statut de rotation": status_str,
            "Analyse prévue aujourd'hui": sched_str,
            "Matchs en base (aujourd'hui)": db_str,
            "Fournisseurs": provider_support,
            "Dernière analyse": s.last_covered_date.strftime("%d/%m/%Y") if s.last_covered_date else "Jamais",
            "Score Prestige": s.prestige_score,
            "Coût req/j estimé": s.estimated_api_cost,
        })
    st.dataframe(cat_data, use_container_width=True)
    st.caption("ℹ️ **Explication des colonnes** : *Analyse prévue aujourd'hui* indique les compétitions planifiées par la rotation pour la journée. "
               "*Matchs en base* indique le nombre de rencontres effectivement collectées localement après le lancement de la chaîne du jour.")


def main() -> None:
    st.set_page_config(page_title="Football Coupon — validation",
                       page_icon="⚽", layout="wide")
    config, config_error, secrets, db, pipeline = get_context(
        os.environ.get("DB_PATH", ""), os.environ.get("OUTBOX_DIR", ""))
    if config is not None:
        st.session_state["tz"] = config.timezone

    with st.sidebar:
        col_title, col_btn = st.columns([4, 1])
        with col_title:
            st.markdown("### ⚙️ Serveur")
        with col_btn:
            if st.button("🛑", key="btn_shutdown_server", help="Éteindre le serveur Streamlit"):
                st.warning("⚠️ **Arrêt du serveur Streamlit en cours...**\nL'application va s'éteindre.")
                st.caption("Vous pouvez fermer cet onglet dans votre navigateur.")
                import time
                time.sleep(0.5)
                os._exit(0)
        st.divider()

    st.title("⚽ Football Coupon — analyse & validation")
    demo = pipeline is not None and pipeline.is_demo
    if demo:
        st.info(
            "**MODE DÉMO** — aucune clé API détectée : données de démonstration "
            "déterministes. Ajoutez `API_FOOTBALL_KEY` dans `.env` pour les "
            "données réelles.",
            icon="🧪",
        )
    if config_error:
        st.error(f"Configuration invalide : {config_error}")

    tab_dash, tab_comps, tab_matches, tab_analyses, tab_coupons, tab_preview, tab_history, \
        tab_settings = st.tabs(
            ["📊 Tableau de bord", "🌍 Compétitions", "⚽ Matchs", "📄 Analyses", "🎟 Coupons",
             "🖼 Prévisualisation", "📜 Historique", "⚙️ Paramètres"])
    with tab_dash:
        screen_dashboard(config, secrets, db, pipeline)
    with tab_comps:
        screen_competitions(config, secrets, db, pipeline)
    with tab_matches:
        screen_matches(config, secrets, db, pipeline)
    with tab_analyses:
        screen_analyses(config, secrets, db, pipeline)
    with tab_coupons:
        screen_coupons(config, secrets, db, pipeline)
    with tab_preview:
        screen_preview(config, secrets, db, pipeline)
    with tab_history:
        screen_history(config, secrets, db, pipeline)
    with tab_settings:
        screen_settings(config, secrets, db, pipeline)



if __name__ == "__main__":
    main()
