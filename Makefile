# ============================================================
# Football Coupon App — tâches courantes
# ============================================================

PY        ?= python3
VENV      ?= .venv
PIP       := $(VENV)/bin/pip
TEST      ?= tests
CONFIG    ?= config/football.yaml
SCHED     := $(VENV)/bin/python -m football.scheduler

.PHONY: help install test test-cov test-e2e test-app run demo \
        morning refresh settle all-jobs status clean

help:
	@echo "Cibles :"
	@echo "  make install      créer le venv et installer les dépendances"
	@echo "  make test         suite complète (352 tests, hors-ligne)"
	@echo "  make test-cov     suite + couverture"
	@echo "  make test-e2e     parcours d'intégration complet seul"
	@echo "  make test-app     smoke tests Streamlit (AppTest) seuls"
	@echo "  make run          lancer l'interface Streamlit (port 8501)"
	@echo "  make morning      chaîne du matin (collecte+analyse+coupons)"
	@echo "  make refresh      rafraîchissement des candidats avant match"
	@echo "  make settle       résultats + règlement + métriques"
	@echo "  make all-jobs     morning + refresh + settle (ordre planifié)"
	@echo "  make status       dernier run et compteurs"
	@echo "  make clean        vider data/ (base, outbox, previews)"

install:
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

test:
	$(VENV)/bin/pytest $(TEST) -q

test-cov:
	$(VENV)/bin/pytest $(TEST) -q --cov=football --cov-report=term-missing

test-e2e:
	$(VENV)/bin/pytest $(TEST)/test_pipeline_e2e.py -q

test-app:
	$(VENV)/bin/pytest $(TEST)/test_app.py -q

run:
	$(VENV)/bin/streamlit run app.py --server.port 8501

# --- Jobs planifiables (voir cron/crontab.example) -------------------

morning:
	$(SCHED) morning --config $(CONFIG)

refresh:
	$(SCHED) refresh --config $(CONFIG)

settle:
	$(SCHED) settle --config $(CONFIG)

all-jobs:
	$(SCHED) all --config $(CONFIG)

status:
	$(SCHED) status --config $(CONFIG)

clean:
	rm -rf data/football.db data/outbox data/previews
