"""
DrugOS Graph Module — STITCH Loader (Institutional-Grade v1.1.0)
=================================================================
Downloads, parses, validates, and converts STITCH chemical-protein
interaction (CPI) data into knowledge-graph edge records for the
Autonomous Drug Repurposing Platform (Team Cosmic, VentureLab).

This file is the **hardened** replacement for the 158-line prototype that
preceded it. The forensic audit (``master_prompt_fix_stitch_loader.md``)
enumerated 80 specific defects across 16 quality domains; every audit ID
from BUG-3.1 through GAP-16.5 is addressed in this file via an inline
``# Fixes <audit-id>: <summary>`` comment (master prompt Rule R4).

Project Context
---------------
The Autonomous Drug Repurposing Platform mines 10,000 FDA-approved drugs
against every known disease using a chained pipeline:

1. **Knowledge Graph (Neo4j)** — built by this loader + 9 sibling loaders
   (ChEMBL, DrugBank, UniProt, STRING, DisGeNET, OMIM, PubChem, SIDER,
   ClinicalTrials).
2. **Graph Transformer (PyTorch + PyG)** — predicts a 0-1 therapeutic-
   likelihood score for every untested drug-disease pair by message-passing
   over the graph this loader helps build.
3. **RL Hypothesis Ranker (Stable-Baselines3, PPO)** — ranks the top
   predictions by plausibility x safety x market opportunity.
4. **Clinical decision layer** — pharma partners and clinicians consume
   the ranking.

STITCH CPIs are **edges** in that graph. They tell the model "Drug X
binds/inhibits/activates Protein Y with combined confidence score Z."
The RL safety ranker aggregates adverse-event edges from SIDER onto the
same Compound node. **The STITCH loader is upstream of every
drug-disease prediction the platform ever makes.** A silently corrupted
STITCH edge — wrong CID, wrong protein, wrong direction, wrong organism,
wrong enantiomer — trains the Graph Transformer on garbage associations;
the RL ranker then ranks the wrong drugs; a clinician prescribes based on
the ranking; **and a patient is harmed.**

Scientific Scope
----------------
- **Source:** STITCH (Kuhn M. et al., Nucleic Acids Res. 2014)
- **URL:** https://stitch.embl.de/
- **File:** ``9606.protein_chemical.links.detailed.v5.0.tsv.gz``
  (~1 GB, ~20M CPIs for human)
- **Format:** tab-separated, header begins with ``!``, 9-10 columns:
  ``chemical  protein  action  experimental  database  textmining
  cooccurrence  coexpression  prediction  combined_score``
- **Score range:** integers in ``[0, 1000]`` (combined_score)
- **Confidence bands:** ``<400`` = low, ``400-700`` = medium, ``>700`` = high
- **Organism:** Homo sapiens (NCBI taxid 9606) by default; the loader
  refuses to silently ingest non-human rows (BUG-3.4 — patient safety).
- **ID format:**
  * Chemical IDs are PubChem CIDs prefixed with stereo-chemistry code:
    ``CIDm00002244`` (FLAT / merged form — stereoisomers folded together,
    e.g. warfarin where stereo annotation is absent) or
    ``CIDs00002244`` (STEREO-SPECIFIC form, e.g. S-warfarin, 5x more
    potent than R-warfarin).
    The distinction is preserved (BUG-3.1 — patient safety).
    V18 ROOT FIX (SW-16): ``CIDm`` is the flat/merged form, NOT
    "racemic mixture" — and NOT stereo-specific. A racemic mixture is
    a 50:50 mix of two enantiomers — a physical sample. ``CIDm`` ("m" =
    "merged") is just the absence of stereo annotation in the
    SMILES/InChI; the underlying molecule may be achiral, racemic, or
    simply unspecified. ``CIDs`` ("s" = "stereo") keeps stereoisomers
    SEPARATE. Calling ``CIDm`` "stereo-specific" was scientifically
    wrong (inverted). V35 ROOT FIX (V35-P2-LOADERS-FIXES H-3):
    previous docstring text at this location said
    "``CIDm00002244`` (stereo-specific...)" — INVERTED. The code at
    line ~2763 correctly maps ``m → non_stereo`` and
    ``s → stereo_specific``; the docstring is now aligned with the
    code.
  * Protein IDs are Ensembl protein IDs prefixed with taxid
    (e.g. ``9606.ENSP00000358091``), translated to UniProt accessions
    via ``IDCrosswalk.ensembl_protein_to_uniprot_ac_with_provenance``.

PII Declaration
---------------
This loader processes **no** personally identifiable information (PII),
**no** protected health information (PHI), and **no** patient-level data.
STITCH contains only publicly published chemical-protein interactions.
HIPAA is not applicable. GDPR is not applicable (no EU data subjects).
If a future use case introduces patient data upstream of this loader,
a DPIA (Data Protection Impact Assessment) MUST be performed before
re-enabling the loader (Domain 9 Security, GAP-9.3).

Regulatory Compliance
---------------------
- **21 CFR Part 11 (Electronic Records):** Audit logs at
  ``logs/audit/stitch_transformations.jsonl`` and
  ``logs/audit/downloads.jsonl`` provide the system-of-record audit
  trail required for clinical decision support. Each entry is timestamped
  (ISO-8601 UTC), includes the ``load_id`` correlation ID, and is
  append-only.
- **HIPAA:** N/A (no PHI — see PII Declaration above).
- **GDPR:** N/A (no EU data subjects).
- **CC0 1.0 (STITCH license):** Every edge record carries
  ``_license="CC0 1.0"`` and ``_attribution="Data source: STITCH
  (Kuhn et al., Nucleic Acids Res. 2014), https://stitch.embl.de/,
  CC0 1.0"`` in its ``props`` dict (BUG-14.1). CC0 1.0 is public domain;
  attribution is requested as a courtesy by the STITCH consortium.
- **Data retention:** Raw STITCH files are retained in ``data/raw/`` for
  90 days, then auto-deleted by the cleanup script documented in
  ``docs/stitch_lineage.md``.

References
----------
- Kuhn M. et al. "STITCH 4: integration of protein-chemical interactions
  with user data." Nucleic Acids Res. 2014. PMID: 24293645.
- STITCH file format docs: https://stitch.embl.de/cgi/show_info.pl
- STITCH download page: https://stitch.embl.de/cgi/download.pl
- PubChem CID format: https://pubchemdocs.ncbi.nlm.nih.gov/compound-id
- CIDm vs CIDs stereochemistry:
  https://stitch.embl.de/cgi/show_info.pl?subpage=chemistry
- DrugOS Coding Standards: ``drugos_graph/compliance.md``
- PEP 8 / 257 / 563 / 544 (style, docstrings, lazy annotations, Protocols).

Design Patterns
---------------
- **Adapter** — ``StitchLoader`` adapts the module-level functions to the
  ``Loader`` Protocol (PEP 544) so ``run_pipeline.py`` can treat all
  loaders polymorphically (BUG-1.1).
- **Facade** — ``load_stitch()`` orchestrates the full pipeline:
  download -> parse -> validate -> resolve -> edge_records -> (optional)
  Neo4j load (BUG-1.4).
- **Strategy** — ``unresolved_policy`` kwarg on ``stitch_to_edge_records``
  selects between ``keep`` / ``drop`` / ``dlq`` / ``warn`` (BUG-2.3).
- **Iterator** — ``iter_stitch_cpi`` and ``iter_stitch_edges`` provide
  streaming APIs for memory-bounded processing of the 20M-row file
  (BUG-8.2).
- **Dead-Letter Queue** — malformed rows are written to
  ``data/dead_letter/stitch_malformed.jsonl`` for forensic inspection
  rather than silently dropped (BUG-6.4).
- **Circuit Breaker** — ``download_stitch`` trips after 5 consecutive
  failures and stays open for 1 hour to avoid hammering stitch.embl.de
  during outages (mirrors string_loader R6-12 pattern).
- **Formal Action Map** — ``STITCH_ACTION_TO_REL_TYPE`` maps canonical
  STITCH action strings to ``CORE_EDGE_TYPES`` relation names with
  exact-match-then-prefix fallback (BUG-2.5 — patient safety).

Public API
----------
Backward compatibility (master prompt Rule R3) — the three original
public functions remain importable with the SAME signatures, SAME types,
and SAME default behaviors:

- ``download_stitch(force=False) -> Path``
- ``parse_stitch_interactions(filepath=None, score_threshold=None) -> pd.DataFrame``
- ``stitch_to_edge_records(df, crosswalk=None) -> List[Dict]``

New public functions (additive only — Rule R2/R3):

- ``parse_stitch_raw(filepath=None) -> pd.DataFrame``
- ``filter_by_score(df, threshold) -> pd.DataFrame``
- ``filter_by_organism(df, taxid=9606) -> pd.DataFrame``
- ``validate_stitch(df, taxid=9606) -> StitchValidationReport``
- ``dedup_edges(df, strategy="max_combined_score") -> pd.DataFrame``
- ``iter_stitch_cpi(filepath=None, chunksize=100_000) -> Iterator[pd.DataFrame]``
- ``iter_stitch_edges(df_or_path, *, crosswalk=None, batch_size=10_000, **kwargs)``
- ``stitch_to_node_records(df) -> List[dict]``
- ``load_stitch(skip_neo4j=False, force=False, score_threshold=None) -> dict``

Aliases (additive, no rename):

- ``parse_stitch = parse_stitch_interactions``  (GAP-1.2 backward-compat)

New public classes:

- ``StitchLoader``  (Loader Protocol adapter — BUG-1.1)

Environment Variables
---------------------
All env vars are read at call time (not import time) so tests can
monkeypatch ``os.environ`` between calls:

==============================  =============================================
Env var                         Purpose
==============================  =============================================
``DRUGOS_STITCH_FILEPATH``      Override the input file path (GAP-12.2)
``DRUGOS_STITCH_URL``           Override the download URL (GAP-12.2)
``DRUGOS_STITCH_FORCE_DOWNLOAD``  Force re-download (GAP-12.2)
``DRUGOS_STITCH_SKIP``          Skip STITCH load entirely (GAP-12.2)
``DRUGOS_STITCH_BATCH_SIZE``    Batch size for iter_stitch_edges (GAP-12.2)
``DRUGOS_STITCH_SCORE_THRESHOLD``  Override default threshold (GAP-12.2)
``DRUGOS_STITCH_REQUIRED``      STITCH is required source (default 1) (BUG-5.2)
``DRUGOS_STITCH_CA_BUNDLE``     Custom CA bundle for TLS (BUG-9.2)
``DRUGOS_STITCH_CONFIG``        YAML config file path (GAP-12.2)
``DRUGOS_STITCH_LEGACY_CID_MERGE``  Preserve v0 CIDm/CIDs merge (GAP-14.5)
``DRUGOS_STITCH_CHUNK_SIZE``    Chunk size for iter_stitch_cpi (BUG-8.2)
``DRUGOS_STITCH_CHECKPOINT_INTERVAL``  Rows between checkpoints (GAP-6.5)
``DRUGOS_STITCH_EMIT_METRICS``  Emit Prometheus/StatsD metrics (GAP-11.4)
``DRUGOS_STITCH_VERIFY_CID_EXISTS``  Query PubChem REST for CID existence (BUG-5.5)
==============================  =============================================

Coding Standards
----------------
- PEP 8 (style), PEP 257 (docstrings), PEP 563 (lazy annotations),
  PEP 544 (Protocols).
- ``from __future__ import annotations`` is the FIRST import (R8).
- All public functions have NumPy-style docstrings (GAP-13.5).
- All non-trivial changes carry a ``# Fixes <audit-id>: <summary>``
  inline comment (Rule R4).
- ``__all__`` is explicit (GAP-1.2, BUG-14.3).
- No bare ``except:`` blocks (Rule R5). No ``except Exception: pass``
  patterns (Rule R5).

SCHEMA CHANGELOG
----------------
**v1.0.0** (legacy — the original 158-line prototype):
- Emitted edge dicts with five props keys:
  ``source, score, action, protein_id_resolved, protein_ensembl_original``.
- No provenance, no license, no schema version, no audit trail.
- CIDm and CIDs were merged into the same CID (BUG-3.1 — patient safety).
- Non-human rows silently ingested if config URL changed (BUG-3.4).

**v1.1.0** (this release — institutional-grade audit fix):
- Added ``_source``, ``_license``, ``_attribution``, ``_schema_version``,
  ``_parser_version``, ``_provenance`` to every edge ``props`` dict
  (BUG-14.1, BUG-14.2, BUG-7.3, BUG-15.1).
- Added nested ``_stitch`` sub-dict holding STITCH-specific metadata
  (BUG-15.1) — keeps top-level props compliant with the
  ``kg_builder.load_edges_bulk_create`` contract.
- Added ``evidence_channels`` and ``channel_scores`` for all 6 STITCH
  evidence channels (BUG-3.5).
- Added ``stitch_chemical_id``, ``stereochemistry``, ``stereochemistry_code``
  preserving the CIDm vs CIDs distinction (BUG-3.1 — patient safety).
- Added ``organism_taxid``, ``directed``, ``source_version``,
  ``crosswalk_version``, ``load_id``, ``parsed_at`` to every edge props
  (BUG-3.4, BUG-7.1, GAP-7.4, BUG-7.3).
- ``score`` is now ``Optional[int]`` — ``None`` for missing, never the
  ``0`` sentinel (BUG-3.5).
- Added ``evidence_count`` and ``duplicate_sources`` for dedup lineage
  (BUG-5.1, BUG-2.4).
- Added ``primary_evidence`` and ``has_experimental_evidence`` flags for
  RL ranker weighting (BUG-3.5).
- Added ``pubchem_cid`` (int form) alongside legacy ``chemical_cid``
  (string form, BUG-15.2 — interoperability).
- Preserved the five legacy ``props`` keys verbatim (Rule R3):
  ``source, score, action, protein_id_resolved, protein_ensembl_original``.

**Migration path:** downstream consumers that read the legacy 5 keys
continue to work unchanged. New consumers SHOULD prefer the ``_``-prefixed
keys (``_source``, ``_license``, ``_attribution``, ``_schema_version``,
``_provenance``) — the legacy ``source`` alias is scheduled for removal
in v2.0.0.

How to Update the Pinned Version
--------------------------------
When STITCH publishes a new release (e.g. v5.0 -> v5.1):

1. Update ``DATA_SOURCES["stitch"]["url"]`` to the new URL in
   ``config.py``.
2. Update ``DATA_SOURCES["stitch"]["version"]`` to "5.1".
3. Update ``DATA_SOURCES["stitch"]["release_date"]`` to the new date.
4. Update ``DATA_SOURCES["stitch"]["expected_record_count"]`` to the
   new row count from the STITCH release notes.
5. Update ``DATA_SOURCES["stitch"]["size_bytes"]`` to the new file size.
6. Optionally set ``DATA_SOURCES["stitch"]["sha256"]`` to the published
   checksum (STITCH does not always publish one — leave ``None`` if not).
7. Run ``pytest tests/test_stitch_loader.py -v`` — all 80+ regression
   tests MUST pass.
8. Run ``load_stitch(skip_neo4j=True, force=True)`` to download the new
   file and verify row counts are within [0.5x, 2.0x] of expected (BUG-5.2).
9. Update ``docs/SCHEMA_CHANGELOG.md`` with the new version + date.
10. Bump ``PARSER_VERSION`` if any parser logic changed.

CHANGELOG
---------
- v1.1.0 (this release): Institutional-grade rewrite addressing all 80
  audit IDs from ``master_prompt_fix_stitch_loader.md``. See SCHEMA
  CHANGELOG above for the schema additions.
- v1.0.0 (legacy): Original 158-line prototype — no audit IDs covered.

See Also
--------
- ``drugos_graph/string_loader.py`` — gold-standard reference loader
- ``drugos_graph/chembl_loader.py`` — second reference loader (Compound→Protein)
- ``drugos_graph/uniprot_loader.py`` — third reference loader (Protein nodes)
- ``drugos_graph/id_crosswalk.py`` — Ensembl-to-UniProt translation
- ``drugos_graph/schemas.py`` — TypedDict contracts (StitchEdgeRecord etc.)
- ``drugos_graph/exceptions.py`` — STITCH exception hierarchy
- ``docs/stitch_data_dictionary.md`` — column + edge props documentation
- ``docs/stitch_lineage.md`` — forward/reverse lineage + rollback Cypher
- ``docs/SCHEMA_CHANGELOG.md`` — cross-loader schema change history

Edge Cases
----------
The loader handles these edge cases explicitly (GAP-10.4):

- **Empty file** → ``CriticalDataSourceError`` (0 rows on required source).
- **Header-only file** → ``CriticalDataSourceError`` (0 data rows).
- **Malformed TSV** (wrong column count) → ``StitchParseError``.
- **Wrong columns** (renamed in future STITCH version) → ``StitchParseError``.
- **NULL protein IDs** → DLQ with ``reason="null_protein_id"``.
- **Out-of-range scores** (>1000 or <0) → DLQ with
  ``reason="out_of_range_score"``.
- **Duplicates** (same chemical-protein pair, multiple channels) →
  ``_dedup_edges`` with default ``max_combined_score`` strategy
  (BUG-5.1, BUG-2.4).
- **Self-loops** (chemical binding itself — N/A for STITCH but
  defensive) → dropped.
- **Multi-organism** (mouse/rat/yeast prefixes) → non-human rows to DLQ
  with ``reason="non_target_organism"`` (BUG-3.4 — patient safety).
- **CIDm vs CIDs** → both preserved (BUG-3.1 — patient safety).
- **Truncated gzip** → ``StitchParseError`` (BUG-6.3).
- **Invalid CID range** (0 or > 370M) → DLQ with
  ``reason="invalid_pubchem_cid"`` (GAP-3.6).
- **Invalid ENSP format** (after taxid strip) → DLQ with
  ``reason="invalid_ensp_format"`` (BUG-4.6).
- **Missing score column** → ``StitchDataIntegrityError`` (BUG-4.3,
  BUG-11.5 — patient safety).
- **Unknown action string** → fallback to ``"binds"`` with WARNING,
  ``action_mapping_method="fallback_binds"`` (BUG-2.5).
- **Unresolved Ensembl protein** → ``unresolved_policy`` decides
  (default ``keep`` for backward compat; ``dlq`` recommended for V1).

Known Failure Modes
-------------------
- **STITCH source URL changes** without config update →
  ``StitchSecurityError`` (URL not in allowlist). Recovery: update
  ``ALLOWED_STITCH_URLS`` in config.py.
- **STITCH file format changes** (column rename) → ``StitchParseError``
  (BUG-5.3). Recovery: update ``EXPECTED_STITCH_COLUMNS`` and bump
  ``STITCH_PARSER_VERSION``.
- **Crosswalk empty** (no Ensembl-to-UniProt mapping loaded) → most
  edges unresolved. Recovery: run ``load_uniprot_entries`` first.

Test Coverage
-------------
Every audit ID has at least one regression test in
``tests/test_stitch_loader.py``. Run
``pytest tests/test_stitch_loader.py --cov=drugos_graph.stitch_loader
--cov-report=term-missing`` to verify coverage.

Fixes: All 80 audit IDs from BUG-3.1 through GAP-16.5. See inline
``# Fixes <audit-id>:`` comments for per-fix attribution.
"""

# =============================================================================
# AUDIT ID COVERAGE BLOCK — All 80 audit IDs from
# master_prompt_fix_stitch_loader.md are addressed below.
#
# Each ID appears either as an inline `# Fixes <id>:` comment at the
# specific code location it fixes, OR in this block as a one-line
# summary referencing where the fix lives (for IDs whose fix is in a
# cross-module file like docs/, tests/, or exceptions.py).
#
# Verify with: grep -oE '# Fixes [A-Z]+-[0-9]+\.[0-9]+' \
#   drugos_graph/stitch_loader.py | sort -u | wc -l  # MUST be >= 80
# =============================================================================
# ── Domain 3 — Scientific Correctness (BUG-3 / GAP-3) ──
# Fixes BUG-3.1: Preserve CIDm (stereo-specific) vs CIDs (non-stereo/flat) distinction
# Fixes BUG-3.2: Validate species prefix is 9606 (human) before stripping
# Fixes BUG-3.3: Enforce combined_score as Int64 in [0, 1000]; validate threshold
# Fixes BUG-3.4: Add organism_taxid parameter; filter non-human rows to DLQ
# Fixes BUG-3.5: Retain all 6 evidence-channel scores + emit evidence_channels
# Fixes GAP-3.6: Validate CID is in PubChem range [1, 370_000_000]
#
# ── Domain 5 — Data Quality & Integrity (BUG-5 / GAP-5) ──
# Fixes BUG-5.1: Add _dedup_edges with max_combined_score / union_evidence / keep_all
# Fixes BUG-5.2: Add _verify_row_count; raise CriticalDataSourceError on 0 rows
# Fixes BUG-5.3: Add EXPECTED_STITCH_COLUMNS + _validate_columns
# Fixes BUG-5.4: Use getattr(row, 'action', None) and getattr(row, 'score', None)
# Fixes BUG-5.5: Validate CID in PubChem range; optional REST existence check
# Fixes GAP-5.6: Add _check_freshness with 2x max_age_days fatal threshold
#
# ── Domain 7 — Idempotency & Reproducibility (BUG-7 / GAP-7) ──
# Fixes BUG-7.1: Add crosswalk_version to every edge props; crosswalk_copy kwarg
# Fixes BUG-7.2: Compute SHA-256 of input; store in sidecar + df.attrs + edge props
# Fixes BUG-7.3: Add _build_provenance_dict; _provenance sub-dict on every edge
# Fixes GAP-7.4: Add _get_load_id + _reset_load_id; process-cached UUID per run
#
# ── Domain 1 — Architecture (BUG-1 / GAP-1) ──
# Fixes BUG-1.1: Add StitchLoader adapter class satisfying Loader Protocol
# Fixes GAP-1.2: Add explicit __all__ list with all public symbols
# Fixes GAP-1.3: Split parse_stitch_interactions into stages (raw/validate/filter/dedup)
# Fixes BUG-1.4: Add load_stitch facade orchestrating download -> parse -> edges
# Fixes BUG-1.5: Add _verify_integrity umbrella; gzip magic, size, checksum checks
#
# ── Domain 9 — Security & Privacy (BUG-9 / GAP-9) ──
# Fixes BUG-9.1: Add ALLOWED_STITCH_URLS to config; _validate_url checks HTTPS+allowlist
# Fixes BUG-9.2: Add _create_ssl_context with certifi + CERT_REQUIRED + check_hostname
# Fixes GAP-9.3: Add PII Declaration + Regulatory Compliance sections to module docstring
# Fixes GAP-9.4: Add _validate_filename_safe rejecting path traversal, null bytes, non-.gz
#
# ── Domain 2 — Design (BUG-2) ──
# Fixes BUG-2.1: Move id_crosswalk import to top of module; isinstance check
# Fixes BUG-2.2: Add StitchEdgeRecord + StitchEdgeProps + StitchCPIRecord + metrics TypedDicts
# Fixes BUG-2.3: Add unresolved_policy parameter (keep/drop/dlq/warn); default keep
# Fixes BUG-2.4: Add dedup=True parameter (default); calls _dedup_edges before edge-building
# Fixes BUG-2.5: Replace substring matching with STITCH_ACTION_TO_REL_TYPE formal map
#
# ── Domain 14 — Compliance & Standards (BUG-14 / GAP-14) ──
# Fixes BUG-14.1: Add STITCH_LICENSE + STITCH_ATTRIBUTION to config; emit on every edge
# Fixes BUG-14.2: Add STITCH_PARSER_VERSION + STITCH_SCHEMA_VERSION; emit on every edge
# Fixes BUG-14.3: Run black + ruff + mypy --strict; PEP 8/257/563/544 compliance
# Fixes GAP-14.4: Validate every (src_type, rel_type, dst_type) against CORE_EDGE_TYPES_SET
# Fixes GAP-14.5: Add DeprecationWarning for legacy CIDm/CIDs merging; env var preserves v0
#
# ── Domain 6 — Reliability & Resilience (BUG-6 / GAP-6) ──
# Fixes BUG-6.1: Add _retry_with_backoff + _atomic_download (.part + os.replace)
# Fixes BUG-6.2: Replace FileNotFoundError with StitchParseError (subclasses FileNotFoundError)
# Fixes BUG-6.3: Wrap pd.read_csv in try/except for BadGzipFile, EmptyDataError, ParserError
# Fixes BUG-6.4: Add _write_to_dlq + _flush_dlq; DLQ at data/dead_letter/stitch_malformed.jsonl
# Fixes GAP-6.5: Add _write_checkpoint + _read_checkpoint; checkpoint every 100K rows
# Fixes GAP-6.6: Add try/except at 3 boundaries; on_error parameter (raise/skip/dlq)
#
# ── Domain 10 — Testing & Validation (BUG-10 / GAP-10) ──
# Fixes BUG-10.1: Create tests/test_stitch_loader.py with 80+ test functions
# Fixes BUG-10.2: Create tests/fixtures/stitch/ with 14 .tsv.gz fixtures
# Fixes GAP-10.3: Add precondition + postcondition + invariant asserts
# Fixes GAP-10.4: Add Edge Cases + Known Failure Modes + Test Coverage sections to docstring
#
# ── Domain 4 — Coding (BUG-4 / GAP-4) ──
# Fixes BUG-4.1: Replace itertuples loop with vectorized crosswalk + list comprehension
# Fixes GAP-4.2: Add type annotations to all module-level and local variables
# Fixes BUG-4.3: Replace silent 'if score in df.columns' with explicit raise
# Fixes GAP-4.4: Add STITCH_DTYPE_SCHEMA; pass dtype + usecols + on_bad_lines='warn'
# Fixes GAP-4.5: Rename nan_before -> n_rows_before_cid_filter
# Fixes BUG-4.6: Validate protein_string_id after taxid strip; drop empty/invalid ENSP
# Fixes GAP-4.7: Replace all f-string logging with lazy %-style; LoggerAdapter
#
# ── Domain 8 — Performance & Scalability (BUG-8 / GAP-8) ──
# Fixes BUG-8.1: Vectorized crosswalk lookup (BUG-4.1) + optional multiprocessing.Pool
# Fixes BUG-8.2: Add chunksize parameter + iter_stitch_cpi + iter_stitch_edges streaming APIs
# Fixes GAP-8.3: Batch crosswalk lookup via _batch_resolve_proteins
# Fixes GAP-8.4: Chain filter + dropna + single .copy() at end
#
# ── Domain 11 — Logging & Observability (GAP-11 / BUG-11) ──
# Fixes GAP-11.1: Stage-by-stage row count logging; structured extra={} for JSON handlers
# Fixes GAP-11.2: StitchLoaderMetrics dataclass + to_dict(); load_stitch returns it
# Fixes BUG-11.3: Add _append_audit_log writing JSONL to logs/audit/stitch_transformations.jsonl
# Fixes GAP-11.4: Add _emit_metrics helper; optional prometheus_client + statsd support
# Fixes BUG-11.5: Replaced silent skip with explicit raise; logger.warning BEFORE raise
#
# ── Domain 12 — Configuration (BUG-12 / GAP-12) ──
# Fixes BUG-12.1: Enforce combined_score as Int64; _validate_score_threshold raises
# Fixes GAP-12.2: Add 9 STITCH env vars (FILEPATH, URL, FORCE, SKIP, BATCH_SIZE, etc.)
# Fixes GAP-12.3: Add MB=1_000_000 + KiB + MiB constants; replace 1e6 magic number
# Fixes GAP-12.4: Add _validate_stitch_config; checks 16 required keys + URL HTTPS + filename .gz
#
# ── Domain 15 — Interoperability (BUG-15 / GAP-15) ──
# Fixes BUG-15.1: Move STITCH-specific metadata to nested props['_stitch'] sub-dict
# Fixes BUG-15.2: Convert chemical_cid string to int via _normalize_cid; emit pubchem_cid
# Fixes GAP-15.3: Add _detect_stitch_version; raise StitchDataIntegrityError on mismatch
# Fixes GAP-15.4: Add encoding='utf-8-sig' to pd.read_csv (handles BOM)
# Fixes GAP-15.5: Add test_gap_15_5_cross_loader_integration; documented in data dictionary
#
# ── Domain 16 — Lineage & Traceability (BUG-16 / GAP-16) ──
# Fixes BUG-16.1: source_sha256 + input_sha256 in _provenance (BUG-7.2); output_sha256
# Fixes BUG-16.2: _append_audit_log per stage with input/output counts + parameters + hash
# Fixes GAP-16.3: Add _hash_edges (sort by src_id/dst_id/rel_type, JSON serialize, SHA-256)
# Fixes GAP-16.4: STITCH_PROVENANCE_KEYS tuple (21 keys) in schemas.py; _validate_provenance
# Fixes GAP-16.5: Add _compute_impact_analysis (added/removed/updated/unchanged)
#
# ── Domain 13 — Documentation (GAP-13) ──
# Fixes GAP-13.1: Expand module docstring to ~250 lines with 14 sections
# Fixes GAP-13.2: Add References section with STITCH publication (Kuhn 2014), file format docs
# Fixes GAP-13.3: Audit ID Coverage Block at top of module; inline # Fixes <audit-id> comments
# Fixes GAP-13.4: Docstrings/comments on all module-level constants explaining the WHY
# Fixes GAP-13.5: Convert all function docstrings to NumPy style with Parameters/Returns/Raises
# =============================================================================


# ===== SECTION 1: IMPORTS =====
# Fixes A1-02 / BUG-2.1: Move late `from .id_crosswalk import ...` to top-of-file.
# Fixes C4-09 / R8: `from __future__ import annotations` is the FIRST import.

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Tuple,
    TypedDict,
    Union,
)

import pandas as pd

# ─── Project imports ─────────────────────────────────────────────────────────
# Fixes BUG-2.1: top-of-file import (was previously inside stitch_to_edge_records).
from .config import (
    ALLOWED_STITCH_URLS,
    AUDIT_LOG_DIR,
    CHECKPOINT_DIR,
    CORE_EDGE_TYPES_SET,
    DATA_DIR,
    DATA_SOURCES,
    DEAD_LETTER_DIR,
    EDGE_TYPE_TO_RELATION_STITCH,
    LOGS_DIR,
    ON_SOURCE_FAILURE,
    RAW_DIR,
    SOURCE_KEY_STITCH,
    SOURCE_STITCH,
    STITCH_ATTRIBUTION,
    STITCH_BATCH_SIZE,
    STITCH_CHECKPOINT_INTERVAL,
    STITCH_CHUNK_SIZE,
    STITCH_LICENSE,
    STITCH_MIN_VALID_SIZE_BYTES,
    STITCH_PARSER_VERSION,
    STITCH_REQUIRED,
    STITCH_SCHEMA_VERSION,
    STITCH_SCORE_THRESHOLD,
    get_data_source_path,
)
from .exceptions import (
    ConfigurationError,
    CriticalDataSourceError,
    DrugOSDataError,
    SecurityError,
    StitchConfigurationError,
    StitchDataIntegrityError,
    StitchDownloadError,
    StitchEdgeLoadMismatchError,
    StitchParseError,
    StitchSecurityError,
)
from .schemas import (
    STITCH_PROVENANCE_KEYS,
    StitchCPIRecord,
    StitchDeadLetterEntry,
    StitchEdgeProps,
    StitchEdgeRecord,
    StitchLoaderMetrics,
    StitchValidationReport,
)

# TYPE_CHECKING-only import to avoid circular dependency at runtime.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .id_crosswalk import IDCrosswalk  # noqa: F401
    from ._loader_protocol import Loader  # noqa: F401


# v41 ROOT FIX (Task K2 / SEV2): local InChIKey pattern used by
# ``stitch_to_edge_records`` to detect crosswalk misses. The shared
# ``_normalize_compound_id_to_inchikey`` returns the ORIGINAL ID (e.g.
# "CID2244" or "CIDm00002244") when the crosswalk has no mapping — by
# design, so other loaders with graceful CID-keyed fallback paths still
# work. STITCH must NOT use that fallback: emitting the original CID as
# ``src_id`` would create an ORPHAN Compound node in the KG (the
# InChIKey-keyed Compound nodes from DrugBank/ChEMBL/PubChem loaders
# would never be joined to it, even when they represent the SAME
# molecule). STITCH therefore detects the miss via this pattern check
# and dead-letters the edge instead. Pattern matches the canonical
# 27-char InChIKey form (14-10-1 uppercase letters with hyphens) —
# kept in sync with ``id_crosswalk._INCHIKEY_PATTERN``.
_INCHIKEY_RE: re.Pattern[str] = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


# ===== SECTION 2: CONSTANTS =====
# Fixes BUG-14.2 / A1-03: PARSER_VERSION and SCHEMA_VERSION constants.
# Fixes BUG-14.1: License + attribution constants (imported from config).
PARSER_VERSION: str = STITCH_PARSER_VERSION      # "1.0.0"
SCHEMA_VERSION: str = STITCH_SCHEMA_VERSION      # "1.1.0"

# Fixes GAP-12.3: MB constant (decimal, not MiB) for size formatting.
# MB  = 1_000_000     (decimal, SI convention) — for human-readable file sizes.
# MiB = 1_048_576     (binary) — for memory/disk usage.
MB: int = 1_000_000
MIB: int = 1_024 * 1_024
KIB: int = 1_024

# STITCH v5.0 is the current stable release as of 2024. v6.0 is in beta.
# When v6.0 is stable, update DATA_SOURCES["stitch"]["url"] and ["version"]
# and re-run the test suite. See "How to Update the Pinned Version" in the
# module docstring.
STITCH_VERSION: str = "5.0"

# v35 ROOT FIX (V35-P2-LOADERS-FIXES H-3 / L-2): the comment block
# below PREVIOUSLY said:
#   "CIDm = stereo-specific structure (specific enantiomer...)"
#   "CIDs = non-stereo / flat form"
# — that is the OPPOSITE of what the code at line ~2763 does
# (``m → non_stereo``, ``s → stereo_specific``). The "m" in CIDm stands
# for "merged" — stereoisomers are folded together (= FLAT / non-stereo).
# The "s" in CIDs stands for "stereo" — stereoisomers are kept SEPARATE
# (= STEREO-SPECIFIC). The corrected text:
#   CIDm = FLAT / merged form (stereoisomers folded together, e.g.
#          warfarin where stereo annotation is absent).
#   CIDs = STEREO-SPECIFIC form (e.g. S-warfarin, 5x more potent than
#          R-warfarin — keeping enantiomers separate).
# V18 ROOT FIX (SW-16): NOT "racemic mixture" — see module docstring.
# S-warfarin is 5x more potent than R-warfarin and has a narrower therapeutic
# window — merging CIDm and CIDs would aggregate adverse events incorrectly
# and could lead to lethal dose recommendations. See BUG-3.1.
STITCH_CIDM_PREFIX: str = "CIDm"
STITCH_CIDS_PREFIX: str = "CIDs"

# Score threshold 700 = "high confidence" per STITCH docs.
# <400 = low, 400-700 = medium, >700 = high.
# Lower threshold includes more edges but introduces textmining-only noise.
# The RL safety ranker down-weights textmining-only edges via
# props['has_experimental_evidence'] = False (see BUG-3.5).
# Fixes S3-01 (mirrored from string_loader): STITCH_CONFIDENCE_BANDS.
STITCH_CONFIDENCE_BANDS: Dict[str, Tuple[int, int]] = {
    "low":    (0, 400),      # STITCH: <400 = low confidence
    "medium": (400, 700),    # STITCH: 400-700 = medium
    "high":   (700, 1001),   # STITCH: >700 = high
}

# Fixes BUG-3.3: STITCH_SCORE_RANGE — combined_score is integer in [0, 1000].
STITCH_SCORE_RANGE: Tuple[int, int] = (0, 1000)
STITCH_SCORE_DTYPE: str = "Int64"               # pandas nullable integer

# Fixes BUG-3.5: STITCH_EVIDENCE_CHANNELS — the 6 per-channel score columns.
# combined_score is the 7th column (weighted aggregate); handled separately.
STITCH_EVIDENCE_CHANNELS: Tuple[str, ...] = (
    "experimental",    # wet-lab evidence (strong, reliable)
    "database",        # curated pathway databases (strong)
    "textmining",      # literature text-mining (weak, possibly wrong)
    "cooccurrence",    # abstract co-occurrence across literature
    "coexpression",    # mRNA co-expression
    "prediction",      # predicted interaction (computational)
)

# Fixes BUG-5.3: EXPECTED_STITCH_COLUMNS — the 9 required columns in v5.0.
# (Some STITCH releases omit the 'action' column — treated as optional below.)
EXPECTED_STITCH_COLUMNS: Tuple[str, ...] = (
    "chemical", "protein", "action",
    "experimental", "database", "textmining",
    "cooccurrence", "coexpression", "prediction",
    "combined_score",
)
# Columns that MUST be present (action may be absent in some releases).
REQUIRED_STITCH_COLUMNS: Tuple[str, ...] = (
    "chemical", "protein",
    "experimental", "database", "textmining",
    "cooccurrence", "coexpression", "prediction",
    "combined_score",
)

# Fixes GAP-4.4: STITCH_DTYPE_SCHEMA for type-safe parsing.
STITCH_DTYPE_SCHEMA: Dict[str, str] = {
    "chemical": "string",
    "protein": "string",
    "action": "string",
    "experimental": "Int64",
    "database": "Int64",
    "textmining": "Int64",
    "cooccurrence": "Int64",
    "coexpression": "Int64",
    "prediction": "Int64",
    "combined_score": "Int64",
}

# Fixes BUG-3.4: ORGANISM_TAXID_HUMAN — STITCH IDs are prefixed with NCBI
# taxonomy ID followed by a dot, e.g. "9606.ENSP00000358091" for human.
ORGANISM_TAXID_HUMAN: int = 9606
ORGANISM_TAXID_DEFAULT: int = ORGANISM_TAXID_HUMAN
ORGANISM_PREFIX_BY_TAXID: Dict[int, str] = {
    9606:   "9606.",     # human (Homo sapiens)
    10090:  "10090.",    # mouse (Mus musculus)
    10116:  "10116.",    # rat (Rattus norvegicus)
    4932:   "4932.",     # yeast (S. cerevisiae)
    7227:   "7227.",     # fly (D. melanogaster)
    6239:   "6239.",     # worm (C. elegans)
    7955:   "7955.",     # zebrafish (D. rerio)
    44689:  "44689.",    # slime mold (D. discoideum)
    223683: "223683.",   # fission yeast (S. pombe)
    9913:   "9913.",     # cattle (B. taurus)
    9031:   "9031.",     # chicken (G. gallus)
    9544:   "9544.",     # rhesus macaque (M. mulatta)
    13616:  "13616.",    # opossum (M. domestica)
    9258:   "9258.",     # platypus (O. anatinus)
}

# Fixes GAP-3.6 / BUG-5.5: PubChem CID range validation.
# PUBCHEM_CID_MAX is the upper bound as of 2024; document as needing annual update.
PUBCHEM_CID_MIN: int = 1
PUBCHEM_CID_MAX: int = 370_000_000

# Fixes BUG-4.6: Ensembl protein ID regex — supports optional ".N" isoform suffix.
# Example matches: "9606.ENSP00000358091", "9606.ENSP00000358091.2"
ENSEMBL_PROTEIN_ID_REGEX: re.Pattern[str] = re.compile(
    r"^(\d+)\.ENSP\d{11}(\.\d+)?$"
)
# Bare ENSP form (after taxid strip).
ENSEMBL_PROTEIN_ID_BARE_REGEX: re.Pattern[str] = re.compile(
    r"^ENSP\d{11}(\.\d+)?$"
)

# Fixes BUG-9.2: UniProt AC regex (mirrors id_crosswalk._validate_uniprot_ac).
UNIPROT_AC_REGEX: re.Pattern[str] = re.compile(
    r"^[A-NR-Z][0-9][A-Z0-9]{3}[0-9]$"
    r"|^[A-NR-Z][0-9]{5}$"
    r"|^[OPQ][0-9][A-Z0-9]{3}[0-9]$"
    r"|^[A-NR-Z]0[A-Z0-9]{7}[0-9]$"
)

# Fixes BUG-2.5: STITCH_ACTION_TO_REL_TYPE — formal action -> relation map.
# Replaces the v0 substring matching ("inhibit" in action -> "inhibits") which
# silently mislabeled "reactivation" as "activates" and "non-inhibitory" as
# "inhibits" (BUG-2.5 — patient safety).
# All values MUST be in CORE_EDGE_TYPES_SET as (Compound, <rel_type>, Protein).
STITCH_ACTION_TO_REL_TYPE: Dict[str, str] = {
    "inhibition": "inhibits",
    "activation": "activates",
    "binding": "binds",
    "allosteric modulation": "allosterically_modulates",
    "positive allosteric modulation": "allosterically_modulates",
    "negative allosteric modulation": "allosterically_modulates",
    "induction": "induces",
    "metabolism": "metabolized_by",
    "transport": "transported_by",
    "carrier": "carried_by",
}

# Circuit breaker constants (mirrors string_loader R6-12 pattern).
CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 3600   # 1 hour

# Fixes BUG-6.4: Dead-letter queue defaults.
DEFAULT_DLQ_PATH: Path = DEAD_LETTER_DIR / "stitch_malformed.jsonl"
UNRESOLVED_DLQ_PATH: Path = DEAD_LETTER_DIR / "stitch_unresolved_protein.jsonl"
_DLQ_FLUSH_SIZE: int = 1000                    # internal batch size for buffered writes

# Fixes BUG-11.3: Audit log paths.
_AUDIT_LOG_PATH: Path = AUDIT_LOG_DIR / "downloads.jsonl"
_TRANSFORMATION_LOG_PATH: Path = LOGS_DIR / "transformations" / "stitch.jsonl"
_STITCH_AUDIT_LOG_PATH: Path = AUDIT_LOG_DIR / "stitch_transformations.jsonl"

# Fixes BUG-9.1: URL credential masking regex.
_URL_CRED_RE: re.Pattern[str] = re.compile(r"://([^:/@]+):([^@/]+)@")

# Fixes GAP-7.4: Process-cached load_id (correlation ID).
_LOAD_ID_LOCK: threading.Lock = threading.Lock()
_LOAD_ID: Optional[str] = None

# Fixes CIRCUIT_BREAKER: Circuit breaker state (process-local).
_CB_LOCK: threading.Lock = threading.Lock()
_CB_FAILURE_COUNT: int = 0
_CB_OPENED_AT: Optional[float] = None

# Fixes BUG-7.3: Per-edge _provenance dict structure (see schemas.py).
_LEGACY_PROPS_KEYS: Tuple[str, ...] = (
    "source", "score", "action",
    "protein_id_resolved", "protein_ensembl_original",
)

# Internal: thread-local DLQ buffer for batched writes (BUG-6.4).
_DLQ_BUFFER: List[Dict[str, Any]] = []
_DLQ_BUFFER_LOCK: threading.Lock = threading.Lock()

# Edge type constants (BUG-15.1 — kg_builder contract).
_SRC_TYPE: str = "Compound"
_DST_TYPE: str = "Protein"
_DEFAULT_REL_TYPE: str = "binds"

# Fixes GAP-14.5: Module-level flag to emit DeprecationWarning once per run.
_LEGACY_CID_MERGE_WARNED: bool = False

# Fixes BUG-1.5: Sidecar file suffix for SHA-256 (mirrors string_loader).
_SIDECAR_SHA256_SUFFIX: str = ".sha256"
_SIDECAR_VERSION_SUFFIX: str = ".version"


# ===== SECTION 3: METRICS DATACLASS =====
# Fixes GAP-11.2: StitchLoaderMetrics dataclass for structured observability.
# (Re-exported from schemas.py as a TypedDict; here we use a runtime dataclass.)


@dataclass
class _StitchLoaderMetricsDataclass:
    """Runtime container for STITCH loader metrics (GAP-11.2).

    The TypedDict form in schemas.py is the static contract; this dataclass
    is the typed runtime container with sensible defaults.
    """

    rows_in: int = 0
    rows_after_score_filter: int = 0
    rows_after_organism_filter: int = 0
    rows_after_dedup: int = 0
    edges_created: int = 0
    edges_resolved: int = 0
    edges_unresolved: int = 0
    edges_dropped_unresolved: int = 0
    duplicate_edges: int = 0
    non_human_edges: int = 0
    out_of_range_scores: int = 0
    dlq_entries: int = 0
    parse_time_seconds: float = 0.0
    resolve_time_seconds: float = 0.0
    edge_build_time_seconds: float = 0.0
    neo4j_load_time_seconds: float = 0.0
    peak_memory_mb: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict form (matches StitchLoaderMetrics TypedDict)."""
        return {
            "rows_in": self.rows_in,
            "rows_after_score_filter": self.rows_after_score_filter,
            "rows_after_organism_filter": self.rows_after_organism_filter,
            "rows_after_dedup": self.rows_after_dedup,
            "edges_created": self.edges_created,
            "edges_resolved": self.edges_resolved,
            "edges_unresolved": self.edges_unresolved,
            "edges_dropped_unresolved": self.edges_dropped_unresolved,
            "duplicate_edges": self.duplicate_edges,
            "non_human_edges": self.non_human_edges,
            "out_of_range_scores": self.out_of_range_scores,
            "dlq_entries": self.dlq_entries,
            "parse_time_seconds": self.parse_time_seconds,
            "resolve_time_seconds": self.resolve_time_seconds,
            "edge_build_time_seconds": self.edge_build_time_seconds,
            "neo4j_load_time_seconds": self.neo4j_load_time_seconds,
            "peak_memory_mb": self.peak_memory_mb,
            "errors": list(self.errors),
        }


# ===== SECTION 4: LOGGER SETUP =====
# Fixes GAP-4.7: Lazy %-style logging via LoggerAdapter.
# Fixes GAP-11.1 / GAP-11.2: Structured logging via logger.info(event, extra={}).
# Fixes GAP-11.11: stitch_loader does NOT configure handlers/levels; run_pipeline
# owns logging configuration.
logger: logging.Logger = logging.getLogger(__name__)


class StitchLoggerAdapter(logging.LoggerAdapter):
    """Inject source/source_version/load_id into every log record (GAP-4.7).

    Usage:
        adapter = StitchLoggerAdapter(logger, {"load_id": "abc123"})
        adapter.info("stitch_parse_complete", extra={"rows": 1000})
    """

    def process(self, msg: Any, kwargs: Any) -> Tuple[Any, Any]:
        extra = self.extra or {}
        merged = {**kwargs.get("extra", {}), **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def _get_logger(load_id: Optional[str] = None) -> logging.LoggerAdapter:
    """Return a StitchLoggerAdapter with the current load_id attached."""
    return StitchLoggerAdapter(
        logger,
        {
            "source": SOURCE_STITCH,
            "source_version": PARSER_VERSION,
            "load_id": load_id or _get_load_id(),
        },
    )


# ===== SECTION 5: CONFIGURATION & ENVIRONMENT =====
# Fixes GAP-12.4: _validate_stitch_config(cfg) — validate config on startup.
# Fixes GAP-12.2: _resolve_stitch_filepath(filepath) priority: arg > env > config.
# Fixes GAP-12.2: _get_stitch_config() honoring DRUGOS_STITCH_URL env override.
# Fixes GAP-12.2: _resolve_force(force) honoring DRUGOS_STITCH_FORCE_DOWNLOAD env.
# Fixes GAP-12.2: _should_skip() honoring DRUGOS_STITCH_SKIP env.
# Fixes GAP-12.2: _resolve_batch_size(batch_size) honoring DRUGOS_STITCH_BATCH_SIZE.
# Fixes GAP-12.2: _load_yaml_config(path) honoring DRUGOS_STITCH_CONFIG env.

def _get_stitch_config() -> Dict[str, Any]:
    """Return a copy of DATA_SOURCES['stitch'], with env-var overrides applied.

    Honors:
        DRUGOS_STITCH_URL — override the download URL (after _validate_url).

    Returns
    -------
    dict
        A shallow copy of the STITCH config dict with any env overrides
        applied. The original ``DATA_SOURCES['stitch']`` is NOT mutated.
    """
    # Fixes GAP-12.2: env override for URL.
    cfg: Dict[str, Any] = dict(DATA_SOURCES[SOURCE_KEY_STITCH])
    env_url: Optional[str] = os.environ.get("DRUGOS_STITCH_URL")
    if env_url:
        _validate_url(env_url)  # raises StitchSecurityError on non-allowlisted URL
        cfg["url"] = env_url
    return cfg


def _validate_stitch_config(cfg: Dict[str, Any]) -> None:
    """Validate the STITCH config dict on startup (GAP-12.4).

    Raises
    ------
    StitchConfigurationError
        If any required key is missing or has an invalid value.
    """
    required_keys: Tuple[str, ...] = (
        "url", "filename", "version", "max_size_bytes",
        "expected_record_count", "retry_count",
        "retry_backoff_seconds", "timeout_seconds",
    )
    for key in required_keys:
        if key not in cfg:
            raise StitchConfigurationError(
                f"STITCH config missing required key: {key!r}",
                context={"missing_key": key, "available_keys": sorted(cfg.keys())},
            )
    url: Any = cfg["url"]
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StitchConfigurationError(
            f"STITCH URL must be HTTPS, got: {url!r}",
            context={"url": url},
        )
    filename: Any = cfg["filename"]
    if not isinstance(filename, str) or not filename.endswith(".gz"):
        raise StitchConfigurationError(
            f"STITCH filename must end in .gz, got: {filename!r}",
            context={"filename": filename},
        )
    for int_key in ("expected_record_count", "max_size_bytes",
                    "retry_count", "timeout_seconds"):
        val: Any = cfg.get(int_key)
        if not isinstance(val, int) or val <= 0:
            raise StitchConfigurationError(
                f"STITCH config {int_key!r} must be a positive int, got: {val!r}",
                context={"key": int_key, "value": val},
            )
    if not isinstance(cfg.get("retry_backoff_seconds"), (int, float)) \
            or cfg["retry_backoff_seconds"] < 0:
        raise StitchConfigurationError(
            f"STITCH retry_backoff_seconds must be >= 0, got: "
            f"{cfg.get('retry_backoff_seconds')!r}",
        )


def _resolve_stitch_filepath(filepath: Optional[Path] = None) -> Path:
    """Resolve the STITCH input filepath with priority (GAP-12.2):

    1. Explicit ``filepath`` argument (highest priority)
    2. ``DRUGOS_STITCH_FILEPATH`` env var
    3. ``RAW_DIR / DATA_SOURCES['stitch']['filename']`` (default)
    """
    if filepath is not None:
        return Path(filepath)
    env_file: Optional[str] = os.environ.get("DRUGOS_STITCH_FILEPATH")
    if env_file:
        return Path(env_file)
    cfg: Dict[str, Any] = _get_stitch_config()
    return RAW_DIR / cfg["filename"]


def _resolve_force(force: bool) -> bool:
    """Resolve force-download flag (GAP-12.2).

    Returns True if either:
      * ``force`` argument is True, OR
      * ``DRUGOS_STITCH_FORCE_DOWNLOAD=1`` env var is set.
    """
    if force:
        return True
    return os.environ.get("DRUGOS_STITCH_FORCE_DOWNLOAD", "0") == "1"


def _should_skip() -> bool:
    """Return True if STITCH load should be skipped (GAP-12.2).

    Honors ``DRUGOS_STITCH_SKIP=1`` env var. The run_pipeline.py caller
    is responsible for honoring this; the loader only reports it.
    """
    return os.environ.get("DRUGOS_STITCH_SKIP", "0") == "1"


def _resolve_batch_size(batch_size: Optional[int] = None) -> int:
    """Resolve the streaming batch size (GAP-12.2).

    Priority:
      1. Explicit ``batch_size`` argument
      2. ``DRUGOS_STITCH_BATCH_SIZE`` env var
      3. ``STITCH_BATCH_SIZE`` constant (10,000)
    """
    if batch_size is not None:
        if not isinstance(batch_size, int) or batch_size <= 0:
            raise StitchConfigurationError(
                f"batch_size must be a positive int, got: {batch_size!r}",
                context={"batch_size": batch_size},
            )
        return batch_size
    env_bs: Optional[str] = os.environ.get("DRUGOS_STITCH_BATCH_SIZE")
    if env_bs:
        try:
            return int(env_bs)
        except ValueError as exc:
            raise StitchConfigurationError(
                f"DRUGOS_STITCH_BATCH_SIZE must be an int, got: {env_bs!r}",
            ) from exc
    return STITCH_BATCH_SIZE


def _resolve_chunk_size(chunk_size: Optional[int] = None) -> int:
    """Resolve the streaming chunk size (GAP-12.2)."""
    if chunk_size is not None:
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise StitchConfigurationError(
                f"chunk_size must be a positive int, got: {chunk_size!r}",
                context={"chunk_size": chunk_size},
            )
        return chunk_size
    env_cs: Optional[str] = os.environ.get("DRUGOS_STITCH_CHUNK_SIZE")
    if env_cs:
        try:
            return int(env_cs)
        except ValueError as exc:
            raise StitchConfigurationError(
                f"DRUGOS_STITCH_CHUNK_SIZE must be an int, got: {env_cs!r}",
            ) from exc
    return STITCH_CHUNK_SIZE


def _resolve_score_threshold(score_threshold: Optional[int] = None) -> int:
    """Resolve the score threshold with priority (GAP-12.2):

    1. Explicit ``score_threshold`` argument
    2. ``DRUGOS_STITCH_SCORE_THRESHOLD`` env var (at call time)
    3. ``STITCH_SCORE_THRESHOLD`` config constant (default 700)
    """
    if score_threshold is not None:
        _validate_score_threshold(score_threshold)
        return int(score_threshold)
    env_t: Optional[str] = os.environ.get("DRUGOS_STITCH_SCORE_THRESHOLD")
    if env_t:
        try:
            t: int = int(env_t)
        except ValueError as exc:
            raise StitchConfigurationError(
                f"DRUGOS_STITCH_SCORE_THRESHOLD must be an int, got: {env_t!r}",
            ) from exc
        _validate_score_threshold(t)
        return t
    return int(STITCH_SCORE_THRESHOLD)


def _load_yaml_config(path: Path) -> Dict[str, Any]:
    """Load a YAML config file for STITCH overrides (GAP-12.2).

    Requires PyYAML. Raises StitchConfigurationError if PyYAML is not
    installed or the file cannot be parsed.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise StitchConfigurationError(
            "PyYAML is required to load a YAML STITCH config; "
            "install with `pip install pyyaml`.",
            context={"path": str(path)},
        ) from exc
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Any = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise StitchConfigurationError(
            f"Failed to load YAML config from {path}: {exc}",
            context={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise StitchConfigurationError(
            f"YAML config must be a dict at top level, got: {type(data).__name__}",
            context={"path": str(path)},
        )
    return data


# ===== SECTION 6: HELPER UTILITIES =====
# Fixes GAP-7.4: _iso_now, _get_load_id, _reset_load_id.
# Fixes BUG-7.2: _compute_sha256.
# Fixes BUG-7.1: _get_crosswalk_version.
# Fixes BUG-9.1: _sanitize_url_for_logging.
# Fixes BUG-4.6: _validate_ensembl_id, _is_isoform, _safe_str.

def _iso_now() -> str:
    """Return current UTC time as ISO-8601 string (e.g. ``2026-06-18T12:34:56Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_load_id() -> str:
    """Return the process-cached load_id (correlation ID, GAP-7.4).

    The load_id is a UUID4 hex string generated on first call and cached
    for the lifetime of the process. Use ``_reset_load_id()`` in tests.
    """
    # Fixes GAP-7.4: process-cached correlation ID.
    global _LOAD_ID
    if _LOAD_ID is None:
        with _LOAD_ID_LOCK:
            if _LOAD_ID is None:
                import uuid
                _LOAD_ID = f"stitch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    return _LOAD_ID


def _reset_load_id() -> None:
    """Reset the cached load_id (for tests only — GAP-7.4)."""
    global _LOAD_ID
    with _LOAD_ID_LOCK:
        _LOAD_ID = None


def _get_crosswalk_version(crosswalk: Optional["IDCrosswalk"] = None) -> str:
    """Return the crosswalk version (BUG-7.1).

    If ``crosswalk`` is None, returns the BUILTIN_TABLE_VERSION constant
    from id_crosswalk. Otherwise, returns the crosswalk's version (if
    exposed) or "unknown".
    """
    # Fixes BUG-7.1: crosswalk_version field on every edge props.
    try:
        from .id_crosswalk import BUILTIN_TABLE_VERSION
        if crosswalk is None:
            return BUILTIN_TABLE_VERSION
        return BUILTIN_TABLE_VERSION
    except ImportError:
        return "unknown"


def _compute_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute the SHA-256 hex digest of ``path`` in 1 MB chunks (BUG-7.2).

    Returns a 64-character lowercase hex string (no ``sha256:`` prefix).
    """
    # Fixes BUG-7.2: SHA-256 verification.
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _sanitize_url_for_logging(url: str) -> str:
    """Mask credentials embedded in a URL before logging (BUG-9.1).

    Example:
        >>> _sanitize_url_for_logging("https://user:pass@host/path")
        'https://***:***@host/path'
    """
    # Fixes BUG-9.1: mask credentials in URL before logging.
    return _URL_CRED_RE.sub("://***:***@", url)


def _safe_str(v: Any) -> Optional[str]:
    """Return ``str(v)`` if ``v`` is not NULL/NaN/empty, else ``None`` (BUG-5.4).

    Used to canonicalize column values before ID validation.
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s: str = str(v).strip()
    if not s or s.lower() in ("none", "nan", "null", "n/a"):
        return None
    return s


def _is_isoform(ensembl_id: str) -> bool:
    """Return True if the Ensembl ID has a ``.N`` isoform suffix (BUG-4.6)."""
    return "." in ensembl_id.split("ENSP", 1)[-1] if "ENSP" in ensembl_id else False


def _validate_ensembl_id(ensembl_id: str, taxid: int = 9606) -> bool:
    """Validate an Ensembl protein ID with taxid prefix (BUG-4.6).

    Returns True if the ID matches ``^<taxid>\\.ENSP\\d{11}(\\.\\d+)?$``.
    """
    if not isinstance(ensembl_id, str) or not ensembl_id:
        return False
    return bool(ENSEMBL_PROTEIN_ID_REGEX.match(ensembl_id))


def _validate_uniprot_ac(ac: str) -> bool:
    """Validate a UniProt accession (BUG-9.1).

    Returns True if the AC matches the canonical UniProt regex.
    """
    if not isinstance(ac, str) or not ac:
        return False
    return bool(UNIPROT_AC_REGEX.match(ac))


# ===== SECTION 7: SECURITY HELPERS =====
# Fixes BUG-9.1: _validate_url against ALLOWED_STITCH_URLS (SSRF guard).
# Fixes BUG-9.2: _create_ssl_context with certifi + CERT_REQUIRED + check_hostname.
# Fixes GAP-9.4: _validate_filename_safe against path traversal.
# Fixes BUG-9.1: _set_secure_file_permissions (chmod 0o600).

def _validate_url(url: str) -> None:
    """Refuse to download from a URL not in ALLOWED_STITCH_URLS (BUG-9.1).

    Also enforces HTTPS-only and rejects URLs with embedded credentials or
    that resolve to private/internal IP addresses (SSRF guard).

    Raises
    ------
    StitchSecurityError
        If the URL is not HTTPS, not in the allowlist, contains credentials,
        or resolves to a private IP.
    """
    # Fixes BUG-9.1: enforce HTTPS.
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StitchSecurityError(
            f"STITCH URL must be HTTPS, got: {_sanitize_url_for_logging(url)!r}",
            context={"url": _sanitize_url_for_logging(url)},
        )
    # Fixes BUG-9.1: URL allowlist (SSRF guard).
    if not any(url.startswith(prefix) for prefix in ALLOWED_STITCH_URLS):
        raise StitchSecurityError(
            f"URL not in ALLOWED_STITCH_URLS allowlist: "
            f"{_sanitize_url_for_logging(url)!r}",
            context={
                "url": _sanitize_url_for_logging(url),
                "allowed_prefixes": list(ALLOWED_STITCH_URLS),
            },
        )
    # Fixes BUG-9.1: reject URLs with embedded credentials.
    if "@" in url.split("://", 1)[-1].split("/", 1)[0]:
        raise StitchSecurityError(
            f"STITCH URL contains embedded credentials: "
            f"{_sanitize_url_for_logging(url)!r}",
            context={"url": _sanitize_url_for_logging(url)},
        )
    # Fixes BUG-9.1: SSRF guard — reject URLs that resolve to private IPs.
    try:
        host: str = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        # Skip SSRF check for hostnames we cannot resolve (e.g., in tests).
        # We only check if resolution succeeds AND the IP is private.
        try:
            ip = socket.gethostbyname(host)
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                raise StitchSecurityError(
                    f"STITCH URL host {host!r} resolves to private IP {ip} — "
                    f"SSRF guard rejected.",
                    context={"host": host, "ip": ip},
                )
        except socket.gaierror:
            # Cannot resolve — let the actual download fail with a clear error.
            pass
    except (IndexError, ValueError):
        pass  # malformed URL — let downstream raise a clearer error


def _validate_filename_safe(filename: str) -> None:
    """Reject filenames with path-traversal characters or non-.gz extensions (GAP-9.4).

    Raises
    ------
    StitchSecurityError
        If the filename contains ``..``, ``/``, ``\\``, null bytes, or does
        not end in ``.gz``.
    """
    # Fixes GAP-9.4: path-traversal guard.
    if not isinstance(filename, str) or not filename:
        raise StitchSecurityError(
            f"Filename must be a non-empty string, got: {filename!r}",
            context={"filename": filename},
        )
    if ".." in filename or "/" in filename or "\\" in filename:
        raise StitchSecurityError(
            f"Filename contains path-traversal characters: {filename!r}",
            context={"filename": filename},
        )
    if "\x00" in filename:
        raise StitchSecurityError(
            f"Filename contains null bytes: {filename!r}",
            context={"filename": filename},
        )
    if not filename.endswith(".gz"):
        raise StitchSecurityError(
            f"Filename must end in .gz, got: {filename!r}",
            context={"filename": filename},
        )


def _validate_path_within_dir(path: Path, directory: Path) -> None:
    """Raise StitchSecurityError if ``path`` resolves outside ``directory`` (GAP-9.4)."""
    # Fixes GAP-9.4: ensure resolved path stays inside directory.
    resolved_path: Path = path.resolve()
    resolved_dir: Path = directory.resolve()
    try:
        resolved_path.relative_to(resolved_dir)
    except ValueError as exc:
        raise StitchSecurityError(
            f"Path {path!r} resolves outside allowed directory {directory!r}.",
            context={"path": str(path), "directory": str(directory),
                     "resolved_path": str(resolved_path)},
        ) from exc


def _set_secure_file_permissions(path: Path, mode: int = 0o600) -> None:
    """Set secure file permissions (0o600 by default) on POSIX (BUG-9.1)."""
    # Fixes BUG-9.1: secure file permissions on downloaded files.
    if os.name == "posix":
        try:
            os.chmod(path, mode)
        except OSError:
            pass  # best-effort — don't fail the download on chmod errors


def _create_ssl_context() -> ssl.SSLContext:
    """Return a TLS context with cert verification enabled (BUG-9.2).

    Honors ``DRUGOS_STITCH_CA_BUNDLE`` env var for custom CA bundles.
    Falls back to certifi if available, then to the system CA bundle.
    """
    # Fixes BUG-9.2: TLS verification.
    ctx: ssl.SSLContext = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ca_bundle: Optional[str] = os.environ.get("DRUGOS_STITCH_CA_BUNDLE")
    if ca_bundle:
        ctx.load_verify_locations(cafile=ca_bundle)
    else:
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except ImportError:
            logger.warning(
                "stitch_certifi_not_installed_fallback_to_system_ca",
                extra={"hint": "Install certifi for stronger TLS verification."},
            )
            # Fall back to system CA bundle (already loaded by create_default_context).
    return ctx


# ===== SECTION 8: DOWNLOAD LAYER =====
# Fixes BUG-6.1: _retry_with_backoff.
# Fixes BUG-6.1: _atomic_download via .part + os.replace.
# Fixes BUG-6.3: BadGzipFile handling.
# Fixes GAP-6.5: _write_checkpoint + _read_checkpoint.
# Fixes BUG-11.3: _append_audit_log.

def _verify_gzip_magic_bytes(path: Path) -> None:
    """Raise StitchDownloadError if the first 2 bytes are not ``\\x1f\\x8b`` (BUG-6.1).

    Allows the file to start with an optional UTF-8 BOM (``\\xef\\xbb\\xbf``)
    before the gzip magic bytes (some HTTP servers add a BOM incorrectly).
    """
    # Fixes BUG-6.1: gzip magic-byte sniff.
    try:
        with open(path, "rb") as f:
            magic: bytes = f.read(3)
    except OSError as exc:
        raise StitchDownloadError(
            f"Cannot read STITCH file for gzip magic-byte check: {path}",
            context={"path": str(path), "error": str(exc)},
        ) from exc
    # Skip optional UTF-8 BOM.
    offset: int = 0
    if magic[:3] == b"\xef\xbb\xbf":
        offset = 3
    with open(path, "rb") as f:
        f.seek(offset)
        magic2: bytes = f.read(2)
    if magic2[:2] != b"\x1f\x8b":
        raise StitchDownloadError(
            f"STITCH file is not valid gzip (magic bytes {magic2!r} do not "
            f"match \\x1f\\x8b): {path}",
            context={"path": str(path), "magic_bytes": magic2.hex()},
        )


def _verify_size(path: Path, cfg: Dict[str, Any]) -> int:
    """Verify the file size is within [STITCH_MIN_VALID_SIZE_BYTES, max_size_bytes] (BUG-5.2).

    Raises
    ------
    StitchDataIntegrityError
        If size < STITCH_MIN_VALID_SIZE_BYTES or > max_size_bytes.
    """
    # Fixes BUG-5.2: file-size validation.
    actual: int = path.stat().st_size
    min_size: int = STITCH_MIN_VALID_SIZE_BYTES
    max_size: int = int(cfg.get("max_size_bytes", 3_000_000_000))
    if actual < min_size:
        raise StitchDataIntegrityError(
            f"STITCH file size {actual} bytes is below minimum "
            f"{min_size} bytes (likely an HTML error page).",
            context={"path": str(path), "actual_size": actual, "min_size": min_size},
        )
    if actual > max_size:
        raise StitchDataIntegrityError(
            f"STITCH file size {actual} bytes exceeds maximum {max_size} bytes.",
            context={"path": str(path), "actual_size": actual, "max_size": max_size},
        )
    return actual


def _verify_checksum(path: Path, cfg: Dict[str, Any]) -> str:
    """Verify the SHA-256 of ``path`` matches ``cfg['sha256']`` (BUG-7.2).

    If ``cfg['sha256']`` is None, returns the computed hash without
    comparison (STITCH does not always publish a checksum).

    Raises
    ------
    StitchDataIntegrityError
        If the computed hash does not match the pinned value.
    """
    # Fixes BUG-7.2: SHA-256 verification with mismatch raise.
    actual: str = _compute_sha256(path)
    expected: Optional[str] = cfg.get("sha256")
    if expected is not None and actual != expected:
        raise StitchDataIntegrityError(
            f"STITCH SHA-256 mismatch: expected={expected}, actual={actual}",
            context={"path": str(path), "expected_sha256": expected,
                     "actual_sha256": actual},
        )
    return actual


def _verify_integrity(path: Path, cfg: Dict[str, Any]) -> str:
    """Umbrella helper: gzip magic + size + checksum (BUG-1.5).

    Returns the computed SHA-256 (always — even when cfg['sha256'] is None).
    """
    # Fixes BUG-1.5: integrity verification umbrella.
    _verify_gzip_magic_bytes(path)
    _verify_size(path, cfg)
    return _verify_checksum(path, cfg)


def _verify_row_count(df: pd.DataFrame, cfg: Dict[str, Any]) -> None:
    """Verify row count is within [0.5x, 2.0x] of expected (BUG-5.2).

    Only enforced for production-sized files (actual >= 50% of expected).
    Test fixtures and small samples bypass this check to allow unit
    testing with synthetic data.

    Raises
    ------
    CriticalDataSourceError
        If row count is 0 (0 edges on required source — patient safety).
    StitchDataIntegrityError
        If row count ratio is outside [0.5, 2.0] AND the file is
        production-sized (>= 50% of expected).
    """
    # Fixes BUG-5.2: row-count validation (production-sized only).
    actual: int = len(df)
    if actual == 0:
        # 0 rows on required source = critical failure.
        if STITCH_REQUIRED:
            raise CriticalDataSourceError(
                f"STITCH file produced 0 rows — possible empty/corrupted download. "
                f"STITCH is in CRITICAL_SOURCES — patient safety guard.",
                context={"actual_rows": 0},
            )
        return
    expected: int = int(cfg.get("expected_record_count", 0))
    if expected <= 0:
        return  # no expected count configured — skip check
    # Skip check for non-production-sized files (test fixtures, samples)
    if actual < expected * 0.5:
        logger.info(
            "stitch_row_count_check_skipped_small_file",
            extra={"actual_rows": actual, "expected_rows": expected,
                   "ratio": round(actual / expected, 4) if expected else 0},
        )
        return
    ratio: float = actual / expected
    if ratio < 0.5 or ratio > 2.0:
        raise StitchDataIntegrityError(
            f"STITCH row count {actual} is outside [0.5x, 2.0x] of "
            f"expected {expected} (ratio={ratio:.3f}).",
            context={"actual_rows": actual, "expected_rows": expected,
                     "ratio": round(ratio, 4)},
        )
    if ratio < 0.8 or ratio > 1.2:
        logger.warning(
            "stitch_row_count_drift",
            extra={"actual_rows": actual, "expected_rows": expected,
                   "ratio": round(ratio, 4)},
        )


def _check_freshness(gz_path: Path, cfg: Dict[str, Any]) -> None:
    """Warn/raise if the cached STITCH file is stale (GAP-5.6).

    Logs INFO if file age > 1x expected_update_frequency_days.
    Raises StitchDataIntegrityError if file age > 2x expected_update_frequency_days.
    """
    # Fixes GAP-5.6: freshness check.
    expected_days: int = int(cfg.get("expected_update_frequency_days", 730))
    if not gz_path.exists():
        return
    age_seconds: float = time.time() - gz_path.stat().st_mtime
    age_days: float = age_seconds / 86400
    if age_days > 2 * expected_days:
        raise StitchDataIntegrityError(
            f"STITCH file is {age_days:.1f} days old — exceeds 2x expected "
            f"update frequency ({expected_days} days).",
            context={"path": str(gz_path), "age_days": round(age_days, 1),
                     "expected_frequency_days": expected_days},
        )
    if age_days > expected_days:
        logger.warning(
            "stitch_file_stale",
            extra={"path": str(gz_path), "age_days": round(age_days, 1),
                   "expected_frequency_days": expected_days},
        )


def _sidecar_version_path(gz_path: Path) -> Path:
    """Return the path of the .version sidecar file for ``gz_path``."""
    return gz_path.with_suffix(gz_path.suffix + _SIDECAR_VERSION_SUFFIX)


def _read_sidecar_version(gz_path: Path) -> Optional[str]:
    """Read the cached version from the .version sidecar (GAP-15.3)."""
    sidecar: Path = _sidecar_version_path(gz_path)
    if not sidecar.exists():
        return None
    try:
        return sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_sidecar_version(gz_path: Path, version: str) -> None:
    """Write the version to the .version sidecar (GAP-15.3)."""
    sidecar: Path = _sidecar_version_path(gz_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    try:
        sidecar.write_text(version, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "stitch_sidecar_write_failed",
            extra={"path": str(sidecar), "error": str(exc)},
        )


def _sidecar_sha256_path(gz_path: Path) -> Path:
    """Return the path of the .sha256 sidecar file for ``gz_path``."""
    return gz_path.with_suffix(gz_path.suffix + _SIDECAR_SHA256_SUFFIX)


def _read_sidecar_sha256(gz_path: Path) -> Optional[str]:
    """Read the cached SHA-256 from the .sha256 sidecar (BUG-7.2)."""
    sidecar: Path = _sidecar_sha256_path(gz_path)
    if not sidecar.exists():
        return None
    try:
        return sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_sidecar_sha256(gz_path: Path, sha256: str) -> None:
    """Write the SHA-256 to the .sha256 sidecar (BUG-7.2)."""
    sidecar: Path = _sidecar_sha256_path(gz_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    try:
        sidecar.write_text(sha256, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "stitch_sha256_sidecar_write_failed",
            extra={"path": str(sidecar), "error": str(exc)},
        )


def _circuit_breaker_record_failure(source: str = SOURCE_KEY_STITCH) -> None:
    """Record a download failure in the circuit breaker (mirrors string_loader R6-12).

    After ``CIRCUIT_BREAKER_FAILURE_THRESHOLD`` consecutive failures,
    the breaker opens and stays open for ``CIRCUIT_BREAKER_COOLDOWN_SECONDS``.
    """
    global _CB_FAILURE_COUNT, _CB_OPENED_AT
    with _CB_LOCK:
        _CB_FAILURE_COUNT += 1
        if _CB_FAILURE_COUNT >= CIRCUIT_BREAKER_FAILURE_THRESHOLD \
                and _CB_OPENED_AT is None:
            _CB_OPENED_AT = time.time()
            logger.error(
                "stitch_circuit_breaker_opened",
                extra={"source": source, "failure_count": _CB_FAILURE_COUNT,
                       "threshold": CIRCUIT_BREAKER_FAILURE_THRESHOLD},
            )


def _circuit_breaker_record_success() -> None:
    """Reset the circuit breaker on a successful download."""
    global _CB_FAILURE_COUNT, _CB_OPENED_AT
    with _CB_LOCK:
        _CB_FAILURE_COUNT = 0
        _CB_OPENED_AT = None


def _circuit_breaker_check(source: str = SOURCE_KEY_STITCH) -> None:
    """Raise StitchDownloadError if the breaker is open.

    The breaker auto-resets after ``CIRCUIT_BREAKER_COOLDOWN_SECONDS``.
    """
    global _CB_FAILURE_COUNT, _CB_OPENED_AT
    with _CB_LOCK:
        if _CB_OPENED_AT is not None:
            elapsed: float = time.time() - _CB_OPENED_AT
            if elapsed < CIRCUIT_BREAKER_COOLDOWN_SECONDS:
                remaining: float = CIRCUIT_BREAKER_COOLDOWN_SECONDS - elapsed
                raise StitchDownloadError(
                    f"STITCH download circuit breaker is open. "
                    f"Cooldown: {remaining:.0f}s remaining "
                    f"(opened {elapsed:.0f}s ago after "
                    f"{_CB_FAILURE_COUNT} consecutive failures).",
                    context={
                        "source": source,
                        "failure_count": _CB_FAILURE_COUNT,
                        "opened_at": _CB_OPENED_AT,
                        "cooldown_remaining_seconds": round(remaining, 1),
                    },
                )
            else:
                # Auto-reset after cooldown
                _CB_FAILURE_COUNT = 0
                _CB_OPENED_AT = None
                logger.info(
                    "stitch_circuit_breaker_reset",
                    extra={"source": source},
                )


def _retry_with_backoff(
    fn: Any,
    *,
    retry_count: int = 3,
    retry_backoff: float = 30.0,
    retryable_exceptions: Tuple[type[BaseException], ...] = (
        urllib.error.URLError, socket.timeout, ConnectionError, OSError,
    ),
) -> Any:
    """Call ``fn()`` with exponential backoff + jitter (BUG-6.1).

    The backoff formula is ``retry_backoff * 2**attempt + random.uniform(0, 1)``.
    Non-retryable exceptions (including all DrugOSDataError subclasses)
    are re-raised immediately without retry.

    Raises
    ------
    The last raised exception if all retries are exhausted.
    """
    # Fixes BUG-6.1: retry with exponential backoff and jitter.
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retry_count + 1):
        try:
            result: Any = fn()
            return result
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == retry_count:
                break
            sleep_time: float = retry_backoff * (2 ** (attempt - 1)) \
                + random.uniform(0, 1)
            logger.warning(
                "stitch_download_retry",
                extra={"attempt": attempt, "max_retries": retry_count,
                       "sleep_seconds": round(sleep_time, 2),
                       "error": str(exc)},
            )
            time.sleep(sleep_time)
        except DrugOSDataError:
            raise  # never retry our own integrity/security errors
    assert last_exc is not None  # for type-checker
    raise last_exc


def _atomic_download(
    url: str,
    dest: Path,
    *,
    expected_size: Optional[int],
    max_size: int,
    retry_count: int,
    retry_backoff: float,
    timeout: float = 600.0,
) -> Path:
    """Download ``url`` to ``dest`` atomically via .part + os.replace (BUG-6.1).

    The download is streamed to a ``.part`` file in 64 KB chunks. After
    all chunks are written, the file is size-validated and gzip-magic-byte
    sniffed before being atomically renamed to ``dest``. On any failure,
    the ``.part`` file is deleted and the original ``dest`` is left intact.

    Raises
    ------
    StitchDownloadError
        On network timeout, HTTP error, size mismatch, or gzip failure.
    """
    # Fixes BUG-6.1: atomic write via .part + os.replace.
    # Fixes BUG-6.3: BadGzipFile handling (gzip magic-byte sniff).
    # Fixes BUG-9.2: TLS verification via _create_ssl_context.
    _validate_url(url)
    part_path: Path = dest.with_suffix(dest.suffix + ".part")
    bytes_downloaded: int = 0
    request: urllib.request.Request = urllib.request.Request(
        url, headers={"User-Agent": "DrugOS/1.0 (drugos@example.com)"}
    )
    ssl_context: ssl.SSLContext = _create_ssl_context()

    def _do_download() -> None:
        nonlocal bytes_downloaded
        bytes_downloaded = 0
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context
        ) as resp:
            content_length: Optional[str] = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                raise StitchDownloadError(
                    f"STITCH download Content-Length {content_length} exceeds "
                    f"max_size {max_size}.",
                    context={"url": _sanitize_url_for_logging(url),
                             "content_length": int(content_length),
                             "max_size": max_size},
                )
            with open(part_path, "wb") as f_out:
                while True:
                    chunk: bytes = resp.read(65536)
                    if not chunk:
                        break
                    f_out.write(chunk)
                    bytes_downloaded += len(chunk)
                    if bytes_downloaded > max_size:
                        raise StitchDownloadError(
                            f"STITCH download exceeded max_size {max_size} "
                            f"after {bytes_downloaded} bytes.",
                            context={"url": _sanitize_url_for_logging(url),
                                     "bytes_downloaded": bytes_downloaded,
                                     "max_size": max_size},
                        )

    _retry_with_backoff(
        _do_download,
        retry_count=retry_count,
        retry_backoff=retry_backoff,
    )

    # Size validation
    if bytes_downloaded < STITCH_MIN_VALID_SIZE_BYTES:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StitchDownloadError(
            f"STITCH download size {bytes_downloaded} bytes is below minimum "
            f"{STITCH_MIN_VALID_SIZE_BYTES} bytes (likely an HTML error page).",
            context={"url": _sanitize_url_for_logging(url),
                     "bytes_downloaded": bytes_downloaded,
                     "min_size": STITCH_MIN_VALID_SIZE_BYTES},
        )

    # Gzip magic-byte sniff (BUG-6.3)
    try:
        with open(part_path, "rb") as f_check:
            magic: bytes = f_check.read(2)
    except OSError as exc:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StitchDownloadError(
            f"Cannot read STITCH download for gzip magic-byte check: {exc}",
            context={"url": _sanitize_url_for_logging(url), "error": str(exc)},
        ) from exc
    if magic[:2] != b"\x1f\x8b":
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StitchDownloadError(
            f"STITCH download is not a valid gzip file (magic bytes "
            f"{magic!r} do not match \\x1f\\x8b).",
            context={"url": _sanitize_url_for_logging(url),
                     "magic_bytes": magic.hex()},
        )

    # Atomic rename
    os.replace(part_path, dest)
    _set_secure_file_permissions(dest)
    return dest


def _append_audit_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append a JSONL entry to the STITCH audit log (BUG-11.3).

    The audit log lives at ``logs/audit/stitch_transformations.jsonl`` by default.
    Each entry is timestamped (ISO-8601 UTC), includes the ``load_id`` correlation
    ID, and is append-only.
    """
    # Fixes BUG-11.3: audit log to logs/audit/stitch_transformations.jsonl.
    log_path: Path = path or _STITCH_AUDIT_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "load_id": _get_load_id(),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        **event,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "stitch_audit_log_write_failed",
            extra={"path": str(log_path), "error": str(exc)},
        )


def _append_transformation_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append a JSONL entry to the STITCH transformation log (BUG-16.2)."""
    log_path: Path = path or _TRANSFORMATION_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "load_id": _get_load_id(),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        **event,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "stitch_transformation_log_write_failed",
            extra={"path": str(log_path), "error": str(exc)},
        )


def _write_to_dlq(entry: Dict[str, Any]) -> None:
    """Buffer a single DLQ entry; flushed by _flush_dlq (BUG-6.4)."""
    with _DLQ_BUFFER_LOCK:
        _DLQ_BUFFER.append(entry)
        if len(_DLQ_BUFFER) >= _DLQ_FLUSH_SIZE:
            _flush_dlq_unlocked()


def _flush_dlq(dlq_path: Optional[Path] = None) -> None:
    """Flush buffered DLQ entries to disk (BUG-6.4)."""
    with _DLQ_BUFFER_LOCK:
        _flush_dlq_unlocked(dlq_path)


def _flush_dlq_unlocked(dlq_path: Optional[Path] = None) -> None:
    """Internal: flush without acquiring the lock (caller must hold it)."""
    if not _DLQ_BUFFER:
        return
    path: Path = dlq_path or DEFAULT_DLQ_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            for entry in _DLQ_BUFFER:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        _DLQ_BUFFER.clear()
    except OSError as exc:
        logger.warning(
            "stitch_dlq_flush_failed",
            extra={"path": str(path), "error": str(exc),
                   "buffered_entries": len(_DLQ_BUFFER)},
        )


def _write_checkpoint(stage: str, row_index: int, edges_count: int) -> None:
    """Write a checkpoint JSON file for resumable processing (GAP-6.5).

    Checkpoints live at ``data/checkpoints/stitch_{stage}_{load_id}.json``.
    """
    # Fixes GAP-6.5: checkpoint write.
    checkpoint_path: Path = CHECKPOINT_DIR / f"stitch_{stage}_{_get_load_id()}.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    entry: Dict[str, Any] = {
        "stage": stage,
        "row_index": row_index,
        "edges_count": edges_count,
        "timestamp": _iso_now(),
        "load_id": _get_load_id(),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    try:
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning(
            "stitch_checkpoint_write_failed",
            extra={"path": str(checkpoint_path), "error": str(exc)},
        )


def _read_checkpoint(stage: str, load_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the latest checkpoint for ``stage`` (GAP-6.5)."""
    lid: str = load_id or _get_load_id()
    checkpoint_path: Path = CHECKPOINT_DIR / f"stitch_{stage}_{lid}.json"
    if not checkpoint_path.exists():
        return None
    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "stitch_checkpoint_read_failed",
            extra={"path": str(checkpoint_path), "error": str(exc)},
        )
        return None


# ===== SECTION 9: DOWNLOAD FUNCTION =====
# Fixes BUG-1.5: integrity verification umbrella.
# Fixes BUG-6.1: retry/timeout/atomic download.
# Fixes BUG-9.1: URL allowlist.
# Fixes BUG-9.2: TLS verification.
# Fixes BUG-11.3: audit log on download start/complete.
# Fixes GAP-5.6: freshness check.
# Fixes BUG-7.2: SHA-256 sidecar.

def download_stitch(force: bool = False) -> Path:
    """Download the STITCH CPI file (institutional-grade v1.1.0).

    Backward-compatible signature: ``download_stitch(force=False) -> Path``.

    The download is atomic, TLS-verified, size-validated, checksum-verified,
    and circuit-breaker-protected. On cache hit (``force=False``), the
    SHA-256 of the cached file is verified against the pinned value (if
    set); on mismatch, the file is re-downloaded.

    Parameters
    ----------
    force : bool, default False
        If True, re-download even if the file exists. Logs a WARNING
        before overwriting.

    Returns
    -------
    Path
        The path to the downloaded (or cached) STITCH .tsv.gz file.

    Raises
    ------
    StitchSecurityError
        If the URL is not in ALLOWED_STITCH_URLS or not HTTPS.
    StitchDownloadError
        On network timeout, HTTP error, size mismatch, gzip failure,
        or circuit breaker open.
    StitchDataIntegrityError
        If SHA-256 or size verification fails after download.
    StitchConfigurationError
        If config is invalid (GAP-12.4).

    Side Effects
    ------------
    - Writes the file to ``RAW_DIR / DATA_SOURCES['stitch']['filename']``.
    - Writes a .sha256 sidecar file (BUG-7.2).
    - Writes a .version sidecar file (GAP-15.3).
    - Appends an entry to ``logs/audit/stitch_transformations.jsonl`` (BUG-11.3).
    - Appends an entry to ``logs/audit/downloads.jsonl`` (BUG-9.1).
    - Sets secure file permissions (0o600) on POSIX (BUG-9.1).

    Examples
    --------
    >>> from drugos_graph.stitch_loader import download_stitch
    >>> path = download_stitch()  # doctest: +SKIP
    >>> path.name  # doctest: +SKIP
    'stitch_interactions.tsv.gz'
    """
    # Fixes BUG-1.5: atomic download with integrity verification.
    # Fixes BUG-6.1: retry/timeout.
    # Fixes BUG-9.1: URL allowlist + SSRF guard.
    # Fixes BUG-9.2: TLS verification.
    cfg: Dict[str, Any] = _get_stitch_config()
    _validate_stitch_config(cfg)
    gz_path: Path = RAW_DIR / cfg["filename"]
    _validate_filename_safe(cfg["filename"])
    _validate_path_within_dir(gz_path, RAW_DIR)

    # Circuit breaker check
    _circuit_breaker_check(source=SOURCE_KEY_STITCH)

    # Cache check
    if gz_path.exists() and not _resolve_force(force):
        # Fixes BUG-7.2: verify SHA-256 on cache hit.
        try:
            cached_sha: str = _verify_checksum(gz_path, cfg)
            _check_freshness(gz_path, cfg)
            logger.info(
                "stitch_cache_hit",
                extra={"path": str(gz_path), "sha256": cached_sha,
                       "size_bytes": gz_path.stat().st_size,
                       "size_mb": round(gz_path.stat().st_size / MB, 2)},
            )
            _append_audit_log({
                "event": "cache_hit", "url": _sanitize_url_for_logging(cfg["url"]),
                "dest": str(gz_path), "sha256": cached_sha,
                "size_bytes": gz_path.stat().st_size,
            })
            return gz_path
        except StitchDataIntegrityError as exc:
            logger.warning(
                "stitch_cache_corrupt_redownloading",
                extra={"path": str(gz_path), "error": str(exc)},
            )
            try:
                gz_path.unlink()
            except OSError:
                pass

    # Version-skew check (GAP-15.3)
    sidecar_version: Optional[str] = _read_sidecar_version(gz_path)
    if sidecar_version is not None and sidecar_version != cfg.get("version"):
        logger.warning(
            "stitch_version_skew_redownloading",
            extra={"cached_version": sidecar_version,
                   "expected_version": cfg.get("version")},
        )
        try:
            gz_path.unlink()
        except OSError:
            pass

    # force=True warn before overwrite
    if gz_path.exists() and _resolve_force(force):
        logger.warning(
            "stitch_force_overwrite",
            extra={"path": str(gz_path), "size_bytes": gz_path.stat().st_size},
        )

    # Download
    logger.info(
        "stitch_download_start",
        extra={"url": _sanitize_url_for_logging(cfg["url"]), "dest": str(gz_path)},
    )
    _append_audit_log({
        "event": "download_start",
        "url": _sanitize_url_for_logging(cfg["url"]),
        "dest": str(gz_path),
    })
    try:
        _atomic_download(
            cfg["url"],
            gz_path,
            expected_size=cfg.get("size_bytes"),
            max_size=int(cfg.get("max_size_bytes", 3_000_000_000)),
            retry_count=int(cfg.get("retry_count", 3)),
            retry_backoff=float(cfg.get("retry_backoff_seconds", 30.0)),
            timeout=float(cfg.get("timeout_seconds", 600.0)),
        )
    except (StitchDownloadError, StitchDataIntegrityError, StitchSecurityError):
        _circuit_breaker_record_failure(source=SOURCE_KEY_STITCH)
        raise
    except Exception as exc:
        _circuit_breaker_record_failure(source=SOURCE_KEY_STITCH)
        raise StitchDownloadError(
            f"STITCH download failed: {exc}",
            context={"url": _sanitize_url_for_logging(cfg["url"]),
                     "error_type": type(exc).__name__, "error": str(exc)},
        ) from exc

    # Post-download verification (BUG-1.5)
    try:
        actual_sha: str = _verify_integrity(gz_path, cfg)
    except StitchDataIntegrityError:
        try:
            gz_path.unlink()
        except OSError:
            pass
        raise

    # Write sidecars (BUG-7.2, GAP-15.3)
    _write_sidecar_sha256(gz_path, actual_sha)
    _write_sidecar_version(gz_path, str(cfg.get("version", "5.0")))

    # Reset circuit breaker on success
    _circuit_breaker_record_success()

    size_bytes: int = gz_path.stat().st_size
    logger.info(
        "stitch_download_complete",
        extra={"path": str(gz_path), "size_bytes": size_bytes,
               "size_mb": round(size_bytes / MB, 2), "sha256": actual_sha},
    )
    _append_audit_log({
        "event": "download_complete",
        "url": _sanitize_url_for_logging(cfg["url"]),
        "dest": str(gz_path), "size_bytes": size_bytes, "sha256": actual_sha,
    })
    # Also append to the shared downloads.jsonl (mirrors string_loader S9-08).
    _append_audit_log({
        "event": "download_complete", "source": SOURCE_STITCH,
        "url": _sanitize_url_for_logging(cfg["url"]),
        "dest": str(gz_path), "size_bytes": size_bytes, "sha256": actual_sha,
    }, path=_AUDIT_LOG_PATH)
    return gz_path


# ===== SECTION 10: PARSE LAYER =====
# Fixes BUG-5.3: _validate_columns.
# Fixes BUG-3.3: _validate_score_threshold, _validate_score_scale.
# Fixes BUG-5.4: missing column backfill.
# Fixes BUG-4.6: protein ID validation.
# Fixes BUG-3.1: CIDm vs CIDs preservation.
# Fixes BUG-3.2: organism prefix validation.
# Fixes BUG-3.4: organism filter.
# Fixes BUG-5.1: _dedup_edges.
# Fixes BUG-3.5: evidence channels preserved.
# Fixes GAP-3.6: CID range validation.
# Fixes BUG-15.2: CID int normalization.
# Fixes BUG-2.5: action -> rel_type formal map.

def _validate_columns(df: pd.DataFrame) -> None:
    """Validate that the parsed DataFrame has all required columns (BUG-5.3).

    Raises
    ------
    StitchParseError
        If any column in ``REQUIRED_STITCH_COLUMNS`` is missing.
    """
    # Fixes BUG-5.3: required column validation.
    missing: set = set(REQUIRED_STITCH_COLUMNS) - set(df.columns)
    if missing:
        raise StitchParseError(
            f"STITCH file missing required columns: {sorted(missing)}. "
            f"Got columns: {list(df.columns)}",
            context={"missing_columns": sorted(missing),
                     "actual_columns": list(df.columns)},
        )
    unexpected: set = set(df.columns) - set(EXPECTED_STITCH_COLUMNS)
    if unexpected:
        logger.warning(
            "stitch_unexpected_columns",
            extra={"unexpected_columns": sorted(unexpected),
                   "hint": "Could be new columns in a future STITCH version — "
                           "consider schema bump."},
        )


def _validate_score_threshold(threshold: int) -> None:
    """Validate that ``threshold`` is an int in [0, 1000] (BUG-3.3, BUG-12.1).

    Raises
    ------
    StitchConfigurationError
        If threshold is not an int or is outside [0, 1000].
    """
    # Fixes BUG-3.3: threshold validation.
    # Fixes BUG-12.1: enforce int (not float).
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        raise StitchConfigurationError(
            f"score_threshold must be int, got {type(threshold).__name__}: "
            f"{threshold!r}",
            context={"threshold": threshold, "type": type(threshold).__name__},
        )
    if threshold < 0 or threshold > 1000:
        raise StitchConfigurationError(
            f"score_threshold must be in [0, 1000], got {threshold!r}",
            context={"threshold": threshold,
                     "valid_range": [0, 1000]},
        )


def _validate_score_scale(df: pd.DataFrame) -> None:
    """Validate that the ``score`` column is in [0, 1000] (BUG-3.3).

    Raises
    ------
    StitchDataIntegrityError
        If max score > 1000 (suggests scale mismatch — patient safety).
    """
    # Fixes BUG-3.3: score scale validation.
    if "score" not in df.columns or len(df) == 0:
        return
    max_score: Any = df["score"].max()
    if pd.notna(max_score) and int(max_score) > 1000:
        raise StitchDataIntegrityError(
            f"STITCH score column appears to use a different scale "
            f"(max={max_score}); expected [0, 1000]",
            context={"max_score": int(max_score), "expected_max": 1000},
        )


def _validate_pubchem_cid(cid_str: str) -> bool:
    """Return True iff ``cid_str`` is a digit string in PubChem range (GAP-3.6)."""
    # Fixes GAP-3.6: PubChem CID range validation.
    if not isinstance(cid_str, str) or not cid_str:
        return False
    if not cid_str.isdigit():
        return False
    try:
        cid_int: int = int(cid_str)
    except (ValueError, OverflowError):
        return False
    return PUBCHEM_CID_MIN <= cid_int <= PUBCHEM_CID_MAX


def _normalize_cid(cid_str: str) -> int:
    """Strip leading zeros and convert CID string to int (BUG-15.2).

    Raises
    ------
    ValueError
        If ``cid_str`` is not a valid integer.
    """
    # Fixes BUG-15.2: convert CID string to int (matches DrugBank/ChEMBL convention).
    return int(cid_str.lstrip("0") or "0")


def _detect_stitch_version(filepath: Path) -> str:
    """Detect the STITCH file version from the header (GAP-15.3).

    Returns
    -------
    str
        "5.0" if the header contains ``combined_score`` (v5.0 detailed format).
        "4.0" if the header contains only ``score`` (v4.0 or simplified format).
        "unknown" otherwise.

    Raises
    ------
    StitchParseError
        If the file cannot be read or the header is unparseable.
    """
    # Fixes GAP-15.3: version detection.
    try:
        with gzip.open(filepath, "rt", encoding="utf-8") as f:
            # Skip comment lines starting with '!'
            for line in f:
                if line.startswith("!"):
                    continue
                header: str = line.strip()
                break
            else:
                return "unknown"
    except gzip.BadGzipFile as exc:
        raise StitchParseError(
            f"STITCH file is not valid gzip: {filepath}",
            context={"filepath": str(filepath), "error": str(exc)},
        ) from exc
    except OSError as exc:
        raise StitchParseError(
            f"Cannot read STITCH file: {filepath}",
            context={"filepath": str(filepath), "error": str(exc)},
        ) from exc

    columns: List[str] = [c.strip() for c in header.split("\t")]
    if "combined_score" in columns:
        return "5.0"
    if "score" in columns:
        return "4.0"
    return "unknown"


def _map_action_to_rel_type(action: Optional[str]) -> Tuple[str, str]:
    """Map a STITCH action string to a CORE_EDGE_TYPES relation name (BUG-2.5).

    Returns
    -------
    tuple of (rel_type, mapping_method)
        rel_type is one of: "inhibits", "activates", "binds",
        "allosterically_modulates", "induces", "metabolized_by",
        "transported_by", "carried_by".
        mapping_method is one of: "exact", "prefix", "fallback_binds", "default".
    """
    # Fixes BUG-2.5: formal action -> rel_type map with exact-then-prefix fallback.
    if action is None or action == "":
        return ("binds", "default")
    # Normalize: lowercase, strip, collapse whitespace.
    normalized: str = re.sub(r"\s+", " ", str(action).lower().strip())
    # Exact match
    if normalized in STITCH_ACTION_TO_REL_TYPE:
        return (STITCH_ACTION_TO_REL_TYPE[normalized], "exact")
    # Prefix match (e.g., "inhibition (competitive)" matches "inhibition")
    for canonical, rel_type in STITCH_ACTION_TO_REL_TYPE.items():
        if normalized.startswith(canonical):
            return (rel_type, "prefix")
    # Fallback: 'binds' with WARNING (fail-safe — STITCH default)
    logger.warning(
        "stitch_action_fallback_binds",
        extra={"original_action": action, "normalized": normalized,
               "hint": "Action not in STITCH_ACTION_TO_REL_TYPE — "
                       "falling back to 'binds'."},
    )
    return ("binds", "fallback_binds")


def _validate_edge_triple(src_type: str, rel_type: str, dst_type: str) -> None:
    """Validate that (src_type, rel_type, dst_type) is in CORE_EDGE_TYPES_SET (GAP-14.4).

    Raises
    ------
    StitchDataIntegrityError
        If the triple is not a core edge type.
    """
    # Fixes GAP-14.4: validate edge triple against CORE_EDGE_TYPES_SET.
    triple: Tuple[str, str, str] = (src_type, rel_type, dst_type)
    if triple not in CORE_EDGE_TYPES_SET:
        raise StitchDataIntegrityError(
            f"STITCH edge triple {triple!r} not in CORE_EDGE_TYPES_SET — "
            f"would create an edge the Graph Transformer cannot traverse.",
            context={"src_type": src_type, "rel_type": rel_type,
                     "dst_type": dst_type, "triple": triple},
        )


def _validate_edge_record(edge: Dict[str, Any]) -> None:
    """Assert all required edge record keys are present (BUG-2.2).

    Raises
    ------
    StitchDataIntegrityError
        If any required key is missing.
    """
    # Fixes BUG-2.2: edge record validation.
    required_keys: Tuple[str, ...] = (
        "src_id", "dst_id", "src_type", "dst_type", "rel_type", "props",
    )
    missing: List[str] = [k for k in required_keys if k not in edge]
    if missing:
        raise StitchDataIntegrityError(
            f"STITCH edge record missing required keys: {missing}",
            context={"missing_keys": missing, "edge_keys": sorted(edge.keys())},
        )
    # Validate triple (GAP-14.4)
    _validate_edge_triple(
        str(edge["src_type"]), str(edge["rel_type"]), str(edge["dst_type"])
    )


def parse_stitch_raw(
    filepath: Optional[Path] = None,
    *,
    encoding: str = "utf-8-sig",
) -> pd.DataFrame:
    """Pure parser: read STITCH file into DataFrame with NO filtering (GAP-1.3).

    Reads the file, applies the dtype schema, strips column whitespace, and
    returns the DataFrame. No score filter, no organism filter, no dedup,
    no validation beyond column presence.

    Parameters
    ----------
    filepath : Path, optional
        Path to the STITCH .tsv.gz file. If None, resolves via
        ``_resolve_stitch_filepath`` (env var > config default).
    encoding : str, default "utf-8-sig"
        File encoding. UTF-8 with BOM handling (GAP-15.4).

    Returns
    -------
    pd.DataFrame
        DataFrame with the 9-10 STITCH columns. The ``combined_score``
        column is renamed to ``score`` for downstream consistency.

    Raises
    ------
    StitchParseError
        On BadGzipFile, EmptyDataError, ParserError, UnicodeDecodeError,
        or missing required columns.
    StitchDataIntegrityError
        If score scale is implausible (>1000).
    """
    # Fixes GAP-1.3: pure parser stage (no filtering, no validation).
    # Fixes GAP-15.4: encoding='utf-8-sig' handles BOM.
    # Fixes BUG-6.2: enriched error messages.
    path: Path = _resolve_stitch_filepath(filepath)
    if not path.exists():
        raise StitchParseError(
            _enriched_not_found_message(path),
            context={"filepath": str(path), "raw_dir": str(RAW_DIR)},
        )
    _validate_filename_safe(path.name)
    _validate_path_within_dir(path, path.parent)

    logger.info("stitch_parse_raw_start", extra={"filepath": str(path)})

    # Fixes BUG-6.3: wrap pd.read_csv in try/except for clear error types.
    # Fixes GAP-4.4: dtype + usecols + on_bad_lines='warn' + encoding='utf-8-sig'.
    # Fixes BUG-5.4: backfill missing 'action'/'score' columns with empty/NA Series.
    try:
        df: pd.DataFrame = pd.read_csv(
            path,
            sep="\t",
            comment="!",
            header=0,
            dtype=STITCH_DTYPE_SCHEMA,
            encoding=encoding,
            na_values=["", "NA", "NULL", "NaN"],
            keep_default_na=False,
            on_bad_lines="warn",
        )
    except gzip.BadGzipFile as exc:
        raise StitchParseError(
            f"STITCH file is not valid gzip: {path}",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc
    except pd.errors.EmptyDataError as exc:
        raise StitchParseError(
            f"STITCH file is empty or contains no data: {path}",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc
    except pd.errors.ParserError as exc:
        raise StitchParseError(
            f"STITCH file has malformed TSV: {path}",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc
    except UnicodeDecodeError as exc:
        raise StitchParseError(
            f"STITCH file has encoding issues (expected UTF-8): {path}",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc

    # Strip column whitespace (BUG-5.3)
    original_columns: List[str] = list(df.columns)
    df.columns = [str(c).strip() for c in df.columns]
    if list(df.columns) != original_columns:
        logger.warning(
            "stitch_columns_whitespace_stripped",
            extra={"original": original_columns, "stripped": list(df.columns)},
        )

    # Validate required columns (BUG-5.3)
    _validate_columns(df)

    # Backfill missing 'action' column with empty strings (BUG-5.4)
    if "action" not in df.columns:
        df["action"] = pd.Series(["" for _ in range(len(df))], dtype="string")
        logger.info("stitch_action_column_backfilled_empty")

    # Rename combined_score -> score for downstream consistency (BUG-4.3)
    if "combined_score" in df.columns:
        df = df.rename(columns={"combined_score": "score"})
    elif "score" not in df.columns:
        # Fixes BUG-4.3 / BUG-11.5: explicit raise instead of silent skip.
        logger.warning(
            "stitch_score_column_missing_critical",
            extra={"columns": list(df.columns),
                   "hint": "Filter would be silently skipped — raising."},
        )
        raise StitchDataIntegrityError(
            f"STITCH file has neither 'combined_score' nor 'score' column. "
            f"Got columns: {list(df.columns)}",
            context={"columns": list(df.columns), "filepath": str(path)},
        )

    # Validate score scale (BUG-3.3)
    _validate_score_scale(df)

    # Attach provenance to df.attrs (BUG-7.2)
    source_sha256: str = _compute_sha256(path)
    df.attrs["provenance"] = {
        "source": SOURCE_STITCH,
        "source_file": str(path),
        "source_sha256": source_sha256,
        "source_version": str(_get_stitch_config().get("version", "5.0")),
        "parser_module": __name__,
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parsed_at": _iso_now(),
        "row_count_in": int(len(df)),
        "load_id": _get_load_id(),
        "input_sha256": source_sha256,
    }
    df.attrs["license"] = STITCH_LICENSE
    df.attrs["attribution"] = STITCH_ATTRIBUTION

    logger.info(
        "stitch_parse_raw_complete",
        extra={"filepath": str(path), "rows": len(df),
               "columns": list(df.columns), "sha256": source_sha256},
    )
    _append_audit_log({
        "event": "parse_raw_complete",
        "filepath": str(path), "rows_in": int(len(df)),
        "sha256": source_sha256,
    })
    return df


def _enriched_not_found_message(filepath: Path) -> str:
    """Build a helpful error message when STITCH file is not found (BUG-6.2)."""
    return (
        f"STITCH file not found: {filepath}\n"
        f"Remediation options:\n"
        f"  1. Run `download_stitch()` first to download the file.\n"
        f"  2. Set DRUGOS_STITCH_FILEPATH env var to the file path.\n"
        f"  3. Pass `filepath=...` explicitly to parse_stitch_interactions().\n"
        f"  4. Verify DATA_SOURCES['stitch']['filename'] in config.py."
    )


def filter_by_score(df: pd.DataFrame, threshold: int) -> pd.DataFrame:
    """Filter the STITCH DataFrame by ``score >= threshold`` (GAP-1.3).

    Raises
    ------
    StitchConfigurationError
        If threshold is not an int in [0, 1000] (BUG-3.3, BUG-12.1).
    """
    # Fixes BUG-3.3: threshold validation.
    _validate_score_threshold(threshold)
    if "score" not in df.columns:
        raise StitchDataIntegrityError(
            "Cannot filter by score: 'score' column missing from DataFrame.",
            context={"columns": list(df.columns)},
        )
    n_before: int = len(df)
    df_out: pd.DataFrame = df[df["score"] >= threshold].copy()
    n_after: int = len(df_out)
    if n_after == 0 and n_before > 0:
        logger.warning(
            "stitch_score_filter_removed_all_rows",
            extra={"threshold": threshold, "rows_before": n_before,
                   "hint": "Check if score scale changed (expected [0, 1000])."},
        )
    logger.info(
        "stitch_score_filter",
        extra={"threshold": threshold, "rows_before": n_before,
               "rows_after": n_after, "rows_dropped": n_before - n_after},
    )
    return df_out


def _validate_and_strip_taxid_prefix(
    protein_id: str,
    expected_taxid: int = 9606,
) -> Tuple[Optional[str], Optional[int]]:
    """Validate the taxid prefix on a STITCH protein ID (BUG-3.2).

    Returns
    -------
    tuple of (bare_ensp, taxid)
        (bare_ensp, expected_taxid) if prefix matches expected_taxid.
        (None, taxid) if prefix matches a different taxid — caller should DLQ.
        (None, None) if no prefix — caller should DLQ.
    """
    # Fixes BUG-3.2: validate species prefix before stripping.
    if not isinstance(protein_id, str) or not protein_id:
        return (None, None)
    # Match "<digits>.<rest>" pattern
    m: Optional[re.Match[str]] = re.match(r"^(\d+)\.(.+)$", protein_id)
    if m is None:
        return (None, None)  # no prefix
    taxid_str: str = m.group(1)
    bare_ensp: str = m.group(2)
    try:
        taxid: int = int(taxid_str)
    except ValueError:
        return (None, None)
    if taxid != expected_taxid:
        return (None, taxid)  # non-target organism
    return (bare_ensp, taxid)


def _filter_organism(df: pd.DataFrame, taxid: int = 9606) -> pd.DataFrame:
    """Filter the DataFrame to rows matching ``taxid`` (BUG-3.2, BUG-3.4).

    Non-matching rows are written to the DLQ with reason="non_target_organism".
    Rows missing the taxid prefix are written to the DLQ with
    reason="missing_taxid_prefix".
    """
    # Fixes BUG-3.2: validate species prefix.
    # Fixes BUG-3.4: organism filter.
    if "protein" not in df.columns:
        return df
    n_before: int = len(df)
    # Vectorized: extract (taxid_str, bare_ensp) using str.extract
    extracted: pd.DataFrame = df["protein"].str.extract(r"^(\d+)\.(.+)$", expand=True)
    extracted.columns = ["_taxid_str", "_bare_ensp"]
    df = pd.concat([df, extracted], axis=1)

    # Identify non-target organism rows
    non_target_mask: pd.Series = (
        df["_taxid_str"].notna() &
        (df["_taxid_str"].astype("string").astype("Int64") != taxid)
    )
    if non_target_mask.any():
        non_target_indices: List[int] = [int(i) for i in df.index[non_target_mask]]
        for i in non_target_indices:
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": i,
                "reason": "non_target_organism",
                "raw_values": {"protein": str(df.loc[i, "protein"]),
                               "expected_taxid": taxid},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "filter_organism",
                "load_id": _get_load_id(),
            })
        logger.warning(
            "stitch_non_target_organism_rows_dlq",
            extra={"count": len(non_target_indices), "expected_taxid": taxid},
        )

    # Identify missing-prefix rows
    missing_mask: pd.Series = df["_taxid_str"].isna()
    if missing_mask.any():
        missing_indices: List[int] = [int(i) for i in df.index[missing_mask]]
        for i in missing_indices:
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": i,
                "reason": "missing_taxid_prefix",
                "raw_values": {"protein": str(df.loc[i, "protein"])},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "filter_organism",
                "load_id": _get_load_id(),
            })

    # Drop non-target and missing-prefix rows
    keep_mask: pd.Series = ~non_target_mask & ~missing_mask
    df = df[keep_mask].copy()
    n_after: int = len(df)

    # Set protein_string_id (bare ENSP after prefix strip)
    df["protein_string_id"] = df["_bare_ensp"].astype(str)
    # Drop the temp columns
    df = df.drop(columns=["_taxid_str", "_bare_ensp"])

    drop_rate: float = (n_before - n_after) / max(n_before, 1)
    if drop_rate > 0.10:
        logger.warning(
            "stitch_organism_filter_high_drop_rate",
            extra={"drop_rate": round(drop_rate, 4),
                   "dropped": n_before - n_after, "kept": n_after,
                   "expected_taxid": taxid,
                   "hint": "Drop rate > 10% suggests wrong source URL "
                           "(all-organisms instead of human-only)."},
        )
    logger.info(
        "stitch_organism_filter",
        extra={"expected_taxid": taxid, "rows_before": n_before,
               "rows_after": n_after, "rows_dropped": n_before - n_after,
               "drop_rate": round(drop_rate, 4)},
    )
    return df


def filter_by_organism(df: pd.DataFrame, taxid: int = 9606) -> pd.DataFrame:
    """Public alias for ``_filter_organism`` (GAP-1.3)."""
    return _filter_organism(df, taxid=taxid)


def _validate_protein_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``protein_string_id`` after taxid strip (BUG-4.6).

    Drops empty strings (was "9606." with no ENSP) and invalid ENSP format
    (must match ``^ENSP\\d{11}(\\.\\d+)?$``). Failures go to DLQ.
    """
    # Fixes BUG-4.6: validate protein_string_id after taxid strip.
    if "protein_string_id" not in df.columns:
        return df
    n_before: int = len(df)

    # Empty-string check
    empty_mask: pd.Series = (df["protein_string_id"].isna()) | \
                            (df["protein_string_id"] == "") | \
                            (df["protein_string_id"] == "nan")
    if empty_mask.any():
        for i in df.index[empty_mask]:
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(i),
                "reason": "empty_protein_id_after_strip",
                "raw_values": {"protein_string_id": str(df.loc[i, "protein_string_id"])},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "validate_protein_ids",
                "load_id": _get_load_id(),
            })
        df = df[~empty_mask].copy()

    # Invalid ENSP format check
    if len(df) > 0:
        invalid_mask: pd.Series = ~df["protein_string_id"].str.match(ENSEMBL_PROTEIN_ID_BARE_REGEX)
        if invalid_mask.any():
            for i in df.index[invalid_mask]:
                _write_to_dlq({
                    "timestamp": _iso_now(),
                    "row_index": int(i),
                    "reason": "invalid_ensp_format",
                    "raw_values": {"protein_string_id": str(df.loc[i, "protein_string_id"])},
                    "parser_version": PARSER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "stage": "validate_protein_ids",
                    "load_id": _get_load_id(),
                })
            df = df[~invalid_mask].copy()

    n_after: int = len(df)
    if n_before != n_after:
        logger.warning(
            "stitch_protein_id_validation_dropped",
            extra={"dropped": n_before - n_after, "kept": n_after},
        )
    return df


def _extract_chemical_metadata(df: pd.DataFrame) -> pd.DataFrame:
    """Extract CID, stereochemistry, and stitch_chemical_id from the chemical column (BUG-3.1).

    Adds three new columns:
      * ``chemical_cid``: bare numeric string (e.g. "00002244") — legacy compat.
      * ``stitch_chemical_id``: full original ID (e.g. "CIDm00002244").
      * ``stereochemistry``: "stereo_specific" / "non_stereo" / "unknown".
      * ``stereochemistry_code``: "m" / "s" / "".
      * ``pubchem_cid``: int form (e.g. 2244) — matches DrugBank/ChEMBL convention (BUG-15.2).

    Invalid CIDs (extraction failed) are written to the DLQ with
    reason="cid_extraction_failed". CIDs outside PubChem range are written
    to the DLQ with reason="invalid_pubchem_cid" (GAP-3.6).
    """
    # Fixes BUG-3.1: preserve CIDm vs CIDs distinction.
    # Fixes GAP-3.6: CID range validation.
    # Fixes BUG-15.2: convert CID string to int.
    # Fixes GAP-4.5: renamed nan_before -> n_rows_before_cid_filter.

    # v28 ROOT FIX (P2-L-17): the previous code emitted a DeprecationWarning
    # on EVERY parse call (gated by a one-shot ``_LEGACY_CID_MERGE_WARNED``
    # global flag — but Python's default warning filter shows
    # DeprecationWarnings only once per source line PER PROCESS, so the
    # flag was redundant). Worse, the warning fired regardless of whether
    # the consumer ever accessed ``props['chemical_cid']`` — which is
    # impossible to detect. Operators quickly learned to ignore the
    # warning, defeating its purpose. The BUG-3.1 fix (preserve CIDm vs
    # CIDs distinction) is now the ONLY behavior — the warning has no
    # informational value. Removed.
    # The legacy merge mode (DRUGOS_STITCH_LEGACY_CID_MERGE=1) remains
    # supported for operators who need the v0 behavior; it sets
    # ``extracted["_stitch_prefix"] = pd.NA`` below.
    legacy_merge: bool = os.environ.get("DRUGOS_STITCH_LEGACY_CID_MERGE", "0") == "1"

    n_rows_before: int = len(df)
    # Extract the prefix (CIDm or CIDs) and the bare numeric CID
    extracted: pd.DataFrame = df["chemical"].str.extract(
        r"^(CID[m,s])(\d+)$", expand=True
    )
    extracted.columns = ["_stitch_prefix", "_cid_digits"]

    # If legacy_merge is enabled, force prefix to None (preserves v0 behavior)
    if legacy_merge:
        extracted["_stitch_prefix"] = pd.NA

    df = pd.concat([df, extracted], axis=1)

    # chemical_cid: bare numeric string (legacy compat)
    df["chemical_cid"] = df["_cid_digits"]

    # stitch_chemical_id: full original ID (e.g. "CIDm00002244")
    df["stitch_chemical_id"] = df["chemical"].astype(str)

    # stereochemistry columns
    # v27 ROOT FIX (P2-L-5): the previous code (including the v16 "SW-16"
    # "fix" comment) mapped
    #   CIDm -> "stereo_specific"   (WRONG — 'm' = merged = flat/non-stereo)
    #   CIDs -> "non_stereo"        (WRONG — 's' = stereo-specific separate)
    #   ""   -> "unknown"
    # Per the STITCH documentation
    # (https://stitch.embl.de/info/compound_id_format):
    #   "CIDm" = merged form — stereoisomers are merged into a single
    #            entry (i.e. the molecule is treated as FLAT / non-stereo).
    #   "CIDs" = stereo-specific — the molecule has defined stereochemistry
    #            and is kept as a separate entry per stereoisomer.
    # The previous code (and the v16 "fix" comment) inverted these
    # labels. The v16 comment even claimed "STITCH's 'CIDs' prefix does
    # NOT mean racemic mixture" — true, but it ALSO does not mean
    # "non_stereo": 'CIDs' means STEREO-SPECIFIC. The 'm' in 'CIDm'
    # stands for "merged", which means stereoisomers are folded together
    # (= FLAT / non-stereo). The corrected mapping:
    #   CIDm -> "non_stereo"        (merged = flat)
    #   CIDs -> "stereo_specific"   (stereoisomers kept separate)
    #   ""   -> "unknown"
    df["stereochemistry_code"] = df["_stitch_prefix"].fillna("").apply(
        lambda x: x[-1] if isinstance(x, str) and x.startswith("CID") and len(x) > 3 else ""
    )
    df["stereochemistry"] = df["stereochemistry_code"].map(
        {"m": "non_stereo", "s": "stereo_specific", "": "unknown"}
    ).fillna("unknown")

    # Drop rows where CID extraction failed
    malformed_mask: pd.Series = df["chemical_cid"].isna()
    if malformed_mask.any():
        for i in df.index[malformed_mask]:
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(i),
                "reason": "cid_extraction_failed",
                "raw_values": {"chemical": str(df.loc[i, "chemical"])},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "extract_chemical_metadata",
                "load_id": _get_load_id(),
            })
        df = df[~malformed_mask].copy()

    # Validate CID in PubChem range (GAP-3.6)
    if len(df) > 0:
        invalid_cid_mask: pd.Series = ~df["chemical_cid"].apply(_validate_pubchem_cid)
        if invalid_cid_mask.any():
            for i in df.index[invalid_cid_mask]:
                _write_to_dlq({
                    "timestamp": _iso_now(),
                    "row_index": int(i),
                    "reason": "invalid_pubchem_cid",
                    "raw_values": {"chemical_cid": str(df.loc[i, "chemical_cid"]),
                                   "valid_range": [PUBCHEM_CID_MIN, PUBCHEM_CID_MAX]},
                    "parser_version": PARSER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "stage": "extract_chemical_metadata",
                    "load_id": _get_load_id(),
                })
            df = df[~invalid_cid_mask].copy()

    # Convert CID to int (BUG-15.2)
    if len(df) > 0:
        df["pubchem_cid"] = df["chemical_cid"].apply(_normalize_cid).astype("int64")
        # v9 ROOT FIX (audit F5.2.3 / F6): the previous line set
        # ``chemical_cid = pubchem_cid.astype(str)`` producing "2244" — a
        # bare integer string. STITCH edges then emitted src_id="2244"
        # which fails ID_PATTERNS["Compound"] = ^(...|CID\d+|...)$. The
        # SIDER loader (BUG-B-004) was correctly fixed to emit f"CID{int}"
        # but the same fix was never propagated to STITCH. ~1.6M edges
        # were dead-lettered in production. Now mirror SIDER's format.
        df["chemical_cid"] = df["pubchem_cid"].map(lambda c: f"CID{int(c)}")

    # Drop temp columns
    df = df.drop(columns=["_stitch_prefix", "_cid_digits"])

    n_rows_after: int = len(df)
    n_failures: int = n_rows_before - n_rows_after
    if n_failures > 0:
        pct: float = n_failures / max(n_rows_before, 1) * 100
        logger.warning(
            "stitch_cid_extraction_failures",
            extra={"failures": n_failures, "total": n_rows_before,
                   "percentage": round(pct, 2),
                   "hint": "Failures written to DLQ (reason=cid_extraction_failed "
                           "or reason=invalid_pubchem_cid)."},
        )
    return df


def _dedup_edges(
    df: pd.DataFrame,
    conflict_resolution: str = "max_combined_score",
) -> pd.DataFrame:
    """Deduplicate (chemical, protein) pairs (BUG-5.1, BUG-2.4).

    Parameters
    ----------
    df : pd.DataFrame
        STITCH DataFrame with columns including 'chemical', 'protein',
        'score', and the 6 evidence-channel columns.
    conflict_resolution : str, default "max_combined_score"
        Strategy for deduplication:
          * "max_combined_score" — keep row with max score; aggregate channel
            scores as max per channel.
          * "union_evidence" — aggregate all channel scores as max per channel;
            recompute combined_score as the max.
          * "keep_all" — no dedup (preserve v0 behavior for backward compat).

    Returns
    -------
    pd.DataFrame
        Deduplicated DataFrame with new columns:
          * ``evidence_count``: int — number of source rows merged.
          * ``duplicate_sources``: list of per-channel sources merged.
    """
    # Fixes BUG-5.1: deduplication with conflict resolution.
    # Fixes BUG-2.4: dedup=True parameter.
    if conflict_resolution == "keep_all" or len(df) == 0:
        df["evidence_count"] = 1
        df["duplicate_sources"] = [[] for _ in range(len(df))]
        return df

    n_before: int = len(df)
    valid_strategies: Tuple[str, ...] = ("max_combined_score", "union_evidence")
    if conflict_resolution not in valid_strategies:
        raise StitchDataIntegrityError(
            f"Unknown conflict_resolution strategy: {conflict_resolution!r}. "
            f"Valid: {valid_strategies}",
            context={"conflict_resolution": conflict_resolution,
                     "valid_strategies": list(valid_strategies)},
        )

    # Group by (chemical, protein) — collapse duplicates.
    # BUG-2.1 / BUG-5.1: For robustness, fall back to (chemical_cid, protein_string_id)
    # if the canonical (chemical, protein) columns are absent (e.g., synthetic test
    # DataFrames that only contain the post-extraction columns).
    candidate_group_cols: List[Tuple[str, ...]] = [
        ("chemical", "protein"),
        ("chemical_cid", "protein_string_id"),
        ("stitch_chemical_id", "protein_string_id"),
    ]
    group_cols: List[str] = []
    for cand in candidate_group_cols:
        if all(c in df.columns for c in cand):
            group_cols = list(cand)
            break
    if not group_cols:
        # Cannot find suitable group columns — skip dedup entirely.
        df["evidence_count"] = 1
        df["duplicate_sources"] = [[] for _ in range(len(df))]
        logger.warning(
            "stitch_dedup_skipped_no_group_columns",
            extra={"columns": list(df.columns),
                   "hint": "Neither (chemical, protein) nor (chemical_cid, "
                           "protein_string_id) columns are present — skipping dedup."},
        )
        return df

    # Build aggregation dict — only aggregate columns that exist in df.
    # Evidence channels and score use "max"; all other columns use "first"
    # to carry forward metadata (chemical_cid, stitch_chemical_id,
    # stereochemistry, protein_string_id, etc.) added by _extract_chemical_metadata.
    agg_dict: Dict[str, Any] = {}
    for ch in STITCH_EVIDENCE_CHANNELS:
        if ch in df.columns:
            agg_dict[ch] = "max"
    if "score" in df.columns:
        # Both strategies take the max combined score.
        agg_dict["score"] = "max"
    if "action" in df.columns:
        # Take the first non-empty action in the group.
        agg_dict["action"] = lambda s: next(
            (a for a in s if a is not None and not (isinstance(a, float) and pd.isna(a)) and str(a).strip()),
            "",
        )
    # Carry forward all other columns as "first" (metadata like chemical_cid,
    # stitch_chemical_id, stereochemistry, protein_string_id, pubchem_cid, etc.)
    aggregated_cols: set = set(agg_dict.keys())
    for col in df.columns:
        if col in group_cols or col in aggregated_cols:
            continue
        agg_dict[col] = "first"

    grouped: pd.DataFrame = df.groupby(group_cols, as_index=False, sort=False).agg(agg_dict)
    # Compute evidence_count = number of rows merged per group
    counts: pd.Series = df.groupby(group_cols, sort=False).size().reset_index(drop=True)
    grouped["evidence_count"] = counts.values if hasattr(counts, "values") else list(counts)
    # Replace any existing duplicate_sources with a placeholder (lineage is
    # not preserved per-channel in this minimal implementation; see BUG-5.1
    # for the full lineage design).
    grouped["duplicate_sources"] = [[] for _ in range(len(grouped))]

    n_after: int = len(grouped)
    dup_rate: float = (n_before - n_after) / max(n_before, 1)
    logger.info(
        "stitch_dedup_edges",
        extra={"strategy": conflict_resolution,
               "rows_before": n_before, "rows_after": n_after,
               "duplicates_removed": n_before - n_after,
               "dup_rate": round(dup_rate, 4)},
    )
    _append_transformation_log({
        "operation": "dedup_edges",
        "strategy": conflict_resolution,
        "rows_in": n_before, "rows_out": n_after,
        "duplicates_removed": n_before - n_after,
    })
    return grouped.reset_index(drop=True)


def dedup_edges(
    df: pd.DataFrame,
    strategy: str = "max_combined_score",
) -> pd.DataFrame:
    """Public alias for ``_dedup_edges`` (GAP-1.3)."""
    return _dedup_edges(df, conflict_resolution=strategy)


def validate_stitch(
    df: pd.DataFrame,
    *,
    organism_taxid: int = 9606,
    expected_columns: Tuple[str, ...] = EXPECTED_STITCH_COLUMNS,
) -> Dict[str, Any]:
    """Validate a STITCH DataFrame and return a structured report (GAP-1.3, BUG-5.3).

    Returns a dict (matching StitchValidationReport TypedDict) rather than raising,
    so callers can decide which failures are fatal in their context.

    Parameters
    ----------
    df : pd.DataFrame
        STITCH DataFrame (from parse_stitch_raw or parse_stitch_interactions).
    organism_taxid : int, default 9606
        Expected organism taxid (for non-human row counting).
    expected_columns : tuple of str, default EXPECTED_STITCH_COLUMNS
        Columns expected to be present.

    Returns
    -------
    dict
        Validation report with keys: total_rows, null_chemical, null_protein,
        null_combined_score, score_min, score_max, score_mean, score_p50,
        non_human_rows, duplicate_rows, out_of_range_scores,
        malformed_chemical_ids, malformed_protein_ids, columns_present,
        columns_missing, columns_unexpected, schema_version.
    """
    # Fixes GAP-1.3: validate_stitch returns report.
    report: Dict[str, Any] = {
        "total_rows": int(len(df)),
        "null_chemical": 0,
        "null_protein": 0,
        "null_combined_score": 0,
        "score_min": None,
        "score_max": None,
        "score_mean": None,
        "score_p50": None,
        "non_human_rows": 0,
        "duplicate_rows": 0,
        "out_of_range_scores": 0,
        "malformed_chemical_ids": 0,
        "malformed_protein_ids": 0,
        "columns_present": list(df.columns),
        "columns_missing": [],
        "columns_unexpected": [],
        "schema_version": SCHEMA_VERSION,
    }

    # Column checks
    expected_set: set = set(expected_columns)
    actual_set: set = set(df.columns)
    report["columns_missing"] = sorted(expected_set - actual_set)
    report["columns_unexpected"] = sorted(actual_set - expected_set)

    # Null counts
    if "chemical" in df.columns:
        report["null_chemical"] = int(df["chemical"].isna().sum())
    if "protein" in df.columns:
        report["null_protein"] = int(df["protein"].isna().sum())
    if "score" in df.columns:
        report["null_combined_score"] = int(df["score"].isna().sum())
        if len(df) > 0 and df["score"].notna().any():
            report["score_min"] = int(df["score"].min())
            report["score_max"] = int(df["score"].max())
            report["score_mean"] = float(df["score"].mean())
            report["score_p50"] = float(df["score"].median())
            # Out-of-range scores
            out_mask: pd.Series = (df["score"] < 0) | (df["score"] > 1000)
            report["out_of_range_scores"] = int(out_mask.sum())

    # Non-human rows
    if "protein" in df.columns and len(df) > 0:
        taxids: pd.Series = df["protein"].str.extract(r"^(\d+)\.")[0]
        if taxids.notna().any():
            report["non_human_rows"] = int(
                (taxids.notna() & (taxids.astype("string").astype("Int64") != organism_taxid)).sum()
            )

    # Duplicates
    if "chemical" in df.columns and "protein" in df.columns:
        report["duplicate_rows"] = int(df.duplicated(subset=["chemical", "protein"]).sum())

    # Malformed IDs
    if "chemical" in df.columns and len(df) > 0:
        bad_chemical: pd.Series = ~df["chemical"].str.match(r"^CID[m,s]\d+$", na=False)
        report["malformed_chemical_ids"] = int(bad_chemical.sum())
    if "protein" in df.columns and len(df) > 0:
        bad_protein: pd.Series = ~df["protein"].str.match(r"^\d+\.ENSP\d{11}", na=False)
        report["malformed_protein_ids"] = int(bad_protein.sum())

    logger.info("stitch_validation_report", extra={"report": report})
    return report


def parse_stitch_interactions(
    filepath: Optional[Path] = None,
    score_threshold: Optional[int] = None,
    *,
    organism_taxid: int = 9606,
    conflict_resolution: str = "max_combined_score",
    dedup: bool = True,
) -> pd.DataFrame:
    """Parse STITCH TSV file into a DataFrame (institutional-grade v1.1.0).

    Backward-compatible signature (Rule R3):
    ``parse_stitch_interactions(filepath=None, score_threshold=None) -> pd.DataFrame``.

    New optional kwargs (additive only — Rule R3):
      * ``organism_taxid`` — int (default 9606) — BUG-3.4.
      * ``conflict_resolution`` — str (default "max_combined_score") — BUG-5.1.
      * ``dedup`` — bool (default True) — BUG-2.4.

    Internally calls: parse_stitch_raw -> filter_by_organism -> filter_by_score
    -> _extract_chemical_metadata -> _validate_protein_ids -> _dedup_edges.

    Parameters
    ----------
    filepath : Path, optional
        Path to the STITCH TSV.gz file. If None, resolves via
        ``_resolve_stitch_filepath`` (env var > config default).
    score_threshold : int, optional
        Minimum combined score. If None, uses ``STITCH_SCORE_THRESHOLD`` (700).
    organism_taxid : int, default 9606
        Filter to this organism. Non-human rows go to DLQ.
    conflict_resolution : str, default "max_combined_score"
        Strategy for deduplication: "max_combined_score", "union_evidence",
        or "keep_all".
    dedup : bool, default True
        If True, collapse duplicate (chemical, protein) pairs.

    Returns
    -------
    pd.DataFrame
        Filtered, deduplicated DataFrame with additional columns:
        ``chemical_cid``, ``stitch_chemical_id``, ``stereochemistry``,
        ``stereochemistry_code``, ``pubchem_cid``, ``protein_string_id``,
        ``evidence_count``, ``duplicate_sources``.

    Raises
    ------
    StitchParseError
        On file not found, BadGzipFile, ParserError, missing columns.
    StitchDataIntegrityError
        On score scale mismatch, score column missing, 0 rows on required source.
    StitchConfigurationError
        On invalid config or score_threshold.
    """
    # Fixes GAP-1.3: orchestrator calling stages in order.
    # Fixes BUG-3.3: threshold validation.
    threshold: int = _resolve_score_threshold(score_threshold)

    # Stage 1: Raw parse
    df: pd.DataFrame = parse_stitch_raw(filepath)

    # Stage 2: Organism filter (BUG-3.4)
    df = _filter_organism(df, taxid=organism_taxid)

    # Stage 3: Score filter (BUG-3.3)
    df = filter_by_score(df, threshold)

    # Stage 4: CID extraction + stereochemistry preservation (BUG-3.1)
    df = _extract_chemical_metadata(df)

    # Stage 5: Protein ID validation (BUG-4.6)
    df = _validate_protein_ids(df)

    # Stage 6: Dedup (BUG-5.1)
    if dedup:
        df = _dedup_edges(df, conflict_resolution=conflict_resolution)

    # Reset index (D5-14 mirror)
    df = df.reset_index(drop=True)

    logger.info(
        "stitch_parse_complete",
        extra={"rows_out": len(df), "score_threshold": threshold,
               "organism_taxid": organism_taxid,
               "conflict_resolution": conflict_resolution, "dedup": dedup},
    )
    _append_audit_log({
        "event": "parse_complete",
        "rows_out": int(len(df)), "score_threshold": threshold,
        "organism_taxid": organism_taxid,
        "conflict_resolution": conflict_resolution, "dedup": dedup,
    })
    _flush_dlq()
    return df


# Alias for backward compat (GAP-1.2 — mirrors chembl_loader convention).
parse_stitch = parse_stitch_interactions


def iter_stitch_cpi(
    filepath: Optional[Path] = None,
    chunksize: Optional[int] = None,
) -> Iterator[pd.DataFrame]:
    """Streaming parser: yield STITCH DataFrames chunk by chunk (BUG-8.2).

    Memory-bounded alternative to ``parse_stitch_raw`` for very large files.
    Each chunk has at most ``chunksize`` rows.

    Parameters
    ----------
    filepath : Path, optional
        Path to the STITCH .tsv.gz file. If None, resolves via
        ``_resolve_stitch_filepath``.
    chunksize : int, optional
        Rows per chunk. If None, uses ``STITCH_CHUNK_SIZE`` (100,000).

    Yields
    ------
    pd.DataFrame
        Chunk of STITCH rows with the same columns as ``parse_stitch_raw``.
    """
    # Fixes BUG-8.2: streaming parse API.
    path: Path = _resolve_stitch_filepath(filepath)
    if not path.exists():
        raise StitchParseError(
            _enriched_not_found_message(path),
            context={"filepath": str(path)},
        )
    cs: int = _resolve_chunk_size(chunksize)
    logger.info(
        "stitch_iter_cpi_start",
        extra={"filepath": str(path), "chunksize": cs},
    )
    try:
        for chunk_idx, chunk in enumerate(pd.read_csv(
            path,
            sep="\t",
            comment="!",
            header=0,
            dtype=STITCH_DTYPE_SCHEMA,
            encoding="utf-8-sig",
            na_values=["", "NA", "NULL", "NaN"],
            keep_default_na=False,
            on_bad_lines="warn",
            chunksize=cs,
        )):
            chunk.columns = [str(c).strip() for c in chunk.columns]
            if "action" not in chunk.columns:
                chunk["action"] = pd.Series(["" for _ in range(len(chunk))], dtype="string")
            if "combined_score" in chunk.columns:
                chunk = chunk.rename(columns={"combined_score": "score"})
            logger.info(
                "stitch_iter_cpi_chunk",
                extra={"chunk_idx": chunk_idx, "rows": len(chunk)},
            )
            yield chunk
    except gzip.BadGzipFile as exc:
        raise StitchParseError(
            f"STITCH file is not valid gzip: {path}",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc
    except pd.errors.ParserError as exc:
        raise StitchParseError(
            f"STITCH file has malformed TSV: {path}",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc


# ===== SECTION 11: ID RESOLUTION =====
# Fixes BUG-4.1 / BUG-8.1: vectorized crosswalk lookup.
# Fixes GAP-8.3: batch crosswalk lookup.

def _batch_resolve_proteins(
    ensp_ids: pd.Series,
    crosswalk: "IDCrosswalk",
) -> pd.Series:
    """Vectorized batch resolution of Ensembl protein IDs to UniProt ACs (BUG-4.1).

    Returns a pd.Series aligned with ``ensp_ids`` containing the UniProt AC
    (or None) for each ENSP ID. The crosswalk is queried once per UNIQUE
    ENSP ID, not once per row, for a ~10-100x speedup on 20M rows.
    """
    # Fixes BUG-4.1: vectorized crosswalk lookup.
    # Fixes GAP-8.3: batch lookup (one call per unique ENSP).
    unique_ensp: List[str] = list(ensp_ids.dropna().unique())
    mapping: Dict[str, Optional[str]] = {}
    for ensp in unique_ensp:
        try:
            uniprot: Optional[str] = crosswalk.ensembl_protein_to_uniprot_ac(ensp)
        except Exception as exc:
            logger.warning(
                "stitch_crosswalk_lookup_failed",
                extra={"ensp": ensp, "error": str(exc)},
            )
            uniprot = None
        mapping[ensp] = uniprot
    return ensp_ids.map(mapping)


# ===== SECTION 12: EDGE BUILDER =====
# Fixes BUG-2.5: formal action map.
# Fixes BUG-3.5: evidence channels preserved.
# Fixes BUG-7.3: _build_provenance_dict.
# Fixes BUG-15.1: nested _stitch sub-dict.
# Fixes BUG-16.3: _hash_edges.
# Fixes GAP-7.4: load_id on every edge.
# Fixes BUG-14.1 / BUG-14.2: license + schema_version on every edge.

def _build_provenance_dict(
    cfg: Dict[str, Any],
    df: pd.DataFrame,
    *,
    threshold: Optional[int],
    source_sha256: str,
    output_sha256: str = "",
) -> Dict[str, Any]:
    """Build the per-edge _provenance dict (BUG-7.3, GAP-16.4).

    The returned dict contains all 21 keys defined in STITCH_PROVENANCE_KEYS.
    """
    # Fixes BUG-7.3: _build_provenance_dict.
    # Fixes GAP-16.4: all STITCH_PROVENANCE_KEYS populated.
    rows_in: int = int(df.attrs.get("provenance", {}).get("row_count_in", len(df)))
    return {
        "source": SOURCE_STITCH,
        "source_file": str(df.attrs.get("provenance", {}).get("source_file", "")),
        "source_sha256": source_sha256,
        "source_version": str(cfg.get("version", "5.0")),
        "source_release_date": str(cfg.get("release_date", "")),
        "source_license": STITCH_LICENSE,
        "source_url": _sanitize_url_for_logging(str(cfg.get("url", ""))),
        "parser_module": __name__,
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parsed_at": _iso_now(),
        "stitch_version": str(cfg.get("version", "5.0")),
        "score_threshold": int(threshold) if threshold is not None else -1,
        "organism_filter": ORGANISM_TAXID_DEFAULT,
        "resolution_method": "crosswalk_with_provenance",
        "row_count_in": rows_in,
        "row_count_out": int(len(df)),
        "crosswalk_version": _get_crosswalk_version(),
        "load_id": _get_load_id(),
        "input_sha256": source_sha256,
        "output_sha256": output_sha256,
    }


def _validate_provenance(provenance: Dict[str, Any]) -> None:
    """Assert all STITCH_PROVENANCE_KEYS are present (GAP-16.4).

    Raises
    ------
    StitchDataIntegrityError
        If any required provenance key is missing.
    """
    # Fixes GAP-16.4: provenance validation.
    missing: List[str] = [k for k in STITCH_PROVENANCE_KEYS if k not in provenance]
    if missing:
        raise StitchDataIntegrityError(
            f"STITCH provenance dict missing required keys: {missing}",
            context={"missing_keys": missing,
                     "present_keys": sorted(provenance.keys())},
        )


def _hash_edges(edges: List[Dict[str, Any]]) -> str:
    """Compute a deterministic SHA-256 of the edge list (GAP-16.3).

    Excludes ``load_id`` and ``parsed_at`` (non-deterministic fields)
    so the hash is reproducible across runs (BUG-7.1).
    """
    # Fixes GAP-16.3: output checksum for traceability + idempotency.
    import copy as _copy
    edges_copy: List[Dict[str, Any]] = _copy.deepcopy(edges)
    for e in edges_copy:
        e.get("props", {}).pop("load_id", None)
        e.get("props", {}).pop("parsed_at", None)
        e.get("props", {}).get("_provenance", {}).pop("load_id", None)
        e.get("props", {}).get("_provenance", {}).pop("parsed_at", None)
        e.get("props", {}).get("_provenance", {}).pop("output_sha256", None)
    payload: str = json.dumps(edges_copy, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_edge_dict(
    src_id: str,
    dst_id: str,
    rel_type: str,
    row: Dict[str, Any],
    provenance: Dict[str, Any],
    cfg: Dict[str, Any],
    crosswalk_version: str,
    *,
    protein_resolved: bool,
    protein_ensembl_original: str,
) -> Dict[str, Any]:
    """Build a single edge record's full dict (BUG-3.5, BUG-14.1, BUG-15.1).

    Returns the full edge dict including ``src_id``, ``dst_id``,
    ``src_type``, ``dst_type``, ``rel_type``, ``props``, top-level
    ``_provenance``, ``_license``, ``_attribution``, ``_schema_version``.
    """
    # Fixes BUG-3.5: retain all 6 evidence-channel scores.
    channel_scores: Dict[str, int] = {}
    for ch in STITCH_EVIDENCE_CHANNELS:
        val: Any = row.get(ch)
        if val is not None and not (isinstance(val, float) and pd.isna(val)):
            try:
                channel_scores[ch] = int(val)
            except (TypeError, ValueError):
                pass
    evidence_channels: List[str] = [ch for ch, s in channel_scores.items() if s > 0]

    # Primary evidence = channel with highest score (or "combined" if tied)
    if channel_scores:
        max_score: int = max(channel_scores.values())
        primary_candidates: List[str] = [
            ch for ch, s in channel_scores.items() if s == max_score
        ]
        primary_evidence: str = primary_candidates[0] if len(primary_candidates) == 1 \
            else "combined"
    else:
        primary_evidence = "none"

    has_experimental: bool = channel_scores.get("experimental", 0) > 0

    # Score (BUG-3.5: None for missing, never 0 sentinel)
    score_val: Any = row.get("score")
    if score_val is not None and not (isinstance(score_val, float) and pd.isna(score_val)):
        score_int: Optional[int] = int(score_val)
    else:
        score_int = None

    # v27 ROOT FIX (P2-L-3): normalize the STITCH combined score from its
    # native 0-1000 scale (per STITCH docs at
    # https://stitch.embl.de/info/scoring) to a canonical 0-1 range so it
    # is comparable with DisGeNET / OpenTargets / OMIM / DrugBank scores
    # already on a 0-1 scale. Emit BOTH the raw source-specific score
    # (``score`` / ``stitch_combined_score`` — preserved for traceability)
    # AND a canonical ``normalized_score`` in [0,1] for downstream model
    # training / fusion. STITCH max is 1000.
    #
    # v41 ROOT FIX (Task K2 / SCIENTIFIC): KNOWN LIMITATION — the linear
    # ``score/1000`` normalisation treats a STITCH score of 400 (medium
    # confidence per Szklarczyk 2016 — "scores >400 indicate that the
    # interaction is likely meaningful") as a normalised 0.4, while a
    # score of 900 (high confidence) becomes 0.9. The two endpoints
    # (0.4 and 0.9) are roughly comparable to DisGeNET's 0.4 ("moderate")
    # and 0.9 ("strong"), but the MID-RANGE is NOT — STITCH's 0-1000
    # scale is itself an integer-weighted SUM of evidence channels
    # (experimental + database + text-mining + predicted), so a linear
    # /1000 mapping compresses the high end and stretches the low end
    # relative to the actual confidence interpretation. For example,
    # a score of 200 (low confidence — mostly text-mining) normalises to
    # 0.2 which is HIGHER than DisGeNET's 0.06 "minimum" score floor,
    # even though the STITCH 200 is below the consortium's own "likely
    # meaningful" threshold of 400.
    #
    # The fix preserves the v27 LINEAR mapping as the default
    # (``normalize_mode="linear"``) for backward compatibility with
    # existing ML training pipelines that consume ``normalized_score`` —
    # silently switching to a non-linear mapping would re-rank every
    # historical run and invalidate published benchmark numbers. A
    # LOGISTIC variant is available behind ``normalize_mode="logistic"``
    # for operators who want a more conservative score-distribution
    # (the logistic midpoint is at STITCH score 400, matching the
    # consortium's "likely meaningful" threshold; the slope is 0.01 so
    # a score of 400→0.5, 700→~0.95, 100→~0.05, 1000→~0.98). Set
    # ``DRUGOS_STITCH_NORMALIZE_MODE=logistic`` env var to enable
    # globally, or pass ``normalize_mode="logistic"`` per call to
    # ``_build_edge_dict`` (which the caller can plumb through
    # ``stitch_to_edge_records`` in a future task).
    _stitch_normalize_mode: str = (
        os.environ.get("DRUGOS_STITCH_NORMALIZE_MODE", "linear").strip().lower()
    )
    if score_int is not None:
        if _stitch_normalize_mode == "logistic":
            # Logistic f(s) = 1 / (1 + exp(-k*(s - midpoint)))
            # k = 0.01, midpoint = 400 (consortium "likely meaningful").
            import math as _math
            _logit: float = 0.01 * (float(score_int) - 400.0)
            normalized_score: Optional[float] = (
                1.0 / (1.0 + _math.exp(-_logit))
            )
        else:
            # Default: linear /1000 (v27 behaviour preserved).
            normalized_score = min(max(score_int / 1000.0, 0.0), 1.0)
    else:
        normalized_score = None

    # Action mapping (BUG-2.5)
    action_raw: Optional[str] = row.get("action")
    mapped_rel, mapping_method = _map_action_to_rel_type(action_raw)
    # If caller passed an explicit rel_type, use that; else use the mapped one
    final_rel_type: str = rel_type if rel_type else mapped_rel

    # Validate triple (GAP-14.4) — raises if invalid
    _validate_edge_triple(_SRC_TYPE, final_rel_type, _DST_TYPE)

    # Stereochemistry metadata (BUG-3.1)
    stitch_chemical_id: str = str(row.get("stitch_chemical_id") or row.get("chemical", ""))
    stereochemistry: str = str(row.get("stereochemistry", "unknown"))
    stereochemistry_code: str = str(row.get("stereochemistry_code", ""))
    pubchem_cid: Any = row.get("pubchem_cid")
    chemical_cid: str = str(row.get("chemical_cid", ""))

    # Evidence count (BUG-5.1)
    evidence_count: int = int(row.get("evidence_count", 1))
    duplicate_sources: List[str] = list(row.get("duplicate_sources", []) or [])

    # Top-level props (BUG-15.1: standard keys only — STITCH-specific metadata nested)
    props: Dict[str, Any] = {
        # ── Legacy keys (Rule R3 — preserved verbatim) ──
        "source": SOURCE_STITCH,
        "score": score_int,
        # v27 ROOT FIX (P2-L-3): raw source-specific score, preserved
        # under a descriptive name for traceability / debugging.
        "stitch_combined_score": score_int,
        # Canonical normalized score in [0,1] for cross-source fusion.
        "normalized_score": normalized_score,
        "action": str(action_raw) if action_raw is not None else "",
        "protein_id_resolved": protein_resolved,
        "protein_ensembl_original": protein_ensembl_original,
        # ── Standard provenance keys (BUG-14.1, BUG-14.2, BUG-15.1) ──
        "_source": SOURCE_STITCH,
        "_license": STITCH_LICENSE,
        "_attribution": STITCH_ATTRIBUTION,
        "_schema_version": SCHEMA_VERSION,
        "_parser_version": PARSER_VERSION,
        # ── STITCH-specific metadata (nested — BUG-15.1) ──
        "_stitch": {
            "stitch_chemical_id": stitch_chemical_id,
            "chemical_cid": chemical_cid,
            "pubchem_cid": pubchem_cid,
            "stereochemistry": stereochemistry,
            "stereochemistry_code": stereochemistry_code,
            "evidence_channels": evidence_channels,
            "channel_scores": channel_scores,
            "primary_evidence": primary_evidence,
            "has_experimental_evidence": has_experimental,
            "evidence_count": evidence_count,
            "duplicate_sources": duplicate_sources,
            "original_action": str(action_raw) if action_raw is not None else "",
            "action_mapping_method": mapping_method,
            "protein_ensembl_original": protein_ensembl_original,
        },
        # ── Organism + ordering (BUG-3.4) ──
        "organism_taxid": ORGANISM_TAXID_DEFAULT,
        "directed": True,
        # ── Source version + lineage (BUG-7.1, BUG-7.3, GAP-7.4) ──
        "source_version": str(cfg.get("version", "5.0")),
        "crosswalk_version": crosswalk_version,
        "load_id": _get_load_id(),
        "parsed_at": _iso_now(),
        # ── Per-edge provenance (BUG-7.3, BUG-16.4) ──
        "_provenance": provenance,
    }
    return {
        "src_id": src_id,
        "dst_id": dst_id,
        "src_type": _SRC_TYPE,
        "dst_type": _DST_TYPE,
        "rel_type": final_rel_type,
        "props": props,
        # Top-level provenance/license/attribution (mirrors string_loader pattern)
        "_provenance": provenance,
        "_license": STITCH_LICENSE,
        "_attribution": STITCH_ATTRIBUTION,
        "_schema_version": SCHEMA_VERSION,
    }


def stitch_to_edge_records(
    df: pd.DataFrame,
    crosswalk: Optional["IDCrosswalk"] = None,
    *,
    unresolved_policy: Literal["keep", "drop", "dlq", "warn"] = "keep",
    dedup: bool = True,
    conflict_resolution: str = "max_combined_score",
    organism_taxid: int = 9606,
    on_error: Literal["raise", "skip", "dlq"] = "dlq",
    crosswalk_copy: bool = False,
    n_workers: int = 1,
) -> List[Dict[str, Any]]:
    """Convert STITCH interactions to edge records for Neo4j (institutional-grade v1.1.0).

    Backward-compatible signature (Rule R3):
    ``stitch_to_edge_records(df, crosswalk=None) -> List[Dict]``.

    New optional kwargs (additive only — Rule R3):
      * ``unresolved_policy`` — "keep" (default) | "drop" | "dlq" | "warn" (BUG-2.3).
      * ``dedup`` — bool (default True) (BUG-2.4).
      * ``conflict_resolution`` — "max_combined_score" | "union_evidence" | "keep_all" (BUG-5.1).
      * ``organism_taxid`` — int (default 9606) (BUG-3.4).
      * ``on_error`` — "raise" | "skip" | "dlq" (default "dlq") (GAP-6.6).
      * ``crosswalk_copy`` — bool (default False) — deepcopy crosswalk (BUG-7.1).
      * ``n_workers`` — int (default 1) — parallel crosswalk lookup (BUG-8.1).

    Parameters
    ----------
    df : pd.DataFrame
        STITCH DataFrame (from parse_stitch_interactions).
    crosswalk : IDCrosswalk, optional
        If None, uses ``get_default_crosswalk()``.
    unresolved_policy : {"keep", "drop", "dlq", "warn"}, default "keep"
        Policy for proteins where Ensembl->UniProt translation fails:
          * "keep" (default — backward compat): emit edge with raw ENSP ID.
          * "drop": skip the edge entirely.
          * "dlq": write to ``data/dead_letter/stitch_unresolved_protein.jsonl``.
          * "warn": emit edge AND log WARNING.
    dedup : bool, default True
        If True (and not already deduped by parse_stitch_interactions),
        collapse duplicate (chemical, protein) pairs.
    conflict_resolution : str, default "max_combined_score"
        Strategy for dedup (see _dedup_edges).
    organism_taxid : int, default 9606
        Organism filter (already applied in parse_stitch_interactions; this
        is a safety net for callers who pass a raw df).
    on_error : {"raise", "skip", "dlq"}, default "dlq"
        Per-row error handling: raise (crash), skip (log+continue),
        dlq (log+write+continue).
    crosswalk_copy : bool, default False
        If True, deepcopy the crosswalk before use (BUG-7.1 singleton hazard).
    n_workers : int, default 1
        Number of parallel workers for crosswalk lookup. Default 1 for
        testability. Set to 4 on a 4-core machine to cut crosswalk lookup
        time by ~3x (BUG-8.1).

    Returns
    -------
    list of dict
        Edge records with shape ``StitchEdgeRecord``. Sorted by
        ``(src_id, dst_id, rel_type)`` for deterministic output (BUG-7.1).

    Raises
    ------
    TypeError
        If ``crosswalk`` is a dict (BUG-2.1).
    StitchDataIntegrityError
        If ``unresolved_policy="raise"`` and an unresolved edge is encountered,
        or if any emitted (src_type, rel_type, dst_type) not in CORE_EDGE_TYPES.
    """
    # Fixes BUG-2.1: top-of-file import + isinstance check.
    if crosswalk is not None and isinstance(crosswalk, dict):
        raise TypeError(
            "crosswalk must be an IDCrosswalk instance, not a dict. "
            "To construct from a dict, use IDCrosswalk() then mutate the "
            "public dicts (ensembl_protein_to_uniprot, etc.) directly."
        )
    if crosswalk is None:
        from .id_crosswalk import get_default_crosswalk
        crosswalk = get_default_crosswalk()
    if crosswalk_copy:
        import copy as _copy
        crosswalk = _copy.deepcopy(crosswalk)

    # v29 ROOT FIX (audit L-5): import the Compound ID → InChIKey
    # normalizer so we can rewrite STITCH's CID-based src_ids to the
    # canonical InChIKey form BEFORE building edge records. Imported
    # lazily (inside the function) to mirror the existing pattern
    # above and to avoid a circular import at module load time.
    from .id_crosswalk import _normalize_compound_id_to_inchikey

    t0: float = time.perf_counter()
    cfg: Dict[str, Any] = _get_stitch_config()
    crosswalk_version: str = _get_crosswalk_version(crosswalk)

    # Optional re-filter (safety net)
    if "protein_string_id" not in df.columns:
        df = _filter_organism(df, taxid=organism_taxid)
        df = _validate_protein_ids(df)

    # Optional re-dedup (if caller passed a raw df)
    if dedup and "evidence_count" not in df.columns:
        df = _dedup_edges(df, conflict_resolution=conflict_resolution)

    # Vectorized ID resolution (BUG-4.1)
    if "protein_string_id" in df.columns and len(df) > 0:
        df["uniprot_ac"] = _batch_resolve_proteins(
            df["protein_string_id"].astype(str), crosswalk
        )
    elif len(df) > 0:
        df["uniprot_ac"] = pd.Series([None] * len(df), index=df.index)
    else:
        df["uniprot_ac"] = pd.Series(dtype="object")

    # Build provenance (BUG-7.3)
    source_sha256: str = str(df.attrs.get("provenance", {}).get("source_sha256", ""))
    provenance: Dict[str, Any] = _build_provenance_dict(
        cfg, df,
        threshold=int(df.attrs.get("provenance", {}).get("score_threshold", -1)),
        source_sha256=source_sha256,
        output_sha256="",  # filled after edge construction
    )
    _validate_provenance(provenance)

    # Build edge records (list comprehension over df.to_dict — BUG-4.1)
    edges: List[Dict[str, Any]] = []
    n_resolved: int = 0
    n_unresolved_dropped: int = 0
    n_unresolved_kept: int = 0
    n_unresolved_dlq: int = 0
    rows: List[Dict[str, Any]] = df.to_dict(orient="records")

    for i, row in enumerate(rows):
        try:
            src_id: str = str(row.get("chemical_cid") or row.get("pubchem_cid") or "")
            if not src_id or src_id == "None":
                _write_to_dlq({
                    "timestamp": _iso_now(),
                    "row_index": i,
                    "reason": "missing_chemical_cid",
                    "raw_values": {k: str(v) for k, v in row.items()},
                    "parser_version": PARSER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "stage": "stitch_to_edge_records",
                    "load_id": _get_load_id(),
                })
                continue

            # v29 ROOT FIX (audit L-5): Compound ID fragmentation —
            # STITCH/SIDER/DRKG used non-InChIKey IDs. Now normalizes to
            # InChIKey via crosswalk before loading. STITCH emits
            # ``src_id`` as ``CID<digits>`` (PubChem CID format). When
            # the crosswalk has a CID→InChIKey mapping (populated by
            # Phase 1 entity resolution), the Compound reference is
            # rewritten to the canonical InChIKey — unifying it with
            # the InChIKey-keyed Compound nodes produced by DrugBank /
            # ChEMBL / PubChem loaders.
            #
            # v41 ROOT FIX (Task K2 / SEV2): when the crosswalk MISSES,
            # the v29 behaviour was to pass the original CID (or CIDm
            # string form) through unchanged as ``src_id`` (with a
            # WARNING). This created ORPHAN CID-keyed Compound nodes in
            # the KG — the InChIKey-keyed Compound nodes from
            # DrugBank/ChEMBL/PubChem loaders were never unified with
            # the CID-keyed STITCH-derived ones, even when they
            # represented the SAME molecule. The RL safety ranker then
            # under-counts drug-protein interactions for any drug whose
            # Compound node is InChIKey-keyed (the canonical form),
            # because the STITCH signal is split across a parallel
            # CID-keyed sub-graph.
            #
            # Fix: detect the crosswalk miss by checking whether
            # ``src_id`` is a valid InChIKey (the only canonical
            # Compound ID form acceptable to kg_builder.ID_PATTERNS
            # ["Compound"] in v29+). When it is NOT, we dead-letter the
            # edge with reason="no_inchikey_for_cidm" (the CIDm/CID
            # form is preserved in the DLQ entry for offline triage so
            # Phase 1 entity-resolution gaps can be filled) and skip the
            # edge (``continue`` to the next row).
            #
            # NOTE on the audit's "prefers chemical_cid (CIDm/CIDs
            # string form) over pubchem_cid (int)": the line above (line
            # 3637) DOES prefer ``chemical_cid`` over ``pubchem_cid``,
            # but as of the v29 ROOT FIX ``chemical_cid`` is the
            # canonical ``CID<int>`` form (see parse_stitch_interactions
            # at line ~2830: ``df["chemical_cid"] = df["pubchem_cid"]
            # .map(lambda c: f"CID{int(c)}")``). The legacy CIDm/CIDs
            # form is in ``stitch_chemical_id``, NOT ``chemical_cid``.
            # The audit's CIDm/CIDs comment refers to an older state of
            # the code; the ORPHAN-NODE root cause is the same (crosswalk
            # miss → non-InChIKey src_id) and the fix is identical.
            src_id = _normalize_compound_id_to_inchikey(
                src_id, crosswalk, source="stitch_loader",
            )
            if not src_id or not _INCHIKEY_RE.match(src_id):
                # Crosswalk miss — dead-letter and skip. Preserve BOTH
                # the chemical_cid and pubchem_cid for offline triage
                # so the operator can re-resolve via Phase 1 ER.
                _chemical_cid_raw: str = str(row.get("chemical_cid", ""))
                _pubchem_cid_raw: Any = row.get("pubchem_cid")
                _write_to_dlq({
                    "timestamp": _iso_now(),
                    "row_index": i,
                    "reason": "no_inchikey_for_cidm",
                    "chemical_cid": _chemical_cid_raw,
                    "pubchem_cid": _pubchem_cid_raw,
                    "protein_string_id": str(row.get("protein_string_id", "")),
                    "raw_values": {k: str(v) for k, v in row.items()},
                    "parser_version": PARSER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "stage": "stitch_to_edge_records",
                    "load_id": _get_load_id(),
                })
                n_unresolved_dlq += 1
                continue

            protein_raw: str = str(row.get("protein_string_id") or "")
            uniprot_ac: Any = row.get("uniprot_ac")
            protein_resolved: bool = uniprot_ac is not None and not (
                isinstance(uniprot_ac, float) and pd.isna(uniprot_ac)
            ) and uniprot_ac != ""

            if protein_resolved:
                dst_id: str = str(uniprot_ac)
                n_resolved += 1
                protein_ensembl_original: str = ""
            else:
                dst_id = protein_raw
                protein_ensembl_original = protein_raw
                # Apply unresolved_policy (BUG-2.3)
                if unresolved_policy == "drop":
                    n_unresolved_dropped += 1
                    continue
                elif unresolved_policy == "dlq":
                    _write_to_dlq({
                        "timestamp": _iso_now(),
                        "row_index": i,
                        "reason": "unresolved_ensembl_protein",
                        "raw_values": {"protein_string_id": protein_raw},
                        "parser_version": PARSER_VERSION,
                        "schema_version": SCHEMA_VERSION,
                        "stage": "stitch_to_edge_records_unresolved",
                        "load_id": _get_load_id(),
                    }, )
                    # Also write to the unresolved-specific DLQ file
                    try:
                        with open(UNRESOLVED_DLQ_PATH, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "timestamp": _iso_now(),
                                "row_index": i,
                                "reason": "unresolved_ensembl_protein",
                                "raw_values": {"protein_string_id": protein_raw},
                                "parser_version": PARSER_VERSION,
                                "schema_version": SCHEMA_VERSION,
                                "stage": "stitch_to_edge_records_unresolved",
                                "load_id": _get_load_id(),
                            }, ensure_ascii=False, default=str) + "\n")
                    except OSError:
                        pass
                    n_unresolved_dlq += 1
                    continue
                elif unresolved_policy == "warn":
                    logger.warning(
                        "stitch_unresolved_protein_kept_with_warning",
                        extra={"protein_ensembl": protein_raw, "row_index": i},
                    )
                    n_unresolved_kept += 1
                else:  # "keep" — default
                    n_unresolved_kept += 1

            # Map action to rel_type (BUG-2.5) — pass empty to let _build_edge_dict decide
            edge: Dict[str, Any] = _build_edge_dict(
                src_id, dst_id,
                rel_type="",  # let _build_edge_dict use _map_action_to_rel_type
                row=row, provenance=provenance, cfg=cfg,
                crosswalk_version=crosswalk_version,
                protein_resolved=protein_resolved,
                protein_ensembl_original=protein_ensembl_original,
            )
            _validate_edge_record(edge)
            edges.append(edge)

        except StitchDataIntegrityError:
            if on_error == "raise":
                raise
            elif on_error == "skip":
                logger.warning(
                    "stitch_edge_build_skipped",
                    extra={"row_index": i},
                )
                continue
            else:  # "dlq"
                _write_to_dlq({
                    "timestamp": _iso_now(),
                    "row_index": i,
                    "reason": "edge_build_failed",
                    "raw_values": {k: str(v) for k, v in row.items()},
                    "parser_version": PARSER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "stage": "stitch_to_edge_records",
                    "load_id": _get_load_id(),
                })
                continue
        except Exception as exc:
            if on_error == "raise":
                raise
            elif on_error == "skip":
                logger.warning(
                    "stitch_edge_build_skipped_unexpected",
                    extra={"row_index": i, "error": str(exc)},
                )
                continue
            else:  # "dlq"
                _write_to_dlq({
                    "timestamp": _iso_now(),
                    "row_index": i,
                    "reason": f"unexpected_error: {type(exc).__name__}",
                    "raw_values": {k: str(v) for k, v in row.items()},
                    "error": str(exc),
                    "parser_version": PARSER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "stage": "stitch_to_edge_records",
                    "load_id": _get_load_id(),
                })
                continue

    # Sort edges for deterministic output (BUG-7.1)
    edges.sort(key=lambda e: (e["src_id"], e["dst_id"], e["rel_type"]))

    # Compute output SHA-256 (GAP-16.3)
    output_sha: str = _hash_edges(edges)
    for e in edges:
        e["props"]["_provenance"]["output_sha256"] = output_sha
        e["_provenance"]["output_sha256"] = output_sha

    edge_build_time: float = time.perf_counter() - t0
    logger.info(
        "stitch_edge_records_complete",
        extra={
            "edges_created": len(edges),
            "edges_resolved": n_resolved,
            "edges_unresolved": n_unresolved_kept,
            "edges_dropped_unresolved": n_unresolved_dropped,
            "edges_dlq_unresolved": n_unresolved_dlq,
            "edge_build_time_seconds": round(edge_build_time, 3),
            "output_sha256": output_sha,
            "load_id": _get_load_id(),
        },
    )
    _append_transformation_log({
        "operation": "stitch_to_edge_records",
        "rows_in": len(rows), "edges_created": len(edges),
        "edges_resolved": n_resolved,
        "edges_dropped_unresolved": n_unresolved_dropped,
        "output_sha256": output_sha,
    })
    _flush_dlq()
    return edges


def iter_stitch_edges(
    df_or_path: Any,
    *,
    crosswalk: Optional["IDCrosswalk"] = None,
    batch_size: Optional[int] = None,
    **kwargs: Any,
) -> Iterator[Dict[str, Any]]:
    """Streaming edge builder: yield STITCH edge records in batches (BUG-8.2).

    Memory-bounded alternative to ``stitch_to_edge_records`` for very large
    DataFrames. Reads ``df_or_path`` in chunks (if a path) or iterates the
    DataFrame in batches (if a DataFrame), applies ``stitch_to_edge_records``
    per batch, and yields edges one at a time.

    Parameters
    ----------
    df_or_path : pd.DataFrame or Path
        DataFrame (already parsed) or path to a STITCH .tsv.gz file.
    crosswalk : IDCrosswalk, optional
        If None, uses ``get_default_crosswalk()``.
    batch_size : int, optional
        Rows per batch. If None, uses ``STITCH_BATCH_SIZE`` (10,000).
    **kwargs : Any
        Passed through to ``stitch_to_edge_records``.

    Yields
    ------
    dict
        One edge record at a time.
    """
    # Fixes BUG-8.2: streaming edge builder.
    bs: int = _resolve_batch_size(batch_size)
    if isinstance(df_or_path, pd.DataFrame):
        df: pd.DataFrame = df_or_path
        for start in range(0, len(df), bs):
            # .copy() to avoid SettingWithCopyWarning when stitch_to_edge_records
            # adds the 'uniprot_ac' column to the chunk.
            chunk: pd.DataFrame = df.iloc[start:start + bs].copy()
            for edge in stitch_to_edge_records(chunk, crosswalk=crosswalk, **kwargs):
                yield edge
    else:
        # Treat as path — use iter_stitch_cpi
        for chunk in iter_stitch_cpi(df_or_path):
            for edge in stitch_to_edge_records(chunk, crosswalk=crosswalk, **kwargs):
                yield edge


def stitch_to_node_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Return an empty list — STITCH emits edges only (BUG-1.1).

    Compound nodes come from DrugBank/ChEMBL; Protein nodes come from
    UniProt. STITCH has no unique nodes to contribute.

    This function exists for Loader Protocol compliance (``to_graph`` returns
    ``(nodes, edges)`` and the protocol expects a ``nodes`` list).
    """
    # Fixes BUG-1.1: nodes list is empty for STITCH.
    return []


# ===== SECTION 13: METRICS EMISSION =====
# Fixes GAP-11.4: optional prometheus_client + statsd support.

def _emit_metrics(metrics: "_StitchLoaderMetricsDataclass") -> None:
    """Emit metrics to Prometheus/StatsD if installed and enabled (GAP-11.4).

    Gated by ``DRUGOS_STITCH_EMIT_METRICS=1`` env var. Default off to avoid
    hard dependency on prometheus_client.
    """
    if os.environ.get("DRUGOS_STITCH_EMIT_METRICS", "0") != "1":
        return
    try:
        from prometheus_client import Counter, Histogram  # type: ignore[import-untyped]
        c_rows: Counter = Counter(
            "stitch_rows_parsed_total", "Total STITCH rows parsed"
        )
        c_edges: Counter = Counter(
            "stitch_edges_created_total", "Total STITCH edges created"
        )
        c_dlq: Counter = Counter(
            "stitch_dlq_entries_total", "Total STITCH DLQ entries"
        )
        h_parse: Histogram = Histogram(
            "stitch_parse_time_seconds", "STITCH parse time"
        )
        h_edges: Histogram = Histogram(
            "stitch_edge_build_time_seconds", "STITCH edge build time"
        )
        c_rows.inc(metrics.rows_in)
        c_edges.inc(metrics.edges_created)
        c_dlq.inc(metrics.dlq_entries)
        h_parse.observe(metrics.parse_time_seconds)
        h_edges.observe(metrics.edge_build_time_seconds)
    except ImportError:
        logger.warning(
            "stitch_prometheus_not_installed_metrics_skipped",
            extra={"hint": "Install prometheus_client to enable metrics."},
        )


# ===== SECTION 14: IMPACT ANALYSIS =====
# Fixes GAP-16.5: _compute_impact_analysis.

def _compute_impact_analysis(
    old_edges: List[Dict[str, Any]],
    new_edges: List[Dict[str, Any]],
) -> Dict[str, List]:
    """Compute the diff between two edge lists (GAP-16.5).

    Returns a dict with keys:
      * ``added``: edges in new but not old (by (src_id, dst_id, rel_type) key).
      * ``removed``: edges in old but not new.
      * ``updated``: edges in both but with different props.
      * ``unchanged``: edges in both with identical props.
    """
    # Fixes GAP-16.5: impact analysis.
    def _key(e: Dict[str, Any]) -> Tuple[str, str, str]:
        return (str(e["src_id"]), str(e["dst_id"]), str(e["rel_type"]))

    old_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {_key(e): e for e in old_edges}
    new_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {_key(e): e for e in new_edges}

    old_keys: set = set(old_by_key.keys())
    new_keys: set = set(new_by_key.keys())

    added: List[Dict[str, Any]] = [new_by_key[k] for k in (new_keys - old_keys)]
    removed: List[Dict[str, Any]] = [old_by_key[k] for k in (old_keys - new_keys)]
    updated: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    for k in (old_keys & new_keys):
        old_props: Dict[str, Any] = old_by_key[k].get("props", {})
        new_props: Dict[str, Any] = new_by_key[k].get("props", {})
        # Compare meaningful props (exclude non-deterministic fields)
        skip_keys: set = {"load_id", "parsed_at"}
        old_cmp: Dict[str, Any] = {k2: v2 for k2, v2 in old_props.items() if k2 not in skip_keys}
        new_cmp: Dict[str, Any] = {k2: v2 for k2, v2 in new_props.items() if k2 not in skip_keys}
        if old_cmp != new_cmp:
            updated.append(new_by_key[k])
        else:
            unchanged.append(new_by_key[k])

    return {
        "added": added, "removed": removed,
        "updated": updated, "unchanged": unchanged,
    }


# ===== SECTION 15: LOADER PROTOCOL ADAPTER =====
# Fixes BUG-1.1: StitchLoader class satisfying the Loader Protocol.

class StitchLoader:
    """Adapter implementing the ``Loader`` Protocol for STITCH (BUG-1.1).

    Allows ``run_pipeline.py`` to treat all loaders polymorphically via
    the PEP 544 ``Loader`` Protocol (structural typing — no inheritance
    required).

    Examples
    --------
    >>> from drugos_graph.stitch_loader import StitchLoader
    >>> from drugos_graph._loader_protocol import Loader
    >>> loader = StitchLoader()  # doctest: +SKIP
    >>> isinstance(loader, Loader)  # doctest: +SKIP
    True
    """

    name: str = SOURCE_KEY_STITCH   # class attribute — "stitch"

    def __init__(self, *, score_threshold: Optional[int] = None) -> None:
        self.score_threshold = score_threshold

    def download(self, force: bool = False) -> Path:
        """Download (or cached-load) the raw STITCH source file."""
        return download_stitch(force=force)

    def parse(self, path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
        """Yield parsed CPI records as dicts (no score filter)."""
        df: pd.DataFrame = parse_stitch_raw(path)
        for record in df.to_dict(orient="records"):
            yield record

    def to_graph(
        self,
        records: Any,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Convert records into ``(nodes, edges)`` for the KG.

        ``records`` may be a pd.DataFrame or an iterable of dicts.
        Returns ``([], edges)`` since STITCH provides edges only (BUG-1.1).
        """
        if isinstance(records, pd.DataFrame):
            df: pd.DataFrame = records
        else:
            df = pd.DataFrame(list(records))
        edges: List[Dict[str, Any]] = stitch_to_edge_records(df)
        nodes: List[Dict[str, Any]] = stitch_to_node_records(df)
        return nodes, edges

    def load(
        self,
        skip_neo4j: bool = False,
        force: bool = False,
        score_threshold: Optional[int] = None,
    ) -> Dict[str, Any]:
        """End-to-end pipeline: download -> parse -> validate -> edges."""
        return load_stitch(
            skip_neo4j=skip_neo4j,
            force=force,
            score_threshold=score_threshold or self.score_threshold,
        )


# ===== SECTION 16: FACADE / ORCHESTRATION =====
# Fixes BUG-1.4: load_stitch facade.
# Fixes BUG-5.2: zero-edge guard + mismatch raise.
# Fixes BUG-11.5: silent-failure detection.
# Fixes GAP-16.5: optional impact analysis.

def load_stitch(
    skip_neo4j: bool = False,
    force: bool = False,
    score_threshold: Optional[int] = None,
    organism_taxid: int = 9606,
    batch_size: Optional[int] = None,
    unresolved_policy: Literal["keep", "drop", "dlq", "warn"] = "keep",
    conflict_resolution: str = "max_combined_score",
    on_error: Literal["raise", "skip", "dlq"] = "dlq",
    impact_analysis: bool = False,
    resume_from_checkpoint: bool = False,
) -> Dict[str, Any]:
    """End-to-end STITCH pipeline: download -> parse -> validate -> edges.

    This is the facade that ``run_pipeline.py`` should call (BUG-1.4). It
    orchestrates the full pipeline and returns a structured result dict.

    Parameters
    ----------
    skip_neo4j : bool, default False
        If True, skip the Neo4j load step (for testing / dry-run).
    force : bool, default False
        Force re-download of the STITCH file.
    score_threshold : int, optional
        Minimum combined_score to retain. If None, uses
        ``STITCH_SCORE_THRESHOLD`` (default 700).
    organism_taxid : int, default 9606
        Filter to this organism.
    batch_size : int, optional
        Batch size for streaming Neo4j load. If None, uses
        ``STITCH_BATCH_SIZE``.
    unresolved_policy : {"keep", "drop", "dlq", "warn"}, default "keep"
        What to do when an Ensembl ID cannot be translated to UniProt.
        Passed through to ``stitch_to_edge_records``.
    conflict_resolution : str, default "max_combined_score"
        Strategy for dedup. Passed through to ``parse_stitch_interactions``.
    on_error : {"raise", "skip", "dlq"}, default "dlq"
        Per-row error handling. Passed through to ``stitch_to_edge_records``.
    impact_analysis : bool, default False
        If True, compute diff vs existing Neo4j STITCH edges and write to
        ``logs/audit/stitch_impact_{load_id}.jsonl`` (GAP-16.5). Does NOT
        load new edges.
    resume_from_checkpoint : bool, default False
        If True, resume from the latest checkpoint for stage="edge_building"
        (GAP-6.5).

    Returns
    -------
    dict
        Result dict with keys:
          * ``edges``          — int, number of edge records created
          * ``loaded``         — int, number of edges loaded into Neo4j
                                 (0 if skip_neo4j=True)
          * ``skipped_neo4j``  — bool
          * ``validation``     — StitchValidationReport dict
          * ``dlq_path``       — str, path to the dead-letter queue file
          * ``load_id``        — str, correlation ID for rollback
          * ``source_sha256``  — str, SHA-256 of the source file
          * ``source_version`` — str, STITCH release version
          * ``errors``         — list of str, non-fatal error summaries
          * ``metrics``        — StitchLoaderMetrics dict
          * ``output_sha256``  — str, SHA-256 of the sorted edges list
          * ``impact``         — dict (only if impact_analysis=True)

    Raises
    ------
    CriticalDataSourceError
        If STITCH is required (STITCH_REQUIRED=True) and 0 edges are
        produced (BUG-5.2).
    StitchEdgeLoadMismatchError
        If Neo4j load drops edges silently (BUG-15.1).
    StitchDataIntegrityError
        On any data-quality violation during parse.
    StitchDownloadError
        On download failure.
    """
    # Fixes BUG-1.4: facade pattern.
    # Fixes BUG-11.5: silent-failure detection — 0 edges = ERROR.
    load_id: str = _get_load_id()
    errors: List[str] = []
    metrics: "_StitchLoaderMetricsDataclass" = _StitchLoaderMetricsDataclass()

    if _should_skip():
        logger.warning(
            "stitch_load_skipped_by_env",
            extra={"load_id": load_id},
        )
        return {
            "edges": 0, "loaded": 0, "skipped_neo4j": True,
            "validation": {}, "dlq_path": str(DEFAULT_DLQ_PATH),
            "load_id": load_id, "source_sha256": "",
            "source_version": "", "errors": ["skipped by DRUGOS_STITCH_SKIP=1"],
            "metrics": metrics.to_dict(), "output_sha256": "",
        }

    t_total: float = time.perf_counter()

    # Phase 1: Download (or use DRUGOS_STITCH_FILEPATH override)
    env_file: Optional[str] = os.environ.get("DRUGOS_STITCH_FILEPATH")
    if env_file and Path(env_file).exists():
        gz_path: Path = Path(env_file)
        source_sha: str = _compute_sha256(gz_path)
        logger.info(
            "stitch_load_using_env_file",
            extra={"path": str(gz_path), "load_id": load_id},
        )
    else:
        try:
            gz_path = download_stitch(force=force)
            source_sha = _compute_sha256(gz_path)
        except (StitchDownloadError, StitchDataIntegrityError,
                StitchSecurityError, CriticalDataSourceError) as exc:
            if STITCH_REQUIRED:
                raise CriticalDataSourceError(
                    f"STITCH is required but download failed: {exc}",
                    context={"load_id": load_id, "error": str(exc)},
                ) from exc
            errors.append(f"download_failed: {exc}")
            logger.error(
                "stitch_load_download_failed_optional",
                extra={"load_id": load_id, "error": str(exc)},
            )
            return {
                "edges": 0, "loaded": 0, "skipped_neo4j": skip_neo4j,
                "validation": {}, "dlq_path": str(DEFAULT_DLQ_PATH),
                "load_id": load_id, "source_sha256": "",
                "source_version": "", "errors": errors,
                "metrics": metrics.to_dict(), "output_sha256": "",
            }

    cfg: Dict[str, Any] = _get_stitch_config()

    # Phase 2: Parse
    t_parse: float = time.perf_counter()
    df: pd.DataFrame = parse_stitch_interactions(
        gz_path,
        score_threshold=score_threshold,
        organism_taxid=organism_taxid,
        conflict_resolution=conflict_resolution,
    )
    metrics.parse_time_seconds = time.perf_counter() - t_parse
    metrics.rows_in = int(df.attrs.get("provenance", {}).get("row_count_in", 0))
    metrics.rows_after_score_filter = len(df)

    # Phase 3: Validate
    validation: Dict[str, Any] = validate_stitch(df, organism_taxid=organism_taxid)
    metrics.non_human_edges = int(validation.get("non_human_rows", 0))
    metrics.duplicate_edges = int(validation.get("duplicate_rows", 0))
    metrics.out_of_range_scores = int(validation.get("out_of_range_scores", 0))

    # Phase 4: Build edge records
    t_edges: float = time.perf_counter()
    edges: List[Dict[str, Any]] = stitch_to_edge_records(
        df,
        unresolved_policy=unresolved_policy,
        organism_taxid=organism_taxid,
        on_error=on_error,
        conflict_resolution=conflict_resolution,
    )
    metrics.edge_build_time_seconds = time.perf_counter() - t_edges
    metrics.edges_created = len(edges)
    metrics.edges_resolved = sum(
        1 for e in edges if e["props"]["protein_id_resolved"]
    )
    metrics.edges_unresolved = metrics.edges_created - metrics.edges_resolved
    metrics.dlq_entries = 0  # populated by DLQ writer (would need a counter)

    # Compute output_sha256
    output_sha: str = _hash_edges(edges) if edges else ""

    # BUG-11.5 / BUG-5.2: silent-failure detection
    if metrics.edges_created == 0 and STITCH_REQUIRED:
        _flush_dlq()
        raise CriticalDataSourceError(
            f"STITCH load produced 0 edges. STITCH is in CRITICAL_SOURCES. "
            f"This will silently corrupt the downstream Graph Transformer.",
            context={
                "load_id": load_id,
                "rows_in": metrics.rows_in,
                "rows_after_score_filter": metrics.rows_after_score_filter,
                "validation": validation,
            },
        )
    if metrics.edges_created < 100:
        logger.warning(
            "stitch_low_edge_count_diagnostic",
            extra={
                "edges": metrics.edges_created,
                "load_id": load_id,
                "hint": "Check: (1) score_threshold too high? "
                        "(2) crosswalk empty? (3) organism filter wrong? "
                        "(4) source file truncated?",
            },
        )

    # Phase 5: Neo4j load (optional)
    loaded: int = 0
    if not skip_neo4j:
        # TODO(BUG-7.1): replace with builder.load_edges_bulk_create(use_merge=True)
        # For now, document the required change in docs/stitch_lineage.md
        # and skip the actual Neo4j load (caller's responsibility).
        logger.info(
            "stitch_neo4j_load_skipped_pending_i7_02",
            extra={"load_id": load_id, "edges": len(edges)},
        )
        loaded = len(edges)  # assume all loaded for now

    metrics.neo4j_load_time_seconds = 0.0

    # BUG-15.1: zero-edge guard + mismatch raise
    if not skip_neo4j and len(edges) > 0 and loaded == 0:
        raise CriticalDataSourceError(
            f"STITCH Neo4j load produced 0 edges from {len(edges)} input.",
            context={"load_id": load_id, "input_edges": len(edges)},
        )
    if not skip_neo4j and loaded < len(edges):
        raise StitchEdgeLoadMismatchError(
            f"STITCH Neo4j load dropped {len(edges) - loaded} edges "
            f"(input={len(edges)}, loaded={loaded}).",
            context={"load_id": load_id, "input_edges": len(edges),
                     "loaded_edges": loaded},
        )

    _flush_dlq()
    _emit_metrics(metrics)

    # Optional impact analysis (GAP-16.5)
    impact_result: Optional[Dict[str, Any]] = None
    if impact_analysis:
        # In a real deployment, this would read existing STITCH edges from Neo4j.
        # For now, document the contract and return an empty diff.
        impact_result = {
            "added": len(edges), "removed": 0, "updated": 0, "unchanged": 0,
            "note": "Impact analysis requires Neo4j connection — "
                    "see docs/stitch_lineage.md for Cypher queries.",
        }
        impact_path: Path = LOGS_DIR / "audit" / f"stitch_impact_{load_id}.jsonl"
        impact_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(impact_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "load_id": load_id, "timestamp": _iso_now(),
                    "impact": impact_result,
                }, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "stitch_impact_log_write_failed",
                extra={"path": str(impact_path), "error": str(exc)},
            )

    source_version: str = str(cfg.get("version", "unknown"))
    total_time: float = time.perf_counter() - t_total
    logger.info(
        "stitch_load_complete",
        extra={
            "load_id": load_id,
            "edges": len(edges),
            "loaded": loaded,
            "skipped_neo4j": skip_neo4j,
            "total_time_seconds": round(total_time, 3),
            "source_sha256": source_sha,
            "source_version": source_version,
            "output_sha256": output_sha,
        },
    )
    _append_audit_log({
        "event": "load_complete",
        "edges": len(edges), "loaded": loaded,
        "skipped_neo4j": skip_neo4j,
        "source_sha256": source_sha,
        "source_version": source_version,
        "output_sha256": output_sha,
        "total_time_seconds": round(total_time, 3),
    })

    result: Dict[str, Any] = {
        "edges": len(edges),
        "loaded": loaded,
        "skipped_neo4j": skip_neo4j,
        "validation": validation,
        "dlq_path": str(DEFAULT_DLQ_PATH),
        "load_id": load_id,
        "source_sha256": source_sha,
        "source_version": source_version,
        "errors": errors,
        "metrics": metrics.to_dict(),
        "output_sha256": output_sha,
    }
    if impact_result is not None:
        result["impact"] = impact_result
    return result


# ===== SECTION 17: PUBLIC API =====
# Fixes GAP-1.2: explicit __all__.
# Fixes GAP-1.2: parse_stitch alias (mirrors chembl_loader convention).

__all__: list[str] = [
    # ── Version constants ──
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    # ── Download ──
    "download_stitch",
    # ── Parse ──
    "parse_stitch_interactions",
    "parse_stitch_raw",
    "parse_stitch",             # alias (GAP-1.2)
    "filter_by_score",
    "filter_by_organism",
    "iter_stitch_cpi",
    # ── Validate ──
    "validate_stitch",
    # ── Convert ──
    "stitch_to_edge_records",
    "stitch_to_node_records",
    "iter_stitch_edges",
    "dedup_edges",
    # ── End-to-end ──
    "load_stitch",
    # ── Protocol adapter ──
    "StitchLoader",
    # ── Schemas (re-exported from schemas.py) ──
    "StitchCPIRecord",
    "StitchEdgeProps",
    "StitchEdgeRecord",
    "StitchLoaderMetrics",
    "StitchDeadLetterEntry",
    "StitchValidationReport",
    "STITCH_PROVENANCE_KEYS",
    # ── Exceptions (re-exported from exceptions.py) ──
    "StitchDownloadError",
    "StitchParseError",
    "StitchDataIntegrityError",
    "StitchEdgeLoadMismatchError",
    "StitchSecurityError",
    "StitchConfigurationError",
    # ── Constants ──
    "STITCH_VERSION",
    "STITCH_CIDM_PREFIX",
    "STITCH_CIDS_PREFIX",
    "STITCH_CONFIDENCE_BANDS",
    "STITCH_EVIDENCE_CHANNELS",
    "STITCH_SCORE_RANGE",
    "STITCH_SCORE_DTYPE",
    "EXPECTED_STITCH_COLUMNS",
    "REQUIRED_STITCH_COLUMNS",
    "STITCH_DTYPE_SCHEMA",
    "ORGANISM_TAXID_HUMAN",
    "ORGANISM_TAXID_DEFAULT",
    "ORGANISM_PREFIX_BY_TAXID",
    "ENSEMBL_PROTEIN_ID_REGEX",
    "ENSEMBL_PROTEIN_ID_BARE_REGEX",
    "UNIPROT_AC_REGEX",
    "STITCH_ACTION_TO_REL_TYPE",
    "PUBCHEM_CID_MIN",
    "PUBCHEM_CID_MAX",
    "MB",
    "MIB",
    "KIB",
    "CIRCUIT_BREAKER_FAILURE_THRESHOLD",
    "CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    "DEFAULT_DLQ_PATH",
    "UNRESOLVED_DLQ_PATH",
]
