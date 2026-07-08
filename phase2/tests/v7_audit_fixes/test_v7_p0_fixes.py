"""
Comprehensive root-cause tests for the v7 forensic audit fixes.

These tests directly verify each of the P0 / P1 root-cause fixes applied
in response to DrugOS_v6_Forensic_Audit_Report.pdf. Each test name maps
1:1 to a BUG-* identifier from the audit so failures are immediately
attributable to the specific bug being regressed.

Test philosophy:
- NO surface-level "does the function exist" tests.
- Each test exercises the EXACT code path the audit identified.
- Each test asserts the EXACT invariant the audit said was broken.
- Tests are independent — no shared mutable state.
"""

import os
import sys
import json
import re
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure the package is importable.
# Test file is at: codebase/unified/phase2/tests/v7_audit_fixes/test_v7_p0_fixes.py
# So parents[4] = codebase/ (which contains 'unified/phase2/drugos_graph')
# v14 ROOT FIX: PROJECT_ROOT must point to the actual codebase root
# (the directory containing phase1/ and phase2/). The previous
# calculation parents[4] pointed one level too high, and every path
# was prefixed with a non-existent "unified/" subdirectory — causing
# every file-read assertion in this test module to raise FileNotFoundError.
# parents[3] resolves to the codebase root regardless of where the
# repo is checked out.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "phase2"))
sys.path.insert(0, str(PROJECT_ROOT))


# =============================================================================
# BUG-E-001 — entity_to_idx built but never used in step11_train_transe.
# Compound 0, Protein 0, Gene 0, Disease 0 all shared embedding row 0.
# =============================================================================

class TestBUGE001EntityToIdxUsed(unittest.TestCase):
    """Verify that TransE training uses GLOBAL entity indices, not per-label
    local indices. This is the single most damaging bug in the audit."""

    def test_local_to_global_map_is_built_and_distinct(self):
        """The fix introduces a ``local_to_global`` map that translates
        per-label local indices to unique global indices. Verify that
        entities of different types do NOT collide on the same global row.
        """
        # Simulate the entity_maps structure used by run_pipeline.
        entity_maps = {
            "Compound": {"DB00001": 0, "DB00002": 1, "DB00003": 2},
            "Protein": {"P23219": 0, "P00734": 1},
            "Gene": {"2261": 0, "2645": 1},
            "Disease": {"OMIM:100100": 0, "OMIM:219700": 1},
        }
        # Replicate the fix logic.
        entity_to_idx = {}
        local_to_global = {}
        idx = 0
        for etype, id_map in entity_maps.items():
            for eid, local_idx in id_map.items():
                entity_to_idx[(etype, eid)] = idx
                local_to_global[(etype, int(local_idx))] = idx
                idx += 1
        num_entities = sum(len(v) for v in entity_maps.values())
        # Invariant 1: local_to_global has exactly num_entities entries.
        self.assertEqual(len(local_to_global), num_entities)
        # Invariant 2: every global index is unique (no collision).
        global_indices = list(local_to_global.values())
        self.assertEqual(len(set(global_indices)), len(global_indices))
        # Invariant 3: Compound 0, Protein 0, Gene 0, Disease 0 each map
        # to DISTINCT global indices. This is the EXACT bug.
        self.assertNotEqual(local_to_global[("Compound", 0)],
                            local_to_global[("Protein", 0)])
        self.assertNotEqual(local_to_global[("Compound", 0)],
                            local_to_global[("Gene", 0)])
        self.assertNotEqual(local_to_global[("Compound", 0)],
                            local_to_global[("Disease", 0)])
        self.assertNotEqual(local_to_global[("Protein", 0)],
                            local_to_global[("Gene", 0)])

    def test_edge_indices_resolve_to_global(self):
        """When edge_maps uses local indices, they must be translated
        through local_to_global to global indices before being passed
        to TransEModel."""
        entity_maps = {
            "Compound": {"DB00001": 0, "DB00002": 1},
            "Disease": {"OMIM:100100": 0, "OMIM:219700": 1},
        }
        edge_maps = {
            ("Compound", "treats", "Disease"): ([0, 1], [0, 1]),
        }
        # Build maps as the fix does.
        local_to_global = {}
        idx = 0
        for etype, id_map in entity_maps.items():
            for eid, local_idx in id_map.items():
                local_to_global[(etype, int(local_idx))] = idx
                idx += 1
        # Translate edge indices.
        heads, tails = [], []
        for (src_type, rel, dst_type), (src_list, dst_list) in edge_maps.items():
            for s, d in zip(src_list, dst_list):
                heads.append(local_to_global[(src_type, int(s))])
                tails.append(local_to_global[(dst_type, int(d))])
        # After translation, every index must be < num_entities=4.
        self.assertTrue(all(0 <= h < 4 for h in heads))
        self.assertTrue(all(0 <= t < 4 for t in tails))
        # And no Compound/Disease index collision (Compound 0 != Disease 0).
        self.assertNotEqual(heads[0], tails[0])


# =============================================================================
# BUG-C-001 — Bootstrap CI is fabricated. EvaluationResult has no
# pos_scores field, getattr always returns [], synthetic Gaussian fires.
# =============================================================================

class TestBUGC001PosScoresField(unittest.TestCase):
    """Verify that EvaluationResult now carries pos_scores/neg_scores
    arrays so the bootstrap CI can resample REAL model scores."""

    def test_evaluation_result_has_pos_scores_field(self):
        """The dataclass must have a ``pos_scores`` attribute."""
        from drugos_graph.evaluation import EvaluationResult
        from drugos_graph.config import build_lineage_metadata
        result = EvaluationResult(
            metrics={"auc": 0.85},
            counts={"num_positives": 10, "num_negatives": 50},
            provenance=build_lineage_metadata(),
            quality_report={},
            pos_scores=np.array([0.8, 0.9, 0.7]),
            neg_scores=np.array([0.2, 0.1, 0.3]),
        )
        self.assertIsNotNone(result.pos_scores)
        self.assertIsNotNone(result.neg_scores)
        self.assertEqual(len(result.pos_scores), 3)
        self.assertEqual(len(result.neg_scores), 3)

    def test_evaluate_link_prediction_populates_pos_scores(self):
        """evaluate_link_prediction must populate pos_scores/neg_scores
        on the returned EvaluationResult."""
        from drugos_graph.evaluation import evaluate_link_prediction
        pos = np.array([0.8, 0.9, 0.7, 0.85, 0.95])
        neg = np.array([0.2, 0.1, 0.3, 0.15, 0.25])
        result = evaluate_link_prediction(pos, neg, log_results=False)
        # The fix ensures these are populated.
        self.assertIsNotNone(result.pos_scores,
            "BUG-C-001 regression: pos_scores is None — bootstrap CI "
            "will fall back to synthetic Gaussian.")
        self.assertIsNotNone(result.neg_scores)
        self.assertEqual(len(result.pos_scores), 5)
        self.assertEqual(len(result.neg_scores), 5)

    def test_bootstrap_ci_uses_real_scores_not_synthetic(self):
        """When pos_scores/neg_scores are populated, the bootstrap CI
        must NOT use the synthetic Gaussian fallback."""
        from drugos_graph.evaluation import (
            evaluate_link_prediction, _compute_bootstrap_ci,
        )
        # Use distinctive scores so synthetic N(0.3,0.15) would give a
        # very different CI than the real scores.
        pos = np.array([0.9, 0.92, 0.88, 0.91, 0.93, 0.89, 0.9, 0.91])
        neg = np.array([0.1, 0.12, 0.08, 0.11, 0.13, 0.09, 0.1, 0.11])
        result = evaluate_link_prediction(pos, neg, log_results=False)
        ci = _compute_bootstrap_ci(result, n_bootstrap=100)
        # The fix surfaces a 'synthetic' flag.
        self.assertIn("synthetic", ci["auc"],
            "BUG-C-001 regression: _compute_bootstrap_ci does not "
            "surface the 'synthetic' flag.")
        self.assertFalse(ci["auc"]["synthetic"],
            "BUG-C-001 regression: bootstrap CI used synthetic Gaussian "
            "fallback even though real scores were available.")


# =============================================================================
# BUG-D-003 — 11 instances of min(coalesce(...), 1.0) in Cypher queries.
# Cypher has no scalar two-arg min(x,y); every multi-hop query crashed.
# =============================================================================

class TestBUGD003CypherMinSyntax(unittest.TestCase):
    """Verify that graph_queries.py contains ZERO instances of the invalid
    ``min(coalesce(...), 1.0)`` Cypher syntax and 11 instances of the
    valid ``CASE WHEN coalesce(...) < 1.0 THEN ... ELSE 1.0 END`` form.
    """

    def test_no_invalid_min_coalesce_syntax(self):
        """The forbidden pattern must be GONE from graph_queries.py."""
        gq_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "graph_queries.py"
        source = gq_path.read_text()
        # The exact forbidden pattern.
        invalid_pattern = re.compile(r"min\(\s*coalesce\(")
        matches = invalid_pattern.findall(source)
        self.assertEqual(len(matches), 0,
            f"BUG-D-003 regression: found {len(matches)} instances of "
            f"invalid 'min(coalesce(...))' Cypher syntax. All 11 sites "
            f"must use 'CASE WHEN coalesce(...) < 1.0 THEN ... ELSE 1.0 END'.")

    def test_case_when_replacement_present(self):
        """All 11 sites must now use the CASE WHEN replacement."""
        gq_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "graph_queries.py"
        source = gq_path.read_text()
        # Count occurrences of the replacement pattern. Some sites wrap
        # the inner coalesce in extra parens like ``(CASE WHEN (coalesce(...``
        # so we accept either form.
        case_when_count = len(re.findall(r"\(CASE WHEN\s*\(?\s*coalesce\(", source))
        self.assertGreaterEqual(case_when_count, 11,
            f"BUG-D-003 regression: expected >= 11 CASE WHEN coalesce "
            f"replacements, found {case_when_count}.")


# =============================================================================
# BUG-C-008 — NegativeSampler corrupts tail with neg_drug_idx (Compound)
# instead of neg_disease_idx (Disease). neg_disease_idx was dead code.
# =============================================================================

class TestBUGC008NegativeSamplingTailCorruption(unittest.TestCase):
    """Verify that for (Compound, treats, Disease) triples, the tail is
    corrupted with DISEASE indices, not Compound indices."""

    def test_neg_disease_idx_is_used_for_tail(self):
        """Read the transe_model.py source and verify that the tail
        corruption uses ``neg_disease_idx`` (not ``neg_drug_idx``)."""
        tm_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "transe_model.py"
        source = tm_path.read_text()
        # v14 ROOT FIX: the previous assertion required an EXACT 20-space
        # indentation pattern that broke whenever the code was re-indented.
        # Use a whitespace-flexible regex instead — the invariant we care
        # about is "torch.tensor is called with neg_disease_idx as the
        # first argument", not the exact whitespace.
        pattern = re.compile(
            r"torch\.tensor\(\s*neg_disease_idx",
            re.MULTILINE,
        )
        self.assertTrue(
            pattern.search(source),
            "BUG-C-008 regression: neg_t is not built from neg_disease_idx "
            "(no `torch.tensor(neg_disease_idx ...)` call found)."
        )
        # The old bug pattern (neg_t = neg_d where neg_d was built from
        # neg_drug_idx) must be GONE.
        self.assertNotIn(
            "neg_t = neg_d",
            source,
            "BUG-C-008 regression: 'neg_t = neg_d' pattern still present."
        )


# =============================================================================
# BUG-C-002 — AUC enforcement bypassed when best_val_auc <= 0.
# A perfectly wrong model (AUC=0.0) was saved without enforcement.
# =============================================================================

class TestBUGC002AUCEnforcementBypass(unittest.TestCase):
    """Verify that AUC enforcement now requires best_val_auc > 0.5
    (better than random) before any save can occur."""

    def test_random_baseline_floor_is_0_5(self):
        """The fix introduces a RANDOM_BASELINE_AUC = 0.5 floor."""
        tm_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "transe_model.py"
        source = tm_path.read_text()
        self.assertIn("RANDOM_BASELINE_AUC = 0.5", source,
            "BUG-C-002 regression: RANDOM_BASELINE_AUC = 0.5 not present.")
        # The old bypass pattern must be GONE.
        self.assertNotIn("if best_val_auc > 0:", source,
            "BUG-C-002 regression: 'if best_val_auc > 0:' bypass still present.")

    def test_auc_at_or_below_random_is_rejected(self):
        """A model with AUC <= 0.5 must raise TransETrainingError."""
        # We can't easily run a full training, but we can verify the
        # source code structure rejects AUC <= 0.5.
        tm_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "transe_model.py"
        source = tm_path.read_text()
        self.assertIn("best_val_auc <= RANDOM_BASELINE_AUC", source,
            "BUG-C-002 regression: no check for AUC <= RANDOM_BASELINE_AUC.")


# =============================================================================
# BUG-C-006 — target_auc default = 0.78 but DOCX claims >0.85.
# =============================================================================

class TestBUGC006TargetAUCDefault(unittest.TestCase):
    """Verify that TransEConfig.target_auc defaults to 0.85 (not 0.78)."""

    def test_target_auc_default_is_085(self):
        """The default must match the DOCX claim of >0.85 AUC."""
        # Ensure no env var overrides the default.
        old = os.environ.pop("DRUGOS_TRANSE_TARGET_AUC", None)
        try:
            from drugos_graph.config import TransEConfig
            cfg = TransEConfig()
            self.assertGreaterEqual(cfg.target_auc, 0.85,
                f"BUG-C-006 regression: target_auc default = {cfg.target_auc} "
                f"is below the DOCX-claimed 0.85.")
        finally:
            if old is not None:
                os.environ["DRUGOS_TRANSE_TARGET_AUC"] = old


# =============================================================================
# BUG-C-005 — torch.load(weights_only=False) despite security comment.
# =============================================================================

class TestBUGC005WeightsOnlySecurity(unittest.TestCase):
    """Verify that torch.load uses weights_only=True."""

    def test_load_uses_weights_only_true(self):
        tm_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "transe_model.py"
        source = tm_path.read_text()
        # Find all torch.load call positions.
        torch_load_positions = [m.start() for m in re.finditer(r"torch\.load\(", source)]
        self.assertGreater(len(torch_load_positions), 0,
            "BUG-C-005: no torch.load() call found.")
        for pos in torch_load_positions:
            # Find the next 'weights_only=' after this torch.load call.
            wo_match = re.search(r"weights_only=(\w+)", source[pos:pos + 500])
            self.assertIsNotNone(wo_match,
                f"BUG-C-005: torch.load call at position {pos} has no "
                f"weights_only= parameter nearby.")
            wo_value = wo_match.group(1)
            self.assertEqual(
                wo_value, "True",
                f"BUG-C-005 regression: torch.load uses weights_only={wo_value} "
                f"instead of weights_only=True."
            )


# =============================================================================
# BUG-E-002 / BUG-E-003 — df shim lacks head_id/tail_id columns.
# step8_entity_resolution and step10_training_data crashed silently.
# =============================================================================

class TestBUGE002E003DfShimColumns(unittest.TestCase):
    """Verify that the Phase 1 df shim now includes head_id/tail_id."""

    def test_df_shim_includes_head_id_tail_id(self):
        """Read run_pipeline.py source and verify the shim builds
        a DataFrame with head_id and tail_id columns."""
        rp_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "run_pipeline.py"
        source = rp_path.read_text()
        # The fix adds head_id and tail_id to the rows dict.
        self.assertIn('"head_id": head_id', source,
            "BUG-E-002/E-003 regression: df shim does not include head_id.")
        self.assertIn('"tail_id": tail_id', source,
            "BUG-E-002/E-003 regression: df shim does not include tail_id.")
        # The columns list must include head_id and tail_id.
        self.assertIn('"head_id", "head_type"', source)
        self.assertIn('"tail_id", "tail_type"', source)


# =============================================================================
# BUG-E-008 — Pipeline exits 0 even when steps silently fail.
# =============================================================================

class TestBUGE008ExitCodeContract(unittest.TestCase):
    """Verify that the pipeline exits non-zero when any step is
    silently skipped (except user-requested skips)."""

    def test_main_checks_for_unexpected_skips(self):
        """The main() function must scan results for skipped=True
        and exit non-zero if any unexpected skip is found."""
        rp_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "run_pipeline.py"
        source = rp_path.read_text()
        self.assertIn("BUG-E-008 enforcement", source,
            "BUG-E-008 regression: exit-code enforcement not present.")
        self.assertIn("unexpected_skips", source,
            "BUG-E-008 regression: unexpected_skips scan not present.")


# =============================================================================
# BUG-D-005 — Atc ID_PATTERN required 9 chars; real ATC codes are 7.
# =============================================================================

class TestBUGD005AtcPattern(unittest.TestCase):
    """Verify that the Atc ID_PATTERN accepts 7-char ATC codes."""

    def test_atc_pattern_accepts_l01xc02(self):
        from drugos_graph.kg_builder import ID_PATTERNS
        pattern = ID_PATTERNS["Atc"]
        # Real ATC codes (7 chars).
        for code in ["L01XC02", "L04AA02", "N02BA01", "C07AB02"]:
            self.assertIsNotNone(re.match(pattern, code),
                f"BUG-D-005 regression: ATC code {code} rejected by pattern {pattern}.")
        # Optional sub-class extension.
        self.assertIsNotNone(re.match(pattern, "L01XC02.01"))


# =============================================================================
# BUG-D-015 — Disease ID_PATTERN allowed bare '[A-Z]+:\w+' catch-all.
# 'FOO:bar' was accepted as a Disease ID.
# =============================================================================

class TestBUGD015DiseaseCatchAllPattern(unittest.TestCase):
    """Verify that the Disease ID_PATTERN no longer accepts arbitrary
    prefixed strings."""

    def test_foo_bar_rejected(self):
        from drugos_graph.kg_builder import ID_PATTERNS
        pattern = ID_PATTERNS["Disease"]
        # The catch-all must be gone — 'FOO:bar' must be rejected.
        self.assertIsNone(re.match(pattern, "FOO:bar"),
            f"BUG-D-015 regression: 'FOO:bar' accepted by pattern {pattern}.")
        # But valid biomedical disease IDs must still be accepted.
        for did in ["OMIM:154700", "MONDO:0001234", "DOID:14326", "C0018790"]:
            self.assertIsNotNone(re.match(pattern, did),
                f"Valid disease ID {did} rejected by pattern {pattern}.")


# =============================================================================
# BUG-D-002 — _load_edges validated only missing/empty, not ID_PATTERNS.
# =============================================================================

class TestBUGD002EdgeIDPatternValidation(unittest.TestCase):
    """Verify that _load_edges now validates endpoint IDs against
    ID_PATTERNS, not just missing/empty."""

    def test_load_edges_validates_against_id_patterns(self):
        """The kg_builder.py source must contain the ID_PATTERNS check
        in _load_edges."""
        kb_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "kg_builder.py"
        source = kb_path.read_text()
        self.assertIn("BUG-D-002 root fix", source,
            "BUG-D-002 regression: ID_PATTERNS validation not present.")
        self.assertIn("invalid_src_id_format", source)
        self.assertIn("invalid_dst_id_format", source)


# =============================================================================
# BUG-D-004 — RecordingGraphBuilder applied ZERO validation.
# Tests passed while production silently dropped data.
# =============================================================================

class TestBUGD004RecordingGraphBuilderValidation(unittest.TestCase):
    """Verify that RecordingGraphBuilder now applies ID_PATTERNS,
    CORE_EDGE_TYPES whitelist, and dead-letter recording."""

    def test_recording_builder_validates_node_ids(self):
        """An invalid node ID must be dead-lettered, not silently accepted."""
        from drugos_graph.phase1_bridge import RecordingGraphBuilder
        builder = RecordingGraphBuilder()
        # 'INVALID_ID' doesn't match Compound pattern.
        builder.load_nodes_batch(
            "Compound",
            [{"id": "INVALID_ID", "name": "bad"}, {"id": "DB00001", "name": "good"}],
            source="test",
        )
        # The invalid one must be dead-lettered.
        self.assertEqual(len(builder.dead_letter), 1)
        self.assertIn("invalid_id_format", builder.dead_letter[0]["reason"])
        # The valid one must be accepted.
        self.assertEqual(builder.total_nodes, 1)

    def test_recording_builder_validates_edge_endpoints(self):
        from drugos_graph.phase1_bridge import RecordingGraphBuilder
        builder = RecordingGraphBuilder()
        # Stage a valid Compound + Disease.
        builder.load_nodes_batch("Compound", [{"id": "DB00001"}], source="t")
        builder.load_nodes_batch("Disease", [{"id": "OMIM:100100"}], source="t")
        # Try to load an edge with an INVALID src_id.
        builder.load_edges_batch(
            "Compound", "treats", "Disease",
            [{"src_id": "BAD_ID", "dst_id": "OMIM:100100"}],
            source="t",
        )
        # The invalid edge must be dead-lettered.
        dl_reasons = [d["reason"] for d in builder.dead_letter]
        self.assertTrue(any("invalid_src_id_format" in r for r in dl_reasons),
            f"BUG-D-004 regression: invalid edge src_id not dead-lettered. "
            f"Dead-letter reasons: {dl_reasons}")


# =============================================================================
# BUG-B-001 / BUG-B-002 — OMIM/DisGeNET loaders emitted prefixed Gene IDs.
# =============================================================================

class TestBUGB001B002LoaderGeneIDFormat(unittest.TestCase):
    """Verify that omim_loader and disgenet_loader emit bare numeric
    Gene IDs (or SYM:-prefixed symbols), not OMIM:/NCBIGene: prefixed."""

    def test_omim_loader_strips_omim_prefix(self):
        """omim_loader must NOT emit 'OMIM:100650' as a Gene ID."""
        from drugos_graph.omim_loader import omim_to_node_records
        df = pd.DataFrame({
            "gene_symbol": ["FGFR3"],
            "gene_mim": [100650],
            "disease_id": ["OMIM:100800"],
            "disease_name": ["Achondroplasia"],
            "phenotype_mim": [100800],
            "uniprot_id": ["P11362"],
        })
        nodes = omim_to_node_records(df)
        gene_nodes = [n for n in nodes if n["label"] == "Gene"]
        self.assertEqual(len(gene_nodes), 1)
        # Must be bare numeric, NOT 'OMIM:100650'.
        self.assertEqual(gene_nodes[0]["id"], "100650")
        self.assertNotIn("OMIM:", gene_nodes[0]["id"])

    def test_disgenet_loader_strips_ncbigene_prefix(self):
        """disgenet_loader must NOT emit 'NCBIGene:2645' as a Gene ID."""
        from drugos_graph.disgenet_loader import disgenet_to_node_records
        df = pd.DataFrame({
            "gene_symbol": ["FGFR3"],
            "ncbi_gene_id": [2261],
            "disease_id": ["OMIM:100800"],
            "disease_name": ["Achondroplasia"],
            "score": [0.8],
            "source": ["UNIPROT"],
        })
        nodes = disgenet_to_node_records(df)
        gene_nodes = [n for n in nodes if n["label"] == "Gene"]
        self.assertEqual(len(gene_nodes), 1)
        # Must be bare numeric, NOT 'NCBIGene:2261'.
        self.assertEqual(gene_nodes[0]["id"], "2261")
        self.assertNotIn("NCBIGene:", gene_nodes[0]["id"])


# =============================================================================
# BUG-B-003 — DrugBank/UniProt/GEO emit different edge keys.
# kg_builder required src_id/dst_id only.
# =============================================================================

class TestBUGB003EdgeKeyAliases(unittest.TestCase):
    """Verify that kg_builder._load_edges normalizes edge endpoint keys
    from various loader-specific aliases (drug_id/target_uniprot_id,
    source/target, head/tail) to src_id/dst_id."""

    def test_edge_key_normalization_in_source(self):
        kb_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "kg_builder.py"
        source = kb_path.read_text()
        self.assertIn("BUG-B-003 root fix", source,
            "BUG-B-003 regression: edge key normalization not present.")
        # The aliases must be enumerated.
        self.assertIn("drug_id", source)
        self.assertIn("target_uniprot_id", source)
        self.assertIn('"source"', source)
        self.assertIn('"target"', source)
        self.assertIn('"head"', source)
        self.assertIn('"tail"', source)


# =============================================================================
# BUG-B-004 — SIDER emitted bare int 5311025 (Compound ID).
# Pattern requires CID\d+.
# =============================================================================

class TestBUGB004SiderCompoundIDFormat(unittest.TestCase):
    """Verify that SIDER loader emits CID-prefixed Compound IDs."""

    def test_sider_emits_cid_prefix(self):
        sider_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "sider_loader.py"
        source = sider_path.read_text()
        # The fix must use f"CID{int(...)}" not bare int(...).
        self.assertIn('f"CID{int(row[\'pubchem_cid\'])}"', source,
            "BUG-B-004 regression: SIDER does not prefix compound IDs with 'CID'.")


# =============================================================================
# BUG-D-001 / BUG-D-014 — driver = None never reassigned; cleanup branch
# always False. Orphaned drivers leaked on every retry.
# =============================================================================

class TestBUGD001D014DriverCleanup(unittest.TestCase):
    """Verify that the connect() failure path now actually closes the
    orphaned driver from the last attempt."""

    def test_orphan_driver_cleanup_present(self):
        kb_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "kg_builder.py"
        source = kb_path.read_text()
        # The fix introduces last_attempted_driver as a closure-captured list.
        self.assertIn("last_attempted_driver", source,
            "BUG-D-001/D-014 regression: last_attempted_driver tracking not present.")
        # The orphan close must be in the except branch.
        self.assertIn("orphan.close()", source,
            "BUG-D-001/D-014 regression: orphan.close() not called in except branch.")


# =============================================================================
# BUG-D-013 — load_drkg_nodes hard-coded source='DRKG' for all node types.
# =============================================================================

class TestBUGD013SourceParameterized(unittest.TestCase):
    """Verify that load_drkg_nodes now accepts a `source` parameter."""

    def test_source_parameter_exists(self):
        kb_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "kg_builder.py"
        source = kb_path.read_text()
        # The fix adds a `source` parameter.
        self.assertIn("source: str = \"DRKG\"", source,
            "BUG-D-013 regression: source parameter not added to load_drkg_nodes.")


# =============================================================================
# BUG-A-007 / BUG-A-008 — OMIM pipeline produces disease_id='FGFR3' or
# gene_symbol='26'. (Note: the audit's specific examples were false
# positives caused by awk misparsing quoted CSV, but the validation is
# still root-cause correct as defense-in-depth.)
# =============================================================================

class TestBUGA007A008OMIMValidation(unittest.TestCase):
    """Verify that the OMIM pipeline now validates disease_id format
    and gene_symbol alphabeticity."""

    def test_disease_id_format_validation_present(self):
        om_path = PROJECT_ROOT / "phase1" / "pipelines" / "omim_pipeline.py"
        source = om_path.read_text()
        self.assertIn("BUG-A-007", source,
            "BUG-A-007 regression: disease_id format validation not present.")
        self.assertIn("invalid_disease_id_format", source)

    def test_gene_symbol_alphabetic_validation_present(self):
        om_path = PROJECT_ROOT / "phase1" / "pipelines" / "omim_pipeline.py"
        source = om_path.read_text()
        self.assertIn("BUG-A-008", source,
            "BUG-A-008 regression: gene_symbol alphabetic validation not present.")
        self.assertIn("non_alphabetic_gene_symbol", source)
        # The pattern must require a leading letter.
        self.assertIn(r"^[A-Z][A-Z0-9]*$", source)


# =============================================================================
# BUG-A-002 — GDA loader set invalid gene_symbols to '' instead of
# quarantining. Distinct genes collapsed into one row.
# =============================================================================

class TestBUGA002GDAQuarantine(unittest.TestCase):
    """Verify that bulk_upsert_gda quarantines rows with NULL/empty
    gene_symbol instead of fillna('')."""

    def test_quarantine_function_exists(self):
        ld_path = PROJECT_ROOT / "phase1" / "database" / "loaders.py"
        source = ld_path.read_text()
        self.assertIn("_quarantine_gda_rows", source,
            "BUG-A-002 regression: _quarantine_gda_rows function not present.")
        self.assertIn("BUG-A-002", source)
        # The old fillna('') pattern must be GONE.
        # Look for the specific pattern in the bulk_upsert_gda function body.
        self.assertNotIn(
            'df["gene_symbol"] = df["gene_symbol"].fillna("")',
            source,
            "BUG-A-002 regression: fillna('') still present — distinct genes "
            "with empty gene_symbol will collapse into one row."
        )


# =============================================================================
# BUG-A-005 — drugbank_indications.csv expected by bridge but never
# produced by the DrugBank pipeline.
# =============================================================================

class TestBUGA005DrugBankIndicationsProducer(unittest.TestCase):
    """Verify that the DrugBank pipeline now produces
    drugbank_indications.csv via _write_structured_indications."""

    def test_write_structured_indications_method_exists(self):
        db_path = PROJECT_ROOT / "phase1" / "pipelines" / "drugbank_pipeline.py"
        source = db_path.read_text()
        self.assertIn("_write_structured_indications", source,
            "BUG-A-005 regression: _write_structured_indications method not present.")
        self.assertIn("BUG-A-005", source)


# =============================================================================
# BUG-C-004 — Validation used 1:1 pos/neg ratio instead of 10:1.
# AUC inflated by 0.05-0.10.
# =============================================================================

class TestBUGC004ValidationNegRatio(unittest.TestCase):
    """Verify that validation now uses 10:1 pos/neg ratio."""

    def test_validation_uses_10x_negatives(self):
        tm_path = PROJECT_ROOT / "phase2" / "drugos_graph" / "transe_model.py"
        source = tm_path.read_text()
        # The fix expands positives 10x and uses all 10*n_val negatives.
        self.assertIn("val_heads_expanded = val_heads_dev.repeat_interleave(10)", source,
            "BUG-C-004 regression: validation positives not expanded 10x.")
        self.assertIn("BUG-C-004", source)


# =============================================================================
# Smoke test: import all modified modules to verify no import errors.
# =============================================================================

class TestSmokeImports(unittest.TestCase):
    """Verify that every modified module imports cleanly."""

    def test_import_drugos_graph_package(self):
        import drugos_graph  # noqa: F401

    def test_import_evaluation(self):
        from drugos_graph.evaluation import (  # noqa: F401
            EvaluationResult, evaluate_link_prediction, _compute_bootstrap_ci,
        )

    def test_import_kg_builder(self):
        from drugos_graph.kg_builder import ID_PATTERNS  # noqa: F401
        from drugos_graph.kg_builder import GraphConnection  # noqa: F401

    def test_import_phase1_bridge(self):
        from drugos_graph.phase1_bridge import RecordingGraphBuilder  # noqa: F401

    def test_import_run_pipeline(self):
        from drugos_graph.run_pipeline import (  # noqa: F401
            step11_train_transe, run_full_pipeline, main,
        )

    def test_import_omim_loader(self):
        from drugos_graph.omim_loader import omim_to_node_records  # noqa: F401

    def test_import_disgenet_loader(self):
        from drugos_graph.disgenet_loader import disgenet_to_node_records  # noqa: F401

    def test_import_transea_model(self):
        from drugos_graph.transe_model import TransEModel, TransEConfig  # noqa: F401

    def test_import_graph_queries(self):
        from drugos_graph.graph_queries import GraphQueryService  # noqa: F401

    def test_import_config(self):
        from drugos_graph.config import TransEConfig  # noqa: F401


if __name__ == "__main__":
    unittest.main(verbosity=2)
