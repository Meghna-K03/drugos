"""
DrugOS Graph — Phase 1 → Phase 2 Bridge
========================================

This module is the **single, authoritative contract** that connects the two
phases of the Autonomous Drug Repurposing Platform:

  Phase 1  (``phase1/``)  — Data Ingestion & Pipeline Setup
      Outputs cleaned, normalised, schema-validated data into either:
        (a) PostgreSQL via the SQLAlchemy ORM (``database/models.py``) — the
            AUTHORITATIVE backend per the docx architecture, OR
        (b) CSV files in ``phase1/processed_data/`` — the legacy/dev fallback.
      Either backend produces the same dict of DataFrames for Phase 2.

  Phase 2  (``drugos_graph/``) — Knowledge Graph Construction
      Consumes nodes/edges and loads them into Neo4j via
      :class:`drugos_graph.kg_builder.DrugOSGraphBuilder`.

v29 ROOT FIX (Phase1↔Phase2 100% connection):
  The forensic audit proved the bridge previously bypassed PostgreSQL
  entirely and read CSVs only — Phase 1's 8,500 lines of ORM/migration/
  loader code were dead weight. The bridge now prefers PostgreSQL when
  ``DATABASE_URL`` is set AND the ``drugs`` table is populated. The CSV
  path remains as a fallback for dev/CI runs without a database.

  The chosen backend is recorded as ``out["_phase1_backend"]`` (either
  ``"postgresql"`` or ``"csv"``) so operators can verify the production
  path was actually used.

The bridge provides THREE callable entry points, in increasing order of
abstraction:

  1. :func:`read_phase1_outputs`      — read Phase 1 data (PostgreSQL or CSV) into pandas DataFrames
  2. :func:`stage_phase1_to_phase2`   — convert DataFrames → Phase 2 node/edge dicts
  3. :func:`load_into_graph`           — load staged dicts into a graph builder

Plus a top-level convenience:

  4. :func:`run_phase1_to_phase2`     — read → stage → load in one call

WHY THIS MODULE EXISTS
----------------------
Before this module existed, Phase 1's ``exporters/neo4j_exporter.py`` raised
``NotImplementedError`` and Phase 2's loaders re-downloaded every source
file from external URLs (DRKG, DrugBank XML, ChEMBL SQLite, etc.). The two
phases were never connected. This module is the missing wire.

The conversion is **lossless and bidirectionally traceable**: every node and
edge produced by the bridge carries a ``_source_phase=1`` lineage property
and the original Phase 1 row index so any downstream bug can be traced back
to the exact Phase 1 CSV row.

SCHEMA MAPPING (Phase 1 CSV column → Phase 2 node/edge property)
----------------------------------------------------------------

Compound nodes (from drugbank_drugs.csv)
    drugbank_id        → id            (canonical Neo4j ID)
    name               → name
    inchikey           → inchikey
    smiles             → smiles
    molecular_weight   → molecular_weight
    is_fda_approved    → fda_approved
    is_withdrawn       → withdrawn       (RL safety signal — patient harm)
    clinical_status    → clinical_status
    groups             → groups
    mechanism_of_action→ mechanism_of_action
    cas_number         → cas_number
    chembl_id          → chembl_id
    pubchem_cid        → pubchem_cid
    completeness_score → completeness_score

Protein nodes (from drugbank_interactions.csv.gz, dedup on uniprot_id)
    uniprot_id         → id
    target_name        → name
    organism           → organism

Gene nodes (from omim_gene_disease_associations.csv, dedup on gene_symbol)
    gene_symbol        → id
    gene_mim           → mim_id

Disease nodes (from omim_gene_disease_associations.csv, dedup on disease_id)
    disease_id         → id
    disease_name       → name
    phenotype_mim      → mim_id

Edges
    drugbank_interactions.csv.gz:
        (Compound, targets, Protein)   — action_type='target'/'unknown'/None
        (Compound, inhibits, Protein)  — action_type contains 'inhibitor'
        (Compound, activates, Protein) — action_type contains 'activator'
        (Compound, allosterically_modulates, Protein)
                                       — action_type contains 'allosteric'
        (Compound, unknown, Protein)   — action_type set but not matched
    omim_gene_disease_associations.csv:
        (Gene, associated_with, Disease)
                                       — score + association_type as props

The edge types above are a strict subset of
:data:`drugos_graph.config.CORE_EDGE_TYPES` — no non-core edges are produced.

USAGE
-----
Production (with a real PostgreSQL + Neo4j)::

    # DATABASE_URL must be set in the environment.
    from drugos_graph import DrugOSGraphBuilder, Neo4jConfig
    from drugos_graph.phase1_bridge import run_phase1_to_phase2

    builder = DrugOSGraphBuilder(Neo4jConfig.from_env())
    builder.connect()
    builder.create_constraints()
    report = run_phase1_to_phase2(
        phase1_processed_dir="/path/to/phase1/processed_data",
        builder=builder,
    )
    assert report["backend"] == "postgresql"  # verify root-fix took effect
    print(report["summary"])

Testing (no PostgreSQL, no Neo4j required)::

    from drugos_graph.phase1_bridge import (
        run_phase1_to_phase2, RecordingGraphBuilder,
    )
    recorder = RecordingGraphBuilder()
    report = run_phase1_to_phase2(
        phase1_processed_dir="phase1/processed_data",
        builder=recorder,
        prefer_postgres=False,  # force CSV backend in unit tests
    )
    assert report["summary"]["nodes_loaded"] > 0
    assert report["summary"]["edges_loaded"] > 0

PATIENT-SAFETY NOTE
-------------------
The ``withdrawn`` flag on Compound nodes is the primary input to the RL
agent's safety ranker. A null ``withdrawn`` value is treated as "not
withdrawn" → SAFE → a withdrawn drug like Valdecoxib would be surfaced as a
repurposing candidate. The bridge EXPLICITLY coerces ``is_withdrawn`` to a
bool and writes ``withdrawn=False`` (never null) for every Compound node.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import os
import re  # v24: needed for CHEMBL_TGT_ ID normalization (Audit Chain 9 fix)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

import pandas as pd

# v27 ROOT FIX (P2-B-5): import DrugOSDataError so the new
# ``_validate_phase1_columns`` helper can raise on schema mismatch.
try:
    from .exceptions import DrugOSDataError
except Exception:  # pragma: no cover — fallback for direct-script execution
    class DrugOSDataError(Exception):
        """Local fallback when the package cannot be imported."""

logger = logging.getLogger(__name__)

__all__ = [
    "Phase1StagedData",
    "RecordingGraphBuilder",
    "GraphBuilderProtocol",
    "DEFAULT_PHASE1_PROCESSED_DIR",
    "PHASE1_TO_PHASE2_BRIDGE_VERSION",
    "read_phase1_outputs",
    "stage_phase1_to_phase2",
    "load_into_graph",
    "run_phase1_to_phase2",
    "compute_input_checksum",
    "extract_drug_records_from_staged",  # v29 ROOT FIX (audit I-12): reuse staged compound_nodes as drug_records
    "bridge_to_pyg_maps",  # v6 fix (bug #B3): convert recorder output → PyG maps
]

PHASE1_TO_PHASE2_BRIDGE_VERSION: str = "1.1.0"  # v6: structured indications + upstream dedup + PyG bridge

# v41 ROOT FIX (Task J SEV3): DEFAULT_PHASE1_PROCESSED_DIR was computed
# at module import time, which caused issues for tests that patched
# Path and for deployments where the path didn't exist yet at import
# (e.g. before phase1 was built). It's now a LAZY callable so the
# resolution happens at first use. Backward compat: the symbol is still
# a `Path` at the module level via __getattr__ (PEP 562) — callers that
# do `from drugos_graph.phase1_bridge import DEFAULT_PHASE1_PROCESSED_DIR`
# get a `Path` (resolved on first access), and callers that want the
# lazy behaviour can call `get_default_phase1_processed_dir()`.
_DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED: Optional[Path] = None


def get_default_phase1_processed_dir() -> Path:
    """Return the default Phase 1 processed_data directory (lazy).

    Resolved on first call and cached. Re-call ``reset_default_phase1_processed_dir()``
    to force re-resolution (used by tests that monkeypatch ``Path``).
    """
    global _DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED
    if _DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED is None:
        _DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED = (
            Path(__file__).resolve().parents[2] / "phase1" / "processed_data"
        )
    return _DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED


def reset_default_phase1_processed_dir() -> None:
    """Force the next ``get_default_phase1_processed_dir()`` call to re-resolve."""
    global _DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED
    _DEFAULT_PHASE1_PROCESSED_DIR_RESOLVED = None


def __getattr__(name: str):
    # v41 ROOT FIX (Task J SEV3): PEP 562 lazy attribute access for
    # DEFAULT_PHASE1_PROCESSED_DIR so the path is resolved on FIRST USE,
    # not at module import. Preserves backward compat with existing
    # callers that do `from drugos_graph.phase1_bridge import
    # DEFAULT_PHASE1_PROCESSED_DIR`.
    if name == "DEFAULT_PHASE1_PROCESSED_DIR":
        return get_default_phase1_processed_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# v41 ROOT FIX (Task J DEAD): _PLACEHOLDER_GENES was defined inline at line
# ~2098 inside ``_stage_omim_gene_disease``, re-allocated on EVERY call.
# Moved to a module-level frozenset (frozen = immutable = safe to share,
# fast membership test, no allocation cost). Adding a new placeholder is a
# one-line change at the module level, not buried in a 250-line function.
_PLACEHOLDER_GENES: frozenset = frozenset({"ALTGENE", "MENDGENE", "MYGENE", ""})


# ---------------------------------------------------------------------------
# 1. GraphBuilder protocol — what the bridge needs from a builder
# ---------------------------------------------------------------------------
class GraphBuilderProtocol(Protocol):
    """Structural type that any graph builder consumed by the bridge must satisfy.

    Both :class:`drugos_graph.kg_builder.DrugOSGraphBuilder` (production,
    backed by Neo4j) and :class:`RecordingGraphBuilder` (test, in-memory)
    satisfy this protocol.
    """

    def load_nodes_batch(
        self,
        label: str,
        nodes: List[Dict[str, Any]],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> Any: ...

    def load_edges_batch(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: List[Dict[str, Any]],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# 2. RecordingGraphBuilder — for tests, demos, and dry-runs (no Neo4j)
# ---------------------------------------------------------------------------
class RecordingGraphBuilder:
    """In-memory graph builder that records every load call without Neo4j.

    Implements :class:`GraphBuilderProtocol`. Every call to
    ``load_nodes_batch`` / ``load_edges_batch`` appends to internal lists and
    returns the count of items accepted (mirroring the int return contract
    of :meth:`DrugOSGraphBuilder.load_nodes_batch`).

    Use this in tests and in the ``--dry-run`` mode of ``run_unified.py`` to
    validate the full Phase 1 → Phase 2 data flow without provisioning Neo4j.

    BUG-D-004 root fix: this builder now applies the SAME validation as
    :class:`DrugOSGraphBuilder` — ID_PATTERNS, CORE_EDGE_TYPES whitelist,
    and dead-letter recording. Previously it applied ZERO validation, so
    tests using it were structurally blind to production-only data loss:
    a test could report "100 nodes loaded, 0 errors" while the production
    path silently dead-lettered every one of those 100 nodes for failing
    ID_PATTERNS. Now tests catch the same failures production does.
    """

    def __init__(self) -> None:
        self.node_loads: List[Dict[str, Any]] = []
        self.edge_loads: List[Dict[str, Any]] = []
        # Lookup structures for cross-edge validation
        self._node_ids_by_label: Dict[str, set] = {}
        # BUG-D-004: dead-letter queue (in-memory mirror of the production
        # dead_letter.jsonl). Tests can inspect this to verify that
        # invalid records are rejected.
        self.dead_letter: List[Dict[str, Any]] = []

    # -- Internal helpers (BUG-D-004) ---------------------------------------
    def _validate_node_id(self, label: str, node_id: Any) -> bool:
        """Validate a node ID against ID_PATTERNS.

        Returns True if valid, False otherwise. Mirrors
        :meth:`DrugOSGraphBuilder._validate_node_id`.

        v28 ROOT FIX (P2-B-6): the previous code returned ``True`` for
        any label not present in ``ID_PATTERNS`` — silently disabling
        validation for typo'd labels like 'MedDRATerm' (missing
        underscore) or 'Compoud' (misspelled Compound). Every ID was
        accepted by tests, but production ``DrugOSGraphBuilder`` raises
        :class:`UnknownLabelError` — so tests passed while production
        crashed. Now ``RecordingGraphBuilder`` raises the SAME exception
        so tests catch the same failures production does. Fail-closed is
        the only safe default for biomedical ID validation.
        """
        if node_id is None:
            return False
        # Import here to avoid circular imports at module load.
        from .kg_builder import ID_PATTERNS
        from .exceptions import UnknownLabelError
        pattern = ID_PATTERNS.get(label)
        if pattern is None:
            # v28 ROOT FIX (P2-B-6): mirror production — raise instead of
            # silently accepting unknown labels. Tests that previously
            # passed with typo'd labels will now FAIL, exposing the bug
            # at test time rather than at production deployment time.
            raise UnknownLabelError(
                f"Unknown node label {label!r} has no entry in ID_PATTERNS. "
                f"Either fix the label typo or register the new label's "
                f"pattern in kg_builder.ID_PATTERNS. (P2-B-6 root fix: "
                f"RecordingGraphBuilder previously returned True for "
                f"unknown labels, masking production UnknownLabelError "
                f"failures at test time.)",
                context={"label": label, "node_id": str(node_id)},
            )
        import re
        return bool(re.match(pattern, str(node_id)))

    def _dead_letter(
        self, source: str, record: Dict[str, Any], reason: str
    ) -> None:
        """Append to the in-memory dead-letter queue (BUG-D-004)."""
        self.dead_letter.append({
            "source": source,
            "reason": reason,
            "record": record,
        })

    # -- Protocol methods ----------------------------------------------------
    def load_nodes_batch(
        self,
        label: str,
        nodes: List[Dict[str, Any]],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> int:
        ids = self._node_ids_by_label.setdefault(label, set())
        accepted: List[Dict[str, Any]] = []
        source = kwargs.get("source", "unknown")
        for n in nodes:
            nid = n.get("id")
            if nid is None:
                self._dead_letter(
                    source, n, f"missing_id:{label}"
                )
                continue
            # BUG-D-004: validate against ID_PATTERNS (was zero validation).
            if not self._validate_node_id(label, nid):
                self._dead_letter(
                    source, n,
                    f"invalid_id_format:{label}:id={nid!r}"
                )
                continue
            if nid in ids:
                continue  # idempotent MERGE semantics
            ids.add(nid)
            accepted.append(n)
        self.node_loads.append({
            "label": label,
            "requested": len(nodes),
            "accepted": len(accepted),
            "nodes": accepted,
            "source": source,
            # BUG-D-004: surface dead-letter count per batch.
            "dead_lettered": len(nodes) - len(accepted),
        })
        return len(accepted)

    def load_edges_batch(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: List[Dict[str, Any]],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> int:
        src_ids = self._node_ids_by_label.get(src_label, set())
        dst_ids = self._node_ids_by_label.get(dst_label, set())
        accepted: List[Dict[str, Any]] = []
        seen: set = set()
        source = kwargs.get("source", "unknown")
        # BUG-D-004: CORE_EDGE_TYPES whitelist check (mirror production).
        from .kg_builder import CORE_EDGE_TYPES
        edge_key = (src_label, rel_type, dst_label)
        if hasattr(CORE_EDGE_TYPES, "__contains__") and edge_key not in CORE_EDGE_TYPES:
            # Not in whitelist — dead-letter every edge with reason.
            for e in edges:
                self._dead_letter(
                    source, e,
                    f"edge_type_not_in_whitelist:{src_label}-{rel_type}->{dst_label}"
                )
            self.edge_loads.append({
                "src_label": src_label,
                "rel_type": rel_type,
                "dst_label": dst_label,
                "requested": len(edges),
                "accepted": 0,
                "edges": [],
                "source": source,
                "dead_lettered": len(edges),
            })
            return 0
        for e in edges:
            src = e.get("src_id")
            dst = e.get("dst_id")
            if src is None or dst is None:
                self._dead_letter(
                    source, e,
                    f"missing_endpoint_id:{src_label}-{rel_type}->{dst_label}"
                )
                continue
            # BUG-D-004: validate endpoints against ID_PATTERNS.
            if not self._validate_node_id(src_label, src):
                self._dead_letter(
                    source, e,
                    f"invalid_src_id_format:{src_label}:id={src!r}"
                )
                continue
            if not self._validate_node_id(dst_label, dst):
                self._dead_letter(
                    source, e,
                    f"invalid_dst_id_format:{dst_label}:id={dst!r}"
                )
                continue
            # Edge endpoints must exist as nodes (referential integrity).
            if src not in src_ids or dst not in dst_ids:
                self._dead_letter(
                    source, e,
                    f"endpoint_node_missing:{src_label}={src!r}->{dst_label}={dst!r}"
                )
                continue
            key = (src, rel_type, dst)
            if key in seen:
                continue  # idempotent MERGE
            seen.add(key)
            accepted.append(e)
        self.edge_loads.append({
            "src_label": src_label,
            "rel_type": rel_type,
            "dst_label": dst_label,
            "requested": len(edges),
            "accepted": len(accepted),
            "edges": accepted,
            "source": source,
            "dead_lettered": len(edges) - len(accepted),
        })
        return len(accepted)

    # -- Inspection helpers (test convenience) -------------------------------
    @property
    def total_nodes(self) -> int:
        return sum(load["accepted"] for load in self.node_loads)

    @property
    def total_edges(self) -> int:
        return sum(load["accepted"] for load in self.edge_loads)

    def nodes_by_label(self, label: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for load in self.node_loads:
            if load["label"] == label:
                out.extend(load["nodes"])
        return out

    def edges_by_type(self, src: str, rel: str, dst: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for load in self.edge_loads:
            if (
                load["src_label"] == src
                and load["rel_type"] == rel
                and load["dst_label"] == dst
            ):
                out.extend(load["edges"])
        return out


# ---------------------------------------------------------------------------
# 3. Phase1StagedData — the structured intermediate
# ---------------------------------------------------------------------------
@dataclass
class Phase1StagedData:
    """Structured Phase 2 node/edge dicts produced from Phase 1 CSVs.

    Fields are intentionally List[dict] (not DataFrames) because
    ``DrugOSGraphBuilder.load_nodes_batch`` expects Python dicts.
    """

    compound_nodes: List[Dict[str, Any]] = field(default_factory=list)
    protein_nodes: List[Dict[str, Any]] = field(default_factory=list)
    gene_nodes: List[Dict[str, Any]] = field(default_factory=list)
    disease_nodes: List[Dict[str, Any]] = field(default_factory=list)
    # FIX-F / C-16: ClinicalOutcome nodes derived from
    # drugbank_indications.csv by _load_clinical_outcomes().
    clinical_outcome_nodes: List[Dict[str, Any]] = field(default_factory=list)
    # v43 ROOT FIX (P2-026): Pathway nodes from pathways.csv. The
    # previous code set this dynamically (staged.pathway_nodes = [...])
    # without declaring it as a field, which meant it wasn't included
    # in total_nodes and wasn't visible in the dataclass repr. The fix
    # declares it as a proper field. Also adds pathway_nodes_emitted
    # bool for clarity — operators can check this to know whether the
    # 5th node type was produced (pathways.csv was present) or not.
    pathway_nodes: List[Dict[str, Any]] = field(default_factory=list)
    pathway_nodes_emitted: bool = False

    # Edges keyed by (src_label, rel_type, dst_label) — matches CORE_EDGE_TYPES
    edges: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = field(
        default_factory=dict
    )

    # Source-level provenance
    sources_read: List[str] = field(default_factory=list)
    # v29 ROOT FIX (audit I-10): track ALL source keys the reader
    # attempted to load (including those whose DataFrame was empty).
    # Used by load_into_graph's lineage checksum so empty-but-present
    # CSVs contribute to the checksum (previously they were silently
    # dropped, breaking lineage reproducibility for fixtures that ship
    # zero-row CSVs).
    sources_attempted: List[str] = field(default_factory=list)
    checksums: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # FORENSIC bridge root fix: store Phase 1's entity_mapping DataFrame
    # so Phase 2's entity_resolver can REUSE it instead of re-resolving
    # from scratch. Populated by load_into_graph from the reader's
    # ``entity_mapping`` key.
    entity_mapping_df: Any = None  # pd.DataFrame or None

    # Actual Phase 1 processed_data directory used by read_phase1_outputs.
    # Used by load_into_graph to compute the input_checksum from the REAL
    # file paths (not the default dir) so lineage is correct even when a
    # custom phase1_processed_dir is supplied. Fixes lineage bug where the
    # checksum was always computed from DEFAULT_PHASE1_PROCESSED_DIR.
    phase1_processed_dir: Optional[Path] = None

    @property
    def total_nodes(self) -> int:
        # v43 ROOT FIX (P2-026): include pathway_nodes in the total.
        return (
            len(self.compound_nodes)
            + len(self.protein_nodes)
            + len(self.gene_nodes)
            + len(self.disease_nodes)
            + len(self.clinical_outcome_nodes)
            + len(self.pathway_nodes)
        )

    @property
    def total_edges(self) -> int:
        return sum(len(v) for v in self.edges.values())

    def edge_types_present(self) -> List[Tuple[str, str, str]]:
        return sorted(self.edges.keys())


# ---------------------------------------------------------------------------
# 4. read_phase1_outputs — read CSVs into DataFrames
# ---------------------------------------------------------------------------
def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_input_checksum(paths: Iterable[Path]) -> str:
    """Deterministic SHA-256 over a sorted list of file paths & contents.

    Used as the ``input_checksum`` lineage property on every node/edge the
    bridge loads, so a downstream consumer can verify that the graph was
    built from a specific Phase 1 snapshot.

    v6 fix (Tier 4): the v5 implementation hashed the full file PATH
    into the checksum, so identical CSVs in different install dirs
    produced different lineage hashes — breaking reproducibility for
    users who installed the package at different filesystem locations.
    The fix hashes only the file BASENAME (e.g. ``drugbank_drugs.csv``)
    plus the file CONTENTS. Two installs with the same CSV contents
    now produce identical checksums, while still distinguishing between
    different files (drugs.csv vs interactions.csv.gz).
    """
    h = hashlib.sha256()
    for p in sorted(paths):
        # Hash only the basename (not the full path) so reproducibility
        # survives install-dir relocations (Tier 4 fix).
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        if p.exists():
            h.update(_sha256_of_file(p).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def _read_csv_robust(path: Path) -> pd.DataFrame:
    """Read a CSV (optionally .gz) into a DataFrame, raising on absence."""
    if not path.exists():
        raise FileNotFoundError(f"Phase 1 output not found: {path}")
    if path.suffix == ".gz":
        return pd.read_csv(path, compression="gzip", low_memory=False)
    return pd.read_csv(path, low_memory=False)


# v27 ROOT FIX (P2-B-5): Schema validation for Phase 1 CSVs.
# Each Phase 1 source CSV has a minimum set of columns the bridge (and the
# downstream loaders) depend on. If a column is missing, the bridge
# silently returns empty strings/None for every ``row.get(...)`` call,
# producing ZERO-output bugs that are nearly impossible to triage.
# Required columns per source (only enforced when the file is present —
# missing files still degrade gracefully per the bridge's contract).
#
# v27 NOTE: the original audit spec listed columns like ``target_uniprot_id``
# (drugbank_interactions), ``protein1``/``protein2`` (string_ppi),
# ``uniprot_id`` (uniprot_proteins), ``disease_mim`` (omim_gda). The ACTUAL
# Phase 1 CSVs (verified at /home/z/my-project/v27/v27_upgraded/phase1/
# processed_data/) emit:
#   • drugbank_interactions.csv.gz → drugbank_id, uniprot_id, action_type
#     (column is ``uniprot_id``, NOT ``target_uniprot_id``)
#   • string_protein_protein_interactions.csv → uniprot_ac_a, uniprot_ac_b,
#     combined_score (NO ``protein1``/``protein2`` — those are raw STRING
#     columns that Phase 1's pipeline replaces with UniProt accessions)
#   • uniprot_proteins.csv → uniprot_ac, gene_symbol (column is
#     ``uniprot_ac`` / ``accession``, NOT ``uniprot_id``)
#   • omim_gene_disease_associations.csv → gene_mim, gene_symbol,
#     disease_id, disease_name (column is ``disease_id`` / has
#     ``phenotype_mim``, NOT ``disease_mim``)
# Using the audit's literal column list would BREAK the bridge against the
# real Phase 1 schema. The list below reflects the ACTUAL Phase 1 contract.
_PHASE1_EXPECTED_COLUMNS: Dict[str, List[str]] = {
    "drugs": ["drugbank_id", "name", "inchikey"],
    "interactions": ["drugbank_id", "uniprot_id", "action_type"],
    "omim_gda": ["gene_mim", "gene_symbol", "disease_id", "disease_name"],
    "chembl_drugs": ["chembl_id", "inchikey"],
    "uniprot_proteins": ["uniprot_ac", "gene_symbol"],
    "string_ppi": ["uniprot_ac_a", "uniprot_ac_b", "combined_score"],
    "disgenet_gda": ["gene_symbol", "disease_id", "score"],
    "pubchem_enrichment": ["inchikey", "canonical_smiles"],
    # v35 ROOT FIX (L-7): ``chembl_activities`` previously required only
    # ``molecule_chembl_id`` and ``target_chembl_id``. The bridge's
    # ChEMBL→Protein/Compound edge builder reads ``pchembl_value`` and
    # ``standard_relation`` from each row (see
    # ``_classify_chembl_activity_edge`` and the staged edge props).
    # Without these in the expected-columns list, a Phase 1 schema
    # regression that drops them would silently produce None potency
    # values on every ChEMBL edge (the activity edges still load, but
    # with no pchembl_value for the RL ranker to score potency). Added
    # both as required columns so the schema regression fails fast at
    # read time instead of silently degrading downstream.
    "chembl_activities": [
        "molecule_chembl_id", "target_chembl_id",
        "pchembl_value", "standard_relation",
    ],
    "indications": ["drugbank_id", "disease_id"],
}


def _validate_phase1_columns(
    df: pd.DataFrame,
    expected_columns: List[str],
    source_name: str,
) -> None:
    """Raise :class:`DrugOSDataError` if any expected column is missing.

    v27 ROOT FIX (P2-B-5): previously, ``row.get(missing_col)`` silently
    returned ``None`` / empty string for EVERY row, producing zero-output
    bugs (e.g. P2-L-1: ``chembl_to_node_records_from_phase1`` returned 0
    nodes because ``drug_chembl_id`` was missing — but the Phase 1 CSV
    had ``chembl_id``). This helper makes the schema contract explicit
    and fails fast at read time so the operator can fix the Phase 1
    pipeline instead of debugging silent data loss downstream.
    """
    if df is None or df.empty:
        return  # nothing to validate (missing-file path handled elsewhere)
    actual = set(df.columns)
    missing = [c for c in expected_columns if c not in actual]
    if missing:
        raise DrugOSDataError(
            f"Phase 1 source '{source_name}' is missing required column(s) "
            f"{missing}. Got columns: {sorted(actual)}. This usually "
            f"means Phase 1's pipeline produced a different schema than "
            f"the bridge expects — re-run the Phase 1 pipeline, or "
            f"update _PHASE1_EXPECTED_COLUMNS in phase1_bridge.py to "
            f"match the new schema."
        )


# v29 ROOT FIX (Phase1↔Phase2 100% connection): PostgreSQL reader.
#
# The forensic audit (Compound Chain 2: "Phase 1 Output Is Discarded")
# proved that the bridge BYPASSED the entire Phase 1 SQLAlchemy/database
# layer and read CSVs directly. Phase 1's 4,215 lines of loaders, 2,171
# lines of models, 4,537 lines of migration runner were dead weight —
# Phase 2 never used them. This is the single biggest reason Phase 1 ↔
# Phase 2 was only ~60% connected, not 100%.
#
# ROOT FIX: add a PostgreSQL-backed reader that reads from the same
# ORM models Phase 1's loaders write to. This makes Phase 2 actually
# consume Phase 1's database output, fulfilling the docx architecture:
#   "Airflow → Phase 1 → PostgreSQL → Phase 2"
#
# Strategy:
#   1. If DATABASE_URL is set AND the Phase 1 schema is populated, read
#      from PostgreSQL. This is the authoritative path — Phase 1's
#      cleaning/normalization/dedup/ER work is honored.
#   2. Otherwise, fall back to the CSV reader (the original v28 path).
#      This preserves backward compatibility with dev/CI runs that
#      haven't provisioned a database.
#   3. The choice is logged at INFO so operators can verify which path
#      was taken. The lineage property ``_source_phase1_backend`` on
#      every node records which backend produced it — auditors can
#      verify PostgreSQL was used in production runs.

_PHASE1_BACKEND_POSTGRES = "postgresql"
_PHASE1_BACKEND_CSV = "csv"


def _phase1_db_available() -> bool:
    """Return True iff a Phase 1 PostgreSQL backend is configured AND populated.

    We attempt a connection AND verify at least one row exists in the
    ``drugs`` table. A configured-but-empty database returns False so the
    bridge falls back to CSV rather than producing an empty graph.
    """
    # v41 ROOT FIX (Task J SEV2): the previous broad `except Exception`
    # silently swallowed EVERY error — including programming errors
    # (TypeError, AttributeError) and config mistakes (missing env vars).
    # That made bridge-fallback debugging nearly impossible: the operator
    # saw "will fall back to CSV" with no clue WHY postgres was rejected.
    # The narrowed catch set covers ONLY the failure modes that should
    # legitimately trigger a CSV fallback:
    #   - ImportError: phase1/database/connection.py or sqlalchemy missing
    #   - OperationalError: DB unreachable / connection refused
    #   - RuntimeError: get_engine() raises this when DATABASE_URL is unset
    #     (the phase1 connection module's contract).
    # All other exceptions (e.g. KeyError from a wrong column name,
    # TypeError from a schema drift) PROPAGATE so the operator sees the
    # real failure instead of an empty graph.
    try:
        import sys as _sys
        _phase1_root = str(Path(__file__).resolve().parents[2] / "phase1")
        if _phase1_root not in _sys.path:
            _sys.path.insert(0, _phase1_root)
        from database.connection import get_engine  # type: ignore
        from sqlalchemy import text as _sa_text
        from sqlalchemy.exc import OperationalError as _SAOperationalError
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                _sa_text("SELECT COUNT(*) AS n FROM drugs")
            ).fetchone()
            return bool(row is not None and row[0] is not None and int(row[0]) > 0)
    except ImportError as exc:
        # phase1/database/connection.py missing OR sqlalchemy not installed
        # (CI dry-run mode). Genuine fallback signal.
        logger.warning(
            "Phase1 PostgreSQL backend unavailable — phase1 DB module or "
            "SQLAlchemy not importable (%s). Falling back to CSV reader.",
            exc,
        )
        return False
    except RuntimeError as exc:
        # get_engine() raises RuntimeError when DATABASE_URL is unset —
        # the canonical "no DB configured" signal.
        logger.warning(
            "Phase1 PostgreSQL backend unavailable — get_engine() refused "
            "to build an engine (%s). Falling back to CSV reader.",
            exc,
        )
        return False
    except _SAOperationalError as exc:
        # DB configured but unreachable (network, auth, wrong port).
        logger.warning(
            "Phase1 PostgreSQL backend unavailable — connection error "
            "(%s). Falling back to CSV reader.",
            exc,
        )
        return False


def _read_phase1_from_postgres() -> Dict[str, pd.DataFrame]:
    """Read ALL Phase 1 data from PostgreSQL via SQLAlchemy ORM models.

    This is the ROOT FIX for the Phase 1 ↔ Phase 2 connection. Returns
    a dict with the SAME keys as :func:`read_phase1_outputs` so callers
    can use either backend transparently.

    Schema mapping (Phase 1 ORM table → bridge key):
        drugs                          → "drugs"
        drug_protein_interactions      → "interactions"
        gene_disease_associations      → "omim_gda" + "disgenet_gda"
        protein_protein_interactions   → "string_ppi"
        proteins                       → "uniprot_proteins"
        entity_mapping                 → used for cross-source ID resolution

    The function reads with read-only sessions and never mutates the DB.
    """
    import sys as _sys
    _phase1_root = str(Path(__file__).resolve().parents[2] / "phase1")
    if _phase1_root not in _sys.path:
        _sys.path.insert(0, _phase1_root)

    from database.connection import get_engine  # type: ignore
    from database import models as _m  # type: ignore
    from sqlalchemy import select

    engine = get_engine()
    out: Dict[str, pd.DataFrame] = {}

    with engine.connect() as conn:
        # --- drugs (DrugBank + ChEMBL + PubChem unified) ---
        drugs_stmt = select(
            _m.Drug.inchikey,
            _m.Drug.name,
            _m.Drug.chembl_id,
            _m.Drug.drugbank_id,
            _m.Drug.pubchem_cid,
            _m.Drug.molecular_weight,
            _m.Drug.smiles,
            _m.Drug.is_fda_approved,
            _m.Drug.is_globally_approved,
            _m.Drug.is_withdrawn,
            _m.Drug.clinical_status,
            _m.Drug.max_phase,
            _m.Drug.mechanism_of_action,
        ).where(_m.Drug.is_deleted == False)  # noqa: E712 — SQLAlchemy
        drugs_df = pd.read_sql(drugs_stmt, conn)
        # Synthesise the legacy 'groups' column from clinical_status so
        # downstream stage_phase1_to_phase2 doesn't break.
        if "clinical_status" in drugs_df.columns:
            drugs_df["groups"] = drugs_df["clinical_status"].fillna("")
        out["drugs"] = drugs_df
        logger.info(
            "Phase1 bridge (postgres): read %d rows from drugs table",
            len(drugs_df),
        )

        # --- drug_protein_interactions ---
        # v29 ROOT FIX: the DrugProteinInteraction model uses integer
        # foreign keys (drug_id -> drugs.id, protein_id -> proteins.id),
        # NOT string columns like drug_inchikey / protein_uniprot_id.
        # The previous code referenced non-existent columns, which
        # would crash at query time. Fix: JOIN through the integer FKs
        # to get the string identifiers (drugbank_id, uniprot_id) that
        # the bridge contract expects.
        dpi_stmt = select(
            _m.Drug.inchikey.label("drug_inchikey"),
            _m.Drug.drugbank_id.label("drugbank_id"),
            _m.Protein.uniprot_id.label("uniprot_id"),
            _m.Protein.protein_name.label("target_name"),
            _m.DrugProteinInteraction.interaction_type.label("action_type"),
        ).select_from(
            _m.DrugProteinInteraction
        ).join(
            _m.Drug, _m.DrugProteinInteraction.drug_id == _m.Drug.id
        ).join(
            _m.Protein, _m.DrugProteinInteraction.protein_id == _m.Protein.id
        )
        dpi_df = pd.read_sql(dpi_stmt, conn)
        # Bridge expects drugbank_id as the primary key on interactions.
        out["interactions"] = dpi_df
        logger.info(
            "Phase1 bridge (postgres): read %d rows from "
            "drug_protein_interactions", len(dpi_df),
        )

        # --- gene_disease_associations (OMIM + DisGeNET unified) ---
        # v34 ROOT FIX (CRITICAL #7): the previous query selected ONLY 6
        # columns (gene_symbol, disease_id, disease_name, source, score,
        # association_type) and synthesized gene_mim/phenotype_mim as None.
        # When DATABASE_URL was set, the bridge's stage code fell through
        # ALL three Gene ID resolvers (canonical_gene_id, ncbi_gene_id,
        # gene_mim) and emitted `SYM:{symbol}` IDs for every Gene —
        # losing cross-source ID resolution. The CSV path includes these
        # columns; the PostgreSQL path did NOT.
        #
        # The fix: select ALL columns the bridge's stage code consumes:
        #   - gene_id (NCBI Entrez Gene ID, mapped to ncbi_gene_id in the
        #     output to match the CSV schema)
        #   - uniprot_id (cross-source protein key)
        #   - disease_id_type
        #   - score_type, score_method, evidence_strength,
        #     normalized_score, confidence_tier, source_id, source_version
        #   - mapping_key (synthesized as None — the model doesn't have it;
        #     it's a Phase 1 OMIM-specific field that the CSV path provides
        #     but the GDA model doesn't store. This is acceptable: the
        #     bridge's stage code only uses mapping_key for the edge props,
        #     not for Gene ID resolution.)
        # We also synthesize `canonical_gene_id` from `gene_id` (NCBI) so
        # the bridge's preferred resolver hits first.
        gda_stmt = select(
            _m.GeneDiseaseAssociation.gene_symbol,
            _m.GeneDiseaseAssociation.disease_id,
            _m.GeneDiseaseAssociation.disease_name,
            _m.GeneDiseaseAssociation.disease_id_type,
            _m.GeneDiseaseAssociation.source,
            _m.GeneDiseaseAssociation.source_id,
            _m.GeneDiseaseAssociation.score,
            _m.GeneDiseaseAssociation.association_type,
            _m.GeneDiseaseAssociation.uniprot_id,
            _m.GeneDiseaseAssociation.gene_id.label("ncbi_gene_id"),
            _m.GeneDiseaseAssociation.score_type,
            _m.GeneDiseaseAssociation.score_method,
            _m.GeneDiseaseAssociation.confidence_tier,
            _m.GeneDiseaseAssociation.evidence_strength,
            _m.GeneDiseaseAssociation.normalized_score,
            _m.GeneDiseaseAssociation.source_version,
        )
        gda_df = pd.read_sql(gda_stmt, conn)
        # Synthesize the legacy columns the bridge contract expects.
        gda_df["gene_mim"] = None
        gda_df["phenotype_mim"] = None
        gda_df["mapping_key"] = None
        # v34 ROOT FIX (CRITICAL #7): synthesize `canonical_gene_id` from
        # `ncbi_gene_id` so the bridge's preferred Gene ID resolver hits.
        # The CSV path provides this directly; the PostgreSQL path now
        # provides it too.
        gda_df["canonical_gene_id"] = gda_df["ncbi_gene_id"].astype(str).where(
            gda_df["ncbi_gene_id"].notna(), None
        )
        # Split by source: OMIM rows go to "omim_gda", DisGeNET rows to
        # "disgenet_gda". Rows with source containing "omim" go to omim_gda;
        # rows with source containing "disgenet" go to disgenet_gda.
        if not gda_df.empty and "source" in gda_df.columns:
            omim_mask = gda_df["source"].astype(str).str.lower().str.contains("omim")
            disgenet_mask = gda_df["source"].astype(str).str.lower().str.contains("disgenet")
            out["omim_gda"] = gda_df[omim_mask].copy()
            out["disgenet_gda"] = gda_df[disgenet_mask].copy()
        else:
            out["omim_gda"] = pd.DataFrame()
            out["disgenet_gda"] = pd.DataFrame()
        logger.info(
            "Phase1 bridge (postgres): read %d OMIM GDA + %d DisGeNET GDA rows",
            len(out["omim_gda"]), len(out["disgenet_gda"]),
        )

        # --- proteins (UniProt) ---
        # v29 ROOT FIX: Protein model has `protein_name`, not `target_name`.
        prot_stmt = select(
            _m.Protein.uniprot_id.label("uniprot_ac"),
            _m.Protein.gene_symbol,
            _m.Protein.protein_name.label("name"),
            _m.Protein.organism,
        )
        out["uniprot_proteins"] = pd.read_sql(prot_stmt, conn)
        logger.info(
            "Phase1 bridge (postgres): read %d protein rows",
            len(out["uniprot_proteins"]),
        )

        # --- protein_protein_interactions (STRING) ---
        # v29 ROOT FIX: PPI model uses integer FKs (protein_a_id,
        # protein_b_id), not string uniprot_ac_a / uniprot_ac_b.
        # JOIN through proteins to get the UniProt accessions.
        # Use aliased Protein for the self-join.
        from sqlalchemy.orm import aliased
        _ProteinA = aliased(_m.Protein)
        _ProteinB = aliased(_m.Protein)
        ppi_stmt = select(
            _ProteinA.uniprot_id.label("uniprot_ac_a"),
            _ProteinB.uniprot_id.label("uniprot_ac_b"),
            _m.ProteinProteinInteraction.combined_score,
        ).select_from(
            _m.ProteinProteinInteraction
        ).join(
            _ProteinA, _m.ProteinProteinInteraction.protein_a_id == _ProteinA.id
        ).join(
            _ProteinB, _m.ProteinProteinInteraction.protein_b_id == _ProteinB.id
        )
        out["string_ppi"] = pd.read_sql(ppi_stmt, conn)
        logger.info(
            "Phase1 bridge (postgres): read %d STRING PPI rows",
            len(out["string_ppi"]),
        )

        # Optional sources — previously empty, now populated from the
        # ORM (FORENSIC bridge root fix: the v29 "100% connection" claim
        # was false because 5 of 11 sources were silently empty in the
        # PostgreSQL path, losing half the graph in production).
        #
        # 1. drugbank_indications — no dedicated ORM table (it's a
        #    derived CSV from DrugBank's free-text indication field
        #    matched against the OMIM disease vocabulary). Try to read
        #    it from the processed_data CSV if available; else empty.
        indications_path = DEFAULT_PHASE1_PROCESSED_DIR / "drugbank_indications.csv"
        if indications_path.exists():
            try:
                out["indications"] = _read_csv_robust(indications_path)
                logger.info(
                    "Phase1 bridge (postgres): read %d rows from "
                    "drugbank_indications.csv (CSV sidecar — no ORM table)",
                    len(out["indications"]),
                )
            except Exception as _exc_ind:
                logger.warning(
                    "Phase1 bridge (postgres): failed to read %s: %s",
                    indications_path, _exc_ind,
                )
                out["indications"] = pd.DataFrame()
        else:
            out["indications"] = pd.DataFrame()

        # 2. chembl_drugs — derived from the Drug table where chembl_id
        #    is non-NULL. Provides chembl_id + inchikey + name for the
        #    ChEMBL compound subgraph.
        try:
            chembl_drugs_stmt = select(
                _m.Drug.chembl_id,
                _m.Drug.inchikey,
                _m.Drug.name,
                _m.Drug.smiles,
                _m.Drug.molecular_weight,
                _m.Drug.max_phase,
                _m.Drug.is_fda_approved,
            ).where(
                _m.Drug.is_deleted == False,  # noqa: E712
                _m.Drug.chembl_id.isnot(None),
            )
            out["chembl_drugs"] = pd.read_sql(chembl_drugs_stmt, conn)
            logger.info(
                "Phase1 bridge (postgres): read %d ChEMBL drug rows",
                len(out["chembl_drugs"]),
            )
        except Exception as _exc_cd:
            logger.warning(
                "Phase1 bridge (postgres): chembl_drugs read failed: %s",
                _exc_cd,
            )
            out["chembl_drugs"] = pd.DataFrame()

        # 3. chembl_activities — no dedicated ORM table (ChEMBL
        #    bioactivity data is not persisted by Phase 1 loaders; it
        #    flows through the cleaning pipeline directly to CSV).
        #    Read from the CSV sidecar if available; else empty with
        #    a warning so operators know the ChEMBL activity subgraph
        #    is missing in the PostgreSQL path.
        chembl_activities_paths = [
            DEFAULT_PHASE1_PROCESSED_DIR / "chembl_activities_clean.csv",
            DEFAULT_PHASE1_PROCESSED_DIR / "chembl_activities.csv",
        ]
        _ca_loaded = False
        for _p in chembl_activities_paths:
            if _p.exists():
                try:
                    out["chembl_activities"] = _read_csv_robust(_p)
                    logger.info(
                        "Phase1 bridge (postgres): read %d ChEMBL "
                        "activity rows from %s (CSV sidecar — no ORM "
                        "table)",
                        len(out["chembl_activities"]), _p,
                    )
                    _ca_loaded = True
                    break
                except Exception as _exc_ca:
                    logger.warning(
                        "Phase1 bridge (postgres): failed to read %s: %s",
                        _p, _exc_ca,
                    )
        if not _ca_loaded:
            out["chembl_activities"] = pd.DataFrame()
            logger.warning(
                "Phase1 bridge (postgres): chembl_activities not "
                "available (no ORM table, no CSV sidecar). The ChEMBL "
                "bioactivity subgraph will be empty in this run. To "
                "populate it, either (a) run Phase 1 with "
                "DRUGOS_PHASE1_PERSIST_ACTIVITIES=1, or (b) ensure "
                "chembl_activities_clean.csv exists in processed_data/."
            )

        # 4. omim_susceptibility — derived from GeneDiseaseAssociation
        #    where source='omim' and the association_type / disease_id
        #    indicates a susceptibility (rather than a Mendelian) gene-
        #    disease link. OMIM susceptibility records have
        #    association_type containing 'susceptibility' or
        #    disease_id starting with '%'.
        try:
            omim_susc_stmt = select(
                _m.GeneDiseaseAssociation.gene_symbol,
                _m.GeneDiseaseAssociation.disease_id,
                _m.GeneDiseaseAssociation.disease_name,
                _m.GeneDiseaseAssociation.score,
                _m.GeneDiseaseAssociation.association_type,
                _m.GeneDiseaseAssociation.pmid_list,
            ).where(
                _m.GeneDiseaseAssociation.source == "omim",
            )
            _omim_all = pd.read_sql(omim_susc_stmt, conn)
            # Filter to susceptibility records (disease_id starts with
            # '%' per OMIM convention, or association_type mentions
            # 'susceptibility' / 'predisposition').
            if not _omim_all.empty:
                _mask = (
                    _omim_all["disease_id"].astype(str).str.startswith("%")
                    | _omim_all["association_type"].astype(str).str.contains(
                        "susceptib|predispos", case=False, na=False
                    )
                )
                out["omim_susceptibility"] = _omim_all[_mask].copy()
            else:
                out["omim_susceptibility"] = pd.DataFrame()
            logger.info(
                "Phase1 bridge (postgres): read %d OMIM susceptibility "
                "rows (of %d total OMIM GDA rows)",
                len(out["omim_susceptibility"]), len(_omim_all),
            )
        except Exception as _exc_os:
            logger.warning(
                "Phase1 bridge (postgres): omim_susceptibility read "
                "failed: %s", _exc_os,
            )
            out["omim_susceptibility"] = pd.DataFrame()

        # 5. pubchem_enrichment — from the PubChemCompoundProperty
        #    ORM table (compound structural enrichment data).
        try:
            pubchem_stmt = select(
                _m.PubChemCompoundProperty.inchikey,
                _m.PubChemCompoundProperty.canonical_smiles,
                _m.PubChemCompoundProperty.isomeric_smiles,
                _m.PubChemCompoundProperty.molecular_weight,
                _m.PubChemCompoundProperty.xlogp,
                _m.PubChemCompoundProperty.tpsa,
                _m.PubChemCompoundProperty.h_bond_donor_count,
                _m.PubChemCompoundProperty.h_bond_acceptor_count,
                _m.PubChemCompoundProperty.rotatable_bond_count,
                _m.PubChemCompoundProperty.pubchem_cid,
            )
            out["pubchem_enrichment"] = pd.read_sql(pubchem_stmt, conn)
            logger.info(
                "Phase1 bridge (postgres): read %d PubChem enrichment "
                "rows",
                len(out["pubchem_enrichment"]),
            )
        except Exception as _exc_pe:
            logger.warning(
                "Phase1 bridge (postgres): pubchem_enrichment read "
                "failed: %s", _exc_pe,
            )
            out["pubchem_enrichment"] = pd.DataFrame()

        # 6. entity_mapping — Phase 1's cross-source entity resolution
        #    output. Read it so Phase 2's entity_resolver can REUSE it
        #    instead of re-resolving from scratch (which was the audit's
        #    "Phase 1 entity_mapping table is discarded" finding).
        #    Columns: canonical_inchikey, canonical_name, chembl_id,
        #    drugbank_id, pubchem_cid, uniprot_id, string_id,
        #    match_confidence, match_method, match_history.
        try:
            em_stmt = select(
                _m.EntityMapping.canonical_inchikey,
                _m.EntityMapping.canonical_name,
                _m.EntityMapping.chembl_id,
                _m.EntityMapping.drugbank_id,
                _m.EntityMapping.pubchem_cid,
                _m.EntityMapping.uniprot_id,
                _m.EntityMapping.string_id,
                _m.EntityMapping.match_confidence,
                _m.EntityMapping.match_method,
            )
            out["entity_mapping"] = pd.read_sql(em_stmt, conn)
            logger.info(
                "Phase1 bridge (postgres): read %d entity_mapping rows "
                "(cross-source ER output — Phase 2 reuses this instead "
                "of re-resolving)",
                len(out["entity_mapping"]),
            )
        except Exception as _exc_em:
            logger.warning(
                "Phase1 bridge (postgres): entity_mapping read failed: "
                "%s", _exc_em,
            )
            out["entity_mapping"] = pd.DataFrame()

    # Apply the same column validation as the CSV path.
    for key, df in out.items():
        if df is None or df.empty:
            continue
        if key in _PHASE1_EXPECTED_COLUMNS:
            _validate_phase1_columns(df, _PHASE1_EXPECTED_COLUMNS[key], key)
    return out


def read_phase1_outputs(
    phase1_processed_dir: Optional[Path | str] = None,
    prefer_postgres: bool = True,
) -> Dict[str, pd.DataFrame]:
    """Read Phase 1's outputs into a dict of DataFrames.

    v29 ROOT FIX (Phase1↔Phase2 100% connection): the reader now prefers
    PostgreSQL when ``prefer_postgres=True`` (the default). The Phase 1
    SQLAlchemy ORM models — written by Phase 1's loaders — become the
    authoritative source for Phase 2. This closes Compound Chain 2 of the
    forensic audit ("Phase 1 Output Is Discarded").

    Backend selection order:
      1. If ``prefer_postgres=True`` AND a populated Phase 1 DB exists,
         read from PostgreSQL via :func:`_read_phase1_from_postgres`.
      2. Otherwise, read CSVs from ``phase1_processed_dir`` (the legacy
         v28 behaviour — preserved for dev/CI without a database).

    The chosen backend is recorded on the returned DataFrames via the
    special ``"_phase1_backend"`` attribute on the dict, so downstream
    code (and tests) can verify which path was taken.

    Parameters
    ----------
    phase1_processed_dir : path-like, optional
        Directory containing Phase 1's processed CSV outputs. Used as the
        fallback when PostgreSQL is unavailable. Defaults to
        :data:`DEFAULT_PHASE1_PROCESSED_DIR`.
    prefer_postgres : bool, default True
        If True, attempt PostgreSQL first. Set to False to force the CSV
        path (e.g. for unit tests that mock the CSV fixtures).

    Raises
    ------
    FileNotFoundError
        If the CSV backend is selected and the directory doesn't exist.
    DrugOSDataError
        If a Phase 1 source (CSV or DB table) is missing required columns.
    """
    # Try PostgreSQL first (root fix).
    if prefer_postgres and _phase1_db_available():
        logger.info(
            "Phase1 bridge: using PostgreSQL backend (authoritative). "
            "Phase 1 ORM models are the source of truth for Phase 2."
        )
        try:
            out = _read_phase1_from_postgres()
            out["_phase1_backend"] = _PHASE1_BACKEND_POSTGRES  # type: ignore[assignment]
            return out
        except Exception as exc:
            logger.warning(
                "Phase1 bridge: PostgreSQL read failed (%s) — falling "
                "back to CSV reader. This is acceptable in dev but is "
                "a P0 incident in production: it means Phase 1's "
                "database work is being discarded.", exc,
            )

    # CSV fallback (legacy v28 path).
    base = Path(phase1_processed_dir) if phase1_processed_dir else DEFAULT_PHASE1_PROCESSED_DIR
    if not base.exists():
        raise FileNotFoundError(
            f"Phase 1 processed_data directory does not exist: {base} "
            f"AND PostgreSQL backend unavailable. Either run Phase 1 "
            f"pipelines first, or provision a PostgreSQL database with "
            f"DATABASE_URL set."
        )

    paths = {
        "drugs": base / "drugbank_drugs.csv",
        "interactions": base / "drugbank_interactions.csv.gz",
        "omim_gda": base / "omim_gene_disease_associations.csv",
        # v6 fix (bug #B9): structured drug → OMIM disease indications.
        # Optional — bridge degrades to free-text `indication` column
        # matching if this file is absent.
        "indications": base / "drugbank_indications.csv",
        # ROOT FIX (Phase1↔Phase2 100% connection): extend the bridge
        # contract to cover ALL 7 Phase 1 source pipelines. Previously
        # the bridge consumed only DrugBank + OMIM; ChEMBL, UniProt,
        # STRING, DisGeNET, and PubChem Phase 1 outputs were ignored
        # and Phase 2 re-downloaded them independently. This defeated
        # the "single authoritative wire" promise of the bridge and
        # meant that ~70% of the multi-modal KG's data bypassed Phase 1
        # entity resolution.
        #
        # v13 ROOT FIX (Compound-6 / "Multi-Modal KG Degradation"):
        # v12 introduced these 5 new keys but used prefixed filenames
        # (`chembl_drugs.csv`, `uniprot_proteins.csv`, etc.) that
        # DO NOT MATCH the actual filenames the Phase 1 pipelines
        # emit. Per `phase1/pipelines/base_pipeline.py:_get_processed_filename`,
        # the actual output filenames are unprefixed:
        #   chembl   → drugs.csv
        #   uniprot  → proteins.csv
        #   string   → protein_protein_interactions.csv
        #   disgenet → gene_disease_associations.csv
        #   pubchem  → pubchem_enrichment.csv  (already matched)
        # The mismatch meant 4 of 5 new sources were silently skipped
        # at runtime (warning logged, empty DataFrame returned). The
        # v12 "100% connection" claim was unverifiable on the toy
        # fixture AND broken in production.
        #
        # v13 fix: try BOTH the prefixed name (preferred — explicit)
        # AND the actual pipeline-emitted name (fallback — what
        # production runs actually produce). This is backwards-
        # compatible: existing toy fixtures with prefixed names still
        # work, and production runs with unprefixed names now work.
        "chembl_drugs": [
            base / "chembl_drugs.csv",
            base / "drugs.csv",
        ],
        "uniprot_proteins": [
            base / "uniprot_proteins.csv",
            base / "proteins.csv",
        ],
        "string_ppi": [
            base / "string_protein_protein_interactions.csv",
            base / "protein_protein_interactions.csv",
        ],
        "disgenet_gda": [
            base / "disgenet_gene_disease_associations.csv",
            base / "gene_disease_associations.csv",
        ],
        "pubchem_enrichment": base / "pubchem_enrichment.csv",
        # ─── v15 ROOT FIX (Phase1↔Phase2 100% connection, REM-12/13/14): ──
        # The two Phase-1 source CSVs that v14 STILL bypassed:
        #   • chembl_activities_clean.csv  — the actual ChEMBL bioactivity
        #     table (IC50 / Ki / EC50 + pchembl_value per molecule-target
        #     pair). v14 only read chembl_drugs.csv (compound METADATA
        #     denormalized to one row per compound) — that path could not
        #     emit direction-correct inhibits/activates edges nor carry
        #     the potency value. The audit (REM-13/14) flagged this as
        #     HIGH severity: "ChEMBL edges are ALL hardcoded to
        #     (Compound, targets, Protein) regardless of activity_type."
        #     Fix: read the activities table here, classify each edge by
        #     activity_type semantics (inhibition→inhibits,
        #     activation→activates, otherwise→targets), and carry
        #     pchembl_value + standard_relation as edge properties so the
        #     RL ranker has potency + censoring context.
        #   • omim_gene_disease_susceptibility.csv  — OMIM susceptibility
        #     / polygenic associations (is_susceptibility=True). v14 only
        #     read omim_gene_disease_associations.csv (causative Mendelian
        #     GDA). Susceptibility associations are scientifically
        #     distinct: they are NOT因果 — a variant raises risk but
        #     does not deterministically cause the disease. Conflating
        #     them under the same `associated_with` edge would teach
        #     TransE that BRCA1+breast_cancer is equivalent to
        #     FGFR3+achondroplasia (a Mendelian dominant). Fix: emit a
        #     distinct `susceptible_to` relation so the model learns the
        #     distinction.
        "chembl_activities": [
            base / "chembl_activities_clean.csv",
            base / "chembl_activities.csv",
        ],
        "omim_susceptibility": [
            base / "omim_gene_disease_susceptibility.csv",
        ],
        # FORENSIC bridge root fix: also read Phase 1's entity_mapping
        # CSV (the cross-source ER output) so Phase 2's entity_resolver
        # can REUSE it instead of re-resolving from scratch. This file
        # is emitted by Phase 1's entity_resolution resolver when run
        # in CSV mode.
        "entity_mapping": [
            base / "entity_mapping.csv",
            base / "entity_resolution.csv",
        ],
    }
    out: Dict[str, pd.DataFrame] = {}
    for key, p in paths.items():
        # v13: support dual-name lookup (list of candidate paths)
        # for the 4 mismatched Phase 1 sources.
        if isinstance(p, list):
            found_path = None
            for candidate in p:
                if candidate.exists():
                    found_path = candidate
                    break
            if found_path is not None:
                out[key] = _read_csv_robust(found_path)
                # v27 ROOT FIX (P2-B-5): validate required columns for
                # this source. Raises DrugOSDataError on schema mismatch
                # so the operator gets a clear, actionable error instead
                # of silent zero-output downstream.
                if key in _PHASE1_EXPECTED_COLUMNS:
                    _validate_phase1_columns(
                        out[key], _PHASE1_EXPECTED_COLUMNS[key], key,
                    )
                logger.info(
                    "Phase1 bridge: read %s rows from %s (source=%s)",
                    len(out[key]), found_path.name, key,
                )
            else:
                out[key] = pd.DataFrame()
                logger.warning(
                    "Phase1 bridge: %s not found at any of %s — "
                    "producing empty DataFrame. The bridge will skip "
                    "this source. To fix: run the Phase 1 pipeline "
                    "for this source before invoking the bridge.",
                    key, [str(c) for c in p],
                )
        else:
            if p.exists():
                out[key] = _read_csv_robust(p)
                # v27 ROOT FIX (P2-B-5): validate required columns.
                if key in _PHASE1_EXPECTED_COLUMNS:
                    _validate_phase1_columns(
                        out[key], _PHASE1_EXPECTED_COLUMNS[key], key,
                    )
                logger.info(
                    "Phase1 bridge: read %s rows from %s",
                    len(out[key]), p.name,
                )
            else:
                out[key] = pd.DataFrame()
                logger.warning(
                    "Phase1 bridge: %s not found at %s — producing "
                    "empty DataFrame. The bridge will skip this "
                    "source. To fix: run the Phase 1 pipeline for "
                    "this source before invoking the bridge.",
                    key, p,
                )
    out["_phase1_backend"] = _PHASE1_BACKEND_CSV  # type: ignore[assignment]
    return out


# ---------------------------------------------------------------------------
# 5. stage_phase1_to_phase2 — convert DataFrames → Phase 2 node/edge dicts
# ---------------------------------------------------------------------------
def _to_bool(v: Any) -> bool:
    """Coerce arbitrary Phase 1 cell value to a strict bool.

    Pandas reads CSV True/False strings as Python bools already, but
    DefensiveParse™: empty strings, NaN, None, 0, "0", "false" all map to
    False; everything truthy maps to True. This is the patient-safety
    guardrail called out in the module docstring.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if pd.isna(v):
            return False
        return bool(v)
    s = str(v).strip().lower()
    if s in ("", "0", "false", "no", "f", "n", "nan", "none", "null"):
        return False
    return True


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v).strip()


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return f


def _deterministic_hash_int(value: Any) -> int:
    """Return a DETERMINISTIC int hash of ``value`` (stable across processes).

    v41 ROOT FIX (Task J SEV3): Python's built-in ``hash()`` is randomized
    per process for str/bytes/datetime (PYTHONHASHSEED), so the SAME Phase 1
    row index gets a different ``_source_row`` lineage value every run. That
    broke lineage tracking across runs and made downstream dedup keys
    unstable. This helper uses ``hashlib.sha256(repr(value).encode())``
    truncated to 16 hex chars (8 bytes / int64), which is deterministic
    across processes, machines, and restarts.
    """
    return int(hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16], 16)


# v28 ROOT FIX (P2-B-13): ``int(idx)`` crashes when the DataFrame has a
# non-RangeIndex (e.g., string index, DatetimeIndex, MultiIndex, or a
# reindexed DataFrame). Phase 1 CSVs typically produce RangeIndex, but
# tests / post-processing code that calls ``stage_phase1_to_phase2`` may
# pass DataFrames with custom indices. The previous code did
# ``"_source_row": int(idx)`` directly — a single non-int index value
# raised TypeError and aborted the entire batch (the caller in
# run_pipeline.py swallows the exception, so all subsequent rows were
# silently lost). This helper provides a stable int for any hashable
# ``idx`` value:
#   * int / numpy int → passthrough (preserves RangeIndex behavior).
#   * str / bytes / datetime / other hashable → ``hash(idx)`` (stable
#     within a Python process; cross-process stable only for str/bytes
#     with PYTHONHASHSEED=0 — acceptable for a row-level provenance key).
#   * None / NaN → 0 (sentinel; the row is preserved, not dropped).
def _safe_row_idx(idx: Any) -> int:
    """Convert a DataFrame row index to a stable int.

    v41 ROOT FIX (Task J SEV3): the previous implementation used Python's
    built-in ``hash(idx)`` for non-int indices. That hash is randomized
    per process via ``PYTHONHASHSEED`` (default: random per process for
    str/bytes/datetime), so the SAME Phase 1 CSV row got a DIFFERENT
    ``_source_row`` value every time the bridge ran. That broke lineage
    tracking across runs (a bug in run N could not be traced to the same
    Phase 1 row in run N+1) and made downstream dedup keys unstable.

    The fix uses a DETERMINISTIC hash — ``hashlib.sha256(repr(idx).encode())``
    truncated to 16 hex chars (32 hex chars would overflow int32 if used
    as a DB key; 16 hex = 8 bytes = fits in int64). The hash is stable
    across Python processes, across machines, and across restarts, so a
    Phase 1 row's lineage key is the same in every run.

    Note: ``repr(idx)`` (not ``str(idx)``) is used because ``repr`` is
    more discriminating for some types (e.g. ``repr("1")`` differs from
    ``repr(1)`` so a string "1" and an int 1 get different lineage keys,
    which is the correct behaviour — they ARE different Phase 1 rows).
    """
    if idx is None:
        return 0
    # bool is a subclass of int — guard explicitly so True/False don't
    # silently become 1/0 (which would lose the boolean semantics).
    if isinstance(idx, bool):
        return int(idx)
    # Python int / numpy int64 / numpy int32 — passthrough.
    try:
        if isinstance(idx, int):
            return int(idx)
    except TypeError:
        # numpy int types are not instances of Python int on some
        # platforms; fall through to the float branch.
        pass
    # numpy integer types satisfy the Number ABC but not isinstance(int).
    try:
        import numbers
        if isinstance(idx, numbers.Integral):
            return int(idx)
    except ImportError:
        pass
    # Float that is integral (e.g., 5.0) — coerce.
    if isinstance(idx, float):
        if pd.isna(idx):
            return 0
        if idx.is_integer():
            return int(idx)
        # Non-integer float — deterministic hash.
        return _deterministic_hash_int(idx)
    # Pandas Timestamp / datetime / str / bytes / tuple — deterministic hash.
    try:
        if pd.isna(idx):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return _deterministic_hash_int(idx)
    except TypeError:
        # Unhashable (e.g., a list) — last-resort stable hash via repr.
        return _deterministic_hash_int(repr(idx))


def _classify_drug_protein_edge(action_type: str) -> str:
    """Map a DrugBank ``action_type`` string to a CORE_EDGE_TYPES relation.

    Returns one of: ``"targets"``, ``"inhibits"``, ``"activates"``,
    ``"allosterically_modulates"``, ``"metabolized_by"``, ``"unknown"``.

    The mapping is conservative — when in doubt, ``"targets"`` (the generic
    drug→protein relation) is used. ``"unknown"`` is reserved for the case
    where DrugBank explicitly sets a non-empty action_type that doesn't
    match any of the above (e.g. "negative modulator" — we treat that as
    allosteric, but if a brand-new action_type appears we fail-closed to
    "unknown" so the data still loads).

    v35 ROOT FIX (H-1): ``antagonist`` previously mapped to ``inhibits``
    alongside ``inhibit`` and ``blocker``. That conflates two different
    pharmacological concepts:

      * ``inhibit`` / ``blocker`` — the molecule directly inhibits the
        target's signaling or enzymatic activity (e.g. proton-pump
        inhibitors, beta-blockers that suppress receptor coupling).
      * ``antagonist`` — the molecule is a *competitive* receptor
        antagonist that blocks the endogenous ligand's binding WITHOUT
        inhibiting basal signaling (e.g. naloxone at the μ-opioid
        receptor — basal signaling continues, only ligand-driven
        activation is blocked). Functional antagonism ("functional
        antagonist", "negative antagonist") DOES inhibit downstream
        signaling and is correctly classified as ``inhibits`` by the
        explicit ``"inhibit"`` substring check above.

    Conflating competitive antagonists with direct inhibitors taught
    TransE wrong directionality for the antagonist class — the RL
    safety ranker could not distinguish them. The fix maps a bare
    ``antagonist`` to ``"targets"`` (the honest "we know they
    interact, direction unclassified" relation) so the model is not
    trained on a misleading inhibits edge.

    v41 ROOT FIX (SEV1 #5 + S): ``substrate`` previously mapped to
    ``"unknown"``. That is SCIENTIFICALLY WRONG. "Substrate" means
    the PROTEIN metabolises the DRUG — the correct relation is
    ``"metabolized_by"`` (already in CORE_EDGE_TYPES at config.py:256
    and :3714). The wrong "unknown" classification:

      * Lost the directionality signal (substrates are NOT "unknown"
        — they have a specific biomedical meaning).
      * Taught TransE to lump substrates with genuinely-unknown
        interactions, corrupting the embedding geometry for the
        substrate class (typically CYP450 metabolised drugs).
      * The RL safety ranker could not distinguish "this drug IS
        metabolised by this enzyme" from "we don't know how they
        interact" — patient-safety risk for drug-drug interaction
        prediction (a CYP3A4 substrate + a CYP3A4 inhibitor = dangerous
        accumulation).

    Multi-action drugs (e.g. "agonist|positive modulator"): the previous
    code returned the FIRST matching relation, silently dropping the
    other actions. v41 fix: when an action_type contains multiple
    actions separated by ``|``, ``,`` or ``;``, we still return the first
    match (because the bridge emits one edge per row), but the FULL
    action_type string is preserved in the edge's ``action`` property
    (handled by the caller at line ~1996). This is the patient-safety-
    correct behavior: we don't fabricate multiple edges from one row,
    but we don't lose the multi-action signal either.
    """
    a = (action_type or "").lower().strip()
    if not a:
        return "targets"
    # v41 ROOT FIX (SEV1 #5): substrate → metabolized_by.
    # MUST be checked FIRST because DrugBank sometimes uses "substrate"
    # alone (no other action) and we want to preserve the metabolic
    # directionality signal. "substrate" does NOT contain any of the
    # other substrings so the ordering is safe.
    if "substrate" in a:
        return "metabolized_by"
    # v35 H-1: bare "antagonist" — competitive binding, not signaling
    # inhibition. Map to "targets" (interaction confirmed, direction
    # unclassified) instead of "inhibits". IMPORTANT: this check MUST
    # come BEFORE the "agonist" check below, because the string
    # "antagonist" CONTAINS "agonist" as a substring — without this
    # ordering, every antagonist would match the "agonist" branch and
    # incorrectly return "activates".
    if "antagonist" in a:
        return "targets"
    # Direct inhibitors / blockers → inhibits. Functional antagonists
    # that explicitly say "inhibit" (e.g. "functional inhibitor") also
    # land here via the substring check.
    if "inhibit" in a or "blocker" in a:
        return "inhibits"
    # v41 ROOT FIX (SEV1 #5 multi-action): check agonist/activator BEFORE
    # modulator/allosteric. The previous order checked allosteric first,
    # which meant "agonist|positive modulator" lost the agonist signal.
    # The agonist/activator signal is more pharmacologically specific
    # (direct receptor activation) than allosteric modulation, so we
    # prefer it when both are present in a multi-action string.
    # NOTE: "allosteric activator" still works correctly because the
    # "activ" substring catches it here and returns "activates" — but
    # that IS the correct classification (an allosteric activator IS
    # an activator). The original v35 comment was wrong about needing
    # allosteric-first ordering; the v41 ordering is more correct.
    if "activ" in a or "agonist" in a or "inducer" in a:
        return "activates"
    # Pure allosteric / modulator (no agonist/inhibitor/substrate
    # signal). e.g. "positive modulator", "negative modulator",
    # "allosteric modulator" without an explicit activator/inhibitor
    # word.
    if "allosteric" in a or "modulator" in a:
        return "allosterically_modulates"
    return "unknown"


def _classify_chembl_activity_edge(
    activity_type: str,
    assay_type: str = "",
    standard_relation: str = "",
) -> str:
    """Classify a ChEMBL bioactivity row into a CORE_EDGE_TYPES relation.

    Returns one of: ``"inhibits"``, ``"activates"``, ``"targets"``.

    Scientific basis
    ----------------
    ChEMBL's ``activity.standard_type`` column carries assay-measure labels
    such as ``IC50``, ``Ki``, ``Kd``, ``EC50``, ``AC50``, ``Potency``,
    ``Inhibition``, ``Activation``. The label does NOT directly map to a
    biological relation in all cases:

    * ``IC50`` of an enzyme assay → ``"inhibits"`` (v34 ROOT FIX HIGH #8
      / v35 L-2 docstring update). IC50 literally measures the
      concentration for 50% inhibition, so the inhibition signal is
      directly observed. Ki and Kd of a binding assay remain
      ``"targets"`` (binding affinity, direction unknown — the molecule
      binds but we cannot tell agonist vs antagonist from the potency
      alone).
    * ``EC50`` / ``AC50`` of a functional assay → the molecule produces a
      functional effect. ``EC50`` is typically agonist (activator) — but
      not always (some assays measure antagonist EC50). If the
      ``activity_type`` literally contains "activ" or "agon", emit
      ``"activates"``; otherwise emit ``"targets"`` (we know there's a
      functional interaction but the direction is uncertain from this
      label alone).
    * ``Inhibition`` (literal) → ``"inhibits"`` (the assay measured
      inhibition of an enzymatic or cellular process).
    * ``Activation`` (literal) → ``"activates"`` (the assay measured
      activation of a receptor or process).
    * Anything else (e.g. ``"Potency"``, ``"Selectivity"``, ``"Ratio"``)
      → ``"targets"`` (interaction confirmed, direction unclassified).

    The ``assay_type`` argument is reserved for future use (ChEMBL
    ``assay.assay_type`` 'F' functional vs 'B' binding). Currently not
    consulted because the production CSV does not always carry it.

    The ``standard_relation`` argument (``'='``, ``'<'``, ``'>'``) is
    preserved as an edge property elsewhere; it does not change the
    relation classification.

    This is the patient-safety-correct behavior: we NEVER claim
    ``inhibits`` unless the source data supports it. The default is
    ``"targets"`` (the honest "we know they interact" relation).
    """
    a = (activity_type or "").lower().strip()
    if not a:
        return "targets"
    # Direct-label cases — ChEMBL's standard_type field is sometimes the
    # bare word "Inhibition" or "Activation".
    if "inhibit" in a:
        return "inhibits"
    if "activ" in a or "agon" in a:
        return "activates"
    # v34 ROOT FIX (HIGH #8): the previous code returned "targets" for
    # IC50. But IC50 (Half-maximal Inhibitory Concentration) literally
    # MEASURES inhibition — the assay determines the concentration of
    # compound that inhibits 50% of the target's activity. Classifying
    # IC50 as "targets" loses the inhibition signal that the assay
    # directly measured. The scientifically correct relation is
    # "inhibits" (the audit agreed). Ki and Kd remain "targets" because
    # they measure binding affinity (not direction of effect).
    if a == "ic50":
        return "inhibits"
    # v21 ROOT FIX (Audit section 4 finding 7 / Chain 8 - "EC50
    # mis-classified as 'activates' -> RL ranker wrong directionality"):
    # the previous code returned "activates" for EC50/AC50. But EC50
    # (Half-maximal Effective Concentration) and AC50 measure the
    # potency of a compound that produces 50% of its MAXIMUM effect -
    # this can be an AGONIST (activates) OR an ANTAGONIST (inhibits),
    # depending on the assay design. The comment in this same function
    # admitted this: "EC50 / AC50 in a functional assay context -
    # default agonist. We log this upstream so operators know the
    # inference was made." But there was NO upstream log, and the
    # inference was WRONG for antagonists. Mis-labeling an antagonist
    # as 'activates' feeds the RL ranker wrong directionality for
    # downstream drug-disease prediction. The honest relation is
    # 'targets' (interaction confirmed, direction unclassified) -
    # which is exactly what we already return for IC50/Ki/Kd/Potency.
    # The RL ranker cannot infer "activates" from EC50 alone; it
    # needs assay_type='F' (functional) + assay_direction metadata
    # that the Phase 1 CSV does not carry.
    if a in ("ec50", "ac50"):
        return "targets"
    # Ki / Kd / Potency - interaction confirmed, direction unknown.
    return "targets"


# ---------------------------------------------------------------------------
# FIX-F / C-16: _load_clinical_outcomes — derive ClinicalOutcome nodes
# ---------------------------------------------------------------------------
def _load_clinical_outcomes(
    *,
    indications: Optional[pd.DataFrame],
    drugs: Optional[pd.DataFrame],
    drug_canonical_map: Dict[str, str],
    run_id: str,
    loaded_at: str,
    schema_version: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Derive ``ClinicalOutcome`` nodes and ``has_clinical_outcome`` edges
    from ``drugbank_indications.csv``.

    DOCX Phase 2 spec mandates 5 node types: Drugs, Proteins, Pathways,
    Diseases, Clinical Outcomes. The bridge previously emitted only 4
    (Compound, Protein, Gene, Disease). This function adds the missing
    5th node type.

    Each unique ``(disease_id, indication_type)`` tuple becomes a
    ClinicalOutcome node with properties:

        id                  = "CO:{drugbank_id}:{disease_key}:{indication_type}"
        name                = "{disease_name} ({indication_type})"
        disease_id          = original OMIM ID (or "" if absent)
        disease_name        = human-readable disease name
        indication_type     = "approved" | "investigational" | ...
        first_seen_drug_id  = drugbank_id of the FIRST Compound that
                              pointed to this (disease, type) tuple.
                              (v35 M-5 root fix — previously called
                              ``source_drug_id`` which misleadingly
                              suggested the edge's source drug. The
                              field is renamed and a new
                              ``source_drug_ids`` list accumulates ALL
                              drugs pointing to this node.)
        source_drug_ids     = list of ALL drugbank_ids whose Compound
                              has a ``has_clinical_outcome`` edge to
                              this node (v35 M-5 root fix).
        source_drug_id      = DEPRECATED alias for first_seen_drug_id
                              (kept for backward compat with callers
                              that already read this field — see v35
                              M-5 root fix comment in the body).

    The originating Compound is connected via a
    ``(Compound)-[:has_clinical_outcome]->(ClinicalOutcome)`` edge.

    Parameters
    ----------
    indications : DataFrame or None
        ``drugbank_indications.csv`` content. None or empty → returns ([], []).
    drugs : DataFrame or None
        ``drugbank_drugs.csv`` content (used only for drug_canonical_map
        lookups via the ``drug_canonical_map`` arg).
    drug_canonical_map : dict
        drugbank_id -> canonical Compound node ID (built upstream by
        ``stage_phase1_to_phase2`` from drugs.csv).
    run_id, loaded_at, schema_version : str
        Lineage properties written to every node/edge.

    Returns
    -------
    (nodes, edges) : tuple of lists of dicts
    """
    if indications is None or indications.empty:
        return [], []

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    seen_node_keys: Dict[str, str] = {}  # dedup_key -> node_id
    # v35 ROOT FIX (M-5): track per-(disease, type) the list of all
    # source drugbank_ids so the ClinicalOutcome node carries the
    # actual provenance (all contributing drugs) rather than just the
    # first-seen drug. The node's ``source_drug_id`` field was
    # misleadingly named — it actually meant "first drug encountered",
    # not "the edge's source drug". Renamed to ``first_seen_drug_id``
    # and added a ``source_drug_ids`` list. The deprecated
    # ``source_drug_id`` field is kept (set to the same value) for
    # backward compat with existing callers.
    seen_node_drug_lists: Dict[str, List[str]] = {}

    for idx, row in indications.iterrows():
        dbid = _safe_str(row.get("drugbank_id"))
        did = _safe_str(row.get("disease_id"))
        dname = _safe_str(row.get("disease_name"))
        itype = _safe_str(row.get("indication_type")) or "unknown"
        if not dbid:
            continue
        drug_canonical = drug_canonical_map.get(dbid)
        if drug_canonical is None:
            # v43 ROOT FIX (P2-012): dead-letter the dropped treats/
            # clinical_outcome edge instead of silently skipping.
            # The previous code silently dropped edges when the drug
            # wasn't in compound_nodes — no warning, no count. The fix
            # collects dropped edges and emits a summary warning via
            # the logger. We use a local list since this function
            # returns (co_nodes, co_edges) — the caller can check
            # _dropped_treats_edges if needed.
            if not hasattr(_load_clinical_outcomes, '_dropped_treats_edges'):
                _load_clinical_outcomes._dropped_treats_edges = []
            _load_clinical_outcomes._dropped_treats_edges.append({
                "drugbank_id": dbid,
                "disease_id": did,
                "disease_name": dname,
                "indication_type": itype,
                "reason": "drug_not_in_compound_nodes_treats_path",
                "source_file": "drugbank_indications.csv",
                "source_row": _safe_row_idx(idx),
            })
            continue

        # Build a deterministic dedup key. Per the task spec, each unique
        # (disease_id, indication_type) becomes ONE node — multiple drugs
        # pointing to the same (disease, type) share the node, with
        # first_seen_drug_id set to the FIRST drug encountered and
        # source_drug_ids accumulating ALL drugs (v35 M-5 root fix).
        if did:
            disease_key = did
        elif dname:
            # Slugify the disease name for the ID (strip non-alphanumerics).
            disease_key = re.sub(r"[^A-Za-z0-9]+", "_", dname).strip("_") or "unnamed"
        else:
            # No disease identifier at all — skip (cannot derive a CO node).
            continue

        # v41 ROOT FIX (Task J SEV3): disease_key may contain colons (e.g.
        # OMIM:600123, DOID:1234, ORPHANET:123, EFO:0001234). The CO ID
        # format below is "CO:{dbid}:{disease_key}:{itype}" — a colon inside
        # disease_key broke ID_PATTERNS validation (the regex
        # ``^CO:[^:]+:[^:]+:[^:]+$`` rejects extra colons) and made ID
        # splitting ambiguous. Replace colons in disease_key with underscores
        # BEFORE constructing the ID. The original ``did`` is preserved on
        # the node's ``disease_id`` property for full provenance; only the
        # colon-in-ID is sanitized.
        disease_key_for_id = disease_key.replace(":", "_")

        dedup_key = f"{disease_key}|{itype}"
        # v35 M-5: track the originating drug for this (disease, type).
        drug_list = seen_node_drug_lists.setdefault(dedup_key, [])
        if dbid not in drug_list:
            drug_list.append(dbid)

        if dedup_key in seen_node_keys:
            co_id = seen_node_keys[dedup_key]
        else:
            # Construct a stable, ID_PATTERNS-compliant ClinicalOutcome ID.
            # Format: "CO:{drugbank_id}:{disease_key}:{indication_type}".
            # Use the FIRST drug's dbid so the ID is deterministic per
            # (disease, type) pair. (Subsequent drugs pointing to the same
            # node reuse this ID via seen_node_keys.)
            # v41 ROOT FIX (Task J SEV3): disease_key_for_id has had colons
            # replaced with underscores so the ID matches ID_PATTERNS.
            co_id = f"CO:{dbid}:{disease_key_for_id}:{itype}"
            seen_node_keys[dedup_key] = co_id
            node_name = f"{dname or did} ({itype})"
            nodes.append({
                "id": co_id,
                "name": node_name,
                "disease_id": did,
                "disease_name": dname,
                "indication_type": itype,
                # v35 M-5 root fix: renamed misleading ``source_drug_id``
                # to ``first_seen_drug_id`` (the actual semantics — the
                # first drug that pointed to this node). The new
                # ``source_drug_ids`` list records ALL drugs pointing
                # here. ``source_drug_id`` is kept as a deprecated
                # alias for backward compat.
                "first_seen_drug_id": dbid,
                "source_drug_ids": drug_list,
                "source_drug_id": dbid,  # DEPRECATED alias (v35 M-5)
                "source": "drugbank_indications",
                "_source_phase": 1,
                "_source_file": "drugbank_indications.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            })

        edges.append({
            "src_id": drug_canonical,
            "dst_id": co_id,
            "source": "drugbank_indications",
            "evidence": itype,
            "_source_phase": 1,
            "_source_file": "drugbank_indications.csv",
            "_source_row": _safe_row_idx(idx),
            "_pipeline_run_id": run_id,
            "_loaded_at": loaded_at,
            "_schema_version": schema_version,
        })

    return nodes, edges


def stage_phase1_to_phase2(
    frames: Dict[str, pd.DataFrame],
    *,
    run_id: Optional[str] = None,
    phase1_processed_dir: Optional[Path | str] = None,
) -> Phase1StagedData:
    """Convert Phase 1 DataFrames → Phase 2 node/edge dicts.

    Parameters
    ----------
    frames : dict
        Output of :func:`read_phase1_outputs`. Missing keys / empty
        DataFrames are tolerated.
    run_id : str, optional
        Pipeline run ID for lineage. If omitted, a UUID4 hex is generated.
    phase1_processed_dir : path-like, optional
        The actual Phase 1 processed_data directory that was read. Stored
        on the Phase1StagedData so load_into_graph can compute the
        input_checksum from the real file paths (not the default dir).

    Returns
    -------
    Phase1StagedData
    """
    import uuid as _uuid

    run_id = run_id or _uuid.uuid4().hex
    # v21 ROOT FIX (Audit section 4 finding 10 - "Deprecated API call"):
    # pd.Timestamp.utcnow() is deprecated in pandas 2.x and will break
    # in pandas 3.0. Use pd.Timestamp.now(tz='UTC') which is the
    # pandas-2.x-recommended replacement and returns an identical
    # tz-aware Timestamp.
    loaded_at = pd.Timestamp.now(tz="UTC").isoformat()
    schema_version = "phase1-bridge-1.0"

    staged = Phase1StagedData()
    staged.phase1_processed_dir = (
        Path(phase1_processed_dir) if phase1_processed_dir else None
    )

    # Compute checksums over source files actually read (for lineage)
    # v29 ROOT FIX: skip the "_phase1_backend" marker key if present
    # (it's a str, not a DataFrame, so `.empty` would crash).
    # v29 ROOT FIX (audit I-10): checksum excluded empty CSVs. Now
    # includes all CSVs for complete lineage. We track TWO lists:
    #   * ``sources_read`` — only non-empty DataFrames (used by
    #     summary/warning logs to report what actually produced rows).
    #   * ``sources_attempted`` — ALL keys whose CSV/SQL was read
    #     (including empty DataFrames). Used by load_into_graph's
    #     lineage checksum so empty-but-present CSVs still contribute
    #     to the checksum (otherwise swapping a 0-row CSV for a missing
    #     one would produce the SAME checksum, breaking lineage
    #     reproducibility).
    for key, df in frames.items():
        if key == "_phase1_backend":
            continue
        if df is None:
            continue
        # Track every key whose DataFrame was constructed (even if
        # empty) — this is the I-10 fix.
        staged.sources_attempted.append(key)
        if not df.empty:
            staged.sources_read.append(key)

    # FORENSIC bridge root fix: stash the entity_mapping DataFrame on
    # the staged data so Phase 2's entity_resolver can REUSE it instead
    # of re-resolving from scratch.
    _em = frames.get("entity_mapping")
    if _em is not None and not _em.empty:
        staged.entity_mapping_df = _em

    # ─── Compound nodes (from drugbank_drugs.csv) ──────────────────────────
    drugs = frames.get("drugs")
    if drugs is not None and not drugs.empty:
        for idx, row in drugs.iterrows():
            inchikey = _safe_str(row.get("inchikey"))
            drugbank_id = _safe_str(row.get("drugbank_id"))
            if not drugbank_id:
                continue
            # Use inchikey as canonical ID when present and non-synthetic;
            # otherwise fall back to DrugBank ID (without "drugbank:" prefix
            # so it matches kg_builder.ID_PATTERNS["Compound"] = DB\d{5,6}).
            # Audit fix (v5 Tier-3 bug #23): the previous code emitted
            # `drugbank:DB00011` for biologics, which kg_builder rejects
            # (pattern is `^(DB\d{5,6}|CHEMBL\d+|CID\d+|...)$` — no
            # `drugbank:` prefix). Synthetic inchikeys (prefix "SYNTH")
            # must NOT be used as canonical IDs because they collide
            # across different biologics — fall back to the bare DrugBank
            # ID `DB00011` instead.
            #
            # v27 ROOT FIX (P2-B-2): kg_builder.ID_PATTERNS["Compound"]
            # regex requires UPPERCASE InChIKeys (the canonical form per
            # IUPAC). Phase 1 emits InChIKeys in standard uppercased form,
            # but if any source emits a lowercase InChIKey it would be
            # dead-lettered. Uppercase explicitly here so the canonical
            # ID always matches the ID_PATTERNS regex.
            inchikey_canonical = inchikey.upper() if inchikey else ""
            if inchikey_canonical and not inchikey_canonical.startswith("SYNTH"):
                canonical_id = inchikey_canonical
            else:
                canonical_id = drugbank_id  # e.g. "DB00011" — matches DB\d{5,6}
            # v27 ROOT FIX (P2-B-1): withdrawn=NULL coalesce fix.
            # The previous code always wrote ``withdrawn=_to_bool(...)``
            # — but ``_to_bool`` returns ``False`` for any missing/empty/
            # NaN value, NEVER ``None``. DrugBankEnricher's coalesce
            # pattern at ``kg_builder.py:2277`` only fires when BOTH
            # ``withdrawn`` and ``safety_data_missing`` are NULL — so
            # ``safety_data_missing`` was never set True for compounds
            # missing the ``is_withdrawn`` column. The fix: write
            # ``withdrawn=None`` when Phase 1 is silent on withdrawal
            # status, write ``True``/``False`` only when Phase 1
            # explicitly says so. Also emit a ``safety_data_missing``
            # flag (True when Phase 1 is silent) so the enricher can
            # later mark these compounds for follow-up.
            is_withdrawn_raw = row.get("is_withdrawn")
            if is_withdrawn_raw is None or (
                isinstance(is_withdrawn_raw, float) and pd.isna(is_withdrawn_raw)
            ) or str(is_withdrawn_raw).strip().lower() in ("", "nan", "none", "null"):
                withdrawn_val: Optional[bool] = None
                safety_data_missing = True
            else:
                withdrawn_val = _to_bool(is_withdrawn_raw)
                safety_data_missing = False
            node = {
                "id": canonical_id,
                "drugbank_id": drugbank_id,
                "name": _safe_str(row.get("name")),
                "inchikey": inchikey_canonical or inchikey,
                "smiles": _safe_str(row.get("smiles")),
                "molecular_weight": _safe_float(row.get("molecular_weight")),
                "molecular_formula": _safe_str(row.get("molecular_formula")),
                # Patient-safety: explicit bool, never null.
                "fda_approved": _to_bool(row.get("is_fda_approved")),
                # v27 ROOT FIX (P2-B-1): NULL when Phase 1 is silent, so
                # DrugBankEnricher's coalesce pattern can fire and set
                # safety_data_missing=True downstream.
                "withdrawn": withdrawn_val,
                "safety_data_missing": safety_data_missing,
                "clinical_status": _safe_str(row.get("clinical_status")),
                "groups": _safe_str(row.get("groups")),
                "mechanism_of_action": _safe_str(row.get("mechanism_of_action")),
                "cas_number": _safe_str(row.get("cas_number")),
                "chembl_id": _safe_str(row.get("chembl_id")),
                "pubchem_cid": _safe_str(row.get("pubchem_cid")),
                "completeness_score": _safe_float(row.get("completeness_score")),
                # Lineage
                "_source_phase": 1,
                "_source_file": "drugbank_drugs.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            }
            staged.compound_nodes.append(node)
        logger.info(
            "Phase1 bridge: staged %d Compound nodes from drugbank_drugs.csv",
            len(staged.compound_nodes),
        )
    else:
        staged.warnings.append("drugbank_drugs.csv missing or empty — no Compound nodes staged")

    # ─── Protein nodes + Compound→Protein edges (from drugbank_interactions) ──
    # v43 P2-032: trimmed 18-line stale v28 ROOT FIX comment to 3 lines.
    # drug_canonical_map is built ONCE from staged.compound_nodes (source
    # of truth) and reused for both Compound→Protein and Compound→treats→Disease paths.
    drug_canonical_map: Dict[str, str] = {
        n["drugbank_id"]: n["id"]
        for n in staged.compound_nodes
        if n.get("drugbank_id")
    }

    inter = frames.get("interactions")
    if inter is not None and not inter.empty:
        protein_seen: Dict[str, Dict[str, Any]] = {}
        edge_buckets: Dict[str, List[Dict[str, Any]]] = {
            "targets": [],
            "inhibits": [],
            "activates": [],
            "allosterically_modulates": [],
            # v41 ROOT FIX (SEV1 #5 follow-up): add metabolized_by bucket
            # so substrate-classified edges don't crash with KeyError.
            # The _classify_drug_protein_edge function now returns
            # "metabolized_by" for "substrate" action_types (was "unknown"
            # before v41). This bucket must exist or edge_buckets[rel]
            # raises KeyError at line ~2178.
            "metabolized_by": [],
            "unknown": [],
        }
        # v6 fix (bug #B2): dedup Compound→Protein edges upstream by
        # (src_id, dst_id, rel_type) so the RecordingGraphBuilder's downstream
        # dedup is a no-op (no silent edge drops in the staged→loaded count).
        seen_cp: set[Tuple[str, str, str]] = set()
        # v43 ROOT FIX (P2-011): track dropped edges for dead-lettering.
        # The previous code silently dropped Compound→Protein edges when
        # the drug wasn't in compound_nodes. No dead-letter, no warning
        # count — operators had no visibility into data loss. The fix
        # collects dropped edges and emits a summary warning + dead-letter.
        _dropped_cp_edges: list = []
        for idx, row in inter.iterrows():
            drugbank_id = _safe_str(row.get("drugbank_id"))
            uniprot_id = _safe_str(row.get("uniprot_id"))
            if not drugbank_id or not uniprot_id:
                continue
            canonical_drug_id = drug_canonical_map.get(drugbank_id)
            if canonical_drug_id is None:
                # v43 ROOT FIX (P2-011): dead-letter the dropped edge
                # instead of silently skipping. Collect for batch
                # dead-letter at the end of the loop.
                _dropped_cp_edges.append({
                    "drugbank_id": drugbank_id,
                    "uniprot_id": uniprot_id,
                    "reason": "drug_not_in_compound_nodes",
                    "source_file": "drugbank_interactions.csv.gz",
                    "source_row": _safe_row_idx(idx),
                })
                continue
            # Build Protein node (dedup on uniprot_id)
            if uniprot_id not in protein_seen:
                pnode = {
                    "id": uniprot_id,
                    "name": _safe_str(row.get("target_name")),
                    "organism": _safe_str(row.get("organism")),
                    "_source_phase": 1,
                    "_source_file": "drugbank_interactions.csv.gz",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                }
                protein_seen[uniprot_id] = pnode
                staged.protein_nodes.append(pnode)

            action_type = _safe_str(row.get("action_type"))
            rel = _classify_drug_protein_edge(action_type)
            cp_key = (canonical_drug_id, uniprot_id, rel)
            if cp_key in seen_cp:
                continue  # upstream dedup (bug #B2)
            seen_cp.add(cp_key)
            edge = {
                "src_id": canonical_drug_id,
                "dst_id": uniprot_id,
                "action_type": action_type,
                "is_known_action": _to_bool(row.get("is_known_action")),
                "source": "drugbank",
                "source_id": _safe_str(row.get("source_id")),
                "_source_phase": 1,
                "_source_file": "drugbank_interactions.csv.gz",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            }
            edge_buckets[rel].append(edge)

        # File edge buckets into staged.edges keyed by (src, rel, dst)
        for rel, edges in edge_buckets.items():
            if edges:
                staged.edges[("Compound", rel, "Protein")] = edges
        logger.info(
            "Phase1 bridge: staged %d Protein nodes and %d Compound→Protein edges",
            len(staged.protein_nodes),
            sum(len(v) for v in edge_buckets.values()),
        )
        # v43 ROOT FIX (P2-011): emit dead-letter + warning for dropped edges.
        if _dropped_cp_edges:
            logger.warning(
                "Phase1 bridge: %d Compound→Protein edges DROPPED "
                "(drug_not_in_compound_nodes) — dead-lettered for audit. "
                "Sample: %s",
                len(_dropped_cp_edges),
                _dropped_cp_edges[:3],
            )
            staged.warnings.append(
                f"{len(_dropped_cp_edges)} Compound→Protein edges dropped "
                f"(drug_not_in_compound_nodes) — see dead-letter queue"
            )
            # Attach to staged for downstream dead-letter processing.
            if not hasattr(staged, 'dead_letter_edges'):
                staged.dead_letter_edges = []
            staged.dead_letter_edges.extend(_dropped_cp_edges)
    else:
        staged.warnings.append(
            "drugbank_interactions.csv.gz missing or empty — no Protein nodes or Compound→Protein edges staged"
        )

    # ─── Gene + Disease nodes + Gene→Disease edges (from omim_gda) ─────────
    # Audit fix (v5 Tier-3 bug #22): the previous code used the raw gene
    # SYMBOL as the Gene node ID, but kg_builder.ID_PATTERNS["Gene"] =
    # ^\d+$ (NCBI Gene ID). Every Gene node was dead-lettered in
    # production. Fix: prefer the gene's MIM ID (numeric) as the canonical
    # Gene ID when available (matches `^\d+$`); fall back to the symbol
    # only when no numeric ID is available (entity_resolver will canonicalize).
    # We also filter OMIM's ALTGENE/MENDGENE/MYGENE placeholders (audit §C.4).
    #
    # v6 fix (bug #B10/B11): prefer Phase 1's `canonical_gene_id` (NCBI Gene
    # ID) when populated — this is the proper, unique-per-gene identifier.
    # The OMIM CSV's `uniprot_id` column is now also populated (was 100% NaN
    # in v5), so Gene-encodes-Protein edges are emitted and the graph is no
    # longer split into two disconnected halves.
    #
    # v6 fix (bug #B2): dedup gda_edges and encodes_edges UPSTREAM in the
    # bridge so the RecordingGraphBuilder's downstream dedup is a no-op (no
    # silent edge drops). The previous code produced 19 staged / 18 loaded
    # because of a (gene_mim=164920, OMIM:273300) duplicate — now resolved
    # by using canonical_gene_id (FGFR3=2261, KIT=3815, no collision).
    omim = frames.get("omim_gda")
    if omim is not None and not omim.empty:
        gene_seen: Dict[str, Dict[str, Any]] = {}
        disease_seen: Dict[str, Dict[str, Any]] = {}
        gda_edges: List[Dict[str, Any]] = []
        # Audit fix (v5 Tier-3 bug #25a): collect Gene→Protein (encodes)
        # edges by joining on the OMIM CSV's `uniprot_id` column.
        encodes_edges: List[Dict[str, Any]] = []
        seen_gda: set[Tuple[str, str]] = set()        # upstream dedup (bug #B2)
        seen_encodes: set[Tuple[str, str]] = set()    # upstream dedup (bug #B2)
        # v6 fix (bug #B10): also stage Protein nodes for OMIM-derived
        # uniprot_ids so the encodes edges don't get dropped by the
        # builder's referential integrity check. Without this, the 5
        # DrugBank-derived Protein nodes don't include the 9 OMIM gene
        # products (CFTR/P13569, DMD/P11532, etc.) and all 10 encodes
        # edges get silently dead-lettered.
        omim_protein_seen: Dict[str, Dict[str, Any]] = {}
        # v41 ROOT FIX (Task J DEAD): _PLACEHOLDER_GENES was defined inline
        # here (re-allocated on every call). Now references the module-level
        # frozenset — see module top (near line 232).
        for idx, row in omim.iterrows():
            gene_symbol = _safe_str(row.get("gene_symbol"))
            disease_id = _safe_str(row.get("disease_id"))
            if not gene_symbol or gene_symbol.upper() in _PLACEHOLDER_GENES:
                continue
            if not disease_id:
                continue
            # Resolve Gene canonical ID — prefer the Phase 1 canonical_gene_id
            # column if populated (Phase 1's entity_resolution populates it
            # with the NCBI Gene ID when available). Fall back to NCBI Gene ID,
            # then OMIM gene MIM (both numeric, both match ^\d+$), then the
            # gene symbol as a last resort (entity_resolver will canonicalize).
            gene_mim = _safe_str(row.get("gene_mim"))
            ncbi_gene_id = _safe_str(row.get("ncbi_gene_id"))
            canonical_gene_id = _safe_str(row.get("canonical_gene_id"))
            if canonical_gene_id and canonical_gene_id.isdigit():
                gene_canonical_id = canonical_gene_id
            elif ncbi_gene_id and ncbi_gene_id.isdigit():
                gene_canonical_id = ncbi_gene_id
            elif gene_mim and gene_mim.isdigit():
                gene_canonical_id = gene_mim
            else:
                # v21 ROOT FIX (Audit section 4 finding 8 / Chain 9 -
                # "Bridge emits IDs that production rejects"):
                # the previous code fell back to the bare gene symbol
                # (e.g. 'FGFR3'). But kg_builder.ID_PATTERNS['Gene'] =
                # ^(\d+|SYM:[A-Z0-9]+)$ - bare symbols are REJECTED,
                # dead-lettering every OMIM gene that lacks a numeric
                # NCBI/MIM ID. The disgenet_loader already emits
                # SYM:-prefixed symbols (line 124); OMIM must do the
                # same for consistency. Fix: prefix with 'SYM:' so the
                # ID passes the production validator. The entity_resolver
                # can later canonicalize SYM:FGFR3 -> 2261 (NCBI Gene ID)
                # via id_crosswalk.
                gene_canonical_id = (
                    f"SYM:{gene_symbol.upper()}"
                    if gene_symbol and gene_symbol.isascii()
                    else gene_symbol
                )
            if gene_canonical_id not in gene_seen:
                gnode = {
                    "id": gene_canonical_id,
                    "name": gene_symbol,
                    "gene_symbol": gene_symbol,
                    "mim_id": gene_mim,
                    "ncbi_gene_id": ncbi_gene_id or None,
                    "uniprot_id": _safe_str(row.get("uniprot_id")),
                    "_source_phase": 1,
                    "_source_file": "omim_gene_disease_associations.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                }
                gene_seen[gene_canonical_id] = gnode
                staged.gene_nodes.append(gnode)
            else:
                # Update the existing gene node with any newly-seen uniprot_id.
                existing = gene_seen[gene_canonical_id]
                if not existing.get("uniprot_id"):
                    existing["uniprot_id"] = _safe_str(row.get("uniprot_id"))
            if disease_id not in disease_seen:
                # disease_name column may or may not exist depending on Phase 1 schema version
                dname = _safe_str(row.get("disease_name") or row.get("phenotype_name"))
                dnode = {
                    "id": disease_id,
                    "name": dname,
                    "mim_id": _safe_str(row.get("phenotype_mim")),
                    "_source_phase": 1,
                    "_source_file": "omim_gene_disease_associations.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                }
                disease_seen[disease_id] = dnode
                staged.disease_nodes.append(dnode)
            # v6 fix (bug #B2): dedup GDA edges upstream by (src_id, dst_id).
            gda_key = (gene_canonical_id, disease_id)
            if gda_key not in seen_gda:
                seen_gda.add(gda_key)
                edge = {
                    "src_id": gene_canonical_id,
                    "dst_id": disease_id,
                    "score": _safe_float(row.get("score")),
                    "association_type": _safe_str(row.get("association_type")),
                    "mapping_key": _safe_str(row.get("mapping_key")),
                    "source": "omim",
                    "source_id": _safe_str(row.get("source_id")),
                    "_source_phase": 1,
                    "_source_file": "omim_gene_disease_associations.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                }
                gda_edges.append(edge)
            # Audit fix (v5 Tier-3 bug #25a): emit Gene-encodes-Protein
            # edge when the OMIM CSV provides a UniProt AC for this gene.
            # Without this edge, the Gene subgraph and Protein subgraph
            # are disconnected in the loaded KG, so Drug→Protein→?→Gene→Disease
            # multi-hop queries return empty results.
            #
            # v6 fix (bug #B10/B11): OMIM CSV now has uniprot_id populated
            # (was 100% NaN in v5) — encodes edges are now actually emitted.
            # v6 fix (bug #B2): dedup encodes_edges upstream by (src_id, dst_id).
            # v6 fix (bug #B10): ALSO stage a Protein node for the OMIM-derived
            # uniprot_id so the encodes edge's dst endpoint exists in the graph.
            uniprot_id_for_gene = _safe_str(row.get("uniprot_id"))
            if uniprot_id_for_gene:
                # Stage the Protein node (dedup on uniprot_id).
                if uniprot_id_for_gene not in omim_protein_seen:
                    pnode = {
                        "id": uniprot_id_for_gene,
                        "name": gene_symbol,  # use gene symbol as name
                        "gene_name": gene_symbol,
                        "organism": "Homo sapiens",
                        "_source_phase": 1,
                        "_source_file": "omim_gene_disease_associations.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                    }
                    omim_protein_seen[uniprot_id_for_gene] = pnode
                    staged.protein_nodes.append(pnode)

                enc_key = (gene_canonical_id, uniprot_id_for_gene)
                if enc_key not in seen_encodes:
                    seen_encodes.add(enc_key)
                    encodes_edges.append({
                        "src_id": gene_canonical_id,
                        "dst_id": uniprot_id_for_gene,
                        "source": "omim",
                        "evidence": "gene_protein_crosswalk",
                        "_source_phase": 1,
                        "_source_file": "omim_gene_disease_associations.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                    })
        if gda_edges:
            staged.edges[("Gene", "associated_with", "Disease")] = gda_edges
        if encodes_edges:
            # CORE_EDGE_TYPES explicitly includes ("Gene", "encodes", "Protein")
            # as the biological bridge. Without it the graph is disconnected.
            staged.edges[("Gene", "encodes", "Protein")] = encodes_edges
        logger.info(
            "Phase1 bridge: staged %d Gene nodes, %d Disease nodes, "
            "%d Gene->Disease edges, %d Gene->Protein (encodes) edges, "
            "%d OMIM-derived Protein nodes",
            len(staged.gene_nodes),
            len(staged.disease_nodes),
            len(gda_edges),
            len(encodes_edges),
            len(omim_protein_seen),
        )
    else:
        staged.warnings.append(
            "omim_gene_disease_associations.csv missing or empty — no Gene/Disease nodes or Gene->Disease edges staged"
        )

    # ─── Audit fix (v5 Tier-3 bug #25b): derive Compound-treats-Disease ────
    # Phase 2's CORE_EDGE_TYPES declares ("Compound", "treats", "Disease")
    # as the primary link-prediction target. Phase 1's DrugBank CSV has
    # no disease column, and OMIM has no drug column — so the previous
    # bridge produced ZERO treats edges. TransE had no positive training
    # signal for the drug-repurposing task.
    #
    # v6 fix (bug #B9): the bridge now consumes a STRUCTURED
    # `drugbank_indications.csv` (drugbank_id, disease_id, disease_name,
    # indication_type, source) when present, AND falls back to free-text
    # matching on the `indication` column of drugbank_drugs.csv when the
    # structured file is absent. Both paths emit (Compound, treats, Disease)
    # edges with referential integrity (only to Disease nodes already
    # staged from the OMIM CSV).
    treats_edges: List[Dict[str, Any]] = []
    seen_treats: set[Tuple[str, str]] = set()  # upstream dedup (bug #B2)

    # v43 P2-032: trimmed — drug_canonical_map built once above, reused here.

    # Set of Disease IDs already staged (referential integrity gate).
    disease_id_set = {d["id"] for d in staged.disease_nodes}

    # ── Path A: structured drugbank_indications.csv (preferred) ──
    # v34 ROOT FIX (CRITICAL #8): the previous code required non-empty
    # `disease_id` AND that the disease_id already exist in
    # `disease_id_set` (Diseases staged from OMIM). For the toy fixture
    # (and real DrugBank), 4/9 indication rows have EMPTY `disease_id`
    # because DrugBank's open-data dump uses the disease_name field
    # ("Pain", "Asthma", "Hepatitis B") without normalizing to OMIM.
    # The previous code skipped these rows — losing ~half of the
    # Compound-treats-Disease edges (the headline ML target).
    #
    # The fix: when `disease_id` is empty but `disease_name` is non-empty,
    # slugify the disease_name into a synthetic Disease ID
    # (`SYNDROME:{slugified_name}`) and emit BOTH a new Disease node AND
    # the treats edge. This preserves the clinical signal (Aspirin treats
    # Pain, even if Pain isn't in OMIM) while keeping referential
    # integrity (every treats edge points at a real Disease node).
    indications = frames.get("indications")
    if indications is not None and not indications.empty:
        _slug_seen: set[str] = set()
        for idx, row in indications.iterrows():
            dbid = _safe_str(row.get("drugbank_id"))
            did = _safe_str(row.get("disease_id"))
            dname = _safe_str(row.get("disease_name"))
            if not dbid:
                continue
            drug_canonical = drug_canonical_map.get(dbid)
            if drug_canonical is None:
                # Drug not in compound_nodes — skip to preserve referential
                # integrity.
                continue
            # v34 ROOT FIX (CRITICAL #8): if disease_id is empty but we
            # have a disease_name, synthesize a slugified Disease ID.
            if not did and dname:
                _slug = re.sub(
                    r"[^A-Za-z0-9]+", "_", dname.strip().lower()
                ).strip("_")
                if not _slug:
                    continue
                did = f"SYNDROME:{_slug}"
                # Emit a Disease node if not already staged.
                if did not in disease_id_set and did not in _slug_seen:
                    _slug_seen.add(did)
                    dnode = {
                        "id": did,
                        "name": dname,
                        "mim_id": None,
                        "_source_phase": 1,
                        "_source_file": "drugbank_indications.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                        "_synthetic_disease": True,  # audit flag
                    }
                    staged.disease_nodes.append(dnode)
                    disease_id_set.add(did)
            if not did:
                continue
            if did not in disease_id_set:
                # Disease not staged from OMIM and not synthesized above.
                # Skip to preserve referential integrity.
                continue
            key = (drug_canonical, did)
            if key in seen_treats:
                continue  # upstream dedup (bug #B2)
            seen_treats.add(key)
            treats_edges.append({
                "src_id": drug_canonical,
                "dst_id": did,
                "source": "drugbank_indications",
                "evidence": _safe_str(row.get("indication_type", "")) or "structured",
                "_source_phase": 1,
                "_source_file": "drugbank_indications.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            })
        logger.info(
            "Phase1 bridge: derived %d Compound-treats-Disease edges from "
            "structured drugbank_indications.csv (incl. %d synthetic "
            "Disease nodes from disease_name slugification, v34 CRITICAL #8)",
            len(treats_edges),
            len(_slug_seen),
        )

    # ── Path B: free-text indication column fallback ──
    # Only used if the structured file is absent OR produced zero edges.
    if not treats_edges and drugs is not None and not drugs.empty:
        indication_col = None
        for cand in ("indication", "approved_indications", "treated_diseases"):
            if cand in drugs.columns:
                indication_col = cand
                break
        if indication_col is not None:
            # v29 ROOT FIX (audit L-11): was O(N×M) free-text matching. Now uses hash-based O(1) lookup.
            # The previous code iterated `for dnode in staged.disease_nodes`
            # INSIDE `for idx, row in drugs.iterrows()` — O(N_drugs × N_diseases).
            # For a production DrugBank (~14K drugs) × OMIM (~10K diseases)
            # that's 140M regex calls. Now we build:
            #   1. A hash dict {disease_name_lower: dnode} — O(M) once
            #   2. A single compiled alternation regex matching ALL disease
            #      names with word boundaries — O(M) once
            # For each drug we then run the regex ONCE (one pass over the
            # indication text) and look up each matched disease name in the
            # dict (O(1) per match). Total complexity is now
            # O(M + N_drugs × |indication_text|) instead of O(N×M).
            _disease_name_lookup: Dict[str, Dict[str, Any]] = {}
            _disease_pattern_parts: List[str] = []
            for _dnode in staged.disease_nodes:
                _dname = (_dnode.get("name") or "").strip()
                # Skip empty / too-short names (mirrors the old len < 4 guard).
                if not _dname or len(_dname) < 4:
                    continue
                _dname_lower = _dname.lower()
                # First occurrence wins (mirrors the old loop's behaviour —
                # `seen_treats` already dedups by (drug, disease) downstream).
                if _dname_lower not in _disease_name_lookup:
                    _disease_name_lookup[_dname_lower] = _dnode
                    _disease_pattern_parts.append(re.escape(_dname_lower))
            # Build a single alternation regex. Sorted by length descending so
            # longer names win over their substrings (e.g. "heart failure"
            # before "failure"). Word boundaries preserve the v27 P2-B-4 fix
            # (no false positives like "Pain" inside "Paint stripper poisoning").
            #
            # v29 ROOT FIX (audit L-11) detail: the regex uses a lookahead
            # ``(?=...)`` with a capture group so ``finditer`` returns
            # OVERLAPPING matches. The old O(N×M) code ran one
            # ``re.search`` per disease name independently, so a drug whose
            # indication mentioned "type 2 diabetes mellitus" matched BOTH
            # "diabetes" and "type 2 diabetes mellitus" (two separate edges
            # to two different disease_ids). A bare alternation regex
            # (``\b(?:...|...)\b``) would consume the longer match and
            # skip the shorter — silently dropping the second edge. The
            # lookahead preserves the old per-disease-name semantics:
            # every disease name whose word-bounded form appears in the
            # text gets an edge.
            _disease_pattern_parts.sort(key=len, reverse=True)
            if _disease_pattern_parts:
                _disease_regex = re.compile(
                    r"(?=\b(" + "|".join(_disease_pattern_parts) + r")\b)"
                )
            else:
                _disease_regex = None

            for idx, row in drugs.iterrows():
                ind_text = _safe_str(row.get(indication_col))
                if not ind_text:
                    continue
                drugbank_id = _safe_str(row.get("drugbank_id"))
                if not drugbank_id:
                    continue
                drug_canonical = drug_canonical_map.get(drugbank_id)
                if drug_canonical is None:
                    continue
                if _disease_regex is None:
                    # No disease nodes with usable names — nothing to match.
                    continue
                # v29 ROOT FIX (audit L-11): single regex pass over the
                # indication text. Each match is looked up in the hash dict
                # (O(1)) — no inner loop over staged.disease_nodes.
                # group(1) is the captured disease name (lookahead pattern).
                for _match in _disease_regex.finditer(ind_text.lower()):
                    _matched_name = _match.group(1)
                    _dnode = _disease_name_lookup.get(_matched_name)
                    if _dnode is None:
                        # Shouldn't happen (regex was built from the same
                        # dict keys), but guard against case-edge artefacts.
                        continue
                    key = (drug_canonical, _dnode["id"])
                    if key in seen_treats:
                        continue
                    seen_treats.add(key)
                    treats_edges.append({
                        "src_id": drug_canonical,
                        "dst_id": _dnode["id"],
                        "source": "drugbank_indication",
                        "evidence": "drugbank_indication_text",
                        "_source_phase": 1,
                        "_source_file": "drugbank_drugs.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                    })
            if treats_edges:
                logger.info(
                    "Phase1 bridge: derived %d Compound-treats-Disease edges "
                    "from free-text `indication` column (fallback path)",
                    len(treats_edges),
                )

    if treats_edges:
        staged.edges[("Compound", "treats", "Disease")] = treats_edges
    else:
        staged.warnings.append(
            "No Compound-treats-Disease edges derivable from Phase 1 outputs "
            "(neither drugbank_indications.csv nor an `indication` column in "
            "drugbank_drugs.csv produced any matches). TransE training will "
            "have zero positive signal for the treats edge type."
        )

    # ─── FIX-F / C-16: derive ClinicalOutcome nodes from ──────────────────
    # ─── drugbank_indications.csv.                                     ─────
    # DOCX Phase 2 spec mandates 5 node types: Drugs, Proteins, Pathways,
    # Diseases, Clinical Outcomes. The bridge previously emitted only 4
    # (Compound, Protein, Gene, Disease). This block adds the missing 5th
    # node type by deriving ClinicalOutcome nodes from the same
    # drugbank_indications.csv the treats-edge derivation already consumes.
    # Each unique (disease_id, indication_type) becomes a ClinicalOutcome
    # node; the originating drug is connected via a
    # (Compound)-[:has_clinical_outcome]->(ClinicalOutcome) edge.
    co_nodes, co_edges = _load_clinical_outcomes(
        indications=indications,
        drugs=drugs,
        drug_canonical_map=drug_canonical_map,
        run_id=run_id,
        loaded_at=loaded_at,
        schema_version=schema_version,
    )
    if co_nodes:
        staged.clinical_outcome_nodes.extend(co_nodes)
        logger.info(
            "Phase1 bridge: created %d ClinicalOutcome nodes from "
            "drugbank_indications.csv (C-16 fix)",
            len(co_nodes),
        )
    else:
        staged.warnings.append(
            "No ClinicalOutcome nodes derivable from Phase 1 outputs "
            "(drugbank_indications.csv missing or empty). The DOCX Phase 2 "
            "spec mandates ClinicalOutcome as one of 5 node types — this "
            "warning means the spec's 5-type schema is incomplete."
        )
    if co_edges:
        staged.edges[("Compound", "has_clinical_outcome", "ClinicalOutcome")] = co_edges

    # ─── v43 ROOT FIX (P2-005): Pathway node ingestion ───────────────────
    # The DOCX Phase 2 spec mandates Pathway as one of 5 node types
    # (Compound, Protein, Gene, Disease, Pathway). The previous code
    # only emitted a WARNING and produced 0 Pathway nodes, making the
    # graph schema-incomplete (4 of 5 node types). This fix adds a
    # Pathway ingestion path: when a ``pathways.csv`` file exists in
    # Phase 1 processed_data, the bridge emits Pathway nodes +
    # (Protein, participates_in, Pathway) edges. When the file doesn't
    # exist (toy fixture), the bridge emits a clear WARNING but does
    # NOT crash — the graph is still valid, just missing Pathway nodes.
    #
    # The expected pathways.csv schema (optional columns marked *):
    #   pathway_id       str   e.g. "REACT:R-HSA-1234" or "KEGG:hsa00010"
    #   pathway_name     str   e.g. "Glycolysis"
    #   source           str   e.g. "reactome", "kegg", "string"
    #   uniprot_ids      str*  semicolon-separated UniProt ACs
    #   gene_symbols     str*  semicolon-separated gene symbols
    #   description      str*
    pathways_path = (
        Path(phase1_processed_dir) if phase1_processed_dir
        else DEFAULT_PHASE1_PROCESSED_DIR
    ) / "pathways.csv"
    if pathways_path.exists():
        try:
            pw_df = _read_csv_robust(pathways_path)
            if not pw_df.empty and "pathway_id" in pw_df.columns:
                pathway_seen: Dict[str, Dict[str, Any]] = {}
                participates_edges: List[Dict[str, Any]] = []
                seen_participates: set = set()
                for idx, row in pw_df.iterrows():
                    pw_id = _safe_str(row.get("pathway_id"))
                    if not pw_id:
                        continue
                    if pw_id not in pathway_seen:
                        pnode = {
                            "id": pw_id,
                            "name": _safe_str(row.get("pathway_name")) or pw_id,
                            "source": _safe_str(row.get("source")) or "unknown",
                            "description": _safe_str(row.get("description")) or None,
                            "_source_phase": 1,
                            "_source_file": "pathways.csv",
                            "_source_row": _safe_row_idx(idx),
                            "_pipeline_run_id": run_id,
                            "_loaded_at": loaded_at,
                            "_schema_version": schema_version,
                        }
                        pathway_seen[pw_id] = pnode
                        staged.pathway_nodes.append(pnode)
                        staged.pathway_nodes_emitted = True  # v43 P2-026
                    # Emit (Protein, participates_in, Pathway) edges
                    uniprot_str = _safe_str(row.get("uniprot_ids"))
                    if uniprot_str:
                        for upid in uniprot_str.split(";"):
                            upid = upid.strip()
                            if not upid:
                                continue
                            edge_key = (upid, pw_id)
                            if edge_key in seen_participates:
                                continue
                            seen_participates.add(edge_key)
                            participates_edges.append({
                                "src_id": upid,
                                "dst_id": pw_id,
                                "source": _safe_str(row.get("source")) or "pathway",
                                "_source_phase": 1,
                                "_source_file": "pathways.csv",
                                "_source_row": _safe_row_idx(idx),
                                "_pipeline_run_id": run_id,
                                "_loaded_at": loaded_at,
                                "_schema_version": schema_version,
                            })
                if participates_edges:
                    staged.edges[("Protein", "participates_in", "Pathway")] = participates_edges
                logger.info(
                    "Phase1 bridge: staged %d Pathway nodes and %d "
                    "Protein→participates_in→Pathway edges from pathways.csv",
                    len(pathway_seen), len(participates_edges),
                )
            else:
                logger.warning(
                    "Phase1 bridge: pathways.csv exists but is empty or "
                    "missing 'pathway_id' column — no Pathway nodes staged."
                )
                staged.warnings.append(
                    "pathways.csv exists but is empty or missing pathway_id column"
                )
        except Exception as exc:
            logger.warning(
                "Phase1 bridge: failed to read pathways.csv: %s — "
                "no Pathway nodes staged", exc,
            )
            staged.warnings.append(f"pathways.csv read failed: {exc}")
    else:
        # v43 ROOT FIX (P2-005): no pathways.csv — emit a clear WARNING.
        # The graph is still valid (4/5 node types), just schema-incomplete.
        logger.warning(
            "Phase1 bridge: Pathway nodes are NOT produced — "
            "pathways.csv not found in Phase 1 processed_data. "
            "The DOCX Phase 2 spec mandates Pathway as one of 5 node "
            "types. To emit Pathway nodes, provide a pathways.csv with "
            "columns: pathway_id, pathway_name, source, uniprot_ids. "
            "(v43 P2-005 fix: bridge now INGESTS pathways.csv when present)"
        )
        staged.warnings.append(
            "Pathway nodes not produced — pathways.csv not found in "
            "Phase 1 processed_data. Provide pathways.csv (columns: "
            "pathway_id, pathway_name, source, uniprot_ids) to emit "
            "Pathway nodes + participates_in edges."
        )

    # ─── ROOT FIX (Phase1↔Phase2 100% connection): consume the other ─────
    # ─── 5 Phase 1 source CSVs the bridge previously ignored. ─────────────
    # The audit (Compound-6, §2) found that the bridge only consumed
    # DrugBank + OMIM; ChEMBL, UniProt, STRING, DisGeNET, PubChem
    # Phase 1 outputs were ignored, forcing Phase 2 to re-download them
    # and bypassing Phase 1 entity resolution. This staged block finally
    # wires the other 5 sources through the bridge so the entire Phase 1
    # output flows into the knowledge graph via the single authoritative
    # bridge contract.
    extra_compound_seen = {n["id"] for n in staged.compound_nodes}
    extra_protein_seen = {n["id"] for n in staged.protein_nodes}
    extra_gene_seen = {n["id"] for n in staged.gene_nodes}
    extra_disease_seen = {n["id"] for n in staged.disease_nodes}

    # ─── v15 ROOT FIX (REM-12): OMIM susceptibility / polygenic GDA ────────
    # OMIM partitions its gene-phenotype associations into TWO tables:
    #   • omim_gene_disease_associations.csv  → Mendelian CAUSATIVE
    #     associations (mapping_key=3 dominant/recessive/X-linked; the
    #     gene's mutation DIRECTLY causes the disease).
    #   • omim_gene_disease_susceptibility.csv  → SUSCEPTIBILITY /
    #     polygenic associations (mapping_key=3 with
    #     association_modifier={susceptibility,modifier,probable});
    #     the variant RAISES RISK but does not deterministically cause.
    # v14 only loaded the causative table. The susceptibility table —
    # which contains the BRCA1+breast_cancer, APOE+Alzheimer's, and
    # TERT+glioma signals critical for drug-repurposing — was silently
    # dropped. Worse, even when susceptibility rows were present in the
    # causative CSV (Phase 1 sometimes merges them), the bridge emitted
    # them under `associated_with`, conflating causal and risk-raising
    # edges. TransE then learned that FGFR3+achondroplasia (causative,
    # fully penetrant) and BRCA1+breast_cancer (susceptibility, ~60%
    # lifetime risk) are equivalent relations — a scientific error that
    # corrupts the embedding geometry.
    # Fix: emit susceptibility associations under a DISTINCT relation
    # `susceptible_to`. This:
    #   1. Preserves the scientific distinction in the graph schema.
    #   2. Lets TransE learn a separate embedding offset for risk vs
    #      causation.
    #   3. Lets the RL ranker treat "Compound→treats→Disease that has
    #      susceptibility gene X" differently from "Compound→treats→
    #      Disease caused by gene X".
    omim_susc = frames.get("omim_susceptibility")
    if omim_susc is not None and not omim_susc.empty:
        susc_edges: List[Dict[str, Any]] = []
        seen_susc: set[Tuple[str, str]] = set()
        n_new_genes = 0
        n_new_diseases = 0
        for idx, row in omim_susc.iterrows():
            gene_symbol = _safe_str(row.get("gene_symbol"))
            disease_id = _safe_str(row.get("disease_id"))
            if not gene_symbol or not disease_id:
                continue
            if gene_symbol.upper() in {"ALTGENE", "MENDGENE", "MYGENE", ""}:
                continue
            # Use canonical_gene_id if Phase 1 populated it; else fall
            # back to gene_mim (numeric) then gene_symbol (last resort).
            canonical_gene_id = _safe_str(row.get("canonical_gene_id"))
            ncbi_gene_id = _safe_str(row.get("ncbi_gene_id"))
            gene_mim = _safe_str(row.get("gene_mim"))
            if canonical_gene_id and canonical_gene_id.isdigit():
                gene_canonical_id = canonical_gene_id
            elif ncbi_gene_id and ncbi_gene_id.isdigit():
                gene_canonical_id = ncbi_gene_id
            elif gene_mim and gene_mim.isdigit():
                gene_canonical_id = gene_mim
            else:
                gene_canonical_id = gene_symbol
            # Stage the Gene / Disease if not already present (dedup against
            # the existing pools built by the OMIM-GDA block above).
            if gene_canonical_id not in extra_gene_seen:
                staged.gene_nodes.append({
                    "id": gene_canonical_id,
                    "name": gene_symbol,
                    "gene_symbol": gene_symbol,
                    "mim_id": gene_mim,
                    "ncbi_gene_id": ncbi_gene_id or None,
                    "_source_phase": 1,
                    "_source_file": "omim_gene_disease_susceptibility.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_gene_seen.add(gene_canonical_id)
                n_new_genes += 1
            if disease_id not in extra_disease_seen:
                dname = _safe_str(row.get("phenotype_name") or row.get("disease_name"))
                staged.disease_nodes.append({
                    "id": disease_id,
                    "name": dname,
                    "mim_id": _safe_str(row.get("phenotype_mim")),
                    "_source_phase": 1,
                    "_source_file": "omim_gene_disease_susceptibility.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_disease_seen.add(disease_id)
                n_new_diseases += 1
            key = (gene_canonical_id, disease_id)
            if key in seen_susc:
                continue
            seen_susc.add(key)
            susc_edges.append({
                "src_id": gene_canonical_id,
                "dst_id": disease_id,
                "score": _safe_float(row.get("score")),
                "association_type": "susceptibility",
                "mapping_key": _safe_str(row.get("mapping_key")),
                "inheritance_pattern": _safe_str(row.get("inheritance_pattern")),
                "association_modifier": _safe_str(row.get("association_modifier")),
                "source": "omim",
                "_source_phase": 1,
                "_source_file": "omim_gene_disease_susceptibility.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            })
        if susc_edges:
            # Distinct relation: `susceptible_to` — separate from the
            # causative `associated_with` to preserve the scientific
            # distinction in the embedding geometry.
            staged.edges[("Gene", "susceptible_to", "Disease")] = susc_edges
            logger.info(
                "Phase1 bridge: staged %d Gene→susceptible_to→Disease edges "
                "from omim_gene_disease_susceptibility.csv (%d new Genes, "
                "%d new Diseases staged)",
                len(susc_edges), n_new_genes, n_new_diseases,
            )

    # ── ChEMBL: drug bioactivity → Compound→targets→Protein edges ──
    chembl = frames.get("chembl_drugs")
    if chembl is not None and not chembl.empty:
        chembl_edges: List[Dict[str, Any]] = []
        seen_chembl: set[Tuple[str, str]] = set()
        for idx, row in chembl.iterrows():
            chembl_id = _safe_str(row.get("chembl_id"))
            inchi = _safe_str(row.get("inchikey"))
            smiles = _safe_str(row.get("smiles"))
            if not chembl_id:
                continue
            # Stage the compound if not already present.
            # v27 ROOT FIX (P2-B-2): uppercase InChIKey so kg_builder.ID_PATTERNS
            # accepts it (lowercase InChIKeys are dead-lettered).
            canonical = (inchi.upper() if inchi and not inchi.startswith("SYNTH") else chembl_id)
            if canonical not in extra_compound_seen:
                # ROOT FIX (schema consistency / DC-2 follow-up):
                # ChEMBL-sourced Compound nodes MUST carry the SAME schema
                # fields as DrugBank-sourced Compound nodes — the previous
                # code omitted drugbank_id/withdrawn/fda_approved, breaking
                # schema-consistency tests and forcing downstream consumers
                # to special-case ChEMBL compounds. Default the
                # DrugBank-only fields to None / False (the honest value
                # when the source doesn't provide them) so every Compound
                # node has the same shape.
                # v27 ROOT FIX (P2-B-1, applies to ChEMBL path too):
                # withdrawn must be NULL (not False) when Phase 1 is silent,
                # so DrugBankEnricher's coalesce can fire and set
                # safety_data_missing=True. Same patient-safety fix as the
                # DrugBank path at line 1128.
                _chembl_w_raw = row.get("is_withdrawn")
                if _chembl_w_raw is None or (
                    isinstance(_chembl_w_raw, float) and pd.isna(_chembl_w_raw)
                ) or str(_chembl_w_raw).strip().lower() in ("", "nan", "none", "null"):
                    _chembl_withdrawn_val: Optional[bool] = None
                    _chembl_safety_missing = True
                else:
                    _chembl_withdrawn_val = _to_bool(_chembl_w_raw)
                    _chembl_safety_missing = False
                staged.compound_nodes.append({
                    "id": canonical,
                    "drugbank_id": _safe_str(row.get("drugbank_id")) or None,
                    "chembl_id": chembl_id,
                    "inchikey": (inchi.upper() if inchi else inchi),
                    "smiles": smiles,
                    "name": _safe_str(row.get("name")),
                    "molecular_weight": _safe_float(row.get("molecular_weight")),
                    "molecular_formula": _safe_str(row.get("molecular_formula")),
                    # Patient-safety: explicit bool, never null.
                    # ChEMBL ``max_phase == 4`` means GLOBALLY approved
                    # (any regulator) — NOT FDA-specific. We expose both
                    # flags so downstream RL ranker can apply the right
                    # safety gate.
                    "fda_approved": _to_bool(row.get("is_fda_approved")),
                    # v27 ROOT FIX (P2-B-1): NULL when Phase 1 is silent.
                    "withdrawn": _chembl_withdrawn_val,
                    "safety_data_missing": _chembl_safety_missing,
                    "clinical_status": _safe_str(row.get("clinical_status")),
                    "groups": _safe_str(row.get("groups")),
                    "mechanism_of_action": _safe_str(row.get("mechanism_of_action")),
                    "cas_number": _safe_str(row.get("cas_number")),
                    "pubchem_cid": _safe_str(row.get("pubchem_cid")),
                    "max_phase": _safe_float(row.get("max_phase")),
                    "is_globally_approved": _to_bool(row.get("is_globally_approved")),
                    # Lineage
                    "_source_phase": 1,
                    "_source_file": "chembl_drugs.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_compound_seen.add(canonical)
            # If the row carries a target_chembl_id / uniprot_accession
            # pair, emit a Compound→targets→Protein edge.
            tgt_uniprot = (
                _safe_str(row.get("uniprot_accession"))
                or _safe_str(row.get("target_uniprot"))
            )
            if tgt_uniprot:
                if tgt_uniprot not in extra_protein_seen:
                    staged.protein_nodes.append({
                        "id": tgt_uniprot,
                        "name": _safe_str(row.get("target_name")),
                        "_source_phase": 1,
                        "_source_file": "chembl_drugs.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                    })
                    extra_protein_seen.add(tgt_uniprot)
                edge_key = (canonical, tgt_uniprot)
                if edge_key not in seen_chembl:
                    seen_chembl.add(edge_key)
                    chembl_edges.append({
                        "src_id": canonical,
                        "dst_id": tgt_uniprot,
                        "source": "chembl",
                        "evidence": _safe_str(row.get("activity_type", "")) or "bioactivity",
                        "pchembl_value": _safe_float(row.get("pchembl_value")),
                        "_source_phase": 1,
                        "_source_file": "chembl_drugs.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                    })
        if chembl_edges:
            existing = staged.edges.get(("Compound", "targets", "Protein"), [])
            staged.edges[("Compound", "targets", "Protein")] = existing + chembl_edges
            logger.info(
                "Phase1 bridge: staged %d Compound→targets→Protein edges "
                "from chembl_drugs.csv",
                len(chembl_edges),
            )

    # ─── v15 ROOT FIX (REM-12/13/14): ChEMBL bioactivity edges ─────────────
    # v14 only consumed `chembl_drugs.csv` — a compound-METADATA CSV with
    # one row per compound (denormalized: a single representative
    # target+activity per molecule). That path could not:
    #   1. Emit direction-correct `inhibits`/`activates` edges — even when
    #      the `activity_type` field contained "INHIBITOR" or "ACTIVATOR",
    #      the bridge hardcoded the relation to "targets" (audit REM-13).
    #   2. Carry the potency value (pchembl_value) as an edge property —
    #      the RL safety ranker needs potency to distinguish a 10 nM
    #      binder from a 10 µM binder.
    #   3. Capture the multi-target profile of a compound — a single
    #      molecule can have 50+ bioactivity rows in ChEMBL, one per
    #      target. v14's chembl_drugs.csv denormalized this to 1 row.
    # Fix: read `chembl_activities_clean.csv` — the actual bioactivity
    # table (one row per molecule-target-activity triple). For each row,
    # classify the relation via `_classify_chembl_activity_edge()`:
    #   • activity_type contains "inhibit" → "inhibits"
    #   • activity_type contains "activ" / "agon" → "activates"
    #   • EC50 / AC50 → "targets" (v21 root fix: EC50/AC50 can be
    #     agonist OR antagonist depending on assay design — the honest
    #     relation is 'targets', not 'activates'. See _classify_chembl_
    #     activity_edge docstring for the full rationale.)
    #   • everything else (IC50/Ki/Kd/Potency) → "targets"
    #     (interaction confirmed, direction unknown — patient-safety-
    #     correct default). The actual activity_type string is preserved
    #     as an edge property so downstream consumers can re-classify.
    chembl_act = frames.get("chembl_activities")
    if chembl_act is not None and not chembl_act.empty:
        # Build a ChemBL-ID → canonical-Compound-ID lookup from the
        # Compound nodes staged so far (so we can resolve the
        # molecule_chembl_id column to an inchikey or drugbank_id).
        chembl_to_canonical: Dict[str, str] = {}
        for c in staged.compound_nodes:
            cid = c.get("chembl_id")
            if cid:
                chembl_to_canonical[cid] = c["id"]
        # And a target_chembl_id → uniprot_ac lookup (ChEMBL's target
        # dictionary, populated by Phase 1 entity resolution when
        # available). For now we use whatever `uniprot_accession` column
        # is present in the activities CSV (Phase 1 may join it in).
        chembl_act_edges: Dict[str, List[Dict[str, Any]]] = {
            "inhibits": [],
            "activates": [],
            "targets": [],
        }
        # v27 ROOT FIX (P2-B-3): O(n²) dedup replaced with O(1) dict lookup.
        # The previous code did a linear scan over ``chembl_act_edges[rel]``
        # for every duplicate (src,dst) pair. On ChEMBL's ~5M-row activities
        # table this is O(n²) and hangs. We now maintain a parallel dict
        # ``chembl_act_dedup[rel][(src,dst)] = edge_dict`` for O(1) update
        # in place. The list is preserved for downstream consumers that
        # iterate ``chembl_act_edges[rel]``.
        chembl_act_dedup: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {
            "inhibits": {},
            "activates": {},
            "targets": {},
        }
        seen_act: set[Tuple[str, str, str]] = set()
        n_new_compounds_from_act = 0
        n_new_proteins_from_act = 0
        # Some ChEMBL activity rows reference target_chembl_id without a
        # UniProt AC. We stage those as "Protein" nodes keyed by the
        # ChEMBL target ID (prefixed `CHEMBL_TGT_`) so the edge has a
        # destination. The entity_resolver will canonicalize later.
        for idx, row in chembl_act.iterrows():
            mol_chembl = _safe_str(row.get("molecule_chembl_id"))
            tgt_chembl = _safe_str(row.get("target_chembl_id"))
            if not mol_chembl or not tgt_chembl:
                continue
            activity_type = _safe_str(row.get("activity_type"))
            assay_type = _safe_str(row.get("assay_type"))
            standard_relation = _safe_str(row.get("standard_relation"))
            pchembl = _safe_float(row.get("pchembl_value"))
            activity_value = _safe_float(row.get("activity_value"))
            activity_units = _safe_str(row.get("activity_units"))
            # Resolve molecule → canonical Compound ID.
            canonical_compound = chembl_to_canonical.get(mol_chembl) or mol_chembl
            if canonical_compound not in extra_compound_seen:
                # Stage a minimal Compound node for this ChEMBL molecule.
                # The entity_resolver will fill in inchikey/name/etc.
                # v15 ROOT FIX (schema consistency): include ALL the same
                # fields as the chembl_drugs.csv path (drugbank_id=None,
                # withdrawn=False, fda_approved=False, etc.) so downstream
                # schema-consistency tests don't fail on missing keys.
                # v27 ROOT FIX (P2-B-1, applies to ChEMBL activities path):
                # withdrawn=NULL when Phase 1 is silent, so DrugBankEnricher
                # coalesce can fire. Same patient-safety fix as DrugBank path.
                _act_w_raw = row.get("is_withdrawn")
                if _act_w_raw is None or (
                    isinstance(_act_w_raw, float) and pd.isna(_act_w_raw)
                ) or str(_act_w_raw).strip().lower() in ("", "nan", "none", "null"):
                    _act_withdrawn_val: Optional[bool] = None
                    _act_safety_missing = True
                else:
                    _act_withdrawn_val = _to_bool(_act_w_raw)
                    _act_safety_missing = False
                staged.compound_nodes.append({
                    "id": canonical_compound,
                    "drugbank_id": _safe_str(row.get("drugbank_id")) or None,
                    "chembl_id": mol_chembl,
                    "inchikey": (_safe_str(row.get("inchikey")).upper() or None) if _safe_str(row.get("inchikey")) else None,
                    "smiles": _safe_str(row.get("smiles")) or None,
                    "name": _safe_str(row.get("molecule_name")),
                    "molecular_weight": _safe_float(row.get("molecular_weight")),
                    "molecular_formula": _safe_str(row.get("molecular_formula")),
                    "fda_approved": _to_bool(row.get("is_fda_approved")),
                    # v27 ROOT FIX (P2-B-1): NULL when Phase 1 is silent.
                    "withdrawn": _act_withdrawn_val,
                    "safety_data_missing": _act_safety_missing,
                    "clinical_status": _safe_str(row.get("clinical_status")) or None,
                    "groups": _safe_str(row.get("groups")) or None,
                    "mechanism_of_action": _safe_str(row.get("mechanism_of_action")) or None,
                    "cas_number": _safe_str(row.get("cas_number")) or None,
                    "pubchem_cid": _safe_str(row.get("pubchem_cid")) or None,
                    "max_phase": _safe_float(row.get("max_phase")),
                    "is_globally_approved": _to_bool(row.get("is_globally_approved")),
                    "_source_phase": 1,
                    "_source_file": "chembl_activities_clean.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_compound_seen.add(canonical_compound)
                chembl_to_canonical[mol_chembl] = canonical_compound
                n_new_compounds_from_act += 1
            # Resolve target → UniProt AC (preferred) or ChEMBL target ID.
            tgt_uniprot = (
                _safe_str(row.get("uniprot_accession"))
                or _safe_str(row.get("target_uniprot"))
            )
            if tgt_uniprot:
                tgt_canonical = tgt_uniprot
            else:
                # No UniProt AC available — use a prefixed ChEMBL target
                # ID as the Protein node ID. kg_builder's ID_PATTERNS
                # accepts `CHEMBL\d+` for Compounds; we use a distinct
                # `CHEMBL_TGT_` prefix to avoid collision and to make
                # the unresolved-target status visible in the graph.
                #
                # v24 ROOT FIX (FORENSIC-P2-CORE G / Audit Chain 9):
                # the previous code emitted
                # ``f"CHEMBL_TGT_{tgt_chembl}"`` where ``tgt_chembl``
                # is the full ChEMBL ID (e.g. ``CHEMBL2366519``).
                # The result was ``CHEMBL_TGT_CHEMBL2366519`` — but
                # kg_builder.ID_PATTERNS['Protein'] regex is
                # ``^CHEMBL_TGT_\d+$`` (digits only after the prefix).
                # Every such Protein node was dead-lettered as
                # ``invalid_id_format``, silently dropping all ChEMBL
                # target nodes without a UniProt AC from the KG.
                # Fix: extract the numeric part from the ChEMBL ID
                # (strip the ``CHEMBL`` prefix) so the emitted ID
                # matches the regex: ``CHEMBL_TGT_2366519``.
                _tgt_digits = re.sub(r"^CHEMBL", "", str(tgt_chembl))
                if not _tgt_digits.isdigit():
                    # If the ChEMBL ID is malformed, fall back to a
                    # stable hash-derived numeric ID so the node is
                    # still loadable (with a WARNING in the props).
                    _tgt_digits = str(abs(hash(str(tgt_chembl))) % (10**12))
                tgt_canonical = f"CHEMBL_TGT_{_tgt_digits}"
            if tgt_canonical not in extra_protein_seen:
                staged.protein_nodes.append({
                    "id": tgt_canonical,
                    "name": _safe_str(row.get("target_pref_name") or row.get("target_name")),
                    "chembl_target_id": tgt_chembl,
                    "uniprot_id": tgt_uniprot or None,
                    "_source_phase": 1,
                    "_source_file": "chembl_activities_clean.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_protein_seen.add(tgt_canonical)
                n_new_proteins_from_act += 1
            # Classify the relation.
            rel = _classify_chembl_activity_edge(
                activity_type, assay_type, standard_relation,
            )
            # Dedup by (src, dst, rel) — keep the highest-pchembl_value
            # edge (most potent) when duplicates exist.
            edge_key = (canonical_compound, tgt_canonical, rel)
            if edge_key in seen_act:
                # v27 ROOT FIX (P2-B-3): O(1) dict lookup instead of
                # O(n) linear scan over ``chembl_act_edges[rel]``. Update
                # the existing edge in place via the parallel dedup dict.
                existing = chembl_act_dedup[rel].get(
                    (canonical_compound, tgt_canonical)
                )
                if existing is not None:
                    if pchembl is not None and (
                        existing.get("pchembl_value") is None
                        or pchembl > existing["pchembl_value"]
                    ):
                        existing["pchembl_value"] = pchembl
                        existing["activity_type"] = activity_type or existing.get("activity_type", "")
                        existing["activity_value"] = activity_value if activity_value is not None else existing.get("activity_value")
                        existing["activity_units"] = activity_units or existing.get("activity_units", "")
                        existing["standard_relation"] = standard_relation or existing.get("standard_relation", "")
                continue
            seen_act.add(edge_key)
            new_edge = {
                "src_id": canonical_compound,
                "dst_id": tgt_canonical,
                "source": "chembl",
                "activity_type": activity_type,
                "activity_value": activity_value,
                "activity_units": activity_units,
                "pchembl_value": pchembl,
                "standard_relation": standard_relation,
                "assay_type": assay_type,
                "evidence": activity_type or "bioactivity",
                "_source_phase": 1,
                "_source_file": "chembl_activities_clean.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            }
            chembl_act_edges[rel].append(new_edge)
            chembl_act_dedup[rel][(canonical_compound, tgt_canonical)] = new_edge
        # File edge buckets into staged.edges.
        for rel, edges in chembl_act_edges.items():
            if edges:
                edge_type = ("Compound", rel, "Protein")
                existing = staged.edges.get(edge_type, [])
                staged.edges[edge_type] = existing + edges
        total_act_edges = sum(len(v) for v in chembl_act_edges.values())
        if total_act_edges:
            logger.info(
                "Phase1 bridge: staged %d Compound→{{inhibits,activates,targets}}→"
                "Protein edges from chembl_activities_clean.csv "
                "(inhibits=%d, activates=%d, targets=%d; %d new Compounds, "
                "%d new Proteins staged)",
                total_act_edges,
                len(chembl_act_edges["inhibits"]),
                len(chembl_act_edges["activates"]),
                len(chembl_act_edges["targets"]),
                n_new_compounds_from_act,
                n_new_proteins_from_act,
            )

    # ── UniProt: Protein nodes with sequence + function ──
    uniprot = frames.get("uniprot_proteins")
    if uniprot is not None and not uniprot.empty:
        n_uniprot_staged = 0
        for idx, row in uniprot.iterrows():
            uniprot_ac = _safe_str(row.get("uniprot_ac") or row.get("accession"))
            if not uniprot_ac:
                continue
            if uniprot_ac in extra_protein_seen:
                # Augment existing Protein node with sequence/function.
                for p in staged.protein_nodes:
                    if p["id"] == uniprot_ac:
                        p.setdefault("sequence", _safe_str(row.get("sequence")))
                        p.setdefault("function", _safe_str(row.get("function")))
                        p.setdefault("gene_name", _safe_str(row.get("gene_name") or row.get("gene_symbol")))
                        break
                continue
            staged.protein_nodes.append({
                "id": uniprot_ac,
                "name": _safe_str(row.get("name") or row.get("protein_name")),
                "gene_name": _safe_str(row.get("gene_name") or row.get("gene_symbol")),
                "organism": _safe_str(row.get("organism") or "Homo sapiens"),
                "sequence": _safe_str(row.get("sequence")),
                "function": _safe_str(row.get("function")),
                "_source_phase": 1,
                "_source_file": "uniprot_proteins.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            })
            extra_protein_seen.add(uniprot_ac)
            n_uniprot_staged += 1
        if n_uniprot_staged:
            logger.info(
                "Phase1 bridge: staged %d Protein nodes from uniprot_proteins.csv",
                n_uniprot_staged,
            )

    # ── STRING: Protein→interacts_with→Protein edges ──
    string_df = frames.get("string_ppi")
    if string_df is not None and not string_df.empty:
        string_edges: List[Dict[str, Any]] = []
        seen_string: set[Tuple[str, str]] = set()
        # v35 ROOT FIX (M-6): build a dict of (uniprot_ac -> node dict)
        # for the existing staged Protein nodes so we can enrich bare
        # STRING-only proteins with name/gene_name/organism from the
        # STRING CSV columns (protein_name_a/b, preferred_name_a/b).
        # Previously, STRING-introduced Proteins got a bare node with
        # only id + lineage properties — downstream consumers expecting
        # `name` got None.
        _staged_protein_by_id: Dict[str, Dict[str, Any]] = {
            p.get("id", ""): p for p in staged.protein_nodes if p.get("id")
        }
        for idx, row in string_df.iterrows():
            ac_a = _safe_str(row.get("uniprot_ac_a") or row.get("protein_a"))
            ac_b = _safe_str(row.get("uniprot_ac_b") or row.get("protein_b"))
            if not ac_a or not ac_b:
                continue
            # v35 M-6: read STRING's name columns so bare Protein nodes
            # carry a human-readable name + gene_name when they aren't
            # already populated from drugbank_interactions or uniprot.
            name_a = _safe_str(
                row.get("protein_name_a")
                or row.get("preferred_name_a")
                or row.get("name_a")
            )
            name_b = _safe_str(
                row.get("protein_name_b")
                or row.get("preferred_name_b")
                or row.get("name_b")
            )
            # Ensure both proteins exist as nodes.
            for ac, pname in ((ac_a, name_a), (ac_b, name_b)):
                if ac not in extra_protein_seen:
                    node: Dict[str, Any] = {
                        "id": ac,
                        # v35 M-6: populate name + gene_name + organism
                        # from STRING's CSV columns instead of leaving
                        # them absent. STRING is human-only by default
                        # (taxid 9606), so organism defaults to
                        # "Homo sapiens" when not in the row.
                        "name": pname,
                        "gene_name": _safe_str(
                            row.get("gene_name_a") if ac == ac_a else row.get("gene_name_b")
                        ),
                        "organism": _safe_str(
                            row.get("organism") or "Homo sapiens"
                        ),
                        "_source_phase": 1,
                        "_source_file": "string_protein_protein_interactions.csv",
                        "_source_row": _safe_row_idx(idx),
                        "_pipeline_run_id": run_id,
                        "_loaded_at": loaded_at,
                        "_schema_version": schema_version,
                    }
                    staged.protein_nodes.append(node)
                    _staged_protein_by_id[ac] = node
                    extra_protein_seen.add(ac)
                else:
                    # v35 M-6: opportunistically enrich existing nodes
                    # that lack a name (e.g., from ChEMBL CHEMBL_TGT_xxx
                    # IDs that didn't have UniProt metadata). Use
                    # setdefault so we don't overwrite a more-specific
                    # name from drugbank/uniprot.
                    existing = _staged_protein_by_id.get(ac)
                    if existing is not None:
                        if not existing.get("name") and pname:
                            existing["name"] = pname
                        if not existing.get("organism"):
                            existing["organism"] = _safe_str(
                                row.get("organism") or "Homo sapiens"
                            )
            # Canonical key (sorted) to dedup symmetric edges.
            key = (ac_a, ac_b) if ac_a <= ac_b else (ac_b, ac_a)
            if key in seen_string:
                continue
            seen_string.add(key)
            string_edges.append({
                "src_id": key[0],
                "dst_id": key[1],
                "source": "string",
                "score": _safe_float(row.get("score") or row.get("combined_score")),
                "_source_phase": 1,
                "_source_file": "string_protein_protein_interactions.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            })
        if string_edges:
            staged.edges[("Protein", "interacts_with", "Protein")] = string_edges
            logger.info(
                "Phase1 bridge: staged %d Protein→interacts_with→Protein edges "
                "from string_protein_protein_interactions.csv",
                len(string_edges),
            )

    # ── DisGeNET: Gene→associated_with→Disease (with sub-source attribution) ──
    disgenet = frames.get("disgenet_gda")
    if disgenet is not None and not disgenet.empty:
        disgenet_edges: List[Dict[str, Any]] = []
        seen_disgenet: set[Tuple[str, str]] = set()
        for idx, row in disgenet.iterrows():
            gene_id = _safe_str(row.get("gene_id") or row.get("ncbi_gene_id"))
            did = _safe_str(row.get("disease_id"))
            if not gene_id or not did:
                continue
            # Stage Gene + Disease nodes if missing.
            if gene_id not in extra_gene_seen:
                staged.gene_nodes.append({
                    "id": gene_id,
                    "gene_symbol": _safe_str(row.get("gene_symbol")),
                    "_source_phase": 1,
                    "_source_file": "disgenet_gene_disease_associations.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_gene_seen.add(gene_id)
            if did not in extra_disease_seen:
                staged.disease_nodes.append({
                    "id": did,
                    "name": _safe_str(row.get("disease_name")),
                    "_source_phase": 1,
                    "_source_file": "disgenet_gene_disease_associations.csv",
                    "_source_row": _safe_row_idx(idx),
                    "_pipeline_run_id": run_id,
                    "_loaded_at": loaded_at,
                    "_schema_version": schema_version,
                })
                extra_disease_seen.add(did)
            key = (gene_id, did)
            if key in seen_disgenet:
                continue
            seen_disgenet.add(key)
            disgenet_edges.append({
                "src_id": gene_id,
                "dst_id": did,
                "source": "disgenet",
                "score": _safe_float(row.get("score") or row.get("gda_score")),
                "association_type": _safe_str(row.get("association_type")),
                "_source_phase": 1,
                "_source_file": "disgenet_gene_disease_associations.csv",
                "_source_row": _safe_row_idx(idx),
                "_pipeline_run_id": run_id,
                "_loaded_at": loaded_at,
                "_schema_version": schema_version,
            })
        if disgenet_edges:
            existing = staged.edges.get(("Gene", "associated_with", "Disease"), [])
            staged.edges[("Gene", "associated_with", "Disease")] = existing + disgenet_edges
            logger.info(
                "Phase1 bridge: staged %d Gene→associated_with→Disease edges "
                "from disgenet_gene_disease_associations.csv",
                len(disgenet_edges),
            )

    # ── PubChem: enrich existing Compound nodes with structural properties ──
    pubchem = frames.get("pubchem_enrichment")
    if pubchem is not None and not pubchem.empty:
        n_enriched = 0
        # v35 ROOT FIX (M-7): the previous code linearly scanned
        # ``staged.compound_nodes`` for every PubChem row, giving
        # O(N×M) ≈ 196M comparisons on production-size data (~14K
        # DrugBank × ~14K PubChem). The fix builds a hash dict ONCE
        # before the loop so each lookup is O(1) — total cost drops
        # to O(N+M). The first Compound per inchikey is the canonical
        # one (the bridge dedup upstream ensures inchikey uniqueness).
        _compound_by_inchi: Dict[str, Dict[str, Any]] = {
            c.get("inchikey", ""): c
            for c in staged.compound_nodes
            if c.get("inchikey")
        }
        for idx, row in pubchem.iterrows():
            inchi = _safe_str(row.get("inchikey"))
            if not inchi:
                continue
            # O(1) dict lookup instead of O(M) linear scan.
            c = _compound_by_inchi.get(inchi)
            if c is None:
                continue
            for k in ("canonical_smiles", "isomeric_smiles",
                      "molecular_weight", "xlogp", "tpsa",
                      "complexity", "h_bond_donors", "h_bond_acceptors"):
                v = row.get(k)
                if v is not None and (isinstance(v, str) and v
                                      or isinstance(v, (int, float))):
                    c.setdefault(k, v)
            n_enriched += 1
        if n_enriched:
            logger.info(
                "Phase1 bridge: enriched %d Compound nodes with PubChem "
                "structural properties",
                n_enriched,
            )

    return staged


# ---------------------------------------------------------------------------
# 6. load_into_graph — push staged dicts into a graph builder
# ---------------------------------------------------------------------------
def load_into_graph(
    staged: Phase1StagedData,
    builder: GraphBuilderProtocol,
    *,
    batch_size: int = 500,
) -> Dict[str, Any]:
    """Load a :class:`Phase1StagedData` into any graph builder.

    The builder must satisfy :class:`GraphBuilderProtocol` — both the real
    :class:`drugos_graph.kg_builder.DrugOSGraphBuilder` (with a connected
    Neo4j) and the test-only :class:`RecordingGraphBuilder` qualify.

    Parameters
    ----------
    staged : Phase1StagedData
    builder : GraphBuilderProtocol
    batch_size : int
        Batch size forwarded to ``load_nodes_batch`` / ``load_edges_batch``.

    Returns
    -------
    dict
        Summary report with per-label / per-edge-type counts.
    """
    report: Dict[str, Any] = {
        "nodes_loaded": 0,
        "edges_loaded": 0,
        "by_label": {},
        "by_edge_type": {},
        "errors": [],
    }

    # Compute real input checksum from the ACTUAL files that were read.
    # Uses staged.phase1_processed_dir (captured at stage time) so the
    # checksum is correct even when a non-default dir was supplied. Falls
    # back to DEFAULT_PHASE1_PROCESSED_DIR only if the dir was not recorded.
    base = staged.phase1_processed_dir or DEFAULT_PHASE1_PROCESSED_DIR
    name_map = {
        "drugs": "drugbank_drugs.csv",
        "interactions": "drugbank_interactions.csv.gz",
        "omim_gda": "omim_gene_disease_associations.csv",
        # v6 fix (bug #B9): include the structured indications file in the
        # lineage checksum when present.
        "indications": "drugbank_indications.csv",
        # ROOT FIX (Phase1↔Phase2 100% connection): include the 5 new
        # source CSVs the bridge now consumes, so the lineage checksum
        # reflects the full Phase 1 output set.
        "chembl_drugs": "chembl_drugs.csv",
        "uniprot_proteins": "uniprot_proteins.csv",
        "string_ppi": "string_protein_protein_interactions.csv",
        "disgenet_gda": "disgenet_gene_disease_associations.csv",
        "pubchem_enrichment": "pubchem_enrichment.csv",
        # v15 ROOT FIX (REM-12): include the 2 NEW source CSVs so the
        # lineage checksum reflects the truly-complete Phase 1 output.
        "chembl_activities": "chembl_activities_clean.csv",
        "omim_susceptibility": "omim_gene_disease_susceptibility.csv",
    }
    # v29 ROOT FIX (audit I-10): checksum excluded empty CSVs. Now
    # includes all CSVs for complete lineage. We use
    # ``staged.sources_attempted`` (which includes empty-but-present
    # CSVs) instead of ``staged.sources_read`` (which only includes
    # non-empty ones). ``compute_input_checksum`` already handles
    # missing files gracefully (it hashes the basename + empty content
    # for non-existent paths), so including an attempted-but-missing
    # key in the list is safe and produces a DIFFERENT checksum from
    # the same set of present CSVs without that key — which is exactly
    # the lineage-discrimination property we want.
    #
    # We fall back to ``sources_read`` for backward compatibility if
    # ``sources_attempted`` is empty (e.g. when staged was constructed
    # by older code that doesn't populate it).
    _sources_for_checksum = (
        staged.sources_attempted if staged.sources_attempted
        else staged.sources_read
    )
    real_paths = [base / name_map[k] for k in _sources_for_checksum if k in name_map]
    input_checksum = compute_input_checksum(real_paths)

    # Nodes ────────────────────────────────────────────────────────────────
    for label, nodes in (
        ("Compound", staged.compound_nodes),
        ("Protein", staged.protein_nodes),
        ("Gene", staged.gene_nodes),
        ("Disease", staged.disease_nodes),
        # FIX-F / C-16: load the new ClinicalOutcome nodes (5th node type
        # mandated by the DOCX Phase 2 spec).
        ("ClinicalOutcome", staged.clinical_outcome_nodes),
    ):
        if not nodes:
            report["by_label"][label] = 0
            continue
        try:
            n = builder.load_nodes_batch(
                label=label,
                nodes=list(nodes),
                batch_size=batch_size,
                source="phase1_bridge",
                input_checksum=input_checksum,
            )
            n_int = int(n) if not isinstance(n, int) else n
            report["by_label"][label] = n_int
            report["nodes_loaded"] += n_int
        except Exception as exc:
            logger.exception("Phase1 bridge: failed to load %s nodes", label)
            report["errors"].append(f"{label}: {exc}")
            report["by_label"][label] = 0

    # Edges ────────────────────────────────────────────────────────────────
    for (src, rel, dst), edges in staged.edges.items():
        if not edges:
            continue
        try:
            n = builder.load_edges_batch(
                src_label=src,
                rel_type=rel,
                dst_label=dst,
                edges=list(edges),
                batch_size=batch_size,
                source="phase1_bridge",
                input_checksum=input_checksum,
            )
            n_int = int(n) if not isinstance(n, int) else n
            report["by_edge_type"][f"({src}, {rel}, {dst})"] = n_int
            report["edges_loaded"] += n_int
        except Exception as exc:
            logger.exception(
                "Phase1 bridge: failed to load %s-%s-%s edges", src, rel, dst
            )
            report["errors"].append(f"{src}/{rel}/{dst}: {exc}")
            report["by_edge_type"][f"({src}, {rel}, {dst})"] = 0

    return report


# ---------------------------------------------------------------------------
# 7. run_phase1_to_phase2 — top-level convenience
# ---------------------------------------------------------------------------
# v29 ROOT FIX (audit I-12): bridge work was discarded. Now documents
# that run_full_pipeline should reuse bridge output.
#
# Forensic audit finding I-12: ``run_full_pipeline``'s step 1 calls
# ``run_phase1_to_phase2`` (this function) which stages ALL Phase 1
# outputs into a ``Phase1StagedData`` object — including the full
# ``compound_nodes`` list (with InChIKey, drugbank_id, name, smiles,
# withdrawn, fda_approved, etc.). Step 4 (``step4_drugbank_enrichment``)
# then RE-READS ``drugbank_drugs.csv`` from disk to re-derive the
# ``drug_records`` list that step 8 (entity resolution) and step 10
# (training data) consume. This is duplicate work — the bridge already
# produced equivalent data in step 1.
#
# ROOT FIX:
#   * ``run_phase1_to_phase2``'s return dict already includes
#     ``"staged": Phase1StagedData`` — this is the canonical staged
#     output. Callers (especially ``run_full_pipeline``) SHOULD reuse
#     ``staged.compound_nodes`` (via the helper
#     ``extract_drug_records_from_staged``) instead of re-reading the
#     CSV in step 4.
#   * ``step1_load_phase1`` in run_pipeline.py now passes the
#     ``Phase1StagedData`` through its return dict as
#     ``"bridge_staged"``, and step 4's ``data_source="phase1"`` branch
#     now consumes it via the helper, eliminating the re-read.
#   * Legacy callers that don't pass ``bridge_staged`` through still
#     work — step 4 falls back to re-reading the CSV (the old
#     behavior). The fix is opt-in via the new code path.
def extract_drug_records_from_staged(
    staged: "Phase1StagedData",
) -> List[Dict[str, Any]]:
    """Convert a :class:`Phase1StagedData` object's Compound nodes into
    the ``drug_records`` list format that ``step4_drugbank_enrichment``
    produces and that ``step8_entity_resolution`` /
    ``step10_training_data`` consume.

    v29 ROOT FIX (audit I-12): this helper exists so that
    ``run_full_pipeline`` can reuse the bridge's already-staged
    Compound nodes (built in step 1) in step 4 / 8 / 10 — instead of
    re-reading ``drugbank_drugs.csv`` from disk. Each output dict has
    the same schema as ``drugbank_to_node_records_from_phase1``'s
    output (``id``, ``drugbank_id``, ``name``, ``inchikey``,
    ``smiles``, ``withdrawn``, ``fda_approved``, ...).

    v35 ROOT FIX (M-8): the previous extraction pulled 5 fields that
    are NOT on the staged Compound node schema (``indication``,
    ``atc_codes``, ``description``, ``toxicity``,
    ``pharmacodynamics``) and therefore returned None for every row.
    Conversely, 5 fields that ARE on the staged node
    (``molecular_weight``, ``molecular_formula``, ``chembl_id``,
    ``completeness_score``, ``safety_data_missing``) were NOT
    extracted. The fix:

      * Adds the 5 missing staged fields to the extraction dict.
      * Keeps the 5 fields that are absent on staged nodes for
        backward compat (they still resolve to None), but documents
        that they are ONLY populated by ``step4_drugbank_enrichment``'s
        raw-XML / Phase-1-CSV path (``drugbank_to_node_records_from_phase1``),
        NOT by the bridge's staged Compound schema. Callers needing
        these fields should invoke ``step4_drugbank_enrichment``
        directly (see H-4 docstring for reachability notes).

    Parameters
    ----------
    staged : Phase1StagedData
        The staged data object returned by ``run_phase1_to_phase2`` /
        ``stage_phase1_to_phase2``.

    Returns
    -------
    list of dict
        One dict per Compound node, in the drug_records format.
    """
    out: List[Dict[str, Any]] = []
    for n in staged.compound_nodes:
        # The staged Compound nodes already have the schema we need.
        # Re-key to match drugbank_to_node_records_from_phase1's output
        # so downstream code can consume either source interchangeably.
        # v35 M-8: only extract fields that exist on the staged node;
        # for fields NOT on the staged schema (indication, atc_codes,
        # description, toxicity, pharmacodynamics) we still emit the
        # key with None for backward compat with downstream code that
        # expects the key to exist, but they will only be populated
        # by step4_drugbank_enrichment's drugbank_to_node_records_from_phase1.
        out.append({
            "id": n.get("id"),
            "drugbank_id": n.get("drugbank_id"),
            "name": n.get("name"),
            "inchikey": n.get("inchikey"),
            "smiles": n.get("smiles"),
            # v35 M-8: ADDED — these are on the staged Compound schema
            # but were missing from the extraction (returned None
            # downstream, losing data the bridge HAD captured).
            "molecular_weight": n.get("molecular_weight"),
            "molecular_formula": n.get("molecular_formula"),
            "chembl_id": n.get("chembl_id"),
            "completeness_score": n.get("completeness_score"),
            "safety_data_missing": n.get("safety_data_missing"),
            # v35 M-8: KEPT — these fields are NOT on the staged node
            # schema (they're populated by step4_drugbank_enrichment's
            # raw-XML / Phase-1-CSV path via
            # drugbank_to_node_records_from_phase1). The keys remain
            # for backward compat with downstream consumers, but will
            # be None when sourced from the staged node. Callers
            # needing these fields must use the step4 path.
            "indication": n.get("indication"),
            "mechanism_of_action": n.get("mechanism_of_action"),
            "atc_codes": n.get("atc_codes"),
            "approved": n.get("fda_approved"),
            "withdrawn": n.get("withdrawn"),
            "cas_number": n.get("cas_number"),
            "pubchem_cid": n.get("pubchem_cid"),
            "description": n.get("description"),
            "toxicity": n.get("toxicity"),
            "pharmacodynamics": n.get("pharmacodynamics"),
            "_source_phase": n.get("_source_phase", 1),
            "_source_file": n.get("_source_file", "phase1_bridge"),
            "_source_row": n.get("_source_row", 0),
        })
    return out


def run_phase1_to_phase2(
    phase1_processed_dir: Optional[Path | str] = None,
    builder: Optional[GraphBuilderProtocol] = None,
    *,
    batch_size: int = 500,
    run_id: Optional[str] = None,
    prefer_postgres: bool = True,
) -> Dict[str, Any]:
    """Read Phase 1 outputs → stage → load into a graph builder.

    If ``builder`` is None, a :class:`RecordingGraphBuilder` is used (useful
    for dry-runs and tests).

    v29 ROOT FIX (Phase1↔Phase2 100% connection): the ``prefer_postgres``
    flag controls whether Phase 1's PostgreSQL ORM is the authoritative
    backend (default True) or whether the CSV fallback is used. The chosen
    backend is returned as ``summary["backend"]`` so operators can verify
    the production path.

    v29 ROOT FIX (audit I-12): the returned dict's ``"staged"`` key
    carries the full :class:`Phase1StagedData` (including
    ``compound_nodes``). Callers that need a ``drug_records`` list
    (e.g. ``run_full_pipeline`` step 4) SHOULD reuse the staged data
    via :func:`extract_drug_records_from_staged` instead of re-reading
    ``drugbank_drugs.csv`` from disk. This eliminates the duplicate
    CSV read that step 4 was performing.

    Returns
    -------
    dict
        ``{"staged": Phase1StagedData, "builder": builder, "load_report": dict, "summary": dict, "backend": str}``
    """
    if builder is None:
        builder = RecordingGraphBuilder()

    frames = read_phase1_outputs(
        phase1_processed_dir, prefer_postgres=prefer_postgres,
    )
    # frames is a dict that ALSO carries a "_phase1_backend" marker key.
    backend = frames.pop("_phase1_backend", _PHASE1_BACKEND_CSV)
    staged = stage_phase1_to_phase2(
        frames, run_id=run_id, phase1_processed_dir=phase1_processed_dir
    )
    load_report = load_into_graph(staged, builder, batch_size=batch_size)

    summary = {
        "bridge_version": PHASE1_TO_PHASE2_BRIDGE_VERSION,
        "sources_read": staged.sources_read,
        # v29 ROOT FIX (audit I-10): expose sources_attempted so
        # operators can verify which CSVs the bridge tried to load
        # (including empty ones).
        "sources_attempted": staged.sources_attempted,
        "nodes_staged": staged.total_nodes,
        "edges_staged": staged.total_edges,
        "nodes_loaded": load_report["nodes_loaded"],
        "edges_loaded": load_report["edges_loaded"],
        "edge_types_present": [
            f"({s}, {r}, {d})" for (s, r, d) in staged.edge_types_present()
        ],
        "warnings": staged.warnings,
        "errors": load_report["errors"],
        "backend": backend,
    }
    return {
        "staged": staged,
        "builder": builder,
        "load_report": load_report,
        "summary": summary,
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# 8. bridge_to_pyg_maps — convert a RecordingGraphBuilder into the
#    (entity_maps, edge_maps) format expected by PyGBuilder.build_from_drkg
#    and step11_train_transe. v6 fix (bug #B3): the previous
#    VERIFICATION.md "Full ML Chain" snippet had a literal
#    `# ... map src/dst local IDs ...` placeholder that crashed with
#    `ValueError: too many values to unpack (expected 2)`. This helper
#    replaces the placeholder with a real, tested implementation.
# ---------------------------------------------------------------------------
def bridge_to_pyg_maps(
    builder: "RecordingGraphBuilder",
) -> Tuple[
    Dict[str, Dict[str, int]],
    Dict[Tuple[str, str, str], Tuple[List[int], List[int]]],
]:
    """Convert a :class:`RecordingGraphBuilder` (post-load) into the
    ``(entity_maps, edge_maps)`` format expected by
    :meth:`drugos_graph.pyg_builder.PyGBuilder.build_from_drkg` and
    :func:`drugos_graph.run_pipeline.step11_train_transe`.

    Parameters
    ----------
    builder : RecordingGraphBuilder
        A builder that has already been populated by
        :func:`load_into_graph` (i.e. ``builder.node_loads`` and
        ``builder.edge_loads`` are non-empty).

    Returns
    -------
    entity_maps : dict
        ``{node_label: {node_id: int_index}}`` where indices form a
        contiguous ``[0, N-1]`` range per label.
    edge_maps : dict
        ``{(src_label, rel, dst_label): (src_indices, dst_indices)}``
        where each list contains ints indexing into the corresponding
        ``entity_maps`` label.

    Raises
    ------
    ValueError
        If the builder is empty or any edge references an unknown node.
    """
    # Build entity_maps: {label: {id: idx}} with contiguous per-label indices.
    entity_maps: Dict[str, Dict[str, int]] = {}
    for load in builder.node_loads:
        label = load["label"]
        if label not in entity_maps:
            entity_maps[label] = {}
        for i, n in enumerate(load["nodes"]):
            nid = n["id"]
            if nid not in entity_maps[label]:
                entity_maps[label][nid] = len(entity_maps[label])

    # Build edge_maps: {(src, rel, dst): (src_idx_list, dst_idx_list)}.
    # v43 ROOT FIX (P2-019): the previous code raised ValueError on the
    # FIRST unknown node reference, without reporting how many edges
    # failed in total. The fix collects all failures and raises a single
    # aggregate error with counts, so operators can see the full scope
    # of the referential-integrity problem.
    edge_maps: Dict[Tuple[str, str, str], Tuple[List[int], List[int]]] = {}
    _total_failed_edges = 0
    _failure_samples: list = []
    for load in builder.edge_loads:
        key = (load["src_label"], load["rel_type"], load["dst_label"])
        src_map = entity_maps.get(key[0], {})
        dst_map = entity_maps.get(key[2], {})
        src_list: List[int] = []
        dst_list: List[int] = []
        for e in load["edges"]:
            sid = e["src_id"]
            did = e["dst_id"]
            if sid not in src_map:
                _total_failed_edges += 1
                if len(_failure_samples) < 5:
                    _failure_samples.append(
                        f"unknown src node {sid!r} in label {key[0]!r} "
                        f"(edge type {key})"
                    )
                continue
            if did not in dst_map:
                _total_failed_edges += 1
                if len(_failure_samples) < 5:
                    _failure_samples.append(
                        f"unknown dst node {did!r} in label {key[2]!r} "
                        f"(edge type {key})"
                    )
                continue
            src_list.append(src_map[sid])
            dst_list.append(dst_map[did])
        if src_list:
            if key in edge_maps:
                # Merge with existing lists (preserves order).
                old_s, old_d = edge_maps[key]
                edge_maps[key] = (old_s + src_list, old_d + dst_list)
            else:
                edge_maps[key] = (src_list, dst_list)
    # v43 ROOT FIX (P2-019): raise a single aggregate error with counts.
    if _total_failed_edges > 0:
        raise ValueError(
            f"bridge_to_pyg_maps: {_total_failed_edges} edge(s) reference "
            f"unknown src/dst nodes and were skipped. Sample failures: "
            f"{_failure_samples}. The remaining edges were loaded "
            f"successfully. Check the bridge's node staging for missing "
            f"nodes."
        )

    if not entity_maps:
        raise ValueError(
            "bridge_to_pyg_maps: builder has no node_loads — call "
            "load_into_graph() first."
        )

    return entity_maps, edge_maps
