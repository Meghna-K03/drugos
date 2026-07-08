"""
Configuration settings for the Drug Repurposing ETL Platform.

This module defines all configuration values consumed by the seven ETL
pipelines (ChEMBL, DrugBank, UniProt, STRING, DisGeNET, OMIM, PubChem),
the database connection layer, the Airflow DAGs, and the entity
resolution modules.

Settings are loaded from environment variables, with .env file support
via python-dotenv (optional, graceful degradation if not installed).
The preferred import pattern is::

    from config import DATABASE_URL, STRING_MIN_COMBINED_SCORE

Direct imports from this module are also supported for backward
compatibility::

    from config.settings import DATABASE_URL

Loading Strategy
----------------
Environment variables are NOT read at import time. Instead, the first
access to any setting triggers ``_ensure_dotenv_loaded()`` which loads
the ``.env`` file exactly once. This makes importing this module
side-effect-free — safe for DAG parsing, test frameworks, and IDE
autocompletion.

Configuration Groups
--------------------
- **Database**: DATABASE_URL
- **ChEMBL**: CHEMBL_VERSION, CHEMBL_API_URL, CHEMBL_MAX_ROWS,
  CHEMBL_MAX_ACTIVITIES, CHEMBL_EXPECTED_DRUG_COUNT_MIN/MAX
- **STRING**: STRING_VERSION, STRING_MIN_COMBINED_SCORE,
  STRING_PROTEIN_LINKS_URL, STRING_ALIASES_URL,
  STRING_PROTEIN_LINKS_DETAILED_URL
- **DisGeNET**: DISGENET_API_KEY, DISGENET_API_URL, DISGENET_USE_API
- **DrugBank**: DRUGBANK_XML_PATH
- **OMIM**: OMIM_API_KEY, OMIM_API_BASE
- **UniProt**: UNIPROT_RELEASE
- **PubChem**: PUBCHEM_REST_BASE, PUBCHEM_FTP_BASE
- **Processing**: CHEMBL_EXPECTED_DRUG_COUNT_MIN/MAX,
  STRING_MIN_COMBINED_SCORE
- **Logging**: LOG_LEVEL, setup_logging()
- **Provenance**: DATA_SNAPSHOT_ID, get_data_version_info(),
  get_provenance_metadata()
- **Environment**: ENVIRONMENT (development / staging / production)

Environment Variables
---------------------
All settings can be overridden via environment variables. See
``.env.example`` for the complete list with descriptions and default
values.

Naming Convention
-----------------
Settings follow the pattern ``{SOURCE}_{TYPE}_{DETAIL}`` where TYPE is
one of: URL, PATH, KEY, LIMIT, SCORE, VERSION, FLAG.

Examples::

    CHEMBL_VERSION             (source=ChEMBL, type=VERSION)
    CHEMBL_API_URL             (source=ChEMBL, type=URL, detail=API)
    DISGENET_API_KEY           (source=DisGeNET, type=KEY, detail=API)
    DRUGBANK_XML_PATH          (source=DrugBank, type=PATH, detail=XML)
    STRING_MIN_COMBINED_SCORE  (source=STRING, type=SCORE, detail=MIN_COMBINED)

Deprecated Settings
-------------------
The following settings are deprecated and will be removed in v2.0.0
(scheduled: 2025-Q4). Accessing them triggers a ``DeprecationWarning``:

- ``CHEMBL_URL`` — use ``CHEMBL_API_URL`` or the ChEMBL pipeline directly
- ``UNIPROT_SPROT_URL`` — use the UniProt REST API
- ``UNIPROT_TREMBL_URL`` — use the UniProt REST API
- ``STRING_PROTEIN_INFO_URL`` — not used by any pipeline
- ``DISGENET_STATIC_URL`` — use ``DISGENET_API_URL`` (static URL
  deprecated since 2024)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Lazy dotenv loading — ARCH-1, ARCH-5, RELI-3
# ---------------------------------------------------------------------------
# python-dotenv is an OPTIONAL dependency. If it is not installed, the
# platform falls back to plain os.getenv() (environment variables must be
# set externally via Docker, systemd, or the shell).

_dotenv_loaded: bool = False

# Module-level load_dotenv binding so tests can mock `config.settings.load_dotenv`
# and downstream code can call it without re-importing. python-dotenv is an
# OPTIONAL dependency — if it's not installed, `load_dotenv` is a no-op
# function that returns False (and logs once).
try:
    from dotenv import load_dotenv as _load_dotenv_func  # type: ignore[import-untyped]

    def load_dotenv(*args, **kwargs):  # type: ignore[no-redef]
        """Module-level wrapper around python-dotenv's load_dotenv."""
        return _load_dotenv_func(*args, **kwargs)

except ImportError:  # pragma: no cover — exercised when dotenv is missing
    def load_dotenv(*args, **kwargs):  # type: ignore[no-redef]
        """No-op fallback when python-dotenv is not installed."""
        logging.getLogger(__name__).info(
            "python-dotenv is not installed. Environment variables must "
            "be set externally. Install with: pip install python-dotenv"
        )
        return False


def _ensure_dotenv_loaded() -> None:
    """Load the .env file exactly once, if it exists.

    This function is called on the first access to any setting via
    ``_getenv()``.  It is idempotent — subsequent calls are no-ops.
    If python-dotenv is not installed, a single info-level message is
    logged and the function falls back to pure ``os.getenv()``.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    try:
        env_path = Path(__file__).parent.parent / ".env"
        loaded = load_dotenv(env_path, override=False)
        if not loaded and not env_path.exists():
            logging.getLogger(__name__).info(
                "No .env file found at %s. All settings will use "
                "environment variables or defaults.",
                env_path,
            )
        elif loaded:
            logging.getLogger(__name__).debug(
                "Loaded .env file from %s", env_path
            )
    except Exception as exc:  # noqa: BLE001 — defensive: never crash on env load
        logging.getLogger(__name__).warning(
            "Failed to load .env file: %s. Falling back to os.getenv().", exc
        )


def _getenv(key: str, default: str = "") -> str:
    """Read an environment variable, ensuring .env has been loaded first.

    This is the canonical way to read env vars in this module. It
    guarantees that ``_ensure_dotenv_loaded()`` has been called before
    any ``os.getenv()`` access.
    """
    _ensure_dotenv_loaded()
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Safe parsing utilities — CODE-1, CODE-2, CODE-4, RELI-1
# ---------------------------------------------------------------------------


def _parse_optional_int(env_key: str, default: Optional[int] = None) -> Optional[int]:
    """Parse an optional integer environment variable.

    - Returns ``None`` if the env var is unset or empty string.
    - Returns ``0`` if the env var is explicitly set to ``0``.
    - Raises ``ValueError`` with a clear message if the value is not a
      valid integer or is negative.
    """
    _ensure_dotenv_loaded()
    raw = os.getenv(env_key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {env_key}={raw!r} is not a valid integer"
        )
    if value < 0:
        raise ValueError(
            f"Environment variable {env_key}={value} must be non-negative"
        )
    return value


def _parse_required_int(env_key: str, default: str) -> int:
    """Parse a required integer environment variable with a default.

    Always returns an ``int``.  Raises ``ValueError`` with the variable
    name if the value cannot be parsed.
    """
    _ensure_dotenv_loaded()
    raw = os.getenv(env_key, default)
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {env_key}={raw!r} is not a valid integer"
        )


def _parse_bool(value: str, default: bool = True) -> bool:
    """Parse a boolean environment variable value.

    Accepts (case-insensitive): true, false, 1, 0, yes, no, on, off.
    Raises ``ValueError`` for unrecognizable values.
    """
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return default
    if cleaned in ("true", "1", "yes", "on"):
        return True
    if cleaned in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"Cannot parse boolean from {value!r}")


def _getenv_bool(key: str, default: bool) -> bool:
    """Read a boolean env var; return default if unset.

    Accepts (case-insensitive): true/false, 1/0, yes/no, on/off.  Empty
    or unset values fall back to ``default``.  Unrecognised non-empty
    values raise ``ValueError`` via ``_parse_bool``.
    """
    raw = _getenv(key, "")
    if not raw.strip():
        return default
    return _parse_bool(raw, default=default)


def _getenv_float(key: str, default: float) -> float:
    """Read a float env var; return default if unset/empty."""
    raw = _getenv(key, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"env var {key!r}={raw!r} is not a valid float"
        ) from exc


def _getenv_int(key: str, default: int) -> int:
    """Read an int env var; return default if unset/empty."""
    raw = _getenv(key, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"env var {key!r}={raw!r} is not a valid int"
        ) from exc


def _parse_csv_ints(key: str, default: list[int]) -> list[int]:
    """Parse a comma-separated list of ints from an env var.

    Returns the default if unset/empty. Raises ValueError on malformed input.
    Used by OMIM_MAPPING_KEYS_INCLUDE (master prompt BUG-2.5).
    """
    raw = _getenv(key, "")
    if not raw.strip():
        return list(default)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return list(default)
    try:
        return [int(p) for p in parts]
    except ValueError as exc:
        raise ValueError(
            f"env var {key!r}={raw!r} contains non-integer values"
        ) from exc


# ---------------------------------------------------------------------------
# Deprecated setting descriptor — DESIGN-1, DOC-4
# ---------------------------------------------------------------------------


class _DeprecatedSetting:
    """Descriptor that raises ``DeprecationWarning`` when accessed.

    Keeps the setting accessible for backward compatibility but actively
    warns any code that accesses it.
    """

    def __init__(self, name: str, replacement: str, value: object) -> None:
        self._name = name
        self._replacement = replacement
        self._value = value

    def __get__(self, obj: object | None, objtype: type | None = None) -> object:
        warnings.warn(
            f"Setting `{self._name}` is DEPRECATED. Use `{self._replacement}` "
            f"instead. Will be removed in v2.0.0 (scheduled: 2025-Q4).",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._value

    def __set__(self, obj: object, value: object) -> None:
        # v28 ROOT FIX (audit TOP-22): previously ``__set__`` silently
        # accepted mutations to deprecated settings. A future operator
        # (or stale code path) could write to the deprecated name, the
        # value would persist, and downstream code reading the
        # REPLACEMENT name would never see the mutation — a silent
        # configuration drift bug. The fix emits a DeprecationWarning
        # on EVERY mutation so the operator sees the deprecated write
        # in logs (the warning machinery forwards to logging if
        # ``warnings.catch_warnings`` is not installed) AND surfaces
        # the canonical replacement name in the message. The mutation
        # is still permitted for backward compatibility — but it is no
        # longer silent.
        warnings.warn(
            f"Setting `{self._name}` is DEPRECATED. Mutating it via "
            f"`{self._name} = ...` will update the deprecated alias "
            f"only; downstream code reading `{self._replacement}` will "
            f"NOT see this change. Use `{self._replacement}` instead. "
            f"Will be removed in v2.0.0 (scheduled: 2025-Q4). "
            f"(v28 audit TOP-22: silent deprecated-setting mutation.)",
            DeprecationWarning,
            stacklevel=2,
        )
        self._value = value


# ---------------------------------------------------------------------------
# Environment & project root — DESIGN-3, ARCH-6
# ---------------------------------------------------------------------------

# FIX TOP-2: Standardize on DRUGOS_ENVIRONMENT across both phases.
# Phase 1 previously read ``ENVIRONMENT`` (vocabulary: dev/staging/prod).
# Phase 2 reads ``DRUGOS_ENVIRONMENT`` (vocabulary: development/staging/
# production). The mismatch meant operators could set DRUGOS_ENVIRONMENT=
# production and Phase 1 would still run in dev mode — silently defeating
# the production-mode guards. We now:
#   * Read DRUGOS_ENVIRONMENT as the canonical name.
#   * Fall back to the legacy ENVIRONMENT var for backward compat.
#   * Standardize the vocabulary on {development, staging, production}
#     (Phase 2's vocabulary). Old values are normalized:
#       dev  -> development
#       prod -> production
#       staging -> staging (unchanged)
# Synchronized with phase2/drugos_graph/config.py — DO NOT diverge
# (audit TOP-2).
_raw_environment: str = (
    os.getenv("DRUGOS_ENVIRONMENT")
    or os.getenv("ENVIRONMENT", "development")
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
ENVIRONMENT: str = _ENV_NORMALIZATION.get(_raw_environment, _raw_environment)

BASE_DIR: Path = Path(_getenv("PROJECT_ROOT", str(Path(__file__).parent.parent)))

# Validate BASE_DIR points to a real project root (ARCH-6)
if not (BASE_DIR / "config").exists():
    warnings.warn(
        f"BASE_DIR ({BASE_DIR}) does not appear to be the project root. "
        f"Set PROJECT_ROOT env var to the correct path.",
        RuntimeWarning,
    )

# ---------------------------------------------------------------------------
# Environment-specific profile defaults — DESIGN-3
# ---------------------------------------------------------------------------

_PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "development": {
        "CHEMBL_MAX_ROWS": "1000",
        "CHEMBL_MAX_ACTIVITIES": "50000",
        "STRING_MIN_COMBINED_SCORE": "700",
        "LOG_LEVEL": "DEBUG",
    },
    "staging": {
        "CHEMBL_MAX_ROWS": "5000",
        "LOG_LEVEL": "INFO",
    },
    "production": {
        "CHEMBL_MAX_ROWS": "0",
        "LOG_LEVEL": "WARNING",
    },
}


def _get_profile_default(key: str, fallback: str) -> str:
    """Get a profile-specific default, overridden by explicit env vars.

    Explicit environment variables always take precedence over profile
    defaults.  Profile defaults are only used when the env var is not set.
    """
    _ensure_dotenv_loaded()
    explicit = os.getenv(key)
    if explicit is not None:
        return explicit
    profile = _PROFILE_DEFAULTS.get(ENVIRONMENT, {})
    return profile.get(key, fallback)


# ---------------------------------------------------------------------------
# Database — DATA-2, SEC-1, CONF-2
# ---------------------------------------------------------------------------

# Default uses placeholder credentials, NOT hardcoded real ones.
# In development, docker-compose defaults are auto-applied with a warning.
DATABASE_URL: str = _getenv(
    "DATABASE_URL",
    "postgresql://REPLACE_USER:REPLACE_PASSWORD@localhost:5432/drug_repurposing",
)

# Auto-apply docker-compose defaults in development when placeholder is present
# v28 ROOT FIX (audit TOP-11): previously this block silently swapped the
# placeholder DATABASE_URL to ``cosmic:cosmic`` in dev mode with only a
# Python ``warnings.warn`` — which is filtered by default in pytest,
# swallowed by logging, and invisible to operators who run the pipeline
# via ``python3 run_unified.py`` (no -W flag). That meant a developer
# could run the entire Phase 1 ETL against a default-credential DB and
# never see a single console message telling them so. Real-world risk:
# someone copy-pastes dev settings into a staging box and the
# cosmic:cosmic default silently takes over because the env var was
# missing — exactly the failure mode that produced the v28 audit.
#
# The fix is two-layered:
#   1. The silent swap is gated behind an EXPLICIT opt-in env var
#      ``DRUGOS_DEV_ALLOW_DEFAULT_DB=1``. Without it, the dev environment
#      will RAISE — forcing the operator to either set DATABASE_URL or
#      acknowledge the insecure default.
#   2. When the opt-in is set, we emit a LOUD log.warning (visible in
#      every log sink, not just Python's warning machinery) AND a
#      UserWarning, so the message survives pytest -p no:warnings and
#      any operator's stderr filter.
if "REPLACE_USER" in DATABASE_URL or "REPLACE_PASSWORD" in DATABASE_URL:
    if ENVIRONMENT == "development":
        # Use a module-level logger so the message lands in the standard
        # log pipeline (file + stderr) regardless of the warnings filter.
        _log = logging.getLogger("drugos.config.settings")
        allow_default_db = os.getenv("DRUGOS_DEV_ALLOW_DEFAULT_DB", "") == "1"
        # v34 ROOT FIX (CRITICAL #4): the previous code fired BOTH warnings
        # AND the credential swap REGARDLESS of `allow_default_db`. The
        # opt-in flag was cosmetic. Now we:
        #   1. REFUSE to apply dev default credentials unless
        #      `DRUGOS_DEV_ALLOW_DEFAULT_DB=1` is explicitly set.
        #   2. If not set, log an ERROR and leave DATABASE_URL pointing at
        #      the placeholder (which will fail at connection time with a
        #      clear "REPLACE_USER" message rather than silently using
        #      cosmic:cosmic).
        #   3. When the opt-in IS set, emit a SINGLE consolidated warning
        #      (not two contradictory ones) and apply the swap.
        if not allow_default_db:
            _log.error(
                "DATABASE_URL contains placeholder credentials "
                "(REPLACE_USER/REPLACE_PASSWORD) but "
                "DRUGOS_DEV_ALLOW_DEFAULT_DB=1 is NOT set. REFUSING to "
                "apply dev default credentials. The module is importable "
                "but any DB connection will fail. To acknowledge the "
                "insecure default and use cosmic:cosmic@localhost, set "
                "DRUGOS_DEV_ALLOW_DEFAULT_DB=1. (v34 root fix CRITICAL #4)"
            )
            # Do NOT modify DATABASE_URL — leave the placeholder so the
            # connection fails loudly with a clear error message.
        else:
            # Opt-in acknowledged. Single consolidated warning.
            _log.warning(
                "DRUGOS DEV MODE: DRUGOS_DEV_ALLOW_DEFAULT_DB=1 is set — "
                "applying docker-compose default credentials "
                "(cosmic:cosmic by default; override with "
                "DRUGOS_DEV_DB_USER / DRUGOS_DEV_DB_PASSWORD / "
                "DRUGOS_DEV_DB_HOST / DRUGOS_DEV_DB_PORT / "
                "DRUGOS_DEV_DB_NAME env vars). This is INSECURE and "
                "MUST NOT be used outside local development. (v34 root "
                "fix CRITICAL #4)"
            )
            warnings.warn(
                "DATABASE_URL contains placeholder credentials. "
                "DRUGOS_DEV_ALLOW_DEFAULT_DB=1 is set — using docker-"
                "compose defaults (cosmic:cosmic by default; override "
                "via DRUGOS_DEV_DB_* env vars). This is INSECURE — set "
                "DATABASE_URL explicitly for any non-local environment.",
                UserWarning,
            )
            _dev_db_user = _getenv("DRUGOS_DEV_DB_USER", "cosmic")
            _dev_db_password = _getenv("DRUGOS_DEV_DB_PASSWORD", "cosmic")
            _dev_db_host = _getenv("DRUGOS_DEV_DB_HOST", "localhost")
            _dev_db_port = _getenv("DRUGOS_DEV_DB_PORT", "5432")
            _dev_db_name = _getenv("DRUGOS_DEV_DB_NAME", "drug_repurposing")
            DATABASE_URL = (
                f"postgresql://{_dev_db_user}:{_dev_db_password}@"
                f"{_dev_db_host}:{_dev_db_port}/{_dev_db_name}"
            )
            del _dev_db_user, _dev_db_password, _dev_db_host
            del _dev_db_port, _dev_db_name
    elif ENVIRONMENT in ("staging", "production"):
        raise ValueError(
            "DATABASE_URL contains placeholder credentials. "
            "Set the DATABASE_URL environment variable with real credentials "
            f"for the {ENVIRONMENT} environment."
        )

# Detect Docker and warn about localhost (CONF-2)
if Path("/.dockerenv").exists() and "localhost" in DATABASE_URL:
    warnings.warn(
        "DATABASE_URL contains localhost but you appear to be running "
        "inside Docker. localhost inside a container refers to the "
        "container itself, not the host. Use host.docker.internal or "
        "the service name (e.g., postgres) instead.",
        UserWarning,
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

RAW_DATA_DIR: Path = BASE_DIR / "raw_data"
# FIX TOP-12: ``PROCESSED_DATA_DIR`` (phase1/processed_data/) is the
# OUTPUT directory for Phase 1 pipelines — DrugBank/OMIM/STRING/ChEMBL
# CSVs land here. It is a READ-ONLY upstream artifact for Phase 2 (Phase 2
# reads these CSVs via the phase1_bridge; never writes to them). Phase 2's
# own outputs go to phase2/data/processed/ — keep the two paths distinct.
# Synchronized with phase2/drugos_graph/config.py
# (``PHASE1_PROCESSED_DIR`` constant) — DO NOT diverge (audit TOP-12).
PROCESSED_DATA_DIR: Path = BASE_DIR / "processed_data"

# ---------------------------------------------------------------------------
# ChEMBL — SCI-2, SCI-4, IDMP-2, INTEROP-2
# ---------------------------------------------------------------------------

DEFAULT_CHEMBL_VERSION: str = "35"  # CONF-1 — minimum supported version

# Valid ChEMBL database release versions.
# ChEMBL is a continuously-updated biomedical database. New releases are
# published 2-3 times per year by EBI. As of 2025, ChEMBL has reached
# release 37. The pipeline ADAPTS to whatever version the live API returns
# (see ChEMBLPipeline._verify_chembl_version), so adding a new version
# here only affects validation warnings — it does NOT block downloads.
# When a new ChEMBL release ships, just append its version string here.
#
# audit-2025 ROOT FIX (issue 33): the previous set included "36",
# "37", "38" — speculative versions that do NOT exist yet (as of
# ChEMBL release 35, 2024-Q2). Listing non-existent versions is a
# hazard: if a caller sets ``CHEMBL_VERSION=36`` (e.g. via a
# misconfigured env var or a typo), the validator would silently
# ACCEPT it instead of warning that the version is unknown. The fix
# removes the speculative versions and keeps only the released ones
# (up to v35). When ChEMBL v36 ships, append "36" to this set and
# add a corresponding entry to ``CHEMBL_VERSION_COUNT_RANGES``.
VALID_CHEMBL_VERSIONS: frozenset[str] = frozenset(
    {"30", "31", "32", "33", "34", "35"}
)


def _validate_chembl_version(version: str) -> str:
    """Validate ChEMBL version string.

    Accepts numeric version strings. Warns on unknown versions.
    Raises ``ValueError`` on clearly invalid values (non-numeric, empty).
    """
    if not version or not version.strip():
        raise ValueError("CHEMBL_VERSION cannot be empty")
    if not version.replace(".", "").isdigit():
        raise ValueError(
            f"CHEMBL_VERSION={version!r} is not a valid version string. "
            f"Expected a numeric version like '35'. "
            f"Valid versions: {sorted(VALID_CHEMBL_VERSIONS)}"
        )
    if version not in VALID_CHEMBL_VERSIONS:
        warnings.warn(
            f"CHEMBL_VERSION={version} is not in the known valid set. "
            f"The ChEMBL API may not support this version. "
            f"Known valid versions: {sorted(VALID_CHEMBL_VERSIONS)}",
            UserWarning,
        )
    return version


CHEMBL_VERSION: str = _validate_chembl_version(
    _getenv("CHEMBL_VERSION", DEFAULT_CHEMBL_VERSION)
)

# ChEMBL API URL — moved from chembl_pipeline.py (INTEROP-2)
CHEMBL_API_URL: str = _getenv(
    "CHEMBL_API_URL", "https://www.ebi.ac.uk/chembl/api/data"
)

# ChEMBL snapshot date for reproducibility (IDMP-2)
CHEMBL_SNAPSHOT_DATE: str = _getenv("CHEMBL_SNAPSHOT_DATE", "")

# Processing limits (CODE-1, CODE-2, CODE-4)
CHEMBL_MAX_ROWS: Optional[int] = _parse_optional_int(
    "CHEMBL_MAX_ROWS", default=None
)
CHEMBL_MAX_ACTIVITIES: Optional[int] = _parse_optional_int(
    "CHEMBL_MAX_ACTIVITIES", default=None
)

# Version-aware count ranges (SCI-2).
#
# ChEMBL clinical phases (max_phase column):
#   0 = preclinical, 1 = Phase I, 2 = Phase II, 3 = Phase III, 4 = Phase 4 (approved).
# Phase 4 means the drug has reached the market in at least one country —
# i.e., GLOBALLY APPROVED (any regulator: FDA, EMA, PMDA, NMPA, etc.),
# NOT FDA-specific. ChEMBL's max_phase is set to 4 if the drug is
# approved by ANY regulatory agency worldwide, not just the FDA.
#
# audit-2025 ROOT FIX (issue 32): the previous rationale strings said
# "FDA-approved" which is INCORRECT — ChEMBL's max_phase=4 means
# globally approved (any regulator). The audit flagged this because
# conflating "globally approved" with "FDA-approved" undercounts: the
# global max_phase=4 set is LARGER than the FDA-only subset (many
# drugs are approved in Europe or Japan but not yet by the FDA). The
# count ranges below are calibrated against the GLOBAL approved set,
# not the FDA-only subset. The fix corrects the rationale strings to
# say "globally approved (any regulator)" so future readers do not
# mistake the ranges for FDA-only counts.
#
# We filter molecules to max_phase=4 to get the set of approved drugs.
# The count ranges below are the expected number of molecules returned
# by /molecule.json?max_phase=4 for each ChEMBL version, used for
# data-quality validation (DQ-13).
CHEMBL_VERSION_COUNT_RANGES: dict[str, tuple[int, int, str]] = {
    # version: (min, max, rationale)
    # v9 ROOT FIX (audit F3.9): the DOCX target is "10,000 FDA-approved
    # drugs" — but ChEMBL contains ~2.3M compounds total. The
    # max_phase=4 (globally-approved) subset for v33/v34/v35 is ~3-15K
    # depending on the release, which is consistent with the DOCX
    # target. The previous sanity ranges were correctly calibrated
    # for globally-approved — they are NOT off by 3 orders of
    # magnitude as the audit feared. The audit's confusion arose
    # because it compared the globally-approved count (3-5K) against the
    # total compound count (2.3M). The ranges are kept as-is but
    # the rationale comment is clarified to prevent future
    # misinterpretation. We add v32 for completeness.
    # v41 ROOT FIX (SCIENTIFIC): v32 and v33 ranges were inflated 3x — the
    # ChEMBL REST endpoint /molecule.json?max_phase=4 returns the GLOBALLY
    # APPROVED set, which for v33 is ~3.5K molecules (not 12K). The audit
    # Agent C identified the inflation. v34/v35 ranges were already
    # correctly calibrated against the actual REST endpoint. v32/v33 are
    # lowered to match the same calibration (~2.5K-5K band). The previous
    # (8000, 15000) range would have ACCEPTED truncated downloads (e.g. a
    # 4K-row download when the API silently returned 1/3 of the data would
    # have been flagged as "within range") — a data-integrity bug.
    "32": (2500, 5000, "ChEMBL v32 max_phase=4 (globally approved, any regulator) ~3.5K molecules"),
    "33": (2500, 5000, "ChEMBL v33 max_phase=4 (globally approved, any regulator) ~3.5K molecules"),
    "34": (3500, 6000, "ChEMBL v34 max_phase=4 (globally approved, any regulator) ~4K molecules"),
    "35": (3000, 5000, "ChEMBL v35 max_phase=4 (globally approved, any regulator) ~3.5-4K molecules"),
}


def _get_default_chembl_count_range(version: str) -> tuple[int, int]:
    """Get the scientifically validated count range for a ChEMBL version."""
    if version in CHEMBL_VERSION_COUNT_RANGES:
        info = CHEMBL_VERSION_COUNT_RANGES[version]
        return info[0], info[1]
    warnings.warn(
        f"CHEMBL_VERSION={version} has no validated count range. "
        f"Count validation will be disabled (min=0, max=999999). "
        f"Run a test download to determine the correct range.",
        UserWarning,
    )
    return 0, 999999


_chembl_range = _get_default_chembl_count_range(CHEMBL_VERSION)
CHEMBL_EXPECTED_DRUG_COUNT_MIN: int = _parse_required_int(
    "CHEMBL_DRUG_COUNT_MIN", str(_chembl_range[0])
)
CHEMBL_EXPECTED_DRUG_COUNT_MAX: int = _parse_required_int(
    "CHEMBL_DRUG_COUNT_MAX", str(_chembl_range[1])
)

# Warn about unlimited processing in non-dev (DATA-4)
if CHEMBL_MAX_ROWS is None and ENVIRONMENT != "development":
    warnings.warn(
        "CHEMBL_MAX_ROWS is not set. The pipeline will process ALL ChEMBL "
        "molecules, which may take several hours and consume significant "
        "memory. Set CHEMBL_MAX_ROWS to cap the number of rows, or set "
        "ENVIRONMENT=development to suppress this warning.",
        UserWarning,
    )

# ---------------------------------------------------------------------------
# ChEMBL pipeline operational settings — added for institutional-grade
# chembl_pipeline.py rewrite (CFG-1 to CFG-15). All values are env-var
# overridable for dev / staging / prod parity (Domain 12).
# ---------------------------------------------------------------------------

# API pagination size (max 1000 per ChEMBL REST API contract; INT-2).
CHEMBL_PAGE_SIZE: int = _getenv_int("CHEMBL_PAGE_SIZE", 1000)

# HTTP retry behavior (R1-R3, C3-C5, C34, C36, C37).
CHEMBL_MAX_RETRIES: int = _getenv_int("CHEMBL_MAX_RETRIES", 5)
if CHEMBL_MAX_RETRIES < 1:
    raise ValueError(
        f"env var 'CHEMBL_MAX_RETRIES' must be >= 1, got {CHEMBL_MAX_RETRIES}"
    )
CHEMBL_RETRY_BACKOFF_BASE: float = _getenv_float("CHEMBL_RETRY_BACKOFF_BASE", 2.0)
if CHEMBL_RETRY_BACKOFF_BASE < 1.0:
    raise ValueError(
        f"env var 'CHEMBL_RETRY_BACKOFF_BASE' must be >= 1.0, "
        f"got {CHEMBL_RETRY_BACKOFF_BASE}"
    )

# Proactive rate limit (P1). ChEMBL's soft limit is ~2 req/sec for short
# bursts; 0.5s average keeps us safely under the threshold while allowing
# the token-bucket to absorb bursts (P4).
CHEMBL_MIN_REQUEST_INTERVAL: float = _getenv_float(
    "CHEMBL_MIN_REQUEST_INTERVAL", 0.5
)
if CHEMBL_MIN_REQUEST_INTERVAL < 0.0:
    raise ValueError(
        f"env var 'CHEMBL_MIN_REQUEST_INTERVAL' must be >= 0.0, "
        f"got {CHEMBL_MIN_REQUEST_INTERVAL}"
    )

# HTTP timeout tuple (connect, read) in seconds (SEC-2, C37).
CHEMBL_HTTP_TIMEOUT: tuple[float, float] = (
    _getenv_float("CHEMBL_HTTP_TIMEOUT_CONNECT", 10.0),
    _getenv_float("CHEMBL_HTTP_TIMEOUT_READ", 60.0),
)

# Maximum acceptable HTTP response body size in bytes (SEC-5). Default 50 MB
# — a single ChEMBL page is ~1-3 MB so this is generous but bounded.
CHEMBL_MAX_RESPONSE_BYTES: int = _getenv_int("CHEMBL_MAX_RESPONSE_BYTES", 50 * 1024 * 1024)
if CHEMBL_MAX_RESPONSE_BYTES < 1024:
    raise ValueError(
        f"env var 'CHEMBL_MAX_RESPONSE_BYTES' must be >= 1024, "
        f"got {CHEMBL_MAX_RESPONSE_BYTES}"
    )

# Circuit breaker (R10). After CHEMBL_CIRCUIT_BREAKER_THRESHOLD consecutive
# failures, the HTTP client goes into "open" state and fails fast for
# CHEMBL_CIRCUIT_BREAKER_RESET_SECONDS before retrying.
CHEMBL_CIRCUIT_BREAKER_THRESHOLD: int = _getenv_int(
    "CHEMBL_CIRCUIT_BREAKER_THRESHOLD", 10
)
CHEMBL_CIRCUIT_BREAKER_RESET_SECONDS: float = _getenv_float(
    "CHEMBL_CIRCUIT_BREAKER_RESET_SECONDS", 60.0
)

# Scientific filters (S10-S12, S15). These define what we keep when
# downloading / cleaning activities. Defaults are conservative — only
# well-measured human-protein binding/functional assays with exact ('=')
# activity relations.
CHEMBL_TARGET_ORGANISM: str = _getenv("CHEMBL_TARGET_ORGANISM", "Homo sapiens")
CHEMBL_MAX_PHASE: int = _getenv_int("CHEMBL_MAX_PHASE", 4)
if not (0 <= CHEMBL_MAX_PHASE <= 4):
    raise ValueError(
        f"env var 'CHEMBL_MAX_PHASE' must be in [0, 4], got {CHEMBL_MAX_PHASE}"
    )

# Lipinski's extended rule-of-5 threshold for macromolecule flagging (S8).
# Used ONLY to set the transient `is_macromolecule` boolean; never to
# overwrite `drug_type` (K6 fix).
# Reference: Lipinski CA et al., Adv Drug Deliv Rev 2001.
CHEMBL_MW_MACROMOLECULE_THRESHOLD: float = _getenv_float(
    "CHEMBL_MW_MACROMOLECULE_THRESHOLD", 900.0
)
if CHEMBL_MW_MACROMOLECULE_THRESHOLD <= 0.0:
    raise ValueError(
        f"env var 'CHEMBL_MW_MACROMOLECULE_THRESHOLD' must be > 0, "
        f"got {CHEMBL_MW_MACROMOLECULE_THRESHOLD}"
    )

# Activity types and units we know how to normalize (D2-5, DQ-15).
# These mirror the normalizer's supported set so we never silently drop
# activities the normalizer could have handled.
CHEMBL_ACTIVITY_TYPES: frozenset[str] = frozenset(
    s.strip()
    for s in _getenv("CHEMBL_ACTIVITY_TYPES", "IC50,Ki,Kd,EC50").split(",")
    if s.strip()
)
CHEMBL_STANDARD_UNITS: frozenset[str] = frozenset(
    s.strip()
    for s in _getenv(
        "CHEMBL_STANDARD_UNITS",
        "nM,uM,\u00b5M,\u03bcM,pM,mM,M,mol/L",
    ).split(",")
    if s.strip()
)

# Censorship and assay filters (S10, S12).
# standard_relation '=' means an exact measurement; '>' / '<' / '~' are
# censored values and are NOT directly comparable to '=' values.
CHEMBL_STANDARD_RELATIONS: frozenset[str] = frozenset(
    s.strip()
    for s in _getenv("CHEMBL_STANDARD_RELATIONS", "=").split(",")
    if s.strip()
)
# assay_type: B = binding, F = functional, U = unknown, A = ADME,
# P = physicochemical, T = toxicity. We keep B and F by default.
CHEMBL_ASSAY_TYPES: frozenset[str] = frozenset(
    s.strip().upper()
    for s in _getenv("CHEMBL_ASSAY_TYPES", "B,F").split(",")
    if s.strip()
)
# target_type: SINGLE PROTEIN, PROTEIN COMPLEX, ORGANISM, CELL-LINE, etc.
# We keep SINGLE PROTEIN and PROTEIN COMPLEX — both have meaningful
# target_components UniProt accessions (S11).
CHEMBL_TARGET_TYPES: frozenset[str] = frozenset(
    s.strip()
    for s in _getenv(
        "CHEMBL_TARGET_TYPES", "SINGLE PROTEIN,PROTEIN COMPLEX"
    ).split(",")
    if s.strip()
)

# How to handle targets with multiple UniProt accessions (S9, D2-10).
# FIRST: take first accession only (legacy behavior; lossy for complexes).
# ALL:   keep all accessions; explode one activity into N DPI rows.
# BY_COMPONENT_TYPE: keep only PROTEIN-type components.
CHEMBL_TARGET_ACCESSION_STRATEGY: str = _getenv(
    "CHEMBL_TARGET_ACCESSION_STRATEGY", "ALL"
).upper()
if CHEMBL_TARGET_ACCESSION_STRATEGY not in {"FIRST", "ALL", "BY_COMPONENT_TYPE"}:
    raise ValueError(
        f"env var 'CHEMBL_TARGET_ACCESSION_STRATEGY' must be one of "
        f"FIRST, ALL, BY_COMPONENT_TYPE; got "
        f"{CHEMBL_TARGET_ACCESSION_STRATEGY!r}"
    )

# Batching and streaming (P2, P9, P11, P13, C22).
CHEMBL_ACTIVITY_CHUNK_SIZE: int = _getenv_int("CHEMBL_ACTIVITY_CHUNK_SIZE", 100_000)
if CHEMBL_ACTIVITY_CHUNK_SIZE < 1000:
    raise ValueError(
        f"env var 'CHEMBL_ACTIVITY_CHUNK_SIZE' must be >= 1000, "
        f"got {CHEMBL_ACTIVITY_CHUNK_SIZE}"
    )
CHEMBL_DPI_BATCH_SIZE: int = _getenv_int("CHEMBL_DPI_BATCH_SIZE", 1000)
if CHEMBL_DPI_BATCH_SIZE < 1:
    raise ValueError(
        f"env var 'CHEMBL_DPI_BATCH_SIZE' must be >= 1, got {CHEMBL_DPI_BATCH_SIZE}"
    )
CHEMBL_TARGET_RESOLUTION_BATCH_SIZE: int = _getenv_int(
    "CHEMBL_TARGET_RESOLUTION_BATCH_SIZE", 50
)
if CHEMBL_TARGET_RESOLUTION_BATCH_SIZE < 1:
    raise ValueError(
        f"env var 'CHEMBL_TARGET_RESOLUTION_BATCH_SIZE' must be >= 1, "
        f"got {CHEMBL_TARGET_RESOLUTION_BATCH_SIZE}"
    )

# Parallelism (P12, R14).
CHEMBL_API_WORKERS: int = _getenv_int("CHEMBL_API_WORKERS", 3)
if CHEMBL_API_WORKERS < 1:
    raise ValueError(
        f"env var 'CHEMBL_API_WORKERS' must be >= 1, got {CHEMBL_API_WORKERS}"
    )
CHEMBL_TARGET_RESOLUTION_WORKERS: int = _getenv_int(
    "CHEMBL_TARGET_RESOLUTION_WORKERS", 3
)
if CHEMBL_TARGET_RESOLUTION_WORKERS < 1:
    raise ValueError(
        f"env var 'CHEMBL_TARGET_RESOLUTION_WORKERS' must be >= 1, "
        f"got {CHEMBL_TARGET_RESOLUTION_WORKERS}"
    )

# Caches (P3, LIN-14, LIN-15).
CHEMBL_TARGET_CACHE_TTL_SECONDS: int = _getenv_int(
    "CHEMBL_TARGET_CACHE_TTL_SECONDS", 7 * 24 * 3600
)
CHEMBL_DRUG_ID_CACHE_TTL_SECONDS: int = _getenv_int(
    "CHEMBL_DRUG_ID_CACHE_TTL_SECONDS", 3600
)
CHEMBL_CACHE_TTL_SECONDS: int = _getenv_int("CHEMBL_CACHE_TTL_SECONDS", 86400)

# Idempotency / resume (I1, I2, R6, I10, I11).
CHEMBL_ALLOW_VERSION_MISMATCH: bool = _getenv_bool(
    "CHEMBL_ALLOW_VERSION_MISMATCH", False
)
CHEMBL_RESUME: bool = _getenv_bool("CHEMBL_RESUME", False)

# Pipeline-level settings used by every pipeline module but not previously
# defined here (CFG-11, L9, SEC-3, A4, I2, R6). All have safe defaults.
# Note: PIPELINE_RUN_ID defaults to "" (not None) so it passes settings
# validation; consumers check `if PIPELINE_RUN_ID:` to detect "not set".
PIPELINE_RUN_ID: str = _getenv("PIPELINE_RUN_ID", "")
PIPELINE_USE_CACHE: bool = _getenv_bool("PIPELINE_USE_CACHE", True)
PIPELINE_LOG_FORMAT: str = _getenv("PIPELINE_LOG_FORMAT", "text").lower()
if PIPELINE_LOG_FORMAT not in {"text", "json"}:
    raise ValueError(
        f"env var 'PIPELINE_LOG_FORMAT' must be 'text' or 'json', "
        f"got {PIPELINE_LOG_FORMAT!r}"
    )
PIPELINE_CONTACT_EMAIL: str = _getenv(
    "PIPELINE_CONTACT_EMAIL", "team-cosmic@example.com"
)
PIPELINE_RESUME: bool = _getenv_bool("PIPELINE_RESUME", False)

# DEPRECATED: ChEMBL FTP URL — values stored in _DEPRECATED_SETTINGS (DESIGN-1)
# Accessing these triggers DeprecationWarning via module __getattr__

# ---------------------------------------------------------------------------
# UniProt — SCI-5, IDMP-1
# ---------------------------------------------------------------------------

# UniProt release for reproducibility. Defaults to 'current_release'
# which is non-reproducible; warn in production.
UNIPROT_RELEASE: str = _getenv("UNIPROT_RELEASE", "current_release")

if UNIPROT_RELEASE == "current_release" and ENVIRONMENT == "production":
    warnings.warn(
        "UNIPROT_RELEASE is set to 'current_release' in production. "
        "This makes pipeline runs non-reproducible. Pin a specific release "
        "(e.g., 'releases/2024_03') for reproducibility.",
        UserWarning,
    )

# DEPRECATED: UniProt FTP URLs (DESIGN-1)
# Accessing these triggers DeprecationWarning via module __getattr__

# ---------------------------------------------------------------------------
# STRING — SCI-1, DESIGN-2
# ---------------------------------------------------------------------------

DEFAULT_STRING_VERSION: str = "12.0"  # CONF-1

# Known valid STRING database versions
VALID_STRING_VERSIONS: frozenset[str] = frozenset(
    {"11.0", "11.0b", "11.5", "12.0"}
)

STRING_VERSION: str = _getenv("STRING_VERSION", DEFAULT_STRING_VERSION)

# Version-aware score thresholds (SCI-1)
# FIX TOP-1: STRING combined_score >= 700 is the canonical high-confidence
# cutoff (Szklarczyk et al. 2023, Nucleic Acids Research — >= 700 achieves
# >80% precision on KEGG pathway benchmarks; >= 400 achieves only ~50%).
# The previous v12.0 entry used 400 — this dropped ~75% of the high-
# confidence PPIs that Phase 1 retained, causing Phase 2 to silently lose
# most of its protein-protein interaction graph. All STRING versions now
# use 700 as the default threshold. Operators can still override via the
# STRING_MIN_COMBINED_SCORE env var. Synchronized with
# phase2/drugos_graph/config.py — DO NOT diverge (audit TOP-1).
STRING_VERSION_SCORE_THRESHOLDS: dict[str, tuple[int, str]] = {
    # version: (default_threshold, scientific_rationale)
    "11.0b": (700, "v11.0b — 700 is the canonical high-confidence cutoff (Szklarczyk 2023)"),
    "11.5": (700, "v11.5 — 700 is the canonical high-confidence cutoff (Szklarczyk 2023)"),
    "12.0": (700, "v12.0 — 700 is the canonical high-confidence cutoff (Szklarczyk 2023); "
                  "previously 400 which retained only ~50% precision PPIs"),
}


def _get_default_string_threshold(version: str) -> int:
    """Get the scientifically validated default threshold for a STRING version."""
    if version in STRING_VERSION_SCORE_THRESHOLDS:
        return STRING_VERSION_SCORE_THRESHOLDS[version][0]
    # For unknown versions, use the most recent known threshold and warn
    latest = max(STRING_VERSION_SCORE_THRESHOLDS.keys())
    fallback = STRING_VERSION_SCORE_THRESHOLDS[latest][0]
    warnings.warn(
        f"STRING_VERSION={version} has no validated score threshold. "
        f"Using fallback threshold {fallback} from v{latest}. "
        f"Validate this threshold against the {version} score distribution "
        f"before using in production.",
        UserWarning,
    )
    return fallback


def _build_string_urls(version: str) -> dict[str, str]:
    """Build and validate STRING DB URLs for the given version.

    Warns if the version is not in the known valid set.
    Returns a dict with keys: protein_links_url, protein_info_url,
    aliases_url, protein_links_detailed_url.
    """
    if version not in VALID_STRING_VERSIONS:
        warnings.warn(
            f"STRING_VERSION={version} is not in the known valid set "
            f"{sorted(VALID_STRING_VERSIONS)}. The URLs may not resolve.",
            UserWarning,
        )
    base = "https://stringdb-downloads.org/download"
    return {
        "protein_links_url": (
            f"{base}/protein.links.v{version}/"
            f"9606.protein.links.v{version}.txt.gz"
        ),
        "protein_info_url": (
            f"{base}/protein.info.v{version}/"
            f"9606.protein.info.v{version}.txt.gz"
        ),
        "aliases_url": (
            f"{base}/protein.aliases.v{version}/"
            f"9606.protein.aliases.v{version}.txt.gz"
        ),
        "protein_links_detailed_url": (
            f"{base}/protein.links.detailed.v{version}/"
            f"9606.protein.links.detailed.v{version}.txt.gz"
        ),
    }


_string_urls = _build_string_urls(STRING_VERSION)

# CODE-5: Fixed env var name to match setting name, with backward compat
STRING_MIN_COMBINED_SCORE: int = _parse_required_int(
    "STRING_MIN_COMBINED_SCORE",
    str(_get_default_string_threshold(STRING_VERSION)),
)

# Backward compatibility: support the old STRING_MIN_SCORE env var name.
# v41 ROOT FIX (SEV2): route through _getenv so .env is loaded first and the
# canonical read path is used. Direct os.getenv bypasses _ensure_dotenv_loaded
# and could read a stale environ in containers that mount .env at runtime.
_legacy_string_score = _getenv("STRING_MIN_SCORE", None)
if _legacy_string_score is not None and _legacy_string_score.strip() != "":
    warnings.warn(
        "Env var STRING_MIN_SCORE is deprecated. "
        "Use STRING_MIN_COMBINED_SCORE instead.",
        DeprecationWarning,
    )
    try:
        STRING_MIN_COMBINED_SCORE = int(_legacy_string_score)
    except ValueError as _exc:
        raise ValueError(
            f"STRING_MIN_SCORE={_legacy_string_score!r} is not a valid int"
        ) from _exc

STRING_PROTEIN_LINKS_URL: str = _string_urls["protein_links_url"]
STRING_ALIASES_URL: str = _string_urls["aliases_url"]
STRING_PROTEIN_LINKS_DETAILED_URL: str = _string_urls["protein_links_detailed_url"]

# ---------------------------------------------------------------------------
# STRING production-override + reliability/reproducibility knobs (BUG-3.4,
# GAP-12.5, GAP-12.6, GAP-12.7, GAP-12.9, GAP-8.1, GAP-8.2).
#
# These are ADDITIVE — no existing setting is removed.  They are consumed by
# the institutional-grade pipelines/string_pipeline.py (v2.0.0).
# ---------------------------------------------------------------------------

# Sci: Szklarczyk et al. 2023 (Nucleic Acids Research) — combined_score
# >= 700 achieves >80% precision on KEGG pathway benchmarks; >= 400 (the
# dev default) achieves only ~50%.  For a clinical-decision-support system,
# 700 is the minimum defensible threshold.
STRING_MIN_COMBINED_SCORE_PROD: int = _getenv_int(
    "STRING_MIN_COMBINED_SCORE_PROD", default=700
)
"""Production override for STRING_MIN_COMBINED_SCORE.

Per Szklarczyk et al. 2023, combined_score >= 700 achieves >80% precision
on KEGG pathway benchmarks; >= 400 (the dev default) achieves only ~50%.
For a clinical-decision-support system, 700 is the minimum defensible
threshold.  In production (ENV=prod), the STRING pipeline forces the
effective threshold to this value if STRING_MIN_COMBINED_SCORE is below it.
"""

# GAP-7.4: Detailed-file requirement.
#   - "optional" (default): attempt download, warn on failure
#   - "required":            download without try/except (failure raises)
#   - "skip":                do not attempt download at all
STRING_DETAILED_MODE: str = _getenv("STRING_DETAILED_MODE", "optional").lower()
if STRING_DETAILED_MODE not in {"optional", "required", "skip"}:
    warnings.warn(
        f"STRING_DETAILED_MODE={STRING_DETAILED_MODE!r} is not one of "
        f"optional/required/skip — falling back to 'optional'.",
        UserWarning,
    )
    STRING_DETAILED_MODE = "optional"
"""How the STRING pipeline handles the detailed-links file.

- ``optional`` (default) — attempt download; on failure, log a WARNING
  and continue without sub-scores.  Reproducible only if the download
  consistently succeeds.
- ``required`` — download without try/except.  Failure raises.  Use this
  for production runs where sub-score coverage MUST be reproducible.
- ``skip`` — do not attempt download at all.  Sub-scores will be NULL.
"""

# GAP-12.6: Self-interaction (homodimer) handling.
# Sci: Homodimers are biologically real and clinically critical —
# receptor dimerization (EGFR, HER2, VEGFR) is the primary mechanism of
# action for trastuzumab, lapatinib, cetuximab, pertuzumab. p53
# tetramerization is fundamental to tumor-suppressor function.  The DB
# schema's chk_ppi_ordered constraint currently forbids a_id == b_id.
# TODO(schema-migration): relax the constraint and load homodimers with
# an is_homodimer flag.  Until then, drop them with WARNING + dead-letter.
STRING_DROP_SELF_INTERACTIONS: bool = _getenv_bool(
    "STRING_DROP_SELF_INTERACTIONS", default=True
)
"""If True (default), drop self-interactions (homodimers) to satisfy the
chk_ppi_ordered DB constraint. If False, fail loudly (do NOT silently
load — the DB constraint will reject them). TODO(schema-migration): When
the constraint is relaxed, set this to False and load homodimers with an
is_homodimer flag."""

# GAP-3.11 / GAP-12.7: Dedup strategy for collapsing multiple STRING
# ENSP pairs that map to the same UniProt pair.
#   - "max_score"  (default, recommended): keep the row with the highest
#                   combined_score (strongest evidence — Szklarczyk et al. 2023)
#   - "mean_score": aggregate by mean (dilutes strong evidence with weak)
#   - "first":      legacy non-deterministic (sorted first for determinism)
STRING_DEDUP_STRATEGY: str = _getenv(
    "STRING_DEDUP_STRATEGY", "max_score"
).lower()
if STRING_DEDUP_STRATEGY not in {"max_score", "mean_score", "first"}:
    warnings.warn(
        f"STRING_DEDUP_STRATEGY={STRING_DEDUP_STRATEGY!r} is not one of "
        f"max_score/mean_score/first — falling back to 'max_score'.",
        UserWarning,
    )
    STRING_DEDUP_STRATEGY = "max_score"
"""Dedup strategy for collapsing multiple STRING ENSP pairs that map to
the same UniProt pair (e.g. isoforms of the same protein).

- ``max_score`` (default, recommended) — keep the row with the highest
  combined_score.  Deterministic and reflects the strongest evidence
  (Szklarczyk et al. 2023).
- ``mean_score`` — aggregate by mean.  Dilutes strong evidence with weak.
- ``first`` — legacy; deterministic because we sort first, but loses
  information.
"""

# GAP-12.4 / BUG-8.1: low_memory flag for pd.read_csv.
STRING_LOW_MEMORY: bool = _getenv_bool("STRING_LOW_MEMORY", default=False)
"""If True, pass low_memory=True to pd.read_csv for STRING files (slower
but lower peak memory).  Default False (full materialization for speed
on machines with >= 8 GB RAM).  STRING v12.0 links file is ~1.5 GB in
memory."""

# BUG-8.1 / GAP-8.9: Chunk size for chunked reading (0 = disabled).
STRING_CHUNK_SIZE: int = _getenv_int("STRING_CHUNK_SIZE", default=0)
"""Chunk size (rows) for chunked reading of STRING files. 0 = disabled
(load entire file in memory).  For machines with < 8 GB RAM, set to
1_000_000 to bound peak memory.  When > 0, the links file is read in
chunks and only rows passing the score filter are concatenated."""

# ---------------------------------------------------------------------------
# Controlled vocabulary for the `source` column across all pipelines
# (GAP-2.7, GAP-14.2).  Implemented as a str-Enum for ergonomic use.
# ---------------------------------------------------------------------------
try:
    from enum import Enum

    class DataSourceName(str, Enum):
        """Controlled vocabulary for the ``source`` column across all pipelines."""

        STRING = "string"
        CHEMBL = "chembl"
        DRUGBANK = "drugbank"
        UNIPROT = "uniprot"
        DISGENET = "disgenet"
        OMIM = "omim"
        PUBCHEM = "pubchem"

except ImportError:  # pragma: no cover — enum is stdlib, this never fires.
    DataSourceName = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# audit-2025 ROOT FIX: VALID_SOURCE_NAMES is the single source of truth for
# the set of pipeline source names recognised across the platform. It is
# derived from the ``DataSourceName`` enum above so adding a new pipeline
# only requires editing the enum — every consumer (``base_pipeline.py``,
# ``database/loaders.py``, the DAGs, etc.) picks up the new value
# automatically. Previously this set was duplicated in three places:
#   1. config/settings.py — DataSourceName enum (here)
#   2. pipelines/base_pipeline.py — VALID_SOURCE_NAMES frozenset literal
#   3. database/loaders.py — local ``valid_sources`` set inside
#      ``get_or_create_pipeline_run``
# Adding a new source to one and forgetting the others produced silent
# inconsistencies (e.g. a new source accepted by the ORM but rejected by
# the loader's validation gate, or vice versa).
# ---------------------------------------------------------------------------
if DataSourceName is not None:
    VALID_SOURCE_NAMES: frozenset[str] = frozenset(
        member.value for member in DataSourceName
    )
else:  # pragma: no cover — defensive fallback if enum import failed
    VALID_SOURCE_NAMES = frozenset({
        "chembl", "drugbank", "uniprot", "string",
        "disgenet", "omim", "pubchem",
    })

# DEPRECATED: STRING protein info URL (DESIGN-1)
# FIX M1: STRING_PROTEIN_INFO_URL is kept for reference but unused in
# the pipeline's download() method. clean() never reads this file.
# Accessing this triggers DeprecationWarning via module __getattr__

# ---------------------------------------------------------------------------
# DisGeNET — SCI-3, SEC-2, DESIGN-4, CONF-4
# ---------------------------------------------------------------------------

# SCI-3: DisGeNET migrated to api.disgenet.com in 2024.
# The old www.disgenet.org/api/ endpoint is deprecated and may not work.
# The new API v1 base is https://api.disgenet.com/api/v1/
DISGENET_API_URL: str = _getenv(
    "DISGENET_API_URL",
    "https://api.disgenet.com/api/v1/gda/summary",
)
DISGENET_API_KEY: str = _getenv("DISGENET_API_KEY", "")
DISGENET_USE_API: bool = _parse_bool(_getenv("DISGENET_USE_API", "true"))

# The primary URL now points to the API by default (SCI-3)
DISGENET_URL: str = DISGENET_API_URL

# DEPRECATED: Static URL (broken since 2024)
# Accessing this triggers DeprecationWarning via module __getattr__

# Warn if someone explicitly opts out of the API
if not DISGENET_USE_API:
    warnings.warn(
        "DISGENET_USE_API=false is set, but the DisGeNET static URL is "
        "deprecated since 2024 and may not work. Set DISGENET_USE_API=true "
        "and provide DISGENET_API_KEY for reliable data access.",
        UserWarning,
    )

# ===========================================================================
# DisGeNET institutional-grade configuration knobs (389-fix audit).
#
# These are ADDITIVE — no existing setting is removed.  They are consumed by
# the institutional-grade ``pipelines/disgenet_pipeline.py`` (v2.0.0).
#
# All scientific thresholds cite Piñero et al., 2020, *DisGeNET: a
# comprehensive platform integrating information on human disease-associated
# genes and variants*, Nucleic Acids Research
# (https://doi.org/10.1093/nar/gkz1021).
# ===========================================================================

# SCI-1 / DES-1 / CONF-1: Minimum score threshold.
# Per Piñero et al. 2020, DisGeNET scores in [0.06, 0.1) constitute "weak
# evidence" — biologically meaningful, especially for rare diseases.  The
# previous default of 0.1 silently destroyed this evidence.  The new default
# is 0.06, the published weak-evidence floor.  Set DISGENET_ALLOW_WEAK_EVIDENCE
# to False to hard-filter at this threshold; otherwise weak-evidence rows are
# kept and tagged with confidence_tier="weak".
DISGENET_MIN_SCORE: float = _getenv_float("DISGENET_MIN_SCORE", default=0.06)
"""Minimum DisGeNET score for inclusion.

Defaults to 0.06 — the floor of DisGeNET's 'weak evidence' band per
Piñero et al., 2020 (Nucleic Acids Research).  Set to 0.0 to disable
filtering entirely (preserve all evidence, including sub-weak).  Pair
with DISGENET_ALLOW_WEAK_EVIDENCE=false to hard-filter at this threshold;
otherwise weak-evidence rows are kept and tagged with confidence_tier='weak'.

Unit: float in [0, 1].
"""

# v41 ROOT FIX (SCIENTIFIC): DISGENET_MIN_SCORE=0.06 retains sub-weak evidence
# by design (rare-disease signal). Downstream consumers (knowledge-graph
# construction, ML negative sampling) need a SEPARATE strong-evidence floor
# for high-precision sub-graphs. Per Piñero et al. 2020 §2.3, scores >= 0.3
# are the "strong evidence" band (>=1 publication from curated sources OR
# >=3 publications from non-curated). We export this as a separate setting
# so the cleaning / bridge code can build a strong-evidence subset WITHOUT
# raising the inclusion floor (which would silently drop rare-disease GDAs).
DISGENET_STRONG_SCORE: float = _getenv_float("DISGENET_STRONG_SCORE", default=0.3)
"""Strong-evidence threshold for DisGeNET scores (downstream filtering).

Defaults to 0.3 — the floor of DisGeNET's 'strong evidence' band per
Piñero et al., 2020. Used by downstream consumers (KG construction,
ML negative sampling) to build high-precision sub-graphs without raising
the inclusion floor (DISGENET_MIN_SCORE), preserving rare-disease signal.

Unit: float in [0, 1]; must be >= DISGENET_MIN_SCORE.
"""

DISGENET_ALLOW_WEAK_EVIDENCE: bool = _getenv_bool(
    "DISGENET_ALLOW_WEAK_EVIDENCE", default=True
)
"""If True (default), do NOT filter out weak-evidence rows (score in
[DISGENET_MIN_SCORE, 0.1)).  Instead, tag them with confidence_tier="weak".
If False, hard-filter at DISGENET_MIN_SCORE (drops weak-evidence rows)."""

# SCI-11 / DES-2 / CONF-2: Confidence tier thresholds.
# Per Piñero et al. 2020 §2.3, the DSGP score bands are:
#   [0.0, 0.06)   — sub-weak (below published floor)
#   [0.06, 0.3)   — weak evidence
#   [0.3, 1.0]    — strong evidence
# These thresholds are configurable via DISGENET_CONFIDENCE_TIERS (JSON-encoded
# list of [threshold, label] pairs).  The previous 0.7 → "very_high" tier is
# removed (no publication supports it).
DISGENET_CONFIDENCE_TIERS_JSON: str = _getenv(
    "DISGENET_CONFIDENCE_TIERS",
    default='[[0.0,"weak"],[0.06,"moderate"],[0.3,"strong"]]',
)
"""JSON-encoded list of [threshold, label] pairs for confidence tier
classification.  Default follows Piñero et al. 2020:
``[[0.0,"weak"],[0.06,"moderate"],[0.3,"strong"]]``.  Thresholds must be
sorted ascending; labels must be non-empty strings."""

# SCI-17 / CONF-3: PMID cap.
# The GeneDiseaseAssociation.pmid_list column is String(2000).  Each PMID is
# 7-8 digits + 1 separator.  Cap is computed dynamically:
#   DISGENET_PMID_CAP = min(200, (PMID_LIST_LENGTH - 1) // 10)
# but the user-set value takes precedence (validated against PMID_LIST_LENGTH).
DISGENET_PMID_CAP: int = _getenv_int("DISGENET_PMID_CAP", default=200)
"""Maximum number of PMIDs retained per record after capping.  Default 200
(utilises the full String(2000) capacity of the pmid_list column).  If the
resulting max string length exceeds PMID_LIST_LENGTH, the pipeline raises
ValueError at init — see DISGENET_PMID_SORT_ORDER for sort semantics."""

DISGENET_PMID_SORT_ORDER: str = _getenv(
    "DISGENET_PMID_SORT_ORDER", "recent_first"
).lower()
if DISGENET_PMID_SORT_ORDER not in {"recent_first", "chronological", "as_returned"}:
    warnings.warn(
        f"DISGENET_PMID_SORT_ORDER={DISGENET_PMID_SORT_ORDER!r} is not one of "
        f"recent_first/chronological/as_returned — falling back to 'recent_first'.",
        UserWarning,
    )
    DISGENET_PMID_SORT_ORDER = "recent_first"
"""Sort order for PMIDs before capping (SCI-16).
- ``recent_first`` (default) — descending PMID (NCBI assigns PMIDs
  monotonically; higher = more recent).  Retains the most evidentially
  important PMIDs.
- ``chronological`` — ascending PMID.
- ``as_returned`` — no sort (legacy behaviour, not recommended)."""

# PERF-15 / CONF-4: API page size.
DISGENET_API_PAGE_SIZE: int = _getenv_int("DISGENET_API_PAGE_SIZE", default=5000)
"""Number of records to fetch per API request.  Default 5000.  The pipeline
validates this against the API's max on first request; if rejected, logs a
WARNING and falls back to 5000.  Higher values reduce request count but
increase per-request memory."""

# CONF-5: Safety cap on total records (prevents infinite pagination).
DISGENET_API_MAX_RECORDS: int = _getenv_int(
    "DISGENET_API_MAX_RECORDS", default=1_000_000
)
"""Hard safety cap on total records fetched (anti-infinite-loop).  Default
1,000,000 (DisGeNET has ~1M GDAs; this is a safety valve, not a normal
termination)."""

# CONF-6 / PERF-16 / REL-13: API timeout.
DISGENET_API_TIMEOUT: int = _getenv_int("DISGENET_API_TIMEOUT", default=30)
"""HTTP timeout (seconds) for a single API request.  Default 30 — DisGeNET
pages typically respond in <5s, 30 is generous.  Lowered from the previous
hardcoded 120s."""

# CONF-7: Max retries.
DISGENET_API_MAX_RETRIES: int = _getenv_int("DISGENET_API_MAX_RETRIES", default=5)
"""Maximum number of retries per API request.  Default 5."""

# CONF-8 / PERF-9: Exponential backoff.
DISGENET_API_BACKOFF_BASE: float = _getenv_float(
    "DISGENET_API_BACKOFF_BASE", default=2.0
)
"""Base for exponential backoff: ``wait = min(base ** attempt, MAX_SECONDS)``.
Default 2.0."""

DISGENET_API_BACKOFF_MAX_SECONDS: int = _getenv_int(
    "DISGENET_API_BACKOFF_MAX_SECONDS", default=60
)
"""Maximum sleep per retry (caps the exponential).  Default 60s."""

DISGENET_API_MAX_RETRY_AFTER: int = _getenv_int(
    "DISGENET_API_MAX_RETRY_AFTER", default=300
)
"""Maximum sleep when honouring a 429 Retry-After header.  Default 300s
(5 minutes)."""

# SEC-20: Client-side rate limiting.
DISGENET_API_RATE_LIMIT: float = _getenv_float(
    "DISGENET_API_RATE_LIMIT", default=2.0
)
"""Maximum API requests per second (token-bucket).  Default 2.0 (DisGeNET
free tier)."""

# REL-8: Circuit breaker.
DISGENET_CIRCUIT_BREAKER_THRESHOLD: int = _getenv_int(
    "DISGENET_CIRCUIT_BREAKER_THRESHOLD", default=5
)
"""Consecutive API failures before the circuit opens.  Default 5."""

DISGENET_CIRCUIT_BREAKER_RESET_SECONDS: int = _getenv_int(
    "DISGENET_CIRCUIT_BREAKER_RESET_SECONDS", default=300
)
"""Seconds the circuit stays open before entering half-open.  Default 300."""

# SEC-16: User-Agent identification.
DISGENET_CONTACT_EMAIL: str = _getenv(
    "DISGENET_CONTACT_EMAIL", default="unknown@example.com"
)
"""Contact email for the User-Agent header (per DisGeNET API terms of use).
Replace with your team's contact in production."""

# SEC-7: SSRF protection.
DISGENET_ALLOWED_DOMAINS: list[str] = [
    d.strip() for d in _getenv(
        "DISGENET_ALLOWED_DOMAINS",
        default="api.disgenet.com,www.disgenet.org,disgenet.org",
    ).split(",") if d.strip()
]
"""Comma-separated list of allowed DisGeNET API domains.  Default
``api.disgenet.com,www.disgenet.org,disgenet.org``.  The primary domain
since 2024 is ``api.disgenet.com``.  The pipeline rejects any other domain
(SEC-7 SSRF protection)."""

# SEC-9: Response size validation.
DISGENET_API_MAX_RESPONSE_BYTES: int = _getenv_int(
    "DISGENET_API_MAX_RESPONSE_BYTES", default=100_000_000
)
"""Maximum acceptable response size in bytes.  Default 100 MB.  Larger
responses raise RuntimeError (defends against accidental memory exhaustion
from a malformed endpoint)."""

# SEC-10: TLS CA bundle override.
DISGENET_API_CA_BUNDLE: str = _getenv("DISGENET_API_CA_BUNDLE", default="")
"""Optional path to a CA bundle file.  Empty string = system default.
Set to a specific path to pin to a custom CA (e.g. corporate proxy)."""

# SEC-14: Output file permissions.
DISGENET_OUTPUT_FILE_MODE: str = _getenv("DISGENET_OUTPUT_FILE_MODE", default="0o640")
"""Octal file mode (as a string) for the output CSV.  Default '0o640'
(owner read/write, group read, no others)."""

# REL-6: Fallback to cache.
DISGENET_FALLBACK_TO_CACHE: bool = _getenv_bool(
    "DISGENET_FALLBACK_TO_CACHE", default=True
)
"""If True (default) and the API fails after all retries, fall back to the
most recent cached TSV in raw_dir with a WARNING.  If False, raise."""

# REL-14: Overall pagination caps.
DISGENET_API_MAX_PAGES: int = _getenv_int("DISGENET_API_MAX_PAGES", default=500)
"""Hard cap on the number of API pages fetched.  Default 500 (at 5000
records/page = 2.5M records, well above DisGeNET's ~1M)."""

DISGENET_DOWNLOAD_PHASE_TIMEOUT: int = _getenv_int(
    "DISGENET_DOWNLOAD_PHASE_TIMEOUT", default=3600
)
"""Overall wall-clock timeout (seconds) for the entire download phase.
Default 3600 (1 hour)."""

# SCI-25 / SCI-35 / IDEM-20: Pagination completeness.
DISGENET_ALLOW_PARTIAL_DATA: bool = _getenv_bool(
    "DISGENET_ALLOW_PARTIAL_DATA", default=False
)
"""If True (dev/debug only), do NOT raise on pagination completeness
mismatch — log ERROR and write a partial-data manifest instead.  Default
False (production: raise)."""

# IDEM-7: UniProt map cache.
DISGENET_UNIPROT_MAP_TTL_HOURS: int = _getenv_int(
    "DISGENET_UNIPROT_MAP_TTL_HOURS", default=24
)
"""TTL (hours) for the cached gene_symbol→uniprot_id map.  Default 24h."""

# IDEM-8: DisGeNET version pinning.
DISGENET_TARGET_VERSION: str = _getenv("DISGENET_TARGET_VERSION", default="")
"""Pin to a specific DisGeNET release (e.g. 'v7').  Empty string = latest.
Stored in score_method (e.g. 'disgenet_v7_2024_06') and source_version."""

# IDEM-14: Snapshot isolation.
DISGENET_FREEZE_VERSION: str = _getenv("DISGENET_FREEZE_VERSION", default="")
"""If set, every GDA row gets snapshot_tag=this value (no overwrite of
existing snapshots).  Empty string = live table (overwrite on conflict)."""

# DQ-25: Minimum expected record count.
DISGENET_MIN_EXPECTED_RECORDS: int = _getenv_int(
    "DISGENET_MIN_EXPECTED_RECORDS", default=100_000
)
"""Minimum number of records expected after clean().  Default 100,000
(DisGeNET has ~1M GDAs; 100K is a conservative floor).  The pipeline
raises RuntimeError if fewer records survive."""

# DQ-19 / DQ-20: Optional referential integrity checks.
DISGENET_DISEASE_ONTOLOGY_PATH: str = _getenv(
    "DISGENET_DISEASE_ONTOLOGY_PATH", default=""
)
"""Optional path to a disease ontology file (MeSH/UMLS/DOID).  When set,
the pipeline validates every disease_id against the ontology and
quarantines invalid rows.  Empty = skip the check."""

DISGENET_HGNC_PATH: str = _getenv("DISGENET_HGNC_PATH", default="")
"""Optional path to an HGNC symbol dump.  When set, the pipeline validates
every gene_symbol against it and quarantines unknown symbols.  Empty = skip."""

# DQ-33: Stale data detection.
DISGENET_MAX_DATA_AGE_DAYS: int = _getenv_int(
    "DISGENET_MAX_DATA_AGE_DAYS", default=180
)
"""Maximum acceptable age (days) of the DisGeNET release.  If the release
date is older than this, the manifest's stale_data flag is set to True
(WARNING, not failure — DisGeNET may have legitimate slow release cycles).
Default 180 (6 months)."""

# CONF-10 / CONF-11: Output / raw filenames.
DISGENET_OUTPUT_FILENAME: str = _getenv(
    "DISGENET_OUTPUT_FILENAME", default="gene_disease_associations.csv"
)
"""Output CSV filename in PROCESSED_DATA_DIR.  Default
'gene_disease_associations.csv'.  Changing this breaks downstream
consumers (Neo4j exporter, Graph Transformer) — change only for testing."""

DISGENET_RAW_FILENAME: str = _getenv(
    "DISGENET_RAW_FILENAME", default=""
)
"""Raw filename in raw_dir.  Empty = auto-detect (static=.tsv.gz, API=.tsv)."""

# PERF-3: Optional chunked processing.
DISGENET_CHUNK_SIZE: int = _getenv_int("DISGENET_CHUNK_SIZE", default=0)
"""Chunk size (rows) for chunked processing of the cleaned TSV.  0 = disabled
(load entire TSV in memory).  For machines with < 8 GB RAM, set to 1_000_000
to bound peak memory."""

# PERF-7: Parallel pagination (future).
DISGENET_API_PARALLEL_PAGES: int = _getenv_int(
    "DISGENET_API_PARALLEL_PAGES", default=1
)
"""Number of concurrent API page requests.  Default 1 (sequential —
DisGeNET's rate limit is 2 req/sec, parallelism just hits 429s).  Reserved
for future optimisation."""

# LOG-21: Log format.
DISGENET_LOG_FORMAT: str = _getenv("DISGENET_LOG_FORMAT", default="text").lower()
if DISGENET_LOG_FORMAT not in {"json", "text"}:
    DISGENET_LOG_FORMAT = "text"
"""Log format for the DisGeNET pipeline: 'json' (structured) or 'text'
(human-readable).  Default 'text'."""

# CONF-19: Environment-specific defaults.
DISGENET_ENV: str = _getenv("DISGENET_ENV", default="dev").lower()
if DISGENET_ENV not in {"dev", "staging", "prod"}:
    DISGENET_ENV = "dev"
"""Environment tier: 'dev', 'staging', 'prod'.  In dev: lower MIN_EXPECTED_RECORDS.
In staging: same as prod but ALLOW_PARTIAL_DATA=True.  In prod: strict defaults."""

# Apply env-specific overrides (CONF-19).
if DISGENET_ENV == "dev":
    DISGENET_MIN_EXPECTED_RECORDS = min(
        DISGENET_MIN_EXPECTED_RECORDS,
        _getenv_int("DISGENET_MIN_EXPECTED_RECORDS", default=100),
    )
    DISGENET_API_TIMEOUT = max(
        DISGENET_API_TIMEOUT, _getenv_int("DISGENET_API_TIMEOUT", default=60)
    )
elif DISGENET_ENV == "staging":
    DISGENET_ALLOW_PARTIAL_DATA = True


def _parse_disgenet_confidence_tiers(raw: str) -> list[tuple[float, str]]:
    """Parse DISGENET_CONFIDENCE_TIERS_JSON into a sorted list of (threshold, label) pairs.

    Raises ValueError on malformed JSON, non-list root, or entries that
    are not [number, string] pairs.
    """
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"DISGENET_CONFIDENCE_TIERS is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise ValueError(
            f"DISGENET_CONFIDENCE_TIERS must be a JSON list, got {type(parsed).__name__}"
        )
    tiers: list[tuple[float, str]] = []
    for entry in parsed:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError(
                f"DISGENET_CONFIDENCE_TIERS entry {entry!r} must be a [threshold, label] pair"
            )
        thr, label = entry
        if not isinstance(thr, (int, float)) or isinstance(thr, bool):
            raise ValueError(
                f"DISGENET_CONFIDENCE_TIERS threshold {thr!r} must be a number"
            )
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                f"DISGENET_CONFIDENCE_TIERS label {label!r} must be a non-empty string"
            )
        tiers.append((float(thr), label))
    if not tiers:
        raise ValueError("DISGENET_CONFIDENCE_TIERS must contain at least one tier")
    tiers.sort(key=lambda t: t[0])
    return tiers


DISGENET_CONFIDENCE_TIERS: list[tuple[float, str]] = _parse_disgenet_confidence_tiers(
    DISGENET_CONFIDENCE_TIERS_JSON
)
"""Parsed confidence tiers (list of (threshold, label) pairs, sorted ascending).
Defaults follow Piñero et al. 2020:
``[(0.0, 'weak'), (0.06, 'moderate'), (0.3, 'strong')]``."""


def _parse_disgenet_source_weights(raw: str) -> dict[str, float]:
    """Parse DISGENET_SOURCE_WEIGHTS_JSON into a dict[str, float]."""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"DISGENET_SOURCE_WEIGHTS is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"DISGENET_SOURCE_WEIGHTS must be a JSON object, got {type(parsed).__name__}"
        )
    out: dict[str, float] = {}
    for k, v in parsed.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(
                f"DISGENET_SOURCE_WEIGHTS['{k}']={v!r} must be a number"
            )
        out[str(k)] = float(v)
    return out


# SCI-38: Source quality weights for normalized_score computation.
# These weights reflect the curation level of each DisGeNET sub-source
# (Piñero et al. 2020 §2.3).  They are heuristic, not derived from a
# closed-form formula — they encode the relative credibility of each source.
DISGENET_SOURCE_WEIGHTS_JSON: str = _getenv(
    "DISGENET_SOURCE_WEIGHTS",
    default=json.dumps({
        "CURATED": 1.0,
        "CGI": 0.95,
        "CLINGEN": 0.95,
        "GENOMICS_ENGLAND": 0.95,
        "ORPHANET": 0.9,
        "CTD_human": 0.85,
        "GWAS_CATALOG": 0.85,
        "UNIPROT": 0.8,
        "PSYGENET": 0.75,
        "LHGDN": 0.7,
        "HPO": 0.7,
        "BEFREE": 0.5,
        "RONB": 0.5,
    }),
)
DISGENET_SOURCE_WEIGHTS: dict[str, float] = _parse_disgenet_source_weights(
    DISGENET_SOURCE_WEIGHTS_JSON
)
"""Per-source credibility weights (0.0-1.0) used to compute
``normalized_score = score * weight``.  Defaults follow Piñero et al. 2020
§2.3 (CURATED gold-standard; BEFREE/RONB text-mined, noisy).  Override
with DISGENET_SOURCE_WEIGHTS env var (JSON object)."""


def _validate_disgenet_config() -> None:
    """Validate all DisGeNET config values (CONF-14, CONF-16, CONF-17).

    Raises ValueError on any invalid value.  Called by DisGeNETPipeline
    at init (and may be called manually).
    """
    if not (0.0 <= DISGENET_MIN_SCORE <= 1.0):
        raise ValueError(
            f"DISGENET_MIN_SCORE={DISGENET_MIN_SCORE} must be in [0, 1]"
        )
    if DISGENET_API_PAGE_SIZE <= 0:
        raise ValueError(
            f"DISGENET_API_PAGE_SIZE={DISGENET_API_PAGE_SIZE} must be > 0"
        )
    if DISGENET_API_MAX_RETRIES < 1:
        raise ValueError(
            f"DISGENET_API_MAX_RETRIES={DISGENET_API_MAX_RETRIES} must be >= 1"
        )
    if DISGENET_API_TIMEOUT <= 0:
        raise ValueError(
            f"DISGENET_API_TIMEOUT={DISGENET_API_TIMEOUT} must be > 0"
        )
    if DISGENET_PMID_CAP <= 0:
        raise ValueError(
            f"DISGENET_PMID_CAP={DISGENET_PMID_CAP} must be > 0"
        )
    if DISGENET_API_BACKOFF_BASE <= 1.0:
        raise ValueError(
            f"DISGENET_API_BACKOFF_BASE={DISGENET_API_BACKOFF_BASE} must be > 1.0"
        )
    if DISGENET_API_BACKOFF_MAX_SECONDS <= 0:
        raise ValueError(
            f"DISGENET_API_BACKOFF_MAX_SECONDS={DISGENET_API_BACKOFF_MAX_SECONDS} must be > 0"
        )
    if DISGENET_API_RATE_LIMIT <= 0:
        raise ValueError(
            f"DISGENET_API_RATE_LIMIT={DISGENET_API_RATE_LIMIT} must be > 0"
        )
    if DISGENET_CIRCUIT_BREAKER_THRESHOLD < 1:
        raise ValueError(
            f"DISGENET_CIRCUIT_BREAKER_THRESHOLD={DISGENET_CIRCUIT_BREAKER_THRESHOLD} must be >= 1"
        )
    if DISGENET_CIRCUIT_BREAKER_RESET_SECONDS <= 0:
        raise ValueError(
            f"DISGENET_CIRCUIT_BREAKER_RESET_SECONDS={DISGENET_CIRCUIT_BREAKER_RESET_SECONDS} must be > 0"
        )
    if DISGENET_API_MAX_PAGES <= 0:
        raise ValueError(
            f"DISGENET_API_MAX_PAGES={DISGENET_API_MAX_PAGES} must be > 0"
        )
    if DISGENET_DOWNLOAD_PHASE_TIMEOUT <= 0:
        raise ValueError(
            f"DISGENET_DOWNLOAD_PHASE_TIMEOUT={DISGENET_DOWNLOAD_PHASE_TIMEOUT} must be > 0"
        )
    if DISGENET_API_MAX_RESPONSE_BYTES <= 0:
        raise ValueError(
            f"DISGENET_API_MAX_RESPONSE_BYTES={DISGENET_API_MAX_RESPONSE_BYTES} must be > 0"
        )
    if DISGENET_API_MAX_RECORDS <= 0:
        raise ValueError(
            f"DISGENET_API_MAX_RECORDS={DISGENET_API_MAX_RECORDS} must be > 0"
        )
    if not DISGENET_ALLOWED_DOMAINS:
        raise ValueError(
            "DISGENET_ALLOWED_DOMAINS must contain at least one domain"
        )

    # CONF-16: Validate DISGENET_API_URL.
    from urllib.parse import urlparse
    parsed = urlparse(DISGENET_API_URL)
    if parsed.scheme != "https":
        raise ValueError(
            f"DISGENET_API_URL scheme must be 'https', got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError(f"DISGENET_API_URL has no hostname: {DISGENET_API_URL!r}")
    if (
        parsed.hostname not in DISGENET_ALLOWED_DOMAINS
        and not any(
            parsed.hostname.endswith("." + d) for d in DISGENET_ALLOWED_DOMAINS
        )
    ):
        raise ValueError(
            f"DISGENET_API_URL hostname {parsed.hostname!r} is not in "
            f"DISGENET_ALLOWED_DOMAINS={DISGENET_ALLOWED_DOMAINS}"
        )

    # CONF-17: API key required when USE_API=True.
    if DISGENET_USE_API and not DISGENET_API_KEY:
        raise ValueError(
            "DISGENET_USE_API=true but DISGENET_API_KEY is not set. "
            "Set the DISGENET_API_KEY environment variable or set "
            "DISGENET_USE_API=false (not recommended - static URL is "
            "deprecated since 2024)."
        )

    # CONF-14: Tier thresholds must be strictly monotonic.
    thresholds = [t[0] for t in DISGENET_CONFIDENCE_TIERS]
    for i in range(1, len(thresholds)):
        if thresholds[i] <= thresholds[i - 1]:
            raise ValueError(
                f"DISGENET_CONFIDENCE_TIERS thresholds must be strictly "
                f"monotonic ascending, got {thresholds}"
            )


# Run validation eagerly so misconfiguration fails fast (CONF-14).
# v41 ROOT FIX (SEV3): the comment said "fail fast" but the try/except converted
# every ValueError into a UserWarning — effectively a no-op that swallowed
# real misconfiguration. In production we MUST fail fast; in development we
# keep the soft-warning behaviour so tests can patch env without aborting.
try:
    _validate_disgenet_config()
except ValueError as _disgenet_cfg_err:
    if ENVIRONMENT == "production":
        # Production: a malformed DisGeNET config is a deployment-blocker.
        # Re-raise so the import itself fails and the operator must fix it
        # before the pipeline can run.
        raise ValueError(
            f"DisGeNET config validation FAILED in production: "
            f"{_disgenet_cfg_err}. Refusing to import settings — fix the "
            f"env vars (DISGENET_MIN_SCORE / DISGENET_CONFIDENCE_TIERS) "
            f"and re-deploy."
        ) from _disgenet_cfg_err
    # Dev / staging: warn and let the pipeline re-validate on init.
    warnings.warn(
        f"DisGeNET config validation warning: {_disgenet_cfg_err}",
        UserWarning,
        stacklevel=2,
    )


# ---------------------------------------------------------------------------
# PubChem — INTEROP-2, DESIGN-5
# ---------------------------------------------------------------------------

PUBCHEM_REST_BASE: str = _getenv(
    "PUBCHEM_REST_BASE", "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
)
PUBCHEM_FTP_BASE: str = _getenv(
    "PUBCHEM_FTP_BASE", "https://ftp.ncbi.nlm.nih.gov/pubchem"
)

# Consistent alias (DESIGN-5)
PUBCHEM_API_URL: str = PUBCHEM_REST_BASE

# ---------------------------------------------------------------------------
# Entity Resolution — audit D12-2 (integrate entity_resolution config
# with config/settings.py).  Every field mirrors ResolverConfig in
# entity_resolution/base.py and is env-overridable with the prefix
# ENTITY_RESOLUTION_.  These settings are consumed by
# ResolverConfig.from_env() at construction time.
# ---------------------------------------------------------------------------

# If False (default, safe), the bulk path build_mapping() never calls
# PubChem and resolve_single() skips the PubChem step.  Opt in via
# ENTITY_RESOLUTION_PUBCHEM_ENABLED=1 when single-record PubChem
# lookup is genuinely needed.  Audit D9-1 / D9-2.
ENTITY_RESOLUTION_PUBCHEM_ENABLED: bool = _getenv_bool(
    "ENTITY_RESOLUTION_PUBCHEM_ENABLED", False
)

# If False (default, safe), two InChIKeys sharing the same 14-char
# connectivity block are NOT merged unless their full 27-char forms
# are identical.  This preserves stereoisomer distinctness (audit D3-4
# — thalidomide-enantiomer safety).  Opt in via =1 for legacy
# behaviour.
ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS: bool = _getenv_bool(
    "ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS", False
)

# Minimum rapidfuzz.fuzz.token_sort_ratio score (on [0,1]) at which a
# fuzzy name match is accepted.  Default 0.85.  Audit D3-3.
ENTITY_RESOLUTION_FUZZY_THRESHOLD: float = _getenv_float(
    "ENTITY_RESOLUTION_FUZZY_THRESHOLD", 0.85
)

# Ceiling on the number of indexed names scanned per fuzzy sweep
# (audit D8-2 — bounds worst-case O(n^2)).  Default 10000.
ENTITY_RESOLUTION_FUZZY_MAX_CANDIDATES: int = _getenv_int(
    "ENTITY_RESOLUTION_FUZZY_MAX_CANDIDATES", 10_000
)

# PubChem REST base URL — configurable so air-gapped deployments can
# point at an internal mirror.  Audit D9-3.
ENTITY_RESOLUTION_PUBCHEM_REST_BASE: str = _getenv(
    "ENTITY_RESOLUTION_PUBCHEM_REST_BASE", PUBCHEM_REST_BASE
)

# Minimum seconds between PubChem API calls.  Default 0.2 (5 req/sec).
ENTITY_RESOLUTION_PUBCHEM_CALL_DELAY: float = _getenv_float(
    "ENTITY_RESOLUTION_PUBCHEM_CALL_DELAY", 0.2
)

# Per-request timeout in seconds.  Default 10.
ENTITY_RESOLUTION_PUBCHEM_TIMEOUT: float = _getenv_float(
    "ENTITY_RESOLUTION_PUBCHEM_TIMEOUT", 10.0
)

# Number of retries with exponential backoff on transient failures.
# Default 3.
ENTITY_RESOLUTION_PUBCHEM_MAX_RETRIES: int = _getenv_int(
    "ENTITY_RESOLUTION_PUBCHEM_MAX_RETRIES", 3
)

# Optional PubChem API key.  When set, the rate limit is raised from
# 5 req/sec to 10 req/sec per PubChem's published limits.  Audit D9-6.
ENTITY_RESOLUTION_PUBCHEM_API_KEY: Optional[str] = (
    _getenv("ENTITY_RESOLUTION_PUBCHEM_API_KEY", "") or None
)

# Optional path to a CA bundle for TLS verification against an
# internal PubChem mirror.  Audit D9-5.
ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE: Optional[str] = (
    _getenv("ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE", "") or None
)

# Optional mTLS client certificate paths.  Audit D9-5.
ENTITY_RESOLUTION_PUBCHEM_CERT_PEM: Optional[str] = (
    _getenv("ENTITY_RESOLUTION_PUBCHEM_CERT_PEM", "") or None
)
ENTITY_RESOLUTION_PUBCHEM_KEY_PEM: Optional[str] = (
    _getenv("ENTITY_RESOLUTION_PUBCHEM_KEY_PEM", "") or None
)

# If True, reject PubChem name lookups that resolve to a salt form
# (e.g. "aspirin" -> "aspirin sodium").  Audit D3-7.
ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM: bool = _getenv_bool(
    "ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM", False
)

# Optional comma-separated whitelist of allowed ``source`` argument
# values passed to add_source_records().  When set, unknown source
# labels raise ValueError.  Audit D9-7.
ENTITY_RESOLUTION_SOURCE_WHITELIST: Optional[Tuple[str, ...]] = (
    tuple(
        s.strip()
        for s in _getenv("ENTITY_RESOLUTION_SOURCE_WHITELIST", "").split(",")
        if s.strip()
    )
    or None
)

# Default organism when protein records omit it.  ⚠️  This default
# assumes human-centric research; non-human protein studies MUST
# override it.  Audit D12-5.
ENTITY_RESOLUTION_DEFAULT_ORGANISM: str = _getenv(
    "ENTITY_RESOLUTION_DEFAULT_ORGANISM", "Homo sapiens"
)

# Schema version of the state-dict format.  Audit D12-4.
ENTITY_RESOLUTION_MAPPING_SCHEMA_VERSION: str = "1.0"


def get_entity_resolution_config() -> Dict[str, Any]:
    """Return a dict of every ENTITY_RESOLUTION_* setting.

    Convenience helper for logging / introspection.  Sensitive fields
    are masked.
    """
    return {
        "pubchem_enabled": ENTITY_RESOLUTION_PUBCHEM_ENABLED,
        "collapse_stereoisomers": ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS,
        "fuzzy_threshold": ENTITY_RESOLUTION_FUZZY_THRESHOLD,
        "fuzzy_max_candidates": ENTITY_RESOLUTION_FUZZY_MAX_CANDIDATES,
        "pubchem_rest_base": ENTITY_RESOLUTION_PUBCHEM_REST_BASE,
        "pubchem_call_delay": ENTITY_RESOLUTION_PUBCHEM_CALL_DELAY,
        "pubchem_timeout": ENTITY_RESOLUTION_PUBCHEM_TIMEOUT,
        "pubchem_max_retries": ENTITY_RESOLUTION_PUBCHEM_MAX_RETRIES,
        "pubchem_api_key": (
            "<redacted>" if ENTITY_RESOLUTION_PUBCHEM_API_KEY else None
        ),
        "pubchem_ca_bundle": ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE,
        "pubchem_cert_pem": ENTITY_RESOLUTION_PUBCHEM_CERT_PEM,
        "pubchem_key_pem": ENTITY_RESOLUTION_PUBCHEM_KEY_PEM,
        "pubchem_strict_salt_form": ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM,
        "source_whitelist": ENTITY_RESOLUTION_SOURCE_WHITELIST,
        "default_organism": ENTITY_RESOLUTION_DEFAULT_ORGANISM,
        "mapping_schema_version": ENTITY_RESOLUTION_MAPPING_SCHEMA_VERSION,
    }


# ===========================================================================
# PubChem Pipeline — institutional-grade settings (CONF-1 … CONF-12, ARCH-7)
#
# These settings are consumed by ``pipelines/pubchem_pipeline.py``.  They
# complement (do NOT duplicate) the ``ENTITY_RESOLUTION_PUBCHEM_*`` block
# above — REST base / call delay / timeout / max retries / API key /
# CA bundle / client cert / strict salt form are reused from there.
#
# Every value is env-var-overridable.  Defaults are documented inline.
# ===========================================================================

# [CONF-1, ARCH-7] Number of InChIKeys per PubChem PUG REST batch request.
# Why 95 and not 100?  PubChem PUG REST hard limit is 100 identifiers per
# batch.  We use 95 to leave a 5% safety margin in case PubChem lowers the
# limit (they have historically).  Set to 5 in dev for fast testing.
# See: https://pubchemdocs.ncbi.nlm.nih.gov/pug-rest
PUBCHEM_PIPELINE_BATCH_SIZE: int = _getenv_int(
    "PUBCHEM_PIPELINE_BATCH_SIZE", 95
)

# [CONF-3] Minimum backoff (seconds) for exponential retry on transient
# PubChem failures (429, 5xx).  Multiplied by 2^attempt, capped at
# ``PUBCHEM_PIPELINE_MAX_BACKOFF``.  Default 2.0s matches PubChem's
# recommendation for courteous retry.
PUBCHEM_PIPELINE_MIN_BACKOFF: float = _getenv_float(
    "PUBCHEM_PIPELINE_MIN_BACKOFF", 2.0
)

# [CONF-3] Maximum backoff (seconds) — caps the exponential growth so a
# badly-degraded PubChem does not stall the pipeline for hours.
PUBCHEM_PIPELINE_MAX_BACKOFF: float = _getenv_float(
    "PUBCHEM_PIPELINE_MAX_BACKOFF", 32.0
)

# [CONF-5, DESIGN-14] Read timeout (seconds) for PubChem PUG REST.  Connect
# timeout comes from ``ENTITY_RESOLUTION_PUBCHEM_TIMEOUT`` (default 10.0).
# Combined as a ``(connect, read)`` tuple passed to ``requests``.
PUBCHEM_PIPELINE_READ_TIMEOUT: float = _getenv_float(
    "PUBCHEM_PIPELINE_READ_TIMEOUT", 30.0
)

# [CONF-1, DQ-14, IDEM-1] Cache TTL (seconds) for ``inchikeys_to_lookup.txt``.
# Files older than this trigger a re-query.  Default 1 hour — balances
# freshness against PubChem API load.  Set to 0 to disable caching.
PUBCHEM_PIPELINE_CACHE_TTL_SECONDS: int = _getenv_int(
    "PUBCHEM_PIPELINE_CACHE_TTL_SECONDS", 3600
)

# [ARCH-13, PERF-1] Concurrency for batch HTTP requests.  Default 1
# (sequential) for determinism.  Production may set to 5 (PubChem allows
# 5 req/sec) for 5x throughput.  Tests run with concurrency=1.
PUBCHEM_PIPELINE_CONCURRENCY: int = _getenv_int(
    "PUBCHEM_PIPELINE_CONCURRENCY", 1
)

# [SCI-7] Optionally fetch PubChem synonyms (voluminous — default False).
# When True, ``pubchem_compound_properties.synonyms`` is populated as a
# JSON array string.  Single source of truth for entity_resolution.
PUBCHEM_PIPELINE_FETCH_SYNONYMS: bool = _getenv_bool(
    "PUBCHEM_PIPELINE_FETCH_SYNONYMS", False
)

# [SCI-6] Optionally fetch CAS Registry Number via the synonyms endpoint.
# Default False — adds 1 extra HTTP call per resolved CID.  When True,
# ``pubchem_compound_properties.cas_number`` is populated and cross-
# validated against ``drugs.cas_number`` (from DrugBank).
PUBCHEM_PIPELINE_FETCH_CAS: bool = _getenv_bool(
    "PUBCHEM_PIPELINE_FETCH_CAS", False
)

# [REL-5] Maximum batch size for split-retry on permanent 4xx failures.
# When a batch returns 400/404 etc., the batch is split into individual
# InChIKey lookups.  This cap prevents 100 individual requests for a
# fully-bad batch — if exceeded, all 100 are dead-lettered without splitting.
PUBCHEM_PIPELINE_SPLIT_RETRY_MAX: int = _getenv_int(
    "PUBCHEM_PIPELINE_SPLIT_RETRY_MAX", 20
)

# [DQ-9, SEC-12] Maximum number of InChIKeys to enrich per run.  None = no
# limit.  Useful for dev/testing and for capping PubChem API load.
PUBCHEM_PIPELINE_MAX_RECORDS: Optional[int] = (
    int(_getenv("PUBCHEM_PIPELINE_MAX_RECORDS", ""))
    if _getenv("PUBCHEM_PIPELINE_MAX_RECORDS", "").strip()
    else None
)

# [LIN-9] Retention period (days) for raw PubChem JSON responses archived
# in ``raw_data/pubchem/pubchem_responses/``.  Older files are eligible for
# cleanup by an external janitor process.  Default 90 days.
PUBCHEM_PIPELINE_RAW_RESPONSE_RETENTION_DAYS: int = _getenv_int(
    "PUBCHEM_PIPELINE_RAW_RESPONSE_RETENTION_DAYS", 90
)

# [ARCH-9, REL-3] Circuit breaker threshold for PubChem 5xx storms.
# After this many consecutive failures, the breaker opens and the
# pipeline fails fast for ``PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS``.
PUBCHEM_CIRCUIT_BREAKER_THRESHOLD: int = _getenv_int(
    "PUBCHEM_CIRCUIT_BREAKER_THRESHOLD", 5
)

# [ARCH-9, REL-3] Circuit breaker reset window (seconds).  After this
# cooldown, the breaker enters HALF_OPEN and allows one probe request.
PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS: float = _getenv_float(
    "PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS", 60.0
)

# [CONF-6] Comma-separated list of PubChem properties to fetch per CID.
# Rarely changed — but exposed for forward-compat with new PubChem fields.
PUBCHEM_PIPELINE_PROPERTIES: list[str] = [
    p.strip()
    for p in _getenv(
        "PUBCHEM_PIPELINE_PROPERTIES",
        ",".join(
            [
                "MolecularFormula",
                "MolecularWeight",
                "InChIKey",
                "InChI",
                "CanonicalSMILES",
                "IsomericSMILES",
                "IUPACName",
                "XLogP",
                "ExactMass",
                "TPSA",
                "Complexity",
                "HBondDonorCount",
                "HBondAcceptorCount",
                "RotatableBondCount",
                "HeavyAtomCount",
            ]
        ),
    ).split(",")
    if p.strip()
]

# [LOG-3] Optional Prometheus metrics emission.  Default False — don't
# add the prometheus_client import overhead in dev.  When True, the
# pipeline emits ``pubchem_batches_total``, ``pubchem_retries_total``,
# ``pubchem_records_loaded``, ``pubchem_api_latency_seconds``.
PROMETHEUS_ENABLED: bool = _getenv_bool("PROMETHEUS_ENABLED", False)

# [LOG-4] Optional OpenTelemetry tracing.  Default False.  When True,
# the pipeline emits spans for each batch lookup.
OTEL_ENABLED: bool = _getenv_bool("OTEL_ENABLED", False)

# [COMP-5] Operator identity for FDA 21 CFR Part 11 electronic-signature
# compliance.  Populated in ``pubchem_compound_properties.triggered_by``
# and ``electronic_signature``.  None when run unattended (Airflow).
OPERATOR_ID: Optional[str] = (
    _getenv("OPERATOR_ID", "").strip() or None
)

# [SCI-10, SCI-15] Auto-detect RDKit availability.  When True, the pipeline
# validates SMILES via RDKit and computes formal charge from the molecule
# object (authoritative).  When False, formal charge is parsed from the
# SMILES string (heuristic) and SMILES are not validated.
try:
    import rdkit  # noqa: F401  — presence check only
    RDKIT_AVAILABLE: bool = True
except ImportError:
    RDKIT_AVAILABLE: bool = False


# ---------------------------------------------------------------------------
# DrugBank — CODE-7, INTEROP-1
# ---------------------------------------------------------------------------

# DrugBank distributes the full database as a .xml.gz file.
# The exact filename varies by release version. Common names:
#   - drugbank_all_full_database.xml.gz
#   - full database.xml.gz
# If your DrugBank file has a different name, set DRUGBANK_XML_PATH
# to the exact path. (DATA-5)
DRUGBANK_XML_PATH: Path = Path(
    _getenv(
        "DRUGBANK_XML_PATH",
        str(RAW_DATA_DIR / "drugbank" / "drugbank_all_full_database.xml.gz"),
    )
    # If the env var is set but empty, fall back to the default.
    # Without this guard, Path("") == Path(".") which is the current
    # directory — causes a confusing IsADirectoryError downstream.
    or str(RAW_DATA_DIR / "drugbank" / "drugbank_all_full_database.xml.gz")
)

# ---------------------------------------------------------------------------
# DrugBank extended configuration block (CF1-CF15).
#
# Mirrors the CHEMBL_VERSION pattern: DEFAULT_* -> VALID_* frozenset ->
# _validate_* helper -> public *_VERSION constant. All values are
# environment-overridable so deployments can change behaviour without
# touching code (CF1-CF15, ID2, ID4, S7, S9, CF3-CF13).
# ---------------------------------------------------------------------------

# CF2 / ID2: DrugBank release version (default 5.1; update when upgrading).
DEFAULT_DRUGBANK_VERSION: str = "5.1"

# Valid DrugBank 5.x release versions (NCBI / Wishart 2018 lineage).
# v28 ROOT FIX (audit TOP-23): "5.2" was listed but does NOT EXIST
# publicly. DrugBank's latest public release as of 2024 is 5.1.x
# (5.1.12 was the most recent). The fictional "5.2" entry would have
# accepted ``DRUGBANK_VERSION=5.2`` as a known-good version, silencing
# the "not in the known valid set" warning — operators could then
# configure the pipeline against a non-existent release and never see
# a hint that the version was wrong. Removed here.
VALID_DRUGBANK_VERSIONS: frozenset[str] = frozenset(
    {"5.0", "5.1", "5.1.8", "5.1.9", "5.1.10", "5.1.11", "5.1.12"}
)


def _validate_drugbank_version(version: str) -> str:
    """Validate DrugBank version string (mirrors _validate_chembl_version).

    Accepts numeric version strings like ``5.1`` or ``5.1.10``. Warns on
    unknown versions. Raises ``ValueError`` on clearly invalid values
    (non-numeric, empty).
    """
    if not version or not version.strip():
        raise ValueError("DRUGBANK_VERSION cannot be empty")
    if not version.replace(".", "").isdigit():
        raise ValueError(
            f"DRUGBANK_VERSION={version!r} is not a valid version string. "
            f"Expected a numeric version like '5.1' or '5.1.10'. "
            f"Valid versions: {sorted(VALID_DRUGBANK_VERSIONS)}"
        )
    if version not in VALID_DRUGBANK_VERSIONS:
        warnings.warn(
            f"DRUGBANK_VERSION={version} is not in the known valid set. "
            f"The DrugBank XML schema may not match. "
            f"Known valid versions: {sorted(VALID_DRUGBANK_VERSIONS)}",
            UserWarning,
        )
    return version


# Public source version constant (CF2 / ID2 / A8).
DRUGBANK_VERSION: str = _validate_drugbank_version(
    _getenv("DRUGBANK_VERSION", DEFAULT_DRUGBANK_VERSION)
)

# CF1: XML namespace (stable since 2010). Config-overridable for forward compat.
DRUGBANK_XML_NAMESPACE: str = _getenv(
    "DRUGBANK_XML_NAMESPACE", "http://drugbank.ca"
)

# S9: organism filter (default Humans-only for human drug repurposing).
# Comma-separated list. For infectious-disease use cases set to
# "Humans,HIV-1,Mycobacterium tuberculosis".
DRUGBANK_TARGET_ORGANISMS: list[str] = [
    org.strip()
    for org in _getenv("DRUGBANK_TARGET_ORGANISMS", "Humans").split(",")
    if org.strip()
]

# S7: synthetic InChIKey generation for biologics (insulin, antibodies).
# Drug model allows 'SYNTH-...' via CheckConstraint (models.py).
DRUGBANK_GENERATE_SYNTH_KEYS: bool = _getenv_bool(
    "DRUGBANK_GENERATE_SYNTH_KEYS", True
)

# S7: hard drop of records with no InChIKey (default False — keep biologics).
DRUGBANK_DROP_NO_INCHIKEY: bool = _getenv_bool("DRUGBANK_DROP_NO_INCHIKEY", False)

# ID4: conservative_defaults flag for fill_missing_drug_fields.
DRUGBANK_CONSERVATIVE_DEFAULTS: bool = _getenv_bool(
    "DRUGBANK_CONSERVATIVE_DEFAULTS", True
)

# CF13: batch size for bulk_upsert_drugs / bulk_upsert_dpi.
DRUGBANK_BATCH_SIZE: int = _parse_required_int("DRUGBANK_BATCH_SIZE", "1000")

# CF7: iterparse log interval (drugs parsed between INFO logs).
DRUGBANK_LOG_INTERVAL: int = _parse_required_int("DRUGBANK_LOG_INTERVAL", "5000")

# CF8: max drug count safety limit (0 = unlimited; for testing).
DRUGBANK_MAX_DRUGS: int = _parse_required_int("DRUGBANK_MAX_DRUGS", "0")

# CF9: extract targets / enzymes / transporters (all default True).
DRUGBANK_EXTRACT_TARGETS: bool = _getenv_bool("DRUGBANK_EXTRACT_TARGETS", True)
DRUGBANK_EXTRACT_ENZYMES: bool = _getenv_bool("DRUGBANK_EXTRACT_ENZYMES", True)
DRUGBANK_EXTRACT_TRANSPORTERS: bool = _getenv_bool(
    "DRUGBANK_EXTRACT_TRANSPORTERS", True
)

# CF12: output CSV compression ("gzip" or "none").
DRUGBANK_CSV_COMPRESSION: str = _getenv("DRUGBANK_CSV_COMPRESSION", "gzip")

# SEC1: optional SHA-256 of the input XML for tamper-evidence.
DRUGBANK_EXPECTED_SHA256: str = _getenv("DRUGBANK_EXPECTED_SHA256", "")

# CF3: expected drug count range for sanity checking.
# v41 ROOT FIX (SCIENTIFIC): DRUGBANK_EXPECTED_DRUG_COUNT_MIN=10000 was
# calibrated against the FULL DrugBank release (~14K small-molecule drugs),
# but the pipeline applies organism / drug-type / groups filters (e.g.
# "approved" group only, biotech=True, etc.) which reduce the post-filter
# subset to ~1-3K. The 10000 floor meant a 2K-row truncated/filtered load
# would have been REJECTED as "below minimum" even when correct. Lowered to
# 1000 with a comment that organism/drug-type filters reduce the count.
DRUGBANK_EXPECTED_DRUG_COUNT_MIN: int = _parse_required_int(
    "DRUGBANK_DRUG_COUNT_MIN", "1000"
)
DRUGBANK_EXPECTED_DRUG_COUNT_MAX: int = _parse_required_int(
    "DRUGBANK_DRUG_COUNT_MAX", "20000"
)

# SEC2: redact proprietary DrugBank content from logs in production.
DRUGBANK_LOG_REDACT: bool = _getenv_bool("DRUGBANK_LOG_REDACT", False)

# SEC12: log full file paths (False = filename only).
DRUGBANK_LOG_FULL_PATHS: bool = _getenv_bool("DRUGBANK_LOG_FULL_PATHS", False)

# CF15: validate the XML path is readable before parsing.
DRUGBANK_VALIDATE_READABILITY: bool = _getenv_bool(
    "DRUGBANK_VALIDATE_READABILITY", True
)

# DPI batch size for chunked bulk_upsert_dpi (P13).
DRUGBANK_DPI_BATCH_SIZE: int = _parse_required_int("DRUGBANK_DPI_BATCH_SIZE", "500")

# ---------------------------------------------------------------------------
# OMIM — SEC-2 + 16-domain institutional-grade config (master prompt §7.12)
# ---------------------------------------------------------------------------
# BUG-9.15 / BUG-12.8: OMIM_API_KEY stripped of whitespace (handles trailing
# newlines that some secret managers inject).
# v41 ROOT FIX (SEV2): route through _getenv so .env is loaded first and the
# canonical read path is used. Direct os.getenv bypasses _ensure_dotenv_loaded
# and could read a stale environ in containers that mount .env at runtime.
OMIM_API_KEY: str = (_getenv("OMIM_API_KEY", "") or "").strip()
OMIM_API_BASE: str = _getenv("OMIM_API_BASE", "https://api.omim.org/api") or "https://api.omim.org/api"

# BUG-2.6 / BUG-12.1: rate-limit interval between OMIM API requests.
# OMIM's published rate limit is 4 req/sec → 0.25s between requests.
OMIM_REQUEST_INTERVAL: float = _getenv_float("OMIM_REQUEST_INTERVAL", 0.25)

# BUG-2.5 / BUG-3.5 / BUG-3.6 / BUG-12.3: which phenotype mapping keys to
# include in the cleaned GDA output. Default [3, 4] — molecular basis known
# (mk=3) plus contiguous gene deletion/duplication syndromes (mk=4, e.g.
# DiGeorge, Williams). Both are clinically real and well-characterized.
# Advanced users can set OMIM_MAPPING_KEYS_INCLUDE=1,2,3,4 for comprehensive
# ingest (mk=1 = wild-type gene mapped, mk=2 = phenotype mapped).
OMIM_MAPPING_KEYS_INCLUDE: list[int] = _parse_csv_ints(
    "OMIM_MAPPING_KEYS_INCLUDE", [3, 4]
)

# BUG-2.7 / BUG-8.2 / BUG-12.2: API pagination page size.
# OMIM REST API max limit is 1000. Setting to 1000 is 5× faster than the
# legacy 200.
OMIM_API_PAGE_LIMIT: int = _getenv_int("OMIM_API_PAGE_LIMIT", 1000)

# BUG-2.7 / BUG-12.19: maximum HTTP retries on retryable status codes.
OMIM_API_MAX_RETRIES: int = _getenv_int("OMIM_API_MAX_RETRIES", 5)

# BUG-12.4: per-request timeouts (seconds).
OMIM_DOWNLOAD_TIMEOUT: int = _getenv_int("OMIM_DOWNLOAD_TIMEOUT", 300)
OMIM_API_TIMEOUT: int = _getenv_int("OMIM_API_TIMEOUT", 120)

# BUG-12.5 / BUG-13.20: output filename (kept configurable for backfill
# isolation and test redirection).
OMIM_OUTPUT_FILENAME: str = _getenv(
    "OMIM_OUTPUT_FILENAME", "omim_gene_disease_associations.csv"
)

# BUG-5.1 / BUG-12.15: minimum expected record count after morbidmap parse.
# OMIM typically publishes ~7,000 morbidmap entries; 5,000 is a safe floor
# that catches truncated downloads without false-failing on legit small runs.
OMIM_MIN_EXPECTED_RECORDS: int = _getenv_int("OMIM_MIN_EXPECTED_RECORDS", 5000)

# BUG-6.5 / BUG-12.16: upper bound on pagination pages.
OMIM_MAX_PAGINATION_PAGES: int = _getenv_int("OMIM_MAX_PAGINATION_PAGES", 1000)

# BUG-12.17: legacy dedup-keep-policy — kept for backward-compat; the new
# atomic-write path (BUG-1.9) doesn't append, so this is informational only.
OMIM_DEDUP_KEEP_POLICY: str = _getenv("OMIM_DEDUP_KEEP_POLICY", "last")

# BUG-2.3 / BUG-3.2 / BUG-12.12 / BUG-12.13: per-mapping-key base scores.
# These are evidence-weighted starting points; the final score is
#   base + min(0.05 * log1p(num_pmids), 0.08) + min(evidence_strength * 0.05, 0.05)
# clamped to [0, 1].
#   - mk=3 (0.9): molecular basis known (mutation found in gene). Strongest
#     single signal — chosen to match DisGeNET's strong-evidence threshold.
#   - mk=4 (0.8): contiguous gene deletion/duplication syndrome. Clinically
#     validated, but the gene-disease causal chain is less direct than mk=3.
#     v41 ROOT FIX (SCIENTIFIC): the 0.8 score for mk=4 is DEBATABLE —
#     contiguous-gene syndromes (DiGeorge, Williams) involve a deletion
#     spanning many genes, and pinning the phenotype to any single gene in
#     the interval is probabilistic. Some curators (e.g. OMIM's own
#     morbidmap) flag these with a star indicating the gene is in the
#     deleted interval but not necessarily the causal gene. A defensible
#     alternative would be 0.7 (between mk=3's 0.9 and mk=2's 0.6). The
#     0.8 value is KEPT for now to avoid silently re-ranking historical
#     runs; revisit during the next evidence-tier calibration review.
#   - mk=2 (0.6): the disease phenotype itself was mapped (no gene identified).
#   - mk=1 (0.5): the wild-type gene was mapped (weakest OMIM evidence tier).
OMIM_CONFIRMED_SCORE: float = _getenv_float("OMIM_CONFIRMED_SCORE", 0.9)
OMIM_CONTIGUOUS_SCORE: float = _getenv_float("OMIM_CONTIGUOUS_SCORE", 0.8)
OMIM_PHENOTYPE_MAPPED_SCORE: float = _getenv_float("OMIM_PHENOTYPE_MAPPED_SCORE", 0.6)
OMIM_GENE_MAPPED_SCORE: float = _getenv_float("OMIM_GENE_MAPPED_SCORE", 0.5)

# BUG-12.20: User-Agent string sent with every OMIM HTTP request.
OMIM_USER_AGENT: str = _getenv(
    "OMIM_USER_AGENT",
    f"drug-repurposing-pipeline/omim (contact={_getenv('OMIM_CONTACT_EMAIL', 'unknown@example.com')})",
)

# BUG-12.6: regex validating the OMIM_API_KEY format. OMIM API keys are UUIDs.
OMIM_API_KEY_FORMAT_RE: str = r"^[a-f0-9-]{36}$"

# BUG-5.6 / BUG-7.2: maximum age (days) of a cached download before forcing
# a refresh.
OMIM_MAX_AGE_DAYS: int = _getenv_int("OMIM_MAX_AGE_DAYS", 30)

# BUG-8.20: DB batch size for bulk_upsert_gda.
OMIM_DB_BATCH_SIZE: int = _getenv_int("OMIM_DB_BATCH_SIZE", 1000)

# BUG-3.13: when True (default — the safe choice for drug repurposing),
# susceptibility ({}) records are routed to a separate CSV and excluded from
# the main GDA load. Downstream ML MUST filter WHERE is_susceptibility = False
# for repurposing candidates. Treating {} as causal is the patient-harm
# failure mode the master prompt explicitly warns about.
OMIM_EXCLUDE_SUSCEPTIBILITY: bool = _getenv_bool("OMIM_EXCLUDE_SUSCEPTIBILITY", True)

# BUG-4.18 / BUG-8.13: pretty-print JSON in dev mode only (production is
# compact + deterministic).
OMIM_JSON_PRETTY: bool = _getenv_bool("OMIM_JSON_PRETTY", False)

# BUG-7.4 / BUG-4.9: random seed for retry backoff jitter. Fixed at module
# load for reproducibility.
OMIM_RANDOM_SEED: int = _getenv_int("OMIM_RANDOM_SEED", 42)

# BUG-9.15: helper — does the OMIM_API_KEY look like a valid UUID?
def _omim_api_key_is_valid_format() -> bool:
    """Return True iff OMIM_API_KEY is empty OR matches the UUID format."""
    if not OMIM_API_KEY:
        return True  # empty is allowed (pipeline will raise at download time)
    return bool(re.match(OMIM_API_KEY_FORMAT_RE, OMIM_API_KEY))


# BUG-12.11: eager validation of OMIM config — mirrors DisGeNET's
# `_validate_disgenet_config` pattern. Raises ValueError on invalid values;
# logs UserWarning if a non-critical key is misconfigured.
def _validate_omim_config() -> None:
    """Validate OMIM_* env vars at module import time.

    Raises:
        ValueError: if a critical config value is out of range.
    """
    errors: list[str] = []
    if OMIM_REQUEST_INTERVAL <= 0:
        errors.append("OMIM_REQUEST_INTERVAL must be > 0")
    if not (1 <= OMIM_API_PAGE_LIMIT <= 1000):
        errors.append("OMIM_API_PAGE_LIMIT must be in [1, 1000]")
    if OMIM_API_MAX_RETRIES < 0:
        errors.append("OMIM_API_MAX_RETRIES must be >= 0")
    for mk in OMIM_MAPPING_KEYS_INCLUDE:
        if mk not in (1, 2, 3, 4):
            errors.append(
                f"OMIM_MAPPING_KEYS_INCLUDE contains invalid mk={mk} "
                f"(must be in {{1, 2, 3, 4}})"
            )
    for name, val in (
        ("OMIM_CONFIRMED_SCORE", OMIM_CONFIRMED_SCORE),
        ("OMIM_CONTIGUOUS_SCORE", OMIM_CONTIGUOUS_SCORE),
        ("OMIM_PHENOTYPE_MAPPED_SCORE", OMIM_PHENOTYPE_MAPPED_SCORE),
        ("OMIM_GENE_MAPPED_SCORE", OMIM_GENE_MAPPED_SCORE),
    ):
        if not (0.0 <= val <= 1.0):
            errors.append(f"{name} must be in [0.0, 1.0] (got {val})")
    if OMIM_MIN_EXPECTED_RECORDS < 0:
        errors.append("OMIM_MIN_EXPECTED_RECORDS must be >= 0")
    if OMIM_MAX_PAGINATION_PAGES < 1:
        errors.append("OMIM_MAX_PAGINATION_PAGES must be >= 1")
    if OMIM_DB_BATCH_SIZE < 1:
        errors.append("OMIM_DB_BATCH_SIZE must be >= 1")
    if OMIM_MAX_AGE_DAYS < 0:
        errors.append("OMIM_MAX_AGE_DAYS must be >= 0")
    # BUG-12.7: validate OMIM_API_BASE URL
    try:
        parsed = urllib.parse.urlparse(OMIM_API_BASE)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append(
                f"OMIM_API_BASE must be an http(s) URL (got {OMIM_API_BASE!r})"
            )
    except Exception as exc:
        errors.append(f"OMIM_API_BASE is not a valid URL: {exc}")

    # BUG-12.6: API key format check (warning only — empty is allowed)
    if not _omim_api_key_is_valid_format():
        warnings.warn(
            f"OMIM_API_KEY does not match expected UUID format — may be mistyped",
            UserWarning,
            stacklevel=2,
        )

    if errors:
        raise ValueError(
            "OMIM config validation failed:\n  - " + "\n  - ".join(errors)
        )


# Run eager validation — but be tolerant if a downstream test wants to
# re-configure env vars and reload. Following DisGeNET's pattern: log warning
# and continue on validation failure (the pipeline will raise a clearer error
# at __init__ time).
#
# audit-2025 ROOT FIX (issue 31): the previous except clause was
# ``except (ValueError, UserWarning)``. ``_validate_omim_config`` ONLY
# raises ``ValueError`` (see the ``raise ValueError(...)`` at line 2337
# above) — it never raises ``UserWarning`` directly. (The function
# does CALL ``warnings.warn(..., UserWarning)`` at line 2330, but
# ``warnings.warn`` does not RAISE the warning as an exception unless
# the caller has installed a custom warning filter like
# ``simplefilter("error")``.) Catching ``UserWarning`` here was dead
# code that misled readers into thinking the validator could raise
# warnings as exceptions. The fix removes ``UserWarning`` from the
# except clause so it truthfully reflects what the validator raises.
try:
    _validate_omim_config()
except ValueError as _omim_cfg_exc:
    warnings.warn(
        f"OMIM config validation warning: {_omim_cfg_exc}",
        UserWarning,
        stacklevel=2,
    )


# ---------------------------------------------------------------------------
# v29 ROOT FIX (audit C-13): consolidated OMIM config dict
# ---------------------------------------------------------------------------
# Previously the 20+ OMIM_* settings existed ONLY as flat module-level
# constants. That worked, but it meant every consumer had to import each
# constant by name (see pipelines/omim_pipeline.py — it imports ~15 of
# them individually). This consolidated dict is the canonical structured
# view of all OMIM settings in one place, suitable for:
#   * programmatic introspection (e.g. /health endpoints)
#   * config-dump / provenance metadata (without leaking OMIM_API_KEY)
#   * new code that prefers dict access over many-name imports
#
# The individual OMIM_* module-level constants above are KEPT for
# backwards compatibility — they are the same values, just accessible as
# flat names. The OMIMPipeline continues to import them by name; new
# consumers should prefer ``OMIM_CONFIG``.
#
# NOTE: ``OMIM_API_KEY`` is masked in ``OMIM_CONFIG["api_key_masked"]``
# but the raw value is still in ``OMIM_API_KEY`` (module-level) so the
# pipeline can authenticate. Do not log ``OMIM_CONFIG["api_key"]`` —
# use ``OMIM_CONFIG["api_key_masked"]`` for any human-facing output.
OMIM_CONFIG: dict[str, object] = {
    # --- Connection -----------------------------------------------------
    "api_key": OMIM_API_KEY,
    "api_key_masked": (
        "<set>" if OMIM_API_KEY else "<unset>"
    ),
    "api_base": OMIM_API_BASE,
    "api_key_format_re": OMIM_API_KEY_FORMAT_RE,
    "user_agent": OMIM_USER_AGENT,
    # --- Rate limiting / retries ---------------------------------------
    "request_interval": OMIM_REQUEST_INTERVAL,
    "api_page_limit": OMIM_API_PAGE_LIMIT,
    "api_max_retries": OMIM_API_MAX_RETRIES,
    "api_timeout": OMIM_API_TIMEOUT,
    "download_timeout": OMIM_DOWNLOAD_TIMEOUT,
    "max_pagination_pages": OMIM_MAX_PAGINATION_PAGES,
    "random_seed": OMIM_RANDOM_SEED,
    # --- Mapping / scoring ---------------------------------------------
    "mapping_keys_include": OMIM_MAPPING_KEYS_INCLUDE,
    "confirmed_score": OMIM_CONFIRMED_SCORE,
    "contiguous_score": OMIM_CONTIGUOUS_SCORE,
    "phenotype_mapped_score": OMIM_PHENOTYPE_MAPPED_SCORE,
    "gene_mapped_score": OMIM_GENE_MAPPED_SCORE,
    "exclude_susceptibility": OMIM_EXCLUDE_SUSCEPTIBILITY,
    # --- Output / batching / caching -----------------------------------
    "output_filename": OMIM_OUTPUT_FILENAME,
    "min_expected_records": OMIM_MIN_EXPECTED_RECORDS,
    "max_age_days": OMIM_MAX_AGE_DAYS,
    "db_batch_size": OMIM_DB_BATCH_SIZE,
    "dedup_keep_policy": OMIM_DEDUP_KEEP_POLICY,
    "json_pretty": OMIM_JSON_PRETTY,
}


def get_omim_config() -> dict[str, object]:
    """Return the consolidated OMIM configuration dict (lazy view).

    Returns a *copy* so callers can mutate without affecting the
    module-level state. For the masked API key view, use
    ``OMIM_CONFIG["api_key_masked"]`` or call :func:`get_omim_config`
    and pop ``api_key`` before logging.
    """
    # Refresh from the module-level constants in case they were mutated
    # by tests. We deliberately return a fresh dict each call.
    return dict(OMIM_CONFIG)


# ---------------------------------------------------------------------------
# Airflow
# ---------------------------------------------------------------------------

AIRFLOW_HOME: Path = BASE_DIR / "airflow"

# ---------------------------------------------------------------------------
# Logging — ARCH-2, IDMP-3, SEC-5, LOG-1, LOG-2, LOG-3
# ---------------------------------------------------------------------------

LOG_LEVEL: str = _getenv("LOG_LEVEL", "INFO")

# [CFG-02] Configurable retention period for orphan GDA record cleanup.
# Records with uniprot_id=NULL older than this many hours are eligible
# for deletion by ``cleanup_orphan_gda_records`` in database.loaders.
ORPHAN_GDA_RETENTION_HOURS: int = int(_getenv("ORPHAN_GDA_RETENTION_HOURS", "24"))

# ---------------------------------------------------------------------------
# Loader-specific configuration (CFG-04, REL-04, PERF-07, LOG-05, SEC-06)
# ---------------------------------------------------------------------------
# These settings control the behaviour of database.loaders and can be
# overridden via environment variables without restarting the application.

# [CFG-04] Strict validation: when True, invalid records are quarantined
# and a WARNING is logged.  When False, invalid records are logged but
# still upserted (useful for initial data loads where completeness
# matters more than correctness).
LOADERS_STRICT_VALIDATION: bool = _getenv(
    "LOADERS_STRICT_VALIDATION", "true"
).lower() in ("true", "1", "yes")

# [REL-04] Maximum retry attempts for database operations with
# exponential backoff.  Applies to cleanup_orphan_gda_records and
# lookup functions.
LOADERS_MAX_RETRY_ATTEMPTS: int = int(
    _getenv("LOADERS_MAX_RETRY_ATTEMPTS", "3")
)

# [REL-04] Base delay in seconds for exponential backoff.  Actual delay
# is base_delay * (2 ** attempt_index).
LOADERS_RETRY_BASE_DELAY: float = float(
    _getenv("LOADERS_RETRY_BASE_DELAY", "0.5")
)

# [LOG-05] Enable timing/metrics logging for upsert operations.
LOADERS_ENABLE_TIMING: bool = _getenv(
    "LOADERS_ENABLE_TIMING", "true"
).lower() in ("true", "1", "yes")

# [REL-06] Enable the dead letter queue for failed/unprocessable records.
LOADERS_DEAD_LETTER_ENABLED: bool = _getenv(
    "LOADERS_DEAD_LETTER_ENABLED", "true"
).lower() in ("true", "1", "yes")

# [SEC-06] Maximum number of records that cleanup_orphan_gda_records
# may delete in a single call.  Prevents mass deletion from
# misconfiguration.
LOADERS_MAX_DELETE_COUNT: int = int(
    _getenv("LOADERS_MAX_DELETE_COUNT", "10000")
)

# [PERF-07] [CFG-03] Per-table batch size overrides.  Parsed from a
# comma-separated env var: "drugs=1000,proteins=500,dpi=2000".
# Tables not listed use DEFAULT_BATCH_SIZE from database.loaders.
_BATCH_SIZE_OVERRIDES_RAW: str = _getenv("LOADERS_BATCH_SIZE_OVERRIDES", "")
BATCH_SIZE_OVERRIDES: dict[str, int] = {}
if _BATCH_SIZE_OVERRIDES_RAW:
    for _pair in _BATCH_SIZE_OVERRIDES_RAW.split(","):
        _pair = _pair.strip()
        if "=" in _pair:
            _tbl, _sz = _pair.split("=", 1)
            try:
                BATCH_SIZE_OVERRIDES[_tbl.strip()] = int(_sz.strip())
            except ValueError:
                pass

_logging_configured: bool = False

logger = logging.getLogger(__name__)


def setup_logging(level: Optional[str] = None) -> None:
    """Configure logging for the platform. Idempotent and safe to call
    multiple times.

    This should be called explicitly in application entry points
    (main.py, DAG files, test conftest.py) rather than at module import
    time. It configures ONLY the platform's own logger namespaces, NOT
    the root logger, to avoid capturing sensitive values from third-party
    modules (SEC-5).

    Parameters
    ----------
    level : str, optional
        Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        Defaults to LOG_LEVEL env var or INFO.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True

    log_level = level or LOG_LEVEL
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # Configure the root logger via logging.basicConfig so that
    # third-party modules' log records also get formatted output.
    # We pass our handler and format to basicConfig (idempotent — if
    # the root logger already has handlers, basicConfig is a no-op).
    logging.basicConfig(
        level=log_level,
        handlers=[handler],
    )
    # Configure ONLY our namespace loggers, not the root logger (SEC-5)
    for namespace in (
        "config",
        "pipelines",
        "database",
        "cleaning",
        "entity_resolution",
        "exporters",
    ):
        ns_logger = logging.getLogger(namespace)
        ns_logger.setLevel(getattr(logging, log_level, logging.INFO))
        ns_logger.addHandler(handler)


def _mask_url(url: str) -> str:
    """Mask password in a URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username}:****@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url


def log_config_summary() -> None:
    """Log a startup summary of the active configuration.

    Sensitive values (API keys, passwords) are masked.
    Should be called from entry points after setup_logging().
    """
    summary = {
        "ENVIRONMENT": ENVIRONMENT,
        "CHEMBL_VERSION": CHEMBL_VERSION,
        "STRING_VERSION": STRING_VERSION,
        "STRING_MIN_COMBINED_SCORE": STRING_MIN_COMBINED_SCORE,
        "UNIPROT_RELEASE": UNIPROT_RELEASE,
        "DISGENET_USE_API": DISGENET_USE_API,
        "DISGENET_API_KEY": "***" if DISGENET_API_KEY else "(not set)",
        "OMIM_API_KEY": "***" if OMIM_API_KEY else "(not set)",
        "CHEMBL_MAX_ROWS": CHEMBL_MAX_ROWS or "(unlimited)",
        "CHEMBL_MAX_ACTIVITIES": CHEMBL_MAX_ACTIVITIES or "(unlimited)",
        "DATABASE_URL": _mask_url(DATABASE_URL),
        "DATA_SNAPSHOT_ID": DATA_SNAPSHOT_ID,
    }
    logger.info("=== Configuration Summary ===")
    for key, value in summary.items():
        logger.info("  %s = %s", key, value)
    logger.info("=== End Configuration Summary ===")


# ---------------------------------------------------------------------------
# Data provenance & versioning — DATA-3, IDMP-4, LINEAGE-1, LINEAGE-2
# ---------------------------------------------------------------------------

DATA_SNAPSHOT_ID: str = _getenv(
    "DATA_SNAPSHOT_ID",
    f"chembl{CHEMBL_VERSION}_string{STRING_VERSION}_"
    f"uniprot{UNIPROT_RELEASE}_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
)


def get_data_version_info() -> dict[str, str]:
    """Return a dict of all data source versions for embedding in output
    metadata.

    This should be called by pipeline entry points and embedded in every
    output CSV/JSON file produced by the pipeline for traceability.
    """
    return {
        "snapshot_id": DATA_SNAPSHOT_ID,
        "chembl_version": CHEMBL_VERSION,
        "string_version": STRING_VERSION,
        "uniprot_release": UNIPROT_RELEASE,
        "disgenet_source": "api" if DISGENET_USE_API else "static",
        "string_min_score": str(STRING_MIN_COMBINED_SCORE),
    }


def get_provenance_metadata() -> dict:
    """Return complete provenance metadata for embedding in pipeline output.

    This metadata should be embedded in every output file produced by the
    pipeline, either as a companion ``_metadata.json`` file or as comment
    headers in the CSV.
    """
    config_str = str(sorted(get_data_version_info().items()))
    config_hash = hashlib.sha256(config_str.encode()).hexdigest()[:12]
    return {
        "config_fingerprint": config_hash,
        "data_snapshot_id": DATA_SNAPSHOT_ID,
        "chembl_version": CHEMBL_VERSION,
        "string_version": STRING_VERSION,
        "string_min_score": STRING_MIN_COMBINED_SCORE,
        "uniprot_release": UNIPROT_RELEASE,
        "disgenet_source": "api" if DISGENET_USE_API else "static",
        "environment": ENVIRONMENT,
        "pipeline_version": "v1.0",
    }


# ---------------------------------------------------------------------------
# URL validation — DATA-1, INTEROP-3
# ---------------------------------------------------------------------------


def validate_all_urls() -> dict[str, bool]:
    """Validate all URL settings with HEAD requests.

    Returns a dict of setting_name -> is_valid. Logs warnings for
    failing URLs. Does NOT raise — the pipeline should start even if a
    URL is temporarily down.
    """
    results: dict[str, bool] = {}
    url_settings = {
        "STRING_PROTEIN_LINKS_URL": STRING_PROTEIN_LINKS_URL,
        "STRING_ALIASES_URL": STRING_ALIASES_URL,
        "DISGENET_API_URL": DISGENET_API_URL,
        "PUBCHEM_REST_BASE": PUBCHEM_REST_BASE,
        "CHEMBL_API_URL": CHEMBL_API_URL,
    }
    for name, url in url_settings.items():
        try:
            import requests

            resp = requests.head(url, timeout=10, allow_redirects=True)
            is_valid = resp.status_code < 400
            if not is_valid:
                logger.warning(
                    "URL validation failed for %s: %s returned HTTP %d",
                    name,
                    url,
                    resp.status_code,
                )
            results[name] = is_valid
        except Exception as exc:
            logger.warning(
                "URL validation failed for %s: %s - %s", name, url, exc
            )
            results[name] = False
    return results


def check_api_endpoints() -> dict[str, dict]:
    """Check availability of all API endpoints.

    Returns a dict of endpoint_name -> status info.
    """
    results: dict[str, dict] = {}
    endpoints = {
        "chembl": CHEMBL_API_URL,
        "disgenet": DISGENET_API_URL,
        "omim": OMIM_API_BASE,
        "pubchem": PUBCHEM_REST_BASE,
    }
    for name, url in endpoints.items():
        try:
            import requests

            resp = requests.head(url, timeout=10, allow_redirects=True)
            results[name] = {
                "url": url,
                "status": resp.status_code,
                "available": resp.status_code < 400,
            }
        except Exception as exc:
            results[name] = {
                "url": url,
                "status": None,
                "available": False,
                "error": str(exc),
            }
    return results


# ---------------------------------------------------------------------------
# API key validation — SEC-2
# ---------------------------------------------------------------------------


def validate_api_keys() -> dict[str, str]:
    """Validate that required API keys are present.

    Returns a dict of key_name -> status ('present' | 'missing').
    Raises ``ValueError`` if DISGENET_USE_API is true and key is missing.
    """
    results = {
        "DISGENET_API_KEY": "present" if DISGENET_API_KEY else "missing",
        "OMIM_API_KEY": "present" if OMIM_API_KEY else "missing",
    }
    if DISGENET_USE_API and not DISGENET_API_KEY:
        raise ValueError(
            "DISGENET_USE_API=true but DISGENET_API_KEY is not set. "
            "Set the DISGENET_API_KEY environment variable or set "
            "DISGENET_USE_API=false (not recommended - static URL is "
            "deprecated)."
        )
    return results


# ---------------------------------------------------------------------------
# .env file checks — SEC-3, COMP-2
# ---------------------------------------------------------------------------


def check_env_git_tracking() -> None:
    """Warn if .env file appears to be tracked by git (SEC-3).

    v41 ROOT FIX (DEAD): this function was defined but NEVER called from
    settings load — operators got no warning even when .env was committed.
    It is now invoked from the module-level init block at the end of this
    file (after BASE_DIR / ENVIRONMENT are defined). The function is
    defensive: no-op if git is unavailable or BASE_DIR is not a repo.
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    try:
        import subprocess

        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(env_path)],
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
        )
        if result.returncode == 0:
            warnings.warn(
                f".env file at {env_path} is tracked by git! "
                f"This exposes API keys and database credentials in "
                f"version control. Run: git rm --cached {env_path} "
                f"&& echo .env >> .gitignore",
                UserWarning,
            )
    except (FileNotFoundError, OSError):
        pass  # git not installed or not a git repo


def validate_env_file(path: Optional[Path] = None) -> list[str]:
    """Validate the .env file format. Returns list of issues found (COMP-2).

    v41 ROOT FIX (DEAD): this function was defined but NEVER called from
    settings load — malformed .env files (e.g. unquoted values containing
    ``=``) silently produced wrong env values. It is now invoked from the
    module-level init block at the end of this file; issues are logged at
    WARNING level (we do NOT abort import — a partially-malformed .env may
    still set the critical vars correctly).
    """
    env_path = path or (BASE_DIR / ".env")
    if not env_path.exists():
        return []  # No .env file is valid
    issues: list[str] = []
    for line_num, line in enumerate(env_path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            issues.append(f"Line {line_num}: Missing = delimiter: {line!r}")
        if line.count("=") > 1:
            key = line.split("=", 1)[0]
            value = line.split("=", 1)[1]
            if not (value.startswith('"') and value.endswith('"')):
                issues.append(
                    f"Line {line_num}: Unquoted value with = sign: {key}"
                )
    return issues


# ---------------------------------------------------------------------------
# Secret management — SEC-4
# ---------------------------------------------------------------------------


def get_secret(key: str, default: str = "") -> str:
    """Get a secret value, preferring platform secret managers over .env.

    Lookup order:
    1. Environment variable (set by K8s Secrets, AWS SM, etc.)
    2. .env file (via dotenv)
    3. Default value
    """
    _ensure_dotenv_loaded()
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Environment schema validation — CONF-3
# ---------------------------------------------------------------------------

ENV_VAR_SCHEMA: dict[str, dict] = {
    "DATABASE_URL": {
        "type": str,
        "required": True,
        "pattern": r"^postgresql://",
    },
    "CHEMBL_VERSION": {
        "type": str,
        "required": False,
        "pattern": r"^[\d.]+$",
    },
    "CHEMBL_MAX_ROWS": {"type": int, "required": False, "min": 0},
    "DISGENET_API_KEY": {"type": str, "required": False},
    "OMIM_API_KEY": {"type": str, "required": False},
    "LOG_LEVEL": {
        "type": str,
        "required": False,
        "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    },
    "ENVIRONMENT": {
        "type": str,
        "required": False,
        "choices": ["development", "staging", "production"],
    },
}


def validate_env_schema() -> list[str]:
    """Validate all env vars against the schema. Returns list of errors."""
    import re

    errors: list[str] = []
    for key, spec in ENV_VAR_SCHEMA.items():
        value = os.getenv(key)
        if spec.get("required") and not value:
            errors.append(f"{key} is required but not set")
            continue
        if value and "pattern" in spec:
            if not re.match(spec["pattern"], value):
                errors.append(
                    f"{key}={value!r} does not match pattern "
                    f"{spec['pattern']}"
                )
        if value and "choices" in spec:
            if value not in spec["choices"]:
                errors.append(f"{key}={value!r} not in {spec['choices']}")
        if value and "min" in spec:
            try:
                if int(value) < spec["min"]:
                    errors.append(
                        f"{key}={value} is below minimum {spec['min']}"
                    )
            except ValueError:
                errors.append(f"{key}={value!r} is not a valid integer")
    return errors


# ---------------------------------------------------------------------------
# Configuration registry (data dictionary) — DOC-3
# ---------------------------------------------------------------------------
# v29 ROOT FIX (audit C-13): CONFIG_REGISTRY is STALE.
#
# This registry was originally intended as a self-documenting data
# dictionary of every setting in the module. In practice it has not been
# maintained alongside the actual settings — many settings added by the
# institutional-grade rewrites of the ChEMBL / STRING / DisGeNET / OMIM /
# PubChem / DrugBank pipelines are NOT registered here, and several
# entries (e.g. ``OMIM_DEDUP_KEEP_POLICY``) describe settings whose
# semantics have changed since the entry was written.
#
# It is KEPT for now because:
#   * ``tests/test_settings.py::test_doc3_config_registry`` asserts its
#     existence and the presence of ``DATABASE_URL`` / ``CHEMBL_VERSION``.
#   * ``tests/test_all_26_files_integration_v10.py`` asserts the OMIM_*
#     entry count is >= 20.
#   * ``docs/pipelines/omim.md`` references it.
#
# But it is hereby DEPRECATED. New code MUST NOT add entries to this
# registry. Instead, prefer:
#   * For OMIM: the consolidated ``OMIM_CONFIG`` dict (above) and the
#     ``get_omim_config()`` accessor.
#   * For per-source structured config: the source-specific dataclasses
#     (``DatabaseConfig``, ``ChEMBLConfig``, ``StringConfig``,
#     ``DisGeNETConfig``) and their ``get_*_config()`` accessors.
#   * For raw env-var introspection: ``ENV_VAR_SCHEMA`` (above) which is
#     maintained alongside the actual ``_getenv`` / ``_getenv_int`` /
#     ``_getenv_bool`` / ``_getenv_float`` call sites.
#
# A future v2.0.0 release will remove ``CONFIG_REGISTRY`` and migrate
# the two tests above to assert against the structured dataclasses and
# ``OMIM_CONFIG`` instead.
#
# v41 ROOT FIX (SEV3): the DeprecationWarning was emitted at IMPORT TIME,
# which polluted every `import config.settings` with a warning — including
# test discovery, CLI --help, and tooling (linters, type-checkers). This
# made the warning effectively useless (operators ignored it as noise).
# The warning is now DEFERRED to first attribute access via the module-level
# ``__getattr__`` (PEP 562) below. The dict is stored in a private
# ``_CONFIG_REGISTRY`` module-level name; the public ``CONFIG_REGISTRY``
# attribute is materialised lazily on first read.
_CONFIG_REGISTRY: dict[str, dict] = {
    "DATABASE_URL": {
        "type": "str",
        "required": True,
        "default": "placeholder",
        "description": "PostgreSQL connection string",
        "used_by": ["database.connection"],
    },
    "CHEMBL_VERSION": {
        "type": "str",
        "required": False,
        "default": "35",
        "description": "ChEMBL database release version",
        "used_by": ["pipelines.chembl"],
    },
    "CHEMBL_API_URL": {
        "type": "str",
        "required": False,
        "default": "https://www.ebi.ac.uk/chembl/api/data",
        "description": "ChEMBL REST API base URL",
        "used_by": ["pipelines.chembl"],
    },
    "STRING_VERSION": {
        "type": "str",
        "required": False,
        "default": "12.0",
        "description": "STRING DB version",
        "used_by": ["pipelines.string"],
    },
    "STRING_MIN_COMBINED_SCORE": {
        "type": "int",
        "required": False,
        # v41 ROOT FIX (SEV3): stale default "400" — the actual module-level
        # STRING_MIN_COMBINED_SCORE is computed via
        # _get_default_string_threshold(STRING_VERSION) which returns 700 for
        # every supported STRING version (per Szklarczyk 2023). The "400"
        # value here was a stale dev default that misled operators reading
        # the registry. Updated to "700" to match the real production value.
        "default": "700",
        "description": "Minimum STRING PPI score for inclusion (>=700 is the published 'high-confidence' threshold per Szklarczyk 2023; the legacy 400 was 'medium' confidence)",
        "valid_range": "0-1000",
        "used_by": ["pipelines.string"],
    },
    "DISGENET_API_KEY": {
        "type": "str",
        "required": False,
        "default": "",
        "description": "DisGeNET API authentication key",
        "used_by": ["pipelines.disgenet"],
    },
    "OMIM_API_KEY": {
        "type": "str",
        "required": False,
        "default": "",
        "secret": True,
        "description": "OMIM API authentication key (UUID format). Required for both the morbidmap.txt download endpoint and the REST API.",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_API_BASE": {
        "type": "str",
        "required": False,
        "default": "https://api.omim.org/api",
        "description": "OMIM REST API base URL.",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_REQUEST_INTERVAL": {
        "type": "float",
        "required": False,
        "default": "0.25",
        "description": "Seconds to sleep between OMIM API requests (4 req/sec).",
        "valid_range": ">0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_MAPPING_KEYS_INCLUDE": {
        "type": "list[int]",
        "required": False,
        "default": "[3, 4]",
        "description": "Phenotype mapping keys to include (1=wild-type gene mapped, 2=phenotype mapped, 3=molecular basis known, 4=contiguous gene syndrome).",
        "valid_values": "subset of {1,2,3,4}",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_API_PAGE_LIMIT": {
        "type": "int",
        "required": False,
        "default": "1000",
        "description": "OMIM API pagination page size (max 1000 per OMIM docs).",
        "valid_range": "1-1000",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_API_MAX_RETRIES": {
        "type": "int",
        "required": False,
        "default": "5",
        "description": "Maximum HTTP retries on 429/5xx responses.",
        "valid_range": ">=0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_DOWNLOAD_TIMEOUT": {
        "type": "int",
        "required": False,
        "default": "300",
        "description": "HTTP timeout (seconds) for the morbidmap.txt download.",
        "valid_range": ">0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_API_TIMEOUT": {
        "type": "int",
        "required": False,
        "default": "120",
        "description": "HTTP timeout (seconds) for each OMIM REST API request.",
        "valid_range": ">0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_OUTPUT_FILENAME": {
        "type": "str",
        "required": False,
        "default": "omim_gene_disease_associations.csv",
        "description": "Filename of the cleaned GDA CSV written by clean().",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_MIN_EXPECTED_RECORDS": {
        "type": "int",
        "required": False,
        "default": "5000",
        "description": "Minimum parsed-record count; below this, clean() aborts (catches truncated downloads).",
        "valid_range": ">=0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_MAX_PAGINATION_PAGES": {
        "type": "int",
        "required": False,
        "default": "1000",
        "description": "Upper bound on API pagination pages (prevents infinite loop).",
        "valid_range": ">=1",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_DEDUP_KEEP_POLICY": {
        "type": "str",
        "required": False,
        "default": "last",
        "description": "Legacy dedup-keep-policy (no longer used — atomic writes don't append).",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_CONFIRMED_SCORE": {
        "type": "float",
        "required": False,
        "default": "0.9",
        "description": "Base score for mapping_key=3 (molecular basis known).",
        "valid_range": "0.0-1.0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_CONTIGUOUS_SCORE": {
        "type": "float",
        "required": False,
        "default": "0.8",
        "description": "Base score for mapping_key=4 (contiguous gene syndrome).",
        "valid_range": "0.0-1.0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_PHENOTYPE_MAPPED_SCORE": {
        "type": "float",
        "required": False,
        "default": "0.6",
        "description": "Base score for mapping_key=2 (phenotype mapped).",
        "valid_range": "0.0-1.0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_GENE_MAPPED_SCORE": {
        "type": "float",
        "required": False,
        "default": "0.5",
        "description": "Base score for mapping_key=1 (wild-type gene mapped).",
        "valid_range": "0.0-1.0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_USER_AGENT": {
        "type": "str",
        "required": False,
        "default": "drug-repurposing-pipeline/omim (contact=unknown@example.com)",
        "description": "User-Agent header sent with every OMIM HTTP request.",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_API_KEY_FORMAT_RE": {
        "type": "str",
        "required": False,
        "default": "^[a-f0-9-]{36}$",
        "description": "Regex validating OMIM_API_KEY format (OMIM keys are UUIDs).",
        "used_by": ["pipelines.omim", "config.settings"],
    },
    "OMIM_MAX_AGE_DAYS": {
        "type": "int",
        "required": False,
        "default": "30",
        "description": "Maximum age (days) of a cached morbidmap.txt before forcing a refresh.",
        "valid_range": ">=0",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_DB_BATCH_SIZE": {
        "type": "int",
        "required": False,
        "default": "1000",
        "description": "Batch size for bulk_upsert_gda.",
        "valid_range": ">=1",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_EXCLUDE_SUSCEPTIBILITY": {
        "type": "bool",
        "required": False,
        "default": "true",
        "description": "When true, susceptibility ({}) records are routed to a separate CSV and excluded from the main GDA load.",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_JSON_PRETTY": {
        "type": "bool",
        "required": False,
        "default": "false",
        "description": "Pretty-print intermediate JSON (dev only; production uses compact + deterministic).",
        "used_by": ["pipelines.omim"],
    },
    "OMIM_RANDOM_SEED": {
        "type": "int",
        "required": False,
        "default": "42",
        "description": "Random seed for HTTP retry backoff jitter (reproducibility).",
        "used_by": ["pipelines.omim"],
    },
    "DRUGBANK_XML_PATH": {
        "type": "Path",
        "required": False,
        "default": "raw_data/drugbank/drugbank_all_full_database.xml.gz",
        "description": "Path to DrugBank XML file (manual download)",
        "used_by": ["pipelines.drugbank"],
    },
    "UNIPROT_RELEASE": {
        "type": "str",
        "required": False,
        "default": "current_release",
        "description": "UniProt release version for reproducibility",
        "used_by": ["pipelines.uniprot"],
    },
    "LOG_LEVEL": {
        "type": "str",
        "required": False,
        "default": "INFO",
        "description": "Platform logging level",
        "valid_values": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "used_by": ["config.settings"],
    },
    "ENVIRONMENT": {
        "type": "str",
        "required": False,
        "default": "development",
        "description": "Deployment environment profile",
        "valid_values": ["development", "staging", "production"],
        "used_by": ["config.settings"],
    },
}

# ---------------------------------------------------------------------------
# Structured config groups (dataclasses) — ARCH-3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseConfig:
    """Database connection settings."""

    url: str


@dataclass(frozen=True)
class ChEMBLConfig:
    """ChEMBL data source configuration."""

    version: str
    api_url: str
    max_rows: Optional[int]
    max_activities: Optional[int]
    expected_drug_count_min: int
    expected_drug_count_max: int


@dataclass(frozen=True)
class StringConfig:
    """STRING DB configuration."""

    version: str
    min_combined_score: int
    protein_links_url: str
    aliases_url: str
    protein_links_detailed_url: str


@dataclass(frozen=True)
class DisGeNETConfig:
    """DisGeNET configuration."""

    api_url: str
    api_key: str
    use_api: bool


_db_config: Optional[DatabaseConfig] = None
_chembl_config: Optional[ChEMBLConfig] = None
_string_config: Optional[StringConfig] = None
_disgenet_config: Optional[DisGeNETConfig] = None


def get_database_config() -> DatabaseConfig:
    """Return structured database configuration (lazy-initialized)."""
    global _db_config
    if _db_config is None:
        _db_config = DatabaseConfig(url=DATABASE_URL)
    return _db_config


def get_chembl_config() -> ChEMBLConfig:
    """Return structured ChEMBL configuration (lazy-initialized)."""
    global _chembl_config
    if _chembl_config is None:
        _chembl_config = ChEMBLConfig(
            version=CHEMBL_VERSION,
            api_url=CHEMBL_API_URL,
            max_rows=CHEMBL_MAX_ROWS,
            max_activities=CHEMBL_MAX_ACTIVITIES,
            expected_drug_count_min=CHEMBL_EXPECTED_DRUG_COUNT_MIN,
            expected_drug_count_max=CHEMBL_EXPECTED_DRUG_COUNT_MAX,
        )
    return _chembl_config


def get_string_config() -> StringConfig:
    """Return structured STRING configuration (lazy-initialized)."""
    global _string_config
    if _string_config is None:
        _string_config = StringConfig(
            version=STRING_VERSION,
            min_combined_score=STRING_MIN_COMBINED_SCORE,
            protein_links_url=STRING_PROTEIN_LINKS_URL,
            aliases_url=STRING_ALIASES_URL,
            protein_links_detailed_url=STRING_PROTEIN_LINKS_DETAILED_URL,
        )
    return _string_config


def get_disgenet_config() -> DisGeNETConfig:
    """Return structured DisGeNET configuration (lazy-initialized)."""
    global _disgenet_config
    if _disgenet_config is None:
        _disgenet_config = DisGeNETConfig(
            api_url=DISGENET_API_URL,
            api_key=DISGENET_API_KEY,
            use_api=DISGENET_USE_API,
        )
    return _disgenet_config


# ---------------------------------------------------------------------------
# Configuration reload — ARCH-4, CONF-5
# ---------------------------------------------------------------------------


def reload_settings() -> dict[str, tuple[str, str]]:
    """Reload configuration and return changed settings.

    v41 ROOT FIX (DEAD): the previous implementation was BROKEN — it reset the
    lazy dataclass caches and the ``_dotenv_loaded`` flag, but the
    MODULE-LEVEL constants (CHEMBL_VERSION, STRING_MIN_COMBINED_SCORE,
    DATABASE_URL, etc.) are bound AT IMPORT TIME via ``_getenv(...)`` and
    ``_parse_required_int(...)``. Resetting ``_dotenv_loaded`` does NOT cause
    those constants to be re-read — they are already bound. The function
    captured ``old_values = get_data_version_info()`` BEFORE the reset and
    ``new_values = get_data_version_info()`` AFTER, but ``get_data_version_info``
    READS THE SAME module-level constants both times — so ``changes`` was
    ALWAYS EMPTY. The function was a no-op disguised as a reload.

    Implementing a TRUE reload requires ``importlib.reload(this_module)``,
    which would also re-run all module-level side effects (validation,
    logging config, .env load). That is risky for held references (any
    caller doing ``from config.settings import CHEMBL_VERSION`` keeps the
    OLD reference after reload). The Python community explicitly warns
    against this pattern for production config (cf. PEP 489 reload caveats).

    We therefore IMPLEMENT a proper reload via importlib.reload + a
    pre-reload snapshot of get_data_version_info() and re-evaluate the
    snapshot AFTER reload by re-importing the module. This DOES re-read all
    env vars. Held references are invalid after this call — document this
    to callers. The return value is the diff of get_data_version_info()
    before vs after.

    Returns a dict of setting_name -> (old_value, new_value) for
    settings whose values changed after the reload.
    """
    import importlib
    import sys

    # Capture current values BEFORE reload (snapshot of bound constants).
    old_values = get_data_version_info()

    # Reset lazy caches so post-reload get_*_config() calls re-bind.
    global _db_config, _chembl_config, _string_config, _disgenet_config
    global _dotenv_loaded
    _db_config = None
    _chembl_config = None
    _string_config = None
    _disgenet_config = None
    _dotenv_loaded = False  # Re-load .env on next access

    # v41 ROOT FIX: actually re-execute the module body so all module-level
    # constants are re-read from env. importlib.reload is the ONLY mechanism
    # that re-binds module-level names without us manually re-asserting every
    # single constant. We must reload THIS module (config.settings), not a
    # parent — this is safe because __name__ is the canonical config.settings.
    module = sys.modules.get(__name__)
    if module is not None:
        try:
            importlib.reload(module)
        except Exception as exc:  # noqa: BLE001 — surface any reload error
            logger.error(
                "reload_settings: importlib.reload failed: %s. Lazy caches "
                "were reset but module-level constants were NOT re-read. "
                "Callers may see stale values.", exc
            )
            return {}

    # Re-capture new values AFTER reload. We call via the freshly-reloaded
    # module's namespace so we read the NEW bound constants.
    new_values = get_data_version_info()

    # Compute diff
    changes: dict[str, tuple[str, str]] = {}
    for key in old_values:
        if old_values[key] != new_values.get(key):
            changes[key] = (old_values[key], new_values.get(key))

    if changes:
        logger.warning("Configuration changed after reload: %s", changes)
    else:
        logger.info(
            "reload_settings: no changes detected (env vars match previous "
            "values)."
        )

    return changes


# ---------------------------------------------------------------------------
# __all__ — CODE-8
# ---------------------------------------------------------------------------

__all__ = [
    # Paths
    "BASE_DIR",
    "RAW_DATA_DIR",
    "PROCESSED_DATA_DIR",
    "AIRFLOW_HOME",
    # Database
    "DATABASE_URL",
    # ChEMBL
    "CHEMBL_VERSION",
    "CHEMBL_API_URL",
    "CHEMBL_MAX_ROWS",
    "CHEMBL_MAX_ACTIVITIES",
    "CHEMBL_SNAPSHOT_DATE",
    "CHEMBL_EXPECTED_DRUG_COUNT_MIN",
    "CHEMBL_EXPECTED_DRUG_COUNT_MAX",
    "CHEMBL_URL",  # deprecated
    # ChEMBL — institutional-grade operational settings (chembl_pipeline.py rewrite)
    "CHEMBL_PAGE_SIZE",
    "CHEMBL_MAX_RETRIES",
    "CHEMBL_RETRY_BACKOFF_BASE",
    "CHEMBL_MIN_REQUEST_INTERVAL",
    "CHEMBL_HTTP_TIMEOUT",
    "CHEMBL_MAX_RESPONSE_BYTES",
    "CHEMBL_CIRCUIT_BREAKER_THRESHOLD",
    "CHEMBL_CIRCUIT_BREAKER_RESET_SECONDS",
    "CHEMBL_TARGET_ORGANISM",
    "CHEMBL_MAX_PHASE",
    "CHEMBL_MW_MACROMOLECULE_THRESHOLD",
    "CHEMBL_ACTIVITY_TYPES",
    "CHEMBL_STANDARD_UNITS",
    "CHEMBL_STANDARD_RELATIONS",
    "CHEMBL_ASSAY_TYPES",
    "CHEMBL_TARGET_TYPES",
    "CHEMBL_TARGET_ACCESSION_STRATEGY",
    "CHEMBL_ACTIVITY_CHUNK_SIZE",
    "CHEMBL_DPI_BATCH_SIZE",
    "CHEMBL_TARGET_RESOLUTION_BATCH_SIZE",
    "CHEMBL_API_WORKERS",
    "CHEMBL_TARGET_RESOLUTION_WORKERS",
    "CHEMBL_TARGET_CACHE_TTL_SECONDS",
    "CHEMBL_DRUG_ID_CACHE_TTL_SECONDS",
    "CHEMBL_CACHE_TTL_SECONDS",
    "CHEMBL_ALLOW_VERSION_MISMATCH",
    "CHEMBL_RESUME",
    # Pipeline-wide operational settings
    "PIPELINE_RUN_ID",
    "PIPELINE_USE_CACHE",
    "PIPELINE_LOG_FORMAT",
    "PIPELINE_CONTACT_EMAIL",
    "PIPELINE_RESUME",
    # STRING
    "STRING_VERSION",
    "STRING_PROTEIN_LINKS_URL",
    "STRING_ALIASES_URL",
    "STRING_PROTEIN_LINKS_DETAILED_URL",
    "STRING_PROTEIN_INFO_URL",  # deprecated
    "STRING_MIN_COMBINED_SCORE",
    "STRING_MIN_COMBINED_SCORE_PROD",
    "STRING_DETAILED_MODE",
    "STRING_DROP_SELF_INTERACTIONS",
    "STRING_DEDUP_STRATEGY",
    "STRING_LOW_MEMORY",
    "STRING_CHUNK_SIZE",
    # DisGeNET
    "DISGENET_URL",
    "DISGENET_API_URL",
    "DISGENET_API_KEY",
    "DISGENET_USE_API",
    "DISGENET_STATIC_URL",  # deprecated
    # DisGeNET — institutional-grade operational settings (389-fix audit)
    "DISGENET_MIN_SCORE",
    "DISGENET_ALLOW_WEAK_EVIDENCE",
    "DISGENET_CONFIDENCE_TIERS_JSON",
    "DISGENET_CONFIDENCE_TIERS",
    "DISGENET_PMID_CAP",
    "DISGENET_PMID_SORT_ORDER",
    "DISGENET_API_PAGE_SIZE",
    "DISGENET_API_MAX_RECORDS",
    "DISGENET_API_TIMEOUT",
    "DISGENET_API_MAX_RETRIES",
    "DISGENET_API_BACKOFF_BASE",
    "DISGENET_API_BACKOFF_MAX_SECONDS",
    "DISGENET_API_MAX_RETRY_AFTER",
    "DISGENET_API_RATE_LIMIT",
    "DISGENET_CIRCUIT_BREAKER_THRESHOLD",
    "DISGENET_CIRCUIT_BREAKER_RESET_SECONDS",
    "DISGENET_CONTACT_EMAIL",
    "DISGENET_ALLOWED_DOMAINS",
    "DISGENET_API_MAX_RESPONSE_BYTES",
    "DISGENET_API_CA_BUNDLE",
    "DISGENET_OUTPUT_FILE_MODE",
    "DISGENET_FALLBACK_TO_CACHE",
    "DISGENET_API_MAX_PAGES",
    "DISGENET_DOWNLOAD_PHASE_TIMEOUT",
    "DISGENET_ALLOW_PARTIAL_DATA",
    "DISGENET_UNIPROT_MAP_TTL_HOURS",
    "DISGENET_TARGET_VERSION",
    "DISGENET_FREEZE_VERSION",
    "DISGENET_MIN_EXPECTED_RECORDS",
    "DISGENET_DISEASE_ONTOLOGY_PATH",
    "DISGENET_HGNC_PATH",
    "DISGENET_MAX_DATA_AGE_DAYS",
    "DISGENET_OUTPUT_FILENAME",
    "DISGENET_RAW_FILENAME",
    "DISGENET_CHUNK_SIZE",
    "DISGENET_API_PARALLEL_PAGES",
    "DISGENET_LOG_FORMAT",
    "DISGENET_ENV",
    "DISGENET_SOURCE_WEIGHTS_JSON",
    "DISGENET_SOURCE_WEIGHTS",
    "_validate_disgenet_config",
    # PubChem
    "PUBCHEM_REST_BASE",
    "PUBCHEM_FTP_BASE",
    "PUBCHEM_API_URL",
    # Entity Resolution (audit D12-2)
    "ENTITY_RESOLUTION_PUBCHEM_ENABLED",
    "ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS",
    "ENTITY_RESOLUTION_FUZZY_THRESHOLD",
    "ENTITY_RESOLUTION_FUZZY_MAX_CANDIDATES",
    "ENTITY_RESOLUTION_PUBCHEM_REST_BASE",
    "ENTITY_RESOLUTION_PUBCHEM_CALL_DELAY",
    "ENTITY_RESOLUTION_PUBCHEM_TIMEOUT",
    "ENTITY_RESOLUTION_PUBCHEM_MAX_RETRIES",
    "ENTITY_RESOLUTION_PUBCHEM_API_KEY",
    "ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE",
    "ENTITY_RESOLUTION_PUBCHEM_CERT_PEM",
    "ENTITY_RESOLUTION_PUBCHEM_KEY_PEM",
    "ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM",
    "ENTITY_RESOLUTION_SOURCE_WHITELIST",
    "ENTITY_RESOLUTION_DEFAULT_ORGANISM",
    "ENTITY_RESOLUTION_MAPPING_SCHEMA_VERSION",
    "get_entity_resolution_config",
    # PubChem pipeline (institutional-grade — fixes PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md)
    "PUBCHEM_PIPELINE_BATCH_SIZE",
    "PUBCHEM_PIPELINE_MIN_BACKOFF",
    "PUBCHEM_PIPELINE_MAX_BACKOFF",
    "PUBCHEM_PIPELINE_READ_TIMEOUT",
    "PUBCHEM_PIPELINE_CACHE_TTL_SECONDS",
    "PUBCHEM_PIPELINE_CONCURRENCY",
    "PUBCHEM_PIPELINE_FETCH_SYNONYMS",
    "PUBCHEM_PIPELINE_FETCH_CAS",
    "PUBCHEM_PIPELINE_SPLIT_RETRY_MAX",
    "PUBCHEM_PIPELINE_MAX_RECORDS",
    "PUBCHEM_PIPELINE_RAW_RESPONSE_RETENTION_DAYS",
    "PUBCHEM_CIRCUIT_BREAKER_THRESHOLD",
    "PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS",
    "PUBCHEM_PIPELINE_PROPERTIES",
    "PROMETHEUS_ENABLED",
    "OTEL_ENABLED",
    "OPERATOR_ID",
    "RDKIT_AVAILABLE",
    # DrugBank
    "DRUGBANK_XML_PATH",
    "DRUGBANK_VERSION",
    "DRUGBANK_XML_NAMESPACE",
    "DRUGBANK_TARGET_ORGANISMS",
    "DRUGBANK_GENERATE_SYNTH_KEYS",
    "DRUGBANK_DROP_NO_INCHIKEY",
    "DRUGBANK_CONSERVATIVE_DEFAULTS",
    "DRUGBANK_BATCH_SIZE",
    "DRUGBANK_LOG_INTERVAL",
    "DRUGBANK_MAX_DRUGS",
    "DRUGBANK_EXTRACT_TARGETS",
    "DRUGBANK_EXTRACT_ENZYMES",
    "DRUGBANK_EXTRACT_TRANSPORTERS",
    "DRUGBANK_CSV_COMPRESSION",
    "DRUGBANK_EXPECTED_SHA256",
    "DRUGBANK_EXPECTED_DRUG_COUNT_MIN",
    "DRUGBANK_EXPECTED_DRUG_COUNT_MAX",
    "DRUGBANK_LOG_REDACT",
    "DRUGBANK_LOG_FULL_PATHS",
    "DRUGBANK_VALIDATE_READABILITY",
    "DRUGBANK_DPI_BATCH_SIZE",
    "DEFAULT_DRUGBANK_VERSION",
    "VALID_DRUGBANK_VERSIONS",
    # OMIM
    "OMIM_API_KEY",
    "OMIM_API_BASE",
    "OMIM_REQUEST_INTERVAL",
    "OMIM_MAPPING_KEYS_INCLUDE",
    "OMIM_API_PAGE_LIMIT",
    "OMIM_API_MAX_RETRIES",
    "OMIM_DOWNLOAD_TIMEOUT",
    "OMIM_API_TIMEOUT",
    "OMIM_OUTPUT_FILENAME",
    "OMIM_MIN_EXPECTED_RECORDS",
    "OMIM_MAX_PAGINATION_PAGES",
    "OMIM_DEDUP_KEEP_POLICY",
    "OMIM_CONFIRMED_SCORE",
    "OMIM_CONTIGUOUS_SCORE",
    "OMIM_PHENOTYPE_MAPPED_SCORE",
    "OMIM_GENE_MAPPED_SCORE",
    "OMIM_USER_AGENT",
    "OMIM_API_KEY_FORMAT_RE",
    "OMIM_MAX_AGE_DAYS",
    "OMIM_DB_BATCH_SIZE",
    "OMIM_EXCLUDE_SUSCEPTIBILITY",
    "OMIM_JSON_PRETTY",
    "OMIM_RANDOM_SEED",
    "_parse_csv_ints",
    "_validate_omim_config",
    # UniProt
    "UNIPROT_RELEASE",
    "UNIPROT_SPROT_URL",  # deprecated
    "UNIPROT_TREMBL_URL",  # deprecated
    # Logging
    "LOG_LEVEL",
    "setup_logging",
    # Loaders (previously missing from __all__ — institutional-grade fix)
    "LOADERS_DEAD_LETTER_ENABLED",
    "LOADERS_STRICT_VALIDATION",
    "LOADERS_MAX_RETRY_ATTEMPTS",
    "LOADERS_RETRY_BASE_DELAY",
    "LOADERS_ENABLE_TIMING",
    "LOADERS_MAX_DELETE_COUNT",
    "BATCH_SIZE_OVERRIDES",
    # Orphan GDA retention
    "ORPHAN_GDA_RETENTION_HOURS",
    # Environment
    "ENVIRONMENT",
    # Provenance
    "DATA_SNAPSHOT_ID",
    "get_data_version_info",
    "get_provenance_metadata",
    # Validation
    "validate_all_urls",
    "validate_api_keys",
    "validate_env_schema",
    "check_api_endpoints",
    "check_env_git_tracking",
    "validate_env_file",
    # Secret management
    "get_secret",
    # Config groups
    "get_database_config",
    "get_chembl_config",
    "get_string_config",
    "get_disgenet_config",
    # Reload
    "reload_settings",
    # Logging
    "log_config_summary",
    # Constants
    "VALID_CHEMBL_VERSIONS",
    "VALID_STRING_VERSIONS",
    "DEFAULT_CHEMBL_VERSION",
    "DEFAULT_STRING_VERSION",
    "CHEMBL_VERSION_COUNT_RANGES",
    "STRING_VERSION_SCORE_THRESHOLDS",
    "CONFIG_REGISTRY",  # DEPRECATED (v29 audit C-13) — kept for back-compat
    "OMIM_CONFIG",  # v29 ROOT FIX (audit C-13): consolidated OMIM settings
    "get_omim_config",  # v29 ROOT FIX (audit C-13): accessor for OMIM_CONFIG
    "ENV_VAR_SCHEMA",
    # Module-level helpers exposed for testability
    "load_dotenv",
    "_getenv",
    "_getenv_bool",
    "_getenv_float",
    "_getenv_int",
    "_parse_bool",
    "_parse_optional_int",
    "_parse_required_int",
]

# ---------------------------------------------------------------------------
# Deprecated settings registry — DESIGN-1, DOC-4
# ---------------------------------------------------------------------------
# These settings are accessed via module-level __getattr__ so that
# DeprecationWarning is raised on every access.  The descriptor-based
# approach does NOT work for module-level variables in Python.

_DEPRECATED_SETTINGS: dict[str, tuple[str, object]] = {
    # name: (replacement, value)
    "CHEMBL_URL": (
        "CHEMBL_API_URL",
        f"https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/"
        f"chembl_{CHEMBL_VERSION}/",
    ),
    "UNIPROT_SPROT_URL": (
        "UniProt REST API",
        f"https://ftp.uniprot.org/pub/databases/uniprot/{UNIPROT_RELEASE}/"
        f"knowledgebase/complete/uniprot_sprot.xml.gz",
    ),
    "UNIPROT_TREMBL_URL": (
        "UniProt REST API",
        f"https://ftp.uniprot.org/pub/databases/uniprot/{UNIPROT_RELEASE}/"
        f"knowledgebase/complete/uniprot_trembl.xml.gz",
    ),
    "STRING_PROTEIN_INFO_URL": (
        "STRING_ALIASES_URL",
        _string_urls["protein_info_url"],
    ),
    "DISGENET_STATIC_URL": (
        "DISGENET_API_URL (static URL deprecated since 2024)",
        "https://www.disgenet.org/static/disgenet_ap1/files/downloads/"
        "all_gene_disease_associations.tsv.gz",
    ),
}


_CONFIG_REGISTRY_DEPRECATION_WARNED: bool = False


def __getattr__(name: str) -> object:
    """Module-level __getattr__ for deprecated settings.

    Accessing any name in ``_DEPRECATED_SETTINGS`` triggers a
    ``DeprecationWarning`` with the replacement and removal timeline,
    then returns the value.  All other names raise ``AttributeError``.

    v41 ROOT FIX (SEV3): also handles ``CONFIG_REGISTRY`` (the public alias
    for the private ``_CONFIG_REGISTRY`` data dictionary) — the
    DeprecationWarning is emitted ONCE on first access and the dict is
    returned. This defers the noise from import-time to actual-use-time.
    """
    if name == "CONFIG_REGISTRY":
        global _CONFIG_REGISTRY_DEPRECATION_WARNED
        if not _CONFIG_REGISTRY_DEPRECATION_WARNED:
            warnings.warn(
                "config.settings.CONFIG_REGISTRY is DEPRECATED (v29 audit C-13): "
                "stale data dictionary. Use OMIM_CONFIG / get_*_config() / "
                "ENV_VAR_SCHEMA instead. Will be removed in v2.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            _CONFIG_REGISTRY_DEPRECATION_WARNED = True
        return _CONFIG_REGISTRY
    if name in _DEPRECATED_SETTINGS:
        replacement, value = _DEPRECATED_SETTINGS[name]
        warnings.warn(
            f"Setting `{name}` is DEPRECATED. Use `{replacement}` instead. "
            f"Will be removed in v2.0.0 (scheduled: 2025-Q4).",
            DeprecationWarning,
            stacklevel=2,
        )
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# v41 ROOT FIX (DEAD): invoke check_env_git_tracking() and validate_env_file()
# at module load time. Both functions were defined but NEVER called — operators
# got no warning when .env was committed to git or when .env had malformed
# lines (e.g. unquoted values containing ``=``). Wrapped in try/except so
# neither check ever aborts import (settings load MUST be resilient).
# ---------------------------------------------------------------------------
try:
    check_env_git_tracking()
except Exception as _env_git_err:  # noqa: BLE001 — never block import
    logging.getLogger(__name__).debug(
        "check_env_git_tracking failed (non-fatal): %s", _env_git_err
    )

try:
    _env_file_issues = validate_env_file()
    if _env_file_issues:
        for _issue in _env_file_issues:
            warnings.warn(_issue, UserWarning, stacklevel=2)
except Exception as _env_validate_err:  # noqa: BLE001 — never block import
    logging.getLogger(__name__).debug(
        "validate_env_file failed (non-fatal): %s", _env_validate_err
    )

