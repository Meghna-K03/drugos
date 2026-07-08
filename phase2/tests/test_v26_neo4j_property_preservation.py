"""FIX-B (Neo4j Node Property Strip) — patient-safety regression test.

This test file is the FORENSIC proof that the production Neo4j load path
(``run_pipeline.step3_load_neo4j``) preserves patient-safety properties
(``withdrawn``, ``fda_approved``, ``clinical_status``, ``molecular_weight``,
``inchikey``, ``smiles``, ...) on Compound nodes when the data source is
Phase 1 (the bridge path).

THE BUG THIS TEST LOCKS DOWN
----------------------------
Before FIX-B, ``step3_load_neo4j`` reconstructed each node as a bare
``{"id": eid, "entity_type": etype}`` dict:

    entity_type_data[etype] = [
        {"id": eid, "entity_type": etype}
        for eid in id_map.keys()
    ]
    node_results = builder.load_drkg_nodes(entity_type_data)

The bridge (``phase1_bridge.RecordingGraphBuilder``) had already populated
the full property dicts (``withdrawn``, ``fda_approved``,
``clinical_status``, ``molecular_weight``, ``inchikey``, ``smiles``,
etc.) on Compound nodes — and the in-memory test path preserved them —
but the production Neo4j load path STRIPPED them. Cerivastatin
(withdrawn 2001 for rhabdomyolysis) would have its ``withdrawn=True``
flag LOST in production Neo4j. The RL safety ranker would then treat
it as SAFE. Patient-safety risk.

THE FIX
-------
``step1_load_phase1`` now exposes a ``node_props_lookup`` dict keyed by
``(label, node_id)`` → full property dict. ``step3_load_neo4j`` reads
this lookup (when provided) to build the per-type node lists. The
production ``kg_builder.load_nodes_batch`` then applies
``NODE_PROPERTY_WHITELIST`` + ``SYSTEM_PROPS`` itself — keeping the
whitelist as the single source of truth for schema enforcement.

THE TEST
--------
1. Run the bridge to populate a ``RecordingGraphBuilder`` with the toy
   fixture shipped in ``phase1/processed_data/``.
2. Build ``entity_maps``, ``edge_maps`` and ``node_props_lookup`` (either
   via ``step1_load_phase1`` or directly from the recorder).
3. Call ``step3_load_neo4j(..., skip_neo4j=True, dry_run_capture=capture)``
   so we can inspect the exact node dicts that WOULD have been sent to
   Neo4j — without contacting Neo4j.
4. Assert that every Compound node in the captured ``entity_type_data``
   retains ``withdrawn``, ``fda_approved``, ``clinical_status``,
   ``molecular_weight``, ``inchikey``, ``smiles``.
5. Assert that the DRKG path (``node_props_lookup=None``) still produces
   the legacy bare-dict shape (so the fix is backward-compatible).
6. Assert that the production whitelist (``NODE_PROPERTY_WHITELIST``)
   would NOT strip the patient-safety properties (the whitelist
   explicitly allows them — bug #B5 fix).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

# ─── Ensure both phase1 and phase2 are importable ──────────────────────────
HERE = Path(__file__).resolve().parent
PHASE2_ROOT = HERE.parent
UNIFIED_ROOT = PHASE2_ROOT.parent
PHASE1_ROOT = UNIFIED_ROOT / "phase1"

for p in (str(PHASE2_ROOT), str(PHASE1_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from drugos_graph.phase1_bridge import (  # noqa: E402
    DEFAULT_PHASE1_PROCESSED_DIR,
    RecordingGraphBuilder,
    bridge_to_pyg_maps,
    run_phase1_to_phase2,
)
from drugos_graph.run_pipeline import (  # noqa: E402
    _build_entity_type_data,
    step1_load_phase1,
    step3_load_neo4j,
)


PHASE1_PROCESSED = PHASE1_ROOT / "processed_data"


# ---------------------------------------------------------------------------
# 1. step1_load_phase1 exposes node_props_lookup
# ---------------------------------------------------------------------------
class TestStep1ExposesNodePropsLookup:
    """FIX-B contract: step1_load_phase1 must return node_props_lookup."""

    def test_step1_returns_node_props_lookup_key(self):
        result = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        assert "node_props_lookup" in result, (
            "step1_load_phase1 must expose 'node_props_lookup' so step3 "
            "can load Compound nodes with their patient-safety properties "
            "(withdrawn/fda_approved/clinical_status). Without this key, "
            "step3 falls back to bare {id, entity_type} dicts — destroying "
            "every clinical-safety property in the production Neo4j load "
            "path. Patient-safety risk."
        )

    def test_node_props_lookup_is_non_empty(self):
        result = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        npl: Dict[Tuple[str, str], Dict[str, Any]] = result["node_props_lookup"]
        assert len(npl) > 0, (
            "node_props_lookup is empty — the bridge produced no nodes, "
            "or step1_load_phase1 failed to walk recorder.node_loads."
        )

    def test_node_props_lookup_compound_entries_have_safety_props(self):
        """Every Compound entry in node_props_lookup must carry the
        patient-safety properties the bridge attaches."""
        result = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        npl: Dict[Tuple[str, str], Dict[str, Any]] = result["node_props_lookup"]
        compound_entries = [
            v for (label, _nid), v in npl.items() if label == "Compound"
        ]
        assert len(compound_entries) > 0, (
            "node_props_lookup contains zero Compound entries — the toy "
            "fixture must ship at least one drug."
        )
        for n in compound_entries:
            # These three are the RL-safety ranker's primary signals.
            assert "withdrawn" in n, (
                f"Compound {n.get('id')} lost its 'withdrawn' flag in "
                f"node_props_lookup — patient-safety signal destroyed."
            )
            assert "fda_approved" in n, (
                f"Compound {n.get('id')} lost its 'fda_approved' flag."
            )
            assert "clinical_status" in n, (
                f"Compound {n.get('id')} lost its 'clinical_status' field."
            )


# ---------------------------------------------------------------------------
# 2. _build_entity_type_data — direct unit test of the helper
# ---------------------------------------------------------------------------
class TestBuildEntityTypeData:
    """The shared helper used by both the dry-run capture and the live
    Neo4j load path. Locks down both the Phase 1 (full-props) and DRKG
    (bare-dict) branches."""

    def test_phase1_path_preserves_full_property_dicts(self):
        """When node_props_lookup is provided, each node dict in the
        returned entity_type_data carries its full property set."""
        # Build a minimal recorder populated by the bridge.
        recorder = RecordingGraphBuilder()
        run_phase1_to_phase2(
            phase1_processed_dir=PHASE1_PROCESSED,
            builder=recorder,
        )
        entity_maps, _edge_maps = bridge_to_pyg_maps(recorder)
        node_props_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for load in recorder.node_loads:
            label = load["label"]
            for n in load["nodes"]:
                node_props_lookup[(label, n["id"])] = n

        etd = _build_entity_type_data(entity_maps, node_props_lookup)

        # Every Compound node must carry the safety properties.
        compound_nodes = etd.get("Compound", [])
        assert len(compound_nodes) > 0, "No Compound nodes in entity_type_data"
        for n in compound_nodes:
            assert n["id"], "Compound node missing 'id'"
            assert "withdrawn" in n, (
                f"Compound {n['id']} lost 'withdrawn' in entity_type_data"
            )
            assert "fda_approved" in n, (
                f"Compound {n['id']} lost 'fda_approved' in entity_type_data"
            )
            assert "clinical_status" in n, (
                f"Compound {n['id']} lost 'clinical_status' in entity_type_data"
            )
            # Bonus: confirm pchem/safety props the bridge emits too.
            for k in ("molecular_weight", "inchikey", "smiles"):
                assert k in n, (
                    f"Compound {n['id']} lost '{k}' in entity_type_data"
                )

    def test_drkg_path_falls_back_to_bare_dict(self):
        """When node_props_lookup is None (DRKG path), each node dict
        is the legacy bare ``{"id": eid, "entity_type": etype}`` shape.
        This locks in backward compatibility — DRKG nodes don't carry
        rich properties, so we must NOT regress that path."""
        # Fake entity_maps with two labels and a couple of IDs each.
        entity_maps = {
            "Compound": {"DB00001": 0, "DB00002": 1},
            "Disease": {"OMIM:100100": 0},
        }
        etd = _build_entity_type_data(entity_maps, node_props_lookup=None)

        assert set(etd.keys()) == {"Compound", "Disease"}
        for etype, nodes in etd.items():
            assert len(nodes) > 0
            for n in nodes:
                assert set(n.keys()) == {"id", "entity_type"}, (
                    f"DRKG-path node {n} should be bare {{id, entity_type}}; "
                    f"got keys {set(n.keys())}. The DRKG path must remain "
                    f"backward-compatible."
                )
                assert n["entity_type"] == etype

    def test_phase1_path_falls_back_for_missing_lookup_entry(self):
        """If a node ID is in entity_maps but missing from
        node_props_lookup, the helper falls back to the bare-dict shape
        rather than crashing. (Defensive — keeps production running
        even if the lookup is partially populated.)"""
        entity_maps = {"Compound": {"DB00001": 0, "DB00002": 1}}
        # Only DB00001 has a full property dict; DB00002 is missing.
        node_props_lookup = {
            ("Compound", "DB00001"): {
                "id": "DB00001",
                "withdrawn": True,
                "fda_approved": False,
            }
        }
        etd = _build_entity_type_data(entity_maps, node_props_lookup)
        nodes = etd["Compound"]
        assert len(nodes) == 2
        by_id = {n["id"]: n for n in nodes}
        # DB00001: full props
        assert "withdrawn" in by_id["DB00001"]
        assert by_id["DB00001"]["withdrawn"] is True
        # DB00002: bare-dict fallback
        assert set(by_id["DB00002"].keys()) == {"id", "entity_type"}


# ---------------------------------------------------------------------------
# 3. step3_load_neo4j with skip_neo4j=True + dry_run_capture
# ---------------------------------------------------------------------------
class TestStep3DryRunCapture:
    """The integration proof: call step3_load_neo4j exactly as the
    pipeline does, but with skip_neo4j=True + dry_run_capture so we can
    inspect what WOULD have been sent to Neo4j."""

    @pytest.fixture(scope="class")
    def captured(self) -> Dict[str, Any]:
        """Run step1 → step3 (skip_neo4j=True) and capture the
        entity_type_data that step3 would have loaded."""
        r1 = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        entity_maps = r1["entity_maps"]
        edge_maps = r1["edge_maps"]
        node_props_lookup = r1["node_props_lookup"]
        capture: Dict[str, Any] = {}
        step3_load_neo4j(
            entity_maps, edge_maps,
            skip_neo4j=True,
            fresh_start=True,
            edge_props_lookup=r1.get("edge_props_lookup"),
            node_props_lookup=node_props_lookup,
            dry_run_capture=capture,
        )
        return capture

    def test_capture_contains_entity_type_data(self, captured):
        assert "entity_type_data" in captured, (
            "dry_run_capture must populate 'entity_type_data' so tests can "
            "verify what would have been sent to Neo4j."
        )
        assert isinstance(captured["entity_type_data"], dict)

    def test_capture_flag_node_props_lookup_provided(self, captured):
        assert captured.get("node_props_lookup_provided") is True, (
            "dry_run_capture['node_props_lookup_provided'] must be True "
            "when node_props_lookup was supplied — confirms the Phase 1 "
            "branch was actually taken."
        )

    def test_compound_nodes_retain_withdrawn(self, captured):
        etd = captured["entity_type_data"]
        compound_nodes = etd.get("Compound", [])
        assert len(compound_nodes) > 0, (
            "No Compound nodes in captured entity_type_data — Phase 1 toy "
            "fixture must ship at least one drug."
        )
        for n in compound_nodes:
            assert "withdrawn" in n, (
                f"Compound {n.get('id')} would have its 'withdrawn' flag "
                f"STRIPPED by the Neo4j load path — patient-safety signal "
                f"destroyed. This is exactly the FIX-B regression."
            )
            # withdrawn must be a real bool, not None.
            assert n["withdrawn"] is not None, (
                f"Compound {n.get('id')} 'withdrawn' is None — the bridge "
                f"explicitly coerces to bool to avoid this."
            )

    def test_compound_nodes_retain_fda_approved(self, captured):
        etd = captured["entity_type_data"]
        for n in etd.get("Compound", []):
            assert "fda_approved" in n, (
                f"Compound {n.get('id')} lost 'fda_approved' in Neo4j load path"
            )

    def test_compound_nodes_retain_clinical_status(self, captured):
        etd = captured["entity_type_data"]
        for n in etd.get("Compound", []):
            assert "clinical_status" in n, (
                f"Compound {n.get('id')} lost 'clinical_status' in Neo4j load path"
            )

    def test_compound_nodes_retain_pchem_properties(self, captured):
        """Bonus: the bridge also emits molecular_weight/inchikey/smiles —
        they must survive the Neo4j load path too."""
        etd = captured["entity_type_data"]
        for n in etd.get("Compound", []):
            for k in ("molecular_weight", "inchikey", "smiles"):
                assert k in n, (
                    f"Compound {n.get('id')} lost '{k}' in Neo4j load path"
                )

    def test_no_compound_node_lost_all_properties(self, captured):
        """If the FIX-B regression were live, every Compound node would
        be a bare {id, entity_type} dict (2 keys). Lock down that NO
        Compound node has fewer than the safety-property key set."""
        etd = captured["entity_type_data"]
        required = {"id", "withdrawn", "fda_approved", "clinical_status"}
        for n in etd.get("Compound", []):
            keys = set(n.keys())
            missing = required - keys
            assert not missing, (
                f"Compound {n.get('id')} is missing required safety "
                f"properties {missing} in the Neo4j load path. Keys "
                f"present: {sorted(keys)}"
            )


# ---------------------------------------------------------------------------
# 4. DRKG path backward-compat — step3 with node_props_lookup=None
# ---------------------------------------------------------------------------
class TestStep3DrkgPathBackwardCompat:
    """When node_props_lookup is None (the DRKG path), step3 must still
    produce bare-dict node shapes — same as before FIX-B. This locks
    down that the fix did not regress the DRKG path."""

    @pytest.fixture(scope="class")
    def captured_drkg(self) -> Dict[str, Any]:
        """Use the Phase 1 entity_maps but pass node_props_lookup=None
        to simulate the DRKG path."""
        r1 = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        entity_maps = r1["entity_maps"]
        edge_maps = r1["edge_maps"]
        capture: Dict[str, Any] = {}
        step3_load_neo4j(
            entity_maps, edge_maps,
            skip_neo4j=True,
            fresh_start=True,
            edge_props_lookup=None,
            node_props_lookup=None,  # DRKG path
            dry_run_capture=capture,
        )
        return capture

    def test_drkg_path_flag(self, captured_drkg):
        assert captured_drkg.get("node_props_lookup_provided") is False

    def test_drkg_path_produces_bare_dicts(self, captured_drkg):
        """DRKG path: each node dict is {id, entity_type} only."""
        etd = captured_drkg["entity_type_data"]
        # Check at least the Compound nodes (which we know have full
        # props on the Phase 1 path — so this confirms the DRKG path
        # really is producing the legacy bare shape).
        compound_nodes = etd.get("Compound", [])
        assert len(compound_nodes) > 0
        for n in compound_nodes:
            assert set(n.keys()) == {"id", "entity_type"}, (
                f"DRKG-path Compound node {n} should be bare "
                f"{{id, entity_type}}; got keys {set(n.keys())}"
            )

    def test_drkg_path_does_not_have_withdrawn(self, captured_drkg):
        """Sanity check: the DRKG path explicitly does NOT carry
        withdrawn/fda_approved (DRKG nodes have no rich properties).
        This is the inverse of the Phase 1 test — confirms the two
        paths are distinguishable."""
        etd = captured_drkg["entity_type_data"]
        for n in etd.get("Compound", []):
            assert "withdrawn" not in n
            assert "fda_approved" not in n
            assert "clinical_status" not in n


# ---------------------------------------------------------------------------
# 5. NODE_PROPERTY_WHITELIST still preserves the safety properties
# ---------------------------------------------------------------------------
class TestWhitelistPreservesSafetyProperties:
    """The kg_builder.load_nodes_batch applies NODE_PROPERTY_WHITELIST
    itself. Lock down that the whitelist does NOT strip the
    patient-safety properties — that would re-introduce the bug at a
    different layer."""

    def test_withdrawn_is_whitelisted_for_compound(self):
        from drugos_graph.kg_builder import NODE_PROPERTY_WHITELIST, SYSTEM_PROPS

        allowed = NODE_PROPERTY_WHITELIST.get("Compound", frozenset()) | SYSTEM_PROPS
        for k in ("withdrawn", "fda_approved", "clinical_status",
                  "molecular_weight", "inchikey", "smiles", "id"):
            assert k in allowed, (
                f"'{k}' is NOT in the Compound NODE_PROPERTY_WHITELIST — "
                f"kg_builder.load_nodes_batch would silently strip it, "
                f"re-introducing the FIX-B bug at the whitelist layer."
            )

    def test_entity_type_is_NOT_whitelisted(self):
        """Sanity: 'entity_type' (the legacy bare-dict key) is NOT in
        the whitelist — it would be stripped by load_nodes_batch. This
        confirms the whitelist really is the schema enforcer."""
        from drugos_graph.kg_builder import NODE_PROPERTY_WHITELIST, SYSTEM_PROPS

        allowed = NODE_PROPERTY_WHITELIST.get("Compound", frozenset()) | SYSTEM_PROPS
        assert "entity_type" not in allowed


# ---------------------------------------------------------------------------
# 6. End-to-end: bridge → step1 → step3 (dry-run) → Compound safety props
# ---------------------------------------------------------------------------
class TestEndToEndPropertyPreservation:
    """The full proof: the same Compound node that the bridge emits
    (with withdrawn=True for cerivastatin-style drugs) is the same
    Compound node that step3 would send to Neo4j."""

    def test_at_least_one_withdrawn_compound_survives_to_neo4j_payload(self):
        """The toy fixture must include at least one withdrawn drug
        (cerivastatin-style). Verify that the 'withdrawn=True' signal
        survives all the way from the bridge CSV through step1 into
        step3's Neo4j payload."""
        r1 = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        node_props_lookup = r1["node_props_lookup"]
        # Find withdrawn compounds in the lookup.
        withdrawn_compounds = [
            (nid, props) for (label, nid), props in node_props_lookup.items()
            if label == "Compound" and props.get("withdrawn") is True
        ]
        # The toy fixture may or may not include a withdrawn drug — if
        # it does, the property must round-trip into step3's payload.
        if not withdrawn_compounds:
            pytest.skip(
                "Toy fixture has no withdrawn Compound — cannot verify "
                "the withdrawn=True round-trip. (Bridge still emits "
                "withdrawn=False for every Compound; that's covered by "
                "the other tests.)"
            )

        # Run step3 with dry_run_capture and confirm the withdrawn
        # compounds appear with withdrawn=True in the captured payload.
        capture: Dict[str, Any] = {}
        step3_load_neo4j(
            r1["entity_maps"], r1["edge_maps"],
            skip_neo4j=True,
            node_props_lookup=node_props_lookup,
            dry_run_capture=capture,
        )
        etd = capture["entity_type_data"]
        captured_by_id = {n["id"]: n for n in etd.get("Compound", [])}
        for nid, props in withdrawn_compounds:
            assert nid in captured_by_id, (
                f"Withdrawn Compound {nid} disappeared between step1 and "
                f"step3's Neo4j payload."
            )
            assert captured_by_id[nid].get("withdrawn") is True, (
                f"Withdrawn Compound {nid} had withdrawn=True in the "
                f"bridge output but withdrawn={captured_by_id[nid].get('withdrawn')!r} "
                f"in step3's Neo4j payload — patient-safety signal lost."
            )

    def test_every_compound_in_step3_payload_has_a_bridge_source(self):
        """Every Compound node that step3 would send to Neo4j must be
        traceable back to a bridge-emitted node_props_lookup entry.
        (No phantom nodes invented by step3.)"""
        r1 = step1_load_phase1(phase1_processed_dir=PHASE1_PROCESSED)
        node_props_lookup = r1["node_props_lookup"]
        capture: Dict[str, Any] = {}
        step3_load_neo4j(
            r1["entity_maps"], r1["edge_maps"],
            skip_neo4j=True,
            node_props_lookup=node_props_lookup,
            dry_run_capture=capture,
        )
        etd = capture["entity_type_data"]
        for n in etd.get("Compound", []):
            key = ("Compound", n["id"])
            assert key in node_props_lookup, (
                f"Compound {n['id']} in step3's Neo4j payload has no "
                f"corresponding bridge entry in node_props_lookup — "
                f"step3 invented a phantom node."
            )
