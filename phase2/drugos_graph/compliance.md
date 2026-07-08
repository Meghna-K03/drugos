# Regulatory Compliance Documentation

> Fixes audit issue 14.7 — FDA/HIPAA/GDPR compliance documentation.

## Scope

This document describes how the DrugOS label mapping
(`drugos_graph/utils.py`) complies with FDA 21 CFR Part 11, HIPAA, and
GDPR requirements for audit trails, data lineage, and schema
documentation.

The DrugOS platform is a research tool for drug repurposing hypothesis
generation. It is **not** a medical device and does not provide medical
advice. However, because its outputs may inform downstream clinical
research, the label mapping layer is held to regulatory-grade audit
and lineage standards.

## FDA 21 CFR Part 11 (Electronic Records; Electronic Signatures)

### Audit trails

Every change to the label mapping is logged to
`logs/audit/label_map_changes.jsonl` via the `commit_label_map_change`
function. Each record includes:

- `timestamp` (UTC ISO 8601)
- `event` (always `"label_map_changed"`)
- `change_type` (`added_entry`, `removed_entry`, `renamed_entry`,
  `metadata_update`)
- `before` (previous value, or `null` for `added_entry`)
- `after` (new value, or `null` for `removed_entry`)
- `rationale` (human-readable reason)
- `audit_issue` (audit issue ID this change resolves, e.g., `"3.1"`)
- `actor` (git author email)
- `label_map_version` (semantic version after the change)
- `label_map_hash` (SHA-256 content hash after the change)

Records are append-only JSONL. Tampering with the file is detectable
via cross-reference with git history.

### Schema versioning

`LABEL_MAP_VERSION` (currently `"1.0.0"`) tracks schema evolution.
The version is stored in the Neo4j graph as a database property at
pipeline start via `store_label_map_metadata_in_graph()`. Operators
can query:

```cypher
CALL dbms.graphproperty('label_map_version') YIELD value RETURN value
CALL dbms.graphproperty('label_map_hash') YIELD value RETURN value
```

If the graph's stored version differs from the code version,
`check_label_map_version_matches_graph()` raises `RuntimeError`,
blocking the pipeline until `migrate_labels()` is run.

Version bumps follow semantic versioning:

- **MAJOR** (e.g., 1.0.0 → 2.0.0): breaking change (entry removed or
  renamed). Requires migration.
- **MINOR** (e.g., 1.0.0 → 1.1.0): additive change (new entry added).
  No migration needed.
- **PATCH** (e.g., 1.0.0 → 1.0.1): metadata change (ontology version
  updated). No migration needed.

### Reproducibility

`LABEL_MAP_HASH` (SHA-256 of the sorted forward mapping, first 16 hex
chars) ensures that the same code + config produces the same graph.
Two pipeline runs with the same hash produce byte-identical results.

The hash is verified at pipeline start via
`verify_label_map_integrity()`. If the recomputed hash differs from
the stored `LABEL_MAP_HASH`, the pipeline aborts with `RuntimeError`
(indicating tampering or a bug).

## HIPAA (Health Insurance Portability and Accountability Act)

### PHI handling

The label mapping itself does not directly store PHI (Protected Health
Information). However, error messages and logs may include
user-supplied identifiers (e.g., a drug name mistyped as a patient
name) that could contain PHI.

**Mitigation:** PII redaction is applied to all error messages and
logs via `_redact_pii()`. The following patterns are redacted:

- SSN (`\b\d{3}-\d{2}-\d{4}\b`) → `[SSN]`
- Email (`\b[\w.+-]+@[\w-]+\.[\w.-]+\b`) → `[EMAIL]`
- Phone (`\b\+?\d{1,3}?[-.\s]?\(?\d{1,4}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}\b`) → `[PHONE]`

Additionally, all identifiers are truncated to 100 characters and
`repr`-escaped before being included in error messages (issue 4.9).

### Access control

The underlying label dict (`_RAW_LABEL_ENTRIES`) is private (underscore
prefix). Public access is via the `LABEL_REGISTRY` instance and the
`DRKG_NODE_TYPE_TO_NEO4J_LABEL` / `NEO4J_LABEL_TO_DRKG_NODE_TYPE`
mappings (which are `MappingProxyType` — read-only).

In multi-tenant deployments, the `LabelRegistry` class can be
subclassed to add ACL checks on `lookup()` and `reverse_lookup()`.
This is documented in the class docstring.

### Audit log retention

Audit logs at `logs/audit/sanitization_failures.jsonl` and
`logs/audit/label_map_changes.jsonl` are append-only JSONL. Retention
policy is 6 years (HIPAA requirement for audit trails). Configure log
rotation at the OS level to enforce retention.

## GDPR (General Data Protection Regulation)

### Right to erasure (Article 17)

The label mapping itself does not store personal data. If a deletion
request affects data already loaded into the Neo4j graph, the
migration framework (`migrate_labels()`) supports label renaming as
part of the erasure workflow:

```python
from drugos_graph.utils import migrate_labels
migrate_labels(builder, {"PatientName": "_ERASED_SUBJECT"})
```

This anonymizes affected nodes without deleting the graph structure.

### Data lineage (Article 30)

Full transformation logs at
`logs/transformations/sanitization.jsonl` allow tracing any value in
the output back to its source. Each record includes:

- `timestamp` (UTC ISO 8601)
- `event` (`"sanitization_transformed"`)
- `kind` (identifier kind)
- `original` (truncated to 1000 chars)
- `sanitized` (truncated to 1000 chars)
- `context` (caller-provided dict — typically includes batch_index,
  row_id, file, correlation_id)
- `label_map_hash`
- `label_map_version`

### Provenance metadata (Article 5(2))

`LABEL_MAP_METADATA` records the source, version, and audit report
reference for the label mapping. This metadata is stored in the Neo4j
graph as a database property at pipeline start, providing end-to-end
provenance from code to deployed graph.

## Audit Trail Format

All audit records are JSONL with the following schemas:

### `logs/audit/label_map_changes.jsonl`

```json
{
  "timestamp": "2026-06-17T12:34:56.789+00:00",
  "event": "label_map_changed",
  "change_type": "added_entry",
  "before": null,
  "after": {"MedDRA_Term": "MedDRATerm"},
  "rationale": "Fixes audit issue 3.1 — canonical SIDER endpoint",
  "audit_issue": "3.1",
  "actor": "manoj@teamcosmic.dev",
  "label_map_version": "1.0.0",
  "label_map_hash": "b8ffc66807f8372a"
}
```

### `logs/audit/sanitization_failures.jsonl`

```json
{
  "timestamp": "2026-06-17T12:34:56.789+00:00",
  "event": "sanitization_failure",
  "kind": "label",
  "reason": "empty or invalid first char",
  "name_length": 0,
  "name_prefix": "''",
  "context": {"batch_index": 42, "row_id": "DB00001"},
  "label_map_hash": "b8ffc66807f8372a"
}
```

### `logs/transformations/sanitization.jsonl`

```json
{
  "timestamp": "2026-06-17T12:34:56.789+00:00",
  "event": "sanitization_transformed",
  "kind": "identifier",
  "original": "Side Effect",
  "sanitized": "Side_Effect",
  "context": {"batch_index": 42, "row_id": "DB00001"},
  "label_map_hash": "b8ffc66807f8372a",
  "label_map_version": "1.0.0"
}
```

### `data/dead_letter/labels.jsonl`

```json
{
  "timestamp": "2026-06-17T12:34:56.789+00:00",
  "event": "identifier_quarantined",
  "kind": "label",
  "original": "123Foo",
  "context": {"batch_index": 42, "row_id": "DB00001"},
  "label_map_hash": "b8ffc66807f8372a",
  "label_map_version": "1.0.0"
}
```

## Change Management

Any change to `drugos_graph/utils.py` that affects the label mapping
MUST:

1. Be accompanied by a regression test that would fail if the change
   were reverted.
2. Update `LABEL_MAP_VERSION` if the change is schema-breaking.
3. Update `utils_FIXLOG.md` with the audit issue ID resolved.
4. Be reviewed by someone who understands the RL safety ranker's
   dependency on adverse-event frequencies.
5. Be recorded in `logs/audit/label_map_changes.jsonl` via
   `commit_label_map_change()` (called from a pre-commit hook).

## Contact

For compliance questions, contact:

- **Manoj** (Product & Tech Lead): manoj@teamcosmic.dev
- **Rohan** (Data & Research): rohan@teamcosmic.dev

---

## DRKG Compliance

> Added by the `drkg_loader` v2.0 audit fix (drkg_loader_repair_prompt.md
> — Domain 14 Compliance, GUARD 14.6).
>
> This section documents how the DRKG loader complies with license,
> attribution, FAIR, schema-versioning, data-freshness, and
> sensitive-data policies. An external reviewer (FDA, pharma partner
> due-diligence) reading this section should be able to answer:
>
> 1. **License?** — MIT.
> 2. **Attribution?** — Himmelstein et al., 2020, Sci Data 7:329.
> 3. **FAIR URIs?** — identifiers.org URIs on every row.
> 4. **Schema version?** — `2.0.0`, in `df.attrs['schema_version']`.
> 5. **Data freshness?** — 365 days; WARNING if cache > 1.5x that.
> 6. **Sensitive data?** — rare-disease rows tagged `sensitive=True`.

### License

The DRKG dataset is released under the **MIT License**. This is the
most permissive open-source license, allowing commercial use,
modification, and redistribution with attribution.

The license string is propagated in `df.attrs['license']` (always
`"MIT"`) on every DataFrame returned by `parse_drkg_tsv`. Downstream
exports (Neo4j graph, PyG HeteroData, MLflow artifacts) MUST preserve
this attribution.

### Attribution

The DRKG paper MUST be cited in any derivative work:

> Himmelstein, D. S., Rubinetti, V., Slochower, D. R., Hu, D.,
> Malladi, V. S., Greene, C. S., & Stuart, J. M. (2020).
> **Drug Repurposing Knowledge Graph (DRKG).** *Scientific Data*, 7,
> 329. doi:10.1038/s41597-020-0465-y

The attribution string is propagated in `df.attrs['attribution']`:

```
DRKG (Himmelstein et al., 2020, Sci Data 7:329,
doi:10.1038/s41597-020-0465-y)
```

### FAIR Data Principles (Findable, Accessible, Interoperable, Reusable)

The DRKG loader emits FAIR identifiers.org URIs on every row:

| Entity type | URI prefix | Example URI |
|-------------|-----------|-------------|
| Compound | `drugbank` | `http://identifiers.org/drugbank:DB00107` |
| Gene | `ncbigene` | `http://identifiers.org/ncbigene:5743` |
| Disease | `doid` | `http://identifiers.org/doid:DOID:1438` |
| Anatomy | `uberon` | `http://identifiers.org/uberon:0000000` |
| Pathway | `reactome` | `http://identifiers.org/reactome:R-HSA-12345` |
| Pharmacologic Class | `chebi` | `http://identifiers.org/chebi:12345` |
| Atc | `atc` | `http://identifiers.org/atc:A01AA01` |
| Taxonomy | `ncbitaxon` | `http://identifiers.org/ncbitaxon:9606` |
| Side Effect | `meddra` | `http://identifiers.org/meddra:10000000` |
| Symptom | `meddra` | `http://identifiers.org/meddra:10000001` |
| MedDRA_Term | `meddra` | `http://identifiers.org/meddra:10000002` |
| Biological Process | `go` | `http://identifiers.org/go:0008150` |
| Molecular Function | `go` | `http://identifiers.org/go:0003674` |
| Cellular Component | `go` | `http://identifiers.org/go:0005575` |
| Gene Expression | `geo` | `http://identifiers.org/geo:GSE92649` |

> **NOTE (GEO Loader v1.0.0 institutional-grade fix):** The URI prefix
> `http://identifiers.org/geo:GSE92649` is the pinned GEO series (Cheng
> et al., 2018, Sci Rep). GEO records in this KG are
> `Protein→expressed_in→Anatomy` edges (NOT `Gene→expressed_in→Anatomy` —
> that is DRKG's domain, per `config.py:EDGE_PRODUCERS`). The
> `Gene Expression` entity type in the table above applies to
> DRKG-imported Gene Expression nodes; the GEO loader (this codebase's
> `drugos_graph/geo_loader.py`) emits `Protein` nodes with
> `expressed_in` edges to `Anatomy` (UBERON) nodes. See
> `GEO_LOADER_MASTER_REPAIR_PROMPT.md` Phase 0.2 for the full
> reconciliation.

The full mapping is in `config.DRKG_ENTITY_TYPE_TO_URI_PREFIX`. These
URIs are resolvable via the identifiers.org resolver
(https://identifiers.org/) so downstream consumers can look up the
authoritative metadata for any entity.

### Schema versioning

The DRKG loader uses semantic versioning for both the parser and the
output schema:

- `PARSER_VERSION = "2.0.0"` — bumped on any parse-logic change.
- `SCHEMA_VERSION = "2.0.0"` — bumped on any output-schema change
  (column added / removed / renamed).

Both are recorded in:
- `df.attrs['schema_version']`
- `df.attrs['provenance']['parser_version']` and `['schema_version']`
- `validate_drkg(df)['parser_version']` and `['schema_version']`
- `data/processed/loader_state.json` under the `"drkg"` key

Downstream consumers SHOULD assert
`df.attrs['schema_version'] == "2.0.0"` before processing.

### Data freshness policy

The DRKG publisher refreshes the dataset roughly annually. The loader
enforces the following freshness policy:

- `config.DATA_SOURCES['drkg']['expected_update_frequency_days'] = 365`
- If the cached `drkg.tar.gz` is older than `365 * 1.5 = 547 days`,
  the loader emits a WARNING on every cache-hit suggesting a forced
  re-download.
- The freshness check is implemented in `download_drkg._warn_if_stale`
  and uses `cfg['last_downloaded_at']` (persisted in
  `data/processed/loader_state.json`).

### Sensitive-data policy (rare diseases)

Some DRKG rows reference rare diseases (prevalence < 1 in 2,000 in the
EU per Orphanet designation). These rows are tagged `sensitive=True`
in the parsed DataFrame so downstream exports can aggregate or
suppress them per GDPR / HIPAA.

The rare-disease code prefixes are in
`config.DRKG_RARE_DISEASE_CODES` (sourced from Orphanet's rare-disease
designation list). Currently includes:

- `ORPHANET:` (Orphanet rare-disease codes)
- `ORPHA:` (Orphanet alternate prefix)
- `DOID:635` (DOID rare disease subtree root)
- Selected `MESH:C*` supplementary concepts for rare diseases

Downstream consumers MUST:

1. NOT export raw `sensitive=True` rows to public-facing APIs.
2. Aggregate rare-disease statistics to a minimum cohort size of 10.
3. Suppress any free-text rare-disease names in logs / error messages.

### Audit trail

Every DRKG pipeline run produces the following audit trail:

1. **`data/processed/loader_state.json`** — last-run state (SHA, row
   count, parser version, validation summary).
2. **`logs/transformations/drkg.jsonl`** — per-transformation audit
   log (BOM strip, type-mismatch exclusion, self-loop drop, etc.).
3. **`data/dead_letter/drkg_malformed.jsonl`** — per-row audit log
   for every dropped / malformed record.
4. **`data/checkpoints/drkg_edge_maps.json`** — build checkpoint
   (resumable; includes source SHA for resume-validity check).

An external auditor can verify a given output by:

1. Computing the SHA-256 of `data/raw/drkg/drkg.tsv`.
2. Cross-referencing with `data/processed/loader_state.json['drkg']['input_sha256']`.
3. Cross-referencing with `df.attrs['provenance']['source_sha256']`.
4. Grepping `logs/transformations/drkg.jsonl` for any transformations
   applied to the entity in question.

See `docs/drkg_loader_runbook.md` → "Lineage Walkthrough" for the
full traceability procedure.
