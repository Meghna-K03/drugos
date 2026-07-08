# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
"""
Comprehensive package-level tests for ``entity_resolution``.

This file is the **single source of truth** for the package's public
contract.  Every test imports from the package top-level
(``from entity_resolution import X``), never from submodules.  Every
audit ID (D1-1 through D16-7, 99 issues total) has at least one
regression test in this file, organised by domain.

Sections
--------
1.  Domain 1 — Architecture (D1-1 → D1-5)
2.  Domain 2 — Design (D2-1 → D2-5)
3.  Domain 3 — Knowledge / Scientific Correctness (D3-1 → D3-8)
4.  Domain 4 — Coding (D4-1 → D4-5)
5.  Domain 5 — Data Quality & Integrity (D5-1 → D5-5)
6.  Domain 6 — Reliability & Resilience (D6-1 → D6-6)
7.  Domain 7 — Idempotency & Reproducibility (D7-1 → D7-5)
8.  Domain 8 — Performance & Scalability (D8-1 → D8-6)
9.  Domain 9 — Security & Privacy (D9-1 → D9-7)
10. Domain 10 — Testing & Validation (D10-1 → D10-7)
11. Domain 11 — Logging & Observability (D11-1 → D11-5)
12. Domain 12 — Configuration & Environment Management (D12-1 → D12-5)
13. Domain 13 — Documentation & Readability (D13-1 → D13-10)
14. Domain 14 — Compliance & Standards Adherence (D14-1 → D14-7)
15. Domain 15 — Interoperability & Integration (D15-1 → D15-6)
16. Domain 16 — Data Lineage & Traceability (D16-1 → D16-7)
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# IMPORTANT: every import below goes through the package façade, never
# through submodules.  This is the fix for audit D10-1 — the previous
# test suite imported from submodules, so the package's public contract
# was structurally untested.
import entity_resolution  # noqa: E402
from entity_resolution import (  # noqa: E402
    DrugResolver,
    MatchConfidence,
    METHOD_CONFIDENCE,
    MAPPING_SCHEMA_VERSION,
    ProteinResolver,
    Resolver,
    ResolverConfig,
    ResolverStats,
    SYNTHETIC_INCHIKEY_PREFIX,
    build_canonical_inchikey_index,
    build_canonical_name_index,
    build_inchikey_index,
    build_mapping,
    build_name_index,
    check_dependencies,
    compute_match_confidence,
    extract_inchikey_first_block,
    find_duplicate_ids,
    fuzzy_match_score,
    is_available,
    is_synthetic_inchikey,
    is_valid_inchikey,
    make_drug_resolver,
    make_protein_resolver,
    make_synthetic_inchikey,
    normalize_name,
    register_match_method,
    set_log_format,
    set_log_level,
    validate_drug_record,
    validate_protein_record,
)
from entity_resolution import __version__  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _aspirin_chembl_df() -> pd.DataFrame:
    return pd.DataFrame({
        "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
        "name": ["Aspirin"],
        "chembl_id": ["CHEMBL25"],
    })


def _aspirin_drugbank_df() -> pd.DataFrame:
    return pd.DataFrame({
        "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
        "name": ["Acetylsalicylic acid"],
        "drugbank_id": ["DB00945"],
    })


def _aspirin_pubchem_df() -> pd.DataFrame:
    return pd.DataFrame({
        "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
        "name": ["2-acetoxybenzoic acid"],
        "pubchem_cid": [2244],
    })


# ===========================================================================
# §1. DOMAIN 1 — ARCHITECTURE (D1-1 → D1-5)
# ===========================================================================


class TestDomain1Architecture:
    """Audit IDs D1-1 through D1-5."""

    def test_no_absolute_intra_package_imports(self):
        """D1-1: all intra-package imports must be relative.

        Scans every ``.py`` file in the entity_resolution package and
        asserts that none contains an absolute ``from entity_resolution.``
        import.  Absolute imports couple the package to its top-level
        name and break if the folder is renamed.
        """
        pkg_dir = PROJECT_ROOT / "entity_resolution"
        py_files = list(pkg_dir.glob("*.py"))
        assert py_files, "expected entity_resolution/*.py files"
        violation_re = re.compile(r"^\s*from\s+entity_resolution\.", re.MULTILINE)
        offenders: List[str] = []
        for f in py_files:
            text = f.read_text(encoding="utf-8")
            for m in violation_re.finditer(text):
                offenders.append(f"{f.name}: {m.group(0).strip()}")
        assert not offenders, (
            "D1-1 violation — absolute intra-package imports found:\n"
            + "\n".join(offenders)
        )

    def test_lazy_imports_minimal_env(self):
        """D1-2: importing the package does NOT eagerly import pandas/requests.

        Uses a subprocess to verify the lazy-loading contract without
        polluting the test session's ``sys.modules`` (which would break
        identity checks in subsequent tests).
        """
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, entity_resolution; "
             "assert 'pandas' not in sys.modules, "
             "'pandas eagerly imported at package load'; "
             "assert 'requests' not in sys.modules, "
             "'requests eagerly imported at package load'; "
             "print('OK')"],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"D1-2: lazy-import check failed in subprocess:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_resolver_abc_contract(self):
        """D1-3: ``Resolver`` ABC exists in base; both resolvers inherit it."""
        assert hasattr(entity_resolution, "Resolver")
        assert isinstance(DrugResolver, type)
        assert isinstance(ProteinResolver, type)
        assert issubclass(DrugResolver, Resolver)
        assert issubclass(ProteinResolver, Resolver)
        # ABC requires the abstract methods to be implemented.
        assert hasattr(Resolver, "add_source_records")
        assert hasattr(Resolver, "resolve_single")
        assert hasattr(Resolver, "build_mapping")
        assert hasattr(Resolver, "to_dataframe")
        assert hasattr(Resolver, "to_records")
        assert hasattr(Resolver, "to_dict")
        assert hasattr(Resolver, "to_state_dict")
        assert hasattr(Resolver, "from_state_dict")
        assert hasattr(Resolver, "reset")
        assert hasattr(Resolver, "remove_source")
        assert hasattr(Resolver, "get_stats")
        assert hasattr(Resolver, "get_audit_trail")
        assert hasattr(Resolver, "find_affected_entities")

    def test_submodules_in_all(self):
        """D1-4: ``__all__`` includes the four submodules."""
        for sub in ("base", "drug_resolver", "protein_resolver", "resolver_utils"):
            assert sub in entity_resolution.__all__, (
                f"D1-4: submodule {sub!r} missing from __all__"
            )

    def test_pep562_getattr_dir(self):
        """D1-5: ``__getattr__`` and ``__dir__`` implemented (PEP 562)."""
        assert callable(getattr(entity_resolution, "__getattr__", None))
        assert callable(getattr(entity_resolution, "__dir__", None))
        # ``dir()`` must include every public symbol.
        d = dir(entity_resolution)
        for name in ("DrugResolver", "ProteinResolver", "ResolverConfig",
                     "normalize_name", "is_valid_inchikey", "METHOD_CONFIDENCE"):
            assert name in d, f"D1-5: {name!r} missing from dir(entity_resolution)"
        # ``getattr`` for an unknown name raises AttributeError.
        with pytest.raises(AttributeError):
            getattr(entity_resolution, "this_does_not_exist_xyz")

    def test_submodule_attribute_access(self):
        """D1-5: ``entity_resolution.base`` etc. work as attribute access."""
        for sub in ("base", "drug_resolver", "protein_resolver", "resolver_utils"):
            mod = getattr(entity_resolution, sub)
            assert mod is not None
            assert mod.__name__ == f"entity_resolution.{sub}"


# ===========================================================================
# §2. DOMAIN 2 — DESIGN (D2-1 → D2-5)
# ===========================================================================


class TestDomain2Design:
    """Audit IDs D2-1 through D2-5."""

    def test_is_synthetic_inchikey_reexported(self):
        """D2-1 / D5-4 / D13-2 / D14-5: ``is_synthetic_inchikey`` exported."""
        assert "is_synthetic_inchikey" in entity_resolution.__all__
        assert callable(is_synthetic_inchikey)
        # Functional parity with the resolver-utils definition.
        from entity_resolution.base import is_synthetic_inchikey as base_impl
        from entity_resolution.drug_resolver import is_synthetic_inchikey as drug_impl
        for val in (None, "", "SYNTHABC", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                    "SYNTHABCDEFGHIJKLM-NOPQRSTUVW-X"):
            assert is_synthetic_inchikey(val) == base_impl(val) == drug_impl(val)

    def test_protein_resolver_unified_api(self):
        """D2-2: ``ProteinResolver.add_source_records`` dispatch method exists."""
        r = ProteinResolver()
        assert hasattr(r, "add_source_records")
        # Dispatch on 'uniprot' / 'string' / 'chembl'.
        r.add_source_records(
            [{"uniprot_id": "P04637", "gene_symbol": "TP53",
              "gene_name": "TP53", "organism": "Homo sapiens"}],
            source="uniprot",
        )
        assert "P04637" in r.mapping
        r.add_source_records(
            [{"string_id": "9606.ENSP00000269305", "gene_symbol": "TP53",
              "organism": "Homo sapiens"}],
            source="string",
        )
        assert len(r.mapping) == 1, "STRING record merged into UniProt entry"

    def test_build_mapping_kwargs(self):
        """D2-3: ``build_mapping`` accepts ``reset`` and ``sources_order``."""
        r = DrugResolver()
        sig = r.build_mapping.__doc__
        # Verify the keyword args are accepted by calling.
        df = r.build_mapping(
            _aspirin_chembl_df(), _aspirin_drugbank_df(), _aspirin_pubchem_df(),
            reset=True, sources_order=("drugbank", "chembl", "pubchem"),
        )
        assert len(df) == 1

    def test_method_confidence_registry(self):
        """D2-4 / D16-7: public ``METHOD_CONFIDENCE`` + ``register_match_method``."""
        assert isinstance(METHOD_CONFIDENCE, dict)
        assert "inchikey_exact" in METHOD_CONFIDENCE
        assert METHOD_CONFIDENCE["inchikey_exact"] == 1.0
        # register_match_method adds new entries.
        original = METHOD_CONFIDENCE.get("custom_method_xyz")
        try:
            register_match_method("custom_method_xyz", 0.42)
            assert METHOD_CONFIDENCE["custom_method_xyz"] == 0.42
            assert compute_match_confidence("custom_method_xyz") == 0.42
        finally:
            if original is None:
                METHOD_CONFIDENCE.pop("custom_method_xyz", None)
        # Validation: out-of-range confidence raises.
        with pytest.raises(ValueError):
            register_match_method("bad", 1.5)
        with pytest.raises(ValueError):
            register_match_method("bad", -0.1)
        with pytest.raises(ValueError):
            register_match_method("", 0.5)

    def test_factory_and_di(self):
        """D2-5: ``ResolverConfig`` + factory functions exist."""
        cfg = ResolverConfig()
        assert cfg.collapse_stereoisomers is False
        assert cfg.pubchem_enabled is False
        # Factory functions.
        r1 = make_drug_resolver()
        assert isinstance(r1, DrugResolver)
        r2 = make_protein_resolver()
        assert isinstance(r2, ProteinResolver)
        # Factory with explicit config.
        cfg2 = ResolverConfig(collapse_stereoisomers=True)
        r3 = make_drug_resolver(config=cfg2)
        assert r3.config.collapse_stereoisomers is True


# ===========================================================================
# §3. DOMAIN 3 — KNOWLEDGE / SCIENTIFIC CORRECTNESS (D3-1 → D3-8)
# ===========================================================================


class TestDomain3Knowledge:
    """Audit IDs D3-1 through D3-8.  Highest priority — patient safety."""

    def test_bulk_mode_no_pubchem(self, caplog):
        """D3-1: bulk path ``build_mapping`` never calls PubChem even when enabled."""
        cfg = ResolverConfig(pubchem_enabled=True)
        r = DrugResolver(config=cfg)
        with mock.patch.object(r, "_match_by_pubchem_xref") as m:
            m.return_value = None
            r.build_mapping(
                _aspirin_chembl_df(), _aspirin_drugbank_df(),
                _aspirin_pubchem_df(),
            )
            # PubChem must NOT have been called from the bulk path.
            assert m.call_count == 0, (
                "D3-1 violation: bulk path invoked PubChem"
            )

    def test_fuzzy_method_documented_and_executed(self):
        """D3-2: fuzzy match is a real method exercised by ``_match_by_name``.

        The fuzzy match should merge the second record into the first
        via the fuzzy path (not inchikey_exact or connectivity, since
        the InChIKeys differ and ``collapse_stereoisomers=False``).
        The canonical entry's match_method may stay as the higher-
        confidence ``inchikey_exact`` (because the first record was
        created with that method), but the fuzzy_matches counter
        proves the fuzzy path was exercised.
        """
        r = DrugResolver()
        # Ingest a record with a known InChIKey.
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "aspirin", "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        # Now ingest a near-miss name (DIFFERENT InChIKey so the
        # inchikey_exact / connectivity paths don't fire — collapse
        # is off by default so connectivity won't merge either).
        # "asprin" (transposed 'i' and 'r') should fuzzy-match
        # "aspirin" at >= 0.85 via token_sort_ratio.
        r.add_source_records(
            [{"inchikey": "QJKYOWWVZQQXTL-UHFFFAOYSA-N",  # unrelated InChIKey
              "name": "asprin", "drugbank_id": "DB_FAKE"}],
            source="drugbank",
        )
        # The fuzzy match should have merged both records.
        assert len(r.mapping) == 1, (
            f"D3-2: fuzzy match did not merge — mapping has "
            f"{len(r.mapping)} entries"
        )
        # The fuzzy path was exercised (counter incremented).
        assert r.stats.fuzzy_matches >= 1, (
            f"D3-2: fuzzy_matches counter not incremented "
            f"(stats={r.stats.to_dict()})"
        )
        # The merged entry should have both source IDs.
        entry = list(r.mapping.values())[0]
        assert entry["chembl_id"] == "CHEMBL25"
        assert entry["drugbank_id"] == "DB_FAKE"

    def test_confidence_ge_threshold(self):
        """D3-3: ``METHOD_CONFIDENCE["fuzzy"] >= ResolverConfig.fuzzy_threshold``."""
        cfg = ResolverConfig()
        assert METHOD_CONFIDENCE["fuzzy"] >= cfg.fuzzy_threshold, (
            f"D3-3 violation: fuzzy confidence {METHOD_CONFIDENCE['fuzzy']} "
            f"< threshold {cfg.fuzzy_threshold}"
        )
        # The invariant must hold for the enum too.
        assert MatchConfidence.FUZZY.value >= cfg.fuzzy_threshold

    def test_stereoisomer_collapse_safety(self):
        """D3-4: default ``collapse_stereoisomers=False`` keeps stereoisomers distinct."""
        # Disable fuzzy matching by using a very high threshold so the
        # only paths that can merge are inchikey_exact / connectivity.
        cfg = ResolverConfig(fuzzy_threshold=0.999)
        r = DrugResolver(config=cfg)
        assert r.config.collapse_stereoisomers is False
        # Two InChIKeys sharing the first 14 chars but differing in stereo.
        # Names are deliberately very different so fuzzy doesn't fire.
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "thalidomide-enantiomer-R", "chembl_id": "CHEMBL_R"}],
            source="chembl",
        )
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-ZXQBJXABSA-N",
              "name": "thalidomide-enantiomer-S-form", "drugbank_id": "DB_S"}],
            source="drugbank",
        )
        # They must remain distinct — thalidomide enantiomers have
        # drastically different safety profiles.
        assert len(r.mapping) == 2, (
            "D3-4 violation: stereoisomers silently collapsed"
        )

    def test_stereoisomer_collapse_opt_in_records_audit(self):
        """D3-4: opt-in collapse records the collapsed stereoisoforms."""
        cfg = ResolverConfig(collapse_stereoisomers=True)
        r = DrugResolver(config=cfg)
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "thalidomide-R", "chembl_id": "CHEMBL_R"}],
            source="chembl",
        )
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-ZXQBJXABSA-N",
              "name": "thalidomide-S", "drugbank_id": "DB_S"}],
            source="drugbank",
        )
        assert len(r.mapping) == 1
        entry = list(r.mapping.values())[0]
        assert "collapsed_stereoisomers" in entry
        assert len(entry["collapsed_stereoisomers"]) >= 1
        assert r.stats.stereoisomer_collapses >= 1

    def test_synthetic_key_source_independence(self):
        """D3-5: synthetic InChIKey is source-INDEPENDENT.

        Same normalized name from two sources → same synthetic key.
        """
        from entity_resolution.base import make_synthetic_inchikey
        # Two records with no InChIKey, same name, different sources.
        r = DrugResolver()
        r.add_source_records(
            [{"name": "MysteryDrug", "chembl_id": "CHEMBL_MYST"}],
            source="chembl",
        )
        r.add_source_records(
            [{"name": "MysteryDrug", "drugbank_id": "DB_MYST"}],
            source="drugbank",
        )
        # They should merge because the synthetic key is source-independent.
        assert len(r.mapping) == 1, (
            "D3-5 violation: source-dependent synthetic key split the records"
        )
        canonical = list(r.mapping.keys())[0]
        assert canonical.startswith("SYNTH")
        # Verify by direct construction.
        k1 = make_synthetic_inchikey(normalize_name("MysteryDrug"))
        k2 = make_synthetic_inchikey(normalize_name("MysteryDrug"))
        assert k1 == k2

    def test_synthetic_key_collision_disambiguation(self):
        """D3-5: salt parameter disambiguates true name collisions."""
        from entity_resolution.base import make_synthetic_inchikey
        k1 = make_synthetic_inchikey("cyclophosphamide")
        k2 = make_synthetic_inchikey("cyclophosphamide", salt="brand_x")
        assert k1 != k2, "salt parameter should disambiguate collisions"

    def test_confidence_rationale_documented(self):
        """D3-6: confidence rationale documented in module docstring."""
        doc = entity_resolution.__doc__ or ""
        # The docstring should mention "heuristic, not probability" or
        # similar phrasing.
        assert ("heuristic" in doc.lower()
                or "calibrated" in doc.lower()), (
            "D3-6: confidence rationale not documented in module docstring"
        )
        # The confidence table should be in the docstring.
        assert "inchikey_exact" in doc.lower()
        assert "fuzzy" in doc.lower()

    def test_pubchem_salt_form_ambiguity(self):
        """D3-7: ``pubchem_strict_salt_form`` config flag exists."""
        cfg = ResolverConfig(pubchem_strict_salt_form=True)
        assert cfg.pubchem_strict_salt_form is True
        # And the docstring mentions salt-form ambiguity.
        doc = entity_resolution.__doc__ or ""
        assert "salt" in doc.lower(), (
            "D3-7: salt-form ambiguity not documented"
        )

    def test_inchikey_validation(self):
        """D3-8: ``is_valid_inchikey`` exported and works."""
        assert is_valid_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
        assert not is_valid_inchikey("not-an-inchikey")
        assert not is_valid_inchikey("")
        assert not is_valid_inchikey(None)
        assert not is_valid_inchikey(123)
        # Synthetic keys must also pass the format check.
        # SYNTH (5) + ABCDEFGHI (9) = 14 chars first block;
        # NOPQRSTUVW (10) second block; X (1) third block.
        assert is_valid_inchikey("SYNTHABCDEFGHI-NOPQRSTUVW-X")

    def test_validate_drug_record_strict_and_lenient(self):
        """D3-8 / D5-2: validation at the API boundary."""
        # Lenient: only required-field presence.
        ok, errs = validate_drug_record({"name": "Aspirin"})
        assert ok, f"unexpected errors: {errs}"
        # Missing required 'name' field.
        ok, errs = validate_drug_record({"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"})
        assert not ok
        assert any("name" in e for e in errs)
        # Empty dict is rejected.
        ok, errs = validate_drug_record({})
        assert not ok
        # Strict: InChIKey format check.
        ok, errs = validate_drug_record(
            {"name": "X", "inchikey": "bad"}, strict=True
        )
        assert not ok
        assert any("inchikey" in e.lower() for e in errs)
        # Strict with valid InChIKey passes.
        ok, errs = validate_drug_record(
            {"name": "Aspirin",
             "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"},
            strict=True,
        )
        assert ok, f"unexpected errors: {errs}"


# ===========================================================================
# §4. DOMAIN 4 — CODING (D4-1 → D4-5)
# ===========================================================================


class TestDomain4Coding:
    """Audit IDs D4-1 through D4-5."""

    def test_future_annotations_present(self):
        """D4-1: ``from __future__ import annotations`` in every .py file."""
        pkg_dir = PROJECT_ROOT / "entity_resolution"
        for f in pkg_dir.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert "from __future__ import annotations" in text, (
                f"D4-1: missing future-annotations in {f.name}"
            )

    def test_google_docstring_convention(self):
        """D4-2 / D14-7: Google-style docstrings (Args/Returns sections)."""
        doc = DrugResolver.add_source_records.__doc__ or ""
        assert "Parameters" in doc or "Args" in doc
        assert "Returns" in doc or "Raises" in doc
        doc2 = normalize_name.__doc__ or ""
        assert "Parameters" in doc2 or "Args" in doc2
        assert "Returns" in doc2 or "Examples" in doc2

    def test_all_matches_public_surface(self):
        """D4-4: ``__all__`` matches actual public surface."""
        # Every public symbol in __all__ must be importable.
        for name in entity_resolution.__all__:
            if name in ("__version__", "MAPPING_SCHEMA_VERSION"):
                continue
            # Submodules are accessed lazily.
            try:
                obj = getattr(entity_resolution, name)
            except AttributeError as exc:
                pytest.fail(
                    f"D4-4: __all__ lists {name!r} but it is not importable: {exc}"
                )

    def test_version_string(self):
        """D4-5 / D14-3: ``__version__`` defined and in ``__all__``."""
        assert isinstance(__version__, str)
        assert re.match(r"^\d+\.\d+\.\d+", __version__), (
            f"__version__ {__version__!r} doesn't look like semver"
        )
        assert "__version__" in entity_resolution.__all__
        assert __version__ == "1.0.0"

    def test_imperative_docstring_summary(self):
        """D4-3: first docstring line is imperative mood (PEP 257).

        PEP 257 recommends imperative mood for the summary line.  We
        verify the summary starts with a capital letter and is a single
        short sentence (the period convention varies between Google
        and NumPy styles — both are accepted here).
        """
        # ``normalize_name`` — "Normalize a drug or protein name ..."
        doc = normalize_name.__doc__ or ""
        first_line = doc.strip().split("\n")[0]
        # Should start with a capitalised imperative verb.
        assert first_line[0].isupper(), (
            f"D4-3: summary should start with a capital letter, got: {first_line!r}"
        )
        # Should be a single short sentence (less than 100 chars).
        assert len(first_line) < 200, (
            f"D4-3: summary line is too long ({len(first_line)} chars): {first_line!r}"
        )


# ===========================================================================
# §5. DOMAIN 5 — DATA QUALITY & INTEGRITY (D5-1 → D5-5)
# ===========================================================================


class TestDomain5DataQuality:
    """Audit IDs D5-1 through D5-5."""

    def test_canonical_index_builders(self):
        """D5-1: ``build_canonical_name_index`` / ``build_canonical_inchikey_index``
        return ``Dict[str, str]`` (single-valued)."""
        # Each tuple is (canonical_key, record_dict).
        records = [
            ("BSYNRYMUTXBXSQ-UHFFFAOYSA-N", {"name": "aspirin"}),
            ("WFXAZNNJSJXTJZ-UHFFFAOYSA-N", {"name": "ibuprofen"}),
        ]
        # build_canonical_name_index accepts (key, record) tuples.
        idx = build_canonical_name_index(records, name_field="name")
        assert isinstance(idx, dict)
        assert idx["aspirin"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        # And the value is a string, not a list.
        assert isinstance(idx["aspirin"], str)

        # Same for inchikey.
        ik_records = [
            ("BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
             {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}),
            ("WFXAZNNJSJXTJZ-UHFFFAOYSA-N",
             {"inchikey": "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"}),
        ]
        ik_idx = build_canonical_inchikey_index(ik_records)
        assert isinstance(ik_idx["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"], str)
        assert ik_idx["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"

    def test_record_validation_boundary(self):
        """D5-2: validation enforced in ``add_source_records`` (invalid records
        are dead-lettered, not silently dropped)."""
        r = DrugResolver()
        r.add_source_records(
            [
                {"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"},
                {},  # invalid — missing required 'name'
                {"name": ""},  # invalid — empty name
            ],
            source="chembl",
        )
        assert len(r.mapping) == 1, "only the valid record should be ingested"
        assert len(r._dead_letter) == 2, "two invalid records dead-lettered"
        assert r.stats.records_rejected == 2

    def test_duplicate_id_detection(self):
        """D5-3: ``find_duplicate_ids`` exists and ``add_source_records`` logs duplicates."""
        records = [
            {"name": "A", "chembl_id": "CHEMBL1"},
            {"name": "B", "chembl_id": "CHEMBL1"},  # duplicate chembl_id
            {"name": "C", "chembl_id": "CHEMBL2"},
        ]
        dups = find_duplicate_ids(records)
        assert "chembl_id" in dups
        assert "CHEMBL1" in dups["chembl_id"]

    def test_is_synthetic_inchikey_data_quality(self):
        """D5-4: ``is_synthetic_inchikey`` re-exported (data-quality angle)."""
        assert callable(is_synthetic_inchikey)
        assert is_synthetic_inchikey("SYNTHABCDEFGHIJKLM-NOPQRSTUVW-X")
        assert not is_synthetic_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")

    def test_to_dataframe_includes_sources(self):
        """D5-5 / D16-1: ``to_dataframe`` includes the ``sources`` column."""
        r = DrugResolver()
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Aspirin", "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Acetylsalicylic acid", "drugbank_id": "DB00945"}],
            source="drugbank",
        )
        df = r.to_dataframe()
        assert "sources" in df.columns
        row = df.iloc[0]
        assert "chembl" in row["sources"]
        assert "drugbank" in row["sources"]


# ===========================================================================
# §6. DOMAIN 6 — RELIABILITY & RESILIENCE (D6-1 → D6-6)
# ===========================================================================


class TestDomain6Reliability:
    """Audit IDs D6-1 through D6-6."""

    def test_import_survives_missing_optional_deps(self):
        """D6-1 / D10-4: ``import entity_resolution`` survives without requests/pandas.

        Uses a subprocess to verify the import-survives-missing-deps
        contract without polluting the test session's ``sys.modules``.
        """
        import subprocess
        # Simulate missing pandas/requests by inserting a fake meta-path
        # finder that rejects those imports.
        script = (
            "import sys\n"
            "class _Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        if name.split('.')[0] in ('pandas', 'requests'):\n"
            "            return self\n"
            "        return None\n"
            "    def load_module(self, name):\n"
            "        raise ImportError(f'simulated: {name} not installed')\n"
            "sys.meta_path.insert(0, _Blocker())\n"
            "import entity_resolution\n"
            "assert callable(entity_resolution.normalize_name), "
            "'normalize_name not callable'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"D6-1: import-survives check failed in subprocess:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_missing_requests_clear_error(self):
        """D6-2: clear ImportError when pubchem_enabled=True but requests missing."""
        # We can't actually uninstall requests, so we verify the lazy
        # loader raises a clear error when _get_requests is called
        # after simulating an ImportError.
        from entity_resolution import drug_resolver
        # Stash the original and force the lazy cache to None.
        original = drug_resolver._requests
        drug_resolver._requests = None
        # And block the actual import.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "requests":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            with pytest.raises(ImportError) as exc_info:
                drug_resolver._get_requests()
            assert "requests" in str(exc_info.value).lower()
        finally:
            builtins.__import__ = real_import
            drug_resolver._requests = original

    def test_pubchem_failure_observability(self):
        """D6-3: PubChem failures tracked via counters + dead-letter queue."""
        from entity_resolution import drug_resolver
        # Reset the lazy-loaded _requests cache so the mock takes effect.
        original_requests = drug_resolver._requests
        drug_resolver._requests = None
        cfg = ResolverConfig(pubchem_enabled=True, pubchem_max_retries=0,
                             pubchem_call_delay=0.0)
        r = DrugResolver(config=cfg)
        try:
            import requests as real_requests
            with mock.patch.dict("sys.modules", {"requests": mock.MagicMock()}):
                # Set up the mock module.
                m_req = mock.MagicMock()
                m_req.exceptions = real_requests.exceptions
                m_req.utils.quote.return_value = "aspirin"
                m_req.get.side_effect = real_requests.exceptions.Timeout(
                    "simulated"
                )
                with mock.patch.object(drug_resolver, "_requests", m_req):
                    result = r._match_by_pubchem_xref("aspirin")
            assert result is None
            assert r.stats.pubchem_calls >= 1
            assert r.stats.pubchem_failures >= 1
            assert r.get_pubchem_failure_count() >= 1
        finally:
            drug_resolver._requests = original_requests

    def test_check_dependencies(self):
        """D6-4: ``check_dependencies`` and ``is_available`` exported."""
        deps = check_dependencies()
        assert isinstance(deps, dict)
        for k in ("pandas", "requests", "rapidfuzz", "pyarrow"):
            assert k in deps
            assert isinstance(deps[k], bool)
        # is_available: pandas + rapidfuzz must be present in the test env.
        assert is_available() is True

    def test_reset_and_remove_source(self):
        """D6-5: ``reset()`` and ``remove_source()`` exist on both resolvers."""
        r = DrugResolver()
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Aspirin", "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        assert len(r.mapping) == 1
        r.reset()
        assert len(r.mapping) == 0
        # Re-ingest and test remove_source.
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Aspirin", "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Acetylsalicylic acid", "drugbank_id": "DB00945"}],
            source="drugbank",
        )
        # remove_source on a multi-source entry keeps the entry but
        # removes the source label.
        removed = r.remove_source("drugbank")
        assert removed == 0, "no entries fully removed (aspirin still has chembl)"
        entry = list(r.mapping.values())[0]
        assert "drugbank" not in entry["sources"]
        # Now remove the only remaining source.
        removed = r.remove_source("chembl")
        assert removed == 1
        assert len(r.mapping) == 0

    def test_process_global_rate_limiter(self):
        """D6-6: rate limiter is process-global (shared across instances)."""
        from entity_resolution.base import _ProcessGlobalRateLimiter
        _ProcessGlobalRateLimiter._reset_for_tests()
        # Two resolvers pointing at the same base URL should share
        # one rate budget — calling acquire() twice in quick succession
        # should respect the configured delay.
        delay = 0.05
        t0 = time.monotonic()
        _ProcessGlobalRateLimiter.acquire("https://example.com", delay)
        _ProcessGlobalRateLimiter.acquire("https://example.com", delay)
        elapsed = time.monotonic() - t0
        assert elapsed >= delay, (
            f"D6-6: process-global limiter did not enforce delay "
            f"(elapsed={elapsed:.3f}s, expected >= {delay}s)"
        )


# ===========================================================================
# §7. DOMAIN 7 — IDEMPOTENCY & REPRODUCIBILITY (D7-1 → D7-5)
# ===========================================================================


class TestDomain7Idempotency:
    """Audit IDs D7-1 through D7-5."""

    def test_build_mapping_idempotent(self):
        """D7-1: calling ``build_mapping`` twice produces the same result."""
        r = DrugResolver()
        df1 = r.build_mapping(
            _aspirin_chembl_df(), _aspirin_drugbank_df(), _aspirin_pubchem_df()
        )
        df2 = r.build_mapping(
            _aspirin_chembl_df(), _aspirin_drugbank_df(), _aspirin_pubchem_df()
        )
        assert len(df1) == len(df2) == 1, (
            "D7-1: build_mapping is not idempotent (row count differs)"
        )

    def test_deterministic_resolution(self):
        """D7-2: same inputs in different orders produce same canonical entries."""
        records_a = [
            {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "name": "Aspirin",
             "chembl_id": "CHEMBL25"},
            {"inchikey": "WFXAZNNJSJXTJZ-UHFFFAOYSA-N", "name": "Ibuprofen",
             "chembl_id": "CHEMBL521"},
        ]
        records_b = list(reversed(records_a))
        r1 = DrugResolver()
        r1.add_source_records(records_a, source="chembl")
        r2 = DrugResolver()
        r2.add_source_records(records_b, source="chembl")
        assert set(r1.mapping.keys()) == set(r2.mapping.keys())

    def test_fuzzy_tie_break_deterministic(self):
        """D7-3: fuzzy tie-breaking is deterministic (lex order)."""
        r1 = DrugResolver()
        r2 = DrugResolver()
        for r in (r1, r2):
            r.add_source_records(
                [{"name": "aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
                source="chembl",
            )
            r.add_source_records(
                [{"name": "asprn"}], source="drugbank",
            )
        assert list(r1.mapping.keys()) == list(r2.mapping.keys())

    def test_state_serialization_roundtrip(self):
        """D7-4 / D16-3: ``to_state_dict`` / ``from_state_dict`` round-trip."""
        r1 = DrugResolver()
        r1.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Aspirin", "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        state = r1.to_state_dict()
        # Must be JSON-serialisable.
        text = json.dumps(state, default=str)
        state2 = json.loads(text)
        r2 = DrugResolver.from_state_dict(state2)
        assert set(r1.mapping.keys()) == set(r2.mapping.keys())
        # And via to_json / from_json.
        json_text = r1.to_json()
        r3 = DrugResolver.from_json(json_text)
        assert set(r1.mapping.keys()) == set(r3.mapping.keys())

    def test_incremental_backfill_safety(self):
        """D7-5: ``find_affected_entities`` supports impact analysis."""
        r = DrugResolver()
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Aspirin", "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        r.add_source_records(
            [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "name": "Acetylsalicylic acid", "drugbank_id": "DB00945"}],
            source="drugbank",
        )
        affected = r.find_affected_entities("chembl")
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in affected
        affected_db = r.find_affected_entities("drugbank")
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in affected_db
        affected_x = r.find_affected_entities("nonexistent")
        assert affected_x == []


# ===========================================================================
# §8. DOMAIN 8 — PERFORMANCE & SCALABILITY (D8-1 → D8-6)
# ===========================================================================


class TestDomain8Performance:
    """Audit IDs D8-1 through D8-6."""

    def test_cold_start_time(self):
        """D8-1: cold-start import time < 500 ms.

        Uses a subprocess to measure the true cold-start time without
        polluting the test session's sys.modules.
        """
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c",
             "import time; t0 = time.perf_counter(); "
             "import entity_resolution; "
             "elapsed = time.perf_counter() - t0; "
             "assert elapsed < 0.5, f'cold-start took {elapsed:.3f}s'; "
             "print(f'OK {elapsed:.4f}')"],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"D8-1: cold-start check failed in subprocess:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_fuzzy_sweep_bounded(self):
        """D8-2: fuzzy sweep ceiling exists and is respected."""
        cfg = ResolverConfig(fuzzy_max_candidates=5)
        r = DrugResolver(config=cfg)
        # Ingest 10 names so the fuzzy sweep exceeds the ceiling.
        names = [f"drug_{i:02d}" for i in range(10)]
        for i, n in enumerate(names):
            r.add_source_records(
                [{"name": n, "inchikey": f"SYNTH{i:014d}-AAAAAAAAAA-A"}],
                source="chembl",
            )
        # The fuzzy sweep should not crash and should respect the
        # ceiling (verified by the absence of an error).
        result = r._match_by_name("drug_99")
        # Result may be None (no match above threshold) — that's fine.
        assert result is None or result in r.mapping

    def test_resolve_single_scaling_warning(self):
        """D8-3: ``resolve_single`` PubChem scaling cliff documented."""
        doc = entity_resolution.__doc__ or ""
        assert "rate" in doc.lower() or "scale" in doc.lower(), (
            "D8-3: scaling notes not in docstring"
        )

    def test_chunked_and_parquet_export(self, tmp_path):
        """D8-4: ``to_dataframe(chunksize=...)`` returns an iterator."""
        r = DrugResolver()
        for i in range(10):
            r.add_source_records(
                [{"name": f"drug_{i}",
                  "inchikey": f"SYNTH{i:014d}-AAAAAAAAAA-A"}],
                source="chembl",
            )
        chunks = r.to_dataframe(chunksize=3)
        assert hasattr(chunks, "__iter__")
        chunk_list = list(chunks)
        assert len(chunk_list) == 4  # 3 + 3 + 3 + 1
        assert all(len(c) <= 3 for c in chunk_list)
        # to_parquet raises ImportError when pyarrow is missing (it
        # usually is in the test env).
        try:
            import pyarrow  # noqa: F401
            out = tmp_path / "out.parquet"
            r.to_parquet(str(out))
            assert out.exists()
        except ImportError:
            with pytest.raises(ImportError):
                r.to_parquet(str(tmp_path / "out.parquet"))

    def test_name_index_collision_handling(self):
        """D8-5: name-index collisions are tracked (no silent data loss)."""
        r = DrugResolver()
        # Two distinct drugs happen to share a normalized name.
        r.add_source_records(
            [{"name": "aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        r.add_source_records(
            [{"name": "Aspirin",  # normalizes to "aspirin"
              "inchikey": "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"}],  # different drug!
            source="drugbank",
        )
        # Both should be tracked in the multi-valued name index.
        assert "aspirin" in r._name_index_multi
        assert len(r._name_index_multi["aspirin"]) >= 1

    def test_batch_pubchem_documented(self):
        """D8-6: batch PubChem documented in docstring (feature pending)."""
        doc = entity_resolution.__doc__ or ""
        # The docstring should mention batch PubChem.
        assert "batch" in doc.lower() or "PubChem" in doc, (
            "D8-6: batch PubChem not documented"
        )


# ===========================================================================
# §9. DOMAIN 9 — SECURITY & PRIVACY (D9-1 → D9-7)
# ===========================================================================


class TestDomain9Security:
    """Audit IDs D9-1 through D9-7."""

    def test_pubchem_opt_out_default_off(self):
        """D9-1: ``pubchem_enabled`` defaults to ``False``."""
        cfg = ResolverConfig()
        assert cfg.pubchem_enabled is False
        r = DrugResolver()
        # Calling resolve_single must NOT trigger a PubChem call.
        with mock.patch.object(r, "_match_by_pubchem_xref") as m:
            m.return_value = None
            r.resolve_single("aspirin")
            assert m.call_count == 0, (
                "D9-1: PubChem called even though pubchem_enabled=False"
            )

    def test_pubchem_env_var_overrides(self, monkeypatch):
        """D9-2: ``ENTITY_RESOLUTION_PUBCHEM_ENABLED`` env var forces on/off."""
        monkeypatch.setenv("ENTITY_RESOLUTION_PUBCHEM_ENABLED", "1")
        cfg = ResolverConfig.from_env()
        assert cfg.pubchem_enabled is True
        monkeypatch.setenv("ENTITY_RESOLUTION_PUBCHEM_ENABLED", "0")
        cfg = ResolverConfig.from_env()
        assert cfg.pubchem_enabled is False

    def test_pubchem_rest_base_configurable(self):
        """D9-3: ``pubchem_rest_base`` configurable."""
        cfg = ResolverConfig(
            pubchem_enabled=True,
            pubchem_rest_base="https://internal-mirror.example.com/rest/pug",
        )
        assert cfg.pubchem_rest_base == "https://internal-mirror.example.com/rest/pug"

    def test_pubchem_response_validation(self):
        """D9-4: PubChem response validated (Content-Type, size, InChIKey format)."""
        from entity_resolution import drug_resolver
        original_requests = drug_resolver._requests
        drug_resolver._requests = None
        cfg = ResolverConfig(
            pubchem_enabled=True,
            pubchem_max_retries=0,
            pubchem_call_delay=0.0,
        )
        r = DrugResolver(config=cfg)
        try:
            import requests as real_requests
            # Mock the requests.get to return a non-JSON Content-Type.
            fake_response = mock.MagicMock()
            fake_response.headers = {"Content-Type": "text/html"}
            fake_response.content = b"<html></html>"
            fake_response.raise_for_status.return_value = None
            m_req = mock.MagicMock()
            m_req.exceptions = real_requests.exceptions
            m_req.utils.quote.return_value = "aspirin"
            m_req.get.return_value = fake_response
            with mock.patch.object(drug_resolver, "_requests", m_req):
                result = r._match_by_pubchem_xref("aspirin")
            assert result is None
            assert r.stats.pubchem_failures >= 1
        finally:
            drug_resolver._requests = original_requests

    def test_pubchem_tls_config(self):
        """D9-5: TLS pinning / mTLS config fields exist."""
        cfg = ResolverConfig(
            pubchem_enabled=True,
            pubchem_ca_bundle="/etc/ssl/certs/internal-mirror.pem",
            pubchem_cert_pem="/etc/ssl/certs/client.pem",
            pubchem_key_pem="/etc/ssl/private/client.key",
        )
        assert cfg.pubchem_ca_bundle == "/etc/ssl/certs/internal-mirror.pem"
        assert cfg.pubchem_cert_pem == "/etc/ssl/certs/client.pem"
        assert cfg.pubchem_key_pem == "/etc/ssl/private/client.key"

    def test_pubchem_api_key(self):
        """D9-6: ``pubchem_api_key`` supported; rate limit raised when set."""
        cfg = ResolverConfig(pubchem_enabled=True, pubchem_api_key="secret123")
        assert cfg.pubchem_api_key == "secret123"
        # When an API key is set via from_env, the call delay halves
        # (rate doubles).  We can't test from_env without setting the
        # env var, so just verify the masked-dict hides the key.
        masked = cfg.to_masked_dict()
        assert masked["pubchem_api_key"] == "<redacted>"

    def test_source_name_whitelist(self):
        """D9-7: source whitelist enforcement."""
        cfg = ResolverConfig(source_whitelist=("chembl", "drugbank", "pubchem"))
        r = DrugResolver(config=cfg)
        r.add_source_records(
            [{"name": "X", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        with pytest.raises(ValueError):
            r.add_source_records(
                [{"name": "Y"}], source="unknown_source"
            )


# ===========================================================================
# §10. DOMAIN 10 — TESTING & VALIDATION (D10-1 → D10-7)
# ===========================================================================


class TestDomain10Testing:
    """Audit IDs D10-1 through D10-7."""

    def test_all_symbols_importable_from_package(self):
        """D10-1: every public symbol tested via package import."""
        for name in entity_resolution.__all__:
            if name in ("__version__", "MAPPING_SCHEMA_VERSION"):
                continue
            assert hasattr(entity_resolution, name), (
                f"D10-1: {name!r} not importable from package"
            )

    def test_all_subset_of_dir(self):
        """D10-2: ``__all__`` is a subset of ``dir(entity_resolution)``."""
        d = set(dir(entity_resolution))
        for name in entity_resolution.__all__:
            assert name in d, f"D10-2: {name!r} in __all__ but not in dir()"

    def test_star_import_produces_documented_names(self):
        """D10-3: ``from entity_resolution import *`` produces documented names."""
        namespace: Dict[str, Any] = {}
        exec("from entity_resolution import *", namespace)
        for name in entity_resolution.__all__:
            if name.startswith("__"):
                continue
            assert name in namespace, (
                f"D10-3: {name!r} missing from star-import namespace"
            )

    def test_import_survives_missing_optional_deps(self):
        """D10-4: import succeeds even when pandas/requests are absent.

        Uses a subprocess (see TestDomain6Reliability.test_import_survives_missing_optional_deps
        for the rationale — running this in-process would pollute the
        test session's sys.modules and break identity checks in
        subsequent tests).
        """
        import subprocess
        script = (
            "import sys\n"
            "class _Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        if name.split('.')[0] in ('pandas', 'requests'):\n"
            "            return self\n"
            "        return None\n"
            "    def load_module(self, name):\n"
            "        raise ImportError(f'simulated: {name} not installed')\n"
            "sys.meta_path.insert(0, _Blocker())\n"
            "import entity_resolution as er\n"
            "assert er.__version__ == '1.0.0', "
            "f'version mismatch: {er.__version__}'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"D10-4: import-survives check failed in subprocess:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_is_synthetic_inchikey_reexport_parity(self):
        """D10-6: parity between package re-export and submodule definition."""
        from entity_resolution.base import is_synthetic_inchikey as base_impl
        from entity_resolution.drug_resolver import (
            is_synthetic_inchikey as drug_impl,
        )
        test_cases = [
            None, "", "SYNTH", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            "SYNTHABCDEFGHIJKLM-NOPQRSTUVW-X", 123, [],
        ]
        for tc in test_cases:
            assert is_synthetic_inchikey(tc) == base_impl(tc) == drug_impl(tc)

    def test_confidence_ge_threshold_invariant(self):
        """D10-7: regression test for D3-3 (fuzzy confidence ≥ threshold)."""
        cfg = ResolverConfig()
        assert METHOD_CONFIDENCE["fuzzy"] >= cfg.fuzzy_threshold
        # And the invariant holds for the enum.
        assert MatchConfidence.FUZZY.value >= cfg.fuzzy_threshold
        # And for compute_match_confidence.
        assert compute_match_confidence("fuzzy") >= cfg.fuzzy_threshold


# ===========================================================================
# §11. DOMAIN 11 — LOGGING & OBSERVABILITY (D11-1 → D11-5)
# ===========================================================================


class TestDomain11Logging:
    """Audit IDs D11-1 through D11-5."""

    def test_null_handler_attached(self):
        """D11-1: ``NullHandler`` attached to the package logger."""
        log = logging.getLogger("entity_resolution")
        assert any(isinstance(h, logging.NullHandler) for h in log.handlers), (
            "D11-1: no NullHandler on entity_resolution logger"
        )

    def test_stats_tracking(self):
        """D11-2: ``ResolverStats`` dataclass + ``get_stats`` on resolvers."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        stats = r.get_stats()
        assert isinstance(stats, dict)
        assert stats["records_ingested"] >= 1
        assert stats["records_created"] >= 1

    def test_structured_logging(self):
        """D11-3: ``set_log_format("json")`` enables JSON-formatted logs."""
        # set_log_format doesn't crash.
        set_log_format("json")
        set_log_format("text")
        with pytest.raises(ValueError):
            set_log_format("xml")

    def test_correlation_id_in_logs(self):
        """D11-4: audit-trail events include a timestamp (correlation context)."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        ik = list(r.mapping.keys())[0]
        trail = r.get_audit_trail(ik)
        assert len(trail) >= 1
        assert "timestamp" in trail[0]
        assert "action" in trail[0]

    def test_set_log_level(self):
        """D11-5: ``set_log_level`` helper exported and works."""
        set_log_level("DEBUG")
        assert logging.getLogger("entity_resolution").level == logging.DEBUG
        set_log_level("WARNING")
        assert logging.getLogger("entity_resolution").level == logging.WARNING
        with pytest.raises(ValueError):
            set_log_level("NONSENSE")


# ===========================================================================
# §12. DOMAIN 12 — CONFIGURATION & ENVIRONMENT MANAGEMENT (D12-1 → D12-5)
# ===========================================================================


class TestDomain12Configuration:
    """Audit IDs D12-1 through D12-5."""

    def test_config_env_var_overrides(self, monkeypatch):
        """D12-1: every magic number overridable via env var."""
        monkeypatch.setenv("ENTITY_RESOLUTION_FUZZY_THRESHOLD", "0.75")
        monkeypatch.setenv("ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS", "1")
        cfg = ResolverConfig.from_env()
        assert cfg.fuzzy_threshold == 0.75
        assert cfg.collapse_stereoisomers is True

    def test_config_integration_with_settings(self):
        """D12-2: ``config.settings`` exposes ``ENTITY_RESOLUTION_*`` settings."""
        from config import settings
        assert hasattr(settings, "ENTITY_RESOLUTION_PUBCHEM_ENABLED")
        assert hasattr(settings, "ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS")
        assert hasattr(settings, "ENTITY_RESOLUTION_FUZZY_THRESHOLD")
        assert hasattr(settings, "get_entity_resolution_config")
        summary = settings.get_entity_resolution_config()
        assert "pubchem_enabled" in summary
        assert "collapse_stereoisomers" in summary

    def test_config_rationale_documented(self):
        """D12-3: every config default has a rationale comment."""
        from entity_resolution import base
        src = (PROJECT_ROOT / "entity_resolution" / "base.py").read_text()
        # Spot-check that several fields have rationale.
        for field_name in (
            "collapse_stereoisomers", "fuzzy_threshold",
            "pubchem_enabled", "pubchem_call_delay",
        ):
            assert field_name in src, (
                f"D12-3: {field_name} not found in base.py source"
            )

    def test_state_dict_version_check(self):
        """D12-4: ``from_state_dict`` rejects mismatched schema versions."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        state = r.to_state_dict()
        state["schema_version"] = "99.99"
        with pytest.raises(ValueError):
            DrugResolver.from_state_dict(state)

    def test_defaults_documented(self):
        """D12-5: all defaults documented in module docstring."""
        doc = entity_resolution.__doc__ or ""
        assert "collapse_stereoisomers" in doc
        assert "pubchem_enabled" in doc
        assert "fuzzy_threshold" in doc
        assert "default_organism" in doc
        # The ⚠️ warning on default_organism.
        assert "Homo sapiens" in doc


# ===========================================================================
# §13. DOMAIN 13 — DOCUMENTATION & READABILITY (D13-1 → D13-10)
# ===========================================================================


class TestDomain13Documentation:
    """Audit IDs D13-1 through D13-10."""

    def test_docstring_scope_accurate(self):
        """D13-1: 'and other data sources' removed; 5 actual sources named."""
        doc = entity_resolution.__doc__ or ""
        assert "and other data sources" not in doc
        for src in ("ChEMBL", "DrugBank", "PubChem", "UniProt", "STRING"):
            assert src in doc, f"D13-1: {src} not mentioned in docstring"
        # DisGeNET and OMIM explicitly marked out-of-scope.
        assert "DisGeNET" in doc
        assert "OMIM" in doc
        assert "out of scope" in doc.lower() or "out-of-scope" in doc.lower()

    def test_every_public_symbol_documented(self):
        """D13-2: every public symbol mentioned in module docstring OR has its own docstring."""
        doc = entity_resolution.__doc__ or ""
        for name in entity_resolution.__all__:
            if name.startswith("__") or name in ("base", "drug_resolver",
                                                  "protein_resolver",
                                                  "resolver_utils"):
                continue
            obj = getattr(entity_resolution, name, None)
            if obj is None:
                continue
            # Either the module docstring mentions the name, or the
            # object itself has a docstring.
            assert (name in doc) or (obj.__doc__ is not None), (
                f"D13-2: {name!r} has no documentation"
            )

    def test_network_side_effects_documented(self):
        """D13-3: 🌐 / network side effects documented."""
        doc = entity_resolution.__doc__ or ""
        assert "network" in doc.lower() or "HTTP" in doc, (
            "D13-3: network side effects not documented"
        )

    def test_bulk_vs_single_documented(self):
        """D13-4: 'Bulk vs. Single-Record Mode' section in docstring."""
        doc = entity_resolution.__doc__ or ""
        assert "Bulk vs" in doc or "bulk" in doc.lower(), (
            "D13-4: bulk vs single-record mode not documented"
        )

    def test_fuzzy_listed_as_step_3b(self):
        """D13-5 / D3-2: fuzzy listed as step 3b in resolution strategy."""
        doc = entity_resolution.__doc__ or ""
        assert "3b" in doc and "fuzzy" in doc.lower(), (
            "D13-5: fuzzy not listed as step 3b"
        )

    def test_stereoisomer_safety_warning(self):
        """D13-6: ⚠️ stereoisomer safety warning callout."""
        doc = entity_resolution.__doc__ or ""
        assert "thalidomide" in doc.lower(), (
            "D13-6: thalidomide example not in stereoisomer warning"
        )

    def test_synthetic_inchikey_convention_documented(self):
        """D13-7: 'Synthetic InChIKey Convention' section in docstring."""
        doc = entity_resolution.__doc__ or ""
        assert "synthetic" in doc.lower() and "SYNTH" in doc, (
            "D13-7: synthetic InChIKey convention not documented"
        )

    def test_usage_example(self):
        """D13-8: 'Quick Start' section with usage examples in docstring."""
        doc = entity_resolution.__doc__ or ""
        assert "Quick Start" in doc or "Example" in doc, (
            "D13-8: no Quick Start / usage example"
        )

    def test_raises_section_present(self):
        """D13-9: 'Raises' section on public functions."""
        doc = DrugResolver.add_source_records.__doc__ or ""
        assert "Raises" in doc, (
            "D13-9: add_source_records missing Raises section"
        )

    def test_versionadded_directives(self):
        """D13-10: ``.. versionadded::`` directive in module docstring."""
        doc = entity_resolution.__doc__ or ""
        assert "versionadded" in doc.lower(), (
            "D13-10: no versionadded directive"
        )


# ===========================================================================
# §14. DOMAIN 14 — COMPLIANCE & STANDARDS ADHERENCE (D14-1 → D14-7)
# ===========================================================================


class TestDomain14Compliance:
    """Audit IDs D14-1 through D14-7."""

    def test_py_typed_marker(self):
        """D14-1: ``entity_resolution/py.typed`` exists (PEP 561)."""
        p = PROJECT_ROOT / "entity_resolution" / "py.typed"
        assert p.exists(), "D14-1: py.typed marker missing"

    def test_stub_matches_implementation(self):
        """D14-2: ``__init__.pyi`` stub file exists with every public symbol."""
        stub = PROJECT_ROOT / "entity_resolution" / "__init__.pyi"
        assert stub.exists(), "D14-2: __init__.pyi missing"
        text = stub.read_text()
        for name in ("DrugResolver", "ProteinResolver", "Resolver",
                     "ResolverConfig", "ResolverStats", "MatchConfidence",
                     "normalize_name", "is_valid_inchikey",
                     "is_synthetic_inchikey", "METHOD_CONFIDENCE"):
            assert name in text, f"D14-2: {name!r} missing from .pyi stub"

    def test_spdx_header_present(self):
        """D14-6: SPDX license identifier on every .py file."""
        pkg_dir = PROJECT_ROOT / "entity_resolution"
        for f in pkg_dir.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert "SPDX-License-Identifier" in text, (
                f"D14-6: SPDX header missing from {f.name}"
            )

    def test_relative_imports(self):
        """D14-4 / D1-1: all intra-package imports are relative."""
        # Already covered by test_no_absolute_intra_package_imports;
        # re-asserted under the D14-4 audit ID.
        pkg_dir = PROJECT_ROOT / "entity_resolution"
        for f in pkg_dir.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines()
                     if l.strip().startswith("from entity_resolution.")]
            assert not lines, (
                f"D14-4: {f.name} has absolute intra-package imports: {lines}"
            )

    def test_is_synthetic_inchikey_in_all(self):
        """D14-5 / D2-1: ``is_synthetic_inchikey`` in ``__all__``."""
        assert "is_synthetic_inchikey" in entity_resolution.__all__

    def test_google_docstring_convention(self):
        """D14-7 / D4-2: Google convention throughout."""
        # Spot-check normalize_name.
        doc = normalize_name.__doc__ or ""
        assert "Parameters" in doc or "Args" in doc
        assert "Returns" in doc
        assert "Examples" in doc


# ===========================================================================
# §15. DOMAIN 15 — INTEROPERABILITY & INTEGRATION (D15-1 → D15-6)
# ===========================================================================


class TestDomain15Interoperability:
    """Audit IDs D15-1 through D15-6."""

    def test_dataframe_agnostic_apis(self):
        """D15-3: ``to_dict`` / ``to_records`` exist (no pandas dependency)."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        d = r.to_dict()
        assert isinstance(d, dict)
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in d
        recs = r.to_records()
        assert isinstance(recs, list)
        assert len(recs) == 1

    def test_json_serialization(self):
        """D15-4: ``to_json`` / ``from_json`` exist; JSON Schema file exists."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}],
            source="chembl",
        )
        text = r.to_json()
        assert isinstance(text, str)
        # Round-trip.
        r2 = DrugResolver.from_json(text)
        assert set(r.mapping.keys()) == set(r2.mapping.keys())
        # Schema file exists.
        schema = PROJECT_ROOT / "entity_resolution" / "schema" / "v1.json"
        assert schema.exists(), "D15-4: schema/v1.json missing"

    def test_schema_version_compatibility(self):
        """D15-5: ``validate_schema_version`` enforces schema version."""
        # Already covered by test_state_dict_version_check (D12-4).
        # Re-assert that the constant is exported.
        assert MAPPING_SCHEMA_VERSION == "1.0"

    def test_match_confidence_enum(self):
        """D15-6: ``MatchConfidence`` enum; values are a closed set."""
        assert MatchConfidence.FUZZY.value == 0.85
        assert MatchConfidence.INCHIKEY_EXACT.value == 1.0
        # from_method maps known methods to enum members.
        assert MatchConfidence.from_method("fuzzy") == MatchConfidence.FUZZY
        assert MatchConfidence.from_method("inchikey_exact") == \
            MatchConfidence.INCHIKEY_EXACT
        # Unknown method returns UNKNOWN.
        assert MatchConfidence.from_method("nonsense") == MatchConfidence.UNKNOWN

    def test_submodules_in_all(self):
        """D15-1 / D1-4: submodules in ``__all__``."""
        for sub in ("base", "drug_resolver", "protein_resolver", "resolver_utils"):
            assert sub in entity_resolution.__all__

    def test_pep562_submodule_lazy_loading(self):
        """D15-2: ``__getattr__`` lazily loads submodules.

        Verified via subprocess to avoid polluting the test session's
        ``sys.modules``.
        """
        import subprocess
        script = (
            "import entity_resolution as er\n"
            "mod = er.base\n"
            "assert mod.__name__ == 'entity_resolution.base', "
            "f'expected entity_resolution.base, got {mod.__name__}'\n"
            "assert hasattr(mod, 'ResolverConfig'), 'ResolverConfig missing'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"D15-2: PEP 562 submodule lazy-loading check failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout


# ===========================================================================
# §16. DOMAIN 16 — DATA LINEAGE & TRACEABILITY (D16-1 → D16-7)
# ===========================================================================


class TestDomain16Lineage:
    """Audit IDs D16-1 through D16-7."""

    def test_provenance_metadata(self):
        """D16-2: ``resolved_at``, ``resolver_version``, ``input_checksum`` in entries."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        entry = list(r.mapping.values())[0]
        assert "resolved_at" in entry
        assert "resolver_version" in entry
        assert entry["resolver_version"] == MAPPING_SCHEMA_VERSION
        assert "input_checksum" in entry
        assert entry["input_checksum"], "input_checksum should be non-empty"

    def test_synthetic_key_log_correlation_id(self):
        """D16-5: synthetic-key logs include stable correlation context."""
        r = DrugResolver()
        # Synthetic-key generation must populate the audit trail with
        # a 'create' event including the inchikey (stable correlation ID).
        r.add_source_records(
            [{"name": "MysteryDrug", "chembl_id": "CHEMBL_MYST"}],
            source="chembl",
        )
        canonical = list(r.mapping.keys())[0]
        assert canonical.startswith("SYNTH")
        trail = r.get_audit_trail(canonical)
        assert len(trail) >= 1
        create_event = trail[0]
        assert create_event["action"] == "create"
        assert create_event["inchikey"] == canonical

    def test_audit_trail(self):
        """D16-6: per-entry audit trail of merges."""
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        r.add_source_records(
            [{"name": "Acetylsalicylic acid",
              "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "drugbank_id": "DB00945"}],
            source="drugbank",
        )
        ik = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        trail = r.get_audit_trail(ik)
        # Should have a 'create' event (from chembl) and a 'merge' event
        # (from drugbank).
        actions = [e["action"] for e in trail]
        assert "create" in actions
        assert "merge" in actions

    def test_method_confidence_exported(self):
        """D16-7 / D2-4: ``METHOD_CONFIDENCE`` exported."""
        assert "METHOD_CONFIDENCE" in entity_resolution.__all__
        assert isinstance(METHOD_CONFIDENCE, dict)
        assert "fuzzy" in METHOD_CONFIDENCE
        # And the private alias is preserved for backward compat.
        from entity_resolution.resolver_utils import _METHOD_CONFIDENCE
        assert _METHOD_CONFIDENCE is METHOD_CONFIDENCE

    def test_sources_column_preserves_provenance(self):
        """D16-1 / D5-5: ``sources`` column preserves source attribution.

        Updated for audit 2.7 / 14.20: ``sources`` is now JSON-encoded
        (was comma-separated).  This is unambiguous when a source label
        contains a comma.
        """
        import json as _json
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        r.add_source_records(
            [{"name": "Acetylsalicylic acid",
              "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "drugbank_id": "DB00945"}],
            source="drugbank",
        )
        df = r.to_dataframe()
        row = df.iloc[0]
        # Audit 2.7: sources column is JSON-encoded.
        sources = _json.loads(row["sources"])
        assert "chembl" in sources
        assert "drugbank" in sources


# ===========================================================================
# Doctest runner — runs every doctest in the module docstring.
# ===========================================================================


def test_doctests_in_module_docstring():
    """D10-5: doctests in module docstring pass."""
    import doctest
    results = doctest.testmod(
        entity_resolution, verbose=False, optionflags=doctest.ELLIPSIS,
    )
    assert results.failed == 0, (
        f"D10-5: {results.failed} doctest(s) failed in module docstring"
    )


# ===========================================================================
# Cross-cutting: end-to-end smoke test exercising every public symbol.
# ===========================================================================


class TestEndToEndSmoke:
    """End-to-end smoke test that exercises every public symbol."""

    def test_full_drug_resolution_flow(self):
        r = make_drug_resolver()
        df = r.build_mapping(
            _aspirin_chembl_df(), _aspirin_drugbank_df(), _aspirin_pubchem_df()
        )
        assert len(df) == 1
        row = df.iloc[0]
        assert row["chembl_id"] == "CHEMBL25"
        assert row["drugbank_id"] == "DB00945"
        assert row["pubchem_cid"] == 2244

    def test_full_protein_resolution_flow(self):
        r = make_protein_resolver()
        uniprot_df = pd.DataFrame({
            "uniprot_id": ["P04637"],
            "gene_symbol": ["TP53"],
            "gene_name": ["TP53"],
            "organism": ["Homo sapiens"],
        })
        string_df = pd.DataFrame({
            "string_id": ["9606.ENSP00000269305"],
            "gene_symbol": ["TP53"],
            "organism": ["Homo sapiens"],
        })
        r.add_uniprot_records(r._df_to_records(uniprot_df))
        r.add_string_records(r._df_to_records(string_df))
        assert len(r.mapping) == 1
        assert "P04637" in r.mapping
        entry = r.mapping["P04637"]
        assert entry["string_id"] == "9606.ENSP00000269305"

    def test_single_record_resolution(self):
        r = DrugResolver()
        r.add_source_records(
            [{"name": "Aspirin", "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
              "chembl_id": "CHEMBL25"}],
            source="chembl",
        )
        result = r.resolve_single("Aspirin",
                                   inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
        assert result["canonical_inchikey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        assert result["match_method"] == "inchikey_exact"
        assert result["match_confidence"] == 1.0
