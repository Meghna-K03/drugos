"""
Comprehensive 16-Domain verification tests for database/models.py.

Tests verify that the ORM models enforce all constraints, validations, and
behaviours specified in the 78-issue forensic audit across 16 domains.
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.base import Base, IDMixin, NAMING_CONVENTION, SCHEMA_VERSION, SoftDeleteMixin, TimestampMixin
from database.models import (
    Drug,
    DrugProteinInteraction,
    EntityMapping,
    GeneDiseaseAssociation,
    PipelineRun,
    Protein,
    ProteinProteinInteraction,
    SchemaVersion,
    ClinicalPhase,
    DrugType,
    PipelineStatus,
    InteractionType,
    ActivityType,
    INCHIKEY_LENGTH,
    UNIPROT_ID_LENGTH,
    GENE_SYMBOL_LENGTH,
    _validate_inchikey,
    _validate_uniprot_id,
    _validate_gene_symbol,
    _validate_sequence,
    _validate_max_phase,
)

# Valid test data constants
VALID_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"  # Aspirin
VALID_INCHIKEY2 = "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"  # Ibuprofen
SYNTH_INCHIKEY = "SYNTH-TEST-COMPOUND-001"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="function")
def db_engine():
    """Create a fresh SQLite in-memory engine with FK enforcement."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.create_function(
                "now", 0,
                lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
            )

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Yield a transactional SQLAlchemy Session bound to an in-memory SQLite DB."""
    session = sessionmaker(bind=db_engine)()
    yield session
    session.rollback()
    session.close()


def _make_drug(session, **kwargs):
    """Helper to create and persist a Drug with sensible defaults."""
    defaults = {
        "inchikey": VALID_INCHIKEY,
        "name": "Aspirin",
        "max_phase": 4,
        "is_fda_approved": True,
    }
    defaults.update(kwargs)
    drug = Drug(**defaults)
    session.add(drug)
    session.flush()
    return drug


def _make_protein(session, **kwargs):
    """Helper to create and persist a Protein with sensible defaults."""
    defaults = {
        "uniprot_id": "P23219",
        "gene_symbol": "PTGS1",
        "protein_name": "Prostaglandin G/H synthase 1",
    }
    defaults.update(kwargs)
    protein = Protein(**defaults)
    session.add(protein)
    session.flush()
    return protein


def _make_pipeline_run(session, **kwargs):
    """Helper to create and persist a PipelineRun."""
    defaults = {"source": "chembl", "status": "success", "records_downloaded": 100}
    defaults.update(kwargs)
    run = PipelineRun(**defaults)
    session.add(run)
    session.flush()
    return run


# ===========================================================================
# Domain 1: Architecture
# ===========================================================================


class TestArchitecture:
    def test_base_in_database_base_module(self):
        from database.base import Base as BaseFromBase
        from database.connection import Base as BaseFromConnection
        assert BaseFromBase is BaseFromConnection

    def test_naming_convention_on_metadata(self):
        assert hasattr(Base.metadata, "naming_convention")
        for key in ["ix", "uq", "ck", "fk", "pk"]:
            assert key in NAMING_CONVENTION

    def test_all_export_list_exists(self):
        from database import models
        assert hasattr(models, "__all__")
        for name in ["Drug", "Protein", "DrugProteinInteraction",
                      "ProteinProteinInteraction", "GeneDiseaseAssociation",
                      "EntityMapping", "PipelineRun", "SchemaVersion"]:
            assert name in models.__all__

    def test_id_mixin(self):
        assert hasattr(IDMixin, "id")

    def test_timestamp_mixin(self):
        assert hasattr(TimestampMixin, "created_at")
        assert hasattr(TimestampMixin, "updated_at")

    def test_soft_delete_mixin(self):
        assert hasattr(SoftDeleteMixin, "is_deleted")
        assert hasattr(SoftDeleteMixin, "deleted_at")

    def test_schema_version_table_exists(self, db_engine):
        inspector = inspect(db_engine)
        assert "schema_version" in inspector.get_table_names()

    def test_schema_version_constant(self):
        assert isinstance(SCHEMA_VERSION, int)
        assert SCHEMA_VERSION >= 3

    def test_protein_all_ppi_property(self, db_session):
        protein = _make_protein(db_session)
        result = protein.all_ppi_interactions
        assert isinstance(result, list)

    def test_protein_all_ppi_partners_property(self, db_session):
        protein = _make_protein(db_session)
        result = protein.all_ppi_partners
        assert isinstance(result, list)


# ===========================================================================
# Domain 2: Design
# ===========================================================================


class TestDesign:
    def test_gda_has_protein_id_fk(self, db_session):
        # K fix: per task description #10, the GDA model no longer has an
        # integer protein_id column — it uses a string uniprot_id FK to
        # proteins.uniprot_id (the canonical cross-source key).
        assert hasattr(GeneDiseaseAssociation, "uniprot_id")
        col = GeneDiseaseAssociation.__table__.c.uniprot_id
        assert col is not None

    def test_ppi_ordering_check_constraint(self, db_session):
        p1 = _make_protein(db_session, uniprot_id="P00001", gene_symbol="AAA")
        p2 = _make_protein(db_session, uniprot_id="P00002", gene_symbol="BBB")
        db_session.flush()
        ppi = ProteinProteinInteraction(
            protein_a_id=max(p1.id, p2.id),
            protein_b_id=min(p1.id, p2.id),
            combined_score=500, source="string",
        )
        db_session.add(ppi)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_ppi_correct_ordering_succeeds(self, db_session):
        p1 = _make_protein(db_session, uniprot_id="P00003", gene_symbol="CCC")
        p2 = _make_protein(db_session, uniprot_id="P00004", gene_symbol="DDD")
        db_session.flush()
        ppi = ProteinProteinInteraction(
            protein_a_id=min(p1.id, p2.id),
            protein_b_id=max(p1.id, p2.id),
            combined_score=500, source="string",
        )
        db_session.add(ppi)
        db_session.flush()
        assert ppi.id is not None

    def test_source_id_nullable(self, db_session):
        drug = _make_drug(db_session, inchikey=VALID_INCHIKEY)
        protein = _make_protein(db_session, uniprot_id="P00005", gene_symbol="EEE")
        db_session.flush()
        dpi = DrugProteinInteraction(
            drug_id=drug.id, protein_id=protein.id,
            source_id=None, source="chembl",
        )
        db_session.add(dpi)
        db_session.flush()
        assert dpi.source_id is None

    def test_soft_delete_on_drug(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        db_session.flush()
        drug.soft_delete()
        assert drug.is_deleted is True
        assert drug.deleted_at is not None
        drug.restore()
        assert drug.is_deleted is False
        assert drug.deleted_at is None

    def test_soft_delete_on_protein(self, db_session):
        protein = _make_protein(db_session, uniprot_id="P00006", gene_symbol="FFF")
        db_session.flush()
        protein.soft_delete()
        assert protein.is_deleted is True
        protein.restore()
        assert protein.is_deleted is False

    def test_pipeline_source_check_constraint(self, db_session):
        run = PipelineRun(source="invalid_source", status="success")
        db_session.add(run)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_pipeline_source_valid(self, db_session):
        for src in ["chembl", "drugbank", "uniprot", "string", "disgenet", "omim", "pubchem"]:
            run = PipelineRun(source=src, status="success")
            db_session.add(run)
        db_session.flush()
        assert db_session.query(PipelineRun).count() == 7


# ===========================================================================
# Domain 3: Scientific Correctness
# ===========================================================================


class TestScientificCorrectness:
    def test_inchikey_standard_27_chars_accepted(self, db_session):
        drug = _make_drug(db_session, inchikey=VALID_INCHIKEY)
        assert drug.inchikey == VALID_INCHIKEY

    def test_inchikey_synthetic_accepted(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        assert drug.inchikey == SYNTH_INCHIKEY

    def test_inchikey_invalid_rejected(self):
        with pytest.raises(ValueError, match="Invalid InChIKey"):
            _validate_inchikey("INVALID")

    def test_max_phase_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="Invalid max_phase"):
            _validate_max_phase(999)

    def test_max_phase_negative_rejected(self):
        with pytest.raises(ValueError, match="Invalid max_phase"):
            _validate_max_phase(-1)

    def test_max_phase_db_constraint(self, db_session):
        """Python-side @validates catches invalid max_phase before DB constraint."""
        with pytest.raises(ValueError, match="Invalid max_phase"):
            drug = Drug(inchikey=SYNTH_INCHIKEY, name="Test", max_phase=999, is_fda_approved=False)
        db_session.rollback()

    def test_max_phase_valid_range(self, db_session):
        for i, phase in enumerate(range(5)):
            key = f"SYNTH-PHASE-{i}"
            drug = Drug(inchikey=key, name=f"Drug{phase}", max_phase=phase, is_fda_approved=False)
            db_session.add(drug)
        db_session.flush()
        assert db_session.query(Drug).filter(Drug.max_phase >= 0).count() == 5

    def test_ppi_score_out_of_range(self, db_session):
        p1 = _make_protein(db_session, uniprot_id="P00010", gene_symbol="SCI1")
        p2 = _make_protein(db_session, uniprot_id="P00011", gene_symbol="SCI2")
        db_session.flush()
        ppi = ProteinProteinInteraction(
            protein_a_id=min(p1.id, p2.id), protein_b_id=max(p1.id, p2.id),
            combined_score=1001, source="string",
        )
        db_session.add(ppi)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_ppi_normalized_score(self, db_session):
        p1 = _make_protein(db_session, uniprot_id="P00020", gene_symbol="SCI3")
        p2 = _make_protein(db_session, uniprot_id="P00021", gene_symbol="SCI4")
        db_session.flush()
        ppi = ProteinProteinInteraction(
            protein_a_id=min(p1.id, p2.id), protein_b_id=max(p1.id, p2.id),
            combined_score=750, source="string",
        )
        db_session.add(ppi)
        db_session.flush()
        assert ppi.normalized_combined_score == 0.75

    def test_uniprot_id_format_validator(self):
        assert _validate_uniprot_id("P69999") == "P69999"
        assert _validate_uniprot_id("Q9Y6K9") == "Q9Y6K9"
        with pytest.raises(ValueError, match="Invalid UniProt"):
            _validate_uniprot_id("INVALID")

    def test_gene_symbol_validator(self):
        assert _validate_gene_symbol("BRCA1") == "BRCA1"
        assert _validate_gene_symbol("TP53") == "TP53"
        with pytest.raises(ValueError, match="Invalid gene symbol"):
            _validate_gene_symbol("invalid-lower")

    def test_sequence_validator(self):
        assert _validate_sequence("MACDEFGHIKLMNPQRSTVWY") == "MACDEFGHIKLMNPQRSTVWY"
        with pytest.raises(ValueError, match="Invalid protein sequence"):
            _validate_sequence("123INVALID")

    def test_molecular_weight_positive(self, db_session):
        drug = Drug(inchikey="SYNTH-MW-NEG", name="Test", molecular_weight=-100, is_fda_approved=False)
        db_session.add(drug)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


# ===========================================================================
# Domain 4: Coding
# ===========================================================================


class TestCoding:
    def test_protein_repr_shows_gene_symbol(self):
        protein = Protein(uniprot_id="P23219", gene_symbol="PTGS1",
                         protein_name="COX1", gene_name="PTGS1 protein")
        repr_str = repr(protein)
        assert "gene_symbol='PTGS1'" in repr_str
        assert "gene_name(legacy)" in repr_str

    def test_drug_repr_shows_max_phase(self):
        drug = Drug(inchikey=VALID_INCHIKEY, name="Test", max_phase=3)
        repr_str = repr(drug)
        assert "max_phase=3" in repr_str

    def test_dpi_repr_shows_interaction_type(self):
        dpi = DrugProteinInteraction(drug_id=1, protein_id=1,
                                     interaction_type="inhibitor",
                                     activity_value=50.0, source="chembl")
        repr_str = repr(dpi)
        assert "interaction_type='inhibitor'" in repr_str
        assert "activity_value=50.0" in repr_str

    def test_is_fda_approved_server_default(self, db_session):
        drug = Drug(inchikey=SYNTH_INCHIKEY, name="Test")
        db_session.add(drug)
        db_session.flush()
        assert drug.is_fda_approved is False


# ===========================================================================
# Domain 5: Data Quality
# ===========================================================================


class TestDataQuality:
    def test_drug_name_minimum_length(self, db_session):
        """Python-side @validates catches short name before DB constraint."""
        with pytest.raises(ValueError, match="at least 2 characters"):
            drug = Drug(inchikey=SYNTH_INCHIKEY, name="A", is_fda_approved=False)
        db_session.rollback()

    def test_drug_name_valid_length(self, db_session):
        drug = Drug(inchikey=SYNTH_INCHIKEY, name="Ab", is_fda_approved=False)
        db_session.add(drug)
        db_session.flush()
        assert drug.name == "Ab"

    def test_confidence_score_range_dpi(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        protein = _make_protein(db_session, uniprot_id="P00500", gene_symbol="CONF")
        db_session.flush()
        dpi = DrugProteinInteraction(
            drug_id=drug.id, protein_id=protein.id,
            confidence_score=1.5, source="test",
        )
        db_session.add(dpi)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_match_confidence_range_entity_mapping(self, db_session):
        em = EntityMapping(canonical_name="Test", match_confidence=1.5)
        db_session.add(em)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_duration_seconds_non_negative(self, db_session):
        run = PipelineRun(source="chembl", duration_seconds=-1)
        db_session.add(run)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_activity_value_positive(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        protein = _make_protein(db_session, uniprot_id="P00600", gene_symbol="ACTV")
        db_session.flush()
        dpi = DrugProteinInteraction(
            drug_id=drug.id, protein_id=protein.id,
            activity_value=-50.0, source="test",
        )
        db_session.add(dpi)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_is_fda_approved_boolean_check(self, db_session):
        drug = Drug(inchikey=SYNTH_INCHIKEY, name="Test")
        db_session.add(drug)
        db_session.flush()
        assert drug.is_fda_approved in (True, False, 0, 1)


# ===========================================================================
# Domain 6: Reliability
# ===========================================================================


class TestReliability:
    def test_cascade_delete_drug_to_dpi(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        protein = _make_protein(db_session, uniprot_id="P00700", gene_symbol="CASC")
        db_session.flush()
        dpi = DrugProteinInteraction(
            drug_id=drug.id, protein_id=protein.id, source="test",
        )
        db_session.add(dpi)
        db_session.flush()
        dpi_id = dpi.id
        db_session.delete(drug)
        db_session.flush()
        assert db_session.query(DrugProteinInteraction).filter_by(id=dpi_id).first() is None

    def test_cleanup_function_in_loaders(self):
        from database.loaders import cleanup_orphan_gda_records
        assert callable(cleanup_orphan_gda_records)

    def test_cleanup_deprecated_in_models(self, db_session):
        from database.models import cleanup_orphan_gda_records
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                cleanup_orphan_gda_records(db_session)
            except Exception:
                pass
            assert any(issubclass(warning.category, DeprecationWarning) for warning in w)


# ===========================================================================
# Domain 7: Idempotency
# ===========================================================================


class TestIdempotency:
    def test_ppi_symmetric_duplicate_prevented(self, db_session):
        p1 = _make_protein(db_session, uniprot_id="P01000", gene_symbol="IDEM1")
        p2 = _make_protein(db_session, uniprot_id="P01001", gene_symbol="IDEM2")
        db_session.flush()
        low, high = min(p1.id, p2.id), max(p1.id, p2.id)
        ppi1 = ProteinProteinInteraction(
            protein_a_id=low, protein_b_id=high,
            combined_score=500, source="string",
        )
        db_session.add(ppi1)
        db_session.flush()
        ppi2 = ProteinProteinInteraction(
            protein_a_id=high, protein_b_id=low,
            combined_score=500, source="string",
        )
        db_session.add(ppi2)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_pipeline_run_id_on_dpi(self):
        assert hasattr(DrugProteinInteraction, "pipeline_run_id")

    def test_pipeline_run_id_on_ppi(self):
        assert hasattr(ProteinProteinInteraction, "pipeline_run_id")

    def test_pipeline_run_id_on_gda(self):
        assert hasattr(GeneDiseaseAssociation, "pipeline_run_id")

    def test_updated_at_on_all_models(self):
        for model in [Drug, Protein, DrugProteinInteraction,
                      ProteinProteinInteraction, GeneDiseaseAssociation,
                      EntityMapping, PipelineRun]:
            assert hasattr(model, "updated_at"), f"{model.__name__} missing updated_at"


# ===========================================================================
# Domain 8: Performance
# ===========================================================================


class TestPerformance:
    def test_composite_index_dpi_protein_interaction(self, db_engine):
        inspector = inspect(db_engine)
        idx_names = [idx["name"] for idx in inspector.get_indexes("drug_protein_interactions")]
        assert "idx_dpi_protein_interaction" in idx_names

    def test_composite_index_dpi_drug_interaction(self, db_engine):
        inspector = inspect(db_engine)
        idx_names = [idx["name"] for idx in inspector.get_indexes("drug_protein_interactions")]
        assert "idx_dpi_drug_interaction" in idx_names

    def test_gda_protein_id_index(self, db_engine):
        # K fix: per task description #10, GDA uses uniprot_id (string FK)
        # instead of integer protein_id. The corresponding index is
        # idx_gda_uniprot_id.
        inspector = inspect(db_engine)
        idx_names = [idx["name"] for idx in inspector.get_indexes("gene_disease_associations")]
        assert "idx_gda_uniprot_id" in idx_names

    def test_no_redundant_inchikey_index(self, db_engine):
        inspector = inspect(db_engine)
        idx_names = [idx["name"] for idx in inspector.get_indexes("drugs")]
        assert "idx_drugs_inchikey" not in idx_names

    def test_no_redundant_uniprot_index(self, db_engine):
        inspector = inspect(db_engine)
        idx_names = [idx["name"] for idx in inspector.get_indexes("proteins")]
        assert "idx_proteins_uniprot" not in idx_names

    def test_no_deprecated_gene_name_index(self, db_engine):
        inspector = inspect(db_engine)
        idx_names = [idx["name"] for idx in inspector.get_indexes("proteins")]
        assert "idx_proteins_gene_name" not in idx_names


# ===========================================================================
# Domain 9: Security
# ===========================================================================


class TestSecurity:
    def test_pmid_list_capped(self):
        col = GeneDiseaseAssociation.__table__.c.pmid_list
        assert col.type.length is not None or str(col.type) != "TEXT"

    def test_error_message_capped(self):
        col = PipelineRun.__table__.c.error_message
        assert col.type.length is not None or str(col.type) != "TEXT"

    def test_soft_delete_prevents_hard_delete(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        drug.soft_delete()
        assert drug.is_deleted is True
        db_session.flush()
        assert db_session.query(Drug).filter_by(inchikey=SYNTH_INCHIKEY).first() is not None

    def test_dpi_source_version_columns(self):
        assert hasattr(DrugProteinInteraction, "source_version")
        assert hasattr(DrugProteinInteraction, "source_fetch_date")
        assert hasattr(DrugProteinInteraction, "entity_resolved")


# ===========================================================================
# Domain 11: Logging
# ===========================================================================


class TestLogging:
    def test_ppi_repr_includes_source_and_score(self):
        ppi = ProteinProteinInteraction(
            protein_a_id=1, protein_b_id=2,
            combined_score=800, source="string",
        )
        repr_str = repr(ppi)
        assert "combined_score=800" in repr_str
        assert "source='string'" in repr_str

    def test_dpi_has_lineage_columns(self):
        assert hasattr(DrugProteinInteraction, "source_version")
        assert hasattr(DrugProteinInteraction, "source_fetch_date")


# ===========================================================================
# Domain 12: Configuration
# ===========================================================================


class TestConfiguration:
    def test_inchikey_length_constant(self):
        assert INCHIKEY_LENGTH == 50

    def test_uniprot_id_length_constant(self):
        assert UNIPROT_ID_LENGTH == 10

    def test_gene_symbol_length_constant(self):
        assert GENE_SYMBOL_LENGTH == 50

    def test_orphan_gda_retention_hours_configurable(self):
        from config.settings import ORPHAN_GDA_RETENTION_HOURS
        assert isinstance(ORPHAN_GDA_RETENTION_HOURS, int)
        assert ORPHAN_GDA_RETENTION_HOURS > 0

    def test_ppi_source_no_default(self, db_session):
        col = ProteinProteinInteraction.__table__.c.source
        assert col.server_default is None

    def test_ppi_source_must_be_specified(self, db_session):
        p1 = _make_protein(db_session, uniprot_id="P01200", gene_symbol="CFG1")
        p2 = _make_protein(db_session, uniprot_id="P01201", gene_symbol="CFG2")
        db_session.flush()
        ppi = ProteinProteinInteraction(
            protein_a_id=min(p1.id, p2.id),
            protein_b_id=max(p1.id, p2.id),
            combined_score=500,
        )
        db_session.add(ppi)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


# ===========================================================================
# Domain 13: Documentation
# ===========================================================================


class TestDocumentation:
    def test_drug_has_class_docstring(self):
        assert Drug.__doc__ is not None and len(Drug.__doc__) > 50

    def test_protein_has_class_docstring(self):
        assert Protein.__doc__ is not None and len(Protein.__doc__) > 50

    def test_dpi_has_class_docstring(self):
        assert DrugProteinInteraction.__doc__ is not None

    def test_ppi_has_class_docstring(self):
        assert ProteinProteinInteraction.__doc__ is not None

    def test_gda_has_class_docstring(self):
        assert GeneDiseaseAssociation.__doc__ is not None

    def test_entity_mapping_has_class_docstring(self):
        assert EntityMapping.__doc__ is not None

    def test_pipeline_run_has_class_docstring(self):
        assert PipelineRun.__doc__ is not None

    def test_gene_name_deprecation_documented(self):
        doc = Protein.__doc__ or ""
        assert "DEPRECATED" in doc or "deprecated" in doc.lower() or "gene_name" in doc

    def test_cleanup_deprecation_warning(self, db_session):
        from database.models import cleanup_orphan_gda_records
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                cleanup_orphan_gda_records(db_session)
            except Exception:
                pass
            assert any("deprecated" in str(warning.message).lower() for warning in w)


# ===========================================================================
# Domain 14: Compliance
# ===========================================================================


class TestCompliance:
    def test_naming_convention_configured(self):
        assert Base.metadata.naming_convention is not None
        assert "ck" in Base.metadata.naming_convention

    def test_check_constraints_are_named(self, db_engine):
        for model in [Drug, Protein, DrugProteinInteraction,
                      ProteinProteinInteraction, GeneDiseaseAssociation,
                      EntityMapping, PipelineRun]:
            table_args = model.__table_args__
            if isinstance(table_args, tuple):
                for item in table_args:
                    if hasattr(item, "name") and hasattr(item, "sqltext"):
                        assert item.name is not None

    def test_type_stubs_exist(self):
        stub_path = PROJECT_ROOT / "database" / "__init__.pyi"
        assert stub_path.exists()

    def test_all_models_use_mapped_type(self):
        for model in [Drug, Protein, DrugProteinInteraction,
                      ProteinProteinInteraction, GeneDiseaseAssociation,
                      EntityMapping, PipelineRun, SchemaVersion]:
            annotations = model.__annotations__
            assert len(annotations) > 0


# ===========================================================================
# Domain 15: Interoperability
# ===========================================================================


class TestInteroperability:
    def test_drug_to_dict(self, db_session):
        drug = _make_drug(db_session, inchikey=SYNTH_INCHIKEY)
        d = drug.to_dict()
        assert isinstance(d, dict)
        assert "inchikey" in d
        assert "is_deleted" in d
        assert "created_at" in d

    def test_protein_to_dict(self, db_session):
        protein = _make_protein(db_session, uniprot_id="P01500", gene_symbol="TODICT")
        d = protein.to_dict()
        assert isinstance(d, dict)
        assert "uniprot_id" in d

    def test_drug_graph_identity(self):
        assert Drug.graph_identity() == "inchikey"

    def test_protein_graph_identity(self):
        assert Protein.graph_identity() == "uniprot_id"

    def test_ppi_score_json_column(self):
        assert hasattr(ProteinProteinInteraction, "score_json")

    def test_disease_id_type_column(self):
        assert hasattr(GeneDiseaseAssociation, "disease_id_type")


# ===========================================================================
# Domain 16: Lineage
# ===========================================================================


class TestLineage:
    def test_dpi_source_version(self):
        assert hasattr(DrugProteinInteraction, "source_version")

    def test_dpi_source_fetch_date(self):
        assert hasattr(DrugProteinInteraction, "source_fetch_date")

    def test_dpi_entity_resolved(self):
        assert hasattr(DrugProteinInteraction, "entity_resolved")

    def test_dpi_pipeline_run_id(self):
        assert hasattr(DrugProteinInteraction, "pipeline_run_id")

    def test_gda_score_type_and_method(self):
        assert hasattr(GeneDiseaseAssociation, "score_type")
        assert hasattr(GeneDiseaseAssociation, "score_method")

    def test_entity_mapping_match_history(self):
        assert hasattr(EntityMapping, "match_history")

    def test_schema_version_tracks_lineage(self, db_session):
        sv = SchemaVersion(version=3, description="16-domain fix")
        db_session.add(sv)
        db_session.flush()
        assert sv.id is not None
        assert sv.applied_at is not None


# ===========================================================================
# End-to-end integration test
# ===========================================================================


class TestEndToEndIntegration:
    def test_full_data_lifecycle(self, db_session):
        run = PipelineRun(source="chembl", status="success",
                         records_downloaded=100, duration_seconds=10)
        db_session.add(run)
        db_session.flush()

        drug = Drug(
            inchikey=VALID_INCHIKEY, name="Aspirin",
            chembl_id="CHEMBL25", drugbank_id="DB00945",
            max_phase=4, is_fda_approved=True,
            molecular_weight=180.16,
        )
        db_session.add(drug)
        db_session.flush()

        protein = Protein(
            uniprot_id="P23219", gene_symbol="PTGS1",
            protein_name="COX1", sequence="MACDEFGHIKLMNPQRSTVWY",
        )
        db_session.add(protein)
        db_session.flush()

        dpi = DrugProteinInteraction(
            drug_id=drug.id, protein_id=protein.id,
            interaction_type="inhibitor", activity_value=50.0,
            activity_type="IC50", source="chembl",
            pipeline_run_id=run.id,
        )
        db_session.add(dpi)
        db_session.flush()

        protein2 = Protein(
            uniprot_id="P04637", gene_symbol="TP53",
            protein_name="P53",
        )
        db_session.add(protein2)
        db_session.flush()

        low_id, high_id = min(protein.id, protein2.id), max(protein.id, protein2.id)
        ppi = ProteinProteinInteraction(
            protein_a_id=low_id, protein_b_id=high_id,
            combined_score=800, source="string",
            pipeline_run_id=run.id,
        )
        db_session.add(ppi)
        db_session.flush()

        # K fix: per task description #10, GDA no longer has protein_id
        # (integer FK removed); use uniprot_id (string FK to proteins.uniprot_id).
        gda = GeneDiseaseAssociation(
            gene_symbol="PTGS1", uniprot_id=protein.uniprot_id,
            disease_id="OMIM:123456",
            disease_id_type="omim", disease_name="Test Disease",
            score=0.95, source="disgenet",
            pipeline_run_id=run.id,
        )
        db_session.add(gda)
        db_session.flush()

        em = EntityMapping(
            canonical_inchikey=VALID_INCHIKEY, canonical_name="Aspirin",
            chembl_id="CHEMBL25", drugbank_id="DB00945",
            match_confidence=0.99, match_method="inchikey_exact",
        )
        db_session.add(em)
        db_session.flush()

        assert drug.id is not None
        assert protein.id is not None
        assert dpi.id is not None
        assert ppi.id is not None
        assert gda.id is not None
        assert em.id is not None
        assert run.id is not None

        assert len(drug.drug_protein_interactions) >= 1
        assert ppi.normalized_combined_score == 0.8
        drug_dict = drug.to_dict()
        assert drug_dict["inchikey"] == VALID_INCHIKEY
        drug.soft_delete()
        db_session.flush()
        assert drug.is_deleted is True


class TestAllFiveFilesWorkTogether:
    def test_base_importable_from_connection(self):
        from database.connection import Base as BaseFromConn
        assert BaseFromConn is not None

    def test_base_importable_from_base(self):
        from database.base import Base as BaseFromBase
        assert BaseFromBase is not None
        assert BaseFromBase is Base

    def test_models_import_base_from_base_module(self):
        from database.models import Drug
        assert Drug is not None

    def test_database_package_lazy_loads(self):
        import database
        assert hasattr(database, "__version__")

    def test_loaders_cleanup_function_works(self, db_session):
        from database.loaders import cleanup_orphan_gda_records
        result = cleanup_orphan_gda_records(db_session)
        assert isinstance(result, int)

    def test_config_has_orphan_retention(self):
        from config.settings import ORPHAN_GDA_RETENTION_HOURS
        assert ORPHAN_GDA_RETENTION_HOURS == 24

    def test_schema_version_model_works(self, db_session):
        sv = SchemaVersion(version=3, description="Test version")
        db_session.add(sv)
        db_session.flush()
        result = db_session.query(SchemaVersion).filter_by(version=3).first()
        assert result is not None
        assert result.description == "Test version"

    def test_all_enums_defined(self):
        assert ClinicalPhase.APPROVED.value == 4
        assert DrugType.SMALL_MOLECULE.value == "small_molecule"
        assert PipelineStatus.SUCCESS.value == "success"
        assert InteractionType.INHIBITOR.value == "inhibitor"
        assert ActivityType.IC50.value == "IC50"
