"""
tests/test_integration_e2e.py
==============================
Integration tests: verify data flows correctly through all pipeline stages.
These tests use an in-memory SQLite database and test the actual data flow
between pipeline components.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

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
from tests.db_helpers import (
    sqlite_bulk_upsert_drugs,
    sqlite_bulk_upsert_dpi,
    sqlite_bulk_upsert_entity_mapping,
    sqlite_bulk_upsert_gda,
    sqlite_bulk_upsert_ppi,
    sqlite_bulk_upsert_proteins,
)


class TestEndToEndPipelineFlow:
    """Integration test: verify data flows correctly through all pipeline stages."""

    def test_full_drug_pipeline_flow(self, db_session):
        """Verify drugs -> DPI flow: ChEMBL drugs load, then activities resolve and load."""
        # Step 1: Load ChEMBL drugs
        drugs_df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Aspirin"],
            "chembl_id": ["CHEMBL25"],
            "is_fda_approved": [True],
            "drug_type": ["Small molecule"],
            "max_phase": [4],
        })
        drug_count = sqlite_bulk_upsert_drugs(db_session, drugs_df)
        assert drug_count == 1

        # Step 2: Load UniProt proteins with gene_symbol
        proteins_df = pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_symbol": ["PTGS1"],
            "gene_name": ["Cyclooxygenase-1"],
            "organism": ["Homo sapiens"],
        })
        protein_count = sqlite_bulk_upsert_proteins(db_session, proteins_df)
        assert protein_count == 1

        # Step 3: Load DPI (simulating ChEMBL activities)
        dpi_df = pd.DataFrame({
            "drug_id": [1], "protein_id": [1],
            "interaction_type": ["IC50"],
            "activity_value": [5000.0],
            "activity_units": ["nM"],
            "activity_type": ["IC50"],
            "source": ["chembl"],
            "source_id": ["act_12345"],
            "confidence_score": [None],
        })
        dpi_count = sqlite_bulk_upsert_dpi(db_session, dpi_df)
        assert dpi_count == 1

        # Step 4: Verify data integrity
        drug = db_session.query(Drug).first()
        assert drug is not None
        assert drug.inchikey == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        assert drug.chembl_id == "CHEMBL25"

        protein = db_session.query(Protein).first()
        assert protein is not None
        assert protein.gene_symbol == "PTGS1"

        dpi = db_session.query(DrugProteinInteraction).first()
        assert dpi is not None
        assert dpi.drug_id == drug.id
        assert dpi.protein_id == protein.id
        assert dpi.interaction_type == "IC50"
        assert dpi.source == "chembl"

    def test_full_gda_pipeline_flow(self, db_session):
        """Verify proteins -> GDA flow: UniProt proteins load, then GDA resolves gene symbols."""
        # Step 1: Load proteins with gene_symbol (Bug #4, #7)
        sqlite_bulk_upsert_proteins(db_session, pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_symbol": ["PTGS1"],
            "gene_name": ["Cyclooxygenase-1"],
        }))

        # Step 2: Simulate DisGeNET gene resolution using gene_symbol (Bug #7)
        result = db_session.execute(
            text("SELECT gene_symbol, gene_name, uniprot_id FROM proteins "
                 "WHERE gene_symbol IS NOT NULL OR gene_name IS NOT NULL")
        )
        gene_to_uniprot = {}
        for row in result:
            if row.gene_symbol:
                gene_to_uniprot[row.gene_symbol.upper()] = row.uniprot_id
            if row.gene_name:
                key = row.gene_name.upper()
                if key not in gene_to_uniprot:
                    gene_to_uniprot[key] = row.uniprot_id

        assert "PTGS1" in gene_to_uniprot, (
            "Bug #7: gene_symbol PTGS1 not found in gene resolution"
        )
        assert gene_to_uniprot["PTGS1"] == "P23219"

        # Step 3: Load GDA with resolved uniprot_id and association_type (Bug #9)
        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1"],
            "uniprot_id": [gene_to_uniprot["PTGS1"]],
            "disease_id": ["C0003843"],
            "disease_name": ["Arthritis"],
            "association_type": ["somatic"],  # Bug #9
            "score": [0.85],
            "source": ["disgenet"],
            "pmid_list": ["12345"],
        })
        gda_count = sqlite_bulk_upsert_gda(db_session, gda_df)
        assert gda_count == 1

        # Step 4: Verify
        gda = db_session.query(GeneDiseaseAssociation).first()
        assert gda is not None
        assert gda.gene_symbol == "PTGS1"
        assert gda.association_type == "somatic"
        assert gda.pmid_list == "12345"
        assert gda.score == pytest.approx(0.85)

    def test_gda_deduplication_with_unique_constraint(self, db_session):
        """Verify GDA records are deduplicated on re-insert (Bug #8)."""
        sqlite_bulk_upsert_proteins(db_session, pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_symbol": ["PTGS1"],
        }))

        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1"],
            "uniprot_id": ["P23219"],
            "disease_id": ["C0003843"],
            "disease_name": ["Arthritis"],
            "association_type": ["unknown"],
            "score": [0.85],
            "source": ["disgenet"],
            "pmid_list": ["12345"],
        })

        # First insert
        sqlite_bulk_upsert_gda(db_session, gda_df)
        count_after_first = db_session.query(GeneDiseaseAssociation).count()

        # Second insert with updated score (should update, not duplicate)
        gda_df2 = gda_df.copy()
        gda_df2["score"] = 0.95
        gda_df2["pmid_list"] = "99999"
        sqlite_bulk_upsert_gda(db_session, gda_df2)
        count_after_second = db_session.query(GeneDiseaseAssociation).count()

        # Count should be the same (deduplication worked)
        assert count_after_first == count_after_second, (
            f"Bug #8: GDA deduplication failed. Before: {count_after_first}, After: {count_after_second}"
        )

        # Score should be updated
        updated = db_session.query(GeneDiseaseAssociation).first()
        assert updated.score == pytest.approx(0.95), (
            f"Bug #8: GDA score not updated on re-insert. Got: {updated.score}"
        )

    def test_ppi_pipeline_flow(self, db_session):
        """Verify STRING PPI data loads with correct column names (Bug #3)."""
        # Step 1: Load two proteins
        sqlite_bulk_upsert_proteins(db_session, pd.DataFrame({
            "uniprot_id": ["P23219", "P04637"],
            "gene_symbol": ["PTGS1", "TP53"],
        }))

        # Step 2: Load PPI with model-matching columns
        ppi_df = pd.DataFrame({
            "protein_a_id": [1], "protein_b_id": [2],
            "combined_score": [900],
            "experimental_score": [800],
            "database_score": [700],
            "textmining_score": [600],
            "source": ["string"],
        })
        ppi_count = sqlite_bulk_upsert_ppi(db_session, ppi_df)
        assert ppi_count == 1

        # Step 3: Verify
        ppi = db_session.query(ProteinProteinInteraction).first()
        assert ppi is not None
        assert ppi.combined_score == 900
        assert ppi.experimental_score == 800
        assert ppi.database_score == 700
        assert ppi.textmining_score == 600

    def test_entity_mapping_deduplication(self, db_session):
        """Verify entity_mapping deduplicates on canonical_inchikey (Bug #8)."""
        em_df = pd.DataFrame({
            "canonical_inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "canonical_name": ["Aspirin"],
            "chembl_id": ["CHEMBL25"],
            "match_confidence": [1.0],
            "match_method": ["inchikey_exact"],
        })
        sqlite_bulk_upsert_entity_mapping(db_session, em_df)
        count1 = db_session.query(EntityMapping).count()

        # Re-insert same mapping — should not duplicate
        em_df2 = em_df.copy()
        em_df2["match_confidence"] = 0.9
        sqlite_bulk_upsert_entity_mapping(db_session, em_df2)
        count2 = db_session.query(EntityMapping).count()

        assert count1 == count2, (
            f"Bug #8: Entity mapping not deduplicated. Before: {count1}, After: {count2}"
        )


class TestInChIKeySpecCompliance:
    """Verify all InChIKeys in the system pass standardize_inchikey() validation (Bug #17, #32)."""

    def test_synthetic_inchikeys_pass_validation(self):
        """Synthetic InChIKeys generated by DrugResolver must pass standardize_inchikey()."""
        from cleaning.normalizer import standardize_inchikey
        from entity_resolution.drug_resolver import is_synthetic_inchikey
        import hashlib

        # Generate a synthetic InChIKey using the fixed algorithm
        norm = "testdrug"
        source = "test"
        hash_digest = hashlib.sha256(f"{norm}:{source}".encode()).hexdigest().upper()
        block1 = "SYNTH" + hash_digest[5:14]
        block2 = hash_digest[14:24]
        block3 = hash_digest[24]
        synthetic_ik = f"{block1}-{block2}-{block3}"

        # Must be exactly 27 characters
        assert len(synthetic_ik) == 27, (
            f"Bug #17: Synthetic InChIKey must be 27 chars, got {len(synthetic_ik)}"
        )

        # Must start with SYNTH
        assert is_synthetic_inchikey(synthetic_ik), (
            "Bug #17: is_synthetic_inchikey should return True for SYNTH-prefixed keys"
        )

        # Must pass standardize_inchikey validation
        validated = standardize_inchikey(synthetic_ik)
        assert validated is not None, (
            f"Bug #17: Synthetic InChIKey '{synthetic_ik}' failed standardize_inchikey() validation"
        )

    def test_real_inchikeys_pass_validation(self):
        """Known real InChIKeys must pass standardize_inchikey()."""
        from cleaning.normalizer import standardize_inchikey

        real_keys = [
            "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",  # Aspirin
            "RZVAJINKQORUAP-UHFFFAOYSA-N",  # Ibuprofen
        ]
        for ik in real_keys:
            validated = standardize_inchikey(ik)
            assert validated is not None, (
                f"Real InChIKey '{ik}' failed validation"
            )

    def test_is_synthetic_inchikey_function(self):
        """is_synthetic_inchikey() must correctly identify synthetic vs real keys."""
        from entity_resolution.drug_resolver import is_synthetic_inchikey

        assert is_synthetic_inchikey("SYNTHABCDEF123-GHIJKLMNOPQ-R") is True
        assert is_synthetic_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") is False
        assert is_synthetic_inchikey("") is False
        assert is_synthetic_inchikey(None) is False


class TestRapidfuzzGuard:
    """Verify rapidfuzz import is guarded (Bug #6)."""

    def test_resolver_utils_has_rapidfuzz_available_flag(self):
        """resolver_utils must expose _RAPIDFUZZ_AVAILABLE flag."""
        from entity_resolution import resolver_utils
        assert hasattr(resolver_utils, '_RAPIDFUZZ_AVAILABLE'), (
            "Bug #6: resolver_utils must have _RAPIDFUZZ_AVAILABLE flag"
        )

    def test_fuzzy_match_score_works_without_rapidfuzz(self):
        """fuzzy_match_score must fall back to exact match when rapidfuzz unavailable."""
        from entity_resolution.resolver_utils import fuzzy_match_score
        # Test that it works (rapidfuzz may or may not be installed)
        # Exact match should return 1.0
        assert fuzzy_match_score("hello", "hello") == 1.0
        # Non-match should return 0.0 (or low score with rapidfuzz)
        assert fuzzy_match_score("hello", "world") < 0.5


class TestNullishHandling:
    """Verify _is_nullish correctly handles 'nan' strings (Bug #19)."""

    def test_nan_string_not_treated_as_null(self):
        """The string 'nan' should NOT be treated as null (Bug #19)."""
        from cleaning.missing_values import _is_nullish
        df = pd.DataFrame({"col": ["nan", "NAN", "NaNGlu"]})
        mask = _is_nullish(df["col"])
        # None of these should be flagged as null
        assert not mask.any(), (
            f"Bug #19: 'nan' strings incorrectly treated as null. Mask: {mask.tolist()}"
        )

    def test_actual_nan_is_detected(self):
        """Actual float NaN values must still be detected."""
        from cleaning.missing_values import _is_nullish
        df = pd.DataFrame({"col": [1.0, float('nan'), None]})
        mask = _is_nullish(df["col"].astype(object))
        assert mask.iloc[1] is True or mask.iloc[1] == True  # NaN must be detected
        assert mask.iloc[2] is True or mask.iloc[2] == True  # None must be detected

    def test_empty_string_detected(self):
        """Empty strings must be detected as null."""
        from cleaning.missing_values import _is_nullish
        df = pd.DataFrame({"col": ["", "  ", "hello"]})
        mask = _is_nullish(df["col"])
        assert mask.iloc[0] == True
        assert mask.iloc[1] == True  # whitespace-only
        assert mask.iloc[2] == False

    def test_null_patterns_detected(self):
        """Common null patterns (null, N/A, etc.) must be detected.
        Note: "none" and "unknown" are intentionally NOT treated as null."""
        from cleaning.missing_values import _is_nullish
        df = pd.DataFrame({"col": ["null", "none", "N/A", "unknown", "hello"]})
        mask = _is_nullish(df["col"])
        assert mask.iloc[0] == True   # "null"
        assert mask.iloc[1] == False  # "none" is NOT nullish — legitimate biomedical value
        assert mask.iloc[2] == True   # "N/A"
        assert mask.iloc[3] == False  # "unknown" is NOT nullish
        assert mask.iloc[4] == False  # "hello" is real data


class TestMakefileClassNames:
    """Verify Makefile uses correct class names (Bug #5)."""

    def test_makefile_uses_stringpipeline(self):
        """Makefile must use StringPipeline, not STRINGPipeline (Bug #5)."""
        makefile = (PROJECT_ROOT / "Makefile").read_text()
        assert "STRINGPipeline" not in makefile, (
            "Bug #5: Makefile still references STRINGPipeline (should be StringPipeline)"
        )
        assert "StringPipeline" in makefile, (
            "Bug #5: Makefile missing StringPipeline reference"
        )

    def test_load_all_uses_run_load_only(self):
        """Makefile load-all target must use run_load_only (Bug #13)."""
        makefile = (PROJECT_ROOT / "Makefile").read_text()
        assert "run_load_only" in makefile, (
            "Bug #13: Makefile load-all must use run_load_only instead of run"
        )


class TestDockerConfig:
    """Verify Docker configuration is correct (Bug #11)."""

    def test_dockerfile_airflow_exists(self):
        """docker/Dockerfile.airflow must exist (Bug #11)."""
        assert (PROJECT_ROOT / "docker" / "Dockerfile.airflow").exists(), (
            "Bug #11: docker/Dockerfile.airflow not found"
        )

    def test_docker_compose_uses_custom_build(self):
        """docker-compose.yml must use custom build, not plain image (Bug #11)."""
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "docker/Dockerfile.airflow" in compose, (
            "Bug #11: docker-compose.yml must reference custom Dockerfile"
        )


class TestMigrationScript:
    """Verify migration script exists and is correct (Section 11)."""

    def test_migration_002_exists(self):
        """002_bug_fixes_migration.sql must exist."""
        assert (PROJECT_ROOT / "database" / "migrations" / "002_bug_fixes_migration.sql").exists(), (
            "Migration script 002_bug_fixes_migration.sql not found"
        )

    def test_migration_contains_gene_symbol(self):
        """Migration must add gene_symbol column."""
        sql = (PROJECT_ROOT / "database" / "migrations" / "002_bug_fixes_migration.sql").read_text()
        assert "gene_symbol" in sql, "Migration missing gene_symbol"
        assert "ALTER TABLE proteins" in sql, "Migration missing ALTER TABLE proteins"

    def test_migration_contains_unique_constraints(self):
        """Migration must add unique constraints for GDA and EntityMapping."""
        sql = (PROJECT_ROOT / "database" / "migrations" / "002_bug_fixes_migration.sql").read_text()
        assert "uq_gda_gene_disease_source" in sql, "Migration missing GDA unique constraint"
        assert "uq_entity_mapping_inchikey" in sql, "Migration missing EntityMapping unique constraint"


class TestNeo4jExportStub:
    """Verify Neo4j export is now implemented via the Phase 1 → Phase 2 bridge.

    Previously (Phase 1 standalone): export_to_neo4j raised NotImplementedError.
    Now (unified package): export_to_neo4j delegates to
    drugos_graph.phase1_bridge.run_phase1_to_phase2 and returns a summary
    dict. This test verifies the new contract.
    """

    def test_neo4j_exporter_exists(self):
        """exporters/neo4j_exporter.py must exist."""
        assert (PROJECT_ROOT / "exporters" / "neo4j_exporter.py").exists(), (
            "Bug #30: exporters/neo4j_exporter.py not found"
        )

    def test_neo4j_exporter_no_longer_raises(self):
        """export_to_neo4j must NOT raise NotImplementedError — it now works.

        Called with no builder and no Neo4j credentials, it falls back to
        the in-memory RecordingGraphBuilder (dry-run mode) and returns a
        summary dict.
        """
        from exporters.neo4j_exporter import export_to_neo4j
        # Must not raise NotImplementedError
        result = export_to_neo4j(
            pg_session=None,
            neo4j_uri=None,
            neo4j_user=None,
            neo4j_password=None,
        )
        assert isinstance(result, dict), (
            "export_to_neo4j must return a dict (bridge summary report). "
            "Got: " + type(result).__name__
        )
        assert "summary" in result, (
            "export_to_neo4j result must contain 'summary' key"
        )
        s = result["summary"]
        # When Phase 1 processed_data is populated, the bridge loads real data.
        assert s["nodes_loaded"] >= 0, "nodes_loaded must be a non-negative int"
        assert s["edges_loaded"] >= 0, "edges_loaded must be a non-negative int"
        assert s["bridge_version"], "bridge_version must be set"

    def test_neo4j_exporter_supports_injected_builder(self):
        """export_to_neo4j must accept a pre-constructed builder (DI mode)."""
        from exporters.neo4j_exporter import export_to_neo4j
        # Use a sentinel object to confirm it's passed through
        class _SentinelBuilder:
            def __init__(self):
                self.calls = []
            def load_nodes_batch(self, label, nodes, batch_size=None, **kw):
                self.calls.append(("nodes", label, len(nodes)))
                return len(nodes)
            def load_edges_batch(self, src, rel, dst, edges, batch_size=None, **kw):
                self.calls.append(("edges", src, rel, dst, len(edges)))
                return len(edges)
        sentinel = _SentinelBuilder()
        result = export_to_neo4j(
            pg_session=None,
            neo4j_uri=None,
            builder=sentinel,
        )
        assert sentinel.calls, (
            "Builder injection mode did not invoke load_nodes_batch or "
            "load_edges_batch on the supplied builder"
        )
        assert result["summary"]["nodes_loaded"] > 0
