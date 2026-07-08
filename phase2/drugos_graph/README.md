# DrugOS — Knowledge Graph Construction & GNN Data Pipeline

Autonomous Drug Repurposing Platform — Week 2 Graph Module

## Overview

This package provides all components for building, loading, querying, and converting the DrugOS biomedical knowledge graph for drug repurposing. It integrates 9+ data sources into a unified Neo4j knowledge graph, constructs PyG HeteroData for GNN training, and includes a TransE baseline model for link prediction.

## Modules

| Module | Description |
|--------|-------------|
| `config` | Global configuration and path constants |
| `drkg_loader` | Download and parse the DRKG baseline knowledge graph |
| `drugbank_parser` | Parse DrugBank XML into structured drug records |
| `kg_builder` | Build and manage the Neo4j knowledge graph |
| `entity_resolver` | Cross-database entity resolution and ID mapping |
| `pyg_builder` | Convert KG to PyTorch Geometric HeteroData for GNN |
| `graph_queries` | Cypher query utilities for graph traversal and search |
| `graph_stats` | KG statistics, validation, and sanity checks |
| `utils` | Shared utilities (identifier sanitization, type mapping) |
| `stitch_loader` | STITCH chemical-protein interaction ingestion |
| `sider_loader` | SIDER side effect database ingestion |
| `string_loader` | STRING protein-protein interaction ingestion |
| `chembl_loader` | ChEMBL bioactivity data ingestion |
| `opentargets_loader` | OpenTargets drug-target-disease evidence ingestion |
| `uniprot_loader` | UniProt Swiss-Prot protein data ingestion |
| `clinicaltrials_loader` | ClinicalTrials.gov AACT database ingestion |
| `geo_loader` | GEO (Gene Expression Omnibus) loader — `Protein→expressed_in→Anatomy` edges (Institutional-Grade v1.0.0) |
| `transe_model` | TransE knowledge graph embedding baseline model |
| `evaluation` | Link prediction evaluation metrics (AUC, P@K, MRR) |
| `negative_sampling` | Negative sampling strategies for training data |
| `training_data` | Training data construction and temporal splitting |
| `chemberta_encoder` | ChemBERTa SMILES molecular embedding generation |
| `mlflow_tracker` | MLflow experiment tracking integration |
| `gpu_utils` | GPU memory validation and batch testing |

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the full pipeline
python -m drugos_graph

# Run with options
python -m drugos_graph --skip-download --skip-neo4j

# Run a specific step
python -m drugos_graph --step 5
```

## Pipeline Steps

1. Load DRKG baseline graph
2. Build entity and edge mappings
3. Load into Neo4j (bulk CREATE for speed)
4. DrugBank enrichment
5. STITCH drug-protein interactions
6. SIDER side effects
7. Additional sources (STRING, ChEMBL, OpenTargets, UniProt, ClinicalTrials)
8. Entity resolution
9. PyG HeteroData construction
10. Training data construction (positive/negative examples, temporal split)
11. TransE baseline training
12. Validation and sanity checks
13. Data README generation

## Environment Variables

- `DRUGOS_NEO4J_PASSWORD`: Neo4j database password (required for Neo4j operations)
- `DRUGOS_NEO4J_URI`: Neo4j bolt URI (default: `bolt://localhost:7687`)
- `DRUGOS_NEO4J_USER`: Neo4j username (default: `neo4j`)
- `DRUGOS_DATA_DIR`: Override the data directory (default: `./data`)
- `DRUGOS_LOG_LEVEL`: Logging level (default: `INFO`)
- `DRUGOS_STRICT_LABEL_MODE`: Strict label mode for `utils.py`
  - `strict` (default) — raise `ValueError` on unknown DRKG types
  - `warn` — log WARNING and fall back to sanitization
  - `quarantine` — log WARNING, write to dead-letter queue, fall back
- `DRUGOS_LABEL_MAP_PATH`: Override path to `label_map.yaml` (default: `drugos_graph/data/label_map.yaml`)
- `DRUGOS_EXTRA_NODE_TYPES`: Comma-separated `Type:Label` pairs to add custom types
- `DRUGOS_EXPECTED_LABEL_MAP_VERSION`: Fail pipeline startup if version mismatches
- `DRUGOS_SELF_TEST`: `1` runs validators at import; `2` also scans for secret logging
- `MLFLOW_TRACKING_URI`: MLflow tracking URI

## Strict Label Mode (utils.py)

The `drkg_node_type_to_neo4j_label()` function in `utils.py` defaults to
**strict mode** — it raises `ValueError` on unknown DRKG types. This is
deliberate: silent fallback was the root cause of multiple data-quality
bugs (audit issues 5.1, 6.2, 7.3). To opt into the legacy fallback
behavior:

```python
from drugos_graph.utils import drkg_node_type_to_neo4j_label

# Strict (default, recommended for production)
label = drkg_node_type_to_neo4j_label("Compound")  # → "Compound"

# Non-strict (logs WARNING, writes to dead-letter queue)
label = drkg_node_type_to_neo4j_label("NewType", strict=False)  # → "NewType"
```

Or via environment variable:

```bash
export DRUGOS_STRICT_LABEL_MODE=warn   # or 'quarantine' or 'strict' (default)
```

## Plugin API (utils.py)

Custom deployments can register additional DRKG types without modifying
`utils.py`:

```python
from drugos_graph.utils import register_node_type

# Returns a NEW LabelRegistry — does NOT mutate the global
registry = register_node_type(
    "DrugFingerprint",
    "DrugFingerprint",
    ontology="custom",
    ontology_version="1.0",
    source="my_plugin",
)
label = registry.lookup("DrugFingerprint")  # → "DrugFingerprint"
```

## Schema Export (utils.py)

External systems can fetch the label schema as JSON:

```python
from drugos_graph.utils import export_label_schema_json

print(export_label_schema_json(indent=2))
```

Suitable for publishing via a FastAPI `/schema/labels` endpoint so
downstream systems (React dashboard, RL agent) don't need to hardcode
the label mapping.

## Compliance Documentation

See `drugos_graph/compliance.md` for FDA 21 CFR Part 11, HIPAA, and GDPR
compliance documentation, including audit trail format, schema
versioning, and PII redaction.

## Data Dictionary

See `drugos_graph/data/label_dictionary.md` for the canonical reference
of every DRKG type name, its Neo4j storage label, its source ontology,
example IDs, and notes on deprecation or aliasing.

## Forensic Audit Fix Log

See `utils_FIXLOG.md` (in the project root) for the complete resolution
log of all 115 audit issues across 16 domains.

## Week 2 Exit Criteria

- [ ] Complete KG with 500K+ nodes and 6M+ edges
- [ ] Training dataset with 15K+ positive and 75K+ negative drug-disease pairs
- [ ] TransE baseline achieving AUC > 0.78
- [ ] PyG data loader confirmed working

## uniprot_loader

Parses UniProtKB/Swiss-Prot flat-file (`.dat`) format into **Protein nodes**
for the DrugOS knowledge graph. UniProt is the *only* source of Protein
nodes — if this loader fails silently, the entire graph has zero proteins
and the Graph Transformer cannot learn any drug-target mechanism.

### What it does
- Downloads (or cached-loads) the pinned 2024_03 UniProt Swiss-Prot release.
- Parses the flat-file format into `UniProtRecord` dicts (see `schemas.py`).
- Converts records to `ProteinNode` dicts for Neo4j.
- Emits `UniProtEdge` records from DR cross-references (ChEMBL, DrugBank,
  HGNC, Pfam, InterPro, Reactome, STRING, …).
- Acts as a **scientific guardian**: cross-checks every parsed `gene_id`
  against `verified_uniprot_gene_crosswalk.yaml` and overrides known-wrong
  values (SCI-1: IRS1 P35568 GeneID 2645 → 3667).

### How to run
```bash
# As part of the full pipeline:
python -m drugos_graph.run_pipeline

# Standalone (uses the local 30-entry sample if no download is available):
python -c "from drugos_graph.uniprot_loader import download_uniprot, parse_uniprot_entries; download_uniprot(); print(len(parse_uniprot_entries()))"
```

### Expected output
- Local sample (`data/raw/uniprot_sprot.dat`): **30 records, 30 nodes, 60 edges**.
- Production (2024_03 release): ~570,000 records.

### Common errors and fixes
| Error | Cause | Fix |
|-------|-------|-----|
| `UniProtDownloadError` | Network/DNS failure | Check `https://status.uniprot.org/`; retry with `allow_stale=True` |
| `UniProtDownloadError: not in allowlist` | URL changed in config | Add the new URL prefix to `config.ALLOWED_UNIPROT_URLS` |
| `UniProtDownloadError: does not look like a UniProt flat file` | URL/filename mismatch (D12-002) | Ensure the URL points at `.dat.gz`, not `.tar.gz` |
| `UniProtDataIntegrityError: SHA-256 mismatch` | File tampered or partially downloaded | Re-download; verify against UniProt's `.sha256` sidecar |
| `UniProtDataIntegrityError: record count` | < 50% of expected records | Check organism filter, URL/format, `data/dead_letter/uniprot_malformed.jsonl` |
| `UniProtParseError: parse error rate` | > 1% of lines had parse errors | Inspect `data/dead_letter/uniprot_malformed.jsonl` for the bad lines |
| `ValueError: organism must be non-empty` | Empty organism string | Pass `organism=None` to disable filtering |
| 0 records returned | Wrong file or filter | Check `DRUGOS_UNIPROT_FILE` env var; check organism filter |

### How to update the pinned version
1. Change `DATA_SOURCES['uniprot']['version']` and `['url']` together (the
   URL must end in `.dat.gz`, not `.tar.gz` — D12-002).
2. Update `data/verified_uniprot_gene_crosswalk.yaml` from the new release.
3. Bump `UNIPROT_PARSER_VERSION` in `config.py`.
4. Re-run `pytest tests/test_uniprot_loader.py` — the SCI-1 regression test
   must still pass.

### Testing with a local fixture
```bash
DRUGOS_UNIPROT_FILE=my_fixture.dat python -c "from drugos_graph.uniprot_loader import parse_uniprot_entries; print(len(parse_uniprot_entries()))"
```

### Audit coverage
This loader addresses all 94 defects from the Forensic Red-Team Audit v1.0
across 16 domains (Architecture, Design, Scientific Correctness, Coding,
Data Quality, Reliability, Idempotency, Performance, Security, Testing,
Logging, Configuration, Documentation, Compliance, Interoperability, Data
Lineage). Every fix is traceable via inline `D<N>-<NNN>` comments. See
`tests/test_uniprot_loader.py` and `tests/test_uniprot_loader_combined.py`
for the verification suite.

## DrugBank Parser v2.0 — Institutional Grade

The `drugbank_parser` module has been upgraded to v2.0 (institutional
grade) with 252 audit-documented fixes across 16 domains plus 18
patient-safety guards. See:

- `docs/drugbank_parser.md` — comprehensive README
- `docs/drugbank_fix_changelog.md` — full changelog of all 252 fixes
- `docs/drugbank_webhook.md` — webhook contract for parse-complete events
- `docs/source_of_truth.md` — source-of-truth matrix across loaders
- `docs/schemas/drugbank_node.schema.json` — JSON Schema for node records
- `docs/schemas/drugbank_edge.schema.json` — JSON Schema for edge records
- `tests/test_drugbank_parser.py` — Test 1 (174 tests, parser-focused)
- `tests/test_drugbank_seven_files_combined.py` — Test 2 (79 integration tests)

## STITCH Loader v1.1.0 — Institutional Grade

The `stitch_loader` module has been upgraded to v1.1.0 (institutional
grade) with 80 audit-documented fixes across 16 domains. The forensic
audit (`master_prompt_fix_stitch_loader.md`) enumerated 80 specific
defects (BUG-3.1 through GAP-16.5); every audit ID is addressed via an
inline `# Fixes <audit-id>:` comment.

### Public API

Backward-compatible signatures (Rule R3 — original 3 functions preserved):

- `download_stitch(force=False) -> Path`
- `parse_stitch_interactions(filepath=None, score_threshold=None) -> pd.DataFrame`
- `stitch_to_edge_records(df, crosswalk=None) -> List[Dict]`

New additive APIs:

- `parse_stitch_raw(filepath=None) -> pd.DataFrame` — pure parser
- `filter_by_score(df, threshold) -> pd.DataFrame`
- `filter_by_organism(df, taxid=9606) -> pd.DataFrame`
- `validate_stitch(df, taxid=9606) -> StitchValidationReport`
- `dedup_edges(df, strategy="max_combined_score") -> pd.DataFrame`
- `iter_stitch_cpi(filepath=None, chunksize=100_000) -> Iterator[pd.DataFrame]`
- `iter_stitch_edges(df_or_path, *, crosswalk=None, batch_size=10_000, **kwargs)`
- `stitch_to_node_records(df) -> List[dict]` — returns `[]` (edges only)
- `load_stitch(skip_neo4j=False, force=False, score_threshold=None, ...) -> dict`
- `StitchLoader` — Loader Protocol adapter class

### Environment Variables

| Env var | Purpose |
|---------|---------|
| `DRUGOS_STITCH_FILEPATH` | Override the input file path |
| `DRUGOS_STITCH_URL` | Override the download URL |
| `DRUGOS_STITCH_FORCE_DOWNLOAD` | Force re-download |
| `DRUGOS_STITCH_SKIP` | Skip STITCH load entirely |
| `DRUGOS_STITCH_BATCH_SIZE` | Batch size for `iter_stitch_edges` |
| `DRUGOS_STITCH_SCORE_THRESHOLD` | Override default threshold (700) |
| `DRUGOS_STITCH_REQUIRED` | STITCH is required source (default 1) |
| `DRUGOS_STITCH_CA_BUNDLE` | Custom CA bundle for TLS |
| `DRUGOS_STITCH_CONFIG` | YAML config file path |
| `DRUGOS_STITCH_LEGACY_CID_MERGE` | Preserve v0 CIDm/CIDs merge (deprecated) |
| `DRUGOS_STITCH_CHUNK_SIZE` | Chunk size for `iter_stitch_cpi` |
| `DRUGOS_STITCH_CHECKPOINT_INTERVAL` | Rows between checkpoints |
| `DRUGOS_STITCH_EMIT_METRICS` | Emit Prometheus/StatsD metrics |
| `DRUGOS_STITCH_VERIFY_CID_EXISTS` | Query PubChem REST for CID existence |

### Patient-Safety Critical Fixes

- **BUG-3.1**: Preserve CIDm (stereo-specific, e.g. S-warfarin 5× potent) vs
  CIDs (racemic mixture) distinction. Merging them would aggregate adverse
  events incorrectly and could lead to lethal dose recommendations.
- **BUG-3.4**: Filter non-human organisms (mouse, rat, yeast) by default.
  Ingesting non-human proteins would train the Graph Transformer on
  cross-species noise.
- **BUG-2.5**: Replace substring matching with formal
  `STITCH_ACTION_TO_REL_TYPE` map. The v0 `"inhibit" in action` pattern
  mislabeled `"reactivation"` as `"activates"` (wrong drug for wrong disease).
- **BUG-4.3 / BUG-11.5**: Raise `StitchDataIntegrityError` if score column
  is missing (instead of silently skipping the filter — would allow 20M
  unfiltered rows into Neo4j).

### Documentation

- `docs/stitch_data_dictionary.md` — column + edge props documentation
- `docs/stitch_lineage.md` — forward/reverse lineage + rollback Cypher
- `docs/SCHEMA_CHANGELOG.md` — cross-loader schema change history
- `tests/test_stitch_loader.py` — Test 1 (146 tests, loader-focused)
- `tests/test_ten_files_combined.py` — Test 2 (68 tests, 10-file integration)
- `tests/fixtures/stitch/` — 14 fixture .tsv.gz files
- `scripts/make_stitch_fixtures.py` — fixture generator (reproducible)

---

## ClinicalTrials Loader (v2.1.0 — Institutional-Grade)

The ClinicalTrials loader downloads, validates, parses, and converts the
ClinicalTrials.gov AACT (Accelerated Clinical Trials Transformation
Initiative) database into `(Compound)-[:tested_for]->(Disease)`
knowledge-graph edge records.

### Why This Loader Matters (Patient Safety)

ClinicalTrials.gov AACT data are the **sole source of clinical-trial
evidence** feeding the RL ranker's "has been tested in humans" dimension.
A silently fabricated `Warfarin → Disease X` edge (because Warfarin was
the *comparator* arm, not the experimental arm) teaches the ranker that
Warfarin treats X. A clinician who trusts that ranker can prescribe
Warfarin off-label to a patient for whom it is contraindicated —
**THAT PATIENT CAN DIE.**

### Critical Patient-Safety Fixes (v2.1.0)

| ID  | Issue                                                                                | Fix                                                                                          |
|-----|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| C1  | AACT SQL `mesh_term` column likely doesn't exist                                      | Detect modern vs legacy schema; refuse unknown schemas.                                       |
| C2  | Cross-product JOIN fabricates drug-disease pairs                                      | Tag comparator/placebo via description regex; ×0.3 evidence multiplier; id_confidence=low.   |
| C3  | No `why_stopped` capture                                                              | Add `why_stopped`; -0.20 evidence penalty + `safety_signal="stopped_for_safety"` flag.       |
| C4  | `intervention_type='Drug'` excludes biologics + Phase 4                              | Default includes Biological + Phase 4 (post-marketing surveillance).                         |
| C5  | `LIKE '%Phase 3%'` substring match                                                    | Replaced with exact-match `IN (?, ?)`.                                                       |
| C6  | `rel_type` mismatch with schema                                                       | Changed to `tested_for`. `treats` is FORBIDDEN (reserved for FDA-approved drugs from DrugBank). |
| C7  | No enrollment count                                                                  | Capture `enrollment`; WARNING if <30 in Phase 3.                                             |
| C8  | Re-runs create duplicate edges                                                        | Deterministic `edge_id` + `use_merge=True` at the Neo4j load site.                           |
| C9  | Identifier scheme mismatch + free-text drug names                                     | MeSH → DrugBank/UMLS crosswalk integration (TODO); id_confidence=low for unresolved IDs.     |
| C10 | Empty `src_id`/`dst_id` on missing data                                               | Quarantine bad rows to DLQ; never emit empty IDs.                                            |

### Public API

Backward-compatible v0 shims (signatures preserved):

- `download_clinicaltrials(force=False) -> Path`
- `parse_clinicaltrials(ct_dir=None, phase="Phase 3") -> pd.DataFrame`
- `clinicaltrials_to_edge_records(df) -> List[Dict]`

New public functions (additive only):

- `parse_clinicaltrials_trials(ct_dir=None, phases=None, *, cfg=None) -> pd.DataFrame`
- `iter_clinicaltrials_trials(ct_dir=None, *, cfg=None) -> Iterator[pd.DataFrame]`
- `clinicaltrials_to_edge_records_streaming(df_or_iter, **kwargs) -> Iterator[Dict]`
- `clinicaltrials_to_node_records(df, **kwargs) -> List[Dict]`
- `clinicaltrials_to_graph(df, **kwargs) -> Tuple[List, List]`
- `validate_clinicaltrials(df, edges, **kwargs) -> Dict[str, Any]`
- `load_clinicaltrials(skip_neo4j=True, **kwargs) -> Dict[str, Any]`

New public class:

- `ClinicalTrialsLoader` — Loader Protocol adapter (PEP 544).
- `ClinicalTrialsConfig` — frozen dataclass with all thresholds.

### Usage

```python
from drugos_graph.clinicaltrials_loader import (
    ClinicalTrialsLoader, ClinicalTrialsConfig,
)

# Default config: Phase 3 + Phase 4, Drug + Biological, Interventional,
# Completed/Active/Recruiting/Enrolling/Not-yet-recruiting statuses.
loader = ClinicalTrialsLoader(cfg=ClinicalTrialsConfig())

# Download + extract AACT.
extract_dir = loader.download(force=False)

# Parse trials (streaming).
for chunk in iter_clinicaltrials_trials(ct_dir=extract_dir, cfg=loader.cfg):
    edges = clinicaltrials_to_edge_records(chunk, cfg=loader.cfg)
    # Load edges to Neo4j ...

# Or end-to-end:
result = load_clinicaltrials(skip_neo4j=True)
print(f"Edges: {result['edges_total']}, Nodes: {result['nodes_total']}")
```

### Environment Variables

See `drugos_graph/data/clinicaltrials_data_dictionary.md` for the full
list of `DRUGOS_CLINICALTRIALS_*` environment variables.

### Files

- `drugos_graph/clinicaltrials_loader.py` — the loader (1,800+ lines)
- `drugos_graph/data/clinicaltrials_data_dictionary.md` — data dictionary
- `tests/test_clinicaltrials_loader.py` — Test 1 (103 tests, loader-focused)
- `tests/test_thirteen_files_combined.py` — Test 2 (63 tests, 13-file integration)
- `tests/fixtures/clinicaltrials/` — 14 fixture `.db` files
- `scripts/make_clinicaltrials_fixtures.py` — fixture generator (reproducible)
- `MIGRATION_NOTES.md` — v0 → v2.1.0 migration notes (repo root)

## GEO Loader (v1.0.0 — Institutional-Grade)

The GEO loader (`drugos_graph.geo_loader`) downloads, parses, validates,
and converts Gene Expression Omnibus (GEO) SOFT files into
`Protein→expressed_in→Anatomy` edges for the knowledge graph. GEO is
the SOLE source of tissue-specificity data in the KG — without it, the
Graph Transformer cannot learn that a drug target is (or is NOT)
expressed in the tissue where the disease acts.

### Quick Start

```python
from drugos_graph.geo_loader import GeoLoader, parse_geo_series, geo_to_edge_records

# Option 1: GeoLoader adapter (Loader Protocol)
loader = GeoLoader()
loader.download()                    # downloads GSE92649 (pinned series)
records = list(loader.parse())       # yields GeoRawRecord dicts
nodes, edges = loader.to_graph(records)  # (empty, [GeoEdgeRecord, ...])

# Option 2: Free-function API (backward-compatible with v0)
from drugos_graph.geo_loader import (
    download_geo, parse_geo_series, geo_to_edge_records,
)
download_geo()                       # downloads GSE92649 (pinned)
records = parse_geo_series()         # parses the downloaded file
edges = geo_to_edge_records(records)  # Protein→expressed_in→Anatomy edges
```

### Configuration

See `DATA_SOURCES["geo"]` in `config.py`. Key env vars:
- `GEO_REQUIRED=1` — fail loudly (`GeoCriticalError`) if GEO produces 0 records.
- `GEO_AUTO_DOWNLOAD=1` — enable automatic download (default: operator
  must place the file manually).
- `GEO_KEEP_BACKUPS=1` — keep `.bak.{timestamp}` of overwritten files.
- `GEO_MEMORY_BUDGET_MB=2048` — memory budget for `parse_geo_series`.
- `NCBI_API_KEY=...` — NCBI API key (optional, increases rate limit).
- `DRUGOS_ENV=dev|staging|prod` — environment selector.
- `GEO_SKIP_RECORD_COUNT_GUARD=1` — testing only (skip the GEO-5.5 check).
- `GEO_SKIP_SHA256=1` — testing only (skip SHA-256 verification).
- `DRUGOS_GEO_OFFLINE=1` — never attempt network calls.

### Patient-Safety Doctrine

GEO is the SOLE source of `Protein→expressed_in→Anatomy` edges. If this
loader silently produces zero records, the KG lacks the entire tissue-
specificity modality, the Graph Transformer cannot learn that a drug
target is absent from the disease tissue, and a clinician can be handed
a "high-confidence" repurposing candidate that will fail in Phase II —
or harm a patient. The loader raises `GeoCriticalError` when
`GEO_REQUIRED=1` and zero records are produced.

### Testing

- `tests/test_geo_loader.py` — Test 1 (247 tests, loader-focused, all 192
  audit IDs covered).
- `tests/test_fourteen_files_combined.py` — Test 2 (110 tests, 14-file
  integration).
- `tests/fixtures/geo/` — 6 fixture `.soft.gz` files (sample, malformed,
  empty, withdrawn, non_human, sensitive).
- `tests/fixtures/geo/make_fixtures.py` — fixture generator (reproducible).

### Files

- `drugos_graph/geo_loader.py` — the loader (~5,400 lines, 19 sections)
- `drugos_graph/exceptions.py` — 8 new GEO exception classes
- `drugos_graph/schemas.py` — 5 new GEO TypedDicts + `GEO_PROVENANCE_KEYS`
- `drugos_graph/config.py` — 50+ new `GEO_*` constants + `get_geo_series_path()`
- `drugos_graph/__init__.py` — GEO docstring updated (no longer "stub")
- `drugos_graph/compliance.md` — GEO URI updated to `GSE92649`

### References

- Barrett T, Wilhite SE, Ledoux P, et al. "NCBI GEO: archive for
  functional genomics data sets—update." Nucleic Acids Res.
  2013;41(D1):D991-D995.
- Edgar R, Domrachev M, Lash AE. "Gene Expression Omnibus: NCBI gene
  expression and hybridization array data repository." Nucleic Acids
  Res. 2002;30(1):207-10.
- GEO homepage: https://www.ncbi.nlm.nih.gov/geo/
- SOFT format spec: https://www.ncbi.nlm.nih.gov/geo/info/soft.html
- Master Repair Prompt: `GEO_LOADER_MASTER_REPAIR_PROMPT.md` (192 audit
  findings across 16 domains).
