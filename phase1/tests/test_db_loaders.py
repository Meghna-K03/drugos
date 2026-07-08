"""
Comprehensive tests for database loaders using SQLite in-memory.

All upsert operations use SQLite-compatible helpers that replicate the
PostgreSQL-specific ``pg_insert`` logic from ``database.loaders`` but
with SQLite's native ``INSERT OR CONFLICT`` support.

Tests cover:
  - ORM model CRUD operations
  - Bulk upsert insert / update (SQLite-compatible)
  - FK resolution for interactions
  - Lookup map functions
  - Batch processing (>1000 rows)
  - ORM relationship traversal
  - PipelineRun model
  - Schema matches spec
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import inspect, text

# ---------------------------------------------------------------------------
# Ensure project root importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.models import (
    Drug,
    DrugProteinInteraction,
    EntityMapping,
    GeneDiseaseAssociation,
    PipelineRun,
    Protein,
    ProteinProteinInteraction,
)

# Import SQLite-compatible helpers
from tests.db_helpers import (
    get_inchikey_to_drug_id_map,
    get_uniprot_to_protein_id_map,
    sqlite_bulk_update_drugs_from_pubchem,
    sqlite_bulk_upsert_dpi,
    sqlite_bulk_upsert_drugs,
    sqlite_bulk_upsert_entity_mapping,
    sqlite_bulk_upsert_gda,
    sqlite_bulk_upsert_ppi,
    sqlite_bulk_upsert_proteins,
)


# ============================================================================
# Helper: insert prerequisite drugs + proteins and return their IDs
# ============================================================================


def _insert_drugs_and_proteins(session, drug_df, protein_df):
    """Insert drug and protein DataFrames and return {inchikey: drug_id},
    {uniprot_id: protein_id} maps."""
    sqlite_bulk_upsert_drugs(session, drug_df)
    sqlite_bulk_upsert_proteins(session, protein_df)
    return (
        get_inchikey_to_drug_id_map(session),
        get_uniprot_to_protein_id_map(session),
    )


# ============================================================================
# 1. bulk_upsert_drugs — INSERT
# ============================================================================


class TestBulkUpsertDrugsInsert:
    """Insert new drugs and verify row count."""

    def test_insert(self, db_session, sample_drug_df):
        count = sqlite_bulk_upsert_drugs(db_session, sample_drug_df)
        assert count == 2

        rows = db_session.query(Drug).all()
        assert len(rows) == 2
        names = {r.name for r in rows}
        assert "Aspirin" in names
        assert "Ibuprofen" in names


# ============================================================================
# 2. bulk_upsert_drugs — UPDATE (upsert)
# ============================================================================


class TestBulkUpsertDrugsUpdate:
    """Insert drug, then upsert same inchikey with new data; verify updated."""

    def test_update(self, db_session, sample_drug_df):
        # First insert
        sqlite_bulk_upsert_drugs(db_session, sample_drug_df)

        # Now upsert with updated data for Aspirin
        updated_df = pd.DataFrame(
            {
                "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "name": ["Aspirin Updated"],
                "chembl_id": ["CHEMBL25"],
                "drugbank_id": ["DB00945"],
                "pubchem_cid": [2244],
                "molecular_weight": [180.16],
                "is_fda_approved": [True],
                "max_phase": [4],
                "drug_type": ["Small molecule"],
                "mechanism_of_action": ["Updated COX inhibitor"],
            }
        )
        sqlite_bulk_upsert_drugs(db_session, updated_df)

        drug = (
            db_session.query(Drug)
            .filter_by(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
            .first()
        )
        assert drug is not None
        assert drug.name == "Aspirin Updated"
        assert drug.mechanism_of_action == "Updated COX inhibitor"
        # Total drugs should still be 2 (no new row)
        assert db_session.query(Drug).count() == 2


# ============================================================================
# 3. bulk_upsert_drugs — empty DataFrame
# ============================================================================


class TestBulkUpsertDrugsEmpty:
    """Empty DataFrame returns 0."""

    def test_empty_df(self, db_session):
        empty_df = pd.DataFrame(
            columns=[
                "inchikey",
                "name",
                "chembl_id",
                "is_fda_approved",
                "drug_type",
                "max_phase",
            ]
        )
        count = sqlite_bulk_upsert_drugs(db_session, empty_df)
        assert count == 0


# ============================================================================
# 4. bulk_upsert_proteins — INSERT
# ============================================================================


class TestBulkUpsertProteinsInsert:
    """Insert new proteins and verify row count."""

    def test_insert(self, db_session, sample_protein_df):
        count = sqlite_bulk_upsert_proteins(db_session, sample_protein_df)
        assert count == 2

        rows = db_session.query(Protein).all()
        assert len(rows) == 2
        # FIX C4/D9: gene_name stores protein names, gene_symbol stores gene symbols
        gene_symbols = {r.gene_symbol for r in rows}
        assert "PTGS1" in gene_symbols
        assert "TP53" in gene_symbols


# ============================================================================
# 5. bulk_upsert_proteins — UPDATE
# ============================================================================


class TestBulkUpsertProteinsUpdate:
    """Update existing protein with new gene_name."""

    def test_update(self, db_session, sample_protein_df):
        sqlite_bulk_upsert_proteins(db_session, sample_protein_df)

        updated_df = pd.DataFrame(
            {
                "uniprot_id": ["P23219"],
                "gene_name": ["PTGS1_UPDATED"],
                "protein_name": ["Prostaglandin G/H synthase 1 Updated"],
                "organism": ["Homo sapiens"],
            }
        )
        sqlite_bulk_upsert_proteins(db_session, updated_df)

        protein = db_session.query(Protein).filter_by(uniprot_id="P23219").first()
        assert protein is not None
        assert protein.gene_name == "PTGS1_UPDATED"
        assert protein.protein_name == "Prostaglandin G/H synthase 1 Updated"
        # Total proteins should still be 2
        assert db_session.query(Protein).count() == 2


# ============================================================================
# 6. bulk_upsert_dpi — INSERT with FK resolution
# ============================================================================


class TestBulkUpsertDpiInsert:
    """Insert drug-protein interactions with FK resolution."""

    def test_insert(self, db_session, sample_drug_df, sample_protein_df):
        drug_ids, protein_ids = _insert_drugs_and_proteins(
            db_session, sample_drug_df, sample_protein_df
        )

        dpi_df = pd.DataFrame(
            {
                "drug_id": [drug_ids["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]],
                "protein_id": [protein_ids["P23219"]],
                "interaction_type": ["inhibitor"],
                "activity_value": [5000.0],
                "activity_type": ["IC50"],
                "activity_units": ["nM"],
                "source": ["chembl"],
                "source_id": ["ACT_1"],
                "confidence_score": [0.9],
            }
        )
        count = sqlite_bulk_upsert_dpi(db_session, dpi_df)
        assert count == 1

        dpi = db_session.query(DrugProteinInteraction).first()
        assert dpi is not None
        assert dpi.activity_value == 5000.0
        assert dpi.source == "chembl"


# ============================================================================
# 7. bulk_upsert_ppi — INSERT
# ============================================================================


class TestBulkUpsertPpiInsert:
    """Insert protein-protein interactions."""

    def test_insert(self, db_session, sample_protein_df):
        _, protein_ids = _insert_drugs_and_proteins(
            db_session,
            pd.DataFrame(
                columns=["inchikey", "name", "chembl_id", "is_fda_approved", "drug_type", "max_phase"]
            ),
            sample_protein_df,
        )

        ppi_df = pd.DataFrame(
            {
                "protein_a_id": [protein_ids["P23219"]],
                "protein_b_id": [protein_ids["P04637"]],
                "combined_score": [900],
                "experimental_score": [800],
                "database_score": [700],
                "textmining_score": [600],
                "source": ["string"],
            }
        )
        count = sqlite_bulk_upsert_ppi(db_session, ppi_df)
        assert count == 1

        ppi = db_session.query(ProteinProteinInteraction).first()
        assert ppi is not None
        assert ppi.combined_score == 900
        assert ppi.source == "string"


# ============================================================================
# 8. bulk_upsert_gda — INSERT
# ============================================================================


class TestBulkUpsertGdaInsert:
    """Insert gene-disease associations."""

    def test_insert(self, db_session, sample_protein_df):
        sqlite_bulk_upsert_proteins(db_session, sample_protein_df)

        gda_df = pd.DataFrame(
            {
                "gene_symbol": ["TP53"],
                "uniprot_id": ["P04637"],
                "disease_id": ["C0027651"],
                "disease_name": ["Breast Cancer"],
                "association_type": ["somatic"],
                "score": [0.8],
                "source": ["disgenet"],
                "pmid_list": ["12345,67890"],
            }
        )
        count = sqlite_bulk_upsert_gda(db_session, gda_df)
        assert count == 1

        gda = db_session.query(GeneDiseaseAssociation).first()
        assert gda is not None
        assert gda.gene_symbol == "TP53"
        assert gda.score == 0.8
        assert gda.pmid_list == "12345,67890"


# ============================================================================
# 9. bulk_upsert_entity_mapping — INSERT
# ============================================================================


class TestBulkUpsertEntityMappingInsert:
    """Insert entity mapping records."""

    def test_insert(self, db_session):
        em_df = pd.DataFrame(
            {
                "canonical_inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "canonical_name": ["Aspirin"],
                "chembl_id": ["CHEMBL25"],
                "drugbank_id": ["DB00945"],
                "pubchem_cid": [2244],
                "uniprot_id": [None],
                "string_id": [None],
                "match_confidence": [1.0],
                "match_method": ["inchikey_exact"],
            }
        )
        count = sqlite_bulk_upsert_entity_mapping(db_session, em_df)
        assert count == 1

        em = db_session.query(EntityMapping).first()
        assert em is not None
        assert em.canonical_name == "Aspirin"
        assert em.match_confidence == 1.0


# ============================================================================
# 10. bulk_update_drugs_from_pubchem
# ============================================================================


class TestBulkUpdateDrugsFromPubchem:
    """Insert drug without pubchem_cid, then update with pubchem data."""

    def test_update(self, db_session):
        # Insert a drug WITHOUT pubchem_cid
        drug_df = pd.DataFrame(
            {
                "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "name": ["Aspirin"],
                "chembl_id": ["CHEMBL25"],
                "is_fda_approved": [True],
                "drug_type": ["Small molecule"],
                "max_phase": [4],
            }
        )
        sqlite_bulk_upsert_drugs(db_session, drug_df)

        # Verify pubchem_cid is None
        drug = db_session.query(Drug).filter_by(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N").first()
        assert drug.pubchem_cid is None

        # Now update with PubChem data
        pubchem_df = pd.DataFrame(
            {
                "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "pubchem_cid": [2244],
                "molecular_formula": ["C9H8O4"],
                "molecular_weight": [180.16],
                "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            }
        )
        sqlite_bulk_update_drugs_from_pubchem(db_session, pubchem_df)

        drug = db_session.query(Drug).filter_by(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N").first()
        assert drug.pubchem_cid == 2244
        assert drug.molecular_formula == "C9H8O4"
        # K fix: SQLite stores Numeric as Decimal; compare via float() for
        # cross-dialect portability (PostgreSQL returns float).
        assert float(drug.molecular_weight) == 180.16


# ============================================================================
# 11. get_uniprot_to_protein_id_map
# ============================================================================


class TestGetUniprotToProteinIdMap:
    """Insert proteins, verify map returned correctly."""

    def test_map(self, db_session, sample_protein_df):
        sqlite_bulk_upsert_proteins(db_session, sample_protein_df)

        mapping = get_uniprot_to_protein_id_map(db_session)
        assert "P23219" in mapping
        assert "P04637" in mapping
        assert isinstance(mapping["P23219"], int)
        assert isinstance(mapping["P04637"], int)


# ============================================================================
# 12. get_inchikey_to_drug_id_map
# ============================================================================


class TestGetInchikeyToDrugIdMap:
    """Insert drugs, verify map returned correctly."""

    def test_map(self, db_session, sample_drug_df):
        sqlite_bulk_upsert_drugs(db_session, sample_drug_df)

        mapping = get_inchikey_to_drug_id_map(db_session)
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in mapping
        assert "WFXAZNNJSJXTJZ-UHFFFAOYSA-N" in mapping
        assert isinstance(mapping["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"], int)


# ============================================================================
# 13. Bulk upsert batch processing (>1000 drugs)
# ============================================================================


class TestBulkUpsertBatchProcessing:
    """Insert >1000 drugs, verify all processed in batches."""

    def test_batch_processing(self, db_session):
        n = 1500
        drug_data = pd.DataFrame(
            {
                "inchikey": [f"BATCHKEY{i:014d}-UHFFFAOYSA-N"[:27] for i in range(n)],
                "name": [f"Drug_{i}" for i in range(n)],
                "chembl_id": [f"CHEMBL_{i}" for i in range(n)],
                "is_fda_approved": [False] * n,
                "drug_type": ["Small molecule"] * n,
                "max_phase": [0] * n,
            }
        )
        count = sqlite_bulk_upsert_drugs(db_session, drug_data, batch_size=500)
        assert count == n

        row_count = db_session.query(Drug).count()
        assert row_count == n

        # Spot-check a few records
        first = db_session.query(Drug).filter_by(name="Drug_0").first()
        assert first is not None
        last = db_session.query(Drug).filter_by(name="Drug_1499").first()
        assert last is not None


# ============================================================================
# 14. Model relationships
# ============================================================================


class TestModelRelationships:
    """Create Drug + Protein + DPI, verify ORM relationships work."""

    def test_drug_dpi_relationship(self, db_session, sample_drug_df, sample_protein_df):
        drug_ids, protein_ids = _insert_drugs_and_proteins(
            db_session, sample_drug_df, sample_protein_df
        )

        dpi_df = pd.DataFrame(
            {
                "drug_id": [
                    drug_ids["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                    drug_ids["WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
                ],
                "protein_id": [
                    protein_ids["P23219"],
                    protein_ids["P04637"],
                ],
                "interaction_type": ["inhibitor", "inhibitor"],
                "activity_value": [5000.0, 10000.0],
                "activity_type": ["IC50", "IC50"],
                "activity_units": ["nM", "nM"],
                "source": ["chembl", "chembl"],
                "source_id": ["ACT_1", "ACT_2"],
                "confidence_score": [0.9, 0.8],
            }
        )
        sqlite_bulk_upsert_dpi(db_session, dpi_df)

        # Traverse the relationship from Drug -> DPI
        drug = (
            db_session.query(Drug)
            .filter_by(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
            .first()
        )
        assert drug is not None
        assert len(drug.drug_protein_interactions) >= 1
        dpi = drug.drug_protein_interactions[0]
        assert dpi.activity_value == 5000.0

        # Traverse DPI -> Protein
        assert dpi.protein is not None
        assert dpi.protein.uniprot_id == "P23219"

        # Traverse Protein -> DPI
        protein = db_session.query(Protein).filter_by(uniprot_id="P23219").first()
        assert protein is not None
        assert len(protein.drug_protein_interactions) >= 1


# ============================================================================
# 15. PipelineRun model
# ============================================================================


class TestPipelineRunModel:
    """Create PipelineRun, verify all fields stored correctly."""

    def test_all_fields(self, db_session):
        now = datetime.now(timezone.utc)
        run = PipelineRun(
            source="chembl",
            run_date=now,
            status="success",
            records_downloaded=1500,
            records_cleaned=1200,
            records_loaded=1100,
            error_message=None,
            duration_seconds=45,
        )
        db_session.add(run)
        db_session.commit()

        retrieved = db_session.query(PipelineRun).first()
        assert retrieved.source == "chembl"
        assert retrieved.status == "success"
        assert retrieved.records_downloaded == 1500
        assert retrieved.records_cleaned == 1200
        assert retrieved.records_loaded == 1100
        assert retrieved.error_message is None
        assert retrieved.duration_seconds == 45


# ============================================================================
# 16. Schema matches spec
# ============================================================================


class TestSchemaMatchesSpec:
    """Verify all model columns match the spec exactly."""

    def _get_columns(self, model_class) -> set:
        """Return the set of column names for a model class."""
        return {c.name for c in inspect(model_class).columns}

    def test_drug_columns(self):
        cols = self._get_columns(Drug)
        required = {
            "id",
            "inchikey",
            "name",
            "chembl_id",
            "drugbank_id",
            "pubchem_cid",
            "molecular_formula",
            "molecular_weight",
            "smiles",
            "is_fda_approved",
            "max_phase",
            "drug_type",
            "mechanism_of_action",
            "created_at",
            "updated_at",
        }
        assert required.issubset(cols), f"Drug missing columns: {required - cols}"

    def test_protein_columns(self):
        cols = self._get_columns(Protein)
        required = {
            "id",
            "uniprot_id",
            "gene_name",
            "protein_name",
            "organism",
            "sequence",
            "function_desc",
            "string_id",
            "created_at",
        }
        assert required.issubset(cols), f"Protein missing columns: {required - cols}"

    def test_dpi_columns(self):
        cols = self._get_columns(DrugProteinInteraction)
        required = {
            "id",
            "drug_id",
            "protein_id",
            "interaction_type",
            "activity_value",
            "activity_type",
            "activity_units",
            "source",
            "source_id",
            "confidence_score",
            "created_at",
        }
        assert required.issubset(cols), f"DPI missing columns: {required - cols}"

    def test_ppi_columns(self):
        cols = self._get_columns(ProteinProteinInteraction)
        required = {
            "id",
            "protein_a_id",
            "protein_b_id",
            "combined_score",
            "experimental_score",
            "database_score",
            "textmining_score",
            "source",
            "created_at",
        }
        assert required.issubset(cols), f"PPI missing columns: {required - cols}"

    def test_gda_columns(self):
        cols = self._get_columns(GeneDiseaseAssociation)
        required = {
            "id",
            "gene_symbol",
            "uniprot_id",
            "disease_id",
            "disease_name",
            "association_type",
            "score",
            "source",
            "pmid_list",
            "created_at",
        }
        assert required.issubset(cols), f"GDA missing columns: {required - cols}"

    def test_entity_mapping_columns(self):
        cols = self._get_columns(EntityMapping)
        required = {
            "id",
            "canonical_inchikey",
            "canonical_name",
            "chembl_id",
            "drugbank_id",
            "pubchem_cid",
            "uniprot_id",
            "string_id",
            "match_confidence",
            "match_method",
            "created_at",
        }
        assert required.issubset(cols), f"EntityMapping missing columns: {required - cols}"

    def test_pipeline_run_columns(self):
        cols = self._get_columns(PipelineRun)
        required = {
            "id",
            "source",
            "run_date",
            "status",
            "records_downloaded",
            "records_cleaned",
            "records_loaded",
            "error_message",
            "duration_seconds",
        }
        assert required.issubset(cols), f"PipelineRun missing columns: {required - cols}"
