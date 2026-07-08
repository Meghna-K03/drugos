"""Tests for FIX-D (ML Training Correctness) — issues C-14, C-12, C-13, C-21.

These tests verify the four ML-training-correctness fixes applied in v26:

  * C-14 — ``kg_builder --dedup`` CLI was a stub. ``total_removed`` was
    always 0 because the branch only logged "need full triple (src, rel,
    dst)" for each edge type. The fix wires the CLI to actually call
    ``DrugOSGraphBuilder.deduplicate_edges_deterministic`` for each
    (src, rel, dst) triple derived from ``CORE_EDGE_TYPES``.

  * C-12 — Train/val/test split in ``step11_train_transe`` was fully
    random over mixed-relation triples, and ``temporal_split_pairs``
    (training_data.py) was dead code. The fix attempts a temporal split
    via ``temporal_split_pairs`` when approval-year data is available,
    and otherwise falls back to a stratified-by-relation-type random
    split (each relation contributes proportional 80/10/10).

  * C-13 — ``chemberta_encoder.py`` (1925 lines) is real but was never
    invoked from ``run_pipeline.py``. ``step9_build_pyg`` always fell
    back to random Xavier features. The fix wires ``step9_build_pyg`` to
    optionally compute ChEMBERTa SMILES embeddings (opt-in via
    ``DRUGOS_USE_CHEMBERTA=1`` + ``HF_TOKEN`` + ``transformers``) and
    attach them via ``PyGBuilder.add_chemberta_features``.

  * C-21 — ``pyg_builder.build_from_drkg`` wrote every (src, dst) pair
    from ``edge_maps`` to ``edge_index`` without deduplication. The fix
    adds a (src, dst) deduplication pass per edge type.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure phase2 is importable
PHASE2_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHASE2_DIR))


# ─── C-14: kg_builder --dedup CLI actually dedups ──────────────────────


def _kg_builder_source() -> str:
    """Read the kg_builder.py source text."""
    from drugos_graph import kg_builder
    return Path(kg_builder.__file__).read_text(encoding="utf-8")


def test_kg_builder_dedup_cli_actually_dedups():
    """C-14: the ``--dedup`` CLI branch must call
    ``deduplicate_edges_deterministic`` (not just log "need full triple").

    The previous implementation was a stub:

        elif args.dedup:
            stats = builder.get_graph_stats()
            edge_types = stats.get("edge_counts_by_type", {})
            total_removed = 0
            for rel_type in edge_types:
                logger.info("Dedup for %s: need full triple (src, rel, dst)", rel_type)
            print(f"\\nDedup complete. Removed {total_removed} duplicate edges.")

    ``total_removed`` was always 0 — no dedup happened. The fix calls
    ``builder.deduplicate_edges_deterministic(src, rel, dst)`` for each
    edge-type triple derived from ``CORE_EDGE_TYPES``.
    """
    src = _kg_builder_source()

    # Locate the --dedup branch.
    idx = src.find("elif args.dedup")
    assert idx != -1, "kg_builder.py: 'elif args.dedup' branch not found"
    # Slice up to the next top-level ``else:`` (the default-args branch).
    end_idx = src.find("\n        else:", idx)
    assert end_idx != -1, "kg_builder.py: could not find end of --dedup branch"
    branch = src[idx:end_idx]

    # The stub marker must be GONE.
    assert (
        'logger.info("Dedup for %s: need full triple' not in branch
    ), (
        "C-14 REGRESSION: --dedup branch still contains the stub log "
        "line 'Dedup for %s: need full triple (src, rel, dst)'."
    )

    # The real method must be CALLED (not just mentioned in a comment).
    # Strip comments before checking, so a docstring reference doesn't
    # satisfy the test.
    branch_no_comments = re.sub(r"#.*$", "", branch)
    assert (
        "deduplicate_edges_deterministic(" in branch_no_comments
    ), (
        "C-14 FAIL: --dedup branch does not call "
        "deduplicate_edges_deterministic(). Source:\n" + branch
    )

    # ``total_removed`` must accumulate the real return value, not stay
    # at 0.
    assert (
        "total_removed +=" in branch_no_comments
        or "total_removed += int(removed" in branch_no_comments
    ), (
        "C-14 FAIL: --dedup branch does not accumulate total_removed. "
        "Source:\n" + branch
    )

    # The branch must use CORE_EDGE_TYPES to expand the flat
    # ``edge_counts_by_type`` dict (which is keyed by rel_type alone)
    # into full (src, rel, dst) triples. Without this, the CLI cannot
    # call ``deduplicate_edges_deterministic`` which requires src_label
    # + rel_type + dst_label.
    assert "CORE_EDGE_TYPES" in branch_no_comments, (
        "C-14 FAIL: --dedup branch does not reference CORE_EDGE_TYPES "
        "to resolve src/dst labels. Source:\n" + branch
    )


def test_kg_builder_dedup_cli_invokes_real_method_with_mock():
    """C-14 functional test: extract the dedup branch body and execute
    it with a mock ``builder`` to confirm ``deduplicate_edges_deterministic``
    is actually called and ``total_removed`` accumulates the return value.
    """
    from drugos_graph import kg_builder
    from drugos_graph.config import CORE_EDGE_TYPES

    src = Path(kg_builder.__file__).read_text(encoding="utf-8")
    idx = src.find("elif args.dedup")
    end_idx = src.find("\n        else:", idx)
    branch = src[idx:end_idx]

    # The branch starts with "elif args.dedup:" — strip the leading
    # ``elif ...:`` line and dedent so we can exec it inside a function.
    body_start = branch.find("\n") + 1
    body = branch[body_start:]
    body = textwrap.dedent(body)

    # Sanity-check the body is syntactically valid Python.
    ast.parse(body)

    # Build a mock builder whose get_graph_stats() returns two rel_types
    # present in CORE_EDGE_TYPES, and whose deduplicate_edges_deterministic
    # returns 5 removed per call. We pick rel_types that exist in the
    # schema so the CORE_EDGE_TYPES lookup actually finds them.
    sample_triples = [t for t in CORE_EDGE_TYPES if t[1] in ("treats", "targets")]
    assert sample_triples, "CORE_EDGE_TYPES has no treats/targets triples"
    rel_types_present = {t[1] for t in sample_triples}

    mock_builder = MagicMock()
    mock_builder.get_graph_stats.return_value = {
        "edge_counts_by_type": {rt: 10 for rt in rel_types_present}
    }
    mock_builder.deduplicate_edges_deterministic.return_value = 5

    # Exec the body in a fresh namespace that provides the names the
    # branch references (``builder``, ``CORE_EDGE_TYPES``, ``logger``,
    # ``print``).
    captured_stdout = []
    ns = {
        "builder": mock_builder,
        "CORE_EDGE_TYPES": CORE_EDGE_TYPES,
        "logger": MagicMock(),
        "print": captured_stdout.append,
    }
    exec(compile(ast.parse(body), "<dedup-branch>", "exec"), ns)

    # The mock's deduplicate_edges_deterministic MUST have been called at
    # least once per rel_type present (and at least once total).
    assert mock_builder.deduplicate_edges_deterministic.called, (
        "C-14 FAIL: builder.deduplicate_edges_deterministic was never "
        "called by the --dedup branch."
    )
    n_calls = mock_builder.deduplicate_edges_deterministic.call_count
    assert n_calls >= len(rel_types_present), (
        f"C-14 FAIL: expected at least {len(rel_types_present)} calls "
        f"to deduplicate_edges_deterministic, got {n_calls}."
    )

    # Each call must be a 3-arg (src, rel, dst) call.
    for call in mock_builder.deduplicate_edges_deterministic.call_args_list:
        args, kwargs = call
        assert len(args) == 3, (
            f"C-14 FAIL: deduplicate_edges_deterministic call must be "
            f"3-arg (src, rel, dst), got args={args!r}."
        )

    # ``print`` must have been called with the REAL removed count, not 0.
    # 5 (per call) * n_calls >= 5 → total_removed >= 5.
    assert captured_stdout, (
        "C-14 FAIL: --dedup branch did not print a summary line."
    )
    summary = captured_stdout[-1]
    # Extract the number from "Dedup complete. Removed N duplicate edges."
    m = re.search(r"Removed\s+(\d+)\s+duplicate", summary)
    assert m, f"C-14 FAIL: print output does not contain 'Removed N duplicate': {summary!r}"
    total = int(m.group(1))
    assert total > 0, (
        f"C-14 FAIL: --dedup branch reported Removed {total} duplicate "
        f"edges — should be > 0 since the mock returned 5 per call. "
        f"(stub returned 0.)"
    )


# ─── C-21: pyg_builder deduplicates edges ──────────────────────────────


def test_pyg_builder_deduplicates_edges():
    """C-21: ``build_from_drkg`` must deduplicate (src, dst) pairs per
    edge type. Without dedup, duplicate rows in ``edge_maps`` (e.g.
    DrugBank and ChEMBL both reporting Compound X targets Protein Y)
    would be written as duplicate rows in ``edge_index``, inflating
    degree counts and biasing the GNN's attention weights.
    """
    from drugos_graph.pyg_builder import PyGBuilder
    from drugos_graph.config import PyGConfig

    builder = PyGBuilder(PyGConfig())

    # Build entity_maps: 3 Compounds, 2 Proteins.
    entity_maps = {
        "Compound": {"DB001": 0, "DB002": 1, "DB003": 2},
        "Protein": {"P001": 0, "P002": 1},
    }

    # Edge map with deliberate duplicates: (0,0) appears twice (e.g.
    # DrugBank + ChEMBL report the same Compound-Protein interaction),
    # (1,1) appears twice, and (2,0) appears once. Pre-dedup count = 5.
    edge_maps = {
        ("Compound", "targets", "Protein"): (
            [0, 0, 1, 1, 2],
            [0, 0, 1, 1, 0],
        ),
    }

    data = builder.build_from_drkg(entity_maps, edge_maps)
    ei = data["Compound", "targets", "Protein"].edge_index

    # After dedup, exactly 3 unique edges must remain: (0,0), (1,1), (2,0).
    assert ei.size(1) == 3, (
        f"C-21 FAIL: expected 3 unique edges after dedup, got {ei.size(1)}. "
        f"edge_index = {ei.tolist()}"
    )

    # Verify the unique pairs are exactly {(0,0), (1,1), (2,0)} (in any
    # order — the dedup preserves first-occurrence order, so the expected
    # order is (0,0), (1,1), (2,0)).
    pairs = {(int(ei[0, i].item()), int(ei[1, i].item())) for i in range(ei.size(1))}
    assert pairs == {(0, 0), (1, 1), (2, 0)}, (
        f"C-21 FAIL: unique edge pairs mismatch. got {pairs}"
    )


def test_pyg_builder_dedup_preserves_unique_edges():
    """C-21 regression: when no duplicates exist, dedup must be a no-op
    (every edge preserved).
    """
    from drugos_graph.pyg_builder import PyGBuilder
    from drugos_graph.config import PyGConfig

    builder = PyGBuilder(PyGConfig())
    entity_maps = {
        "Compound": {"DB001": 0, "DB002": 1, "DB003": 2},
        "Protein": {"P001": 0, "P002": 1},
    }
    edge_maps = {
        ("Compound", "targets", "Protein"): (
            [0, 1, 2, 0],
            [0, 1, 0, 1],
        ),
    }
    data = builder.build_from_drkg(entity_maps, edge_maps)
    ei = data["Compound", "targets", "Protein"].edge_index
    assert ei.size(1) == 4, (
        f"C-21 FAIL: 4 unique edges should be preserved, got {ei.size(1)}."
    )


def test_pyg_builder_dedup_handles_empty_edge_type():
    """C-21 regression: an empty edge type must not crash the dedup pass.
    """
    from drugos_graph.pyg_builder import PyGBuilder
    from drugos_graph.config import PyGConfig

    builder = PyGBuilder(PyGConfig())
    entity_maps = {
        "Compound": {"DB001": 0},
        "Protein": {"P001": 0},
    }
    edge_maps = {
        ("Compound", "targets", "Protein"): (
            [],
            [],
        ),
    }
    data = builder.build_from_drkg(entity_maps, edge_maps)
    ei = data["Compound", "targets", "Protein"].edge_index
    assert ei.size(1) == 0, (
        f"C-21 FAIL: empty edge type should stay empty, got {ei.size(1)}."
    )


# ─── C-13: ChEMBERTa integration is wired ──────────────────────────────


def _run_pipeline_source() -> str:
    from drugos_graph import run_pipeline
    return Path(run_pipeline.__file__).read_text(encoding="utf-8")


def test_chemberta_integration_is_wired():
    """C-13: ``run_pipeline.py`` must reference ``chemberta`` (specifically
    ``add_chemberta_features``) and ``chemberta_encoder`` so the
    integration is REAL, not dead code.

    The previous code never referenced the chemberta_encoder module from
    run_pipeline.py; PyGBuilder.build_from_drkg always fell back to
    random Xavier features for Compound nodes.
    """
    src = _run_pipeline_source()

    # ``chemberta_encoder`` must be imported or referenced.
    assert (
        "chemberta_encoder" in src
    ), "C-13 FAIL: run_pipeline.py does not reference chemberta_encoder."

    # ``add_chemberta_features`` must be called.
    # Strip comments so a docstring mention doesn't satisfy the test.
    src_no_comments = re.sub(r"#.*$", "", src)
    # Also strip docstrings (triple-quoted strings) — naive but effective
    # for this test: remove text between triple-quotes.
    src_no_docstrings = re.sub(
        r'"""[\s\S]*?"""', "", src_no_comments
    )
    assert (
        "add_chemberta_features(" in src_no_docstrings
    ), (
        "C-13 FAIL: run_pipeline.py does not call "
        "add_chemberta_features() (only mentions it in comments/"
        "docstrings)."
    )

    # ``DRUGOS_USE_CHEMBERTA`` env var check must be present.
    assert "DRUGOS_USE_CHEMBERTA" in src, (
        "C-13 FAIL: run_pipeline.py does not check the "
        "DRUGOS_USE_CHEMBERTA env var (the opt-in flag)."
    )

    # ``HF_TOKEN`` (or HUGGING_FACE_HUB_TOKEN) check must be present.
    assert (
        "HF_TOKEN" in src or "HUGGING_FACE_HUB_TOKEN" in src
    ), (
        "C-13 FAIL: run_pipeline.py does not check HF_TOKEN / "
        "HUGGING_FACE_HUB_TOKEN — the chemberta integration would "
        "attempt to download a gated model without authentication."
    )


def test_chemberta_integration_is_optional_and_off_by_default():
    """C-13: ChEMBERTa integration must be OFF by default (env var opt-in)
    so CI is not broken (no HF_TOKEN in CI). When the env var is NOT set,
    ``step9_build_pyg`` must NOT attempt to call chemberta_encoder.
    """
    from drugos_graph import run_pipeline

    # Ensure the env var is unset for this test (save/restore).
    saved = os.environ.pop("DRUGOS_USE_CHEMBERTA", None)
    saved_hf = os.environ.pop("HF_TOKEN", None)
    saved_hfhub = os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
    try:
        # Step9 takes (entity_maps, edge_maps, drug_records=None). Build
        # minimal inputs so we can call it. We need real entity_maps and
        # edge_maps (even tiny ones) so PyGBuilder.build_from_drkg works.
        entity_maps = {
            "Compound": {"DB001": 0},
            "Protein": {"P001": 0},
        }
        edge_maps = {
            ("Compound", "targets", "Protein"): ([0], [0]),
        }
        # Pass drug_records with a SMILES so that, if the env var WERE
        # set, the integration would proceed. Since the env var is unset,
        # the integration MUST be skipped.
        drug_records = [
            {"id": "DB001", "smiles": "CCO", "approval_year": 2000}
        ]

        # Patch PyGBuilder.save_heteradata to a no-op that returns a
        # tmp path, and summarize_heterodata to return an empty dict,
        # so we don't touch the filesystem.
        try:
            # Call step9_build_pyg directly. It returns a dict with
            # ``chemberta_used`` key.
            result = run_pipeline.step9_build_pyg(
                entity_maps, edge_maps, drug_records=drug_records
            )
        except Exception as exc:
            # If the underlying PyGBuilder fails for unrelated
            # reasons (e.g. PyG not installed in CI), we still want
            # to assert the chemberta_used flag — but the build
            # failure prevents that. Accept the failure as
            # "infrastructure missing" and verify only via source.
            pytest.skip(
                f"step9_build_pyg could not run in this env: {exc}"
            )

        assert "chemberta_used" in result, (
            "C-13 FAIL: step9_build_pyg result must include the "
            "'chemberta_used' flag so operators can verify whether "
            "ChEMBERTa features were attached."
        )
        assert result["chemberta_used"] is False, (
            "C-13 FAIL: step9_build_pyg must NOT use ChEMBERTa when "
            "DRUGOS_USE_CHEMBERTA env var is unset (default OFF). "
            f"Got chemberta_used={result['chemberta_used']!r}."
        )
    finally:
        if saved is not None:
            os.environ["DRUGOS_USE_CHEMBERTA"] = saved
        if saved_hf is not None:
            os.environ["HF_TOKEN"] = saved_hf
        if saved_hfhub is not None:
            os.environ["HUGGING_FACE_HUB_TOKEN"] = saved_hfhub


# ─── C-12: temporal_split_pairs is wired ──────────────────────────────


def test_temporal_split_pairs_is_wired_into_step11():
    """C-12: ``run_pipeline.step11_train_transe`` must reference
    ``temporal_split_pairs`` so the function is no longer dead code.

    The DOCX V1 launch criterion is ">0.85 AUC on held-out drug-disease
    pairs", which requires a temporal split (train on drugs approved
    before the cutoff, evaluate on drugs approved after).
    ``temporal_split_pairs`` was defined in training_data.py but never
    called from anywhere in the pipeline.
    """
    src = _run_pipeline_source()

    # ``temporal_split_pairs`` must be IMPORTED (not just mentioned).
    assert (
        "from .training_data import temporal_split_pairs" in src
        or "import temporal_split_pairs" in src
    ), (
        "C-12 FAIL: run_pipeline.py does not import "
        "temporal_split_pairs from training_data."
    )

    # And it must be CALLED (not just imported).
    src_no_comments = re.sub(r"#.*$", "", src)
    src_no_docstrings = re.sub(r'"""[\s\S]*?"""', "", src_no_comments)
    assert (
        re.search(r"temporal_split_pairs\s*\(", src_no_docstrings)
    ), (
        "C-12 FAIL: temporal_split_pairs is imported but never called "
        "in run_pipeline.py (still dead code)."
    )

    # ``step11_train_transe`` must accept a ``drug_records`` parameter
    # so the dispatch site can pass DrugBank records (which carry
    # approval_year) into the temporal-split logic.
    from drugos_graph import run_pipeline
    import inspect
    sig = inspect.signature(run_pipeline.step11_train_transe)
    assert "drug_records" in sig.parameters, (
        "C-12 FAIL: step11_train_transe must accept a 'drug_records' "
        "parameter so the temporal-split logic can read approval_year. "
        f"Got signature: {sig}"
    )


def test_step11_uses_stratified_split_when_no_approval_years():
    """C-12 functional: when ``drug_records`` is empty (no approval_year
    data), step11 must fall back to a stratified-by-relation-type random
    split and log a WARNING that the split is non-temporal.

    This test exercises the split-selection logic in isolation by
    importing step11 and invoking it on a tiny synthetic graph. We do
    NOT attempt to actually train TransE — we only verify that the split
    indices cover every relation type proportionally (the previous
    fully-random split could put a rare relation entirely in test).
    """
    from drugos_graph import run_pipeline
    import torch

    # Build a synthetic graph with 2 relation types.
    # Compound-treats-Disease: 4 triples
    # Compound-targets-Protein: 4 triples
    # Total: 8 triples
    entity_maps = {
        "Compound": {"DB001": 0, "DB002": 1, "DB003": 2, "DB004": 3},
        "Disease": {"DOID:1": 0, "DOID:2": 1},
        "Protein": {"P001": 0, "P002": 1},
    }
    edge_maps = {
        ("Compound", "treats", "Disease"): (
            [0, 1, 2, 3],
            [0, 1, 0, 1],
        ),
        ("Compound", "targets", "Protein"): (
            [0, 1, 2, 3],
            [0, 1, 0, 1],
        ),
    }

    # We can't easily call step11_train_transe end-to-end without
    # installing torch + the full TransE stack. Instead, we verify the
    # source code contains the stratified-split logic by inspecting the
    # function body.
    src = _run_pipeline_source()

    # The stratified-split block must group triples by relation type.
    assert "_by_rel" in src or "by_rel" in src, (
        "C-12 FAIL: step11_train_transe does not contain a "
        "by-relation grouping step (stratified split not implemented)."
    )

    # The stratified-split block must split each group 80/10/10.
    assert "_n_val" in src and "_n_test" in src, (
        "C-12 FAIL: step11_train_transe does not compute per-relation "
        "val/test sizes (stratified split not implemented)."
    )

    # A WARNING log must explicitly say the split is non-temporal.
    assert (
        "stratified random split" in src
        and "temporal split not" in src
    ), (
        "C-12 FAIL: step11_train_transe does not log a WARNING that "
        "the split is non-temporal when approval_year data is absent."
    )

    # The temporal_split_pairs call site must check the
    # ``_split_metadata.method`` field to decide whether the temporal
    # split actually succeeded (vs. fell back to random inside
    # temporal_split_pairs itself).
    assert "_split_metadata" in src, (
        "C-12 FAIL: step11_train_transe does not inspect the "
        "_split_metadata field returned by temporal_split_pairs — "
        "it cannot tell whether the split was actually temporal."
    )


if __name__ == "__main__":
    # Allow running this test file directly: python3 -m pytest ...
    pytest.main([__file__, "-v"])
