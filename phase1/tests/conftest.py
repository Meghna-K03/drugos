"""
Shared pytest fixtures for the Drug Repurposing ETL test suite.

Provides:
  - SQLite in-memory database session for testing
  - Sample DataFrames for drugs, proteins
  - Temp directory for file operations
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Logger-level isolation fixture (prevents test-isolation bugs)
# ---------------------------------------------------------------------------
# Some test modules set LOG_LEVEL=WARNING at import time via
# ``os.environ.setdefault("LOG_LEVEL", "WARNING")``.  When ``setup_logging()``
# is later called by another test, the ``pipelines`` logger level is
# permanently set to WARNING, which causes ``caplog`` to miss INFO records
# in subsequent tests (e.g. test_string_pipeline_institutional_v149).
# This autouse fixture resets the key namespace loggers to NOTSET after
# every test so that each test starts with a clean logger state.  It does
# NOT affect tests that explicitly set the level within their own scope
# (those tests set the level after this fixture's setup phase).
@pytest.fixture(autouse=True)
def _reset_namespace_logger_levels():
    """Reset platform namespace logger levels to NOTSET after each test.

    This prevents test-isolation bugs where one test sets a logger level
    (e.g. via ``setup_logging()`` or ``set_log_level()``) and the level
    persists into subsequent tests, breaking ``caplog`` capture.
    """
    _namespaces = (
        "config",
        "pipelines",
        "pipelines.base_pipeline",
        "pipelines.chembl_pipeline",
        "pipelines.drugbank_pipeline",
        "pipelines.uniprot_pipeline",
        "pipelines.string_pipeline",
        "pipelines.disgenet_pipeline",
        "pipelines.omim_pipeline",
        "pipelines.pubchem_pipeline",
        "database",
        "cleaning",
        "entity_resolution",
        "exporters",
    )
    _saved_levels: dict[str, int] = {}
    for ns in _namespaces:
        _saved_levels[ns] = logging.getLogger(ns).level
    yield
    for ns in _namespaces:
        logger = logging.getLogger(ns)
        # Only reset if the level was changed during the test
        if logger.level != _saved_levels[ns]:
            logger.setLevel(_saved_levels[ns])

from database.base import Base
from database.models import (
    Drug,
    DrugProteinInteraction,
    EntityMapping,
    GeneDiseaseAssociation,
    PipelineRun,
    Protein,
    ProteinProteinInteraction,
)


# ============================================================================
# Database fixtures
# ============================================================================


@pytest.fixture(scope="function")
def db_engine():
    """Create a fresh SQLite in-memory engine with FK enforcement and ``now()`` support."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            # Enable foreign-key enforcement (off by default in SQLite)
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            # FIX #31: Return datetime string that SQLite can parse for DEFAULT values
            dbapi_conn.create_function(
                "now", 0,
                lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
            )

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Yield a transactional SQLAlchemy ``Session`` bound to an in-memory SQLite DB.

    The session is rolled back after each test to keep the DB clean.
    """
    session = sessionmaker(bind=db_engine)()
    yield session
    session.rollback()
    session.close()


# ============================================================================
# Sample-data fixtures
# ============================================================================


@pytest.fixture
def sample_drug_df() -> pd.DataFrame:
    """Minimal drug DataFrame matching the ``Drug`` model columns.

    Returns a fresh copy each time so tests that mutate the DataFrame
    don't pollute other tests (test-isolation hygiene).
    """
    return pd.DataFrame(
        {
            "inchikey": [
                "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "WFXAZNNJSJXTJZ-UHFFFAOYSA-N",
            ],
            "name": ["Aspirin", "Ibuprofen"],
            "chembl_id": ["CHEMBL25", "CHEMBL521"],
            "drugbank_id": ["DB00945", "DB01050"],
            "pubchem_cid": [2244, 3672],
            "molecular_formula": ["C9H8O4", "C13H18O2"],
            "molecular_weight": [180.16, 206.28],
            "smiles": [
                "CC(=O)Oc1ccccc1C(=O)O",
                "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
            ],
            "is_fda_approved": [True, True],
            "max_phase": [4, 4],
            "drug_type": ["small_molecule", "small_molecule"],
            "mechanism_of_action": ["COX inhibitor", "COX inhibitor"],
        }
    ).copy()


@pytest.fixture
def sample_protein_df() -> pd.DataFrame:
    """Minimal protein DataFrame matching the ``Protein`` model columns.

    FIX C4/D9: gene_name stores CANONICAL PROTEIN NAME, NOT gene symbols.
    gene_symbol is the actual gene symbol used for GDA resolution.

    Returns a fresh copy each time so tests that mutate the DataFrame
    don't pollute other tests (test-isolation hygiene).
    """
    return pd.DataFrame(
        {
            "uniprot_id": ["P23219", "P04637"],
            "gene_name": [
                "Prostaglandin G/H synthase 1",  # protein name, NOT "PTGS1"
                "Cellular tumor antigen p53",        # protein name, NOT "TP53"
            ],
            "gene_symbol": ["PTGS1", "TP53"],  # actual gene symbols
            "protein_name": [
                "Prostaglandin G/H synthase 1",
                "Cellular tumor antigen p53",
            ],
            "organism": ["Homo sapiens", "Homo sapiens"],
            "sequence": ["M" * 100, "M" * 100],
            "function_desc": ["COX enzyme", "Tumor suppressor"],
            "string_id": [
                "9606.ENSP00000269305",
                "9606.ENSP00000269306",
            ],
        }
    ).copy()


@pytest.fixture
def temp_dir(tmp_path) -> Path:
    """Temporary directory for file-based tests."""
    return tmp_path


# ============================================================================
# PostgreSQL integration test fixtures (FIX #20)
# ============================================================================


@pytest.fixture(scope="session")
def pg_engine():
    """Create a PostgreSQL engine for integration testing.

    FIX #20: Allows running integration tests against a real PostgreSQL
    database. Set TEST_DATABASE_URL environment variable to enable.
    Skips tests if not configured.
    """
    import os
    test_db_url = os.getenv("TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set, skipping PostgreSQL tests")
    engine = create_engine(test_db_url, echo=False)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def pg_session(pg_engine):
    """Yield a session bound to a PostgreSQL test database.

    FIX #20: Provides a session connected to a real PostgreSQL instance
    for integration testing. Rolls back after each test.
    """
    session = sessionmaker(bind=pg_engine)()
    yield session
    session.rollback()
    session.close()
