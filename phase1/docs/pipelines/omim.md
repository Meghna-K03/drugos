# OMIM Pipeline

> Gene-phenotype mappings from OMIM (Online Mendelian Inheritance in Man).

This document describes the OMIM pipeline (`pipelines/omim_pipeline.py`),
its data source, schema, scoring model, marker semantics, license terms,
and known limitations. It is intended for engineers, scientists, and
auditors who need to understand exactly how OMIM data flows into the
Autonomous Drug Repurposing Platform.

---

## Data Source

**OMIM** (Online Mendelian Inheritance in Man) is a continuously updated
catalog of human genes, genetic phenotypes, and their relationships. It is
maintained by Johns Hopkins University and is the authoritative source for
Mendelian disease–gene associations.

### Two download paths

1. **`morbidmap.txt` (preferred)** — a tab-delimited file downloaded from
   `https://data.omim.org/downloads/{API_KEY}/morbidmap.txt`. The API key
   is part of the URL **path** (OMIM's downloads endpoint does NOT accept
   `Authorization: ApiKey` headers — any such attempt returns 401).
2. **OMIM REST API (`/api/geneMap`)** — paginated JSON, used as a fallback
   or for programmatic access. Uses the `Authorization: ApiKey` header
   (preferred over the query-string `apiKey` form to avoid CDN/proxy
   logging of the key).

### morbidmap.txt format

- Tab-separated, **no header row**. The file begins with `#`-prefixed
  copyright/credits lines, then immediately data rows.
- Columns: `Phenotype`, `Gene Symbols`, `MIM Number`, `Cyto Location`.
- The phenotype column has the format:
  `"[?*+%]\s?Phenotype Name, MIM_NUMBER (MAPPING_KEY)"`
  where the leading marker is optional and conveys semantic type.

### OMIM phenotype mapping keys (1–4 only)

| Key | Meaning | Evidence strength |
|-----|---------|-------------------|
| 1 | Disorder positioned by mapping of the wild-type gene | Weakest |
| 2 | Disease phenotype itself was mapped | Weak |
| 3 | Molecular basis is known (mutation found in gene) | Strongest |
| 4 | Contiguous gene deletion/duplication syndrome | Strong |

Keys outside `{1, 2, 3, 4}` are malformed and rejected.

### OMIM phenotype markers (leading character of phenotype name)

| Marker | Meaning | association_type |
|--------|---------|------------------|
| `[ ]` | Non-disease (mutation found in healthy individual) | `non_disease` |
| `{ }` | Susceptibility / multifactorial (NOT Mendelian causal) | `susceptibility` |
| `?` | Unconfirmed / provisional | `provisional` |
| `*` | Gene-locus (gene of known sequence with phenotype) | `gene_locus` |
| `+` | Gene of known sequence and phenotype (alt form) | `gene_locus` |
| `%` | Confirmed Mendelian phenotype (locus mapped, gene unknown) | `mendelian_phenotype` |
| (none) | Standard phenotype entry | `causal` (default for mk=3) |

**CRITICAL — patient safety**: susceptibility markers (`{}`) encode
risk-increase, NOT causation. Treating `{BRCA1}`-for-breast-cancer as a
causal GDA produces the exact wrong prediction (BRCA1 inhibition as a
cancer treatment). By default, susceptibility records are routed to a
separate CSV (`omim_gene_disease_susceptibility.csv`) and excluded from
the main GDA load. Downstream ML MUST filter `WHERE is_susceptibility =
False` for repurposing candidates.

### OMIM MIM number ranges

| Range | Meaning |
|-------|---------|
| 100100–299999 | Autosomal loci/phenotypes (created before May 1994) |
| 300000–399999 | X-linked |
| 400000–499999 | Y-linked |
| 500000–599999 | Mitochondrial |
| 600100–999999 | Loci/phenotypes created after May 1994 |

A MIM < 100100 or > 999999 is invalid. MIM = 0 is invalid.

### Cyto-location format

Pattern: `^(\d{1,2}|X|Y)[pq]\d{1,2}(\.\d{1,2})?$` (e.g., `4p16.3`,
`17q21.31`, `Xp21.2`, `Yq11.2`). Malformed cyto-locations pass through
with a WARNING and `cyto_location_valid=False` flag — the field is
metadata, not a join key.

### Inheritance patterns

OMIM phenotype names frequently include inheritance annotations:
`autosomal dominant`, `autosomal recessive`, `X-linked dominant`,
`X-linked recessive`, `Y-linked`, `mitochondrial`, `digenic`,
`triallelic`, `multifactorial`, `somatic`, `sporadic`. These are
extracted via case-insensitive regex into the `inheritance_pattern`
column. They encode different drug-targeting strategies (a recessive LoF
gene is a different target than a dominant GoF gene).

---

## Output Schema

The cleaned DataFrame contains the following columns. All columns are
additive over the legacy schema — none removed.

### Identity
- `gene_symbol` (str, uppercase, HGNC-validated)
- `uniprot_id` (str|None, populated by `load()`)
- `gene_mim` (str|None) — OMIM gene MIM number
- `disease_id` (str, format `"OMIM:{int}"` where `100100 <= int <= 999999`)
- `disease_name` (str, normalized; no leading markers; no trailing comma)
- `disease_id_type` (str, always `"omim"` for OMIM-sourced rows)
- `cyto_location` (str|None, validated format)
- `inheritance_pattern` (str|None, extracted from phenotype_name)

### Association semantics
- `association_modifier` (str|None) — raw leading marker
- `association_type` (str) — derived: `causal`, `susceptibility`,
  `non_disease`, `provisional`, `gene_locus`, `mendelian_phenotype`
- `is_susceptibility` (bool) — True iff `association_modifier == "{}"`
- `mapping_key` (int, in `{1,2,3,4}`)

### Scoring
- `score` (float, in `[0.0, 1.0]`) — derived from `(mapping_key,
  num_pmids, evidence_strength)`
- `score_type` (str, always `"omim_mapping_key"`)
- `score_method` (str, e.g. `"omim_v1_2024-06-15"`)
- `confidence_tier` (str) — `weak` / `moderate` / `strong` (never `high`)
- `confidence_tier_method` (str, `pinero_2020_v1`)
- `evidence_strength` (float|None)
- `normalized_score` (float|None)

### Source & lineage
- `source` (str, always `"omim"`)
- `source_id` (str, format `"OMIM:{gene_mim}_{phenotype_mim}"`)
- `source_version` (str, parsed from morbidmap header `Generated:` line)
- `source_url` (str, sanitized)
- `source_format` (str, `"morbidmap_txt"` or `"api_json"`)
- `download_method` (str, `"morbidmap"` or `"api"`)
- `download_date` (str, ISO-8601 UTC)
- `schema_version` (str, `"2.0"`)
- `pipeline_run_id` (int|None)
- `input_checksum` (str, SHA-256 of the cleaned DataFrame)
- `dedup_strategy` (str, `"validate_gda_scores_dedup"`)
- `canonical_gene_id` (str|None, = `uniprot_id` after resolution)
- `canonical_disease_id` (str, = `disease_id`)
- `as_of_date` (str, ISO date for backfill safety)
- `hgnc_snapshot_version` (str|None)
- `source_record_id` (str|None, SHA-256 of the raw line, truncated to 16 hex)
- `source_line_number` (int|None, 1-indexed for morbidmap records)
- `transformations` (str, JSON list of applied transformation steps)

### Validator-emitted (prefixed with `_`)
- `_score_was_clipped` (bool)
- `_original_score` (float|None)
- `_score_was_coerced_nan` (bool)
- `_score_direction` (str|None, always `"positive"` for OMIM)
- `_disease_name_was_filled` (bool)
- `_association_type_was_filled` (bool)

### Optional
- `pmid_list` (str|None)
- `original_pmid_count` (int|None)
- `pmid_list_was_capped` (bool)
- `year_initial`, `year_final` (int|None)

---

## Scoring Model

Score is a function of `(mapping_key, num_pmids, evidence_strength)`:

```
base = SCORE_BY_MAPPING_KEY.get(mapping_key, 0.4)
       # mk=3 → 0.9 (molecular basis known)
       # mk=4 → 0.8 (contiguous gene syndrome)
       # mk=2 → 0.6 (phenotype mapped)
       # mk=1 → 0.5 (wild-type gene mapped)
pmid_bonus = min(0.05 * log1p(num_pmids), 0.08)        # cap at +0.08
evidence_bonus = min(evidence_strength * 0.05, 0.05)   # cap at +0.05
score = clip(base + pmid_bonus + evidence_bonus, 0.0, 1.0)
```

**Rationale**: mk=3 (molecular basis known) is the strongest single
signal; supplementary PMID count and evidence_strength add modest uplift.
We deliberately cap bonuses so a single weak paper can't inflate a
weakly-mapped record past a strongly-mapped one.

### Confidence tier

Derived from `score` via the shared `cleaning.confidence.classify_confidence`:

| Score range | Tier |
|-------------|------|
| [0.0, 0.06) | weak |
| [0.06, 0.3) | moderate |
| [0.3, 1.0] | strong |

This matches the Piñero et al. 2020 DisGeNET evidence tiers. OMIM and
DisGeNET scores are not directly comparable — OMIM uses mapping-key-based
evidence weighting; DisGeNET uses the published Piñero 2020 DSGP score.
Downstream ML should normalize per-source.

---

## Idempotency

- Running `clean()` twice on the same input produces **byte-identical** CSV
  output (deterministic mergesort by `(gene_symbol, disease_id, source)`).
- Running `load()` twice on the same cleaned data creates **no duplicate**
  DB rows (unique constraint on `(gene_symbol, disease_id, source)` +
  `dedup_already_done=True`).
- All randomness is seeded (`OMIM_RANDOM_SEED=42`).
- All timestamps are UTC ISO-8601.
- All file writes are atomic (`.tmp` + `os.replace`).

---

## Manifest

Each `clean()` run writes a manifest at
`processed_data/omim_gene_disease_associations.csv.manifest.json` with:

- `primary_source`, `license`
- `pipeline_run_id`, `input_checksum`, `output_csv_sha256`, `source_sha256`
- `source_version`, `source_url`, `source_format`, `download_method`
- `schema_version`, `download_date`
- `row_count`, `column_count`, `columns`
- `filter_criteria`, `exclude_susceptibility`, `mapping_keys_include`
- `hgnc_snapshot_version`
- `clean_started_at`, `clean_finished_at`
- `load_completed_at`, `rows_upserted` (populated by `load()`)

`load()` reads the manifest and verifies the on-disk CSV's SHA-256 matches
`output_csv_sha256`. If mismatched, it refuses to load.

---

## Quarantine & Dead-Letter

Malformed records are routed to:

- `processed_data/omim_quarantine.jsonl` — parse failures (malformed
  morbidmap lines, out-of-range MIMs, invalid mapping keys).
- `processed_data/dead_letter/omim_unresolved_*.csv` — gene symbols that
  could not be resolved to a UniProt ID.
- `dead_letter_gda` DB table — same unresolved records, for DB-level
  auditability.
- `processed_data/omim_disgenet_overlap.jsonl` — OMIM-direct rows that
  duplicate DisGeNET rows (post-load dedup).

Each entry includes the reason, the original record, and the source line
number for traceability.

---

## Configuration

All `OMIM_*` env vars are documented in `config/settings.py` and
registered in `CONFIG_REGISTRY`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `OMIM_API_KEY` | `""` | OMIM API authentication key (UUID format) |
| `OMIM_API_BASE` | `https://api.omim.org/api` | OMIM REST API base URL |
| `OMIM_REQUEST_INTERVAL` | `0.25` | Seconds between API requests (4 req/sec) |
| `OMIM_MAPPING_KEYS_INCLUDE` | `[3, 4]` | Which mapping keys to include |
| `OMIM_API_PAGE_LIMIT` | `1000` | API pagination page size (max 1000) |
| `OMIM_API_MAX_RETRIES` | `5` | Max HTTP retries on 429/5xx |
| `OMIM_DOWNLOAD_TIMEOUT` | `300` | HTTP timeout for morbidmap download |
| `OMIM_API_TIMEOUT` | `120` | HTTP timeout per API request |
| `OMIM_OUTPUT_FILENAME` | `omim_gene_disease_associations.csv` | Output filename |
| `OMIM_MIN_EXPECTED_RECORDS` | `5000` | Min parsed-record count (catches truncation) |
| `OMIM_MAX_PAGINATION_PAGES` | `1000` | Upper bound on pagination pages |
| `OMIM_CONFIRMED_SCORE` | `0.9` | Base score for mk=3 |
| `OMIM_CONTIGUOUS_SCORE` | `0.8` | Base score for mk=4 |
| `OMIM_PHENOTYPE_MAPPED_SCORE` | `0.6` | Base score for mk=2 |
| `OMIM_GENE_MAPPED_SCORE` | `0.5` | Base score for mk=1 |
| `OMIM_USER_AGENT` | `drug-repurposing-pipeline/omim (...)` | User-Agent header |
| `OMIM_API_KEY_FORMAT_RE` | `^[a-f0-9-]{36}$` | UUID format regex |
| `OMIM_MAX_AGE_DAYS` | `30` | Max cached-download age |
| `OMIM_DB_BATCH_SIZE` | `1000` | Bulk upsert batch size |
| `OMIM_EXCLUDE_SUSCEPTIBILITY` | `True` | Route `{}` records to separate CSV |
| `OMIM_JSON_PRETTY` | `False` | Pretty-print intermediate JSON (dev only) |
| `OMIM_RANDOM_SEED` | `42` | Random seed for backoff jitter |

---

## License

OMIM data is licensed under OMIM's terms of use:
<https://omim.org/help/agreement>.

The output CSV is marked `license = "OMIM-restricted"` in the manifest.
Downstream consumers must verify they hold a valid OMIM license before
reading this data.

---

## Retention Policy

`OMIM_RETENTION_DAYS = 365` (configurable). A cleanup script
`scripts/omim_retention_cleanup.py` (planned) deletes CSVs older than
this. Run weekly via Airflow.

---

## Known Limitations

1. **No historical snapshots**: OMIM does not expose historical versions
   of `morbidmap.txt`. `run_backfill(start_date, end_date)` is a documented
   no-op — it logs a WARNING and returns 0.
2. **HGNC validation is best-effort**: if the HGNC approved-symbols file
   is missing, the pipeline proceeds without HGNC validation (OMIM is the
   source of truth for some recently-added symbols).
3. **Cross-source dedup with DisGeNET**: OMIM-direct rows that duplicate
   DisGeNET rows are deleted post-load. This is safe because DisGeNET-curated
   already includes ~80% of morbidmap with richer scoring.
4. **`gene_mim` is not used as a resolver key** (the proteins table does
   not have a `gene_mim` column). Resolution is via `gene_symbol` only.
   Unresolved symbols are routed to dead-letter.
5. **API path requires the same `OMIM_API_KEY`** as the morbidmap path —
   there is no anonymous access.

---

## Audit Trail

Every fix in this pipeline references its audit finding ID in a code
comment (e.g., `# BUG-3.1`, `# BUG-2.8`). To verify all 131 findings are
addressed:

```bash
grep -c "BUG-" pipelines/omim_pipeline.py
```

The forensic audit document (`OMIM_PIPELINE_FORENSIC_AUDIT.md`) lists
every finding with its ID, severity, location, and recommended fix.
