# Entity Resolver — Data Dictionary

**Module:** `drugos_graph/entity_resolver.py`
**Version:** 1.1.0
**Schema Version:** 1.1.0

This document is the canonical reference for every type, field, and
data structure exposed by the entity resolver. If a downstream
consumer (KG builder, Graph Transformer, RL ranker) needs to know the
exact shape of a mapping or edge, this is the source of truth.

---

## 1. Enums

### `EntityType`

| Value | Description | Canonical ID System |
|-------|-------------|---------------------|
| `COMPOUND` | Chemical compound (drug) | InChIKey |
| `DISEASE` | Medical condition | DOID (fallback: MESH/OMIM/EFO/HP/ORPHANET/SNOMED CT/ICD-10) |
| `GENE` | Gene | NCBI Gene ID (integer) |
| `PROTEIN` | Protein | UniProt primary accession |
| `PATHWAY` | Biological pathway | Reactome ID |

### `IdSystem`

| Value | Used For | Multi-valued? |
|-------|----------|---------------|
| `inchikey` | Compound | No (string) |
| `drugbank_id` | Compound | No |
| `chembl_id` | Compound | No |
| `pubchem_cid` | Compound | No |
| `chebi_id` | Compound | No |
| `drkg_id` | All types | No |
| `atc_code` | Compound | Yes (list) |
| `doid` | Disease | No |
| `omim_id` | Disease | No |
| `mesh_id` | Disease | No |
| `efo_id` | Disease | No |
| `hpo_id` | Disease | No |
| `orphanet_id` | Disease | No |
| `snomed_ct` | Disease | No |
| `icd_10` | Disease | No |
| `ncbi_gene_id` | Gene, Protein | No |
| `ensembl_id` | Gene | No |
| `hgnc_id` | Gene | No |
| `uniprot_id` | Protein | No |
| `gene_symbol` | Protein | No |
| `gene_id_other` | Protein | No (non-numeric gene IDs) |
| `secondary_accessions` | Protein | Yes (list) |
| `reactome_id` | Pathway | No |
| `kegg_id` | Pathway | No |

### `ConflictResolution`

| Value | Description |
|-------|-------------|
| `max_confidence` | Keep edge with highest `confidence` from group. |
| `union` | Keep all edges (no dedup). |
| `average` | Average `confidence` across group; preserve `evidence_count`. |

---

## 2. Dataclasses

### `Provenance` (frozen)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `_source` | `str` | Yes | Upstream source name (e.g. "DrugBank"). |
| `_source_version` | `str` | Yes | Source version (e.g. "5.1.10"). |
| `_parsed_at` | `str` | Yes | ISO-8601 UTC timestamp when source record was parsed. |
| `_parser_version` | `str` | Yes | Parser version (e.g. "drugbank_parser:2.3.0"). |
| `_input_checksum` | `str` | Yes | SHA-256 hex digest of source record. |
| `_license` | `str` | Yes | License (e.g. "CC BY-NC 4.0"). |
| `_attribution` | `str` | Yes | Citation string. |
| `_schema_version` | `str` | Default | EntityResolver SCHEMA_VERSION ("1.1.0"). |
| `_resolver_version` | `str` | Default | EntityResolver RESOLVER_VERSION ("1.1.0"). |
| `_created_at` | `str` | Default | ISO-8601 UTC timestamp at construction. |

### `EntityMapping` (frozen)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `canonical_type` | `EntityType` | (required) | Type of this entity. |
| `canonical_id` | `str` | (required) | Canonical identifier. |
| `name` | `str` | `""` | Sanitized human-readable name. |
| `aliases` | `Dict[str, Union[str, List[str]]]` | `{}` | Cross-database ID aliases. |
| `confidence` | `float` | `0.0` | Confidence in [0.0, 1.0]. |
| `needs_review` | `bool` | `False` | True if a human must verify. |
| `safety_flags` | `frozenset[str]` | `frozenset()` | E.g. `{"withdrawn", "deprecated", "illicit"}`. |
| `provenance` | `Optional[Provenance]` | `None` | Source metadata. **Mandatory** (raises if None). |
| `_checksum` | `str` | (computed) | SHA-256 of (canonical_type, canonical_id, name, aliases, confidence). |

**Validation rules (in `__post_init__`):**
- `confidence` must be in [0.0, 1.0] (raises `ResolverConfigurationError`).
- `provenance` must not be None (raises `ResolverProvenanceError`).
- For `Compound`: `canonical_id` is `.upper()`-normalized.
- All alias string values are `.strip()`-ed; InChIKey aliases `.upper()`-ed.
- Alias list values: empty strings filtered, others stripped.
- Alias values of unsupported types (not str, not list) raise `ResolverDataQualityError`.

### `EntityMappingBuilder`

Fluent builder for `EntityMapping`. Methods (all return `self`):

| Method | Description |
|--------|-------------|
| `.with_entity_type(et)` | Set EntityType. |
| `.with_canonical_id(cid)` | Set canonical_id. |
| `.with_name(name)` | Set name. |
| `.with_alias(system, value)` | Add alias. |
| `.with_confidence(c)` | Set confidence. |
| `.needs_review(flag=True)` | Set needs_review. |
| `.with_safety_flag(flag)` | Add a safety flag. |
| `.with_provenance(p)` | Set provenance. |
| `.build()` | Construct the EntityMapping. |

---

## 3. Stats Dict Schema

### `get_resolution_stats()` Return Value

```python
{
    "Compound": {
        "total": int,             # total mappings
        "resolved": int,          # not needs_review
        "unresolved": int,        # in self.unresolved
        "needs_review": int,      # mappings flagged needs_review
        "with_cross_refs": int,   # mappings with >1 alias
        "avg_cross_refs": float,  # rounded to 2 decimals
    },
    "Disease": {...},
    "Gene": {...},
    "Protein": {...},
}
```

### `resolve_compounds_from_drugbank()` Return Value

```python
{
    "total": int,                    # input record count
    "resolved": int,                 # successfully stored
    "rejected_withdrawn": int,
    "rejected_deprecated": int,
    "rejected_low_confidence": int,
    "skipped_no_id": int,
    "duplicates_detected": int,
    "conflicts_detected": int,
}
```

### `resolve_compounds_from_drkg()` Return Value

```python
{
    "total_drkg_compounds": int,
    "matched": int,                  # linked to existing canonical
    "unmatched": int,                # created UNRESOLVED:DRKG:... placeholder
    "skipped_nan": int,
    "rejected_low_confidence": int,
}
```

### `resolve_diseases_from_drkg()` Return Value

```python
{
    "total_diseases": int,
    "mapped": int,                   # recognized prefix
    "unmapped": int,                 # unknown prefix
    "skipped_nan": int,
}
```

### `resolve_genes_from_drkg()` Return Value

```python
{
    "total_genes": int,
    "resolved": int,                 # NCBI Gene ID
    "unresolved": int,               # ENSG/HGNC/unknown
    "skipped_nan": int,
}
```

### `resolve_proteins_from_uniprot()` Return Value

```python
{
    "total_proteins": int,
    "mapped": int,
    "skipped_no_accession": int,
    "with_gene_link": int,           # has ncbi_gene_id alias
    "duplicates_detected": int,
    "conflicts_detected": int,
}
```

### `merge_mappings_by_inchikey()` Return Value

```python
{
    "groups_total": int,
    "groups_merged": int,
    "mappings_before": int,
    "mappings_after": int,
    "conflicts_detected": int,
}
```

### `delete_entity()` Return Value

```python
{
    "mappings_removed": int,         # 0 or 1
    "reverse_entries_removed": int,
    "unresolved_removed": int,
    "lineage_entries_removed": int,
}
```

### `health_check()` Return Value

```python
{
    "mappings_count": int,
    "unresolved_count": int,
    "dead_letter_count": int,
    "last_updated": str,             # ISO-8601 UTC
    "schema_version": str,           # "1.1.0"
    "resolver_version": str,         # "1.1.0"
    "circuit_open": bool,
    "config_hash": str,
}
```

---

## 4. Edge Dict Schema

Output of `build_gene_protein_edges()` and the canonical shape after
`merge_duplicate_edges()`:

```python
{
    "src_id": str,                   # source canonical_id
    "dst_id": str,                   # destination canonical_id
    "src_type": str,                 # e.g. "Gene"
    "dst_type": str,                 # e.g. "Protein"
    "rel_type": str,                 # e.g. "encodes"
    "source": str,                   # top-level (D15-003)
    "confidence": float,             # in [0, 1] (D15-004)
    "evidence_count": int,           # number of sources contributing (D15-009)
    "sources": str,                  # pipe-joined, sorted (D7-001, D15-009)
    "props": dict,                   # optional metadata
}
```

---

## 5. Dead-Letter Entry Schema

```python
{
    "entity_type": str,              # "Compound", "Disease", etc.
    "reason": str,                   # e.g. "withdrawn", "missing_drugbank_id"
    "timestamp": str,                # ISO-8601 UTC
    "record_preview": str,           # PII-safe repr, max 200 chars
    "extra": dict,                   # optional context (e.g. drugbank_id_prefix)
}
```

Common `reason` values:
- `missing_drugbank_id` — record had no `drugbank_id` field.
- `withdrawn` — drug was withdrawn from market.
- `deprecated` — drug was deprecated or terminated.
- `low_confidence_rejected` — confidence below reject threshold.
- `canonical_id_conflict` — two records claimed the same canonical_id but couldn't merge.
- `merge_conflict` — `EntityMapping.merge()` raised `ResolverConflictError`.
- `missing_accession` — UniProt record had no `accession` field.
- `unmatched_low_confidence` — DRKG compound unmatched AND below reject threshold.
- `mapping_construction_failed` — `EntityMapping.__init__` raised.

---

## 6. Transformation Log Entry Schema

```python
{
    "action": str,                   # "created", "merged_by_inchikey",
                                     # "conflict_detected", "deleted",
                                     # "created_unresolved"
    "entity_type": str,              # optional
    "canonical_id": str,             # optional (full)
    "canonical_id_prefix": str,      # optional (first 8 chars, PII-safe)
    "id_system": str,                # optional
    "external_id_prefix": str,       # optional (first 4 chars)
    "canonical_ids": list[str],      # optional (for conflicts)
    "canonical_id_survivor_prefix": str,  # for merges
    "canonical_id_merged_prefix": str,    # for merges
    "inchikey_prefix": str,          # for InChIKey merges
    "timestamp": str,                # ISO-8601 UTC (always present)
    "resolver_version": str,         # "1.1.0"
    "schema_version": str,           # "1.1.0"
}
```

---

## 7. Audit Log Entry Schema

Audit logs go to the `drugos.audit.entity_resolver` logger. Every
public method call writes at least one entry. Structured as
`logger.info("event_name", extra={...})`. Common `event_name` values:

- `entity_resolver_initialized`
- `entity_resolver_config_snapshot`
- `resolved_compounds_from_drugbank`
- `resolved_compounds_from_drkg` (also: `drkg_compound_resolution`)
- `resolved_diseases_from_drkg`
- `resolved_genes_from_drkg`
- `resolved_proteins_from_uniprot`
- `built_gene_protein_edges`
- `edge_dedup_complete`
- `merged_compounds_by_inchikey`
- `saved_mappings`
- `loaded_mappings`
- `cypher_export_complete`
- `lineage_exported`
- `conflict_detected`
- `dead_letter`
- `call_order_violation`
- `duplicate_drugbank_id`
- `invalid_inchikey_format`
- `canonical_id_validation_failed`
- `phantom_gene_protein_edge_skipped`
- `stale_source_data`
- `non_public_id_detected`
- `circuit_breaker_opened`
- `max_confidence_is_zero`
- `malformed_edge_skipped`
- `match_rate_below_threshold`
- `lookup_miss`
- `error_reported`

---

## 8. Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `SCHEMA_VERSION` | `"1.1.0"` | EntityResolver schema version. |
| `RESOLVER_VERSION` | `"1.1.0"` | EntityResolver implementation version. |
| `PUBLIC_ID_REGEX` | (regex) | Whitelist of public biomedical ID patterns (D9-003). |
| `_UNIPROT_AC_REGEX` | (regex) | UniProt accession pattern (D5-017). |

---

## 9. File Layout

| File | Purpose |
|------|---------|
| `drugos_graph/entity_resolver.py` | Main module (~2,200 lines). |
| `drugos_graph/entity_resolver_README.md` | This file's README. |
| `drugos_graph/entity_resolver_DATA_DICTIONARY.md` | This file. |
| `drugos_graph/entity_resolver_DECISIONS.md` | Design decision log. |
| `tests/test_entity_resolver.py` | Comprehensive test suite (165 tests). |
| `tests/test_fifteen_files_combined.py` | 15-file integration test (53 tests). |
| `scripts/smoke_test_entity_resolver.py` | 14-test smoke script. |

---

## 10. Versioning

The schema is versioned via `SCHEMA_VERSION` (currently `"1.1.0"`).
Every `EntityMapping` carries `_schema_version` in its `Provenance`
block, so any schema change is detectable on load.

Breaking changes require a `SCHEMA_VERSION` bump and a corresponding
entry in `entity_resolver_DECISIONS.md`.
