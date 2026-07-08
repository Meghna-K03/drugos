"""Type stubs for the cleaning package (v3.0.0)."""

from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, Union, Iterable, Iterator, Literal, NamedTuple
from pathlib import Path
import enum

import pandas as pd

# Version
__version__: str

# Constants
ALLOWED_TYPES: list[str]
FUZZY_THRESHOLD: float
UNIT_CONVERSIONS: dict[str, float]
MAX_SEQUENCE_LENGTH: int
WITHDRAWN_GROUP_KEYWORDS: tuple[str, ...]
STEREO_POLICY: Literal["preserve", "ignore"]
RECORD_SCHEMA: dict[str, Any]

# Normalizer — original v1.0.0 public API
def convert_to_inchikey(
    smiles: Union[str, bytes, Any, None],
    *,
    options: Optional[str] = ...,
    standard: bool = ...,
    timeout: Optional[float] = ...,
    raise_on_error: bool = ...,
) -> Optional[str]: ...
def standardize_inchikey(raw_inchikey: Union[str, bytes, None]) -> Optional[str]: ...
def standardize_drug_record(
    record: dict,
    *,
    required_keys: Optional[Tuple[str, ...]] = ...,
    known_inchikeys: Optional[set[str]] = ...,
    seen_inchikeys: Optional[set[str]] = ...,
    mw_range: Optional[Tuple[float, float]] = ...,
    source: Optional[str] = ...,
    operator_id: Optional[str] = ...,
    source_dataset_id: Optional[str] = ...,
    raise_on_error: bool = ...,
) -> dict: ...
def normalize_activity_value(
    value: Any,
    units: Any,
    *,
    activity_type: Optional[str] = ...,
    temperature_c: Optional[float] = ...,
    raise_on_error: bool = ...,
) -> Any: ...

# Normalizer — v2.1.0 new public API
def convert_to_inchikey_detailed(
    smiles: Union[str, bytes, Any, None],
    *,
    options: Optional[str] = ...,
    standard: bool = ...,
    timeout: Optional[float] = ...,
    raise_on_error: bool = ...,
    activity_type: Optional[str] = ...,
) -> Any: ...
def convert_to_inchikeys(
    smiles_list: Iterable[Union[str, bytes, None]],
    *,
    options: Optional[str] = ...,
    standard: bool = ...,
    timeout: Optional[float] = ...,
    max_workers: Optional[int] = ...,
) -> List[Optional[str]]: ...
def normalize_inchikey(raw: Union[str, bytes, None]) -> Optional[str]: ...
def validate_inchikey(key: str, *, strict: bool = ...) -> bool: ...
def is_valid_inchikey(key: str) -> bool: ...
def is_synthetic_inchikey(inchikey: str) -> bool: ...
def fuzzy_match_drug_type(raw_type: Any) -> str: ...
def fuzzy_match_drug_types(raw_types: Iterable[Any]) -> List[str]: ...
def standardize_drug_records_batch(
    records: List[dict],
    *,
    checkpoint_every: int = ...,
    on_checkpoint: Optional[Callable[[int, List[dict]], None]] = ...,
    **kwargs: Any,
) -> Dict[str, List[dict]]: ...
def standardize_drug_records_chunked(
    records: Iterable[dict],
    chunk_size: int = ...,
    on_chunk: Optional[Callable[[int, int], None]] = ...,
    **kwargs: Any,
) -> Iterator[dict]: ...
def normalize_activity_values(
    values: Iterable[Any],
    units: Union[Iterable[str], str],
    *,
    activity_type: Optional[str] = ...,
    temperature_c: Optional[float] = ...,
) -> List[Any]: ...
def refresh_capabilities() -> None: ...
def get_dq_counts() -> Dict[str, int]: ...
def reset_dq_counts() -> None: ...
def get_cache_info() -> Dict[str, Any]: ...
def configure_normalizer(
    *,
    fuzzy_threshold: Optional[float] = ...,
    fuzzy_scorer: Optional[str] = ...,
    allowed_types: Optional[List[str]] = ...,
    unit_conversions: Optional[Dict[str, float]] = ...,
    stereo_policy: Optional[Literal["preserve", "ignore"]] = ...,
    rate_limit_per_sec: Optional[float] = ...,
    openlineage_emitter: Optional[Callable[[dict], None]] = ...,
) -> None: ...
def save_config(path: str) -> None: ...
def load_config(path: str) -> None: ...
def watch_config(path: str, interval_sec: float = ...) -> None: ...
def validate_config() -> List[str]: ...
def requires_api_version(min_version: str) -> None: ...
def is_backfill_needed(old_rule_version: str, new_rule_version: str) -> bool: ...
def sign_output(record: dict, signer_id: str) -> dict: ...
def get_validation_status() -> Dict[str, Any]: ...

# Normalizer — types
class ActivityValue(tuple):
    value: Optional[float]
    unit: str
    original_value: Any
    original_unit: Optional[str]
    conversion_factor: Optional[float]
    censored: bool
    censor_direction: Optional[str]
    activity_type: Optional[str]
    temperature_c: Optional[float]
    warnings: Tuple[str, ...]
    def __new__(
        cls,
        value: Optional[float],
        unit: str,
        *,
        original_value: Any = ...,
        original_unit: Optional[str] = ...,
        conversion_factor: Optional[float] = ...,
        censored: bool = ...,
        censor_direction: Optional[str] = ...,
        activity_type: Optional[str] = ...,
        temperature_c: Optional[float] = ...,
        warnings: Tuple[str, ...] = ...,
    ) -> "ActivityValue": ...

class ConversionResult(NamedTuple):
    success: bool
    inchikey: Optional[str]
    error: Optional[str] = ...
    error_category: Optional[str] = ...
    smiles_hash: Optional[str] = ...
    canonical_smiles: Optional[str] = ...
    potential_collision: bool = ...
    stereo_ambiguous: bool = ...
    rdkit_version: str = ...

# Deduplicator functions — v3.0.0
def dedup_by_inchikey(
    df: pd.DataFrame,
    *,
    reset_index: bool = ...,
    return_result: bool = ...,
    conservative_defaults: bool = ...,
    merge_fields: bool = ...,
    keep_lineage_columns: bool = ...,
    validate_inchikeys: bool = ...,
    auto_standardize: bool = ...,
    synth_handling: Literal["strict", "by_name", "skip"] = ...,
    weight: Optional["CompletenessWeight"] = ...,
    dedup_by_version_char: bool = ...,
    null_inchikey_handler: Literal["keep_all", "drop", "quarantine"] = ...,
    skip_if_already_deduped: bool = ...,
    max_duplicate_ratio: Optional[float] = ...,
    source: Optional[str] = ...,
    operator_id: Optional[str] = ...,
    source_dataset_id: Optional[str] = ...,
) -> Union[pd.DataFrame, "DedupResult"]: ...
def dedup_interactions(
    df: pd.DataFrame,
    keys: Optional[List[str]] = ...,
    *,
    activity_type_column: Optional[str] = ...,
    activity_value_column: Optional[str] = ...,
    activity_units_column: Optional[str] = ...,
    confidence_column: Optional[str] = ...,
    direction: Literal["asc", "desc", "auto"] = ...,
    keep: Literal["best", "first", "last", "mark"] = ...,
    segment_by_activity_type: bool = ...,
    normalize_units: bool = ...,
    handle_censored: bool = ...,
    null_keys_handler: Literal["keep_all", "drop", "quarantine"] = ...,
    strict_activity_type: bool = ...,
    reset_index: bool = ...,
    return_result: bool = ...,
    conservative_defaults: bool = ...,
    keep_lineage_columns: bool = ...,
    skip_if_already_deduped: bool = ...,
    max_duplicate_ratio: Optional[float] = ...,
    source: Optional[str] = ...,
    operator_id: Optional[str] = ...,
    source_dataset_id: Optional[str] = ...,
) -> Union[pd.DataFrame, "DedupResult"]: ...
def dedup_by_inchikey_chunked(
    df: pd.DataFrame,
    chunk_size: int = ...,
    **kwargs: Any,
) -> Union[pd.DataFrame, "DedupResult"]: ...
def compute_completeness_score(
    df: pd.DataFrame,
    *,
    weight: Optional["CompletenessWeight"] = ...,
) -> pd.Series: ...
def merge_duplicate_groups(
    df: pd.DataFrame,
    keys: List[str],
    *,
    weight: Optional["CompletenessWeight"] = ...,
) -> pd.DataFrame: ...
def quality_report(
    df: pd.DataFrame,
    *,
    data_type: Literal["drug", "interaction"] = ...,
) -> Dict[str, Any]: ...
def referential_integrity_check(
    df: pd.DataFrame,
    *,
    known_inchikeys: Optional[set] = ...,
    drug_id_to_inchikey: Optional[Dict[int, str]] = ...,
) -> Dict[str, Any]: ...
def backfill_safety_check(
    df: pd.DataFrame,
    known_inchikeys: set,
    *,
    on_conflict: Literal["warn", "error", "keep_existing"] = ...,
) -> Tuple[pd.DataFrame, List[str]]: ...
def recover_from_failure(
    df: pd.DataFrame,
    partial_result: Optional[pd.DataFrame],
    error: Exception,
    *,
    keys: Optional[List[str]] = ...,
) -> pd.DataFrame: ...
def checkpoint_state(
    df: pd.DataFrame,
    *,
    keys: Optional[List[str]] = ...,
) -> Dict[str, Any]: ...
def validate_recovery_state(checkpoint: Dict) -> bool: ...
def performance_benchmark(
    df: pd.DataFrame,
    *,
    keys: Optional[List[str]] = ...,
) -> Dict[str, Any]: ...
def is_reproducible(result_a: pd.DataFrame, result_b: pd.DataFrame) -> bool: ...
def reproducibility_report(df: pd.DataFrame) -> Dict[str, Any]: ...
def get_metrics() -> Dict[str, int]: ...
def reset_metrics() -> None: ...
def get_dead_letters() -> List[Dict[str, Any]]: ...
def clear_dead_letters() -> None: ...
def flush_dead_letters(path: Optional[Union[str, Path]] = ...) -> int: ...
def set_correlation_id(cid: Optional[str]) -> None: ...
def get_correlation_id() -> Optional[str]: ...
def get_provenance(result: Union[pd.DataFrame, "DedupResult"]) -> Dict[str, Any]: ...
def timing_report() -> Dict[str, Dict[str, float]]: ...
def health_check() -> Dict[str, Any]: ...
def configure_deduplicator(
    *,
    completeness_weights: Optional[Dict[str, float]] = ...,
    max_duplicate_ratio: Optional[float] = ...,
    max_dataframe_rows: Optional[int] = ...,
    default_strategy: Optional[Union["DedupStrategy", str]] = ...,
    reset_index_default: Optional[bool] = ...,
    log_format: Optional[Literal["text", "json"]] = ...,
    log_level: Optional[str] = ...,
) -> None: ...
def validate_config() -> List[str]: ...
def validate_environment() -> Dict[str, Any]: ...
def revert_configuration(steps: int = ...) -> None: ...
def requires_api_version(min_version: str) -> bool: ...

# Deduplicator types
class DedupStrategy(str, enum.Enum):
    MOST_COMPLETE: str
    FIRST_OCCURRENCE: str
    LAST_OCCURRENCE: str
    LOWEST_ACTIVITY: str
    HIGHEST_ACTIVITY: str
    MERGE_FIELDS: str

class ActivityDirection(str, enum.Enum):
    ASC: str
    DESC: str
    AUTO: str

class CompletenessWeight:
    weights: Dict[str, float]
    default_weight: float
    exclude_columns: frozenset
    def score_row(self, row: pd.Series) -> float: ...
    def score_dataframe(self, df: pd.DataFrame) -> pd.Series: ...

class DedupResult:
    df: pd.DataFrame
    rows_before: int
    rows_after: int
    duplicates_removed: int
    quarantined: int
    dead_letter_count: int
    duration_seconds: float
    warnings: List[str]
    columns_affected: Dict[str, Dict[str, int]]
    dtype_changes: Dict[str, Tuple[str, str]]
    dropped_rows: List[Dict[str, Any]]
    strategy: str
    provenance: Dict[str, Any]
    def __int__(self) -> int: ...
    def __len__(self) -> int: ...
    def quality_summary(self) -> Dict[str, Any]: ...

# Deduplicator constants
DEFAULT_COMPLETENESS_WEIGHTS: CompletenessWeight
DEFAULT_DPI_KEYS: List[str]
POTENCY_ACTIVITY_TYPES: frozenset
INVERSE_ACTIVITY_TYPES: frozenset
PERCENT_ACTIVITY_TYPES: frozenset
MAX_DATAFRAME_ROWS: int
MAX_DEAD_LETTERS: int
MAX_DROPPED_ROWS_IN_RESULT: int

# Missing values functions
def handle_missing_inchikey(df: pd.DataFrame) -> pd.DataFrame: ...
def fill_missing_drug_fields(df: pd.DataFrame) -> pd.DataFrame: ...
def handle_missing_protein_fields(df: pd.DataFrame) -> pd.DataFrame: ...
def validate_gda_scores(df: pd.DataFrame) -> pd.DataFrame: ...

# Package-level utilities
def check_health() -> Dict[str, Any]: ...
def validate_all_exports() -> list[str]: ...
def validate_environment() -> Dict[str, Any]: ...
def clean_drugs(df: pd.DataFrame, *, steps: Optional[List[str]] = ..., skip_steps: Optional[set] = ...) -> pd.DataFrame: ...
def clean_proteins(df: pd.DataFrame) -> pd.DataFrame: ...
def clean_gda(df: pd.DataFrame) -> pd.DataFrame: ...
def clean_drugs_chunked(df: pd.DataFrame, chunk_size: int = ..., *, steps: Optional[List[str]] = ..., skip_steps: Optional[set] = ...) -> pd.DataFrame: ...
def get_cleaning_function(name: str) -> Callable: ...
def list_cleaning_functions() -> List[str]: ...
def quality_report(df: pd.DataFrame, *, data_type: str = ...) -> dict: ...
def compute_data_fingerprint(df: pd.DataFrame) -> str: ...
def configure(*, fuzzy_threshold: Optional[float] = ..., max_sequence_length: Optional[int] = ...) -> None: ...
def has_rdkit_support() -> bool: ...
def has_rapidfuzz_support() -> bool: ...
def get_affected_functions(column_name: str) -> List[str]: ...
def get_load_times() -> Dict[str, float]: ...
def get_metrics() -> Dict[str, Any]: ...
def get_dead_letters() -> List[Dict[str, Any]]: ...
def clear_dead_letters() -> None: ...
def get_circuit_breaker(name: str) -> Any: ...
def set_correlation_id(cid: Optional[str]) -> None: ...
def get_correlation_id() -> Optional[str]: ...
def register_pre_clean_hook(hook: Callable) -> None: ...
def register_post_clean_hook(hook: Callable) -> None: ...

# Exception classes
class CleaningError(Exception): ...
class CleaningWarning(UserWarning): ...
class SchemaValidationError(CleaningError): ...
class DependencyNotAvailableError(CleaningError): ...

# Lazy import map (internal)
_LAZY_IMPORTS: Dict[str, str]
_OPTIONAL_DEPS: Dict[str, Dict[str, bool]]
_API_VERSIONS: Dict[str, str]
_CLEANING_REGISTRY: Dict[str, Callable]
_DEPRECATED_NAMES: Dict[str, str]
_CLEANING_DEPENDENCY_GRAPH: Dict[str, List[str]]

__all__ = [
    "ALLOWED_TYPES",
    "ActivityValue",
    "ActivityDirection",
    "CompletenessWeight",
    "ConversionResult",
    "DEFAULT_COMPLETENESS_WEIGHTS",
    "DEFAULT_DPI_KEYS",
    "DedupResult",
    "DedupStrategy",
    "FUZZY_THRESHOLD",
    "INVERSE_ACTIVITY_TYPES",
    "MAX_DATAFRAME_ROWS",
    "MAX_DEAD_LETTERS",
    "MAX_DROPPED_ROWS_IN_RESULT",
    "MAX_SEQUENCE_LENGTH",
    "PERCENT_ACTIVITY_TYPES",
    "POTENCY_ACTIVITY_TYPES",
    "RECORD_SCHEMA",
    "STEREO_POLICY",
    "UNIT_CONVERSIONS",
    "WITHDRAWN_GROUP_KEYWORDS",
    "backfill_safety_check",
    "checkpoint_state",
    "clean_interactions",
    "clear_dead_letters",
    "compute_completeness_score",
    "configure_deduplicator",
    "configure_normalizer",
    "convert_to_inchikey",
    "convert_to_inchikey_detailed",
    "convert_to_inchikeys",
    "dedup_by_inchikey",
    "dedup_by_inchikey_chunked",
    "dedup_interactions",
    "fill_missing_drug_fields",
    "flush_dead_letters",
    "fuzzy_match_drug_type",
    "fuzzy_match_drug_types",
    "get_cache_info",
    "get_correlation_id",
    "get_dead_letters",
    "get_dq_counts",
    "get_metrics",
    "get_provenance",
    "get_validation_status",
    "handle_missing_inchikey",
    "handle_missing_protein_fields",
    "health_check",
    "is_backfill_needed",
    "is_reproducible",
    "is_synthetic_inchikey",
    "is_valid_inchikey",
    "load_config",
    "merge_duplicate_groups",
    "normalize_activity_value",
    "normalize_activity_values",
    "normalize_inchikey",
    "performance_benchmark",
    "quality_report",
    "recover_from_failure",
    "referential_integrity_check",
    "refresh_capabilities",
    "reproducibility_report",
    "requires_api_version",
    "reset_dq_counts",
    "reset_metrics",
    "revert_configuration",
    "save_config",
    "set_correlation_id",
    "sign_output",
    "standardize_drug_record",
    "standardize_drug_records_batch",
    "standardize_drug_records_chunked",
    "standardize_inchikey",
    "timing_report",
    "validate_config",
    "validate_environment",
    "validate_gda_scores",
    "validate_inchikey",
    "validate_recovery_state",
]
