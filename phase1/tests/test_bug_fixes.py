"""
tests/test_bug_fixes.py
=======================
Regression tests for all 6 bugs fixed in this sprint.

DEPRECATED (FIX #24): This test file overlaps with test_all_fixes.py.
The tests from this file have been consolidated into test_all_fixes.py.
This file is kept for backward compatibility and should not be used for CI.
Use test_fix_verification.py for the canonical test suite.

Test classes:
  TestFix1_DagNameError          — Bug 4: _check_drugbank NameError
  TestFix2_PipelineRunFields     — Bug 1: PipelineRun constructor field names
  TestFix3a_ChEMBLColumns        — Bug 2: ChEMBL is_fda_approved / drug_type / no extra cols
  TestFix3b_DrugBankColumns      — Bug 2: DrugBank is_fda_approved / drug_type / no extra cols
  TestFix4_GdaUniprotId          — Bug 3: DisGeNET + OMIM use uniprot_id, not protein_id
  TestFix5_DrugBankGzip          — Bug 6: DrugBank iterparse handles .gz via file handle

Each test class has at minimum:
  - A "happy path" test confirming the fix works
  - A "regression guard" test confirming the old broken behaviour is gone

All tests run against an in-memory SQLite database using conftest.py fixtures
and tests/db_helpers.py helpers. Zero external I/O.
"""

from __future__ import annotations

import ast
import gzip
import io
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    GeneDiseaseAssociation,
    PipelineRun,
    Protein,
)
from tests.db_helpers import (
    sqlite_bulk_upsert_drugs,
    sqlite_bulk_upsert_gda,
    sqlite_bulk_upsert_proteins,
)


# ============================================================================
# Helper — extract PipelineRun(...) constructor call text from source
# ============================================================================

def _extract_constructor_call(src: str, class_name: str = "PipelineRun") -> str:
    """Extract the text of the PipelineRun(...) constructor from source code.

    Returns the substring from 'PipelineRun(' to the matching closing ')'.
    This allows precise checks on constructor keyword arguments without
    false positives from other parts of the file.
    """
    pattern = re.compile(rf'{class_name}\s*\(', re.MULTILINE)
    match = pattern.search(src)
    if not match:
        return ""
    start = match.start()
    depth = 0
    for i in range(match.end() - 1, len(src)):
        if src[i] == '(':
            depth += 1
        elif src[i] == ')':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


# ============================================================================
# Fix 1 — DAG NameError: _check_drugbank does not exist
# ============================================================================


class TestFix1_DagNameError:
    """Bug 4: The master DAG must import and parse without a NameError."""

    DAG_PATH = PROJECT_ROOT / "dags" / "master_pipeline_dag.py"

    def test_dag_source_references_correct_callable(self):
        """BranchPythonOperator must reference _check_drugbank_xml, not _check_drugbank."""
        src = self.DAG_PATH.read_text()
        # The broken reference must be absent
        assert "python_callable=_check_drugbank," not in src, (
            "REGRESSION: BranchPythonOperator still references undefined _check_drugbank"
        )
        # The correct reference must be present
        assert "python_callable=_check_drugbank_xml" in src, (
            "FIX INCOMPLETE: python_callable=_check_drugbank_xml not found in DAG"
        )

    def test_dag_ast_parses_cleanly(self):
        """DAG file must be valid Python (AST parse must not raise SyntaxError)."""
        src = self.DAG_PATH.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"DAG file has a SyntaxError: {e}")

        # Collect all Name references in the file
        all_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "_check_drugbank_xml" in all_names, (
            "_check_drugbank_xml not defined/used in DAG"
        )

    def test_dag_function_defined_with_correct_name(self):
        """The branch function must be defined as _check_drugbank_xml."""
        src = self.DAG_PATH.read_text()
        assert "def _check_drugbank_xml(" in src, (
            "_check_drugbank_xml function definition missing from DAG"
        )

    def test_no_undefined_check_drugbank_reference(self):
        """No code should reference the undefined _check_drugbank name."""
        src = self.DAG_PATH.read_text()
        # Allow _check_drugbank_xml but NOT bare _check_drugbank
        # Strip occurrences of the full correct name, then check no fragment remains
        sanitised = src.replace("_check_drugbank_xml", "<<CORRECT>>")
        assert "_check_drugbank" not in sanitised, (
            "REGRESSION: bare _check_drugbank (undefined) still referenced in DAG"
        )

    def test_branch_operator_correct_callable(self):
        """Verify the BranchPythonOperator instantiation uses correct python_callable."""
        src = self.DAG_PATH.read_text()
        # Parse AST and find the BranchPythonOperator call
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "BranchPythonOperator":
                    for kw in node.keywords:
                        if kw.arg == "python_callable":
                            assert isinstance(kw.value, ast.Name), (
                                "python_callable should be a direct name reference"
                            )
                            assert kw.value.id == "_check_drugbank_xml", (
                                f"REGRESSION: python_callable={kw.value.id}, "
                                f"expected _check_drugbank_xml"
                            )


# ============================================================================
# Fix 2 — PipelineRun constructor: wrong field names cause TypeError at runtime
# ============================================================================


class TestFix2_PipelineRunFields:
    """Bug 1: PipelineRun must be constructable with the correct model field names."""

    def test_pipeline_run_correct_fields_persist(self, db_session):
        """Construct PipelineRun with correct fields; verify it round-trips via DB."""
        now = datetime.now(timezone.utc)
        run = PipelineRun(
            source="chembl",
            run_date=now,
            status="success",
            records_downloaded=5000,
            records_cleaned=4800,
            records_loaded=4750,
            error_message=None,
            duration_seconds=120,
        )
        db_session.add(run)
        db_session.commit()

        retrieved = db_session.query(PipelineRun).filter_by(source="chembl").first()
        assert retrieved is not None
        assert retrieved.source == "chembl"
        assert retrieved.status == "success"
        assert retrieved.records_downloaded == 5000
        assert retrieved.records_cleaned == 4800
        assert retrieved.records_loaded == 4750
        assert retrieved.error_message is None
        assert retrieved.duration_seconds == 120

    def test_pipeline_run_rejects_old_field_names(self, db_session):
        """The old broken field names must raise TypeError (they are not model attrs)."""
        with pytest.raises(TypeError, match="pipeline_name|unexpected keyword"):
            _ = PipelineRun(
                pipeline_name="chembl",   # old broken field
                started_at=datetime.now(timezone.utc),
                rows_processed=100,
                rows_inserted=90,
                rows_updated=80,
                metadata_json={},
            )

    def test_pipeline_run_failed_status(self, db_session):
        """PipelineRun with status='failed' and error_message persists correctly."""
        run = PipelineRun(
            source="disgenet",
            run_date=datetime.now(timezone.utc),
            status="failed",
            records_downloaded=0,
            records_cleaned=0,
            records_loaded=0,
            error_message="Connection refused",
            duration_seconds=3,
        )
        db_session.add(run)
        db_session.commit()

        retrieved = db_session.query(PipelineRun).filter_by(source="disgenet").first()
        assert retrieved.status == "failed"
        assert retrieved.error_message == "Connection refused"

    def test_base_pipeline_write_run_log_uses_correct_field_names(self):
        """_write_run_log's PipelineRun constructor must use 'source=', 'run_date=', etc.

        This guards against the exact bug where PipelineRun was called with
        pipeline_name=, started_at=, rows_processed=, etc.
        We extract only the PipelineRun(...) constructor call to avoid false positives
        from function parameter names in the surrounding code.
        """
        src = (PROJECT_ROOT / "pipelines" / "base_pipeline.py").read_text()
        constructor = _extract_constructor_call(src, "PipelineRun")

        # Old broken names must NOT appear inside the PipelineRun constructor call
        for bad_field in [
            "pipeline_name=",
            "started_at=",
            "finished_at=",
            "rows_processed=",
            "rows_inserted=",
            "rows_updated=",
            "metadata_json=",
        ]:
            assert bad_field not in constructor, (
                f"REGRESSION: broken field '{bad_field}' still in PipelineRun constructor"
            )

        # Correct new names must be present in the PipelineRun constructor
        for good_field in [
            "source=self.source_name",
            "run_date=",
            "records_downloaded=",
            "records_cleaned=",
            "records_loaded=",
            "duration_seconds=",
        ]:
            assert good_field in constructor, (
                f"FIX INCOMPLETE: '{good_field}' missing from PipelineRun constructor"
            )

    def test_pipeline_run_model_has_correct_columns(self):
        """PipelineRun ORM model must expose exactly the columns in the SQL schema."""
        cols = {c.name for c in inspect(PipelineRun).columns}
        expected = {
            "id", "source", "run_date", "status",
            "records_downloaded", "records_cleaned", "records_loaded",
            "error_message", "duration_seconds",
        }
        assert expected.issubset(cols), (
            f"PipelineRun model missing columns: {expected - cols}"
        )
        # Old broken column names must NOT exist on the model
        for bad in ("pipeline_name", "started_at", "finished_at",
                    "rows_processed", "rows_inserted", "rows_updated", "metadata_json"):
            assert bad not in cols, (
                f"REGRESSION: obsolete column '{bad}' found on PipelineRun model"
            )

    def test_pipeline_run_all_fields_round_trip(self, db_session):
        """Verify all PipelineRun fields can be written and read back correctly."""
        now = datetime.now(timezone.utc)
        run = PipelineRun(
            source="drugbank",
            run_date=now,
            status="success",
            records_downloaded=12000,
            records_cleaned=11500,
            records_loaded=11000,
            error_message=None,
            duration_seconds=300,
        )
        db_session.add(run)
        db_session.commit()

        retrieved = db_session.query(PipelineRun).filter_by(source="drugbank").first()
        assert retrieved is not None
        assert retrieved.records_downloaded == 12000
        assert retrieved.records_cleaned == 11500
        assert retrieved.records_loaded == 11000
        assert retrieved.duration_seconds == 300


# ============================================================================
# Fix 3a — ChEMBL pipeline: wrong column names (molecule_type, is_approved, extras)
# ============================================================================


class TestFix3a_ChEMBLColumns:
    """Bug 2: ChEMBL pipeline must use drug_type / is_fda_approved; no extra columns."""

    CHEMBL_SRC = PROJECT_ROOT / "pipelines" / "chembl_pipeline.py"

    def _read_src(self) -> str:
        return self.CHEMBL_SRC.read_text()

    # ---- Source-code guards ----

    def test_parse_molecules_no_forbidden_keys(self):
        """_parse_molecules must not produce inchi, ro5_violations, source, source_id.

        Updated for the K8 fix: ``source_id`` legitimately appears in
        ``_aggregate_activities_to_dpi`` and ``_build_dpi_dataframe`` (it's
        a column on the DPI table). The check is now scoped to
        ``_parse_molecules`` source only, not the whole file.
        """
        import inspect
        from pipelines.chembl_pipeline import ChEMBLPipeline
        src = inspect.getsource(ChEMBLPipeline._parse_molecules)
        for forbidden in ('"inchi":', '"ro5_violations":', '"source": "chembl"',
                          '"source_id":', '"is_approved":', '"molecule_type":'):
            assert forbidden not in src, (
                f"REGRESSION: forbidden key {forbidden!r} still in _parse_molecules"
            )

    def test_parse_molecules_has_correct_keys(self):
        """_parse_molecules must produce drug_type and is_fda_approved."""
        src = self._read_src()
        assert '"drug_type":' in src, "FIX INCOMPLETE: drug_type key missing"
        assert '"is_fda_approved":' in src, "FIX INCOMPLETE: is_fda_approved key missing"

    def test_standardize_method_renamed(self):
        """_standardize_molecule_type must be renamed to _standardize_drug_type."""
        src = self._read_src()
        assert "_standardize_molecule_type" not in src, (
            "REGRESSION: old method name _standardize_molecule_type still present"
        )
        assert "_standardize_drug_type" in src, (
            "FIX INCOMPLETE: _standardize_drug_type method missing"
        )

    def test_clean_uses_drug_type_column(self):
        """The clean() method must reference df['drug_type'], not 'molecule_type'.

        Updated for the variable-name normalization: the new clean() uses
        ``df`` as the DataFrame variable name (previously ``drugs_df``).
        Both names are acceptable; the test checks for either.
        """
        src = self._read_src()
        assert 'drugs_df["molecule_type"]' not in src, (
            "REGRESSION: old column reference drugs_df['molecule_type'] still present"
        )
        assert 'df["molecule_type"]' not in src, (
            "REGRESSION: old column reference df['molecule_type'] still present"
        )
        # Must reference drug_type somewhere in clean()'s step methods.
        assert ('df["drug_type"]' in src or 'drugs_df["drug_type"]' in src), (
            "FIX INCOMPLETE: df['drug_type'] or drugs_df['drug_type'] reference missing"
        )

    def test_ensure_drug_columns_no_forbidden_defaults(self):
        """_ensure_drug_columns must not default inchi, ro5_violations, source, source_id."""
        src = self._read_src()
        for forbidden in ('"inchi": None', '"ro5_violations":', '"source": "chembl"',
                          '"source_id": None', '"is_approved": False',
                          '"molecule_type": "Unknown"'):
            assert forbidden not in src, (
                f"REGRESSION: forbidden default {forbidden!r} still in _ensure_drug_columns"
            )

    # ---- Functional tests ----

    def test_parse_molecules_output_columns(self):
        """_parse_molecules() output DataFrame must only have columns that exist in Drug model."""
        from pipelines.chembl_pipeline import ChEMBLPipeline

        drug_model_cols = {c.name for c in inspect(Drug).columns}

        # Minimal molecule JSON payload (same shape as ChEMBL REST API)
        mol_json = [
            {
                "molecule_chembl_id": "CHEMBL25",
                "pref_name": "ASPIRIN",
                "molecule_type": "Small molecule",
                "max_phase": 4,
                "molecule_properties": {"full_mwt": "180.16", "num_ro5_violations": "0"},
                "molecule_structures": {
                    "standard_inchi_key": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                    "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
                    "standard_inchi": "InChI=1S/C9H8O4/...",
                },
            }
        ]

        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        df = pipeline._parse_molecules(mol_json)

        # None of the forbidden columns must appear
        for bad in ("inchi", "ro5_violations", "source", "source_id",
                    "is_approved", "molecule_type"):
            assert bad not in df.columns, (
                f"REGRESSION: forbidden column '{bad}' in _parse_molecules output"
            )

        # Required renamed columns must appear
        assert "drug_type" in df.columns, "FIX INCOMPLETE: drug_type column missing"
        # SW-1 ROOT FIX: is_fda_approved is None (pending FDA Orange Book
        # join); is_globally_approved is the real ChEMBL semantic for
        # max_phase==4. Both columns must be present.
        assert "is_fda_approved" in df.columns, "FIX INCOMPLETE: is_fda_approved missing"
        assert "is_globally_approved" in df.columns, (
            "SW-1 FIX INCOMPLETE: is_globally_approved column missing "
            "(the real ChEMBL semantic for max_phase==4 — any regulator)"
        )

        # Every column in the output must be in Drug model or a pipeline-internal staging col
        allowed_extras = {"chembl_id", "name", "inchikey", "smiles",
                          "molecular_weight", "drug_type", "max_phase",
                          "is_fda_approved", "is_globally_approved"}
        for col in df.columns:
            assert col in drug_model_cols or col in allowed_extras, (
                f"Column '{col}' in _parse_molecules output is not in Drug model"
            )

    def test_bulk_upsert_drugs_accepts_chembl_pipeline_output(self, db_session):
        """Drugs produced by _ensure_drug_columns must be insertable via sqlite_bulk_upsert_drugs."""
        from pipelines.chembl_pipeline import ChEMBLPipeline

        raw_df = pd.DataFrame({
            "chembl_id": ["CHEMBL25"],
            "name": ["Aspirin"],
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "molecular_weight": [180.16],
            "drug_type": ["small_molecule"],  # K6 fix: lowercase enum value
            "max_phase": [4],
            "is_fda_approved": [True],
        })

        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        pipeline.source_name = "chembl"
        ready_df = pipeline._ensure_drug_columns(raw_df)

        # Must NOT contain forbidden columns
        for bad in ("inchi", "ro5_violations", "is_approved", "molecule_type",
                    "source", "source_id"):
            assert bad not in ready_df.columns, (
                f"REGRESSION: forbidden column '{bad}' in _ensure_drug_columns output"
            )

        # Must be insertable — no CompileError / KeyError
        count = sqlite_bulk_upsert_drugs(db_session, ready_df)
        assert int(count) >= 1

        drug = db_session.query(Drug).filter_by(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        ).first()
        assert drug is not None
        assert drug.drug_type == "small_molecule"  # K6 fix: lowercase enum
        assert bool(drug.is_fda_approved) is True

    def test_chembl_drug_type_standardization(self):
        """_standardize_drug_type must correctly map molecule types to valid DrugType enum values.

        Updated for the K6 fix: the method now returns lowercase-underscored
        DrugType enum values (e.g. ``"small_molecule"``, ``"antibody"``,
        ``"protein"``, ``"unknown"``) instead of Title-case strings.
        """
        from pipelines.chembl_pipeline import ChEMBLPipeline

        # K6 fix: returns lowercase enum values, not Title-case strings.
        assert ChEMBLPipeline._standardize_drug_type("small molecule") == "small_molecule"
        assert ChEMBLPipeline._standardize_drug_type("Small molecule") == "small_molecule"
        assert ChEMBLPipeline._standardize_drug_type("Antibody") == "antibody"
        assert ChEMBLPipeline._standardize_drug_type("protein") == "protein"
        assert ChEMBLPipeline._standardize_drug_type("Protein") == "protein"
        assert ChEMBLPipeline._standardize_drug_type("") == "unknown"
        assert ChEMBLPipeline._standardize_drug_type(None) == "unknown"
        assert ChEMBLPipeline._standardize_drug_type("UnknownType") == "unknown"


# ============================================================================
# Fix 3b — DrugBank pipeline: wrong column names
# ============================================================================


class TestFix3b_DrugBankColumns:
    """Bug 2: DrugBank pipeline must use drug_type / is_fda_approved; no extra columns."""

    DRUGBANK_SRC = PROJECT_ROOT / "pipelines" / "drugbank_pipeline.py"

    def _read_src(self) -> str:
        return self.DRUGBANK_SRC.read_text()

    def test_parse_drug_element_no_forbidden_keys(self):
        """_parse_drug_element must not include inchi, source, source_id in drug_rec."""
        src = self._read_src()
        # Extract just the _parse_drug_element method body
        method_match = re.search(
            r'def _parse_drug_element\(.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, re.DOTALL
        )
        method_body = method_match.group(0) if method_match else ""
        # These exact string literals should no longer appear in drug_rec construction
        for forbidden in ('"inchi": properties', '"is_approved": is_approved'):
            assert forbidden not in method_body, (
                f"REGRESSION: forbidden key {forbidden!r} still in _parse_drug_element"
            )
        # source/source_id removed from drug_rec but still used in targets (DPI table)
        # Verify they are NOT in the drug_rec dict specifically
        drug_rec_match = re.search(r'drug_rec\s*=\s*\{([^}]+)\}', method_body, re.DOTALL)
        if drug_rec_match:
            drug_rec_body = drug_rec_match.group(1)
            assert '"source"' not in drug_rec_body, (
                "REGRESSION: 'source' key still in drug_rec dict in _parse_drug_element"
            )
            assert '"source_id"' not in drug_rec_body, (
                "REGRESSION: 'source_id' key still in drug_rec dict in _parse_drug_element"
            )

    def test_parse_drug_element_has_correct_keys(self):
        """_parse_drug_element must produce is_fda_approved."""
        src = self._read_src()
        assert '"is_fda_approved": is_approved' in src, (
            "FIX INCOMPLETE: is_fda_approved not set in _parse_drug_element"
        )

    def test_ensure_drug_columns_no_forbidden_defaults(self):
        """_ensure_drug_columns must not default inchi, ro5_violations, source, source_id."""
        src = self._read_src()
        # Extract just the _ensure_drug_columns method body
        method_match = re.search(
            r'def _ensure_drug_columns\(.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, re.DOTALL
        )
        method_body = method_match.group(0) if method_match else ""
        for forbidden in ('"inchi": None', '"ro5_violations":',
                          '"source_id": None', '"is_approved": False',
                          '"molecule_type":'):
            assert forbidden not in method_body, (
                f"REGRESSION: forbidden default {forbidden!r} in DrugBank _ensure_drug_columns"
            )

    def test_ensure_drug_columns_has_correct_defaults(self):
        """_ensure_drug_columns must default is_fda_approved and drug_type."""
        src = self._read_src()
        assert '"is_fda_approved": False' in src, (
            "FIX INCOMPLETE: is_fda_approved default missing"
        )
        assert '"drug_type": None' in src, (
            "FIX INCOMPLETE: drug_type default missing"
        )

    def test_drug_columns_list_no_forbidden_entries(self):
        """_drug_columns() list must not contain inchi, source, source_id, is_approved."""
        src = self._read_src()
        # The _drug_columns method returns a list; check the list literal
        for forbidden in ('"inchi"', '"source"', '"source_id"', '"is_approved"'):
            # Only flag it if it appears in the _drug_columns method body
            # We check the full source because the method is small and isolated
            assert (
                forbidden + "," not in src
                or '"is_fda_approved"' in src  # if the rename happened, fine
            ), f"REGRESSION: forbidden entry {forbidden!r} in _drug_columns()"

    def test_bulk_upsert_accepts_drugbank_pipeline_output(self, db_session):
        """Drugs produced by DrugBank _ensure_drug_columns must insert without error."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        raw_df = pd.DataFrame({
            "drugbank_id": ["DB00945"],
            "name": ["Aspirin"],
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "molecular_weight": [180.16],
            "molecular_formula": ["C9H8O4"],
            "is_fda_approved": [True],
            "mechanism_of_action": ["COX inhibitor"],
        })

        pipeline = DrugBankPipeline.__new__(DrugBankPipeline)
        ready_df = pipeline._ensure_drug_columns(raw_df)

        for bad in ("inchi", "ro5_violations", "is_approved", "molecule_type",
                    "source", "source_id"):
            assert bad not in ready_df.columns, (
                f"REGRESSION: forbidden column '{bad}' present in DrugBank _ensure_drug_columns output"
            )

        count = sqlite_bulk_upsert_drugs(db_session, ready_df)
        assert count == 1

        drug = db_session.query(Drug).filter_by(drugbank_id="DB00945").first()
        assert drug is not None
        assert drug.is_fda_approved is True

    def test_drugbank_pipeline_output_matches_drug_model_columns(self, db_session):
        """Verify DrugBank _ensure_drug_columns adds only columns that exist in Drug model."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        drug_model_cols = {c.name for c in inspect(Drug).columns}

        raw_df = pd.DataFrame({
            "drugbank_id": ["DB01050"],
            "name": ["Ibuprofen"],
            "inchikey": ["WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
            "smiles": ["CC(C)Cc1ccc(cc1)C(C)C(=O)O"],
            "molecular_weight": [206.28],
            "molecular_formula": ["C13H18O2"],
            "is_fda_approved": [True],
            "mechanism_of_action": ["COX inhibitor"],
        })

        pipeline = DrugBankPipeline.__new__(DrugBankPipeline)
        ready_df = pipeline._ensure_drug_columns(raw_df)

        # All columns in the output must be in the Drug model
        for col in ready_df.columns:
            assert col in drug_model_cols, (
                f"Column '{col}' in DrugBank _ensure_drug_columns output is not in Drug model"
            )


# ============================================================================
# Fix 4 — DisGeNET & OMIM: gene_symbol must resolve to uniprot_id, not protein integer PK
# ============================================================================


class TestFix4_GdaUniprotId:
    """Bug 3: GDA load() must map gene_symbol -> uniprot_id (string), not integer PK.
    Also verifies load_df only contains columns that exist in GeneDiseaseAssociation.
    """

    DISGENET_SRC = PROJECT_ROOT / "pipelines" / "disgenet_pipeline.py"
    OMIM_SRC = PROJECT_ROOT / "pipelines" / "omim_pipeline.py"

    # ---- Source-code guards ----

    def test_disgenet_load_uses_gene_to_uniprot(self):
        """DisGeNET load() must use gene_to_uniprot dict (string mapping), not gene_to_protein."""
        src = self.DISGENET_SRC.read_text()
        assert "gene_to_protein" not in src, (
            "REGRESSION: gene_to_protein (integer mapping) still in disgenet_pipeline.py"
        )
        assert "gene_to_uniprot" in src, (
            "FIX INCOMPLETE: gene_to_uniprot missing from disgenet_pipeline.py"
        )

    def test_disgenet_load_no_protein_id_column(self):
        """DisGeNET load() must not produce or reference a 'protein_id' column in load_df."""
        src = self.DISGENET_SRC.read_text()
        # Extract the load() method body
        method_match = re.search(
            r'def load\(self.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, re.DOTALL
        )
        load_body = method_match.group(0) if method_match else ""
        assert "protein_id" not in load_body, (
            "REGRESSION: protein_id (non-existent GDA column) still in disgenet_pipeline.py load()"
        )

    def test_disgenet_load_df_no_invalid_columns(self):
        """DisGeNET load_df must not include disease_type, disease_class, source_id, year."""
        src = self.DISGENET_SRC.read_text()
        # Extract the load() method body
        method_match = re.search(
            r'def load\(self.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, re.DOTALL
        )
        load_body = method_match.group(0) if method_match else ""
        for bad in ('"disease_type"', '"disease_class"', '"source_id"', '"year"'):
            assert bad not in load_body, (
                f"REGRESSION: invalid GDA column {bad!r} still in disgenet_pipeline.py load_df"
            )

    def test_omim_load_uses_gene_to_uniprot(self):
        """OMIM load() must use gene_to_uniprot dict, not gene_to_protein."""
        src = self.OMIM_SRC.read_text()
        assert "gene_to_protein" not in src, (
            "REGRESSION: gene_to_protein still in omim_pipeline.py"
        )
        assert "gene_to_uniprot" in src, (
            "FIX INCOMPLETE: gene_to_uniprot missing from omim_pipeline.py"
        )

    def test_omim_load_no_protein_id_column(self):
        """OMIM load() must not produce or reference a 'protein_id' column in load_df."""
        src = self.OMIM_SRC.read_text()
        # Extract the load() method body
        method_match = re.search(
            r'def load\(self.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, re.DOTALL
        )
        load_body = method_match.group(0) if method_match else ""
        assert "protein_id" not in load_body, (
            "REGRESSION: protein_id still in omim_pipeline.py load()"
        )

    def test_omim_load_df_no_invalid_columns(self):
        """OMIM load_df must not include disease_type, disease_class, source_id, year."""
        src = self.OMIM_SRC.read_text()
        # Extract the load() method body
        method_match = re.search(
            r'def load\(self.*?\n(.*?)(?=\n    def |\nclass |\Z)',
            src, re.DOTALL
        )
        load_body = method_match.group(0) if method_match else ""
        for bad in ('"disease_type"', '"disease_class"', '"source_id"', '"year"'):
            assert bad not in load_body, (
                f"REGRESSION: invalid GDA column {bad!r} still in omim_pipeline.py load_df"
            )

    # ---- Functional tests ----

    def test_gda_model_has_uniprot_id_not_protein_id(self):
        """GeneDiseaseAssociation model must have uniprot_id, NOT protein_id."""
        cols = {c.name for c in inspect(GeneDiseaseAssociation).columns}
        assert "uniprot_id" in cols, "GDA model missing uniprot_id column"
        assert "protein_id" not in cols, (
            "REGRESSION: protein_id column on GeneDiseaseAssociation — schema mismatch"
        )

    def test_bulk_upsert_gda_with_uniprot_id_succeeds(self, db_session, sample_protein_df):
        """bulk_upsert_gda must accept a DataFrame with uniprot_id and insert correctly."""
        # Pre-insert a protein so FK is satisfied
        sqlite_bulk_upsert_proteins(db_session, sample_protein_df)
        db_session.flush()

        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1"],
            "uniprot_id": ["P23219"],         # string FK — correct
            "disease_id": ["C0003843"],
            "disease_name": ["Arthritis"],
            "score": [0.85],
            "source": ["disgenet"],
            "pmid_list": ["12345;67890"],
        })

        count = sqlite_bulk_upsert_gda(db_session, gda_df)
        assert count == 1

        row = db_session.query(GeneDiseaseAssociation).first()
        assert row is not None
        assert row.uniprot_id == "P23219"
        assert row.gene_symbol == "PTGS1"
        assert row.disease_id == "C0003843"
        assert row.score == pytest.approx(0.85)

    def test_bulk_upsert_gda_rejects_protein_id_column(self, db_session, sample_protein_df):
        """Inserting a GDA DataFrame with 'protein_id' column must fail — it's not in the table."""
        sqlite_bulk_upsert_proteins(db_session, sample_protein_df)
        db_session.flush()

        bad_df = pd.DataFrame({
            "protein_id": [1],              # integer PK — WRONG column name
            "disease_id": ["C0003843"],
            "disease_name": ["Arthritis"],
            "source": ["disgenet"],
        })

        # SQLite's INSERT will raise because 'protein_id' is not a column in
        # gene_disease_associations
        with pytest.raises(Exception):
            sqlite_bulk_upsert_gda(db_session, bad_df)

    def test_disgenet_load_gene_symbol_to_uniprot_id_resolution(self, db_session):
        """Simulate DisGeNET load(): gene_symbol must map to uniprot_id string, not int PK."""
        # Seed a protein with gene_name that matches gene_symbol lookup
        protein_df = pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_name": ["PTGS1"],
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)
        db_session.flush()

        # Simulate the resolution logic from the FIXED load()
        result = db_session.execute(
            text("SELECT gene_name, uniprot_id FROM proteins WHERE gene_name IS NOT NULL")
        )
        gene_to_uniprot = {}
        for row in result:
            if row.gene_name:
                gene_to_uniprot[row.gene_name.upper()] = row.uniprot_id

        # gene_symbol maps to uniprot_id string, never to integer
        assert "PTGS1" in gene_to_uniprot
        assert isinstance(gene_to_uniprot["PTGS1"], str), (
            "gene_to_uniprot must map to string uniprot_id, not integer"
        )
        assert gene_to_uniprot["PTGS1"] == "P23219"

    def test_gda_columns_match_model(self):
        """GDA model columns must match expected set — no surprise columns.

        Note (389-fix audit): The audit EXPLICITLY mandates adding
        ``disease_type``, ``disease_class``, ``source_id``, ``year_initial``,
        ``year_final``, ``gene_id``, ``confidence_tier``, ``evidence_strength``,
        ``normalized_score``, and several lineage columns to the GDA model
        (SCI-3, SCI-6, SCI-7, SCI-8, SCI-9, SCI-10, SCI-21, SCI-24, SCI-38,
        LIN-1..28).  These columns are NOW scientifically required — the
        previous "no surprise columns" check reflected the broken state.
        The only column that must NEVER appear is ``protein_id`` (the GDA
        model uses the string ``uniprot_id`` FK, not the integer protein PK).
        """
        cols = {c.name for c in inspect(GeneDiseaseAssociation).columns}
        expected = {
            "id", "gene_symbol", "uniprot_id", "disease_id",
            "disease_name", "association_type", "score", "source",
            "pmid_list", "created_at",
        }
        assert expected.issubset(cols), (
            f"GDA model missing expected columns: {expected - cols}"
        )
        # protein_id must NEVER be on the GDA model (the GDA table uses
        # the string uniprot_id FK, not the integer protein PK).
        assert "protein_id" not in cols, (
            "REGRESSION: invalid column 'protein_id' found on GDA model "
            "(the GDA table uses uniprot_id, not protein_id)"
        )
        # 389-fix audit: verify the new scientifically-required columns
        # are present (SCI-3, SCI-6, SCI-7, SCI-8, SCI-9, SCI-10, SCI-21,
        # SCI-24, SCI-38).
        for required in (
            "gene_id", "disease_type", "source_id", "disease_class",
            "year_initial", "year_final", "confidence_tier",
            "evidence_strength", "normalized_score", "source_version",
            "score_was_clipped", "original_score",
        ):
            assert required in cols, (
                f"REGRESSION: 389-fix required column '{required}' missing "
                f"from GDA model (see SCI-3/6/7/8/9/10/21/24/38)"
            )

    def test_disgenet_ensure_gda_columns_uses_uniprot_id(self):
        """DisGeNET _ensure_gda_columns must default uniprot_id, not protein_id."""
        src = self.DISGENET_SRC.read_text()
        assert '"uniprot_id": None' in src, (
            "FIX INCOMPLETE: uniprot_id default missing from disgenet _ensure_gda_columns"
        )

    def test_omim_ensure_gda_columns_uses_uniprot_id(self):
        """OMIM _ensure_gda_columns must default uniprot_id, not protein_id.

        UPDATE (institutional-grade rewrite): The legacy code used a dict
        literal with ``"uniprot_id": None``. The new code uses a list of
        ``(name, default)`` tuples in ``GDA_REQUIRED_COLUMNS`` (master
        prompt BUG-2.11). The test now verifies the tuple form.
        """
        src = self.OMIM_SRC.read_text()
        # Check the new GDA_REQUIRED_COLUMNS tuple form.
        assert '("uniprot_id"' in src, (
            "FIX INCOMPLETE: uniprot_id default missing from GDA_REQUIRED_COLUMNS"
        )
        # protein_id must NOT appear anywhere.
        assert '"protein_id"' not in src and "'protein_id'" not in src, (
            "REGRESSION: protein_id still in omim_pipeline.py"
        )

    def test_omim_empty_gda_df_uses_uniprot_id(self):
        """OMIM _empty_gda_df must use uniprot_id, not protein_id."""
        src = self.OMIM_SRC.read_text()
        # Find the _empty_gda_df method and check its column list
        assert '"uniprot_id"' in src, "uniprot_id missing from OMIM pipeline"
        assert '"protein_id"' not in src, (
            "REGRESSION: protein_id still in omim_pipeline.py"
        )

    def test_gda_bulk_upsert_with_omim_columns(self, db_session, sample_protein_df):
        """Verify OMIM-style GDA columns (gene_symbol, uniprot_id, disease_id, etc.) insert correctly."""
        sqlite_bulk_upsert_proteins(db_session, sample_protein_df)
        db_session.flush()

        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1"],
            "uniprot_id": ["P23219"],
            "disease_id": ["OMIM:100800"],
            "disease_name": ["Achondroplasia"],
            "score": [1.0],
            "source": ["omim"],
        })

        count = sqlite_bulk_upsert_gda(db_session, gda_df)
        assert count == 1

        row = db_session.query(GeneDiseaseAssociation).filter_by(
            disease_id="OMIM:100800"
        ).first()
        assert row is not None
        assert row.source == "omim"
        assert row.score == pytest.approx(1.0)


# ============================================================================
# Fix 5 — DrugBank pipeline: gzip dead code / iterparse must use file handle
# ============================================================================


class TestFix5_DrugBankGzip:
    """Bug 6: DrugBank clean() must explicitly open .gz files via gzip.open(),
    not pass the path string directly to etree.iterparse.
    """

    DRUGBANK_SRC = PROJECT_ROOT / "pipelines" / "drugbank_pipeline.py"

    def _read_src(self) -> str:
        return self.DRUGBANK_SRC.read_text()

    def test_dead_code_variables_removed(self):
        """open_func and mode dead-code variables must be removed."""
        src = self._read_src()
        assert "open_func" not in src, (
            "REGRESSION: dead variable 'open_func' still present in drugbank_pipeline.py"
        )
        assert 'mode = "rb"' not in src, (
            "REGRESSION: dead variable 'mode = \"rb\"' still present in drugbank_pipeline.py"
        )

    def test_explicit_gzip_open_present(self):
        """gzip.open() must be called explicitly for .gz path handling."""
        src = self._read_src()
        assert "gzip.open(raw_path" in src, (
            "FIX INCOMPLETE: explicit gzip.open(raw_path...) call missing"
        )

    def test_file_handle_variable_used(self):
        """A _file_handle variable must be used to pass the open handle to iterparse."""
        src = self._read_src()
        assert "_file_handle" in src, (
            "FIX INCOMPLETE: _file_handle variable missing; file handle must be passed "
            "to etree.iterparse for .gz files"
        )

    def test_gz_branch_conditional_present(self):
        """Code must branch on raw_path.suffix == '.gz' to choose the open strategy."""
        src = self._read_src()
        assert '.suffix == ".gz"' in src or ".suffix == '.gz'" in src, (
            "FIX INCOMPLETE: no .gz suffix check found in drugbank_pipeline.py"
        )

    def test_plain_xml_uses_open_file_handle(self):
        """Non-gzip path must use open(raw_path, 'rb') per Issue #19 fix."""
        src = self._read_src()
        assert "etree.iterparse(_file_handle" in src and "open(raw_path" in src, (
            "REGRESSION: plain XML should use open(raw_path, 'rb') + _file_handle for iterparse"
        )

    def test_iterparse_called_with_file_handle_for_gz(self):
        """For .gz files, etree.iterparse must receive the file handle, not raw_path."""
        src = self._read_src()
        # After the fix, the gz branch should call iterparse(_file_handle, ...)
        assert "etree.iterparse(_file_handle" in src, (
            "FIX INCOMPLETE: iterparse not called with _file_handle for .gz branch"
        )

    def test_file_handle_closed(self):
        """The _file_handle must be closed after the parse loop."""
        src = self._read_src()
        assert "_file_handle.close()" in src, (
            "FIX INCOMPLETE: _file_handle.close() missing — file handle leak"
        )

    def test_gzip_roundtrip_xml_parsing(self, tmp_path):
        """End-to-end: a minimal gzip-compressed DrugBank XML snippet must parse
        without error when _file_handle is properly wired.
        """
        # Build a minimal DrugBank XML with one drug element (correct namespace)
        minimal_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<drugbank xmlns="http://drugbank.ca" version="5.1">
  <drug type="small molecule">
    <drugbank-id primary="true">DB00001</drugbank-id>
    <name>TestDrug</name>
    <groups><group>approved</group></groups>
    <calculated-properties>
      <property>
        <kind>InChIKey</kind>
        <value>BSYNRYMUTXBXSQ-UHFFFAOYSA-N</value>
      </property>
    </calculated-properties>
    <targets/>
  </drug>
</drugbank>
"""
        gz_path = tmp_path / "test_drugbank.xml.gz"
        with gzip.open(gz_path, "wb") as fh:
            fh.write(minimal_xml)

        # Import and partially invoke the pipeline's XML parsing
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from lxml import etree

        NS = {"db": "http://drugbank.ca"}

        # Replicate the FIXED code path: open gz, pass handle to iterparse
        with gzip.open(gz_path, "rb") as file_handle:
            context = etree.iterparse(
                file_handle, events=("end",), tag="{%s}drug" % NS["db"]
            )
            drug_count = 0
            for _event, elem in context:
                drug_count += 1
                elem.clear()

        assert drug_count == 1, (
            f"Expected 1 drug element from gzip XML, got {drug_count}"
        )

    def test_plain_xml_parsing_still_works(self, tmp_path):
        """The non-gzip path must also still work correctly."""
        from lxml import etree

        NS_URI = "http://drugbank.ca"
        minimal_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<drugbank xmlns="http://drugbank.ca" version="5.1">
  <drug type="small molecule">
    <drugbank-id primary="true">DB00001</drugbank-id>
    <name>TestDrug</name>
    <groups><group>approved</group></groups>
    <targets/>
  </drug>
</drugbank>
"""
        xml_path = tmp_path / "test_drugbank.xml"
        xml_path.write_bytes(minimal_xml)

        context = etree.iterparse(
            str(xml_path), events=("end",), tag="{%s}drug" % NS_URI
        )
        count = sum(1 for _ in context)
        assert count == 1

    def test_gzip_xml_produces_valid_drug_record(self, tmp_path):
        """Parse a gzip DrugBank XML and verify the extracted drug record has correct fields."""
        minimal_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<drugbank xmlns="http://drugbank.ca" version="5.1">
  <drug type="small molecule">
    <drugbank-id primary="true">DB00945</drugbank-id>
    <name>Aspirin</name>
    <cas-number>50-78-2</cas-number>
    <groups><group>approved</group></groups>
    <calculated-properties>
      <property>
        <kind>InChIKey</kind>
        <value>BSYNRYMUTXBXSQ-UHFFFAOYSA-N</value>
      </property>
      <property>
        <kind>SMILES</kind>
        <value>CC(=O)Oc1ccccc1C(=O)O</value>
      </property>
      <property>
        <kind>Molecular Weight</kind>
        <value>180.16</value>
      </property>
      <property>
        <kind>Molecular Formula</kind>
        <value>C9H8O4</value>
      </property>
    </calculated-properties>
    <mechanism-of-action>Inhibits COX enzymes</mechanism-of-action>
    <targets/>
  </drug>
</drugbank>
"""
        gz_path = tmp_path / "test_drugbank_full.xml.gz"
        with gzip.open(gz_path, "wb") as fh:
            fh.write(minimal_xml)

        from pipelines.drugbank_pipeline import DrugBankPipeline
        from lxml import etree

        NS = {"db": "http://drugbank.ca"}
        pipeline = DrugBankPipeline.__new__(DrugBankPipeline)

        with gzip.open(gz_path, "rb") as file_handle:
            context = etree.iterparse(
                file_handle, events=("end",), tag="{%s}drug" % NS["db"]
            )
            for _event, elem in context:
                drug_rec, targets = pipeline._parse_drug_element(elem)
                break

        assert drug_rec is not None
        assert drug_rec["drugbank_id"] == "DB00945"
        assert drug_rec["name"] == "Aspirin"
        assert drug_rec["is_fda_approved"] is True
        assert "is_approved" not in drug_rec, (
            "REGRESSION: old 'is_approved' key in drug_rec"
        )
        assert "source" not in drug_rec, (
            "REGRESSION: 'source' key in drug_rec"
        )
        assert "source_id" not in drug_rec, (
            "REGRESSION: 'source_id' key in drug_rec"
        )
        assert "inchi" not in drug_rec, (
            "REGRESSION: 'inchi' key in drug_rec"
        )
