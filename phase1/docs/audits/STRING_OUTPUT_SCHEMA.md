# STRING Pipeline Output Schema

Data dictionary for `protein_protein_interactions.csv` produced by
`pipelines/string_pipeline.py` (v2.0.0 — institutional-grade rewrite).

## Schema source of truth

The output schema is defined in `pipelines/schema/v1.json` under the key
`protein_protein_interactions.csv`. This document is the human-readable
data dictionary that explains each column.

## Output columns

| Column                | Type      | Source                                         | Range / Pattern                          | Required | Notes                                                                              |
|-----------------------|-----------|------------------------------------------------|------------------------------------------|----------|------------------------------------------------------------------------------------|
| `string_id_a`         | str       | `links.protein1` (canonical-ordered)           | `^9606\.ENSP\d+$`                        | YES      | Required by `schema/v1.json`. Canonical-ordered so `string_id_a ≤ string_id_b`.    |
| `string_id_b`         | str       | `links.protein2` (canonical-ordered)           | `^9606\.ENSP\d+$`                        | YES      | Required by `schema/v1.json`.                                                      |
| `uniprot_id_a`        | str / NULL| `aliases.alias` (where `source='UniProt_AC'`)  | UniProt accession pattern                | NO       | Optional per schema. Uppercased. Canonical only (isoforms excluded).               |
| `uniprot_id_b`        | str / NULL| `aliases.alias` (where `source='UniProt_AC'`)  | UniProt accession pattern                | NO       | Optional per schema.                                                               |
| `combined_score`      | int       | `links.combined_score` (filtered ≥ threshold)  | `[0, 1000]`                              | YES      | Sci: STRING's native range. Divide by 1000 for `[0, 1]` ML edge weights.           |
| `source`              | str       | `self.source_name` (= `"string"`)              | `"string"` (controlled vocab)            | YES      | Controlled vocabulary: `DataSourceName.STRING`.                                   |
| `neighborhood`        | int / NaN | `detailed_links.neighborhood`                  | `[0, 1000]` or NaN                       | NO       | NULL if detailed file absent. Phylogenetic signal (Szklarczyk et al. 2023).        |
| `fusion`              | int / NaN | `detailed_links.fusion`                        | `[0, 1000]` or NaN                       | NO       | NULL if detailed file absent. Structural signal (gene fusion events).              |
| `cooccurrence`        | int / NaN | `detailed_links.cooccurrence`                  | `[0, 1000]` or NaN                       | NO       | NULL if detailed file absent. Phylogenetic signal.                                 |
| `coexpression`        | int / NaN | `detailed_links.coexpression`                  | `[0, 1000]` or NaN                       | NO       | NULL if detailed file absent. Transcriptomic signal (tissue-specific repurposing). |
| `experimental_score`  | int / NaN | `detailed_links.experimental`                  | `[0, 1000]` or NaN                       | NO       | DB column. NULL if detailed file absent.                                          |
| `database_score`      | int / NaN | `detailed_links.database`                      | `[0, 1000]` or NaN                       | NO       | DB column. NULL if detailed file absent.                                          |
| `textmining_score`    | int / NaN | `detailed_links.textmining`                    | `[0, 1000]` or NaN                       | NO       | DB column. NULL if detailed file absent.                                          |
| `score_json`          | JSON / NULL | `detailed_links.{neighborhood,fusion,cooccurrence,coexpression}` | JSON object or NULL | NO       | Carries the 4 sub-scores not in dedicated DB columns. `_provenance: "detailed_file"`. |
| `created_at`          | ISO 8601  | runtime                                        | UTC timestamp                            | NO       | Provenance — CSV only, NOT loaded to DB.                                           |
| `string_version`      | str       | `config.STRING_VERSION`                        | e.g. `"12.0"`                            | NO       | Provenance — CSV only.                                                             |
| `pipeline_run_id`     | str       | `self.run_id` (UUID)                           | UUID string                              | NO       | Provenance — CSV only. The DB `pipeline_run_id` column is the integer FK to `pipeline_runs.id`. |
| `source_url`          | str       | `config.STRING_PROTEIN_LINKS_URL`              | URL                                      | NO       | Provenance — CSV only.                                                             |
| `source_sha256`       | str / NULL| `_compute_sha256(links_path)`                  | hex string                               | NO       | Provenance — CSV only. NULL if download was mocked.                                |

## DB columns (loaded by `bulk_upsert_ppi`)

Only these columns are loaded into `protein_protein_interactions` table:

| Column                | Type      | Constraint                                    |
|-----------------------|-----------|-----------------------------------------------|
| `protein_a_id`        | int (FK)  | `proteins.id`, NOT NULL, `protein_a_id < protein_b_id` |
| `protein_b_id`        | int (FK)  | `proteins.id`, NOT NULL, `protein_a_id < protein_b_id` |
| `combined_score`      | int / NULL| `[0, 1000]`                                   |
| `experimental_score`  | int / NULL| `[0, 1000]`                                   |
| `database_score`      | int / NULL| `[0, 1000]`                                   |
| `textmining_score`    | int / NULL| `[0, 1000]`                                   |
| `score_json`          | Text / NULL| JSON string with 4 sub-scores                |
| `source`              | str       | NOT NULL, controlled vocab: `"string"`        |
| `pipeline_run_id`     | int / NULL| FK to `pipeline_runs.id`                      |

## UniProt accession pattern

```
^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$
```

Two alternatives:
- **6-char canonical**: `[OPQ][0-9][A-Z0-9]{3}[0-9]` — e.g. `P69905`, `Q8WXI7`
- **10-char canonical**: `[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}` — e.g. `A0A024RBG1`

Source: https://www.uniprot.org/help/accession

## Dead-letter files

Every dropped record is persisted to
`PROCESSED_DATA_DIR/dead_letter/string_run_{run_id}_{reason}.json`:

| Reason                       | When                                                              |
|------------------------------|-------------------------------------------------------------------|
| `nan_combined_score_rows`    | Row had NaN `combined_score` (filtered by score threshold)        |
| `wrong_taxon`                | Row had non-`9606.` taxon prefix (cross-species contamination)    |
| `invalid_uniprot`            | Aliases entry had non-canonical UniProt accession                 |
| `isoform_mappings`           | Aliases entry was an isoform (`<canonical>-<N>`)                  |
| `unmapped_string_id_protein1`| `protein1` STRING ID had no UniProt mapping in aliases            |
| `unmapped_string_id_protein2`| `protein2` STRING ID had no UniProt mapping in aliases            |
| `unmapped_uniprot_ids`       | UniProt ID had no `protein.id` in DB (UniProt pipeline not run)   |
| `homodimers_dropped`         | `protein_a_id == protein_b_id` (DB constraint `chk_ppi_ordered`)  |
| `missing_combined_score`     | Output row had NaN `combined_score` after cleaning                |
| `combined_score_out_of_range`| Output row had `combined_score` outside `[0, 1000]`               |
| `swap_inconsistency`         | After FK swap, `uniprot_id_a` no longer mapped to `protein_a_id`  |
| `schema_validation_failure`  | Output failed `validate_output()` against `schema/v1.json`        |
| `detailed_file_corrupted`    | Detailed file failed gzip magic-byte / SHA-256 sidecar check      |
| `pii_detected`               | PII heuristic flagged a column (should never fire for STRING)     |

## Sidecar files

For every successful `clean()` run, three files are written to
`PROCESSED_DATA_DIR/`:

1. `protein_protein_interactions.csv` — the cleaned data (written by
   `BasePipeline._persist_cleaned_data` with `encoding="utf-8"`,
   `QUOTE_NONNUMERIC`).
2. `protein_protein_interactions.csv.sha256` — SHA-256 sidecar (written
   by `_persist_cleaned_data`).
3. `protein_protein_interactions.csv.metadata.json` — provenance
   metadata (written by `StringPipeline._write_metadata_sidecar`):
   - `schema_version`: `"v2.0"`
   - `string_version`: e.g. `"12.0"`
   - `pipeline_run_id`: UUID string
   - `correlation_id`: UUID string
   - `source_url`: STRING links URL
   - `source_sha256`: SHA-256 of links file
   - `aliases_sha256`: SHA-256 of aliases file (or NULL)
   - `detailed_sha256`: SHA-256 of detailed file (or NULL)
   - `effective_score_threshold`: int (700 in production)
   - `dedup_strategy`: `"max_score"` | `"mean_score"` | `"first"`
   - `detailed_mode`: `"optional"` | `"required"` | `"skip"`
   - `created_at`: ISO 8601 timestamp
4. `protein_protein_interactions.csv.transform.json` — transformation
   log (written by `StringPipeline._write_transformation_log`):
   list of `{stage, before, after, dropped, reason, sample, timestamp}`.

## Scientific references

- Szklarczyk D. et al. **The STRING database in 2023: protein-protein
  association networks and functional enrichment analyses for any
  sequenced genome of interest.** *Nucleic Acids Res.* 2023.
  doi:10.1093/nar/gkac1000
- UniProt Consortium. **UniProt: the Universal Protein Knowledgebase
  in 2023.** *Nucleic Acids Res.* 2023. doi:10.1093/nar/gkac1052
- STRING DB documentation: https://string-db.org/cgi/help
- UniProt accession help: https://www.uniprot.org/help/accession
- UniProt isoforms help: https://www.uniprot.org/help/isoforms

## Regulatory compliance

- **FDA 21 CFR Part 11** — Audit trails: `source_version`,
  `pipeline_run_id`, and SHA-256 checksums recorded in the audit
  record. Every PPI row loaded to the DB carries `pipeline_run_id`.
- **GxP ALCOA+** — Attributable, Legible, Contemporaneous, Original,
  Accurate, + Complete, Consistent, Enduring, Available. The
  dead-letter queue and per-stage metrics support Attributable and
  Complete.
