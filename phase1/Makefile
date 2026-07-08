.PHONY: setup download-all download-parallel clean-all load-all test all down reset run-airflow

VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PIP ?= $(VENV)/bin/pip

# FIX #17: Add venv setup to Makefile
setup:
        docker-compose up -d
        sleep 10
        python3 -m venv $(VENV) || true
        $(PIP) install --upgrade pip
        $(PIP) install -r requirements.txt
        $(PYTHON) -c "from database.connection import init_db; init_db()"

download-all:
# v34 ROOT FIX (HIGH #6): the previous order ran DrugBank BEFORE OMIM,
# but master_pipeline_dag.py requires `omim >> drugbank` because
# DrugBank's `_write_structured_indications` needs the OMIM CSV. The
# Makefile and DAG disagreed on ordering. Now OMIM runs BEFORE DrugBank
# to match the DAG's dependency contract.
        $(PYTHON) -c "from pipelines.chembl_pipeline import ChEMBLPipeline; ChEMBLPipeline().run()"
        $(PYTHON) -c "from pipelines.omim_pipeline import OMIMPipeline; OMIMPipeline().run()"
        $(PYTHON) -c "from pipelines.drugbank_pipeline import DrugBankPipeline; DrugBankPipeline().run()"
        $(PYTHON) -c "from pipelines.uniprot_pipeline import UniProtPipeline; UniProtPipeline().run()"
        $(PYTHON) -c "from pipelines.string_pipeline import StringPipeline; StringPipeline().run()"
        $(PYTHON) -c "from pipelines.disgenet_pipeline import DisGeNETPipeline; DisGeNETPipeline().run()"
        $(PYTHON) -c "from pipelines.pubchem_pipeline import PubChemPipeline; PubChemPipeline().run()"

# FIX #28: Make download-parallel target call the script
download-parallel:
        $(PYTHON) scripts/download_parallel.py

clean-all:
        rm -rf raw_data/*/
        rm -rf processed_data/*.csv

load-all:
        $(PYTHON) -c "from pipelines.chembl_pipeline import ChEMBLPipeline; ChEMBLPipeline().run_load_only()"
        $(PYTHON) -c "from pipelines.drugbank_pipeline import DrugBankPipeline; DrugBankPipeline().run_load_only()"
        $(PYTHON) -c "from pipelines.uniprot_pipeline import UniProtPipeline; UniProtPipeline().run_load_only()"
        $(PYTHON) -c "from pipelines.string_pipeline import StringPipeline; StringPipeline().run_load_only()"
        $(PYTHON) -c "from pipelines.disgenet_pipeline import DisGeNETPipeline; DisGeNETPipeline().run_load_only()"
        $(PYTHON) -c "from pipelines.omim_pipeline import OMIMPipeline; OMIMPipeline().run_load_only()"
        $(PYTHON) -c "from pipelines.pubchem_pipeline import PubChemPipeline; PubChemPipeline().run_load_only()"

test:
        $(PYTHON) -m pytest tests/ -v --tb=short

# FIX AUDIT-38: Removed load-all from 'all' target — load-all is redundant
# because download-all already loads data (each pipeline runs download->clean->load).
# Use 'make load-all' explicitly if you need to re-load from existing CSVs.
all: setup download-all test

down:
        docker-compose down

reset: down
        docker-compose down -v
        rm -rf raw_data/*/
        rm -rf processed_data/*.csv

# v29 ROOT FIX (audit O-7 — Makefile run-airflow non-functional):
# The forensic audit found that the v28 ``run-airflow`` target was
# broken: it had no ``AIRFLOW_HOME``, no ``db init``, no DAG symlink,
# no PYTHONPATH. The target was a stub that crashed immediately.
# ROOT FIX: make it actually work — set AIRFLOW_HOME, init the DB,
# symlink the project DAGs into AIRFLOW_HOME/dags, set PYTHONPATH so
# the DAGs can import the project modules, then start webserver +
# scheduler.
run-airflow:
        @test -n "$(AIRFLOW_HOME)" || export AIRFLOW_HOME="$(CURDIR)/.airflow_home"; \
        mkdir -p $$AIRFLOW_HOME/dags; \
        ln -sfn $(CURDIR)/dags/master_pipeline_dag.py $$AIRFLOW_HOME/dags/master_pipeline_dag.py; \
        ln -sfn $(CURDIR)/dags/chembl_dag.py $$AIRFLOW_HOME/dags/chembl_dag.py; \
        ln -sfn $(CURDIR)/dags/drugbank_dag.py $$AIRFLOW_HOME/dags/drugbank_dag.py; \
        ln -sfn $(CURDIR)/dags/uniprot_dag.py $$AIRFLOW_HOME/dags/uniprot_dag.py; \
        ln -sfn $(CURDIR)/dags/string_dag.py $$AIRFLOW_HOME/dags/string_dag.py; \
        ln -sfn $(CURDIR)/dags/disgenet_dag.py $$AIRFLOW_HOME/dags/disgenet_dag.py; \
        ln -sfn $(CURDIR)/dags/omim_dag.py $$AIRFLOW_HOME/dags/omim_dag.py; \
        ln -sfn $(CURDIR)/dags/pubchem_dag.py $$AIRFLOW_HOME/dags/pubchem_dag.py; \
        export PYTHONPATH="$(CURDIR):$$PYTHONPATH"; \
        export DATABASE_URL="$${DATABASE_URL:-sqlite:///$$AIRFLOW_HOME/airflow.db}"; \
        echo "AIRFLOW_HOME=$$AIRFLOW_HOME"; \
        echo "PYTHONPATH=$$PYTHONPATH"; \
        echo "Initializing Airflow DB..."; \
        airflow db migrate || airflow db init; \
        (airflow users create --username admin --password admin --firstname Admin --lastname User --role Admin --email admin@example.com || true); \
        echo "Starting Airflow webserver on :8080 and scheduler..."; \
        airflow webserver -p 8080 & \
        airflow scheduler
