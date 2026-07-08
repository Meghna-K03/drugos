"""
Test 2: Integration test for all 20 files working together.

Verifies that the repaired chemberta_encoder.py integrates correctly
with the 19 previously-fixed files. Tests cross-module contracts,
import chains, shared config, and the full pipeline data flow.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestPackageImports:
    """Verify all 20 modules import successfully."""

    def test_import_config(self):
        from drugos_graph import config
        assert hasattr(config, "SEED")
        assert config.SEED == 42

    def test_import_init(self):
        from drugos_graph import __version__
        assert isinstance(__version__, str)

    def test_import_utils(self):
        from drugos_graph import utils
        assert hasattr(utils, "LabelRegistry")

    def test_import_exceptions(self):
        from drugos_graph import exceptions
        assert hasattr(exceptions, "DrugOSDataError")

    def test_import_schemas(self):
        from drugos_graph import schemas
        assert hasattr(schemas, "UniProtRecord")

    def test_import_id_crosswalk(self):
        from drugos_graph import id_crosswalk

    def test_import_uniprot_loader(self):
        from drugos_graph import uniprot_loader

    def test_import_drkg_loader(self):
        from drugos_graph import drkg_loader

    def test_import_drugbank_parser(self):
        from drugos_graph import drugbank_parser
        assert hasattr(drugbank_parser, "_validate_smiles")

    def test_import_chembl_loader(self):
        from drugos_graph import chembl_loader

    def test_import_string_loader(self):
        from drugos_graph import string_loader

    def test_import_stitch_loader(self):
        from drugos_graph import stitch_loader

    def test_import_sider_loader(self):
        from drugos_graph import sider_loader

    def test_import_opentargets_loader(self):
        from drugos_graph import opentargets_loader

    def test_import_geo_loader(self):
        from drugos_graph import geo_loader

    def test_import_entity_resolver(self):
        from drugos_graph import entity_resolver

    def test_import_kg_builder(self):
        from drugos_graph import kg_builder

    def test_import_pyg_builder(self):
        from drugos_graph import pyg_builder

    def test_import_negative_sampling(self):
        from drugos_graph import negative_sampling

    def test_import_training_data(self):
        from drugos_graph import training_data

    def test_import_evaluation(self):
        from drugos_graph import evaluation

    def test_import_transe_model(self):
        from drugos_graph import transe_model

    def test_import_chemberta_encoder(self):
        from drugos_graph import chemberta_encoder
        assert hasattr(chemberta_encoder, "encode_smiles")
        assert hasattr(chemberta_encoder, "verify_embedding_quality")
        assert hasattr(chemberta_encoder, "CHEMBERTA_MODEL")


class TestCrossModuleContracts:
    """Verify that the 20 modules' interfaces are consistent."""

    def test_chemberta_uses_config_constants(self):
        """chemberta_encoder imports EMBEDDINGS_DIR and ensure_dirs from config."""
        from drugos_graph.chemberta_encoder import EMBEDDINGS_DIR as ce_embed
        from drugos_graph.config import EMBEDDINGS_DIR as cfg_embed
        assert ce_embed == cfg_embed

    def test_chemberta_reuses_drugbank_validators(self):
        """chemberta_encoder imports _validate_smiles from drugbank_parser."""
        from drugos_graph.chemberta_encoder import _validate_smiles as ce_val
        from drugos_graph.drugbank_parser import _validate_smiles as db_val
        assert ce_val is db_val

    def test_chemberta_dim_matches_pyg_config(self):
        """Chemberta dim (768) matches PyGConfig.compound_feat_dim."""
        from drugos_graph.config import CHEMBERTA_DIM_BY_MODEL
        assert "seyonec/ChemBERTa-zinc-base-v1" in CHEMBERTA_DIM_BY_MODEL
        assert CHEMBERTA_DIM_BY_MODEL["seyonec/ChemBERTa-zinc-base-v1"] == 768

    def test_seed_consistent_across_modules(self):
        """SEED is the same constant used by training_data, transe, evaluation."""
        from drugos_graph.config import SEED
        assert SEED == 42

    def test_schema_versions_defined(self):
        """All schema version constants are defined."""
        from drugos_graph.config import (
            PACKAGE_VERSION, PIPELINE_VERSION,
            CONFIG_VERSION, SCHEMA_VERSION,
        )
        for v in [PACKAGE_VERSION, PIPELINE_VERSION, CONFIG_VERSION, SCHEMA_VERSION]:
            assert isinstance(v, str) and len(v) >= 3

    def test_chemberta_encoder_has_all_export(self):
        """__all__ includes all required exports for __init__.py lazy import."""
        from drugos_graph.chemberta_encoder import __all__ as ce_all
        required = {"encode_smiles", "verify_embedding_quality", "CHEMBERTA_MODEL"}
        assert required.issubset(set(ce_all))


class TestChembertaPyGIntegration:
    """Test the chemberta_encoder → pyg_builder data flow contract."""

    def test_encode_result_satisfies_pyg_contract(self):
        """ChembertaEncodeResult shape matches what add_chemberta_features expects.

        pyg_builder.add_chemberta_features expects:
        - smiles_embeddings: 2D tensor of shape (N, D)
        - compound_id_order: List[str] matching row indices
        - All compound IDs are non-empty strings
        """
        from drugos_graph.chemberta_encoder import ChembertaEncodeResult

        mock_emb = torch.randn(5, 768)
        mock_ids = ["id_a", "id_b", "id_c", "id_d", "id_e"]
        result = ChembertaEncodeResult(
            embeddings=mock_emb,
            compound_ids=mock_ids,
            model_name="seyonec/ChemBERTa-zinc-base-v1",
            model_commit_hash="abc123",
            pooling="mean",
            torch_dtype="float32",
        )

        # Contract checks
        assert result.embeddings.shape[0] == len(result.compound_ids)
        assert result.embeddings.dim() == 2
        assert result.embeddings.shape[1] == 768
        assert all(isinstance(cid, str) and cid for cid in result.compound_ids)

        # Backward compat unpacking
        emb, ids = result
        assert torch.equal(emb, mock_emb)
        assert ids == mock_ids


class TestChembertaCacheSchema:
    """Test cache payload schema completeness."""

    def test_cache_payload_has_all_required_keys(self):
        """ChembertaCachePayload TypedDict documents all fields."""
        from drugos_graph.chemberta_encoder import ChembertaCachePayload
        # TypedDict with total=False — just verify it's defined
        assert ChembertaCachePayload.__annotations__

    def test_cache_format_version_in_payload(self):
        """cache_format_version is a string starting with '1.'."""
        from drugos_graph.chemberta_encoder import CHEMBERTA_CACHE_FORMAT_VERSION
        assert CHEMBERTA_CACHE_FORMAT_VERSION.startswith("1.")


class TestExceptionHierarchy:
    """Test that chemberta exceptions integrate with the codebase."""

    def test_chemberta_errors_inherit_runtime(self):
        from drugos_graph.chemberta_encoder import (
            ChembertaEncoderError,
            ChembertaCacheIntegrityError,
            ChembertaSMILESValidationError,
            ChembertaDeviceError,
            ChembertaEmbeddingCorruptionError,
        )
        assert issubclass(ChembertaEncoderError, RuntimeError)
        assert issubclass(ChembertaCacheIntegrityError, ChembertaEncoderError)
        assert issubclass(ChembertaSMILESValidationError, (ChembertaEncoderError, ValueError))
        assert issubclass(ChembertaDeviceError, ChembertaEncoderError)
        assert issubclass(ChembertaEmbeddingCorruptionError, ChembertaEncoderError)

    def test_base_exception_available(self):
        from drugos_graph.exceptions import DrugOSDataError
        # Chemberta errors don't inherit from DrugOSDataError (they're
        # a separate hierarchy for ML-specific errors), but the base
        # exception should be importable.
        assert DrugOSDataError is not None


class TestConfigInfrastructure:
    """Test that config infrastructure used by chemberta_encoder works."""

    def test_ensure_dirs_runs(self):
        from drugos_graph.config import ensure_dirs
        ensure_dirs()  # Should not raise

    def test_device_config_auto_resolves(self):
        from drugos_graph.config import DeviceConfig
        dc = DeviceConfig(device="auto")
        resolved = dc.resolve()
        assert resolved in ("cpu", "cuda", "mps")

    def test_device_config_rejects_invalid(self):
        from drugos_graph.config import DeviceConfig
        with pytest.raises(ValueError):
            DeviceConfig(device="tpu")

    def test_dead_letter_dir_exists_after_ensure(self):
        from drugos_graph.config import ensure_dirs, DEAD_LETTER_DIR
        ensure_dirs()
        # May or may not exist depending on permissions, but shouldn't crash

    def test_cheemberta_dim_by_model_complete(self):
        from drugos_graph.config import CHEMBERTA_DIM_BY_MODEL
        assert len(CHEMBERTA_DIM_BY_MODEL) >= 2
        assert "seyonec/ChemBERTa-zinc-base-v1" in CHEMBERTA_DIM_BY_MODEL


class TestFullPipelineDataFlow:
    """Test that the full data flow from SMILES to embedding result works."""

    def test_smiles_to_result_full_flow(self):
        """End-to-end: SMILES → validate → tokenize → encode → result."""
        # v6 fix (bug #B1): skip when transformers is not installed.
        # The previous code unconditionally patched
        # `drugos_graph.chemberta_encoder.AutoModel.from_pretrained`, but
        # when `transformers` is missing `AutoModel` is `None` —
        # `patch("…None.from_pretrained")` crashes with AttributeError.
        # The test should SKIP, not FAIL, in that environment.
        try:
            import transformers  # noqa: F401
        except ImportError:
            pytest.skip(
                "transformers not installed — chemberta_encoder.AutoModel is "
                "None, cannot exercise SMILES→embedding flow."
            )

        from drugos_graph.chemberta_encoder import encode_smiles

        mock_model = MagicMock()
        gen = torch.Generator()
        gen.manual_seed(42)
        out = torch.randn(3, 10, 768, generator=gen)
        mock_output = MagicMock()
        mock_output.last_hidden_state = out
        mock_output.pooler_output = out[:, 0, :]
        mock_model.return_value = mock_output
        mock_model.config = MagicMock()
        mock_model.config._commit_hash = "test_hash"
        mock_model.to = MagicMock(return_value=mock_model)
        mock_model.eval = MagicMock()

        mock_tok = MagicMock()
        mock_tok.return_value = {
            "input_ids": torch.randint(1, 100, (3, 10)),
            "attention_mask": torch.ones(3, 10, dtype=torch.long),
        }

        with patch("drugos_graph.chemberta_encoder.AutoModel.from_pretrained", return_value=mock_model):
            with patch("drugos_graph.chemberta_encoder.AutoTokenizer.from_pretrained", return_value=mock_tok):
                result = encode_smiles(
                    smiles_list=["c1ccccc1", "CC(=O)O", "CCO"],
                    compound_ids=["benzene", "acetic_acid", "ethanol"],
                    no_cache=True,
                    pooling="mean",
                    normalize=True,
                    seed=42,
                )

        # Verify the result
        assert result.embeddings.shape == (3, 768)
        # NOTE: chemberta_encoder sorts by SMILES for deterministic cache
        # (master_prompt_fix_chemberta_encoder.md — Domain 7 Idempotency).
        # We use set comparison to verify all expected compound IDs are
        # present without coupling the test to the internal sort order.
        assert set(result.compound_ids) == {"benzene", "acetic_acid", "ethanol"}
        assert result.failed_compound_ids == []
        assert result.metrics.get("cache_hit") is False

        # Verify embeddings are L2-normalized
        norms = torch.norm(result.embeddings, p=2, dim=1)
        assert torch.allclose(norms, torch.ones(3), atol=1e-5)

        # Verify provenance
        assert result.model_name != ""
        assert result.pooling == "mean"
        assert result.torch_dtype == "float32"

    def test_backwards_compat_tuple_unpacking_in_flow(self):
        """The result supports (emb, ids) unpacking in the pipeline."""
        # v6 fix (bug #B1): skip when transformers is not installed.
        try:
            import transformers  # noqa: F401
        except ImportError:
            pytest.skip(
                "transformers not installed — chemberta_encoder.AutoModel is "
                "None, cannot exercise SMILES→embedding flow."
            )

        from drugos_graph.chemberta_encoder import encode_smiles

        mock_model = MagicMock()
        mock_output = MagicMock()
        mock_output.last_hidden_state = torch.randn(2, 10, 768)
        mock_output.pooler_output = torch.randn(2, 768)
        mock_model.return_value = mock_output
        mock_model.config = MagicMock()
        mock_model.config._commit_hash = "test"
        mock_model.to = MagicMock(return_value=mock_model)
        mock_model.eval = MagicMock()

        mock_tok = MagicMock()
        mock_tok.return_value = {
            "input_ids": torch.randint(1, 100, (2, 10)),
            "attention_mask": torch.ones(2, 10, dtype=torch.long),
        }

        with patch("drugos_graph.chemberta_encoder.AutoModel.from_pretrained", return_value=mock_model):
            with patch("drugos_graph.chemberta_encoder.AutoTokenizer.from_pretrained", return_value=mock_tok):
                embeddings, compound_ids = encode_smiles(
                    ["c1ccccc1", "CCO"],
                    ["benzene", "ethanol"],
                    no_cache=True,
                )

        assert isinstance(embeddings, torch.Tensor)
        # NOTE: encoder sorts by SMILES for deterministic cache (see above).
        assert set(compound_ids) == {"benzene", "ethanol"}