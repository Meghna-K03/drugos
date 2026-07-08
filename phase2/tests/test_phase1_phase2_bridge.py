"""
Phase 1 → Phase 2 Integration Test (THE bridge verification)
=============================================================

This test file is the SINGLE proof that Phase 1's processed_data outputs
flow through the bridge and land in Phase 2's graph builder with the
correct node/edge types, referential integrity, and lineage properties.

It is the contract test for the unified package: if this test passes, the
two phases are 100% connected.

The test uses :class:`RecordingGraphBuilder` (in-memory, no Neo4j) so it
runs in CI without external dependencies.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure both phase1 and phase2 are importable
HERE = Path(__file__).resolve().parent
PHASE2_ROOT = HERE.parent
UNIFIED_ROOT = PHASE2_ROOT.parent
PHASE1_ROOT = UNIFIED_ROOT / "phase1"

for p in (str(PHASE2_ROOT), str(PHASE1_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from drugos_graph import phase1_bridge  # noqa: E402
from drugos_graph.config import CORE_EDGE_TYPES, CORE_NODE_TYPES  # noqa: E402
from drugos_graph.phase1_bridge import (  # noqa: E402
    DEFAULT_PHASE1_PROCESSED_DIR,
    PHASE1_TO_PHASE2_BRIDGE_VERSION,
    Phase1StagedData,
    RecordingGraphBuilder,
    compute_input_checksum,
    load_into_graph,
    read_phase1_outputs,
    run_phase1_to_phase2,
    stage_phase1_to_phase2,
)


PHASE1_PROCESSED = PHASE1_ROOT / "processed_data"


# ---------------------------------------------------------------------------
# 1. Phase 1 outputs are physically present
# ---------------------------------------------------------------------------
class TestPhase1OutputsExist:
    """If Phase 1's outputs are missing, the bridge has nothing to convert."""

    def test_processed_data_dir_exists(self):
        assert PHASE1_PROCESSED.exists(), (
            f"Phase 1 processed_data directory missing: {PHASE1_PROCESSED}. "
            "Run the Phase 1 pipeline before invoking the bridge."
        )

    @pytest.mark.parametrize("filename", [
        "drugbank_drugs.csv",
        "drugbank_interactions.csv.gz",
        "omim_gene_disease_associations.csv",
    ])
    def test_required_csv_exists(self, filename):
        p = PHASE1_PROCESSED / filename
        assert p.exists(), f"Required Phase 1 output missing: {p}"
        assert p.stat().st_size > 0, f"Phase 1 output is empty: {p}"


# ---------------------------------------------------------------------------
# 2. read_phase1_outputs returns well-formed DataFrames
# ---------------------------------------------------------------------------
class TestReadPhase1Outputs:
    def test_reads_all_three_sources(self):
        frames = read_phase1_outputs(PHASE1_PROCESSED)
        # v6: bridge now also reads the optional `indications` source
        # (drugbank_indications.csv). The required sources remain
        # drugs/interactions/omim_gda; indications is optional.
        required = {"drugs", "interactions", "omim_gda"}
        assert required.issubset(set(frames.keys())), (
            f"Missing required sources: {required - set(frames.keys())}"
        )
        # If indications is present, it must be non-empty (the fixture ships it).
        if "indications" in frames:
            assert not frames["indications"].empty, (
                "drugbank_indications.csv produced empty DataFrame"
            )
        for k, df in frames.items():
            if k in required:
                assert not df.empty, f"Phase 1 source '{k}' produced empty DataFrame"

    def test_drugs_has_required_columns(self):
        frames = read_phase1_outputs(PHASE1_PROCESSED)
        required = {
            "drugbank_id", "name", "inchikey", "smiles",
            "is_fda_approved", "is_withdrawn",
        }
        missing = required - set(frames["drugs"].columns)
        assert not missing, f"drugbank_drugs.csv missing columns: {missing}"

    def test_interactions_has_required_columns(self):
        frames = read_phase1_outputs(PHASE1_PROCESSED)
        required = {"drugbank_id", "uniprot_id", "action_type"}
        missing = required - set(frames["interactions"].columns)
        assert not missing, f"drugbank_interactions.csv.gz missing columns: {missing}"

    def test_omim_has_required_columns(self):
        frames = read_phase1_outputs(PHASE1_PROCESSED)
        required = {"gene_symbol", "disease_id"}
        missing = required - set(frames["omim_gda"].columns)
        assert not missing, f"omim_gene_disease_associations.csv missing columns: {missing}"


# ---------------------------------------------------------------------------
# 3. stage_phase1_to_phase2 produces correctly-typed node/edge dicts
# ---------------------------------------------------------------------------
class TestStaging:
    @pytest.fixture(scope="class")
    def staged(self):
        frames = read_phase1_outputs(PHASE1_PROCESSED)
        return stage_phase1_to_phase2(frames, run_id="test-run-001")

    def test_staged_object_is_correct_type(self, staged):
        assert isinstance(staged, Phase1StagedData)

    def test_compound_nodes_have_required_fields(self, staged):
        assert len(staged.compound_nodes) > 0
        for n in staged.compound_nodes:
            assert "id" in n and n["id"]
            assert "drugbank_id" in n
            # v27 ROOT FIX (P2-B-1): withdrawn is now Optional[bool] —
            # None when Phase 1 is silent (so DrugBankEnricher coalesce
            # can fire and set safety_data_missing=True), True/False
            # only when Phase 1 explicitly says so. Old test asserted
            # isinstance(bool) which assumed the buggy always-False behavior.
            assert "withdrawn" in n and isinstance(n["withdrawn"], (bool, type(None)))
            # safety_data_missing must be present (P2-B-1 fix)
            assert "safety_data_missing" in n and isinstance(n["safety_data_missing"], bool)
            assert "fda_approved" in n and isinstance(n["fda_approved"], bool)
            # Lineage
            assert n["_source_phase"] == 1
            assert n["_pipeline_run_id"] == "test-run-001"

    def test_protein_nodes_have_required_fields(self, staged):
        # Protein nodes come from interactions; assume at least one
        if not staged.protein_nodes:
            pytest.skip("No interactions in Phase 1 fixture")
        for n in staged.protein_nodes:
            assert "id" in n and n["id"]
            assert n["_source_phase"] == 1

    def test_gene_and_disease_nodes_have_required_fields(self, staged):
        if not staged.gene_nodes:
            pytest.skip("No OMIM GDA rows in Phase 1 fixture")
        for n in staged.gene_nodes:
            assert "id" in n and n["id"]
        for n in staged.disease_nodes:
            assert "id" in n and n["id"]

    def test_all_edge_types_are_core(self, staged):
        """Every edge type produced by the bridge MUST be in CORE_EDGE_TYPES."""
        for et in staged.edge_types_present():
            assert et in set(CORE_EDGE_TYPES), (
                f"Bridge produced non-core edge type {et}. "
                f"This would corrupt the knowledge graph schema."
            )

    def test_edges_carry_lineage(self, staged):
        for edges in staged.edges.values():
            for e in edges:
                assert e["_source_phase"] == 1
                assert "_loaded_at" in e
                assert "_pipeline_run_id" in e

    def test_edge_endpoints_reference_existing_nodes(self, staged):
        """Referential integrity: every edge endpoint must be a staged node ID."""
        compound_ids = {n["id"] for n in staged.compound_nodes}
        protein_ids = {n["id"] for n in staged.protein_nodes}
        gene_ids = {n["id"] for n in staged.gene_nodes}
        disease_ids = {n["id"] for n in staged.disease_nodes}
        # FIX-F / C-16 (added by concurrent agent): the bridge now
        # emits ClinicalOutcome nodes derived from
        # drugbank_indications.csv. The endpoint_map must include
        # them or has_clinical_outcome edges KeyError out here.
        clinical_outcome_ids = {n["id"] for n in staged.clinical_outcome_nodes}

        endpoint_map = {
            "Compound": compound_ids,
            "Protein": protein_ids,
            "Gene": gene_ids,
            "Disease": disease_ids,
            "ClinicalOutcome": clinical_outcome_ids,
        }
        for (src, _rel, dst), edges in staged.edges.items():
            assert src in endpoint_map, (
                f"Edge type {src}-[{_rel}]->{dst} references unstaged "
                f"src label {src!r}"
            )
            assert dst in endpoint_map, (
                f"Edge type {src}-[{_rel}]->{dst} references unstaged "
                f"dst label {dst!r}"
            )
            src_ids = endpoint_map[src]
            dst_ids = endpoint_map[dst]
            for e in edges:
                assert e["src_id"] in src_ids, (
                    f"Edge {e} references unknown {src} node {e['src_id']}"
                )
                assert e["dst_id"] in dst_ids, (
                    f"Edge {e} references unknown {dst} node {e['dst_id']}"
                )


# ---------------------------------------------------------------------------
# 4. load_into_graph + RecordingGraphBuilder round-trip
# ---------------------------------------------------------------------------
class TestLoadIntoGraph:
    @pytest.fixture(scope="class")
    def loaded(self):
        result = run_phase1_to_phase2(
            phase1_processed_dir=PHASE1_PROCESSED,
            builder=RecordingGraphBuilder(),
            run_id="integration-test-001",
        )
        return result

    def test_summary_present(self, loaded):
        assert "summary" in loaded
        s = loaded["summary"]
        assert s["bridge_version"] == PHASE1_TO_PHASE2_BRIDGE_VERSION
        assert s["nodes_staged"] > 0
        assert s["nodes_loaded"] > 0
        assert s["errors"] == []

    def test_nodes_loaded_match_staged(self, loaded):
        s = loaded["summary"]
        assert s["nodes_loaded"] == s["nodes_staged"], (
            f"Loaded {s['nodes_loaded']} nodes but staged {s['nodes_staged']} — "
            f"some nodes were silently dropped by the builder."
        )

    def test_edges_loaded_match_staged(self, loaded):
        s = loaded["summary"]
        assert s["edges_loaded"] == s["edges_staged"], (
            f"Loaded {s['edges_loaded']} edges but staged {s['edges_staged']} — "
            f"some edges were silently dropped by the builder (likely "
            f"referential integrity failure)."
        )

    def test_at_least_one_edge_type_present(self, loaded):
        s = loaded["summary"]
        assert len(s["edge_types_present"]) >= 1, (
            "Bridge produced zero edge types — Phase 1 → Phase 2 flow is broken."
        )

    def test_compound_protein_edges_present(self, loaded):
        """DrugBank interactions MUST produce at least one Compound→Protein edge."""
        builder: RecordingGraphBuilder = loaded["builder"]
        cp_edges = []
        for et in [("Compound", "targets", "Protein"),
                   ("Compound", "inhibits", "Protein"),
                   ("Compound", "activates", "Protein"),
                   ("Compound", "allosterically_modulates", "Protein"),
                   ("Compound", "unknown", "Protein")]:
            cp_edges.extend(builder.edges_by_type(*et))
        assert len(cp_edges) > 0, (
            "Expected Compound→Protein edges from drugbank_interactions.csv.gz "
            "but none were loaded."
        )

    def test_gene_disease_edges_present(self, loaded):
        """OMIM GDA MUST produce at least one Gene→Disease edge."""
        builder: RecordingGraphBuilder = loaded["builder"]
        gd_edges = builder.edges_by_type("Gene", "associated_with", "Disease")
        assert len(gd_edges) > 0, (
            "Expected Gene→Disease edges from omim_gene_disease_associations.csv "
            "but none were loaded."
        )

    def test_recorder_node_count_matches(self, loaded):
        builder: RecordingGraphBuilder = loaded["builder"]
        s = loaded["summary"]
        assert builder.total_nodes == s["nodes_loaded"]
        assert builder.total_edges == s["edges_loaded"]


# ---------------------------------------------------------------------------
# 5. Phase 1's neo4j_exporter.export_to_neo4j() now works (no longer raises)
# ---------------------------------------------------------------------------
class TestPhase1Neo4jExporterNoLongerRaises:
    """The Phase 1 stub used to raise NotImplementedError. Now it must work."""

    def test_export_to_neo4j_uses_bridge(self):
        # Import from Phase 1's exporters module
        try:
            from exporters.neo4j_exporter import export_to_neo4j
        except ImportError:
            pytest.skip("phase1/exporters not on path")
        # Should NOT raise NotImplementedError
        result = export_to_neo4j(
            pg_session=None,
            neo4j_uri=None,
            neo4j_user=None,
            neo4j_password=None,
            phase1_processed_dir=PHASE1_PROCESSED,
            builder=RecordingGraphBuilder(),
        )
        assert "summary" in result
        assert result["summary"]["nodes_loaded"] > 0
        assert result["summary"]["edges_loaded"] > 0


# ---------------------------------------------------------------------------
# 6. Determinism — same inputs → same staged output
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_two_runs_produce_identical_node_ids(self):
        r1 = run_phase1_to_phase2(phase1_processed_dir=PHASE1_PROCESSED)
        r2 = run_phase1_to_phase2(phase1_processed_dir=PHASE1_PROCESSED)
        ids1 = {n["id"] for n in r1["staged"].compound_nodes}
        ids2 = {n["id"] for n in r2["staged"].compound_nodes}
        assert ids1 == ids2, "Bridge is non-deterministic across runs"

    def test_input_checksum_is_stable(self):
        paths = [
            PHASE1_PROCESSED / "drugbank_drugs.csv",
            PHASE1_PROCESSED / "drugbank_interactions.csv.gz",
            PHASE1_PROCESSED / "omim_gene_disease_associations.csv",
        ]
        c1 = compute_input_checksum(paths)
        c2 = compute_input_checksum(paths)
        assert c1 == c2
        assert len(c1) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# 7. Bridge module is exposed via drugos_graph package
# ---------------------------------------------------------------------------
class TestPackageExposure:
    def test_phase1_bridge_in_all(self):
        import drugos_graph
        assert "phase1_bridge" in drugos_graph.__all__

    def test_phase1_bridge_importable_via_package(self):
        import drugos_graph
        # __getattr__ should resolve this lazily
        mod = drugos_graph.phase1_bridge
        assert mod is phase1_bridge
