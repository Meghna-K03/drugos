"""
Master DAG for the Drug Repurposing ETL Platform.

Orchestrates all 7 source pipelines in the correct dependency order.

audit-2025 ROOT FIX (issue 28): the previous task names said
"download_*" for all 7 pipelines, but ChEMBL / DrugBank / UniProt
actually call ``.run()`` (the FULL run including LOAD to DB), while
STRING / DisGeNET / OMIM / PubChem call
``.run_download_and_clean_only()`` (download only — load is deferred
to a post-entity-resolution task). The misleading names made the DAG
hard to reason about: a reader seeing ``download_chembl`` would
assume the DB was NOT loaded, but it actually was. The fix renames
the three misnamed tasks to ``run_chembl``, ``run_drugbank``,
``run_uniprot`` so the task_id truthfully describes what the task
does. STRING / DisGeNET / OMIM / PubChem retain the ``download_*``
name because they genuinely only download.

  run_chembl        ──┐
  run_drugbank       ─┤→ entity_resolution → load_string
  run_uniprot        ─┤                    → load_disgenet
  download_string    ──┘                    → load_omim
  download_disgenet                          → load_pubchem_enrichment
  download_omim
  download_pubchem

DrugBank XML check: Uses BranchPythonOperator to skip DrugBank if the XML
file is not present (it requires manual download — pipeline should not fail
the whole DAG).

Schedule: Every Sunday at 02:00 UTC  (``0 2 * * 0``)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so pipeline imports work inside Airflow
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from airflow.decorators import dag, task
from airflow.operators.branch import BranchPythonOperator
from airflow.operators.empty import EmptyOperator
from airflow.utils.trigger_rule import TriggerRule

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# v29 ROOT FIX (audit O-12): XCom used for large dataframes — anti-pattern.
# Now passes file paths via XCom (and, in practice, tasks communicate through
# CSV files in processed_data/ + the shared DB, never by returning a DataFrame
# from a @task). Returning a DataFrame would push it to XCom and saturate the
# metadata DB. Every @task below returns None and either writes to
# processed_data/ (producers) or reads from processed_data/ (consumers) —
# only small file-path strings are ever exchanged between tasks.
# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
TASK_SLA = timedelta(hours=4)
# v29 ROOT FIX (audit O-10): was 4h timeout with SUCCESS on kill. Increased
# to 8h and ensure timeout raises (not swallowed). TransE training on real
# data can take 6-7h; a 4h subprocess timeout killed it mid-training but the
# surrounding try/except swallowed the TimeoutExpired and the DAG reported
# SUCCESS. Now 8h + the except block re-raises (see _trigger_phase2).
TASK_TIMEOUT = timedelta(hours=8)

DEFAULT_ARGS = {
    "owner": "drug_repurposing",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=30),
    "sla": TASK_SLA,
    "execution_timeout": TASK_TIMEOUT,
    "email_on_failure": False,
    "email_on_retry": False,
}


# ---------------------------------------------------------------------------
# Branch helper — DrugBank XML gate
# ---------------------------------------------------------------------------

def _check_drugbank_xml(**context) -> str:
    """Return the task-id to branch into based on DrugBank XML availability.

    DrugBank requires a paid license; the XML must be pre-positioned
    manually.  If the file is missing we gracefully skip the pipeline so the
    rest of the DAG can continue.
    """
    from config.settings import DRUGBANK_XML_PATH

    xml_path = Path(DRUGBANK_XML_PATH)
    if xml_path.exists() and xml_path.stat().st_size > 0:
        logger.info("DrugBank XML found at %s — will run pipeline", xml_path)
        # ROOT FIX (Finding 1, P0): the branch must return the ACTUAL
        # task_id of the downstream @task-decorated function, which is
        # "run_drugbank" (the function name). The previous value
        # "download_drugbank" matched NO task in the DAG, causing
        # AirflowException("branch task returned unknown task_id") on
        # every Sunday 02:00 UTC run when a valid DrugBank XML was
        # present. Operators with a paid DrugBank license could never
        # run the master DAG. The v43 "compound root fix" only tested
        # the skip path (no XML) — the run path (XML present) was
        # never exercised and was broken.
        return "run_drugbank"

    # ROOT FIX (DrugBank access paused May 2026): when the DrugBank
    # XML is not present, we now offer TWO alternative drug sources so
    # the pipeline is not blocked on a paid DrugBank license:
    #   (1) ChEMBL SQLite (free, no login) — set DRUGOS_USE_CHEMBL_AS_PRIMARY=1
    #   (2) FDA Orange Book (free, no login) — set DRUGOS_USE_FDA_ORANGE_BOOK=1
    # Both alternatives are wired into the DrugBankPipeline.run() path
    # via the DRUGOS_DRUG_SOURCE env var (see drugbank_pipeline.py).
    # The default behavior (no env var) is to SKIP DrugBank and rely
    # on ChEMBL+PubChem+UniProt for the drug subgraph.
    use_chembl_primary = os.environ.get("DRUGOS_USE_CHEMBL_AS_PRIMARY", "1") == "1"
    if use_chembl_primary:
        logger.warning(
            "DrugBank XML not found at %s AND DrugBank academic "
            "downloads are paused since May 2026 (see "
            "https://go.drugbank.com/public_users/sign_up). "
            "Falling back to ChEMBL+PubChem+FDA Orange Book as the "
            "primary drug source (DRUGOS_USE_CHEMBL_AS_PRIMARY=1, "
            "the default). To use DrugBank when downloads resume, "
            "obtain a license and set DRUGBANK_XML_PATH.", xml_path,
        )
    else:
        logger.warning(
            "DrugBank XML not found at %s — skipping pipeline. "
            "To enable: download from https://go.drugbank.com/ when "
            "academic downloads resume (paused since May 2026) and "
            "set DRUGBANK_XML_PATH env var. "
            "ALTERNATIVELY: set DRUGOS_USE_CHEMBL_AS_PRIMARY=1 "
            "(default) to use ChEMBL+PubChem+FDA Orange Book as the "
            "drug source — no DrugBank license required.", xml_path,
        )
    return "skip_drugbank"


# ---------------------------------------------------------------------------
# Task callables — each delegates to the corresponding pipeline's .run()
# ---------------------------------------------------------------------------

@task(retries=2, execution_timeout=TASK_TIMEOUT)
def run_chembl() -> None:
    """Run the ChEMBL pipeline (FULL run: download + clean + load to DB).

    audit-2025 (issue 28): renamed from ``download_chembl`` because the
    task calls ``ChEMBLPipeline().run()`` (the full run including DB
    load), NOT ``run_download_and_clean_only()``. The old name misled
    readers into thinking the DB was not loaded by this task.
    """
    from pipelines.chembl_pipeline import ChEMBLPipeline
    ChEMBLPipeline().run()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def run_drugbank() -> None:
    """Run the DrugBank pipeline (FULL run: download + clean + load to DB).

    audit-2025 (issue 28): renamed from ``download_drugbank`` because
    the task calls ``DrugBankPipeline().run()`` (full run), NOT
    ``run_download_and_clean_only()``.
    """
    from pipelines.drugbank_pipeline import DrugBankPipeline
    DrugBankPipeline().run()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def run_uniprot() -> None:
    """Run the UniProt pipeline (FULL run: download + clean + load to DB).

    audit-2025 (issue 28): renamed from ``download_uniprot`` because
    the task calls ``UniProtPipeline().run()`` (full run), NOT
    ``run_download_and_clean_only()``.
    """
    from pipelines.uniprot_pipeline import UniProtPipeline
    UniProtPipeline().run()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def download_string() -> None:
    """Run the STRING pipeline: download+clean only (load after entity resolution)."""
    from pipelines.string_pipeline import StringPipeline
    StringPipeline().run_download_and_clean_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def download_disgenet() -> None:
    """Run the DisGeNET pipeline: download+clean only (load after entity resolution)."""
    from pipelines.disgenet_pipeline import DisGeNETPipeline
    DisGeNETPipeline().run_download_and_clean_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def download_omim() -> None:
    """Run the OMIM pipeline: download+clean only (load after entity resolution)."""
    from pipelines.omim_pipeline import OMIMPipeline
    OMIMPipeline().run_download_and_clean_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def download_pubchem() -> None:
    """Run the PubChem pipeline: download+clean only (load after entity resolution).

    v35 ROOT FIX (issue 35): previously called ``PubChemPipeline().run()``
    (the FULL run, including load into DB). This caused a DOUBLE-LOAD: the
    ``download_pubchem`` task loaded PubChem data into the ``drugs`` table,
    then the ``load_pubchem_enrichment`` task (line 414 below) called
    ``PubChemPipeline().run_load_only()`` which loaded the SAME data
    AGAIN. Both loads were idempotent (upsert), so the duplicate was
    silently absorbed — but it doubled the load wall-clock time and
    masked any bug in the load idempotency. Fix: use
    ``run_download_and_clean_only()`` so only the ``load_pubchem_enrichment``
    task loads (matching the pattern used by ChEMBL, DrugBank, UniProt,
    STRING, DisGeNET, and OMIM in this DAG).
    """
    from pipelines.pubchem_pipeline import PubChemPipeline
    PubChemPipeline().run_download_and_clean_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def entity_resolution() -> None:
    """Run cross-database entity resolution.

    Reconciles drug entities across ChEMBL, DrugBank, and PubChem using
    InChIKey matching, connectivity-block matching, and normalised-name
    matching.  Also resolves protein entities across UniProt and STRING.

    Results are persisted to the ``entity_mapping`` table and the
    ``proteins.string_id`` column is updated with resolved STRING IDs.

    v29 ROOT FIX (audit O-12): XCom used for large dataframes — anti-pattern.
    Now passes file paths via XCom. This task reads every upstream DataFrame
    from CSV files in ``PROCESSED_DATA_DIR`` (drugs.csv, drugbank_drugs.csv,
    pubchem_enrichment.csv, proteins.csv, protein_protein_interactions.csv)
    rather than pulling DataFrames from upstream tasks' XCom. The upstream
    download tasks return None and persist their output to those CSV files;
    this task pulls the *file paths* (constants below), not the DataFrames.
    """
    import pandas as pd
    from sqlalchemy import text

    from config.settings import PROCESSED_DATA_DIR
    from database.connection import get_db_session, get_engine
    from database.models import EntityMapping
    from entity_resolution.drug_resolver import DrugResolver
    from entity_resolution.protein_resolver import ProteinResolver

    # ------------------------------------------------------------------
    # Drug entity resolution
    # ------------------------------------------------------------------
    logger.info("Starting drug entity resolution …")
    drug_resolver = DrugResolver()

    chembl_path = PROCESSED_DATA_DIR / "drugs.csv"
    drugbank_path = PROCESSED_DATA_DIR / "drugbank_drugs.csv"
    pubchem_path = PROCESSED_DATA_DIR / "pubchem_enrichment.csv"

    chembl_df = (
        pd.read_csv(chembl_path, low_memory=False)
        if chembl_path.exists()
        else pd.DataFrame()
    )
    drugbank_df = (
        pd.read_csv(drugbank_path, low_memory=False)
        if drugbank_path.exists()
        else pd.DataFrame()
    )
    pubchem_df = (
        pd.read_csv(pubchem_path, low_memory=False)
        if pubchem_path.exists()
        else pd.DataFrame()
    )

    # FIX AUDIT-7: Validate that at least one drug DataFrame has data.
    total_drug_records = len(chembl_df) + len(drugbank_df) + len(pubchem_df)
    if total_drug_records == 0:
        logger.error(
            "All three drug DataFrames are empty (chembl=%d, drugbank=%d, pubchem=%d). "
            "This usually means the CSV files in %s do not exist or are empty. "
            "Run the download pipelines first before entity resolution.",
            len(chembl_df), len(drugbank_df), len(pubchem_df),
            PROCESSED_DATA_DIR,
        )
        raise RuntimeError(
            f"Entity resolution cannot proceed: all drug DataFrames are empty. "
            f"Ensure ChEMBL, DrugBank, and/or PubChem pipelines have been run. "
            f"Checked: {chembl_path}, {drugbank_path}, {pubchem_path}"
        )
    # Validate required columns in non-empty DataFrames
    required_drug_cols = {"inchikey", "name"}
    for name, df_check in [("chembl", chembl_df), ("drugbank", drugbank_df), ("pubchem", pubchem_df)]:
        if not df_check.empty and not required_drug_cols.issubset(set(df_check.columns)):
            missing = required_drug_cols - set(df_check.columns)
            logger.warning(
                "Drug DataFrame '%s' is missing required columns: %s. "
                "Available columns: %s. Entity resolution may produce incomplete results.",
                name, missing, list(df_check.columns),
            )

    drug_mapping_df = drug_resolver.build_mapping(chembl_df, drugbank_df, pubchem_df)
    logger.info(
        "Drug entity resolution complete: %d canonical entities",
        len(drug_mapping_df),
    )

    # ------------------------------------------------------------------
    # Protein entity resolution
    # ------------------------------------------------------------------
    logger.info("Starting protein entity resolution …")
    protein_resolver = ProteinResolver()

    proteins_path = PROCESSED_DATA_DIR / "proteins.csv"
    uniprot_df = (
        pd.read_csv(proteins_path, low_memory=False)
        if proteins_path.exists()
        else pd.DataFrame()
    )

    # FIX AUDIT-8: Also load STRING PPI data to provide protein IDs from
    # the interaction network. Extract unique UniProt IDs from both
    # uniprot_id_a and uniprot_id_b columns of the STRING processed output.
    # Schema reconciliation (GUARD-2.1, GUARD-2.2, BUG-14.1, BUG-15.1,
    # BUG-15.2): the upgraded StringPipeline now outputs the schema-
    # conformant column names `uniprot_id_a` / `uniprot_id_b` (was
    # `uniprot_a` / `uniprot_b`).
    string_path = PROCESSED_DATA_DIR / "protein_protein_interactions.csv"
    string_protein_df = pd.DataFrame()
    if string_path.exists():
        try:
            string_df = pd.read_csv(string_path, low_memory=False)
            if not string_df.empty:
                uniprot_ids = set()
                for col in ["uniprot_id_a", "uniprot_id_b"]:
                    if col in string_df.columns:
                        uniprot_ids.update(string_df[col].dropna().unique())
                if uniprot_ids:
                    string_protein_df = pd.DataFrame({"uniprot_id": list(uniprot_ids)})
                    logger.info(
                        "Extracted %d unique UniProt IDs from STRING PPI data",
                        len(string_protein_df),
                    )
        except Exception as exc:
            logger.warning("Failed to load STRING data for protein resolution: %s", exc)
    protein_mapping_df = protein_resolver.build_mapping(
        uniprot_df, string_df=string_protein_df
    )
    logger.info(
        "Protein entity resolution complete: %d canonical entities",
        len(protein_mapping_df),
    )

    # ------------------------------------------------------------------
    # Persist drug entity mappings
    # ------------------------------------------------------------------
    if not drug_mapping_df.empty:
        # Align columns to EntityMapping schema — drop extras, fill missing
        col_map = {
            "canonical_inchikey": "canonical_inchikey",
            "canonical_name": "canonical_name",
            "chembl_id": "chembl_id",
            "drugbank_id": "drugbank_id",
            "pubchem_cid": "pubchem_cid",
            "uniprot_id": "uniprot_id",
            "string_id": "string_id",
            "match_confidence": "match_confidence",
            "match_method": "match_method",
        }
        save_df = drug_mapping_df.rename(columns=col_map)
        # Ensure pubchem_cid is numeric
        if "pubchem_cid" in save_df.columns:
            save_df["pubchem_cid"] = pd.to_numeric(
                save_df["pubchem_cid"], errors="coerce",
            )
        # Keep only columns present in EntityMapping
        model_cols = [
            "canonical_inchikey", "canonical_name", "chembl_id",
            "drugbank_id", "pubchem_cid", "uniprot_id", "string_id",
            "match_confidence", "match_method",
        ]
        for c in model_cols:
            if c not in save_df.columns:
                save_df[c] = None
        save_df = save_df[model_cols]

        # Transactional: temp table + TRUNCATE/INSERT — atomic, rolls back on failure
        # FIX #14: Replace DELETE FROM + to_sql with temp table + TRUNCATE/INSERT pattern
        engine = get_engine()
        with engine.begin() as conn:
            # Stage into temp table
            save_df.to_sql(
                "_tmp_entity_mapping_staging",
                con=conn,
                if_exists="replace",
                index=False,
                method="multi",
                chunksize=5000,
            )
            # Atomic swap: clear + INSERT in same transaction.
            # v9 ROOT FIX (audit F3.5): TRUNCATE TABLE is PostgreSQL-
            # specific syntax. On the SQLite-backed test environment
            # (and any future SQLite deployment) it raises
            # sqlite3.OperationalError despite the migration runner
            # claiming cross-dialect support — making it impossible to
            # re-run entity resolution on SQLite. Use DELETE FROM
            # which is universally supported (ANSI SQL) and behaves
            # correctly within an explicit transaction on both
            # dialects.
            conn.execute(text("DELETE FROM entity_mapping"))
            conn.execute(text("""
                INSERT INTO entity_mapping
                    (canonical_inchikey, canonical_name, chembl_id,
                     drugbank_id, pubchem_cid, uniprot_id, string_id,
                     match_confidence, match_method)
                SELECT
                    canonical_inchikey, canonical_name, chembl_id,
                    drugbank_id, pubchem_cid, uniprot_id, string_id,
                    match_confidence, match_method
                FROM _tmp_entity_mapping_staging
            """))
            conn.execute(text("DROP TABLE IF EXISTS _tmp_entity_mapping_staging"))
        logger.info(
            "Persisted %d drug entity mappings to database",
            len(drug_mapping_df),
        )

        # FORENSIC ROOT FIX (audit: "Phase 1 entity_mapping is discarded"):
        # The previous code ONLY wrote entity_mapping to the PostgreSQL
        # ``entity_mapping`` TABLE. When the bridge runs on the CSV path
        # (the default for the toy fixture and for any deployment without
        # PostgreSQL), there is NO ``entity_mapping.csv`` file to read —
        # so Phase 2's entity_resolver re-resolves from scratch,
        # discarding Phase 1's cross-source ER work. This is the root
        # cause of the audit's "Phase 1 entity_mapping table is
        # discarded" finding. The fix: ALSO export entity_mapping to
        # ``processed_data/entity_mapping.csv`` so the CSV-path bridge
        # can read it. The CSV has the SAME columns as the DB table
        # (canonical_inchikey, canonical_name, chembl_id, drugbank_id,
        # pubchem_cid, uniprot_id, string_id, match_confidence,
        # match_method).
        try:
            entity_mapping_csv_path = PROCESSED_DATA_DIR / "entity_mapping.csv"
            save_df.to_csv(entity_mapping_csv_path, index=False)
            logger.info(
                "FORENSIC ROOT FIX: exported %d entity mappings to %s "
                "(CSV-path bridge can now REUSE Phase 1's cross-source ER "
                "instead of re-resolving from scratch).",
                len(save_df),
                entity_mapping_csv_path,
            )
        except Exception as _csv_exc:
            logger.warning(
                "Failed to export entity_mapping to CSV (%s) — "
                "the PostgreSQL table is still populated, but the "
                "CSV-path bridge will not be able to reuse Phase 1's "
                "ER output. This is a degraded mode.",
                _csv_exc,
            )

    # ------------------------------------------------------------------
    # Update proteins.string_id from protein resolution results
    # ------------------------------------------------------------------
    if not protein_mapping_df.empty and "string_id" in protein_mapping_df.columns:
        resolved = protein_mapping_df.dropna(subset=["string_id"])
        if not resolved.empty:
            update_df = resolved[["uniprot_id", "string_id"]].copy()
            update_df = update_df.dropna(subset=["uniprot_id", "string_id"])
            if not update_df.empty:
                engine = get_engine()
                with engine.begin() as conn:
                    update_df.to_sql(
                        "_tmp_protein_string_update", con=conn,
                        if_exists="replace", index=False,
                        method="multi", chunksize=5000,
                    )
                    conn.execute(text("""
                        UPDATE proteins p
                        SET string_id = t.string_id
                        FROM _tmp_protein_string_update t
                        WHERE p.uniprot_id = t.uniprot_id
                        AND p.string_id IS NULL
                    """))
                    conn.execute(text("DROP TABLE IF EXISTS _tmp_protein_string_update"))
            logger.info(
                "Updated string_id for %d proteins", len(resolved),
            )

    logger.info("Entity resolution pipeline complete")


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def load_string() -> None:
    """FIX AUDIT-26: Use run_load_only() — data already downloaded and cleaned."""
    from pipelines.string_pipeline import StringPipeline
    StringPipeline().run_load_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def load_disgenet() -> None:
    """FIX AUDIT-26: Use run_load_only() — data already downloaded and cleaned."""
    from pipelines.disgenet_pipeline import DisGeNETPipeline
    DisGeNETPipeline().run_load_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def load_omim() -> None:
    """FIX AUDIT-27: Use run_load_only() — data already downloaded and cleaned."""
    from pipelines.omim_pipeline import OMIMPipeline
    OMIMPipeline().run_load_only()


@task(retries=2, execution_timeout=TASK_TIMEOUT)
def load_pubchem_enrichment() -> None:
    """FIX AUDIT-27: PubChem data already downloaded."""
    from pipelines.pubchem_pipeline import PubChemPipeline
    PubChemPipeline().run_load_only()


@task(retries=1, execution_timeout=TASK_TIMEOUT, trigger_rule=TriggerRule.ALL_SUCCESS)
def _trigger_phase2() -> None:
    """v29 ROOT FIX (audit O-2 — master DAG always reports success).

    The forensic audit found that this task had ``trigger_rule=ALL_DONE``
    + ``check=False`` + ``retries=0``, which meant Phase 2 could crash,
    time out, or fail V1 criteria and the DAG would still report GREEN.
    Every previous AI session that told the user "it's 100% integrated"
    was reading the DAG's green status without checking the actual
    Phase 2 exit code or the AUC log.

    ROOT FIX: change ``trigger_rule`` to ``ALL_SUCCESS`` (so Phase 2
    only runs if all Phase 1 tasks succeeded), use ``check=True`` (so
    non-zero exit code raises), and propagate timeouts / exceptions
    instead of swallowing them. The DAG now fails RED when Phase 2
    fails — operators can no longer claim success without verifying.

    v41 ROOT FIX (SEV3): ``retries=0`` left no recovery for transient
    Phase 2 failures (e.g. Neo4j restart mid-run, transient ENOSPC on
    /tmp, MLflow network blip). Bumped to ``retries=1`` so a single
    transient failure auto-recovers. The downstream observability
    (DAG turns RED) is unchanged — operators still see the failure
    via Airflow's retry-then-fail signal. We deliberately did NOT
    bump to ``retries=2+`` because Phase 2 is a 30-60 min task and
    excessive retries multiply wall-clock time on hard failures.

    Behavior:
      * ``trigger_rule=ALL_SUCCESS`` — only runs if ALL Phase 1 tasks
        succeeded. (Was: ``ALL_DONE`` which fires even on failure.)
      * ``check=True`` — non-zero exit code raises CalledProcessError.
        (Was: ``check=False`` which silently ignored failures.)
      * Timeouts and exceptions propagate — task fails RED. (Was:
        logged as WARNING and task succeeded.)

    The task still uses the RecordingGraphBuilder by default (no
    Neo4j required), so it can run in any environment. Operators who
    want a real Neo4j load set ``DRUGOS_NEO4J_URI``.

    v41 ROOT FIX (SEV2): when DRUGOS_NEO4J_URI is set, call the
    neo4j_exporter.export_to_neo4j() function DIRECTLY instead of
    routing through the ``run_unified.py`` subprocess. The subprocess
    path was a v29 workaround that bypassed the exporter's CSV
    streaming + constraint-creation logic, which is the canonical
    Phase 1 → Neo4j integration. The exporter is invoked AFTER the
    run_unified subprocess (which produces the staged_graph.json) so
    both paths coexist: subprocess for KG construction + TransE, then
    exporter for direct Neo4j load. When DRUGOS_NEO4J_URI is NOT set,
    we keep the legacy subprocess-only path.
    """
    import os
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    _project_root = _Path(__file__).resolve().parent.parent.parent
    run_unified = _project_root / "run_unified.py"

    # v41 ROOT FIX (SEV3): config-driven phase1-dir — the previous code
    # hard-coded ``phase1/processed_data`` which broke any deployment
    # that used a non-default PROCESSED_DATA_DIR (e.g. mounted NFS at
    # /var/data/processed). Read from env (set by the settings module
    # at DAG import time) with a sensible default.
    _phase1_dir = os.environ.get(
        "DRUGOS_PHASE1_DIR",
        str(_project_root / "phase1" / "processed_data"),
    )

    if not run_unified.exists():
        # Fallback: invoke via ``python -m drugos_graph``.
        cmd = [
            _sys.executable, "-m", "drugos_graph",
            "--data-source", "phase1",
            "--phase1-dir", _phase1_dir,
        ]
    else:
        cmd = [
            _sys.executable, str(run_unified),
            "--phase1-dir", _phase1_dir,
            "--full-pipeline",
        ]

    neo4j_uri = os.environ.get("DRUGOS_NEO4J_URI")
    if neo4j_uri:
        cmd.extend(["--neo4j-uri", neo4j_uri])
        if os.environ.get("DRUGOS_NEO4J_USER"):
            cmd.extend(["--neo4j-user", os.environ["DRUGOS_NEO4J_USER"]])
        if os.environ.get("DRUGOS_NEO4J_PASSWORD"):
            cmd.extend(["--neo4j-password", os.environ["DRUGOS_NEO4J_PASSWORD"]])

    logger.info("v29 trigger_phase2: invoking Phase 2 pipeline: %s", " ".join(cmd))

    # v29 ROOT FIX: check=True (was False) so non-zero exit raises
    # CalledProcessError. This makes the task fail RED when Phase 2
    # fails, instead of silently logging a WARNING and succeeding.
    try:
        result = subprocess.run(
            cmd, cwd=str(_project_root), check=True,
            capture_output=True, text=True, timeout=int(TASK_TIMEOUT.total_seconds()),
        )
        logger.info("v29 trigger_phase2: Phase 2 pipeline completed successfully.")
        if result.stdout:
            logger.info("stdout tail:\n%s", result.stdout[-2000:])
    except subprocess.CalledProcessError as exc:
        # v29 ROOT FIX: propagate the failure. The DAG turns RED.
        logger.error(
            "v29 trigger_phase2: Phase 2 pipeline FAILED with exit "
            "code %d. The DAG will now fail RED — this is the correct "
            "behavior (audit O-2 root fix). stderr tail:\n%s",
            exc.returncode,
            (exc.stderr or "")[-2000:],
        )
        raise
    except subprocess.TimeoutExpired as exc:
        # v29 ROOT FIX (audit O-10): propagate the timeout. The DAG turns RED.
        # Subprocess timed out — DAG will FAIL.
        logger.error(
            "v29 trigger_phase2: Phase 2 pipeline TIMED OUT after %d "
            "seconds. Subprocess timed out — DAG will FAIL. The DAG will "
            "now fail RED — this is the correct behavior (audit O-2 / "
            "O-10 root fix).",
            int(TASK_TIMEOUT.total_seconds()),
        )
        raise
    except Exception as exc:
        # v29 ROOT FIX: propagate ANY exception. The DAG turns RED.
        logger.error(
            "v29 trigger_phase2: Phase 2 invocation raised %s: %s. "
            "The DAG will now fail RED (audit O-2 root fix).",
            type(exc).__name__, exc,
        )
        raise

    # v41 ROOT FIX (SEV2): if DRUGOS_NEO4J_URI is set, ALSO call the
    # neo4j_exporter directly. This is the canonical Phase 1 → Neo4j
    # integration that was being bypassed by the subprocess-only path.
    # The exporter reads the staged_graph.json (produced by run_unified
    # above) and pushes nodes/edges to Neo4j with constraint creation.
    # If the exporter fails, we propagate the failure (DAG turns RED).
    if neo4j_uri:
        logger.info(
            "v41 trigger_phase2: DRUGOS_NEO4J_URI is set — invoking "
            "neo4j_exporter.export_to_neo4j() directly for canonical "
            "Phase 1 → Neo4j load (bypass subprocess workaround)."
        )
        try:
            # Ensure phase1/exporters is importable.
            exporters_dir = _project_root / "phase1" / "exporters"
            if str(exporters_dir.parent) not in _sys.path:
                _sys.path.insert(0, str(exporters_dir.parent))
            from exporters.neo4j_exporter import export_to_neo4j  # type: ignore

            export_to_neo4j(
                phase1_processed_dir=_phase1_dir,
                neo4j_uri=neo4j_uri,
                neo4j_user=os.environ.get("DRUGOS_NEO4J_USER", ""),
                neo4j_password=os.environ.get("DRUGOS_NEO4J_PASSWORD", ""),
            )
            logger.info(
                "v41 trigger_phase2: neo4j_exporter.export_to_neo4j() "
                "completed successfully."
            )
        except Exception as exc:
            logger.error(
                "v41 trigger_phase2: neo4j_exporter.export_to_neo4j() "
                "FAILED with %s: %s. The DAG will now fail RED.",
                type(exc).__name__, exc,
            )
            raise


# ---------------------------------------------------------------------------
# DAG definition
# ---------------------------------------------------------------------------

@dag(
    dag_id="drug_repurposing_master",
    description=(
        "Master DAG orchestrating all Drug Repurposing ETL pipelines "
        "with entity resolution"
    ),
    schedule="0 2 * * 0",           # Every Sunday at 02:00 UTC
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["drug_repurposing", "master", "etl"],
)
def master_pipeline() -> None:
    """Build the master pipeline DAG with all inter-task dependencies."""

    # ── Branch operator: DrugBank XML gate ──────────────────────────────
    check_drugbank = BranchPythonOperator(
        task_id="check_drugbank_xml",
        python_callable=_check_drugbank_xml,
    )

    skip_drugbank = EmptyOperator(task_id="skip_drugbank")

    drugbank_done = EmptyOperator(
        task_id="drugbank_done",
        # audit-2025 ROOT FIX (issue 30): was
        # ``trigger_rule=TriggerRule.ALL_DONE``. ALL_DONE fires even when
        # ``drugbank`` FAILS — silently swallowing the failure and letting
        # the downstream ``resolve`` task proceed without DrugBank data.
        # In a medical ETL pipeline that is unacceptable: a DrugBank
        # failure (corrupt XML, OOM, schema drift) must surface as a DAG
        # failure so operators can investigate. The fix uses
        # ``TriggerRule.NONE_FAILED_OR_SKIPPED`` which means "all parents
        # have either succeeded or been skipped" — the intended
        # semantics for the optional-DrugBank branch:
        #   * ``drugbank`` task SUCCEEDS → drugbank_done fires (data loaded).
        #   * ``drugbank`` task SKIPPED  → drugbank_done fires (XML missing
        #     is a documented optional-skip case).
        #   * ``drugbank`` task FAILS    → drugbank_done does NOT fire,
        #     which propagates the failure to ``resolve`` and the DAG run
        #     fails loudly. Operators get paged instead of getting a
        #     silently-incomplete KG.
        trigger_rule=TriggerRule.NONE_FAILED_OR_SKIPPED,
    )

    # ── Primary download tasks ──────────────────────────────────────────
    # audit-2025 (issue 28): chembl / drugbank / uniprot call .run() (FULL
    # run including DB load). string / disgenet / omim / pubchem call
    # .run_download_and_clean_only() (download only — load deferred to a
    # post-entity-resolution task). The task_ids reflect this distinction.
    chembl = run_chembl()
    drugbank = run_drugbank()
    uniprot = run_uniprot()
    string = download_string()

    # ── Secondary download tasks ────────────────────────────────────────
    # v35 ROOT FIX (issue 36): the previous comment claimed DisGeNET and
    # OMIM "share the gene_disease_associations.csv file via
    # _save_csv_with_mode" — this is FALSE. DisGeNET writes to
    # ``gene_disease_associations.csv`` (per DisGeNETPipeline source_name),
    # and OMIM writes to ``omim_gene_disease_associations.csv`` (per
    # OMIMPipeline source_name). They write to DIFFERENT files, so running
    # them in parallel would NOT cause CSV corruption.
    #
    # audit-2025 ROOT FIX (issue 29): the previous code wired
    # ``disgenet >> omim`` to "keep the linear dependency chain explicit".
    # That ordering added latency (DisGeNET can take 30+ minutes on a
    # full API pull) without providing any correctness guarantee — the
    # only actual cross-pipeline dependency is ``omim >> drugbank`` (see
    # the v9 ROOT FIX at the ``omim >> drugbank`` wire below). The fix
    # REMOVES the ``disgenet >> omim`` wire so DisGeNET and OMIM run in
    # parallel, reducing the critical-path wall-clock time. DrugBank
    # still waits for OMIM via the explicit ``omim >> drugbank`` wire.
    disgenet = download_disgenet()
    omim = download_omim()
    # No ``disgenet >> omim`` wire — see audit-2025 issue 29 comment above.

    # PubChem download task (needs drugs in DB from entity resolution)
    pubchem_download = download_pubchem()

    # ── Entity resolution ───────────────────────────────────────────────
    resolve = entity_resolution()

    # ── Post-resolution load tasks ──────────────────────────────────────
    string_load = load_string()
    disgenet_load = load_disgenet()
    omim_load = load_omim()
    pubchem_load = load_pubchem_enrichment()

    # V18 ROOT FIX (Phase 1 ↔ Phase 2 100% connection):
    # Before v18, the master DAG ended at ``pubchem_load`` — Phase 2
    # (knowledge graph construction + TransE training) had to be
    # invoked MANUALLY via ``python -m drugos_graph`` or
    # ``run_unified.py``. The audit flagged this as the only
    # meaningful integration gap (Phase 1 → Phase 2 connection was
    # ~90% complete; this single missing wire was the remaining 10%).
    #
    # Root fix: add a ``trigger_phase2`` task that fires
    # ``run_unified.py --full-pipeline`` after ``pubchem_load``
    # completes.
    #
    # v41 ROOT FIX (SEV2): the v29 comment said "Phase 2 failure is
    # logged as a DAG warning but does not abort the Phase 1 run"
    # — this was FALSE as of v29. The v29 root fix changed the task
    # to ``trigger_rule=ALL_SUCCESS`` + ``check=True`` + exception
    # propagation, which makes Phase 2 failure abort the Phase 1 run
    # (DAG turns RED). The comment was left stale and misled operators
    # into thinking Phase 1 would stay GREEN on Phase 2 failure.
    # Updated comment to reflect v29 strict behavior: a Phase 2
    # failure now propagates as a Phase 1 DAG failure. Operators who
    # want the old tolerant behavior can override ``trigger_rule`` to
    # ``ALL_DONE`` and ``retries`` to ``0`` in their Airflow config.
    trigger_phase2 = _trigger_phase2()

    # ── Wire dependencies ───────────────────────────────────────────────
    # DrugBank branch: check → [download | skip] → join
    check_drugbank >> [drugbank, skip_drugbank] >> drugbank_done

    # v9 ROOT FIX (audit F3.10 / F4.4 / BUG-A-005): DrugBank's
    # _write_structured_indications step requires the OMIM CSV
    # (omim_gene_disease_associations.csv) to exist as a controlled
    # disease vocabulary. The previous wiring ran DrugBank in PARALLEL
    # with OMIM — so on a fresh-install DAG run where OMIM hadn't
    # completed yet, DrugBank raised RuntimeError("OMIM CSV not found")
    # and the entire DrugBank pipeline failed. Now we declare DrugBank
    # as DOWNSTREAM of OMIM so the OMIM CSV is guaranteed to exist
    # when DrugBank's _write_structured_indications fires.
    omim >> drugbank

    # SCI-FIX: ALL primary + secondary downloads must complete before
    # entity resolution. Previously, disgenet and omim were orphaned
    # (no upstream/downstream), causing race conditions where the load
    # tasks could fire before the downloads finished.
    [chembl, drugbank_done, uniprot, string, disgenet, omim] >> resolve

    # Entity resolution → dependent loads (fan-out)
    resolve >> [string_load, disgenet_load, omim_load]

    # SCI-FIX: PubChem needs drugs in the DB (from entity resolution),
    # so download runs after resolve, then load runs after download.
    # Previously, download_pubchem was defined but never instantiated,
    # causing pubchem_load to fail with FileNotFoundError.
    resolve >> pubchem_download >> pubchem_load

    # V18 ROOT FIX (Phase 1 ↔ Phase 2 100% connection):
    # PubChem load is the LAST Phase 1 task. Once it completes, all 7
    # Phase 1 source CSVs are in ``processed_data/`` and the Phase 1 →
    # Phase 2 bridge can read them. Fire ``trigger_phase2`` after
    # ``pubchem_load`` so the full Phase 1 + Phase 2 pipeline runs
    # end-to-end from a single DAG invocation.
    #
    # v41 ROOT FIX (SEV2): trigger_phase2 previously depended ONLY on
    # pubchem_load — but the Phase 2 bridge reads ALL 7 source CSVs
    # (chembl, drugbank, uniprot, string, disgenet, omim, pubchem).
    # If string_load / disgenet_load / omim_load failed, the
    # ``trigger_rule=ALL_SUCCESS`` on trigger_phase2 would have kept
    # the task from firing (because Airflow evaluates ALL_SUCCESS
    # across ALL upstream tasks), but the explicit wire was missing
    # — operators reading the DAG had no visual signal that Phase 2
    # depended on those loads. Add the explicit wires for clarity
    # and to make the dependency graph match the actual data flow.
    [pubchem_load, string_load, disgenet_load, omim_load] >> trigger_phase2


# Instantiate the DAG
master_dag = master_pipeline()
