# DrugOS Graph Module — Institutional-Grade Codebase (v2.0.0)

## Package: drugos-graph v2.0.0 | Pipeline: 2.0.0-week2 | Schema: 2.0.0

### What's in this release

This package contains the FULL institutional-grade DrugOS Autonomous Drug
Repurposing Platform codebase — 24 source files (23 previously-fixed
plus the newly-upgraded `__main__.py`) plus supporting documentation
and tests.

### Files upgraded in this release

**`drugos_graph/__main__.py`** — Institutional-grade CLI entry point.

Upgraded from 4 lines to 1,893 lines, addressing all 56 unique issues
across 16 mandated quality and safety domains:

| Domain | Issues | What's fixed |
|--------|--------|--------------|
| 3. Scientific Correctness | D3-SCI-01..04 | Global seed init, env validation, config drift detection, validation-skipped warning |
| 5. Data Quality | D5-DQ-01..03 | Directory pre-flight, input-file check, stale-data warning |
| 7. Idempotency | D7-IDP-01..02 | Re-run detection, incomplete-run detection |
| 1. Architecture | D1-ARCH-01..04 | Lazy import, package integrity, runtime context, `__all__` |
| 9. Security | D9-SEC-01..04 | Credential check, root guard, secret masking, path tampering |
| 2. Design | D2-DES-01..02 | `run()` function with int return, exit-code contract |
| 14. Compliance | D14-COMP-01..03 | Python ≥3.10 enforcement, license display, schema version check |
| 6. Reliability | D6-REL-01..04 | Top-level handler, signal handlers, atexit cleanup, concurrency lock |
| 10. Testing | D10-TST-01..02 | `run(argv=None)` programmatic API, `--self-test` smoke check |
| 4. Coding | D4-COD-01..03 | Docstring, type annotations, guard comment |
| 8. Performance | D8-PERF-01..02 | Lazy import, system-resource logging |
| 11. Logging | D11-LOG-01..03 | Fallback logging, preamble, structured exit log |
| 12. Configuration | D12-CONF-01..03 | `.env` loader, config dump, drift detection |
| 15. Interoperability | D15-INT-01..02 | Wrong-invocation error, programmatic API |
| 16. Data Lineage | D16-LIN-01..02 | Run-ID generation, preliminary manifest |
| 13. Documentation | D13-DOC-01..03 | Comprehensive docstring, confirmation prompt, startup banner |

### Test Suites (3 total — all pass with zero errors)

1. **`tests/test_main_py_56_fixes.py`** — 76 tests verifying every one
   of the 56 fixes in `__main__.py` (one test per fix, plus end-to-end
   exit-code contract tests).  Real behavior verification — no
   `hasattr(...)` existence checks.

2. **`tests/test_24_files_combined.py`** — 85 tests verifying all 24
   files work together: import chain integrity, config-constant
   consistency across modules, cross-module data-flow contracts
   (DRKG → entity resolution → KG build → PyG build → TransE →
   evaluation), lineage chain integrity, and real subprocess
   invocation of `python -m drugos_graph`.

3. **`tests/test_20_files_combined.py`** + **`tests/test_graph_stats.py`**
   — 112 pre-existing tests (2 minor assertion updates to match the
   chemberta_encoder's deterministic-sort behavior added in the
   institutional-grade chemberta_encoder fix).

**Total: 273 tests, all passing.**

### How to verify

```bash
# Install dependencies
pip install -r drugos_graph/requirements.txt

# Run all 3 test suites
python -m pytest tests/ -v

# Or run individually
python -m pytest tests/test_main_py_56_fixes.py -v
python -m pytest tests/test_24_files_combined.py -v
python -m pytest tests/test_20_files_combined.py tests/test_graph_stats.py -v
```

### Entry-point usage

```bash
# Self-test (installation verification — runs in <1s)
mkdir -p /tmp/drugos/{data/raw,data/processed,logs,models}
DRUGOS_PROJECT_ROOT=/tmp/drugos python -m drugos_graph --self-test

# Show all data-source licenses
DRUGOS_PROJECT_ROOT=/tmp/drugos python -m drugos_graph --show-licenses

# Help
DRUGOS_PROJECT_ROOT=/tmp/drugos python -m drugos_graph --help

# Full pipeline (requires Neo4j + data files)
DRUGOS_NEO4J_PASSWORD=secret python -m drugos_graph --yes
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic error (step failure, exception) |
| 2 | Validation failure (Step 12 skipped, exit criteria not met) |
| 3 | Configuration failure (Python version, schema mismatch, missing env) |
| 4 | Aborted (operator declined, SIGINT, concurrent lock held) |

### Team Cosmic / VentureLab

Manoj · Rohan · Aseem
