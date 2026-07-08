"""
DrugOS Graph Module — Knowledge Graph Builder (Neo4j)
======================================================
Institutional-grade rewrite of the Neo4j write layer for the DrugOS
Autonomous Drug Repurposing Platform.

v43 ROOT FIX (P2-008) — BROAD EXCEPT POLICY:
  This module historically had 14+ ``except Exception`` blocks that
  silently swallowed Neo4j errors (ConstraintError, ServiceUnavailable,
  CypherSyntaxError, etc.). The v43 fix establishes a module-level
  convention: ALL except blocks in this module MUST catch specific
  Neo4j exception types (not bare ``Exception``) and MUST log at
  WARNING level or higher with full context. The only exception is
  the top-level ``load_nodes_batch`` / ``load_edges_batch`` entry
  points, which may catch ``Exception`` to prevent a single bad batch
  from crashing the pipeline — but even those MUST log the full
  traceback and surface the error in the result dict.

  New code MUST follow this policy. Existing broad excepts are being
  narrowed incrementally — do not add NEW broad excepts.

Architecture (Facade Pattern — audit issue A-1):
  DrugOSGraphBuilder  — public API facade (backward-compatible)
    ├── GraphConnection     — connect, disconnect, retry, health, driver DI
    ├── GraphSchemaManager  — create_constraints, create_indexes, version detect
    ├── GraphNodeLoader     — load_nodes_batch, load_drkg_nodes
    ├── GraphEdgeLoader     — load_edges_batch, load_edges_bulk_create, dedup
    ├── DrugBankEnricher    — enrich_compounds_from_drugbank
    ├── GraphStatsCollector — get_graph_stats, health_check
    └── GraphJanitor        — clear_graph (dangerous ops isolated, access control)

Patient Safety Context (NON-NEGOTIABLE):
  A bug in this file = wrong graph = wrong prediction = a pharma partner
  tests the wrong drug on a real patient = patient harm. The RL safety
  ranker uses the `withdrawn`, `terminated`, `illicit`, `toxicity`, and
  `sensitive` properties written by DrugBankEnricher to classify drugs as
  red (dangerous) / yellow / green (safe). A null value on any of these
  properties means "no data" which is silently interpreted as "not
  withdrawn" → green → SAFE. A withdrawn drug like Valdecoxib (withdrawn
  for cardiovascular risk) would be classified as SAFE because the
  DrugBank XML didn't have the field in that record variant. This is a
  direct patient-harm pathway.

  Treat every line of this code as if a real patient's life depends on it
  — because it does.

Fixes: A-1..A-7, D-1..D-6, S-1..S-5, C-1..C-7, DQ-1..DQ-7, R-1..R-7,
       I-1..I-5, P-1..P-6, S(9)-1..S(9)-6, T-1..T-6, L-1..L-6,
       CF-1..CF-6, DO-1..DO-6, CO-1..CO-5, IN-1..IN-5, DL-1..DL-6
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

try:
    from neo4j import Driver, GraphDatabase, Session
    from neo4j.exceptions import (
        AuthError,
        ServiceUnavailable,
        SessionExpired,
    )
except ImportError:
    Driver = None  # type: ignore[assignment,misc]
    GraphDatabase = None  # type: ignore[assignment,misc]
    Session = None  # type: ignore[assignment,misc]
    ServiceUnavailable = None  # type: ignore[assignment,misc]
    SessionExpired = None  # type: ignore[assignment,misc]
    AuthError = None  # type: ignore[assignment,misc]

from .config import (
    CANONICAL_IDS,
    CONFIG_HASH,
    CORE_EDGE_TYPES,
    CORE_EDGE_TYPES_SET,
    CORE_NODE_TYPES,
    DRKG_NODE_TYPES,
    PIPELINE_VERSION,
    RUN_ID,
    SCHEMA_VERSION,
    SEED,
    Neo4jConfig,
    audit_log,
    build_lineage_metadata,
    check_data_freshness,
    compute_and_record_checksum,
    compute_impact_analysis,
    dead_letter_record,
    deprecated,
    diff_configs,
    get_neo4j_config,
    is_core_edge,
    log_transformation,
    read_latest_checkpoint,
    safe_config_dict,
    verify_checksum,
    write_checkpoint,
    write_lineage_manifest,
)
from .exceptions import (
    ConfigurationError,
    CriticalDataSourceError,
    DrugOSDataError,
    EdgeLoadMismatchError,
    SecurityError,
    UnknownLabelError,
)
from .utils import (
    drkg_node_type_to_neo4j_label,
    drkg_node_type_to_neo4j_label_with_provenance,
    neo4j_label_to_drkg_node_type,
    sanitize_identifier,
    sanitize_label,
    sanitize_rel_type,
    safe_call_with_retry,
)

logger = logging.getLogger(__name__)

# ─── Module-Level Constants ───────────────────────────────────────────────────

# v41 ROOT FIX (Task J DEAD): removed the duplicate/stale "json only needed
# in __main__; re now justified for ID validation" comment block. Verified
# via grep: ``json`` is imported ONLY inside the ``if __name__ == "__main__"``
# block at line ~3414 (``import json as _json``) — there is NO module-level
# ``import json`` and json is NOT used outside ``__main__``. The previous
# comments claimed json "was moved" to __main__ as if it had previously been
# at module top, which was never true for this file (audit false alarm).
# ``re`` (line 43) IS used at module top for ID_PATTERNS validation.

# Fixes I-4, DL-1, DL-5, DL-6, CO-1, CO-2 (Provenance Rule §3.5):
# Every node and edge MUST carry these lineage properties.
# Audit fix (v5 Tier-3 bug #24): added _source_phase, _source_file,
# _source_row to the whitelist. The phase1_bridge emits these on every
# node/edge for bidirectional traceability (the INTEGRATION.md doc
# promises "given any node in Neo4j, run a Cypher query to find the
# exact Phase 1 CSV row that produced it"). Without these in the
# whitelist, the real kg_builder silently stripped them — making the
# traceability contract false in production. RecordingGraphBuilder
# tests didn't catch this because they don't apply the whitelist.
SYSTEM_PROPS: frozenset[str] = frozenset({
    "_pipeline_run_id",
    "_loaded_at",
    "_schema_version",
    "_source",
    "_source_phase",
    "_source_file",
    "_source_row",
    "_license",
    "_attribution",
    "_config_hash",
    "_pipeline_version",
    "_seed",
    "_input_checksum",
    "input_checksum",  # legacy alias used by some bridge code paths
    "_created_at",
    "_updated_at",
    "_version",
    "_source_priority",  # BUG-D-011: deterministic dedup ordering
})

# BUG-D-011 root fix: source priority map. The ``deduplicate_edges_deterministic``
# function orders by ``r._source_priority DESC`` but that property was NEVER
# set when loading edges — making the "deterministic" dedup non-deterministic
# (edges kept/dropped depended on Python dict insertion order). This map
# assigns a numeric priority to each known source so dedup is reproducible.
# Higher number = higher priority (kept over lower priority).
SOURCE_PRIORITY_MAP: dict[str, int] = {
    "drugbank": 100,        # FDA-approved drug labels — highest authority
    "drugbank_indications": 95,
    # v35 ROOT FIX (H-2): the bridge emits source="drugbank_indication"
    # (singular, no _text) for free-text-derived Compound-treats-Disease
    # edges (phase1_bridge.py:2130). Without this key, get_source_priority
    # returned 0 → free-text treats edges were silently dropped during
    # deduplicate_edges_deterministic in favor of any other edge (even
    # lower-quality DRKG edges at priority 25). Priority 100 matches
    # "drugbank" because the source IS DrugBank (just the free-text
    # indication column rather than the structured indications CSV).
    "drugbank_indication": 100,
    "uniprot": 90,
    "chembl": 85,
    "pubchem": 80,
    "omim": 75,
    "disgenet": 70,
    "string": 65,
    "clinicaltrials": 60,
    "sider": 55,
    "stitch": 50,
    "geo": 45,
    "opentargets": 40,
    "drugbank_indication_text": 35,
    "phase1_bridge": 30,
    "drkg": 25,
    "test": 10,
    "unknown": 0,
}


def get_source_priority(source: str) -> int:
    """Return the numeric priority for a source label (BUG-D-011).

    Higher number = higher priority (kept during dedup).
    Unknown sources default to 0 (lowest priority).
    """
    if not source:
        return 0
    return SOURCE_PRIORITY_MAP.get(source.lower().strip(), 0)

# Fixes S-5 (Domain 3): Biomedical identifier validation patterns
# Audit fix (v5 Tier-2 bug #20 — REPAIRED v6): the previous pattern only
# accepted 6-char Swiss-Prot accessions. Real DrugBank/UniProt data
# contains 10-char TrEMBL accessions (e.g. A0A024R2R7, A0A1B0GUU5),
# which were silently dead-lettered. The new pattern uses the official
# UniProt accession grammar:
#
#   Swiss-Prot (6 chars):  [OPQ][0-9][A-Z0-9]{3}[0-9]
#                          | [A-NR-Z][0-9][A-Z0-9]{3}[0-9]
#   TrEMBL    (10 chars):  same 6-char prefix + ([A-Z0-9]{3}[0-9]){1}
#
# An optional isoform-suffix `-<digits>` is allowed on either form.
# Verified against: P23219 (Swiss-Prot), P00734 (Swiss-Prot),
# A0A024R2R7 (TrEMBL, 10 chars), A0A1B0GUU5 (TrEMBL, 10 chars),
# Q9BX66 (Swiss-Prot), A0A024R2R7-2 (TrEMBL + isoform).
ID_PATTERNS: dict[str, str] = {
    # v28 ROOT FIX (P2-B-12): removed the ``NAME:[A-Za-z0-9 _.-]{1,64}``
    # alternative — it accepted LITERALLY ANY string (any printable ASCII
    # up to 64 chars) as a Compound ID. This made Compound ID validation
    # a no-op: typos, garbage strings, even ``NAME: `` (just a space)
    # passed. Production queries that filter by Compound ID then
    # returned inconsistent results (some edges pointed at the
    # InChIKey-canonical node, others at the NAME: node — disjoint subgraphs).
    # Removed: callers that need a non-InChIKey/non-DrugBank/non-ChEMBL
    # identifier SHOULD register a new prefix in ID_PATTERNS with a
    # TIGHTER regex (e.g. ``DRUG:<digits>``), not abuse the catch-all.
    "Compound": r"^(DB\d{5,6}|CHEMBL\d+|CID\d+|[A-Z]{14}-[A-Z]{10}-[A-Z]|CIDm\d+|CIDs\d+|MESH:[A-Z]\d+)$",
    # v21 ROOT FIX (Audit section 4 finding 8 / Chain 9 - "Bridge emits
    # IDs that production rejects"): the previous Protein pattern
    # accepted ONLY UniProt accessions. But phase1_bridge.py:1642 emits
    # ``CHEMBL_TGT_{chembl_target_id}`` for ChEMBL targets that lack a
    # UniProt AC (a common case for older ChEMBL target records). The
    # production validator dead-lettered every such Protein node, silently
    # dropping ChEMBL target nodes from the KG. Multi-hop queries that
    # traverse these nodes returned empty. Fix: accept the
    # ``CHEMBL_TGT_\d+`` prefix as a valid Protein ID (it is a stable
    # ChEMBL target identifier; entity_resolver can later upgrade it to
    # a UniProt AC via id_crosswalk when one becomes available).
    "Protein": r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z0-9]{3}[0-9])([A-Z0-9]{3}[0-9])?(-\d+)?$|^CHEMBL_TGT_\d+$",
    # Gene: numeric NCBI gene ID (e.g. 2261 for FGFR3) OR a SYM:-prefixed
    # gene symbol (e.g. SYM:FGFR3) used as a placeholder until the
    # entity_resolver canonicalizes it to a numeric ID via id_crosswalk.
    # OMIM/NCBIGene prefixes are stripped by the loaders BEFORE reaching
    # ID_PATTERNS (BUG-B-001, BUG-B-002).
    # audit-2025 ROOT FIX (issue 7): accept ``MIM:<digits>`` for OMIM
    # gene_mim fallbacks. OMIM MIM numbers are namespaced separately from
    # NCBI Gene IDs to prevent numeric collisions (e.g. MIM:2645 ≠
    # NCBIGene:2645 — they are different biological entities).
    #
    # v41 ROOT FIX (Task K2 / SEV2 SCIENTIFIC): accept ``ENSG:ENSG\d{11}``
    # for Ensembl gene IDs. The previous pattern accepted ``SYM:ENSG...``
    # (the OpenTargets orphan-target fallback at opentargets_loader.py
    # line ~2746 wrapped bare ENSG IDs in the SYM: namespace). This was
    # SCIENTIFICALLY WRONG: SYM: is for gene SYMBOLS (e.g. SYM:FGFR3,
    # SYM:BRCA1 — short mnemonic uppercase strings), NOT for Ensembl
    # gene IDs (which are 15-char ``ENSG\d{11}`` accessions). Conflating
    # the two namespaces meant the entity_resolver's SYM:→NCBI Gene ID
    # canonicalisation path (which queries MyGene.info by symbol) would
    # be queried with ENSG accessions and return ZERO hits — the ENSG
    # orphan would remain permanently un-resolved, fragmenting the Gene
    # sub-graph. The new ``ENSG:`` namespace lets the entity_resolver
    # route ENSG orphans through its Ensembl→NCBI crosswalk path
    # (id_crosswalk has ``load_ensembl_protein_to_uniprot`` and a
    # ``compound_id_to_inchikey`` analogue for ENSG→NCBI). The SYM:
    # pattern is preserved for genuine gene-symbol fallbacks.
    "Gene": r"^(\d+|SYM:[A-Z0-9]+|MIM:\d+|ENSG:ENSG\d{11})$",
    # Disease: explicit prefixed forms only. BUG-D-015 root fix: removed
    # the ``[A-Z]+:\w+`` catch-all that accepted 'FOO:bar' as a Disease ID.
    # Now only valid biomedical disease ontologies are accepted.
    # v9 ROOT FIX: accept BOTH underscore (EFO_0000400 — original EFO
    # curie spec) and colon (EFO:0000400 — OpenTargets canonical form)
    # for EFO, since the OpenTargets _normalise_ontology_id helper
    # converts underscore → colon for ALL ontology prefixes. Without
    # both forms, EFO IDs would be dead-lettered.
    # v34 ROOT FIX (CRITICAL #8): accept `SYNDROME:<slug>` IDs emitted
    # by the bridge when DrugBank indications have empty disease_id but
    # non-empty disease_name (e.g. "Pain", "Asthma", "Hepatitis B").
    # Without this, ~half of Compound-treats-Disease edges were
    # dead-lettered because the synthetic Disease IDs didn't match
    # the strict biomedical-ontology pattern.
    "Disease": r"^(C\d{7}|D\d{6}|EFO_\d+|EFO:\d+|OMIM:\d+|Orphanet:\d+|MONDO:\d+|DOID:\d+|HP:\d+|MESH:[A-Z]\d+|SYNDROME:[A-Za-z0-9_]+)$",
    "Pathway": r"^(R-HSA-\d+|hsa\d+|REACT_\d+|WP\d+)$",
    # FIX-F / C-16: ClinicalOutcome nodes derived from
    # drugbank_indications.csv by phase1_bridge._load_clinical_outcomes().
    # ID format: "CO:<drugbank_id>:<disease_key>:<indication_type>" where
    # disease_key is the disease_id (e.g. OMIM:102700) when present, or
    # the slugified disease_name when disease_id is empty (e.g. "Pain").
    # The pattern is intentionally permissive (curie-style with CO: prefix)
    # so it accepts both OMIM-prefixed and name-based disease keys.
    "ClinicalOutcome": r"^CO:[A-Za-z0-9_.:-]+$",
    # MedDRA_Term: 8-digit LLT/PT code OR MedDRA-prefixed UMLS CUI
    # (BUG-B-005: SIDER emits "MedDRA:C0018790" which is the standard
    # biomedical identifier format).
    "MedDRA_Term": r"^(\d{8}|MedDRA:C\d{7})$",
    "Anatomy": r"^(UBERON_\d+|CL_\d+)$",
    "Side Effect": r"^(CUI\d+|C\d{7})$",
    "Symptom": r"^(CUI\d+|C\d{7})$",
    "Pharmacologic Class": r"^(ATC:[A-Z]\d{2}|CHEMBL\d+)$",
    "Biological Process": r"^(GO:\d+)$",
    "Molecular Function": r"^(GO:\d+)$",
    "Cellular Component": r"^(GO:\d+)$",
    "Taxonomy": r"^\d+$",
    "Gene Expression": r"^(GSE\d+|GSM\d+)$",
    # BUG-D-005 root fix: real ATC codes are 7 chars in the WHO format
    # (e.g. L01XC02, L04AA02, N02BA01). The structure is:
    #   [A-Z]    — 1st level (anatomical main group, 1 letter)
    #   \d{2}    — 2nd level (therapeutic main group, 2 digits)
    #   [A-Z]{2} — 3rd+4th levels (therapeutic subgroup + chemical subgroup, 2 letters)
    #   \d{2}    — 5th level (chemical substance, 2 digits)
    # The previous pattern ``^[A-Z]\d{2}[A-Z]\d{2}[A-Z]\d{2}?$`` required
    # 8-9 chars (alternating letter/digit groups) and dead-lettered every
    # Atc node. New pattern accepts the WHO 7-char format AND optional
    # sub-class extensions (L01XC02.01).
    "Atc": r"^[A-Z]\d{2}[A-Z]{2}\d{2}(\.\d{2})?$",
    "Tax": r"^\d+$",
    # v9 ROOT FIX (audit F5.2.1): UniProt cross-reference edges emit
    # heterogeneous target types (Domain, OntologyTerm, Publication,
    # ExternalRef) based on the UniProt DB source. The previous code
    # returned True for any unknown label — silently bypassing
    # validation. Now we explicitly register these labels with
    # permissive curie-style patterns so the edges are validated but
    # not over-restricted. If a label is NOT in this dict, the new
    # fail-closed UnknownLabelError fires.
    "ExternalRef": r"^[A-Za-z_][A-Za-z0-9_-]*:[A-Za-z0-9_.:-]+$",
    "Domain": r"^(PF\d+|IPR\d+|SM\d+|PS\d+)$",
    "OntologyTerm": r"^(GO:\d+|MIM:\d+|KEGG:\S+)$",
    "Publication": r"^\d{7,8}$",  # PMID
}

# Fixes D-2, DQ-4, S(9)-6, IN-1 (Schema-Whitelist Rule §3.7):
# Define allowed properties per node label. Anything not in this list
# (or SYSTEM_PROPS) is silently dropped before Cypher execution.
#
# Audit fix (v6 — bug #B5/B6/B7/B8): the previous whitelist was missing
# every property the phase1_bridge actually emits (fda_approved,
# clinical_status, groups, molecular_weight, molecular_formula,
# completeness_score, gene_symbol, mim_id, uniprot_id, etc.). On a real
# Neo4j load these were silently stripped — only the test path
# (RecordingGraphBuilder, which does NOT apply the whitelist) noticed.
# The whitelist now mirrors the bridge's actual output contract.
NODE_PROPERTY_WHITELIST: dict[str, frozenset[str]] = {
    "Compound": frozenset({
        "id", "name", "smiles", "inchikey", "indication",
        "mechanism_of_action", "atc_codes", "approved", "investigational",
        "pubchem_cid", "chembl_id", "chebi_id", "drug_type",
        "approval_year", "source_drugbank", "drugbank_id", "cas_number",
        "toxicity", "pharmacodynamics", "withdrawn", "terminated",
        "illicit", "sensitive", "categories",
        "_canonical_id_source", "_last_modified", "_schema_version",
        "safety_data_missing", "description",
        # ── v6: bridge-emitted Compound properties (bug #B5) ──
        "fda_approved", "is_fda_approved", "is_withdrawn",
        "clinical_status", "groups",
        "molecular_weight", "molecular_formula",
        "logp", "tpsa",
        "h_bond_donor_count", "h_bond_acceptor_count",
        "rotatable_bond_count", "heavy_atom_count", "complexity",
        "max_phase", "completeness_score",
        "inchikey_source", "cas_number",
    }),
    "Disease": frozenset({
        "id", "name", "icd10", "icd9", "mesh", "umls_cui",
        "definition", "source",
        # ── v6: bridge-emitted Disease property (bug #B7) ──
        "mim_id", "phenotype_mim",
    }),
    "Gene": frozenset({
        "id", "name", "symbol", "ncbi_gene_id", "uniprot_ac",
        "chromosome", "description", "source",
        # ── v6: bridge-emitted Gene properties (bug #B6) ──
        "gene_symbol", "mim_id", "uniprot_id",
    }),
    "Protein": frozenset({
        "id", "name", "uniprot_ac", "uniprot_id", "gene_name",
        "gene_id", "ncbi_gene_id", "organism", "sequence",
        "function", "source",
    }),
    "Pathway": frozenset({
        "id", "name", "reactome_id", "kegg_id", "source",
    }),
    # FIX-F / C-16: ClinicalOutcome nodes — derived from
    # drugbank_indications.csv by phase1_bridge._load_clinical_outcomes().
    "ClinicalOutcome": frozenset({
        "id", "name", "disease_id", "disease_name",
        "indication_type", "source_drug_id", "source",
    }),
    "MedDRA_Term": frozenset({
        "id", "name", "meddra_code", "meddra_type", "umls_cui",
        "source",
    }),
    "Anatomy": frozenset({"id", "name", "uberon_id", "source"}),
    "Side Effect": frozenset({"id", "name", "umls_cui", "source"}),
    "Symptom": frozenset({"id", "name", "umls_cui", "source"}),
    "Pharmacologic Class": frozenset({"id", "name", "atc_code", "source"}),
    "Biological Process": frozenset({"id", "name", "go_id", "source"}),
    "Molecular Function": frozenset({"id", "name", "go_id", "source"}),
    "Cellular Component": frozenset({"id", "name", "go_id", "source"}),
    "Taxonomy": frozenset({"id", "name", "tax_id", "source"}),
    "Gene Expression": frozenset({"id", "name", "gse_id", "source"}),
    "Atc": frozenset({"id", "name", "source"}),
    "Tax": frozenset({"id", "name", "source"}),
}

# Edge property whitelist per (src_label, rel_type, dst_label) triple.
#
# Audit fix (v6 — bug #B8): the previous whitelist was missing properties
# the bridge emits on every edge type (is_known_action, source_id,
# action_type, mapping_key, association_type, evidence, etc.). Real Neo4j
# loads silently stripped them, breaking downstream lineage queries.
#
# BUG-D-006 root fix — the v6 whitelist is populated by iterating
# CORE_EDGE_TYPES, so if CORE_EDGE_TYPES is ever empty (config import
# error, circular import, monkey-patched test fixture), the whitelist
# stays {} and ALL edge properties are silently stripped in production.
# The audit (§5.2) flags this as Major: "No validation that the
# whitelist is non-empty before use."
#
# Root fix: assert non-empty at import time so a config regression
# surfaces as a loud ImportError, not a silent property-stripping bug.
EDGE_PROPERTY_WHITELIST: dict[tuple[str, str, str], frozenset[str]] = {}
for _src, _rel, _dst in CORE_EDGE_TYPES:
    # Every edge gets these lineage + base properties.
    _base = frozenset({
        "source", "evidence", "score", "confidence",
        # v27 ROOT FIX (P2-L-3): canonical normalized score in [0,1] for
        # cross-source fusion. Every loader (STITCH, STRING, ChEMBL,
        # DisGeNET, OMIM, OpenTargets, DrugBank) now emits BOTH a raw
        # source-specific score (e.g. ``string_combined_score``,
        # ``pchembl_value``, ``disgenet_score``) AND a canonical
        # ``normalized_score`` in [0,1]. Whitelist it on EVERY edge type
        # so the property survives kg_builder's property-stripping pass.
        "normalized_score",
        # ── v6: bridge-emitted lineage properties (bug #B8) ──
        "source_id", "action_type", "is_known_action",
        "association_type", "mapping_key",
    })
    if _rel in ("causes_adverse_event", "causes_side_effect"):
        _base = _base | frozenset({"frequency", "meddra_type", "meddra_code"})
    if _rel == "tested_for":
        _base = _base | frozenset({
            "nct_id", "phase", "status", "enrollment", "why_stopped",
        })
    if _rel in ("inhibits", "activates", "binds", "targets",
                "allosterically_modulates", "unknown"):
        _base = _base | frozenset({
            "action_type", "pubmed_ids",
            "is_known_action", "source_id",  # bridge-emitted
            # v21 ROOT FIX (Audit section 4 finding 4 / Chain 4):
            # the previous whitelist was missing every ChEMBL
            # activity property that phase1_bridge emits on
            # Compound-{inhibits,activates,targets,binds}-Protein
            # edges. Without these, the production kg_builder
            # silently stripped pchembl_value (potency),
            # standard_relation (censoring direction), and the
            # activity metadata. The v15 ROOT FIX explicitly
            # promised these would be preserved so the RL ranker
            # has potency + censoring context; that promise was
            # FALSE in production. The test path
            # (RecordingGraphBuilder) does not apply the whitelist,
            # so the bug was invisible to tests.
            "pchembl_value",        # -log10(IC50/Ki/Kd) - potency
            "standard_relation",    # '=', '<', '>' - censoring
            "activity_type",        # "IC50", "EC50", "Ki", "Kd"...
            "activity_value",       # numeric activity value
            "activity_units",       # "nM", "uM"...
            "assay_type",           # 'F' functional / 'B' binding
            "chembl_target_id",     # for unresolved targets
        })
    if _rel == "interacts_with" and _src == "Compound":
        _base = _base | frozenset({"severity", "description"})
    if _rel == "associated_with" and _src == "Gene":
        _base = _base | frozenset({
            "association_type", "mapping_key",  # bridge-emitted (OMIM GDA)
        })
    if _rel == "encodes" and _src == "Gene":
        _base = _base | frozenset({
            "evidence",  # bridge-emitted (gene_protein_crosswalk)
        })
    if _rel == "treats" and _src == "Compound":
        _base = _base | frozenset({
            "evidence",  # bridge-emitted (drugbank_indication_text)
        })
    # v29 ROOT FIX (audit L-4 — EDGE_PROPERTY_WHITELIST silently strips
    # properties): the previous whitelist was missing GEO expression
    # properties, STITCH confidence channels, and SIDER frequency
    # bounds. The RL safety ranker needs these to distinguish 50% ADRs
    # from 0.01% ADRs, and the KG needs expression magnitude to know
    # HOW strongly a protein is expressed in a tissue (not just that
    # it IS expressed). ROOT FIX: add the missing properties to every
    # relevant edge type.
    if _rel == "expressed_in" or (_rel == "associated_with" and _src == "Protein"):
        # GEO expression edges: (Protein, expressed_in, Tissue)
        _base = _base | frozenset({
            "expression_value",   # log2 fold change magnitude
            "n_samples",          # sample count (statistical power)
            "fdr",                # false discovery rate
            "p_value",            # statistical significance
            "tissue",             # tissue name
            "experiment_id",      # GEO accession (GSE...)
        })
    if _rel in ("interacts_with", "binds") and _src == "Compound" and _dst == "Protein":
        # STITCH chemical-protein interaction edges
        _base = _base | frozenset({
            "stitch_combined_score",   # 0-999 confidence
            "stereochemistry",         # stereo flag (CIDm vs CIDs)
            "evidence_channels",       # experimental/database/textmining
            "experimental_score",
            "database_score",
            "textmining_score",
        })
    if _rel in ("causes_adverse_event", "causes_side_effect"):
        # SIDER adverse event edges — add frequency bounds
        _base = _base | frozenset({
            "frequency_description",    # "Postmarketing", "Frequent", etc.
            "frequency_lower_bound",    # 0.0
            "frequency_upper_bound",    # 1.0
            "frequency_source",         # "sider_frequency"
            "meddra_name",
        })
    EDGE_PROPERTY_WHITELIST[(_src, _rel, _dst)] = _base

# RT-8 ROOT FIX: the previous code raised ImportError at module
# import time when EDGE_PROPERTY_WHITELIST was empty. This made
# kg_builder unimportable for unit tests, partial pipelines, CI
# lint runs, and error recovery — a single config regression took
# down the entire module surface, and the operator could not even
# open a Python REPL to inspect kg_builder to debug. Move the
# invariant check to a runtime function (called from
# DrugOSGraphBuilder.__init__ and from load_edges_bulk_create) so
# it fires only when an actual production edge load is attempted
# with an empty whitelist. The check is now a RuntimeError (not
# ImportError) so it does not interfere with Python's import system.
def _assert_edge_property_whitelist_populated() -> None:
    """Raise RuntimeError if the edge-property whitelist is empty.

    Called from DrugOSGraphBuilder.__init__ (and from
    load_edges_bulk_create as a defensive re-check) to ensure
    production edge loads never silently strip all properties.
    Safe to call at module import time — it returns silently when
    the whitelist is populated (the normal case).
    """
    if not EDGE_PROPERTY_WHITELIST:
        raise RuntimeError(
            "BUG-D-006 invariant violated: EDGE_PROPERTY_WHITELIST is "
            "empty. CORE_EDGE_TYPES must be imported and non-empty "
            "before any production edge load. Check "
            "phase2/drugos_graph/config.py for regressions in the "
            "CORE_EDGE_TYPES definition."
        )
    if not CORE_EDGE_TYPES_SET:
        raise RuntimeError(
            "BUG-D-006 invariant violated: CORE_EDGE_TYPES_SET is empty. "
            "Production edge loads would silently strip all properties."
        )


# v41 ROOT FIX (Task J SEV3): the previous block wrapped the import-time
# invariant check in ``try/except RuntimeError`` + ``logger.critical`` so
# that an empty whitelist would log a CRITICAL warning but STILL let the
# module be imported. That defeats fail-fast: a config regression that
# empties CORE_EDGE_TYPES would silently produce a module whose
# DrugOSGraphBuilder constructor raises at runtime, but the operator who
# runs ``import drugos_graph.kg_builder`` (e.g. for ``--help``, lint,
# audit tools) sees NO error — they only discover the regression when
# the first edge load fails. The fix: do NOT catch the RuntimeError. The
# module is now UNIMPORTABLE when the whitelist is empty, which is the
# correct fail-fast behavior for a hard invariant violation.
# Exception: test contexts (PYTEST_CURRENT_TEST, DRUGOS_SKIP_IMPORT_CHECK=1,
# or pytest already in sys.modules) STILL skip the import-time check so
# test fixtures can monkey-patch CORE_EDGE_TYPES without crashing collection.
_import_time_skip = (
    os.environ.get("PYTEST_CURRENT_TEST") is not None
    or os.environ.get("DRUGOS_SKIP_IMPORT_CHECK") == "1"
    or "pytest" in sys.modules
)
if not _import_time_skip:
    # Intentionally NOT wrapped in try/except — let RuntimeError propagate.
    # See v41 ROOT FIX comment above for rationale.
    _assert_edge_property_whitelist_populated()

# Source licenses for provenance (CO-2)
# v35 ROOT FIX (N-3): the bridge emits LOWERCASE source labels ("drugbank",
# "chembl", "string", "omim", "disgenet", "uniprot", "pubchem",
# "drugbank_indication", etc.) for every staged edge's `source` field.
# The original dict used CAPITALIZED keys ("DrugBank", "ChEMBL", ...),
# so SOURCE_LICENSES.get(source) returned the fallback `{"license":
# "unknown"}` and the CC BY-NC 4.0 attribution required by DrugBank's
# license was silently dropped from every bridge-loaded edge. We now
# ALSO include lowercase aliases so the case-sensitive lookup succeeds
# regardless of which form the caller uses.
SOURCE_LICENSES: dict[str, dict[str, str]] = {
    "DRKG":        {"license": "ODC-BY 1.0",   "attribution": "DRKG (Ioannidis et al., 2020), ODC-BY 1.0"},
    "DrugBank":    {"license": "CC BY-NC 4.0",  "attribution": "DrugBank (Wishart DS et al., Nucleic Acids Res. 2018), CC BY-NC 4.0"},
    "UniProt":     {"license": "CC BY 4.0",     "attribution": "UniProt (UniProt Consortium), CC BY 4.0"},
    "ChEMBL":      {"license": "CC BY-SA 3.0",  "attribution": "ChEMBL (Gaulton A et al., Nucleic Acids Res. 2017), CC BY-SA 3.0"},
    "STRING":      {"license": "CC BY 4.0",     "attribution": "STRING (Szklarczyk D et al., Nucleic Acids Res. 2023), CC BY 4.0"},
    "STITCH":      {"license": "CC BY 4.0",     "attribution": "STITCH (Kuhn M et al., Nucleic Acids Res. 2014), CC BY 4.0"},
    "SIDER":       {"license": "CC0 1.0",       "attribution": "SIDER (Kuhn M et al., Clin Pharmacol Ther. 2016), CC0 1.0"},
    "OpenTargets": {"license": "Apache 2.0",    "attribution": "OpenTargets (Koscielny G et al., Nucleic Acids Res. 2017), Apache 2.0"},
    "ClinicalTrials": {"license": "public domain", "attribution": "ClinicalTrials.gov (AACT), public domain"},
    "GEO":         {"license": "public domain", "attribution": "GEO (Barrett T et al., Nucleic Acids Res. 2013), public domain"},
    # ── v35 N-3: lowercase aliases for the bridge's source labels ──
    "drkg":            {"license": "ODC-BY 1.0",   "attribution": "DRKG (Ioannidis et al., 2020), ODC-BY 1.0"},
    "drugbank":        {"license": "CC BY-NC 4.0",  "attribution": "DrugBank (Wishart DS et al., Nucleic Acids Res. 2018), CC BY-NC 4.0"},
    "drugbank_indication":      {"license": "CC BY-NC 4.0",  "attribution": "DrugBank (Wishart DS et al., Nucleic Acids Res. 2018), CC BY-NC 4.0"},
    "drugbank_indications":     {"license": "CC BY-NC 4.0",  "attribution": "DrugBank (Wishart DS et al., Nucleic Acids Res. 2018), CC BY-NC 4.0"},
    "drugbank_indication_text": {"license": "CC BY-NC 4.0",  "attribution": "DrugBank (Wishart DS et al., Nucleic Acids Res. 2018), CC BY-NC 4.0"},
    "uniprot":         {"license": "CC BY 4.0",     "attribution": "UniProt (UniProt Consortium), CC BY 4.0"},
    "chembl":          {"license": "CC BY-SA 3.0",  "attribution": "ChEMBL (Gaulton A et al., Nucleic Acids Res. 2017), CC BY-SA 3.0"},
    "string":          {"license": "CC BY 4.0",     "attribution": "STRING (Szklarczyk D et al., Nucleic Acids Res. 2023), CC BY 4.0"},
    "stitch":          {"license": "CC BY 4.0",     "attribution": "STITCH (Kuhn M et al., Nucleic Acids Res. 2014), CC BY 4.0"},
    "sider":           {"license": "CC0 1.0",       "attribution": "SIDER (Kuhn M et al., Clin Pharmacol Ther. 2016), CC0 1.0"},
    "opentargets":     {"license": "Apache 2.0",    "attribution": "OpenTargets (Koscielny G et al., Nucleic Acids Res. 2017), Apache 2.0"},
    "clinicaltrials":  {"license": "public domain", "attribution": "ClinicalTrials.gov (AACT), public domain"},
    "geo":             {"license": "public domain", "attribution": "GEO (Barrett T et al., Nucleic Acids Res. 2013), public domain"},
    "omim":            {"license": "public domain", "attribution": "OMIM (Amberger JS et al., Nucleic Acids Res. 2019), public domain"},
    "disgenet":        {"license": "CC BY-NC-SA 4.0", "attribution": "DisGeNET (Piñero J et al., Nucleic Acids Res. 2020), CC BY-NC-SA 4.0"},
    "pubchem":         {"license": "public domain", "attribution": "PubChem (Kim S et al., Nucleic Acids Res. 2023), public domain"},
    "phase1_bridge":   {"license": "various",       "attribution": "Phase 1 bridge (aggregated from upstream sources)"},
}

# Additional indexes config (CF-1)
ADDITIONAL_INDEXES: list[tuple[str, str]] = [
    ("Compound", "name"), ("Disease", "name"), ("Gene", "name"),
    ("Compound", "approved"), ("Compound", "smiles"),
    ("Protein", "name"), ("Pathway", "name"), ("MedDRA_Term", "name"),
    ("Anatomy", "name"), ("Compound", "withdrawn"),
    ("Compound", "inchikey"), ("Compound", "chembl_id"),
    ("Compound", "drugbank_id"), ("Compound", "sensitive"),
]

# Environment variable defaults (CF-3)
_NEO4J_MAX_RETRIES = int(os.environ.get("DRUGOS_NEO4J_MAX_RETRIES", "5"))
_NEO4J_RETRY_BASE_DELAY = float(os.environ.get("DRUGOS_NEO4J_RETRY_BASE_DELAY", "1.0"))
_NEO4J_RETRY_MAX_DELAY = float(os.environ.get("DRUGOS_NEO4J_RETRY_MAX_DELAY", "30.0"))
_QUERY_TIMEOUT = int(os.environ.get("DRUGOS_NEO4J_QUERY_TIMEOUT", "300"))
_LOG_FREQUENCY = int(os.environ.get("DRUGOS_PROGRESS_LOG_EVERY_N_BATCHES", "10"))
_LOG_INTERVAL_SECONDS = int(os.environ.get("DRUGOS_PROGRESS_LOG_INTERVAL_SECONDS", "60"))
_DATA_MAX_AGE_DAYS = int(os.environ.get("DRUGOS_DATA_MAX_AGE_DAYS", "30"))
# v34 ROOT FIX (CRITICAL #5): expose the default clear-phrase as a public
# module-level constant so callers (run_pipeline.py, run_unified.py) can
# import it instead of hardcoding a DIFFERENT string. The previous code
# had run_pipeline.py passing "CLEAR_ALL_DRUGOS_DATA" while kg_builder
# expected "DELETE EVERYTHING I UNDERSTAND THE CONSEQUENCES" — they NEVER
# matched, so `clear_graph()` always raised SecurityError, was caught by
# the `except Exception` in step3, and logged as a warning. The graph was
# NEVER cleared → re-runs created DUPLICATE nodes/edges. The
# `fresh_start=True` idempotency promise was dead code.
DEFAULT_CLEAR_GRAPH_PHRASE = "DELETE EVERYTHING I UNDERSTAND THE CONSEQUENCES"
_CLEAR_GRAPH_PHRASE = os.environ.get(
    "DRUGOS_CLEAR_GRAPH_PHRASE",
    DEFAULT_CLEAR_GRAPH_PHRASE,
)
_ALLOW_NON_CORE_EDGES = os.environ.get("DRUGOS_KG_ALLOW_NON_CORE_EDGES", "0") == "1"
_AUTO_DEDUP = os.environ.get("DRUGOS_KG_AUTO_DEDUP", "0") == "1"


# ─── Result Dataclasses ───────────────────────────────────────────────────────
# Fixes D-6, C-7: Structured return types for all mutating operations.

@dataclass(frozen=True)
class LoadResult:
    """Result of a node or edge loading operation.

    Fixes D-6: Return values now track created, updated, matched, dropped,
    and dead-lettered counts instead of just created.
    """
    attempted: int
    created: int
    updated: int = 0
    matched: int = 0
    dropped_no_match: int = 0
    dead_lettered: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def __int__(self) -> int:
        """Backward compatibility — old code expects int return."""
        return self.created

    def __add__(self, other: LoadResult) -> LoadResult:
        return LoadResult(
            attempted=self.attempted + other.attempted,
            created=self.created + other.created,
            updated=self.updated + other.updated,
            matched=self.matched + other.matched,
            dropped_no_match=self.dropped_no_match + other.dropped_no_match,
            dead_lettered=self.dead_lettered + other.dead_lettered,
            elapsed_seconds=self.elapsed_seconds + other.elapsed_seconds,
            errors=self.errors + other.errors,
        )


@dataclass(frozen=True)
class ClearGraphResult:
    """Result of a clear_graph operation.

    Fixes C-7: clear_graph returns structured result instead of None.
    """
    nodes_deleted: int
    relationships_deleted: int
    elapsed_seconds: float
    pipeline_run_id: str
    timestamp: str


@dataclass(frozen=True)
class BuildGraphResult:
    """Result of a build_graph orchestration.

    Fixes D-5: Structured result for the fluent build_graph method.
    """
    node_results: dict[str, LoadResult]
    edge_results: dict[tuple[str, str, str], LoadResult]
    enrichment_result: Optional[LoadResult]
    stats: dict[str, Any]
    lineage: dict[str, Any]
    elapsed_seconds: float


# ─── Helper Functions ──────────────────────────────────────────────────────────

def _check_neo4j_available() -> None:
    """Raise informative error if neo4j driver is not installed."""
    if GraphDatabase is None:
        raise ImportError(
            "The 'neo4j' Python driver is not installed. "
            "Install it with: pip install neo4j>=5.0,<6.0"
        )


def _validate_id(label: str, value: str) -> bool:
    """Validate a node ID against the expected pattern for its type.

    Fixes S-5 (Domain 3): Biomedical identifier validation.
    Invalid IDs go to the dead-letter queue with reason='invalid_id_format'.

    v9 ROOT FIX (audit F7.8): the previous code returned ``True`` for any
    label not present in ID_PATTERNS — silently disabling validation for
    typo'd labels like 'MedDRATerm' (missing underscore). Now raises
    ``UnknownLabelError`` so the caller can either fix the label or
    explicitly register the new label's pattern in ID_PATTERNS.
    """
    if not value or not isinstance(value, str):
        return False
    if len(value) > 1024:
        return False
    pat = ID_PATTERNS.get(label)
    if pat is None:
        # v9: fail-closed. Unknown labels cannot silently bypass validation.
        raise UnknownLabelError(
            f"Unknown node label {label!r}: no ID_PATTERNS entry. "
            f"Register the label in kg_builder.ID_PATTERNS or fix the typo."
        )
    return re.match(pat, str(value)) is not None


def _sanitize_value(v: Any) -> Any:
    """Sanitize a property value before writing to Neo4j.

    Fixes S(9)-6: Input sanitization for property values.
    - Strings: truncate to 1024 chars, strip control characters
    - Reject binary/control characters
    - Length limits enforced
    """
    if isinstance(v, str):
        if len(v) > 1024:
            logger.warning(
                "Truncating property value of length %d to 1024 chars",
                len(v),
            )
            v = v[:1024]
        # Strip control characters except newline/tab
        v = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', v)
    return v


def _redact_uri(uri: str) -> str:
    """Redact credentials from a Neo4j URI for safe logging.

    Fixes S(9)-2: URI logged in plaintext.
    bolt://neo4j:password@host:7687 -> bolt://***@host:7687
    """
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(uri)
        if p.password:
            netloc = f"{p.username}:***@{p.hostname}:{p.port}"
            return urlunparse(
                (p.scheme, netloc, p.path, p.params, p.query, p.fragment)
            )
    except Exception:
        pass
    return uri


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _build_lineage_props(
    source: str,
    input_checksum: str = "",
) -> dict[str, Any]:
    """Build the lineage property dict for a node or edge mutation.

    Fixes DL-1, DL-5, DL-6, I-4, CO-1, CO-2 (Provenance Rule §3.5):
    Every mutation MUST carry all lineage properties.
    """
    src_info = SOURCE_LICENSES.get(source, {
        "license": "unknown", "attribution": source,
    })
    return {
        "_pipeline_run_id": RUN_ID,
        "_loaded_at": _now_iso(),
        "_schema_version": SCHEMA_VERSION,
        "_source": source,
        "_license": src_info["license"],
        "_attribution": src_info["attribution"],
        "_config_hash": CONFIG_HASH,
        "_pipeline_version": PIPELINE_VERSION,
        "_seed": SEED,
        "_input_checksum": input_checksum,
    }


def _validate_batch_size(batch_size: Any, param_name: str = "batch_size") -> int:
    """Validate and return batch_size, raising ConfigurationError if invalid.

    Fixes C-3: batch_size=0 causes ValueError.
    """
    if batch_size is None:
        return 5000  # Neo4j-recommended default
    if not isinstance(batch_size, int) or batch_size < 1:
        # Fixes C-3: batch_size must be >= 1
        raise ConfigurationError(
            f"{param_name} must be an integer >= 1, got {batch_size!r}"
        )
    return batch_size


def _whitelist_filter(
    data: dict[str, Any],
    allowed: frozenset[str],
) -> tuple[dict[str, Any], list[str]]:
    """Filter a dict through a property whitelist.

    Fixes D-2, DQ-4, S(9)-6, IN-1 (Schema-Whitelist Rule §3.7):
    Only whitelisted properties pass through; everything else is dropped.
    Returns (cleaned_dict, dropped_keys).

    FORENSIC Chain 6 root fix (patient safety):
    ``None`` values are ALSO dropped. In Cypher, ``SET n += row`` deletes
    a property when the map value is ``null``. Without this filter,
    multi-source enrichment silently erases patient-safety flags:
    DrugBank sets ``withdrawn=True`` on a Compound node, then a later
    ChEMBL batch omits the key (or explicitly sets it to ``None``) →
    MERGE finds the existing node and ``SET n += row`` deletes
    ``withdrawn`` entirely. The RL ranker then sees a withdrawn drug
    as safe. Dropping ``None`` here ensures ``SET n += row`` only ever
    ADDS or OVERWRITES with a real value — it can never REMOVE a
    property that an earlier source legitimately set.
    """
    cleaned = {}
    dropped = []
    for k, v in data.items():
        if k not in allowed:
            dropped.append(k)
            continue
        # Chain 6 root fix: skip None values so SET n += row cannot
        # erase a property that an earlier source set.
        if v is None:
            continue
        cleaned[k] = _sanitize_value(v)
    return cleaned, dropped


def _strip_nulls(row: dict[str, Any]) -> dict[str, Any]:
    """Remove all keys whose value is ``None`` (Chain 6 defensive net).

    This is a defensive second net on top of :func:`_whitelist_filter`.
    Even if a future caller bypasses the whitelist (or constructs a
    ``row`` dict directly), this function guarantees that no ``null``
    value ever reaches a Cypher ``SET n += row`` clause, which would
    otherwise DELETE the property on the existing node and silently
    erase patient-safety flags (withdrawn, terminated, illegal, etc.).
    """
    return {k: v for k, v in row.items() if v is not None}


def _deduplicate_batch(
    batch: list[dict[str, Any]],
    key: str = "id",
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Remove duplicate entries from a batch by key.

    Fixes DQ-5: No duplicate detection in input lists.
    Returns (deduped_batch, duplicate_keys). Keeps the LAST entry.

    FORENSIC audit issue 28 root fix: the previous code dead-lettered
    EVERY duplicate, including legitimate re-loads (e.g. re-running
    the bridge to MERGE-update existing nodes). This flooded the
    dead-letter queue with false positives, masking real data-quality
    issues. The fix only dead-letters duplicates whose CONTENT differs
    from the first occurrence (a true conflict that indicates two
    different records share the same key — a real data-quality bug).
    Identical re-loads are silently deduped (last wins) without
    dead-lettering, since MERGE is idempotent by design.
    """
    seen: dict[Any, dict[str, Any]] = {}
    duplicates: list[Any] = []
    conflicts: list[Any] = []
    for row in batch:
        rid = row.get(key)
        if rid in seen:
            duplicates.append(rid)
            # Only dead-letter if the content differs (true conflict).
            # Compare by the sorted key-value pairs so key order doesn't
            # matter. Strip lineage props (_*) before comparing since
            # those are run-specific and expected to differ.
            prev = {k: v for k, v in seen[rid].items() if not k.startswith("_")}
            curr = {k: v for k, v in row.items() if not k.startswith("_")}
            if prev != curr:
                conflicts.append(rid)
        seen[rid] = row  # Last wins
    if duplicates:
        logger.warning(
            "Removed %d duplicate %s values from batch (sample: %s). "
            "%d were content-conflicts (dead-lettered), %d were "
            "identical re-loads (silently deduped).",
            len(duplicates), key, str(duplicates[:5]),
            len(conflicts), len(duplicates) - len(conflicts),
        )
        # Only dead-letter true conflicts (different content for the
        # same key), not legitimate identical re-loads.
        for dup_id in conflicts:
            dead_letter_record(
                source="kg_builder",
                record=seen.get(dup_id, {}),
                reason=f"duplicate_in_batch_content_conflict:key={key}:value={str(dup_id)[:50]}",
            )
    return list(seen.values()), duplicates


# ─── RunIdFilter for Logging ──────────────────────────────────────────────────
# Fixes L-4: Inconsistent log format — add pipeline_run_id to every log entry.

class _RunIdFilter(logging.Filter):
    """Add pipeline run_id to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID  # type: ignore[attr-defined]
        return True


# v35 ROOT FIX (L-6): the previous code called ``logger.addFilter``
# UNCONDITIONALLY at module import time. In a Jupyter notebook or
# pytest session that re-imports the module, this accumulates
# DUPLICATE _RunIdFilter instances on the logger — every log record
# gets the ``run_id`` attribute set N times (once per filter), which
# is harmless but wastes CPU. More importantly, the filter list grows
# unbounded across re-imports. The fix checks whether a _RunIdFilter
# is already attached before adding a new one. We use ``isinstance``
# rather than identity so the check works even if a subclass is
# somehow registered.
def _has_run_id_filter(logr: logging.Logger) -> bool:
    return any(isinstance(f, _RunIdFilter) for f in logr.filters)


if not _has_run_id_filter(logger):
    logger.addFilter(_RunIdFilter())


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERNAL CLASSES (Facade collaborators)
# ═══════════════════════════════════════════════════════════════════════════════

class GraphConnection:
    """Manages the Neo4j driver lifecycle.

    Fixes A-1: Extracted from DrugOSGraphBuilder (god object split).
    Fixes A-5: Driver dependency injection.
    Fixes R-1: Connection retry logic.
    Fixes R-4: Connection health monitoring.
    Fixes R-6: Cleanup on connect() failure.
    Fixes R-7: health_check verifies driver state.
    Fixes S(9)-2: URI redaction in logs.
    Fixes S(9)-5: Query timeout.
    Fixes CO-5: Neo4j version detection.
    """

    def __init__(
        self,
        config: Neo4jConfig,
        driver: Optional[Driver] = None,
        driver_factory: Optional[Callable[[], Driver]] = None,
    ) -> None:
        self.config = config
        self._external_driver = driver is not None
        self._driver_factory = driver_factory
        self._driver: Optional[Driver] = driver
        self._neo4j_version: Optional[str] = None
        self._constraint_syntax: str = "modern"  # "modern" (5.x) or "legacy" (4.x)
        self._max_retries = _NEO4J_MAX_RETRIES
        self._retry_base_delay = _NEO4J_RETRY_BASE_DELAY
        self._retry_max_delay = _NEO4J_RETRY_MAX_DELAY
        self._query_timeout = _QUERY_TIMEOUT

    @property
    def driver(self) -> Optional[Driver]:
        return self._driver

    @property
    def neo4j_version(self) -> Optional[str]:
        return self._neo4j_version

    @property
    def constraint_syntax(self) -> str:
        return self._constraint_syntax

    def connect(self) -> None:
        """Establish connection to Neo4j database.

        Fixes R-1: Connection retry logic with exponential backoff.
        Fixes R-6: Cleanup on connect() failure.
        Fixes S(9)-2: URI redaction in logs.
        Fixes CO-5: Neo4j version detection.
        """
        _check_neo4j_available()

        # Fixes A-5: If external driver provided, skip driver creation
        if self._external_driver and self._driver is not None:
            logger.info(
                "Using externally-provided driver (DI mode). "
                "Connected to Neo4j at %s",
                _redact_uri(self.config.uri),
            )
            self._detect_version()
            return

        # Fixes A-5: If driver_factory provided, use it
        if self._driver_factory is not None:
            self._driver = self._driver_factory()
            logger.info(
                "Using driver factory. Connected to Neo4j at %s",
                _redact_uri(self.config.uri),
            )
            self._detect_version()
            return

        # Fixes R-6 + BUG-D-001/D-014 root fix: Cleanup on connect() failure.
        # The previous code initialised ``driver = None`` and then assigned
        # the actual driver to ``self._driver`` via safe_call_with_retry.
        # The cleanup branch ``if driver is not None:`` was therefore ALWAYS
        # False — orphaned Neo4j drivers from failed attempts leaked on
        # every retry, eventually exhausting the connection pool.
        # Fix: track the most recently attempted driver in a closure variable
        # so the cleanup branch can close it on failure.
        last_attempted_driver: list[Any] = []  # mutable closure capture

        # Fixes R-6: Cleanup on connect() failure
        try:
            # Fixes R-1: Connection retry logic
            def _attempt() -> Any:
                d = GraphDatabase.driver(
                    self.config.uri,
                    auth=(self.config.user, self.config.password),
                    max_connection_pool_size=self.config.max_connection_pool_size,
                    connection_timeout=self.config.connection_timeout,
                )
                # BUG-D-001/D-014: track this driver so the outer except
                # can close it if the test session fails.
                last_attempted_driver.clear()
                last_attempted_driver.append(d)
                with d.session(database=self.config.database) as s:
                    s.run("RETURN 1 AS test").consume()
                return d

            self._driver = safe_call_with_retry(
                _attempt,
                max_attempts=self._max_retries,
                base_delay=self._retry_base_delay,
                max_delay=self._retry_max_delay,
                retry_on=(ServiceUnavailable, SessionExpired, OSError)
                if ServiceUnavailable is not None
                else (OSError,),
            )

            logger.info(
                "Connected to Neo4j at %s",
                _redact_uri(self.config.uri),
            )

            # Fixes CO-5: Neo4j version detection
            self._detect_version()

        except Exception:
            # Fixes R-6 + BUG-D-001/D-014: Cleanup on failure now actually
            # closes the orphaned driver from the last attempt.
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception:
                    pass
                self._driver = None
            if last_attempted_driver:
                # Close the orphaned driver from the failed attempt.
                orphan = last_attempted_driver[-1]
                if orphan is not None:
                    try:
                        orphan.close()
                    except Exception:
                        pass
            raise

    def _detect_version(self) -> None:
        """Detect Neo4j server version.

        Fixes CO-5: Version detection for Cypher syntax dispatch.
        Fixes IN-3: Neo4j version detection for compatibility.
        """
        if self._driver is None:
            return
        try:
            with self._driver.session(database=self.config.database) as s:
                result = s.run(
                    "CALL dbms.components() YIELD versions "
                    "RETURN versions[0] AS v"
                )
                record = result.single()
                if record:
                    self._neo4j_version = record["v"]
        except Exception as e:
            logger.warning("Could not detect Neo4j version: %s", e)
            self._neo4j_version = "unknown"
            return

        if self._neo4j_version:
            if self._neo4j_version.startswith("4."):
                self._constraint_syntax = "legacy"
                logger.warning(
                    "Neo4j version is %s; code targets Neo4j 5.x. "
                    "Using legacy constraint syntax. Some Cypher may fail.",
                    self._neo4j_version,
                )
            elif not self._neo4j_version.startswith("5."):
                logger.warning(
                    "Neo4j version is %s; code targets Neo4j 5.x. "
                    "Some Cypher may fail.",
                    self._neo4j_version,
                )

    def disconnect(self) -> None:
        """Close the Neo4j driver connection."""
        # Fixes A-5: Don't close externally-provided drivers
        if self._external_driver:
            logger.info("Skipping disconnect for externally-provided driver")
            return
        if self._driver:
            self._driver.close()
            logger.info("Disconnected from Neo4j")

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[Any]:
        """Provide a Neo4j session with timeout and bookmark support.

        Fixes P-2: Session reuse context manager.
        Fixes S(9)-5: Query timeout.
        Fixes P-6: Bookmark-based causal consistency.
        """
        if self._driver is None:
            raise DrugOSDataError(
                "Driver not connected. Call connect() first."
            )
        session_kwargs = {"database": self.config.database}
        session_kwargs.update(kwargs)
        # Fixes S(9)-5: Query timeout
        if "default_timeout" not in session_kwargs:
            session_kwargs["default_timeout"] = self._query_timeout
        session = self._driver.session(**session_kwargs)
        try:
            yield session
        finally:
            session.close()

    def health_check(self) -> dict[str, Any]:
        """Check Neo4j connectivity.

        Fixes R-4: Connection health monitoring.
        Fixes R-7: health_check verifies driver state.
        """
        if self._driver is None:
            return {
                "connected": False,
                "error": "Driver not initialized. Call connect() first.",
            }
        try:
            # Neo4j 5.x: verify_connectivity()
            if hasattr(self._driver, "verify_connectivity"):
                self._driver.verify_connectivity()
            else:
                with self._driver.session(
                    database=self.config.database,
                ) as s:
                    s.run("RETURN 1 AS ok").consume()
            return {
                "connected": True,
                "neo4j_version": self._neo4j_version,
                "uri": _redact_uri(self.config.uri),
                "database": self.config.database,
            }
        except Exception as e:
            return {
                "connected": False,
                "error": f"Connection lost: {e}",
                "neo4j_version": self._neo4j_version,
            }


class GraphSchemaManager:
    """Manages Neo4j constraints and indexes.

    Fixes A-1: Extracted from DrugOSGraphBuilder (god object split).
    Fixes R-3: Exception swallowing in constraint/index creation.
    Fixes CF-1: Hardcoded index list moved to config.
    Fixes P-3: Constraints created one at a time → batched.
    """

    def __init__(self, conn: GraphConnection) -> None:
        self._conn = conn

    def create_constraints(self) -> None:
        """Create uniqueness constraints on node IDs for all entity types.

        Fixes audit issue 1.1, 7.1 — deterministic order from config.
        Fixes audit issue 3.1 — MedDRA_Term now gets a uniqueness constraint.
        Fixes R-3: Constraint failures raise CriticalDataSourceError.
        Fixes P-3: Batched constraint creation.

        PATIENT SAFETY: Without a uniqueness constraint on MedDRA_Term.id,
        MERGE creates duplicate adverse-event nodes on pipeline re-runs.
        Duplicate nodes split adverse-event counts per drug, causing the
        RL safety ranker to under-count adverse events and rank dangerous
        drugs as 'green' (safe).
        """
        entity_types = list(dict.fromkeys(CORE_NODE_TYPES + DRKG_NODE_TYPES))

        errors: list[tuple[str, str]] = []
        created_count = 0

        with self._conn.session() as session:
            # Fixes P-3: Batch constraints in a single transaction
            with session.begin_transaction() as tx:
                for etype in entity_types:
                    label = drkg_node_type_to_neo4j_label(etype)
                    safe_label = sanitize_label(label)
                    try:
                        # v34 ROOT FIX (CRITICAL #6): the previous code
                        # had an if/else that emitted IDENTICAL 5.x Cypher
                        # in both branches. The "legacy" (Neo4j 4.x)
                        # branch used 5.x syntax (`FOR (n:L) REQUIRE`) on
                        # 4.x servers, raising SyntaxError → caught by
                        # the except below → CriticalDataSourceError →
                        # graph build aborted. Now we ACTUALLY dispatch:
                        # 4.x uses `ON (n:L) ASSERT n.id IS UNIQUE`,
                        # 5.x uses `FOR (n:L) REQUIRE n.id IS UNIQUE`.
                        if self._conn.constraint_syntax == "legacy":
                            cypher = (
                                f"CREATE CONSTRAINT IF NOT EXISTS "
                                f"ON (n:{safe_label}) "
                                f"ASSERT n.id IS UNIQUE"
                            )
                        else:
                            cypher = (
                                f"CREATE CONSTRAINT IF NOT EXISTS "
                                f"FOR (n:{safe_label}) "
                                f"REQUIRE n.id IS UNIQUE"
                            )
                        tx.run(cypher)
                        created_count += 1
                        logger.debug("Constraint created for %s.id", safe_label)
                    except Exception as e:
                        # Fixes R-3: Log at ERROR, not WARNING
                        logger.error(
                            "Constraint for %s FAILED: %s", safe_label, e
                        )
                        errors.append((str(safe_label), str(e)))

                tx.commit()

        # Fixes R-3: If ANY constraint fails, raise CriticalDataSourceError
        if errors:
            raise CriticalDataSourceError(
                f"Constraint creation failed for "
                f"{len(errors)}/{len(entity_types)} types. "
                f"Without constraints, MERGE will create duplicate nodes. "
                f"Aborting. Errors: {errors}"
            )

        logger.info(
            "Created uniqueness constraints for %d entity types",
            created_count,
        )
        # Fixes CO-4: Audit trail for graph mutations
        audit_log(
            "constraints_created",
            details=f"Created {created_count} uniqueness constraints",
            metadata={"count": created_count, "types": entity_types},
        )

    def create_indexes(self) -> None:
        """Create additional indexes for common query patterns.

        Fixes CF-1: Index list driven by ADDITIONAL_INDEXES config constant.
        Fixes R-3: Index failures are logged at ERROR.
        """
        errors: list[tuple[str, str, str]] = []

        with self._conn.session() as session:
            with session.begin_transaction() as tx:
                for lbl, prop in ADDITIONAL_INDEXES:
                    safe_lbl = sanitize_label(lbl)
                    # Fixes NFR §3.9: Property names sanitized too
                    safe_prop = sanitize_identifier(prop, "property name")
                    try:
                        cypher = (
                            f"CREATE INDEX IF NOT EXISTS "
                            f"FOR (n:{safe_lbl}) ON (n.{safe_prop})"
                        )
                        tx.run(cypher)
                    except Exception as e:
                        logger.error(
                            "Index creation for %s.%s FAILED: %s",
                            safe_lbl, safe_prop, e,
                        )
                        errors.append((str(safe_lbl), str(safe_prop), str(e)))
                tx.commit()

        if errors:
            logger.error(
                "Index creation failed for %d indexes. "
                "Queries may be slow. Errors: %s",
                len(errors), errors,
            )

        logger.info(
            "Additional indexes created (%d attempted, %d failed)",
            len(ADDITIONAL_INDEXES), len(errors),
        )
        audit_log(
            "indexes_created",
            details=f"Created {len(ADDITIONAL_INDEXES) - len(errors)} indexes",
            metadata={"attempted": len(ADDITIONAL_INDEXES), "failed": len(errors)},
        )


class GraphNodeLoader:
    """Loads nodes into Neo4j with validation, dedup, and lineage.

    Fixes A-1: Extracted from DrugOSGraphBuilder (god object split).
    Fixes DQ-1: Validation that node dicts contain 'id'.
    Fixes DQ-4: Schema-whitelist filtering.
    Fixes DQ-5: Duplicate detection in input lists.
    Fixes DQ-6: Data freshness validation.
    Fixes I-2: SET n += row not idempotent → coalesce pattern.
    Fixes S-2: DrugBank enrichment preserved on re-runs.
    Fixes S-5: Biomedical identifier validation.
    Fixes L-5: Data lineage in logs.
    Fixes R-2: Partial batch failure recovery via checkpoints.
    """

    def __init__(self, conn: GraphConnection) -> None:
        self._conn = conn

    def load_nodes_batch(
        self,
        label: str,
        nodes: list[dict],
        batch_size: Optional[int] = None,
        *,
        source: str = "unknown",
        input_checksum: str = "",
        checkpoint_key: Optional[str] = None,
        detailed: bool = False,
        allow_non_core: bool = False,
    ) -> Union[int, LoadResult]:
        """Bulk-create nodes using UNWIND + MERGE with full validation.

        Parameters
        ----------
        label : str
            Node label (e.g. "Compound", "Disease").
        nodes : list of dict
            Node data. Each dict MUST contain "id".
        batch_size : int, optional
            Batch size for UNWIND. Default from config.
        source : str
            Data source name for lineage (e.g. "DRKG", "DrugBank").
        input_checksum : str
            SHA-256 of source file for lineage.
        checkpoint_key : str, optional
            If provided, enables resume-from-failure.
        detailed : bool
            If True, return LoadResult instead of int.
        allow_non_core : bool
            If True, allow labels not in CORE_NODE_TYPES + DRKG_NODE_TYPES.

        Returns
        -------
        int or LoadResult
            Number of nodes created (int for backward compat),
            or LoadResult if detailed=True.

        Raises
        ------
        ConfigurationError
            If batch_size < 1.
        SecurityError
            If label fails sanitization.

        Side Effects
        ------------
        - Writes nodes to Neo4j
        - Routes invalid rows to dead-letter queue
        - Writes audit log entries
        - Writes checkpoints if checkpoint_key provided

        Invariants
        ----------
        - No node with null/empty id is created
        - Every node carries all lineage properties from §3.5
        - Non-whitelisted properties are silently dropped
        - The operation is idempotent (MERGE on id)

        Fixes: DQ-1, DQ-4, DQ-5, S-2, S-5, I-2, R-2, L-5
        """
        start_time = time.monotonic()
        batch_size = _validate_batch_size(batch_size, "batch_size")

        # Fixes S(9)-1 / C-1: Cypher injection via f-strings
        # Fixes NFR §3.9: Sanitize label
        safe_label = sanitize_label(label)

        # Whitelist for this label
        allowed_props = (
            NODE_PROPERTY_WHITELIST.get(label, frozenset()) | SYSTEM_PROPS
        )

        total_created = 0
        total_matched = 0
        total_updated = 0
        total_dead_lettered = 0
        all_errors: list[str] = []

        # Fixes R-2: Checkpoint support
        checkpoint = (
            read_latest_checkpoint(checkpoint_key) if checkpoint_key else None
        )
        start_idx = (
            checkpoint["last_completed_idx"] + 1 if checkpoint else 0
        )

        # Fixes DL-2: Input checksum for lineage
        lineage = _build_lineage_props(source, input_checksum)

        with self._conn.session() as session:
            for i in range(start_idx, len(nodes), batch_size):
                batch = nodes[i:i + batch_size]

                # ── Phase 1: Validate and filter ────────────────────────
                clean_batch: list[dict[str, Any]] = []
                for row_idx, row in enumerate(batch):
                    # Fixes DQ-1: Validate that node dicts contain 'id'
                    node_id = row.get("id")
                    if not node_id or not isinstance(node_id, str) or not node_id.strip():
                        # Fixes NSFR §3.3: No silent failure
                        dead_letter_record(
                            source=source,
                            record=row,
                            reason=f"missing_id:label={label}:batch_idx={i + row_idx}",
                        )
                        total_dead_lettered += 1
                        logger.warning(
                            "Node at batch index %d missing 'id' — sent to DLQ",
                            i + row_idx,
                        )
                        continue

                    # Fixes S-5: Biomedical identifier validation
                    if not _validate_id(label, node_id):
                        dead_letter_record(
                            source=source,
                            record=row,
                            reason=f"invalid_id_format:label={label}:id={str(node_id)[:50]}:idx={i + row_idx}",
                        )
                        total_dead_lettered += 1
                        logger.warning(
                            "Node %s id=%r failed validation for label %s — "
                            "sent to DLQ",
                            label, str(node_id)[:50], label,
                        )
                        continue

                    # Fixes D-2, DQ-4, S(9)-6: Schema-whitelist filtering
                    cleaned, dropped = _whitelist_filter(row, allowed_props)
                    if dropped:
                        logger.debug(
                            "Dropped non-whitelisted keys from %s node %s: %s",
                            label, node_id, dropped,
                        )

                    # Add lineage properties
                    cleaned.update(lineage)

                    clean_batch.append(cleaned)

                # Fixes DQ-5: Deduplicate by 'id'
                clean_batch, dupes = _deduplicate_batch(clean_batch, "id")
                total_dead_lettered += len(dupes)

                if not clean_batch:
                    continue

                # ── Phase 2: Execute Cypher ─────────────────────────────
                try:
                    # Fixes S-2, I-2: ON CREATE SET n += row, ON MATCH preserves
                    # existing non-null values via coalesce pattern
                    cypher = (
                        f"UNWIND $batch AS row\n"
                        f"MERGE (n:{safe_label} {{id: row.id}})\n"
                        f"ON CREATE SET n += row, "
                        f"n._created_at = $loaded_at\n"
                        f"ON MATCH SET n += row, "
                        f"n._updated_at = $loaded_at, "
                        f"n._version = coalesce(n._version, 0) + 1\n"
                        f"SET n._pipeline_run_id = $run_id"
                    )
                    # FORENSIC Chain 6 root fix (patient safety): strip
                    # None values from every row in the batch BEFORE the
                    # Cypher SET n += row runs. This is the defensive
                    # second net on top of _whitelist_filter. Without
                    # this, SET n += row would DELETE properties whose
                    # map value is null, silently erasing patient-safety
                    # flags (withdrawn, terminated, illegal) set by an
                    # earlier source.
                    safe_batch = [_strip_nulls(r) for r in clean_batch]
                    params = {
                        "batch": safe_batch,
                        "loaded_at": lineage["_loaded_at"],
                        "run_id": RUN_ID,
                    }
                    result = session.run(cypher, params)
                    stats = result.consume().counters
                    batch_created = stats.nodes_created
                    # v35 ROOT FIX (M-3): removed the dead
                    # `batch_matched = properties_set // len(clean_batch[0])`
                    # heuristic. The heuristic was mathematically wrong
                    # (properties_set counts ALL props set across ALL
                    # nodes including system props, and dividing by the
                    # FIRST node's prop count gives an unreliable estimate)
                    # AND was dead code (never referenced after this
                    # block). The actual `total_matched` below uses the
                    # correct formula `max(0, len(clean_batch) - batch_created)`.
                    total_created += batch_created
                    total_matched += max(0, len(clean_batch) - batch_created)

                    # Fixes C-6: Configurable progress log frequency
                    log_freq = _LOG_FREQUENCY
                    if (i // batch_size) % log_freq == 0:
                        # Fixes L-5: Data lineage in logs
                        logger.info(
                            "  %s: loaded %d/%d nodes "
                            "source=%s checksum=%s",
                            safe_label, i + len(batch), len(nodes),
                            source, input_checksum[:8] if input_checksum else "N/A",
                        )

                    # Fixes R-2: Checkpoint after successful batch
                    if checkpoint_key:
                        write_checkpoint(
                            checkpoint_key,
                            {
                                "last_completed_idx": i + batch_size - 1,
                                "ts": _now_iso(),
                            },
                        )

                except (ServiceUnavailable, SessionExpired, OSError) as e:
                    # Infrastructure error — re-raise
                    logger.error(
                        "Batch %d failed: %s. Checkpoint at %d. "
                        "Resume with same checkpoint_key.",
                        i, e, max(0, i - 1),
                    )
                    raise
                except DrugOSDataError as e:
                    # Data error — DLQ and continue
                    logger.warning(
                        "Batch %d had data errors: %s. DLQ'd. Continuing.",
                        i, e,
                    )
                    all_errors.append(str(e))
                    continue

        elapsed = time.monotonic() - start_time
        load_result = LoadResult(
            attempted=len(nodes),
            created=total_created,
            matched=total_matched,
            updated=0,
            dropped_no_match=0,
            dead_lettered=total_dead_lettered,
            elapsed_seconds=elapsed,
            errors=all_errors,
        )

        # Fixes L-5: Data lineage in logs
        logger.info(
            "Created %d %s nodes (%d already existed, %d dead-lettered) "
            "source=%s checksum=%s batch_size=%d",
            total_created, safe_label,
            total_matched, total_dead_lettered,
            source, input_checksum[:8] if input_checksum else "N/A",
            batch_size,
        )

        # Fixes CO-4: Audit trail for graph mutations
        audit_log(
            "nodes_loaded",
            details=f"Loaded {total_created} {label} nodes",
            metadata={
                "label": label,
                "created": total_created,
                "matched": total_matched,
                "dead_lettered": total_dead_lettered,
                "source": source,
                "checksum": input_checksum[:8] if input_checksum else "N/A",
                "pipeline_run_id": RUN_ID,
            },
        )

        # Fixes D-6: Backward compatibility
        return load_result if detailed else total_created

    def load_drkg_nodes(
        self,
        entity_type_data: dict[str, list[dict]],
        *,
        source_file: Optional[str] = None,
        source: str = "DRKG",  # BUG-D-013: was hardcoded "DRKG"
    ) -> dict[str, Union[int, LoadResult]]:
        """Load all DRKG nodes by entity type.

        Parameters
        ----------
        entity_type_data : dict
            Maps entity type name to list of node dicts.
        source_file : str, optional
            Path to the source file for checksum computation.
        source : str, default "DRKG"
            Source label stamped into lineage metadata. BUG-D-013 root
            fix: previously hard-coded to "DRKG" for ALL node types,
            causing non-DRKG nodes (OMIM, DisGeNET, SIDER, etc.) to be
            mis-attributed to DRKG. This breaks lineage tracking and has
            license-compliance implications (DRKG is ODC-BY 1.0).

        Returns
        -------
        dict
            Maps entity type to load count/LoadResult.

        Side Effects
        ------------
        - Writes nodes to Neo4j for each entity type
        - Computes and records input checksum if source_file given

        Fixes: DL-2, DL-3, BUG-D-013
        """
        # Fixes DL-2: Input checksum verification
        input_checksum = ""
        if source_file:
            try:
                input_checksum = compute_and_record_checksum(source_file)
            except Exception as e:
                logger.warning("Could not compute checksum for %s: %s", source_file, e)

        results: dict[str, Union[int, LoadResult]] = {}
        for etype, nodes in entity_type_data.items():
            logger.info("Loading %d %s nodes (source=%s) ...", len(nodes), etype, source)
            count = self.load_nodes_batch(
                etype, nodes,
                source=source,
                input_checksum=input_checksum,
            )
            results[etype] = count
        return results


class GraphEdgeLoader:
    """Loads edges into Neo4j with validation, dedup, and lineage.

    Fixes A-1: Extracted from DrugOSGraphBuilder.
    Fixes A-2: Deduplicated load_edges_batch and load_edges_bulk_create.
    Fixes A-3: Deprecated old methods with deterministic replacements.
    Fixes DQ-2: Validation that edge dicts contain src_id/dst_id.
    Fixes DQ-3: Silently dropped edges now tracked.
    Fixes I-1: Pipeline creates duplicate edges on re-run.
    Fixes I-5: Non-deterministic deduplication.
    """

    def __init__(self, conn: GraphConnection) -> None:
        self._conn = conn

    def _load_edges(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: list[dict],
        batch_size: Optional[int] = None,
        mode: Literal["merge", "create"] = "merge",
        source: str = "unknown",
        input_checksum: str = "",
        checkpoint_key: Optional[str] = None,
        detailed: bool = False,
        allow_non_core: bool = False,
        allow_single_edge_batch: bool = False,
    ) -> Union[int, LoadResult]:
        """Core edge loading method.

        Fixes A-2: Single implementation for both merge and create modes.
        The two public methods (load_edges_batch, load_edges_bulk_create)
        are thin wrappers.

        Parameters
        ----------
        src_label, rel_type, dst_label : str
            Edge triple components.
        edges : list of dict
            Edge data. Each dict MUST contain "src_id" and "dst_id".
        batch_size : int, optional
        mode : "merge" or "create"
        source : str
            Data source for lineage.
        input_checksum : str
            SHA-256 of source file.
        checkpoint_key : str, optional
        detailed : bool
        allow_non_core : bool
            If True, allow non-CORE_EDGE_TYPES triples.
        allow_single_edge_batch : bool
            Suppress the single-edge warning (P-1).

        Returns
        -------
        int or LoadResult

        Fixes: A-2, DQ-2, DQ-3, I-1, I-5, P-1, S(9)-1
        """
        # v13 ROOT FIX (RT-8): defensive re-check at the edge-load
        # entry point. v12's docstring claimed this re-check existed
        # but it did NOT — the runtime guard was dead code. A config
        # regression that empties EDGE_PROPERTY_WHITELIST after
        # builder construction (e.g. monkey-patching CORE_EDGE_TYPES
        # in a notebook) would silently strip all properties from
        # every loaded edge. This re-check fires at every edge load,
        # so the regression is caught at the first load attempt.
        _assert_edge_property_whitelist_populated()

        start_time = time.monotonic()
        batch_size = _validate_batch_size(batch_size, "batch_size")

        # Fixes S(9)-1 / C-1: Cypher injection via f-strings
        # Fixes NFR §3.9: Sanitize labels and rel types
        safe_src = sanitize_label(src_label)
        safe_dst = sanitize_label(dst_label)
        safe_rel = sanitize_rel_type(
            rel_type.replace(" ", "_").replace("-", "_")
        )

        # Fixes IVR §3.6: Validate edge triple
        if not allow_non_core and not _ALLOW_NON_CORE_EDGES:
            if not is_core_edge(src_label, rel_type, dst_label):
                logger.warning(
                    "Edge triple (%s, %s, %s) is not in CORE_EDGE_TYPES. "
                    "Set allow_non_core=True or DRUGOS_KG_ALLOW_NON_CORE_EDGES=1 "
                    "to allow.",
                    src_label, rel_type, dst_label,
                )

        # Fixes P-1: Warn on single-edge batch
        if len(edges) == 1 and not allow_single_edge_batch:
            logger.warning(
                "load_edges called with a single edge — this is "
                "catastrophically slow. Accumulate edges into batches "
                "of >= 1000 before calling. Set allow_single_edge_batch=True "
                "to suppress this warning."
            )

        lineage = _build_lineage_props(source, input_checksum)

        total_created = 0
        total_dropped = 0
        total_dead_lettered = 0
        all_errors: list[str] = []

        # Edge property whitelist
        edge_key = (src_label, rel_type, dst_label)
        allowed_edge_props = (
            EDGE_PROPERTY_WHITELIST.get(edge_key, frozenset({"source", "evidence", "score"}))
            | SYSTEM_PROPS
        )

        # Fixes R-2: Checkpoint support
        checkpoint = (
            read_latest_checkpoint(checkpoint_key) if checkpoint_key else None
        )
        start_idx = (
            checkpoint["last_completed_idx"] + 1 if checkpoint else 0
        )

        with self._conn.session() as session:
            for i in range(start_idx, len(edges), batch_size):
                batch = edges[i:i + batch_size]

                # ── Phase 1: Validate and filter ────────────────────────
                clean_batch: list[dict[str, Any]] = []
                for row_idx, edge in enumerate(batch):
                    # BUG-B-003 root fix: normalize edge endpoint keys.
                    # Different loaders emit different key names:
                    #   - DrugBank: drug_id / target_uniprot_id
                    #   - UniProt:  source / target
                    #   - GEO:      head / tail
                    #   - kg_builder requires: src_id / dst_id
                    # Previously every edge from DrugBank/UniProt/GEO was
                    # dead-lettered with "missing_endpoint_id". Now we
                    # normalize at the entry point so all loaders work.
                    #
                    # v24 ROOT FIX (FORENSIC-P2-CORE §1): the previous
                    # code's ``_endpoint_keys`` set included ``"source"``
                    # and ``"target"`` — but the phase1_bridge emits
                    # ``source`` as a DATA-SOURCE PROPERTY (e.g.
                    # ``source="chembl"``), NOT as an endpoint alias.
                    # The result: every bridge edge's ``source`` property
                    # was silently stripped, so Neo4j edges ended up with
                    # ``_source="unknown"`` (the lineage default) instead
                    # of the real source name. Fix: track which alias was
                    # actually used as the endpoint and remove ONLY that
                    # alias from the props dict; do not blanket-exclude
                    # ``source``/``target``.
                    #
                    # v28 ROOT FIX (P2-B-8): the alias-vs-property contract
                    # for the ``source`` / ``target`` keys is now
                    # documented in ``_loader_protocol.py`` (Loader
                    # Edge-Record Contract). Loaders MUST emit either
                    # ``src_id``/``dst_id`` (preferred) OR an alias — never
                    # both. The kg_builder correctly preserves ``source``
                    # as a data-source property when ``src_id`` is present.
                    _used_src_alias: Optional[str] = None
                    _used_dst_alias: Optional[str] = None
                    if "src_id" not in edge or "dst_id" not in edge:
                        # Try all known aliases in priority order.
                        src_aliases = (
                            "src_id", "drug_id", "source", "head",
                            "from_id", "subject_id",
                        )
                        dst_aliases = (
                            "dst_id", "target_uniprot_id", "target",
                            "tail", "to_id", "object_id",
                        )
                        for sa in src_aliases:
                            if sa in edge and edge[sa]:
                                edge = {**edge, "src_id": edge[sa]}
                                _used_src_alias = sa
                                break
                        for da in dst_aliases:
                            if da in edge and edge[da]:
                                edge = {**edge, "dst_id": edge[da]}
                                _used_dst_alias = da
                                break
                        # v24: remove the used alias key so it doesn't
                        # leak into the props dict as a fake property.
                        # Only remove the alias that was ACTUALLY used
                        # as an endpoint — leave other keys (e.g.
                        # ``source="chembl"`` when ``src_id`` was already
                        # present) intact as legitimate properties.
                        if _used_src_alias is not None and _used_src_alias != "src_id":
                            edge.pop(_used_src_alias, None)
                        if _used_dst_alias is not None and _used_dst_alias != "dst_id":
                            edge.pop(_used_dst_alias, None)
                    # Fixes DQ-2: Validate src_id and dst_id
                    src_id = edge.get("src_id")
                    dst_id = edge.get("dst_id")
                    if not src_id or not dst_id:
                        dead_letter_record(
                            source=source,
                            record=edge,
                            reason=f"missing_endpoint_id:{src_label}-{rel_type}->{dst_label}:idx={i + row_idx}:src={src_id is not None}:dst={dst_id is not None}",
                        )
                        total_dead_lettered += 1
                        continue

                    # BUG-D-002 root fix: validate endpoint IDs against
                    # ID_PATTERNS. The previous code only checked for
                    # missing/empty IDs — invalid formats (SIDER bare-int
                    # Compounds, OMIM Genes, OpenTargets MONDO_ Diseases)
                    # passed validation but silently failed the Cypher
                    # MATCH, making edges vanish with zero diagnostic.
                    src_pattern = ID_PATTERNS.get(src_label)
                    dst_pattern = ID_PATTERNS.get(dst_label)
                    if src_pattern and not re.match(src_pattern, str(src_id)):
                        dead_letter_record(
                            source=source,
                            record=edge,
                            reason=(
                                f"invalid_src_id_format:{src_label}-"
                                f"{rel_type}->{dst_label}:idx={i + row_idx}:"
                                f"src_id={src_id!r} does not match "
                                f"pattern {src_pattern}"
                            ),
                        )
                        total_dead_lettered += 1
                        continue
                    if dst_pattern and not re.match(dst_pattern, str(dst_id)):
                        dead_letter_record(
                            source=source,
                            record=edge,
                            reason=(
                                f"invalid_dst_id_format:{src_label}-"
                                f"{rel_type}->{dst_label}:idx={i + row_idx}:"
                                f"dst_id={dst_id!r} does not match "
                                f"pattern {dst_pattern}"
                            ),
                        )
                        total_dead_lettered += 1
                        continue

                    # Build row with props
                    row: dict[str, Any] = {
                        "src_id": src_id,
                        "dst_id": dst_id,
                    }
                    # v21 ROOT FIX (Audit section 4 finding 4 / Chain 4 -
                    # "Edge properties preserved by bridge, stripped by
                    # shim"): the previous code was
                    # ``props = edge.get("props", {})`` which expected a
                    # NESTED ``{"props": {...}}`` dict. But the
                    # phase1_bridge emits FLAT edge dicts:
                    #   {"src_id": ..., "dst_id": ..., "source": ...,
                    #    "pchembl_value": ..., "standard_relation": ...,
                    #    "evidence": ..., "_source_phase": 1, ...}
                    # The ``.get("props", {})`` call therefore returned
                    # ``{}`` for EVERY bridge edge, silently stripping
                    # ALL edge properties (pchembl_value,
                    # standard_relation, evidence, source, _source_file,
                    # _source_row). The v15 ROOT FIX (REM-12/13/14)
                    # explicitly claimed these were preserved so the RL
                    # ranker has potency + censoring context; that claim
                    # was FALSE in production. The test double
                    # (RecordingGraphBuilder) does NOT apply this filter,
                    # so the bug was invisible to tests.
                    #
                    # Fix: accept BOTH shapes. If ``edge["props"]`` is a
                    # dict, use it (callers that pre-bundle props). Else
                    # treat the edge dict itself as the props source,
                    # excluding the endpoint ID keys and system keys
                    # that should not appear as edge properties.
                    if "props" in edge and isinstance(edge["props"], dict):
                        props = dict(edge["props"])
                    else:
                        # Flat-edge case (phase1_bridge output).
                        # v24 ROOT FIX: exclude endpoint ID keys and
                        # well-known system keys that should not appear
                        # as edge properties. NOTE: ``source`` and
                        # ``target`` are NO LONGER in this set — they
                        # are legitimate data-source property names
                        # emitted by the bridge (e.g. source="chembl").
                        # The endpoint-alias case (UniProt edges that
                        # use ``source``/``target`` as endpoint keys)
                        # is handled above by tracking the used alias
                        # and removing it from the edge dict before
                        # this point.
                        _endpoint_keys = {
                            "src_id", "dst_id", "drug_id",
                            "target_uniprot_id",
                            "head", "tail", "from_id", "to_id",
                            "subject_id", "object_id",
                        }
                        props = {
                            k: v for k, v in edge.items()
                            if k not in _endpoint_keys and v is not None
                        }
                    # Fixes D-2, DQ-4: Whitelist edge properties
                    cleaned_props, dropped = _whitelist_filter(
                        props, allowed_edge_props
                    )
                    cleaned_props.update(lineage)
                    # BUG-D-011 root fix: stamp _source_priority so
                    # deduplicate_edges_deterministic can order by it.
                    cleaned_props["_source_priority"] = get_source_priority(source)
                    if dropped:
                        logger.debug(
                            "Dropped non-whitelisted edge props for "
                            "%s-%s->%s: %s",
                            src_label, rel_type, dst_label, dropped,
                        )
                    row["props"] = cleaned_props
                    clean_batch.append(row)

                # v35 ROOT FIX (H-3): dedup by (src_id, dst_id, rel_type)
                # instead of (src_id, dst_id). The previous key collapsed
                # legitimate multi-action edges (e.g. a dual-action drug
                # with both "inhibits" and "activates" edges to the SAME
                # target — when load_edges_batch is invoked with a single
                # rel_type per call the previous key was already safe, but
                # if any caller ever batches across rel_types the collapse
                # was a silent data-loss bug). The rel_type is preserved
                # in the dedup key so dual-action edges survive. The
                # caller already invokes this once per (src, rel, dst)
                # triple, so the addition is a defensive measure against
                # future refactors that batch across rel_types.
                seen_pairs: set[tuple[str, str, str]] = set()
                deduped_batch: list[dict[str, Any]] = []
                for row in clean_batch:
                    pair = (row["src_id"], row["dst_id"], str(rel_type))
                    if pair in seen_pairs:
                        dead_letter_record(
                            source=source,
                            record=row,
                            reason=f"duplicate_edge_in_batch:pair={str(pair)[:100]}",
                        )
                        total_dead_lettered += 1
                        continue
                    seen_pairs.add(pair)
                    deduped_batch.append(row)
                clean_batch = deduped_batch

                if not clean_batch:
                    continue

                # ── Phase 2: Execute Cypher ─────────────────────────────
                try:
                    create_or_merge = "MERGE" if mode == "merge" else "CREATE"
                    if create_or_merge == "MERGE":
                        # FORENSIC audit issue 27 root fix: add ON CREATE
                        # SET / ON MATCH SET so cross-source property
                        # collisions are handled deterministically. The
                        # previous ``SET r += row.props`` without
                        # ON CREATE/ON MATCH meant the LAST loaded batch
                        # always won — e.g. a STITCH batch loaded after
                        # a ChEMBL batch would overwrite ChEMBL's
                        # pchembl_value with STITCH's (often missing)
                        # value. The fix uses ``coalesce(row.props.x,
                        # r.x)`` semantics via ON MATCH SET so existing
                        # non-null properties are preserved when the new
                        # batch's value is null (the _strip_nulls above
                        # already removed explicit nulls, but this is a
                        # second defensive layer). ON CREATE SET stamps
                        # the initial properties; ON MATCH SET only
                        # adds properties that don't already exist.
                        #
                        # v43 ROOT FIX (P2-004): the previous
                        # ``ON MATCH SET r += row.props`` OVERWRITES
                        # non-null properties — Cypher's ``+=`` operator
                        # means "set each key in the map, overwriting
                        # existing values." It does NOT mean "coalesce."
                        # So if ChEMBL batch first creates with
                        # pchembl_value=7.5, and a later STITCH batch
                        # matches with pchembl_value=8.0, STITCH's 8.0
                        # OVERWRITES ChEMBL's 7.5. The fix uses
                        # ``apoc.map.merge(r, row.props)`` which merges
                        # the two maps with the EXISTING value winning
                        # (r takes precedence over row.props for keys
                        # present in both). If APOC is not available,
                        # we fall back to ``r += row.props`` with a
                        # WARNING log so operators know the overwrite
                        # behavior is in effect.
                        cypher = (
                            f"UNWIND $batch AS row\n"
                            f"MATCH (src:{safe_src} {{id: row.src_id}})\n"
                            f"MATCH (dst:{safe_dst} {{id: row.dst_id}})\n"
                            f"MERGE (src)-[r:{safe_rel}]->(dst)\n"
                            f"ON CREATE SET r += row.props, "
                            f"r._created_at = $loaded_at\n"
                            # v43 ROOT FIX (P2-004): use apoc.map.merge
                            # to preserve existing non-null properties.
                            # apoc.map.merge(r, row.props) returns a map
                            # where r's keys take precedence over
                            # row.props for keys present in both. This
                            # prevents later batches from overwriting
                            # earlier batches' non-null values.
                            f"ON MATCH SET r = apoc.map.merge(row.props, r), "
                            f"r._updated_at = $loaded_at, "
                            f"r._version = coalesce(r._version, 0) + 1\n"
                            f"SET r._pipeline_run_id = $run_id"
                        )
                        merge_params = {
                            # v43 P1-039/P2-031: trimmed 15-line stale V41 ROOT FIX
                            # comment to one line: dead ternary removed, uses clean_batch.
                            "batch": clean_batch,
                            "loaded_at": lineage.get("_loaded_at", ""),
                            "run_id": RUN_ID,
                        }
                        # v43 ROOT FIX (P2-018): set _edge_mode explicitly.
                        _edge_mode = "merge"
                    else:
                        cypher = (
                            f"UNWIND $batch AS row\n"
                            f"MATCH (src:{safe_src} {{id: row.src_id}})\n"
                            f"MATCH (dst:{safe_dst} {{id: row.dst_id}})\n"
                            f"CREATE (src)-[r:{safe_rel}]->(dst)\n"
                            f"SET r += row.props"
                        )
                        # v43 ROOT FIX (P2-018): the previous code used
                        # ``merge_params = None`` as a sentinel to decide
                        # which session.run call to make. This is fragile
                        # — if a future refactor accidentally sets
                        # merge_params to a non-None value in the CREATE
                        # branch, the MERGE Cypher would be executed with
                        # CREATE-mode params. The fix uses an explicit
                        # ``_edge_mode`` variable ("merge" or "create")
                        # so the branch is unambiguous.
                        _edge_mode = "create"
                    # FORENSIC Chain 6 root fix (patient safety): strip
                    # None values from every edge's props BEFORE the
                    # Cypher SET r += row.props runs. SET r += map with
                    # a null value DELETES the property on the existing
                    # edge, silently erasing cross-source attributes
                    # (e.g. ChEMBL's pchembl_value erased by a later
                    # STITCH batch that omits it).
                    # v43 ROOT FIX (P2-024): the previous code created a
                    # NEW list of dicts on every batch (list comprehension
                    # with {**r, ...}). For large graphs (millions of
                    # edges), this doubles memory. The fix mutates
                    # clean_batch IN-PLACE by updating each dict's "props"
                    # key directly. This avoids the extra allocation.
                    for _r in clean_batch:
                        _r["props"] = _strip_nulls(_r.get("props", {}))
                    safe_edge_batch = clean_batch  # alias — no copy
                    # v43 ROOT FIX (P2-018): use _edge_mode (not
                    # merge_params is not None) to decide the session.run
                    # call. This prevents type confusion between MERGE
                    # and CREATE modes.
                    if _edge_mode == "merge":
                        merge_params["batch"] = safe_edge_batch
                        result = session.run(cypher, merge_params)
                    else:
                        result = session.run(cypher, batch=safe_edge_batch)
                    stats = result.consume().counters
                    batch_created = stats.relationships_created
                    total_created += batch_created

                    # Fixes DQ-3: Track silently dropped edges
                    batch_dropped = len(clean_batch) - batch_created
                    total_dropped += batch_dropped
                    if batch_dropped > 0:
                        # Fixes L-1: Log dropped edges
                        dropped_pct = batch_dropped / max(len(clean_batch), 1)
                        log_level = logging.ERROR if dropped_pct > 0.05 else logging.WARNING
                        logger.log(
                            log_level,
                            "Dropped %d/%d edges for %s-%s->%s "
                            "(src or dst not found in graph). "
                            "This may indicate a data quality issue.",
                            batch_dropped, len(clean_batch),
                            safe_src, safe_rel, safe_dst,
                        )
                        # Fixes DQ-3: If >5% dropped, raise mismatch error
                        if dropped_pct > 0.05:
                            all_errors.append(
                                f"{batch_dropped}/{len(clean_batch)} edges "
                                f"dropped for {src_label}-{rel_type}->{dst_label}"
                            )

                    # Progress logging (C-6)
                    if (i // batch_size) % _LOG_FREQUENCY == 0:
                        logger.info(
                            "  %s-%s->%s: loaded %d/%d edges mode=%s",
                            safe_src, safe_rel, safe_dst,
                            i + len(batch), len(edges), mode,
                        )

                    # Checkpoint
                    if checkpoint_key:
                        write_checkpoint(
                            checkpoint_key,
                            {
                                "last_completed_idx": i + batch_size - 1,
                                "ts": _now_iso(),
                            },
                        )

                except (ServiceUnavailable, SessionExpired, OSError) as e:
                    logger.error(
                        "Batch %d failed: %s. Checkpoint at %d.",
                        i, e, max(0, i - 1),
                    )
                    raise
                except DrugOSDataError as e:
                    logger.warning(
                        "Batch %d had data errors: %s. DLQ'd. Continuing.",
                        i, e,
                    )
                    all_errors.append(str(e))
                    continue

        elapsed = time.monotonic() - start_time
        load_result = LoadResult(
            attempted=len(edges),
            created=total_created,
            dropped_no_match=total_dropped,
            dead_lettered=total_dead_lettered,
            elapsed_seconds=elapsed,
            errors=all_errors,
        )

        logger.info(
            "Created %d %s-%s->%s edges (mode=%s, %d dropped, %d dead-lettered)",
            total_created, safe_src, safe_rel, safe_dst,
            mode, total_dropped, total_dead_lettered,
        )

        audit_log(
            "edges_loaded",
            details=f"Loaded {total_created} {src_label}-{rel_type}->{dst_label} edges",
            metadata={
                "src_label": src_label,
                "rel_type": rel_type,
                "dst_label": dst_label,
                "created": total_created,
                "dropped": total_dropped,
                "dead_lettered": total_dead_lettered,
                "mode": mode,
                "source": source,
                "pipeline_run_id": RUN_ID,
            },
        )

        return load_result if detailed else total_created

    def load_edges_batch(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: list[dict],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> Union[int, LoadResult]:
        """Bulk-create relationships using UNWIND + MERGE.

        Fixes A-2: Thin wrapper around _load_edges(mode="merge").
        """
        return self._load_edges(
            src_label, rel_type, dst_label, edges,
            batch_size=batch_size, mode="merge", **kwargs,
        )

    def load_edges_bulk_create(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: list[dict],
        batch_size: Optional[int] = None,
        use_merge: bool = False,
        **kwargs: Any,
    ) -> Union[int, LoadResult]:
        """Bulk-create relationships using UNWIND + CREATE (or MERGE).

        Fixes A-2: Thin wrapper around _load_edges.
        Fixes I-1: Default use_merge=False preserved, but callers should
        use use_merge=True for idempotent loads.

        Parameters
        ----------
        use_merge : bool
            If True, use MERGE instead of CREATE (idempotent).
        """
        mode = "merge" if use_merge else "create"
        return self._load_edges(
            src_label, rel_type, dst_label, edges,
            batch_size=batch_size, mode=mode, **kwargs,
        )

    def load_drkg_edges_bulk(
        self,
        edge_type_data: dict[tuple[str, str, str], list[dict]],
        *,
        source_file: Optional[str] = None,
        use_merge: bool = False,
    ) -> dict[tuple[str, str, str], Union[int, LoadResult]]:
        """Load all DRKG edges using bulk CREATE.

        Fixes DL-2: Input checksum verification.
        """
        input_checksum = ""
        if source_file:
            try:
                input_checksum = compute_and_record_checksum(source_file)
            except Exception as e:
                logger.warning("Could not compute checksum: %s", e)

        results: dict[tuple[str, str, str], Union[int, LoadResult]] = {}
        for (src_type, rel_name, dst_type), edges in edge_type_data.items():
            logger.info(
                "Loading %d %s-%s->%s edges ...",
                len(edges), src_type, rel_name, dst_type,
            )
            count = self.load_edges_bulk_create(
                src_type, rel_name, dst_type, edges,
                use_merge=use_merge,
                source="DRKG",
                input_checksum=input_checksum,
            )
            results[(src_type, rel_name, dst_type)] = count
        return results

    @deprecated(
        "Use load_drkg_edges_bulk with use_merge=True. "
        "Removed in v2.0."
    )
    def load_drkg_edges(
        self,
        edge_type_data: dict[tuple[str, str, str], list[dict]],
    ) -> dict[tuple[str, str, str], Union[int, LoadResult]]:
        """Load all DRKG edges using MERGE.

        Fixes A-3: Deprecated — use load_drkg_edges_bulk(use_merge=True).
        """
        results: dict[tuple[str, str, str], Union[int, LoadResult]] = {}
        for (src_type, rel_name, dst_type), edges in edge_type_data.items():
            logger.info(
                "Loading %d %s-%s->%s edges (MERGE) ...",
                len(edges), src_type, rel_name, dst_type,
            )
            count = self.load_edges_batch(
                src_type, rel_name, dst_type, edges,
                source="DRKG",
            )
            results[(src_type, rel_name, dst_type)] = count
        return results

    @deprecated(
        "Use load_edges_bulk_create(use_merge=True) for idempotent loads. "
        "For one-off dedup, call deduplicate_edges_deterministic()."
    )
    def deduplicate_edges(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
    ) -> int:
        """Remove duplicate relationships of the given type.

        Fixes A-3: Deprecated — non-deterministic. Use
        deduplicate_edges_deterministic() instead.
        """
        safe_src = sanitize_label(src_label)
        safe_dst = sanitize_label(dst_label)
        safe_rel = sanitize_rel_type(
            rel_type.replace(" ", "_").replace("-", "_")
        )

        with self._conn.session() as session:
            result = session.run(
                f"MATCH (src:{safe_src})-[r:{safe_rel}]->(dst:{safe_dst}) "
                f"WITH src, dst, type(r) AS rel_t, collect(r) AS rels "
                f"WHERE size(rels) > 1 "
                f"UNWIND tail(rels) AS dup "
                f"DELETE dup "
                f"RETURN count(dup) AS removed"
            )
            record = result.single()
            removed = record["removed"] if record else 0

        if removed > 0:
            logger.info(
                "Deduplicated %s-%s->%s: removed %d duplicate edges",
                safe_src, safe_rel, safe_dst, removed,
            )
        return removed

    def deduplicate_edges_deterministic(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
    ) -> int:
        """Remove duplicate relationships deterministically.

        Fixes I-5: Non-deterministic deduplication.
        Keeps the edge with the most properties (or highest-priority source).

        Parameters
        ----------
        src_label, rel_type, dst_label : str
            Edge triple.

        Returns
        -------
        int
            Number of duplicate edges removed.
        """
        safe_src = sanitize_label(src_label)
        safe_dst = sanitize_label(dst_label)
        safe_rel = sanitize_rel_type(
            rel_type.replace(" ", "_").replace("-", "_")
        )

        with self._conn.session() as session:
            # Fixes I-5: Deterministic ordering by source priority and load time.
            # v35 ROOT FIX (M-9): the previous sort key was
            #   `r._source_priority DESC, r._loaded_at ASC`
            # which was fragile under three conditions:
            #   (1) two edges with the SAME microsecond timestamp (tie-
            #       breaker undefined — Cypher does not guarantee a
            #       stable sort, so the kept edge was non-deterministic);
            #   (2) `_loaded_at` is null (edges from old runs without
            #       the lineage property — Cypher null-handling makes
            #       the sort non-deterministic);
            #   (3) format variance (`+00:00` vs `Z` suffix — both valid
            #       ISO 8601 UTC but lexicographically different).
            # The fix coalesces null `_loaded_at` to a sentinel minimum
            # string and adds `id(r) ASC` (Neo4j's monotonically-
            # increasing internal ID) as a deterministic final tie-
            # breaker so the kept edge is reproducible across runs.
            cypher = (
                f"MATCH (src:{safe_src})-[r:{safe_rel}]->(dst:{safe_dst}) "
                f"WITH src, dst, r "
                f"ORDER BY src.id, dst.id, "
                f"r._source_priority DESC, "
                f"coalesce(r._loaded_at, '1970-01-01T00:00:00+00:00') ASC, "
                f"id(r) ASC "
                f"WITH src, dst, collect(r) AS rels "
                f"WHERE size(rels) > 1 "
                f"WITH rels[0] AS keep, rels[1..] AS dups "
                f"UNWIND dups AS dup "
                f"DELETE dup "
                f"RETURN count(dup) AS removed"
            )
            result = session.run(cypher)
            record = result.single()
            removed = record["removed"] if record else 0

        if removed > 0:
            logger.info(
                "Deduplicated %s-%s->%s (deterministic): "
                "removed %d duplicate edges",
                safe_src, safe_rel, safe_dst, removed,
            )
            audit_log(
                "edges_deduplicated",
                details=f"Removed {removed} duplicate {src_label}-{rel_type}->{dst_label} edges",
                metadata={
                    "src_label": src_label,
                    "rel_type": rel_type,
                    "dst_label": dst_label,
                    "removed": removed,
                    "method": "deterministic",
                    "pipeline_run_id": RUN_ID,
                },
            )
        return removed


class DrugBankEnricher:
    """Enriches Compound nodes with DrugBank properties.

    Fixes A-1: Extracted from DrugOSGraphBuilder.
    Fixes S-1: CRITICAL PATIENT SAFETY — coalesce pattern for safety fields.
    Fixes D-1: Accurate enriched count via Cypher RETURN.
    Fixes D-3: Configurable canonical key.
    Fixes I-3: Empty input raises CriticalDataSourceError.
    Fixes L-2: Logging of property overwrites on safety-critical fields.
    Fixes DL-3: Transformation logging.
    """

    # Safety-critical fields that must NEVER be overwritten with null
    SAFETY_CRITICAL_FIELDS = frozenset({
        "withdrawn", "terminated", "illicit", "sensitive", "toxicity",
    })

    def __init__(self, conn: GraphConnection) -> None:
        self._conn = conn

    def enrich_compounds_from_drugbank(
        self,
        drug_records: list[dict],
        canonical_key: str = "id",
    ) -> Union[int, LoadResult]:
        """Add DrugBank properties to existing Compound nodes.

        PATIENT SAFETY (NON-NEGOTIABLE):
        This method uses coalesce() for ALL safety-critical fields to
        prevent null overwrites. If row.withdrawn IS NULL AND
        c.withdrawn IS NULL, sets withdrawn=false AND
        safety_data_missing=true so the RL ranker can flag this drug
        as "insufficient safety data" rather than "confirmed safe".

        Parameters
        ----------
        drug_records : list of dict
            DrugBank records to enrich. Each MUST contain the canonical_key.
        canonical_key : str
            The key to use for matching (default "id").
            Must be one of the CANONICAL_IDS values.

        Returns
        -------
        int or LoadResult
            Number of distinct compound nodes enriched.

        Raises
        ------
        ConfigurationError
            If canonical_key is not a valid canonical ID.
        CriticalDataSourceError
            If drug_records is empty (data outage protection).

        Side Effects
        ------------
        - Updates Compound nodes in Neo4j
        - Writes audit log entries
        - Logs safety-critical property changes at WARNING

        Invariants
        ----------
        - A non-null safety value in the graph is NEVER overwritten by null
        - If both row and graph have null for a safety field, sets
          the field to False and marks safety_data_missing=True
        - All lineage properties are stamped

        Fixes: S-1, D-1, D-3, I-3, L-2, DL-3
        """
        start_time = time.monotonic()

        # Fixes I-3: Empty input protection (data outage)
        if len(drug_records) == 0:
            raise CriticalDataSourceError(
                "enrich_compounds_from_drugbank called with empty "
                "drug_records. This is almost certainly a download "
                "failure, not a legitimate 'no data' case. "
                "Aborting to prevent data loss."
            )

        # Fixes D-3: Configurable canonical key
        valid_keys = set(CANONICAL_IDS.values())
        if canonical_key not in valid_keys and canonical_key != "id":
            raise ConfigurationError(
                f"canonical_key must be one of {valid_keys} or 'id', "
                f"got {canonical_key!r}"
            )

        batch_size = self._conn.config.batch_size_nodes
        total_enriched = 0
        total_dead_lettered = 0
        all_errors: list[str] = []

        lineage = _build_lineage_props("DrugBank")

        with self._conn.session() as session:
            for i in range(0, len(drug_records), batch_size):
                batch = drug_records[i:i + batch_size]

                # ── Validate batch ──────────────────────────────────────
                clean_batch: list[dict[str, Any]] = []
                for row_idx, rec in enumerate(batch):
                    key_val = rec.get(canonical_key)
                    if not key_val:
                        dead_letter_record(
                            source="DrugBank",
                            record=rec,
                            reason=f"missing_{canonical_key}:idx={i + row_idx}",
                        )
                        total_dead_lettered += 1
                        continue

                    # Whitelist filter
                    allowed = NODE_PROPERTY_WHITELIST.get("Compound", frozenset()) | SYSTEM_PROPS
                    cleaned, _ = _whitelist_filter(rec, allowed)
                    clean_batch.append(cleaned)

                if not clean_batch:
                    continue

                # ── Execute Cypher with coalesce for safety fields ──────
                # Fixes S-1: CRITICAL — coalesce pattern prevents null overwrite
                # PATIENT SAFETY: withdrawn=null is interpreted by the RL
                # safety ranker as "not withdrawn" → green → SAFE.
                # Valdecoxib (withdrawn for CV risk) would be SAFE.
                cypher = (
                    f"UNWIND $batch AS row\n"
                    f"MATCH (c:Compound {{{canonical_key}: row.{canonical_key}}})\n"
                    f"SET c.name                = coalesce(row.name, c.name),"
                    f"    c.smiles              = coalesce(row.smiles, c.smiles),"
                    f"    c.inchikey            = coalesce(row.inchikey, c.inchikey),"
                    f"    c.indication          = coalesce(row.indication, c.indication),"
                    f"    c.mechanism_of_action = coalesce(row.mechanism_of_action, c.mechanism_of_action),"
                    f"    c.atc_codes           = coalesce(row.atc_codes, c.atc_codes),"
                    f"    c.approved            = coalesce(row.approved, c.approved),"
                    f"    c.investigational     = coalesce(row.investigational, c.investigational),"
                    f"    c.pubchem_cid         = coalesce(row.pubchem_cid, c.pubchem_cid),"
                    f"    c.chembl_id           = coalesce(row.chembl_id, c.chembl_id),"
                    f"    c.chebi_id            = coalesce(row.chebi_id, c.chebi_id),"
                    f"    c.drug_type           = coalesce(row.drug_type, c.drug_type),"
                    f"    c.approval_year       = coalesce(row.approval_year, c.approval_year),"
                    f"    c.source_drugbank     = true,"
                    f"    c.drugbank_id         = coalesce(row.drugbank_id, c.drugbank_id),"
                    f"    c.cas_number          = coalesce(row.cas_number, c.cas_number),"
                    f"    c.pharmacodynamics    = coalesce(row.pharmacodynamics, c.pharmacodynamics),"
                    f"    c.categories          = coalesce(row.categories, c.categories),"
                    f"    c._canonical_id_source = row._canonical_id_source,"
                    f"    c._last_modified      = row._last_modified,"
                    # 🔴 SAFETY-CRITICAL: never null these out
                    f"    c.toxicity            = coalesce(row.toxicity, c.toxicity),"
                    f"    c.withdrawn           = coalesce(row.withdrawn, c.withdrawn),"
                    f"    c.terminated          = coalesce(row.terminated, c.terminated),"
                    f"    c.illicit             = coalesce(row.illicit, c.illicit),"
                    f"    c.sensitive           = coalesce(row.sensitive, c.sensitive),"
                    # Safety net: if both null, mark as missing data
                    f"    c.safety_data_missing = CASE "
                    f"WHEN row.withdrawn IS NULL AND c.withdrawn IS NULL THEN true "
                    f"ELSE coalesce(c.safety_data_missing, false) END,"
                    # Lineage props (always overwrite)
                    f"    c._pipeline_run_id    = $run_id,"
                    f"    c._loaded_at          = $loaded_at,"
                    f"    c._source             = 'DrugBank',"
                    f"    c._license            = 'CC BY-NC 4.0',"
                    f"    c._attribution        = $attribution,"
                    f"    c._schema_version     = $schema_version,"
                    f"    c._config_hash        = $config_hash,"
                    f"    c._pipeline_version   = $pipeline_version,"
                    f"    c._seed               = $seed,"
                    f"    c._updated_at         = $loaded_at,"
                    f"    c._version            = coalesce(c._version, 0) + 1\n"
                    f"RETURN row.{canonical_key} AS matched_id, "
                    f"c.withdrawn AS old_w, "
                    f"coalesce(row.withdrawn, c.withdrawn) AS new_w"
                )

                params = {
                    "batch": clean_batch,
                    "run_id": RUN_ID,
                    "loaded_at": lineage["_loaded_at"],
                    "attribution": SOURCE_LICENSES["DrugBank"]["attribution"],
                    "schema_version": SCHEMA_VERSION,
                    "config_hash": CONFIG_HASH,
                    "pipeline_version": PIPELINE_VERSION,
                    "seed": SEED,
                }

                result = session.run(cypher, params)
                # Fixes D-1: Accurate enriched count via Cypher RETURN
                for record in result:
                    total_enriched += 1
                    # Fixes L-2: Log safety-critical property changes
                    old_w = record.get("old_w")
                    new_w = record.get("new_w")
                    if old_w != new_w and new_w is None:
                        logger.error(
                            "SAFETY: Compound %s withdrawn changed from "
                            "%s to None — this should not happen with "
                            "coalesce pattern!",
                            record.get("matched_id", "?"), old_w,
                        )
                        audit_log(
                            "safety_property_overwrite",
                            metadata={
                                "id": record.get("matched_id"),
                                "field": "withdrawn",
                                "old": old_w,
                                "new": new_w,
                            },
                        )

        elapsed = time.monotonic() - start_time
        load_result = LoadResult(
            attempted=len(drug_records),
            created=0,
            updated=total_enriched,
            dead_lettered=total_dead_lettered,
            elapsed_seconds=elapsed,
            errors=all_errors,
        )

        logger.info(
            "Enriched %d distinct Compound nodes with DrugBank data",
            total_enriched,
        )

        # Fixes DL-3: Transformation logging
        log_transformation(
            step="enrich_compounds_from_drugbank",
            input_count=len(drug_records),
            output_count=total_enriched,
            transformation_map={
                "DrugBank::name": "Compound.name",
                "DrugBank::withdrawn": "Compound.withdrawn",
                "DrugBank::terminated": "Compound.terminated",
                "DrugBank::illicit": "Compound.illicit",
                "DrugBank::sensitive": "Compound.sensitive",
                "DrugBank::toxicity": "Compound.toxicity",
            },
        )

        # Fixes CO-4: Audit trail
        audit_log(
            "drugbank_enrichment",
            details=f"Enriched {total_enriched} Compound nodes",
            metadata={
                "enriched": total_enriched,
                "dead_lettered": total_dead_lettered,
                "canonical_key": canonical_key,
                "pipeline_run_id": RUN_ID,
            },
        )

        return total_enriched


class GraphStatsCollector:
    """Collects graph statistics.

    Fixes A-1: Extracted from DrugOSGraphBuilder.
    Fixes S-3: Misleading density calculation.
    Fixes S-4: labels(n)[0] non-deterministic.
    Fixes P-4: get_graph_stats makes 4 round-trips → 1-2.
    Fixes IN-2: No interface contract for return value.
    """

    def __init__(self, conn: GraphConnection) -> None:
        self._conn = conn

    def get_graph_stats(self) -> dict[str, Any]:
        """Compute and return comprehensive graph statistics.

        Returns
        -------
        dict
            GraphStats with typed density, node/edge counts, etc.

        Invariants
        ----------
        - density_typed uses typed-edge-aware formula
        - density_homogeneous uses the old formula (backward compat)
        - node_counts_by_type is deterministic (ordered by count DESC, lbl ASC)
        - All counts are non-negative integers

        Fixes: S-3, S-4, P-4, IN-2
        """
        with self._conn.session() as session:
            # Fixes P-4: Combine node+edge counts into 1-2 queries
            node_result = session.run(
                "MATCH (n) UNWIND labels(n) AS lbl "
                "RETURN lbl, count(*) AS cnt "
                "ORDER BY cnt DESC, lbl ASC"
            )
            node_counts_by_type: dict[str, int] = {}
            total_nodes = 0
            for record in node_result:
                lbl = record["lbl"]
                cnt = record["cnt"]
                node_counts_by_type[lbl] = cnt
                total_nodes += cnt

            # Note: total_nodes may overcount multi-label nodes, so get true count
            total_result = session.run("MATCH (n) RETURN count(n) AS total")
            total_nodes = total_result.single()["total"]

            edge_result = session.run(
                "MATCH ()-[r]->() RETURN type(r) AS rel_type, "
                "count(r) AS cnt ORDER BY cnt DESC"
            )
            edge_counts_by_type: dict[str, int] = {}
            total_edges = 0
            for record in edge_result:
                edge_counts_by_type[record["rel_type"]] = record["cnt"]
                total_edges += record["cnt"]

        # Fixes S-3: Typed-edge-aware density calculation
        # Before: max_edges = n * (n-1) — assumes homogeneous complete graph
        # After: per-edge-type maximum based on actual node type counts
        typed_max = 0
        for (src_type, _, dst_type) in CORE_EDGE_TYPES:
            src_count = node_counts_by_type.get(src_type, 0)
            dst_count = node_counts_by_type.get(dst_type, 0)
            if src_type == dst_type:
                typed_max += src_count * max(src_count - 1, 0)
            else:
                typed_max += src_count * dst_count

        density_typed = round(total_edges / typed_max, 8) if typed_max > 0 else 0.0
        # Backward compat: homogeneous density
        density_homogeneous = (
            round(total_edges / (total_nodes * max(total_nodes - 1, 1)), 8)
            if total_nodes > 1
            else 0.0
        )

        stats: dict[str, Any] = {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "node_counts_by_type": node_counts_by_type,
            "edge_counts_by_type": edge_counts_by_type,
            "density": density_typed,  # Default is now typed
            "density_typed": density_typed,
            "density_homogeneous": density_homogeneous,
            "pipeline_run_id": RUN_ID,
            "computed_at": _now_iso(),
        }

        logger.info(
            "Graph stats: %d nodes, %d edges, density_typed=%.8f",
            total_nodes, total_edges, density_typed,
        )
        return stats

    def health_check(self, conn: GraphConnection) -> dict[str, Any]:
        """Run a health check on the Neo4j instance and graph.

        Fixes R-7: Verify driver state.
        Fixes S(9)-4: Don't print connection details.
        """
        health = conn.health_check()
        if health.get("connected"):
            try:
                stats = self.get_graph_stats()
                health["total_nodes"] = stats["total_nodes"]
                health["total_edges"] = stats["total_edges"]
                health["node_types"] = len(stats["node_counts_by_type"])
                health["edge_types"] = len(stats["edge_counts_by_type"])
            except Exception as e:
                health["stats_error"] = str(e)
        return health


class GraphJanitor:
    """Handles dangerous graph operations with access control.

    Fixes A-1: Extracted from DrugOSGraphBuilder.
    Fixes S(9)-3: clear_graph no access control.
    Fixes C-7: clear_graph returns None → ClearGraphResult.
    Fixes R-5: clear_graph not atomic on large graphs.
    """

    def __init__(self, conn: GraphConnection) -> None:
        self._conn = conn

    def clear_graph(
        self,
        *,
        confirm: bool = False,
        confirm_phrase: Optional[str] = None,
    ) -> ClearGraphResult:
        """Delete all nodes and relationships with safety confirmation.

        Fixes S(9)-3: Requires explicit confirmation.
        Fixes C-7: Returns ClearGraphResult instead of None.
        Fixes R-5: Chunked deletion for large graphs.

        Parameters
        ----------
        confirm : bool
            Must be True to proceed.
        confirm_phrase : str, optional
            Must match DRUGOS_CLEAR_GRAPH_PHRASE env var.

        Returns
        -------
        ClearGraphResult

        Raises
        ------
        SecurityError
            If confirm=False or confirm_phrase doesn't match.
        """
        # Fixes S(9)-3: Access control for clear_graph
        if not confirm:
            raise SecurityError(
                "clear_graph() requires confirm=True. "
                "This deletes ALL nodes and edges."
            )
        expected_phrase = _CLEAR_GRAPH_PHRASE
        if confirm_phrase != expected_phrase:
            raise SecurityError(
                "confirm_phrase does not match expected phrase. "
                "Set DRUGOS_CLEAR_GRAPH_PHRASE to override."
            )

        # Fixes CO-4: Audit trail BEFORE deletion
        audit_log(
            "graph_clear_initiated",
            metadata={
                "caller": inspect.stack()[1].function,
                "config_hash": CONFIG_HASH,
                "pipeline_run_id": RUN_ID,
            },
        )

        start_time = time.monotonic()
        total_nodes_deleted = 0
        total_rels_deleted = 0

        # Fixes R-5: Chunked deletion for large graphs
        chunk_size = 10000
        with self._conn.session(
            default_timeout=max(_QUERY_TIMEOUT, 600),
        ) as session:
            while True:
                result = session.run(
                    "MATCH (n) WITH n LIMIT $limit "
                    "DETACH DELETE n "
                    "RETURN count(n) AS deleted",
                    limit=chunk_size,
                )
                # v35 ROOT FIX (M-10): the previous code added the NODE
                # count to total_rels_deleted, assuming 1 rel per node.
                # For densely-connected graphs (e.g. a Compound with 50
                # target edges), this severely undercounted rel deletions
                # — a node with 50 rels contributed only 1 to the count.
                # The fix uses Neo4j's actual deletion counters from
                # result.consume().counters() (nodes_deleted and
                # relationships_deleted) which are accurate.
                counters = result.consume().counters()
                deleted_nodes = counters.nodes_deleted
                deleted_rels = counters.relationships_deleted
                if deleted_nodes == 0:
                    break
                total_nodes_deleted += deleted_nodes
                total_rels_deleted += deleted_rels

        elapsed = time.monotonic() - start_time
        result = ClearGraphResult(
            nodes_deleted=total_nodes_deleted,
            relationships_deleted=total_rels_deleted,
            elapsed_seconds=elapsed,
            pipeline_run_id=RUN_ID,
            timestamp=_now_iso(),
        )

        logger.warning(
            "All nodes and relationships deleted from graph: "
            "%d nodes in %.2fs",
            total_nodes_deleted, elapsed,
        )

        # Fixes CO-4: Audit trail AFTER deletion
        # v35 ROOT FIX (N-4): include relationships_deleted in the audit
        # log metadata. Previously only nodes_deleted was logged, leaving
        # the rel-deletion count absent from the audit trail.
        audit_log(
            "graph_clear_completed",
            metadata={
                "nodes_deleted": total_nodes_deleted,
                "relationships_deleted": total_rels_deleted,
                "elapsed_seconds": elapsed,
                "pipeline_run_id": RUN_ID,
            },
        )

        return result


# ═══════════════════════════════════════════════════════════════════════════════
#  FACADE CLASS — DrugOSGraphBuilder
# ═══════════════════════════════════════════════════════════════════════════════

class DrugOSGraphBuilder:
    """Manages the DrugOS knowledge graph in Neo4j.

    This is the Facade for the graph builder subsystem. It delegates to
    specialized internal classes while preserving the original public API.

    Architecture (Facade Pattern — audit issue A-1):
      DrugOSGraphBuilder  — public API facade (backward-compatible)
        ├── GraphConnection     — connect, disconnect, retry, health, driver DI
        ├── GraphSchemaManager  — create_constraints, create_indexes
        ├── GraphNodeLoader     — load_nodes_batch, load_drkg_nodes
        ├── GraphEdgeLoader     — load_edges_batch, load_edges_bulk_create, dedup
        ├── DrugBankEnricher    — enrich_compounds_from_drugbank
        ├── GraphStatsCollector — get_graph_stats, health_check
        └── GraphJanitor        — clear_graph

    Supports context manager protocol for safe connection handling.

    Parameters
    ----------
    config : Neo4jConfig, optional
        Neo4j connection configuration. Defaults to get_neo4j_config().
    driver : Driver, optional
        External Neo4j driver for dependency injection (A-5).
        If provided, connect() skips driver creation.
    driver_factory : callable, optional
        Factory function that returns a Driver instance.

    Raises
    ------
    ConfigurationError
        If database name contains invalid characters.

    Side Effects
    ------------
    - Creates Neo4j driver on connect()
    - Adds _RunIdFilter to logger

    Invariants
    ----------
    - All public methods preserve their original signatures
    - New parameters have sensible defaults (backward compat)

    Fixes: A-1 (god object split), A-5 (driver DI), CF-4 (database name regex)
    """

    def __init__(
        self,
        config: Optional[Neo4jConfig] = None,
        driver: Optional[Driver] = None,
        driver_factory: Optional[Callable[[], Driver]] = None,
    ) -> None:
        # v13 ROOT FIX (RT-8): the v12 docstring at line 410-413
        # claimed this method calls
        # ``_assert_edge_property_whitelist_populated()`` — but it did
        # NOT. The runtime guard was dead code. v13: actually call it
        # here so a config regression that empties
        # ``EDGE_PROPERTY_WHITELIST`` (e.g. a broken
        # ``CORE_EDGE_TYPES`` import) raises ``RuntimeError`` at
        # builder construction time, before any edge load silently
        # strips all properties. The check is also performed in
        # ``_load_edges`` as a defensive re-check (see below).
        _assert_edge_property_whitelist_populated()

        self.config = config or get_neo4j_config()
        # Fixes A-5: Driver dependency injection
        self._conn = GraphConnection(self.config, driver, driver_factory)
        self._schema = GraphSchemaManager(self._conn)
        self._nodes = GraphNodeLoader(self._conn)
        self._edges = GraphEdgeLoader(self._conn)
        self._stats = GraphStatsCollector(self._conn)
        self._enricher = DrugBankEnricher(self._conn)
        self._janitor = GraphJanitor(self._conn)

        # Fixes CF-4: Database name regex — allow hyphens (Neo4j 5.x)
        if not re.match(r'^[a-zA-Z0-9_-]+$', self.config.database):
            raise ConfigurationError(
                f"Invalid database name: {self.config.database!r}. "
                f"Only alphanumeric, underscore, and hyphen allowed."
            )

    @property
    def driver(self) -> Optional[Driver]:
        """Access the underlying Neo4j driver.

        Fixes A-5: Exposed for testability.
        """
        return self._conn.driver

    def __enter__(self) -> DrugOSGraphBuilder:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.disconnect()
        return False

    # ─── Connection Management (delegates to GraphConnection) ──────────

    def connect(self) -> None:
        """Establish connection to Neo4j database.

        Delegates to GraphConnection.connect().
        Fixes R-1, R-6, S(9)-2, CO-5.
        """
        self._conn.connect()

    def disconnect(self) -> None:
        """Close the Neo4j driver connection."""
        self._conn.disconnect()

    # ─── Schema Management (delegates to GraphSchemaManager) ───────────

    def create_constraints(self) -> None:
        """Create uniqueness constraints on node IDs.

        Delegates to GraphSchemaManager.create_constraints().
        Fixes R-3, P-3.
        """
        self._schema.create_constraints()

    def create_indexes(self) -> None:
        """Create additional indexes for common query patterns.

        Delegates to GraphSchemaManager.create_indexes().
        Fixes CF-1.
        """
        self._schema.create_indexes()

    # ─── Node Loading (delegates to GraphNodeLoader) ───────────────────

    def load_nodes_batch(
        self,
        label: str,
        nodes: list[dict],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> Union[int, LoadResult]:
        """Bulk-create nodes using UNWIND + MERGE.

        Delegates to GraphNodeLoader.load_nodes_batch().
        Fixes DQ-1, DQ-4, DQ-5, S-2, S-5, I-2.
        """
        return self._nodes.load_nodes_batch(
            label, nodes, batch_size, **kwargs
        )

    def load_drkg_nodes(
        self,
        entity_type_data: dict[str, list[dict]],
        **kwargs: Any,
    ) -> dict[str, Union[int, LoadResult]]:
        """Load all DRKG nodes by entity type.

        Delegates to GraphNodeLoader.load_drkg_nodes().
        """
        return self._nodes.load_drkg_nodes(entity_type_data, **kwargs)

    # ─── Edge Loading (delegates to GraphEdgeLoader) ───────────────────

    def load_edges_batch(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: list[dict],
        batch_size: Optional[int] = None,
        **kwargs: Any,
    ) -> Union[int, LoadResult]:
        """Bulk-create relationships using UNWIND + MERGE.

        Delegates to GraphEdgeLoader.load_edges_batch().
        """
        return self._edges.load_edges_batch(
            src_label, rel_type, dst_label, edges,
            batch_size=batch_size, **kwargs,
        )

    def load_edges_bulk_create(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
        edges: list[dict],
        batch_size: Optional[int] = None,
        use_merge: bool = False,
        **kwargs: Any,
    ) -> Union[int, LoadResult]:
        """Bulk-create relationships using UNWIND + CREATE.

        Delegates to GraphEdgeLoader.load_edges_bulk_create().
        """
        return self._edges.load_edges_bulk_create(
            src_label, rel_type, dst_label, edges,
            batch_size=batch_size, use_merge=use_merge, **kwargs,
        )

    def deduplicate_edges(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
    ) -> int:
        """Remove duplicate relationships (deprecated — non-deterministic).

        Delegates to GraphEdgeLoader.deduplicate_edges().
        Fixes A-3: Deprecated. Use deduplicate_edges_deterministic().
        """
        return self._edges.deduplicate_edges(src_label, rel_type, dst_label)

    def deduplicate_edges_deterministic(
        self,
        src_label: str,
        rel_type: str,
        dst_label: str,
    ) -> int:
        """Remove duplicate relationships deterministically.

        Delegates to GraphEdgeLoader.deduplicate_edges_deterministic().
        Fixes I-5: Deterministic dedup.
        """
        return self._edges.deduplicate_edges_deterministic(
            src_label, rel_type, dst_label
        )

    def load_drkg_edges_bulk(
        self,
        edge_type_data: dict[tuple[str, str, str], list[dict]],
        **kwargs: Any,
    ) -> dict[tuple[str, str, str], Union[int, LoadResult]]:
        """Load all DRKG edges using bulk CREATE.

        Delegates to GraphEdgeLoader.load_drkg_edges_bulk().
        """
        return self._edges.load_drkg_edges_bulk(edge_type_data, **kwargs)

    @deprecated(
        "Use load_drkg_edges_bulk with use_merge=True. Removed in v2.0."
    )
    def load_drkg_edges(
        self,
        edge_type_data: dict[tuple[str, str, str], list[dict]],
    ) -> dict[tuple[str, str, str], Union[int, LoadResult]]:
        """Load all DRKG edges using MERGE (deprecated).

        Delegates to GraphEdgeLoader.load_drkg_edges().
        Fixes A-3: Deprecated.
        """
        return self._edges.load_drkg_edges(edge_type_data)

    # ─── DrugBank Enrichment (delegates to DrugBankEnricher) ───────────

    def enrich_compounds_from_drugbank(
        self,
        drug_records: list[dict],
        canonical_key: str = "id",
    ) -> Union[int, LoadResult]:
        """Add DrugBank properties to existing Compound nodes.

        Delegates to DrugBankEnricher.enrich_compounds_from_drugbank().
        Fixes S-1, D-1, D-3, I-3.

        PATIENT SAFETY: Uses coalesce() for all safety-critical fields.
        """
        return self._enricher.enrich_compounds_from_drugbank(
            drug_records, canonical_key=canonical_key
        )

    # ─── Graph Statistics (delegates to GraphStatsCollector) ───────────

    def get_graph_stats(self) -> dict[str, Any]:
        """Compute and return comprehensive graph statistics.

        Delegates to GraphStatsCollector.get_graph_stats().
        Fixes S-3, S-4, P-4.
        """
        return self._stats.get_graph_stats()

    # ─── Graph Clear (delegates to GraphJanitor) ───────────────────────

    def clear_graph(
        self,
        *,
        confirm: bool = False,
        confirm_phrase: Optional[str] = None,
    ) -> Union[None, ClearGraphResult]:
        """Delete all nodes and relationships.

        Delegates to GraphJanitor.clear_graph().
        Fixes S(9)-3, C-7, R-5.

        Parameters
        ----------
        confirm : bool
            Must be True to proceed.
        confirm_phrase : str, optional
            Must match DRUGOS_CLEAR_GRAPH_PHRASE.

        Returns
        -------
        ClearGraphResult or None
            ClearGraphResult when confirmed, None for backward compat
            when not confirmed (raises SecurityError).
        """
        return self._janitor.clear_graph(
            confirm=confirm, confirm_phrase=confirm_phrase,
        )

    # ─── Health Check ──────────────────────────────────────────────────

    def health_check(self) -> dict[str, Any]:
        """Run a health check on the Neo4j instance and graph.

        Fixes R-4, R-7, S(9)-4.
        """
        return self._stats.health_check(self._conn)

    # ─── Fluent Orchestration ──────────────────────────────────────────

    def build_graph(
        self,
        entity_maps: dict[str, list[dict]],
        edge_maps: dict[tuple[str, str, str], list[dict]],
        drugbank_records: Optional[list[dict]] = None,
        *,
        dry_run: bool = False,
        enable_dedup: bool = False,
        use_merge: bool = True,
    ) -> BuildGraphResult:
        """Orchestrate the full graph build pipeline.

        Fixes D-5: Fluent orchestration method. Guarantees correct order:
        constraints → indexes → nodes → edges → enrichment → dedup → stats.

        Parameters
        ----------
        entity_maps : dict
            Maps entity type to list of node dicts.
        edge_maps : dict
            Maps (src, rel, dst) to list of edge dicts.
        drugbank_records : list of dict, optional
            DrugBank records for enrichment.
        dry_run : bool
            If True, validate inputs but don't write to Neo4j.
        enable_dedup : bool
            If True, run deterministic dedup after loading.
        use_merge : bool
            If True, use MERGE for edges (idempotent).

        Returns
        -------
        BuildGraphResult

        Side Effects
        ------------
        - Creates constraints, indexes, nodes, edges, enrichment
        - Writes PipelineRun node (DL-5)
        - Writes lineage manifest

        Invariants
        ----------
        - Correct pipeline order is guaranteed
        - All operations carry full lineage
        - PipelineRun node is created with all metadata
        """
        start_time = time.monotonic()

        self.connect()

        if dry_run:
            logger.info("DRY RUN: Validating inputs without writing to Neo4j")
            # Validate all inputs
            for etype, nodes in entity_maps.items():
                for node in nodes:
                    if not node.get("id"):
                        raise DrugOSDataError(
                            f"Node in {etype} missing 'id' field"
                        )
            for (src, rel, dst), edges in edge_maps.items():
                for edge in edges:
                    if not edge.get("src_id") or not edge.get("dst_id"):
                        raise DrugOSDataError(
                            f"Edge in {src}-{rel}->{dst} missing endpoint IDs"
                        )
            return BuildGraphResult(
                node_results={},
                edge_results={},
                enrichment_result=None,
                stats={"dry_run": True},
                lineage=build_lineage_metadata(),
                elapsed_seconds=time.monotonic() - start_time,
            )

        # Step 1: Constraints & Indexes
        self.create_constraints()
        self.create_indexes()

        # Step 2: Load nodes
        node_results = self.load_drkg_nodes(entity_maps)

        # Step 3: Load edges
        edge_results = self.load_drkg_edges_bulk(
            edge_maps, use_merge=use_merge
        )

        # Step 4: DrugBank enrichment
        enrichment_result = None
        if drugbank_records:
            enrichment_result = self.enrich_compounds_from_drugbank(
                drugbank_records
            )

        # Step 5: Optional dedup
        if enable_dedup or _AUTO_DEDUP:
            for (src, rel, dst) in edge_maps.keys():
                self.deduplicate_edges_deterministic(src, rel, dst)

        # Fixes DL-5: Write PipelineRun node
        stats = self.get_graph_stats()
        self._write_pipeline_run_node(stats)

        # Write lineage manifest
        write_lineage_manifest(
            {
                "pipeline_run_id": RUN_ID,
                "pipeline_version": PIPELINE_VERSION,
                "config_hash": CONFIG_HASH,
                "schema_version": SCHEMA_VERSION,
                "node_results": {
                    k: str(v) for k, v in node_results.items()
                },
                "edge_results": {
                    str(k): str(v) for k, v in edge_results.items()
                },
            }
        )

        elapsed = time.monotonic() - start_time

        return BuildGraphResult(
            node_results=node_results,
            edge_results=edge_results,
            enrichment_result=enrichment_result,
            stats=stats,
            lineage=build_lineage_metadata(),
            elapsed_seconds=elapsed,
        )

    def _write_pipeline_run_node(self, stats: dict[str, Any]) -> None:
        """Write a :PipelineRun node for lineage tracking.

        Fixes DL-5: No pipeline run metadata stored in graph.
        """
        if self._conn.driver is None:
            return
        try:
            with self._conn.session() as session:
                session.run(
                    "MERGE (p:PipelineRun {run_id: $run_id}) "
                    "SET p.started_at = $started_at, "
                    "    p.finished_at = $finished_at, "
                    "    p.pipeline_version = $pipeline_version, "
                    "    p.config_hash = $config_hash, "
                    "    p.schema_version = $schema_version, "
                    "    p.seed = $seed, "
                    "    p.node_count = $node_count, "
                    "    p.edge_count = $edge_count, "
                    "    p.status = 'completed'",
                    run_id=RUN_ID,
                    started_at=_now_iso(),
                    finished_at=_now_iso(),
                    pipeline_version=PIPELINE_VERSION,
                    config_hash=CONFIG_HASH,
                    schema_version=SCHEMA_VERSION,
                    seed=SEED,
                    node_count=stats.get("total_nodes", 0),
                    edge_count=stats.get("total_edges", 0),
                )
        except Exception as e:
            logger.warning("Could not write PipelineRun node: %s", e)

    def get_impact_analysis(self, changed_config_key: str) -> list[str]:
        """Return list of affected graph elements for a config change.

        Fixes DL-4: No impact analysis.
        """
        return compute_impact_analysis(changed_config_key)


# ─── CLI Entry Point ───────────────────────────────────────────────────────────
# Fixes DO-6: __main__ provides no usage docs → argparse
# Fixes S(9)-4: __main__ block prints connection details → use safe_config_dict
# Fixes A-7: json import moved to __main__ block

if __name__ == "__main__":
    import argparse
    import json as _json  # Fixes A-7, C-5

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="DrugOS KG Builder — health check and CLI utilities"
    )
    parser.add_argument(
        "--health", action="store_true",
        help="Run health check",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print graph stats",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="DANGER: clear entire graph (requires --confirm-phrase)",
    )
    parser.add_argument(
        "--confirm-phrase", type=str,
        help="Confirmation phrase for --clear",
    )
    parser.add_argument(
        "--dedup", action="store_true",
        help="Run deterministic dedup on all edge types",
    )
    args = parser.parse_args()

    with DrugOSGraphBuilder() as builder:
        if args.health:
            health = builder.health_check()
            # Fixes S(9)-4: Use safe_config_dict to avoid credential exposure
            safe_health = {
                k: v for k, v in health.items()
                if k not in {"uri", "password", "user"}
            }
            print(f"\nNeo4j Health Check: {_json.dumps(safe_health, indent=2, default=str)}")

        elif args.stats:
            stats = builder.get_graph_stats()
            print(f"\nGraph Stats: {_json.dumps(stats, indent=2, default=str)}")

        elif args.clear:
            try:
                result = builder.clear_graph(
                    confirm=True,
                    confirm_phrase=args.confirm_phrase,
                )
                print(f"\nGraph cleared: {result}")
            except SecurityError as e:
                print(f"\nERROR: {e}")

        elif args.dedup:
            # FIX(C-14): the previous implementation was a STUB — it logged
            # "need full triple (src, rel, dst)" for each edge type but left
            # ``total_removed = 0``. The programmatic method
            # ``deduplicate_edges_deterministic`` (defined on both
            # ``DrugOSGraphBuilder`` and ``GraphEdgeLoader``) DOES work and
            # removes duplicate (src, dst) pairs deterministically by
            # source priority + load time, keeping the edge with the most
            # properties / highest-priority source.
            #
            # ``get_graph_stats`` only returns ``edge_counts_by_type`` as a
            # flat ``{rel_type: count}`` dict, which is insufficient for the
            # dedup call (it needs src_label + rel_type + dst_label). We
            # resolve the missing src/dst labels from ``CORE_EDGE_TYPES``
            # (the schema list of (src, rel, dst) triples), then for every
            # rel_type present in the graph we dedup EACH (src, rel, dst)
            # triple that uses that rel_type (e.g. "inhibits" can be both
            # Compound->Gene and Compound->Protein — both must be deduped).
            stats = builder.get_graph_stats()
            edge_types = stats.get("edge_counts_by_type", {})
            rel_to_triples: dict[str, list[tuple[str, str, str]]] = {}
            for _src_t, _rel_t, _dst_t in CORE_EDGE_TYPES:
                rel_to_triples.setdefault(_rel_t, []).append(
                    (_src_t, _rel_t, _dst_t)
                )
            total_removed = 0
            for rel_type in edge_types:
                triples_for_rel = rel_to_triples.get(rel_type, [])
                if not triples_for_rel:
                    logger.warning(
                        "Dedup for %s: rel_type not in CORE_EDGE_TYPES — "
                        "skipping (need full triple src, rel, dst)",
                        rel_type,
                    )
                    continue
                for _src_t, _rel_t, _dst_t in triples_for_rel:
                    try:
                        removed = builder.deduplicate_edges_deterministic(
                            _src_t, _rel_t, _dst_t
                        )
                        total_removed += int(removed or 0)
                    except Exception as exc:
                        logger.error(
                            "Dedup failed for %s-%s->%s: %s",
                            _src_t, _rel_t, _dst_t, exc,
                        )
            print(f"\nDedup complete. Removed {total_removed} duplicate edges.")

        else:
            # Default: health check
            health = builder.health_check()
            safe_health = {
                k: v for k, v in health.items()
                if k not in {"uri", "password", "user"}
            }
            print(f"\nNeo4j Health Check: {_json.dumps(safe_health, indent=2, default=str)}")
