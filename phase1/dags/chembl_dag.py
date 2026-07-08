"""
ChEMBL DAG — standalone pipeline for ChEMBL drug and bioactivity data.

Downloads FDA-approved molecules and bioactivity data from the ChEMBL REST
API, cleans / normalises InChIKeys, deduplicates, and bulk-upserts into
the ``drugs`` and ``drug_protein_interactions`` tables.

Can be triggered independently or as part of the master pipeline.
Schedule: every Sunday at 02:00 UTC (cron ``0 2 * * 0``). ChEMBL
releases a new dump every Sunday; the standalone DAG runs weekly on
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
# ChEMBLPipeline persists its output to processed_data/ (drugs.csv,
# drug_protein_interactions.csv). Downstream DAGs (master pipeline) read those
# CSVs by path — no DataFrame is ever pushed to / pulled from XCom.

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
def run_chembl() -> None:
    """Execute the full ChEMBL pipeline: download → clean → load."""
    from pipelines.chembl_pipeline import ChEMBLPipeline
    ChEMBLPipeline().run()


@dag(
    dag_id="chembl_pipeline",
    description="ChEMBL ETL pipeline: approved drugs and bioactivity data",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # ChEMBL releases a new dump every Sunday; standalone DAG runs weekly on
    # Sunday so ad-hoc / per-source refreshes work without requiring the
    # master DAG. The master DAG remains on the same cadence and simply
    # skips sources that have already been refreshed today.
    #
    # v41 ROOT FIX (SEV1 #4 / SEV2): schedule was "0 2 * * 0" which
    # COLLIDES with the master_pipeline_dag's Sunday 02:00 UTC slot.
    # Two DAGs firing simultaneously on the same ChEMBL API would race
    # on the same downstream DB writes (duplicate-key IntegrityErrors,
    # half-committed transactions, dead-letter pollution). Move to
    # Sunday 03:30 UTC — after the master DAG's 8h window has progressed
    # past ChEMBL (master runs chembl first), so the standalone DAG
    # only fires if the master DAG didn't run (or skipped chembl).
    schedule="30 3 * * 0",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "chembl", "etl"],
)
def chembl_dag() -> None:
    """Build the ChEMBL pipeline DAG."""
    run_chembl()


chembl_dag_instance = chembl_dag()
