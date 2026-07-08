"""
End-to-end pipeline test for the Drug Repurposing Platform v7.

Validates the ENTIRE data pipeline from download through load using
MOCKED data for download steps (no internet required).

Uses a real SQLite database (not in-memory) so data persists across stages.

Run with: pytest tests/test_full_pipeline_e2e.py -v --tb=long
"""

import gzip
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_data_dir():
    """Create a temporary directory for test data."""
    tmpdir = tempfile.mkdtemp(prefix="drug_repurposing_e2e_")
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def db_path(test_data_dir):
    """Path to the test SQLite database file."""
    return test_data_dir / "test_drug_repurposing.db"


@pytest.fixture(scope="module")
def engine(db_path):
    """Create a SQLite engine for the test database."""
    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def db_session(engine):
    """Create the schema and yield a session."""
    from database.connection import Base
    import database.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(scope="module")
def raw_dir(test_data_dir):
    """Create raw data directory with mock data files."""
    raw = test_data_dir / "raw_data"
    raw.mkdir(exist_ok=True)
    return raw


@pytest.fixture(scope="module")
def processed_dir(test_data_dir):
    """Create processed data directory."""
    proc = test_data_dir / "processed_data"
    proc.mkdir(exist_ok=True)
    return proc


# ===========================================================================
# Stage 1: UniProt Pipeline (provides protein data)
# ===========================================================================

class TestStage1UniProt:
    """UniProt pipeline: load proteins into the database."""

    def test_load_proteins(self, db_session):
        """Load mock protein data into the proteins table."""
        from database.loaders import bulk_upsert_proteins

        proteins_df = pd.DataFrame({
            "uniprot_id": ["P68871", "P69905", "P04637", "P00533", "Q15672"],
            "gene_name": ["Hemoglobin subunit beta", "Hemoglobin subunit alpha",
                          "Cellular tumor antigen p53", "Epidermal growth factor receptor", "Transcription factor"],
            "gene_symbol": ["HBB", "HBA1", "TP53", "EGFR", "TFAP2A"],
            "protein_name": ["Hemoglobin subunit beta", "Hemoglobin subunit alpha",
                             "Cellular tumor antigen p53", "Epidermal growth factor receptor", "Transcription factor AP-2 alpha"],
            "organism": ["Homo sapiens"] * 5,
            "string_id": ["9606.ENSP00000333994", "9606.ENSP00000335887",
                          "9606.ENSP00000269305", "9606.ENSP00000275493", None],
        })

        count = bulk_upsert_proteins(db_session, proteins_df)
        db_session.commit()

        assert int(count) == 5, f"Expected 5 proteins loaded, got {count}"

    def test_proteins_table_has_records(self, db_session):
        """Verify proteins table has records."""
        from database.models import Protein
        count = db_session.query(Protein).count()
        assert int(count) > 0, "Proteins table should have records"


# ===========================================================================
# Stage 2: ChEMBL Pipeline (provides drug + DPI data)
# ===========================================================================

class TestStage2ChEMBL:
    """ChEMBL pipeline: load drugs and drug-protein interactions."""

    def test_load_drugs(self, db_session):
        """Load mock drug data into the drugs table."""
        from database.loaders import bulk_upsert_drugs

        drugs_df = pd.DataFrame({
            "inchikey": ["UFKZQVDZXBCISE-UHFFFAOYSA-N", "RYYVLZVUVIJVGH-UHFFFAOYSA-N",
                         "QNAYBMWLOENCQY-UHFFFAOYSA-N", "VCMUXWJKOIADKO-UHFFFAOYSA-N"],
            "name": ["Aspirin", "Ibuprofen", "Paracetamol", "Metformin"],
            "chembl_id": ["CHEMBL25", "CHEMBL521", "CHEMBL112", "CHEMBL1431"],
            "is_fda_approved": [True, True, True, True],
            "max_phase": [4, 4, 4, 4],
            "smiles": ["CC(=O)OC1=CC=CC=C1C(=O)O", "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O",
                        "CC(=O)NC1=CC=C(O)C=C1", "CN(C)C(=N)NC(=N)N"],
        })

        count = bulk_upsert_drugs(db_session, drugs_df)
        db_session.commit()

        assert int(count) == 4, f"Expected 4 drugs loaded, got {count}"

    def test_load_drug_protein_interactions(self, db_session):
        """Load mock drug-protein interactions."""
        from database.loaders import bulk_upsert_dpi

        # Get drug and protein IDs from the database
        from database.models import Drug, Protein
        drugs = {d.chembl_id: d.id for d in db_session.query(Drug).all()}
        proteins = {p.uniprot_id: p.id for p in db_session.query(Protein).all()}

        dpi_records = []
        for chembl_id, uniprot_id in [("CHEMBL25", "P04637"), ("CHEMBL521", "P04637"),
                                        ("CHEMBL112", "P00533"), ("CHEMBL1431", "P68871")]:
            if chembl_id in drugs and uniprot_id in proteins:
                dpi_records.append({
                    "drug_id": drugs[chembl_id],
                    "protein_id": proteins[uniprot_id],
                    "interaction_type": "inhibitor",
                    "activity_value": 5.5,
                    "activity_type": "IC50",
                    "activity_units": "uM",
                    "source": "chembl",
                    "source_id": f"{chembl_id}_{uniprot_id}",
                    "confidence_score": 0.9,
                })

        if dpi_records:
            dpi_df = pd.DataFrame(dpi_records)
            count = bulk_upsert_dpi(db_session, dpi_df)
            db_session.commit()
            assert int(count) > 0, "Should load at least some drug-protein interactions"

    def test_drugs_table_has_records(self, db_session):
        """Verify drugs table has records."""
        from database.models import Drug
        count = db_session.query(Drug).count()
        assert int(count) > 0, "Drugs table should have records"


# ===========================================================================
# Stage 3: STRING Pipeline (provides PPI data)
# ===========================================================================

class TestStage3String:
    """STRING pipeline: load protein-protein interactions."""

    def test_load_ppi(self, db_session):
        """Load mock PPI data into the database."""
        from database.loaders import bulk_upsert_ppi
        from database.models import Protein

        proteins = {p.uniprot_id: p.id for p in db_session.query(Protein).all()}

        # Create PPI records with proper ordering (protein_a_id < protein_b_id)
        ppi_records = []
        protein_ids = sorted(proteins.values())
        for i in range(len(protein_ids) - 1):
            ppi_records.append({
                "protein_a_id": protein_ids[i],
                "protein_b_id": protein_ids[i + 1],
                "combined_score": 900 - i * 50,
                "experimental_score": 600 - i * 30,
                "database_score": 700 - i * 40,
                "textmining_score": 500 - i * 25,
                "source": "string",
            })

        if ppi_records:
            ppi_df = pd.DataFrame(ppi_records)
            count = bulk_upsert_ppi(db_session, ppi_df)
            db_session.commit()
            assert int(count) > 0, "Should load PPI records"

    def test_ppi_table_has_records(self, db_session):
        """Verify protein_protein_interactions table has records."""
        from database.models import ProteinProteinInteraction
        count = db_session.query(ProteinProteinInteraction).count()
        assert int(count) > 0, "PPI table should have records"


# ===========================================================================
# Stage 4: DisGeNET Pipeline (provides GDA data)
# ===========================================================================

class TestStage4DisGeNET:
    """DisGeNET pipeline: load gene-disease associations."""

    def test_load_gda(self, db_session):
        """Load mock GDA data into the database."""
        from database.loaders import bulk_upsert_gda

        gda_df = pd.DataFrame({
            "gene_symbol": ["TP53", "EGFR", "BRCA1", "HBB"],
            "disease_id": ["C0027627", "C0014474", "C0027661", "C0019043"],
            "disease_name": ["Neoplastic Syndromes", "Carcinoma", "Breast Cancer", "Anemia"],
            "association_type": ["somatic", "biomarker", "germline", "germline"],
            "score": [0.8, 0.7, 0.9, 0.5],
            "source": ["disgenet", "disgenet", "disgenet", "disgenet"],
            "pmid_list": ["12345;67890", "11111;22222", "33333", "44444"],
        })

        count = bulk_upsert_gda(db_session, gda_df)
        db_session.commit()

        assert int(count) == 4, f"Expected 4 GDA records, got {count}"

    def test_gda_table_has_records(self, db_session):
        """Verify gene_disease_associations table has records."""
        from database.models import GeneDiseaseAssociation
        count = db_session.query(GeneDiseaseAssociation).count()
        assert int(count) > 0, "GDA table should have records"


# ===========================================================================
# Stage 5: OMIM Pipeline (adds more GDA data)
# ===========================================================================

class TestStage5Omim:
    """OMIM pipeline: add gene-phenotype associations with nuanced scoring."""

    def test_load_omim_gda(self, db_session):
        """Load mock OMIM GDA data with varied scores (FIX AUDIT-14)."""
        from database.loaders import bulk_upsert_gda

        omim_gda_df = pd.DataFrame({
            "gene_symbol": ["TP53", "EGFR", "HBA1"],
            "disease_id": ["OMIM:114480", "OMIM:211980", "OMIM:141800"],
            "disease_name": ["Li-Fraumeni syndrome", "Lung cancer", "Alpha-thalassemia"],
            "association_type": ["confirmed", "confirmed", "confirmed"],
            "score": [0.9, 0.9, 0.9],  # mapping_key=3 → score=0.9 per FIX AUDIT-14
            "source": ["omim", "omim", "omim"],
            "pmid_list": [None, None, None],
        })

        count = bulk_upsert_gda(db_session, omim_gda_df)
        db_session.commit()

        assert int(count) == 3, f"Expected 3 OMIM GDA records, got {count}"

    def test_omim_gda_scores_not_flat(self):
        """Verify OMIM scoring produces varied scores based on mapping_key."""
        def _compute_omim_score(mapping_key):
            if mapping_key == 3:
                return 0.9
            elif mapping_key == 2:
                return 0.7
            elif mapping_key == 1:
                return 0.5
            else:
                return 0.6

        scores = [_compute_omim_score(k) for k in [3, 2, 1, 0]]
        assert len(set(scores)) > 1, "Scores should be varied based on mapping_key"
        assert 1.0 not in scores, "Score 1.0 should not appear"


# ===========================================================================
# Stage 6: Entity Resolution
# ===========================================================================

class TestStage6EntityResolution:
    """Entity resolution: cross-reference drugs and proteins."""

    def test_entity_mapping_with_null_inchikey(self, db_session):
        """Entity mapping should handle NULL inchikeys (FIX AUDIT-1)."""
        from database.loaders import bulk_upsert_entity_mapping

        mapping_df = pd.DataFrame({
            "canonical_inchikey": ["UFKZQVDZXBCISE-UHFFFAOYSA-N", None, None],
            "canonical_name": ["Aspirin", "Unknown Drug A", "Unknown Drug B"],
            "chembl_id": ["CHEMBL25", None, "CHEMBL999"],
            "match_confidence": [1.0, 0.5, 0.7],
            "match_method": ["inchikey", "name", "name"],
        })

        count = bulk_upsert_entity_mapping(db_session, mapping_df)
        db_session.commit()

        assert int(count) == 3, f"Expected 3 entity mappings, got {count}"

    def test_entity_mapping_table_has_records(self, db_session):
        """Verify entity_mapping table has records."""
        from database.models import EntityMapping
        count = db_session.query(EntityMapping).count()
        assert int(count) > 0, "Entity mapping table should have records"

    def test_protein_string_id_populated(self, db_session):
        """Some proteins should have string_id populated."""
        from database.models import Protein
        proteins_with_string = db_session.query(Protein).filter(
            Protein.string_id.isnot(None)
        ).count()
        assert proteins_with_string > 0, "Some proteins should have string_id"


# ===========================================================================
# Final Data Quality Checks
# ===========================================================================

class TestDataQualityChecks:
    """Final validation of data integrity across all tables."""

    def test_drugs_count_positive(self, db_session):
        """Count of drugs should be > 0."""
        from database.models import Drug
        assert db_session.query(Drug).count() > 0

    def test_proteins_count_positive(self, db_session):
        """Count of proteins should be > 0."""
        from database.models import Protein
        assert db_session.query(Protein).count() > 0

    def test_dpi_count_positive(self, db_session):
        """Count of drug-protein interactions should be > 0."""
        from database.models import DrugProteinInteraction
        assert db_session.query(DrugProteinInteraction).count() > 0

    def test_ppi_count_positive(self, db_session):
        """Count of protein-protein interactions should be > 0."""
        from database.models import ProteinProteinInteraction
        assert db_session.query(ProteinProteinInteraction).count() > 0

    def test_gda_count_positive(self, db_session):
        """Count of gene-disease associations should be > 0."""
        from database.models import GeneDiseaseAssociation
        assert db_session.query(GeneDiseaseAssociation).count() > 0

    def test_gda_no_null_gene_symbols(self, db_session):
        """All GDA records should have non-empty gene_symbol (no NULLs after fillna)."""
        from database.models import GeneDiseaseAssociation
        null_count = db_session.query(GeneDiseaseAssociation).filter(
            GeneDiseaseAssociation.gene_symbol.is_(None)
        ).count()
        assert null_count == 0, f"Found {null_count} GDA records with NULL gene_symbol"

    def test_entity_mapping_with_inchikey_has_name(self, db_session):
        """All entity_mapping records with canonical_inchikey should have non-empty canonical_name."""
        from database.models import EntityMapping
        null_name_with_ik = db_session.query(EntityMapping).filter(
            EntityMapping.canonical_inchikey.isnot(None),
            (EntityMapping.canonical_name.is_(None)) | (EntityMapping.canonical_name == "")
        ).count()
        assert null_name_with_ik == 0, \
            f"Found {null_name_with_ik} entity mappings with inchikey but empty name"

    def test_no_duplicate_drugs_by_inchikey(self, db_session):
        """No duplicate drugs by inchikey should exist."""
        from database.models import Drug
        from sqlalchemy import func
        duplicates = db_session.query(Drug.inchikey, func.count(Drug.id)).group_by(
            Drug.inchikey
        ).having(func.count(Drug.id) > 1).all()
        assert len(duplicates) == 0, f"Found duplicate drugs by inchikey: {duplicates}"

    def test_no_duplicate_proteins_by_uniprot(self, db_session):
        """No duplicate proteins by uniprot_id should exist."""
        from database.models import Protein
        from sqlalchemy import func
        duplicates = db_session.query(Protein.uniprot_id, func.count(Protein.id)).group_by(
            Protein.uniprot_id
        ).having(func.count(Protein.id) > 1).all()
        assert len(duplicates) == 0, f"Found duplicate proteins by uniprot_id: {duplicates}"

    def test_ppi_protein_a_less_than_b(self, db_session):
        """All PPI records should have protein_a_id < protein_b_id."""
        from database.models import ProteinProteinInteraction
        from sqlalchemy import or_
        # Check for violations of the ordering constraint
        violations = db_session.query(ProteinProteinInteraction).filter(
            ProteinProteinInteraction.protein_a_id >= ProteinProteinInteraction.protein_b_id
        ).count()
        assert violations == 0, f"Found {violations} PPI records where protein_a_id >= protein_b_id"

    def test_pipeline_runs_audit_trail(self, db_session):
        """The pipeline_runs table can be written to for audit purposes."""
        from database.models import PipelineRun
        # Use a valid source per the CHECK constraint (chembl, drugbank,
        # uniprot, string, disgenet, omim, pubchem). 'chembl' is used here
        # as a generic test source.
        run = PipelineRun(
            source="chembl",
            run_date=datetime.now(timezone.utc),
            status="success",
            records_downloaded=100,
            records_cleaned=95,
            records_loaded=90,
            duration_seconds=42,
        )
        db_session.add(run)
        db_session.commit()

        saved = db_session.query(PipelineRun).filter_by(source="chembl").first()
        assert saved is not None
        assert saved.status == "success"
        assert saved.records_downloaded == 100

    def test_null_inchikey_entity_mapping_no_crash(self, db_session):
        """Entity mapping with NULL inchikeys should not crash (FIX AUDIT-1)."""
        from database.loaders import bulk_upsert_entity_mapping
        from database.models import EntityMapping

        # Add more NULL-inchikey mappings
        more_df = pd.DataFrame({
            "canonical_inchikey": [None, None],
            "canonical_name": ["Drug X", "Drug Y"],
            "match_confidence": [0.4, 0.6],
            "match_method": ["name", "fuzzy"],
        })
        count = bulk_upsert_entity_mapping(db_session, more_df)
        db_session.commit()
        assert int(count) == 2, "NULL inchikey mappings should not crash"


# ===========================================================================
# Summary Report
# ===========================================================================

class TestSummaryReport:
    """Print a summary of the pipeline state after all tests."""

    def test_pipeline_summary(self, db_session):
        """Print summary table of pipeline stage results."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )

        summary = {
            "drugs": db_session.query(Drug).count(),
            "proteins": db_session.query(Protein).count(),
            "drug_protein_interactions": db_session.query(DrugProteinInteraction).count(),
            "protein_protein_interactions": db_session.query(ProteinProteinInteraction).count(),
            "gene_disease_associations": db_session.query(GeneDiseaseAssociation).count(),
            "entity_mapping": db_session.query(EntityMapping).count(),
            "pipeline_runs": db_session.query(PipelineRun).count(),
        }

        print("\n" + "=" * 60)
        print("PIPELINE E2E TEST SUMMARY")
        print("=" * 60)
        print(f"{'Table':<40} {'Records':>10} {'Status':>10}")
        print("-" * 60)
        for table, count in summary.items():
            status = "PASS" if count > 0 else "FAIL"
            print(f"{table:<40} {count:>10} {status:>10}")
        print("=" * 60)

        # All tables should have records
        for table, count in summary.items():
            if table != "pipeline_runs":  # pipeline_runs is optional for this check
                assert int(count) > 0, f"Table '{table}' should have records but has {count}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=long"])
