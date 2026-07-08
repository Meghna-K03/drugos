"""
PubChem DAG — standalone pipeline for PubChem drug enrichment.

Reads InChIKeys from the ``drugs`` table where ``pubchem_cid`` IS NULL,
batch-queries the PubChem PUG REST API for properties, and bulk-updates
the ``drugs`` table with retrieved molecular data.

Can be triggered independently or as part of the master pipeline.
Schedule: every Sunday at 08:00 UTC (cron ``0 8 * * 0``). PubChem
updates compound properties continuously; the standalone DAG runs every
Sunday so ad-hoc / per-source refreshes work without requiring the
master DAG.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from airflow.decorators import dag, task

# v29 ROOT FIX (audit O-12): XCom used for large dataframes — anti-pattern.
# Now passes file paths via XCom. The single @task below returns None and the
# PubChemPipeline persists its output to processed_data/ (pubchem_enrichment.csv).
# Downstream DAGs (master pipeline) read that CSV by path — no DataFrame is
# ever pushed to / pulled from XCom.

DEFAULT_ARGS = {
    "owner": "drug_repurposing",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(hours=4),
    "sla": timedelta(hours=4),
    "email_on_failure": False,
    "email_on_retry": False,
}


@task(retries=2, execution_timeout=timedelta(hours=4))
def run_pubchem() -> None:
    """Execute the full PubChem pipeline: download → clean → load."""
    from pipelines.pubchem_pipeline import PubChemPipeline
    PubChemPipeline().run()


@dag(
    dag_id="pubchem_pipeline",
    description="PubChem ETL pipeline: drug enrichment via PUG REST API",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # PubChem updates compound properties continuously; standalone DAG runs
    # every Sunday so ad-hoc / per-source refreshes work without requiring
    # the master DAG.
    #
    # v41 ROOT FIX (SEV2): schedule "0 8 * * 0" OVERLAPS the master
    # DAG's 8h window (master starts 02:00 UTC, runs ~6-8h; PubChem
    # step runs LAST in the master window, ~6-7h in). PubChem has a
    # strict 5 req/sec API rate limit — two concurrent DAGs would
    # split the rate budget and risk 503 throttles (PubChem is more
    # aggressive than DisGeNET about IP bans). Move to Sunday 06:00
    # UTC — past the master DAG's typical PubChem step.
    schedule="0 6 * * 0",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "pubchem", "etl"],
)
def pubchem_dag() -> None:
    """Build the PubChem pipeline DAG."""
    run_pubchem()


pubchem_dag_instance = pubchem_dag()
