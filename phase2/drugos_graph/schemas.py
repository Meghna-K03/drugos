"""DrugOS Graph Module — Loader Schemas (TypedDicts)
=====================================================
Authoritative, statically-typed schemas for the records produced and
consumed by the UniProt loader.

Why TypedDict (not Pydantic / dataclasses)?
  * ``TypedDict`` is stdlib (``typing``) — no new dependency (D1-005).
  * It describes the *shape* of a ``dict`` without changing runtime
    behaviour, so the existing dict-based callers
    (``entity_resolver.resolve_proteins_from_uniprot``,
    ``id_crosswalk.load_from_uniprot_records``) continue to work with
    zero modifications (D1-003 / D15-001 interface contract).
  * ``total=False`` lets us declare optional fields without runtime
    validation overhead — the loader's own ``_validate_record`` helper
    (in ``uniprot_loader.py``) performs the runtime checks.

These TypedDicts are the **single source of truth** for the field names
emitted by the loader. Adding a field here is a schema change and MUST be
accompanied by a ``SCHEMA_VERSION`` bump (D14-004).

Fixes: D1-003 (schema contract between parse and to_node_records),
       D15-001 (stable output dict schema),
       D2-001 (alternative_names always list),
       D2-004 / D15-003 (uniprot_id canonical + id backward-compat),
       D4-004 (gene_ids always list),
       D5-003 (ncbi_taxid int),
       D2-005 (cross_references dict),
       D3-007/D3-008/D3-009 (ec_numbers, protein_existence, sequence),
       D16-001 (_provenance), D14-001 (_license, _attribution).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

__all__: list[str] = [
    "UniProtRecord",
    "ProteinNode",
    "UniProtEdge",
    "PROVENANCE_KEYS",
    # DRKG schemas — added by drkg_loader v2.0 audit fix
    # (drkg_loader_repair_prompt.md — Domain 2 Design, BUG 2.4 / GAP 2.5).
    "DRKGRecord",
    "DRKGValidationResult",
    "DRKG_PROVENANCE_KEYS",
    # DrugBank schemas — added by drugbank_parser v2.0 audit fix
    # (drugbank_parser_fix_prompt.md — Domain 2 Design, FIX 2.2).
    "DrugBankRecord",
    "DrugBankNode",
    "DrugBankEdge",
    "DrugInteraction",
    "DRUGBANK_PROVENANCE_KEYS",
    "DRUGBANK_NODE_SCHEMA",
    "DRUGBANK_EDGE_SCHEMA",
    # ChEMBL schemas — added by chembl_loader v2.0 institutional-grade audit fix
    # (chembl_loader — Domain 2 Design).
    "ChEMBLActivityRecord",
    "ChEMBLEdgeRecord",
    "CHEMBL_PROVENANCE_KEYS",
    # STRING schemas — added by string_loader v1.0 institutional-grade audit fix
    # (master_prompt_fix_string_loader.md — Domains 2/4/15/16).
    "StringPPIRecord",
    "StringEdgeProps",
    "StringEdgeRecord",
    "StringLoaderMetrics",
    "StringDeadLetterEntry",
    "StringValidationReport",
    "STRING_PROVENANCE_KEYS",
    # STITCH schemas — added by stitch_loader v1.1.0 institutional-grade audit fix
    # (master_prompt_fix_stitch_loader.md — Domains 2/4/7/15/16).
    "StitchCPIRecord",
    "StitchEdgeProps",
    "StitchEdgeRecord",
    "StitchLoaderMetrics",
    "StitchDeadLetterEntry",
    "StitchValidationReport",
    "STITCH_PROVENANCE_KEYS",
    # SIDER schemas — added by sider_loader v1.0.0 institutional-grade audit fix
    # (master_prompt — Domains 2/4/7/15/16).
    "SiderSideEffectRow",
    "SiderNodeProps",
    "SiderNodeRecord",
    "SiderEdgeProps",
    "SiderEdgeRecord",
    "SiderLegacyEdgeRecord",
    "SiderLoaderMetrics",
    "SiderDeadLetterEntry",
    "SiderValidationReport",
    "SIDER_PROVENANCE_KEYS",
    # OpenTargets schemas — added by opentargets_loader v2.0 institutional-grade
    # audit fix (opentargets_loader_repair_prompt.md — Domains 2/4/7/15/16).
    "OpenTargetsActivityRecord",
    "OpenTargetsEdgeRecord",
    "OpenTargetsNodeRecord",
    "OpenTargetsLoaderMetrics",
    "OpenTargetsDeadLetterEntry",
    "OpenTargetsValidationReport",
    "OPENTARGETS_PROVENANCE_KEYS",
    # ClinicalTrials schemas — added by clinicaltrials_loader v2.1.0
    # institutional-grade audit fix (PROMPT_fix_clinicaltrials_loader.md —
    # Domains 2/4/7/15/16). Mirrors the OpenTargets/SIDER/STITCH schema
    # pattern. Every emitted edge record conforms to
    # ``ClinicalTrialEdgeRecord``; the loader's own ``_validate_edge_record``
    # enforces required fields at runtime.
    "ClinicalTrialTrialRecord",
    "ClinicalTrialEdgeRecord",
    "ClinicalTrialNodeRecord",
    "ClinicalTrialsLoaderMetrics",
    "ClinicalTrialsDeadLetterEntry",
    "ClinicalTrialsValidationReport",
    "CLINICALTRIALS_PROVENANCE_KEYS",
    # GEO schemas — added by geo_loader v1.0.0 institutional-grade audit fix
    # (GEO_LOADER_MASTER_REPAIR_PROMPT.md — 192 findings across 16 domains,
    # Domains 2/4/7/15/16). Mirrors the ClinicalTrials/OpenTargets/SIDER/
    # STITCH/STRING schema pattern. Every emitted raw record conforms to
    # ``GeoRawRecord``; every emitted edge conforms to ``GeoEdgeRecord``;
    # the loader's own ``validate_geo_record`` / ``validate_geo_edge``
    # enforces required fields at runtime.
    "GeoRawRecord",
    "GeoEdgeRecord",
    "GeoLoaderMetrics",
    "GeoDeadLetterEntry",
    "GeoValidationReport",
    "GEO_PROVENANCE_KEYS",
]


class UniProtRecord(TypedDict, total=False):
    """A single parsed UniProtKB/Swiss-Prot entry.

    Produced by ``parse_uniprot_entries`` / ``iter_uniprot_entries``.
    Consumed by ``uniprot_to_node_records`` and
    ``id_crosswalk.load_from_uniprot_records``.

    Field naming rules:
      * Plural nouns (``alternative_names``, ``gene_names``,
        ``gene_ids``, ``ec_numbers``, ``secondary_accessions``) are ALWAYS
        ``list[str]`` — never a polymorphic ``str | list[str]`` (D2-001).
      * Singular nouns (``accession``, ``gene_name``, ``gene_id``,
        ``protein_name``, ``entry_name``) are ``str`` and represent the
        *primary* (first) value of the corresponding plural field, kept for
        backward compatibility with existing consumers.
      * ``ncbi_taxid`` is ``int`` (D5-003). The original string is kept in
        ``_provenance["raw_ncbi_taxid"]`` for audit.
      * ``cross_references`` is ``dict[str, list[str]]`` keyed by database
        name (``"GeneID"``, ``"HGNC"``, ``"ChEMBL"``, …) — fixes D2-005.
      * Every record carries a ``_provenance`` dict (D16-001), a
        ``_source`` tag, a ``_license`` string, and an ``_attribution``
        string (D14-001 — CC BY 4.0 compliance).
    """

    # ── Identity ────────────────────────────────────────────────────────
    accession: str                       # primary UniProt accession (validated)
    secondary_accessions: List[str]      # all other ACs (multi-line AC — D3-004)
    entry_name: str                      # e.g. "PGH1_HUMAN" (ID line)

    # ── Protein description (DE lines) ──────────────────────────────────
    protein_name: str                    # RecName: Full=
    alternative_names: List[str]         # AltName: Full=  (always list — D2-001)
    alternative_name: str                # backward-compat: first alt name or ""
    contains_names: List[str]            # DE Contains: sub-record RecNames (D3-002)
    includes_names: List[str]            # DE Includes: sub-record RecNames (D3-002)
    ec_numbers: List[str]                # EC=1.14.99.1 (D3-007)

    # ── Gene names (GN lines — D3-003) ──────────────────────────────────
    gene_name: str                       # primary Name= (backward-compat)
    gene_names: List[str]                # all Name= values (handles 'and' separator)
    gene_synonyms: List[str]             # Synonyms=
    gene_orf_names: List[str]            # ORFNames=
    gene_locus_names: List[str]          # LocusNames=

    # ── NCBI Gene cross-reference (DR GeneID — D4-004, D3-001) ──────────
    gene_id: str                         # primary, VERIFIED via crosswalk (D3-001)
    gene_ids: List[str]                  # all DR GeneID values (always list)

    # ── Taxonomy (OS / OX lines — D3-005, D5-003) ───────────────────────
    organism: str                        # scientific name from OS line
    ncbi_taxid: int                      # int, cross-checked against OS (D3-005)

    # ── Protein evidence & sequence (PE / SQ lines — D3-008, D3-009) ────
    protein_existence: int               # 1..5
    sequence: str                        # uppercase, whitespace-stripped
    sequence_length: int                 # from SQ header (validated vs sequence)

    # ── All cross-references (DR lines — D2-005) ────────────────────────
    cross_references: Dict[str, List[str]]

    # ── Provenance & compliance (D16-001, D14-001) ──────────────────────
    _provenance: Dict[str, Any]
    _source: str
    _license: str
    _attribution: str


class ProteinNode(TypedDict, total=False):
    """A Protein node ready for the Neo4j knowledge graph.

    Produced by ``uniprot_to_node_records``. The canonical primary key is
    ``uniprot_id`` (matches ``config.CANONICAL_IDS["Protein"]`` — D2-004).
    The legacy ``id`` key is emitted as a backward-compat shim and will be
    removed in schema v3.0.0 (D15-003).

    ``name`` is NEVER empty for a valid record: the fallback chain is
    ``protein_name -> entry_name -> accession`` (D4-002).
    """

    # ── Identity ────────────────────────────────────────────────────────
    uniprot_id: str                      # canonical PK (config.CANONICAL_IDS["Protein"])
    id: str                              # backward-compat alias (deprecated, D15-003)
    uniprot_uri: str                     # identifiers.org URI (FAIR — D14-002)
    name: str                            # protein_name | entry_name | accession
    entry_name: str

    # ── Gene linkage ────────────────────────────────────────────────────
    gene_name: str                       # primary gene symbol
    gene_names: List[str]                # all gene symbols (D3-003)
    gene_id: str                         # primary, VERIFIED (D3-001)
    gene_ids: List[str]                  # all NCBI Gene IDs (D4-004)

    # ── Functional annotation ───────────────────────────────────────────
    ncbi_taxid: int                      # int (D5-003)
    ec_numbers: List[str]                # D3-007
    protein_existence: int               # D3-008
    sequence: str                        # D3-009

    # ── Graph metadata ──────────────────────────────────────────────────
    entity_type: str                     # always "Protein"
    source: str                          # always "UniProt"

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


class UniProtEdge(TypedDict, total=False):
    """An edge derived from a UniProt DR cross-reference (D2-005)."""

    source: str                          # e.g. "uniprot:P23219"
    source_type: str                     # "Protein"
    target: str                          # e.g. "ChEMBL:CHEMBL218"
    target_type: str                     # mapped via _DB_TO_ENTITY_TYPE
    relation: str                        # mapped via _DB_TO_EDGE_TYPE
    source_db: str                       # "UniProt"
    _provenance: Dict[str, Any]


# Keys that every record's _provenance MUST contain (D16-001, D16-005).
# Used by _validate_record in uniprot_loader.py.
PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "organism_filter",
    "organism_match_mode",
    "entry_line_no",
    "byte_range",
)


# =============================================================================
# DRKG schemas — added by drkg_loader v2.0 audit fix
# (drkg_loader_repair_prompt.md — Domain 2 Design, BUG 2.4 / GAP 2.5).
#
# These TypedDicts describe the *shape* of the records emitted by
# ``drugos_graph.drkg_loader.parse_drkg_tsv`` and the result of
# ``validate_drkg``. They are the **single source of truth** for the
# field names: adding a field here is a schema change and MUST be
# accompanied by a ``SCHEMA_VERSION`` bump in ``drkg_loader.py``.
#
# RATIONALE for ``total=False``:
#   DRKG emits a heterogeneous set of triples (Compound-Disease,
#   Compound-Gene, Gene-Disease, …). Not every column makes sense for
#   every triple; for example, ``sensitive`` (rare-disease tag —
#   GAP 9.6) is only meaningful when ``tail_type == "Disease"``. Using
#   ``total=False`` lets us declare all possible columns without
#   forcing every row to populate every field — the loader's own
#   runtime checks (BUG 15.3) enforce the required columns.
# =============================================================================


class DRKGRecord(TypedDict, total=False):
    """A single parsed DRKG triple row (one TSV line).

    Produced by ``drugos_graph.drkg_loader.parse_drkg_tsv`` as one row
    of the returned DataFrame; consumed by ``build_entity_id_maps``,
    ``build_edge_index_maps``, ``build_networkx_graph``, and the three
    subgraph selectors.

    Column naming rules (mirror ``UniProtRecord``):
      * Singular nouns (``head_entity``, ``relation``, ``tail_entity``,
        ``head_type``, ``head_id``, ``tail_type``, ``tail_id``,
        ``relation_name``, ``relation_human_name``, ``relation_source``,
        ``relation_dst_type``, ``evidence_strength``, ``source_confidence``,
        ``head_uri``, ``tail_uri``) are ``str``.
      * ``sensitive`` is ``bool`` and is only set when the tail is a
        rare disease (GAP 9.6 — GDPR/HIPAA-aware tagging).
      * ``_provenance`` is a ``dict[str, Any]`` and MUST contain every
        key in ``DRKG_PROVENANCE_KEYS`` (BUG 16.1).
      * ``_license`` and ``_attribution`` propagate the DRKG MIT
        license + Himmelstein 2020 citation (BUG 14.1).

    Fixes: BUG 2.5, BUG 14.1, BUG 14.2, BUG 14.3, BUG 15.3, BUG 16.1,
           GAP 3.7, GAP 3.8, GAP 9.6, GUARD 3.10.
    """

    # ── Raw TSV columns ───────────────────────────────────────────────
    head_entity: str          # "Compound::DB00107"
    relation: str             # "Hetionet::CtD::Compound:Disease"
    tail_entity: str          # "Disease::DOID:1438"

    # ── Parsed entity components (BUG 4.3) ────────────────────────────
    head_type: str            # "Compound"
    head_id: str              # "DB00107"
    tail_type: str            # "Disease"
    tail_id: str              # "DOID:1438"

    # ── Parsed relation components (BUG 1.5 — uses config.split_drkg_relation)
    relation_source: str      # "Hetionet"  (also: DRUGBANK, GNBR, bioarx, …)
    relation_name: str        # "CtD"       (the abbreviation, NOT a verb)
    relation_dst_type: str    # "Compound:Disease" (the third token, kept for
                              #   cross-check against head_type/tail_type — BUG 3.5)
    relation_human_name: str  # "Compound-treats-Disease"  (GAP 3.7)

    # ── Evidence + source-confidence tagging (GAP 3.8, GUARD 3.10) ────
    evidence_strength: str    # "strong" | "moderate" | "weak" | "unknown"
    # v28 ROOT FIX (P2-L-12): ``source_confidence`` is now NUMERIC (float
    # in [0,1]); the categorical label is preserved as
    # ``source_confidence_label``.
    source_confidence: float  # 1.0 (verified) | 0.8 (curated) |
                              #   0.5 (text_mined) | 0.3 (preprint) | 0.0 (unknown)
    source_confidence_label: str  # "verified" | "curated" | "text_mined" |
                                  #   "preprint" | "unknown"

    # ── Privacy / compliance (GAP 9.6) ───────────────────────────────
    sensitive: bool           # True iff tail is a rare-disease code
                              # (Orphanet / rare DOID)

    # ── FAIR identifiers.org URIs (BUG 14.2) ─────────────────────────
    head_uri: str             # "http://identifiers.org/drugbank:DB00107"
    tail_uri: str             # "http://identifiers.org/doid:DOID:1438"

    # ── Provenance + compliance (BUG 14.1, BUG 16.1) ─────────────────
    _provenance: Dict[str, Any]
    _license: str             # "MIT"
    _attribution: str         # "DRKG (Himmelstein et al., 2020, Sci Data 7:329)"


class DRKGValidationResult(TypedDict, total=False):
    """The result dict returned by ``validate_drkg(df)``.

    Typed so downstream consumers (``run_pipeline.py``, MLflow tracker,
    test suite) can rely on a stable schema. Any new key added here
    requires a ``SCHEMA_VERSION`` bump in ``drkg_loader.py``.

    Fixes: BUG 2.4 — replaces the old untyped ``Dict[str, Any]`` return.
    """

    # ── Counts ────────────────────────────────────────────────────────
    total_triples: int
    total_unique_entities: int
    entity_type_count: int
    relation_type_count: int
    entity_type_breakdown: Dict[str, int]

    # ── Null / malformed checks ──────────────────────────────────────
    null_heads: int
    null_tails: int
    null_relations: int
    malformed_entity_ids: int
    malformed_relation_strings: int

    # ── Data-quality guards (Domain 5) ───────────────────────────────
    expected_record_count: int
    actual_record_count: int
    row_count_within_tolerance: bool
    entity_types_within_tolerance: bool
    relation_types_within_tolerance: bool
    exact_duplicate_triples: int
    cross_source_duplicate_triples: int
    self_loop_triples: int
    unknown_entity_types: List[str]
    biologically_invalid_triples: int
    text_mined_treats_edges_excluded: int

    # ── Lineage / version metadata (Domain 16) ───────────────────────
    parser_version: str
    schema_version: str
    validation_timestamp: str


# Keys that every DRKG DataFrame's ``df.attrs['provenance']`` MUST contain
# (BUG 16.1 — mirrors ``PROVENANCE_KEYS`` for UniProt). Used by
# ``parse_drkg_tsv`` to assert provenance completeness before returning.
DRKG_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "row_count",
)


# =============================================================================
# DrugBank schemas — added by drugbank_parser v2.0 audit fix
# (drugbank_parser_fix_prompt.md — Domain 2 Design, FIX 2.2).
#
# These TypedDicts describe the *shape* of the records emitted by
# ``drugos_graph.drugbank_parser.parse_drugbank_xml`` /
# ``drugos_graph.drugbank_parser.drugbank_to_node_records`` /
# ``drugos_graph.drugbank_parser.drugbank_to_target_edges`` /
# ``drugos_graph.drugbank_parser.drugbank_to_interaction_edges``.
#
# They are the **single source of truth** for the field names: adding a
# field here is a schema change and MUST be accompanied by a
# ``SCHEMA_VERSION`` bump in ``drugbank_parser.py`` (FIX 7.2 / FIX 14.11).
#
# RATIONALE for ``total=False``:
#   DrugBank emits a heterogeneous set of compounds — small molecules,
#   biotech drugs, antibodies, peptides — and not every field makes sense
#   for every drug. For example, ``smiles`` is empty for biotech drugs
#   (FIX 3.6), ``approval_year`` is None for investigational drugs (FIX
#   3.1), and ``withdrawn`` is False for the vast majority of drugs (FIX
#   3.11). Using ``total=False`` lets us declare all possible fields
#   without forcing every record to populate every field — the loader's
#   own runtime checks enforce the required fields.
# =============================================================================


class DrugInteraction(TypedDict, total=False):
    """A drug-drug interaction parsed from DrugBank <drug-interaction>.

    Fixes FIX[(2.13)] — typed shape for the ``interactions`` list field
    on every ``DrugRecord``. Replaces the previous untyped
    ``List[Dict]``.
    """

    drugbank_id: str          # interacting drug's primary DrugBank ID
    name: str                 # interacting drug's name
    description: str          # free-text interaction description
    severity: str             # "contraindicated" | "major" | "moderate" | "unknown"
    orphan_interaction: bool  # True if partner drug not in same XML file (FIX 5.16)


class DrugBankRecord(TypedDict, total=False):
    """A single parsed DrugBank <drug> element.

    Produced by ``parse_drug`` / ``iter_drugbank`` / ``parse_drugbank_xml``.
    Consumed by ``drugbank_to_node_records``, ``drugbank_to_target_edges``,
    and ``drugbank_to_interaction_edges``. The shape mirrors the
    ``DrugRecord`` dataclass (kept for backward compat) but is a plain
    dict for Protocol-level polymorphism.

    Field naming rules (mirror ``UniProtRecord`` and ``DRKGRecord``):
      * Singular nouns (``drugbank_id``, ``name``, ``smiles``,
        ``inchikey``, ``cas_number``, ``indication``, ``toxicity``,
        ``mechanism_of_action``, ``pharmacodynamics``, ``drug_type``,
        ``approval_year``, ``organism_filter``) are ``str`` / ``int``.
      * Plural nouns (``targets``, ``enzymes``, ``carriers``,
        ``transporters``, ``atc_codes``, ``categories``,
        ``external_ids``, ``interactions``, ``atc_hierarchy``) are ALWAYS
        ``list`` — never a polymorphic ``str | list[str]``.
      * Boolean flags (``approved``, ``investigational``, ``withdrawn``,
        ``terminated``, ``illicit``, ``sensitive``, ``_valid``) are
        ``bool``.
      * ``_provenance`` is a ``dict[str, Any]`` and MUST contain every
        key in ``DRUGBANK_PROVENANCE_KEYS`` (FIX 16.1).
      * ``_license`` and ``_attribution`` propagate the DrugBank CC
        BY-NC 4.0 license + Wishart 2024 citation (FIX 14.1).

    Fixes: FIX 2.2, FIX 2.3, FIX 2.4, FIX 2.10, FIX 2.13, FIX 3.6,
           FIX 3.7, FIX 3.10, FIX 3.11, FIX 7.3, FIX 14.1, FIX 16.1.
    """

    # ── Identity ────────────────────────────────────────────────────────
    drugbank_id: str                 # primary DrugBank ID (validated ^DB\d{5,7}$)
    name: str                        # drug name (e.g., "Bimatoprost")
    drug_type: str                   # "small molecule" | "biotech" | ...

    # ── Chemistry ───────────────────────────────────────────────────────
    smiles: str                      # canonical SMILES (RDKit-validated)
    inchikey: str                    # InChIKey (validated ^[A-Z]{14}-[A-Z]{10}-[A-Z]$)
    cas_number: str                  # CAS Registry Number (validated)

    # ── Free-text fields (truncated, with full-text SHA-256) ───────────
    indication: str
    pharmacodynamics: str
    mechanism_of_action: str
    toxicity: str

    # ── Regulatory status ──────────────────────────────────────────────
    approved: bool
    investigational: bool
    withdrawn: bool                  # FIX 3.11 — withdrawn drugs must NOT be recommended
    terminated: bool                 # FIX 3.11 — clinical trial terminated
    illicit: bool                    # FIX 3.11 — illicit market use
    approval_year: int               # FIX 3.1 — for temporal split (training_data)

    # ── Related entities (always list) ─────────────────────────────────
    targets: List[Any]               # List[DrugTarget] — drug targets (FIX 3.2)
    enzymes: List[Any]               # List[DrugTarget] — metabolic enzymes (FIX 3.3)
    carriers: List[Any]              # List[DrugTarget] — plasma protein carriers (FIX 3.3)
    transporters: List[Any]          # List[DrugTarget] — membrane transporters (FIX 3.3)

    # ── Classifications ────────────────────────────────────────────────
    atc_codes: List[Any]             # List[Dict[str, Any]] — ATC hierarchy (FIX 3.7)
    atc_hierarchy: List[Any]         # alias — full hierarchy with level info
    categories: List[str]            # drug categories (FIX 3.10)

    # ── Cross-database identifiers (always dict[str, list[str]]) ──────
    external_ids: Dict[str, Any]     # FIX 5.8 — multi-valued

    # ── Drug-drug interactions (always list[DrugInteraction]) ─────────
    interactions: List[Any]

    # ── Privacy / compliance ───────────────────────────────────────────
    sensitive: bool                  # FIX 9.8 — rare-disease / PII flag

    # ── Provenance + compliance (FIX 14.1, FIX 16.1) ──────────────────
    _provenance: Dict[str, Any]
    _source: str                     # always "drugbank"
    _license: str                    # always "CC BY-NC 4.0 (academic)"
    _attribution: str                # Wishart et al. citation
    _valid: bool                     # FIX 2.4 — post_init validation result
    _canonical_id_source: str        # FIX 2.1 — "inchikey" | "drugbank_id (no inchikey)"


class DrugBankNode(TypedDict, total=False):
    """A Compound node ready for the Neo4j knowledge graph.

    Produced by ``drugbank_to_node_records``. The canonical primary key
    is ``id`` (FIX 2.1) which is ``inchikey`` when available (matches
    ``config.CANONICAL_IDS["Compound"]``), otherwise falls back to
    ``drugbank_id``. The legacy ``drugbank_id`` key is always emitted as
    a separate property for backward compat with ``entity_resolver``.

    Fixes: FIX 2.1, FIX 2.2, FIX 2.9, FIX 3.6, FIX 3.9, FIX 3.10,
           FIX 3.11, FIX 3.12, FIX 3.13, FIX 3.14, FIX 3.15,
           FIX 14.1, FIX 14.2, FIX 14.3, FIX 16.1, FIX G.4, FIX G.5,
           FIX G.8, FIX G.14, FIX G.15.
    """

    # ── Identity (FIX 2.1) ─────────────────────────────────────────────
    id: str                          # canonical PK (inchikey or drugbank_id fallback)
    drugbank_id: str                 # always emitted for entity_resolver backward compat
    _canonical_id_source: str        # "inchikey" | "drugbank_id (no inchikey)"
    drugbank_uri: str                # identifiers.org URI (FAIR — FIX 14.3)

    # ── Chemistry ───────────────────────────────────────────────────────
    name: str
    smiles: str                      # RDKit-validated (FIX 3.6, FIX G.5)
    inchikey: str                    # validated (FIX 3.6)
    cas_number: str                  # validated (FIX 3.6, FIX 3.12)
    drug_type: str                   # "small molecule" | "biotech" | ...

    # ── Free-text fields (truncated, with SHA-256 + truncation flag) ──
    indication: str                  # truncated at sentence boundary (FIX 3.13, FIX G.9)
    indication_truncated: bool
    indication_full_sha256: str
    mechanism_of_action: str
    mechanism_of_action_truncated: bool
    mechanism_of_action_full_sha256: str
    toxicity: str                    # FIX 3.14 — was parsed but not exported
    toxicity_truncated: bool
    toxicity_full_sha256: str
    pharmacodynamics: str            # FIX 3.15 — was parsed but not exported
    pharmacodynamics_truncated: bool
    pharmacodynamics_full_sha256: str

    # ── Classifications ────────────────────────────────────────────────
    atc_codes: str                   # "|"-joined for entity_resolver (FIX 2.5)
    atc_hierarchy: List[Any]         # full hierarchy (FIX 3.7)
    categories: List[str]            # FIX 3.10 — JSON-serializable list

    # ── Regulatory status (FIX 3.11) ───────────────────────────────────
    approved: bool
    investigational: bool
    withdrawn: bool                  # FIX 3.11 / FIX G.4 — patient safety
    terminated: bool
    illicit: bool
    approval_year: int               # FIX 3.1 — for temporal split

    # ── Cross-references (FIX 3.16) ────────────────────────────────────
    pubchem_cid: str                 # backward-compat: first CID
    pubchem_cids: List[str]          # FIX 5.8 — all CIDs (stereoisomers)
    chembl_id: str
    chebi_id: str
    pubchem_uri: str                 # FAIR identifiers.org URI

    # ── Privacy / compliance ───────────────────────────────────────────
    sensitive: bool                  # FIX 9.8 — rare-disease flag

    # ── Graph metadata ─────────────────────────────────────────────────
    entity_type: str                 # always "Compound"
    target_uniprot_ids: List[str]    # FIX G.6 — for downstream validation

    # ── Provenance + compliance (FIX 14.1, FIX 16.1) ──────────────────
    _provenance: Dict[str, Any]
    _source: str                     # always "drugbank" (FIX 16.16)
    _license: str                    # always "CC BY-NC 4.0 (academic)" (FIX 14.1, FIX G.15)
    _attribution: str                # Wishart et al. citation (FIX 14.2)
    _commercial_use_allowed: bool    # always False (FIX 14.1, FIX G.15)
    _last_modified: str              # ISO-8601 timestamp (FIX 7.6)
    _schema_version: str             # FIX 15.10


class DrugBankEdge(TypedDict, total=False):
    """A drug-target, drug-enzyme, drug-carrier, drug-transporter, or
    drug-drug-interaction edge derived from DrugBank.

    Produced by ``drugbank_to_target_edges`` (drug-target) and
    ``drugbank_to_interaction_edges`` (drug-drug). The ``relation``
    field is one of the canonical relations in
    ``config.CORE_EDGE_TYPES_SET`` (FIX 3.3, FIX 3.4, FIX 2.7).

    Fixes: FIX 2.2, FIX 2.8, FIX 2.9, FIX 3.2, FIX 3.3, FIX 3.4,
           FIX 3.5, FIX 3.18, FIX 3.19, FIX 3.20, FIX 7.9, FIX 14.1,
           FIX 16.1, FIX 16.15, FIX G.3, FIX G.6, FIX G.7.
    """

    # ── Endpoints ──────────────────────────────────────────────────────
    drug_id: str                     # source drug's DrugBank ID
    target_uniprot_id: str           # target's UniProt accession (validated FIX 3.5)
    drug_a_id: str                   # for drug-drug interactions only
    drug_b_id: str                   # for drug-drug interactions only

    # ── Edge semantics (FIX 3.3, FIX 3.4) ─────────────────────────────
    relation: str                    # canonical relation (in CORE_EDGE_TYPES_SET)
    action: str                      # raw DrugBank action string (for audit)
    section: str                     # "targets" | "enzymes" | "carriers" | "transporters"

    # ── Target metadata (FIX 3.2, FIX 3.5, FIX 3.18) ──────────────────
    gene_name: str
    gene_name_confidence: str        # "high" | "low" (FIX 3.8)
    organism: str                    # scientific name
    ncbi_taxid: int                  # NCBI TaxID
    non_human: bool                  # FIX 3.2 — True if organism != human
    unknown_target: bool             # FIX 3.19 — no <polypeptide> element
    uniprot_id_source: str           # "Swiss-Prot" | "TrEMBL" | "" (FIX 3.5, FIX 3.18)
    polypeptide_source: str          # alias for uniprot_id_source

    # ── Confidence / evidence (FIX 2.8) ────────────────────────────────
    confidence: float                # 0.0–1.0
    evidence_strength: str           # "curated" | "unreviewed" | "low"

    # ── Dedup / idempotency (FIX 3.20, FIX 7.9, FIX G.7) ──────────────
    dedup_hash: str                  # SHA-256(drug_id|uniprot_id|relation|action)[:16]

    # ── Drug-drug interaction metadata (FIX 3.9) ───────────────────────
    description: str                 # interaction description
    severity: str                    # "contraindicated" | "major" | "moderate" | "unknown"
    orphan_interaction: bool         # FIX 5.16 — partner drug not in same XML

    # ── FAIR URIs (FIX 14.3) ───────────────────────────────────────────
    head_uri: str                    # identifiers.org URI for source
    tail_uri: str                    # identifiers.org URI for target

    # ── Provenance + compliance (FIX 14.1, FIX 16.1, FIX 16.15) ───────
    _provenance: Dict[str, Any]
    _source: str                     # always "drugbank"
    _license: str                    # always "CC BY-NC 4.0 (academic)"
    _attribution: str                # Wishart et al. citation
    _commercial_use_allowed: bool    # always False


# Keys that every DrugBank record's ``_provenance`` MUST contain
# (FIX 16.1 — mirrors ``PROVENANCE_KEYS`` for UniProt and
# ``DRKG_PROVENANCE_KEYS`` for DRKG). Used by ``_validate_record`` in
# ``drugbank_parser`` to assert provenance completeness before returning.
DRUGBANK_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "organism_filter",
    "organism_match_mode",
    "entry_line_no",
    "byte_range",
)


# Runtime schema (field name -> Python type) for ``DrugBankNode`` records.
# Used by ``drugbank_parser._validate_record`` for runtime schema
# validation. Mirrors ``PROTEIN_NODE_SCHEMA`` in ``uniprot_loader.py``.
DRUGBANK_NODE_SCHEMA: Dict[str, type] = {
    "id": str,
    "drugbank_id": str,
    "_canonical_id_source": str,
    "drugbank_uri": str,
    "name": str,
    "smiles": str,
    "inchikey": str,
    "cas_number": str,
    "drug_type": str,
    "indication": str,
    "indication_truncated": bool,
    "indication_full_sha256": str,
    "mechanism_of_action": str,
    "mechanism_of_action_truncated": bool,
    "mechanism_of_action_full_sha256": str,
    "toxicity": str,
    "toxicity_truncated": bool,
    "toxicity_full_sha256": str,
    "pharmacodynamics": str,
    "pharmacodynamics_truncated": bool,
    "pharmacodynamics_full_sha256": str,
    "atc_codes": str,
    "atc_hierarchy": list,
    "categories": list,
    "approved": bool,
    "investigational": bool,
    "withdrawn": bool,
    "terminated": bool,
    "illicit": bool,
    "approval_year": int,
    "pubchem_cid": str,
    "pubchem_cids": list,
    "chembl_id": str,
    "chebi_id": str,
    "pubchem_uri": str,
    "sensitive": bool,
    "entity_type": str,
    "target_uniprot_ids": list,
    "_provenance": dict,
    "_source": str,
    "_license": str,
    "_attribution": str,
    "_commercial_use_allowed": bool,
    "_last_modified": str,
    "_schema_version": str,
}


# Runtime schema for ``DrugBankEdge`` records.
DRUGBANK_EDGE_SCHEMA: Dict[str, type] = {
    "drug_id": str,
    "target_uniprot_id": str,
    "relation": str,
    "action": str,
    "section": str,
    "gene_name": str,
    "gene_name_confidence": str,
    "organism": str,
    "ncbi_taxid": int,
    "non_human": bool,
    "unknown_target": bool,
    "uniprot_id_source": str,
    "polypeptide_source": str,
    "confidence": float,
    "evidence_strength": str,
    "dedup_hash": str,
    "head_uri": str,
    "tail_uri": str,
    "_provenance": dict,
    "_source": str,
    "_license": str,
    "_attribution": str,
    "_commercial_use_allowed": bool,
}


# =============================================================================
# ChEMBL schemas — added by chembl_loader v2.0 institutional-grade audit fix
# (chembl_loader — Domain 2 Design).
#
# These TypedDicts describe the *shape* of the records emitted by
# ``drugos_graph.chembl_loader.parse_chembl_activities`` and the edge
# records from ``chembl_to_edge_records``. They are the **single source of
# truth** for the field names: adding a field here is a schema change and
# MUST be accompanied by a ``SCHEMA_VERSION`` bump in ``chembl_loader.py``.
#
# RATIONALE for ``total=False``:
#   ChEMBL activities are heterogeneous — some have SMILES, some don't;
#   some have UniProt accessions via target_components, some don't. Using
#   ``total=False`` lets us declare all possible columns without forcing
#   every row to populate every field — the loader's own runtime checks
#   enforce the required columns.
# =============================================================================


class ChEMBLActivityRecord(TypedDict, total=False):
    """A single ChEMBL bioactivity record from the SQLite database.

    This is the raw output of the SQL query in
    ``chembl_loader.parse_chembl_activities``. Each row represents one
    measured drug-target interaction with associated metadata.

    Required fields (enforced by the loader at runtime):
        drug_chembl_id, target_chembl_id, pchembl_value, standard_type,
        target_type

    Optional fields (may be None/absent depending on ChEMBL data):
        smiles, uniprot_accession, component_description, target_name,
        standard_value, standard_units, assay_type, confidence_score,
        organism, tax_id
    """

    # ── Compound identification ─────────────────────────────────────────
    drug_chembl_id: str              # e.g. "CHEMBL25"
    smiles: str                      # canonical SMILES

    # ── Target identification ───────────────────────────────────────────
    target_chembl_id: str            # e.g. "CHEMBL218"
    target_name: str                 # e.g. "Cyclooxygenase-1"
    target_type: str                 # e.g. "SINGLE PROTEIN"
    uniprot_accession: str           # e.g. "P23219" — from target_components
    component_description: str       # target component description

    # ── Activity measurement ────────────────────────────────────────────
    pchembl_value: float             # -log10(activity in M), e.g. 7.52
    standard_type: str               # e.g. "IC50", "Ki", "EC50"
    standard_value: float            # activity value in standard_units
    standard_units: str              # e.g. "nM"

    # ── Assay metadata ──────────────────────────────────────────────────
    assay_type: str                  # "B" (Binding) or "F" (Functional)
    confidence_score: int            # 0-9, target assignment confidence

    # ── Organism filter ─────────────────────────────────────────────────
    organism: str                    # e.g. "Homo sapiens"
    tax_id: int                      # NCBI taxonomy ID, e.g. 9606

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


class ChEMBLEdgeRecord(TypedDict, total=False):
    """An edge record for the knowledge graph, derived from a ChEMBL activity.

    Each record represents a Compound→Protein relationship with properties
    including the activity measurement, relation type, and resolution path.

    Required fields (enforced by the loader at runtime):
        src_id, dst_id, src_type, dst_type, rel_type
    """

    # ── Edge endpoints ──────────────────────────────────────────────────
    src_id: str                      # drug_chembl_id, e.g. "CHEMBL25"
    dst_id: str                      # uniprot_accession, e.g. "P23219"
    src_type: str                    # always "Compound"
    dst_type: str                    # always "Protein"
    rel_type: str                    # "inhibits", "activates", "binds", "modulates"

    # ── Edge properties ─────────────────────────────────────────────────
    props: Dict[str, Any]            # metadata dict (see below)

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


# Keys that every ChEMBL edge record's _provenance MUST contain
# (Domain 16 Lineage, Domain 7 Idempotency).
CHEMBL_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "chembl_version",
    "min_pchembl",
    "organism_filter",
    "resolution_method",
    "row_count_in",
    "row_count_out",
    "crosswalk_version",
)


# =============================================================================
# STRING schemas — added by string_loader v1.0 institutional-grade audit fix
# (master_prompt_fix_string_loader.md — Sections 2, 4, 7, 15, 16).
#
# These TypedDicts and the STRING_PROVENANCE_KEYS tuple mirror the
# ChEMBL pattern. They are the **single source of truth** for the field
# names emitted by the STRING loader; adding/removing a field here is a
# SCHEMA_VERSION bump (Domain 14 Compliance, Domain 15 Interoperability).
#
# Fixes: D2-03 (TypedDict return type), D2-04 (schemas.py home),
#        I15-01 (_schema_version field), I15-04 (standard provenance keys),
#        L16-02 (_provenance field on every edge).
# =============================================================================


class StringPPIRecord(TypedDict, total=False):
    """A single parsed row from STRING ``protein.links.full.v12.0.txt.gz``.

    STRING v12.0 file format (whitespace-separated, header begins with ``#``):

        #string_protein_id_1\tstring_protein_id_2\tneighborhood\tfusion\t
        #cooccurrence\tcoexpression\texperimental\tdatabase\ttextmining\t
        #combined_score
        9606.ENSP00000000233 9606.ENSP00000000412    0   0   0   119 0   0   103 124

    All score columns are integers in ``[0, 1000]``.
    """

    # ── Endpoints ─────────────────────────────────────────────────────
    protein1: str                 # e.g. "9606.ENSP00000000233"
    protein2: str                 # e.g. "9606.ENSP00000000412"

    # ── 8 evidence channel scores ─────────────────────────────────────
    neighborhood: int             # gene neighborhood
    fusion: int                   # gene fusion
    cooccurrence: int             # genome co-occurrence
    coexpression: int             # co-expression
    experimental: int             # experimental evidence
    database: int                 # curated pathway databases
    textmining: int               # literature text-mining
    combined_score: int           # weighted geometric mean of the above

    # ── Provenance & compliance ───────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


class StringEdgeProps(TypedDict, total=False):
    """The ``props`` sub-dict on every STRING Protein→Protein edge record.

    Backward-compatibility contract (master prompt Rule R3): the original
    six keys — ``source, combined_score, src_id_resolved, dst_id_resolved,
    src_ensembl_original, dst_ensembl_original`` — MUST remain a subset of
    this dict. Bump SCHEMA_VERSION if any of those six keys is removed or
    renamed (master prompt Rule R6).
    """

    # ── Legacy keys (preserved verbatim from v0 — Rule R2/R3) ─────────
    source: str                                # "STRING" (legacy alias of _source)
    combined_score: Optional[int]              # None for missing; never 0-sentinel
    src_id_resolved: bool                      # True if src translated to UniProt AC
    dst_id_resolved: bool                      # True if dst translated to UniProt AC
    src_ensembl_original: str                  # original Ensembl ID ("" if resolved)
    dst_ensembl_original: str                  # original Ensembl ID ("" if resolved)

    # ── Standard provenance keys (master prompt I15-04 / C14-01) ──────
    _source: str                               # "STRING"
    _license: str                              # "CC BY 4.0"
    _attribution: str                          # Szklarczyk et al. citation
    _schema_version: str                       # SCHEMA_VERSION constant

    # ── Evidence channels (master prompt S3-05) ───────────────────────
    evidence_channels: List[str]               # e.g. ["experimental","database"]
    channel_scores: Dict[str, int]             # {"experimental": 800, ...}

    # ── ID resolution metadata (master prompt S3-07 / S3-08) ──────────
    id_resolved: bool                          # src_id_resolved AND dst_id_resolved
    src_all_mappings: List[str]                # all UniProt ACs for src ENSP
    dst_all_mappings: List[str]                # all UniProt ACs for dst ENSP
    is_isoform_src: bool                       # True if src ENSP has ".N" suffix
    is_isoform_dst: bool                       # True if dst ENSP has ".N" suffix

    # ── Organism + deterministic ordering (S3-03 / I7-05) ─────────────
    organism_taxid: int                        # NCBI taxid (default 9606)
    directed: bool                             # False (STRING PPIs are undirected)

    # ── Source version + lineage (I7-06 / I7-07 / I7-09) ──────────────
    source_version: str                        # cfg["version"], e.g. "12.0"
    crosswalk_version: str                     # BUILTIN_TABLE_VERSION
    load_id: str                               # process-cached UUID for rollback

    # ── Per-edge provenance (D2-06 / L16-02) ──────────────────────────
    _provenance: Dict[str, Any]                # see STRING_PROVENANCE_KEYS


class StringEdgeRecord(TypedDict, total=False):
    """An edge record for the knowledge graph, derived from a STRING PPI.

    Each record represents a Protein→Protein ``interacts_with`` relationship.
    The endpoints are UniProt accessions (e.g. "P23219") when the crosswalk
    resolved the Ensembl protein ID; otherwise the original Ensembl ID is
    retained and ``props['id_resolved']`` is False (master prompt S3-08).

    Required top-level fields (enforced by the loader at runtime):
        src_id, dst_id, src_type, dst_type, rel_type
    """

    # ── Edge endpoints ──────────────────────────────────────────────────
    src_id: str                      # UniProt AC or original Ensembl ID
    dst_id: str                      # UniProt AC or original Ensembl ID
    src_type: str                    # always "Protein" (CANONICAL_NODE_TYPES)
    dst_type: str                    # always "Protein"
    rel_type: str                    # always "interacts_with"

    # ── Edge properties ─────────────────────────────────────────────────
    props: Dict[str, Any]            # see StringEdgeProps

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


class StringLoaderMetrics(TypedDict, total=False):
    """Pipeline-run metrics for the STRING loader (Domain 11 Observability).

    The :func:`drugos_graph.string_loader.load_string` facade populates
    every field and returns the dict in its result. The
    :class:`drugos_graph.string_loader.StringLoaderMetrics` dataclass in
    ``string_loader.py`` is the typed runtime container; this TypedDict is
    the static-type contract for the dict form.
    """

    rows_in: int                                # raw rows from file
    rows_after_score_filter: int                # after filter_by_score
    rows_after_organism_filter: int             # after _filter_organism
    rows_after_self_loop_filter: int            # after _drop_self_loops
    rows_after_dedup: int                       # after _drop_duplicates
    edges_created: int                          # final edge count
    edges_resolved: int                         # both endpoints UniProt
    edges_unresolved: int                       # at least one endpoint Ensembl
    edges_dropped_unresolved: int               # dropped by unresolved_policy
    duplicate_edges: int                        # removed by _drop_duplicates
    self_loops: int                             # removed by _drop_self_loops
    non_human_edges: int                        # removed by _filter_organism
    out_of_range_scores: int                    # removed by _validate_score_range
    dlq_entries: int                            # written to dead-letter queue
    parse_time_seconds: float
    resolve_time_seconds: float
    edge_build_time_seconds: float
    neo4j_load_time_seconds: float
    peak_memory_mb: float
    errors: List[str]                           # non-fatal error summaries


class StringDeadLetterEntry(TypedDict, total=False):
    """One entry in the STRING dead-letter queue (JSONL).

    Written by :func:`drugos_graph.string_loader._write_to_dlq` whenever a
    row is dropped due to malformed data, missing score, non-target organism,
    bad ID format, or unresolved translation. The DLQ file lives at
    ``data/dead_letter/string_malformed.jsonl``.
    """

    timestamp: str                              # ISO-8601 UTC
    row_index: Optional[int]                    # original df row index (or None)
    reason: str                                 # short machine-readable reason code
    raw_values: Dict[str, Any]                  # the offending row's values
    parser_version: str                         # PARSER_VERSION constant
    schema_version: str                         # SCHEMA_VERSION constant
    stage: str                                  # pipeline stage that emitted the entry
    load_id: str                                # process-cached UUID for rollback


class StringValidationReport(TypedDict, total=False):
    """Result of :func:`drugos_graph.string_loader.validate_string`.

    Returns a structured report rather than raising, so callers can decide
    which validation failures are fatal in their context (master prompt
    Section 11, Domain 5 Data Quality).
    """

    total_rows: int
    null_protein1: int
    null_protein2: int
    null_combined_score: int
    score_min: Optional[int]
    score_max: Optional[int]
    score_mean: Optional[float]
    score_p50: Optional[float]
    non_human_rows: int
    duplicate_rows: int
    self_loops: int
    out_of_range_scores: int
    malformed_ensembl_ids: int
    columns_present: List[str]
    columns_missing: List[str]
    columns_unexpected: List[str]
    schema_version: str


# Keys that every STRING edge record's _provenance MUST contain
# (master prompt I7-03 / D2-06 / L16-01).
#
# Shape mirrors CHEMBL_PROVENANCE_KEYS but with STRING-specific extras:
#   * ``string_version``        — STRING release (e.g. "12.0")
#   * ``score_threshold``       — the combined_score cutoff applied
#   * ``organism_filter``       — the taxid (e.g. 9606)
#   * ``resolution_method``     — "crosswalk_with_provenance"
#   * ``row_count_in / out``    — counts before/after filtering
#   * ``crosswalk_version``     — BUILTIN_TABLE_VERSION
#   * ``load_id``               — process-cached UUID for rollback
STRING_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "string_version",
    "score_threshold",
    "organism_filter",
    "resolution_method",
    "row_count_in",
    "row_count_out",
    "crosswalk_version",
    "load_id",
    "input_sha256",
    "output_sha256",
)


# =============================================================================
# STITCH schemas — added by stitch_loader v1.1.0 institutional-grade audit fix
# (master_prompt_fix_stitch_loader.md — Domains 2, 4, 7, 15, 16).
#
# These TypedDicts and the STITCH_PROVENANCE_KEYS tuple mirror the STRING
# pattern. They are the single source of truth for the field names emitted
# by the STITCH loader; adding/removing a field here is a SCHEMA_VERSION bump.
#
# Fixes: BUG-2.2 (TypedDicts for edge records), BUG-7.3 (provenance dict),
#        BUG-7.4 (load_id), BUG-14.1 (license/attribution), BUG-14.2 (schema
#        version), BUG-15.1 (kg_builder contract), BUG-16.1 (input hash),
#        BUG-16.4 (provenance metadata), GAP-16.4 (provenance keys).
# =============================================================================


class StitchCPIRecord(TypedDict, total=False):
    """A single parsed row from STITCH ``protein_chemical.links.detailed.v5.0.tsv.gz``.

    STITCH v5.0 detailed file format (tab-separated, header begins with ``#``):

        #chemical_action\tprotein\texperimental\tdatabase\ttextmining\t
        #cooccurrence\tcoexpression\tprediction\tcombined_score
        CIDm00002244\t9606.ENSP00000358091\tinhibition\t800\t900\t0\t0\t0\t0\t900

    All score columns are integers in ``[0, 1000]``.

    Stereo-chemistry note (BUG-3.1):
      * ``CIDm`` prefix  -> stereo-specific form (e.g. S-warfarin, 5x potent)
      * ``CIDs`` prefix  -> racemic mixture (e.g. commercial warfarin)
      * The bare CID (e.g. "00002244") is preserved in ``chemical_cid`` but
        MUST NOT be used as the sole identifier — ``stitch_chemical_id``
        preserves the full ``CIDm00002244`` form.
    """

    # ── Endpoints ─────────────────────────────────────────────────────
    chemical: str                 # e.g. "CIDm00002244" or "CIDs00002244"
    protein: str                  # e.g. "9606.ENSP00000358091"

    # ── Action (optional in some STITCH releases) ─────────────────────
    action: str                   # e.g. "inhibition", "activation" (may be "")

    # ── 6 per-channel evidence scores + combined_score (7th) ─────────
    experimental: int             # wet-lab evidence
    database: int                 # curated database evidence
    textmining: int               # literature text-mining (weakest)
    cooccurrence: int             # abstract co-occurrence
    coexpression: int             # mRNA co-expression
    prediction: int               # predicted interaction
    combined_score: int           # weighted aggregate (the canonical filter key)

    # ── Provenance & compliance ───────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


class StitchEdgeProps(TypedDict, total=False):
    """The ``props`` sub-dict on every STITCH Compound->Protein edge record.

    Backward-compatibility contract (master prompt Rule R3): the original
    five keys -- ``source, score, action, protein_id_resolved,
    protein_ensembl_original`` -- MUST remain a subset of this dict. Bump
    SCHEMA_VERSION if any of those five keys is removed or renamed (Rule R6).
    """

    # ── Legacy keys (preserved verbatim from v0 — Rule R2/R3) ─────────
    source: str                                # "STITCH" (legacy alias of _source)
    score: Optional[int]                       # combined_score; None for missing
    action: str                                # raw action string (may be "")
    protein_id_resolved: bool                  # True if protein translated to UniProt AC
    protein_ensembl_original: str              # original Ensembl ID ("" if resolved)

    # ── Standard provenance keys (master prompt BUG-15.1 / C14-01) ────
    _source: str                               # "STITCH"
    _license: str                              # "CC0 1.0"
    _attribution: str                          # Kuhn et al. citation
    _schema_version: str                       # SCHEMA_VERSION constant
    _parser_version: str                       # PARSER_VERSION constant

    # ── STITCH-specific metadata (nested under props['_stitch']) ──────
    # BUG-15.1: STITCH-specific metadata is nested to keep top-level
    # props compliant with the kg_builder.load_edges_bulk_create contract.
    _stitch: Dict[str, Any]

    # ── Organism + deterministic ordering (BUG-3.4 / I7-05) ───────────
    organism_taxid: int                        # NCBI taxid (default 9606)
    directed: bool                             # True for STITCH (Compound->Protein)

    # ── Source version + lineage (BUG-7.1 / BUG-7.3 / GAP-7.4) ────────
    source_version: str                        # cfg["version"], e.g. "5.0"
    crosswalk_version: str                     # BUILTIN_TABLE_VERSION
    load_id: str                               # process-cached UUID for rollback
    parsed_at: str                             # ISO-8601 UTC timestamp

    # ── Per-edge provenance (BUG-7.3 / BUG-16.4 / L16-02) ─────────────
    _provenance: Dict[str, Any]                # see STITCH_PROVENANCE_KEYS


class StitchEdgeRecord(TypedDict, total=False):
    """An edge record for the knowledge graph, derived from a STITCH CPI.

    Each record represents a Compound->Protein relationship (``binds``,
    ``inhibits``, ``activates``, or one of the formal action map values).
    The Compound endpoint is a PubChem CID (e.g. "2244"); the Protein endpoint
    is a UniProt accession (e.g. "P23219") when the crosswalk resolved the
    Ensembl protein ID, otherwise the original Ensembl ID is retained and
    ``props['protein_id_resolved']`` is False (master prompt BUG-2.3).

    Required top-level fields (enforced by the loader at runtime):
        src_id, dst_id, src_type, dst_type, rel_type, props
    """

    # ── Edge endpoints ──────────────────────────────────────────────────
    src_id: str                      # PubChem CID as string (e.g. "2244")
    dst_id: str                      # UniProt AC or original Ensembl ID
    src_type: str                    # always "Compound" (CANONICAL_NODE_TYPES)
    dst_type: str                    # always "Protein"
    rel_type: str                    # "binds" / "inhibits" / "activates" / etc.

    # ── Edge properties ─────────────────────────────────────────────────
    props: Dict[str, Any]            # see StitchEdgeProps

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str
    _schema_version: str


class StitchLoaderMetrics(TypedDict, total=False):
    """Pipeline-run metrics for the STITCH loader (Domain 11 Observability).

    The :func:`drugos_graph.stitch_loader.load_stitch` facade populates
    every field and returns the dict in its result. The
    :class:`drugos_graph.stitch_loader.StitchLoaderMetrics` dataclass in
    ``stitch_loader.py`` is the typed runtime container; this TypedDict is
    the static-type contract for the dict form.
    """

    rows_in: int                                # raw rows from file
    rows_after_score_filter: int                # after filter_by_score
    rows_after_organism_filter: int             # after _filter_organism
    rows_after_dedup: int                       # after _dedup_edges
    edges_created: int                          # final edge count
    edges_resolved: int                         # protein endpoint UniProt-resolved
    edges_unresolved: int                       # protein endpoint still Ensembl
    edges_dropped_unresolved: int               # dropped by unresolved_policy
    duplicate_edges: int                        # removed by _dedup_edges
    non_human_edges: int                        # removed by _filter_organism
    out_of_range_scores: int                    # removed by _validate_score_range
    dlq_entries: int                            # written to dead-letter queue
    parse_time_seconds: float
    resolve_time_seconds: float
    edge_build_time_seconds: float
    neo4j_load_time_seconds: float
    peak_memory_mb: float
    errors: List[str]                           # non-fatal error summaries


class StitchDeadLetterEntry(TypedDict, total=False):
    """One entry in the STITCH dead-letter queue (JSONL).

    Written by :func:`drugos_graph.stitch_loader._write_to_dlq` whenever a
    row is dropped due to malformed data, missing score, non-target organism,
    bad ID format, or unresolved translation. The DLQ file lives at
    ``data/dead_letter/stitch_malformed.jsonl``.
    """

    timestamp: str                              # ISO-8601 UTC
    row_index: Optional[int]                    # original df row index (or None)
    reason: str                                 # short machine-readable reason code
    raw_values: Dict[str, Any]                  # the offending row's values
    parser_version: str                         # PARSER_VERSION constant
    schema_version: str                         # SCHEMA_VERSION constant
    stage: str                                  # pipeline stage that emitted the entry
    load_id: str                                # process-cached UUID for rollback


class StitchValidationReport(TypedDict, total=False):
    """Result of :func:`drugos_graph.stitch_loader.validate_stitch`.

    Returns a structured report rather than raising, so callers can decide
    which validation failures are fatal in their context (master prompt
    Section 3, Domain 5 Data Quality, BUG-5.1 through GAP-5.6).
    """

    total_rows: int
    null_chemical: int
    null_protein: int
    null_combined_score: int
    score_min: Optional[int]
    score_max: Optional[int]
    score_mean: Optional[float]
    score_p50: Optional[float]
    non_human_rows: int
    duplicate_rows: int
    out_of_range_scores: int
    malformed_chemical_ids: int
    malformed_protein_ids: int
    columns_present: List[str]
    columns_missing: List[str]
    columns_unexpected: List[str]
    schema_version: str


# Keys that every STITCH edge record's _provenance MUST contain
# (master prompt BUG-7.3 / BUG-16.4 / GAP-16.4 / L16-01).
#
# Shape mirrors STRING_PROVENANCE_KEYS but with STITCH-specific extras:
#   * ``stitch_version``        — STITCH release (e.g. "5.0")
#   * ``score_threshold``       — the combined_score cutoff applied
#   * ``organism_filter``       — the taxid (e.g. 9606)
#   * ``resolution_method``     — "crosswalk_with_provenance"
#   * ``row_count_in / out``    — counts before/after filtering
#   * ``crosswalk_version``     — BUILTIN_TABLE_VERSION
#   * ``load_id``               — process-cached UUID for rollback
#   * ``input_sha256``          — SHA-256 of source file (alias of source_sha256)
#   * ``output_sha256``         — SHA-256 of sorted edges (idempotency check)
STITCH_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "stitch_version",
    "score_threshold",
    "organism_filter",
    "resolution_method",
    "row_count_in",
    "row_count_out",
    "crosswalk_version",
    "load_id",
    "input_sha256",
    "output_sha256",
)


# =============================================================================
# SIDER schemas — added by sider_loader v1.0.0 institutional-grade audit fix
# (master_prompt — Domains 2/4/7/15/16).
#
# These TypedDicts document the contract of every SIDER output record. The
# loader emits dicts that match these TypedDicts at runtime; static type
# checkers (mypy/pyright) can verify the contract at development time.
#
# Backward-compatibility contract (master prompt Rule R3): the original
# five fields — ``src_id, dst_id, src_type, dst_type, rel_type, props`` —
# MUST remain a subset of every edge record. Bump SIDER_SCHEMA_VERSION if
# any of those five fields is removed or renamed (Rule R6).
# =============================================================================


class SiderSideEffectRow(TypedDict, total=False):
    """A single parsed row from SIDER ``meddra_all_se.tsv.gz``.

    SIDER v2023 file format (tab-separated, no header — SIDER ships a
    header-less file):

        CIDm00002244\tCIDs00002244\tC0018790\tPT\tC0018790\tRash

    Six columns, fixed order:

    ======  =======================  =============  ====================================
    Col     Name                     Dtype          Description
    ======  =======================  =============  ====================================
    1       stitch_id_flat           string         CIDm-prefixed PubChem CID (racemic)
    2       stitch_id_stereo         string         CIDs-prefixed PubChem CID (stereo)
    3       umls_id_label            string         UMLS CUI of the side-effect label
    4       meddra_type              string         One of {PT, LLT, HLT, HLGT, SOC}
    5       umls_id_meddra           string         UMLS CUI of the MedDRA term (canonical)
    6       side_effect_name         string         Human-readable side-effect name
    ======  =======================  =============  ====================================

    Plus derived columns added by the parser:
      * ``pubchem_cid``       — Int64, the integer PubChem CID (CIDm/CIDs
                                prefix stripped, leading zeros stripped).
                                Cross-loader canonical Compound ID.
      * ``stereochemistry``   — "flat" / "stereo" / "both" (D3.2).
      * ``stitch_id_raw``     — the original (CIDm/CIDs) string.
      * ``_source_row``       — 1-indexed line number in the source TSV.
      * ``_provenance``       — see SIDER_PROVENANCE_KEYS.
    """

    stitch_id_flat: str          # e.g. "CIDm00002244"
    stitch_id_stereo: str        # e.g. "CIDs00002244"
    umls_id_label: str           # UMLS CUI of the side-effect label
    meddra_type: str             # PT / LLT / HLT / HLGT / SOC
    umls_id_meddra: str          # UMLS CUI of the MedDRA term
    side_effect_name: str        # human-readable name (sanitized)

    # Derived columns (added by parser)
    pubchem_cid: int             # canonical int Compound ID
    stereochemistry: str         # "flat" / "stereo" / "both"
    stitch_id_raw: str           # original CIDm/CIDs string
    drug_cid: str                # DEPRECATED — zero-padded str alias of pubchem_cid

    # Provenance & compliance
    _source_row: int
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str


class SiderNodeProps(TypedDict, total=False):
    """The ``props`` sub-dict on every SIDER MedDRA_Term node record."""

    source: str                                # "SIDER"
    meddra_type: str                           # PT / LLT / HLT / HLGT / SOC
    umls_id_label: Optional[str]               # may differ from umls_id_meddra
    side_effect_name: str                      # sanitized name
    meddra_version: str                        # e.g. "26.0"
    source_version: str                        # SIDER release, e.g. "2023-10-25"

    # Standard provenance keys (BUG-14.1, BUG-14.2)
    _source: str                               # "SIDER"
    _license: str                              # "CC0 1.0"
    _attribution: str                          # Kuhn et al. citation
    _schema_version: str                       # SIDER_SCHEMA_VERSION
    _parser_version: str                       # SIDER_PARSER_VERSION


class SiderNodeRecord(TypedDict, total=False):
    """A node record for the knowledge graph, derived from a SIDER side effect.

    Each record represents a MedDRA_Term node — the canonical adverse-event
    vocabulary term per ``config.CORE_NODE_TYPES``.

    Required top-level fields (enforced by the loader at runtime):
        id, name, entity_type, source, props
    """

    id: str                          # "MedDRA:C0018790" (prefixed, per D15.2)
    name: str                        # sanitized side_effect_name
    entity_type: str                 # "MedDRA_Term" (canonical, per Phase 0.3)
    source: str                      # "SIDER"
    props: Dict[str, Any]            # see SiderNodeProps

    # Provenance & compliance
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str
    _schema_version: str


class SiderEdgeProps(TypedDict, total=False):
    """The ``props`` sub-dict on every SIDER Compound→MedDRA_Term edge record.

    Backward-compatibility contract (master prompt Rule R3): the original
    three keys — ``source, meddra_type, umls_id_label`` — MUST remain a
    subset of this dict. Bump SIDER_SCHEMA_VERSION if any of those three
    keys is removed or renamed (Rule R6).
    """

    # ── Legacy keys (preserved verbatim from v0 — Rule R3) ─────────────
    source: str                                # "SIDER"
    meddra_type: Optional[str]                 # PT / LLT / HLT / HLGT / SOC, or None
    umls_id_label: Optional[str]               # UMLS CUI of label, or None

    # ── Standard provenance keys (BUG-14.1, BUG-14.2) ──────────────────
    _source: str                               # "SIDER"
    _license: str                              # "CC0 1.0"
    _attribution: str                          # Kuhn et al. citation
    _schema_version: str                       # SIDER_SCHEMA_VERSION
    _parser_version: str                       # SIDER_PARSER_VERSION

    # ── SIDER-specific metadata (nested under props['_sider']) ─────────
    # BUG-15.1 (mirrors STITCH): SIDER-specific metadata is nested to keep
    # top-level props compliant with the kg_builder.load_edges_bulk_create
    # contract.
    _sider: Dict[str, Any]

    # ── Stereochemistry + Compound identity (D3.2 / Phase 0.1) ────────
    pubchem_cid: int                           # canonical int Compound ID
    stereochemistry: str                       # "flat" / "stereo" / "both"
    stitch_id_raw: str                         # original CIDm/CIDs string

    # ── Adverse-event metadata (D3.3) ──────────────────────────────────
    black_box_warning: bool                    # True if FDA label has BBW
    fda_label_count: int                       # # of FDA labels mentioning this AE

    # ── Deterministic ordering + lineage ───────────────────────────────
    source_version: str                        # SIDER release, e.g. "2023-10-25"
    meddra_version: str                        # e.g. "26.0"
    load_id: str                               # process-cached UUID for rollback
    parsed_at: str                             # ISO-8601 UTC timestamp

    # ── Per-edge provenance (BUG-7.3 / BUG-16.4) ───────────────────────
    _provenance: Dict[str, Any]                # see SIDER_PROVENANCE_KEYS


class SiderEdgeRecord(TypedDict, total=False):
    """An edge record for the knowledge graph, derived from a SIDER side effect.

    Each record represents a Compound→MedDRA_Term relationship
    (``causes_adverse_event``). The Compound endpoint is a PubChem CID
    (int); the MedDRA_Term endpoint is a UMLS CUI prefixed with
    ``"MedDRA:"`` (per D15.2 — prevents collision with Disease UMLS CUIs).

    Required top-level fields (enforced by the loader at runtime):
        id, src_id, dst_id, src_type, dst_type, rel_type, props
    """

    # ── Edge identity (deterministic — D2.8) ────────────────────────────
    id: str                          # sha1(src_id|dst_id|rel_type|SIDER)[:16]

    # ── Edge endpoints ──────────────────────────────────────────────────
    src_id: int                      # pubchem_cid (int, per Phase 0.1)
    dst_id: str                      # "MedDRA:C0018790" (prefixed, per D15.2)
    src_type: str                    # always "Compound"
    dst_type: str                    # "MedDRA_Term" (canonical, per Phase 0.3)
    rel_type: str                    # "causes_adverse_event" (canonical)

    # ── Edge properties ─────────────────────────────────────────────────
    props: Dict[str, Any]            # see SiderEdgeProps

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str
    _schema_version: str


class SiderLegacyEdgeRecord(TypedDict, total=False):
    """A LEGACY edge record (Compound→Side Effect, causes_side_effect).

    Kept for migration-period dual-write (Phase 0.3 / D2.10). New code
    SHOULD use ``SiderEdgeRecord`` (canonical). This legacy record will
    be removed in v2.0.

    The legacy record differs from the canonical record in:
      * ``dst_type`` is ``"Side Effect"`` (with space) — NOT ``"MedDRA_Term"``.
      * ``rel_type`` is ``"causes_side_effect"`` — NOT ``"causes_adverse_event"``.
      * ``id`` is suffixed with ``"|SIDER_LEGACY"`` so canonical and legacy
        edges have different deterministic IDs (D2.8 — prevents id
        collision in MERGE).
    """

    id: str                          # sha1(src_id|dst_id|rel_type|SIDER_LEGACY)[:16]
    src_id: int                      # pubchem_cid (int, per Phase 0.1)
    dst_id: str                      # "MedDRA:C0018790" (prefixed, per D15.2)
    src_type: str                    # always "Compound"
    dst_type: str                    # "Side Effect" (legacy, with space)
    rel_type: str                    # "causes_side_effect" (legacy)
    props: Dict[str, Any]            # see SiderEdgeProps
    _provenance: Dict[str, Any]
    _license: str
    _attribution: str
    _schema_version: str


class SiderLoaderMetrics(TypedDict, total=False):
    """Pipeline-run metrics for the SIDER loader (Domain 11 Observability).

    The ``load_sider`` facade populates every field and returns the dict
    in its result.
    """

    rows_in: int                                # raw rows from file
    rows_after_cid_filter: int                  # after _extract_pubchem_cid
    rows_after_meddra_filter: int               # after _apply_meddra_type_filter
    rows_after_dedup: int                       # after _dedupe
    nodes_created: int                          # final node count
    edges_created: int                          # final edge count
    duplicate_edges: int                        # removed by _dedupe_edges
    invalid_umls_cui: int                       # DLQ'd by _validate_umls_ids
    invalid_pubchem_cid: int                    # DLQ'd by CID range check
    invalid_meddra_type: int                    # DLQ'd by _validate_meddra_type
    invalid_side_effect_name: int               # DLQ'd by _validate_side_effect_name
    stitch_id_numeric_mismatch: int             # DLQ'd by cross-col check
    dlq_entries: int                            # total written to DLQ
    parse_time_seconds: float
    edge_build_time_seconds: float
    peak_memory_mb: float
    errors: List[str]                           # non-fatal error summaries


class SiderDeadLetterEntry(TypedDict, total=False):
    """One entry in the SIDER dead-letter queue (JSONL).

    Written whenever a row is dropped due to malformed data, missing CID,
    invalid UMLS CUI, bad meddra_type, etc. The DLQ file lives at
    ``logs/dlq/sider_dlq.jsonl``.
    """

    timestamp: str                              # ISO-8601 UTC
    row_index: Optional[int]                    # original df row index (or None)
    reason: str                                 # short machine-readable reason code
    raw_values: Dict[str, Any]                  # the offending row's values
    parser_version: str                         # SIDER_PARSER_VERSION
    schema_version: str                         # SIDER_SCHEMA_VERSION
    stage: str                                  # pipeline stage that emitted the entry
    load_id: str                                # process-cached UUID for rollback


class SiderValidationReport(TypedDict, total=False):
    """Result of ``validate_sider`` (Domain 5 Data Quality)."""

    total_rows: int
    null_stitch_id_flat: int
    null_stitch_id_stereo: int
    null_umls_id_meddra: int
    null_side_effect_name: int
    invalid_umls_cui: int
    invalid_pubchem_cid: int
    invalid_meddra_type: int
    invalid_side_effect_name: int
    stitch_id_numeric_mismatch: int
    duplicate_rows: int
    pt_rows: int
    llt_rows: int
    hlt_rows: int
    hlgt_rows: int
    soc_rows: int
    unique_drugs: int
    unique_meddra_terms: int
    columns_present: List[str]
    columns_missing: List[str]
    columns_unexpected: List[str]
    schema_version: str


# Keys that every SIDER edge record's _provenance MUST contain
# (master prompt BUG-7.3 / BUG-16.4 / GAP-16.4 / L16-01).
#
# Shape mirrors STITCH_PROVENANCE_KEYS but with SIDER-specific extras:
#   * ``sider_version``        — SIDER release (e.g. "2023-10-25")
#   * ``meddra_version``       — MedDRA vocabulary version (e.g. "26.0")
#   * ``meddra_type_filter``   — the meddra_type cutoff applied (default "PT")
#   * ``stereo_mode``          — "flat" / "stereo" / "both"
#   * ``row_count_in / out``   — counts before/after filtering
#   * ``load_id``              — process-cached UUID for rollback
#   * ``input_sha256``         — SHA-256 of source file (alias of source_sha256)
#   * ``output_sha256``        — SHA-256 of sorted edges (idempotency check)
SIDER_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "sider_version",
    "meddra_version",
    "meddra_type_filter",
    "stereo_mode",
    "row_count_in",
    "row_count_out",
    "load_id",
    "input_sha256",
    "output_sha256",
)


# =============================================================================
# OpenTargets schemas — added by opentargets_loader v2.0 institutional-grade
# audit fix (opentargets_loader_repair_prompt.md — Section 2.6).
#
# These TypedDicts describe the records produced and consumed by the
# OpenTargets loader. They are the **single source of truth** for the
# field names emitted by the loader (D1-003 / D15-001 interface contract).
#
# IMPORTANT (SCI-1 fix): the REAL OpenTargets 25.03 evidence JSONL is FLAT:
#   {"datasourceId":"chembl","datatypeId":"known_drug","targetId":"ENSG...",
#    "diseaseId":"EFO...","drugId":"CHEMBL...","score":0.5,
#    "evidenceScore":0.5}
# The v1 parser used a fabricated nested schema (entry["drug"]["id"], etc.)
# which silently dropped 100% of real records. The v2.0 schema here mirrors
# the REAL flat fields.
#
# Patient-safety doctrine: OpenTargets is the SOLE source of evidence-scored
# drug-target-disease triples feeding the Graph Transformer's confidence
# training objective. If this loader silently drops 100% of records
# (the v1 SCI-1 condition), the model trains on an empty OpenTargets
# signal — worse than no signal. These schemas MUST match the real
# OpenTargets JSONL format exactly.
#
# Fixes: SCI-1 (flat schema), SCI-2 (datasourceId+datatypeId),
#        SCI-3 (disease ID crosswalk), SCI-4 (ChEMBL ID validation),
#        SCI-5 (score validation), SCI-7 (organism filter),
#        SCI-8 (no "indication" label), ARCH-4 (TypedDicts),
#        D14-004 (schema version), D15-001 (stable interface contract).
# =============================================================================


class OpenTargetsActivityRecord(TypedDict, total=False):
    """A single parsed OpenTargets evidence record.

    Produced by ``iter_opentargets_evidence`` / ``parse_opentargets_evidence``.
    Consumed by ``opentargets_to_edge_records`` and
    ``opentargets_to_node_records``.

    The flat-schema fields mirror the REAL OpenTargets 25.03 JSONL
    (NOT the fabricated nested schema in v1 — fixes SCI-1).

    Fields
    ------
    drug_id : str
        CHEMBLxxxxx — validated by ``^CHEMBL\\d+$`` (SCI-4).
    target_id : str
        ENSG\\d{11} — validated by ``^ENSG\\d{11}$`` (SCI-10); organism-filtered
        to human (targetTaxId == 9606, SCI-7).
    disease_id : str
        EFO_xxxxxxxx / MONDO_xxxxxxxx / HP_xxxxxxxx / MP_xxxxxxxx /
        Orphanet_xxxxx / SNOMEDCT_xxxxx / OTAR_xxxxxxxx — validated against
        ``OPENTARGETS_DISEASE_ID_PATTERNS`` (DQ-11). Empty string if the
        record has no disease association.
    drug_name : str
        Optional drug display name (sanitized for Cypher — SEC-4).
    disease_name : str
        Optional disease display name (sanitized for Cypher — SEC-4).
    score : float
        Validated float in [0, 1] (SCI-5, DQ-8). NaN/Infinity/bool rejected.
    evidence_score : float
        Alias of ``score`` — OpenTargets emits both ``score`` and
        ``evidenceScore``; we read either.
    datasource_id : str
        e.g. "chembl", "evrot", "crispr", "ot_genetics_portal" (SCI-2).
    datatype_id : str
        e.g. "known_drug", "genetic_association", "literature" (SCI-2).
    target_tax_id : int
        NCBI Taxonomy ID — 9606 for human (SCI-7). Default 9606 when absent.
    _provenance : dict
        Full provenance dict with all ``OPENTARGETS_PROVENANCE_KEYS`` (LIN-1..5).
    _source : str
        Always "OpenTargets" (COMP-2).
    _license : str
        Always "CC0 1.0" (COMP-3).
    _attribution : str
        Full attribution string (COMP-3).
    _schema_version : str
        Always matches ``SCHEMA_VERSION`` (COMP-3).
    """

    # Identity (flat — fixes SCI-1)
    drug_id: str           # CHEMBLxxxxx — validated by _RE_CHEMBL_ID
    target_id: str         # ENSG\d{11}  — validated; organism-filtered
    disease_id: str        # EFO/MONDO/HP/MP/Orphanet/SNOMED/OTAR
    # Names (optional, sanitized — fixes SEC-4)
    drug_name: str
    disease_name: str
    # Scores (validated float in [0,1] — fixes SCI-5, DQ-8, COD-1..4)
    score: float
    evidence_score: float  # alias — OpenTargets emits both `score` and `evidenceScore`
    # Evidence typing (real fields — fixes SCI-2)
    datasource_id: str     # "chembl", "evrot", "crispr", etc.
    datatype_id: str       # "known_drug", "genetic_association", "literature", etc.
    # Organism (fixes SCI-7)
    target_tax_id: int     # 9606 for human
    # Provenance (fixes LIN-1..5, COMP-2..5)
    _provenance: Dict[str, Any]
    _source: str
    _license: str
    _attribution: str
    _schema_version: str


class OpenTargetsEdgeRecord(TypedDict, total=False):
    """An OpenTargets edge record emitted to the KG builder.

    Edges are deduplicated by (src_id, dst_id, src_type, dst_type, rel_type)
    keeping the record with the maximum score (SCI-13). Every edge carries
    a full ``_provenance`` dict with all ``OPENTARGETS_PROVENANCE_KEYS``.

    Fields
    ------
    src_id : str
        Source node ID (always a ChEMBL Compound ID, e.g. "CHEMBL123").
    dst_id : str
        Destination node ID (UniProt AC for Protein, NCBI Gene ID for Gene,
        UMLS CUI for Disease — depends on ``dst_type``).
    src_type : str
        Always "Compound" (D15.8).
    dst_type : str
        One of {"Protein", "Gene", "Disease", "Pathway"}.
    rel_type : str
        One of {"binds", "targets", "tested_for", "associated_with",
        "disrupted_in"}. NEVER "indication" (SCI-8).
    props : dict
        Edge properties including semantic-specific score keys (SCI-12),
        ``_provenance``, ``_source``, ``_license``, ``_attribution``,
        ``_schema_version``, ``id`` (deterministic edge ID).
    """

    src_id: str
    dst_id: str
    src_type: str
    dst_type: str
    rel_type: str
    props: Dict[str, Any]


class OpenTargetsNodeRecord(TypedDict, total=False):
    """An OpenTargets node record emitted to the KG builder.

    Compound nodes are derived from the unique ``drug_id`` values in the
    parsed records. Disease, Protein, and Gene nodes are produced by
    other loaders (DRKG, UniProt); the OpenTargets loader only emits
    Compound nodes to avoid duplicate-node writes.

    Fields
    ------
    node_id : str
        Compound node ID (ChEMBL ID, e.g. "CHEMBL123").
    node_type : str
        Always "Compound" (D15.8).
    props : dict
        Node properties including ``name``, ``_provenance``,
        ``_source``, ``_license``, ``_attribution``, ``_schema_version``.
    """

    node_id: str
    node_type: str
    props: Dict[str, Any]


class OpenTargetsLoaderMetrics(TypedDict, total=False):
    """Runtime metrics for an OpenTargets loader run.

    Emitted by ``iter_opentargets_evidence`` (per-batch) and
    ``validate_opentargets`` (final). Mirrors the SIDER/STITCH metrics
    pattern (D11.2).

    Fields
    ------
    n_lines_read : int
        Total lines read from the source file.
    n_records_kept : int
        Records that passed all validation gates.
    n_records_skipped_low_score : int
        Records dropped because ``score < per_evidence_type_threshold``.
    n_records_skipped_missing_id : int
        Records dropped because drug_id or target_id was missing.
    n_records_skipped_non_human : int
        Records dropped because target_tax_id != 9606 (SCI-7).
    n_records_skipped_malformed_id : int
        Records dropped because ChEMBL/ENSG/disease ID failed regex.
    n_records_dead_lettered : int
        Records written to the dead-letter queue.
    n_targets_resolved : int
        Records where ENSG was crosswalked to a UniProt AC.
    n_targets_unresolved : int
        Records where ENSG could not be crosswalked (orphan, flagged).
    n_diseases_resolved : int
        Records where disease_id was crosswalked to a UMLS CUI.
    n_diseases_unresolved : int
        Records where disease_id could not be crosswalked (orphan, flagged).
    n_edges_compound_targets_protein : int
        Compound→targets→Protein edges emitted (after UniProt resolution).
    n_edges_compound_targets_gene : int
        Compound→targets→Gene edges emitted (NCBI or ENSG-prefixed).
    n_edges_compound_disease : int
        Compound→{binds|tested_for|associated_with}→Disease edges emitted.
    n_edges_protein_disease : int
        Protein→associated_with→Disease edges emitted.
    n_edges_deduped : int
        Edges removed by the dedupe step (SCI-13).
    elapsed_seconds : float
        Wall-clock time of the parse stage.
    """

    n_lines_read: int
    n_records_kept: int
    n_records_skipped_low_score: int
    n_records_skipped_missing_id: int
    n_records_skipped_non_human: int
    n_records_skipped_malformed_id: int
    n_records_dead_lettered: int
    n_targets_resolved: int
    n_targets_unresolved: int
    n_diseases_resolved: int
    n_diseases_unresolved: int
    n_edges_compound_targets_protein: int
    n_edges_compound_targets_gene: int
    n_edges_compound_disease: int
    n_edges_protein_disease: int
    n_edges_deduped: int
    elapsed_seconds: float


class OpenTargetsDeadLetterEntry(TypedDict, total=False):
    """One entry in the OpenTargets dead-letter queue (JSONL).

    Written whenever a record is dropped due to malformed data, missing
    IDs, non-human organism, invalid score, etc. The DLQ file lives at
    ``data/dead_letter/opentargets_malformed.jsonl`` (REL-5).

    Fields
    ------
    timestamp : str
        ISO-8601 UTC timestamp.
    line_no : int or None
        Source line number (0-indexed).
    reason : str
        Short machine-readable reason code (e.g. "invalid_drug_id",
        "non_human_target", "invalid_score", "low_score",
        "invalid_target_id", "invalid_disease_id", "per_record_error").
    raw : str or None
        Sanitized raw input (truncated to 100 chars, no PII).
    parsed_partial : dict or None
        Partial parse result if available.
    error_type : str
        Exception class name (when reason is "per_record_error").
    error_message : str
        Truncated exception message.
    parser_version : str
        ``PARSER_VERSION`` at the time of the DLQ entry.
    schema_version : str
        ``SCHEMA_VERSION`` at the time of the DLQ entry.
    load_id : str
        Process-cached UUID for rollback.
    """

    timestamp: str
    line_no: Optional[int]
    reason: str
    raw: Optional[str]
    parsed_partial: Optional[Dict[str, Any]]
    error_type: str
    error_message: str
    parser_version: str
    schema_version: str
    load_id: str


class OpenTargetsValidationReport(TypedDict, total=False):
    """Result of ``validate_opentargets`` (Domain 5 Data Quality).

    Fields
    ------
    is_valid : bool
        True if no errors (warnings are non-blocking).
    errors : list of str
        Blocking errors — pipeline cannot continue in CLINICAL+ mode.
    warnings : list of str
        Non-blocking warnings (e.g. low resolution rate in DEV mode).
    metrics : OpenTargetsLoaderMetrics
        Loader metrics (subset).
    schema_version : str
        Schema version at validation time.
    parser_version : str
        Parser version at validation time.
    """

    is_valid: bool
    errors: List[str]
    warnings: List[str]
    metrics: Dict[str, Any]
    schema_version: str
    parser_version: str


# Keys that every OpenTargets edge record's _provenance MUST contain
# (opentargets_loader_repair_prompt.md Section 2.6 / LIN-1..5 / COMP-2..5).
#
# Shape mirrors SIDER_PROVENANCE_KEYS / STITCH_PROVENANCE_KEYS but with
# OpenTargets-specific extras:
#   * ``opentargets_release``      — OpenTargets release (e.g. "25.03")
#   * ``min_score``                — min-score threshold applied
#   * ``per_evidence_type_thresholds`` — per-datasource thresholds
#   * ``organism_filter``          — "9606" (human)
#   * ``organism_match_mode``      — "exact_taxid" / "ensg_prefix"
#   * ``row_count_in / out``       — counts before/after filtering
#   * ``n_dead_letter``            — DLQ count for this run
#   * ``crosswalk_version``        — ENSG→UniProt crosswalk version
#   * ``disease_crosswalk_version`` — Disease→UMLS crosswalk version
#   * ``resolution_rate``          — UniProt resolution rate (target edges)
#   * ``resolution_path``          — How the ID was resolved (e.g.
#                                    "ensembl_to_uniprot_direct")
OPENTARGETS_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "opentargets_release",
    "min_score",
    "per_evidence_type_thresholds",
    "organism_filter",
    "organism_match_mode",
    "row_count_in",
    "row_count_out",
    "n_dead_letter",
    "crosswalk_version",
    "disease_crosswalk_version",
    "resolution_rate",
)


# =============================================================================
# ClinicalTrials schemas — added by clinicaltrials_loader v2.1.0
# institutional-grade audit fix (PROMPT_fix_clinicaltrials_loader.md —
# Domains 2 / 4 / 7 / 15 / 16).
#
# These TypedDicts describe the *shape* of the records emitted by
# ``drugos_graph.clinicaltrials_loader.parse_clinicaltrials`` and the result
# of ``validate_clinicaltrials``. They are the **single source of truth** for
# the field names: adding a field here is a schema change and MUST be
# accompanied by a ``SCHEMA_VERSION`` bump in ``clinicaltrials_loader.py``
# (Issue 14.6).
#
# RATIONALE for ``total=False``:
#   AACT emits a heterogeneous set of trial records. Not every column makes
#   sense for every trial; for example, ``why_stopped`` is only populated
#   for terminated trials, and ``drug_mesh`` may be NULL when the MeSH
#   crosswalk fails. Using ``total=False`` lets us declare all possible
#   columns without forcing every row to populate every field — the loader's
#   own runtime checks (Issue 5.1, 5.10, 5.11) enforce the required columns.
# =============================================================================


class ClinicalTrialTrialRecord(TypedDict, total=False):
    """A single parsed AACT clinical-trial record (one JOIN row).

    Produced by ``clinicaltrials_loader.parse_clinicaltrials`` as one row
    of the returned DataFrame; consumed by
    ``clinicaltrials_to_edge_records`` to produce ``ClinicalTrialEdgeRecord``
    edges.

    Column naming rules (mirror ``OpenTargetsActivityRecord``):
      * Singular nouns (``nct_id``, ``phase``, ``drug_name``, ``drug_mesh``,
        ``condition_name``, ``condition_mesh``, ``enrollment``,
        ``why_stopped``, ``overall_status``, ``study_type``, ``allocation``,
        ``masking``, ``intervention_model``, ``primary_purpose``,
        ``primary_outcome``, ``brief_title``, ``start_date``,
        ``completion_date``, ``drug_role``, ``description``) are ``str`` or
        ``Optional[str]``.
      * ``enrollment`` is ``Optional[int]`` — AACT may store NULL for
        trials with unknown enrollment.
      * ``_provenance`` is a ``dict[str, Any]`` and MUST contain every key
        in ``CLINICALTRIALS_PROVENANCE_KEYS`` (Issue 16.1).
      * ``_license`` and ``_attribution`` propagate the AACT CC0 1.0
        license + CTTI citation (Issue 13.7, 13.8).

    Fixes: Issues 3.1 (schema), 3.5 (why_stopped), 3.6 (enrollment),
           3.7 (study_type), 3.8 (allocation/masking), 3.9 (primary_outcome),
           3.10 (has_results), 3.12 (brief_title), 3.13 (start/completion_date),
           3.14 (MeSH aggregation), 14.9 (MeSH normalization).
    """

    # ── Identity ────────────────────────────────────────────────────────
    nct_id: str                          # NCT ID, validated ^NCT\d{8}$ (Issue 3.15)
    brief_title: str                     # studies.brief_title (Issue 3.12)
    nct_url: str                         # https://clinicaltrials.gov/study/{nct_id} (Issue 16.6)

    # ── Trial design ────────────────────────────────────────────────────
    phase: str                           # "Phase 3", "Phase 4", etc. (Issue 3.2)
    overall_status: str                  # "Completed", "Terminated", etc. (Issue 3.11)
    study_type: str                      # "Interventional", "Observational" (Issue 3.7)
    enrollment: Optional[int]            # studies.enrollment (Issue 3.6)
    why_stopped: Optional[str]           # studies.why_stopped (Issue 3.5)
    has_results: bool                    # studies.has_results (Issue 3.10)

    # ── Trial dates ─────────────────────────────────────────────────────
    start_date: Optional[str]            # ISO-8601 YYYY-MM-DD or None (Issue 3.13, 14.7)
    completion_date: Optional[str]       # ISO-8601 YYYY-MM-DD or None (Issue 3.13, 14.7)

    # ── Design details (JOIN designs) ───────────────────────────────────
    allocation: Optional[str]            # "Randomized", "Non Randomized" (Issue 3.8)
    intervention_model: Optional[str]    # "Parallel", "Crossover", etc. (Issue 3.8)
    masking: Optional[str]               # "Double", "Single", "None" (Issue 3.8)
    primary_purpose: Optional[str]       # "Treatment", "Prevention" (Issue 3.8)

    # ── Outcome ─────────────────────────────────────────────────────────
    primary_outcome: Optional[str]       # GROUP_CONCAT of primary_outcomes.measure (Issue 3.9)

    # ── Drug / condition (one row per cross-join combination) ───────────
    drug_name: str                       # interventions.name (Issue 15.10 — normalized)
    drug_mesh: Optional[str]             # interventions_mesh_terms.mesh_term (Issue 3.1, 3.14)
    drug_role: str                       # "experimental" | "comparator_or_placebo" (Issue 3.3)
    description: Optional[str]           # interventions.description (Issue 3.3)
    condition_name: str                  # conditions.name
    condition_mesh: Optional[str]        # conditions_mesh_terms.mesh_term (Issue 3.1, 3.14)

    # ── Provenance & compliance ─────────────────────────────────────────
    _provenance: Dict[str, Any]
    _source: str
    _license: str
    _attribution: str
    _schema_version: str


class ClinicalTrialEdgeRecord(TypedDict, total=False):
    """A clinical-trial edge record emitted to the KG builder.

    Produced by ``clinicaltrials_to_edge_records``. Consumed by
    ``kg_builder.DrugOSGraphBuilder.load_edges_bulk_create`` to create
    ``(Compound)-[:tested_for]->(Disease)`` edges in Neo4j.

    SCIENTIFIC CORRECTNESS CONTRACT (Issue 2.1, 14.1, 15.3):
      * ``rel_type`` is ALWAYS ``"tested_for"`` — NEVER ``"clinical_trial"``
        (DEPRECATED v0 name) and NEVER ``"treats"`` (FORBIDDEN — reserved
        for FDA-approved drugs from DrugBank).
      * ``src_type`` is ALWAYS ``"Compound"`` (Issue 15.9).
      * ``dst_type`` is ALWAYS ``"Disease"`` (Issue 15.9).

    Fields
    ------
    src_id : str
        Canonical Compound node ID. Preference order:
        1. MeSH term crosswalked to DrugBank ID via id_crosswalk (Issue 15.7)
        2. MeSH term (raw) — id_confidence="low"
        3. drug_name (normalized) — id_confidence="low"
        Never empty (Issue 4.7).
    dst_id : str
        Canonical Disease node ID. Preference order:
        1. MeSH term crosswalked to UMLS CUI via id_crosswalk (Issue 15.8)
        2. MeSH term (raw) — id_confidence="low"
        3. condition_name (normalized) — id_confidence="low"
        Never empty (Issue 4.7).
    src_type : str
        Always ``"Compound"`` (Issue 15.9).
    dst_type : str
        Always ``"Disease"`` (Issue 15.9).
    rel_type : str
        Always ``"tested_for"`` (Issue 2.1, 14.1, 15.3). Never
        ``"clinical_trial"`` (deprecated). Never ``"treats"`` (forbidden —
        reserved for FDA-approved drugs from DrugBank).
    edge_id : str
        Deterministic SHA-1 hash of
        ``"{src_id}|{dst_id}|{src_type}|{dst_type}|{rel_type}|{nct_id}"``.
        Used for deduplication and idempotency (Issue 2.3, 7.1).
    source_tag : str
        Always ``"ClinicalTrials"`` (Issue 2.6).
    evidence_strength : float
        Float in [0.0, 1.0]. Computed by ``_compute_evidence_strength``
        from phase, enrollment, allocation, masking, has_results,
        why_stopped, drug_role (Issue 2.5). Comparator/placebo edges get
        ``evidence_strength *= 0.3`` (Issue 3.3).
    confidence : str
        ``"high"`` / ``"medium"`` / ``"low"`` — RL ranker safety dimension
        (Issue 2.5, 2.6).
    id_confidence : str
        ``"high"`` / ``"medium"`` / ``"low"`` — ID resolution confidence.
        ``"low"`` when src_id or dst_id is a free-text fallback, when the
        MeSH crosswalk failed, when drug_role="comparator_or_placebo",
        when why_stopped matched safety pattern, or when referential
        integrity check failed (Issue 16.12).
    props : dict
        All edge properties including:
          * ``nct_id``, ``nct_url``, ``phase``, ``status``, ``enrollment``,
            ``why_stopped``, ``drug_name``, ``drug_mesh``, ``condition_name``,
            ``condition_mesh``, ``drug_role``, ``allocation``, ``masking``,
            ``intervention_model``, ``primary_purpose``, ``primary_outcome``,
            ``brief_title``, ``start_date``, ``completion_date``,
            ``safety_signal`` (when stopped for safety — Issue 3.5),
            ``orphan_src`` / ``orphan_dst`` (when referential integrity
            fails — Issue 5.4).
          * Lineage fields: ``source_url``, ``downloaded_at``,
            ``source_sha256``, ``source_version``, ``pipeline_version``,
            ``schema_version``, ``license``, ``citation`` (Issue 16.1-16.6).
          * Compliance fields: ``_source``, ``_license``, ``_attribution``,
            ``_schema_version``, ``_provenance`` (Issue 13.7, 14.4).
    """

    src_id: str
    dst_id: str
    src_type: str
    dst_type: str
    rel_type: str
    edge_id: str
    source_tag: str
    evidence_strength: float
    confidence: str
    id_confidence: str
    props: Dict[str, Any]


class ClinicalTrialNodeRecord(TypedDict, total=False):
    """A ClinicalTrials node record (emitted for completeness).

    The ClinicalTrials loader emits edges ONLY — Compound and Disease nodes
    are owned by DrugBank / ChEMBL / OpenTargets and DisGeNET / OMIM
    respectively (Issue 15.2 — schema matches sibling loaders). However,
    a minimal node record is emitted for the rare case where a clinical
    trial references a Compound or Disease not in the KG — the KG builder
    can then create a placeholder node.

    Fields
    ------
    node_id : str
        Canonical node ID (matches ``src_id`` or ``dst_id`` of an edge).
    node_type : str
        ``"Compound"`` or ``"Disease"`` (Issue 15.9).
    props : dict
        Node properties including ``name``, ``mesh_term``, ``source``,
        ``_source``, ``_license``, ``_attribution``, ``_schema_version``,
        ``_provenance``.
    """

    node_id: str
    node_type: str
    props: Dict[str, Any]


class ClinicalTrialsLoaderMetrics(TypedDict, total=False):
    """Runtime metrics for a ClinicalTrials loader run.

    Emitted by ``parse_clinicaltrials`` and ``clinicaltrials_to_edge_records``
    via structured logging (Issue 11.3) and persisted to the lineage file
    (Issue 16.10).

    Fields
    ------
    started_at : str
        ISO-8601 timestamp when the run started.
    finished_at : str
        ISO-8601 timestamp when the run finished.
    elapsed_seconds : float
        Wall-clock seconds for the run (Issue 11.4).
    rows_before_filter : int
        Raw row count from the SQL query (Issue 16.7).
    rows_after_filter : int
        Row count after applying max_trial_age_years, min_enrollment (Issue 16.7).
    total_rows : int
        Final row count fed to edge builder (alias for rows_after_filter).
    null_nct_id : int
        Count of rows with NULL nct_id (Issue 5.8).
    null_drug_mesh : int
        Count of rows with NULL drug_mesh (Issue 5.8).
    null_drug_name : int
        Count of rows with NULL drug_name (Issue 5.8).
    null_both_drug : int
        Count of rows with BOTH drug_mesh AND drug_name NULL (Issue 5.8).
    null_condition_mesh : int
        Count of rows with NULL condition_mesh (Issue 5.8).
    null_condition_name : int
        Count of rows with NULL condition_name (Issue 5.8).
    null_both_condition : int
        Count of rows with BOTH condition_mesh AND condition_name NULL.
    null_enrollment : int
        Count of rows with NULL enrollment.
    null_phase : int
        Count of rows with NULL phase.
    stopped_for_safety : int
        Count of rows whose why_stopped matched the safety pattern (Issue 3.5).
    rows_dropped_null_nct : int
        Rows dropped because nct_id was NULL (Issue 5.1).
    rows_dropped_invalid_nct : int
        Rows dropped because nct_id failed regex validation (Issue 3.15).
    rows_dropped_garbage_mesh : int
        Rows dropped because MeSH term was in the garbage blocklist (Issue 5.11).
    rows_dropped_empty_src : int
        Rows dropped because src_id was empty after fallback (Issue 4.7).
    rows_dropped_empty_dst : int
        Rows dropped because dst_id was empty after fallback (Issue 4.7).
    rows_dropped_age : int
        Rows dropped because trial older than max_trial_age_years (Issue 3.13).
    rows_quarantined_total : int
        Total rows written to the dead-letter queue (Issue 6.5).
    edges_total : int
        Total edges emitted (before dedup).
    edges_deduped : int
        Duplicate edges collapsed by edge_id (Issue 2.4, 7.1).
    edges_orphan_src : int
        Edges whose src_id does not resolve to a known Compound (Issue 5.4).
    edges_orphan_dst : int
        Edges whose dst_id does not resolve to a known Disease (Issue 5.4).
    edges_with_low_id_confidence : int
        Edges with id_confidence="low" (Issue 16.12).
    edges_with_safety_signal : int
        Edges with safety_signal="stopped_for_safety" (Issue 3.5).
    edges_with_comparator : int
        Edges with drug_role="comparator_or_placebo" (Issue 3.3).
    final_edge_count : int
        Edges emitted after dedup (Issue 16.7).
    phase_counts : dict
        {phase_value: count} for post-SQL data quality audit (Issue 5.5).
    status_counts : dict
        {status_value: count} for post-SQL data quality audit.
    """

    started_at: str
    finished_at: str
    elapsed_seconds: float
    rows_before_filter: int
    rows_after_filter: int
    total_rows: int
    null_nct_id: int
    null_drug_mesh: int
    null_drug_name: int
    null_both_drug: int
    null_condition_mesh: int
    null_condition_name: int
    null_both_condition: int
    null_enrollment: int
    null_phase: int
    stopped_for_safety: int
    rows_dropped_null_nct: int
    rows_dropped_invalid_nct: int
    rows_dropped_garbage_mesh: int
    rows_dropped_empty_src: int
    rows_dropped_empty_dst: int
    rows_dropped_age: int
    rows_quarantined_total: int
    edges_total: int
    edges_deduped: int
    edges_orphan_src: int
    edges_orphan_dst: int
    edges_with_low_id_confidence: int
    edges_with_safety_signal: int
    edges_with_comparator: int
    final_edge_count: int
    phase_counts: Dict[str, int]
    status_counts: Dict[str, int]


class ClinicalTrialsDeadLetterEntry(TypedDict, total=False):
    """One entry in the ClinicalTrials dead-letter queue (JSONL).

    Written to ``CLINICALTRIALS_DEAD_LETTER_PATH`` for every quarantined
    row. Used for forensic inspection of bad data (Issue 6.5).

    Fields
    ------
    timestamp : str
        ISO-8601 UTC timestamp when the entry was written.
    nct_id : str or None
        The NCT ID of the row (if known — may be NULL for the
        ``null_or_empty_nct_id`` reason).
    reason : str
        Quarantine reason. One of:
          * ``null_or_empty_nct_id`` (Issue 5.1)
          * ``invalid_nct_id_format`` (Issue 3.15)
          * ``empty_src_id`` (Issue 4.7)
          * ``empty_dst_id`` (Issue 4.7)
          * ``garbage_mesh_term`` (Issue 5.11)
          * ``suspected_secret_in_data`` (Issue 9.10)
          * ``build_edge_exception:<ExceptionType>:<message>`` (Issue 6.6)
    raw : str or None
        The full row serialized as JSON (truncated to 2000 chars).
    parsed_partial : dict or None
        The partially-built edge record (if any).
    error_type : str
        For ``build_edge_exception`` entries, the exception class name.
    error_message : str
        Truncated exception message.
    parser_version : str
        ``PARSER_VERSION`` at the time of the DLQ entry.
    schema_version : str
        ``SCHEMA_VERSION`` at the time of the DLQ entry.
    load_id : str
        Process-cached UUID for rollback.
    """

    timestamp: str
    nct_id: Optional[str]
    reason: str
    raw: Optional[str]
    parsed_partial: Optional[Dict[str, Any]]
    error_type: str
    error_message: str
    parser_version: str
    schema_version: str
    load_id: str


class ClinicalTrialsValidationReport(TypedDict, total=False):
    """Result of ``validate_clinicaltrials`` (Domain 5 Data Quality).

    Fields
    ------
    is_valid : bool
        True if no errors (warnings are non-blocking).
    errors : list of str
        Blocking errors — pipeline cannot continue in CLINICAL+ mode.
    warnings : list of str
        Non-blocking warnings (e.g. low resolution rate in DEV mode).
    metrics : ClinicalTrialsLoaderMetrics
        Loader metrics (subset).
    schema_version : str
        Schema version at validation time.
    parser_version : str
        Parser version at validation time.
    """

    is_valid: bool
    errors: List[str]
    warnings: List[str]
    metrics: Dict[str, Any]
    schema_version: str
    parser_version: str


# Keys that every ClinicalTrials edge record's _provenance MUST contain
# (PROMPT_fix_clinicaltrials_loader.md Section 16 — Data Lineage &
# Traceability, Issues 16.1-16.12).
#
# Shape mirrors OPENTARGETS_PROVENANCE_KEYS / SIDER_PROVENANCE_KEYS /
# STITCH_PROVENANCE_KEYS but with ClinicalTrials-specific extras:
#   * ``aact_release``             — AACT snapshot release date
#   * ``phases_filter``            — tuple of phases applied (Issue 16.7)
#   * ``intervention_types_filter`` — tuple of intervention_types (Issue 16.7)
#   * ``study_types_filter``       — tuple of study_types (Issue 16.7)
#   * ``statuses_filter``          — tuple of allowed_statuses (Issue 16.7)
#   * ``min_enrollment_filter``    — min_enrollment applied (Issue 16.7)
#   * ``max_trial_age_years``      — max_trial_age_years applied (Issue 16.7)
#   * ``row_count_in / out``       — counts before/after filtering
#   * ``n_dead_letter``            — DLQ count for this run
#   * ``n_orphan_src / dst``       — referential integrity failures
#   * ``n_safety_signal``          — edges with safety_signal set
#   * ``n_comparator``             — edges with drug_role=comparator_or_placebo
#   * ``crosswalk_version``        — MeSH→DrugBank/UMLS crosswalk version
CLINICALTRIALS_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "aact_release",
    "phases_filter",
    "intervention_types_filter",
    "study_types_filter",
    "statuses_filter",
    "min_enrollment_filter",
    "max_trial_age_years",
    "row_count_in",
    "row_count_out",
    "n_dead_letter",
    "n_orphan_src",
    "n_orphan_dst",
    "n_safety_signal",
    "n_comparator",
    "crosswalk_version",
)


# =============================================================================
# GEO schemas — added by geo_loader v1.0.0 institutional-grade audit fix
# (GEO_LOADER_MASTER_REPAIR_PROMPT.md — 192 findings across 16 domains).
#
# These TypedDicts and the GEO_PROVENANCE_KEYS tuple mirror the
# ClinicalTrials/OpenTargets/SIDER/STITCH/STRING schema pattern. Every
# record emitted by ``parse_geo_series`` conforms to ``GeoRawRecord``;
# every edge emitted by ``geo_to_edge_records`` conforms to
# ``GeoEdgeRecord``. The loader's own ``validate_geo_record`` /
# ``validate_geo_edge`` enforce required fields at runtime.
#
# SCIENTIFIC CORRECTNESS CONTRACT (Phase 0.2 / GEO-3.1):
#   * GEO emits ``Protein→expressed_in→Anatomy`` edges (NOT
#     ``Gene→expressed_in→Anatomy`` — that is DRKG's domain, see
#     ``config.py:3705``). The KG is protein-centric because drug targets
#     are proteins. GEO measures mRNA (a proxy for protein expression),
#     so we map probe → gene → UniProt accession to produce a Protein
#     edge.
#   * ``head`` is ALWAYS a UniProt accession (e.g. ``"P23219"`` for
#     PTGS1/COX-1) — NEVER a gene symbol or NCBI Gene ID.
#   * ``tail`` is ALWAYS a UBERON URI (e.g.
#     ``"http://purl.obolibrary.org/obo/UBERON_0002048"`` for lung).
#   * ``relation`` is ALWAYS ``"expressed_in"``.
#
# Fixes: GEO-2.2 (GeoRawRecord schema), GEO-2.3 (GeoEdgeRecord schema),
#        GEO-7.4 (pipeline-version stamping), GEO-13.3 (data dictionary),
#        GEO-14.5 (schema versioning), GEO-15.3 (interface contract),
#        GEO-15.8 (source on records), GEO-16.1-16.13 (lineage fields).
# =============================================================================


class GeoRawRecord(TypedDict, total=False):
    """A single parsed GEO SOFT record (one probe x one sample).

    Produced by ``geo_loader.parse_geo_series`` / ``iter_geo_records`` as
    one row of the parsed SOFT file; consumed by ``geo_to_edge_records``
    to produce ``GeoEdgeRecord`` edges.

    A single SOFT file typically yields ~50,000 GeoRawRecords (one per
    probe x sample combination). Each record carries the full provenance
    chain (R15) so the KG builder can trace any edge back to its source
    file, source series, parser version, and ingested-at timestamp.

    Field groups
    ------------
    Identifiers : ``series_id``, ``sample_id``, ``platform_id``,
                  ``probe_id``, ``gene_id``, ``uniprot_id``
    Sample metadata : ``sample_title``, ``sample_organism``,
                      ``sample_taxid``, ``sample_tissue``,
                      ``sample_tissue_uberon``, ``sample_characteristics``
    Expression : ``expression_value``, ``expression_unit``,
                 ``is_differential``
    Provenance (R15) : ``_source``, ``_source_version``, ``_source_url``,
                       ``_source_release_date``, ``_license``,
                       ``_attribution``, ``_schema_version``,
                       ``_ingested_at``, ``_pipeline_version``,
                       ``_input_sha256``, ``_source_series``,
                       ``_parser_version``
    Safety / Privacy : ``sensitive`` (GEO-9.1)

    Fixes: GEO-2.2 (typed record), GEO-3.4 (probe→gene→UniProt),
           GEO-3.9 (expression_unit), GEO-3.13 (organism filter),
           GEO-5.1 (data quality), GEO-7.2 (deterministic ordering),
           GEO-9.1 (sensitive flag), GEO-14.12 (ISO 8601 dates),
           GEO-16.1 (provenance).
    """

    # ── Identifiers ────────────────────────────────────────────────────
    series_id: str           # GSE accession, validated ^GSE\d+$ (GEO-2.1)
    sample_id: str           # GSM accession, validated ^GSM\d+$ (GEO-5.1)
    platform_id: str         # GPL accession, validated ^GPL\d+$ (GEO-5.1)
    probe_id: str            # manufacturer probe ID, e.g. "117_at" (GEO-3.4)
    gene_id: str             # NCBI Gene ID, resolved via id_crosswalk (GEO-3.4)
    uniprot_id: str          # UniProt accession, resolved via id_crosswalk (GEO-3.4)

    # ── Sample metadata ────────────────────────────────────────────────
    sample_title: str        # !Sample_title from SOFT (GEO-5.7)
    sample_organism: str     # scientific name, e.g. "Homo sapiens" (GEO-3.13)
    sample_taxid: int        # NCBI Taxonomy ID, e.g. 9606 (GEO-3.13)
    sample_tissue: str       # raw tissue description from SOFT (GEO-3.3)
    sample_tissue_uberon: str  # UBERON URI after ontology mapping (GEO-3.3)
    sample_characteristics: Dict[str, str]  # other !Sample_characteristics fields

    # ── Expression value ───────────────────────────────────────────────
    expression_value: float  # normalized expression (log2 space) (GEO-3.9)
    expression_unit: str     # "log2_rma", "log2_tpm", "raw_counts", etc. (GEO-3.9)
    is_differential: bool    # True if from a differential-expression call (GEO-3.5)
    fdr: Optional[float]     # BH-corrected FDR if is_differential=True (GEO-3.7)
    batch_corrected: bool    # always False in v1.0.0 (GEO-3.6)

    # ── Safety / Privacy (GEO-9.1) ─────────────────────────────────────
    sensitive: bool          # True if sample_characteristics contains PII fields

    # ── Provenance (R15) ───────────────────────────────────────────────
    _source: str             # always "geo"
    _source_version: str     # DATA_SOURCES["geo"]["version"] (e.g. "GSE92649")
    _source_url: str         # DATA_SOURCES["geo"]["url"]
    _source_release_date: str  # DATA_SOURCES["geo"]["release_date"] (ISO 8601)
    _license: str            # DATA_SOURCES["geo"]["license"] ("Public Domain")
    _attribution: str        # GEO_ATTRIBUTION (Barrett T et al., 2013)
    _schema_version: str     # DATA_SOURCES["geo"]["schema_version"] ("GEO-SOFT-2.0")
    _ingested_at: str        # ISO-8601 UTC at parse time
    _pipeline_version: str   # __init__.__pipeline_version__
    _input_sha256: str       # sha256 of the SOFT file (streamed, 64KB chunks)
    _source_series: str      # the series ID that produced this record
    _parser_version: str     # geo_loader.PARSER_VERSION ("1.0.0")


class GeoEdgeRecord(TypedDict, total=False):
    """A GEO edge record emitted to the KG builder.

    Produced by ``geo_loader.geo_to_edge_records``. Consumed by
    ``kg_builder.DrugOSGraphBuilder.load_edges_bulk_create`` to create
    ``(Protein)-[:expressed_in]->(Anatomy)`` edges in Neo4j.

    SCIENTIFIC CORRECTNESS CONTRACT (Phase 0.2 / GEO-3.1):
      * ``head`` is ALWAYS a UniProt accession (e.g. ``"P23219"``).
      * ``head_type`` is ALWAYS ``"Protein"`` (NOT ``"Gene"`` — that is
        DRKG's domain).
      * ``tail`` is ALWAYS a UBERON URI (e.g.
        ``"http://purl.obolibrary.org/obo/UBERON_0002048"``).
      * ``tail_type`` is ALWAYS ``"Anatomy"``.
      * ``relation`` is ALWAYS ``"expressed_in"``.

    An edge ``Protein P → expressed_in → Anatomy A`` is emitted when GEO
    records show mRNA abundance for the gene encoding P in samples
    derived from tissue A, with expression value above
    ``expression_threshold`` (default log2 = 4.0, ~16 TPM) OR a
    differential-expression call significant at FDR < 0.05.

    Fields
    ------
    head : str
        Canonical Protein node ID (UniProt accession).
    head_type : str
        Always "Protein".
    tail : str
        Canonical Anatomy node ID (UBERON URI).
    tail_type : str
        Always "Anatomy".
    relation : str
        Always "expressed_in".
    evidence_strength : str
        "strong" | "moderate" | "weak" — from EVIDENCE_STRENGTH_BY_EDGE_TYPE.
    expression_value : float
        The expression value backing this edge (max across aggregated records).
    n_samples : int
        Number of samples that supported this edge.
    n_series : int
        Number of GEO series that produced this edge (for aggregation).
    fdr : Optional[float]
        False discovery rate from BH correction (if differential).
    sensitive : bool
        True if any backing record was tagged sensitive (GEO-9.1).
    _edge_sha256 : str
        sha256(head|tail|relation|source|source_version) for dedup.

    Fixes: GEO-2.3 (edge schema), GEO-3.2 (expressed_in semantics),
           GEO-3.5 (differential expression), GEO-3.7 (BH-FDR),
           GEO-5.11 (deduplication), GEO-7.7 (deterministic ordering),
           GEO-9.11 (sensitive flag), GEO-16.9 (source_series on edge),
           GEO-16.12 (edge checksum).
    """

    # ── Edge triple ────────────────────────────────────────────────────
    # v35 ROOT FIX (V35-P2-LOADERS-FIXES H-5): standard kg_builder
    # schema keys are the PRIMARY form. The legacy ``head``/``tail``/
    # ``relation`` aliases are kept for backwards compatibility with
    # the dedup helper ``_build_edge_sha256`` and any external consumer
    # still reading the old keys.
    src_id: str             # Protein node ID (UniProt accession)
    src_type: str           # always "Protein"
    dst_id: str             # Anatomy node ID (UBERON URI)
    dst_type: str           # always "Anatomy"
    rel_type: str           # always "expressed_in"
    # Legacy aliases (kept for backwards compatibility).
    head: str                # alias for src_id
    head_type: str           # alias for src_type, always "Protein"
    tail: str                # alias for dst_id
    tail_type: str           # alias for dst_type, always "Anatomy"
    relation: str            # alias for rel_type

    # ── Evidence ───────────────────────────────────────────────────────
    evidence_strength: str   # "strong" | "moderate" | "weak"
    expression_value: float  # the expression value backing this edge
    n_samples: int           # number of samples that supported this edge
    n_series: int            # number of GEO series that produced this edge
    fdr: Optional[float]     # BH-corrected FDR (if differential)

    # ── Safety / Privacy (GEO-9.11) ────────────────────────────────────
    sensitive: bool          # True if any backing record was sensitive

    # ── Provenance (R15) ───────────────────────────────────────────────
    _source: str
    _source_version: str
    _source_url: str
    _source_release_date: str
    _license: str
    _attribution: str
    _schema_version: str
    _ingested_at: str
    _pipeline_version: str
    _input_sha256: str
    _source_series: str
    _parser_version: str

    # ── Lineage checksum (GEO-16.12) ───────────────────────────────────
    _edge_sha256: str        # sha256(head|tail|relation|source|source_version)


class GeoLoaderMetrics(TypedDict, total=False):
    """Runtime metrics container returned by ``GeoLoader.parse`` etc.

    A TypedDict mirror of the ``_GeoLoaderMetricsDataclass`` in
    ``geo_loader.py``. Holds counts (records parsed, dropped,
    dead-lettered, edges emitted, edges deduplicated), file metadata
    (path, size, sha256), and timing (duration_ms) for every parse /
    edge-conversion run.

    The metrics dict is JSON-serialisable so it can be:
      * Logged to ``logs/geo_metrics.jsonl`` (GEO-11.5).
      * Sent to MLflow via ``mlflow_tracker.log_metrics`` (GEO-11.5).
      * Emitted to Prometheus as counters (GEO-11.5, optional).
      * Stored in the run registry for audit purposes (GEO-16.6).

    Fields
    ------
    series_id : str
        The series ID that was parsed.
    file_path : str
        Path to the SOFT file (POSIX format).
    file_size_bytes : int
        Size of the SOFT file in bytes.
    file_sha256 : str
        SHA-256 of the SOFT file.
    records_parsed : int
        Number of GeoRawRecords emitted.
    records_dropped : int
        Number of records dropped (NaN, unresolvable, etc.).
    records_dead_lettered : int
        Number of records written to the dead-letter queue.
    edges_emitted : int
        Number of GeoEdgeRecords emitted (pre-dedup).
    edges_deduplicated : int
        Number of duplicate edges removed.
    duration_ms : int
        Wall-clock time of the parse / edge conversion in milliseconds.
    parser_version : str
        ``PARSER_VERSION`` of the loader that produced these metrics.
    schema_version : str
        ``SCHEMA_VERSION`` of the loader that produced these metrics.
    warnings : List[str]
        List of warning messages (rate-limited).
    errors : List[str]
        List of error messages (rate-limited).
    submission_date : Optional[str]
        ``!Series_submission_date`` from SOFT (GEO-16.7).
    last_update_date : Optional[str]
        ``!Series_last_update_date`` from SOFT (GEO-16.7).
    data_freshness_days : Optional[int]
        ``(now - submission_date).days`` (GEO-16.13).

    Fixes: GEO-11.5 (metrics emission), GEO-16.6 (audit trail),
           GEO-16.7 (dataset versioning), GEO-16.13 (freshness indicator).
    """

    series_id: str
    file_path: str
    file_size_bytes: int
    file_sha256: str
    records_parsed: int
    records_dropped: int
    records_dead_lettered: int
    edges_emitted: int
    edges_deduplicated: int
    duration_ms: int
    parser_version: str
    schema_version: str
    warnings: List[str]
    errors: List[str]
    submission_date: Optional[str]
    last_update_date: Optional[str]
    data_freshness_days: Optional[int]


class GeoDeadLetterEntry(TypedDict, total=False):
    """A dead-letter entry written to ``data/dead_letter/geo_malformed.jsonl``.

    Each entry represents a single record that could not be processed
    (malformed SOFT line, unresolvable probe, unmappable tissue, NaN
    expression value, etc.). The full record content + the reason +
    context (line number, series ID, parser version) is preserved so an
    operator can forensic-inspect the failure later.

    Fields
    ------
    timestamp : str
        ISO-8601 UTC when the entry was written.
    series_id : str
        The series ID being processed when the failure occurred.
    line_number : Optional[int]
        1-indexed line number in the SOFT file (if applicable).
    reason : str
        Short reason code (e.g. "unresolvable_probe", "nan_expression").
    record : Dict[str, Any]
        The record content (or partial content) that failed.
    parser_version : str
        ``PARSER_VERSION`` of the loader that wrote this entry.

    Fixes: GEO-6.4 (dead-letter queue), GEO-11.6 (error context),
           GEO-16.3 (transformation log).
    """

    timestamp: str
    series_id: str
    line_number: Optional[int]
    reason: str
    record: Dict[str, Any]
    parser_version: str


class GeoValidationReport(TypedDict, total=False):
    """Validation report returned by ``validate_geo_record`` / ``validate_geo_edge``.

    Aggregates the results of validating a list of records / edges
    against the ``GeoRawRecord`` / ``GeoEdgeRecord`` schemas. Includes
    per-record error messages so an operator can see exactly which
    records failed validation and why.

    Fields
    ------
    is_valid : bool
        True if all records/edges passed validation.
    n_total : int
        Total number of records/edges validated.
    n_valid : int
        Number of records/edges that passed validation.
    n_invalid : int
        Number of records/edges that failed validation.
    errors : List[Dict[str, Any]]
        List of error dicts, one per failed record/edge.
        Each dict has: {"index": int, "record": dict, "errors": [str, ...]}.
    parser_version : str
        ``PARSER_VERSION`` of the loader that produced this report.

    Fixes: GEO-2.2 (validate_geo_record), GEO-2.3 (validate_geo_edge),
           GEO-5.1 (data quality checks), GEO-10.4 (assertion quality).
    """

    is_valid: bool
    n_total: int
    n_valid: int
    n_invalid: int
    errors: List[Dict[str, Any]]
    parser_version: str


# Shape mirrors CLINICALTRIALS_PROVENANCE_KEYS / OPENTARGETS_PROVENANCE_KEYS
# / SIDER_PROVENANCE_KEYS / STITCH_PROVENANCE_KEYS but with GEO-specific
# extras:
#   * ``source_series``     — the GSE accession that produced this record
#   * ``input_sha256``      — sha256 of the SOFT file (streamed, 64KB chunks)
#   * ``parser_version``    — geo_loader.PARSER_VERSION ("1.0.0")
#   * ``schema_version``    — "GEO-SOFT-2.0"
#   * ``sample_taxid``      — NCBI Taxonomy ID (organism filter audit)
#   * ``tissue_uberon``     — UBERON URI after ontology mapping
#   * ``is_differential``   — whether the record came from a DE call
#   * ``fdr``               — BH-corrected FDR (if differential)
#   * ``batch_corrected``   — always False in v1.0.0 (GEO-3.6)
#   * ``sensitive``         — True if PII fields detected (GEO-9.1)
#   * ``n_records_in``      — count before filtering
#   * ``n_records_out``     — count after filtering
#   * ``n_dead_letter``     — DLQ count for this run
#   * ``crosswalk_version`` — VERIFIED_UNIPROT_GENE_CROSSWALK version
# ======================================================================
# Negative Sampling TypedDict (Fix 1.2)
# ======================================================================

class NegativeSampleDict(TypedDict, total=False):
    """TypedDict schema for negative sample output dicts.

    Produced by ``drugos_graph.negative_sampling.NegativeSampler``.

    Required fields:
      drug_id (str)        — Compound entity ID from the KG
      disease_id (str)     — Disease entity ID from the KG
      strategy (str)        — One of: "random", "wrong_class", "failed_phase3"
      confidence (float)    — Estimated P(true negative), range [0.3, 0.9]
      evidence_type (str)   — "absence_of_evidence" | "mechanistic_mismatch"
                              | "clinical_failure"

    Optional fields:
      nct_id (str)               — ClinicalTrials.gov ID (failed_phase3 only)
      trial_status (str)         — Trial status (failed_phase3 only)
      atc_class_known (str)      — Drug's known ATC class (wrong_class only)
      atc_class_sampled (str)    — Disease's ATC class (wrong_class only)
      _provenance (dict)         — Lineage metadata
      _schema_version (str)      — Schema version "2.1.0"

    Fixes: D1.2 (schema contract), D3.4 (evidence_type), D16.1 (provenance),
           D13.3 (confidence documentation).
    """

    drug_id: str
    disease_id: str
    strategy: str
    confidence: float
    evidence_type: str
    nct_id: Optional[str]
    trial_status: Optional[str]
    atc_class_known: Optional[str]
    atc_class_sampled: Optional[str]
    _provenance: Optional[Dict[str, Any]]
    _schema_version: Optional[str]


NEGATIVE_SAMPLE_REQUIRED_KEYS: tuple[str, ...] = (
    "drug_id", "disease_id", "strategy", "confidence", "evidence_type",
)

NEGATIVE_SAMPLE_OPTIONAL_KEYS: tuple[str, ...] = (
    "nct_id", "trial_status", "atc_class_known", "atc_class_sampled",
    "_provenance", "_schema_version",
)

NEGATIVE_SAMPLING_PROVENANCE_KEYS: tuple[str, ...] = (
    "generated_at",
    "generator_version",
    "schema_version",
    "pipeline_version",
    "package_version",
    "seed",
    "source_data_version",
    "generation_seed",
    "strategy",
    "strategy_params",
)

GEO_PROVENANCE_KEYS: tuple[str, ...] = (
    "source",
    "source_file",
    "source_sha256",
    "source_version",
    "source_release_date",
    "source_license",
    "source_url",
    "parser_module",
    "parser_version",
    "schema_version",
    "parsed_at",
    "source_series",
    "input_sha256",
    "sample_taxid",
    "tissue_uberon",
    "is_differential",
    "fdr",
    "batch_corrected",
    "sensitive",
    "n_records_in",
    "n_records_out",
    "n_dead_letter",
    "crosswalk_version",
)
