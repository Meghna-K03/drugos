"""
Type stubs for the config package.

This file provides static type information for IDEs and type checkers
(mypy, pyright) so that autocompletion and type checking work correctly
even though the actual implementation uses ``__getattr__`` for lazy
attribute resolution.
"""

from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------
__version__: str

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENVIRONMENT: str

# ---------------------------------------------------------------------------
# Settings: Database
# ---------------------------------------------------------------------------
DATABASE_URL: str

# ---------------------------------------------------------------------------
# Settings: Paths
# ---------------------------------------------------------------------------
AIRFLOW_HOME: Path
BASE_DIR: Path
DRUGBANK_XML_PATH: Path
PROCESSED_DATA_DIR: Path
RAW_DATA_DIR: Path

# ---------------------------------------------------------------------------
# Settings: ChEMBL
# ---------------------------------------------------------------------------
CHEMBL_API_URL: str
CHEMBL_EXPECTED_DRUG_COUNT_MAX: int
CHEMBL_EXPECTED_DRUG_COUNT_MIN: int
CHEMBL_MAX_ACTIVITIES: int | None
CHEMBL_MAX_ROWS: int | None
CHEMBL_SNAPSHOT_DATE: str
CHEMBL_VERSION: str

# ---------------------------------------------------------------------------
# Settings: STRING
# ---------------------------------------------------------------------------
STRING_ALIASES_URL: str
STRING_MIN_COMBINED_SCORE: int
STRING_PROTEIN_LINKS_DETAILED_URL: str
STRING_PROTEIN_LINKS_URL: str
STRING_VERSION: str

# ---------------------------------------------------------------------------
# Settings: DisGeNET
# ---------------------------------------------------------------------------
DISGENET_API_KEY: str
DISGENET_API_URL: str
DISGENET_URL: str
DISGENET_USE_API: bool

# ---------------------------------------------------------------------------
# Settings: OMIM
# ---------------------------------------------------------------------------
OMIM_API_BASE: str
OMIM_API_KEY: str

# ---------------------------------------------------------------------------
# Settings: PubChem
# ---------------------------------------------------------------------------
PUBCHEM_API_URL: str
PUBCHEM_FTP_BASE: str
PUBCHEM_REST_BASE: str

# ---------------------------------------------------------------------------
# Settings: UniProt
# ---------------------------------------------------------------------------
UNIPROT_RELEASE: str

# ---------------------------------------------------------------------------
# Settings: Provenance
# ---------------------------------------------------------------------------
DATA_SNAPSHOT_ID: str

# ---------------------------------------------------------------------------
# Settings: Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigValidationError(Exception):
    results: list
    def __init__(self, message: str, results: list | None = ...) -> None: ...

class ConfigLoadError(Exception):
    original_error: Exception | None
    def __init__(self, message: str, original_error: Exception | None = ...) -> None: ...

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class ConfigValidationResult:
    severity: str
    setting_name: str
    message: str
    def __init__(self, severity: str, setting_name: str, message: str) -> None: ...

class ConfigDict(dict):
    def to_dict(self) -> dict: ...

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

def get_config() -> ConfigDict: ...
def get_config_fingerprint() -> str: ...
def get_config_summary() -> dict: ...
def initialize(configure_logging: bool = ...) -> None: ...
def is_loaded() -> bool: ...
def reload() -> None: ...
def validate_config(strict: bool = ...) -> list: ...
