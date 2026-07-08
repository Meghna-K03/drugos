"""
UniProt DAG — standalone pipeline for human reviewed (Swiss-Prot) protein data.

Downloads human reviewed proteins from the UniProt REST API using
cursor-based pagination, cleans and normalises records, and bulk-upserts
into the ``proteins`` table.

Can be triggered independently or as part of the master pipeline.
Schedule: 1st of every month at 04:00 UTC (cron ``0 4 1 * *``). UniProt's
Swiss-Prot human reviewed set updates monthly; the standalone DAG runs
on the 1st of every month so ad-hoc / per-source refreshes work without
requiring the master DAG.
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
# UniProtPipeline persists its output to processed_data/ (proteins.csv).
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
def run_uniprot() -> None:
    """Execute the full UniProt pipeline: download → clean → load."""
    from pipelines.uniprot_pipeline import UniProtPipeline
    UniProtPipeline().run()


@dag(
    dag_id="uniprot_pipeline",
    description="UniProt ETL pipeline: human reviewed protein data",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # UniProt's Swiss-Prot human reviewed set updates monthly; standalone DAG
    # runs on the 1st of every month at 04:00 UTC so ad-hoc / per-source
    # refreshes work without requiring the master DAG.
    schedule="0 4 1 * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "uniprot", "etl"],
)
def uniprot_dag() -> None:
    """Build the UniProt pipeline DAG."""
    run_uniprot()


uniprot_dag_instance = uniprot_dag()
