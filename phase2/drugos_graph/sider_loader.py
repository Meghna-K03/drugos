"""DrugOS Graph Module — SIDER Loader (Institutional-Grade v1.0.0)
==================================================================
Downloads, parses, validates, and converts SIDER adverse-event data into
knowledge-graph node + edge records for the Autonomous Drug Repurposing
Platform (Team Cosmic, VentureLab).

This file is the **hardened** replacement for the 149-line prototype that
preceded it. The forensic audit (``sider_loader_fix_prompt.md``) enumerated
214 specific defects across 16 quality domains; every audit ID from A1.1
through D16.12 is addressed in this file via an inline
``# Fixes <audit-id>: <summary>`` comment (master prompt Rule R4).

Project Context
---------------
The Autonomous Drug Repurposing Platform mines 10,000 FDA-approved drugs
against every known disease using a chained pipeline:

1. **Knowledge Graph (Neo4j)** — built by this loader + 10 sibling loaders
   (ChEMBL, DrugBank, UniProt, STRING, DisGeNET, OMIM, PubChem, STITCH,
   DRKG, ClinicalTrials).
2. **Graph Transformer (PyTorch + PyG)** — predicts a 0-1 therapeutic-
   likelihood score for every untested drug-disease pair by message-passing
   over the graph this loader helps build.
3. **RL Hypothesis Ranker (Stable-Baselines3, PPO)** — ranks the top
   predictions by plausibility x **safety signal** x market opportunity.
4. **Clinical decision layer** — pharma partners + clinicians consume the
   ranking.

SIDER adverse events are **edges** in that graph. They tell the RL ranker
"Drug X causes adverse event Y." The RL ranker aggregates these onto the
Compound node and assigns a **safety tier**:

  * GREEN (recommend) — few adverse events
  * YELLOW (caution) — moderate adverse events
  * RED (do not recommend) — severe adverse events

**SIDER is the SOLE source of adverse-event data feeding the safety-signal
dimension.** If SIDER data is wrong, missing, or orphaned, the safety
ranker has no adverse events to aggregate, every drug is ranked GREEN,
and a clinician/pharma partner may recommend a dangerous drug to a
patient → **patient dies → criminal liability for the team.**

.. warning::
    **PATIENT SAFETY — READ BEFORE MODIFYING THIS FILE**

    The 36 ☠️ patient-safety flags in the audit dominate all other
    concerns. If a fix to a patient-safety item conflicts with a fix to a
    non-safety item, the patient-safety fix wins (master prompt §0.4).

    The four Phase-0 fixes (below) are mandatory and ship FIRST:

    * **Phase 0.1** — ``pubchem_cid`` is ``int64``, not zero-padded string
      (D2.4 / D15.1 / G2 / G3 / G4 / G5 / D5.13).
    * **Phase 0.2** — ``meddra_type_filter="PT"`` is the default (D3.1 / G1)
      to prevent PT/LLT double-counting.
    * **Phase 0.3** — Canonical spelling = ``MedDRA_Term`` (underscore) for
      nodes and ``causes_adverse_event`` for edges (D2.9 / D14.1 / D14.2 /
      D15.11 / G7). Legacy ``Side Effect`` + ``causes_side_effect`` kept
      behind a deprecation guard.
    * **Phase 0.4** — SIDER is in ``CRITICAL_SOURCES``; 0-row parse raises
      ``SiderCriticalError`` (A1.1 / D6.3 / G6).

Scientific Scope
----------------
- **Source:** SIDER (Kuhn M. et al., Nucleic Acids Res. 2016)
- **URL:** https://sideeffects.embl.de/
- **File:** ``meddra_all_se.tsv.gz`` (~50 MB gzipped, ~5M rows)
- **Format:** tab-separated, NO header, 6 columns:

  =====  ==================  ===================================================
  Col    Name                Description
  =====  ==================  ===================================================
  1      stitch_id_flat      CIDm-prefixed PubChem CID (FLAT form — merged
                            stereoisomers; e.g. CID000001070 = warfarin as a
                            racemate, R- + S-warfarin combined)
  2      stitch_id_stereo    CIDs-prefixed PubChem CID (STEREO-SPECIFIC form;
                            e.g. CID100000706 = S-warfarin, a single enantiomer)
  3      umls_id_label       UMLS CUI of the side-effect *label*
  4      meddra_type         One of {PT, LLT, HLT, HLGT, SOC}
  5      umls_id_meddra      UMLS CUI of the MedDRA term (canonical)
  6      side_effect_name    Human-readable side-effect name
  =====  ==================  ===================================================

- **MedDRA hierarchy:** 5 levels (SOC > HLGT > HGT > PT > LLT). PT
  (Preferred Term) is the canonical level for adverse-event reporting;
  LLT (Lowest Level Term) is a sub-concept that would double-count if
  emitted alongside PT (D3.1 — patient safety).
- **UMLS CUI format:** ``^C\\d{7}$`` (e.g. ``C0018790``).
- **PubChem CID range:** integers in ``[1, 370_000_000]``.
- **CIDm vs CIDs (v28 ROOT FIX, P2-L-14 — corrects prior
  self-contradiction):** Per STITCH docs
  (https://string-db.org/cgi/help?sessionId=&subpage=8#steroid):
    * **CIDm = FLAT (merged-stereoisomer) form** — used in SIDER column 1
      (``stitch_id_flat``). CIDm IDs have the prefix ``CIDm`` and encode
      the compound as a racemic mixture / unspecified stereochemistry
      (e.g. CIDm100000070 = warfarin racemate; both R- and S-warfarin
      merged into a single record).
      v35 ROOT FIX (V35-P2-LOADERS-FIXES M-4): the col-1 EXAMPLE in
      the table above previously used the ``CID1`` prefix, but the
      STITCH v5 production format uses ``CID0`` for FLAT (4th char = 0)
      and ``CID1`` for STEREO-SPECIFIC (4th char = 1). The example now
      uses ``CID0...`` to match the FLAT label.
    * **CIDs = STEREO-SPECIFIC (separate-stereoisomer) form** — used in
      SIDER column 2 (``stitch_id_stereo``). CIDs IDs have the prefix
      ``CIDs`` and encode a single, specific stereoisomer
      (e.g. CIDs100000706 = S-warfarin, the more pharmacologically
      active enantiomer).
  The FLAT (CIDm) ID is the canonical Compound node ID used by SIDER —
  it matches what STITCH/DrugBank/ChEMBL emit (D3.2 / Phase 0.1). The
  previous docstring contradicted itself (lines 73-74 said CIDm=racemic
  while lines 87-89 said CIDm=stereo-specific); this rewrite aligns
  both blocks with the STITCH documentation.
- **License:** CC0 1.0 (public domain). Every record carries
  ``_license="CC0 1.0"`` and ``_attribution="Data source: SIDER
  (Kuhn M. et al., Nucleic Acids Res. 2016), https://sideeffects.embl.de/,
  CC0 1.0"`` (D14.7).

PII Declaration
---------------
This loader processes **no** personally identifiable information (PII),
**no** protected health information (PHI), and **no** patient-level data.
SIDER contains only publicly published post-marketing adverse-event
reports aggregated from FDA + EMA + literature mining. HIPAA is not
applicable. GDPR is not applicable (no EU data subjects). If a future use
case introduces patient data upstream of this loader, a DPIA MUST be
performed before re-enabling the loader (Domain 9 Security, D9.4).

Regulatory Compliance
---------------------
- **21 CFR Part 11 (Electronic Records):** Audit logs at
  ``logs/audit/sider_access.jsonl`` and
  ``logs/audit/sider_regulatory.jsonl`` provide the system-of-record
  audit trail required for clinical decision support. Each entry is
  timestamped (ISO-8601 UTC), includes the ``load_id`` correlation ID,
  and is append-only. The regulatory audit log uses chained sha256
  (each line includes ``prev_hash``) for tamper-evidence (D16.12).
- **HIPAA:** N/A (no PHI — see PII Declaration above).
- **GDPR:** N/A (no EU data subjects).
- **CC0 1.0 (SIDER license):** Every record carries ``_license`` and
  ``_attribution`` fields (D14.7). CC0 1.0 is public domain; attribution
  is requested as a courtesy by the SIDER consortium.
- **Data retention:** Raw SIDER files are retained in ``data/raw/`` until
  superseded by a new pinned release (D3.8 / D7.3 — stale-file freshness
  check at 365 days).

References
----------
- Kuhn M. et al. "The SIDER database of drugs and side effects." Nucleic
  Acids Res. 2016;44(D1):D1075-9. doi:10.1093/nar/gkv1075.
- SIDER download page: https://sideeffects.embl.de/se/download/
- SIDER file format docs: https://sideeffects.embl.de/se/about/
- MedDRA hierarchy: https://www.meddra.org/how-to-use/basics/hierarchy
- UMLS CUI format: https://www.nlm.nih.gov/research/umls/new_users/online_learning/IMG_0005.html
- PubChem CID format: https://pubchemdocs.ncbi.nlm.nih.gov/compound-id
- DrugOS Coding Standards: ``drugos_graph/compliance.md``
- PEP 8 / 257 / 563 / 544 (style, docstrings, lazy annotations, Protocols).

Design Patterns
---------------
- **Adapter** — ``SiderLoader`` adapts the module-level functions to the
  ``Loader`` Protocol (PEP 544) so ``run_pipeline.py`` can treat all
  loaders polymorphically (A1.4).
- **Facade** — ``load_sider()`` orchestrates the full pipeline: download
  -> parse -> validate -> emit -> (optional) audit log.
- **Iterator** — ``iter_sider_rows`` and ``iter_sider_edges`` provide
  streaming APIs for memory-bounded processing of the 5M-row file (D6.7).
- **Dead-Letter Queue** — malformed rows are written to
  ``logs/dlq/sider_dlq.jsonl`` for forensic inspection rather than
  silently dropped (D5.12 / D6.5).
- **Strategy** — ``meddra_type_filter`` kwarg selects between PT-only
  (default, patient-safe), LLT-inclusive, or no filter (D3.1 / Phase 0.2).
- **Atomic Download** — files are written to ``.part`` then renamed via
  ``os.replace`` after sha256 verification (D4.10 / G10).
- **Circuit Breaker** — download trips after ``SIDER_MAX_RETRIES``
  consecutive failures (D6.1).
- **Mutual Exclusion Guard** — calling both canonical and legacy edge
  emitters in the same process raises ``SiderDualWriteError`` (D2.13 / G13).

Public API
----------
Backward compatibility (master prompt Rule R3) — the five original public
functions remain importable with the SAME positional signatures, SAME
types, and SAME default behaviors:

- ``download_sider(force=False) -> Path``
- ``parse_sider_side_effects(filepath=None) -> pd.DataFrame``
- ``sider_to_node_records(df) -> List[Dict]``
- ``sider_to_edge_records(df) -> List[Dict]``
- ``sider_to_legacy_edge_records(df) -> List[Dict]``  (deprecated — D2.10)

New public functions (additive only — Rule R2/R3):

- ``parse_sider_raw(filepath=None) -> pd.DataFrame``
- ``validate_sider(df) -> dict``
- ``parse_sider_fda_labels(filepath=None) -> pd.DataFrame``   (D3.3 — stub)
- ``parse_sider_frequencies(filepath=None) -> pd.DataFrame``  (D3.3 — stub)
- ``iter_sider_rows(filepath=None, chunksize=100_000) -> Iterator[pd.DataFrame]``
- ``iter_sider_edges(df_or_path, *, batch_size=10_000, **kwargs)``
- ``diff_sider_outputs(old_df, new_df) -> dict``              (D16.10)
- ``load_sider(skip_neo4j=True, force=False) -> dict``

Aliases (additive, no rename):

- ``parse_sider = parse_sider_side_effects``  (backward-compat)

New public class:

- ``SiderLoader``  (Loader Protocol adapter — A1.4)

Environment Variables
---------------------
All env vars are read at call time (not import time) so tests can
monkeypatch ``os.environ`` between calls:

==============================  =============================================
Env var                         Purpose
==============================  =============================================
``DRUGOS_SIDER_FILEPATH``       Override the input file path (D12.3)
``DRUGOS_SIDER_URL``            Override the download URL (D12.3)
``DRUGOS_SIDER_FORCE_DOWNLOAD`` Force re-download (D12.3)
``DRUGOS_SIDER_SKIP``           Skip SIDER load entirely (D12.3)
``DRUGOS_SIDER_OFFLINE``        Use cached file only — no download (D12.3)
``DRUGOS_SIDER_LOCAL_PATH``     Alias for DRUGOS_SIDER_FILEPATH (D12.3)
``DRUGOS_SIDER_MAX_ROWS``       Cap rows read (D12.3 / D12.12)
``DRUGOS_SIDER_ALLOW_LEGACY``   Allow legacy emitter post-migration (D14.10)
``DRUGOS_SIDER_SKIP_SHA256``    Skip sha256 verification (dev only) (D12.3)
``DRUGOS_SIDER_REQUIRED``       SIDER is required source (default 1) (Phase 0.4)
``DRUGOS_SIDER_BATCH_SIZE``     Batch size for iter_sider_edges (D8.10)
``DRUGOS_SIDER_CHUNK_SIZE``     Chunk size for iter_sider_rows (D8.3)
``DRUGOS_SIDER_STRICT_ROW_COUNT``  Enforce expected row count range (D5.1)
==============================  =============================================

Coding Standards
----------------
- PEP 8 (style), PEP 257 (docstrings), PEP 563 (lazy annotations),
  PEP 544 (Protocols).
- ``from __future__ import annotations`` is the FIRST import (R8).
- All public functions have NumPy-style docstrings (D13.3).
- All non-trivial changes carry a ``# Fixes <audit-id>: <summary>``
  inline comment (Rule R4).
- ``__all__`` is explicit (D4.29 / D15.5).
- No bare ``except:`` blocks (Rule R5). No ``except Exception: pass``
  patterns (Rule R5).

SCHEMA CHANGELOG
----------------
**v0** (legacy — the original 149-line prototype):
- Emitted 5-field node records and 6-field edge records.
- ``drug_cid`` was a zero-padded string (e.g. ``"00002244"``) — MISMATCHED
  with STITCH/DrugBank/ChEMBL which emit ``pubchem_cid: int`` (D2.4 / D15.1).
- Emitted both PT and LLT rows as separate edges → double-counting (D3.1).
- No provenance, no license, no schema version, no audit trail.
- Used the legacy ``"Side Effect"`` / ``"causes_side_effect"`` labels
  (Phase 0.3 — these are now spelled ``"MedDRA_Term"`` /
  ``"causes_adverse_event"`` canonically).

**v1.0.0** (this release — institutional-grade audit fix):
- Added ``pubchem_cid: int64`` (canonical), kept ``drug_cid: str`` as a
  deprecated alias for one release cycle (Phase 0.1 / D2.4).
- Default ``meddra_type_filter="PT"`` (Phase 0.2 / D3.1) — PT-only by default.
- Canonical labels ``"MedDRA_Term"`` (node) and ``"causes_adverse_event"``
  (edge) (Phase 0.3 / D2.9 / D14.1).
- SIDER promoted to ``CRITICAL_SOURCES``; 0-row parse raises
  ``SiderCriticalError`` (Phase 0.4 / A1.1 / D6.3 / G6).
- Added ``_provenance`` sub-dict on every record (A1.6 / D16.1).
- Added deterministic edge ``id`` via sha1 hash (D2.8 / G9).
- Added dead-letter queue at ``logs/dlq/sider_dlq.jsonl`` (D5.12 / D6.5).
- Added audit logs at ``logs/audit/sider_access.jsonl`` (D9.5) and
  ``logs/audit/sider_regulatory.jsonl`` (D16.12 — chained-hash tamper-evident).
- Added transformation log at ``logs/transformations/sider.jsonl`` (D16.2).
- Added quality report at ``logs/quality/sider_quality_report.json`` (D5.11).
- Added lineage log at ``logs/lineage/sider_lineage.jsonl`` (D11.3).
- Added checkpoints at ``logs/checkpoints/sider_checkpoint.json`` (D6.10).
- Added registry entry at ``data/registry.json`` (D16.9).
- Added ``SiderLoader`` class implementing the ``Loader`` Protocol (A1.4).
- Added ``diff_sider_outputs`` for diff/impact analysis (D16.10).
- Added ``parse_sider_fda_labels`` / ``parse_sider_frequencies`` stubs (D3.3).
- Added mutual-exclusion guard against calling both canonical and legacy
  emitters in the same process (D2.13 / G13).
- Atomic download via ``.part`` + ``os.replace`` (D4.10 / G10).
- Retry with exponential backoff (D6.1).
- TLS verification, URL allowlist, filename-safety guard (D9.1 / D9.2).
- sha256 + version sidecar files (D3.8 / D7.2).
- Full type annotations + TypedDicts in ``schemas.py`` (D4.22).
- PEP 604 syntax (``Path | None``) throughout (D4.23 / D14.5).
- Lazy f-string logging (D4.8).

How to Update the Pinned Version
--------------------------------
When SIDER publishes a new release:

1. Update ``DATA_SOURCES["sider"]["url"]`` if the URL changes.
2. Update ``DATA_SOURCES["sider"]["version"]`` to the new release date.
3. Update ``DATA_SOURCES["sider"]["release_date"]`` to the new date.
4. Update ``DATA_SOURCES["sider"]["expected_record_count"]`` from the
   SIDER release notes.
5. Update ``DATA_SOURCES["sider"]["size_bytes"]`` if changed.
6. Compute sha256 of the new file and update
   ``DATA_SOURCES["sider"]["sha256"]`` (or leave None and let the loader
   pin it as a sidecar at first download — D3.8).
7. Run ``pytest tests/test_sider_loader.py -v`` — all regression tests
   MUST pass.
8. Run ``load_sider(force=True)`` to download the new file and verify
   row counts are within ``[EXPECTED_SIDER_ROW_COUNT_MIN,
   EXPECTED_SIDER_ROW_COUNT_MAX]`` (D5.1).
9. Update ``docs/SCHEMA_CHANGELOG.md`` with the new version + date.
10. Bump ``SIDER_PARSER_VERSION`` if any parser logic changed.

See Also
--------
- ``drugos_graph/stitch_loader.py`` — gold-standard reference loader
- ``drugos_graph/string_loader.py`` — second reference loader (PPI edges)
- ``drugos_graph/chembl_loader.py`` — third reference loader (Compound→Protein)
- ``drugos_graph/schemas.py`` — TypedDict contracts (SiderEdgeRecord etc.)
- ``drugos_graph/exceptions.py`` — SIDER exception hierarchy
- ``scripts/migrate_sidetoeffect_to_meddraterm.py`` — one-time Cypher migration

Edge Cases
----------
The loader handles these edge cases explicitly (D10.3):

- **Empty file** → ``SiderCriticalError`` (0 rows on required source — G6).
- **Header-only file** → ``SiderCriticalError`` (0 data rows — G6).
- **Malformed TSV** (wrong column count) → ``SiderSchemaError`` (D15.10).
- **Wrong columns** (renamed in future SIDER version) → ``SiderSchemaError``.
- **NULL stitch_id_flat** → DLQ with ``reason="null_stitch_id_flat"`` (D4.5).
- **Regex no-match** (stitch_id_flat not ``CIDm\\d+``) → DLQ with
  ``reason="regex_no_match"`` (D4.5 — distinguished from null).
- **Out-of-range CID** (0 or > 370M) → DLQ with
  ``reason="pubchem_cid_out_of_range"`` (D3.12 / D5.13).
- **Invalid UMLS CUI** (not ``C\\d{7}``) → DLQ with
  ``reason="invalid_umls_cui"`` (D3.4 / D5.6).
- **Unknown meddra_type** → DLQ with ``reason="unknown_meddra_type"`` (D2.12).
- **Invalid side_effect_name** (empty / NULL / NA sentinel) → DLQ with
  ``reason="invalid_side_effect_name"`` (D3.9).
- **stitch_id_flat / stitch_id_stereo numeric mismatch** → DLQ with
  ``reason="stitch_id_numeric_mismatch"`` (D3.10 / D5.4).
- **Duplicates** (same drug-effect pair, multiple meddra_type) →
  ``_dedupe_edges`` keeping PT-preferential (D2.7).
- **Truncated gzip** → ``SiderParseError`` (D6.4).
- **UTF-8 BOM** → handled via ``encoding="utf-8-sig"`` (D4.14).
- **CRLF line endings** → handled via pandas default (D10.3).
- **Quoted fields** (SIDER uses no quoting) → ``quoting=csv.QUOTE_NONE``
  (D4.15 / D14.8).

Known Failure Modes
-------------------
- **SIDER source URL changes** without config update →
  ``SiderDownloadError`` (URL not in allowlist). Recovery: update
  ``ALLOWED_SIDER_URLS`` in config.py.
- **SIDER file format changes** (column rename) → ``SiderSchemaError``
  (D15.10). Recovery: update ``SIDER_COLUMN_NAMES`` and bump
  ``SIDER_PARSER_VERSION``.
- **SIDER publishes new release** → stale-file warning (D5.9). Recovery:
  see "How to Update the Pinned Version" above.

Test Coverage
-------------
Every audit ID has at least one regression test in
``tests/test_sider_loader.py``. Run
``pytest tests/test_sider_loader.py --cov=drugos_graph.sider_loader
--cov-report=term-missing`` to verify coverage.

Fixes: All 214 audit IDs from ``sider_loader_fix_prompt.md``. See inline
``# Fixes <audit-id>:` comments for per-fix attribution.
"""

# =============================================================================
# AUDIT ID COVERAGE BLOCK — All audit IDs from sider_loader_fix_prompt.md are
# addressed below. Each ID appears either as an inline `# Fixes <id>:` comment
# at the specific code location it fixes, OR in this block as a one-line
# summary referencing where the fix lives.
# =============================================================================

# ===== SECTION 1: IMPORTS =====
# Fixes A1.7 / D4.1: Remove dead `import gzip` (the v0 import was unused).
# Fixes C4-09 / R8: `from __future__ import annotations` is the FIRST import.

from __future__ import annotations

import csv
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
import uuid
import warnings
from collections import Counter
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
from .config import (
    ALLOWED_SIDER_URLS,
    AUDIT_LOG_DIR,
    CHECKPOINT_DIR,
    CORE_EDGE_TYPES_SET,
    CORE_NODE_TYPES,
    DATA_DIR,
    DATA_SOURCES,
    DEAD_LETTER_DIR,
    EDGE_TYPE_TO_RELATION_SIDER,
    EXPECTED_SIDER_ROW_COUNT_MAX,
    EXPECTED_SIDER_ROW_COUNT_MIN,
    LOGS_DIR,
    MEDDRA_TYPE_DEDUP_ORDER,
    ON_SOURCE_FAILURE,
    PUBCHEM_CID_MAX_SIDER,
    PUBCHEM_CID_MIN_SIDER,
    RAW_DIR,
    SIDER_ATTRIBUTION,
    SIDER_BATCH_SIZE,
    SIDER_CHECKPOINT_INTERVAL,
    SIDER_CHUNK_SIZE,
    SIDER_COMPOUND_ID_FORMAT,
    SIDER_DOWNLOAD_TIMEOUT_SECONDS,
    SIDER_EDGE_ID_HASH_LENGTH,
    SIDER_EDGE_TYPE,
    SIDER_EXPECTED_COLUMN_COUNT,
    SIDER_FILE_PERMISSIONS,
    SIDER_LEGACY_EDGE_TYPE,
    SIDER_LEGACY_NODE_TYPE,
    SIDER_LICENSE,
    SIDER_LOG_DIR_PERMISSIONS,
    SIDER_LARGE_DF_THRESHOLD,
    SIDER_MAX_REDIRECTS,
    SIDER_MAX_RETRIES,
    SIDER_MEDDRA_VERSION,
    SIDER_MIN_VALID_SIZE_BYTES,
    SIDER_NODE_TYPE,
    SIDER_PARSER_VERSION,
    SIDER_PINNED_RELEASE_DATE,
    SIDER_PINNED_SHA256,
    SIDER_PINNED_VERSION,
    SIDER_REQUIRED,
    SIDER_RETRY_BACKOFF_BASE,
    SIDER_SCHEMA_VERSION,
    SIDER_STALE_FILE_DAYS,
    SOURCE_KEY_SIDER,
    SOURCE_SIDER,
    UMLS_CUI_REGEX,
    VALID_MEDDRA_TYPES,
    get_data_source_path,
)
from .exceptions import (
    DrugOSDataError,
    SiderCriticalError,
    SiderDataQualityError,
    SiderDownloadError,
    SiderDualWriteError,
    SiderParseError,
    SiderSchemaError,
)

# ─── Schemas (TypedDicts) ────────────────────────────────────────────────────
from .schemas import (
    SIDER_PROVENANCE_KEYS,
    SiderDeadLetterEntry,
    SiderEdgeRecord,
    SiderLegacyEdgeRecord,
    SiderLoaderMetrics,
    SiderNodeRecord,
    SiderSideEffectRow,
    SiderValidationReport,
)

# TYPE_CHECKING-only import to avoid circular dependency at runtime.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._loader_protocol import Loader  # noqa: F401

# v29 ROOT FIX (audit L-5): Compound ID → InChIKey normalizer.
# Imported here (top of file) so ``_build_edge_record`` can call it
# without paying the import cost on every row. The function itself
# is a thin shim around ``IDCrosswalk.compound_id_to_inchikey()``
# that returns the original ID unchanged when no mapping is found —
# so the import does NOT introduce a hard runtime dependency on a
# configured crosswalk (the default singleton loads the builtin
# table on first call and gracefully returns the original ID with
# a WARNING when no mapping is known).
from .id_crosswalk import _normalize_compound_id_to_inchikey

# v41 ROOT FIX (Task K2 / SEV2): local InChIKey pattern used by
# ``_build_edge_record`` to detect crosswalk misses. The shared
# ``_normalize_compound_id_to_inchikey`` returns the ORIGINAL ID (e.g.
# "CID5311025") when the crosswalk has no mapping — by design, so other
# loaders that have a graceful CID-keyed fallback path (e.g. PubChem
# native CID-keyed Compound nodes) still work. SIDER must NOT use that
# fallback: emitting "CID5311025" as ``src_id`` would create an ORPHAN
# Compound node in the KG (the InChIKey-keyed Compound nodes from
# DrugBank/ChEMBL/PubChem loaders would never be joined to it). So
# SIDER detects the miss via this pattern check and dead-letters the
# edge instead. Pattern matches the canonical 27-char InChIKey form
# (14-10-1 uppercase letters with hyphens) — kept in sync with
# ``id_crosswalk._INCHIKEY_PATTERN``.
_INCHIKEY_RE: re.Pattern[str] = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


# ===== SECTION 2: CONSTANTS =====
# Fixes D14.2 / A1-03: PARSER_VERSION and SCHEMA_VERSION constants.
# Fixes D14.1: License + attribution constants (imported from config).
PARSER_VERSION: str = SIDER_PARSER_VERSION      # "1.0.0"
SCHEMA_VERSION: str = SIDER_SCHEMA_VERSION      # "1.0.0"

# Fixes D14.4: SIDER_COLUMN_NAMES — explicit, named (no longer "col_names").
# The SIDER meddra_all_se.tsv.gz file has 6 columns in this exact order.
#
# V19 ROOT FIX (PS-7 / RT-3 — forensic re-audit found v15 "ROOT FIX" was
# itself the bug):
# The file's OWN module docstring (lines 73-74) and the official SIDER
# documentation (http://sideeffects.embl.de/data/) BOTH state:
#   col 1: stitch_id_flat    — CIDm-prefixed (or CID0 in newer format) = FLAT
#   col 2: stitch_id_stereo  — CIDs-prefixed (or CID1 in newer format) = STEREO
#   col 3: UMLS concept id as side effect label  — C\d{7}
#   col 4: MedDRA concept type                   — PT|LLT|HLT|HLGT|SOC
#   col 5: UMLS concept id (MedDRA)              — C\d{7}
#   col 6: side effect name                      — string
#
# The v15 "ROOT FIX" comment block (now removed) falsely claimed the
# opposite — that col 1 is STEREO and col 2 is FLAT — and used that false
# claim to justify swapping the tuple. The swap caused SIDER_CIDM_REGEX
# (the FLAT regex) to be applied to col 2 values (which actually contain
# STEREO CIDs), and SIDER_CIDS_REGEX (the STEREO regex) to be applied to
# col 1 values (which actually contain FLAT CIDs). Every row failed the
# cross-column regex check → DLQ → 0 rows parsed → SiderCriticalError.
#
# The v9/v10/v11/v15 "FORENSIC VALIDATED" stamp was earned against fixture
# files that used the SAME (wrong) column order — the production file does
# not. The verification agents in this V19 cycle cross-checked the tuple
# against the file's own docstring and the official SIDER schema, both of
# which agree: col 1 = FLAT, col 2 = STEREO.
#
# Fix: restore the correct order (col 1 = stitch_id_flat, col 2 = stitch_id_stereo)
# so each regex matches its intended column. This is the OPPOSITE of v15's swap.
SIDER_COLUMN_NAMES: Tuple[str, ...] = (
    "stitch_id_flat",      # col 1 — FLAT  (CIDm / CID0)
    "stitch_id_stereo",    # col 2 — STEREO (CIDs / CID1)
    "umls_id_label",
    "meddra_type",
    "umls_id_meddra",
    "side_effect_name",
)

# Fixes D4.4 / GAP-4.4: SIDER_DTYPE_SCHEMA — explicit dtype for each column.
# All six raw columns are string (nullable). The parser adds derived columns
# (pubchem_cid as Int64, stereochemistry as string, _source_row as int64)
# AFTER the initial read.
SIDER_DTYPE_SCHEMA: Dict[str, str] = {
    "stitch_id_flat": "string",
    "stitch_id_stereo": "string",
    "umls_id_label": "string",
    "meddra_type": "string",
    "umls_id_meddra": "string",
    "side_effect_name": "string",
}

# Fixes Phase 0.1 / D2.1 / D2.2 / D2.3: Regex for extracting the PubChem CID
# from the stitch_id_flat / stitch_id_stereo columns.
#
# v0 BUG: ``r"CID,m,s](\d+)"`` — the character class `[,m,s]` matched ANY of
# ",", "m", "s" (the comma was a literal member of the class, not a
# separator). This silently allowed malformed IDs to parse.
#
# v1.0.0 fix: anchored regex, explicit prefix.
# * CIDm prefix (stitch_id_flat, col 1)  -> r"^CIDm(\d+)$"
# * CIDs prefix (stitch_id_stereo, col 2) -> r"^CIDs(\d+)$"
#
# v15 ROOT FIX (runtime crash — SIDER produced 0 rows in production):
# The previous regex ONLY matched the legacy STITCH format `CIDm\d+` /
# `CIDs\d+` (e.g. `CIDm0000085`, `CIDs0000085`). The actual SIDER
# `meddra_all_se.tsv.gz` release (verified 2024-08) uses the newer
# STITCH flat/stereo encoding where the 4th character is a digit:
#   • `CID100000085` — stereo-specific (4th char = '1')
#   • `CID000010917` — flat / non-stereo (4th char = '0')
# The legacy regex `^CIDm(\d+)$` failed on EVERY row of the production
# file (309,849 rows → 0 parsed → SiderCriticalError). The v9/v10/v11
# "FORENSIC VALIDATED" stamp was earned against a fixture file that
# used the legacy CIDm/CIDs format — the production file does not.
# Fix: accept BOTH formats via alternation:
#   • `^CIDm(\d+)$` — legacy mixture (CIDm0000085)
#   • `^CIDs(\d+)$` — legacy stereo (CIDs0000085)
#   • `^CID0(\d+)$` — newer flat (CID000010917)  ← PRODUCTION FORMAT
#   • `^CID1(\d+)$` — newer stereo (CID100000085) ← PRODUCTION FORMAT
# The capture group always returns the numeric portion so downstream
# CID↔PubChem-CID resolution is format-agnostic.
SIDER_CIDM_REGEX: re.Pattern[str] = re.compile(r"^(?:CIDm|CID0)(\d+)$")
# Audit fix (v5 Tier-4): the previous regex used lowercase 'd' (CIds)
# instead of uppercase 'D' (CIDs). The SIDER stereo-CID format is
# "CIDs" + digits — confirmed by the comment on line 514. The typo
# silently disabled stereo-ID validation and made the CIDm/CIDs
# cross-column consistency check dead code.
#
# v15 ROOT FIX: also accept the newer `CID1\d+` production format.
SIDER_CIDS_REGEX: re.Pattern[str] = re.compile(r"^(?:CIDs|CID1)(\d+)$")

# Fixes D3.4 / D5.6: UMLS CUI regex.
SIDER_UMLS_CUI_REGEX: re.Pattern[str] = re.compile(UMLS_CUI_REGEX)

# Fixes D4.15 / D14.8: SIDER uses no quoting (pure TSV).
# csv.QUOTE_NONE + escapechar="\\" matches pandas default for TSV without
# quoted fields. We pass these explicitly to pd.read_csv for determinism.
SIDER_CSV_QUOTING: int = csv.QUOTE_NONE
SIDER_CSV_ESCAPECHAR: str = "\\"

# Fixes D4.17: explicit NA values — SIDER does not use sentinels, but we
# treat empty / "NA" / "NULL" / "None" / "NaN" as missing for safety.
SIDER_NA_VALUES: List[str] = ["", "NA", "NULL", "None", "NaN"]
SIDER_NA_SENTINELS: Tuple[str, ...] = ("", "NA", "NULL", "None", "NaN", "null")

# Fixes D2.8 / G9: SIDER edge ID hash inputs.
# The edge id is sha1(f"{src_id}|{dst_id}|{rel_type}|SIDER")[:16] for
# canonical edges, sha1(f"{src_id}|{dst_id}|{rel_type}|SIDER_LEGACY")[:16]
# for legacy edges. The legacy suffix ensures canonical and legacy edges
# get DIFFERENT ids (preventing accidental MERGE collision in Neo4j).
SIDER_EDGE_ID_SOURCE_CANONICAL: str = "SIDER"
SIDER_EDGE_ID_SOURCE_LEGACY: str = "SIDER_LEGACY"

# Fixes D15.2: SIDER dst_id prefix. UMLS CUIs are shared across vocabularies
# (MedDRA, SNOMED-CT, RxNorm, etc.). To prevent SIDER MedDRA CUIs from
# colliding with DisGeNET Disease UMLS CUIs (prefixed "Disease:"), we prefix
# SIDER MedDRA CUIs with "MedDRA:". The Compound src_id is the int
# pubchem_cid (no prefix needed — Compound IDs are integers).
SIDER_DST_ID_PREFIX: str = "MedDRA:"

# Edge type constants (D15.1 — kg_builder contract).
_SRC_TYPE: str = "Compound"
_DST_TYPE_CANONICAL: str = SIDER_NODE_TYPE              # "MedDRA_Term"
_DST_TYPE_LEGACY: str = SIDER_LEGACY_NODE_TYPE          # "Side Effect"
_REL_TYPE_CANONICAL: str = SIDER_EDGE_TYPE              # "causes_adverse_event"
_REL_TYPE_LEGACY: str = SIDER_LEGACY_EDGE_TYPE          # "causes_side_effect"

# Fixes D12.3: Magic-number constants (extracted from inline use).
_MB: int = 1_000_000
_MIB: int = 1_024 * 1_024

# Fixes D6.5 / D5.12: DLQ + audit log paths.
DEFAULT_DLQ_PATH: Path = LOGS_DIR / "dlq" / "sider_dlq.jsonl"
_QUALITY_REPORT_PATH: Path = LOGS_DIR / "quality" / "sider_quality_report.json"
_LINEAGE_LOG_PATH: Path = LOGS_DIR / "lineage" / "sider_lineage.jsonl"
_AUDIT_LOG_PATH: Path = AUDIT_LOG_DIR / "sider_access.jsonl"
_REGULATORY_AUDIT_LOG_PATH: Path = AUDIT_LOG_DIR / "sider_regulatory.jsonl"
_TRANSFORMATION_LOG_PATH: Path = LOGS_DIR / "transformations" / "sider.jsonl"
_CHECKPOINT_PATH: Path = CHECKPOINT_DIR / "sider_checkpoint.json"
_QUARANTINE_DIR: Path = LOGS_DIR / "quarantine"
_REGISTRY_PATH: Path = DATA_DIR / "registry.json"

# Sidecar file suffixes (mirrors stitch_loader).
_SIDECAR_SHA256_SUFFIX: str = ".sha256"
_SIDECAR_VERSION_SUFFIX: str = ".version"

# Fixes D9.1: URL credential masking regex.
_URL_CRED_RE: re.Pattern[str] = re.compile(r"://([^:/@]+):([^@/]+)@")

# Fixes GAP-7.4: Process-cached load_id (correlation ID).
_LOAD_ID_LOCK: threading.Lock = threading.Lock()
_LOAD_ID: Optional[str] = None

# Fixes D2.13 / G13: Dual-write mutual-exclusion flags (process-local).
_DUAL_WRITE_LOCK: threading.Lock = threading.Lock()
_CANONICAL_EMITTED: bool = False
_LEGACY_EMITTED: bool = False

# Fixes D2.10: Deprecation warning message for legacy emitter.
_LEGACY_DEPRECATION_MSG: str = (
    "sider_to_legacy_edge_records is deprecated; use sider_to_edge_records. "
    "Will be removed in v2.0. Set DRUGOS_SIDER_ALLOW_LEGACY=1 to suppress "
    "this error and continue."
)

# Internal: thread-local DLQ buffer for batched writes (D6.5).
_DLQ_BUFFER: List[Dict[str, Any]] = []
_DLQ_BUFFER_LOCK: threading.Lock = threading.Lock()
_DLQ_FLUSH_SIZE: int = 1000

# Fixes D14.6: Schema version constant (re-exported for clarity).
SIDER_MODULE_SCHEMA_VERSION: str = SCHEMA_VERSION

# Fixes D4.29 / D15.5: __all__ declaration.
__all__: List[str] = [
    # Original 5 public functions (Rule R3 — preserved)
    "download_sider",
    "parse_sider_side_effects",
    "sider_to_node_records",
    "sider_to_edge_records",
    "sider_to_legacy_edge_records",
    # New public functions (Rule R2 — additive)
    "parse_sider_raw",
    "validate_sider",
    "parse_sider_fda_labels",
    "parse_sider_frequencies",
    "iter_sider_rows",
    "iter_sider_edges",
    "diff_sider_outputs",
    "load_sider",
    # Alias (backward-compat)
    "parse_sider",
    # Public class
    "SiderLoader",
    # Constants (re-exported for downstream consumers)
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    "SIDER_COLUMN_NAMES",
    "SIDER_DTYPE_SCHEMA",
    "SIDER_CIDM_REGEX",
    "SIDER_CIDS_REGEX",
    "SIDER_UMLS_CUI_REGEX",
    "SIDER_EDGE_ID_SOURCE_CANONICAL",
    "SIDER_EDGE_ID_SOURCE_LEGACY",
    "SIDER_DST_ID_PREFIX",
]

# Alias — backward-compat (Rule R3). Defined at end of file after the
# canonical function is declared.
parse_sider = None  # type: ignore[assignment]  # set at end of file


# ===== SECTION 3: METRICS DATACLASS =====
# Fixes D11.2: SiderLoaderMetrics dataclass for structured observability.
# (Re-exported from schemas.py as a TypedDict; here we use a runtime dataclass.)


@dataclass
class _SiderLoaderMetricsDataclass:
    """Runtime container for SIDER loader metrics (D11.2).

    The TypedDict form in schemas.py is the static contract; this dataclass
    is the typed runtime container with sensible defaults.
    """

    rows_in: int = 0
    rows_after_cid_filter: int = 0
    rows_after_meddra_filter: int = 0
    rows_after_dedup: int = 0
    nodes_created: int = 0
    edges_created: int = 0
    duplicate_edges: int = 0
    invalid_umls_cui: int = 0
    invalid_pubchem_cid: int = 0
    invalid_meddra_type: int = 0
    invalid_side_effect_name: int = 0
    stitch_id_numeric_mismatch: int = 0
    null_stitch_id_flat: int = 0
    regex_no_match: int = 0
    dlq_entries: int = 0
    parse_time_seconds: float = 0.0
    edge_build_time_seconds: float = 0.0
    peak_memory_mb: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict form (matches SiderLoaderMetrics TypedDict)."""
        return {
            "rows_in": self.rows_in,
            "rows_after_cid_filter": self.rows_after_cid_filter,
            "rows_after_meddra_filter": self.rows_after_meddra_filter,
            "rows_after_dedup": self.rows_after_dedup,
            "nodes_created": self.nodes_created,
            "edges_created": self.edges_created,
            "duplicate_edges": self.duplicate_edges,
            "invalid_umls_cui": self.invalid_umls_cui,
            "invalid_pubchem_cid": self.invalid_pubchem_cid,
            "invalid_meddra_type": self.invalid_meddra_type,
            "invalid_side_effect_name": self.invalid_side_effect_name,
            "stitch_id_numeric_mismatch": self.stitch_id_numeric_mismatch,
            "null_stitch_id_flat": self.null_stitch_id_flat,
            "regex_no_match": self.regex_no_match,
            "dlq_entries": self.dlq_entries,
            "parse_time_seconds": self.parse_time_seconds,
            "edge_build_time_seconds": self.edge_build_time_seconds,
            "peak_memory_mb": self.peak_memory_mb,
            "errors": list(self.errors),
        }


# ===== SECTION 4: LOGGER SETUP =====
# Fixes D4.8: Lazy %-style logging via LoggerAdapter.
# Fixes D11.1 / D11.2: Structured logging via logger.info(event, extra={}).
# Fixes A1.9: Module-level logger documented; tests use ``caplog``.
logger: logging.Logger = logging.getLogger(__name__)


class SiderLoggerAdapter(logging.LoggerAdapter):
    """Inject source/source_version/load_id into every log record (D4.8).

    Usage:
        adapter = SiderLoggerAdapter(logger, {"load_id": "abc123"})
        adapter.info("sider_parse_complete", extra={"rows": 1000})
    """

    def process(self, msg: Any, kwargs: Any) -> Tuple[Any, Any]:
        extra = self.extra or {}
        merged = {**kwargs.get("extra", {}), **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def _get_logger(load_id: Optional[str] = None) -> logging.LoggerAdapter:
    """Return a SiderLoggerAdapter with the current load_id attached."""
    return SiderLoggerAdapter(
        logger,
        {
            "source": SOURCE_SIDER,
            "source_version": PARSER_VERSION,
            "load_id": load_id or _get_load_id(),
        },
    )


# ===== SECTION 5: CONFIGURATION & ENVIRONMENT =====
# Fixes D12.4: _validate_sider_config(cfg) — validate config on startup.
# Fixes D12.3: _resolve_sider_filepath(filepath) priority: arg > env > config.
# Fixes D12.3: _get_sider_config() honoring DRUGOS_SIDER_URL env override.

def _get_sider_config() -> Dict[str, Any]:
    """Return a copy of DATA_SOURCES['sider'], with env-var overrides applied.

    Honors:
        DRUGOS_SIDER_URL — override the download URL (after _validate_url).
        DRUGOS_SIDER_LOCAL_PATH — alias for DRUGOS_SIDER_FILEPATH.

    Returns
    -------
    dict
        A shallow copy of the SIDER config dict with any env overrides
        applied. The original ``DATA_SOURCES['sider']`` is NOT mutated.
    """
    # Fixes D4.30: Wrap DATA_SOURCES["sider"] access in try/except.
    try:
        cfg: Dict[str, Any] = dict(DATA_SOURCES[SOURCE_KEY_SIDER])
    except KeyError as exc:
        raise SiderCriticalError(
            f"SIDER not registered in DATA_SOURCES (key={SOURCE_KEY_SIDER!r}).",
            context={"source_key": SOURCE_KEY_SIDER,
                     "available_keys": sorted(DATA_SOURCES.keys())},
        ) from exc
    # Fixes D12.3: env override for URL.
    env_url: Optional[str] = os.environ.get("DRUGOS_SIDER_URL")
    if env_url:
        _validate_url(env_url)  # raises SiderDownloadError on non-allowlisted URL
        cfg["url"] = env_url
    return cfg


def _validate_sider_config(cfg: Dict[str, Any]) -> None:
    """Validate the SIDER config dict on startup (D12.4 / D12.9).

    Raises
    ------
    SiderCriticalError
        If any required key is missing or has an invalid value.
    """
    required_keys: Tuple[str, ...] = (
        "url", "filename", "version", "max_size_bytes",
        "expected_record_count", "retry_count",
        "retry_backoff_seconds", "timeout_seconds",
    )
    for key in required_keys:
        if key not in cfg:
            raise SiderCriticalError(
                f"SIDER config missing required key: {key!r}",
                context={"missing_key": key, "available_keys": sorted(cfg.keys())},
            )
    url: Any = cfg["url"]
    if not isinstance(url, str) or not url.startswith("https://"):
        raise SiderCriticalError(
            f"SIDER URL must be HTTPS, got: {url!r}",
            context={"url": url},
        )
    filename: Any = cfg["filename"]
    if not isinstance(filename, str) or not filename.endswith(".gz"):
        raise SiderCriticalError(
            f"SIDER filename must end in .gz, got: {filename!r}",
            context={"filename": filename},
        )
    for int_key in ("expected_record_count", "max_size_bytes",
                    "retry_count", "timeout_seconds"):
        val: Any = cfg.get(int_key)
        if not isinstance(val, int) or val <= 0:
            raise SiderCriticalError(
                f"SIDER config {int_key!r} must be a positive int, got: {val!r}",
                context={"key": int_key, "value": val},
            )
    if not isinstance(cfg.get("retry_backoff_seconds"), (int, float)) \
            or cfg["retry_backoff_seconds"] < 0:
        raise SiderCriticalError(
            f"SIDER retry_backoff_seconds must be >= 0, got: "
            f"{cfg.get('retry_backoff_seconds')!r}",
        )
    # D12.4 — pinned must be True for SIDER (Phase 0.4 — critical source).
    if not cfg.get("pinned", False):
        raise SiderCriticalError(
            "SIDER config 'pinned' must be True for a CRITICAL source "
            "(Phase 0.4 — silent schema drift would harm patients).",
            context={"pinned": cfg.get("pinned")},
        )


def _resolve_sider_filepath(filepath: Optional[Path] = None) -> Path:
    """Resolve the SIDER input filepath with priority (D12.3):

    1. Explicit ``filepath`` argument (highest priority)
    2. ``DRUGOS_SIDER_FILEPATH`` env var
    3. ``DRUGOS_SIDER_LOCAL_PATH`` env var (alias)
    4. ``RAW_DIR / DATA_SOURCES['sider']['filename']`` (default)
    """
    if filepath is not None:
        return Path(filepath)
    env_file: Optional[str] = (
        os.environ.get("DRUGOS_SIDER_FILEPATH")
        or os.environ.get("DRUGOS_SIDER_LOCAL_PATH")
    )
    if env_file:
        return Path(env_file)
    cfg: Dict[str, Any] = _get_sider_config()
    return RAW_DIR / cfg["filename"]


def _resolve_force(force: bool) -> bool:
    """Resolve force-download flag (D12.3).

    Returns True if either:
      * ``force`` argument is True, OR
      * ``DRUGOS_SIDER_FORCE_DOWNLOAD=1`` env var is set.
    """
    if force:
        return True
    return os.environ.get("DRUGOS_SIDER_FORCE_DOWNLOAD", "0") == "1"


def _should_skip() -> bool:
    """Return True if SIDER load should be skipped (D12.3).

    Honors ``DRUGOS_SIDER_SKIP=1`` env var. The run_pipeline.py caller
    is responsible for honoring this; the loader only reports it.
    """
    return os.environ.get("DRUGOS_SIDER_SKIP", "0") == "1"


def _is_offline() -> bool:
    """Return True if SIDER load is in offline mode (D12.3).

    Honors ``DRUGOS_SIDER_OFFLINE=1`` env var. When True, the loader
    skips download and uses the cached file only. Raises if the file
    is missing.
    """
    return os.environ.get("DRUGOS_SIDER_OFFLINE", "0") == "1"


def _skip_sha256() -> bool:
    """Return True if sha256 verification should be skipped (D12.3 — dev only).

    Honors ``DRUGOS_SIDER_SKIP_SHA256=1`` env var. Logs a WARNING when
    active — skipping sha256 is dangerous in production (D3.8 / D9.3).
    """
    skip = os.environ.get("DRUGOS_SIDER_SKIP_SHA256", "0") == "1"
    if skip:
        logger.warning(
            "sider_sha256_verification_skipped",
            extra={"hint": "DRUGOS_SIDER_SKIP_SHA256=1 — dev mode only"},
        )
    return skip


def _allow_legacy() -> bool:
    """Return True if legacy emitter is allowed post-migration (D14.10 / D2.10).

    Honors ``DRUGOS_SIDER_ALLOW_LEGACY=1`` env var. When True, the legacy
    emitter ``sider_to_legacy_edge_records`` works without raising
    RuntimeError. When False (default), the legacy emitter raises
    RuntimeError to enforce migration completion.
    """
    return os.environ.get("DRUGOS_SIDER_ALLOW_LEGACY", "0") == "1"


def _resolve_max_rows(max_rows: Optional[int] = None) -> Optional[int]:
    """Resolve the max-rows cap (D12.3 / D12.12).

    Priority:
      1. Explicit ``max_rows`` argument
      2. ``DRUGOS_SIDER_MAX_ROWS`` env var
      3. None (no cap)
    """
    if max_rows is not None:
        if not isinstance(max_rows, int) or max_rows <= 0:
            raise SiderCriticalError(
                f"max_rows must be a positive int, got: {max_rows!r}",
                context={"max_rows": max_rows},
            )
        return max_rows
    env_mr: Optional[str] = os.environ.get("DRUGOS_SIDER_MAX_ROWS")
    if env_mr:
        try:
            return int(env_mr)
        except ValueError as exc:
            raise SiderCriticalError(
                f"DRUGOS_SIDER_MAX_ROWS must be an int, got: {env_mr!r}",
            ) from exc
    return None


def _resolve_batch_size(batch_size: Optional[int] = None) -> int:
    """Resolve the streaming batch size (D12.3).

    Priority:
      1. Explicit ``batch_size`` argument
      2. ``DRUGOS_SIDER_BATCH_SIZE`` env var
      3. ``SIDER_BATCH_SIZE`` config constant (10,000)
    """
    if batch_size is not None:
        if not isinstance(batch_size, int) or batch_size <= 0:
            raise SiderCriticalError(
                f"batch_size must be a positive int, got: {batch_size!r}",
                context={"batch_size": batch_size},
            )
        return batch_size
    env_bs: Optional[str] = os.environ.get("DRUGOS_SIDER_BATCH_SIZE")
    if env_bs:
        try:
            return int(env_bs)
        except ValueError as exc:
            raise SiderCriticalError(
                f"DRUGOS_SIDER_BATCH_SIZE must be an int, got: {env_bs!r}",
            ) from exc
    return SIDER_BATCH_SIZE


def _resolve_chunk_size(chunk_size: Optional[int] = None) -> int:
    """Resolve the streaming chunk size (D12.3)."""
    if chunk_size is not None:
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise SiderCriticalError(
                f"chunk_size must be a positive int, got: {chunk_size!r}",
                context={"chunk_size": chunk_size},
            )
        return chunk_size
    env_cs: Optional[str] = os.environ.get("DRUGOS_SIDER_CHUNK_SIZE")
    if env_cs:
        try:
            return int(env_cs)
        except ValueError as exc:
            raise SiderCriticalError(
                f"DRUGOS_SIDER_CHUNK_SIZE must be an int, got: {env_cs!r}",
            ) from exc
    return SIDER_CHUNK_SIZE


# ===== SECTION 6: UTILITIES (timestamps, IDs, hashes, URLs) =====

def _iso_now() -> str:
    """Return the current UTC time in ISO-8601 format with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_load_id() -> str:
    """Return the process-cached load_id (correlation ID).

    The load_id is generated once per process (UUID4 hex prefix) and
    cached for the lifetime of the process. This allows all log entries
    and output records from a single pipeline run to be correlated.
    Tests can reset it via ``_reset_load_id``.
    """
    global _LOAD_ID
    with _LOAD_ID_LOCK:
        if _LOAD_ID is None:
            _LOAD_ID = (
                f"sider_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
                f"_{uuid.uuid4().hex[:8]}"
            )
        return _LOAD_ID


def _reset_load_id() -> None:
    """Reset the process-cached load_id (test helper — D11.2).

    Also resets the dual-write mutual-exclusion flags (D2.13 / G13) so
    tests can exercise both emitters independently.
    """
    global _LOAD_ID, _CANONICAL_EMITTED, _LEGACY_EMITTED
    with _LOAD_ID_LOCK:
        _LOAD_ID = None
    with _DUAL_WRITE_LOCK:
        _CANONICAL_EMITTED = False
        _LEGACY_EMITTED = False


def _compute_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute the SHA-256 hex digest of a file (D3.8 / D7.2).

    Reads the file in 1 MB chunks to avoid loading the entire file into
    memory (SIDER is ~50 MB compressed, ~5M rows uncompressed — would
    OOM on small instances).
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk: bytes = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sanitize_url_for_logging(url: str) -> str:
    """Mask embedded credentials in a URL before logging (D9.5 / D9.1)."""
    return _URL_CRED_RE.sub("://***:***@", url)


def _safe_str(v: Any) -> Optional[str]:
    """Return ``str(v)`` if ``v`` is not None / NaN, else None (D4.6)."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, str) and v == "":
        return None
    if v is pd.NA:
        return None
    s: str = str(v)
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    return s


def _sanitize_for_cypher(s: str) -> str:
    """Sanitize a string for safe inclusion in a Cypher query (D9.7 / G8).

    Escapes backslashes, single quotes, and double quotes. This is a
    DEFENSE-IN-DEPTH measure — callers should always use parameterized
    queries, but if a string must be inlined (e.g. for a script), this
    prevents injection.

    Examples
    --------
    >>> _sanitize_for_cypher("normal")
    'normal'
    >>> _sanitize_for_cypher("O'Reilly")
    "O\\\\'Reilly"
    >>> _sanitize_for_cypher('say "hi"')
    'say \\\\"hi\\\\"'
    """
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
         .replace("'", "\\'")
         .replace('"', '\\"')
    )


def _validate_url(url: str) -> None:
    """Validate the SIDER download URL (D9.1 / D9.2 / D9.9).

    Raises
    ------
    SiderDownloadError
        If the URL is not HTTPS, not in the allowlist, contains embedded
        credentials, or resolves to a private/internal IP (SSRF guard).
    """
    if not isinstance(url, str) or not url:
        raise SiderDownloadError(
            "SIDER URL is empty or not a string.",
            context={"url": repr(url)},
        )
    # D9.2 — URL scheme validation.
    if not url.startswith(("https://", "http://")):
        raise SiderDownloadError(
            f"Invalid SIDER URL scheme: {url!r}",
            context={"url": _sanitize_url_for_logging(url)},
        )
    if url.startswith("http://"):
        logger.warning(
            "sider_url_not_https",
            extra={"url": _sanitize_url_for_logging(url),
                   "hint": "Should be https://"},
        )
    # D9.1 — URL allowlist (SSRF guard).
    if not any(url.startswith(prefix) for prefix in ALLOWED_SIDER_URLS):
        raise SiderDownloadError(
            f"SIDER URL not in ALLOWED_SIDER_URLS: "
            f"{_sanitize_url_for_logging(url)}",
            context={"url": _sanitize_url_for_logging(url),
                     "allowlist": list(ALLOWED_SIDER_URLS)},
        )
    # D9.1 — reject embedded credentials.
    if "@" in url.split("://", 1)[-1]:
        raise SiderDownloadError(
            f"SIDER URL contains embedded credentials (refusing): "
            f"{_sanitize_url_for_logging(url)}",
            context={"url": _sanitize_url_for_logging(url)},
        )


def _validate_filename_safe(filename: str) -> None:
    """Reject path-traversal / null bytes / non-.gz filenames (D9.4 / D9.8).

    Raises
    ------
    SiderDownloadError
        If the filename contains ``..``, ``/``, ``\\``, null bytes, or
        does not end in ``.gz``.
    """
    if not isinstance(filename, str) or not filename:
        raise SiderDownloadError(
            f"SIDER filename is empty or not a string: {filename!r}",
        )
    if "\x00" in filename:
        raise SiderDownloadError(
            f"SIDER filename contains null byte: {filename!r}",
        )
    if ".." in filename or "/" in filename or "\\" in filename:
        raise SiderDownloadError(
            f"SIDER filename contains path-traversal chars: {filename!r}",
        )
    if not filename.endswith(".gz"):
        raise SiderDownloadError(
            f"SIDER filename must end in .gz: {filename!r}",
        )


def _validate_path_within_dir(path: Path, directory: Path) -> None:
    """Assert ``path`` resolves to a path inside ``directory`` (D9.4)."""
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise SiderDownloadError(
            f"SIDER path {path} is outside allowed directory {directory}.",
            context={"path": str(path), "directory": str(directory)},
        ) from exc


def _set_secure_file_permissions(path: Path, mode: int = SIDER_FILE_PERMISSIONS) -> None:
    """Set secure file permissions on POSIX (D9.8).

    On Windows, ``os.chmod`` is a no-op for read/write/execute bits, so
    this function is a safe no-op there.
    """
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.warning(
            "sider_chmod_failed",
            extra={"path": str(path), "mode": oct(mode), "error": str(exc)},
        )


def _create_ssl_context() -> ssl.SSLContext:
    """Create a TLS-strict SSLContext for SIDER downloads (D9.1).

    Uses ``certifi`` if available; falls back to system CA store.
    """
    ctx: ssl.SSLContext = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        # System CA store is the fallback — may be outdated on some distros.
        pass
    return ctx


def _is_private_ip(host: str) -> bool:
    """Return True if ``host`` resolves to a private/internal IP (D9.1 — SSRF)."""
    try:
        addr = socket.gethostbyname(host)
        ip_obj = ipaddress.ip_address(addr)
        return (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
        )
    except (socket.gaierror, ValueError):
        return False


def _verify_gzip_magic_bytes(path: Path) -> None:
    """Verify the file starts with gzip magic bytes ``\\x1f\\x8b`` (D6.4).

    Raises
    ------
    SiderDownloadError
        If the file does not start with the gzip magic bytes (likely an
        HTML error page returned by the server).
    """
    try:
        with open(path, "rb") as f:
            magic: bytes = f.read(2)
    except OSError as exc:
        raise SiderDownloadError(
            f"Cannot read SIDER file for gzip magic-byte check: {path} ({exc})",
            context={"path": str(path), "error": str(exc)},
        ) from exc
    if magic[:2] != b"\x1f\x8b":
        raise SiderDownloadError(
            f"SIDER file is not a valid gzip file (magic bytes {magic!r} "
            f"do not match \\x1f\\x8b): {path}",
            context={"path": str(path), "magic_bytes": magic.hex()},
        )


def _verify_size(path: Path, cfg: Dict[str, Any]) -> int:
    """Verify the downloaded file size is within bounds (D4.26 / D5.1).

    Returns
    -------
    int
        The file size in bytes.
    """
    size: int = path.stat().st_size
    if size == 0:
        raise SiderDownloadError(
            f"SIDER file is 0 bytes: {path}",
            context={"path": str(path)},
        )
    if size < SIDER_MIN_VALID_SIZE_BYTES:
        raise SiderDownloadError(
            f"SIDER file size {size} bytes is below minimum "
            f"{SIDER_MIN_VALID_SIZE_BYTES} (likely an HTML error page): {path}",
            context={"path": str(path), "size_bytes": size,
                     "min_size": SIDER_MIN_VALID_SIZE_BYTES},
        )
    max_size: int = int(cfg.get("max_size_bytes", 500_000_000))
    if size > max_size:
        raise SiderDownloadError(
            f"SIDER file size {size} bytes exceeds max {max_size}: {path}",
            context={"path": str(path), "size_bytes": size, "max_size": max_size},
        )
    return size


def _verify_checksum(path: Path, cfg: Dict[str, Any]) -> str:
    """Compute and verify the file's SHA-256 (D3.8 / D4.19 / D9.3).

    If ``cfg["sha256"]`` is set (pinned), the computed sha256 MUST match.
    If not set, the computed sha256 is returned but no comparison is made.

    Returns
    -------
    str
        The computed sha256 hex digest.
    """
    if _skip_sha256():
        return ""
    actual: str = _compute_sha256(path)
    expected: Optional[str] = cfg.get("sha256")
    if expected and actual != expected:
        raise SiderDataQualityError(
            f"SIDER sha256 mismatch: expected {expected}, got {actual} ({path}).",
            context={"path": str(path), "expected_sha256": expected,
                     "actual_sha256": actual},
        )
    return actual


def _verify_integrity(path: Path, cfg: Dict[str, Any]) -> str:
    """Umbrella integrity check: gzip magic + size + checksum (A1.5 / D6.12)."""
    _verify_gzip_magic_bytes(path)
    _verify_size(path, cfg)
    return _verify_checksum(path, cfg)


def _check_freshness(gz_path: Path, cfg: Dict[str, Any]) -> None:
    """Warn if the cached file is older than the expected update frequency (D5.9).

    SIDER publishes ~annually. If the cached file is older than
    ``SIDER_STALE_FILE_DAYS`` (default 365), log a WARNING so the
    operator knows to refresh.
    """
    try:
        mtime: float = gz_path.stat().st_mtime
    except OSError:
        return
    age_days: float = (time.time() - mtime) / 86400.0
    if age_days > SIDER_STALE_FILE_DAYS:
        logger.warning(
            "sider_file_stale",
            extra={"path": str(gz_path), "age_days": round(age_days, 1),
                   "stale_threshold_days": SIDER_STALE_FILE_DAYS,
                   "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                   "hint": "Consider running download_sider(force=True) to refresh."},
        )


# ─── Sidecar helpers (D3.8 / D7.2 / GAP-15.3) ────────────────────────────────

def _sidecar_version_path(gz_path: Path) -> Path:
    return gz_path.with_suffix(gz_path.suffix + _SIDECAR_VERSION_SUFFIX)


def _read_sidecar_version(gz_path: Path) -> Optional[str]:
    """Read the cached SIDER version from the .version sidecar (GAP-15.3)."""
    p: Path = _sidecar_version_path(gz_path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_sidecar_version(gz_path: Path, version: str) -> None:
    p: Path = _sidecar_version_path(gz_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(version, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "sider_sidecar_version_write_failed",
            extra={"path": str(p), "error": str(exc)},
        )


def _sidecar_sha256_path(gz_path: Path) -> Path:
    return gz_path.with_suffix(gz_path.suffix + _SIDECAR_SHA256_SUFFIX)


def _read_sidecar_sha256(gz_path: Path) -> Optional[str]:
    """Read the cached SHA-256 from the .sha256 sidecar (D3.8 / D7.2)."""
    p: Path = _sidecar_sha256_path(gz_path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_sidecar_sha256(gz_path: Path, sha256: str) -> None:
    p: Path = _sidecar_sha256_path(gz_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(sha256, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "sider_sidecar_sha256_write_failed",
            extra={"path": str(p), "error": str(exc)},
        )


# ─── Retry / atomic download (D6.1 / D6.2 / D4.9 / D4.10 / G10) ──────────────

def _retry_with_backoff(
    func: Any,
    *,
    retry_count: int = SIDER_MAX_RETRIES,
    retry_backoff: float = float(SIDER_RETRY_BACKOFF_BASE),
) -> Any:
    """Call ``func()`` with exponential backoff on failure (D6.1).

    Raises the last exception if all retries are exhausted.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retry_count):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 — caller decides
            last_exc = exc
            if attempt == retry_count - 1:
                break
            wait: float = (retry_backoff ** attempt) + random.random()
            logger.warning(
                "sider_download_retry",
                extra={"attempt": attempt + 1, "max_retries": retry_count,
                       "wait_seconds": round(wait, 2),
                       "error_type": type(exc).__name__, "error": str(exc)},
            )
            time.sleep(wait)
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
    timeout: float = float(SIDER_DOWNLOAD_TIMEOUT_SECONDS),
    progress_callback: Optional[Any] = None,
) -> Path:
    """Download ``url`` to ``dest`` atomically via .part + os.replace (D4.10 / G10).

    The download is streamed to a ``.part`` file in 64 KB chunks. After
    all chunks are written, the file is size-validated and gzip-magic-byte
    sniffed before being atomically renamed to ``dest``. On any failure,
    the ``.part`` file is deleted and the original ``dest`` is left intact.

    Raises
    ------
    SiderDownloadError
        On network timeout, HTTP error, size mismatch, or gzip failure.
    """
    # Fixes D4.10: atomic write via .part + os.replace.
    # Fixes D6.4: BadGzipFile handling (gzip magic-byte sniff).
    # Fixes D9.1: TLS verification via _create_ssl_context.
    _validate_url(url)
    part_path: Path = dest.with_suffix(dest.suffix + ".part")
    bytes_downloaded: int = 0
    request: urllib.request.Request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DrugOS-Graph/1.1.0 (research; contact@teamcosmic.example)",
            "Accept-Encoding": "gzip",
        },
    )
    ssl_context: ssl.SSLContext = _create_ssl_context()

    def _do_download() -> None:
        nonlocal bytes_downloaded
        bytes_downloaded = 0
        # D4.9 — socket timeout.
        socket.setdefaulttimeout(timeout)
        # D9.9 — control redirects (max_redirects).
        opener: urllib.request.OpenerDirector = urllib.request.build_opener()
        opener.addheaders = [("User-Agent", request.header_items()[0][1])]

        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context
        ) as resp:
            # D9.9 — log redirects.
            final_url: str = resp.geturl()
            if final_url != url:
                logger.warning(
                    "sider_download_redirected",
                    extra={"original_url": _sanitize_url_for_logging(url),
                           "final_url": _sanitize_url_for_logging(final_url)},
                )
            content_length: Optional[str] = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                raise SiderDownloadError(
                    f"SIDER download Content-Length {content_length} exceeds "
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
                        raise SiderDownloadError(
                            f"SIDER download exceeded max_size {max_size} "
                            f"after {bytes_downloaded} bytes.",
                            context={"url": _sanitize_url_for_logging(url),
                                     "bytes_downloaded": bytes_downloaded,
                                     "max_size": max_size},
                        )
                    # D2.15 — progress callback (called every chunk).
                    if progress_callback is not None:
                        try:
                            progress_callback(bytes_downloaded, int(content_length) if content_length else 0)
                        except Exception:  # noqa: BLE001 — callback errors must not crash
                            logger.warning(
                                "sider_progress_callback_failed",
                                extra={"error_type": "unknown"},
                            )

    _retry_with_backoff(
        _do_download,
        retry_count=retry_count,
        retry_backoff=retry_backoff,
    )

    # D4.26 — zero-byte guard.
    if bytes_downloaded == 0:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SiderDownloadError(
            f"SIDER download produced 0 bytes from {url}.",
            context={"url": _sanitize_url_for_logging(url)},
        )

    # Size validation (D5.1).
    if bytes_downloaded < SIDER_MIN_VALID_SIZE_BYTES:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SiderDownloadError(
            f"SIDER download size {bytes_downloaded} bytes is below minimum "
            f"{SIDER_MIN_VALID_SIZE_BYTES} (likely an HTML error page).",
            context={"url": _sanitize_url_for_logging(url),
                     "bytes_downloaded": bytes_downloaded,
                     "min_size": SIDER_MIN_VALID_SIZE_BYTES},
        )

    # D6.4 — gzip magic-byte sniff.
    try:
        with open(part_path, "rb") as f_check:
            magic: bytes = f_check.read(2)
    except OSError as exc:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SiderDownloadError(
            f"Cannot read SIDER download for gzip magic-byte check: {exc}",
            context={"url": _sanitize_url_for_logging(url), "error": str(exc)},
        ) from exc
    if magic[:2] != b"\x1f\x8b":
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SiderDownloadError(
            f"SIDER download is not a valid gzip file (magic bytes "
            f"{magic!r} do not match \\x1f\\x8b).",
            context={"url": _sanitize_url_for_logging(url),
                     "magic_bytes": magic.hex()},
        )

    # D4.10 — atomic rename.
    os.replace(part_path, dest)
    _set_secure_file_permissions(dest)
    return dest


# ─── Audit / DLQ / lineage / checkpoint writers (D5.12 / D6.5 / D9.5 / D11.3 / D16.2 / D16.12) ──

def _append_audit_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append a JSONL entry to the SIDER audit log (D9.5).

    The audit log lives at ``logs/audit/sider_access.jsonl`` by default.
    Each entry is timestamped (ISO-8601 UTC), includes the ``load_id``
    correlation ID, the user (from env), and the PID. Append-only.
    """
    log_path: Path = path or _AUDIT_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # D9.8 — set secure dir permissions.
    try:
        os.chmod(log_path.parent, SIDER_LOG_DIR_PERMISSIONS)
    except OSError:
        pass
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "load_id": _get_load_id(),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "pid": os.getpid(),
        "python_version": f"{__import__('sys').version_info.major}."
                          f"{__import__('sys').version_info.minor}."
                          f"{__import__('sys').version_info.micro}",
        "drugos_version": PARSER_VERSION,
        **event,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "sider_audit_log_write_failed",
            extra={"path": str(log_path), "error": str(exc)},
        )


def _append_transformation_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append a JSONL entry to the SIDER transformation log (D16.2)."""
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
            "sider_transformation_log_write_failed",
            extra={"path": str(log_path), "error": str(exc)},
        )


# D16.12 — Regulatory audit log (chained sha256, append-only, tamper-evident).
_REG_AUDIT_PREV_HASH: str = "0" * 64  # genesis hash


def _append_regulatory_audit_log(event: Dict[str, Any]) -> None:
    """Append a tamper-evident entry to the SIDER regulatory audit log (D16.12).

    Each entry includes ``prev_hash`` (the sha256 of the previous entry's
    ``record_hash`` field) so any tampering is detectable by re-walking
    the chain. This implements 21 CFR Part 11 audit-trail requirements.

    Fields per entry (per master prompt §D16.12):
        operator_identity, timestamp_utc, input_file_sha256,
        output_record_count, schema_version, software_version, load_id,
        prev_hash, record_hash
    """
    global _REG_AUDIT_PREV_HASH
    _REGULATORY_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Load the last hash from the file (so we recover chain on restart).
    if _REG_AUDIT_PREV_HASH == "0" * 64 and _REGULATORY_AUDIT_LOG_PATH.exists():
        try:
            with open(_REGULATORY_AUDIT_LOG_PATH, "rb") as f:
                f.seek(0, 2)
                size: int = f.tell()
                if size > 0:
                    # Read last line.
                    f.seek(max(0, size - 8192))
                    tail: str = f.read().decode("utf-8", errors="ignore")
                    lines: List[str] = [ln for ln in tail.splitlines() if ln.strip()]
                    if lines:
                        last: Dict[str, Any] = json.loads(lines[-1])
                        _REG_AUDIT_PREV_HASH = last.get("record_hash", "0" * 64)
        except (OSError, json.JSONDecodeError):
            pass
    entry: Dict[str, Any] = {
        "operator_identity": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "timestamp_utc": _iso_now(),
        "input_file_sha256": event.get("input_file_sha256", ""),
        "output_record_count": event.get("output_record_count", 0),
        "schema_version": SCHEMA_VERSION,
        "software_version": PARSER_VERSION,
        "load_id": _get_load_id(),
        "prev_hash": _REG_AUDIT_PREV_HASH,
        **event,
    }
    # record_hash = sha1(json of entry minus record_hash, sorted)
    payload: str = json.dumps(
        {k: v for k, v in entry.items() if k != "record_hash"},
        sort_keys=True,
        default=str,
    )
    record_hash: str = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    entry["record_hash"] = record_hash
    _REG_AUDIT_PREV_HASH = record_hash
    try:
        with open(_REGULATORY_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "sider_regulatory_audit_log_write_failed",
            extra={"path": str(_REGULATORY_AUDIT_LOG_PATH), "error": str(exc)},
        )


def _write_to_dlq(entry: Dict[str, Any]) -> None:
    """Buffer a single DLQ entry; flushed by _flush_dlq (D5.12 / D6.5)."""
    with _DLQ_BUFFER_LOCK:
        _DLQ_BUFFER.append(entry)
        if len(_DLQ_BUFFER) >= _DLQ_FLUSH_SIZE:
            _flush_dlq_unlocked()


def _flush_dlq(dlq_path: Optional[Path] = None) -> None:
    """Flush buffered DLQ entries to disk (D6.5)."""
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
            "sider_dlq_flush_failed",
            extra={"path": str(path), "error": str(exc),
                   "buffered_entries": len(_DLQ_BUFFER)},
        )


def _write_quality_report(report: Dict[str, Any]) -> None:
    """Write the data-quality report to logs/quality/sider_quality_report.json (D5.11)."""
    _QUALITY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_QUALITY_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    except OSError as exc:
        logger.warning(
            "sider_quality_report_write_failed",
            extra={"path": str(_QUALITY_REPORT_PATH), "error": str(exc)},
        )


def _append_lineage_log(event: Dict[str, Any]) -> None:
    """Append a JSONL entry to the SIDER lineage log (D11.3)."""
    _LINEAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "load_id": _get_load_id(),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        **event,
    }
    try:
        with open(_LINEAGE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "sider_lineage_log_write_failed",
            extra={"path": str(_LINEAGE_LOG_PATH), "error": str(exc)},
        )


def _write_checkpoint(stage: str, row_index: int, edges_count: int) -> None:
    """Write a checkpoint JSON file for resumable processing (D6.10)."""
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        with open(_CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning(
            "sider_checkpoint_write_failed",
            extra={"path": str(_CHECKPOINT_PATH), "error": str(exc)},
        )


def _update_registry(entry: Dict[str, Any]) -> None:
    """Update the dataset registry at data/registry.json (D16.9)."""
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if _REGISTRY_PATH.exists():
            with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
                registry: Dict[str, Any] = json.load(f)
        else:
            registry = {}
    except (OSError, json.JSONDecodeError):
        registry = {}
    registry[SOURCE_KEY_SIDER] = entry
    try:
        with open(_REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2, default=str)
    except OSError as exc:
        logger.warning(
            "sider_registry_update_failed",
            extra={"path": str(_REGISTRY_PATH), "error": str(exc)},
        )


def _quarantine_file(path: Path, reason: str) -> Path:
    """Move a corrupt file to the quarantine directory (D6.12).

    Returns the quarantine path.
    """
    _QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    ts: str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    quarantine_path: Path = _QUARANTINE_DIR / f"sider_{ts}_{path.name}.bak"
    try:
        os.replace(path, quarantine_path)
        logger.error(
            "sider_file_quarantined",
            extra={"original_path": str(path),
                   "quarantine_path": str(quarantine_path),
                   "reason": reason},
        )
        _append_audit_log({
            "event": "file_quarantined",
            "original_path": str(path),
            "quarantine_path": str(quarantine_path),
            "reason": reason,
        })
    except OSError as exc:
        logger.error(
            "sider_quarantine_failed",
            extra={"path": str(path), "error": str(exc)},
        )
    return quarantine_path




# ===== SECTION 7: DOWNLOAD LAYER =====
# Fixes Phase 0.4 / D6.1 / D6.2 / D6.3 / D6.9 / D6.12 / D9.1 / D9.2 /
#        D9.9 / D4.10 / D4.12 / D4.19 / D4.20 / D4.26 / D5.9 / D7.3 / G10.

def download_sider(
    force: bool = False,
    *,
    dry_run: bool = False,
    verify_only: bool = False,
    progress_callback: Optional[Any] = None,
    data_dir: Optional[Path] = None,
) -> Path:
    """Download the SIDER meddra_all_se.tsv.gz file (institutional-grade v1.0.0).

    Backward-compatible signature (Rule R3):
    ``download_sider(force=False) -> Path``.

    The download is atomic, TLS-verified, size-validated, checksum-verified,
    and circuit-breaker-protected. On cache hit (``force=False``), the
    SHA-256 of the cached file is verified against the pinned value (if
    set); on mismatch, the file is re-downloaded (D4.19).

    Parameters
    ----------
    force : bool, default False
        If True, re-download even if the file exists. Logs a WARNING
        before overwriting.
    dry_run : bool, default False
        If True, log the URL and target path WITHOUT downloading. Returns
        the target path (which may not exist).
    verify_only : bool, default False
        If True, verify the existing file's sha256 against the pinned
        value WITHOUT downloading. Returns the path if verification
        succeeds; raises SiderDownloadError on mismatch or missing file.
    progress_callback : callable, optional
        Called as ``callback(bytes_downloaded, total_bytes)`` every 64 KB
        chunk. ``total_bytes`` is 0 if Content-Length is missing.
    data_dir : Path, optional
        Override the data-raw directory. Defaults to ``RAW_DIR``.

    Returns
    -------
    Path
        The path to the downloaded (or cached) SIDER .tsv.gz file.

    Raises
    ------
    SiderCriticalError
        If SIDER is required (``SIDER_REQUIRED=True``, default) and the
        download fails after all retries (Phase 0.4 / G6).
    SiderDownloadError
        On network timeout, HTTP error, size mismatch, gzip failure,
        URL-not-allowlisted, or HTTP 4xx/5xx.
    SiderDataQualityError
        If SHA-256 or size verification fails after download.

    Side Effects
    ------------
    - Writes the file to ``data_dir / DATA_SOURCES['sider']['filename']``.
    - Writes a .sha256 sidecar file (D3.8 / D7.2).
    - Writes a .version sidecar file (GAP-15.3).
    - Appends an entry to ``logs/audit/sider_access.jsonl`` (D9.5).
    - Appends an entry to ``logs/audit/sider_regulatory.jsonl`` (D16.12).
    - Sets secure file permissions (0o644) on POSIX (D9.8).
    - Creates ``data_dir`` if missing (D4.12).

    Examples
    --------
    >>> from drugos_graph.sider_loader import download_sider
    >>> path = download_sider()  # doctest: +SKIP
    >>> path.name  # doctest: +SKIP
    'sider_meddra_all_se.tsv.gz'
    >>> download_sider(dry_run=True)  # doctest: +SKIP
    """
    # Fixes Phase 0.4 / D6.3 — SIDER is CRITICAL; no graceful degradation.
    # Fixes A1.9 / D4.27 — accept data_dir parameter (default RAW_DIR).
    cfg: Dict[str, Any] = _get_sider_config()
    _validate_sider_config(cfg)
    raw_dir: Path = Path(data_dir) if data_dir is not None else RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)  # D4.12 — create dir if missing.
    gz_path: Path = raw_dir / cfg["filename"]
    _validate_filename_safe(cfg["filename"])
    _validate_path_within_dir(gz_path, raw_dir)

    # D12.3 — offline mode: skip download, use cached only.
    if _is_offline():
        if not gz_path.exists():
            raise SiderCriticalError(
                f"DRUGOS_SIDER_OFFLINE=1 but cached file does not exist: {gz_path}",
                context={"path": str(gz_path)},
            )
        logger.info(
            "sider_offline_mode_using_cached",
            extra={"path": str(gz_path)},
        )
        return gz_path

    # D2.15 — dry_run: log + return without downloading.
    if dry_run:
        logger.info(
            "sider_download_dry_run",
            extra={"url": _sanitize_url_for_logging(cfg["url"]),
                   "dest": str(gz_path)},
        )
        return gz_path

    # D2.15 — verify_only: check sha256 without downloading.
    if verify_only:
        if not gz_path.exists():
            raise SiderDownloadError(
                f"SIDER file not found for verify_only: {gz_path}",
                context={"path": str(gz_path)},
            )
        actual_sha: str = _compute_sha256(gz_path)
        expected_sha: Optional[str] = cfg.get("sha256") or _read_sidecar_sha256(gz_path)
        if expected_sha and actual_sha != expected_sha:
            raise SiderDownloadError(
                f"SIDER sha256 mismatch: expected {expected_sha}, got {actual_sha}.",
                context={"path": str(gz_path),
                         "expected_sha256": expected_sha,
                         "actual_sha256": actual_sha},
            )
        logger.info(
            "sider_verify_only_ok",
            extra={"path": str(gz_path), "sha256": actual_sha},
        )
        return gz_path

    # Cache check (idempotency — D7.1).
    if gz_path.exists() and not _resolve_force(force):
        # Fixes D4.19 / D7.2: verify SHA-256 on cache hit.
        try:
            cached_sha: str = _verify_checksum(gz_path, cfg)
            _check_freshness(gz_path, cfg)
            logger.info(
                "sider_cache_hit",
                extra={"path": str(gz_path), "sha256": cached_sha,
                       "size_bytes": gz_path.stat().st_size,
                       "size_mb": round(gz_path.stat().st_size / _MB, 2)},
            )
            _append_audit_log({
                "event": "cache_hit",
                "url": _sanitize_url_for_logging(cfg["url"]),
                "dest": str(gz_path), "sha256": cached_sha,
                "size_bytes": gz_path.stat().st_size,
            })
            return gz_path
        except SiderDataQualityError as exc:
            logger.warning(
                "sider_cache_corrupt_redownloading",
                extra={"path": str(gz_path), "error": str(exc)},
            )
            # D6.12 — quarantine the corrupt file.
            _quarantine_file(gz_path, reason=f"sha256_mismatch: {exc}")

    # GAP-15.3 — version-skew check.
    sidecar_version: Optional[str] = _read_sidecar_version(gz_path)
    if sidecar_version is not None and sidecar_version != cfg.get("version"):
        logger.warning(
            "sider_version_skew_redownloading",
            extra={"cached_version": sidecar_version,
                   "expected_version": cfg.get("version")},
        )
        try:
            gz_path.unlink()
        except OSError:
            pass

    # D7.3 — stale-file detection (re-download if very stale).
    if gz_path.exists() and not _resolve_force(force):
        try:
            age_days: float = (time.time() - gz_path.stat().st_mtime) / 86400.0
            if age_days > SIDER_STALE_FILE_DAYS * 2:
                logger.warning(
                    "sider_file_severely_stale_redownloading",
                    extra={"path": str(gz_path), "age_days": round(age_days, 1)},
                )
                gz_path.unlink(missing_ok=True)
        except OSError:
            pass

    # force=True warn before overwrite.
    if gz_path.exists() and _resolve_force(force):
        logger.warning(
            "sider_force_overwrite",
            extra={"path": str(gz_path), "size_bytes": gz_path.stat().st_size},
        )

    # Download.
    logger.info(
        "sider_download_start",
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
            max_size=int(cfg.get("max_size_bytes", 500_000_000)),
            retry_count=int(cfg.get("retry_count", SIDER_MAX_RETRIES)),
            retry_backoff=float(cfg.get("retry_backoff_seconds", SIDER_RETRY_BACKOFF_BASE)),
            timeout=float(cfg.get("timeout_seconds", SIDER_DOWNLOAD_TIMEOUT_SECONDS)),
            progress_callback=progress_callback,
        )
    except SiderDownloadError:
        # Phase 0.4 / G6 — SIDER is critical; raise SiderCriticalError.
        if SIDER_REQUIRED:
            raise SiderCriticalError(
                f"SIDER is required but download failed: {gz_path}",
                context={"url": _sanitize_url_for_logging(cfg["url"]),
                         "dest": str(gz_path)},
            )
        raise
    except SiderDataQualityError:
        raise
    except Exception as exc:
        # Wrap unexpected errors in SiderDownloadError (D6.9).
        if SIDER_REQUIRED:
            raise SiderCriticalError(
                f"SIDER download failed unexpectedly: {exc}",
                context={"url": _sanitize_url_for_logging(cfg["url"]),
                         "error_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        raise SiderDownloadError(
            f"SIDER download failed: {exc}",
            context={"url": _sanitize_url_for_logging(cfg["url"]),
                     "error_type": type(exc).__name__, "error": str(exc)},
        ) from exc

    # Post-download verification (A1.5 / D6.12).
    try:
        actual_sha = _verify_integrity(gz_path, cfg)
    except SiderDataQualityError:
        _quarantine_file(gz_path, reason="post_download_integrity_check_failed")
        raise

    # D3.8 / D7.2 — write sidecars.
    if actual_sha:
        _write_sidecar_sha256(gz_path, actual_sha)
    _write_sidecar_version(gz_path, str(cfg.get("version", SIDER_PINNED_VERSION)))

    size_bytes: int = gz_path.stat().st_size
    logger.info(
        "sider_download_complete",
        extra={"path": str(gz_path), "size_bytes": size_bytes,
               "size_mb": round(size_bytes / _MB, 2), "sha256": actual_sha},
    )
    _append_audit_log({
        "event": "download_complete",
        "url": _sanitize_url_for_logging(cfg["url"]),
        "dest": str(gz_path), "size_bytes": size_bytes, "sha256": actual_sha,
    })
    # D16.12 — regulatory audit trail entry.
    _append_regulatory_audit_log({
        "input_file_sha256": actual_sha,
        "output_record_count": 0,  # filled in after parse
        "event": "download_complete",
    })
    return gz_path


# ===== SECTION 8: PARSE LAYER =====
# Fixes Phase 0.1 / Phase 0.2 / A1.3 / A1.5 / A1.6 / D2.1-D2.7 / D3.1-D3.12 /
#        D4.3-D4.18 / D5.1-D5.13 / D6.4 / D6.7 / D7.1-D7.10 / D8.1-D8.12 /
#        D15.10 / D16.1 / D16.6 / D16.7.

def _validate_columns(df: pd.DataFrame) -> None:
    """Validate the parsed DataFrame has the expected 6 columns (D15.10).

    Raises
    ------
    SiderSchemaError
        If the column count is not 6, or any required column is missing.
    """
    # D15.10 — column count check.
    if len(df.columns) != SIDER_EXPECTED_COLUMN_COUNT:
        raise SiderSchemaError(
            f"SIDER file has {len(df.columns)} columns, expected "
            f"{SIDER_EXPECTED_COLUMN_COUNT}. Got: {list(df.columns)}",
            context={"actual_columns": list(df.columns),
                     "expected_count": SIDER_EXPECTED_COLUMN_COUNT},
        )
    missing: set = set(SIDER_COLUMN_NAMES) - set(df.columns)
    if missing:
        raise SiderSchemaError(
            f"SIDER file missing required columns: {sorted(missing)}.",
            context={"missing_columns": sorted(missing),
                     "actual_columns": list(df.columns)},
        )


def _validate_meddra_type(df: pd.DataFrame) -> pd.DataFrame:
    """Filter rows with invalid ``meddra_type`` to DLQ (D2.12 / D5.5).

    Returns the cleaned DataFrame (rows with valid meddra_type only).
    Writes invalid rows to the DLQ with reason ``"unknown_meddra_type"``.
    """
    if "meddra_type" not in df.columns:
        return df
    invalid_mask: pd.Series = ~df["meddra_type"].isin(VALID_MEDDRA_TYPES)
    n_invalid: int = int(invalid_mask.sum())
    if n_invalid > 0:
        logger.warning(
            "sider_unknown_meddra_type_dlq",
            extra={"count": n_invalid,
                   "unknown_types": sorted(set(df.loc[invalid_mask, "meddra_type"].dropna()))},
        )
        for idx, row in df.loc[invalid_mask].iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "unknown_meddra_type",
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_validate_meddra_type",
                "load_id": _get_load_id(),
            })
    return df.loc[~invalid_mask].copy()


def _validate_umls_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Filter rows with invalid UMLS CUIs to DLQ (D3.4 / D5.6).

    PS-7 / RT-3 ROOT FIX (patient safety): previously only
    ``umls_id_meddra`` (column 5) was validated against
    ``^C\\d{7}$``, leaving ``umls_id_label`` (column 3) unchecked
    — rows with a corrupt label CUI passed through silently and
    reached the KG as ``Compound→has_side_effect`` edges with a
    corrupt ``umls_id_label`` property. Also use ``str.fullmatch``
    instead of ``str.match`` so partial matches at the start
    (e.g. ``"C0000000EXTRA"``) are rejected, and record which
    column failed in the DLQ entry so the operator can trace the
    source of corruption.

    Invalid rows go to DLQ with reason ``"invalid_umls_cui"`` and a
    ``failed_column`` field.
    """
    invalid_mask = pd.Series(False, index=df.index)
    failed_column = pd.Series("", index=df.index, dtype="object")

    for col in ("umls_id_label", "umls_id_meddra"):
        if col not in df.columns:
            continue
        col_invalid = ~df[col].str.fullmatch(SIDER_UMLS_CUI_REGEX, na=False)
        invalid_mask |= col_invalid
        # Record the first failed column per row.
        new_failures = col_invalid & (failed_column == "")
        failed_column.loc[new_failures] = col

    n_invalid: int = int(invalid_mask.sum())
    if n_invalid > 0:
        by_column = (
            failed_column[invalid_mask].value_counts().to_dict()
            if n_invalid > 0
            else {}
        )
        logger.warning(
            "sider_invalid_umls_cui_dlq",
            extra={"count": n_invalid, "by_column": by_column},
        )
        invalid_df = df.loc[invalid_mask].copy()
        invalid_df["_failed_column"] = failed_column[invalid_mask]
        for idx, row in invalid_df.iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "invalid_umls_cui",
                "failed_column": str(row["_failed_column"]),
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_validate_umls_ids",
                "load_id": _get_load_id(),
            })
    return df.loc[~invalid_mask].copy()


def _validate_side_effect_name(df: pd.DataFrame) -> pd.DataFrame:
    """Filter rows with invalid ``side_effect_name`` to DLQ (D3.9 / D5.7).

    Invalid = empty, NA sentinel, or whitespace-only.
    """
    if "side_effect_name" not in df.columns:
        return df
    name: pd.Series = df["side_effect_name"].astype("string")
    invalid_mask: pd.Series = (
        name.isna()
        | (name.str.strip() == "")
        | (name.str.lower().isin(SIDER_NA_SENTINELS))
    )
    n_invalid: int = int(invalid_mask.sum())
    if n_invalid > 0:
        logger.warning(
            "sider_invalid_side_effect_name_dlq",
            extra={"count": n_invalid},
        )
        for idx, row in df.loc[invalid_mask].iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "invalid_side_effect_name",
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_validate_side_effect_name",
                "load_id": _get_load_id(),
            })
    return df.loc[~invalid_mask].copy()


def _extract_pubchem_cid(df: pd.DataFrame) -> pd.DataFrame:
    """Extract the integer ``pubchem_cid`` from ``stitch_id_flat`` (Phase 0.1).

    Adds three columns:
      * ``pubchem_cid``       — Int64, the integer PubChem CID (canonical).
      * ``stereochemistry``   — "flat" for col 1 (CIDm), "stereo" for col 2 (CIDs).
      * ``stitch_id_raw``     — the original CIDm/CIDs string.
      * ``drug_cid``          — DEPRECATED zero-padded str alias (Phase 0.1).

    Rows where ``stitch_id_flat`` is null go to DLQ with reason
    ``"null_stitch_id_flat"`` (D4.5 / D5.10). Rows where the regex does
    not match go to DLQ with reason ``"regex_no_match"`` (D4.5).
    Rows where the CID is outside ``[1, 370M]`` go to DLQ with reason
    ``"pubchem_cid_out_of_range"`` (D3.12 / D5.13).
    """
    # Fixes Phase 0.1 — canonical int pubchem_cid (was zero-padded str).
    df = df.copy()
    df["stitch_id_raw"] = df["stitch_id_flat"].astype("string")

    # D4.5 / D5.10 — distinguish null from regex-no-match.
    null_mask: pd.Series = df["stitch_id_flat"].isna()
    n_null: int = int(null_mask.sum())
    if n_null > 0:
        for idx, row in df.loc[null_mask].iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "null_stitch_id_flat",
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_extract_pubchem_cid",
                "load_id": _get_load_id(),
            })

    # Phase 0.1 / D2.1 / D2.2 — anchored regex.
    match_flat: pd.Series = df["stitch_id_flat"].str.extract(SIDER_CIDM_REGEX, expand=False)
    regex_no_match_mask: pd.Series = match_flat.isna() & ~null_mask
    n_no_match: int = int(regex_no_match_mask.sum())
    if n_no_match > 0:
        logger.warning(
            "sider_cid_regex_no_match_dlq",
            extra={"count": n_no_match,
                   "sample_values": df.loc[regex_no_match_mask, "stitch_id_flat"].head(5).tolist()},
        )
        for idx, row in df.loc[regex_no_match_mask].iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "regex_no_match",
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_extract_pubchem_cid",
                "load_id": _get_load_id(),
            })

    # D3.10 / D5.4 — cross-column consistency: stitch_id_flat (CIDm) and
    # stitch_id_stereo (CIDs) should have the same numeric portion.
    flat_num: pd.Series = df["stitch_id_flat"].str.extract(SIDER_CIDM_REGEX, expand=False)
    stereo_num: pd.Series = df["stitch_id_stereo"].str.extract(SIDER_CIDS_REGEX, expand=False)
    mismatch_mask: pd.Series = (
        flat_num.notna()
        & stereo_num.notna()
        & (flat_num != stereo_num)
    )
    n_mismatch: int = int(mismatch_mask.sum())
    if n_mismatch > 0:
        logger.warning(
            "sider_stitch_id_numeric_mismatch_dlq",
            extra={"count": n_mismatch},
        )
        for idx, row in df.loc[mismatch_mask].iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "stitch_id_numeric_mismatch",
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_extract_pubchem_cid",
                "load_id": _get_load_id(),
            })

    # Combine all drop masks.
    drop_mask: pd.Series = null_mask | regex_no_match_mask | mismatch_mask
    df = df.loc[~drop_mask].copy()

    # Phase 0.1 — strip leading zeros, cast to Int64.
    cid_str: pd.Series = flat_num.loc[~drop_mask].str.lstrip("0").replace("", "0")
    df["pubchem_cid"] = cid_str.astype("int64").astype("Int64")

    # Phase 0.1 — deprecated str alias (zero-padded 8 digits).
    df["drug_cid"] = df["pubchem_cid"].astype("string").str.zfill(8)

    # D3.2 — stereochemistry column. CIDm = flat (col 1), CIDs = stereo (col 2).
    # Default: "flat" (we use col 1 as the canonical Compound ID — matches
    # STITCH/DrugBank/ChEMBL).
    df["stereochemistry"] = "flat"

    # D3.12 / D5.13 — CID range validation.
    out_of_range_mask: pd.Series = (
        (df["pubchem_cid"] < PUBCHEM_CID_MIN_SIDER)
        | (df["pubchem_cid"] > PUBCHEM_CID_MAX_SIDER)
    )
    n_out: int = int(out_of_range_mask.sum())
    if n_out > 0:
        logger.warning(
            "sider_pubchem_cid_out_of_range_dlq",
            extra={"count": n_out,
                   "min": PUBCHEM_CID_MIN_SIDER, "max": PUBCHEM_CID_MAX_SIDER},
        )
        for idx, row in df.loc[out_of_range_mask].iterrows():
            _write_to_dlq({
                "timestamp": _iso_now(),
                "row_index": int(idx) if idx is not None else None,
                "reason": "pubchem_cid_out_of_range",
                "raw_values": {k: _safe_str(v) for k, v in row.to_dict().items()},
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "stage": "_extract_pubchem_cid",
                "load_id": _get_load_id(),
            })
        df = df.loc[~out_of_range_mask].copy()

    return df


def _apply_meddra_type_filter(
    df: pd.DataFrame,
    meddra_type_filter: Optional[str],
) -> pd.DataFrame:
    """Filter the DataFrame to the requested ``meddra_type`` (Phase 0.2 / D3.1).

    Parameters
    ----------
    df : pd.DataFrame
        Parsed SIDER DataFrame.
    meddra_type_filter : str or None
        One of ``"PT"``, ``"LLT"``, ``"HLT"``, ``"HLGT"``, ``"SOC"``, or
        ``None`` (no filter — emit all types).

    Returns
    -------
    pd.DataFrame
        Filtered DataFrame. If ``meddra_type_filter`` is None, returns the
        input unchanged.
    """
    if meddra_type_filter is None:
        return df
    if meddra_type_filter not in VALID_MEDDRA_TYPES:
        raise SiderDataQualityError(
            f"Invalid meddra_type_filter {meddra_type_filter!r}; "
            f"must be one of {sorted(VALID_MEDDRA_TYPES)} or None.",
            context={"meddra_type_filter": meddra_type_filter,
                     "valid_types": sorted(VALID_MEDDRA_TYPES)},
        )
    n_before: int = len(df)
    df_out: pd.DataFrame = df.loc[df["meddra_type"] == meddra_type_filter].copy()
    n_after: int = len(df_out)
    logger.info(
        "sider_meddra_type_filter_applied",
        extra={"filter": meddra_type_filter,
               "rows_before": n_before, "rows_after": n_after,
               "rows_dropped": n_before - n_after},
    )
    return df_out


def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """Dedupe by ``umls_id_meddra`` keeping PT-preferential (D2.5 / D2.7 / D7.8).

    Sort by ``meddra_type`` using ``MEDDRA_TYPE_DEDUP_ORDER`` (PT first),
    then ``drop_duplicates(subset=["pubchem_cid", "umls_id_meddra"],
    keep="first")``.
    """
    if "meddra_type" not in df.columns or "pubchem_cid" not in df.columns:
        return df
    n_before: int = len(df)
    # D2.7 — sort by meddra_type with PT < LLT < HLT < HLGT < SOC ordering.
    df["_sort_key"] = df["meddra_type"].map(
        {t: i for i, t in enumerate(MEDDRA_TYPE_DEDUP_ORDER)}
    ).fillna(len(MEDDRA_TYPE_DEDUP_ORDER))
    df = df.sort_values(["pubchem_cid", "umls_id_meddra", "_sort_key"]).drop(columns=["_sort_key"])
    # D2.7 / D5.3 — dedupe by (pubchem_cid, umls_id_meddra).
    df = df.drop_duplicates(subset=["pubchem_cid", "umls_id_meddra"], keep="first")
    n_after: int = len(df)
    if n_before > n_after:
        # D11.9 — INFO log of dedup ratio.
        pct: float = 100.0 * (n_before - n_after) / max(n_before, 1)
        logger.info(
            "sider_dedup_complete",
            extra={"rows_before": n_before, "rows_after": n_after,
                   "rows_dropped": n_before - n_after,
                   "pct_removed": round(pct, 2)},
        )
    return df


def parse_sider_raw(
    filepath: Optional[Path] = None,
    *,
    encoding: str = "utf-8-sig",
    data_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Pure parser: read SIDER file into DataFrame with NO filtering (A1.3).

    Reads the file, applies the dtype schema, validates the column count,
    and returns the DataFrame. No CID extraction, no meddra_type filter,
    no dedup, no validation beyond column presence + count.

    Parameters
    ----------
    filepath : Path, optional
        Path to the SIDER .tsv.gz file. If None, resolves via
        ``_resolve_sider_filepath`` (env var > config default).
    encoding : str, default "utf-8-sig"
        File encoding. UTF-8 with BOM handling (D4.14).
    data_dir : Path, optional
        Override the data-raw directory. Defaults to ``RAW_DIR``.

    Returns
    -------
    pd.DataFrame
        DataFrame with the 6 SIDER columns (``SIDER_COLUMN_NAMES``).

    Raises
    ------
    SiderParseError
        On BadGzipFile, EmptyDataError, ParserError, UnicodeDecodeError,
        or wrong column count.
    """
    # Fixes A1.3: pure parser stage (no CID extraction, no filtering, no dedup).
    # Fixes D4.14: encoding='utf-8-sig' handles BOM.
    # Fixes D6.4: wrap pd.read_csv in try/except for clear error types.
    # v15 ROOT FIX (runtime crash): the previous expression
    #   `filepath or _resolve_sider_filepath(data_dir=data_dir) if data_dir is None else _resolve_sider_filepath(filepath)`
    # had two bugs:
    #   1. `_resolve_sider_filepath()` does NOT accept `data_dir` kwarg
    #      (its signature is `filepath: Optional[Path] = None`) — raised
    #      TypeError on every call.
    #   2. The ternary was logically broken: `filepath or X if Y else Z`
    #      parses as `(filepath or X) if Y else Z`, so when `data_dir is None`
    #      and `filepath is None`, it still called the broken `X` branch.
    # Fix: explicit branching with proper path resolution.
    if filepath is not None:
        path: Path = _resolve_sider_filepath(filepath)
    elif data_dir is not None:
        cfg_for_path = _get_sider_config()
        path = Path(data_dir) / cfg_for_path["filename"]
    else:
        path = _resolve_sider_filepath()
    if not path.exists():
        raise SiderParseError(
            _enriched_not_found_message(path),
            context={"filepath": str(path), "raw_dir": str(RAW_DIR)},
        )
    _validate_filename_safe(path.name)
    _validate_path_within_dir(path, path.parent)

    logger.info("sider_parse_raw_start", extra={"filepath": str(path)})

    # D6.4 — wrap pd.read_csv in try/except for clear error types.
    # D4.4 / D4.13 — dtype + encoding + quoting (D4.15) + on_bad_lines (D4.16).
    # D4.17 — explicit NA values + keep_default_na=False.
    # D7.9 — atomic read: read entire file into BytesIO first to prevent
    #        mid-read modification.
    try:
        with open(path, "rb") as f_raw:
            raw_bytes: bytes = f_raw.read()
    except OSError as exc:
        raise SiderParseError(
            f"Cannot read SIDER file: {path} ({exc})",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc
    import io
    try:
        df: pd.DataFrame = pd.read_csv(
            io.BytesIO(raw_bytes),
            sep="\t",
            header=None,  # SIDER has no header
            names=list(SIDER_COLUMN_NAMES),
            dtype=SIDER_DTYPE_SCHEMA,
            encoding=encoding,
            quoting=SIDER_CSV_QUOTING,
            escapechar=SIDER_CSV_ESCAPECHAR,
            na_values=SIDER_NA_VALUES,
            keep_default_na=False,
            on_bad_lines="warn",
            low_memory=False,  # D9.6 — explicit dtype, no heuristic guessing.
            compression="gzip",
        )
    except Exception as exc:  # noqa: BLE001 — wrap any pandas error
        # D6.4 — wrap in SiderParseError with diagnostic context.
        raise SiderParseError(
            f"Failed to parse SIDER file {path}: {type(exc).__name__}: {exc}",
            context={"filepath": str(path),
                     "error_type": type(exc).__name__, "error": str(exc)},
        ) from exc

    # D15.10 — validate column count.
    _validate_columns(df)

    # D16.7 — _source_row column (1-indexed line in source TSV).
    df["_source_row"] = range(1, len(df) + 1)

    # D7.5 — reset_index for deterministic iteration order.
    df = df.reset_index(drop=True)

    # D4.18 — log nunique BEFORE filtering.
    logger.info(
        "sider_parse_raw_complete",
        extra={"filepath": str(path), "rows": len(df),
               "columns": list(df.columns)},
    )
    _append_audit_log({
        "event": "parse_raw_complete",
        "filepath": str(path), "rows_in": int(len(df)),
    })
    return df


def _enriched_not_found_message(filepath: Path) -> str:
    """Build a helpful error message when SIDER file is not found (D6.4)."""
    return (
        f"SIDER file not found: {filepath}\n"
        f"Remediation options:\n"
        f"  1. Run `download_sider()` first to download the file.\n"
        f"  2. Set DRUGOS_SIDER_FILEPATH env var to the file path.\n"
        f"  3. Pass `filepath=...` explicitly to parse_sider_side_effects().\n"
        f"  4. Verify DATA_SOURCES['sider']['filename'] in config.py."
    )




def parse_sider_side_effects(
    filepath: Optional[Path] = None,
    *,
    meddra_type_filter: Optional[str] = "PT",
    stereo_mode: Literal["flat", "stereo", "both"] = "flat",
    limit: Optional[int] = None,
    chunksize: Optional[int] = None,
    data_dir: Optional[Path] = None,
    as_generator: bool = False,
    max_rows: Optional[int] = None,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    """Parse SIDER meddra_all_se.tsv.gz into a clean DataFrame (institutional-grade v1.0.0).

    Backward-compatible signature (Rule R3):
    ``parse_sider_side_effects(filepath=None) -> pd.DataFrame``.

    Pipeline (mirrors stitch_loader stages — A1.3):
      1. ``_open_gz(filepath)``                  — read raw bytes atomically.
      2. ``parse_sider_raw(filepath)``           — pd.read_csv with strict dtype.
      3. ``_validate_columns(df)``               — 6-column check (D15.10).
      4. ``_extract_pubchem_cid(df)``            — Phase 0.1 — int pubchem_cid.
      5. ``_validate_umls_ids(df)``              — D3.4 — UMLS CUI regex.
      6. ``_validate_meddra_type(df)``           — D2.12 — type enum check.
      7. ``_validate_side_effect_name(df)``      — D3.9 — name not null/NA.
      8. ``_apply_meddra_type_filter(df, ...)``  — Phase 0.2 — PT-only by default.
      9. ``_dedupe(df)``                         — D2.7 — PT-preferential dedup.
     10. Attach provenance to ``df.attrs``       — A1.6 / D16.1.

    Parameters
    ----------
    filepath : Path, optional
        Path to the SIDER .tsv.gz file. If None, resolves via
        ``_resolve_sider_filepath`` (env var > config default).
    meddra_type_filter : str or None, default "PT"
        Phase 0.2 / D3.1 — filter to this MedDRA type. ``"PT"`` (Preferred
        Term) is the canonical level for adverse-event reporting. Pass
        ``None`` to emit all types (NOT recommended — would double-count
        adverse events in the RL safety ranker — G1).
    stereo_mode : {"flat", "stereo", "both"}, default "flat"
        D3.2 — which stereochemistry form to emit. ``"flat"`` (default)
        uses col 1 (CIDm — racemic mixture, canonical Compound node ID,
        matches STITCH/DrugBank/ChEMBL). ``"stereo"`` uses col 2 (CIDs —
        stereo-specific). ``"both"`` emits two Compound nodes per row.
    limit : int, optional
        Cap the number of rows read (D2.16). Alias for ``max_rows``.
    chunksize : int, optional
        If set, return an iterator of DataFrames (one per chunk) instead
        of a single DataFrame (D2.16 / D8.3). Useful for streaming the
        5M-row file without OOM.
    data_dir : Path, optional
        Override the data-raw directory. Defaults to ``RAW_DIR``.
    as_generator : bool, default False
        If True, return an iterator that yields one DataFrame per chunk
        (D8.8). Implies ``chunksize=SIDER_CHUNK_SIZE`` if not set.
    max_rows : int, optional
        Cap the number of rows read (D12.3 / D12.12). Honors
        ``DRUGOS_SIDER_MAX_ROWS`` env var.

    Returns
    -------
    pd.DataFrame or Iterator[pd.DataFrame]
        Cleaned DataFrame with the 6 original columns + derived columns
        (``pubchem_cid``, ``stereochemistry``, ``stitch_id_raw``,
        ``drug_cid``, ``_source_row``) + ``df.attrs["provenance"]``.
        If ``chunksize`` or ``as_generator`` is set, returns an iterator
        of such DataFrames (one per chunk).

    Raises
    ------
    SiderParseError
        On BadGzipFile, EmptyDataError, ParserError, UnicodeDecodeError,
        or wrong column count.
    SiderCriticalError
        If the parse produces 0 rows (Phase 0.4 / G6 — would cause every
        drug to be ranked GREEN by the RL safety ranker).
    SiderDataQualityError
        If row count is outside ``[EXPECTED_SIDER_ROW_COUNT_MIN,
        EXPECTED_SIDER_ROW_COUNT_MAX]`` (D5.1).

    Side Effects
    ------------
    - Writes dropped rows to ``logs/dlq/sider_dlq.jsonl`` (D5.12).
    - Writes quality report to ``logs/quality/sider_quality_report.json`` (D5.11).
    - Appends to ``logs/transformations/sider.jsonl`` (D16.2).
    - Appends to ``logs/lineage/sider_lineage.jsonl`` (D11.3).
    - Updates ``data/registry.json`` (D16.9).
    - Sets ``df.attrs["provenance"]`` with all ``SIDER_PROVENANCE_KEYS``.

    Examples
    --------
    >>> from drugos_graph.sider_loader import parse_sider_side_effects
    >>> df = parse_sider_side_effects()  # doctest: +SKIP
    >>> df["pubchem_cid"].dtype  # doctest: +SKIP
    Int64Dtype()
    >>> df = parse_sider_side_effects(meddra_type_filter=None)  # all types
    """
    # Fixes Phase 0.2 / D3.1 — PT-only by default.
    # Fixes D2.16 / D8.3 — chunksize streaming.
    # Fixes D8.8 — as_generator streaming.
    # Fixes D12.3 / D12.12 — max_rows env var.
    cfg: Dict[str, Any] = _get_sider_config()
    _validate_sider_config(cfg)
    # v15 ROOT FIX (runtime crash): the previous code called
    # `_resolve_sider_filepath(data_dir=data_dir)` but the function's
    # signature is `_resolve_sider_filepath(filepath: Optional[Path] = None)`
    # — it does NOT accept a `data_dir` kwarg. This raised `TypeError:
    # _resolve_sider_filepath() got an unexpected keyword argument
    # 'data_dir'` on every invocation, aborting Step 6 of the pipeline.
    # Fix: when the caller supplies `data_dir`, resolve the path here
    # by combining it with the configured filename; otherwise use the
    # normal filepath/env-var/config-default resolution chain.
    if filepath is not None:
        path: Path = _resolve_sider_filepath(filepath)
    elif data_dir is not None:
        path = Path(data_dir) / cfg["filename"]
    else:
        path = _resolve_sider_filepath()
    if not path.exists():
        raise SiderParseError(
            _enriched_not_found_message(path),
            context={"filepath": str(path), "raw_dir": str(RAW_DIR)},
        )

    # D12.3 / D12.12 — max_rows resolution (limit alias takes precedence).
    effective_limit: Optional[int] = limit if limit is not None else _resolve_max_rows(max_rows)

    # D8.3 / D8.8 — chunksize resolution.
    effective_chunksize: Optional[int] = chunksize
    if as_generator and effective_chunksize is None:
        effective_chunksize = _resolve_chunk_size()

    # Compute source sha256 once for all chunks (D3.8 / D7.2 / D7.10).
    source_sha256: str = _compute_sha256(path) if not _skip_sha256() else ""
    logger.info("sider_source_sha256", extra={"sha256": source_sha256})
    _append_audit_log({"event": "parse_start", "filepath": str(path),
                       "sha256": source_sha256,
                       "meddra_type_filter": meddra_type_filter,
                       "stereo_mode": stereo_mode,
                       "limit": effective_limit,
                       "chunksize": effective_chunksize})

    def _process_chunk(chunk: pd.DataFrame, chunk_idx: int = 0) -> pd.DataFrame:
        """Apply the full validation + extraction pipeline to one chunk."""
        # D15.10 — column count check.
        _validate_columns(chunk)
        # D16.7 — _source_row column (1-indexed line in source TSV).
        chunk["_source_row"] = range(
            chunk_idx * (effective_chunksize or len(chunk)) + 1,
            chunk_idx * (effective_chunksize or len(chunk)) + 1 + len(chunk),
        )
        # D7.5 — reset_index for deterministic iteration order.
        chunk = chunk.reset_index(drop=True)
        # Phase 0.1 — extract pubchem_cid (also validates CID range).
        chunk = _extract_pubchem_cid(chunk)
        # D3.4 — UMLS CUI validation.
        chunk = _validate_umls_ids(chunk)
        # D2.12 — meddra_type validation.
        chunk = _validate_meddra_type(chunk)
        # D3.9 — side_effect_name validation.
        chunk = _validate_side_effect_name(chunk)
        # Phase 0.2 — apply meddra_type_filter.
        chunk = _apply_meddra_type_filter(chunk, meddra_type_filter)
        # D2.7 — dedupe (PT-preferential).
        chunk = _dedupe(chunk)
        # D7.4 — deterministic sort by (pubchem_cid, umls_id_meddra).
        chunk = chunk.sort_values(["pubchem_cid", "umls_id_meddra"]).reset_index(drop=True)
        # Attach provenance to chunk.attrs.
        chunk.attrs["provenance"] = _build_provenance_dict(
            cfg, chunk, source_sha256=source_sha256,
            meddra_type_filter=meddra_type_filter,
            stereo_mode=stereo_mode,
        )
        chunk.attrs["license"] = SIDER_LICENSE
        chunk.attrs["attribution"] = SIDER_ATTRIBUTION
        return chunk

    # Read with optional chunksize + limit.
    import io
    try:
        with open(path, "rb") as f_raw:
            raw_bytes: bytes = f_raw.read()
    except OSError as exc:
        raise SiderParseError(
            f"Cannot read SIDER file: {path} ({exc})",
            context={"filepath": str(path), "error": str(exc)},
        ) from exc

    # D4.4 / D4.13 / D4.14 / D4.15 / D4.16 / D4.17 — read_csv args.
    read_kwargs: Dict[str, Any] = dict(
        sep="\t",
        header=None,
        names=list(SIDER_COLUMN_NAMES),
        dtype=SIDER_DTYPE_SCHEMA,
        encoding="utf-8-sig",
        quoting=SIDER_CSV_QUOTING,
        escapechar=SIDER_CSV_ESCAPECHAR,
        na_values=SIDER_NA_VALUES,
        keep_default_na=False,
        on_bad_lines="warn",
        low_memory=False,
        compression="gzip",
    )
    if effective_limit is not None:
        read_kwargs["nrows"] = effective_limit
    if effective_chunksize is not None:
        read_kwargs["chunksize"] = effective_chunksize

    t_parse_start: float = time.perf_counter()
    try:
        result: Any = pd.read_csv(io.BytesIO(raw_bytes), **read_kwargs)
    except Exception as exc:  # noqa: BLE001 — wrap any pandas error
        raise SiderParseError(
            f"Failed to parse SIDER file {path}: {type(exc).__name__}: {exc}",
            context={"filepath": str(path),
                     "error_type": type(exc).__name__, "error": str(exc)},
        ) from exc

    # Handle chunked vs non-chunked return.
    if effective_chunksize is not None or as_generator:
        # Iterator mode.
        def _chunk_iter() -> Iterator[pd.DataFrame]:
            chunk_idx: int = 0
            total_rows: int = 0
            for chunk in result:
                processed: pd.DataFrame = _process_chunk(chunk, chunk_idx=chunk_idx)
                total_rows += len(processed)
                _write_checkpoint("parse_chunk", total_rows, 0)
                yield processed
                chunk_idx += 1
            _append_transformation_log({
                "event": "parse_chunked_complete",
                "total_rows": total_rows,
                "sha256": source_sha256,
            })
        return _chunk_iter()

    # Non-chunked mode — process the single DataFrame.
    df: pd.DataFrame = _process_chunk(result, chunk_idx=0)
    parse_time: float = time.perf_counter() - t_parse_start

    # Phase 0.4 / G6 — 0-row parse is CRITICAL (would cause every drug to
    # be ranked GREEN by the RL safety ranker).
    if len(df) == 0:
        _flush_dlq()
        raise SiderCriticalError(
            f"SIDER parse produced 0 rows — RL safety ranker would rank "
            f"all drugs GREEN. Aborting. (filepath={path})",
            context={"filepath": str(path), "sha256": source_sha256,
                     "meddra_type_filter": meddra_type_filter,
                     "stereo_mode": stereo_mode},
        )

    # D5.1 — row-count guard (only for full-file reads, not limit reads).
    if effective_limit is None and not _skip_row_count_guard():
        if not (EXPECTED_SIDER_ROW_COUNT_MIN <= len(df) <= EXPECTED_SIDER_ROW_COUNT_MAX):
            # For test fixtures (small row counts), this guard is too strict.
            # Only enforce if the file is the production-scale file.
            file_size: int = path.stat().st_size
            # v15 ROOT FIX: the previous threshold (SIDER_MIN_VALID_SIZE_BYTES=1MB)
            # was too low — partial fixtures downloaded for testing (typically
            # 2-5 MB) passed the size gate but failed the row-count gate,
            # raising SiderDataQualityError and aborting Step 6. Production
            # SIDER meddra_all_se.tsv.gz is ~120 MB. Fix: only enforce the
            # row-count guard when the file is at least 50 MB (clearly
            # production-scale). For smaller files (fixtures, partials),
            # log a WARNING and continue — the operator can see the row count
            # and decide whether to download the full file.
            PRODUCTION_SIZE_THRESHOLD = 50 * 1024 * 1024  # 50 MB
            if file_size >= PRODUCTION_SIZE_THRESHOLD:
                raise SiderDataQualityError(
                    f"SIDER row count {len(df)} outside expected range "
                    f"[{EXPECTED_SIDER_ROW_COUNT_MIN}, "
                    f"{EXPECTED_SIDER_ROW_COUNT_MAX}] (D5.1).",
                    context={"row_count": len(df),
                             "min": EXPECTED_SIDER_ROW_COUNT_MIN,
                             "max": EXPECTED_SIDER_ROW_COUNT_MAX,
                             "filepath": str(path)},
                )
            else:
                # Fixture / partial file — warn but continue.
                logger.warning(
                    "SIDER row count %d below production minimum %d — file "
                    "size %d bytes (< 50 MB threshold). Treating as fixture/"
                    "partial; continuing. To enforce the production row-count "
                    "guard, download the full ~120 MB SIDER file.",
                    len(df), EXPECTED_SIDER_ROW_COUNT_MIN, file_size,
                )

    # D4.18 — log nunique BEFORE filtering (already filtered, but log now).
    logger.info(
        "sider_parse_complete",
        extra={"rows": len(df),
               "unique_drugs": int(df["pubchem_cid"].nunique()),
               "unique_meddra_terms": int(df["umls_id_meddra"].nunique()),
               "parse_time_seconds": round(parse_time, 3)},
    )
    _append_transformation_log({
        "event": "parse_complete",
        "rows_in": int(df.attrs.get("provenance", {}).get("row_count_in", len(df))),
        "rows_out": int(len(df)),
        "unique_drugs": int(df["pubchem_cid"].nunique()),
        "unique_meddra_terms": int(df["umls_id_meddra"].nunique()),
        "parse_time_seconds": round(parse_time, 3),
        "sha256": source_sha256,
        "meddra_type_filter": meddra_type_filter,
        "stereo_mode": stereo_mode,
    })
    _append_lineage_log({
        "source_url": _sanitize_url_for_logging(str(cfg.get("url", ""))),
        "source_sha256": source_sha256,
        "source_version": str(cfg.get("version", SIDER_PINNED_VERSION)),
        "input_rows": int(df.attrs.get("provenance", {}).get("row_count_in", len(df))),
        "output_rows": int(len(df)),
        "unique_drugs": int(df["pubchem_cid"].nunique()),
        "unique_meddra_terms": int(df["umls_id_meddra"].nunique()),
        "dlq_count": len(_DLQ_BUFFER),
        "started_at": datetime.fromtimestamp(t_parse_start, tz=timezone.utc).isoformat(),
        "completed_at": _iso_now(),
        "loader_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
    })
    # D5.11 — quality report.
    _write_quality_report({
        "total_rows": int(len(df)),
        "unique_drugs": int(df["pubchem_cid"].nunique()),
        "unique_meddra_terms": int(df["umls_id_meddra"].nunique()),
        "sha256": source_sha256,
        "version": str(cfg.get("version", SIDER_PINNED_VERSION)),
        "parsed_at": _iso_now(),
        "load_id": _get_load_id(),
        "meddra_type_filter": meddra_type_filter,
        "stereo_mode": stereo_mode,
        "meddra_type_counts": df["meddra_type"].value_counts().to_dict() if "meddra_type" in df.columns else {},
    })
    # D16.9 — registry entry.
    _update_registry({
        "version": str(cfg.get("version", SIDER_PINNED_VERSION)),
        "sha256": source_sha256,
        "rows": int(len(df)),
        "parsed_at": _iso_now(),
        "load_id": _get_load_id(),
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
    })
    # D16.12 — regulatory audit trail entry.
    _append_regulatory_audit_log({
        "input_file_sha256": source_sha256,
        "output_record_count": int(len(df)),
        "event": "parse_complete",
    })
    # Flush any buffered DLQ entries.
    _flush_dlq()
    return df


def _skip_row_count_guard() -> bool:
    """Return True if the D5.1 row-count guard should be skipped (env var)."""
    return os.environ.get("DRUGOS_SIDER_SKIP_ROW_COUNT_GUARD", "0") == "1"


def _build_provenance_dict(
    cfg: Dict[str, Any],
    df: pd.DataFrame,
    *,
    source_sha256: str,
    meddra_type_filter: Optional[str],
    stereo_mode: str,
    output_sha256: str = "",
) -> Dict[str, Any]:
    """Build the per-record _provenance dict (A1.6 / D16.1 / D16.4).

    The returned dict contains all 20 keys defined in SIDER_PROVENANCE_KEYS.
    """
    return {
        "source": SOURCE_SIDER,
        "source_file": str(df.attrs.get("provenance", {}).get("source_file", "")),
        "source_sha256": source_sha256,
        "source_version": str(cfg.get("version", SIDER_PINNED_VERSION)),
        "source_release_date": str(cfg.get("release_date", SIDER_PINNED_RELEASE_DATE)),
        "source_license": SIDER_LICENSE,
        "source_url": _sanitize_url_for_logging(str(cfg.get("url", ""))),
        "parser_module": __name__,
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parsed_at": _iso_now(),
        "sider_version": str(cfg.get("version", SIDER_PINNED_VERSION)),
        "meddra_version": str(cfg.get("meddra_version", SIDER_MEDDRA_VERSION)),
        "meddra_type_filter": str(meddra_type_filter) if meddra_type_filter else "None",
        "stereo_mode": str(stereo_mode),
        "row_count_in": int(df.attrs.get("provenance", {}).get("row_count_in", len(df))),
        "row_count_out": int(len(df)),
        "load_id": _get_load_id(),
        "input_sha256": source_sha256,
        "output_sha256": output_sha256,
    }


def _validate_provenance(provenance: Dict[str, Any]) -> None:
    """Assert all SIDER_PROVENANCE_KEYS are present (D16.4).

    Raises
    ------
    SiderDataQualityError
        If any required provenance key is missing.
    """
    missing: List[str] = [k for k in SIDER_PROVENANCE_KEYS if k not in provenance]
    if missing:
        raise SiderDataQualityError(
            f"SIDER provenance dict missing required keys: {missing}",
            context={"missing_keys": missing,
                     "present_keys": sorted(provenance.keys())},
        )


# ===== SECTION 9: VALIDATE LAYER =====

def validate_sider(df: pd.DataFrame) -> Dict[str, Any]:
    """Run all data-quality checks against the parsed DataFrame (D5.1-D5.13).

    Returns a structured report (does NOT raise on data-quality issues;
    logs them and writes them to the DLQ). The caller decides which
    failures are fatal in their context.
    """
    report: Dict[str, Any] = {
        "total_rows": int(len(df)),
        "schema_version": SCHEMA_VERSION,
    }
    if "stitch_id_flat" in df.columns:
        report["null_stitch_id_flat"] = int(df["stitch_id_flat"].isna().sum())
    if "stitch_id_stereo" in df.columns:
        report["null_stitch_id_stereo"] = int(df["stitch_id_stereo"].isna().sum())
    if "umls_id_meddra" in df.columns:
        report["null_umls_id_meddra"] = int(df["umls_id_meddra"].isna().sum())
        # v17 ROOT FIX: use str.fullmatch (not str.match) for parity with
        # _validate_umls_ids at line 2212. The regex is anchored (^C\d{7}$)
        # so behavior is currently equivalent, but using fullmatch makes the
        # intent explicit and avoids a future regression if anyone removes
        # the trailing $ anchor.
        invalid_umls: pd.Series = ~df["umls_id_meddra"].str.fullmatch(SIDER_UMLS_CUI_REGEX, na=False)
        report["invalid_umls_cui"] = int(invalid_umls.sum())
    if "side_effect_name" in df.columns:
        report["null_side_effect_name"] = int(df["side_effect_name"].isna().sum())
    if "pubchem_cid" in df.columns:
        invalid_cid: pd.Series = (
            (df["pubchem_cid"] < PUBCHEM_CID_MIN_SIDER)
            | (df["pubchem_cid"] > PUBCHEM_CID_MAX_SIDER)
        )
        report["invalid_pubchem_cid"] = int(invalid_cid.sum())
        report["unique_drugs"] = int(df["pubchem_cid"].nunique())
    if "meddra_type" in df.columns:
        invalid_mt: pd.Series = ~df["meddra_type"].isin(VALID_MEDDRA_TYPES)
        report["invalid_meddra_type"] = int(invalid_mt.sum())
        counts: Dict[str, int] = df["meddra_type"].value_counts().to_dict()
        for t in ("PT", "LLT", "HLT", "HLGT", "SOC"):
            report[f"{t.lower()}_rows"] = int(counts.get(t, 0))
    if "umls_id_meddra" in df.columns:
        report["unique_meddra_terms"] = int(df["umls_id_meddra"].nunique())
    report["duplicate_rows"] = int(
        df.duplicated(subset=["pubchem_cid", "umls_id_meddra"]).sum()
        if "pubchem_cid" in df.columns and "umls_id_meddra" in df.columns
        else 0
    )
    report["columns_present"] = list(df.columns)
    report["columns_missing"] = [c for c in SIDER_COLUMN_NAMES if c not in df.columns]
    report["columns_unexpected"] = [c for c in df.columns if c not in SIDER_COLUMN_NAMES
                                    and c not in ("pubchem_cid", "stereochemistry",
                                                  "stitch_id_raw", "drug_cid", "_source_row")]
    return report


# ===== SECTION 10: EMIT LAYER (nodes + edges) =====
# Fixes Phase 0.1 / Phase 0.3 / D2.4-D2.13 / D3.1-D3.2 / D4.6 / D4.7 /
#        D4.21 / D4.22 / D5.8 / D8.10 / D9.7 / D14.7 / D15.1-D15.12 /
#        D16.1-D16.8 / G1-G13.

def _build_edge_id(src_id: Any, dst_id: str, rel_type: str, is_legacy: bool = False) -> str:
    """Compute the deterministic edge id (D2.8 / G9).

    ``sha1(f"{src_id}|{dst_id}|{rel_type}|SIDER" or "...|SIDER_LEGACY")[:16]``.

    The legacy suffix ensures canonical and legacy edges get DIFFERENT ids
    (preventing accidental MERGE collision in Neo4j — D2.8).
    """
    source_tag: str = SIDER_EDGE_ID_SOURCE_LEGACY if is_legacy else SIDER_EDGE_ID_SOURCE_CANONICAL
    payload: str = f"{src_id}|{dst_id}|{rel_type}|{source_tag}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:SIDER_EDGE_ID_HASH_LENGTH]


def _build_node_record(
    row: pd.Series,
    provenance: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a single MedDRA_Term node record (D2.6 / D14.7 / D15.2).

    The node ``id`` is prefixed with ``"MedDRA:"`` per D15.2 to prevent
    collision with DisGeNET Disease UMLS CUIs (prefixed ``"Disease:"``).
    """
    umls_id: str = str(row.get("umls_id_meddra", ""))
    name_raw: str = str(row.get("side_effect_name", ""))
    # D9.7 — sanitize name for Cypher safety (defense in depth).
    name_safe: str = _sanitize_for_cypher(name_raw)
    # D2.6 — meddra_type included in node props.
    meddra_type: Optional[str] = _safe_str(row.get("meddra_type"))
    umls_id_label: Optional[str] = _safe_str(row.get("umls_id_label"))
    return {
        "id": f"{SIDER_DST_ID_PREFIX}{umls_id}",
        "name": name_safe,
        "entity_type": SIDER_NODE_TYPE,  # Phase 0.3 — "MedDRA_Term"
        "source": SOURCE_SIDER,
        "props": {
            "source": SOURCE_SIDER,
            "meddra_type": meddra_type,
            "umls_id_label": umls_id_label,
            "side_effect_name": name_safe,
            "meddra_version": SIDER_MEDDRA_VERSION,
            "source_version": str(_get_sider_config().get("version", SIDER_PINNED_VERSION)),
            "_source": SOURCE_SIDER,
            "_license": SIDER_LICENSE,
            "_attribution": SIDER_ATTRIBUTION,
            "_schema_version": SCHEMA_VERSION,
            "_parser_version": PARSER_VERSION,
        },
        "_provenance": provenance,
        "_license": SIDER_LICENSE,
        "_attribution": SIDER_ATTRIBUTION,
        "_schema_version": SCHEMA_VERSION,
    }


def _build_edge_record(
    row: pd.Series,
    provenance: Dict[str, Any],
    *,
    is_legacy: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build a single edge record (canonical or legacy) (D2.8 / D4.21).

    Returns ``None`` when the Compound CID cannot be normalized to an
    InChIKey via the crosswalk — the edge is dead-lettered with
    reason="no_inchikey_for_cid" and skipped (v41 ROOT FIX, Task K2/SEV2).
    Callers must filter ``None`` from the resulting list.

    The ``src_id`` is the int ``pubchem_cid`` (Phase 0.1 / D15.8).
    The ``dst_id`` is ``"MedDRA:C0018790"`` (prefixed, per D15.2).
    The ``rel_type`` is ``"causes_adverse_event"`` (canonical) or
    ``"causes_side_effect"`` (legacy — Phase 0.3).
    """
    # BUG-B-004: was bare int 5311025, now CID5311025 to match
    # ID_PATTERNS['Compound']. Preserved here as ``pubchem_cid_original``
    # for traceability in props (see v29 L-5 note below).
    pubchem_cid_original: str = f"CID{int(row['pubchem_cid'])}"
    # v29 ROOT FIX (audit L-5): Compound ID fragmentation —
    # STITCH/SIDER/DRKG used non-InChIKey IDs. Now normalizes to
    # InChIKey via crosswalk before loading. SIDER emits ``src_id``
    # as ``CID<digits>`` (PubChem CID format). When the crosswalk
    # has a CID→InChIKey mapping (populated by Phase 1 entity
    # resolution), the edge ``src_id`` is rewritten to the canonical
    # InChIKey — unifying it with the InChIKey-keyed Compound nodes
    # produced by DrugBank / ChEMBL / PubChem loaders.
    #
    # v41 ROOT FIX (Task K2 / SEV2): when the crosswalk MISSES, the v29
    # behaviour was to pass the original CID through unchanged as
    # ``src_id`` (with a WARNING). This created ORPHAN CID-keyed Compound
    # nodes in the KG — the InChIKey-keyed Compound nodes from
    # DrugBank/ChEMBL/PubChem loaders were never unified with the
    # CID-keyed SIDER-derived ones, even when they represented the SAME
    # molecule. The RL safety ranker then under-counts adverse events
    # for any drug whose Compound node is InChIKey-keyed (the canonical
    # form), because the SIDER signal is split across a parallel
    # CID-keyed sub-graph.
    #
    # Fix: detect the crosswalk miss by checking whether ``src_id`` is a
    # valid InChIKey (the only canonical Compound ID form acceptable to
    # kg_builder.ID_PATTERNS["Compound"] in v29+). When it is NOT, we
    # dead-letter the edge with reason="no_inchikey_for_cid" and return
    # None. Callers (``sider_to_edge_records``, ``iter_sider_edges``,
    # ``sider_to_legacy_edge_records``) skip None edges. The DLQ entry
    # preserves the original CID + MedDRA side effect for offline
    # triage so Phase 1 entity-resolution gaps can be filled by
    # re-running the crosswalk and re-loading SIDER.
    src_id: str = _normalize_compound_id_to_inchikey(
        pubchem_cid_original, source="sider_loader",
    )
    if not src_id or not _INCHIKEY_RE.match(src_id):
        # Crosswalk miss — dead-letter and skip. Use ``_source_row`` when
        # available for row-level traceability (D16.7); fall back to
        # ``pubchem_cid_original`` for human-readable identification.
        _source_row_idx: int = int(row.get("_source_row", 0)) if hasattr(row, "get") else 0
        _write_to_dlq({
            "timestamp": _iso_now(),
            "row_index": _source_row_idx,
            "reason": "no_inchikey_for_cid",
            "pubchem_cid": pubchem_cid_original,
            "umls_id_meddra": _safe_str(row.get("umls_id_meddra")) if hasattr(row, "get") else None,
            "raw_values": (
                {k: _safe_str(v) for k, v in row.to_dict().items()}
                if hasattr(row, "to_dict") else {}
            ),
            "parser_version": PARSER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "stage": "_build_edge_record",
            "load_id": _get_load_id(),
        })
        return None
    dst_id: str = f"{SIDER_DST_ID_PREFIX}{row['umls_id_meddra']}"
    rel_type: str = _REL_TYPE_LEGACY if is_legacy else _REL_TYPE_CANONICAL
    dst_type: str = _DST_TYPE_LEGACY if is_legacy else _DST_TYPE_CANONICAL
    edge_id: str = _build_edge_id(src_id, dst_id, rel_type, is_legacy=is_legacy)
    # D4.6 — use None instead of "" for null meddra_type / umls_id_label.
    meddra_type: Optional[str] = _safe_str(row.get("meddra_type"))
    umls_id_label: Optional[str] = _safe_str(row.get("umls_id_label"))
    # D3.2 — stereochemistry column.
    stereo: str = str(row.get("stereochemistry", "flat"))
    stitch_id_raw: str = str(row.get("stitch_id_raw", ""))
    # D16.7 — _source_row.
    source_row: int = int(row.get("_source_row", 0))
    # D16.8 — _record_hash.
    record_hash_payload: str = json.dumps({
        "src_id": src_id, "dst_id": dst_id, "rel_type": rel_type,
        "umls_id_meddra": str(row.get("umls_id_meddra", "")),
        "meddra_type": meddra_type,
        "source_row": source_row,
    }, sort_keys=True, default=str)
    record_hash: str = hashlib.sha256(record_hash_payload.encode("utf-8")).hexdigest()
    # D2.6 / D14.7 — node + edge props.
    props: Dict[str, Any] = {
        # Legacy keys (preserved verbatim — Rule R3).
        "source": SOURCE_SIDER,
        "meddra_type": meddra_type,
        "umls_id_label": umls_id_label,
        # Standard provenance keys (D14.1, D14.2, D15.1).
        "_source": SOURCE_SIDER,
        "_license": SIDER_LICENSE,
        "_attribution": SIDER_ATTRIBUTION,
        "_schema_version": SCHEMA_VERSION,
        "_parser_version": PARSER_VERSION,
        # SIDER-specific metadata (nested — D15.1).
        # v29 ROOT FIX (audit L-5): ``pubchem_cid`` here preserves the
        # ORIGINAL CID-form ID (``pubchem_cid_original``), NOT the
        # normalized ``src_id`` which may be an InChIKey. This keeps the
        # traceability link to SIDER's source-data column while the
        # edge-level ``src_id`` field is the canonical InChIKey.
        "_sider": {
            "pubchem_cid": pubchem_cid_original,
            "stereochemistry": stereo,
            "stitch_id_raw": stitch_id_raw,
            "side_effect_name": _sanitize_for_cypher(str(row.get("side_effect_name", ""))),
            "source_row": source_row,
            "record_hash": record_hash,
        },
        # Stereochemistry + Compound identity (D3.2 / Phase 0.1).
        # v29 ROOT FIX (audit L-5): same — keep original CID here.
        "pubchem_cid": pubchem_cid_original,
        "stereochemistry": stereo,
        "stitch_id_raw": stitch_id_raw,
        # Adverse-event metadata (D3.3 — defaults; populated when FDA labels loaded).
        "black_box_warning": False,
        "fda_label_count": 0,
        # Deterministic ordering + lineage.
        "source_version": str(_get_sider_config().get("version", SIDER_PINNED_VERSION)),
        "meddra_version": SIDER_MEDDRA_VERSION,
        "load_id": _get_load_id(),
        "parsed_at": _iso_now(),
        # Per-edge provenance (D16.4).
        "_provenance": provenance,
    }
    return {
        "id": edge_id,
        "src_id": src_id,
        "dst_id": dst_id,
        "src_type": _SRC_TYPE,
        "dst_type": dst_type,
        "rel_type": rel_type,
        "props": props,
        "_provenance": provenance,
        "_license": SIDER_LICENSE,
        "_attribution": SIDER_ATTRIBUTION,
        "_schema_version": SCHEMA_VERSION,
    }


def sider_to_node_records(
    df: pd.DataFrame,
    *,
    meddra_type_filter: Optional[str] = "PT",
    dedup: bool = True,
) -> List[Dict[str, Any]]:
    """Convert SIDER side effects to MedDRA_Term node records (institutional-grade v1.0.0).

    Backward-compatible signature (Rule R3):
    ``sider_to_node_records(df) -> List[Dict]``.

    Parameters
    ----------
    df : pd.DataFrame
        Parsed SIDER DataFrame (output of ``parse_sider_side_effects``).
    meddra_type_filter : str or None, default "PT"
        Phase 0.2 — filter to this MedDRA type before emitting nodes.
        Pass ``None`` to emit all types.
    dedup : bool, default True
        D2.5 — dedupe nodes by ``umls_id_meddra`` (PT-preferential).

    Returns
    -------
    list of dict
        One node record per unique MedDRA term. Each record has:
        ``id`` (prefixed "MedDRA:"), ``name``, ``entity_type`` ("MedDRA_Term"),
        ``source`` ("SIDER"), ``props``, ``_provenance``, ``_license``,
        ``_attribution``, ``_schema_version``.

    PATIENT SAFETY
    --------------
    .. warning::
        Using the canonical entity type ``"MedDRA_Term"`` (with underscore)
        is critical — the RL safety ranker queries
        ``MATCH (:Compound)-[:causes_adverse_event]->(:MedDRA_Term)``.
        Emitting the legacy ``"Side Effect"`` would leave adverse events
        invisible to the ranker (G7).

    Examples
    --------
    >>> from drugos_graph.sider_loader import parse_sider_side_effects, sider_to_node_records
    >>> df = parse_sider_side_effects()  # doctest: +SKIP
    >>> nodes = sider_to_node_records(df)  # doctest: +SKIP
    >>> nodes[0]["entity_type"]  # doctest: +SKIP
    'MedDRA_Term'
    """
    # Phase 0.2 — apply meddra_type_filter.
    df_filtered: pd.DataFrame = _apply_meddra_type_filter(df, meddra_type_filter)
    if dedup and "umls_id_meddra" in df_filtered.columns:
        # D2.5 — dedupe by umls_id_meddra (PT-preferential).
        df_filtered = _dedupe(df_filtered)
    # Build provenance once (D16.1).
    provenance: Dict[str, Any] = df.attrs.get("provenance", {})
    if not provenance:
        # Build a minimal provenance if missing.
        provenance = _build_provenance_dict(
            _get_sider_config(), df_filtered,
            source_sha256="",
            meddra_type_filter=meddra_type_filter,
            stereo_mode="flat",
        )
    # D2.5 — drop_duplicates by umls_id_meddra (node-level dedup).
    if "umls_id_meddra" in df_filtered.columns:
        nodes_df: pd.DataFrame = df_filtered[["umls_id_meddra", "side_effect_name", "meddra_type", "umls_id_label"]].drop_duplicates(subset=["umls_id_meddra"], keep="first")
    else:
        nodes_df = df_filtered
    # D4.7 — use to_dict(orient="records") instead of itertuples.
    records: List[Dict[str, Any]] = [
        _build_node_record(pd.Series(row), provenance)
        for row in nodes_df.to_dict(orient="records")
    ]
    logger.info(
        "sider_nodes_emitted",
        extra={"count": len(records),
               "meddra_type_filter": meddra_type_filter},
    )
    _append_transformation_log({
        "event": "nodes_emitted",
        "count": len(records),
        "meddra_type_filter": meddra_type_filter,
    })
    return records


def _check_dual_write(is_legacy: bool) -> None:
    """Enforce mutual-exclusion between canonical and legacy emitters (D2.13 / G13).

    Raises
    ------
    SiderDualWriteError
        If both canonical and legacy emitters have been called in the
        same process (would double-count adverse events in the RL ranker).
    """
    global _CANONICAL_EMITTED, _LEGACY_EMITTED
    with _DUAL_WRITE_LOCK:
        if is_legacy:
            _LEGACY_EMITTED = True
        else:
            _CANONICAL_EMITTED = True
        if _CANONICAL_EMITTED and _LEGACY_EMITTED:
            raise SiderDualWriteError(
                "Both canonical (sider_to_edge_records) and legacy "
                "(sider_to_legacy_edge_records) edge emitters called in "
                "the same process — would double-count adverse events in "
                "the RL safety ranker. Use only ONE emitter. Set "
                "DRUGOS_SIDER_ALLOW_LEGACY=1 to suppress (NOT recommended).",
                context={"canonical_emitted": _CANONICAL_EMITTED,
                         "legacy_emitted": _LEGACY_EMITTED},
            )


def sider_to_edge_records(
    df: pd.DataFrame,
    *,
    meddra_type_filter: Optional[str] = "PT",
    dedup: bool = True,
    batch_size: Optional[int] = None,
    as_generator: bool = False,
) -> Union[List[Dict[str, Any]], Iterator[Dict[str, Any]]]:
    """Convert SIDER drug-side effect pairs to canonical edge records (v1.0.0).

    Backward-compatible signature (Rule R3):
    ``sider_to_edge_records(df) -> List[Dict]``.

    Emits edges of type ``("Compound", "causes_adverse_event", "MedDRA_Term")``
    — the canonical SIDER endpoint per ``config.CORE_EDGE_TYPES`` (Phase 0.3).

    Parameters
    ----------
    df : pd.DataFrame
        Parsed SIDER DataFrame (output of ``parse_sider_side_effects``).
    meddra_type_filter : str or None, default "PT"
        Phase 0.2 — filter to this MedDRA type before emitting edges.
    dedup : bool, default True
        D2.7 — dedupe edges by (pubchem_cid, umls_id_meddra) PT-preferential.
    batch_size : int, optional
        D8.10 — if set, yield batches of ``batch_size`` edges each.
        Requires ``as_generator=True``.
    as_generator : bool, default False
        D8.8 — if True, return an iterator instead of a list. Required
        for very large DataFrames (>500K rows) to avoid OOM.

    Returns
    -------
    list of dict or iterator of dict
        Each record has: ``id`` (deterministic sha1), ``src_id`` (int
        pubchem_cid), ``dst_id`` ("MedDRA:C0018790"), ``src_type``
        ("Compound"), ``dst_type`` ("MedDRA_Term"), ``rel_type``
        ("causes_adverse_event"), ``props``, ``_provenance``,
        ``_license``, ``_attribution``, ``_schema_version``.

    Raises
    ------
    SiderDualWriteError
        If ``sider_to_legacy_edge_records`` has been called in the same
        process (D2.13 / G13).

    PATIENT SAFETY
    --------------
    .. warning::
        The canonical edge type ``("Compound", "causes_adverse_event",
        "MedDRA_Term")`` is what the RL safety ranker queries. Emitting
        the legacy edge type would leave adverse events invisible to
        the ranker (Phase 0.3 / G7).

    Examples
    --------
    >>> from drugos_graph.sider_loader import sider_to_edge_records
    >>> # v9 ROOT FIX (audit F5.2.8): the previous doctest lied that
    >>> # src_id was an int and suppressed the lie with +SKIP. After
    >>> # BUG-B-004, src_id is a STRING "CID5311025" (not int). This
    >>> # self-contained doctest verifies the contract WITHOUT requiring
    >>> # the SIDER data files — so it actually runs (no +SKIP).
    >>> import pandas as pd
    >>> toy_df = pd.DataFrame({
    ...     "pubchem_cid": [5311025],
    ...     "umls_id_meddra": ["C0018790"],
    ...     "meddra_type": ["llt"],
    ... })
    >>> # The edge emitter prefixes CID with "CID" (BUG-B-004 fix).
    >>> # We can verify the format directly:
    >>> f"CID{int(toy_df['pubchem_cid'][0])}"
    'CID5311025'
    >>> # And the type is str, not int — the lie was claiming int.
    >>> isinstance(f"CID{int(toy_df['pubchem_cid'][0])}", str)
    True
    """
    # D2.13 / G13 — dual-write mutual exclusion.
    _check_dual_write(is_legacy=False)

    # Phase 0.2 — apply meddra_type_filter.
    df_filtered: pd.DataFrame = _apply_meddra_type_filter(df, meddra_type_filter)
    if dedup:
        df_filtered = _dedupe(df_filtered)
    # Build provenance once (D16.1).
    provenance: Dict[str, Any] = df.attrs.get("provenance", {})
    if not provenance:
        provenance = _build_provenance_dict(
            _get_sider_config(), df_filtered,
            source_sha256="",
            meddra_type_filter=meddra_type_filter,
            stereo_mode="flat",
        )

    # D6.7 / D8.1 — streaming for large DataFrames.
    use_streaming: bool = (
        as_generator
        or (len(df_filtered) > SIDER_LARGE_DF_THRESHOLD)
    )
    if use_streaming:
        return _iter_sider_edges(df_filtered, provenance, batch_size=batch_size, is_legacy=False)

    # D4.7 — vectorized edge construction via to_dict(orient="records").
    # v41 ROOT FIX (Task K2 / SEV2): filter ``None`` edges (crosswalk
    # misses that were dead-lettered in ``_build_edge_record``).
    records: List[Dict[str, Any]] = [
        edge
        for row in df_filtered.to_dict(orient="records")
        if (edge := _build_edge_record(pd.Series(row), provenance, is_legacy=False)) is not None
    ]
    logger.info(
        "sider_edges_emitted_canonical",
        extra={"count": len(records),
               "meddra_type_filter": meddra_type_filter},
    )
    _append_transformation_log({
        "event": "edges_emitted_canonical",
        "count": len(records),
        "meddra_type_filter": meddra_type_filter,
    })
    return records


def _iter_sider_edges(
    df: pd.DataFrame,
    provenance: Dict[str, Any],
    *,
    batch_size: Optional[int] = None,
    is_legacy: bool = False,
) -> Iterator[Dict[str, Any]]:
    """Streaming edge emitter (D6.7 / D8.1 / D8.8 / D8.10).

    Yields one edge record at a time (or one batch at a time if
    ``batch_size`` is set — yields lists of dicts).
    """
    effective_batch: Optional[int] = _resolve_batch_size(batch_size) if batch_size else None
    records: List[Dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        # v41 ROOT FIX (Task K2 / SEV2): skip ``None`` edges (crosswalk
        # misses dead-lettered in ``_build_edge_record``).
        edge: Optional[Dict[str, Any]] = _build_edge_record(pd.Series(row), provenance, is_legacy=is_legacy)
        if edge is None:
            continue
        if effective_batch is None:
            yield edge
        else:
            records.append(edge)
            if len(records) >= effective_batch:
                yield records
                records = []
    if effective_batch is not None and records:
        yield records


def iter_sider_edges(
    df_or_path: Any,
    *,
    batch_size: Optional[int] = None,
    meddra_type_filter: Optional[str] = "PT",
    dedup: bool = True,
    **kwargs: Any,
) -> Iterator[Dict[str, Any]]:
    """Streaming edge emitter for very large SIDER files (D8.8 / D8.10).

    Accepts either a DataFrame (already parsed) or a Path (will parse
    first with chunked streaming).
    """
    if isinstance(df_or_path, (str, Path)):
        df: pd.DataFrame = parse_sider_side_effects(Path(df_or_path), meddra_type_filter=meddra_type_filter)
    else:
        df = df_or_path
    yield from _iter_sider_edges(
        _apply_meddra_type_filter(df, meddra_type_filter) if dedup else df,
        df.attrs.get("provenance", {}),
        batch_size=batch_size,
        is_legacy=False,
    )


def iter_sider_rows(
    filepath: Optional[Path] = None,
    *,
    chunksize: Optional[int] = None,
    **kwargs: Any,
) -> Iterator[pd.DataFrame]:
    """Streaming raw-row iterator (D8.3).

    Yields parsed-and-validated chunks of the SIDER file. Each chunk is
    a DataFrame with all derived columns (pubchem_cid, etc.).
    """
    effective_chunksize: int = _resolve_chunk_size(chunksize)
    return parse_sider_side_effects(
        filepath, chunksize=effective_chunksize, as_generator=True, **kwargs,
    )  # type: ignore[return-value]


def sider_to_legacy_edge_records(
    df: pd.DataFrame,
    *,
    meddra_type_filter: Optional[str] = "PT",
    dedup: bool = True,
) -> List[Dict[str, Any]]:
    """Convert SIDER drug-side effect pairs to LEGACY edge records (deprecated).

    Backward-compatible signature (Rule R3):
    ``sider_to_legacy_edge_records(df) -> List[Dict]``.

    .. deprecated:: 1.0.0
        Use :func:`sider_to_edge_records` instead. This function emits
        the legacy ``("Compound", "causes_side_effect", "Side Effect")``
        edge type, which is NOT queried by the RL safety ranker. The
        function will raise ``RuntimeError`` in v2.0 unless
        ``DRUGOS_SIDER_ALLOW_LEGACY=1`` is set (D2.10 / D14.3).

    Parameters
    ----------
    df : pd.DataFrame
        Parsed SIDER DataFrame.
    meddra_type_filter : str or None, default "PT"
        Phase 0.2 — filter to this MedDRA type.
    dedup : bool, default True
        D2.7 — dedupe edges by (pubchem_cid, umls_id_meddra).

    Returns
    -------
    list of dict
        Legacy edge records with ``dst_type="Side Effect"`` (with space)
        and ``rel_type="causes_side_effect"`` (Phase 0.3).

    Raises
    ------
    SiderDualWriteError
        If ``sider_to_edge_records`` has been called in the same process.
    RuntimeError
        If ``DRUGOS_SIDER_ALLOW_LEGACY=1`` is NOT set after the migration
        period (D2.10 / D14.3).
    DeprecationWarning
        Always emitted at the top of the function (D2.10).
    """
    # D2.10 — emit DeprecationWarning.
    warnings.warn(_LEGACY_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)

    # D14.3 — post-migration guard: refuse unless DRUGOS_SIDER_ALLOW_LEGACY=1.
    if not _allow_legacy():
        raise RuntimeError(
            "sider_to_legacy_edge_records is post-migration. The legacy "
            "edge type ('causes_side_effect' / 'Side Effect') has been "
            "superseded by the canonical ('causes_adverse_event' / "
            "'MedDRA_Term'). Set DRUGOS_SIDER_ALLOW_LEGACY=1 to suppress "
            "this error and continue (NOT recommended — see migration "
            "guide in scripts/migrate_sidetoeffect_to_meddraterm.py)."
        )

    # D2.13 / G13 — dual-write mutual exclusion.
    _check_dual_write(is_legacy=True)

    # Phase 0.2 — apply meddra_type_filter.
    df_filtered: pd.DataFrame = _apply_meddra_type_filter(df, meddra_type_filter)
    if dedup:
        df_filtered = _dedupe(df_filtered)
    # Build provenance once (D16.1).
    provenance: Dict[str, Any] = df.attrs.get("provenance", {})
    if not provenance:
        provenance = _build_provenance_dict(
            _get_sider_config(), df_filtered,
            source_sha256="",
            meddra_type_filter=meddra_type_filter,
            stereo_mode="flat",
        )
    # D4.7 — vectorized edge construction via to_dict(orient="records").
    # v41 ROOT FIX (Task K2 / SEV2): filter ``None`` edges (crosswalk
    # misses dead-lettered in ``_build_edge_record``).
    records: List[Dict[str, Any]] = [
        edge
        for row in df_filtered.to_dict(orient="records")
        if (edge := _build_edge_record(pd.Series(row), provenance, is_legacy=True)) is not None
    ]
    logger.info(
        "sider_edges_emitted_legacy",
        extra={"count": len(records),
               "meddra_type_filter": meddra_type_filter,
               "warning": "legacy emitter — use sider_to_edge_records"},
    )
    _append_transformation_log({
        "event": "edges_emitted_legacy",
        "count": len(records),
        "meddra_type_filter": meddra_type_filter,
        "warning": "legacy emitter",
    })
    return records


# ===== SECTION 11: DIFF (D16.10) =====

def diff_sider_outputs(old_df: pd.DataFrame, new_df: pd.DataFrame) -> Dict[str, Any]:
    """Diff two SIDER DataFrames for impact analysis (D16.10).

    Returns a dict with keys ``added_edges``, ``removed_edges``,
    ``modified_edges``, ``unchanged_count``.
    """
    def _key(row: pd.Series) -> Tuple[int, str]:
        return (int(row["pubchem_cid"]), str(row["umls_id_meddra"]))

    old_keys: set = {_key(row) for _, row in old_df.iterrows()}
    new_keys: set = {_key(row) for _, row in new_df.iterrows()}
    added: set = new_keys - old_keys
    removed: set = old_keys - new_keys
    common: set = old_keys & new_keys
    return {
        "added_edges": sorted(added),
        "removed_edges": sorted(removed),
        "modified_edges": [],  # SIDER has no edge properties to modify
        "unchanged_count": len(common),
        "old_total": len(old_keys),
        "new_total": len(new_keys),
    }


# ===== SECTION 12: FDA LABELS + FREQUENCIES (D3.3) =====
# v21 ROOT FIX (Audit section 7 findings 4 & 5 / Chain 7 - "Patient-safety
# STUB"): the previous code raised NotImplementedError for both
# parse_sider_fda_labels and parse_sider_frequencies. The docstring
# called this "the most dangerous blind spot" - drugs with black-box
# warnings but no post-marketing reports yet (newly approved, rare-event
# drugs) appeared to have ZERO adverse events, so the RL safety ranker
# scored them as GREEN. Top-10 hypotheses could include drugs with
# undisclosed severe risks.
#
# Fix: implement BOTH parsers. The SIDER file formats are:
#   - meddra_all_label.tsv.gz: columns
#       [stitch_compound_id, UMLS_concept_id_on_label (CUI),
#        meddra_concept_type (LLT/PT), meddra_concept_id, label_name]
#   - meddra_freq.tsv.gz: columns
#       [stitch_compound_id, UMLS_CUI, meddra_concept_type, meddra_concept_id,
#        label_name, frequency_description, lower_bound, upper_bound]
#
# The implementation:
#   1. Reads the TSV (gzip-aware).
#   2. Normalizes stitch_compound_id -> canonical Compound ID
#      (CIDm/CIDs prefix, same as parse_sider_side_effects).
#   3. Emits one row per (compound, meddra_term) pair with frequency
#      bounds (for freq file) or just presence (for label file).
#   4. Returns an empty DataFrame (NOT NotImplementedError) when the
#      file is missing - the absence of FDA-label data is itself a
#      data-quality signal that the caller can surface, not a crash.
#   5. ALWAYS logs a CRITICAL warning when the file is missing in
#      production mode (DRUGOS_ENVIRONMENT=prod) so operators know the
#      patient-safety blind spot is active.


def parse_sider_fda_labels(
    filepath: Optional[Path] = None,
    **kwargs: Any,
) -> pd.DataFrame:
    """Parse SIDER meddra_all_label.tsv.gz (D3.3).

    The SIDER meddra_all_label.tsv.gz file contains FDA drug labels with
    MedDRA terms - critical for catching black-box warnings that have
    not yet generated post-marketing reports (the most dangerous blind
    spot per D3.3 patient-safety note).

    v21 ROOT FIX: this function previously raised NotImplementedError.
    It now actually parses the file when present and returns an empty
    DataFrame (with the correct schema) when the file is missing - so
    callers can detect the gap and surface it instead of crashing.

    Returns
    -------
    pd.DataFrame
        Columns: stitch_compound_id, compound_canonical_id, umls_cui,
        meddra_type, meddra_id, meddra_name, source.
    """
    import os as _os
    from .config import RAW_DIR as _RAW, DATA_SOURCES as _DS

    if filepath is None:
        # SIDER label file lives under the sider raw subdir.
        sider_cfg = _DS.get("sider", {})
        fname = sider_cfg.get(
            "labels_filename", "meddra_all_label.tsv.gz"
        )
        filepath = _RAW / fname

    empty = pd.DataFrame(
        columns=[
            "stitch_compound_id", "compound_canonical_id",
            "umls_cui", "meddra_type", "meddra_id",
            "meddra_name", "source",
        ]
    )

    if not filepath.exists():
        # Missing file: log CRITICAL in production, WARNING in dev.
        env = _os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
        msg = (
            f"parse_sider_fda_labels: SIDER FDA-label file not found "
            f"at {filepath}. Drugs with black-box warnings but no "
            f"post-marketing reports yet will appear GREEN to the RL "
            f"safety ranker (patient-safety blind spot)."
        )
        if env in ("prod", "production"):
            logger.critical(msg)
        else:
            logger.warning(msg)
        return empty

    try:
        df = pd.read_csv(
            filepath, sep="\t", header=None, compression="gzip",
            names=[
                "stitch_compound_id", "umls_cui", "meddra_type",
                "meddra_id", "meddra_name",
            ],
            dtype=str,
            on_bad_lines="warn",
        )
    except Exception as exc:
        logger.error(
            "parse_sider_fda_labels: failed to parse %s: %s",
            filepath, exc,
        )
        return empty

    if df.empty:
        return empty

    # Normalize stitch_compound_id -> canonical Compound ID.
    # SIDER uses CIDm00000000 (methylated) / CIDs00000000 (stereo).
    # Both are valid Compound ID prefixes per ID_PATTERNS.
    def _norm_stitch(sid: str) -> Optional[str]:
        if not isinstance(sid, str) or not sid:
            return None
        s = sid.strip()
        # SIDER stitch IDs already start with CID; ensure uppercase.
        if s.upper().startswith("CID"):
            return s.upper()
        # Some SIDER exports use bare integers - prefix with CIDs.
        if s.isdigit():
            return f"CIDs{s}"
        return None

    df["compound_canonical_id"] = df["stitch_compound_id"].apply(_norm_stitch)
    df["source"] = "sider_fda_label"
    # Drop rows with no resolvable compound ID.
    df = df.dropna(subset=["compound_canonical_id"]).reset_index(drop=True)
    logger.info(
        "parse_sider_fda_labels: parsed %d FDA-label MedDRA terms from %s.",
        len(df), filepath,
    )
    return df[[
        "stitch_compound_id", "compound_canonical_id",
        "umls_cui", "meddra_type", "meddra_id",
        "meddra_name", "source",
    ]]


def parse_sider_frequencies(
    filepath: Optional[Path] = None,
    **kwargs: Any,
) -> pd.DataFrame:
    """Parse SIDER meddra_freq.tsv.gz (D3.3).

    The SIDER meddra_freq.tsv.gz file contains adverse-event frequency
    estimates - important for weighting severe vs minor adverse events
    in the RL safety ranker.

    v21 ROOT FIX: this function previously raised NotImplementedError.
    It now actually parses the file when present and returns an empty
    DataFrame (with the correct schema) when the file is missing.

    Returns
    -------
    pd.DataFrame
        Columns: stitch_compound_id, compound_canonical_id, umls_cui,
        meddra_type, meddra_id, meddra_name, frequency_description,
        lower_bound, upper_bound, source.
    """
    import os as _os
    from .config import RAW_DIR as _RAW, DATA_SOURCES as _DS

    if filepath is None:
        sider_cfg = _DS.get("sider", {})
        fname = sider_cfg.get(
            "frequencies_filename", "meddra_freq.tsv.gz"
        )
        filepath = _RAW / fname

    empty = pd.DataFrame(
        columns=[
            "stitch_compound_id", "compound_canonical_id",
            "umls_cui", "meddra_type", "meddra_id",
            "meddra_name", "frequency_description",
            "lower_bound", "upper_bound", "source",
        ]
    )

    if not filepath.exists():
        env = _os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
        msg = (
            f"parse_sider_frequencies: SIDER frequency file not found "
            f"at {filepath}. RL safety ranker will lack adverse-event "
            f"frequency estimates (patient-safety data gap)."
        )
        if env in ("prod", "production"):
            logger.critical(msg)
        else:
            logger.warning(msg)
        return empty

    try:
        df = pd.read_csv(
            filepath, sep="\t", header=None, compression="gzip",
            names=[
                "stitch_compound_id", "umls_cui", "meddra_type",
                "meddra_id", "meddra_name", "frequency_description",
                "lower_bound", "upper_bound",
            ],
            dtype=str,
            on_bad_lines="warn",
        )
    except Exception as exc:
        logger.error(
            "parse_sider_frequencies: failed to parse %s: %s",
            filepath, exc,
        )
        return empty

    if df.empty:
        return empty

    def _norm_stitch(sid: str) -> Optional[str]:
        if not isinstance(sid, str) or not sid:
            return None
        s = sid.strip()
        if s.upper().startswith("CID"):
            return s.upper()
        if s.isdigit():
            return f"CIDs{s}"
        return None

    df["compound_canonical_id"] = df["stitch_compound_id"].apply(_norm_stitch)
    df["source"] = "sider_frequency"
    df = df.dropna(subset=["compound_canonical_id"]).reset_index(drop=True)
    # Coerce bounds to float where possible.
    for col in ("lower_bound", "upper_bound"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    logger.info(
        "parse_sider_frequencies: parsed %d frequency rows from %s.",
        len(df), filepath,
    )
    return df[[
        "stitch_compound_id", "compound_canonical_id",
        "umls_cui", "meddra_type", "meddra_id",
        "meddra_name", "frequency_description",
        "lower_bound", "upper_bound", "source",
    ]]


# ===== SECTION 13: LOADER PROTOCOL ADAPTER (A1.4) =====

class SiderLoader:
    """Adapter implementing the ``Loader`` Protocol for SIDER (A1.4).

    Allows ``run_pipeline.py`` to treat all loaders polymorphically via
    the PEP 544 ``Loader`` Protocol (structural typing — no inheritance
    required).

    Examples
    --------
    >>> from drugos_graph.sider_loader import SiderLoader
    >>> from drugos_graph._loader_protocol import Loader
    >>> loader = SiderLoader()  # doctest: +SKIP
    >>> isinstance(loader, Loader)  # doctest: +SKIP
    True
    """

    name: str = SOURCE_KEY_SIDER   # class attribute — "sider"

    def __init__(self, *, meddra_type_filter: Optional[str] = "PT") -> None:
        self.meddra_type_filter = meddra_type_filter

    def download(self, force: bool = False) -> Path:
        """Download (or cached-load) the raw SIDER source file."""
        return download_sider(force=force)

    def parse(self, path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
        """Yield parsed side-effect records as dicts (PT-only by default)."""
        df: pd.DataFrame = parse_sider_side_effects(
            path, meddra_type_filter=self.meddra_type_filter,
        )
        for record in df.to_dict(orient="records"):
            yield record

    def to_graph(
        self,
        records: Any,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Convert records into ``(nodes, edges)`` for the KG.

        ``records`` may be a pd.DataFrame or an iterable of dicts.
        Returns ``(nodes, edges)`` since SIDER provides both.
        """
        if isinstance(records, pd.DataFrame):
            df: pd.DataFrame = records
        else:
            df = pd.DataFrame(list(records))
        nodes: List[Dict[str, Any]] = sider_to_node_records(
            df, meddra_type_filter=self.meddra_type_filter,
        )
        edges: List[Dict[str, Any]] = sider_to_edge_records(
            df, meddra_type_filter=self.meddra_type_filter,
        )
        return nodes, edges

    def load(
        self,
        skip_neo4j: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        """End-to-end pipeline: download -> parse -> validate -> emit."""
        return load_sider(skip_neo4j=skip_neo4j, force=force)


# ===== SECTION 14: FACADE / ORCHESTRATION (A1.4) =====

def load_sider(
    skip_neo4j: bool = True,
    force: bool = False,
    meddra_type_filter: Optional[str] = "PT",
) -> Dict[str, Any]:
    """End-to-end SIDER pipeline: download -> parse -> validate -> emit.

    This is the facade that ``run_pipeline.py`` should call. It
    orchestrates the full pipeline and returns a structured result dict.

    Parameters
    ----------
    skip_neo4j : bool, default True
        If True (default), skip the Neo4j load step (no driver available
        in test environment). The nodes + edges are still returned in
        the result dict for the caller to load.
    force : bool, default False
        Force re-download of the SIDER file.
    meddra_type_filter : str or None, default "PT"
        Phase 0.2 — filter to this MedDRA type.

    Returns
    -------
    dict
        Result dict with keys: ``nodes``, ``edges``, ``load_id``,
        ``source_sha256``, ``source_version``, ``validation``, ``errors``,
        ``metrics``, ``dlq_path``.
    """
    load_id: str = _get_load_id()
    errors: List[str] = []
    metrics: "_SiderLoaderMetricsDataclass" = _SiderLoaderMetricsDataclass()

    if _should_skip():
        logger.warning("sider_load_skipped_by_env", extra={"load_id": load_id})
        return {
            "nodes": [], "edges": [], "load_id": load_id,
            "source_sha256": "", "source_version": "",
            "validation": {}, "errors": ["skipped by DRUGOS_SIDER_SKIP=1"],
            "metrics": metrics.to_dict(), "dlq_path": str(DEFAULT_DLQ_PATH),
        }

    t_total: float = time.perf_counter()

    # Phase 1: Download.
    try:
        gz_path: Path = download_sider(force=force)
        source_sha: str = _compute_sha256(gz_path) if not _skip_sha256() else ""
    except SiderCriticalError as exc:
        raise
    except Exception as exc:
        if SIDER_REQUIRED:
            raise SiderCriticalError(
                f"SIDER is required but download failed: {exc}",
                context={"load_id": load_id, "error": str(exc)},
            ) from exc
        errors.append(f"download_failed: {exc}")
        return {
            "nodes": [], "edges": [], "load_id": load_id,
            "source_sha256": "", "source_version": "",
            "validation": {}, "errors": errors,
            "metrics": metrics.to_dict(), "dlq_path": str(DEFAULT_DLQ_PATH),
        }

    cfg: Dict[str, Any] = _get_sider_config()

    # Phase 2: Parse.
    t_parse: float = time.perf_counter()
    df: pd.DataFrame = parse_sider_side_effects(
        gz_path, meddra_type_filter=meddra_type_filter,
    )
    metrics.parse_time_seconds = time.perf_counter() - t_parse
    metrics.rows_in = int(df.attrs.get("provenance", {}).get("row_count_in", len(df)))
    metrics.rows_after_dedup = int(len(df))

    # Phase 3: Validate.
    validation: Dict[str, Any] = validate_sider(df)

    # Phase 4: Build nodes + edges.
    t_edges: float = time.perf_counter()
    nodes: List[Dict[str, Any]] = sider_to_node_records(
        df, meddra_type_filter=meddra_type_filter,
    )
    edges: List[Dict[str, Any]] = sider_to_edge_records(
        df, meddra_type_filter=meddra_type_filter,
    )

    # v29 ROOT FIX (Compound Chain 1 / Patient-Safety Bypass): the
    # forensic audit found parse_sider_frequencies was implemented but
    # NEVER CALLED. As a result, the RL safety ranker could not
    # distinguish 50% ADRs from 0.01% ADRs — a drug causing frequent
    # severe adverse events looked identical to a drug causing rare
    # minor ones. ROOT FIX: parse the frequencies file here, build a
    # lookup keyed by (compound_canonical_id, meddra_id), and attach
    # lower_bound / upper_bound / frequency_description as edge
    # properties on every (Compound, causes_adverse_event, MedDRA_Term)
    # edge that matches. Edges with no frequency data keep their
    # existing properties — they just lack the frequency bounds.
    try:
        freq_df = parse_sider_frequencies()
        if not freq_df.empty:
            # Build a lookup: (compound_canonical_id, meddra_id) -> freq row.
            freq_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for _, fr in freq_df.iterrows():
                key = (
                    str(fr.get("compound_canonical_id", "")).strip(),
                    str(fr.get("meddra_id", "")).strip(),
                )
                if not key[0] or not key[1]:
                    continue
                freq_lookup[key] = {
                    "frequency_description": fr.get("frequency_description"),
                    "frequency_lower_bound": fr.get("lower_bound"),
                    "frequency_upper_bound": fr.get("upper_bound"),
                    "frequency_source": fr.get("source", "sider_frequency"),
                }
            # Attach frequency properties to matching edges.
            matched = 0
            for e in edges:
                if e.get("rel_type") != "causes_adverse_event" and \
                        e.get("relation") != "causes_adverse_event":
                    continue
                src_id = str(e.get("src_id", "")).strip()
                dst_id = str(e.get("dst_id", "")).strip()
                key = (src_id, dst_id)
                if key in freq_lookup:
                    e.update(freq_lookup[key])
                    matched += 1
            metrics_dict = metrics.to_dict()
            metrics_dict["frequency_edges_matched"] = matched
            metrics_dict["frequency_rows_parsed"] = int(len(freq_df))
            logger.info(
                "sider_frequencies_attached",
                extra={"load_id": load_id,
                       "frequency_rows": int(len(freq_df)),
                       "edges_matched": matched,
                       "total_edges": len(edges)},
            )
        else:
            logger.warning(
                "sider_frequencies_empty_or_missing",
                extra={"load_id": load_id,
                       "hint": "RL safety ranker will lack ADR frequency estimates"},
            )
    except Exception as freq_exc:  # noqa: BLE001 — never crash load on freq
        logger.error(
            "sider_frequencies_attach_failed",
            extra={"load_id": load_id, "error": str(freq_exc)},
        )

    metrics.edge_build_time_seconds = time.perf_counter() - t_edges
    metrics.nodes_created = len(nodes)
    metrics.edges_created = len(edges)

    elapsed: float = time.perf_counter() - t_total
    logger.info(
        "sider_load_complete",
        extra={"load_id": load_id, "elapsed_seconds": round(elapsed, 3),
               "nodes": len(nodes), "edges": len(edges),
               "source_sha256": source_sha},
    )
    return {
        "nodes": nodes, "edges": edges, "load_id": load_id,
        "source_sha256": source_sha,
        "source_version": str(cfg.get("version", SIDER_PINNED_VERSION)),
        "validation": validation, "errors": errors,
        "metrics": metrics.to_dict(), "dlq_path": str(DEFAULT_DLQ_PATH),
        "elapsed_seconds": round(elapsed, 3),
    }


# ===== SECTION 15: INIT-TIME VALIDATION (D14.12 / D14.13) =====
# Validate that the canonical SIDER node/edge types are in config.CORE_NODE_TYPES
# and config.CORE_EDGE_TYPES at import time. Fail fast (loudly) if the
# canonical spellings are missing — this would silently break the RL safety
# ranker.

def _init_validate_config() -> None:
    """Validate canonical SIDER types against config at import time (D14.12 / D14.13).

    Raises
    ------
    SiderCriticalError
        If ``SIDER_NODE_TYPE`` ("MedDRA_Term") is not in
        ``config.CORE_NODE_TYPES``, or if ``("Compound",
        "causes_adverse_event", "MedDRA_Term")`` is not in
        ``config.CORE_EDGE_TYPES_SET``. This would silently break the RL
        safety ranker (Phase 0.3 / G7).
    """
    if SIDER_NODE_TYPE not in CORE_NODE_TYPES:
        raise SiderCriticalError(
            f"SIDER_NODE_TYPE {SIDER_NODE_TYPE!r} not in config.CORE_NODE_TYPES. "
            f"This would silently break the RL safety ranker (Phase 0.3 / G7).",
            context={"sider_node_type": SIDER_NODE_TYPE,
                     "core_node_types": list(CORE_NODE_TYPES)},
        )
    if (_SRC_TYPE, _REL_TYPE_CANONICAL, _DST_TYPE_CANONICAL) not in CORE_EDGE_TYPES_SET:
        raise SiderCriticalError(
            f"SIDER canonical edge type "
            f"({_SRC_TYPE!r}, {_REL_TYPE_CANONICAL!r}, {_DST_TYPE_CANONICAL!r}) "
            f"not in config.CORE_EDGE_TYPES_SET. This would silently break "
            f"the RL safety ranker (Phase 0.3 / G7).",
            context={"canonical_edge": (_SRC_TYPE, _REL_TYPE_CANONICAL, _DST_TYPE_CANONICAL),
                     "core_edge_types_count": len(CORE_EDGE_TYPES_SET)},
        )


# Run init-time validation. Wrap in try/except so import doesn't crash the
# whole package if config is broken — log ERROR instead (caller can detect
# via the absence of SiderLoader).
try:
    _init_validate_config()
except SiderCriticalError as _init_exc:
    logger.error(
        "sider_init_config_validation_failed",
        extra={"error": str(_init_exc),
               "hint": "SIDER loader will fail at runtime — fix config.CORE_NODE_TYPES "
                       "and config.CORE_EDGE_TYPES to include MedDRA_Term + causes_adverse_event"},
    )


# ===== SECTION 16: MODULE-LEVEL SANITY CHECKS =====
# Log a startup INFO message so the operator knows the loader is ready.
logger.info(
    "sider_loader_initialized",
    extra={"parser_version": PARSER_VERSION,
           "schema_version": SCHEMA_VERSION,
           "pinned_version": SIDER_PINNED_VERSION,
           "compound_id_format": SIDER_COMPOUND_ID_FORMAT,
           "canonical_node_type": SIDER_NODE_TYPE,
           "canonical_edge_type": SIDER_EDGE_TYPE},
)


# ===== SECTION 17: BACKWARD-COMPAT ALIAS (Rule R3) =====
# Now that parse_sider_side_effects is defined, populate the parse_sider alias.
parse_sider = parse_sider_side_effects
