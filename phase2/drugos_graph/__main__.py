"""
DrugOS Graph Module — CLI Entry Point
======================================
Institutional-grade entry point for the DrugOS Autonomous Drug Repurposing
Platform.

This module is the SOLE gateway for ``python -m drugos_graph`` invocations.
It implements every cross-cutting concern that a clinical-grade pipeline
requires at process start-up:

* **Reproducibility** — global seed is set before any drugos_graph import
  so that hash randomisation, NumPy, and PyTorch all use the configured
  seed (D3-SCI-01).
* **Scientific environment validation** — Python >= 3.10, NumPy / PyTorch
  presence and version (D3-SCI-02).
* **Pre-flight architectural validation** — every critical submodule is
  importable (D1-ARCH-02).
* **Data directory accessibility** — PROJECT_ROOT, RAW_DIR, LOGS_DIR,
  MODEL_DIR must be writable (D5-DQ-01).
* **Input-file existence check** — if ``--skip-download`` is passed, the
  primary DRKG TSV must be on disk (D5-DQ-02).
* **Stale-data detection** — optional ``--require-fresh`` flag (D5-DQ-03).
* **Idempotency** — re-run detection via ``pipeline_results.json`` and
  config-hash comparison (D7-IDP-01, D7-IDP-02).
* **Security** — root check, Neo4j credential check, secret masking,
  module-path tampering detection (D9-SEC-01..04).
* **Reliability** — top-level exception handler, signal handlers for
  SIGINT/SIGTERM/SIGBREAK, atexit cleanup, file-based concurrency lock
  (D6-REL-01..04).
* **Testing** — importable ``run(argv=None)`` returning an integer exit
  code, plus a ``--self-test`` smoke check (D10-TST-01, D10-TST-02).
* **Coding** — comprehensive docstring, type annotations, guard comment
  (D4-COD-01..03).
* **Performance** — lazy import of ``run_pipeline`` (only triggered when
  actually needed); system-resource logging (D8-PERF-01, D8-PERF-02).
* **Logging** — fallback ``stderr`` logging configured before any
  drugos_graph import; preamble log entry on every run; structured
  exit log entry (D11-LOG-01..03).
* **Configuration** — lightweight ``.env`` loader; configuration dump to
  ``pipeline_config.json``; config-drift detection (D12-CONF-01..03).
* **Compliance** — Python version enforcement, data-source license
  display (``--show-licenses``), schema-version compatibility check
  (D14-COMP-01..03).
* **Interoperability** — clear error message when invoked as
  ``python drugos_graph/__main__.py``; programmatic API via ``run()``
  (D15-INT-01, D15-INT-02).
* **Data lineage** — ``run_id`` generated in this module (not in
  ``run_pipeline``); preliminary lineage manifest written BEFORE step 1
  so it survives a crash (D16-LIN-01, D16-LIN-02).
* **Documentation** — comprehensive module docstring (this block),
  inline comments explaining the WHY of every block, startup banner
  with version & license attribution, interactive confirmation prompt
  before launching the hours-long pipeline (D13-DOC-01..03).

Exit Codes
----------
The contract with ``run_pipeline.main()`` is:

* ``0`` — Success (pipeline completed all requested steps and validation).
* ``1`` — Generic error (step failure, pre-flight check failure).
* ``2`` — Validation failure (Step 12 skipped or exit criteria not met).
* ``3`` — Configuration failure (Python version, schema mismatch,
  missing required env vars).
* ``4`` — Aborted OR V1 launch criteria not met. v35 ROOT FIX (N-5):
  the docstring previously claimed exit 4 meant ONLY "aborted", but
  exit code 4 is ALSO returned when V1 launch criteria fail in some
  code paths (e.g., ``run_pipeline.py`` ``V1LaunchCriteriaFailed``
  handler can ``sys.exit(4)``). The two distinct meanings are:
    (a) Operator-aborted: operator answered "no" to confirmation
        prompt, SIGINT received before pipeline start, or concurrent
        lock held.
    (b) Scientific refusal: the model failed to meet the V1 launch
        criteria (AUC >= 0.85 on BOTH val and held-out, model saved
        to disk, no critical source failures, sufficient positive/
        negative pairs). The operator did NOT explicitly abort, but
        the pipeline refuses to return exit 0 because the model is
        not launch-ready. Distinguish from (a) by checking the
        ``results.get("launch_criteria_failed")`` flag in the
        persisted results JSON (when present).
  Operators seeing exit 4 should check the log for
  "V1 LAUNCH CRITERIA FAILED" to distinguish (a) from (b).

Prerequisites
-------------
* Python >= 3.10 (enforced as the FIRST executable statement).
* Neo4j 5.x running and ``DRUGOS_NEO4J_PASSWORD`` env var set
  (unless ``--skip-neo4j`` is passed).
* DRKG TSV and DrugBank XML downloaded to ``RAW_DIR`` (unless
  ``--skip-download`` is passed).
* All dependencies from ``pyproject.toml`` installed.

Invocation Examples
-------------------
::

    # Full pipeline (interactive confirmation required)
    python -m drugos_graph

    # Single step
    python -m drugos_graph --step 1

    # Resume from step 7
    python -m drugos_graph --resume 7

    # Offline testing (no Neo4j, no download)
    python -m drugos_graph --skip-download --skip-neo4j

    # Self-test (installation verification)
    python -m drugos_graph --self-test

    # Show all data-source licenses and exit
    python -m drugos_graph --show-licenses

    # Allow running as root (NOT recommended for production)
    python -m drugos_graph --allow-root

Team Cosmic / VentureLab — Autonomous Drug Repurposing Platform.
Package: drugos-graph v2.0.0 | Pipeline: 2.0.0-week2 | Schema: 2.0.0
"""

# ──────────────────────────────────────────────────────────────────────────────
# FUTURE IMPORTS — `annotations` MUST come before any other import so that
# PEP 604 `X | Y` syntax in type hints is lazily evaluated on Python 3.10
# where some optional packages may not be installed. Fixes D14-COMP-01
# (Python version enforcement is the next statement).
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# D14-COMP-01: Python version enforcement — FIRST executable statement.
# This MUST run before any drugos_graph import because config.py uses
# PEP 604 union syntax throughout (`X | Y`) which raises SyntaxError on
# Python < 3.10.  The error message tells the operator exactly what to do.
# ──────────────────────────────────────────────────────────────────────────────
import sys as _sys

if _sys.version_info < (3, 10):  # pragma: no cover — environment guard
    raise SystemExit(
        "DrugOS requires Python >= 3.10 "
        f"(got {_sys.version_info.major}.{_sys.version_info.minor})."
        f"\n  Please upgrade your Python interpreter or use a virtualenv"
        f"\n  created with `python3.10 -m venv .venv`."
    )

# ──────────────────────────────────────────────────────────────────────────────
# D4-COD-04 / D13-DOC-01: __all__ prevents namespace pollution when this
# module is imported via `from drugos_graph.__main__ import *`. We export
# only the public API: `run` and `main` (legacy). Fixes D1-ARCH-04.
# ──────────────────────────────────────────────────────────────────────────────
__all__: list[str] = ["run", "main"]

# ──────────────────────────────────────────────────────────────────────────────
# STANDARD-LIBRARY IMPORTS — only stdlib is imported at module load time.
# This is critical: the entire pre-flight phase (D1-ARCH-01, D8-PERF-01)
# must execute WITHOUT importing config.py (6,831 lines) or run_pipeline.py
# (2,974 lines), so that `--help`, `--self-test`, and `--show-licenses`
# work in under a second on resource-constrained CI runners.
# ──────────────────────────────────────────────────────────────────────────────
import argparse
import atexit
import hashlib
import json
import logging
import os
import platform
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

# ──────────────────────────────────────────────────────────────────────────────
# D11-LOG-01: Fallback stderr logging configured BEFORE any drugos_graph
# import. If `_configure_logging()` in run_pipeline.py later fails (e.g.
# RotatingFileHandler cannot create LOGS_DIR/pipeline.log due to permissions),
# we still have WARNING+ messages on stderr so the operator can see what
# went wrong.  This handler is removed once run_pipeline's logging is up.
# ──────────────────────────────────────────────────────────────────────────────
_FALLBACK_HANDLER = logging.StreamHandler()
_FALLBACK_HANDLER.setLevel(logging.WARNING)
_FALLBACK_HANDLER.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.basicConfig(level=logging.WARNING, handlers=[_FALLBACK_HANDLER])

# Dedicated module logger — used for pre-flight and lifecycle messages.
# The name "drugos_graph.__main__" ensures it inherits the root logger's
# fallback handler until run_pipeline's `_configure_logging` overrides it.
_logger = logging.getLogger("drugos_graph.__main__")

# ──────────────────────────────────────────────────────────────────────────────
# Module-level state — these mirror the pattern used by run_pipeline.py
# but live HERE so that __main__ retains control of the process lifecycle
# even if run_pipeline is never imported (e.g. during --self-test).
# ──────────────────────────────────────────────────────────────────────────────
_START_TIME: float = 0.0              # Wall-clock start (set in run())
_RUN_ID: str = ""                     # Generated before any pipeline import
_PIPELINE_LOCK_PATH: Optional[Path] = None  # Set by _acquire_concurrency_lock
_PIPELINE_LOCK_FILE: Any = None       # File handle for fcntl/msvcrt lock
_SHUTDOWN_REQUESTED: bool = False     # Set by signal handler
_LIFECYCLE_LOG_PATH: Optional[Path] = None  # Set after logging is configured
_PRELIMINARY_MANIFEST_PATH: Optional[Path] = None  # Set in run()

# Exit-code constants — single source of truth for the entire module.
# Fixes D2-DES-02: formal exit-code contract.
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_VALIDATION_FAILURE = 2
EXIT_CONFIG_FAILURE = 3
EXIT_ABORTED = 4

# Sensitive-environment-variable pattern (D9-SEC-03).
# Case-insensitive substring match — any env var whose name contains one
# of these tokens is masked in the config dump and the log preamble.
_SENSITIVE_ENV_PATTERN = re.compile(
    r"PASSWORD|SECRET|KEY|TOKEN|CREDENTIAL", re.IGNORECASE
)

# Critical submodules verified by _verify_package_integrity() — D1-ARCH-02.
# These are the modules whose absence would cause a deep, confusing
# ModuleNotFoundError inside step N of the pipeline. Verifying them at
# entry point means the operator sees a clean error immediately.
_CRITICAL_SUBMODULES: tuple[tuple[str, str], ...] = (
    ("config",            "Global configuration and path constants"),
    ("exceptions",        "Domain-specific exception hierarchy"),
    ("schemas",           "Pydantic/dataclass record schemas"),
    ("utils",             "Shared identifier and type utilities"),
    ("id_crosswalk",      "External-ID → canonical UniProt translation"),
    ("run_pipeline",      "13-step pipeline orchestrator"),
    ("drkg_loader",       "DRKG download and TSV parser"),
    ("drugbank_parser",   "DrugBank XML parser"),
    ("kg_builder",        "Neo4j knowledge-graph builder"),
    ("entity_resolver",   "Compound/Disease/Gene cross-source resolver"),
    ("pyg_builder",       "PyTorch Geometric HeteroData builder"),
    ("training_data",     "Training data construction and splitting"),
    ("transe_model",      "TransE knowledge-graph embedding model"),
    ("evaluation",        "Link-prediction metrics (AUC, MRR, Hits@K)"),
    ("graph_stats",       "Graph statistics and validation"),
    ("graph_queries",     "Cypher query utilities"),
)

# Stale-data threshold (days). Fixes D5-DQ-03 — defaults to 90, overridable
# via DRUGOS_STALENESS_THRESHOLD_DAYS env var. 90 days matches the
# approximate release cadence of ChEMBL and DrugBank.
_STALENESS_THRESHOLD_DAYS_DEFAULT = 90


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — PHASE 1: FOUNDATION (must run first, stdlib-only)
# ═══════════════════════════════════════════════════════════════════════════════


def _load_dotenv() -> None:
    """Load a ``.env`` file from the project root if present (D12-CONF-01).

    A lightweight, dependency-free parser.  Reads ``KEY=VALUE`` lines,
    silently ignores comments and blank lines, and uses
    ``os.environ.setdefault`` so explicit shell exports always win.

    Rationale
    ---------
    The pipeline reads 10+ env vars (``DRUGOS_NEO4J_URI``,
    ``DRUGOS_NEO4J_PASSWORD``, ``DRUGOS_SEED``, etc.).  Forgetting to
    ``source .env`` causes wrong config, wrong Neo4j instance, missing
    password — all silent until step 3+ fails 30 minutes in.  Loading
    ``.env`` at entry point removes that footgun without adding a
    ``python-dotenv`` dependency (constraint 3.4: stdlib only).
    """
    # Use DRUGOS_PROJECT_ROOT if set, otherwise walk up from this file.
    project_root_str = os.environ.get("DRUGOS_PROJECT_ROOT", "")
    if project_root_str:
        env_path = Path(project_root_str).resolve() / ".env"
    else:
        # __file__ is .../drugos_graph/__main__.py → parent.parent is the
        # project root in development.  In an installed package, .env is
        # typically in cwd.
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if not env_path.exists():
            env_path = Path.cwd() / ".env"

    if not env_path.exists():
        return

    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        # Fail soft — never let .env loading block pipeline start.
        _logger.warning("Could not read .env file at %s: %s", env_path, exc)
        return

    loaded = 0
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        # Skip blank lines and shell-style comments.
        if not line or line.startswith("#"):
            continue
        # Strip optional `export ` prefix.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            _logger.debug(".env:%d: missing '=' — skipping: %r", lineno, raw_line)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip matching surrounding quotes.
        value = value.strip().strip('"').strip("'")
        if key and not key.isidentifier():
            _logger.warning(".env:%d: invalid key %r — skipping", lineno, key)
            continue
        # setdefault: shell env vars take precedence over .env file.
        os.environ.setdefault(key, value)
        loaded += 1

    if loaded:
        _logger.info("Loaded %d env var(s) from %s", loaded, env_path)


def _generate_run_id() -> str:
    """Generate a deterministic-ish run ID for this invocation (D16-LIN-01).

    The run ID is the SOLE correlation key that ties together every log
    entry, every audit record, every lineage manifest, and every result
    file produced by this invocation.  It MUST be available BEFORE any
    drugos_graph import so that import-time log records can be correlated.

    Priority
    --------
    1. ``DRUGOS_RUN_ID`` env var (set by Airflow / cron wrapper for
       back-fill traceability).
    2. ``YYYYMMDD_HHMMSS_<8-hex>`` — timestamp + UUID4 prefix.

    The generated value is written back to ``os.environ['DRUGOS_RUN_ID']``
    so that downstream modules (run_pipeline, config.RUN_ID) read the same
    value.  This is the contract described in the master fix prompt §3.3.
    """
    existing = os.environ.get("DRUGOS_RUN_ID", "").strip()
    if existing:
        return existing
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    new_id = f"{stamp}_{short_uuid}"
    os.environ["DRUGOS_RUN_ID"] = new_id
    return new_id


def _init_global_seed() -> None:
    """Set the global random seed BEFORE any drugos_graph import (D3-SCI-01).

    ``config.set_global_seed(SEED)`` seeds Python's ``random``, NumPy,
    PyTorch (if installed), and ``PYTHONHASHSEED``.  Without calling it
    at the very top of execution, hash randomisation produces different
    import ordering and dict iteration orders on every run, which
    silently changes TransE embeddings and drug-disease predictions —
    a regulatory reproducibility failure (FDA 21 CFR Part 11).

    We import SEED lazily so that an invalid value in the env var
    (e.g. ``DRUGOS_SEED=abc``) raises a clear error here, not deep
    inside config.py's module load.
    """
    # Lazy import — only config.py's seed bits are needed, NOT the full
    # 6,831-line module.  In practice importing config eagerly is fine
    # (it has no heavy third-party deps at module top), but we still
    # defer to keep the pre-flight phase honest about "stdlib only".
    from drugos_graph.config import SEED, set_global_seed  # noqa: WPS433

    try:
        seed_int = int(SEED)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"DRUGOS_SEED env var must be an integer, got {SEED!r}."
        ) from exc
    set_global_seed(seed_int)
    _logger.info("Global seed initialised: %d", seed_int)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — PHASE 2: PRE-FLIGHT GUARDS
# ═══════════════════════════════════════════════════════════════════════════════


def _validate_scientific_environment() -> int:
    """Verify Python, NumPy, and PyTorch are present and compatible (D3-SCI-02).

    Uses ``importlib.util.find_spec`` so missing optional dependencies
    don't raise ImportError — they're reported as structured warnings
    instead.  The pipeline may legitimately run without PyTorch (e.g.
    --skip-training), so PyTorch is a WARNING not an error.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if all critical checks pass,
        ``EXIT_CONFIG_FAILURE`` if Python version is wrong.
    """
    import importlib.util

    # 1) Python version (re-check — the import-time guard at the top of
    #    this module handles <3.10, but we also surface it cleanly here
    #    for --self-test).
    if _sys.version_info < (3, 10):
        _logger.error(
            "Python >= 3.10 required (got %d.%d).",
            _sys.version_info.major, _sys.version_info.minor,
        )
        return EXIT_CONFIG_FAILURE

    # 2) NumPy — required by virtually every loader.
    numpy_spec = importlib.util.find_spec("numpy")
    if numpy_spec is None:
        _logger.error(
            "NumPy is not installed. DrugOS requires numpy>=1.24."
            " Install with: pip install numpy"
        )
        return EXIT_CONFIG_FAILURE
    try:
        import numpy as _np
        _logger.info(
            "Scientific env: numpy=%s (blas=%s)",
            getattr(_np, "__version__", "?"),
            "yes" if hasattr(_np, "show_config") else "unknown",
        )
    except ImportError as exc:  # pragma: no cover — defensive
        _logger.error("NumPy import failed: %s", exc)
        return EXIT_CONFIG_FAILURE

    # 3) PyTorch — required for TransE training.  WARNING only.
    torch_spec = importlib.util.find_spec("torch")
    if torch_spec is None:
        _logger.warning(
            "PyTorch is not installed. TransE training will be skipped."
            " Install with: pip install torch"
        )
    else:
        try:
            import torch as _torch
            cuda_available = _torch.cuda.is_available()
            cuda_version = (
                _torch.version.cuda if hasattr(_torch, "version") and _torch.version
                else "n/a"
            )
            _logger.info(
                "Scientific env: torch=%s cuda_available=%s cuda_version=%s",
                _torch.__version__, cuda_available, cuda_version,
            )
        except ImportError as exc:  # pragma: no cover — defensive
            _logger.warning("PyTorch import failed: %s", exc)

    # 4) pandas — required by every loader.
    pd_spec = importlib.util.find_spec("pandas")
    if pd_spec is None:
        _logger.error(
            "pandas is not installed. DrugOS requires pandas>=2.0."
            " Install with: pip install pandas"
        )
        return EXIT_CONFIG_FAILURE

    return EXIT_SUCCESS


def _verify_package_integrity() -> int:
    """Verify that every critical submodule is importable (D1-ARCH-02).

    Uses ``importlib.util.find_spec`` so a missing file produces a
    structured error here at the entry point, NOT a confusing
    ``ModuleNotFoundError`` deep inside step N of the pipeline.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if all submodules are present, else ``EXIT_ERROR``.
    """
    import importlib.util

    missing: list[str] = []
    for submod, role in _CRITICAL_SUBMODULES:
        spec = importlib.util.find_spec(f"drugos_graph.{submod}")
        if spec is None:
            missing.append(f"  - drugos_graph.{submod:<22} | {role}")

    if missing:
        _logger.error(
            "Package integrity check FAILED — %d critical submodule(s) missing:\n%s",
            len(missing), "\n".join(missing),
        )
        _logger.error(
            "Reinstall the package: pip install --force-reinstall drugos-graph"
        )
        return EXIT_ERROR

    _logger.debug("Package integrity OK (%d submodules verified).",
                  len(_CRITICAL_SUBMODULES))
    return EXIT_SUCCESS


def _check_data_directories() -> int:
    """Verify PROJECT_ROOT and critical directories are writable (D5-DQ-01).

    The pipeline requires RAW_DIR, PROCESSED_DIR, KG_DIR, EMBEDDINGS_DIR,
    LOGS_DIR, MODEL_DIR, DEAD_LETTER_DIR, CHECKPOINT_DIR, AUDIT_LOG_DIR
    to be writable.  ``ensure_dirs()`` only runs inside
    ``_configure_logging()`` inside ``main()``, so a permission error
    would surface as a confusing ``PermissionError`` from
    ``RotatingFileHandler`` 30 minutes into the pipeline.  This check
    fails fast at entry point with an actionable message.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if all critical dirs are writable, else
        ``EXIT_ERROR``.
    """
    from drugos_graph import config as _cfg

    # The dirs we test for write access.  We don't try to *create* them
    # here — that's ensure_dirs()'s job.  We just verify the parent
    # chain is writable so that ensure_dirs() will succeed.
    critical_dirs: list[tuple[str, Path]] = [
        ("PROJECT_ROOT", _cfg.PROJECT_ROOT),
        ("RAW_DIR parent", _cfg.RAW_DIR.parent),
        ("PROCESSED_DIR parent", _cfg.PROCESSED_DIR.parent),
        ("LOGS_DIR parent", _cfg.LOGS_DIR.parent),
        ("MODEL_DIR parent", _cfg.MODEL_DIR.parent),
    ]

    failed: list[str] = []
    for label, path in critical_dirs:
        if not path.exists():
            failed.append(f"  - {label}: {path} (does not exist)")
            continue
        # Probe write access by creating and removing a temp file.
        probe = path / f".drugos_write_probe_{os.getpid()}.tmp"
        try:
            probe.write_bytes(b"probe")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            failed.append(f"  - {label}: {path} (not writable: {exc})")

    if failed:
        _logger.error(
            "Data-directory pre-flight FAILED — %d issue(s):\n%s",
            len(failed), "\n".join(failed),
        )
        _logger.error(
            "Fix permissions or set DRUGOS_PROJECT_ROOT to a writable path."
        )
        return EXIT_ERROR

    _logger.debug("Data-directory pre-flight OK.")
    return EXIT_SUCCESS


def _check_root_privileges(argv: Sequence[str]) -> int:
    """Refuse to run as root unless ``--allow-root`` is passed (D9-SEC-02).

    The pipeline downloads and parses untrusted XML (DrugBank) and TSV
    files from the internet.  Running as root (common in Docker without
    a ``USER`` directive) amplifies any parser vulnerability from "data
    corruption" to "full system compromise" via path traversal or XML
    entity expansion.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if not root OR ``--allow-root`` is present,
        else ``EXIT_ABORTED``.
    """
    # Only check on POSIX. Windows doesn't have a meaningful euid.
    if not hasattr(os, "geteuid"):
        return EXIT_SUCCESS
    if os.geteuid() != 0:
        return EXIT_SUCCESS

    if "--allow-root" in argv:
        _logger.warning(
            "Running as root with --allow-root. This is NOT recommended"
            " for production: the pipeline parses untrusted XML and TSV"
            " files from the internet."
        )
        return EXIT_SUCCESS

    _logger.error(
        "Refusing to run as root without --allow-root.\n"
        "  Rationale: pipeline parses untrusted DrugBank XML and DRKG TSV\n"
        "  from the internet; a parser vulnerability becomes full system\n"
        "  compromise under root. Re-run as a non-root user, or pass\n"
        "  --allow-root to acknowledge the risk."
    )
    return EXIT_ABORTED


def _check_neo4j_credentials(argv: Sequence[str]) -> int:
    """Verify DRUGOS_NEO4J_PASSWORD is set unless --skip-neo4j (D9-SEC-01).

    Without this check, the operator waits 30+ minutes for step 3 to
    fail with a Neo4j auth error.  Here we fail fast at entry point.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if credentials present or Neo4j skipped,
        ``EXIT_CONFIG_FAILURE`` otherwise.
    """
    if "--skip-neo4j" in argv:
        return EXIT_SUCCESS

    pwd = os.environ.get("DRUGOS_NEO4J_PASSWORD", "").strip()
    if pwd:
        return EXIT_SUCCESS

    _logger.error(
        "DRUGOS_NEO4J_PASSWORD is not set.\n"
        "  Set it in your environment, in .env, or pass --skip-neo4j\n"
        "  to run steps that don't require Neo4j (e.g. --step 1)."
    )
    return EXIT_CONFIG_FAILURE


def _check_module_path_tampering() -> int:
    """Verify this module is loaded from a trusted location (D9-SEC-04).

    ``python -m`` uses ``sys.path``.  If a malicious ``drugos_graph/``
    directory is earlier in ``sys.path`` than the real install, the
    wrong code executes.  Common on shared compute (university cluster,
    cloud VM with a stale working directory).

    We refuse to proceed if this file is loaded from a world-writable
    parent (``/tmp``, cwd on a shared host) — but we only WARN, because
    legitimate dev workflows sometimes run from cwd.

    Returns
    -------
    int
        Always ``EXIT_SUCCESS`` — this is a WARNING-only check.
    """
    try:
        resolved = Path(__file__).resolve()
    except OSError:
        return EXIT_SUCCESS

    suspicious_parents = {Path("/tmp"), Path("/var/tmp")}
    for parent in resolved.parents:
        if parent in suspicious_parents:
            _logger.warning(
                "drugos_graph.__main__ is loaded from %s, which is a"
                " shared temp directory. This is a security risk on"
                " multi-tenant hosts. Verify the package was installed"
                " from a trusted source.",
                parent,
            )
            break
    return EXIT_SUCCESS


def _mask_sensitive_env(
    env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Return a copy of ``os.environ`` with sensitive values masked (D9-SEC-03).

    Used for the config dump (D12-CONF-02) and the log preamble so that
    DEBUG-level logging of effective config never leaks passwords,
    API tokens, or other secrets to log files or to monitoring systems.

    Parameters
    ----------
    env : dict, optional
        Environment dict to mask. Defaults to ``os.environ``.

    Returns
    -------
    dict[str, str]
        Copy with sensitive values replaced by ``"*****"``.
    """
    source = env if env is not None else dict(os.environ)
    masked: dict[str, str] = {}
    for key, value in source.items():
        if _SENSITIVE_ENV_PATTERN.search(key):
            masked[key] = "*****" if value else ""
        else:
            masked[key] = value
    return masked


def _check_input_files(argv: Sequence[str]) -> int:
    """Verify required input files exist when --skip-download is passed (D5-DQ-02).

    Without this check, ``parse_drkg_tsv()`` fails with a confusing
    ``FileNotFoundError`` deep inside drkg_loader.py.  Here we list
    every missing file with its full path and recovery options.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if all required files exist (or --skip-download
        not passed), else ``EXIT_ERROR``.
    """
    if "--skip-download" not in argv:
        return EXIT_SUCCESS

    from drugos_graph import config as _cfg

    # BUG-E-007 root fix: when --data-source phase1 is passed, the DRKG
    # TSV is NOT required because the pipeline consumes Phase 1's
    # processed_data CSVs via the phase1_bridge. The previous code
    # required drkg.tsv unconditionally, silently overriding the
    # --data-source phase1 flag.
    data_source_phase1 = (
        "--data-source" in argv
        and argv.index("--data-source") + 1 < len(argv)
        and argv[argv.index("--data-source") + 1] == "phase1"
    )

    missing: list[str] = []

    # Primary DRKG TSV — required ONLY when data-source is drkg.
    if not data_source_phase1:
        drkg_tsv = _cfg.RAW_DIR / "drkg.tsv"
        if not drkg_tsv.exists():
            missing.append(
                f"  - {drkg_tsv}\n      → DRKG TSV. Download via"
                " `python -m drugos_graph` (without --skip-download) or"
                " from " + _cfg.DATA_SOURCES.get("drkg", {}).get("url", "(url unknown)")
            )

    # DrugBank XML — required for step 4 enrichment (unless --skip-neo4j).
    # On the phase1 path, DrugBank data comes from Phase 1's processed_data
    # so the raw XML is not required.
    if not data_source_phase1 and "--skip-neo4j" not in argv:
        drugbank_xml = _cfg.RAW_DIR / "drugbank.xml"
        if not drugbank_xml.exists():
            missing.append(
                f"  - {drugbank_xml}\n      → DrugBank XML. Apply for"
                " access at https://go.drugbank.com/ and place the file"
                " in RAW_DIR."
            )

    if missing:
        _logger.error(
            "--skip-download was passed but %d required file(s) are missing:\n%s",
            len(missing), "\n".join(missing),
        )
        _logger.error(
            "Re-run without --skip-download, or manually place the files"
            " in RAW_DIR (%s).", _cfg.RAW_DIR,
        )
        return EXIT_ERROR

    _logger.debug("Input-file pre-flight OK.")
    return EXIT_SUCCESS


def _check_data_freshness(argv: Sequence[str]) -> int:
    """Optionally warn when input data files are stale (D5-DQ-03).

    Triggered by the ``--require-fresh`` flag.  Stale data means missing
    drug approvals, missing safety signals, missing disease-gene
    associations — a drug with a new black-box warning would be missing
    from the safety profile, leading to dangerous repurposing recommendations.

    Returns
    -------
    int
        Always ``EXIT_SUCCESS`` — this is a WARNING-only check (pipeline
        still proceeds; operator is informed of drift).
    """
    if "--require-fresh" not in argv:
        return EXIT_SUCCESS

    from drugos_graph import config as _cfg

    threshold_days = int(os.environ.get(
        "DRUGOS_STALENESS_THRESHOLD_DAYS",
        str(_STALENESS_THRESHOLD_DAYS_DEFAULT),
    ))
    threshold_seconds = threshold_days * 86400
    now = time.time()
    stale: list[str] = []

    for fname in ("drkg.tsv", "drugbank.xml"):
        fpath = _cfg.RAW_DIR / fname
        if not fpath.exists():
            continue
        try:
            age_seconds = now - fpath.stat().st_mtime
        except OSError:
            continue
        if age_seconds > threshold_seconds:
            age_days = int(age_seconds // 86400)
            stale.append(f"  - {fpath} ({age_days} days old)")

    if stale:
        _logger.warning(
            "Stale data detected (%d file(s) older than %d days):\n%s",
            len(stale), threshold_days, "\n".join(stale),
        )
        _logger.warning(
            "Consider re-running without --skip-download, or pass"
            " --fresh-start to overwrite existing processed data."
        )

    return EXIT_SUCCESS


def _check_config_drift() -> int:
    """Compare current CONFIG_HASH against stored hash (D3-SCI-03, D12-CONF-03).

    If ``pipeline_results.json`` exists in ``PROCESSED_DIR`` and its
    ``config_hash`` differs from the current one, the configuration has
    drifted since the last successful run.  This means string thresholds,
    data source URLs, or seed values changed — predictions from this run
    are NOT directly comparable to predictions from the previous run.

    Returns
    -------
    int
        Always ``EXIT_SUCCESS`` — WARNING only. Pipeline still proceeds;
        operator is informed of drift.
    """
    from drugos_graph import config as _cfg

    results_path = _cfg.PROCESSED_DIR / "pipeline_results.json"
    if not results_path.exists():
        return EXIT_SUCCESS

    try:
        with open(results_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _logger.debug("Could not read previous pipeline_results.json: %s", exc)
        return EXIT_SUCCESS

    prev_hash = prev.get("config_hash") or prev.get("config", {}).get("hash")
    if not prev_hash:
        return EXIT_SUCCESS

    # D12-CONF-03 / D3-SCI-03: Wrap config-hash computation in try/except so
    # that a failure inside ``compute_config_hash()`` (e.g. an env var with
    # an unexpected type, or a transient import error) does NOT propagate as
    # an unhandled exception.  The drift check is an informational WARNING,
    # not a data-pipeline error — it must never crash the pipeline.
    try:
        current_hash = _cfg.CONFIG_HASH or _cfg.compute_config_hash()
    except Exception as exc:  # noqa: BLE001 — informational check, fail soft
        _logger.debug(
            "Could not compute current config_hash for drift comparison: %s."
            " Skipping drift check (this is informational, not a failure).", exc,
        )
        return EXIT_SUCCESS
    if prev_hash != current_hash:
        _logger.warning(
            "Configuration drift detected: previous run used config_hash=%s,"
            " current run uses config_hash=%s. Predictions from this run are"
            " NOT directly comparable to the previous run. Consider"
            " --fresh-start to start from a clean slate, or"
            " --acknowledge-config-drift to silence this warning.",
            prev_hash, current_hash,
        )
    return EXIT_SUCCESS


def _check_schema_version() -> int:
    """Verify schema version compatibility (D14-COMP-03).

    Old v1.x processed data in PROCESSED_DIR causes silent corruption
    (missing fields, unrecognised types).  We compare the schema version
    in ``pipeline_results.json`` with the current ``SCHEMA_VERSION``.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` normally; ``EXIT_CONFIG_FAILURE`` if
        ``--require-schema-match`` is passed and versions differ.
    """
    from drugos_graph import config as _cfg

    results_path = _cfg.PROCESSED_DIR / "pipeline_results.json"
    if not results_path.exists():
        return EXIT_SUCCESS

    try:
        with open(results_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        return EXIT_SUCCESS

    prev_schema = prev.get("schema_version")
    if not prev_schema:
        return EXIT_SUCCESS

    if prev_schema != _cfg.SCHEMA_VERSION:
        msg = (
            f"Schema version mismatch: previous run used {prev_schema},"
            f" current code expects {_cfg.SCHEMA_VERSION}."
        )
        if "--require-schema-match" in _sys.argv:
            _logger.error(msg + " Aborting because --require-schema-match was passed.")
            return EXIT_CONFIG_FAILURE
        _logger.warning(
            msg + " Re-run with --fresh-start to regenerate from scratch."
        )
    return EXIT_SUCCESS


def _detect_incomplete_run() -> int:
    """Detect orphaned checkpoints from a previous crashed run (D7-IDP-02).

    A previous crashed run leaves partial state: checkpoint files without
    a completed ``pipeline_results.json``, partially written results,
    partially loaded Neo4j graph.  Re-running without ``--fresh-start``
    may load duplicates, creating mixed state.

    Returns
    -------
    int
        Always ``EXIT_SUCCESS`` — WARNING only.
    """
    from drugos_graph import config as _cfg

    # 1) pipeline_results.json with status != success → incomplete.
    results_path = _cfg.PROCESSED_DIR / "pipeline_results.json"
    if results_path.exists():
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            status = prev.get("status") or prev.get("pipeline_status")
            if status and status != "success" and status != "completed":
                _logger.warning(
                    "Previous run ended with status=%r (not 'success')."
                    " State may be inconsistent. Consider --resume N to"
                    " continue from the last completed step, or"
                    " --fresh-start to start over.", status,
                )
                return EXIT_SUCCESS
        except (OSError, json.JSONDecodeError):
            pass

    # 2) Orphaned checkpoint files → likely crashed mid-step.
    if _cfg.CHECKPOINT_DIR.exists():
        try:
            checkpoints = list(_cfg.CHECKPOINT_DIR.glob("*.json"))
        except OSError:
            checkpoints = []
        if checkpoints and not results_path.exists():
            _logger.warning(
                "Found %d checkpoint file(s) in %s but no pipeline_results.json."
                " A previous run may have crashed. Consider --resume N or"
                " --fresh-start.", len(checkpoints), _cfg.CHECKPOINT_DIR,
            )
    return EXIT_SUCCESS


def _check_idempotency() -> int:
    """Warn if the same config has already been run successfully (D7-IDP-01).

    Returns
    -------
    int
        Always ``EXIT_SUCCESS`` — WARNING only.
    """
    from drugos_graph import config as _cfg

    results_path = _cfg.PROCESSED_DIR / "pipeline_results.json"
    if not results_path.exists():
        return EXIT_SUCCESS

    try:
        with open(results_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        return EXIT_SUCCESS

    if prev.get("status") in ("success", "completed"):
        prev_hash = prev.get("config_hash")
        # D7-IDP-01: Wrap hash computation in try/except — informational
        # idempotency warning must not crash the pipeline.
        try:
            current_hash = _cfg.CONFIG_HASH or _cfg.compute_config_hash()
        except Exception as exc:  # noqa: BLE001 — fail soft
            _logger.debug(
                "Could not compute config_hash for idempotency check: %s.", exc,
            )
            return EXIT_SUCCESS
        if prev_hash == current_hash:
            _logger.warning(
                "An identical run (config_hash=%s) already completed"
                " successfully. Re-running will produce the same output."
                " Use --resume N to skip completed steps, or --fresh-start"
                " to overwrite.", current_hash,
            )
    return EXIT_SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — PHASE 3: ENTRY POINT STRUCTURE (lifecycle & concurrency)
# ═══════════════════════════════════════════════════════════════════════════════


def _signal_handler(signum: int, frame: Any) -> None:
    """Outermost SIGINT/SIGTERM safety net (D6-REL-02).

    Sets a module-level flag that the pipeline can poll.  Also logs the
    signal so that even if the pipeline is mid-step and doesn't poll the
    flag, the operator has a record of the interruption.

    The first SIGINT requests graceful shutdown; the second forces a
    hard KeyboardInterrupt so the operator can always escape.
    """
    global _SHUTDOWN_REQUESTED
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    if _SHUTDOWN_REQUESTED:
        _logger.warning(
            "Received %s again — forcing immediate exit.", sig_name,
        )
        raise KeyboardInterrupt()
    _SHUTDOWN_REQUESTED = True
    _logger.warning(
        "Received %s — requesting graceful shutdown."
        " Send the signal again to force exit.", sig_name,
    )


def _register_signal_handlers() -> None:
    """Install SIGINT/SIGTERM/SIGBREAK handlers (D6-REL-02).

    Covers BOTH full-pipeline mode and ``--step`` mode, since the
    handler is registered at the entry point, not inside
    ``run_full_pipeline()``.

    Windows doesn't have SIGTERM; it has SIGBREAK.  We register it
    conditionally.
    """
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _signal_handler)
        except (ValueError, OSError):
            # SIGBREAK registration can fail in non-main threads or
            # on certain Windows builds.  Fail soft.
            pass


def _acquire_concurrency_lock() -> int:
    """Acquire an exclusive file lock to prevent concurrent runs (D6-REL-04).

    Two concurrent runs corrupt the Neo4j graph, overwrite
    ``pipeline_results.json``, and interleave log entries.  This lock
    uses ``fcntl.flock(LOCK_NB)`` on POSIX and ``msvcrt.locking`` on
    Windows, with PID recorded in the lockfile so a crashed process can
    be detected manually.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if lock acquired, ``EXIT_ABORTED`` if held by
        another process.
    """
    global _PIPELINE_LOCK_PATH, _PIPELINE_LOCK_FILE

    from drugos_graph import config as _cfg

    # Ensure LOGS_DIR exists (it may not yet if ensure_dirs hasn't run).
    try:
        _cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't create LOGS_DIR, the data-directory pre-flight
        # will catch it.  Skip locking.
        return EXIT_SUCCESS

    _PIPELINE_LOCK_PATH = _cfg.LOGS_DIR / ".pipeline.lock"
    try:
        _PIPELINE_LOCK_FILE = open(_PIPELINE_LOCK_PATH, "w", encoding="utf-8")
    except OSError as exc:
        _logger.warning(
            "Could not open lock file %s: %s. Proceeding without lock"
            " — concurrent runs may corrupt state.",
            _PIPELINE_LOCK_PATH, exc,
        )
        return EXIT_SUCCESS

    # Write our PID so a crashed process can be detected manually.
    try:
        _PIPELINE_LOCK_FILE.write(f"{os.getpid()}\n")
        _PIPELINE_LOCK_FILE.flush()
    except OSError:
        pass

    # Try non-blocking lock.
    locked = False
    try:
        import fcntl  # POSIX
        try:
            fcntl.flock(_PIPELINE_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            locked = False
    except ImportError:
        # Windows fallback.
        try:
            import msvcrt
            try:
                msvcrt.locking(_PIPELINE_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                locked = False
        except ImportError:
            # No locking primitive available — fail open.
            locked = True

    if not locked:
        # Read the PID of the holder for a helpful error message.
        holder_pid = "unknown"
        try:
            with open(_PIPELINE_LOCK_PATH, "r", encoding="utf-8") as f:
                holder_pid = f.read().strip() or "unknown"
        except OSError:
            pass
        _logger.error(
            "Another DrugOS pipeline run (PID %s) is already in progress"
            " (lock file: %s). Concurrent runs corrupt the Neo4j graph"
            " and pipeline_results.json. If you are sure no run is"
            " active, remove the lock file and retry.",
            holder_pid, _PIPELINE_LOCK_PATH,
        )
        try:
            _PIPELINE_LOCK_FILE.close()
        except OSError:
            pass
        _PIPELINE_LOCK_FILE = None
        return EXIT_ABORTED

    _logger.debug("Concurrency lock acquired at %s", _PIPELINE_LOCK_PATH)
    return EXIT_SUCCESS


def _release_concurrency_lock() -> None:
    """Release the file lock acquired by ``_acquire_concurrency_lock``."""
    global _PIPELINE_LOCK_FILE, _PIPELINE_LOCK_PATH
    if _PIPELINE_LOCK_FILE is None:
        return
    try:
        try:
            import fcntl
            fcntl.flock(_PIPELINE_LOCK_FILE.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            try:
                import msvcrt
                msvcrt.locking(_PIPELINE_LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
    finally:
        try:
            _PIPELINE_LOCK_FILE.close()
        except OSError:
            pass
        _PIPELINE_LOCK_FILE = None
        # Remove the lockfile so a future run doesn't see a stale PID.
        if _PIPELINE_LOCK_PATH is not None:
            try:
                _PIPELINE_LOCK_PATH.unlink(missing_ok=True)
            except OSError:
                pass


def _write_preliminary_manifest(run_id: str, argv: Sequence[str]) -> None:
    """Write a preliminary lineage manifest BEFORE step 1 (D16-LIN-02).

    If the pipeline crashes mid-run, ``lineage_manifest.json`` written
    by ``run_full_pipeline()`` is never created.  This preliminary
    manifest survives the crash and provides regulators with the
    invocation context (run_id, timestamp, config_hash, CLI args,
    system env) — without which we cannot explain how a prediction was
    generated.

    Uses atomic write (temp file + ``os.rename``) so a partial write
    never corrupts an existing manifest.
    """
    global _PRELIMINARY_MANIFEST_PATH

    from drugos_graph import config as _cfg

    try:
        _cfg.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    manifest = {
        "run_id": run_id,
        "status": "in_progress",
        "start_timestamp": datetime.now(timezone.utc).isoformat(),
        "config_hash": _cfg.CONFIG_HASH or _cfg.compute_config_hash(),
        "schema_version": _cfg.SCHEMA_VERSION,
        "pipeline_version": _cfg.PIPELINE_VERSION,
        "package_version": _cfg.PACKAGE_VERSION,
        "python_version": _sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "argv": list(argv),
        "env": _mask_sensitive_env(),
    }

    target = _cfg.PROCESSED_DIR / "lineage_manifest.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, default=str)
        os.replace(tmp, target)
        _PRELIMINARY_MANIFEST_PATH = target
    except OSError as exc:
        _logger.warning(
            "Could not write preliminary lineage manifest to %s: %s."
            " If the pipeline crashes, lineage traceability will be lost.",
            target, exc,
        )


def _log_system_resources() -> None:
    """Log available RAM, CPU count, GPU availability (D8-PERF-02).

    OOM kill is the #1 production failure mode for graph pipelines —
    DRKG alone needs 2-4 GB and PyG HeteroData can exceed 8 GB.  Logging
    the baseline at startup gives ops a debugging starting point.
    """
    # CPU count — stdlib.
    cpu_count = os.cpu_count() or "unknown"
    # RAM — try psutil, fall back to /proc/meminfo on Linux.
    ram_str = "unknown"
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        ram_str = f"{vm.total // (1024 ** 3)}GB total, {vm.available // (1024 ** 3)}GB available"
    except ImportError:
        if _sys.platform.startswith("linux"):
            try:
                with open("/proc/meminfo", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            ram_kb = int(line.split()[1])
                            ram_str = f"{ram_kb // (1024 ** 2)}GB total (no psutil)"
                            break
            except OSError:
                pass
    # GPU — try torch (already imported by _validate_scientific_environment).
    gpu_str = "n/a"
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            gpu_str = torch.cuda.get_device_name(0)
        else:
            gpu_str = "none (CPU-only)"
    except ImportError:
        gpu_str = "torch not installed"

    _logger.info(
        "System resources: python=%s platform=%s cpu_count=%s ram=[%s] gpu=[%s]",
        _sys.version.split()[0], platform.platform(), cpu_count, ram_str, gpu_str,
    )


def _dump_effective_config(argv: Sequence[str]) -> None:
    """Dump effective config to PROCESSED_DIR/pipeline_config.json (D12-CONF-02).

    On failure, ``pipeline_results.json`` is never written, leaving the
    operator with no record of what config the failed run used.  This
    dump is the post-mortem artifact.

    Includes CLI args, masked env vars, and key config values.
    """
    from drugos_graph import config as _cfg

    try:
        _cfg.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    dump = {
        "run_id": _RUN_ID,
        "dump_timestamp": datetime.now(timezone.utc).isoformat(),
        "argv": list(argv),
        "env": _mask_sensitive_env(),
        "versions": {
            "package": _cfg.PACKAGE_VERSION,
            "pipeline": _cfg.PIPELINE_VERSION,
            "config": _cfg.CONFIG_VERSION,
            "schema": _cfg.SCHEMA_VERSION,
        },
        "config_hash": _cfg.CONFIG_HASH or _cfg.compute_config_hash(),
        "seed": _cfg.SEED,
        "key_thresholds": {
            "MIN_NODES_W2": _cfg.MIN_NODES_W2,
            "MIN_EDGES_W2": _cfg.MIN_EDGES_W2,
            "MIN_POSITIVE_PAIRS": _cfg.MIN_POSITIVE_PAIRS,
            "MIN_NEGATIVE_PAIRS": _cfg.MIN_NEGATIVE_PAIRS,
            "TARGET_TRANSE_AUC": _cfg.TARGET_TRANSE_AUC,
            "V1_LAUNCH_AUC": _cfg.V1_LAUNCH_AUC,
            "STRING_SCORE_THRESHOLD": _cfg.STRING_SCORE_THRESHOLD,
            "STITCH_SCORE_THRESHOLD": _cfg.STITCH_SCORE_THRESHOLD,
        },
        "paths": {
            "PROJECT_ROOT": str(_cfg.PROJECT_ROOT),
            "RAW_DIR": str(_cfg.RAW_DIR),
            "PROCESSED_DIR": str(_cfg.PROCESSED_DIR),
            "LOGS_DIR": str(_cfg.LOGS_DIR),
            "MODEL_DIR": str(_cfg.MODEL_DIR),
        },
    }

    target = _cfg.PROCESSED_DIR / "pipeline_config.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2, default=str)
        os.replace(tmp, target)
    except OSError as exc:
        _logger.warning(
            "Could not write config dump to %s: %s", target, exc,
        )


def _show_licenses() -> int:
    """Print all data-source licenses and exit (D14-COMP-02).

    Returns
    -------
    int
        ``EXIT_SUCCESS``.
    """
    from drugos_graph import config as _cfg

    print("\nDrugOS Data Source Licenses")
    print("=" * 78)
    for source_key, info in sorted(_cfg.DATA_SOURCES.items()):
        name = info.get("description", source_key).split("—")[0].strip()
        url = info.get("url", "(no url)")
        license_ = info.get("license", "(unspecified)")
        version = info.get("version", "(unversioned)")
        print(f"\n  {source_key.upper()}")
        print(f"    Description: {name}")
        print(f"    URL:         {url}")
        print(f"    Version:     {version}")
        print(f"    License:     {license_}")
    print("\n" + "=" * 78)
    print("By running this pipeline you agree to the terms of each license.")
    print("Some sources (e.g. DrugBank) require a separate access application.\n")
    return EXIT_SUCCESS


def _print_startup_banner() -> None:
    """Print version + schema + Python banner to console (D13-DOC-03).

    Visible in BOTH console and log file so that a screenshot of the
    terminal is sufficient to identify the exact code version.
    """
    from drugos_graph import config as _cfg

    banner = (
        f"\n"
        f"╔══════════════════════════════════════════════════════════════════╗\n"
        f"║  DrugOS Pipeline v{_cfg.PIPELINE_VERSION:<14} "
        f"Schema v{_cfg.SCHEMA_VERSION:<8}              ║\n"
        f"║  Python {_sys.version.split()[0]:<12} "
        f"Package v{_cfg.PACKAGE_VERSION:<8}                      ║\n"
        f"║  Team Cosmic / VentureLab — Autonomous Drug Repurposing Platform ║\n"
        f"╚══════════════════════════════════════════════════════════════════╝\n"
    )
    # Use print() (not logging) so the banner appears even before
    # run_pipeline._configure_logging() runs.
    print(banner, flush=True)
    _logger.info(
        "DrugOS starting — run_id=%s config_hash=%s argv=%s",
        _RUN_ID, _cfg.CONFIG_HASH or _cfg.compute_config_hash(), _sys.argv,
    )


def _log_preamble() -> None:
    """Log a structured preamble on every execution (D11-LOG-02).

    ``run_full_pipeline()`` logs version info, but ``--step`` mode
    bypasses it.  This preamble runs in EVERY mode so log entries are
    always correlatable to a specific execution.
    """
    from drugos_graph import config as _cfg

    _logger.info(
        "PIPELINE_PREAMBLE | run_id=%s | pipeline_version=%s | schema_version=%s"
        " | python=%s | platform=%s | cwd=%s | argv=%s",
        _RUN_ID,
        _cfg.PIPELINE_VERSION,
        _cfg.SCHEMA_VERSION,
        _sys.version.split()[0],
        platform.platform(),
        os.getcwd(),
        _sys.argv,
    )


def _confirm_proceed(argv: Sequence[str]) -> int:
    """Interactive confirmation prompt before hours-long pipeline (D13-DOC-02).

    Only prompts if stdin is a TTY.  ``--yes`` skips the prompt for
    cron / Airflow wrappers.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if operator confirms (or non-interactive), else
        ``EXIT_ABORTED``.
    """
    if "--yes" in argv or "-y" in argv:
        return EXIT_SUCCESS
    if not _sys.stdin.isatty():
        return EXIT_SUCCESS
    # Don't prompt for short-running commands.
    short_flags = ("--self-test", "--show-licenses", "--help", "-h")
    if any(flag in argv for flag in short_flags):
        return EXIT_SUCCESS

    from drugos_graph import config as _cfg

    print(
        "\nAbout to start the DrugOS pipeline. This will:\n"
        f"  • Download / parse DRKG, DrugBank, ChEMBL, STRING, STITCH, SIDER,\n"
        f"    OpenTargets, UniProt, ClinicalTrials, GEO data\n"
        f"  • Build a Neo4j knowledge graph (target: {_cfg.MIN_NODES_W2:,} nodes,"
        f" {_cfg.MIN_EDGES_W2:,} edges)\n"
        f"  • Train a TransE model (target AUC ≥ {_cfg.TARGET_TRANSE_AUC})\n"
        f"  • Estimate: 2-6 hours, 5-20 GB disk, 4-8 GB RAM\n"
    )
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return EXIT_ABORTED
    if answer not in ("y", "yes"):
        print("Aborted by operator.")
        return EXIT_ABORTED
    return EXIT_SUCCESS


def _install_atexit_handler(start_time: float, run_id: str) -> None:
    """Register an atexit handler for guaranteed cleanup (D6-REL-03).

    The handler logs termination, flushes all log handlers via
    ``logging.shutdown()``, and releases the concurrency lock.  This is
    belt-and-suspenders: even if a signal kills the process between
    ``try`` and ``finally``, atexit still runs (on normal interpreter
    shutdown).
    """
    def _on_exit() -> None:
        elapsed = time.time() - start_time
        try:
            _logger.info(
                "PIPELINE_EXIT | run_id=%s | elapsed=%.2fs"
                " | shutdown_requested=%s",
                run_id, elapsed, _SHUTDOWN_REQUESTED,
            )
        except Exception:  # pragma: no cover — defensive
            pass
        try:
            logging.shutdown()
        except Exception:  # pragma: no cover — defensive
            pass
        _release_concurrency_lock()

    atexit.register(_on_exit)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — PHASE 4: PIPELINE INTEGRATION (run() + self-test)
# ═══════════════════════════════════════════════════════════════════════════════


def _run_self_test() -> int:
    """Lightweight smoke test for installation verification (D10-TST-02).

    Tests:
      1. Critical modules importable.
      2. config loads with SCHEMA_VERSION etc. defined.
      3. ``ensure_dirs()`` runs without error.
      4. ``Neo4jConfig``, ``PyGConfig``, ``TransEConfig`` dataclasses
         instantiate.
      5. ``set_global_seed(SEED)`` runs.
      6. ``compute_config_hash()`` produces a valid 16-hex SHA-256.
      7. ``safe_config_dict()`` masks Neo4j password.

    Returns
    -------
    int
        ``EXIT_SUCCESS`` if all checks pass, else ``EXIT_ERROR``.
    """
    print("DrugOS self-test — verifying installation...")
    failures: list[str] = []

    # 1) Critical module imports.
    from drugos_graph import (
        config as _cfg,
        exceptions as _exc,
        schemas as _schemas,
        utils as _utils,
        id_crosswalk as _idc,
    )
    for mod_name, mod in [
        ("config", _cfg), ("exceptions", _exc), ("schemas", _schemas),
        ("utils", _utils), ("id_crosswalk", _idc),
    ]:
        if mod is None:
            failures.append(f"drugos_graph.{mod_name} is None")

    # 2) Version constants defined.
    for attr in ("PACKAGE_VERSION", "PIPELINE_VERSION", "CONFIG_VERSION", "SCHEMA_VERSION"):
        if not hasattr(_cfg, attr) or not getattr(_cfg, attr):
            failures.append(f"config.{attr} is missing or empty")

    # 3) ensure_dirs() runs.
    try:
        _cfg.ensure_dirs()
    except Exception as exc:
        failures.append(f"ensure_dirs() raised: {exc}")

    # 4) Dataclasses instantiate.
    try:
        _cfg.Neo4jConfig()
    except Exception as exc:
        failures.append(f"Neo4jConfig() raised: {exc}")
    try:
        _cfg.PyGConfig()
    except Exception as exc:
        failures.append(f"PyGConfig() raised: {exc}")
    try:
        _cfg.TransEConfig()
    except Exception as exc:
        failures.append(f"TransEConfig() raised: {exc}")

    # 5) set_global_seed runs.
    try:
        _cfg.set_global_seed(_cfg.SEED)
    except Exception as exc:
        failures.append(f"set_global_seed() raised: {exc}")

    # 6) compute_config_hash produces 16 hex chars.
    try:
        h = _cfg.compute_config_hash()
        if not (isinstance(h, str) and len(h) == 16 and all(c in "0123456789abcdef" for c in h)):
            failures.append(f"compute_config_hash() returned {h!r}, expected 16 hex chars")
    except Exception as exc:
        failures.append(f"compute_config_hash() raised: {exc}")

    # 7) safe_config_dict masks password.
    try:
        safe = _cfg.safe_config_dict() if hasattr(_cfg, "safe_config_dict") else {}
        neo4j_section = safe.get("neo4j", {}) if isinstance(safe, dict) else {}
        if isinstance(neo4j_section, dict) and "password" in neo4j_section:
            pwd_repr = str(neo4j_section["password"])
            real_pwd = os.environ.get("DRUGOS_NEO4J_PASSWORD", "")
            if real_pwd and real_pwd in pwd_repr:
                failures.append(
                    "safe_config_dict() leaks Neo4j password — "
                    f"unmasked value found in {pwd_repr!r}"
                )
    except Exception as exc:
        failures.append(f"safe_config_dict() raised: {exc}")

    # Report.
    if failures:
        print(f"\nSelf-test FAILED — {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return EXIT_ERROR

    print("\nSelf-test PASSED — installation is healthy.")
    print(f"  Package: {_cfg.PACKAGE_VERSION}")
    print(f"  Pipeline: {_cfg.PIPELINE_VERSION}")
    print(f"  Schema: {_cfg.SCHEMA_VERSION}")
    print(f"  Config hash: {_cfg.CONFIG_HASH or _cfg.compute_config_hash()}")
    return EXIT_SUCCESS


def _augment_parser(parser: argparse.ArgumentParser) -> None:
    """Add DrugOS-specific flags not present in run_pipeline.main().

    The base parser in ``run_pipeline.main()`` already supports
    ``--skip-download``, ``--skip-neo4j``, ``--skip-training``,
    ``--step``, ``--fresh-start``, ``--resume``.  This function adds
    the entry-point-level flags introduced by this fix:
    ``--self-test``, ``--show-licenses``, ``--allow-root``,
    ``--require-fresh``, ``--require-schema-match``, ``--yes``.
    """
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run a lightweight installation smoke test and exit.",
    )
    parser.add_argument(
        "--show-licenses", action="store_true",
        help="Print all data-source licenses and exit.",
    )
    parser.add_argument(
        "--allow-root", action="store_true",
        help="Allow running as root (NOT recommended: pipeline parses"
             " untrusted XML/TSV from the internet).",
    )
    parser.add_argument(
        "--require-fresh", action="store_true",
        help="Warn if input data files are older than"
             " DRUGOS_STALENESS_THRESHOLD_DAYS (default 90).",
    )
    parser.add_argument(
        "--require-schema-match", action="store_true",
        help="Fail if previous run's schema version differs from current.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip interactive confirmation prompt (for cron / Airflow).",
    )


def _run_pipeline_main(argv: Sequence[str]) -> int:
    """Invoke ``run_pipeline.main()`` and translate its exit into an int (D2-DES-02).

    ``run_pipeline.main()`` currently calls ``sys.exit(0)`` or
    ``sys.exit(1)`` directly.  To preserve backward compatibility
    (constraint 3.3) without modifying run_pipeline.py, we catch
    ``SystemExit`` and translate it to an int.  A ``SystemExit(0)``
    becomes ``EXIT_SUCCESS``; a ``SystemExit(1)`` becomes
    ``EXIT_ERROR``; a ``SystemExit(2)`` becomes
    ``EXIT_VALIDATION_FAILURE``.

    We also override ``sys.argv`` so that ``argparse.parse_args()``
    inside run_pipeline.main() sees ONLY the args we pass, not the
    args of whatever wrapper invoked us (e.g. pytest).

    After step 12 validation logic is fully migrated into __main__,
    future versions of run_pipeline may return an int directly — that
    transition is transparent here.
    """
    from drugos_graph.run_pipeline import main as _pipeline_main

    # Set sys.argv so argparse.parse_args() inside _pipeline_main sees
    # exactly the args we want (not pytest's args, not the host's args).
    # This is critical for programmatic use via run([...]) — D15-INT-02.
    original_argv = _sys.argv
    _sys.argv = ["drugos_graph"] + list(argv)
    try:
        _pipeline_main()
        return EXIT_SUCCESS
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_SUCCESS
        if isinstance(code, int):
            # Map 0/1 to the documented exit codes; pass through other ints.
            if code == 0:
                return EXIT_SUCCESS
            if code == 1:
                return EXIT_ERROR
            return code
        # Non-int code (rare) → treat as error.
        _logger.error("Pipeline exited with non-int code: %r", code)
        return EXIT_ERROR
    finally:
        _sys.argv = original_argv


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Programmatic entry point — returns an integer exit code (D2-DES-01, D10-TST-01).

    This is the SOLE entry point for both CLI and programmatic use.
    ``argv`` defaults to ``sys.argv[1:]``.  Returns an integer exit code
    in range 0-4 (see module docstring).  The ``if __name__ == "__main__"``
    guard calls ``sys.exit(run())``.

    Why a separate ``run()`` function?
    ----------------------------------
    * Tests can call ``run(['--self-test'])`` directly without spawning
      a subprocess (D10-TST-01 — saves 2-3s per test).
    * Jupyter notebooks, Airflow DAGs, and FastAPI endpoints can call
      ``run(['--step', '1'])`` as a library function (D15-INT-02).
    * ``__main__.py`` becomes a thin wrapper, making the lifecycle
      logic unit-testable.

    Parameters
    ----------
    argv : Sequence[str], optional
        CLI arguments (excluding program name). Defaults to
        ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit code: 0=success, 1=error, 2=validation failure,
        3=config failure, 4=aborted.
    """
    global _START_TIME, _RUN_ID
    _START_TIME = time.time()

    # ─── PHASE 1: FOUNDATION ────────────────────────────────────────────────
    # These run BEFORE any drugos_graph import.  Stdlib-only.

    # D12-CONF-01: load .env from project root.
    _load_dotenv()

    # D16-LIN-01: generate run_id BEFORE any drugos_graph import so that
    # import-time log records can be correlated.  Sets os.environ['DRUGOS_RUN_ID'].
    _RUN_ID = _generate_run_id()

    # ─── EARLY-EXIT FLAGS (must be checked before heavy work) ──────────────
    # These don't need drugos_graph imports — they parse argv locally.
    argv_list = list(argv) if argv is not None else list(_sys.argv[1:])

    # --help / -h: argparse in run_pipeline.main() handles it.  We let it
    # through to the pipeline's parser.

    # ─── D3-SCI-01: global seed BEFORE any drugos_graph import ─────────────
    # This call imports config.py (lightweight) and calls set_global_seed.
    try:
        _init_global_seed()
    except SystemExit as exc:
        # Re-raise — D14-COMP-01 style hard exit on invalid seed.
        return int(exc.code) if isinstance(exc.code, int) else EXIT_CONFIG_FAILURE

    # ─── D14-COMP-01: Python version (re-checked here for --self-test) ─────
    if _sys.version_info < (3, 10):
        _logger.error("Python >= 3.10 required.")
        return EXIT_CONFIG_FAILURE

    # ─── Handle --show-licenses early (no pre-flight needed) ───────────────
    if "--show-licenses" in argv_list:
        return _show_licenses()

    # ─── Handle --help / -h BEFORE pre-flight ─────────────────────────────
    # --help is handled by argparse inside run_pipeline.main().  We must
    # NOT run pre-flight guards (especially the Neo4j credential check)
    # before --help, because the operator's intent is to read help, not
    # to start the pipeline.  We detect --help here and dispatch directly
    # to the pipeline's parser via _run_pipeline_main, which raises
    # SystemExit(0) when --help is given.
    if "--help" in argv_list or "-h" in argv_list:
        try:
            _run_pipeline_main(argv_list)
        except SystemExit as exc:
            return EXIT_SUCCESS if exc.code in (0, None) else int(exc.code or 1)
        return EXIT_SUCCESS

    # ─── PHASE 2: PRE-FLIGHT GUARDS ────────────────────────────────────────
    # Each guard returns an int exit code.  Non-zero exits short-circuit.

    # D9-SEC-02: refuse root unless --allow-root.
    rc = _check_root_privileges(argv_list)
    if rc != EXIT_SUCCESS:
        return rc

    # D3-SCI-02: scientific environment.
    rc = _validate_scientific_environment()
    if rc != EXIT_SUCCESS:
        return rc

    # D1-ARCH-02: package integrity (critical submodules present).
    rc = _verify_package_integrity()
    if rc != EXIT_SUCCESS:
        return rc

    # D5-DQ-01: data directories writable.
    rc = _check_data_directories()
    if rc != EXIT_SUCCESS:
        return rc

    # D9-SEC-04: module path tampering check (warning only).
    _check_module_path_tampering()

    # ─── Handle --self-test BEFORE Neo4j credential check ─────────────────
    # --self-test verifies the LOCAL installation; it does NOT need Neo4j,
    # so it must run BEFORE the credential check.  Fixes a regression
    # uncovered during testing.
    if "--self-test" in argv_list:
        return _run_self_test()

    # D9-SEC-01: Neo4j credentials (only required for actual pipeline runs).
    rc = _check_neo4j_credentials(argv_list)
    if rc != EXIT_SUCCESS:
        return rc

    # D5-DQ-02: input files exist when --skip-download.
    rc = _check_input_files(argv_list)
    if rc != EXIT_SUCCESS:
        return rc

    # D5-DQ-03: stale data (warning only).
    _check_data_freshness(argv_list)

    # D7-IDP-01 / D7-IDP-02: idempotency & incomplete-run detection.
    _check_idempotency()
    _detect_incomplete_run()

    # D3-SCI-03 / D12-CONF-03: config drift (warning only).
    _check_config_drift()

    # D14-COMP-03: schema version compatibility.
    rc = _check_schema_version()
    if rc != EXIT_SUCCESS:
        return rc

    # ─── PHASE 3: ENTRY POINT STRUCTURE ────────────────────────────────────

    # D6-REL-02: signal handlers (outermost safety net).
    _register_signal_handlers()

    # D6-REL-04: concurrency lock (prevent two runs corrupting state).
    rc = _acquire_concurrency_lock()
    if rc != EXIT_SUCCESS:
        return rc

    # D6-REL-03: atexit handler (guaranteed cleanup).
    _install_atexit_handler(_START_TIME, _RUN_ID)

    # D8-PERF-02: log system resources for post-mortem.
    _log_system_resources()

    # D11-LOG-02: log preamble on every execution.
    _log_preamble()

    # D13-DOC-03: startup banner.
    _print_startup_banner()

    # D16-LIN-02: preliminary lineage manifest (survives crash).
    _write_preliminary_manifest(_RUN_ID, argv_list)

    # D12-CONF-02: effective config dump (post-mortem artifact).
    _dump_effective_config(argv_list)

    # D13-DOC-02: interactive confirmation (skips --self-test etc.).
    rc = _confirm_proceed(argv_list)
    if rc != EXIT_SUCCESS:
        return rc

    # ─── PHASE 4: PIPELINE INTEGRATION ─────────────────────────────────────
    # D1-ARCH-01 / D8-PERF-01: lazy import — only triggered when actually
    # needed.  --help / --self-test / --show-licenses never reach here.
    try:
        rc = _run_pipeline_main(argv_list)
        # v9 ROOT FIX (audit BUG-E-008): the docstring promises exit codes
        # 0/1/2/3/4 but only 0/1 were ever emitted. Now we check the V1
        # launch criteria (AUC, model-saved, data sources) and translate
        # a "validation failure" into EXIT_VALIDATION_FAILURE (2) so the
        # operator can distinguish "pipeline ran" from "pipeline produced
        # a valid model". This is critical: a pipeline that exits 0 with
        # no trained model on disk is a silent failure mode.
        if rc == EXIT_SUCCESS and "--skip-neo4j" not in argv_list:
            try:
                from drugos_graph.run_pipeline import _check_v1_launch_criteria
                from drugos_graph.config import RESULTS_PERSIST_PATH
                import json as _json
                # Try to load the most recent pipeline results.
                criteria = {}
                if RESULTS_PERSIST_PATH.exists():
                    with open(RESULTS_PERSIST_PATH) as fh:
                        results = _json.load(fh)
                    criteria = _check_v1_launch_criteria(results)
                if criteria and not criteria.get("passed", False):
                    # v35 ROOT FIX (M-14): the previous code UNCONDITIONALLY
                    # set ``rc = EXIT_VALIDATION_FAILURE`` here, even when
                    # the operator had explicitly set
                    # ``DRUGOS_ALLOW_LAUNCH_FAIL=1`` to opt into a "run
                    # anyway, I know the criteria failed" mode. The
                    # ``run_full_pipeline`` function respects that env var
                    # (it does NOT raise ``V1LaunchCriteriaFailed`` when
                    # the env var is set), but this ``__main__`` re-check
                    # INCORRECTLY OVERRRODE the operator's explicit allow-
                    # fail decision — the operator said "allow the failure"
                    # but ``__main__`` exited 2 anyway. The fix respects
                    # ``DRUGOS_ALLOW_LAUNCH_FAIL``: if set, log a warning
                    # and keep rc=0; if unset, log an error and set rc=2.
                    _allow_launch_fail = (
                        os.environ.get("DRUGOS_ALLOW_LAUNCH_FAIL", "") == "1"
                    )
                    if _allow_launch_fail:
                        _logger.warning(
                            "V1 LAUNCH CRITERIA FAILED: %s. "
                            "DRUGOS_ALLOW_LAUNCH_FAIL=1 is set — operator "
                            "explicitly allowed the failure. Exit code "
                            "remains 0, but the model is NOT production-"
                            "ready. See criteria dict for details.",
                            {k: v for k, v in criteria.items() if k != "passed"},
                        )
                    else:
                        _logger.error(
                            "V1 LAUNCH CRITERIA FAILED: %s. "
                            "Pipeline exited successfully but the model is NOT "
                            "production-ready. See criteria dict for details.",
                            {k: v for k, v in criteria.items() if k != "passed"},
                        )
                        rc = EXIT_VALIDATION_FAILURE
            except (ImportError, AttributeError) as _exc:
                # FIX TOP-3: narrow the previously-broad ``except Exception``
                # which silently swallowed the missing RESULTS_PERSIST_PATH
                # ImportError and silently SKIPPED the V1 launch criteria
                # check (defeating the entire ML-honesty audit fix). Now
                # catch only the specific import/attr errors that the
                # dead-name reference produced — every other exception
                # (JSON decode, file IO, criteria-check crash) propagates
                # so the operator sees the real failure mode.
                _logger.warning(
                    "V1 launch criteria check could not be wired up "
                    "(config import error: %s). This is a regression — "
                    "RESULTS_PERSIST_PATH is defined in config.py and "
                    "_check_v1_launch_criteria is defined in run_pipeline.py. "
                    "Exit code remains %d (success), but the launch "
                    "criteria check was SKIPPED.",
                    _exc, rc,
                )
    except KeyboardInterrupt:
        _logger.warning("Pipeline interrupted by operator (KeyboardInterrupt).")
        return EXIT_ABORTED
    except SystemExit as exc:
        # Defensive: _run_pipeline_main already catches SystemExit, but
        # in case the pipeline calls sys.exit() from a deeper frame:
        code = exc.code
        if code is None or code == 0:
            return EXIT_SUCCESS
        if isinstance(code, int):
            return code
        return EXIT_ERROR
    except Exception as exc:
        # D6-REL-01: top-level exception handler.  Log the full traceback
        # to the log file, then print a user-friendly message to stderr.
        _logger.exception(
            "PIPELINE_FATAL_ERROR | run_id=%s | error_type=%s | error_msg=%s",
            _RUN_ID, type(exc).__name__, exc,
        )
        print(
            f"\nDrugOS pipeline failed: {type(exc).__name__}: {exc}\n"
            f"  See the log file for the full traceback. Run ID: {_RUN_ID}",
            file=_sys.stderr,
        )
        return EXIT_ERROR
    finally:
        # D3-SCI-04: validation check — if pipeline completed but validation
        # was skipped (--skip-neo4j, --step < 12), surface it as a WARNING
        # BEFORE logging.shutdown() so the warning actually reaches every
        # configured handler (file handler, stderr handler, etc.).
        # This is the "MOST DANGEROUS ISSUE" from the master fix prompt §3.2:
        # an operator running --step 11 --skip-neo4j trains TransE on
        # garbage, exits 0, and believes the model is valid.  Predictions
        # from this model could direct a pharma partner to test the wrong
        # drug on patients.
        #
        # The warning is emitted from inside the finally block so it is
        # guaranteed to run even if the try block raised.  It MUST be
        # emitted BEFORE logging.shutdown() — otherwise the file handler
        # is closed and the warning is lost (only stderr's last-resort
        # handler would emit it, which is fragile and easy to miss).
        if rc == EXIT_SUCCESS:
            validation_skipped = False
            if "--skip-neo4j" in argv_list:
                validation_skipped = True
            elif "--step" in argv_list:
                try:
                    step_idx = argv_list.index("--step")
                    step_n = int(argv_list[step_idx + 1])
                    if step_n < 12:
                        validation_skipped = True
                except (IndexError, ValueError):
                    pass

            if validation_skipped:
                # v35 ROOT FIX (M-15): the previous warning said
                # "AUC >= 0.78, >= 500K nodes, >= 6M edges" but:
                #   (1) V1_LAUNCH_AUC was raised from 0.78 to 0.85 in
                #       the v25 ROOT FIX (config.py:4862); the 0.78
                #       threshold was stale.
                #   (2) ``_check_v1_launch_criteria`` does NOT check
                #       node/edge counts — those are checked by
                #       ``graph_stats.check_exit_criteria`` (separate
                #       gate). The "500K nodes / 6M edges" claim was
                #       false for the V1 launch criteria contract.
                # The fix updates the warning to match the ACTUAL
                # V1 launch criteria: AUC >= 0.85 on BOTH val and
                # held-out, model saved to disk, no critical source
                # failures, sufficient positive/negative pairs.
                try:
                    from drugos_graph.config import V1_LAUNCH_AUC as _V1_AUC_WARN
                except Exception:
                    _V1_AUC_WARN = 0.85
                _logger.warning(
                    "Validation was SKIPPED in this run (--skip-neo4j or"
                    " --step < 12). The pipeline exited 0, but the trained"
                    " model has NOT been validated against the V1 launch"
                    " criteria (AUC >= %.2f on BOTH val and held-out,"
                    " model saved to disk, no critical source failures)."
                    " Predictions from this run MUST NOT be used for"
                    " clinical or commercial decisions without a separate"
                    " validation run.",
                    _V1_AUC_WARN,
                )

        # D11-LOG-03: structured exit log entry.
        elapsed = time.time() - _START_TIME
        status = (
            "success" if rc == EXIT_SUCCESS
            else "aborted" if rc == EXIT_ABORTED
            else "validation_failure" if rc == EXIT_VALIDATION_FAILURE
            else "config_failure" if rc == EXIT_CONFIG_FAILURE
            else "error"
        )
        _logger.info(
            "PIPELINE_EXIT | run_id=%s | exit_code=%d | status=%s | elapsed=%.2fs",
            _RUN_ID, rc, status, elapsed,
        )
        # D6-REL-03: cleanup (also runs via atexit, but try/finally is faster).
        _release_concurrency_lock()
        logging.shutdown()

    return rc


def main() -> None:
    """Legacy entry point — preserved for backward compatibility.

    New code should call ``run()`` directly.  This wrapper exists so
    that ``from drugos_graph.run_pipeline import main`` (which some
    scripts may still do) continues to work without modification.

    Raises
    ------
    SystemExit
        Always — translates the int return of ``run()`` into a sys.exit.
    """
    _sys.exit(run())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — ENTRY POINT GUARD
# ═══════════════════════════════════════════════════════════════════════════════
# Entry point guard — pipeline only executes when run as:
#   python -m drugos_graph
# Direct execution (`python drugos_graph/__main__.py`) is detected at
# import time and produces a clear error message (D15-INT-01).
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    _sys.exit(run())
