"""
tests/test_production_loaders.py
================================
Schema compliance tests: verify all pipeline load DataFrames contain ONLY columns
that exist in the corresponding ORM model. These tests catch the root cause of Bugs #1-#4.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.models import (
    Drug,
    DrugProteinInteraction,
    EntityMapping,
    GeneDiseaseAssociation,
    Protein,
    ProteinProteinInteraction,
)


class TestProductionLoaderSchemaCompliance:
    """Verify that all pipeline load DataFrames contain ONLY columns that exist
    in the corresponding ORM model."""

    def _get_model_columns(self, model_class):
        return {c.name for c in inspect(model_class).columns}

    def test_chembl_dpi_columns_match_model(self):
        """ChEMBL DPI DataFrame columns must be subset of DrugProteinInteraction model."""
        model_cols = self._get_model_columns(DrugProteinInteraction)
        dpi_df = pd.DataFrame({
            "drug_id": [1], "protein_id": [1],
            "interaction_type": ["IC50"], "activity_value": [100.0],
            "activity_units": ["nM"], "activity_type": ["IC50"],
            "source": ["chembl"], "source_id": ["12345"],
            "confidence_score": [None],
        })
        for col in dpi_df.columns:
            assert col in model_cols, (
                f"Column '{col}' in ChEMBL DPI DataFrame not in DrugProteinInteraction model. "
                f"Available: {model_cols}"
            )

    def test_chembl_dpi_no_extra_columns(self):
        """ChEMBL DPI must NOT contain action_type, pchembl_value, assay_id (Bug #1)."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        src = (PROJECT_ROOT / "pipelines" / "chembl_pipeline.py").read_text()
        # In _load_activities, the dpi_df must not have these columns
        for bad_col in ['dpi_df["action_type"]', 'dpi_df["pchembl_value"]', 'dpi_df["assay_id"]']:
            assert bad_col not in src, (
                f"REGRESSION: Bug #1 - {bad_col} still in ChEMBL pipeline"
            )

    def test_drugbank_dpi_columns_match_model(self):
        """DrugBank DPI DataFrame columns must be subset of DrugProteinInteraction model."""
        model_cols = self._get_model_columns(DrugProteinInteraction)
        dpi_df = pd.DataFrame({
            "drug_id": [1], "protein_id": [1],
            "interaction_type": ["inhibitor"],
            "activity_value": [None], "activity_units": [None],
            "activity_type": [None],
            "source": ["drugbank"], "source_id": ["DB00001_P23219"],
            "confidence_score": [None],
        })
        for col in dpi_df.columns:
            assert col in model_cols, (
                f"Column '{col}' in DrugBank DPI DataFrame not in DrugProteinInteraction model. "
                f"Available: {model_cols}"
            )

    def test_drugbank_dpi_no_extra_columns(self):
        """DrugBank DPI must NOT contain action_type/pchembl_value/assay_id as separate cols (Bug #2)."""
        src = (PROJECT_ROOT / "pipelines" / "drugbank_pipeline.py").read_text()
        # These should not be separate columns in the dpi_df
        for bad in ['"action_type": interactions_df["action_type"]', '"pchembl_value": None', '"assay_id": None']:
            assert bad not in src, (
                f"REGRESSION: Bug #2 - {bad} still in DrugBank pipeline dpi_df"
            )

    def test_string_ppi_columns_match_model(self):
        """STRING PPI DataFrame columns must be subset of ProteinProteinInteraction model."""
        model_cols = self._get_model_columns(ProteinProteinInteraction)
        ppi_df = pd.DataFrame({
            "protein_a_id": [1], "protein_b_id": [2],
            "combined_score": [900], "experimental_score": [800],
            "database_score": [700], "textmining_score": [600],
            "source": ["string"],
        })
        for col in ppi_df.columns:
            assert col in model_cols, (
                f"Column '{col}' in STRING PPI DataFrame not in ProteinProteinInteraction model. "
                f"Available: {model_cols}"
            )

    def test_string_ppi_no_extra_columns(self):
        """STRING PPI must NOT contain neighborhood/fusion/cooccurrence/coexpression (Bug #3)."""
        src = (PROJECT_ROOT / "pipelines" / "string_pipeline.py").read_text()
        for bad in ['"neighborhood"', '"fusion"', '"cooccurrence"', '"coexpression"']:
            # Check in load method - these should not be in model_columns
            assert bad + "," not in src or "model_columns" not in src.split(bad)[0][-200:], (
                f"REGRESSION: Bug #3 - {bad} still in STRING pipeline load_columns"
            )

    def test_uniprot_protein_columns_match_model(self):
        """UniProt load DataFrame columns must be subset of Protein model."""
        model_cols = self._get_model_columns(Protein)
        load_df = pd.DataFrame({
            "uniprot_id": ["P23219"], "gene_symbol": ["PTGS1"],
            "gene_name": ["Cyclooxygenase-1"], "protein_name": ["PTGS1"],
            "organism": ["Homo sapiens"], "sequence": ["MSSA..."],
            "function_desc": [""], "string_id": [None],
        })
        for col in load_df.columns:
            assert col in model_cols, (
                f"Column '{col}' in UniProt load DataFrame not in Protein model. "
                f"Available: {model_cols}"
            )

    def test_uniprot_load_no_source_source_id(self):
        """UniProt load must NOT include source/source_id columns (Bug #4)."""
        src = (PROJECT_ROOT / "pipelines" / "uniprot_pipeline.py").read_text()
        # In load_columns, source and source_id should not appear
        if "load_columns" in src:
            load_section = src.split("load_columns")[1].split("]")[0]
            assert '"source"' not in load_section, (
                "REGRESSION: Bug #4 - 'source' still in UniProt load_columns"
            )

    def test_gda_columns_match_model(self):
        """GDA load DataFrame columns must be subset of GeneDiseaseAssociation model."""
        model_cols = self._get_model_columns(GeneDiseaseAssociation)
        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1"], "uniprot_id": ["P23219"],
            "disease_id": ["C0003843"], "disease_name": ["Arthritis"],
            "association_type": ["unknown"], "score": [0.85],
            "source": ["disgenet"], "pmid_list": ["12345"],
        })
        for col in gda_df.columns:
            assert col in model_cols, (
                f"Column '{col}' in GDA DataFrame not in GeneDiseaseAssociation model. "
                f"Available: {model_cols}"
            )

    def test_protein_model_has_gene_symbol(self):
        """Protein model must have gene_symbol column (Bug #4)."""
        model_cols = self._get_model_columns(Protein)
        assert "gene_symbol" in model_cols, (
            "Bug #4: gene_symbol column missing from Protein model"
        )

    def test_gda_model_has_unique_constraint(self):
        """GeneDiseaseAssociation must have unique constraint on (gene_symbol, disease_id, source) (Bug #8)."""
        constraints = [c for c in GeneDiseaseAssociation.__table__.constraints 
                       if hasattr(c, 'name') and c.name == "uq_gda_gene_disease_source"]
        assert len(constraints) > 0, (
            "Bug #8: Unique constraint uq_gda_gene_disease_source missing from GeneDiseaseAssociation"
        )

    def test_entity_mapping_model_has_unique_constraint(self):
        """EntityMapping must have unique index on canonical_inchikey (Bug #8)."""
        # EntityMapping now uses partial unique Index instead of UniqueConstraint
        indexes = [idx for idx in EntityMapping.__table__.indexes if idx.name == "uq_entity_mapping_inchikey"]
        assert len(indexes) > 0, (
            "Bug #8: Unique index uq_entity_mapping_inchikey missing from EntityMapping"
        )
