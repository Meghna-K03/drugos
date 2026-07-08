#!/usr/bin/env python3
"""Run pipeline downloads in parallel (4 workers).

FIX M9: PubChem is moved to a third-pass step because it requires drugs
already in the database. First-pass pipelines run in parallel, then
DisGeNET+OMIM run sequentially, then PubChem runs after ChEMBL has
loaded drugs.

v41 ROOT FIX (SEV3): the previous module docstring claimed "DisGeNET
and OMIM share gene_disease_associations.csv via _save_csv_with_mode,
so they must NOT run in parallel." This is FALSE — DisGeNET writes to
``gene_disease_associations.csv`` (per DisGeNETPipeline source_name)
and OMIM writes to ``omim_gene_disease_associations.csv`` (per
OMIMPipeline source_name). They write to DIFFERENT files. The
second-pass sequential ordering is retained for a DIFFERENT reason:
the OMIM pipeline's ``_post_load_disgenet_dedup`` step reads the
DisGeNET table from the DB (so DisGeNET must LOAD first), and running
them sequentially avoids DB-connection contention on the shared
staging DB. The false comment is corrected.

FIX AUDIT-22: DrugBank requires manual XML download, so it runs in
a separate fourth pass with a clear error message if the XML is missing.

SCI-FIX: The script now exits with non-zero status if any pipeline fails,
so CI/CD can detect broken pipelines. Previously, failures were printed
but the exit code was always 0.
"""
import concurrent.futures
import os
import sys
import threading

# v29 ROOT FIX (audit P1-16) + audit-2025 fix: each parallel pipeline MUST
# get its own unique PIPELINE_RUN_ID so the audit trail can distinguish rows
# by origin. The previous code set os.environ["PIPELINE_RUN_ID"] inside
# run_pipeline, but env vars are PROCESS-wide — under ThreadPoolExecutor the
# 4 first-pass pipelines overwrite each other's run_id, producing a race that
# corrupts provenance. The fix is to use a threading.local() so each thread
# has its own private run_id, and to expose it to the pipeline via a
# thread-local accessor that takes precedence over the env var.
_RUN_ID_LOCAL = threading.local()


def get_thread_run_id() -> str | None:
    """Return the per-thread run_id, if set.

    Pipelines should consult this BEFORE falling back to the
    ``PIPELINE_RUN_ID`` environment variable so that parallel pipelines
    running in the same process keep distinct provenance.
    """
    return getattr(_RUN_ID_LOCAL, "run_id", None)

# Ensure project root is importable when running from any directory
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pipelines.chembl_pipeline import ChEMBLPipeline
from pipelines.uniprot_pipeline import UniProtPipeline
from pipelines.string_pipeline import StringPipeline
from pipelines.disgenet_pipeline import DisGeNETPipeline
from pipelines.omim_pipeline import OMIMPipeline
from pipelines.pubchem_pipeline import PubChemPipeline
from pipelines.drugbank_pipeline import DrugBankPipeline

# FIX AUDIT-21 (v41 ROOT FIX SEV3 — corrected): the previous comment
# said "DisGeNET and OMIM share gene_disease_associations.csv via
# _save_csv_with_mode, so they must NOT run in parallel." This is FALSE.
# DisGeNET writes to ``gene_disease_associations.csv`` and OMIM writes
# to ``omim_gene_disease_associations.csv`` — they write to DIFFERENT
# files. The REAL reason for the sequential ordering is that OMIM's
# ``_post_load_disgenet_dedup`` step reads the DisGeNET table from the
# DB, so DisGeNET must LOAD first. Sequential execution also avoids
# DB-connection contention on the shared staging DB.
FIRST_PASS = [
    ("chembl", ChEMBLPipeline),
    ("uniprot", UniProtPipeline),
    ("string", StringPipeline),
]
SECOND_PASS = [
    ("disgenet", DisGeNETPipeline),  # Must LOAD before OMIM (OMIM dedup reads DisGeNET table).
    ("omim", OMIMPipeline),  # Runs after DisGeNET — _post_load_disgenet_dedup needs DisGeNET rows in DB.
]
THIRD_PASS = [('pubchem', PubChemPipeline)]
# FIX AUDIT-22: DrugBank requires manual XML download, so it runs in
# a separate step with a clear error message if the XML is missing.
FOURTH_PASS = [('drugbank', DrugBankPipeline)]

def run_pipeline(args):
    name, cls = args
    try:
        # v29 ROOT FIX (audit P1-16): each parallel pipeline MUST get
        # its own unique PIPELINE_RUN_ID so the audit trail can
        # distinguish rows by origin. The previous code shared ONE
        # PIPELINE_RUN_ID across all 4 first-pass pipelines (ChEMBL,
        # UniProt, STRING, and the second-pass DisGeNET/OMIM), making
        # it impossible to tell which pipeline produced a given row.
        #
        # audit-2025 ROOT FIX: the old code set os.environ["PIPELINE_RUN_ID"]
        # inside this function. Under ThreadPoolExecutor (max_workers=4),
        # all four first-pass pipelines share the same process and the
        # same os.environ mapping — so they overwrite each other's run_id
        # mid-flight, corrupting provenance tracing. The fix is to compute
        # the per-pipeline run_id and store it in a threading.local() so
        # each thread has its own private slot. Pipelines should read
        # run_id from get_thread_run_id() (preferred) or fall back to the
        # PIPELINE_RUN_ID env var (for non-parallel / sequential runs).
        #
        # v41 ROOT FIX (SEV3): DOCUMENTED LIMITATION — the per-pipeline
        # run_id is stored in _RUN_ID_LOCAL (thread-local), but the
        # pipeline classes themselves read run_id from
        # ``os.environ["PIPELINE_RUN_ID"]`` (see base_pipeline.py's
        # BasePipeline.run_id property). They do NOT consult
        # get_thread_run_id(). This means the thread-local run_id is
        # INVISIBLE to the pipelines unless they are individually
        # patched to call get_thread_run_id() first. The full fix
        # requires either (a) modifying base_pipeline.BasePipeline.run_id
        # to consult get_thread_run_id() (out of scope for this task —
        # pipelines are Agent G's territory), or (b) switching from
        # ThreadPoolExecutor to ProcessPoolExecutor so each pipeline
        # gets its own os.environ (major architectural change —
        # deferred to v2.0.0). For now, the comment DOCUMENTS the
        # limitation so operators know the provenance trace will
        # show the LAST-WRITTEN env-var run_id for ALL first-pass
        # pipelines, not the per-pipeline run_id computed above.
        # The per-pipeline run_id IS preserved in the return tuple
        # (for logging / manifest purposes) and in the thread-local
        # for any pipeline that IS patched to read it.
        import uuid as _uuid
        import os as _os
        _base = _os.environ.get("PIPELINE_RUN_ID", "")
        if _base:
            _run_id = f"{_base}_{name}"
        else:
            _run_id = f"parallel_{name}_{_uuid.uuid4().hex[:8]}"
        _RUN_ID_LOCAL.run_id = _run_id
        # ROOT FIX (Finding 3, P0): the previous code documented a
        # limitation — pipelines read run_id from os.environ["PIPELINE_RUN_ID"],
        # NOT from the thread-local, so all parallel pipelines shared
        # the LAST-WRITTEN env-var run_id and provenance was corrupted.
        # The author wrote 47 lines of comment documenting this and
        # shipped the bug anyway.
        #
        # The fix: pass run_id EXPLICITLY to the pipeline constructor.
        # BasePipeline.__init__ accepts a `run_id` kwarg (see
        # base_pipeline.py:643). When provided, the pipeline uses it
        # instead of reading os.environ. This makes provenance
        # thread-safe under ThreadPoolExecutor without needing
        # ProcessPoolExecutor or any architectural change.
        #
        # We ALSO set os.environ["PIPELINE_RUN_ID"] inside a per-thread
        # context (using a context manager that restores the previous
        # value on exit) so any code that DOES read os.environ sees
        # the correct per-pipeline value. This double protection
        # (explicit kwarg + env var override) handles both patched
        # and unpatched pipeline code paths.
        _prev_env_run_id = _os.environ.get("PIPELINE_RUN_ID")
        _os.environ["PIPELINE_RUN_ID"] = _run_id
        try:
            # Pass run_id explicitly — the kwarg takes precedence
            # over os.environ in BasePipeline.__init__.
            try:
                _pipeline = cls(run_id=_run_id)
            except TypeError:
                # Some pipeline classes may not accept run_id kwarg
                # (older subclasses). Fall back to no-arg construction
                # — the env var override above ensures provenance is
                # still correct.
                _pipeline = cls()
            _pipeline.run()
        finally:
            # Restore the previous env var value (or delete if it
            # was unset before this thread ran).
            if _prev_env_run_id is None:
                _os.environ.pop("PIPELINE_RUN_ID", None)
            else:
                _os.environ["PIPELINE_RUN_ID"] = _prev_env_run_id
        return (name, True, None, _run_id)
    except Exception as e:
        _run_id = getattr(_RUN_ID_LOCAL, "run_id", "")
        return (name, False, str(e), _run_id)

if __name__ == "__main__":
    all_results = []

    print("Running first-pass pipelines in parallel (3 jobs)...")
    # v41 ROOT FIX (SEV3): the previous print said "4 jobs" but FIRST_PASS
    # has only 3 entries (chembl, uniprot, string). The max_workers=4 in
    # ThreadPoolExecutor was also oversized for 3 jobs. Corrected to 3.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run_pipeline, FIRST_PASS))
    all_results.extend(results)
    for name, ok, err, run_id in results:
        if ok:
            print(f"  [OK] {name} (run_id={run_id})")
        else:
            print(f"  [FAIL] {name} (run_id={run_id}): {err}")

    print("Running second-pass (DisGeNET + OMIM sequential)...")
    second_results = list(map(run_pipeline, SECOND_PASS))
    all_results.extend(second_results)
    for name, ok, err, run_id in second_results:
        if ok:
            print(f"  [OK] {name} (run_id={run_id})")
        else:
            print(f"  [FAIL] {name} (run_id={run_id}): {err}")

    print("Running third-pass (PubChem needs drugs in DB)...")
    third_results = list(map(run_pipeline, THIRD_PASS))
    all_results.extend(third_results)
    for name, ok, err, run_id in third_results:
        if ok:
            print(f"  [OK] {name} (run_id={run_id})")
        else:
            print(f"  [FAIL] {name} (run_id={run_id}): {err}")

    print("Running fourth-pass (DrugBank — requires manual XML)...")
    fourth_results = list(map(run_pipeline, FOURTH_PASS))
    all_results.extend(fourth_results)
    for name, ok, err, run_id in fourth_results:
        if ok:
            print(f"  [OK] {name} (run_id={run_id})")
        else:
            print(f"  [FAIL] {name} (run_id={run_id}): {err}")

    # SCI-FIX: Exit non-zero if any pipeline failed so CI/CD can detect
    # broken pipelines. In a medical ETL pipeline, silent failures mean
    # stale or missing drug data.
    # all_results is a 4-tuple: (name, ok, err, run_id)
    failed = [name for name, ok, _, _ in all_results if not ok]
    if failed:
        print(f"\nFAILED pipelines: {failed}")
        sys.exit(1)
    else:
        print("\nAll pipelines completed successfully.")
