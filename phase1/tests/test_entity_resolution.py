"""
Comprehensive tests for entity resolution modules.

Tests cover:
  - entity_resolution.resolver_utils  (normalize_name, fuzzy_match_score,
    extract_inchikey_first_block, build_name_index, build_inchikey_index,
    compute_match_confidence)
  - entity_resolution.drug_resolver  (DrugResolver: exact InChIKey match,
    connectivity match, name match, full build_mapping, aspirin integration)
  - entity_resolution.protein_resolver  (ProteinResolver: UniProt exact,
    gene+organism match, DataFrame output)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Ensure project root importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entity_resolution.drug_resolver import DrugResolver
from entity_resolution.protein_resolver import ProteinResolver
from entity_resolution.resolver_utils import (
    build_inchikey_index,
    build_name_index,
    compute_match_confidence,
    extract_inchikey_first_block,
    fuzzy_match_score,
    normalize_name,
)
from entity_resolution.base import ResolverConfig


# =====================================================================
# 1. Exact InChIKey match
# =====================================================================


class TestExactInchikeyMatch:
    """Same InChIKey in ChEMBL and DrugBank records -> single canonical entry."""

    def test_exact_inchikey_match(self):
        resolver = DrugResolver()
        chembl_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Aspirin",
                "chembl_id": "CHEMBL25",
            }
        ]
        drugbank_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Acetylsalicylic acid",
                "drugbank_id": "DB00945",
            }
        ]
        resolver.add_source_records(chembl_records, source="chembl")
        resolver.add_source_records(drugbank_records, source="drugbank")

        # Should be a single canonical entry with both IDs
        assert len(resolver.mapping) == 1
        canonical_ik = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        assert canonical_ik in resolver.mapping
        entry = resolver.mapping[canonical_ik]
        assert entry["chembl_id"] == "CHEMBL25"
        assert entry["drugbank_id"] == "DB00945"


# =====================================================================
# 2. Name normalization match
# =====================================================================


class TestNameNormalizationMatch:
    """Names that normalize to the same string are matched."""

    def test_name_normalization_match(self):
        result1 = normalize_name("Acetylsalicylic acid")
        result2 = normalize_name("acetylsalicylicacid")
        # After normalization both should be identical
        assert result1 == result2


# =====================================================================
# 3. Connectivity match (first 14 chars)
# =====================================================================


class TestConnectivityMatch:
    """Same InChIKey first 14 chars but different stereochemistry.

    Audit D3-4 fix: the default ``collapse_stereoisomers=False`` keeps
    stereoisomers distinct (patient-safety).  The legacy behaviour
    (merging by connectivity block) is now opt-in via
    ``ResolverConfig(collapse_stereoisomers=True)``.
    """

    def test_connectivity_match(self):
        """Opt-in stereoisomer collapse merges by first 14 chars."""
        cfg = ResolverConfig(collapse_stereoisomers=True)
        resolver = DrugResolver(config=cfg)
        # Two InChIKeys that share the first 14 chars (connectivity block)
        # but differ in stereochemistry
        chembl_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Aspirin",
                "chembl_id": "CHEMBL25",
            }
        ]
        drugbank_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-ZXQBJXABSA-N",  # same first 14 chars
                "name": "Aspirin-enantiomer",
                "drugbank_id": "DB99999",
            }
        ]
        resolver.add_source_records(chembl_records, source="chembl")
        resolver.add_source_records(drugbank_records, source="drugbank")

        # Both records should resolve to the same canonical InChIKey
        assert len(resolver.mapping) == 1
        canonical_ik = list(resolver.mapping.keys())[0]
        assert canonical_ik == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        entry = resolver.mapping[canonical_ik]
        assert entry["chembl_id"] == "CHEMBL25"
        assert entry["drugbank_id"] == "DB99999"

    def test_stereoisomer_collapse_off_by_default(self):
        """Default config keeps stereoisomers distinct (D3-4 fix)."""
        resolver = DrugResolver()  # collapse_stereoisomers=False default
        assert resolver.config.collapse_stereoisomers is False
        chembl_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Aspirin",
                "chembl_id": "CHEMBL25",
            }
        ]
        drugbank_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-ZXQBJXABSA-N",  # same first 14 chars
                "name": "Aspirin-enantiomer",
                "drugbank_id": "DB99999",
            }
        ]
        resolver.add_source_records(chembl_records, source="chembl")
        resolver.add_source_records(drugbank_records, source="drugbank")

        # Default config keeps them as separate canonical entries
        assert len(resolver.mapping) == 2


# =====================================================================
# 4. No false positives
# =====================================================================


class TestNoFalsePositives:
    """Different drugs with different InChIKeys don't merge."""

    def test_no_false_positives(self):
        resolver = DrugResolver()
        chembl_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",  # Aspirin
                "name": "Aspirin",
                "chembl_id": "CHEMBL25",
            }
        ]
        drugbank_records = [
            {
                "inchikey": "WFXAZNNJSJXTJZ-UHFFFAOYSA-N",  # Ibuprofen
                "name": "Ibuprofen",
                "drugbank_id": "DB01050",
            }
        ]
        resolver.add_source_records(chembl_records, source="chembl")
        resolver.add_source_records(drugbank_records, source="drugbank")

        # Should remain two separate canonical entries
        assert len(resolver.mapping) == 2
        keys = set(resolver.mapping.keys())
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in keys
        assert "WFXAZNNJSJXTJZ-UHFFFAOYSA-N" in keys


# =====================================================================
# 5. Fuzzy match threshold
# =====================================================================


class TestFuzzyMatchThreshold:
    """Verify fuzzy_match_score returns 0 for completely different names."""

    def test_fuzzy_match_completely_different(self):
        score = fuzzy_match_score("aspirin", "xylophone")
        assert score < 0.5  # very different names

    def test_fuzzy_match_identical(self):
        score = fuzzy_match_score("aspirin", "aspirin")
        assert score == 1.0

    def test_fuzzy_match_empty(self):
        assert fuzzy_match_score("", "aspirin") == 0.0
        assert fuzzy_match_score("aspirin", "") == 0.0


# =====================================================================
# 6. normalize_name strips punctuation
# =====================================================================


class TestNormalizeName:
    """Tests for ``entity_resolution.resolver_utils.normalize_name``."""

    def test_strips_punctuation(self):
        """'Aspirin, (acetyl)' becomes 'aspirin' — parenthetical content
        is removed first, then punctuation and spaces are stripped."""
        result = normalize_name("Aspirin, (acetyl)")
        # "(acetyl)" is removed by the parentheses regex, then ", " is
        # stripped by the non-alnum regex → "aspirin"
        assert result == "aspirin"

    def test_strips_hyphens_and_spaces(self):
        """Hyphens are now preserved (Fix #34) to distinguish stereochemistry."""
        result = normalize_name("Acetyl-salicylic acid")
        assert result == "acetyl-salicylicacid"

    def test_non_parenthetical_content_preserved(self):
        """Content outside parentheses is kept after normalization."""
        result = normalize_name("Aspirin acetyl")
        assert result == "aspirinacetyl"

    def test_none_returns_empty(self):
        assert normalize_name(None) == ""

    def test_empty_returns_empty(self):
        assert normalize_name("") == ""


# =====================================================================
# 7. extract_inchikey_first_block
# =====================================================================


class TestExtractInchikeyFirstBlock:
    """Tests for ``entity_resolution.resolver_utils.extract_inchikey_first_block``."""

    def test_valid_inchikey(self):
        assert extract_inchikey_first_block("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") == "BSYNRYMUTXBXSQ"

    def test_short_string(self):
        """Strings shorter than 14 chars return None."""
        assert extract_inchikey_first_block("SHORT") is None

    def test_empty_string(self):
        assert extract_inchikey_first_block("") is None

    def test_none(self):
        assert extract_inchikey_first_block(None) is None


# =====================================================================
# 8. compute_match_confidence
# =====================================================================


class TestComputeMatchConfidence:
    """Tests for ``entity_resolution.resolver_utils.compute_match_confidence``."""

    def test_inchikey_exact(self):
        assert compute_match_confidence("inchikey_exact") == 1.0

    def test_inchikey_connectivity(self):
        assert compute_match_confidence("inchikey_connectivity") == 0.9

    def test_name_normalized(self):
        assert compute_match_confidence("name_normalized") == 0.8

    def test_pubchem_xref(self):
        assert compute_match_confidence("pubchem_xref") == 0.7

    def test_fuzzy(self):
        # D3-3 fix: fuzzy confidence raised from 0.6 → 0.85 so that
        # METHOD_CONFIDENCE["fuzzy"] >= _FUZZY_THRESHOLD (0.85).
        assert compute_match_confidence("fuzzy") == 0.85

    def test_uniprot_exact(self):
        assert compute_match_confidence("uniprot_exact") == 1.0

    def test_gene_name_organism(self):
        assert compute_match_confidence("gene_name_organism") == 0.85

    def test_unknown_method(self):
        assert compute_match_confidence("nonexistent_method") == 0.5


# =====================================================================
# 9. DrugResolver build_mapping
# =====================================================================


class TestDrugResolverBuildMapping:
    """Create 3 small DataFrames (chembl, drugbank, pubchem) with overlapping
    drugs; verify merged correctly."""

    def test_build_mapping(self):
        chembl_df = pd.DataFrame(
            {
                "inchikey": [
                    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                    "WFXAZNNJSJXTJZ-UHFFFAOYSA-N",
                ],
                "name": ["Aspirin", "Ibuprofen"],
                "chembl_id": ["CHEMBL25", "CHEMBL521"],
            }
        )
        drugbank_df = pd.DataFrame(
            {
                "inchikey": [
                    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",  # overlap with ChEMBL
                    "HEFNNWSQWZIEIR-UHFFFAOYSA-N",  # new drug: Paracetamol
                ],
                "name": ["Acetylsalicylic acid", "Paracetamol"],
                "drugbank_id": ["DB00945", "DB00316"],
            }
        )
        pubchem_df = pd.DataFrame(
            {
                "inchikey": [
                    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",  # overlap
                    "HEFNNWSQWZIEIR-UHFFFAOYSA-N",  # overlap
                ],
                "name": ["Aspirin", "Acetaminophen"],
                "pubchem_cid": [2244, 1983],
            }
        )

        resolver = DrugResolver()
        result_df = resolver.build_mapping(chembl_df, drugbank_df, pubchem_df)

        # 3 unique drugs total
        assert len(result_df) == 3

        # Aspirin should have all 3 cross-database IDs
        aspirin_row = result_df[
            result_df["canonical_inchikey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        ]
        assert len(aspirin_row) == 1
        aspirin = aspirin_row.iloc[0]
        assert aspirin["chembl_id"] == "CHEMBL25"
        assert aspirin["drugbank_id"] == "DB00945"
        assert aspirin["pubchem_cid"] == 2244

        # Paracetamol should have drugbank + pubchem but no chembl_id
        para_row = result_df[
            result_df["canonical_inchikey"] == "HEFNNWSQWZIEIR-UHFFFAOYSA-N"
        ]
        assert len(para_row) == 1
        para = para_row.iloc[0]
        assert para["drugbank_id"] == "DB00316"
        assert para["pubchem_cid"] == 1983


# =====================================================================
# 10. DrugResolver aspirin integration
# =====================================================================


class TestDrugResolverAspirinIntegration:
    """Full integration: CHEMBL25/Aspirin + DB00945/Acetylsalicylic acid +
    CID 2244 -> single canonical entry."""

    def test_aspirin_integration(self):
        chembl_df = pd.DataFrame(
            {
                "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "name": ["Aspirin"],
                "chembl_id": ["CHEMBL25"],
            }
        )
        drugbank_df = pd.DataFrame(
            {
                "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "name": ["Acetylsalicylic acid"],
                "drugbank_id": ["DB00945"],
            }
        )
        pubchem_df = pd.DataFrame(
            {
                "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                "name": ["2-acetoxybenzoic acid"],
                "pubchem_cid": [2244],
            }
        )

        resolver = DrugResolver()
        result_df = resolver.build_mapping(chembl_df, drugbank_df, pubchem_df)

        assert len(result_df) == 1
        row = result_df.iloc[0]
        assert row["canonical_inchikey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        assert row["chembl_id"] == "CHEMBL25"
        assert row["drugbank_id"] == "DB00945"
        assert row["pubchem_cid"] == 2244


# =====================================================================
# 11. ProteinResolver UniProt exact match
# =====================================================================


class TestProteinResolverUniprotExact:
    """Same UniProt ID in two sources -> single entry."""

    def test_uniprot_exact_match(self):
        resolver = ProteinResolver()
        uniprot_records = [
            {
                "uniprot_id": "P04637",
                "gene_symbol": "TP53",
                "gene_name": "TP53",
                "organism": "Homo sapiens",
            }
        ]
        string_records = [
            {
                "string_id": "9606.ENSP00000269305",
                "gene_symbol": "TP53",
                "organism": "Homo sapiens",
            }
        ]
        resolver.add_uniprot_records(uniprot_records)
        resolver.add_string_records(string_records)

        assert len(resolver.mapping) == 1
        assert "P04637" in resolver.mapping
        entry = resolver.mapping["P04637"]
        assert entry["string_id"] == "9606.ENSP00000269305"


# =====================================================================
# 12. ProteinResolver gene match
# =====================================================================


class TestProteinResolverGeneMatch:
    """Match by gene symbol + organism when UniProt ID is missing."""

    def test_gene_match(self):
        resolver = ProteinResolver()
        uniprot_records = [
            {
                "uniprot_id": "P04637",
                "gene_symbol": "TP53",
                "gene_name": "TP53",
                "organism": "Homo sapiens",
            }
        ]
        # STRING record with no UniProt ID but matching gene symbol
        string_records = [
            {
                "string_id": "9606.ENSP00000269305",
                "gene_symbol": "TP53",
                "organism": "Homo sapiens",
            }
        ]
        resolver.add_uniprot_records(uniprot_records)
        resolver.add_string_records(string_records)

        # The STRING record should be merged into the UniProt entry via
        # gene-name + organism match
        assert "P04637" in resolver.mapping
        entry = resolver.mapping["P04637"]
        assert entry["string_id"] == "9606.ENSP00000269305"
        assert "string" in entry.get("sources", [])


# =====================================================================
# 13. Entity mapping DataFrame output
# =====================================================================


class TestEntityMappingDataframeOutput:
    """Verify to_dataframe() returns correct columns."""

    def test_drug_resolver_to_dataframe_columns(self):
        resolver = DrugResolver()
        resolver.add_source_records(
            [
                {
                    "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                    "name": "Aspirin",
                    "chembl_id": "CHEMBL25",
                }
            ],
            source="chembl",
        )
        df = resolver.to_dataframe()
        # Audit C.17 — output columns expanded to include smiles,
        # smiles_form, molecular_formula, molecular_weight, created_at,
        # and data_quality_score.  Audit 2.7 — ``sources`` is JSON-encoded.
        expected_cols = [
            "canonical_inchikey",
            "canonical_name",
            "chembl_id",
            "drugbank_id",
            "pubchem_cid",
            "uniprot_id",
            "string_id",
            "smiles",
            "smiles_form",
            "molecular_formula",
            "molecular_weight",
            "match_confidence",
            "match_method",
            "sources",
            "resolved_at",
            "created_at",
            "resolver_version",
            "input_checksum",
            "data_quality_score",
        ]
        assert list(df.columns) == expected_cols

    def test_protein_resolver_to_dataframe_columns(self):
        resolver = ProteinResolver()
        resolver.add_uniprot_records(
            [
                {
                    "uniprot_id": "P04637",
                    "gene_symbol": "TP53",
                    "gene_name": "TP53",
                    "organism": "Homo sapiens",
                }
            ]
        )
        df = resolver.to_dataframe()
        # D5-5 / D16-1 / D16-2: sources, resolved_at, resolver_version,
        # input_checksum columns are now included.
        expected_cols = [
            "uniprot_id",
            "canonical_name",
            "gene_symbol",
            "gene_name",
            "organism",
            "string_id",
            "chembl_target_id",
            "match_confidence",
            "match_method",
            "sources",
            "resolved_at",
            "resolver_version",
            "input_checksum",
        ]
        assert list(df.columns) == expected_cols


# =====================================================================
# 14. DrugResolver empty input
# =====================================================================


class TestDrugResolverEmptyInput:
    """Empty DataFrames don't crash."""

    def test_empty_dataframes(self):
        empty_df = pd.DataFrame(columns=["inchikey", "name", "chembl_id"])
        resolver = DrugResolver()
        result_df = resolver.build_mapping(empty_df, empty_df, empty_df)
        assert len(result_df) == 0
        # Verify columns still present
        assert "canonical_inchikey" in result_df.columns

    def test_empty_record_list(self):
        resolver = DrugResolver()
        resolver.add_source_records([], source="chembl")
        assert len(resolver.mapping) == 0


# =====================================================================
# Index builder tests (bonus coverage)
# =====================================================================


class TestIndexBuilders:
    """Tests for build_name_index and build_inchikey_index."""

    def test_build_name_index(self):
        records = [
            {"name": "Aspirin"},
            {"name": "Ibuprofen"},
            {"name": "Aspirin"},  # duplicate
        ]
        index = build_name_index(records)
        # "aspirin" should map to indices [0, 2]
        assert "aspirin" in index
        assert index["aspirin"] == [0, 2]
        assert "ibuprofen" in index

    def test_build_inchikey_index(self):
        records = [
            {"inchikey": "AAA-BBB-C"},
            {"inchikey": "DDD-EEE-F"},
        ]
        index = build_inchikey_index(records)
        assert "AAA-BBB-C" in index
        assert "DDD-EEE-F" in index
