"""
DisGeNET DAG — standalone pipeline for gene-disease associations.

Downloads the full gene-disease association dataset from DisGeNET,
filters by minimum score, normalises confidence tiers, and bulk-upserts
into the ``gene_disease_associations`` table.

If ``DISGENET_API_KEY`` is set the Bearer auth header is used for the
download; otherwise the public endpoint is attempted.

Can be triggered independently or as part of the master pipeline.
Schedule: every Sunday at 06:00 UTC (cron ``0 6 * * 0``). DisGeNET
curates weekly; the standalone DAG runs every Sunday so per-source
refreshes work without requiring the master DAG.
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
# DisGeNETPipeline persists its output to processed_data/
# (gene_disease_associations.csv). Downstream DAGs (master pipeline) read that
# CSV by path — no DataFrame is ever pushed to / pulled from XCom.

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
def run_disgenet() -> None:
    """Execute the full DisGeNET pipeline: download → clean → load."""
    from pipelines.disgenet_pipeline import DisGeNETPipeline
    DisGeNETPipeline().run()


@dag(
    dag_id="disgenet_pipeline",
    description="DisGeNET ETL pipeline: gene-disease associations",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # DisGeNET curates weekly; standalone DAG runs every Sunday so
    # ad-hoc / per-source refreshes work without requiring the master DAG.
    #
    # v41 ROOT FIX (SEV2): schedule "0 6 * * 0" OVERLAPS the master
    # DAG's 8h window (master starts 02:00 UTC, runs ~6-8h; DisGeNET
    # step runs ~2-3h into the master window). DisGeNET has a strict
    # 4 req/sec API rate limit — two concurrent DAGs would split the
    # rate budget, slow both runs, and risk 429s. Move to Sunday
    # 05:00 UTC — past the master DAG's typical DisGeNET step.
    schedule="0 5 * * 0",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "disgenet", "etl"],
)
def disgenet_dag() -> None:
    """Build the DisGeNET pipeline DAG."""
    run_disgenet()


disgenet_dag_instance = disgenet_dag()
