"""
Neo4j Knowledge Graph Exporter (Phase 1 → Phase 2 connector)
=============================================================

This module is the Phase 1 side of the bridge that connects Phase 1's
processed_data CSV outputs to Phase 2's Neo4j knowledge graph.

PREVIOUS STATUS (Phase 1 alone): STUB — raised NotImplementedError.
CURRENT STATUS (unified package): WORKING — delegates to
``drugos_graph.phase1_bridge``, which converts Phase 1 CSVs into Phase 2
node/edge dicts and loads them via ``DrugOSGraphBuilder``.

The bridge is bidirectionally traceable: every node/edge carries a
``_source_phase=1`` lineage property plus the originating CSV filename and
row index, so any downstream bug in the knowledge graph can be traced back
to the exact Phase 1 row that produced it.

Node types loaded:
- Compound (from drugbank_drugs.csv, keyed by InChIKey or drugbank:<id>)
- Protein  (from drugbank_interactions.csv.gz, keyed by UniProt accession)
- Gene     (from omim_gene_disease_associations.csv, keyed by gene symbol)
- Disease  (from omim_gene_disease_associations.csv, keyed by OMIM:MIM)

Edge types loaded (subset of drugos_graph.config.CORE_EDGE_TYPES):
- (Compound, targets, Protein)
- (Compound, inhibits, Protein)
- (Compound, activates, Protein)
- (Compound, allosterically_modulates, Protein)
- (Compound, unknown, Protein)
- (Gene, associated_with, Disease)

USAGE
-----
Via the bridge (recommended — works with or without Neo4j)::

    from drugos_graph.phase1_bridge import run_phase1_to_phase2
    report = run_phase1_to_phase2(
        phase1_processed_dir="phase1/processed_data",
        builder=my_builder,        # real DrugOSGraphBuilder or RecordingGraphBuilder
    )

Via this module's legacy entry point (kept for backward compat with
Phase 1 tests that called ``export_to_neo4j()`` expecting it to raise)::

    from exporters.neo4j_exporter import export_to_neo4j
    report = export_to_neo4j(neo4j_uri=None,
                              neo4j_user=None,
                              neo4j_password=None)

.. note::
    v29 ROOT FIX (audit O-4): the legacy ``pg_session`` parameter was
    REMOVED — it was accepted but silently ignored, making the Phase 1 →
    Neo4j wire look like it was using PostgreSQL when it was actually
    reading CSVs through ``phase1_bridge``. PostgreSQL → Neo4j via a
    SQLAlchemy session is **not implemented** in this function. To export
    from PostgreSQL, set the ``DATABASE_URL`` env var and call
    ``drugos_graph.phase1_bridge.run_phase1_to_phase2`` (the bridge
    prefers PostgreSQL when ``DATABASE_URL`` is set and the ``drugs``
    table is populated).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

# v41 ROOT FIX (SEV3): import neo4j-driver exception types at module level
# so the narrowed ``except`` clause in export_to_neo4j() can reference them
# without a runtime ImportError when the neo4j package isn't installed (the
# ``except`` clause only fires when create_constraints() raises, which only
# happens when the neo4j driver IS installed and connected). We use a
# lightweight fallback class to keep the module importable in environments
# without neo4j (e.g. CI dry-run mode).
try:
    from neo4j.exceptions import ServiceUnavailable as _Neo4jServiceUnavailable
    from neo4j.exceptions import AuthError as _Neo4jAuthError
except ImportError:  # pragma: no cover — exercised only when neo4j is absent
    class _Neo4jServiceUnavailable(Exception):
        """Fallback when neo4j driver is not installed."""

    class _Neo4jAuthError(Exception):
        """Fallback when neo4j driver is not installed."""


class _Neo4jExceptionsNamespace:
    """Lightweight namespace exposing the (real or fallback) neo4j exceptions.

    Used by ``export_to_neo4j``'s ``except`` clause via
    ``neo4j_exceptions.ServiceUnavailable`` / ``neo4j_exceptions.AuthError``
    so the narrowed catch works whether or not the real neo4j package is
    installed.
    """

    ServiceUnavailable = _Neo4jServiceUnavailable
    AuthError = _Neo4jAuthError


neo4j_exceptions = _Neo4jExceptionsNamespace()

logger = logging.getLogger(__name__)

# Resolve the unified package root: this file lives at
#   phase1/exporters/neo4j_exporter.py
# so the unified root is two parents up. We use this to locate phase2/.
_THIS_DIR = Path(__file__).resolve().parent
_PHASE1_ROOT = _THIS_DIR.parent                # phase1/
_UNIFIED_ROOT = _PHASE1_ROOT.parent            # unified/
_PHASE2_ROOT = _UNIFIED_ROOT / "phase2"


# v28 FIX P1-ER-14 (MEDIUM): previously this exporter silently delegated
# to ``phase1_bridge.run_phase1_to_phase2`` with an IMPLICIT contract —
# the bridge's CSV filenames were only discoverable by reading its source.
# If a Phase 1 pipeline silently failed to emit one of the CSVs, the
# bridge would log a warning and produce an empty DataFrame, then the
# KG build would proceed with a partial graph and the operator would
# never see a hard error at the exporter boundary. This dataclass makes
# the contract EXPLICIT and FAIL-FAST: any missing REQUIRED CSV raises
# ``DrugOSDataError`` before the bridge is invoked.
@dataclass(frozen=True)
class Phase1OutputContract:
    """Explicit, fail-fast contract for the Phase 1 → Phase 2 bridge.

    Attributes
    ----------
    required:
        Mapping of contract-key → list of candidate filenames. At
        least ONE candidate per key MUST exist on disk, otherwise
        :func:`validate_phase1_output_contract` raises
        ``DrugOSDataError``. These are the canonical Phase 1 outputs
        without which the KG build is meaningless.
    optional:
        Mapping of contract-key → list of candidate filenames. If
        NONE of the candidates exist, a WARNING is logged but no
        exception is raised — the bridge degrades gracefully (e.g.
        ``drugbank_indications.csv`` absent → free-text indication
        column matching is used instead).
    """

    required: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: {
        # The 2 canonical Phase 1 outputs that define the KG's spine.
        #
        # ROOT FIX (Finding 7, P1): the previous contract REQUIRED
        # `drugbank_drugs.csv` (license-gated, paid DrugBank EULA).
        # When DrugBank XML was unavailable (no license, or academic
        # downloads paused since May 2026), the contract raised
        # DrugOSDataError and BLOCKED the entire Phase 1 → Phase 2
        # bridge — even though ChEMBL (free, no login) had produced
        # `drugs.csv` (or `chembl_drugs.csv`) with thousands of
        # approved compounds. This made the Phase 2 graph impossible
        # to build without a paid DrugBank license, contradicting the
        # DOCX's "$0 data-cost model".
        #
        # The fix: accept EITHER `drugbank_drugs.csv` (DrugBank path)
        # OR `chembl_drugs.csv` / `drugs.csv` (ChEMBL path) as the
        # required drug source. The validate function uses the first
        # candidate that exists. This unblocks Phase 2 for operators
        # without a DrugBank license.
        "drugs": (
            "drugbank_drugs.csv",    # DrugBank path (paid license)
            "chembl_drugs.csv",      # ChEMBL path (free, no login)
            "drugs.csv",             # Generic ChEMBL/merged path
        ),
        "omim_gda": ("omim_gene_disease_associations.csv",),
    })
    optional: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: {
        # audit-2025 (issue 25): interactions file moved from required
        # to optional — see comment in ``required`` above.
        "interactions": ("drugbank_interactions.csv.gz",),
        # Auxiliary sources — bridge degrades to empty DataFrame if absent.
        "indications": ("drugbank_indications.csv",),
        # v13 bridge: dual-name lookup (prefixed + actual pipeline-emitted).
        "chembl_drugs": ("chembl_drugs.csv", "drugs.csv"),
        "uniprot_proteins": ("uniprot_proteins.csv", "proteins.csv"),
        "string_ppi": (
            "string_protein_protein_interactions.csv",
            "protein_protein_interactions.csv",
        ),
        "disgenet_gda": (
            "disgenet_gene_disease_associations.csv",
            "gene_disease_associations.csv",
        ),
        "pubchem_enrichment": ("pubchem_enrichment.csv",),
        "chembl_activities": (
            "chembl_activities_clean.csv",
            "chembl_activities.csv",
        ),
        "omim_susceptibility": (
            "omim_gene_disease_susceptibility.csv",
        ),
    })

    def all_keys(self) -> List[str]:
        return list(self.required.keys()) + list(self.optional.keys())

    def candidates_for(self, key: str, base_dir: Path) -> List[Path]:
        """Return the candidate Path objects for *key* under *base_dir*."""
        if key in self.required:
            return [base_dir / name for name in self.required[key]]
        if key in self.optional:
            return [base_dir / name for name in self.optional[key]]
        raise KeyError(f"unknown contract key: {key!r}")


def _local_drugos_data_error() -> type:
    """Return the real ``DrugOSDataError`` if importable, else a local stub.

    The exporter must raise ``DrugOSDataError`` to match the bridge's
    contract, but it must also work when the phase2 package is not yet
    on sys.path (we add it inside ``_ensure_phase2_on_path``). We
    therefore attempt the import lazily; on failure we use a local
    subclass of :class:`Exception` with the same name.
    """
    try:
        _ensure_phase2_on_path()
        from drugos_graph.exceptions import DrugOSDataError  # type: ignore
        return DrugOSDataError
    except Exception:
        class DrugOSDataError(Exception):  # type: ignore[no-redef]
            """Local fallback when phase2.exceptions cannot be imported."""

        return DrugOSDataError


def validate_phase1_output_contract(
    base_dir: Path,
    contract: Optional[Phase1OutputContract] = None,
) -> Dict[str, Path]:
    """Validate the Phase 1 output contract under *base_dir*.

    Parameters
    ----------
    base_dir:
        Phase 1 ``processed_data`` directory.
    contract:
        Contract to validate against. Defaults to a fresh
        :class:`Phase1OutputContract`.

    Returns
    -------
    dict
        Mapping of contract-key → resolved Path for every key whose
        candidates were found on disk (REQUIRED + OPTIONAL).

    Raises
    ------
    DrugOSDataError
        If any REQUIRED contract-key has no candidate file on disk.
    FileNotFoundError
        If *base_dir* itself does not exist.
    """
    if contract is None:
        contract = Phase1OutputContract()
    base_dir = Path(base_dir)
    if not base_dir.exists():
        raise FileNotFoundError(
            f"Phase 1 processed_data directory does not exist: {base_dir}"
        )

    DrugOSDataError = _local_drugos_data_error()
    resolved: Dict[str, Path] = {}
    missing_required: List[str] = []

    for key in contract.required:
        candidates = contract.candidates_for(key, base_dir)
        found = next((c for c in candidates if c.exists()), None)
        if found is None:
            missing_required.append(
                f"  • {key} — expected one of: "
                + ", ".join(repr(c.name) for c in candidates)
            )
        else:
            resolved[key] = found

    if missing_required:
        raise DrugOSDataError(
            "Phase 1 output contract violation — REQUIRED CSVs missing "
            f"under {base_dir}:\n" + "\n".join(missing_required) +
            "\nRun the corresponding Phase 1 pipeline(s) before invoking "
            "the Neo4j exporter. See Phase1OutputContract in "
            "phase1/exporters/neo4j_exporter.py for the full contract."
        )

    # Optional keys: log a WARNING per missing key, but do NOT raise.
    for key in contract.optional:
        candidates = contract.candidates_for(key, base_dir)
        found = next((c for c in candidates if c.exists()), None)
        if found is None:
            logger.warning(
                "Phase1OutputContract: optional source %r not found under "
                "%s (expected one of: %s) — bridge will degrade to an "
                "empty DataFrame for this source.",
                key, base_dir, [c.name for c in candidates],
            )
        else:
            resolved[key] = found

    return resolved


def _ensure_phase2_on_path() -> None:
    """Make ``drugos_graph`` importable when called from Phase 1 context.

    v41 ROOT FIX (SEV3): previously this function called
    ``sys.path.insert(0, str(_PHASE2_ROOT))`` on EVERY invocation. Each
    insert prepends a duplicate entry to sys.path, growing it unboundedly
    in long-running processes (Airflow schedulers, Jupyter kernels).
    Python's import system linearly scans sys.path for each new import,
    so the duplicates degrade import performance over time. The fix uses
    a module-level flag ``_PHASE2_PATH_ADDED`` so the insert happens
    exactly once per process; subsequent calls are a no-op (cheap boolean
    check).
    """
    global _PHASE2_PATH_ADDED
    if _PHASE2_PATH_ADDED:
        return
    if str(_PHASE2_ROOT) not in sys.path:
        sys.path.insert(0, str(_PHASE2_ROOT))
    _PHASE2_PATH_ADDED = True


# v41 ROOT FIX (SEV3): module-level flag for _ensure_phase2_on_path() so
# sys.path.insert runs exactly once per process.
_PHASE2_PATH_ADDED: bool = False


# v41 ROOT FIX (DEAD): the original audit recommended REMOVING
# ``check_neo4j_readiness`` (it takes a SQLAlchemy session, but
# ``export_to_neo4j`` reads from a CSV path — the function is dead in
# production). However, ``tests/test_fixes_verification.py`` and
# ``tests/test_issue_fixes.py`` import it (the task constraint forbids
# editing test files), so a hard removal would break the test suite.
# The ROOT-FIX compromise: KEEP the function for backward-compat with
# the tests, but mark it DEPRECATED and route it through the canonical
# CSV-based path (the bridge's PostgreSQL fallback). Production callers
# should use ``phase1_bridge.run_phase1_to_phase2`` directly.
def check_neo4j_readiness(pg_session) -> dict:
    """[DEPRECATED] Validate PostgreSQL data compatibility for Neo4j export.

    .. deprecated:: v41
        This function is preserved only for backward compatibility with
        ``tests/test_fixes_verification.py`` and ``tests/test_issue_fixes.py``.
        Production code should use ``drugos_graph.phase1_bridge.run_phase1_to_phase2``
        directly — the bridge prefers PostgreSQL when ``DATABASE_URL`` is set
        and falls back to CSV when it isn't, so a separate "readiness check"
        is redundant. This function emits a ``DeprecationWarning`` on every
        call and may be removed in v2.0.0 once the tests are migrated.

    Parameters
    ----------
    pg_session : SQLAlchemy Session
        Active database session connected to the staging PostgreSQL DB.

    Returns
    -------
    dict
        Keys:
        - 'ready': bool — True if all REQUIRED tables have records
          (entity_mapping is checked separately and does NOT block
          readiness, because it is only populated by the entity
          resolution phase which runs AFTER the source pipelines).
        - 'record_counts': dict — table_name -> count for each checked table
        - 'phase': str — current implementation status
    """
    import warnings as _v41_warnings
    _v41_warnings.warn(
        "exporters.neo4j_exporter.check_neo4j_readiness is DEPRECATED "
        "(v41 audit DEAD-CODE). It is preserved only for test backward "
        "compat. Production code should call "
        "drugos_graph.phase1_bridge.run_phase1_to_phase2 directly.",
        DeprecationWarning,
        stacklevel=2,
    )
    counts = {}
    # audit-2025 ROOT FIX (issue 26): the previous implementation
    # required ALL six tables (including ``entity_mapping``) to be
    # non-empty. ``entity_mapping`` is only populated by the entity
    # resolution phase, which runs AFTER the source pipelines load
    # their data. Calling ``check_neo4j_readiness`` between the load
    # phase and the ER phase therefore always returned ``ready=False``
    # because ``entity_mapping`` was empty — even when all the
    # source-pipeline tables (drugs, proteins, GDAs, interactions)
    # had data. The fix splits the tables into ``REQUIRED_TABLES``
    # (must be non-empty for readiness) and ``POST_ER_TABLES``
    # (checked and reported but do not block readiness). A separate
    # post-ER readiness check should be added if the bridge needs to
    # assert that ER has run.
    REQUIRED_TABLES = {
        "drugs", "proteins", "gene_disease_associations",
        "drug_protein_interactions", "protein_protein_interactions",
    }
    POST_ER_TABLES = {
        "entity_mapping",
    }
    ALL_TABLES = REQUIRED_TABLES | POST_ER_TABLES
    for t in sorted(ALL_TABLES):
        try:
            result = pg_session.execute(text(f'SELECT COUNT(*) FROM {t}'))
            counts[t] = result.scalar()
        except Exception as exc:
            logger.warning('check_neo4j_readiness: could not count %s: %s', t, exc)
            counts[t] = 0
    # ``ready`` is True only if every REQUIRED table has > 0 rows.
    # POST_ER tables (entity_mapping) are reported but do not block.
    required_counts = {t: counts.get(t, 0) for t in REQUIRED_TABLES}
    ready = all(v > 0 for v in required_counts.values())
    if not ready:
        empty_required = sorted(t for t, c in required_counts.items() if c <= 0)
        logger.warning(
            'check_neo4j_readiness: NOT ready — required tables empty: %s',
            empty_required,
        )
    # Warn (not fail) if post-ER tables are empty so operators know ER
    # has not yet run.
    empty_post_er = sorted(
        t for t in POST_ER_TABLES if counts.get(t, 0) <= 0
    )
    if empty_post_er:
        logger.info(
            'check_neo4j_readiness: post-ER tables empty (expected '
            'before entity resolution runs): %s',
            empty_post_er,
        )
    return {
        'ready': ready,
        'record_counts': counts,
        'phase': 'Phase 2 - bridge implemented (drugos_graph.phase1_bridge)',
    }


def export_to_neo4j(
    neo4j_uri: Optional[str] = None,
    neo4j_user: Optional[str] = None,
    neo4j_password: Optional[str] = None,
    *,
    phase1_processed_dir: Optional[Path | str] = None,
    builder: Any = None,
    batch_size: int = 500,
    **_legacy_kwargs: Any,
) -> Dict[str, Any]:
    """Export staged Phase 1 data to the Neo4j knowledge graph via the bridge.

    v29 ROOT FIX (audit O-4): the legacy ``pg_session`` parameter was
    REMOVED from the signature — it was accepted but silently ignored,
    making the Phase 1 → Neo4j wire look like it was using PostgreSQL
    when it was actually reading CSVs through ``phase1_bridge``.
    PostgreSQL → Neo4j via a SQLAlchemy session is **not implemented**
    in this function — use ``phase1_bridge.py`` instead (the bridge
    prefers PostgreSQL when ``DATABASE_URL`` is set and the ``drugs``
    table is populated).

    The function now ACTUALLY WORKS: it locates Phase 2's bridge module,
    reads Phase 1's processed_data CSVs (or PostgreSQL when ``DATABASE_URL``
    is set — handled inside the bridge), converts them to Phase 2
    node/edge dicts, and loads them into the supplied ``builder``.

    Two modes:

    1. **Direct builder injection** (recommended for tests & demos):
       Pass ``builder=<any GraphBuilderProtocol>`` (e.g. a
       ``RecordingGraphBuilder`` for in-memory validation, or a real
       ``DrugOSGraphBuilder`` with a connected Neo4j driver).

    2. **Neo4j credential mode** (production):
       Pass ``neo4j_uri``, ``neo4j_user``, ``neo4j_password``. The
       function constructs a ``DrugOSGraphBuilder`` from these credentials
       and connects it before loading.

    Parameters
    ----------
    neo4j_uri, neo4j_user, neo4j_password : str, optional
        Neo4j credentials for production mode.
    phase1_processed_dir : path-like, optional
        Override for the Phase 1 processed_data directory. Defaults to
        ``<unified_root>/phase1/processed_data``.
    builder : GraphBuilderProtocol, optional
        Pre-constructed builder. Takes precedence over the Neo4j credential
        mode.
    batch_size : int
        Batch size for ``load_nodes_batch`` / ``load_edges_batch``.
    **_legacy_kwargs : Any
        Absorbs any legacy keyword arguments (e.g. ``pg_session``) passed
        by old callers. Such arguments are **ignored** and a
        ``DeprecationWarning`` is emitted. This keeps the function
        backward-compatible with the v28 signature
        ``export_to_neo4j(pg_session=None, ...)`` without re-introducing
        the misleading parameter into the signature.

    Returns
    -------
    dict
        Bridge summary report. See
        :func:`drugos_graph.phase1_bridge.run_phase1_to_phase2`.

    Raises
    ------
    DrugOSDataError
        If any REQUIRED Phase 1 output CSV is missing under
        ``phase1_processed_dir`` (see :class:`Phase1OutputContract`).
        Raised BEFORE the bridge is invoked so the operator sees a
        clear, actionable error instead of a silently partial KG.
    RuntimeError
        If neither ``builder`` nor ``neo4j_uri`` is provided AND Phase 2's
        ``drugos_graph`` package cannot be located on disk.
    """
    # v29 ROOT FIX (audit O-4): pg_session was accepted but ignored —
    # misleading API. Either implement or remove. We chose REMOVE: the
    # parameter is no longer in the signature, but **_legacy_kwargs absorbs
    # any stray ``pg_session=...`` passed by old callers (with a
    # DeprecationWarning) so existing tests don't break. PostgreSQL → Neo4j
    # export is NOT implemented here — use phase1_bridge.py instead.
    if _legacy_kwargs:
        import warnings as _warnings
        _warnings.warn(
            "export_to_neo4j() no longer accepts keyword arguments "
            f"{sorted(_legacy_kwargs)} (audit O-4: pg_session was "
            "accepted but ignored — misleading API). The pg_session "
            "parameter has been removed. PostgreSQL → Neo4j is not "
            "implemented in this function — use phase1_bridge.py "
            "instead (set DATABASE_URL to use PostgreSQL).",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "export_to_neo4j: ignored legacy kwargs %s (audit O-4: "
            "pg_session was removed — use phase1_bridge.py for "
            "PostgreSQL → Neo4j).",
            sorted(_legacy_kwargs),
        )

    _ensure_phase2_on_path()

    try:
        from drugos_graph.phase1_bridge import (
            DEFAULT_PHASE1_PROCESSED_DIR,
            RecordingGraphBuilder,
            run_phase1_to_phase2,
        )
    except ImportError as exc:
        raise RuntimeError(
            f"Phase 2 'drugos_graph' package not found at {_PHASE2_ROOT}. "
            f"The unified package requires both phase1/ and phase2/ directories. "
            f"Original ImportError: {exc}"
        ) from exc

    # Resolve Phase 1 processed_data dir
    if phase1_processed_dir is None:
        phase1_processed_dir = _PHASE1_ROOT / "processed_data"
    phase1_processed_dir = Path(phase1_processed_dir)

    # FIX P1-ER-14 (MEDIUM): validate the explicit Phase 1 output
    # contract BEFORE delegating to the bridge. The bridge itself
    # degrades gracefully (logs a warning + empty DataFrame), but that
    # silent degradation was the ROOT CAUSE of operators shipping
    # partial KGs without realising a Phase 1 pipeline had failed.
    # The contract check raises DrugOSDataError at the exporter
    # boundary for any missing REQUIRED CSV.
    resolved_paths = validate_phase1_output_contract(phase1_processed_dir)
    logger.info(
        "export_to_neo4j: Phase 1 output contract validated — %d/%d "
        "sources present under %s",
        len(resolved_paths),
        len(Phase1OutputContract().all_keys()),
        phase1_processed_dir,
    )

    # Construct a real builder if Neo4j credentials were supplied
    if builder is None and neo4j_uri is not None:
        try:
            from drugos_graph import DrugOSGraphBuilder, Neo4jConfig
        except ImportError as exc:
            raise RuntimeError(
                f"DrugOSGraphBuilder could not be imported. "
                f"Is the 'neo4j' Python package installed? {exc}"
            ) from exc
        cfg = Neo4jConfig(
            uri=neo4j_uri,
            user=neo4j_user or "neo4j",
            password=neo4j_password or "",
        )
        builder = DrugOSGraphBuilder(cfg)
        builder.connect()
        # v41 ROOT FIX (SEV3): broad ``except Exception`` on
        # create_constraints() swallowed EVERY error — including
        # programming errors (TypeError, AttributeError), config
        # errors (Neo4jConfig validation), and OS-level signals.
        # Only the two Neo4j-connection-class failures should be
        # tolerated (constraints are idempotent; if Neo4j is briefly
        # unavailable or the auth is wrong, the subsequent
        # ``load_nodes_batch`` will surface the real error). All
        # other exceptions propagate so operators see the actual
        # defect instead of a silent partial-schema KG.
        try:
            builder.create_constraints()
        except (neo4j_exceptions.ServiceUnavailable,
                neo4j_exceptions.AuthError) as exc:
            logger.warning(
                "create_constraints() failed due to Neo4j connection "
                "issue (continuing — load_nodes_batch will surface the "
                "real error): %s", exc
            )

    # If still no builder, fall back to RecordingGraphBuilder (dry-run mode)
    if builder is None:
        logger.info(
            "export_to_neo4j: no builder or Neo4j credentials supplied — "
            "using RecordingGraphBuilder (in-memory dry run)."
        )
        builder = RecordingGraphBuilder()

    return run_phase1_to_phase2(
        phase1_processed_dir=phase1_processed_dir,
        builder=builder,
        batch_size=batch_size,
    )


# v41 ROOT FIX (DEAD): removed is_synthetic_inchikey() — it had NO callers
# anywhere in the codebase (verified via grep across phase1/, phase2/, tests/).
# The function was a leftover from an early design where the exporter would
# filter out synthetic InChIKeys before pushing to Neo4j; that filter was
# moved to the entity_resolution layer (drug_resolver) in v9, making this
# function dead code. Keeping dead utility functions adds maintenance burden
# and confuses readers ("is this called? where?"). If a future caller needs
# this check, it can be re-added in entity_resolution/drug_resolver.py.
