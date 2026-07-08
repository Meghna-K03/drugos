"""
DrugBank DAG — standalone pipeline for DrugBank XML drug and target data.

Parses the DrugBank full-database XML file (requires manual download due
to licensing).  Extracts drug metadata and target interactions, normalises
InChIKeys, deduplicates, and bulk-upserts into the ``drugs`` and
``drug_protein_interactions`` tables.

If the DrugBank XML file is not present the pipeline will raise a clear
``FileNotFoundError`` with download instructions.

Can be triggered independently or as part of the master pipeline.
Schedule: every Sunday at 03:00 UTC (cron ``0 3 * * 0``). DrugBank XML
is manually positioned; the weekly standalone run picks up any newly-
positioned XML without requiring the master DAG.
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
# DrugBankPipeline persists its output to processed_data/ (drugbank_drugs.csv).
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
def run_drugbank() -> None:
    """Execute the full DrugBank pipeline: download (verify XML) → clean → load."""
    from pipelines.drugbank_pipeline import DrugBankPipeline
    DrugBankPipeline().run()


@dag(
    dag_id="drugbank_pipeline",
    description="DrugBank ETL pipeline: drug and target data from XML",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # DrugBank XML is manually positioned; weekly standalone run on Sunday
    # picks up any newly-positioned XML without requiring the master DAG.
    # The master DAG remains the primary orchestrator.
    #
    # v41 ROOT FIX (SEV2): schedule "0 3 * * 0" OVERLAPS the master
    # DAG's 8h window (master starts 02:00 UTC, runs ~6-8h). DrugBank
    # is part of the master DAG, so a simultaneous standalone run
    # would race on the DrugBank XML parse + DB write (lxml iterparse
    # is single-threaded per file; concurrent runs would corrupt the
    # audit-log write lock). Move to Sunday 04:00 UTC — past the
    # master DAG's typical DrugBank step.
    schedule="0 4 * * 0",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "drugbank", "etl"],
)
def drugbank_dag() -> None:
    """Build the DrugBank pipeline DAG."""
    run_drugbank()


drugbank_dag_instance = drugbank_dag()
