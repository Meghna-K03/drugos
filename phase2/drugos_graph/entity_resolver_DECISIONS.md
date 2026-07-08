# Entity Resolver — Decision Log

This file records every significant design decision made in
`drugos_graph/entity_resolver.py`. Each entry is dated and includes
the rationale, trade-offs, and reversibility assessment.

The log is append-only. New decisions go at the bottom.

---

## 2026-06-19 — InChIKey as canonical Compound ID

**Decision:** Use InChIKey (not DrugBank ID) as `canonical_id` for Compound.

**Rationale:** Project doc §3 mandates InChIKey. IUPAC international
standard (Heller et al., J. Cheminform., 2015). Database-independent:
ChEMBL, PubChem, DrugBank, and ChEBI all emit InChIKey for every
compound. DrugBank IDs (DB00945) are database-internal and do not
survive cross-source merging.

**Trade-offs:** Requires every source to emit InChIKey. DrugBank does
(when known); ChEMBL does; PubChem does. Compounds without InChIKey
fall back to `UNRESOLVED:DRUGBANK:{drugbank_id}` placeholder with
`needs_review=True` and `confidence=0.50`.

**Reversibility:** High — change `config.CANONICAL_IDS["Compound"]`
and re-run.

**Fixes:** D3-001, D5-001, D5-002, D14-001, D15-001.

---

## 2026-06-19 — Three-tier confidence scoring

**Decision:** Use `config.flag_entity_confidence()` with thresholds
0.95 / 0.85 / 0.50.

**Rationale:**
- 0.95 = human-curated gold standard (DrugBank curated InChIKey).
- 0.85 = standard NER confidence (per spaCy/SciSpacy defaults).
- 0.50 = below random-chance for binary classification.

**Trade-offs:** Below 0.50 = dropped (data loss). Acceptable for
patient-safety-grade pipeline.

**Reversibility:** High — override via env vars
`DRUGOS_ENTITY_CONFIDENCE_THRESHOLD`,
`DRUGOS_ENTITY_CONFIDENCE_STRICT`,
`DRUGOS_ENTITY_CONFIDENCE_REJECT`.

**Fixes:** D3-009, D3-010, D14-003.

---

## 2026-06-19 — Frozen EntityMapping

**Decision:** Make `EntityMapping` a `@dataclass(frozen=True)`.

**Rationale:** Prevents accidental mutation; enables safe caching
(D8-013); enables safe concurrent reads (D6-019); enables safe use
as dict keys (via `__hash__`).

**Trade-offs:** Modifications require `dataclasses.replace()` —
slightly more verbose. Test fixtures must construct `Provenance`
explicitly.

**Reversibility:** Low — once frozen, downstream code that mutates
mappings will break. But that's the point — mutation was a bug source.

**Fixes:** D2-002, D7-011, D7-004.

---

## 2026-06-19 — Mandatory Provenance

**Decision:** Refuse to construct `EntityMapping` without `Provenance`.

**Rationale:** D16-015 — regulatory non-compliance if mappings are
untraceable. Patient safety. Every mapping MUST be traceable to its
source, version, license, and input checksum.

**Trade-offs:** Test fixtures must construct `Provenance`. Slightly
more verbose. Acceptable cost.

**Reversibility:** Low — making it optional again would break the
audit trail contract.

**Fixes:** D16-001, D16-003, D16-004, D16-010 through D16-015.

---

## 2026-06-19 — One-to-many reverse index

**Decision:** Reverse index uses `Dict[Tuple[str, str], Dict[str, List[str]]]`
(not `Dict[str, str]`).

**Rationale:** Multiple canonical_ids can map to the same external_id
(e.g., one ATC code is shared by many drugs; one gene_symbol maps to
many proteins). The old single-value reverse map silently overwrote
conflicts.

**Trade-offs:** Lookups return a list — caller must pick highest-
confidence candidate (built into `lookup_canonical_id`).

**Reversibility:** Medium — schema change requires migration.

**Fixes:** D3-003, D5-005, D5-006, D5-007, D5-008, D3-015.

---

## 2026-06-19 — Sorted iteration everywhere

**Decision:** Replace all `set`/`dict` iterations in output paths
with `sorted(...)`.

**Rationale:** Python sets and (before 3.7) dicts are unordered.
Even on 3.7+, dict order depends on insertion order, which can vary
between runs if upstream sources reorder their data. Sorted iteration
guarantees deterministic output.

**Trade-offs:** Slight O(n log n) cost. Negligible for typical
biomedical dataset sizes (<10M items).

**Reversibility:** Low — once downstream consumers depend on sorted
output, unsorting would break them.

**Fixes:** D7-001, D7-002.

---

## 2026-06-19 — `_running_total_conf` / `_running_total_count` for averaging

**Decision:** In `merge_duplicate_edges("average")`, store running
totals across multiple early reductions.

**Rationale:** The original code computed `avg = sum(group) / len(group)`
per reduction pass. If a group exceeded the early-reduction threshold
multiple times, the running totals were lost — producing a wrong
final average. The fix preserves `_running_total_conf` and
`_running_total_count` so subsequent reductions use the correct sum.

**Trade-offs:** Two extra dict keys per reduced edge, popped in the
final pass. Negligible memory overhead.

**Reversibility:** Low — the math fix is fundamental.

**Fixes:** D7-003 (the CRITICAL averaging bug).

---

## 2026-06-19 — LRU cache on lookups

**Decision:** Add `_lookup_cache: OrderedDict` with FIFO eviction.

**Rationale:** The Graph Transformer's embedding step calls
`lookup_canonical_id` ~3x per drug-disease pair. With 32K pairs per
training batch, that's ~100K lookups per batch — many of which hit
the same ~10K hot drugs. An LRU cache reduces this to ~10K cold
lookups per batch.

**Trade-offs:** ~20MB cache (100K entries × ~200 bytes). Cache
invalidation on every mutation (cheap).

**Reversibility:** High — set `ENTITY_RESOLVER_LRU_CACHE_SIZE=0` to
disable.

**Fixes:** D8-013.

---

## 2026-06-19 — Dead-letter queue instead of silent drop

**Decision:** Every rejected record goes to `self.dead_letter` with
full reason and PII-safe preview.

**Rationale:** Silent drops are the #1 cause of "why did my pipeline
produce 0 records?" debugging sessions. The dead-letter queue makes
every rejection observable and replayable.

**Trade-offs:** Memory grows with rejections. For production, the
queue should be periodically drained to disk (TODO: not implemented
in v1.1.0 — caller's responsibility).

**Reversibility:** Low — callers depend on `resolver.dead_letter`.

**Fixes:** D6-011, D6-012.

---

## 2026-06-19 — Circuit breaker on lookups

**Decision:** After 100 consecutive lookup failures, open the circuit
for 60 seconds.

**Rationale:** If the upstream (config module, mappings dict) is
broken, hammering it with lookups just wastes CPU and obscures the
real error. The circuit breaker gives the system time to recover.

**Trade-offs:** During the 60-second window, lookups raise
`ResolverError`. Caller must catch and retry or fail-fast.

**Reversibility:** High — set
`ENTITY_RESOLVER_CIRCUIT_BREAKER_FAILURE_THRESHOLD=999999` to
effectively disable.

**Fixes:** D6-013.

---

## 2026-06-19 — PII masking in `__repr__`

**Decision:** `EntityMapping.__repr__` truncates `name` to 8 chars
+ "...".

**Rationale:** Protein/gene names can be PII in clinical contexts
(e.g., patient-derived cell-line names). The default `dataclass`
`__repr__` would dump the full name into logs.

**Trade-offs:** Debugging is slightly harder — use `mapping.name`
directly when you need the full name.

**Reversibility:** Low — security hardening.

**Fixes:** D9-001, D9-010, D11-014.

---

## 2026-06-19 — Refuse pickling

**Decision:** `EntityMapping.__reduce__` raises `ResolverError`.

**Rationale:** Pickling a mapping would serialize the `Provenance`
block (which contains the `_input_checksum` of the source record).
If that pickle file leaks, an attacker can verify which version of
DrugBank was used — a minor but real information leak.

**Trade-offs:** Cannot use `pickle.dumps(mapping)`. Use
`mapping.to_dict()` + `json.dumps()` instead.

**Reversibility:** Low — security hardening.

**Fixes:** D9-011.

---

## 2026-06-19 — `_sanitize_name` strips injection chars

**Decision:** Strip `\\`, `"`, `'`, `` ` ``, `;`, `$` from entity names.

**Rationale:** Names flow into Cypher MERGE statements (via
`to_cypher()`). Even with parameterized queries, injection chars in
names can break log lines and downstream consumers that don't
parameterize.

**Trade-offs:** A few legitimate drug names containing `'` (e.g.
"Children's Tylenol") will have the apostrophe stripped. Acceptable
— the canonical_id (InChIKey) is the source of truth, not the name.

**Reversibility:** Medium — could switch to escape-and-quote instead.

**Fixes:** D9-008, D9-004.

---

## 2026-06-19 — Schema drift guards on first record

**Decision:** `resolve_compounds_from_drugbank` and
`resolve_proteins_from_uniprot` check that the first record has the
required fields, and raise `ResolverDataQualityError` if not.

**Rationale:** If a DrugBank release changes the schema (e.g. renames
`drugbank_id` to `drug_bank_id`), the resolver would silently emit 0
records. The guard makes the failure loud.

**Trade-offs:** If the first record is malformed but the rest are
fine, the whole batch fails. Acceptable — schema drift is a
pipeline-halting event.

**Reversibility:** Low — safety-critical guard.

**Fixes:** D14-017.

---

## 2026-06-19 — Streaming dedup API

**Decision:** Add `merge_duplicate_edges_streaming()` alongside the
in-memory `merge_duplicate_edges()`.

**Rationale:** The in-memory version loads all edges into a
`defaultdict`. For 5.9M edges, that's ~5GB of RAM. The streaming
variant requires sorted input but uses O(1) memory per group.

**Trade-offs:** Caller must sort input by `(src_id, rel_type, dst_id)`.
Slight complexity increase.

**Reversibility:** High — both APIs coexist.

**Fixes:** D8-002, D8-009.

---

## 2026-06-19 — Specialized resolver helper classes (D1-003)

**Decision:** Add `_CompoundResolver`, `_DiseaseResolver`,
`_GeneResolver`, `_ProteinResolver`, `_EdgeDeduplicator` as
delegation classes, exposed via `resolver.compounds`,
`resolver.diseases`, etc.

**Rationale:** The `EntityResolver` class was becoming a god class
(~25 public methods). The specialized classes don't add behavior —
they just group methods by entity type, making the API discoverable.

**Trade-offs:** Slight indirection. Public API surface is unchanged.

**Reversibility:** High — the helper classes are pure delegation.

**Fixes:** D1-003, D1-013.

---

## 2026-06-19 — Mandatory call ordering

**Decision:** `resolve_compounds_from_drkg` warns if
`resolve_compounds_from_drugbank` hasn't run first.
`build_gene_protein_edges` warns if `resolve_genes_from_drkg` and
`resolve_proteins_from_uniprot` haven't run.

**Rationale:** The resolver has implicit dependencies: DRKG compound
resolution depends on DrugBank mappings; gene-protein edge building
depends on both gene and protein mappings. Calling them in the wrong
order silently produces empty results.

**Trade-offs:** Warning (not error) — the caller can ignore if they
know what they're doing. In a future major version, this could be
upgraded to an error.

**Reversibility:** Medium — could be made stricter.

**Fixes:** D7-015.

---

## 2026-06-19 — `from __future__ import annotations`

**Decision:** Add `from __future__ import annotations` as the first
non-comment line.

**Rationale:** Enables PEP 585 style `dict[str, str]` instead of
`Dict[str, str]` throughout, even on Python 3.9. Makes type hints
lazy strings (faster import).

**Trade-offs:** Annotations are strings at runtime — code that
inspects `__annotations__` and expects types will see strings. Use
`typing.get_type_hints()` to resolve.

**Reversibility:** Low — idiomatic modern Python.

**Fixes:** D4-008, D4-009, D14-010.

---

## 2026-06-19 — JSON / JSONL / CSV / Parquet / Cypher export

**Decision:** `save_mappings()` supports 4 formats; `to_cypher()`
adds a 5th.

**Rationale:** Different downstream consumers need different formats.
Neo4j wants Cypher; data scientists want Parquet; humans want CSV;
JSON is universal.

**Trade-offs:** 4 code paths to maintain. Each is small (~20 lines).

**Reversibility:** High — add new formats by extending the `if fmt
==` chain.

**Fixes:** D7-010, D15-011, D15-012, D15-013, D15-014.

---

## 2026-06-19 — Optional encryption at rest

**Decision:** `save_mappings(..., encrypt=True)` uses Fernet to
encrypt the output file.

**Rationale:** Mappings may contain proprietary drug-discovery data
in a commercial deployment. Encryption at rest is a baseline
security control.

**Trade-offs:** Requires the `cryptography` package (optional
dependency). Caller must manage the encryption key.

**Reversibility:** High — `encrypt=False` by default.

**Fixes:** D9-007.

---

## 2026-06-19 — GDPR right-to-be-forgotten

**Decision:** Add `delete_entity(entity_type, canonical_id)` that
removes the mapping and all its reverse-index / lineage / unresolved
entries.

**Rationale:** GDPR Article 17 right to erasure. If a patient's data
somehow enters the KG (e.g. via a patient-derived cell line), they
can request deletion. This method makes deletion complete and
auditable.

**Trade-offs:** O(n) scan of reverse index. For 10M mappings, ~1s.
Acceptable for a rare operation.

**Reversibility:** Low — regulatory requirement.

**Fixes:** D14-013.

---

## 2026-06-19 — Diff between two resolvers

**Decision:** Add `diff(other_resolver)` returning
`{"added": ..., "removed": ..., "modified": ...}`.

**Rationale:** When re-running the pipeline on updated source data,
the operator needs to know what changed. The diff makes this
observable.

**Trade-offs:** O(n) in both mappings dicts. Acceptable for
operational use (not called in hot path).

**Reversibility:** Low — operational feature.

**Fixes:** D16-008.

---

## 2026-06-19 — Audit trail via transformation_log + audit_logger

**Decision:** Two parallel audit mechanisms:
1. `self.transformation_log` — in-memory list of every
   transformation.
2. `audit_logger` (Python logger) — structured log events.

**Rationale:** The in-memory log supports `get_audit_trail()` queries.
The Python logger supports log aggregation (Splunk, ELK, Datadog).

**Trade-offs:** Two sources of truth. They're kept in sync by having
every public method write to both.

**Reversibility:** Low — both are depended on by D16-006 and D14-014.

**Fixes:** D16-002, D16-006, D14-014.

---

## 2026-06-19 — SCHEMA_VERSION = "1.1.0"

**Decision:** Bump schema version from 1.0.0 to 1.1.0.

**Rationale:** This release changes the EntityMapping schema (adds
`provenance`, `safety_flags`, `_checksum` fields; changes
`confidence` default; adds `_schema_version` and `_resolver_version`
to Provenance). Old saved mappings files cannot be loaded by the new
resolver without migration.

**Trade-offs:** Breaks backward compatibility with v1.0.0 saved
files. Acceptable — v1.0.0 was the audit-failing version.

**Reversibility:** Low — schema version is immutable per release.

**Fixes:** D14-011, D7-008.

---

## How to Add a New Decision

When making a significant design decision in `entity_resolver.py`,
append a new entry to this file with:

1. **Date** (YYYY-MM-DD).
2. **Decision** (one-sentence summary).
3. **Rationale** (why this choice, not the alternatives).
4. **Trade-offs** (what we gave up).
5. **Reversibility** (High / Medium / Low — how hard to undo).
6. **Fixes** (which audit finding IDs this addresses).

Do not edit or delete existing entries — they are the historical
record of why the code looks the way it does.
