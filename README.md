# ⚽ Football Coupon App — MVP d'analyse probabiliste et de coupons

> **Estimation probabiliste — aucun résultat garanti. Jouez de manière
> responsable.**

Application locale **Python + SQLite + Streamlit** qui collecte des données
football (cotes, classements, indisponibilités, résultats) depuis deux
fournisseurs, les croise pour détecter les incohérences, calcule des
**probabilités déterministes normalisées sur le marché**, score la qualité
des données, construit des **coupons** (max 2 sélections en mode pilote),
les soumet à une **validation humaine obligatoire**, les rend en PNG et les
publie sur **Telegram** (canal de test, mode sec par défaut). Un réglage
(settlement) journalier archive les résultats et met à jour les métriques de
calibration.

Ce projet est un **système d'aide à la décision**, pas un prédicteur de
matchs : la référence de marché (cotes normalisées) est le modèle de départ
obligatoire, aucun « pari de valeur » n'est calculé en mode `market`, et
aucune publication n'est possible sans approbation humaine explicite.

---

## 1. Fonctionnalités

| Domaine | Détails |
|---|---|
| Collecte | API-Football (primaire) + football-data.org (secondaire de contrôle). Cotes horodatées (1X2, double chance, over/under 2.5), classements → snapshots d'équipe, indisponibilités, résultats. |
| Contrôle croisé | Divergence de kickoff > 5 min → match marqué `INCOHERENT` et **exclu** de toute sélection. Noms d'équipes flous → `MATCHING_REVIEW_REQUIRED` (exclu, visible dans l'interface). |
| Probabilités | Cotes implicites **normalisées** (somme = 1, marge retirée). Écart à 0.5 (incertitude) pénalisé. Version de modèle tracée : `market_normalized_v1`. |
| Qualité | Score 0–100 par match (fraîcheur 35 %, accord sources 20 %, couverture cotes 15 %, données équipes 20 %, confiance indispos 10 %) + pénalité 25 points si contradiction. Seuils : **éligible ≥ 80**, vigilance 65–79. Âge maximal des cotes : 6 h. |
| Éligibilité (grille 100) | Qualité données **35** + accord sources **20** + stabilité des cotes **15** + probabilité de l'issue **15** + complétude de l'explication **15** (1.0 si IA validée, 0.90 en gabarit). |
| Couper les risques | Max 2 sélections (pilote) / 3 (public), 1 sélection par match, jamais la même équipe deux fois, jamais deux matchs de la même compétition à moins de 90 min l'un de l'autre (« même contexte »), jamais forcer un coupon : si rien ne passe, zéro coupon. |
| IA (optionnelle) | Modes `off` (gabarits déterministes), `gemini_free`, `ollama_local`. Gemini peut activer sa recherche Web pour relever des risques et inconnues ; les chiffres de décision restent limités aux fournisseurs football. JSON strict validé au départ : schéma complet, **références de sources obligatoirement présentes dans les données fournies**, contradiction numérique détectée (un nombre inventé → rejet → repli gabarit). |
| Publication | Approbation humaine obligatoire → PNG (Pillow, sans logos, ≤ 10 Mo) → Telegram **canal de test uniquement**. **Mode sec** si pas de token : écriture locale dans `data/outbox/`. **Idempotence** : la contrainte unique en base bloque la double publication (2e envoi = 0 message). Max 1 coupon publié / jour. |
| Interdiction lexicale | Les mots « garanti », « sûr », « infaillible » (et variantes) sont refusés dans tout texte publié, y compris en mode sec (contrôle de contenu avant envoi). |
| Réglage | Récupération des résultats, règlement des sélections et coupons, enregistrement des probabilités pour la calibration. |
| Métriques | Brier, log loss, taux de réussite, calibration par bin et par marché — archivés dans `calibration_records`, affichés dans l'onglet Historique. |
| Traçabilité | `run_log` (chaque job), `operator_actions` (chaque clic approuver/refuser/publier), version du config (hash) dans chaque analyse, `published_events` (message Telegram, hash du PNG). |
| Robustesse | Budgets d'appels (85/jour API-Football < 100 accordés), limite de débit, cache HTTP + ETag, 3 tentatives avec backoff, reprise sans doublon après redémarrage (upserts idempotents), horloge injectable (tests), **base SQLite thread-safe** (Streamlit réexécute le script dans un thread différent à chaque session/rerun : connexion partagée + verrou `RLock`), 352 tests hors-ligne. |

---

## 2. Démarrage rapide

### Prérequis

- Python **3.10+**
- Aucune clé API n'est requise : sans clé, l'application démarre en **mode
  démo** (fournisseur déterministe intégré, journée fixe, idéal pour le
  pilote et les tests).

### Installation

```bash
cd football_coupon_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# ou, avec le Makefile :
make install
```

### Lancer l'interface (mode démo)

```bash
streamlit run app.py
# ou
make run
```

Puis, dans le navigateur :

1. **Tableau de bord** → cliquer **▶ Lancer la chaîne du matin** (collecte →
   analyse → coupons).
2. **Coupons** → examiner le candidat, puis **Approuver** (ou Refuser —
   l'action est journalisée).
3. **Aperçu & publication** → générer l'aperçu PNG, vérifier la légende,
   puis **Publier (canal de test)** : sans `TELEGRAM_BOT_TOKEN`, c'est du
   **mode sec** (fichier écrit dans `data/outbox/`, aucun réseau).
4. **Historique & performance** → après les règlements (le soir), les
   métriques Brier / log loss / calibration apparaissent.
5. **Réglages** → mode démo, API, IA, publication, journal des runs.

Le fuseau d'affichage est `Africa/Ouagadougou` (modifiable dans le YAML) ;
**tout est stocké en UTC**.

### Mode réel (avec clés)

```bash
cp .env.example .env
# remplir API_FOOTBALL_KEY et FOOTBALL_DATA_TOKEN (fournisseurs),
# puis TELEGRAM_BOT_TOKEN + TELEGRAM_TEST_CHANNEL_ID (publication)
streamlit run app.py
```

Le mode démo est **automatiquement désactivé** dès qu'au moins une clé de
fournisseur est présente — il n'y a pas de mélange des deux mondes.

---

## 3. Architecture

```
                    ┌────────────────────────────────────────────┐
                    │                app.py (Streamlit)          │
                    │  7 onglets — actions humaines tracées      │
                    └──────┬─────────────────────────┬───────────┘
                           │ (boutons CLI équivalents)│
                    ┌──────▼─────────────┐   ┌───────▼──────────┐
                    │   scheduler.py     │   │   render.py      │
                    │ morning/refresh/   │   │ PNG (Pillow),    │
                    │ settle/all/status  │   │ légende, mots    │
                    │ (cron local)       │   │ interdits        │
                    └──────┬─────────────┘   └───────┬──────────┘
                           │                         │
                    ┌──────▼─────────────────────────▼──────────┐
                    │            pipeline.py (orchestrateur)    │
                    │  collecte → analyse → coupons → règlement │
                    └──┬──────────┬──────────┬──────────┬───────┘
        ┌──────────────┘          │          │          └────────────┐
┌───────▼────────┐      ┌─────────▼───┐ ┌───▼─────────┐    ┌────────▼──────┐
│ providers/     │      │ quality.py  │ │ selection.py│    │ telegram.py   │
│ api_football   │      │ (grille     │ │ (score 100, │    │ dry-run, idem-│
│ football_data  │      │  35/20/15/  │ │   grilles,  │    │ pótection du  │
│ demo (offline) │      │  15/15/15)  │ │  éligibilité│    │  double pub   │
│ cache, retry,  │      └─────────────┘ └─────────────┘    └───────────────┘
│ rate limit,    │      ┌─────────────────────────┐
│ budget 85/j    │      │ analysis.py (IA)        │
└───────┬────────┘      │ off/gemini/ollama,      │
        │               │ schéma strict, refs     │
┌───────▼────────┐      │ sources, contradictions │
│ storage.py     │      └─────────────────────────┘
│ SQLite (20     │
│ tables, UTC)   │
└────────────────┘
```

### Modules

| Module | Rôle |
|---|---|
| `app.py` | Interface Streamlit (7 onglets), actions humaines, journal des opérateurs. |
| `football/config.py` | Chargement/validation du YAML + `.env` (Secrets). Toute erreur → message clair, pas de crash muet. |
| `football/providers/base.py` | Contrat fournisseur, `ProviderError`, `QuotaExceeded`. |
| `football/providers/api_football.py` | Fournisseur primaire (budget 85 req/jour). |
| `football/providers/football_data.py` | Fournisseur secondaire de contrôle (10 req/min). |
| `football/providers/demo.py` | Fournisseur hors-ligne déterministe (mode démo). |
| `football/http.py` | Client HTTP : cache + ETag, 3 tentatives, backoff, limite de débit. |
| `football/cache.py` | Cache des réponses en base (TTL, ETag) — réduit les appels API. |
| `football/normalize.py` | Normalisation des noms d'équipes ; correspondance floue avec seuils `review` (0.78) / `match` (0.92). |
| `football/probabilities.py` | Cotes implicites normalisées (marge retirée), `ProbabilityError` si cote invalide. |
| `football/features.py` | Extraction des caractéristiques par match (cotes, forme, classement). |
| `football/quality.py` | Score de qualité des données 0–100 (fraîcheur 35 %, accord sources 20 %, couverture 15 %, équipes 20 %, indispos 10 %) ; pénalité contradiction 25. |
| `football/selection.py` | Grille d'éligibilité 35/20/15/15/15 ; statuts `ELIGIBLE` / `REVIEW_REQUIRED` / `EXCLUDED`. |
| `football/analysis.py` | Couche IA (off/gemini/ollama) + validation stricte du JSON (schéma, références sources, contradiction numérique) + gabarits français de repli. |
| `football/coupons.py` | Constructeur de coupons : contraintes, cote jointe et probabilité conjointe (indépendance **affichée comme hypothèse**), score du coupon. |
| `football/pipeline.py` | Orchestrateur : `run_morning`, `run_refresh`, `run_settle` ; reprise idempotente. |
| `football/scheduler.py` | CLI compatible cron : `python3 -m football.scheduler {morning\|refresh\|settle\|all\|status}`. |
| `football/storage.py` | SQLite : 20 tables, migrations versionnées (v3), upserts idempotents, tout en UTC. |
| `football/metrics.py` | Brier, log loss, hit rate, calibration par bin/marché. |
| `football/render.py` | PNG (Pillow) sans logos, légende Telegram ≤ 1024 caractères, contrôle des mots interdits. |
| `football/telegram.py` | Publication : dry-run par défaut, idempotence (contrainte unique), canal de test uniquement. |
| `football/utils.py` | Horodatage UTC, fuseau d'affichage, parsing ISO, hash. |

### Flux de données (jour type)

1. **Matin (≈ 08:00)** — `scheduler morning` :
   - collecte de la journée (fixtures, cotes 1X2 + DC + OU, classements,
     indispos) des deux fournisseurs ;
   - contrôle croisé : kickoff divergent → `INCOHERENT` (exclu), noms flous →
     `MATCHING_REVIEW_REQUIRED` (exclu, signalé) ;
   - analyse : probabilités normalisées, qualité des données, grille
     d'éligibilité, explication (IA ou gabarit) ;
   - construction des coupons candidats (≤ 2 sélections en pilote) →
     `REVIEW_REQUIRED` — **rien n'est publié**.
2. **Humain** — dans l'interface : approuver/refuser (traçé), prévisualiser le
   PNG, publier sur le canal de test (mode sec si pas de token).
3. **2–3 h avant les matchs** — `scheduler refresh` : rafraîchissement ciblé
   des candidats (cache HTTP → peu d'appels API).
4. **Soir (après les matchs)** — `scheduler settle` : résultats, règlement des
   sélections/coupons, enregistrement des probabilités → métriques.

### Modèle de données (SQLite, 20 tables, UTC)

| Table | Contenu |
|---|---|
| `schema_migrations` | Version du schéma (v3). |
| `fixtures` | Matchs de la journée (clé primaire `fixture_key` = compétition+coupe+équipes), statut de correspondance. |
| `fixtures_providers` | Identifiants par fournisseur (reprise sans doublon). |
| `odds_snapshots` | Cotes horodatées par fournisseur/marché/issue (unique : fournisseur+match+maker+marché+issue+horodatage). |
| `team_snapshots` | Classement/forme des équipes (P/W/D/L, buts, forme récente). |
| `availability_snapshots` | Indisponibilités (cartons, blessures). |
| `source_observations` | Observations brutes par source (audit). |
| `http_cache` | Cache des réponses API (TTL, ETag). |
| `api_usage` | Compteur d'appels par fournisseur/jour (budgets). |
| `match_features` | Caractéristiques calculées par match. |
| `analyses` | Analyses (analyste + contradicteur IA), version du config (hash). |
| `selections` | Sélections par match (marché, issue, cote, probabilité, score qualité, score éligibilité, statut, justification, références). |
| `coupons` | Coupons candidats/approuvés/publiés (sélection(s), cote jointe, probabilité conjointe). |
| `published_events` | Publications réelles/simulées (message Telegram, hash PNG, mode sec) — **contrainte unique anti-doublon**. |
| `results` | Résultats finaux (score, statut). |
| `selection_results` | Issue de chaque sélection (won/lost) — alimente la calibration. |
| `coupon_results` | Issue de chaque coupon. |
| `calibration_records` | Probabilités archivées (input de Brier/log loss/calibration). |
| `operator_actions` | Journal des actions humaines (approuver/refuser/publier). |
| `run_log` | Journal des runs (job, statut, compteurs, erreurs). |

---

## 4. Moteur probabiliste (déterministe)

**Règle fondatrice** : en mode `market` (seul mode autorisé au MVP), la
référence de marché est le **modèle de départ obligatoire**. Les cotes
implied sont normalisées pour retirer la marge du bookmaker :

```
p(i) = (1 / cote(i)) / Σ_j (1 / cote(j))      # sur l'ensemble des issues
```

- Cote ≤ 1.0 ou nulle → `ProbabilityError` (sélection exclue, jamais de
  probabilité inventée).
- Probabilité proche de 0.5 (incertitude forte) → pénalisée par le score de
  coupon (pénalité par paliers : ≥ 0.5 → 1.0 ; 0.25–0.5 → 0.75 ; < 0.25 → 0.5).
- Probabilité minimale d'une sélection : **0.45** (configurable).
- La probabilité **conjointe** d'un coupon est le produit des probabilités —
  l'**indépendance est affichée comme hypothèse** sur le PNG et la légende
  (jamais présentée comme un fait).
- `poisson` existe comme mode de config mais est **banni au MVP** : il
  n'est autorisé qu'après validation chronologique (phases 5/6 du plan
  initial).

### Grille d'éligibilité (100 points)

| Critère | Poids | Détail |
|---|---|---|
| Qualité des données | 35 | Score `quality.py` (0–100) × 0.35 |
| Accord des sources | 20 | Proximité des cotes des deux fournisseurs |
| Stabilité des cotes | 15 | Variation limitée sur la fenêtre |
| Probabilité de l'issue | 15 | Distance à 0.45 (seuil minimal) |
| Complétude de l'explication | 15 | 1.0 si l'analyse IA est validée, 0.90 en gabarit déterministe |

Statuts : **ELIGIBLE ≥ 80**, **REVIEW_REQUIRED 65–79** (visible, jamais
publié seul), **EXCLUDED < 65** (ou contradiction → −25 points).

---

## 5. Couche IA (optionnelle, strictement contrôlée)

- Modes : `off` (défaut — gabarits français déterministes), `gemini_free`,
  `ollama_local`.
- **Schéma JSON strict** : tout champ manquant ou au mauvais type → rejet
  complet (pas de « réparation » implicite).
- **Références de sources** : chaque donnée citée par l'analyste doit
  exister dans les données effectivement fournies ; une référence inexistante
  → rejet.
- **Contradiction numérique** : tout nombre mentionné dans le texte et absent
  des données (et hors échelle explicite « /100 ») → rejet.
- **Contredicteur** : un second passage (même fournisseur) cherche les
  faiblesses ; un score d'analyse est produit (« 80.0/100 »).
- En cas d'erreur, de timeout ou de rejet : **repli automatique sur les
  gabarits déterministes** (qualité de l'explication 0.90/1.0) — le pipeline
  ne s'arrête jamais à cause de l'IA.

---

## 6. Contrôles de publication (sécurité)

1. `require_human_approval: true` — **non désactivable** au MVP (config
   validée au chargement).
2. Un coupon ne peut être publié que s'il est **APPROVED** par un humain
   (bouton dédié, action journalisée dans `operator_actions`).
3. **Mode sec** par défaut : sans `TELEGRAM_BOT_TOKEN`, la « publication »
   écrit dans `data/outbox/` (message texte + PNG) — zéro réseau, zéro envoi.
4. **Idempotence** : `published_events` porte une contrainte unique sur le
   coupon — une 2e publication lève `PublishBlocked` et **0 message est
   envoyé** (pas de réessai automatique).
5. Max **1 coupon publié par jour** (config `max_published_per_day: 1`).
6. **Canal de test uniquement** pendant le pilote (`TELEGRAM_TEST_CHANNEL_ID`).
7. **Mots interdits** (`garanti`, `sûr`, `infaillible`, variantes) : refusés
   par le rendu, y compris en mode sec — le contrôle de contenu précède tout
   écrit.
8. Mention obligatoire sur chaque PNG et légende :
   « Estimation probabiliste — aucun résultat garanti. Jouez de manière
   responsable. »
9. Budgets API logiciels (85/jour < 100 API-Football) : au-delà, le
   fournisseur lève `QuotaExceeded` et le pipeline s'arrête proprement sans
   analyser de données potentiellement périmées.

---

## 7. Configuration

Deux fichiers, tous les deux versionnés dans les analyses (hash du YAML) :

### `config/football.yaml` (règles du jeu)

| Section | Clés principales |
|---|---|
| `project` | `timezone: Africa/Ouagadougou`, `mode: pilot`, `require_human_approval: true` |
| `competitions` | Liste prioritaire (1–3) : `premier_league`, `la_liga`, `serie_a` |
| `fallback_competitions` | Repli quotidien si la liste prioritaire ne renvoie aucun match : Bundesliga, Ligue 1, Eredivisie, Ligue des champions, Ligue Europa ; uniquement des compétitions couvertes par les deux fournisseurs |
| `markets` | `match_winner`, `double_chance`, `over_under_2_5` (fixes au MVP) |
| `providers` | Primaire/secondaire, `api_football_daily_budget: 85`, `max_retries: 3`, timeout HTTP |
| `quality` | Seuils 80/65, `max_odds_age_hours: 6`, `kickoff_tolerance_minutes: 5`, poids du score qualité |
| `selection` | Grille 35/20/15/15/15, `min_selection_probability: 0.45` |
| `coupons` | `pilot_max_selections: 2`, `public_max_selections: 3`, `same_context_minutes: 90`, `max_published_per_day: 1` |
| `ai` | `mode: off\|gemini_free\|ollama_local`, `enable_web_search: true\|false`, `require_valid_source_refs: true`, `prompt_version: v1` |
| `publishing` | Canal de test, `max_caption_chars: 1024`, `max_png_bytes`, mention RG/responsable |
| `probabilities` | `source: market` (seul mode MVP), `model_version: market_normalized_v1` |
| `scheduler` | `refresh_window_minutes: 180` |

### `.env` (secret, à ne jamais committer)

| Variable | Effet |
|---|---|
| `API_FOOTBALL_KEY` | Active le fournisseur primaire réel. |
| `FOOTBALL_DATA_TOKEN` | Active le fournisseur secondaire réel. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | IA cloud (mode `gemini_free`). |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | IA locale (mode `ollama_local`). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_TEST_CHANNEL_ID` | Publication réelle (sinon mode sec). |
| `DB_PATH`, `OUTBOX_DIR` | Emplacements locaux (défauts `data/`). |

**Mode démo** : aucune clé fournisseur → `DemoProvider` déterministe
(journée ancrée sur le jour courant, bascule à 21 h UTC ; coups d'envoi
16 h–21 h UTC ; résultats disponibles après 23 h 50 UTC du jour du match).

L'onglet **Paramètres** permet de modifier directement les réglages non
secrets (IA, recherche Web, compétitions, quotas, seuils de qualité, coupons
et planification). Chaque modification est validée puis écrite de façon
atomique dans le YAML. Les clés restent uniquement dans `.env`.

---

## 8. Planificateur (cron)

CLI compatible cron (horloge du serveur) :

```bash
python3 -m football.scheduler morning   # collecte + analyse + coupons
python3 -m football.scheduler refresh   # rafraîchissement des candidats (2–3 h avant)
python3 -m football.scheduler settle    # résultats + règlement + métriques
python3 -m football.scheduler all       # les trois, dans l'ordre
python3 -m football.scheduler status    # dernier run + compteurs
```

Exemple de crontab (fuseau du serveur, ici UTC — voir `cron/crontab.example`) :

```cron
# Minute Heure  jour  mois  semaine  commande
0  8    * * *   cd /chemin/football_coupon_app && .venv/bin/python -m football.scheduler morning  >> data/cron.log 2>&1
0 14,15 * * *   cd /chemin/football_coupon_app && .venv/bin/python -m football.scheduler refresh >> data/cron.log 2>&1
30 23  * * *   cd /chemin/football_coupon_app && .venv/bin/python -m football.scheduler settle   >> data/cron.log 2>&1
```

La chaîne est **idempotente** : une re-collecte du même jour ne crée ni
doublon de match ni doublon de cote (upserts + contraintes uniques), et une
reprise après redémarrage ne republie pas un coupon déjà publié.

---

## 9. Tests

**352 tests, tous hors-ligne** (aucun appel réseau, horloges injectées) :

```bash
make test          # pytest complet
make test-cov      # avec couverture
pytest tests/test_pipeline_e2e.py -q   # parcours complet seul
```

| Fichier | Couvre |
|---|---|
| `test_pipeline_e2e.py` | Parcours complet : collecte (8 matchs, 1 incohérent exclu, 1 cote périmée exclue) → analyse → coupons (contraintes) → approbation → rendu PNG → publication mode sec (double pub bloquée) → règlement → calibration ; panne primaire/secondaire ; quota ; reprise sans doublon ; mots interdits. |
| `test_app.py` | Lancement de l'app, 7 onglets, chaîne complète depuis l'UI (AppTest), blocage de la 2e publication. |
| `test_providers.py` | Fournisseurs (réels via mocks HTTP, démo) : cotes, fixtures, résultats, quota, horodatages. |
| `test_probabilities.py` | Normalisation, marge retirée, erreurs de cote, edge = 0 par construction. |
| `test_quality.py`, `test_selection.py` | Grille qualité 0–100, pénalité contradiction, grille d'éligibilité, statuts. |
| `test_coupons.py` | Contraintes (1/match, pas d'équipe commune, contexte 90 min), cote jointe, score, affichage par taille. |
| `test_analysis_validator.py`, `test_ai_providers.py`, `test_templates.py` | Schéma strict, références sources, contradiction numérique, repli gabarit, modes off/gemini/ollama. |
| `test_normalize.py` | Correspondance d'équipes (seuils flous, mismatch, review). |
| `test_storage.py` | Migrations v1→v3, idempotence des upserts, contraintes uniques, thread-safety (base créée dans un thread, utilisée dans d'autres — scénario Streamlit). |
| `test_metrics.py` | Brier, log loss, hit rate, bins de calibration, par marché. |
| `test_telegram.py`, `test_render.py` | Idempotence de publication, mots interdits, PNG (taille, hash, mention). |
| `test_http_transport.py`, `test_cache.py`, `test_rate_limiter.py` | Retries/backoff, cache ETag/TTL, limites de débit. |
| `test_scheduler.py` | CLI, ordre des jobs, stats. |
| `test_config.py` | Validation YAML/.env, erreurs claires. |

---

## 10. Structure du dépôt

```
football_coupon_app/
├── app.py                  # Interface Streamlit (7 onglets)
├── requirements.txt
├── Makefile                # install / test / run / jobs
├── .env.example            # Modèle de secrets (à copier en .env)
├── config/
│   └── football.yaml       # Règles du jeu (versionnées dans les analyses)
├── cron/
│   └── crontab.example     # Planning cron de référence
├── data/                   # SQLite + outbox + previews (gitignoré)
├── football/               # Package métier (voir table des modules)
└── tests/                  # 350 tests hors-ligne
```

## 11. Limites assumées (MVP)

- **1–2 compétitions, 3 marchés** : élargissement ultérieur par décision
  documentée, pas par dérive.
- **1 coupon/jour max** et canal de test uniquement.
- Probabilités = **référence de marché** : le système mesure la qualité de
  ses données et la calibration de ses probabilités ; il ne promet rien sur
  les matchs.
- L'indépendance des sélections d'un coupon est une **hypothèse affichée**,
  pas une garantie.
- Pas de « valeur » (edge) calculée : interdit en mode `market`, et c'est
  volontaire — le MVP ne cherche pas à battre le marché, il cherche à ne
  jamais publier de donnée non vérifiée.

## 12. Dépannage

| Symptôme | Cause / solution |
|---|---|
| « Mode démo actif » dans l'interface | Normal sans clés. Remplir `.env` pour le réel. |
| Publication en « MODE SEC » | `TELEGRAM_BOT_TOKEN` absent → écriture locale (comportement voulu). |
| `PublishBlocked : déjà publié` | Idempotence : le coupon est déjà publié. Attendre le lendemain. |
| Match `INCOHERENT` | Divergence de kickoff > 5 min entre fournisseurs → exclu par conception. |
| Match `MATCHING_REVIEW_REQUIRED` | Noms d'équipes flous entre sources → exclu, à vérifier manuellement dans l'onglet Matchs. |
| Run `quota_exceeded` | Budget journalier API atteint → le job s'arrête proprement ; relancer le lendemain. |
| `ConfigError` au démarrage | YAML invalide : le message indique la clé en cause (le mode ne démarre pas avec des règles brisées). |
| Base corrompue / réinitialiser | Supprimer `data/football.db` (les migrations recréent le schéma v3). |
