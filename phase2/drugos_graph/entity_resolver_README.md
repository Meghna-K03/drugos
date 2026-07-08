# Entity Resolver — README

**Module:** `drugos_graph/entity_resolver.py`
**Version:** 1.1.0 (institutional-grade)
**Schema Version:** 1.1.0
**Last Updated:** 2026-06-19
**Audit Reference:** `ENTITY_RESOLVER_FIX_PROMPT.md` — 188 findings across 16 domains

---

## Overview

The `EntityResolver` is the **only** module in the Autonomous Drug Repurposing Platform that decides whether DrugBank's "DB00945", ChEMBL's "CHEMBL25", PubChem's "2244", and DRKG's "Compound::DB00945" all refer to the **same molecule** (aspirin). If it emits the wrong canonical ID, two records of the same molecule become two separate nodes in the knowledge graph; the Graph Transformer then learns wrong drug-disease edges; the RL ranker surfaces wrong repurposing candidates; a clinician can act on a wrong recommendation; **a patient can die.**

This file is therefore patient-safety-grade. Every public API is treated as clinical-grade.

---

## Quickstart

```python
from drugos_graph.entity_resolver import EntityResolver
import pandas as pd

# 1. Construct (config & seed are auto-injected from drugos_graph.config)
resolver = EntityResolver(seed=42)

# 2. Load DrugBank compounds first (mandatory ordering)
resolver.resolve_compounds_from_drugbank([
    {
        "id": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        "drugbank_id": "DB00945",
        "name": "Aspirin",
        "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        "chembl_id": "CHEMBL25",
        "pubchem_cid": "2244",
        "atc_codes": "N02BA01|A01AD05",
        "withdrawn": False,
        "deprecated": False,
    },
])

# 3. Match DRKG compounds to DrugBank canonical IDs
drkg_df = pd.DataFrame({
    "head_type": ["Compound"],
    "head_id": ["Compound::DB00945"],
    "tail_type": ["Disease"],
    "tail_id": ["DOID:1438"],
    "rel_type": ["treats"],
})
resolver.resolve_compounds_from_drkg(drkg_df)

# 4. Resolve diseases, genes, proteins
resolver.resolve_diseases_from_drkg(drkg_df)
resolver.resolve_genes_from_drkg(drkg_df)
resolver.resolve_proteins_from_uniprot([
    {"accession": "P00533", "protein_name": "EGFR",
     "gene_id": "1956", "gene_name": "EGFR"},
])

# 5. Build Gene-encodes-Protein edges (with referential integrity)
edges = resolver.build_gene_protein_edges()

# 6. Deduplicate edges from multiple sources
edges = resolver.merge_duplicate_edges(edges, "max_confidence")

# 7. Lookup
cid = resolver.lookup_canonical_id(
    "Compound", "drugbank_id", "DB00945",
    min_confidence=0.95,
    exclude_needs_review=True,
)
# cid == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"

# 8. Stats & reports
print(resolver.get_resolution_stats())
print(resolver.get_unresolved_report())

# 9. Persist
resolver.save_mappings("/tmp/mappings.json", fmt="json")
resolver.export_lineage("/tmp/lineage.json")
```

---

## API Reference

### `EntityResolver(config=None, logger=None, thresholds=None, seed=None)`

Construct a new resolver. All parameters optional.

**Parameters:**
- `config`: Config module to use. Defaults to `drugos_graph.config`.
- `logger`: Custom logger. Defaults to module logger.
- `thresholds`: Dict overriding default thresholds (see Configuration below).
- `seed`: Random seed for reproducibility. Defaults to `config.SEED`.

**Example:**
```python
r = EntityResolver(thresholds={
    "unmatched_drkg_confidence": 0.65,
    "edge_dedup_early_reduction_threshold": 500,
})
```

### Public Methods

| Method | Purpose |
|--------|---------|
| `resolve_compounds_from_drugbank(records)` | Build Compound mappings from DrugBank records. |
| `resolve_compounds_from_drkg(df)` | Match DRKG Compound entities to existing canonical IDs. |
| `resolve_diseases_from_drkg(df)` | Build Disease mappings from DRKG. |
| `resolve_genes_from_drkg(df)` | Build Gene mappings from DRKG. |
| `resolve_proteins_from_uniprot(records)` | Build Protein mappings from UniProt records. |
| `merge_mappings_by_inchikey()` | Merge Compound mappings sharing the same InChIKey. |
| `build_gene_protein_edges()` | Emit Gene→encodes→Protein edges with referential integrity. |
| `merge_duplicate_edges(edges, strategy)` | Deduplicate edges: same (src, rel, dst) from multiple sources. |
| `merge_duplicate_edges_streaming(edges, strategy)` | Streaming variant for large graphs (>100K edges). |
| `lookup_canonical_id(entity_type, id_system, external_id, ...)` | Reverse-lookup canonical ID by any alias. |
| `get_mapping(entity_type, canonical_id)` | Get the full EntityMapping object. |
| `get_resolution_stats()` | Per-entity-type statistics. |
| `get_unresolved_report()` | All IDs flagged for human review. |
| `get_audit_trail(entity_type, canonical_id)` | Filtered transformation log. |
| `diff(other_resolver)` | Compute added/removed/modified between two resolvers. |
| `delete_entity(entity_type, canonical_id)` | GDPR right-to-be-forgotten. |
| `clear()` | Reset all state without re-instantiating. |
| `health_check()` | Return resolver health snapshot. |
| `save_mappings(path, fmt, encrypt=False)` | Persist to JSON/JSONL/CSV/Parquet. |
| `load_mappings(path, fmt)` | Load from disk. |
| `to_cypher(path)` | Export as Neo4j Cypher MERGE statements. |
| `export_lineage(path, fmt)` | Export lineage metadata. |

### Properties (Specialized Resolvers, D1-003)

| Property | Type | Description |
|----------|------|-------------|
| `compounds` | `_CompoundResolver` | Delegates to compound-specific methods. |
| `diseases` | `_DiseaseResolver` | Delegates to disease-specific methods. |
| `genes` | `_GeneResolver` | Delegates to gene-specific methods. |
| `proteins` | `_ProteinResolver` | Delegates to protein-specific methods. |
| `deduplicator` | `_EdgeDeduplicator` | Delegates to edge-dedup methods. |

---

## Configuration

All thresholds are env-overridable. The defaults are snapshotted at `__init__` time so a mid-run config change cannot corrupt an in-flight resolution.

| Constant | Default | Env Var | Purpose |
|----------|---------|---------|---------|
| `UNMATCHED_DRKG_CONFIDENCE` | 0.80 | `DRUGOS_UNMATCHED_DRKG_CONFIDENCE` | Confidence assigned to unmatched DRKG compounds. |
| `EDGE_DEDUP_EARLY_REDUCTION_THRESHOLD` | 1000 | `DRUGOS_EDGE_DEDUP_EARLY_REDUCTION_THRESHOLD` | Group size triggering early reduction in dedup. |
| `DEFAULT_ENTITY_CONFIDENCE` | 0.0 | (hard-coded) | Default confidence — NOT 1.0 (D3-010). |
| `ATC_DELIMITER` | `\|` | `DRUGOS_ATC_DELIMITER` | Delimiter for atc_codes string. |
| `DATA_STALENESS_DAYS` | 730 | `DRUGOS_DATA_STALENESS_DAYS` | Staleness threshold (WARNING if older). |
| `ENTITY_NAME_MAX_LENGTH` | 500 | `DRUGOS_ENTITY_NAME_MAX_LENGTH` | Truncation limit for entity names. |
| `ENTITY_RESOLVER_TIMEOUT_SECONDS` | 3600 | `DRUGOS_ENTITY_RESOLVER_TIMEOUT_SECONDS` | Per-operation timeout. |
| `ENTITY_RESOLVER_MAX_LOOKUPS_PER_SECOND` | 10000 | `DRUGOS_ENTITY_RESOLVER_MAX_LOOKUPS_PER_SECOND` | Rate limit on lookups. |
| `ENTITY_RESOLVER_LOG_LEVEL` | INFO | `DRUGOS_ENTITY_RESOLVER_LOG_LEVEL` | Log level for the resolver module. |
| `ENTITY_RESOLVER_CONFIG_VERSION` | 1.1.0 | (hard-coded) | Config schema version. |
| `ENTITY_RESOLVER_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | 100 | `DRUGOS_ER_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | Failures before circuit opens. |
| `ENTITY_RESOLVER_CIRCUIT_BREAKER_RESET_SECONDS` | 60 | `DRUGOS_ER_CIRCUIT_BREAKER_RESET_SECONDS` | Seconds before circuit closes. |
| `ENTITY_RESOLVER_LRU_CACHE_SIZE` | 100000 | `DRUGOS_ENTITY_RESOLVER_LRU_CACHE_SIZE` | LRU cache size for hot lookups. |

---

## Scientific Decisions

1. **Canonical Compound ID = InChIKey** (project doc §3, §12-Risk-1). IUPAC international standard, database-independent.
2. **Canonical Disease ID = DOID** when available, else fall back to MESH / OMIM / EFO / HP / ORPHANET / SNOMED CT / ICD-10 (FHIR-compatible — D14-012).
3. **Canonical Gene ID = NCBI Gene ID** (integer). Ensembl IDs and HGNC symbols are aliases only.
4. **Canonical Protein ID = UniProt primary accession**. Secondary accessions are aliases.
5. **Three-tier confidence** (see `config.flag_entity_confidence`):
   - `high_conf` (≥0.95): stored, full downstream trust.
   - `low_conf_flag` (0.85–0.95): stored, flagged for filtering.
   - `low_conf_warn` (0.50–0.85): stored, warning logged.
   - `rejected` (<0.50): NEVER stored, dead-letter queued.

---

## FAQ

**Q: Why does `lookup_canonical_id` return None for a DrugBank ID I just added?**

A: The lookup applies safety filters by default — it skips mappings with `needs_review=True` or with safety flags like `withdrawn`/`deprecated`. To see ALL matches, pass `min_confidence=0.0, exclude_needs_review=False`:

```python
cid = resolver.lookup_canonical_id(
    "Compound", "drugbank_id", "DB00945",
    min_confidence=0.0,
    exclude_needs_review=False,
)
```

**Q: Why was my drug rejected?**

A: Check `resolver.dead_letter` — every rejected record produces a dead-letter entry with the reason. Common reasons: `withdrawn`, `deprecated`, `missing_drugbank_id`, `low_confidence_rejected`, `canonical_id_conflict`.

**Q: How do I see the dead-letter queue?**

A: `resolver.dead_letter` (list of dicts) or filter the resolver's logs at WARNING level.

**Q: How do I export mappings to Neo4j?**

A: `resolver.to_cypher("/tmp/mappings.cypher")` produces UNWIND+MERGE statements ready to feed to Neo4j.

**Q: Why is `merge_duplicate_edges` slow on 5M edges?**

A: The in-memory `merge_duplicate_edges` loads all edges into a `defaultdict`. For >100K edges, use `merge_duplicate_edges_streaming` which requires sorted input but uses O(1) memory per group.

**Q: How do I make the resolver deterministic across runs?**

A: Pass `seed=42` (or any int) to `EntityResolver(seed=42)`. The resolver calls `set_global_seed(seed)` in `__init__`. Combined with the `sorted()` iteration everywhere, two runs on the same data produce the same output.

---

## Troubleshooting

### "ResolverDataQualityError: drug_records missing 'drugbank_id' field"

Your DrugBank records don't conform to the `DRUGBANK_KG_BUILDER_FIELDS` schema. Verify the first record has the `drugbank_id` key. The schema drift guard (D14-017) refuses to process records that don't have this mandatory field.

### "ResolverProvenanceError: Provenance missing required field"

Every `EntityMapping` MUST carry a `Provenance` block. If you're constructing mappings directly (not via the resolver), pass `provenance=Provenance(...)` explicitly.

### "ResolverConflictError: Cannot merge different canonical_ids"

You tried to merge two mappings with different `canonical_id` values. The `merge()` method requires both mappings to share the same canonical_id. To merge by InChIKey across different canonical_ids, use `resolver.merge_mappings_by_inchikey()` instead.

### "Circuit breaker open"

The lookup rate exceeded `ENTITY_RESOLVER_CIRCUIT_BREAKER_FAILURE_THRESHOLD` failures. Wait `ENTITY_RESOLVER_CIRCUIT_BREAKER_RESET_SECONDS` (default 60s) or call `resolver._record_success()` to reset.

---

## Migration Notes

### From v1.0.0 to v1.1.0

- `EntityMapping` is now **frozen** (immutable). Use `dataclasses.replace()` to modify.
- `EntityMapping.confidence` default changed from `1.0` to `0.0`. Always pass confidence explicitly.
- `Provenance` is now **mandatory** on every `EntityMapping`.
- `lookup_canonical_id` now takes keyword-only args: `min_confidence`, `exclude_needs_review`, `exclude_safety_flags`.
- `get_resolution_stats()` now returns a richer schema: `total`, `resolved`, `unresolved`, `needs_review`, `with_cross_refs`, `avg_cross_refs` (per entity type).
- The `__main__` block was removed; use `python scripts/smoke_test_entity_resolver.py` instead.

### Backward Compatibility

All public method names and signatures are preserved from v1.0.0 (additive only). Existing callers in `run_pipeline.py`, `kg_builder.py`, `graph_queries.py`, and `pyg_builder.py` continue to work without modification.

---

## Performance Tuning

### For Large Graphs (>1M edges)

1. Use `merge_duplicate_edges_streaming()` instead of `merge_duplicate_edges()`. Requires input sorted by `(src_id, rel_type, dst_id)`.
2. Increase `ENTITY_RESOLVER_LRU_CACHE_SIZE` (default 100K) if you have hot lookups.
3. Set `EDGE_DEDUP_EARLY_REDUCTION_THRESHOLD` (default 1000) to a value matching your group-size 99th percentile.
4. Set `ENTITY_RESOLVER_LOG_LEVEL=WARNING` to reduce log volume.

### For Memory-Constrained Environments

1. Set `EDGE_DEDUP_EARLY_REDUCTION_THRESHOLD=100` (more aggressive early reduction).
2. Use `merge_duplicate_edges_streaming()`.
3. Set `ENTITY_RESOLVER_LRU_CACHE_SIZE=10000` (smaller cache).

---

## See Also

- `drugos_graph/entity_resolver_DATA_DICTIONARY.md` — schema reference.
- `drugos_graph/entity_resolver_DECISIONS.md` — design decision log.
- `ENTITY_RESOLVER_FIX_PROMPT.md` — the 188-finding forensic audit prompt.
- `tests/test_entity_resolver.py` — 165-test comprehensive suite.
- `tests/test_fifteen_files_combined.py` — 53-test integration suite across 15 files.
- `scripts/smoke_test_entity_resolver.py` — 14-test smoke script.
