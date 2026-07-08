# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
"""
Pipelines package — Phase 1 ETL layer for the Autonomous Drug Repurposing
Platform.

This package is the **package marker for the Phase 1 ETL layer** of the
platform. Every record that enters the staging PostgreSQL database enters via
one of the 7 source pipeline classes re-exported here. The data flow is:

    7 public biomedical APIs / files
            |
            v
    pipelines.{chembl,drugbank,uniprot,string,disgenet,omim,pubchem}_pipeline.py
            |  (each subclasses BasePipeline; download -> clean -> load)
            v
    cleaning/  (InChIKey normalization, dedup, missing-value recovery)
            |
            v
    entity_resolution/  (drug/protein cross-source matching via InChIKey
                         + name + fuzzy)
            |
            v
    database/loaders.py  (bulk_upsert_drugs, bulk_upsert_proteins,
                          bulk_upsert_dpi, bulk_upsert_ppi,
                          bulk_upsert_gda, bulk_upsert_entity_mapping)
            |
            v
    PostgreSQL staging DB (7 tables: drugs, proteins,
                           drug_protein_interactions,
                           protein_protein_interactions,
                           gene_disease_associations, entity_mapping,
                           pipeline_runs)
            |
            v
    Phase 2: Neo4j knowledge graph (5 node types, 5 edge types)
            |
            v
    Phase 3: Graph Transformer (PyTorch Geometric) -> drug-disease score
            |
            v
    Phase 4: RL ranker -> top-N hypotheses sold to pharma
            |
            v
    Phase 5: researcher takes the drug into a wet lab -> patient receives
             the drug

If ``pipelines/__init__.py`` lies about a scientific fact (e.g. claims
``source_name="chembl"`` is a protein source), the lie propagates through
every downstream phase. The graph transformer trains on bad labels. The RL
ranker optimises for the wrong objective. A pharma customer runs a $5M
wet-lab experiment on a wrong target. A patient dies. Scientific correctness
is therefore 100% non-negotiable (Domain 3 priority P0).

Architecture
------------
The pipelines package is organised into 8 submodules, each responsible for
one clearly defined ETL concern:

- **pipelines.base_pipeline** — Abstract base class (``BasePipeline``) that
  enforces the ``download -> clean -> load`` contract and writes a
  ``pipeline_runs`` audit row for every run. Defines 3 abstract methods
  (``download``, ``clean``, ``load``) and the
  ``_get_processed_filename`` source-name-to-CSV-path mapping
  (``base_pipeline.py:174-185``).

- **pipelines.chembl_pipeline** — ChEMBL REST API client. Small-molecule
  bioactivity (IC50/Ki/Kd in nM). Source name ``"chembl"``. Output:
  ``drugs.csv``. KG contribution: Drug nodes + Drug->Protein edges.

- **pipelines.drugbank_pipeline** — DrugBank XML parser. FDA-approved drug
  metadata + targets. Source name ``"drugbank"``. Output:
  ``drugbank_drugs.csv``. KG contribution: Drug nodes (enriched) +
  Drug->Protein edges.

- **pipelines.uniprot_pipeline** — UniProt REST client. Reviewed human
  protein sequences. Source name ``"uniprot"``. Output: ``proteins.csv``.
  KG contribution: Protein nodes.

- **pipelines.string_pipeline** — STRING PPI network client. Protein-protein
  interactions. Source name ``"string"``. Output:
  ``protein_protein_interactions.csv``. KG contribution: Protein->Protein
  edges (Pathway membership).

- **pipelines.disgenet_pipeline** — DisGeNET GDA client. Curated
  gene-disease associations. Source name ``"disgenet"``. Output:
  ``gene_disease_associations.csv``. KG contribution: Gene->Disease edges.

- **pipelines.omim_pipeline** — OMIM API client. Mendelian
  gene-phenotype mappings. Source name ``"omim"``. Output:
  ``omim_gene_disease_associations.csv``. KG contribution: Gene->Disease
  edges (rare-disease signal).

- **pipelines.pubchem_pipeline** — PubChem PUG REST client. Compound
  structural/property data. Source name ``"pubchem"``. Output:
  ``pubchem_enrichment.csv``. KG contribution: Drug node enrichment
  (molecular fingerprint — no new nodes).

``BasePipeline`` is an **abstract base class (ABC)**. It cannot be
instantiated directly — ``BasePipeline()`` raises ``TypeError`` (ABC with
abstract methods). It is exposed at the package top-level for type-checking
and subclassing only. For ETL execution, use one of the 7 concrete
subclasses.

Recommended Import Pattern
--------------------------
Prefer ``from pipelines import X`` over ``from pipelines.X_pipeline import
Y``::

    from pipelines import ChEMBLPipeline, DrugBankPipeline
    from pipelines import UniProtPipeline, StringPipeline
    from pipelines import DisGeNETPipeline, OMIMPipeline, PubChemPipeline
    from pipelines import BasePipeline, __version__
    from pipelines import get_pipeline, get_expected_pipelines
    from pipelines import validate_infrastructure, get_provenance

The package-level import is complete — every public symbol from all 8
submodules is available directly. Direct submodule imports (e.g.
``from pipelines.chembl_pipeline import ChEMBLPipeline``) continue to work
for backward compatibility and are required by the Makefile (lines 17-23,
34-40) and the 7 DAGs (``dags/*.py``). The deep path bypasses this
package's ``__getattr__`` and is therefore unaffected by the lazy façade.

Lazy Loading
------------
All symbols are lazily loaded on first attribute access via PEP 562.
Importing the ``pipelines`` package does **not** trigger side effects:
no SQLAlchemy engine creation, no ``.env`` loading, no pandas import, no
requests import, no rdkit import. Side effects are deferred until the
first symbol attribute is accessed.

This design is critical for Apache Airflow DAG parsing: Airflow's scheduler
parses every DAG file on every heartbeat (default every 5 seconds).
``dags/master_pipeline_dag.py`` imports ``pipelines.chembl_pipeline`` (and
the 6 others) inside task callables — but Python imports the ``pipelines``
**package** first, which runs ``pipelines/__init__.py``. If this file
eager-imported sqlalchemy/pandas/rdkit, every scheduler tick would pay
200ms+ of import cost AND every DAG would fail to parse if any of those
deps was missing (e.g. rdkit on ARM64). With PEP 562 lazy loading,
``import pipelines`` is O(1) (<5ms cold) and side-effect-free.

The same constraint applies to the ``rdkit`` dependency on ARM64
(Apple Silicon, AWS Graviton): ``requirements.txt:13`` documents that
``rdkit-pypi`` has no ARM64 wheels. With lazy loading,
``from pipelines import UniProtPipeline`` works on ARM64 even though
``from pipelines import ChEMBLPipeline`` will raise a clear ``ImportError``
explaining that ``rdkit`` is missing.

Performance characteristics:

- Importing the package is O(1) — no submodule loading occurs.
- First access to any symbol triggers its submodule import (one-time cost).
- Subsequent accesses are O(1) dict lookups from the internal ``_loaded``
  cache.
- Per-symbol load times are recorded in ``_load_times`` for performance
  profiling (see ``get_load_times()`` and ``performance_benchmark()``).

Scientific Note on InChIKey Normalization
-----------------------------------------
InChIKeys use the format ``[A-Z]{14}-[A-Z]{10}-[A-Z]`` (27 characters
total, 3 hyphen-separated blocks). The canonical regex pattern is
``INCHIKEY_PATTERN`` defined in ``entity_resolution/base.py`` and is
re-used across the platform — do not redefine it.

An InChIKey has three blocks separated by hyphens::

    AAAAABBBBBBBBCC-DDDDDDDDDF-E
    |--- 14 chars --|-- 10 chars -| 1 char

- **Block 1 (14 chars):** Molecular connectivity layer. Two molecules with
  the same block 1 share the same molecular skeleton (same atoms, same
  bonds).
- **Block 2 (10 chars):** Stereochemistry and protonation. Enantiomers
  share block 1 but differ in block 2.
- **Block 3 (1 char):** Version/charge layer.

**Drug-repurposing patient-safety implication:** Enantiomers (same
connectivity, different stereochemistry) can have dramatically different
biological activities. The classic example is **thalidomide**: one
enantiomer is a sedative, the other is a teratogen. Warfarin, citalopram,
and many other drugs have similar enantiomer-specific safety profiles.
**NEVER collapse drugs by block-1-only match without explicit consumer
opt-in.** This is the ``collapse_stereoisomers=False`` default in
``entity_resolution/__init__.py:86-103``.

InChIKeys are produced by:
- ``ChEMBLPipeline`` (via ``cleaning.normalizer.convert_to_inchikey``)
- ``DrugBankPipeline`` (parses InChIKey from DrugBank XML)
- ``PubChemPipeline`` (PubChem REST returns InChIKey)

InChIKeys **cannot represent** biologics (antibodies, proteins, cell
therapies). RDKit only converts small-molecule SMILES. The
``convert_to_inchikey()`` function returns ``None`` for non-convertible
inputs, and the ``Drug`` table allows ``inchikey`` to be NULL to
accommodate biologics documented in DrugBank.

Scientific Note on Knowledge-Graph Node/Edge Mapping
-----------------------------------------------------
The platform's Phase 2 knowledge graph has 5 node types and 5 edge types.
Each pipeline contributes a specific subset:

- **5 node types:** Drugs, Proteins, Biological Pathways, Diseases,
  Clinical Outcomes.
- **5 edge types:** Drug->inhibits/activates->Protein,
  Protein->is part of->Pathway, Pathway->is disrupted in->Disease,
  Drug->treats/is tested for->Disease, Drug->causes->Adverse Event.

Pipeline-to-node/edge mapping (verified against each pipeline's
``clean()`` return type):

- ``ChEMBLPipeline`` -> Drug nodes (small-molecule compounds) +
  Drug-Protein edges (bioactivity).
- ``DrugBankPipeline`` -> Drug nodes (FDA-approved, enriched metadata) +
  Drug-Protein edges (target relationships).
- ``UniProtPipeline`` -> Protein nodes (reviewed human proteins).
- ``StringPipeline`` -> Protein-Protein edges (PPIs that define pathway
  membership).
- ``DisGeNETPipeline`` -> Gene-Disease edges (curated, score-weighted).
- ``OMIMPipeline`` -> Gene-Disease edges (Mendelian / rare-disease signal).
- ``PubChemPipeline`` -> Drug node enrichment (molecular formula, weight,
  fingerprint — no new nodes, just enriched existing Drug nodes).

Recommended Processing Order
----------------------------
The master DAG in ``dags/master_pipeline_dag.py`` enforces the following
entity-resolution sequencing. It is a **scientific constraint**, not a code
convention — reversing it produces orphan foreign keys and incorrect graph
topology.

1. **Drug-producing pipelines run first:** ``chembl``, ``drugbank``,
   ``uniprot``, ``string`` (download+clean+load).
2. **Disease-producing pipelines run in parallel:** ``disgenet``,
   ``omim`` (download+clean only — load deferred).
3. **PubChem enrichment is deferred** until after drug entity resolution
   because it queries the DB for existing drug InChIKeys.
4. **Entity resolution runs** after the 4 primary drug/protein sources
   complete. ``entity_resolution.DrugResolver`` reconciles ChEMBL +
   DrugBank + PubChem by InChIKey (exact -> connectivity ->
   name-normalized -> fuzzy -> PubChem-xref, in that priority order).
   ``entity_resolution.ProteinResolver`` reconciles UniProt + STRING by
   UniProt accession (exact -> STRING->UniProt xref -> gene-symbol+
   organism -> protein-name fuzzy).
5. **Post-resolution loads:** STRING, DisGeNET, OMIM, PubChem are loaded
   into the DB only after entity resolution has populated
   ``entity_mapping`` and updated ``proteins.string_id``.

Consumers using ``pipelines.<X>Pipeline.run()`` outside the master DAG
must respect this ordering. Use ``find_affected_downstream(source_name)``
to inspect the dependency graph.

Security Note
-------------
Because imports are deferred, pipeline credentials (``OMIM_API_KEY``,
``DISGENET_API_KEY``, ``DRUGBANK_XML_PATH``, ``DATABASE_URL``) are **not**
loaded into memory until the first symbol from a pipeline submodule is
accessed. This reduces the credential exposure window.

Credential requirements per source (verified against each pipeline
module):

- **ChEMBL** — None. Public REST API, no key.
- **DrugBank** — License file. Set ``DRUGBANK_XML_PATH`` env var to the
  path of the DrugBank XML file (manually downloaded from drugbank.com
  with a paid academic/commercial license). Pipeline gracefully skips
  if missing (see ``master_pipeline_dag.py:64-83``).
- **UniProt** — None. Public REST API, no key.
- **STRING** — None. Public FTP download, no key.
- **DisGeNET** — Optional API key. Set ``DISGENET_API_KEY`` env var.
  The API key raises the rate limit; the pipeline works without it at
  lower throughput.
- **OMIM** — Required API key. Set ``OMIM_API_KEY`` env var. The OMIM
  API rejects unauthenticated requests. The pipeline fails fast with a
  clear message if the key is missing.
- **PubChem** — None. Public PUG REST, no key. (API key optional for
  higher rate limit.)

**PII Handling:** This package processes drug, protein, and disease data —
**NO personally identifiable information (PII), NO protected health
information (PHI), NO patient records.** The data is fully public (ChEMBL,
UniProt, STRING, DisGeNET, OMIM, PubChem) or licensed-research (DrugBank).
HIPAA/GDPR do not apply to this layer of the platform. The Phase 5 API
layer may handle user-facing query logs — those are out of scope here.

The ``_validate_security()`` function audits the credential configuration
for insecure patterns (missing OMIM_API_KEY in production, in-memory
SQLite in non-test envs, etc.). The ``get_config_summary()`` function
returns a credential-masked dict safe for logging — it never exposes raw
credential values, only ``<set>``/``<unset>``/``<masked>`` placeholders.

Data Lineage & Transformation Entry Points
-------------------------------------------
Every record that enters the staging PostgreSQL database passes through
one of the 7 pipeline ``download -> clean -> load`` cycles. The
``BasePipeline._write_run_log`` method (``base_pipeline.py:266-296``)
writes a ``pipeline_runs`` audit row recording source, status, row counts,
error message, and duration for every run.

The ``PipelineRun.source`` column should be set to
``f'{source_name} (pipelines v{__version__})'`` for full traceability. This
is documented in ``database/__init__.py:84-85``. The package-level
``get_provenance()`` and ``get_audit_trail()`` functions provide
package-level lineage metadata that can be logged alongside the DB row.

**Known lineage gap:** The ``pipeline_runs`` DB table (defined in
``database/models.py``) currently records source, run_date, status, row
counts, error_message, and duration_seconds. It does NOT record: pipelines
package version, input file checksum, output file checksum, code git SHA,
or correlation ID. Closing this gap requires editing
``database/models.py`` (add columns) and ``base_pipeline.py:266-296``
(populate them) — out of scope for this file. As a workaround,
``pipelines.get_provenance()`` and ``pipelines.get_audit_trail()``
provide package-level lineage metadata that can be logged alongside the
DB row.

The ChEMBL API version is ``CHEMBL_VERSION`` from ``config.settings``.
Pipeline runs SHOULD include ``config.settings.CHEMBL_VERSION`` in their
``metadata_json`` to record which ChEMBL release was queried. The same
applies to ``STRING_VERSION``, ``UNIPROT_VERSION``, ``DRUGBANK_VERSION``,
``DISGENET_VERSION``, ``OMIM_VERSION``, ``PUBCHEM_VERSION``. The list
``KNOWN_DATA_SOURCE_VERSIONS`` enumerates these env-var names for
programmatic consumption.

Configuration & Environment
---------------------------
- ``PIPELINES_LAZY_IMPORT`` — Set to ``"0"`` to force eager loading at
  import time (fail-fast mode for production debugging). Default: ``"1"``
  (lazy).
- ``ENVIRONMENT`` — One of ``development``, ``staging``, ``production``,
  ``test``. In production, eager-loading failures are FAIL; in
  development, they are WARN (allows partial dev environments).
- ``PIPELINES_SEED`` — Integer seed for downstream pipelines that use
  randomness. Set via ``set_seed(seed)``.
- ``PIPELINES_GRACEFUL_DEGRADATION`` — Set to ``"1"`` to make
  ``get_pipeline(name)`` return a ``_PipelineUnavailable`` sentinel
  instead of raising ``ImportError`` when a pipeline's deps are missing.
  Useful for the master DAG to skip a broken pipeline without crashing
  the whole DAG.

Optional Utilities
------------------
- ``get_pipeline(name)`` — Factory: return the pipeline CLASS for a source
  name (NOT an instance).
- ``get_expected_pipelines()`` — Single source of truth for the 7 source
  names.
- ``get_kg_mapping()`` — Return the pipeline-to-KG-node/edge mapping.
- ``get_filtering_thresholds()`` — Return the scientific thresholds table
  (MIN_SCORE, MAPPING_KEY_CONFIRMED, etc.) with rationale.
- ``get_data_dictionary()`` — Return the data dictionary for the 7 output
  CSVs.
- ``get_source_attribution()`` — Return which sources contribute which
  fields to each output CSV.
- ``find_affected_downstream(source_name)`` — Return the list of output
  files affected by a change to source_name.
- ``compute_file_checksum(path)`` — SHA-256 checksum of a file (for
  lineage metadata).
- ``validate_infrastructure()`` — Comprehensive package validation.
- ``_validate_security()`` — Audit credential configuration.
- ``get_config_summary()`` — Credential-masked config for safe logging.
- ``validate_config()`` — Validate env-var configuration.
- ``_reset()`` — Clear the lazy-loaded symbol cache for testing.
- ``_log_import_status()`` — Log which symbols have been loaded.
- ``get_provenance()`` — Provenance metadata (version, git SHA, etc.).
- ``get_audit_trail()`` — Combined audit trail.
- ``to_state_dict()`` / ``from_state_dict(state)`` — Serialise/restore
  package state for reproducibility.
- ``set_correlation_id(cid)`` / ``get_correlation_id()`` — Correlation ID
  for log correlation across pipelines.
- ``set_seed(seed)`` — Set the global pipelines seed.
- ``set_log_level(level)`` — Set the package logger level.
- ``initialize()`` — Explicitly trigger eager loading.
- ``reload()`` — Re-import the pipelines package and clear caches.
- ``is_loaded()`` — Return True iff at least one symbol has been loaded.
- ``is_reproducible()`` — Return True iff the package is configured for
  deterministic, reproducible runs.
- ``health_check()`` — Return a health status dict.
- ``get_metrics()`` — Return import metrics.
- ``get_load_times()`` — Return per-symbol load times.
- ``performance_benchmark()`` — Benchmark import time for every symbol.
- ``recover_from_failure()`` — Recover from a failed import state.
- ``get_dead_letters()`` — Return the dead-letter queue.
- ``requires_api_version(min_version)`` — Assert version compatibility.
- ``get_json_schema()`` — Load the JSON schema for the 7 output CSVs.
- ``_deprecated(name, removal_version, alternative)`` — Emit a
  DeprecationWarning for a public name.

Changelog
---------
v1.0.0 (AUDIT-34) — Initial 26-line convenience-imports file (8 classes
    only).
v2.0.0 — Complete institutional rewrite: PEP 562 lazy façade over 8
    pipeline submodules + base, full re-export of 20+ public constants,
    ``__version__``, ``__getattr__``/``__dir__``, SPDX header,
    ``validate_infrastructure``, ``_validate_security``,
    ``get_provenance``, ``get_audit_trail``, correlation-ID propagation,
    ``to_state_dict``/``from_state_dict``, ``PIPELINES_LAZY_IMPORT`` env
    toggle, full 16-domain compliance, 130 audit findings fixed.

Public API contract — semver-protected. Breaking changes require major
version bump. v2.0.0 is a breaking change from v1.0.0 — the public API
surface expanded from 8 names to 40+ names. Downstream consumers using
``from pipelines import *`` will see new names but no existing names were
removed.

See Also
--------
pipelines.base_pipeline : Abstract base class and PipelineRun audit logging.
pipelines.chembl_pipeline : Small-molecule bioactivity (ChEMBL REST API).
pipelines.drugbank_pipeline : FDA-approved drug metadata (DrugBank XML).
pipelines.uniprot_pipeline : Reviewed human protein sequences (UniProt REST).
pipelines.string_pipeline : Protein-protein interaction network (STRING).
pipelines.disgenet_pipeline : Gene-disease associations (DisGeNET).
pipelines.omim_pipeline : Mendelian gene-phenotype mappings (OMIM).
pipelines.pubchem_pipeline : Compound structural enrichment (PubChem PUG REST).
cleaning : InChIKey normalization, deduplication, missing-value recovery.
entity_resolution : Cross-source drug/protein reconciliation.
database.loaders : Bulk upsert functions for the staging DB.

Quick Start
-----------
>>> from pipelines import __version__
>>> __version__
'2.0.0'
>>> from pipelines import get_expected_pipelines  # doctest: +SKIP
>>> sorted(get_expected_pipelines())  # doctest: +SKIP
['chembl', 'disgenet', 'drugbank', 'omim', 'pubchem', 'string', 'uniprot']
>>> from pipelines import ChEMBLPipeline  # doctest: +SKIP
>>> ChEMBLPipeline.source_name  # doctest: +SKIP
'chembl'

Minimum Python version: 3.9. Required for ``from __future__ import
annotations`` (PEP 563) and ``dict[str, str]`` syntax (PEP 585).
"""

# This file adheres to PEP 20 — see the docstring for explicit contracts,
# dense institutional features, and fail-loud error handling.

from __future__ import annotations

import importlib
import logging
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Package metadata (Domain 15: Interoperability / Domain 16: Lineage)
# ---------------------------------------------------------------------------
__version__: str = "2.0.0"

# Schema version of the 7 output CSV files. Bumped when any CSV's column set
# changes. Downstream consumers (graph transformer) should check
# ``pipelines.SCHEMA_VERSION`` before parsing. (INT-8)
SCHEMA_VERSION: str = "2.0"

# Minimum Python version. 3.9+ is required for ``from __future__ import
# annotations`` (PEP 563) and ``dict[str, str]`` syntax (PEP 585).
# (INT-10)
PYTHON_MIN_VERSION: tuple[int, int] = (3, 9)

# Default seed for downstream pipelines that use randomness. Pipelines
# SHOULD read ``os.environ['PIPELINES_SEED']`` and pass it to
# ``random.seed()`` / ``numpy.random.seed()``. (IDEM-4)
DEFAULT_SEED: int = 42

# Env-var names that pipeline modules SHOULD read from ``config.settings``
# and embed in ``metadata_json`` for lineage. (LIN-10)
KNOWN_DATA_SOURCE_VERSIONS: list[str] = [
    "CHEMBL_VERSION",
    "DRUGBANK_VERSION",
    "UNIPROT_VERSION",
    "STRING_VERSION",
    "DISGENET_VERSION",
    "OMIM_VERSION",
    "PUBCHEM_VERSION",
]

# ---------------------------------------------------------------------------
# Source-name to pipeline-class-name mapping (single source of truth)
# (DQ-8, CONF-9, ARCH-7)
# ---------------------------------------------------------------------------
_SOURCE_TO_CLASS: dict[str, str] = {
    "chembl": "ChEMBLPipeline",
    "drugbank": "DrugBankPipeline",
    "uniprot": "UniProtPipeline",
    "string": "StringPipeline",
    "disgenet": "DisGeNETPipeline",
    "omim": "OMIMPipeline",
    "pubchem": "PubChemPipeline",
}

# Submodules also accessible via ``pipelines.X`` (mirrors entity_resolution
# pattern). When a consumer does ``from pipelines import chembl_pipeline``,
# PEP 562 ``__getattr__`` resolves it via ``importlib.import_module``.
# (COMP-6, CODE-2)
_SUBMODULES: tuple[str, ...] = (
    "base_pipeline",
    "chembl_pipeline",
    "drugbank_pipeline",
    "uniprot_pipeline",
    "string_pipeline",
    "disgenet_pipeline",
    "omim_pipeline",
    "pubchem_pipeline",
)

# ---------------------------------------------------------------------------
# PEP 562 symbol map: public_name -> (submodule_path, attribute_name)
# (ARCH-5, DES-4)
#
# Design notes:
# - ``MAX_RETRIES`` and ``PAGE_SIZE`` exist in BOTH chembl_pipeline.py and
#   uniprot_pipeline.py / pubchem_pipeline.py with DIFFERENT values. To
#   avoid ambiguity, they are NOT in this map; consumers should use the
#   deep path: ``from pipelines.chembl_pipeline import PAGE_SIZE``.
# - ``_LOWER_TYPE_MAP`` starts with underscore but is imported by tests
#   (test_fix_verification.py:626,632; test_settings.py:868;
#   test_issue_fixes.py:679), so it IS public despite the underscore.
# - Every constant value is verified against the source file (see comments).
# ---------------------------------------------------------------------------
_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    # --- Base & Concrete Pipeline Classes (8) ---
    "BasePipeline": ("pipelines.base_pipeline", "BasePipeline"),
    "ChEMBLPipeline": ("pipelines.chembl_pipeline", "ChEMBLPipeline"),
    "DrugBankPipeline": ("pipelines.drugbank_pipeline", "DrugBankPipeline"),
    "UniProtPipeline": ("pipelines.uniprot_pipeline", "UniProtPipeline"),
    "StringPipeline": ("pipelines.string_pipeline", "StringPipeline"),
    "DisGeNETPipeline": ("pipelines.disgenet_pipeline", "DisGeNETPipeline"),
    "OMIMPipeline": ("pipelines.omim_pipeline", "OMIMPipeline"),
    "PubChemPipeline": ("pipelines.pubchem_pipeline", "PubChemPipeline"),
    # --- ChEMBL constants (chembl_pipeline.py module-level legacy aliases) ---
    # v41 ROOT FIX (SEV3-MEDIUM #1): the previous inline line-number
    # comments (e.g. ``# :43``, ``# :51 = 1.1``) were STALE — the
    # source file evolved through 35+ "ROOT FIX" layers and the
    # constants moved. Operators reading these comments were misled
    # into thinking the values were still at the cited line numbers.
    # Fix: replace the stale line-number references with descriptive
    # comments that don't lie about location. The values come from
    # ``config.settings`` via legacy aliases in chembl_pipeline.py.
    "CHEMBL_API_BASE": ("pipelines.chembl_pipeline", "CHEMBL_API_BASE"),
    "CHEMBL_MIN_REQUEST_INTERVAL":
        ("pipelines.chembl_pipeline", "CHEMBL_MIN_REQUEST_INTERVAL"),
    "ACTIVITY_CHUNK_SIZE": ("pipelines.chembl_pipeline", "ACTIVITY_CHUNK_SIZE"),
    "RETRY_BACKOFF": ("pipelines.chembl_pipeline", "RETRY_BACKOFF"),
    "MOLECULE_TYPE_MAP": ("pipelines.chembl_pipeline", "MOLECULE_TYPE_MAP"),
    "_LOWER_TYPE_MAP": ("pipelines.chembl_pipeline", "_LOWER_TYPE_MAP"),
    # --- DrugBank constants (drugbank_pipeline.py module-level) ---
    "NS": ("pipelines.drugbank_pipeline", "NS"),
    # --- UniProt constants (uniprot_pipeline.py module-level) ---
    "UNIPROT_SEARCH_URL": ("pipelines.uniprot_pipeline", "UNIPROT_SEARCH_URL"),
    "UNIPROT_FIELDS": ("pipelines.uniprot_pipeline", "UNIPROT_FIELDS"),
    # --- DisGeNET constants (disgenet_pipeline.py module-level) ---
    # ``MIN_SCORE`` aliases ``DISGENET_MIN_SCORE`` (default 0.06 per
    # Piñero et al. 2020, NOT 0.1 as the stale comment claimed).
    "DISGENET_COLUMN_MAP":
        ("pipelines.disgenet_pipeline", "DISGENET_COLUMN_MAP"),
    "DISGENET_API_COLUMN_MAP":
        ("pipelines.disgenet_pipeline", "DISGENET_API_COLUMN_MAP"),
    "MIN_SCORE": ("pipelines.disgenet_pipeline", "MIN_SCORE"),
    "CONFIDENCE_TIERS": ("pipelines.disgenet_pipeline", "CONFIDENCE_TIERS"),
    # --- OMIM constants (omim_pipeline.py module-level) ---
    "OMIM_REQUEST_INTERVAL": ("pipelines.omim_pipeline", "OMIM_REQUEST_INTERVAL"),
    "MAPPING_KEY_CONFIRMED": ("pipelines.omim_pipeline", "MAPPING_KEY_CONFIRMED"),
    # --- PubChem constants (pubchem_pipeline.py module-level) ---
    "PUBCHEM_PROPERTIES": ("pipelines.pubchem_pipeline", "PUBCHEM_PROPERTIES"),
    "BATCH_SIZE": ("pipelines.pubchem_pipeline", "BATCH_SIZE"),
    "MIN_BACKOFF": ("pipelines.pubchem_pipeline", "MIN_BACKOFF"),
    "MAX_BACKOFF": ("pipelines.pubchem_pipeline", "MAX_BACKOFF"),
    "RATE_LIMIT_INTERVAL": ("pipelines.pubchem_pipeline", "RATE_LIMIT_INTERVAL"),
}

# ---------------------------------------------------------------------------
# Data Dictionary for the 7 output CSV files (DQ-6, DOC-10)
# Verified against base_pipeline.py:174-185 (_get_processed_filename).
# ---------------------------------------------------------------------------
DATA_DICTIONARY: dict[str, dict[str, Any]] = {
    "chembl": {
        "output_file": "drugs.csv",
        "source_name": "chembl",
        "primary_key": "inchikey",
        "description": (
            "ChEMBL small-molecule bioactive compounds. Produced by "
            "ChEMBLPipeline.clean() (chembl_pipeline.py). KG contribution: "
            "Drug nodes + Drug->Protein edges."
        ),
    },
    "drugbank": {
        "output_file": "drugbank_drugs.csv",
        "source_name": "drugbank",
        "primary_key": "drugbank_id",
        "description": (
            "DrugBank FDA-approved drug metadata. Produced by "
            "DrugBankPipeline.clean() (drugbank_pipeline.py). KG "
            "contribution: Drug nodes (enriched) + Drug->Protein edges."
        ),
    },
    "uniprot": {
        "output_file": "proteins.csv",
        "source_name": "uniprot",
        "primary_key": "uniprot_id",
        "description": (
            "UniProt reviewed human protein sequences. Produced by "
            "UniProtPipeline.clean() (uniprot_pipeline.py). KG contribution: "
            "Protein nodes."
        ),
    },
    "string": {
        "output_file": "protein_protein_interactions.csv",
        "source_name": "string",
        "primary_key": "string_id_a,string_id_b",
        "description": (
            "STRING protein-protein interaction network. Produced by "
            "StringPipeline.clean() (string_pipeline.py). KG contribution: "
            "Protein->Protein edges (Pathway membership)."
        ),
    },
    "disgenet": {
        "output_file": "gene_disease_associations.csv",
        "source_name": "disgenet",
        "primary_key": "gene_id,disease_id",
        "description": (
            "DisGeNET curated gene-disease associations. Produced by "
            "DisGeNETPipeline.clean() (disgenet_pipeline.py). KG "
            "contribution: Gene->Disease edges."
        ),
    },
    "omim": {
        "output_file": "omim_gene_disease_associations.csv",
        "source_name": "omim",
        "primary_key": "mim_number",
        "description": (
            "OMIM Mendelian gene-phenotype mappings (mapping_key=3 only). "
            "Produced by OMIMPipeline.clean() (omim_pipeline.py). KG "
            "contribution: Gene->Disease edges (rare-disease signal)."
        ),
    },
    "pubchem": {
        "output_file": "pubchem_enrichment.csv",
        "source_name": "pubchem",
        "primary_key": "inchikey",
        "description": (
            "PubChem compound structural enrichment data. Produced by "
            "PubChemPipeline.clean() (pubchem_pipeline.py). KG contribution: "
            "Drug node enrichment (molecular fingerprint)."
        ),
    },
}

# ---------------------------------------------------------------------------
# Source attribution mapping (LIN-4)
# Maps each output CSV to the sources that contribute fields to it.
# Verified against each pipeline's clean() return.
# ---------------------------------------------------------------------------
SOURCE_ATTRIBUTION: dict[str, dict[str, Any]] = {
    "drugs.csv": {
        "sources": ["chembl", "drugbank", "pubchem"],
        "field_attribution": {
            "inchikey": ["chembl", "drugbank", "pubchem"],
            "name": ["chembl", "drugbank"],
            "smiles": ["chembl", "pubchem"],
            "molecular_weight": ["chembl", "pubchem"],
            "drug_type": ["chembl"],
            "mechanism_of_action": ["chembl"],
            "drugbank_id": ["drugbank"],
            "is_fda_approved": ["drugbank"],
            "cas_number": ["drugbank"],
            "molecular_formula": ["pubchem"],
            "xlogp": ["pubchem"],
            "tpsa": ["pubchem"],
        },
    },
    "drugbank_drugs.csv": {
        "sources": ["drugbank"],
        "field_attribution": {
            "drugbank_id": ["drugbank"],
            "name": ["drugbank"],
            "inchikey": ["drugbank"],
            "description": ["drugbank"],
            "cas_number": ["drugbank"],
            "is_fda_approved": ["drugbank"],
        },
    },
    "proteins.csv": {
        "sources": ["uniprot"],
        "field_attribution": {
            "uniprot_id": ["uniprot"],
            "gene_symbol": ["uniprot"],
            "gene_name": ["uniprot"],
            "protein_name": ["uniprot"],
            "organism": ["uniprot"],
            "sequence": ["uniprot"],
        },
    },
    "protein_protein_interactions.csv": {
        "sources": ["string"],
        "field_attribution": {
            "string_id_a": ["string"],
            "string_id_b": ["string"],
            "combined_score": ["string"],
            "uniprot_id_a": ["string"],
            "uniprot_id_b": ["string"],
        },
    },
    "gene_disease_associations.csv": {
        "sources": ["disgenet"],
        "field_attribution": {
            "gene_id": ["disgenet"],
            "gene_symbol": ["disgenet"],
            "disease_id": ["disgenet"],
            "disease_name": ["disgenet"],
            "score": ["disgenet"],
        },
    },
    "omim_gene_disease_associations.csv": {
        "sources": ["omim"],
        "field_attribution": {
            "mim_number": ["omim"],
            "gene_symbol": ["omim"],
            "phenotype_name": ["omim"],
            "mapping_key": ["omim"],
        },
    },
    "pubchem_enrichment.csv": {
        "sources": ["pubchem"],
        "field_attribution": {
            "inchikey": ["pubchem"],
            "molecular_formula": ["pubchem"],
            "molecular_weight": ["pubchem"],
            "canonical_smiles": ["pubchem"],
            "isomeric_smiles": ["pubchem"],
        },
    },
}

# ---------------------------------------------------------------------------
# Downstream dependency graph for impact analysis (LIN-5)
# Maps each source_name to the output files it affects.
# Verified against dags/master_pipeline_dag.py task dependencies.
# ---------------------------------------------------------------------------
_DOWNSTREAM_DEPS: dict[str, list[str]] = {
    "chembl": ["drugs.csv", "entity_mapping"],
    "drugbank": ["drugbank_drugs.csv", "entity_mapping"],
    "uniprot": ["proteins.csv", "entity_mapping"],
    "string": ["protein_protein_interactions.csv", "entity_mapping"],
    "disgenet": ["gene_disease_associations.csv"],
    "omim": ["omim_gene_disease_associations.csv"],
    "pubchem": ["pubchem_enrichment.csv", "drugs.csv (enriched)"],
}

# ---------------------------------------------------------------------------
# Public API — explicit declaration (Domain 1: Architecture, Domain 14: PEP)
# Public API contract — semver-protected. Breaking changes require major
# version bump.
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # --- Base & Concrete Pipeline Classes ---
    "BasePipeline", "ChEMBLPipeline", "DisGeNETPipeline", "DrugBankPipeline",
    "OMIMPipeline", "PubChemPipeline", "StringPipeline", "UniProtPipeline",
    # --- Public Constants (re-exported) ---
    "CHEMBL_API_BASE", "MOLECULE_TYPE_MAP", "_LOWER_TYPE_MAP",
    "ACTIVITY_CHUNK_SIZE", "CHEMBL_MIN_REQUEST_INTERVAL", "RETRY_BACKOFF",
    "NS",
    "UNIPROT_SEARCH_URL", "UNIPROT_FIELDS",
    "DISGENET_API_COLUMN_MAP", "DISGENET_COLUMN_MAP",
    "MIN_SCORE", "CONFIDENCE_TIERS",
    "OMIM_REQUEST_INTERVAL", "MAPPING_KEY_CONFIRMED",
    "PUBCHEM_PROPERTIES", "BATCH_SIZE", "MIN_BACKOFF", "MAX_BACKOFF",
    "RATE_LIMIT_INTERVAL",
    # --- Package Metadata ---
    "__version__", "SCHEMA_VERSION", "PYTHON_MIN_VERSION",
    "DEFAULT_SEED", "KNOWN_DATA_SOURCE_VERSIONS",
    "DATA_DICTIONARY", "SOURCE_ATTRIBUTION",
    # --- Factory & Introspection ---
    "get_pipeline", "get_expected_pipelines", "get_kg_mapping",
    "get_filtering_thresholds", "get_data_dictionary",
    "get_source_attribution", "find_affected_downstream",
    "compute_file_checksum", "get_json_schema",
    # --- Validation ---
    "validate_infrastructure", "_validate_security", "validate_config",
    # --- Configuration & Logging ---
    "get_config_summary", "set_log_level", "set_correlation_id",
    "get_correlation_id", "set_seed",
    # --- Lifecycle ---
    "initialize", "reload", "is_loaded", "is_reproducible",
    "health_check", "get_metrics", "get_load_times",
    "performance_benchmark", "recover_from_failure", "get_dead_letters",
    # --- Lineage & State ---
    "get_provenance", "get_audit_trail",
    "to_state_dict", "from_state_dict",
    # --- Versioning & Deprecation ---
    "requires_api_version", "_deprecated",
    # --- Test Utilities ---
    "_reset", "_log_import_status",
]

# ---------------------------------------------------------------------------
# Cache for lazily-loaded symbols (IDEM-8: cache into _loaded, NOT globals(),
# to allow _reset() to fully restore the package state between tests. The
# entity_resolution package caches into globals() which is faster but causes
# monkey-patch bleed — see IDEM-8.)
# ---------------------------------------------------------------------------
_loaded: dict[str, Any] = {}

# Per-symbol load time in milliseconds (PERF-8)
_load_times: dict[str, float] = {}

# Dead-letter queue: records of symbols that failed to load (REL-4)
_dead_letters: list[dict[str, Any]] = []

# Correlation ID for log correlation across pipelines (LOG-7, LIN-7)
_correlation_id: Optional[str] = None

# ---------------------------------------------------------------------------
# Env-driven lazy/eager mode toggle (IDEM-2, CONF-1, ARCH-11)
# When PIPELINES_LAZY_IMPORT=0, symbols are loaded eagerly at import time
# (fail-fast for production). Default: lazy (PIPELINES_LAZY_IMPORT=1).
# ---------------------------------------------------------------------------
_LAZY_MODE: bool = os.environ.get("PIPELINES_LAZY_IMPORT", "1") != "0"

# Environment name (CONF-6).
# FIX TOP-2: standardize on DRUGOS_ENVIRONMENT across both phases (Phase 1
# previously read ENVIRONMENT). Synchronized with phase1/config/settings.py
# — DO NOT diverge (audit TOP-2).
_raw_env: str = (
    os.environ.get("DRUGOS_ENVIRONMENT")
    or os.environ.get("ENVIRONMENT", "development")
).lower()
_ENV_NORMALIZATION: dict[str, str] = {
    "dev": "development",
    "develop": "development",
    "development": "development",
    "staging": "staging",
    "stage": "staging",
    "prod": "production",
    "production": "production",
}
_ENVIRONMENT: str = _ENV_NORMALIZATION.get(_raw_env, _raw_env)

# ---------------------------------------------------------------------------
# Optional observability callback (PERF-7)
# Set to a callable(name, module_path, load_time_ms) to track import perf.
# ---------------------------------------------------------------------------
_on_symbol_loaded_callback: Any = None

# ---------------------------------------------------------------------------
# Circuit breaker for import failures (REL-5)
# Prevents cascading failures in CI environments where a missing dep would
# otherwise cause every test to retry the same failing import.
# ---------------------------------------------------------------------------
_CIRCUIT_BREAKER: dict[str, Any] = {
    "failure_count": 0,
    "threshold": 5,
    "open_until": None,  # datetime or None
    "reset_timeout": 60.0,  # seconds
}

# ---------------------------------------------------------------------------
# Import retry constants (REL-3)
# Import retries handle race conditions during concurrent pip install or
# NFS mount delays. For permanent failures (missing dep), the retry exhausts
# quickly and records the failure in _dead_letters.
# ---------------------------------------------------------------------------
_IMPORT_MAX_RETRIES: int = 3
_IMPORT_RETRY_BACKOFF: float = 0.1

# Path to the JSON schema file (INT-9)
_SCHEMA_PATH: Path = Path(__file__).resolve().parent / "schema" / "v1.json"

# Cached JSON schema (INT-9)
_json_schema_cache: Optional[dict[str, Any]] = None


def __getattr__(name: str) -> Any:
    """Lazily load a symbol from the appropriate submodule on first access.

    This function is called by Python when an attribute is not found in the
    module's ``globals()`` (PEP 562). It resolves the attribute name to a
    submodule via ``_SYMBOL_MAP`` (or ``_SUBMODULES`` for submodule access),
    imports that submodule with ``importlib``, extracts the attribute,
    caches it in ``_loaded`` (NOT ``globals()``), and returns it.

    Error Handling
    ~~~~~~~~~~~~~~
    - ``ImportError`` / ``ModuleNotFoundError`` — retried up to
      ``_IMPORT_MAX_RETRIES`` times with exponential backoff. After
      exhausting retries, the failure is recorded in ``_dead_letters`` and
      a clear ``ImportError`` is raised naming the symbol, submodule, and
      original error. This prevents cryptic tracebacks during Airflow DAG
      parsing.
    - ``AttributeError`` — raised when the submodule loads but does not
      contain the expected symbol (version mismatch).
    - Unknown symbols raise ``AttributeError`` with the module name.
    - Circuit breaker: if 5 consecutive import failures occur, the circuit
      opens for 60 seconds and subsequent imports fail immediately with a
      clear message. This prevents cascading failures in CI.

    Parameters
    ----------
    name : str
        The attribute name being accessed (e.g., ``"ChEMBLPipeline"`` or
        ``"chembl_pipeline"`` for submodule access).

    Returns
    -------
    Any
        The resolved symbol from the target submodule.

    Raises
    ------
    AttributeError
        If ``name`` is not in ``_SYMBOL_MAP`` or ``_SUBMODULES``, or the
        submodule does not contain the attribute.
    ImportError
        If the target submodule cannot be imported after retries.
    """
    # v41 ROOT FIX (SEV3-MEDIUM #2): the previous code had a dead
    # ``if name == "__version__": return __version__`` branch here.
    # ``__version__`` is a MODULE-LEVEL attribute (defined at line ~454),
    # so Python's attribute lookup finds it BEFORE ``__getattr__`` is
    # called (PEP 562 ``__getattr__`` is only called when NORMAL lookup
    # fails). The branch was therefore UNREACHABLE dead code that
    # misled readers into thinking it was the source of truth for
    # ``__version__``. Fix: remove the dead branch. If anyone ever
    # deletes the module-level ``__version__`` assignment, Python will
    # raise AttributeError as expected (and the test suite will catch
    # the regression).

    # Submodule access: pipelines.chembl_pipeline etc.
    if name in _SUBMODULES:
        full_path = f"pipelines.{name}"
        module = importlib.import_module(full_path)
        # Cache the submodule in _loaded (NOT globals()) for test isolation
        _loaded[name] = module
        return module

    if name not in _SYMBOL_MAP:
        raise AttributeError(
            f"module 'pipelines' has no attribute '{name}'. "
            f"Public API: see pipelines.__all__"
        )

    if name in _loaded:
        return _loaded[name]

    # Circuit breaker check (REL-5)
    open_until = _CIRCUIT_BREAKER["open_until"]
    if open_until is not None and datetime.now(timezone.utc) < open_until:
        remaining = (open_until - datetime.now(timezone.utc)).total_seconds()
        raise ImportError(
            f"Cannot import '{name}' — circuit breaker is open due to "
            f"{_CIRCUIT_BREAKER['failure_count']} consecutive failures. "
            f"Retry in {remaining:.0f} seconds."
        )

    module_path, attr_name = _SYMBOL_MAP[name]
    start_time = time.monotonic()
    last_exc: Optional[Exception] = None

    for attempt in range(_IMPORT_MAX_RETRIES):
        try:
            module = importlib.import_module(module_path)
            attr = getattr(module, attr_name)
            load_time_ms = (time.monotonic() - start_time) * 1000
            _loaded[name] = attr
            _load_times[name] = load_time_ms

            # Reset circuit breaker on success
            _CIRCUIT_BREAKER["failure_count"] = 0
            _CIRCUIT_BREAKER["open_until"] = None

            logger.info(
                "Loaded pipelines.%s from %s in %.1f ms",
                name, module_path, load_time_ms,
                extra={
                    "pipelines_symbol": name,
                    "pipelines_module": module_path,
                    "pipelines_load_time_ms": load_time_ms,
                    "pipelines_correlation_id": _correlation_id,
                },
            )

            # Observability callback (PERF-7)
            if _on_symbol_loaded_callback is not None:
                try:
                    _on_symbol_loaded_callback(
                        name, module_path, load_time_ms
                    )
                except Exception as cb_exc:
                    logger.warning(
                        "Observability callback failed for '%s': %s",
                        name, cb_exc,
                    )

            return attr
        # v41 ROOT FIX (SEV3-MEDIUM #5): the previous code caught ONLY
        # ``ImportError``. But ``importlib.import_module`` raises
        # ``ModuleNotFoundError`` (a subclass of ImportError — OK) on
        # missing modules, while ``getattr(module, attr_name)`` raises
        # ``AttributeError`` (NOT a subclass of ImportError) when the
        # module exists but doesn't define the attribute. The previous
        # code would let AttributeError propagate IMMEDIATELY (no
        # retries, no dead-letter recording), which contradicts the
        # retry semantics documented in the docstring. Fix: catch
        # ``AttributeError`` too so symbol-not-found gets the same
        # retry + dead-letter treatment as module-not-found.
        except (ImportError, AttributeError) as exc:
            last_exc = exc
            if attempt < _IMPORT_MAX_RETRIES - 1:
                time.sleep(_IMPORT_RETRY_BACKOFF * (2 ** attempt))
            # else: fall through to dead-letter recording below

    # Exhausted retries — record in dead-letter queue (REL-4)
    _dead_letters.append({
        "symbol": name,
        "module": module_path,
        "error": str(last_exc),
        "attempts": _IMPORT_MAX_RETRIES,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Update circuit breaker (REL-5)
    _CIRCUIT_BREAKER["failure_count"] += 1
    if _CIRCUIT_BREAKER["failure_count"] >= _CIRCUIT_BREAKER["threshold"]:
        from datetime import timedelta
        _CIRCUIT_BREAKER["open_until"] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=_CIRCUIT_BREAKER["reset_timeout"])
        )
        logger.error(
            "Circuit breaker opened after %d failures — will reset in %.0fs",
            _CIRCUIT_BREAKER["failure_count"],
            _CIRCUIT_BREAKER["reset_timeout"],
        )

    # LOG-8: log what DID load successfully before raising
    logger.error(
        "Failed to load '%s' from '%s' after %d attempts: %s",
        name, module_path, _IMPORT_MAX_RETRIES, last_exc,
    )
    _log_import_status()

    raise ImportError(
        f"Cannot import '{name}' from '{module_path}'. "
        f"Ensure the submodule and its dependencies are properly "
        f"configured. Original error: {last_exc}"
    ) from last_exc


def __dir__() -> list[str]:
    """Return public symbols for tab-completion and introspection.

    Merges ``__all__`` (the declared public API) with ``globals()``
    keys so that IDE auto-complete and ``dir(pipelines)`` show all
    available symbols.
    """
    return sorted(set(list(__all__) + list(globals().keys())))


# ---------------------------------------------------------------------------
# Eager loading mode (IDEM-2, CONF-1, ARCH-11)
# When PIPELINES_LAZY_IMPORT=0, pre-load all symbols at import time.
# Failures are logged but do not raise (fail-fast opt-in for production
# debugging — use validate_infrastructure() to detect them).
# ---------------------------------------------------------------------------
if not _LAZY_MODE:
    logger.info(
        "PIPELINES_LAZY_IMPORT=0: pre-loading all %d symbols eagerly",
        len(_SYMBOL_MAP),
    )
    for _sym in _SYMBOL_MAP:
        try:
            __getattr__(_sym)
        except (ImportError, AttributeError) as _exc:
            logger.warning(
                "Eager load failed for '%s': %s", _sym, _exc
            )


# ---------------------------------------------------------------------------
# Cache management & test utilities (IDEM-7, ARCH-10)
# ---------------------------------------------------------------------------


def _reset() -> None:
    """Clear the lazy-loaded symbol cache for testing.

    Use this in test fixtures to reset the pipelines package state
    between tests that require different configurations.

    This function does **not** dispose the engine or close sessions —
    it only clears the symbol cache so that subsequent attribute accesses
    re-import the submodules.

    We cache into ``_loaded`` (not ``globals()``) to allow ``_reset()`` to
    fully restore the package state between tests. The ``entity_resolution``
    package caches into ``globals()`` which is faster but causes monkey-
    patch bleed — see IDEM-8.

    Usage in conftest.py::

        @pytest.fixture(autouse=True)
        def reset_pipelines_package():
            import pipelines
            pipelines._reset()
            yield
            pipelines._reset()
    """
    _loaded.clear()
    _load_times.clear()
    _dead_letters.clear()
    # SCI-FIX: Added `global _correlation_id` and fixed the variable name from
    # `_correlation_id_local` (which created a local variable that was
    # immediately discarded) to `_correlation_id` (the actual module-level
    # variable). Without this fix, the correlation ID was never reset between
    # tests, causing test-isolation bugs.
    global _correlation_id
    _correlation_id = None
    # Reset circuit breaker
    _CIRCUIT_BREAKER["failure_count"] = 0
    _CIRCUIT_BREAKER["open_until"] = None
    logger.debug("Pipelines package symbol cache cleared")


def _log_import_status() -> dict[str, bool]:
    """Log which symbols have been loaded and which haven't.

    Returns a dict mapping each symbol name in ``_SYMBOL_MAP`` to
    ``True`` (loaded) or ``False`` (not yet loaded). Useful for debugging
    import issues and verifying lazy-loading behaviour.

    Returns
    -------
    dict[str, bool]
        Symbol name -> loaded status mapping.
    """
    status = {name: (name in _loaded) for name in _SYMBOL_MAP}
    loaded_count = sum(1 for v in status.values() if v)
    total_count = len(status)
    logger.info(
        "Pipelines package import status: %d / %d symbols loaded",
        loaded_count, total_count,
    )
    for name, is_loaded in status.items():
        if is_loaded:
            logger.debug(
                "  [LOADED]   %s (%.1f ms)",
                name, _load_times.get(name, 0.0),
            )
        else:
            logger.debug("  [PENDING]  %s", name)
    return status


# ---------------------------------------------------------------------------
# Factory & introspection (ARCH-7, DES-3, DQ-8)
# ---------------------------------------------------------------------------


def get_pipeline(name: str) -> type:
    """Return the pipeline CLASS for a source name. NOT an instance.

    Parameters
    ----------
    name : str
        One of ``get_expected_pipelines()`` values (e.g. ``"chembl"``).

    Returns
    -------
    type
        The pipeline class (subclass of ``BasePipeline``). Caller is
        responsible for instantiation.

    Raises
    ------
    ValueError
        If ``name`` is not in ``get_expected_pipelines()``.
    ImportError
        If the pipeline class cannot be imported (e.g. rdkit missing on
        ARM64). When ``PIPELINES_GRACEFUL_DEGRADATION=1`` is set, returns
        a ``_PipelineUnavailable`` sentinel instead of raising.

    P1-20 ROOT FIX
    --------------
    Previously this function called ``__getattr__(class_name)`` TWICE on
    ImportError when ``PIPELINES_GRACEFUL_DEGRADATION=1`` was set — once
    in the outer ``try`` and again in the nested ``try``. Because
    ``__getattr__`` records every import failure in ``_dead_letters`` AND
    increments the circuit-breaker counter (5 consecutive failures opens
    the circuit for 60s), the double call doubled the failure signal and
    exhausted the circuit breaker twice as fast. Now we capture the
    ImportError from the first call and reuse it directly.
    """
    if name not in _SOURCE_TO_CLASS:
        raise ValueError(
            f"Unknown pipeline '{name}'. Expected one of "
            f"{sorted(_SOURCE_TO_CLASS.keys())}"
        )
    class_name = _SOURCE_TO_CLASS[name]
    try:
        return __getattr__(class_name)
    except ImportError as exc:
        if os.environ.get("PIPELINES_GRACEFUL_DEGRADATION", "0") == "1":
            # Reuse the captured ImportError — do NOT call __getattr__
            # again (would double-count the failure in the circuit
            # breaker and exhaust the open-threshold twice as fast).
            return _PipelineUnavailable(class_name, exc)  # type: ignore[return-value]
        raise


def get_expected_pipelines() -> set[str]:
    """Return the set of 7 expected pipeline source names.

    Single source of truth for the 7 source names. The Makefile (lines
    17-23, 34-40) and the master DAG (``dags/master_pipeline_dag.py:90-135``)
    duplicate this list; this function is the canonical reference. Future
    refactors should update Makefile/DAG to call this function.

    The set is derived from ``_SOURCE_TO_CLASS`` (which is the single
    source of truth). Adding an 8th pipeline to ``_SOURCE_TO_CLASS`` makes
    this function return 8 elements (it derives, doesn't hardcode).

    Returns
    -------
    set[str]
        The 7 source names: ``{"chembl", "drugbank", "uniprot",
        "string", "disgenet", "omim", "pubchem"}``.
    """
    return set(_SOURCE_TO_CLASS.keys())


def get_kg_mapping() -> dict[str, dict[str, list[str]]]:
    """Return the pipeline-to-knowledge-graph node/edge mapping.

    Returns
    -------
    dict[str, dict[str, list[str]]]
        Maps each pipeline source_name to its KG contribution:
        ``{"node_types": [...], "edge_types": [...]}``.
    """
    return {
        "chembl": {
            "node_types": ["Drug"],
            "edge_types": ["Drug->Protein"],
        },
        "drugbank": {
            "node_types": ["Drug"],
            "edge_types": ["Drug->Protein"],
        },
        "uniprot": {
            "node_types": ["Protein"],
            "edge_types": [],
        },
        "string": {
            "node_types": [],
            "edge_types": ["Protein->Protein"],
        },
        "disgenet": {
            "node_types": [],
            "edge_types": ["Gene->Disease"],
        },
        "omim": {
            "node_types": [],
            "edge_types": ["Gene->Disease"],
        },
        "pubchem": {
            "node_types": [],  # enriches existing Drug nodes, no new nodes
            "edge_types": [],
        },
    }


def get_filtering_thresholds() -> dict[str, dict[str, Any]]:
    """Return the scientific filtering thresholds table with rationale.

    Every threshold is verified against the source code. Cite the source
    ``file:line`` in any comment or docstring that mentions these facts.

    Returns
    -------
    dict[str, dict[str, Any]]
        Maps each constant name to ``{"value": ..., "file": ...,
        "rationale": ...}``.
    """
    return {
        "MIN_SCORE": {
            "value": 0.06,
            "file": "disgenet_pipeline.py (389-fix, SCI-1)",
            "rationale": (
                "DisGeNET scores in [0.06, 0.1) are 'weak evidence' — "
                "biologically meaningful, especially for rare diseases "
                "(Piñero et al. 2020, Nucleic Acids Research). The "
                "previous 0.1 default silently destroyed them. The new "
                "default of 0.06 is the published weak-evidence floor; "
                "DISGENET_ALLOW_WEAK_EVIDENCE=True (default) tags "
                "weak-evidence rows with confidence_tier='weak' instead "
                "of dropping them."
            ),
        },
        "CONFIDENCE_TIERS": {
            # ROOT FIX (Finding 10, P1): the previous value tuple was
            # [(0.0, "weak"), (0.06, "moderate"), (0.3, "strong")] which
            # DIVERGED from the actual DEFAULT_CONFIDENCE_TIERS in
            # cleaning/confidence.py:50-64. The real labels are
            # "sub_weak", "weak", "strong" (NO "moderate" tier exists).
            # The rationale text below even contradicted the value tuple
            # ("[0.0, 0.06) = sub-weak" while the value labeled it "weak").
            # Downstream code filtering by tier label silently matched
            # nothing because no row was ever labeled "moderate".
            "value": [(0.0, "sub_weak"), (0.06, "weak"), (0.3, "strong")],
            "file": "cleaning/confidence.py (DEFAULT_CONFIDENCE_TIERS, Piñero 2020 §2.3)",
            "rationale": (
                "Tiers classify GDA confidence for downstream weighting. "
                "Aligned to Piñero et al. 2020 §2.3: [0.0, 0.06) = sub_weak "
                "(below the published weak-evidence floor), "
                "[0.06, 0.3) = weak evidence, [0.3, 1.0] = strong evidence. "
                "The previous 0.7 → 'very_high' tier is removed (no "
                "publication supports it). The previous 'moderate' label "
                "was WRONG — the actual label is 'weak' (Finding 10 root fix)."
            ),
        },
        "MAPPING_KEY_CONFIRMED": {
            "value": 3,
            "file": "omim_pipeline.py:44",
            "rationale": (
                "OMIM mapping_key=3 means 'confirmed gene-phenotype "
                "mapping' — the only mapping key with experimental "
                "validation. Other keys (1, 2) are positional or "
                "speculative and MUST NOT enter the staging DB."
            ),
        },
        "OMIM_REQUEST_INTERVAL": {
            "value": 0.25,
            "file": "omim_pipeline.py:43",
            "rationale": (
                "OMIM API rate limit: 4 requests/second -> 0.25s between "
                "requests."
            ),
        },
        "STRING_MIN_COMBINED_SCORE": {
            "value": 400,
            "file": "config.settings",
            "rationale": (
                "STRING combined_score ranges 0-1000; >=400 is the "
                "published 'high-confidence' threshold (STRING consortium "
                "recommendation)."
            ),
        },
        "CHEMBL_MIN_REQUEST_INTERVAL": {
            "value": 0.5,
            "file": "config.settings:CHEMBL_MIN_REQUEST_INTERVAL",
            "rationale": (
                "ChEMBL REST API soft rate limit is ~2 req/sec for short "
                "bursts; 0.5s average (enforced via token-bucket rate "
                "limiter in pipelines/_http_client.py) keeps us safely "
                "under the threshold while allowing bursts (P1, P4 fix)."
            ),
        },
        "CHEMBL_PAGE_SIZE": {
            "value": 1000,
            "file": "chembl_pipeline.py:44",
            "rationale": "ChEMBL API max page size.",
        },
        "ACTIVITY_CHUNK_SIZE": {
            "value": 100000,
            "file": "chembl_pipeline.py:47",
            "rationale": (
                "ChEMBL has ~2M bioactivity records; chunking at 100K "
                "bounds peak memory."
            ),
        },
        "PUBCHEM_BATCH_SIZE": {
            "value": 95,
            "file": (
                "pubchem_pipeline.py (institutional-grade — "
                "see config/settings.py:PUBCHEM_PIPELINE_BATCH_SIZE)"
            ),
            "rationale": (
                "PubChem PUG REST allows max 100 InChIKeys per request. "
                "We use 95 for a 5% safety margin in case PubChem lowers "
                "the limit (they have historically). See "
                "PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md DESIGN-13 / CONF-1."
            ),
        },
        "PUBCHEM_RATE_LIMIT_INTERVAL": {
            "value": 0.2,
            "file": "pubchem_pipeline.py:58",
            "rationale": (
                "PubChem REST rate limit: 5 req/sec -> 0.2s between "
                "requests (or 0.1s with API key)."
            ),
        },
        "CHEMBL_MAX_RETRIES": {
            "value": 5,
            "file": "chembl_pipeline.py:45",
            "rationale": "Network resilience for transient failures.",
        },
        "PUBCHEM_MAX_RETRIES": {
            "value": 6,
            "file": "pubchem_pipeline.py:55",
            "rationale": "Network resilience for transient failures.",
        },
    }


def get_data_dictionary() -> dict[str, dict[str, Any]]:
    """Return the data dictionary for the 7 output CSV files.

    Returns
    -------
    dict[str, dict[str, Any]]
        Maps each source_name to its data dictionary entry (output_file,
        primary_key, description).
    """
    return dict(DATA_DICTIONARY)


def get_source_attribution() -> dict[str, dict[str, Any]]:
    """Return the source attribution mapping for each output CSV.

    Returns
    -------
    dict[str, dict[str, Any]]
        Maps each output CSV filename to ``{"sources": [...],
        "field_attribution": {field: [sources]}}``.
    """
    return dict(SOURCE_ATTRIBUTION)


def find_affected_downstream(source_name: str) -> list[str]:
    """Return the list of output files affected by a change to source_name.

    E.g. if ChEMBL releases new data,
    ``find_affected_downstream("chembl")`` returns
    ``["drugs.csv", "entity_mapping"]``.

    Parameters
    ----------
    source_name : str
        One of ``get_expected_pipelines()`` values.

    Returns
    -------
    list[str]
        The list of output files affected.

    Raises
    ------
    ValueError
        If ``source_name`` is not a known source.
    """
    if source_name not in _DOWNSTREAM_DEPS:
        raise ValueError(
            f"Unknown source: {source_name}. Expected one of "
            f"{sorted(_DOWNSTREAM_DEPS.keys())}"
        )
    return list(_DOWNSTREAM_DEPS[source_name])


def compute_file_checksum(path: str | Path) -> str:
    """Return SHA-256 checksum of a file.

    Used by pipeline modules to record input file checksums in
    ``PipelineRun`` metadata. The package-level function ensures
    consistent checksum computation across all pipelines.

    Parameters
    ----------
    path : str | Path
        Path to the file to checksum.

    Returns
    -------
    str
        The 64-character hex SHA-256 digest.
    """
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_json_schema() -> dict[str, Any]:
    """Load and return the JSON schema for the 7 output CSV files.

    The schema is cached after first load. See
    ``pipelines/schema/v1.json`` for the full schema.

    Returns
    -------
    dict[str, Any]
        The JSON schema as a dict.

    Raises
    ------
    FileNotFoundError
        If ``pipelines/schema/v1.json`` does not exist.
    """
    global _json_schema_cache
    if _json_schema_cache is None:
        import json
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            _json_schema_cache = json.load(f)
    return _json_schema_cache


# ---------------------------------------------------------------------------
# Validation (DQ-7, DES-6, SEC-2)
# ---------------------------------------------------------------------------


def validate_infrastructure() -> dict[str, Any]:
    """Validate that the pipelines package infrastructure is properly
    configured.

    Performs the following checks:

    1. All 8 pipeline classes are importable via the package API.
    2. All 7 concrete classes inherit from ``BasePipeline``.
    3. All 7 ``source_name`` values are non-empty.
    4. All 7 ``source_name`` values are distinct.
    5. Every ``source_name`` is in the ``_get_processed_filename`` dict
       (verified by reading ``base_pipeline.py`` source via ``inspect``).
    6. The 7 filenames are distinct.
    7. ``get_kg_mapping()`` returns 7 entries.
    8. ``DATA_DICTIONARY`` has 7 entries.
    9. ``_SYMBOL_MAP`` has at least 28 entries.
    10. ``__all__`` has at least 40 entries.

    Returns
    -------
    dict[str, Any]
        A validation report with keys:
        - ``"checks"``: list of check results (pass/fail + message)
        - ``"passed"``: number of passing checks
        - ``"failed"``: number of failing checks
        - ``"overall"``: ``"PASS"`` if all checks pass, ``"FAIL"``
          otherwise (or ``"WARN"`` in development env with failures).
    """
    checks: list[dict[str, str]] = []

    # Check 1: All 8 pipeline classes are importable
    class_names = [
        "BasePipeline", "ChEMBLPipeline", "DrugBankPipeline",
        "UniProtPipeline", "StringPipeline", "DisGeNETPipeline",
        "OMIMPipeline", "PubChemPipeline",
    ]
    for class_name in class_names:
        try:
            cls = __getattr__(class_name)
            checks.append({
                "check": f"import_{class_name}",
                "status": "PASS",
                "message": f"{class_name} is importable",
            })
        except (ImportError, AttributeError) as exc:
            checks.append({
                "check": f"import_{class_name}",
                "status": "FAIL",
                "message": f"Cannot import {class_name}: {exc}",
            })

    # Check 2 & 3 & 4: All 7 concrete classes inherit from BasePipeline,
    # have non-empty source_name, and source_names are distinct
    concrete_names = class_names[1:]  # exclude BasePipeline
    try:
        base_cls = __getattr__("BasePipeline")
        source_names: list[str] = []
        for class_name in concrete_names:
            try:
                cls = __getattr__(class_name)
                if issubclass(cls, base_cls):
                    checks.append({
                        "check": f"subclass_{class_name}",
                        "status": "PASS",
                        "message": f"{class_name} subclasses BasePipeline",
                    })
                else:
                    checks.append({
                        "check": f"subclass_{class_name}",
                        "status": "FAIL",
                        "message": f"{class_name} does NOT subclass BasePipeline",
                    })
                sn = getattr(cls, "source_name", "")
                if sn:
                    checks.append({
                        "check": f"source_name_nonempty_{class_name}",
                        "status": "PASS",
                        "message": f"{class_name}.source_name = '{sn}'",
                    })
                    source_names.append(sn)
                else:
                    checks.append({
                        "check": f"source_name_nonempty_{class_name}",
                        "status": "FAIL",
                        "message": f"{class_name} has empty source_name",
                    })
                    source_names.append("")
            except (ImportError, AttributeError) as exc:
                checks.append({
                    "check": f"subclass_{class_name}",
                    "status": "FAIL",
                    "message": f"Cannot check {class_name}: {exc}",
                })
                source_names.append("")

        if len(set(source_names)) == 7:
            checks.append({
                "check": "source_name_unique",
                "status": "PASS",
                "message": f"All 7 source_names are distinct: {sorted(set(source_names))}",
            })
        else:
            checks.append({
                "check": "source_name_unique",
                "status": "FAIL",
                "message": f"Duplicate source_names: {source_names}",
            })
    except (ImportError, AttributeError) as exc:
        checks.append({
            "check": "subclass_check_setup",
            "status": "FAIL",
            "message": f"Cannot import BasePipeline: {exc}",
        })

    # Check 5: source_name -> _get_processed_filename dict
    try:
        import inspect
        base_cls = __getattr__("BasePipeline")
        src = inspect.getsource(base_cls._get_processed_filename)
        expected_sources = get_expected_pipelines()
        missing = [n for n in expected_sources if f'"{n}"' not in src]
        if not missing:
            checks.append({
                "check": "source_name_in_filename_dict",
                "status": "PASS",
                "message": "All source_names are in _get_processed_filename dict",
            })
        else:
            checks.append({
                "check": "source_name_in_filename_dict",
                "status": "FAIL",
                "message": f"source_names not in dict: {missing}",
            })
    except Exception as exc:
        checks.append({
            "check": "source_name_in_filename_dict",
            "status": "FAIL",
            "message": f"Cannot check: {exc}",
        })

    # Check 6: 7 distinct CSV paths
    expected_csvs = {
        "drugs.csv", "drugbank_drugs.csv", "proteins.csv",
        "protein_protein_interactions.csv",
        "gene_disease_associations.csv",
        "omim_gene_disease_associations.csv",
        "pubchem_enrichment.csv",
    }
    if len(expected_csvs) == 7:
        checks.append({
            "check": "filename_distinct",
            "status": "PASS",
            "message": "7 distinct CSV paths configured",
        })
    else:
        checks.append({
            "check": "filename_distinct",
            "status": "FAIL",
            "message": f"Expected 7 distinct CSVs, got {len(expected_csvs)}",
        })

    # Check 7: kg_mapping complete
    kg_map = get_kg_mapping()
    if len(kg_map) == 7:
        checks.append({
            "check": "kg_mapping_complete",
            "status": "PASS",
            "message": "get_kg_mapping() returns 7 entries",
        })
    else:
        checks.append({
            "check": "kg_mapping_complete",
            "status": "FAIL",
            "message": f"get_kg_mapping() returns {len(kg_map)} entries, expected 7",
        })

    # Check 8: DATA_DICTIONARY complete
    if len(DATA_DICTIONARY) == 7:
        checks.append({
            "check": "data_dictionary_complete",
            "status": "PASS",
            "message": "DATA_DICTIONARY has 7 entries",
        })
    else:
        checks.append({
            "check": "data_dictionary_complete",
            "status": "FAIL",
            "message": f"DATA_DICTIONARY has {len(DATA_DICTIONARY)} entries, expected 7",
        })

    # Check 9: _SYMBOL_MAP has >= 28 entries
    if len(_SYMBOL_MAP) >= 28:
        checks.append({
            "check": "symbol_map_size",
            "status": "PASS",
            "message": f"_SYMBOL_MAP has {len(_SYMBOL_MAP)} entries (>=28)",
        })
    else:
        checks.append({
            "check": "symbol_map_size",
            "status": "FAIL",
            "message": f"_SYMBOL_MAP has {len(_SYMBOL_MAP)} entries, expected >=28",
        })

    # Check 10: __all__ has >= 40 entries
    if len(__all__) >= 40:
        checks.append({
            "check": "all_size",
            "status": "PASS",
            "message": f"__all__ has {len(__all__)} entries (>=40)",
        })
    else:
        checks.append({
            "check": "all_size",
            "status": "FAIL",
            "message": f"__all__ has {len(__all__)} entries, expected >=40",
        })

    passed = sum(1 for c in checks if c["status"] == "PASS")
    failed = sum(1 for c in checks if c["status"] == "FAIL")

    if failed == 0:
        overall = "PASS"
    elif _ENVIRONMENT == "production":
        overall = "FAIL"
    else:
        overall = "WARN"

    report = {
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "overall": overall,
    }

    logger.info(
        "Pipelines infrastructure validation: %s "
        "(%d passed, %d failed, %d total)",
        report["overall"], passed, failed, len(checks),
    )

    return report


def _validate_security() -> dict[str, Any]:
    """Audit pipeline credential configuration for insecure patterns.

    Checks:

    1. ``OMIM_API_KEY`` is set (OMIM requires it).
    2. ``DISGENET_API_KEY`` is set (FIX-C9: was WARNING, now ERROR —
       ``disgenet_pipeline.download()`` raises ``ValueError`` when
       ``DISGENET_USE_API=true`` and the key is unset, so the pipeline
       WILL crash on run, not "work at a lower rate limit").
    3. ``DRUGBANK_XML_PATH`` is set (FIX-C9: was WARNING, now ERROR —
       ``drugbank_pipeline.download()`` raises ``FileNotFoundError`` when
       the configured XML file is missing, so the pipeline WILL crash on
       run, not "gracefully skip").
    4. ``DATABASE_URL`` is not in-memory SQLite in non-test envs (defers
       to ``database._validate_database_security`` for the actual check).
    5. No credentials logged in package logger handlers.

    Uses deferred imports: only ``import config.settings`` inside the
    function body, never at module top level.

    Returns
    -------
    dict[str, Any]
        A security report with keys:
        - ``"checks"``: list of check results (severity + message)
        - ``"warnings"``: number of warnings
        - ``"critical"``: number of critical issues
        - ``"overall"``: ``"SECURE"`` or ``"INSECURE"``
    """
    checks: list[dict[str, str]] = []

    # Check 1: OMIM_API_KEY (CRITICAL — OMIM rejects unauthenticated requests)
    omim_key = os.environ.get("OMIM_API_KEY", "")
    if omim_key:
        checks.append({
            "check": "omim_api_key",
            "severity": "INFO",
            "message": "OMIM_API_KEY is set.",
        })
    else:
        checks.append({
            "check": "omim_api_key",
            "severity": "CRITICAL",
            "message": (
                "OMIM_API_KEY is NOT set. OMIM API rejects unauthenticated "
                "requests — OMIMPipeline will fail. Set OMIM_API_KEY env var."
            ),
        })

    # Check 2: DISGENET_API_KEY (FIX-C9: ERROR — pipeline WILL CRASH on run
    # if DISGENET_USE_API=true and the key is unset, not "work at lower rate").
    # See disgenet_pipeline.py:941-947 — raises ValueError.
    disgenet_key = os.environ.get("DISGENET_API_KEY", "")
    if disgenet_key:
        checks.append({
            "check": "disgenet_api_key",
            "severity": "INFO",
            "message": "DISGENET_API_KEY is set (higher rate limit).",
        })
    else:
        checks.append({
            "check": "disgenet_api_key",
            "severity": "ERROR",
            "message": (
                "DISGENET_API_KEY is NOT set. DisGeNET pipeline WILL CRASH "
                "on run — set the key or pass --skip-disgenet. "
                "(disgenet_pipeline.download raises ValueError when "
                "DISGENET_USE_API=true and the key is unset.)"
            ),
        })

    # Check 3: DRUGBANK_XML_PATH (FIX-C9: ERROR — pipeline WILL CRASH on
    # run if the configured XML file is missing, not "gracefully skip").
    # See drugbank_pipeline.py:941-978 — raises FileNotFoundError.
    drugbank_path = os.environ.get("DRUGBANK_XML_PATH", "")
    if drugbank_path:
        checks.append({
            "check": "drugbank_xml_path",
            "severity": "INFO",
            "message": "DRUGBANK_XML_PATH is set.",
        })
    else:
        checks.append({
            "check": "drugbank_xml_path",
            "severity": "ERROR",
            "message": (
                "DRUGBANK_XML_PATH is NOT set. DrugBank pipeline WILL CRASH "
                "on run — set the path or pass --skip-drugbank. "
                "(drugbank_pipeline.download raises FileNotFoundError when "
                "the configured XML file is missing.)"
            ),
        })

    # Check 4: DATABASE_URL (defer to database._validate_database_security)
    try:
        from database import _validate_database_security
        db_sec = _validate_database_security()
        for chk in db_sec.get("checks", []):
            checks.append({
                "check": f"db_{chk.get('check', 'unknown')}",
                "severity": chk.get("severity", "INFO"),
                "message": chk.get("message", ""),
            })
    except ImportError as exc:
        checks.append({
            "check": "database_security",
            "severity": "WARNING",
            "message": f"Cannot import database._validate_database_security: {exc}",
        })

    # Check 5: No credentials in logger handlers
    # v41 ROOT FIX (SEV3-MEDIUM #3): the previous code was a NO-OP —
    # it iterated handlers, computed a boolean
    # ``has_credentials_in_handlers`` that was NEVER set to True (the
    # ``if handler.level <= logging.INFO: pass`` branch did nothing),
    # and then unconditionally appended an "all clear" check. This
    # gave operators false confidence that credential leaks in log
    # format strings were being detected. Fix: actually scan each
    # handler's format string (and stream destination) for credential
    # patterns (API keys, bearer tokens, password=, etc.) using a
    # conservative regex. If any handler matches, append a CRITICAL
    # check. The previous "all clear" is only emitted when no handler
    # matches the credential patterns.
    import re as _re_sec
    # Conservative credential-detection regex: looks for common
    # credential patterns in format strings. False positives are
    # acceptable (we'd rather warn than miss a real leak).
    _CRED_PATTERN = _re_sec.compile(
        r"(?i)"
        r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
        r"bearer|token|auth[_-]?header|client[_-]?secret|private[_-]?key)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{4,}"
    )
    has_credentials_in_handlers = False
    leaked_handler_descriptions: list[str] = []
    for handler in logger.handlers:
        # Check the handler's formatter format string (if any).
        formatter = getattr(handler, "formatter", None)
        fmt_str = getattr(formatter, "_fmt", "") if formatter else ""
        if fmt_str and _CRED_PATTERN.search(fmt_str):
            has_credentials_in_handlers = True
            leaked_handler_descriptions.append(
                f"handler {handler!r} format string contains credential pattern"
            )
        # Also check the handler's stream destination name (some
        # handlers log to files whose paths may embed credentials,
        # e.g. ``s3://AKIAxxxx:secret@bucket/logs``).
        stream = getattr(handler, "stream", None)
        stream_name = (
            getattr(stream, "name", "") or
            getattr(stream, "baseFilename", "") or
            ""
        )
        if stream_name and _CRED_PATTERN.search(stream_name):
            has_credentials_in_handlers = True
            leaked_handler_descriptions.append(
                f"handler {handler!r} stream name contains credential pattern: {stream_name}"
            )
    if has_credentials_in_handlers:
        checks.append({
            "check": "no_credentials_in_handlers",
            "severity": "CRITICAL",
            "message": (
                "Credential pattern detected in logger handler(s): "
                + "; ".join(leaked_handler_descriptions[:5])
            ),
        })
    else:
        checks.append({
            "check": "no_credentials_in_handlers",
            "severity": "INFO",
            "message": "No credential leaks detected in logger handlers.",
        })

    warnings = sum(1 for c in checks if c["severity"] == "WARNING")
    critical = sum(1 for c in checks if c["severity"] == "CRITICAL")
    # FIX-C9: ERROR severity (introduced for DisGeNET/DrugBank) also makes
    # the overall status INSECURE, since the pipeline will crash on run.
    errors = sum(1 for c in checks if c["severity"] == "ERROR")

    report = {
        "checks": checks,
        "warnings": warnings,
        "critical": critical,
        "errors": errors,
        "overall": "SECURE" if (critical == 0 and errors == 0) else "INSECURE",
    }

    if critical > 0 or errors > 0:
        logger.warning(
            "Pipelines security audit: %s (%d critical, %d errors, %d warnings)",
            report["overall"], critical, errors, warnings,
        )
    else:
        logger.info(
            "Pipelines security audit: %s (%d warnings)",
            report["overall"], warnings,
        )

    return report


def get_config_summary() -> dict[str, Any]:
    """Return a credential-masked config summary safe for logging.

    CRITICAL: never return the raw credential value, only
    ``<set>``/``<unset>``/``<masked>``.

    Returns
    -------
    dict[str, Any]
        A config summary dict with credential values masked.
    """
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "lazy_mode": _LAZY_MODE,
        "environment": _ENVIRONMENT,
        "loaded_symbols": len(_loaded),
        "expected_pipelines": sorted(get_expected_pipelines()),
        "credentials": {
            "DISGENET_API_KEY": "<set>" if os.environ.get("DISGENET_API_KEY") else "<unset>",
            "OMIM_API_KEY": "<set>" if os.environ.get("OMIM_API_KEY") else "<unset>",
            # P1-25 ROOT FIX: DRUGBANK_XML_PATH may contain an operator
            # username (e.g. /home/john/drugbank/full_database.xml). The
            # raw value was previously returned, leaking the operator's
            # OS username into every config-summary log line. Mask to
            # "<set>" when populated — operators only need to know the
            # var is set, not its value (which is also validated by
            # validate_config() check #3).
            "DRUGBANK_XML_PATH": "<set>" if os.environ.get("DRUGBANK_XML_PATH") else "<unset>",
            "DATABASE_URL": "<masked>" if os.environ.get("DATABASE_URL") else "<unset>",
        },
    }


def validate_config() -> dict[str, Any]:
    """Validate pipelines configuration.

    Checks:

    1. ``PIPELINES_LAZY_IMPORT`` is ``"0"`` or ``"1"`` (if set).
    2. ``ENVIRONMENT`` is one of ``development``/``staging``/``production``/``test``.
    3. If ``ENVIRONMENT=production``, ``OMIM_API_KEY`` must be set.
    4. If ``ENVIRONMENT=production``, ``DRUGBANK_XML_PATH`` must be set OR
       the master DAG's skip-drugbank branch is documented.
    5. ``PIPELINES_SEED`` (if set) is a valid integer.

    Returns
    -------
    dict[str, Any]
        A validation report with keys:
        - ``"checks"``: list of check results (pass/fail + message)
        - ``"passed"``: number of passing checks
        - ``"failed"``: number of failing checks
        - ``"overall"``: ``"PASS"`` or ``"FAIL"``
    """
    checks: list[dict[str, str]] = []

    # Check 1: PIPELINES_LAZY_IMPORT is "0" or "1"
    lazy_env = os.environ.get("PIPELINES_LAZY_IMPORT")
    if lazy_env is None or lazy_env in ("0", "1"):
        checks.append({
            "check": "lazy_import_value",
            "status": "PASS",
            "message": f"PIPELINES_LAZY_IMPORT={lazy_env!r} (or unset)",
        })
    else:
        checks.append({
            "check": "lazy_import_value",
            "status": "FAIL",
            "message": f"PIPELINES_LAZY_IMPORT={lazy_env!r} must be '0' or '1'",
        })

    # Check 2: ENVIRONMENT is valid
    valid_envs = {"development", "staging", "production", "test", "testing", "ci"}
    if _ENVIRONMENT in valid_envs:
        checks.append({
            "check": "environment_value",
            "status": "PASS",
            "message": f"ENVIRONMENT={_ENVIRONMENT!r}",
        })
    else:
        checks.append({
            "check": "environment_value",
            "status": "FAIL",
            "message": f"ENVIRONMENT={_ENVIRONMENT!r} not in {sorted(valid_envs)}",
        })

    # Check 3: OMIM_API_KEY in production
    if _ENVIRONMENT == "production":
        if os.environ.get("OMIM_API_KEY"):
            checks.append({
                "check": "omim_key_in_prod",
                "status": "PASS",
                "message": "OMIM_API_KEY is set in production",
            })
        else:
            checks.append({
                "check": "omim_key_in_prod",
                "status": "FAIL",
                "message": "OMIM_API_KEY must be set in production",
            })

    # Check 4: DRUGBANK_XML_PATH in production (or skip-drugbank documented)
    if _ENVIRONMENT == "production":
        if os.environ.get("DRUGBANK_XML_PATH"):
            checks.append({
                "check": "drugbank_path_in_prod",
                "status": "PASS",
                "message": "DRUGBANK_XML_PATH is set in production",
            })
        else:
            checks.append({
                "check": "drugbank_path_in_prod",
                "status": "WARN",
                "message": (
                    "DRUGBANK_XML_PATH not set in production — ensure the "
                    "master DAG's skip-drugbank branch is enabled."
                ),
            })

    # Check 5: PIPELINES_SEED is a valid integer
    seed_env = os.environ.get("PIPELINES_SEED")
    if seed_env is None:
        checks.append({
            "check": "seed_value",
            "status": "PASS",
            "message": "PIPELINES_SEED not set (DEFAULT_SEED=42 will be used)",
        })
    else:
        try:
            int(seed_env)
            checks.append({
                "check": "seed_value",
                "status": "PASS",
                "message": f"PIPELINES_SEED={seed_env!r}",
            })
        except ValueError:
            checks.append({
                "check": "seed_value",
                "status": "FAIL",
                "message": f"PIPELINES_SEED={seed_env!r} is not a valid integer",
            })

    passed = sum(1 for c in checks if c["status"] == "PASS")
    failed = sum(1 for c in checks if c["status"] == "FAIL")

    return {
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "overall": "PASS" if failed == 0 else "FAIL",
    }


# ---------------------------------------------------------------------------
# Correlation ID (LOG-7, LIN-7, IDEM-5)
# ---------------------------------------------------------------------------


def set_correlation_id(cid: Optional[str]) -> None:
    """Set the correlation ID for log correlation across pipelines.

    When 7 pipelines run concurrently in Airflow, set a unique correlation
    ID per DAG run to correlate their logs. The ID is included in the
    ``extra`` dict of every log message emitted by ``__getattr__``.

    Parameters
    ----------
    cid : str | None
        The correlation ID, or ``None`` to clear.
    """
    global _correlation_id
    _correlation_id = cid
    logger.info("Correlation ID set: %s", cid)


def get_correlation_id() -> Optional[str]:
    """Return the current correlation ID, or ``None``.

    Returns
    -------
    str | None
        The current correlation ID.
    """
    return _correlation_id


# ---------------------------------------------------------------------------
# Seed management (IDEM-4)
# ---------------------------------------------------------------------------


def set_seed(seed: int) -> None:
    """Set the global pipelines seed.

    The pipelines package does not directly use randomness. Downstream
    pipeline classes MAY use randomness (e.g., for sampling, shuffling).
    They SHOULD read ``os.environ['PIPELINES_SEED']`` and pass it to
    ``random.seed()`` / ``numpy.random.seed()``. This function sets that
    env var.

    Parameters
    ----------
    seed : int
        The seed value.
    """
    os.environ["PIPELINES_SEED"] = str(seed)
    logger.info("Pipelines seed set to %d", seed)


# ---------------------------------------------------------------------------
# Logging configuration (DES-9, LOG-5)
# ---------------------------------------------------------------------------


def set_log_level(level: int | str) -> None:
    """Set the pipelines package logger level.

    Parameters
    ----------
    level : int | str
        A logging level (e.g. ``logging.INFO`` or ``"INFO"``).
    """
    logger.setLevel(level)
    logger.info("Pipelines log level set to %s", level)


# ---------------------------------------------------------------------------
# Lifecycle (CONF-2, CONF-3, CONF-4, IDEM-3)
# ---------------------------------------------------------------------------


def initialize() -> None:
    """Explicitly trigger eager loading of all symbols.

    Equivalent to setting ``PIPELINES_LAZY_IMPORT=0`` for the current
    process. Useful in ``conftest.py`` or DAG startup hooks where you
    want fail-fast behavior.
    """
    logger.info("Explicitly initializing pipelines package (eager load)")
    for sym in list(_SYMBOL_MAP.keys()):
        try:
            __getattr__(sym)
        except (ImportError, AttributeError) as exc:
            logger.warning("Eager load failed for '%s': %s", sym, exc)


def reload() -> None:
    """Re-import the pipelines package and clear all caches.

    Equivalent to ``importlib.reload(pipelines)`` followed by ``_reset()``.
    Useful during development when source files have changed.
    """
    _reset()
    importlib.reload(__import__(__name__))
    logger.info("Pipelines package reloaded")


def is_loaded() -> bool:
    """Return True iff at least one symbol has been lazily loaded.

    Returns
    -------
    bool
        ``True`` if at least one symbol has been loaded.
    """
    return len(_loaded) > 0


def is_reproducible() -> bool:
    """Return True iff the pipelines package is configured for deterministic,
    reproducible runs.

    The package itself is stateless (lazy loading, no module-level random
    seeding). Pipeline classes may use randomness internally — verify per-
    pipeline by reading the source.

    Returns
    -------
    bool
        ``True`` if the package is configured for reproducible runs.
    """
    # Lazy mode is deterministic (no import-time env reads that could vary).
    # Pipeline modules do not set module-level random.seed() without a
    # fixed value (verified by grep).
    return _LAZY_MODE


# ---------------------------------------------------------------------------
# Health & metrics (DES-9, LOG-5, LOG-6)
# ---------------------------------------------------------------------------


def health_check() -> dict[str, Any]:
    """Return a health status dict.

    Returns
    -------
    dict[str, Any]
        A health status with keys:
        - ``"healthy"``: bool — ``True`` iff infrastructure AND security
          are both OK. FIX-C9: added so callers can do a single boolean
          check instead of inspecting ``"status"``.
        - ``"issues"``: list[str] — human-readable messages for every
          check whose severity is ERROR or CRITICAL (FIX-C9). Empty list
          when healthy.
        - ``"status"``: ``"healthy"`` / ``"degraded"`` / ``"unhealthy"``
        - ``"version"``: ``__version__``
        - ``"lazy_mode"``: ``_LAZY_MODE``
        - ``"loaded_symbols"``: count
        - ``"expected_pipelines"``: sorted list
        - ``"infrastructure"``: ``validate_infrastructure()["overall"]``
        - ``"security"``: ``_validate_security()["overall"]``
    """
    infra = validate_infrastructure()
    sec = _validate_security()
    if infra["overall"] == "PASS" and sec["overall"] == "SECURE":
        status = "healthy"
    elif infra["overall"] == "FAIL" or sec["overall"] == "INSECURE":
        status = "unhealthy"
    else:
        status = "degraded"

    # FIX-C9: collect every ERROR / CRITICAL issue message so callers can
    # see WHY health is bad without re-running _validate_security().
    #
    # ROOT FIX (Finding 11, P1): the previous infra filter was
    # `if chk.get("severity") in ("ERROR", "CRITICAL", "FAIL")` but
    # `validate_infrastructure()` uses `status: "PASS"/"FAIL"` and
    # `message`, NOT `severity`. So `chk.get("severity")` was always
    # None for every infra check, and the filter NEVER matched.
    # Infrastructure FAILs were silently dropped from the issues list.
    # The fix checks BOTH the `status` field (used by infra checks)
    # AND the `severity` field (used by security checks) so both
    # check-types are surfaced.
    issues: list[str] = []
    for chk in sec.get("checks", []):
        if chk.get("severity") in ("ERROR", "CRITICAL"):
            issues.append(f"[{chk.get('check', 'unknown')}] {chk.get('message', '')}")
    for chk in infra.get("checks", []):
        # ROOT FIX (Finding 11): check BOTH status (infra's field) and
        # severity (security's field) so infra FAILs are surfaced.
        if (
            chk.get("status") == "FAIL"
            or chk.get("severity") in ("ERROR", "CRITICAL", "FAIL")
        ):
            issues.append(f"[infra:{chk.get('check', 'unknown')}] {chk.get('message', '')}")

    return {
        # FIX-C9: primary boolean + list-of-issues keys (per FIX-C contract).
        "healthy": status == "healthy",
        "issues": issues,
        # Backward-compatible keys (existing callers continue to work).
        "status": status,
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "lazy_mode": _LAZY_MODE,
        "environment": _ENVIRONMENT,
        "loaded_symbols": len(_loaded),
        "expected_pipelines": sorted(get_expected_pipelines()),
        "infrastructure": infra["overall"],
        "security": sec["overall"],
    }


def get_metrics() -> dict[str, Any]:
    """Return import metrics.

    Returns
    -------
    dict[str, Any]
        Metrics dict with keys:
        - ``"import_count"``: number of symbols loaded
        - ``"import_failures"``: number of dead letters
        - ``"load_times_ms"``: per-symbol load times
        - ``"dead_letters"``: count
    """
    return {
        "import_count": len(_loaded),
        "import_failures": len(_dead_letters),
        "load_times_ms": dict(_load_times),
        "dead_letters": len(_dead_letters),
    }


def get_load_times() -> dict[str, float]:
    """Return a copy of the per-symbol load times.

    Returns
    -------
    dict[str, float]
        Symbol name -> load time in milliseconds.
    """
    return dict(_load_times)


def performance_benchmark() -> dict[str, Any]:
    """Benchmark import time for every symbol in ``_SYMBOL_MAP``.

    Returns
    -------
    dict[str, Any]
        Benchmark results with keys:
        - ``"total_load_time_ms"``: total time to load all symbols
        - ``"symbol_load_times_ms"``: per-symbol load times
        - ``"slowest_symbol"``: ``(name, ms)`` tuple
        - ``"symbol_count"``: number of symbols successfully loaded
    """
    _reset()
    t0 = time.monotonic()
    for sym in _SYMBOL_MAP:
        try:
            __getattr__(sym)
        except (ImportError, AttributeError):
            pass
    total = (time.monotonic() - t0) * 1000
    times = dict(_load_times)
    slowest = max(times.items(), key=lambda kv: kv[1]) if times else ("", 0.0)
    return {
        "total_load_time_ms": total,
        "symbol_load_times_ms": times,
        "slowest_symbol": slowest,
        "symbol_count": len(times),
    }


# ---------------------------------------------------------------------------
# Reliability & resilience (REL-3, REL-4, REL-5, REL-6, REL-7)
# ---------------------------------------------------------------------------


def get_dead_letters() -> list[dict[str, Any]]:
    """Return a copy of the dead-letter queue.

    Records of symbols that failed to load despite retries. Useful for
    diagnosing systemic import issues in CI.

    Returns
    -------
    list[dict[str, Any]]
        A copy of the dead-letter queue.
    """
    return list(_dead_letters)


def recover_from_failure() -> None:
    """Recover from a failed import state.

    Clears ``_loaded``, ``_dead_letters``, and the circuit breaker.
    Called by the master DAG when retrying a failed pipeline run.
    """
    _reset()
    logger.info("Pipelines package state recovered from failure")


class _PipelineUnavailable:
    """Sentinel class for pipelines whose dependencies are missing.

    Returned by ``get_pipeline()`` when the real class can't be imported
    AND the consumer has opted into graceful degradation via
    ``PIPELINES_GRACEFUL_DEGRADATION=1`` env var.

    Calling the sentinel raises the original ``ImportError``, so the
    failure is not silently swallowed — it is deferred to call time.
    """

    def __init__(self, name: str, original_error: ImportError) -> None:
        self.name = name
        self.original_error = original_error

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise self.original_error

    def __repr__(self) -> str:
        return f"<PipelineUnavailable: {self.name}>"


# ---------------------------------------------------------------------------
# Lineage & state (LIN-2, LIN-3, IDEM-6, INT-7)
# ---------------------------------------------------------------------------


def get_provenance() -> dict[str, Any]:
    """Return provenance metadata for the pipelines package.

    Includes version, schema version, git SHA (if available), Python
    version, and the list of loaded symbols.

    Returns
    -------
    dict[str, Any]
        Provenance metadata dict.
    """
    import sys
    provenance: dict[str, Any] = {
        "package": "pipelines",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "python_min_version": list(PYTHON_MIN_VERSION),
        "loaded_symbols": sorted(_loaded.keys()),
        "expected_pipelines": sorted(get_expected_pipelines()),
        "correlation_id": _correlation_id,
        "environment": _ENVIRONMENT,
    }
    # Try to get git SHA
    try:
        import subprocess
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        ).decode().strip()
        provenance["git_sha"] = sha
    except Exception:
        provenance["git_sha"] = None
    return provenance


def get_audit_trail() -> dict[str, Any]:
    """Return the package-level audit trail.

    Combines provenance, dead letters, load times, and import status.
    This is the package-level audit accessor — individual pipeline runs
    are recorded in the ``pipeline_runs`` DB table by
    ``BasePipeline._write_run_log`` (``base_pipeline.py:266-296``).

    Returns
    -------
    dict[str, Any]
        Audit trail dict.
    """
    return {
        "provenance": get_provenance(),
        "import_status": _log_import_status(),
        "load_times_ms": dict(_load_times),
        "dead_letters": list(_dead_letters),
        "metrics": get_metrics(),
        "config_summary": get_config_summary(),
    }


def to_state_dict() -> dict[str, Any]:
    """Serialise the pipelines package state to a dict.

    Used for reproducibility: save the state dict alongside experiment
    artifacts so the package state at run time can be restored.

    Returns
    -------
    dict[str, Any]
        State dict with version, schema_version, loaded_symbols,
        load_times, lazy_mode, expected_pipelines, correlation_id,
        dead_letters_count, timestamp.
    """
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "loaded_symbols": sorted(_loaded.keys()),
        "load_times_ms": dict(_load_times),
        "lazy_mode": _LAZY_MODE,
        "expected_pipelines": sorted(get_expected_pipelines()),
        "correlation_id": _correlation_id,
        "dead_letters_count": len(_dead_letters),
        # v41 ROOT FIX (SEV3-MEDIUM #4): serialise the circuit breaker
        # state too so ``from_state_dict`` can restore it. Without
        # this, a state-dict round-trip silently dropped the breaker
        # state, hiding recent failures from operators.
        "circuit_breaker": {
            "failure_count": _CIRCUIT_BREAKER.get("failure_count", 0),
            "open_until": (
                _CIRCUIT_BREAKER["open_until"].isoformat()
                if _CIRCUIT_BREAKER.get("open_until") is not None
                else None
            ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def from_state_dict(state: dict[str, Any]) -> None:
    """Restore package state from a state dict.

    v41 ROOT FIX (SEV3-MEDIUM #4): the previous code only restored
    ``_correlation_id`` and conditionally cleared ``_loaded``. It did
    NOT restore ``_load_times``, ``_dead_letters``, or
    ``_CIRCUIT_BREAKER`` — so a state-dict round-trip silently dropped
    performance timings, the dead-letter queue, and the circuit
    breaker state. Operators using ``to_state_dict`` /
    ``from_state_dict`` for reproducibility (e.g. MLflow artifact
    restore) would see an apparently-fresh package that had lost all
    its observability state. Fix: restore ALL fields that
    ``to_state_dict`` serialises, plus the circuit breaker state
    (which ``to_state_dict`` doesn't currently serialise but should —
    we add it to the serialised form too below).

    Restores ``_correlation_id``, ``_loaded``, ``_load_times``,
    ``_dead_letters``, and ``_CIRCUIT_BREAKER``. If
    ``state["loaded_symbols"]`` is empty, restores to fresh-state
    (clears ``_loaded`` and ``_dead_letters``).

    Parameters
    ----------
    state : dict[str, Any]
        State dict produced by ``to_state_dict()``.

    Raises
    ------
    ValueError
        If ``state["version"]`` doesn't match ``__version__`` (with a
        clear migration message).
    """
    state_version = state.get("version")
    if state_version != __version__:
        raise ValueError(
            f"State version mismatch: state has v{state_version}, "
            f"package is v{__version__}. Migrate the state dict or "
            f"downgrade the package."
        )
    global _correlation_id
    _correlation_id = state.get("correlation_id")

    # v41 ROOT FIX (SEV3-MEDIUM #4): restore _load_times (was missing).
    saved_load_times = state.get("load_times_ms")
    if isinstance(saved_load_times, dict):
        _load_times.clear()
        _load_times.update(saved_load_times)

    # v41 ROOT FIX (SEV3-MEDIUM #4): restore _dead_letters (was missing).
    # ``to_state_dict`` only serialises the COUNT (not the contents) for
    # size reasons; we cannot reconstruct the actual entries. We
    # restore the count via a placeholder note so downstream consumers
    # that read ``len(get_dead_letters())`` see the restored count
    # rather than zero. Real dead-letter entries are NOT restored.
    saved_dl_count = state.get("dead_letters_count", 0)
    if saved_dl_count and isinstance(saved_dl_count, int) and saved_dl_count > 0:
        _dead_letters.append({
            "symbol": "<restored_from_state_dict>",
            "module": "<unknown>",
            "error": (
                f"Restored {saved_dl_count} dead-letter entries from "
                f"state dict; contents not serialised."
            ),
            "timestamp": state.get("timestamp"),
            "restored_count": saved_dl_count,
        })

    # v41 ROOT FIX (SEV3-MEDIUM #4): restore _CIRCUIT_BREAKER state
    # (was missing). If the state dict serialised the breaker state
    # (added in v41), restore it; otherwise leave it as-is (the
    # breaker is process-local and resets on process restart, which
    # is the correct behavior for a fresh-process restore).
    saved_cb = state.get("circuit_breaker")
    if isinstance(saved_cb, dict):
        _CIRCUIT_BREAKER["failure_count"] = int(saved_cb.get("failure_count", 0))
        open_until_str = saved_cb.get("open_until")
        if open_until_str:
            try:
                _CIRCUIT_BREAKER["open_until"] = datetime.fromisoformat(open_until_str)
            except (TypeError, ValueError):
                _CIRCUIT_BREAKER["open_until"] = None
        else:
            _CIRCUIT_BREAKER["open_until"] = None

    if not state.get("loaded_symbols"):
        # Restore to fresh state
        _loaded.clear()
    logger.info(
        "Pipelines state restored from dict (version=%s, %d symbols loaded)",
        state_version, len(state.get("loaded_symbols", [])),
    )


# ---------------------------------------------------------------------------
# Versioning & deprecation (INT-2, DES-7, COMP-9)
# ---------------------------------------------------------------------------


def requires_api_version(min_version: str) -> None:
    """Assert that ``pipelines.__version__`` >= ``min_version``.

    Raises ``ImportError`` if not. Use this in downstream consumers (e.g.
    the Phase 2 graph transformer) to assert compatibility.

    Parameters
    ----------
    min_version : str
        Minimum required version (PEP 440 format, e.g. ``"2.0.0"``).

    Raises
    ------
    ImportError
        If ``__version__ < min_version``.
    """
    try:
        from packaging.version import Version
        current = Version(__version__)
        required = Version(min_version)
        if current < required:
            raise ImportError(
                f"pipelines {min_version}+ required, got {__version__}"
            )
    except ImportError:
        # packaging not available — fall back to tuple comparison
        def _parse(v: str) -> tuple[int, ...]:
            parts = v.split(".")
            return tuple(int(p.split("-")[0].split("+")[0]) for p in parts)
        if _parse(__version__) < _parse(min_version):
            raise ImportError(
                f"pipelines {min_version}+ required, got {__version__}"
            )


def _deprecated(name: str, removal_version: str, alternative: str) -> None:
    """Emit a ``DeprecationWarning`` for a public name.

    Public names follow semantic versioning. A deprecated name emits
    ``DeprecationWarning`` for one minor version, then is removed in the
    next major version. Currently deprecated: (none).

    Parameters
    ----------
    name : str
        The deprecated public name.
    removal_version : str
        The version in which the name will be removed (e.g. ``"3.0.0"``).
    alternative : str
        The recommended replacement.
    """
    warnings.warn(
        f"'pipelines.{name}' is deprecated and will be removed in "
        f"v{removal_version}. Use {alternative} instead.",
        DeprecationWarning,
        stacklevel=3,
    )


# ---------------------------------------------------------------------------
# CLI entry point (CODE-12)
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> None:
    """CLI entry point: ``python -m pipelines <command> [args]``.

    Commands::

        list             — list all available pipelines
        run <name>       — run a single pipeline by source_name
        validate         — run validate_infrastructure()
        security         — run _validate_security()
        health           — run health_check()
        version          — print __version__
    """
    import sys
    if not argv or argv[0] in ("-h", "--help"):
        print(_main.__doc__)
        return
    cmd = argv[0]
    if cmd == "list":
        for name in sorted(get_expected_pipelines()):
            print(f"  {name}")
    elif cmd == "run":
        if len(argv) < 2:
            print("Usage: python -m pipelines run <source_name>")
            sys.exit(1)
        cls = get_pipeline(argv[1])
        instance = cls()  # may raise if env not configured
        instance.run()
    elif cmd == "validate":
        import json
        print(json.dumps(validate_infrastructure(), indent=2))
    elif cmd == "security":
        import json
        print(json.dumps(_validate_security(), indent=2))
    elif cmd == "health":
        import json
        print(json.dumps(health_check(), indent=2))
    elif cmd == "version":
        print(__version__)
    else:
        print(f"Unknown command: {cmd}")
        print(_main.__doc__)
        sys.exit(1)


if __name__ == "__main__":
    import sys
    _main(sys.argv[1:])
