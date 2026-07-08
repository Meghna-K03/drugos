# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
"""Type stubs for the pipelines package (PEP 561).

This file provides static type information for every public symbol in
``pipelines.__all__``. It mirrors the lazy-loading ``__getattr__`` pattern
in ``pipelines/__init__.py`` but uses direct re-exports for type-checker
consumption.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Re-exported classes (8)
# ---------------------------------------------------------------------------
from pipelines.base_pipeline import BasePipeline as BasePipeline
from pipelines.chembl_pipeline import ChEMBLPipeline as ChEMBLPipeline
from pipelines.disgenet_pipeline import DisGeNETPipeline as DisGeNETPipeline
from pipelines.drugbank_pipeline import DrugBankPipeline as DrugBankPipeline
from pipelines.omim_pipeline import OMIMPipeline as OMIMPipeline
from pipelines.pubchem_pipeline import PubChemPipeline as PubChemPipeline
from pipelines.string_pipeline import StringPipeline as StringPipeline
from pipelines.uniprot_pipeline import UniProtPipeline as UniProtPipeline

# ---------------------------------------------------------------------------
# Re-exported constants
# ---------------------------------------------------------------------------
from pipelines.chembl_pipeline import (
    ACTIVITY_CHUNK_SIZE as ACTIVITY_CHUNK_SIZE,
    CHEMBL_API_BASE as CHEMBL_API_BASE,
    CHEMBL_MIN_REQUEST_INTERVAL as CHEMBL_MIN_REQUEST_INTERVAL,
    MOLECULE_TYPE_MAP as MOLECULE_TYPE_MAP,
    RETRY_BACKOFF as RETRY_BACKOFF,
    _LOWER_TYPE_MAP as _LOWER_TYPE_MAP,
)
from pipelines.disgenet_pipeline import (
    CONFIDENCE_TIERS as CONFIDENCE_TIERS,
    DISGENET_API_COLUMN_MAP as DISGENET_API_COLUMN_MAP,
    DISGENET_COLUMN_MAP as DISGENET_COLUMN_MAP,
    MIN_SCORE as MIN_SCORE,
)
from pipelines.drugbank_pipeline import NS as NS
from pipelines.omim_pipeline import (
    MAPPING_KEY_CONFIRMED as MAPPING_KEY_CONFIRMED,
    OMIM_REQUEST_INTERVAL as OMIM_REQUEST_INTERVAL,
)
from pipelines.pubchem_pipeline import (
    BATCH_SIZE as BATCH_SIZE,
    MAX_BACKOFF as MAX_BACKOFF,
    MIN_BACKOFF as MIN_BACKOFF,
    PUBCHEM_PROPERTIES as PUBCHEM_PROPERTIES,
    RATE_LIMIT_INTERVAL as RATE_LIMIT_INTERVAL,
)
from pipelines.uniprot_pipeline import (
    UNIPROT_FIELDS as UNIPROT_FIELDS,
    UNIPROT_SEARCH_URL as UNIPROT_SEARCH_URL,
)

# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------
__version__: str
SCHEMA_VERSION: str
PYTHON_MIN_VERSION: tuple[int, int]
DEFAULT_SEED: int
KNOWN_DATA_SOURCE_VERSIONS: list[str]
DATA_DICTIONARY: dict[str, dict[str, Any]]
SOURCE_ATTRIBUTION: dict[str, dict[str, Any]]

# ---------------------------------------------------------------------------
# Factory & introspection
# ---------------------------------------------------------------------------
def get_pipeline(name: str) -> type: ...
def get_expected_pipelines() -> set[str]: ...
def get_kg_mapping() -> dict[str, dict[str, list[str]]]: ...
def get_filtering_thresholds() -> dict[str, dict[str, Any]]: ...
def get_data_dictionary() -> dict[str, dict[str, Any]]: ...
def get_source_attribution() -> dict[str, dict[str, Any]]: ...
def find_affected_downstream(source_name: str) -> list[str]: ...
def compute_file_checksum(path: str | Path) -> str: ...
def get_json_schema() -> dict[str, Any]: ...

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_infrastructure() -> dict[str, Any]: ...
def _validate_security() -> dict[str, Any]: ...
def validate_config() -> dict[str, Any]: ...

# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------
def get_config_summary() -> dict[str, Any]: ...
def set_log_level(level: int | str) -> None: ...
def set_correlation_id(cid: Optional[str]) -> None: ...
def get_correlation_id() -> Optional[str]: ...
def set_seed(seed: int) -> None: ...

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def initialize() -> None: ...
def reload() -> None: ...
def is_loaded() -> bool: ...
def is_reproducible() -> bool: ...

# ---------------------------------------------------------------------------
# Health & metrics
# ---------------------------------------------------------------------------
def health_check() -> dict[str, Any]: ...
def get_metrics() -> dict[str, Any]: ...
def get_load_times() -> dict[str, float]: ...
def performance_benchmark() -> dict[str, Any]: ...
def recover_from_failure() -> None: ...
def get_dead_letters() -> list[dict[str, Any]]: ...

# ---------------------------------------------------------------------------
# Lineage & state
# ---------------------------------------------------------------------------
def get_provenance() -> dict[str, Any]: ...
def get_audit_trail() -> dict[str, Any]: ...
def to_state_dict() -> dict[str, Any]: ...
def from_state_dict(state: dict[str, Any]) -> None: ...

# ---------------------------------------------------------------------------
# Versioning & deprecation
# ---------------------------------------------------------------------------
def requires_api_version(min_version: str) -> None: ...
def _deprecated(name: str, removal_version: str, alternative: str) -> None: ...

# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------
def _reset() -> None: ...
def _log_import_status() -> dict[str, bool]: ...

# ---------------------------------------------------------------------------
# Sentinel class for graceful degradation
# ---------------------------------------------------------------------------
class _PipelineUnavailable:
    name: str
    original_error: ImportError
    def __init__(self, name: str, original_error: ImportError) -> None: ...
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...
    def __repr__(self) -> str: ...

# ---------------------------------------------------------------------------
# Module-level state (exposed for type-checkers)
# ---------------------------------------------------------------------------
logger: logging.Logger
_loaded: dict[str, Any]
_load_times: dict[str, float]
_dead_letters: list[dict[str, Any]]
_correlation_id: Optional[str]
_LAZY_MODE: bool

__all__: list[str]
