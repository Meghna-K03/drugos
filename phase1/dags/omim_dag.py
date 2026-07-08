"""
OMIM DAG — standalone pipeline for OMIM gene-phenotype mappings.

Downloads morbidmap.txt (if OMIM_API_KEY is set) or uses the OMIM API
with pagination, parses confirmed and probabilistic gene-phenotype
associations (``mapping_key ∈ OMIM_MAPPING_KEYS_INCLUDE``, default
``[3, 4]`` — i.e. 3 = "confirmed" and 4 = "probable" per OMIM
#173110), and bulk-upserts into the ``gene_disease_associations``
table.

audit-2025 ROOT FIX (issue 27): the previous docstring said
``mapping_key == 3`` but ``config.settings.OMIM_MAPPING_KEYS_INCLUDE``
defaults to ``[3, 4]``. The docstring was stale — it described only
the "confirmed" mapping_key (3) and omitted the "probable" one (4)
that the pipeline also ingests by default. The fix updates the
docstring to match the actual default include-list and explains the
two mapping_key values.

Can be triggered independently or as part of the master pipeline.
Schedule: 1st of every month at 07:00 UTC
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
# OMIMPipeline persists its output to processed_data/
# (omim_gene_disease_associations.csv). Downstream DAGs (master pipeline) read
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
def run_omim() -> None:
    """Execute the full OMIM pipeline: download → clean → load."""
    from pipelines.omim_pipeline import OMIMPipeline
    OMIMPipeline().run()


@dag(
    dag_id="omim_pipeline",
    description="OMIM ETL pipeline: gene-phenotype mappings",
    # v29 ROOT FIX (audit O-11): was schedule=None (dead). Now scheduled.
    # OMIM releases new morbidmap entries monthly; standalone DAG runs on
    # the 1st of every month at 07:00 UTC so ad-hoc / per-source refreshes
    # work without requiring the master DAG.
    schedule="0 7 1 * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "omim", "etl"],
)
def omim_dag() -> None:
    """Build the OMIM pipeline DAG."""
    run_omim()


omim_dag_instance = omim_dag()
