"""
FIX-F verification tests for the v26 data-quality + schema fixes.

Each test class corresponds to one of the six problems fixed in this audit
pass (C-7, C-8, C-10, C-16, C-18, C-19). Tests are written so that they
FAIL loudly if any of the fixes are reverted.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — make both phase1 and phase2 importable.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # v26_upgraded/
PHASE1_DIR = PROJECT_ROOT / "phase1"
PHASE2_DIR = PROJECT_ROOT / "phase2"
for p in (str(PHASE1_DIR), str(PHASE2_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

PROCESSED_DIR = PHASE1_DIR / "processed_data"


# ===========================================================================
# C-7: Shipped fixture data must be scientifically correct
# ===========================================================================
class TestC7ScientificallyCorrectFixtures:
    """Verify the drugbank_indications.csv and disgenet_gene_disease_associations.csv
    fixtures contain REAL drug-disease and gene-disease biology."""

    def test_drugbank_indications_no_aspirin_sickle_cell(self):
        """C-7: Aspirin (DB00645) must NOT be marked approved for Sickle cell anemia."""
        path = PROCESSED_DIR / "drugbank_indications.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["drugbank_id"] == "DB00645" and r["disease_id"] == "OMIM:603903":
                if r["indication_type"] == "approved":
                    pytest.fail(
                        "DB00645 (Aspirin) is still marked approved for "
                        "Sickle cell anemia — scientifically false."
                    )

    def test_drugbank_indications_aspirin_has_real_indication(self):
        """C-7: Aspirin must have a REAL indication (Pain, Cardiovascular disease, Fever)."""
        path = PROCESSED_DIR / "drugbank_indications.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        aspirin_rows = [r for r in rows if r["drugbank_id"] == "DB00645"]
        assert aspirin_rows, "No DB00645 (Aspirin) rows in indications CSV"
        real_indications = {"Pain", "Cardiovascular disease", "Fever"}
        found = {(r["disease_name"] or "").strip() for r in aspirin_rows}
        assert found & real_indications, (
            f"Aspirin must have at least one real FDA-approved indication "
            f"from {real_indications}; found {found}"
        )

    def test_drugbank_indications_hepatitis_b_vaccine_correct(self):
        """C-7: DB00011 (Hepatitis B vaccine) must NOT be approved for Cystic fibrosis."""
        path = PROCESSED_DIR / "drugbank_indications.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["drugbank_id"] == "DB00011" and r["disease_id"] == "OMIM:219700":
                pytest.fail(
                    "DB00011 (Hepatitis B vaccine) is still marked approved "
                    "for Cystic fibrosis — scientifically false."
                )
        # And must now be approved for Hepatitis B
        hb_rows = [r for r in rows if r["drugbank_id"] == "DB00011"]
        assert hb_rows, "No DB00011 rows in indications CSV"
        assert any(
            "hepatitis b" in (r["disease_name"] or "").lower() for r in hb_rows
        ), f"DB00011 must now indicate Hepatitis B; found {hb_rows}"

    def test_drugbank_indications_pegademase_correct(self):
        """C-7: DB00008 (Pegademase bovine) must be approved for ADA-SCID (OMIM:102700),
        not Cystic fibrosis."""
        path = PROCESSED_DIR / "drugbank_indications.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["drugbank_id"] == "DB00008" and r["disease_id"] == "OMIM:219700":
                pytest.fail(
                    "DB00008 (Pegademase bovine) is still marked for "
                    "Cystic fibrosis — scientifically false."
                )
        ada_rows = [
            r for r in rows
            if r["drugbank_id"] == "DB00008" and r["disease_id"] == "OMIM:102700"
        ]
        assert ada_rows, (
            "DB00008 must be approved for Adenosine deaminase deficiency (OMIM:102700)"
        )

    def test_drugbank_indications_lepirudin_correct(self):
        """C-7: DB00001 (Lepirudin) must be approved for Heparin-induced thrombocytopenia."""
        path = PROCESSED_DIR / "drugbank_indications.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        lep_rows = [r for r in rows if r["drugbank_id"] == "DB00001"]
        assert lep_rows, "No DB00001 rows in indications CSV"
        assert any(
            "heparin-induced thrombocytopenia" in (r["disease_name"] or "").lower()
            for r in lep_rows
        ), f"DB00001 (Lepirudin) must be approved for HIT; found {lep_rows}"

    def test_drugbank_indications_no_db00463_marfan(self):
        """C-7: DB00463 must NOT be marked investigational for Marfan syndrome."""
        path = PROCESSED_DIR / "drugbank_indications.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["drugbank_id"] == "DB00463" and r["disease_id"] == "OMIM:154700":
                pytest.fail(
                    "DB00463 is still marked for Marfan syndrome — should be replaced."
                )

    def test_disgenet_no_hmgcr_marfan(self):
        """C-7: HMGCR must NOT be associated with Marfan syndrome."""
        path = PROCESSED_DIR / "disgenet_gene_disease_associations.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["gene_symbol"] == "HMGCR" and r["disease_id"] == "OMIM:154700":
                pytest.fail(
                    "HMGCR -> Marfan syndrome is still in disgenet CSV — scientifically false."
                )

    def test_disgenet_fbn1_marfan_present(self):
        """C-7: FBN1 -> Marfan syndrome (correct biology) must be present."""
        path = PROCESSED_DIR / "disgenet_gene_disease_associations.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        fbn1_marfan = [
            r for r in rows
            if r["gene_symbol"] == "FBN1" and r["disease_id"] == "OMIM:154700"
        ]
        assert fbn1_marfan, (
            "FBN1 -> Marfan syndrome must be present (correct biology — Marfan "
            "is caused by FBN1 mutations)."
        )

    def test_disgenet_cftr_and_dmd_preserved(self):
        """C-7: CFTR -> CF and DMD -> DMD must remain (these were already correct)."""
        path = PROCESSED_DIR / "disgenet_gene_disease_associations.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert any(
            r["gene_symbol"] == "CFTR" and r["disease_id"] == "OMIM:219700"
            for r in rows
        ), "CFTR -> Cystic fibrosis was inadvertently removed."
        assert any(
            r["gene_symbol"] == "DMD" and r["disease_id"] == "OMIM:310200"
            for r in rows
        ), "DMD -> DMD was inadvertently removed."


# ===========================================================================
# C-8: ChEMBL activity provenance + uM→nM conversion
# ===========================================================================
class TestC8ChEMBLProvenanceAndUnits:
    """Verify the provenance sidecar matches the CSV and units are nM."""

    def test_provenance_row_count_matches_csv(self):
        """C-8: provenance.row_count must equal actual CSV row count (6, not 0)."""
        prov_path = PROCESSED_DIR / "chembl_activities_clean.csv.provenance.json"
        with open(prov_path) as f:
            prov = json.load(f)
        csv_path = PROCESSED_DIR / "chembl_activities_clean.csv"
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
        assert prov.get("row_count") == len(rows), (
            f"provenance.row_count={prov.get('row_count')} != "
            f"actual rows={len(rows)}"
        )
        assert prov.get("row_count") == 6, (
            f"Expected 6 rows (was 0); got {prov.get('row_count')}"
        )

    def test_provenance_columns_match_csv(self):
        """C-8: provenance.columns must list the actual CSV columns."""
        prov_path = PROCESSED_DIR / "chembl_activities_clean.csv.provenance.json"
        with open(prov_path) as f:
            prov = json.load(f)
        csv_path = PROCESSED_DIR / "chembl_activities_clean.csv"
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            cols = list(reader.fieldnames or [])
        assert prov.get("columns") == cols, (
            f"provenance.columns={prov.get('columns')} != CSV cols={cols}"
        )

    def test_activity_units_are_nm(self):
        """C-8: rows with concentration units must be 'nM' (not 'uM')."""
        csv_path = PROCESSED_DIR / "chembl_activities_clean.csv"
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["activity_units"] in ("uM", "nM", "mM"):
                assert r["activity_units"] == "nM", (
                    f"Concentration row {r['activity_id']} has units "
                    f"{r['activity_units']!r}, expected 'nM'"
                )

    def test_activity_values_converted_correctly(self):
        """C-8: uM values must be multiplied by 1000 to become nM values."""
        csv_path = PROCESSED_DIR / "chembl_activities_clean.csv"
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
        # Original uM values were 0.05, 0.04, 0.5 (rows 1, 2, 5).
        # After x1000: 50.0, 40.0, 500.0.
        by_id = {r["activity_id"]: r for r in rows}
        assert float(by_id["1"]["activity_value"]) == pytest.approx(50.0)
        assert by_id["1"]["activity_units"] == "nM"
        assert float(by_id["2"]["activity_value"]) == pytest.approx(40.0)
        assert by_id["2"]["activity_units"] == "nM"
        assert float(by_id["5"]["activity_value"]) == pytest.approx(500.0)
        assert by_id["5"]["activity_units"] == "nM"
        # % rows must be untouched.
        assert float(by_id["3"]["activity_value"]) == pytest.approx(95.0)
        assert by_id["3"]["activity_units"] == "%"


# ===========================================================================
# C-10: OMIM _EMBEDDED_GENE_XREF crosswalk must be >= 50 genes
# ===========================================================================
class TestC10OmimGeneCrosswalk:
    """Verify the embedded gene crosswalk has been expanded to 50+ genes."""

    def test_crosswalk_size_at_least_50(self):
        from pipelines.omim_pipeline import _EMBEDDED_GENE_XREF
        assert len(_EMBEDDED_GENE_XREF) >= 50, (
            f"Crosswalk must have >= 50 entries; has {len(_EMBEDDED_GENE_XREF)}"
        )

    def test_original_9_genes_preserved(self):
        from pipelines.omim_pipeline import _EMBEDDED_GENE_XREF
        for sym in ("CFTR", "DMD", "FANCE", "FBN1", "FGFR3",
                    "HBB", "HFE", "HTT", "KIT"):
            assert sym in _EMBEDDED_GENE_XREF, (
                f"Original gene {sym} was removed from crosswalk"
            )

    @pytest.mark.parametrize("sym,ncbi,uniprot", [
        ("TP53", "7157", "P04637"),
        ("BRCA1", "672", "P38398"),
        ("BRCA2", "675", "P51587"),
        ("EGFR", "1956", "P00533"),
        ("KRAS", "3845", "P01116"),
        ("APOE", "348", "P02649"),
        ("APP", "351", "P05067"),
        ("MAPT", "4137", "P10636"),
        ("LRRK2", "120892", "Q5S007"),
        ("SNCA", "6622", "P37840"),
        ("TNF", "7124", "P01375"),
        ("IL6", "3569", "P05231"),
        ("PTEN", "5728", "P60484"),
        ("VHL", "7428", "P40337"),
        ("ATM", "472", "Q13315"),
        ("WT1", "7490", "P19544"),
        ("TSC2", "7249", "P49815"),
        ("NF1", "4763", "P21359"),
        ("APC", "324", "P25054"),
        ("MLH1", "4292", "P40692"),
        ("MSH2", "4436", "P43246"),
    ])
    def test_clinically_important_genes_present(self, sym, ncbi, uniprot):
        """C-10: each clinically-important gene must be in the crosswalk with
        verified NCBI gene ID and canonical UniProt accession."""
        from pipelines.omim_pipeline import _EMBEDDED_GENE_XREF
        assert sym in _EMBEDDED_GENE_XREF, f"{sym} missing from crosswalk"
        entry = _EMBEDDED_GENE_XREF[sym]
        assert entry["ncbi_gene_id"] == ncbi, (
            f"{sym} ncbi_gene_id={entry['ncbi_gene_id']} != expected {ncbi}"
        )
        assert entry["uniprot_id"] == uniprot, (
            f"{sym} uniprot_id={entry['uniprot_id']} != expected {uniprot}"
        )


# ===========================================================================
# C-16: ClinicalOutcome node type + bridge loader
# ===========================================================================
class TestC16ClinicalOutcomeNode:
    """Verify ClinicalOutcome is in CORE_NODE_TYPES and the bridge produces it."""

    def test_clinical_outcome_in_core_node_types(self):
        from drugos_graph.config import CORE_NODE_TYPES
        assert "ClinicalOutcome" in CORE_NODE_TYPES, (
            f"ClinicalOutcome missing from CORE_NODE_TYPES: {CORE_NODE_TYPES}"
        )

    def test_clinical_outcome_id_pattern_defined(self):
        from drugos_graph.kg_builder import ID_PATTERNS
        assert "ClinicalOutcome" in ID_PATTERNS, (
            "ClinicalOutcome missing from ID_PATTERNS"
        )

    def test_clinical_outcome_property_whitelist_defined(self):
        from drugos_graph.kg_builder import NODE_PROPERTY_WHITELIST
        assert "ClinicalOutcome" in NODE_PROPERTY_WHITELIST, (
            "ClinicalOutcome missing from NODE_PROPERTY_WHITELIST"
        )
        wl = NODE_PROPERTY_WHITELIST["ClinicalOutcome"]
        for prop in ("id", "name", "disease_id", "indication_type",
                     "source_drug_id", "source"):
            assert prop in wl, f"ClinicalOutcome whitelist missing {prop}"

    def test_has_clinical_outcome_edge_type_in_core_edges(self):
        from drugos_graph.config import CORE_EDGE_TYPES
        assert ("Compound", "has_clinical_outcome", "ClinicalOutcome") in CORE_EDGE_TYPES, (
            "Compound-has_clinical_outcome-ClinicalOutcome edge missing from CORE_EDGE_TYPES"
        )

    def test_bridge_produces_clinical_outcome_nodes(self):
        """C-16: the bridge must produce ClinicalOutcome nodes from
        drugbank_indications.csv when run on the actual fixture data."""
        from drugos_graph.phase1_bridge import (
            run_phase1_to_phase2, RecordingGraphBuilder,
        )
        rec = RecordingGraphBuilder()
        report = run_phase1_to_phase2(
            phase1_processed_dir=str(PROCESSED_DIR),
            builder=rec,
        )
        staged = report["staged"]
        assert len(staged.clinical_outcome_nodes) > 0, (
            "Bridge produced zero ClinicalOutcome nodes — C-16 fix did not take effect"
        )
        # Each node must carry the required properties.
        for n in staged.clinical_outcome_nodes:
            assert n["id"].startswith("CO:"), (
                f"ClinicalOutcome node id must start with 'CO:': {n['id']}"
            )
            for prop in ("disease_id", "indication_type", "source_drug_id"):
                assert prop in n, (
                    f"ClinicalOutcome node missing required property {prop}: {n}"
                )

    def test_bridge_produces_has_clinical_outcome_edges(self):
        """C-16: the bridge must emit (Compound, has_clinical_outcome, ClinicalOutcome) edges."""
        from drugos_graph.phase1_bridge import (
            run_phase1_to_phase2, RecordingGraphBuilder,
        )
        rec = RecordingGraphBuilder()
        report = run_phase1_to_phase2(
            phase1_processed_dir=str(PROCESSED_DIR),
            builder=rec,
        )
        staged = report["staged"]
        key = ("Compound", "has_clinical_outcome", "ClinicalOutcome")
        assert key in staged.edges, (
            "has_clinical_outcome edge type not produced by the bridge"
        )
        assert len(staged.edges[key]) > 0, "has_clinical_outcome edges list is empty"

    def test_pathway_warning_emitted(self):
        """C-16: the bridge must emit a WARNING that Pathway nodes are not
        produced from the toy fixture (TODO until STRING pathway data is wired)."""
        from drugos_graph.phase1_bridge import (
            run_phase1_to_phase2, RecordingGraphBuilder,
        )
        rec = RecordingGraphBuilder()
        report = run_phase1_to_phase2(
            phase1_processed_dir=str(PROCESSED_DIR),
            builder=rec,
        )
        warnings = report["staged"].warnings
        assert any("Pathway" in w for w in warnings), (
            f"Expected a Pathway-related warning; got {warnings}"
        )


# ===========================================================================
# C-18: Unified dead-letter queue
# ===========================================================================
class TestC18UnifiedDeadLetterQueue:
    """Verify cleaning.get_dead_letters() aggregates from all three queues."""

    def setup_method(self):
        """Clear all three queues before each test."""
        from cleaning import clear_dead_letters
        from cleaning.deduplicator import clear_dead_letters as clear_dedup
        from cleaning.missing_values import clear_dead_letters as clear_mv
        clear_dead_letters()
        clear_dedup()
        clear_mv()

    def test_get_dead_letters_aggregates_dedup(self):
        from cleaning import get_dead_letters
        from cleaning.deduplicator import _dead_letter_queue
        _dead_letter_queue.append({"test": "from dedup"})
        letters = get_dead_letters()
        assert any(l.get("test") == "from dedup" for l in letters), (
            "Dedup dead letters not aggregated into get_dead_letters()"
        )

    def test_get_dead_letters_aggregates_missing_values(self):
        from cleaning import get_dead_letters
        from cleaning.missing_values import _dead_letter_queue as mv_q
        mv_q.append({"test": "from mv"})
        letters = get_dead_letters()
        assert any(l.get("test") == "from mv" for l in letters), (
            "missing_values dead letters not aggregated into get_dead_letters()"
        )

    def test_get_dead_letters_aggregates_all_three(self):
        from cleaning import get_dead_letters, _dead_letters as pkg_q
        from cleaning.deduplicator import _dead_letter_queue as dedup_q
        from cleaning.missing_values import _dead_letter_queue as mv_q
        pkg_q.append({"test": "from pkg"})
        dedup_q.append({"test": "from dedup"})
        mv_q.append({"test": "from mv"})
        letters = get_dead_letters()
        tests = {l.get("test") for l in letters}
        assert tests >= {"from pkg", "from dedup", "from mv"}, (
            f"Expected all three queues aggregated; got {tests}"
        )

    def test_get_dead_letters_returns_list_not_internal_reference(self):
        """Aggregation must return a fresh list (mutating the return value
        must not corrupt the internal queues)."""
        from cleaning import get_dead_letters
        from cleaning.deduplicator import _dead_letter_queue as dedup_q
        dedup_q.append({"test": "from dedup"})
        letters = get_dead_letters()
        letters.clear()
        # Internal dedup queue must NOT have been cleared.
        assert any(l.get("test") == "from dedup" for l in dedup_q), (
            "get_dead_letters() returned the internal list reference — "
            "caller mutations corrupted the queue."
        )


# ===========================================================================
# C-19: InChIKey regex accepts protonation indicator
# ===========================================================================
class TestC19InchiKeyProtonation:
    """Verify the InChIKey pattern accepts both standard 27-char keys and
    keys with an optional protonation indicator suffix."""

    def test_normalize_standard_27_char_key(self):
        from cleaning.normalizer import normalize_inchikey
        r = normalize_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
        assert r == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"

    def test_normalize_key_with_protonation_suffix(self):
        from cleaning.normalizer import normalize_inchikey
        r = normalize_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N-a")
        assert r is not None
        # normalize_inchikey uppercases everything (standard 27-char blocks
        # are uppercase; the protonation suffix is also uppercased).
        assert r.upper() == r

    def test_validate_standard_27_char_key(self):
        from cleaning.normalizer import validate_inchikey
        assert validate_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") is True

    def test_validate_key_with_protonation_suffix(self):
        from cleaning.normalizer import validate_inchikey, normalize_inchikey
        # normalize first (since validate expects normalized input)
        normalized = normalize_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N-a")
        assert validate_inchikey(normalized) is True, (
            f"Protonation-form InChIKey {normalized!r} was rejected by "
            f"validate_inchikey() — C-19 regex fix did not take effect."
        )

    def test_pattern_accepts_protonation_form_directly(self):
        """The _INCHIKEY_PATTERN regex itself must match the protonation form."""
        from cleaning.normalizer import _INCHIKEY_PATTERN
        assert _INCHIKEY_PATTERN.match("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
        assert _INCHIKEY_PATTERN.match("BSYNRYMUTXBXSQ-UHFFFAOYSA-N-a")
        assert _INCHIKEY_PATTERN.match("BSYNRYMUTXBXSQ-UHFFFAOYSA-N-A")
        # Lowercase protonation indicator is also accepted by the pattern.
        assert _INCHIKEY_PATTERN.match("BSYNRYMUTXBXSQ-UHFFFAOYSA-N-z")

    def test_pattern_still_rejects_invalid_forms(self):
        """The regex must still reject malformed InChIKeys."""
        from cleaning.normalizer import _INCHIKEY_PATTERN
        # Too short
        assert not _INCHIKEY_PATTERN.match("BSYNRYMUTXBXSQ-UHFFFAOYSA")
        # Wrong block lengths
        assert not _INCHIKEY_PATTERN.match("BSYNRYMUTXBXSQ-UHFFFAOYS-N")
        # Random garbage
        assert not _INCHIKEY_PATTERN.match("not-an-inchikey")
        assert not _INCHIKEY_PATTERN.match("")
