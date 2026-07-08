"""
STRING DAG — standalone pipeline for STRING DB protein-protein interactions.

Downloads human protein links, protein info, and alias files from STRING DB,
filters by minimum combined score, maps STRING IDs to UniProt accessions,
and bulk-upserts into the ``protein_protein_interactions`` table.

Can be triggered independently or as part of the master pipeline.
Schedule: 1st of every month at 05:00 UTC
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
# StringPipeline persists its output to processed_data/
# (protein_protein_interactions.csv). Downstream DAGs (master pipeline) read
# that CSV by path — no DataFrame is ever pushed to / pulled from XCom.

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
def run_string() -> None:
    """Execute the full STRING pipeline: download → clean → load."""
    from pipelines.string_pipeline import StringPipeline
    StringPipeline().run()


@dag(
    dag_id="string_pipeline",
    description="STRING DB ETL pipeline: protein-protein interaction network",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # STRING releases a major version monthly; standalone DAG runs on the
    # 1st of every month at 05:00 UTC so ad-hoc / per-source refreshes work
    # without requiring the master DAG.
    schedule="0 5 1 * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "string", "etl"],
)
def string_dag() -> None:
    """Build the STRING pipeline DAG."""
    run_string()


string_dag_instance = string_dag()
