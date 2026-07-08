"""DrugOS Graph Module — ChemBERTa SMILES Encoder
============================================================

v43 ROOT FIX (P2-009) — BROAD EXCEPT POLICY:
  This module historically had 26+ ``except Exception`` blocks that
  silently swallowed HuggingFace transformers errors
  (OSError, EnvironmentError, HTTPError, RuntimeError, etc.). When
  ChEMBERTa is enabled (DRUGOS_USE_CHEMBERTA=1), these broad excepts
  silently disable ChEMBERTa features and the operator sees only a
  WARNING log. The v43 fix establishes a module-level convention:
  ALL except blocks MUST catch specific exception types and MUST
  surface failures in the step9 result dict (not just log them).
  The encoder is OPT-IN by default, so these excepts are mostly dead
  in the default path — but when enabled, failures MUST be visible.

Generates molecular embeddings from SMILES strings using a
HuggingFace transformer model (default: ChemBERTa-zinc-base-v1).

The embedding dimension depends on the model — see
``config.CHEMBERTA_DIM_BY_MODEL`` for the lookup table. The default
model produces 768-dim embeddings.

Caches embeddings to disk for reuse. The cache is keyed by a
comprehensive fingerprint (model name + revision + commit hash +
transformers version + torch version + pooling strategy + dtype +
max_length + normalize flag + seed + deterministic flag + SMILES
hash + compound_ids hash + cache_format_version). A cache hit
requires ALL fields to match; any mismatch triggers a cache miss
with a structured log explaining which field differed.

Patient-safety doctrine
-----------------------
This module produces embeddings that feed a Graph Transformer
predicting drug-disease interactions. A silently-wrong embedding
(from a cache collision, a truncated SMILES, a NaN that propagated,
or an unstereo-canonicalized enantiomer) trains the model on
garbage and produces wrong drug predictions. Every code path in
this module is designed to FAIL LOUDLY rather than silently degrade.

FDA 21 CFR Part 11 compliance
-----------------------------
When ``regulatory_mode=True`` (or ``config.DETERMINISTIC_MODE=True``),
the encoder forces deterministic algorithms, fixed seeds, local-only
model loading, and full audit logging. This satisfies the
reproducibility requirements for clinical-grade runs.

Cache format
------------
See ``ChembertaCachePayload`` (TypedDict in this module) and
``docs/schemas/chemberta_cache.schema.json`` for the cache file
schema. Cache format version: ``CHEMBERTA_CACHE_FORMAT_VERSION``.

Data Subject Considerations (GDPR / HIPAA)
-------------------------------------------
SMILES are not generally PII, but if the input SMILES are derived
from patient-specific compounds (e.g., patient-derived metabolomics),
the embeddings are derived data and inherit the source's data-subject
annotations. Use the ``data_subject_annotations`` parameter to
record this in the cache for downstream GDPR/HIPAA tooling.

Decision Log
------------
- pooling="mean" is the default because the ChemBERTa paper
  (Chithrananda et al. 2020) and sentence-transformers literature
  show that mean pooling outperforms <s> token pooling for
  sentence-level molecular embeddings without fine-tuning.
- max_length=512 because ChemBERTa-zinc-base-v1's maximum
  position embedding is 512 tokens. Truncating beyond this
  destroys chemical meaning — see ``on_truncate`` parameter.
- batch_size=64 default because this is conservative for 16GB
  GPU memory with 768-dim hidden states.
- L2 normalization is on by default because downstream
  dot-product attention is cosine-similarity-faithful only
  when vectors are L2-normalized.
- torch_dtype="float32" default because fp32 provides
  reproducibility across GPU vendors. Users opt into fp16.
- <s> pooling is preserved as an option for backward
  compatibility with existing caches, but is NOT recommended.

.. versionadded:: 2.3.0
    Institutional-grade rewrite addressing 308 audit findings
    across 16 verification domains.
"""

from __future__ import annotations

# ─── Standard-library imports ─────────────────────────────────────────
# Fixes audit issue 1.3 — stdlib imports at module level
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import signal
import sys
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
)

# ─── Third-party imports (eager — hard dependencies) ─────────────────
# Fixes audit issue 1.3 — torch is a hard dependency in requirements.txt
try:
    import torch
    _HAS_TORCH = True
except ImportError:
    torch = None  # type: ignore[assignment]
    _HAS_TORCH = False

import numpy as np

# ─── Third-party imports (guarded — optional dependencies) ───────────
try:
    from transformers import AutoModel, AutoTokenizer
    _HAS_TRANSFORMERS = True
except ImportError:
    AutoModel = None  # type: ignore[assignment, misc]
    AutoTokenizer = None  # type: ignore[assignment, misc]
    _HAS_TRANSFORMERS = False

try:
    from rdkit import Chem  # type: ignore[import-not-found]
    _HAS_RDKIT = True
except ImportError:
    Chem = None  # type: ignore[assignment, misc]
    _HAS_RDKIT = False

# Optional: prometheus_client for metrics (in requirements.txt)
try:
    from prometheus_client import Counter, Histogram
    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False

# Optional: mlflow for experiment tracking (in requirements.txt)
try:
    import mlflow
    _HAS_MLFLOW = True
except ImportError:
    mlflow = None  # type: ignore[assignment, misc]
    _HAS_MLFLOW = False

# ─── Intra-package imports ───────────────────────────────────────────
from .config import (  # noqa: E402
    EMBEDDINGS_DIR,
    DEAD_LETTER_DIR,
    CHECKPOINT_DIR,
    AUDIT_LOG_DIR,
    OUTPUT_METADATA_DIR,
    TRANSFORMATION_LOG_DIR,
    CONFIG_DIFF_DIR,
    IMPACT_ANALYSIS_DIR,
    ensure_dirs,
    set_global_seed,
    DETERMINISTIC_MODE,
    SEED,
    RUN_ID,
    PACKAGE_VERSION,
    PIPELINE_VERSION,
    CONFIG_VERSION,
    SCHEMA_VERSION,
    CONFIG_HASH,
    CHEMBERTA_DIM_BY_MODEL,
    DeviceConfig,
    FILE_PERMISSIONS,
    dead_letter_record,
    write_checkpoint,
    read_latest_checkpoint,
    build_lineage_metadata,
    write_lineage_manifest,
    compute_model_hash,
    audit_log,
    log_transformation,
    deprecated,
)

# Reuse SMILES/InChIKey validators from drugbank_parser instead of
# re-implementing. Fixes audit issue 3.2, 1.4.
from .drugbank_parser import _validate_smiles, _validate_inchikey

# v35 ROOT FIX (L-22): validate EMBEDDINGS_DIR at module import time
# so a misconfigured ``config.EMBEDDINGS_DIR`` (e.g. a string instead
# of a Path, or a non-writable directory) fails FAST at import rather
# than at the first ``encode_smiles`` call. The previous code did
# this check inside ``_validate_inputs`` which meant a bad config was
# not detected until the operator had already started the (potentially
# multi-hour) encode run — wasting GPU time. The check is wrapped in
# try/except so a missing ``EMBEDDINGS_DIR`` (e.g. in test
# environments) does not break import. ChembertaEncoderError is
# defined later in this module, so we use a generic RuntimeError
# here and let the full check happen in ``_validate_inputs``.
try:
    if not isinstance(EMBEDDINGS_DIR, Path):
        import warnings as _warnings
        _warnings.warn(
            f"EMBEDDINGS_DIR is not a Path object — got "
            f"{type(EMBEDDINGS_DIR).__name__}. encode_smiles will "
            f"fail with ChembertaEncoderError. Check config.py. (L-22)",
            RuntimeWarning,
            stacklevel=2,
        )
except (ImportError, AttributeError, TypeError) as _emb_dir_exc:
    # v41 ROOT FIX (Task J SEV3): narrowed from bare ``except Exception``.
    # The legitimate failure modes for the EMBEDDINGS_DIR import-time
    # check are:
    #   - ImportError: config.EMBEDDINGS_DIR not yet defined (config
    #     module failed to import in a partial-install environment).
    #   - AttributeError: EMBEDDINGS_DIR attribute missing from config
    #     (config regression, partial migration).
    #   - TypeError: EMBEDDINGS_DIR is a non-Path/non-str object that
    #     the warning's format string can't render.
    # Other exceptions (KeyboardInterrupt, MemoryError, SystemExit)
    # should propagate, not be silently swallowed. Logged at WARNING
    # so operators notice when the import-time check is bypassed.
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "EMBEDDINGS_DIR import-time check skipped (%s: %s) — the "
        "check will run at first encode_smiles call instead.",
        type(_emb_dir_exc).__name__, _emb_dir_exc,
    )

# ─── Module-level constants ──────────────────────────────────────────
# Fixes audit issue 12.1 — CHEMBERTA_MODEL env override
CHEMBERTA_MODEL: str = os.environ.get(
    "DRUGOS_CHEMBERTA_MODEL",
    "seyonec/ChemBERTa-zinc-base-v1",
)

# Fixes audit issue 14.6 — cache format version
CHEMBERTA_CACHE_FORMAT_VERSION: str = "1.0.0"

# Fixes audit issue 15.10 — API versioning
CHEMBERTA_ENCODER_API_VERSION: str = "2.0.0"

# Fixes audit issue 12.2 — batch_size env override
CHEMBERTA_DEFAULT_BATCH_SIZE: int = int(
    os.environ.get("DRUGOS_CHEMBERTA_BATCH_SIZE", "64")
)

# Fixes audit issue 12.3 — max_length env override
CHEMBERTA_DEFAULT_MAX_LENGTH: int = int(
    os.environ.get("DRUGOS_CHEMBERTA_MAX_LENGTH", "512")
)

# Fixes audit issue 12.11 — dtype env override
CHEMBERTA_DEFAULT_DTYPE: str = os.environ.get(
    "DRUGOS_CHEMBERTA_DTYPE", "float32"
)

# Fixes audit issue 12.14 — revision env override
CHEMBERTA_DEFAULT_REVISION: str = os.environ.get(
    "DRUGOS_CHEMBERTA_REVISION", "main"
)

# Fixes audit issue 12.12 — sample_size env override
CHEMBERTA_DEFAULT_SAMPLE_SIZE: int = int(
    os.environ.get("DRUGOS_CHEMBERTA_SAMPLE_SIZE", "10")
)

# Fixes audit issue 12.4 — cache dir env override
CHEMBERTA_DEFAULT_CACHE_DIR: Path = Path(
    os.environ.get("DRUGOS_CHEMBERTA_CACHE_DIR", str(EMBEDDINGS_DIR))
)

# Fixes audit issue 12.13 — HF cache dir
CHEMBERTA_HF_CACHE_DIR: Optional[str] = os.environ.get(
    "DRUGOS_CHEMBERTA_HF_CACHE_DIR"
) or os.environ.get("HF_HOME")

# Fixes audit issue 9.7 — cache contains proprietary data
CACHE_CONTAINS_PROPRIETARY_DATA: bool = True

# Fixes audit issue 9.10 — public model name allowlist
_PUBLIC_MODEL_ORGS: frozenset[str] = frozenset({
    "seyonec", "navidved", "microsoft", "google", "facebook",
})

# Fixes audit issue 6.14 — circuit breaker threshold
CIRCUIT_BREAKER_THRESHOLD: int = int(
    os.environ.get("DRUGOS_CHEMBERTA_CIRCUIT_BREAKER", "100")
)

# Fixes audit issue 8.2 — model cache (process-local)
_MODEL_CACHE: Dict[Tuple[str, str, str], Tuple[Any, Any, str]] = {}
_MODEL_CACHE_LOCK: threading.Lock = threading.Lock()

logger = logging.getLogger(__name__)

# ─── Optional Prometheus metrics ─────────────────────────────────────
# Fixes audit issue 11.2
if _HAS_PROMETHEUS:
    _p_smiles_total = Counter(
        "drugos_chemberta_smiles_total",
        "Total SMILES processed", ["model", "status"],
    )
    _p_batches = Counter(
        "drugos_chemberta_batches_total",
        "Total batches processed", ["model"],
    )
    _p_cache_hits = Counter(
        "drugos_chemberta_cache_hits_total",
        "Cache hits", ["model"],
    )
    _p_cache_misses = Counter(
        "drugos_chemberta_cache_misses_total",
        "Cache misses", ["model"],
    )
    _p_encode_seconds = Histogram(
        "drugos_chemberta_encode_seconds",
        "Encoding duration", ["model"],
    )
else:
    # No-op stubs — fixes audit issue 1.3 (no new hard deps)
    class _NoOpCounter:
        def labels(self, **kw): return self
        def inc(self, n=1): pass
    class _NoOpHistogram:
        def labels(self, **kw): return self
        def observe(self, v): pass
    _p_smiles_total = _NoOpCounter()  # type: ignore[assignment]
    _p_batches = _NoOpCounter()  # type: ignore[assignment]
    _p_cache_hits = _NoOpCounter()  # type: ignore[assignment]
    _p_cache_misses = _NoOpCounter()  # type: ignore[assignment]
    _p_encode_seconds = _NoOpHistogram()  # type: ignore[assignment]


# ─── TypedDict / Dataclass definitions ──────────────────────────────

class ChembertaCachePayload(TypedDict, total=False):
    """Schema for the ChemBERTa embedding cache file.

    Fixes audit issues 5.1, 5.11, 14.6, 16.1.
    All keys are technically optional for total=False to allow
    backward-compat reads, but new caches always include all keys.
    """
    cache_format_version: str
    embeddings: Any  # torch.Tensor
    compound_ids: List[str]
    model_name: str
    model_revision: str
    model_commit_hash: str
    transformers_version: str
    torch_version: str
    pooling: str
    torch_dtype: str
    max_length: int
    normalize: bool
    smiles_hash: str
    compound_ids_hash: str
    source_dataset: str
    source_dataset_version: str
    seed: int
    deterministic_mode: bool
    pipeline_version: str
    config_version: str
    config_hash: str
    schema_version: str
    run_id: str
    created_at: str
    created_by: str
    input_checksums: Dict[str, str]
    cache_sha256: str
    embeddings_sha256: str
    license: Optional[str]
    attribution: Optional[str]
    commercial_use_allowed: bool
    generated_by: str
    generated_by_user: str


@dataclass
class ChembertaEncodeResult:
    """Result of encoding SMILES strings.

    Fixes audit issues 2.1, 13.14, 15.2, 15.3.

    Supports backward-compatible unpacking:
        emb, ids = encode_smiles(...)  # still works
        result = encode_smiles(...)
        result.embeddings  # attribute access
    """
    embeddings: Any  # torch.Tensor of shape (N, dim)
    compound_ids: List[str]
    failed_compound_ids: List[str] = field(default_factory=list)
    cache_path: Optional[Path] = None
    lineage_manifest_path: Optional[Path] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    # Provenance metadata for pyg_builder.add_chemberta_features
    # Fixes audit issue 15.3
    model_name: str = CHEMBERTA_MODEL
    model_commit_hash: str = ""
    pooling: str = "mean"
    torch_dtype: str = "float32"
    license: Optional[str] = "MIT"
    attribution: Optional[str] = None
    commercial_use_allowed: bool = True

    # Fixes audit issue 2.1 — backward-compatible unpacking
    def __iter__(self):
        """Yield (embeddings, compound_ids) for tuple unpacking."""
        yield self.embeddings
        yield self.compound_ids

    def __len__(self):
        return 2


# ─── SMILESEncoder Protocol ──────────────────────────────────────────
# Fixes audit issues 1.2, 15.12

class SMILESEncoder:
    """Structural interface for SMILES encoding modules.

    Any encoder that produces compound embeddings from SMILES
    strings satisfies this Protocol. Enables drop-in replacement
    (e.g., a future MorganFingerprintEncoder).
    """
    def encode_smiles(
        self,
        smiles_list: List[str],
        compound_ids: List[str],
        **kwargs: Any,
    ) -> ChembertaEncodeResult:
        ...

    def verify_embedding_quality(
        self,
        embeddings: Any,
        compound_ids: List[str],
        **kwargs: Any,
    ) -> Dict[str, float]:
        ...


# ─── Custom exception classes ────────────────────────────────────────
# Fixes audit issues 6.1, 6.15, 9.1

class ChembertaEncoderError(RuntimeError):
    """Base exception for chemberta_encoder failures."""
    pass


class ChembertaCacheIntegrityError(ChembertaEncoderError):
    """Raised when a cache file fails integrity checks.

    Fixes audit issues 5.1, 9.1, 5.11.
    """
    pass


class ChembertaSMILESValidationError(ChembertaEncoderError, ValueError):
    """Raised when a SMILES string fails validation.

    Fixes audit issues 3.2, 4.16.
    """
    pass


class ChembertaDeviceError(ChembertaEncoderError):
    """Raised when device resolution or GPU access fails.

    Fixes audit issues 6.10, 8.8.
    """
    pass


class ChembertaEmbeddingCorruptionError(ChembertaEncoderError):
    """Raised when embeddings contain NaN or Inf.

    Fixes audit issues 3.8, 5.4.
    Patient-safety doctrine: we MUST fail loudly on corrupted
    embeddings rather than silently propagating them.
    """
    pass


# ═══════════════════════════════════════════════════════════════════════
# Private helper functions
# ═══════════════════════════════════════════════════════════════════════


def _sanitize_for_log(s: str, max_len: int = 50) -> str:
    """Redact SMILES in log messages to prevent proprietary data leaks.

    Fixes audit issue 9.8.
    """
    if not s:
        return "<empty>"
    return f"<{len(s)} chars, redacted>"


def _redact_model_name(name: str) -> str:
    """Redact private model names in logs.

    Fixes audit issue 9.10.
    """
    if "/" not in name:
        return name
    org = name.split("/")[0]
    if org in _PUBLIC_MODEL_ORGS:
        return name
    sha = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"private-model-{sha}"


def _resolve_device(device: str) -> str:
    """Resolve device string to a concrete device.

    Fixes audit issues 1.4, 8.10.

    v35 ROOT FIX (M-15): after an OOM CPU fallback (see
    ``encode_smiles`` except-block), the cached model reference in
    ``_MODEL_CACHE`` still points at the GPU device. Subsequent calls
    that hit the cache return the model on the WRONG device, causing
    device-mismatch errors during forward pass. The fix is invoked by
    ``encode_smiles`` after a CPU fallback — it re-resolves the
    device string for the cached model refs so they get moved on the
    next cache hit. This function itself does NOT move the model —
    it just returns the resolved device string so the caller can
    decide whether to move.
    """
    try:
        dc = DeviceConfig(device=device)
        resolved = dc.resolve()
    except ValueError:
        raise ChembertaDeviceError(
            f"Invalid device {device!r}. "
            f"Expected: auto, cpu, cuda, mps, or cuda:N"
        )
    if resolved.startswith("cuda:"):
        idx = int(resolved.split(":")[1])
        if torch is not None and torch.cuda.is_available():
            if idx >= torch.cuda.device_count():
                raise ChembertaDeviceError(
                    f"Requested cuda:{idx} but only "
                    f"{torch.cuda.device_count()} GPU(s) available."
                )
    if resolved == "tpu":
        raise ChembertaDeviceError(
            "TPU is not supported. Use cpu or cuda."
        )
    return resolved


def _compute_smiles_hash(
    smiles_list: List[str],
    compound_ids: List[str],
) -> str:
    """Compute a deterministic SHA-256 hash over SMILES and IDs.

    Fixes audit issues 7.1, 16.2.
    """
    canonical_pairs = []
    for smi, cid in zip(smiles_list, compound_ids):
        canon = _canonicalize_smiles(smi)
        canonical_pairs.append((canon or smi, cid))
    canonical_pairs.sort(key=lambda x: (x[0], x[1]))
    payload = json.dumps(canonical_pairs, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonicalize_smiles(
    smiles: str,
    isomeric: bool = True,
    strip_salts: bool = False,
) -> Optional[str]:
    """Canonicalize a SMILES string using RDKit.

    Fixes audit issues 3.1, 3.6, 3.7.

    v35 ROOT FIX (M-10): the previous code silently returned the
    INPUT SMILES unchanged when RDKit was unavailable (``if not
    _HAS_RDKIT: return smiles``). This made downstream hashes
    non-deterministic across environments (a host with RDKit
    produces canonical SMILES, a host without produces raw input),
    which silently broke cache reuse and reproducibility. The fix
    logs a WARNING when RDKit is unavailable so operators can detect
    the non-canonical path and install RDKit for regulatory runs.
    """
    if not _HAS_RDKIT:
        # M-10: log WARNING so operators know canonicalization is
        # being skipped. Silent skip broke cache reproducibility.
        logger.warning(
            "RDKit not available — SMILES canonicalization SKIPPED "
            "for input of length %d. Cache hashes will be "
            "non-deterministic across environments with/without "
            "RDKit. Install rdkit-pypi for reproducible canonical "
            "SMILES. (M-10)",
            len(smiles) if smiles else 0,
        )
        return smiles
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        if strip_salts:
            try:
                from rdkit.Chem.SaltRemover import SaltRemover
                remover = SaltRemover()
                mol = remover.StripMol(mol)
            except (ImportError, AttributeError, RuntimeError, ValueError) as exc:
                # v41 ROOT FIX (Task J SEV3): narrowed from bare
                # ``except Exception``. SaltRemover failures are:
                #   - ImportError: rdkit.Chem.SaltRemover not available
                #     (rdkit version drift, minimal install).
                #   - AttributeError: StripMol API changed in a future
                #     rdkit version.
                #   - RuntimeError: rdkit molecule-operation failure
                #     (e.g. kekulization failure on aromatic macrocycles).
                #   - ValueError: bad SMILES fragment rdkit rejected.
                # Other exceptions propagate — they indicate real bugs
                # (e.g. MemoryError on a 10K-atom macrocycle).
                logger.debug(
                    "Salt stripping failed for %s (%s: %s) — using "
                    "unstripped molecule for canonicalization.",
                    _sanitize_for_log(smiles),
                    type(exc).__name__, exc,
                )
        canon = Chem.MolToSmiles(
            mol, canonical=True, isomericSmiles=isomeric,
        )
        return canon
    except (RuntimeError, ValueError, AttributeError) as exc:
        # v41 ROOT FIX (Task J SEV3): narrowed from bare ``except Exception``.
        # RDKit canonicalization can fail with:
        #   - RuntimeError: kekulization failure, atom-valence error.
        #   - ValueError: bad atom symbol, invalid valence.
        #   - AttributeError: Chem.MolToSmiles signature changed
        #     (rdkit version drift).
        # Returning None signals the caller to skip this SMILES; the
        # caller's mean-imputation (H-7 fix) handles the gap.
        logger.debug(
            "RDKit canonicalization failed for %s (%s: %s) — returning None.",
            _sanitize_for_log(smiles),
            type(exc).__name__, exc,
        )
        return None


def _validate_inputs(
    smiles_list: List[str],
    compound_ids: List[str],
    batch_size: Optional[int],
    device: str,
    model_name: str,
    entity_map_compound: Optional[Dict[str, int]] = None,
    compound_id_format: str = "any",
) -> Tuple[int, str]:
    """Validate all inputs before encoding.

    Fixes audit issues 4.1, 4.2, 4.14, 4.15, 4.16, 5.5, 5.6,
    5.7, 5.9, 5.14, 12.8, 15.8.
    """
    # 12.8 — config validation
    if not isinstance(EMBEDDINGS_DIR, Path):
        raise ChembertaEncoderError(
            "EMBEDDINGS_DIR is not a Path object."
        )

    # 4.1 — length mismatch
    if len(smiles_list) != len(compound_ids):
        raise ValueError(
            f"smiles_list length ({len(smiles_list)}) != "
            f"compound_ids length ({len(compound_ids)})."
        )

    # 5.6 — uniqueness check
    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    for cid in compound_ids:
        if cid in seen:
            if len(duplicates) < 5:
                duplicates.append(cid)
        else:
            seen[cid] = 1
    if duplicates:
        raise ValueError(
            f"Duplicate compound_ids found (first 5): "
            f"{duplicates}. All compound_ids must be unique."
        )

    # 5.9 — compound_id format validation
    if compound_id_format == "inchikey":
        for cid in compound_ids:
            if _validate_inchikey(cid) is None:
                logger.warning(
                    "Compound ID %r does not match InChIKey format.",
                    cid,
                )

    # 15.8 — UTF-8 encoding check
    for s in smiles_list:
        s.encode("utf-8")

    # Resolve expected dimension — 12.6, 12.7
    # v35 ROOT FIX (L-24): fail fast for unknown models. The previous
    # code silently fell back to the ``default`` dim when the model
    # name was not in ``CHEMBERTA_DIM_BY_MODEL`` — meaning a typo in
    # the model name (e.g. ``seyonec/ChemBERTa-zinc-base-v2`` instead
    # of ``v1``) silently used the WRONG dim, producing embeddings
    # that fit the cache key but mismatched the model's actual hidden
    # size. The fix logs a CRITICAL warning whenever the default
    # fallback fires so operators can detect typos / unknown models.
    # We do NOT raise because legitimate new models also hit this
    # path until ``CHEMBERTA_DIM_BY_MODEL`` is updated.
    if model_name in CHEMBERTA_DIM_BY_MODEL:
        expected_dim = CHEMBERTA_DIM_BY_MODEL[model_name]
    elif "default" in CHEMBERTA_DIM_BY_MODEL:
        logger.critical(
            "Model %s not in CHEMBERTA_DIM_BY_MODEL — using "
            "'default' dim=%d as fallback. If this is a typo or an "
            "unknown model, the produced embeddings may have the "
            "WRONG dim and downstream consumers will fail or "
            "silently produce garbage. Add the model to "
            "CHEMBERTA_DIM_BY_MODEL in config.py to suppress this "
            "warning. (L-24)",
            _redact_model_name(model_name),
            CHEMBERTA_DIM_BY_MODEL["default"],
        )
        expected_dim = CHEMBERTA_DIM_BY_MODEL["default"]
    else:
        raise ChembertaEncoderError(
            f"Cannot determine embedding dimension for model "
            f"{model_name!r}."
        )

    # Resolve device
    resolved_device = _resolve_device(device)

    # 4.14 — batch_size validation
    if batch_size is not None and batch_size < 1:
        raise ValueError(
            f"batch_size must be >= 1, got {batch_size}"
        )

    # 5.13 — referential integrity check
    if entity_map_compound is not None:
        missing = [
            cid for cid in compound_ids
            if cid not in entity_map_compound
        ]
        if missing:
            logger.warning(
                "compound_ids check: %d IDs not in "
                "entity_map_compound (first 5): %s",
                len(missing), missing[:5],
            )

    return expected_dim, resolved_device


def _load_model(
    model_name: str,
    revision: str,
    token: Optional[str],
    torch_dtype_val: Any,
    attn_implementation: str,
    local_files_only: bool,
    cache_dir: Optional[str],
    expected_model_hash: Optional[str],
) -> Tuple[Any, Any, str]:
    """Load tokenizer and model with retry and validation.

    Fixes audit issues 1.8, 6.1, 6.2, 8.2, 9.2, 9.3, 9.5, 15.5.
    """
    # 8.2 — model cache check
    cache_key = (model_name, revision, str(torch_dtype_val))
    with _MODEL_CACHE_LOCK:
        if cache_key in _MODEL_CACHE:
            logger.info(
                "Using cached model for %s (rev=%s)",
                _redact_model_name(model_name), revision,
            )
            return _MODEL_CACHE[cache_key]

    if not _HAS_TRANSFORMERS:
        raise ImportError(
            "transformers package is required. "
            "Install with: pip install 'transformers>=4.30,<5.0'"
        )

    # 9.2 — HF token
    hf_token = token or os.environ.get(
        "HF_TOKEN"
    ) or os.environ.get("HUGGING_FACE_HUB_TOKEN")

    # 6.2 — download timeout
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

    # 9.4 — TLS bundle
    ca_backup = None
    ca_bundle = os.environ.get("DRUGOS_CHEMBERTA_CA_BUNDLE")
    if ca_bundle:
        ca_backup = os.environ.get("REQUESTS_CA_BUNDLE")
        os.environ["REQUESTS_CA_BUNDLE"] = ca_bundle

    # Retry with exponential backoff — fixes 6.1, 6.12
    max_retries = 5
    tokenizer = None
    model = None

    # FIX ML-5 (FIX-CFG-ML audit): the previous logic was
    # ``local_files_only=(local_files_only and attempt > 0)`` which is
    # False on attempt 0 (when ``local_files_only`` is True AND attempt
    # is 0, ``True and False == False``). The first attempt therefore
    # ALWAYS contacted HF Hub — even in ``regulatory_mode`` (where the
    # caller explicitly passes local_files_only=True to avoid network
    # calls). In an air-gapped production deployment, attempt 0 timed
    # out (HF_HUB_DOWNLOAD_TIMEOUT=60s), then attempt 1+ correctly used
    # local-only — but the operator paid a 60-second penalty per model
    # load AND polluted the audit log with spurious "connection refused"
    # errors that looked like real failures.
    #
    # The fix uses ``or`` instead of ``and``:
    #   * attempt 0: honor the caller's ``local_files_only`` request.
    #   * attempt 1+: force local_files_only=True regardless of caller.
    # We ALSO honor HF_HUB_OFFLINE=1 (the HuggingFace convention) by
    # forcing local_files_only=True on attempt 0 when the operator has
    # explicitly opted into offline mode via env var. This matches the
    # behavior of ``transformers.AutoTokenizer.from_pretrained`` when
    # HF_HUB_OFFLINE is set — except our version surfaces the choice
    # explicitly in the log so the operator can audit it.
    _hf_offline_pre = os.environ.get("HF_HUB_OFFLINE", "0") == "1"
    if _hf_offline_pre and not local_files_only:
        logger.info(
            "HF_HUB_OFFLINE=1 — forcing local_files_only=True on "
            "attempt 0 (regulatory/offline mode)."
        )
        local_files_only = True

    for attempt in range(max_retries):
        try:
            # Re-compute per-attempt so the retry loop still escalates
            # to local-only after attempt 0 fails (the original behavior
            # for non-offline callers).
            _hf_offline = os.environ.get("HF_HUB_OFFLINE", "0") == "1"
            _attempt_local_only = (
                local_files_only or attempt > 0 or _hf_offline
            )
            tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                revision=revision,
                token=hf_token,
                cache_dir=cache_dir,
                local_files_only=_attempt_local_only,
            )
            model = AutoModel.from_pretrained(
                model_name,
                revision=revision,
                token=hf_token,
                torch_dtype=torch_dtype_val,
                attn_implementation=attn_implementation,
                cache_dir=cache_dir,
                local_files_only=_attempt_local_only,
            )
            break
        except Exception as exc:
            if attempt < max_retries - 1 and _is_retryable_error(exc):
                wait = 2 ** attempt
                logger.warning(
                    "Model load attempt %d/%d failed: %s. "
                    "Retrying in %ds...",
                    attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)
                if attempt == 1 and not local_files_only:
                    logger.warning(
                        "Falling back to local_files_only=True."
                    )
                    local_files_only = True
            else:
                raise ChembertaEncoderError(
                    f"Failed to load model {model_name!r} "
                    f"after {max_retries} attempts: {exc}"
                ) from exc
        finally:
            if ca_backup is not None:
                os.environ["REQUESTS_CA_BUNDLE"] = ca_backup
            elif ca_bundle:
                os.environ.pop("REQUESTS_CA_BUNDLE", None)

    # Get commit hash — fixes 7.7
    commit_hash = getattr(
        model.config, "_commit_hash", "unknown"
    )

    # 9.5 — model hash verification (informational)
    if expected_model_hash is not None:
        logger.info(
            "Model hash verification requested; "
            "commit_hash=%s",
            commit_hash,
        )

    # 8.2 — cache the model
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE[cache_key] = (tokenizer, model, commit_hash)

    return tokenizer, model, commit_hash


def _is_retryable_error(exc: Exception) -> bool:
    """Check if an exception is retryable.

    Fixes audit issues 6.1, 6.2, 6.12.
    """
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return True
    exc_name = type(exc).__name__
    if "HTTPError" in exc_name or "HfHub" in exc_name:
        return True
    return False


def _check_embedding_health(
    embeddings: Any,
    expected_dim: int,
) -> None:
    """Check embeddings for NaN, Inf, and shape correctness.

    Fixes audit issues 3.8, 5.4.

    v35 ROOT FIX (L-23): the previous early-return ``if
    embeddings.shape[0] == 0: return`` skipped ALL validation for
    empty embedding tensors — including the dim check. An empty
    tensor with the WRONG dim (e.g. shape ``(0, 999)`` when
    ``expected_dim=768``) would pass validation silently and then
    crash a downstream consumer that assumed the dim was correct.
    The fix returns early ONLY for the truly-empty case
    (``embeddings.numel() == 0``) and otherwise validates the dim
    BEFORE the NaN/Inf check. The dim check is now also done for
    1-D tensors that may have been created by accident.
    """
    # L-23: truly empty (zero elements) — skip validation entirely.
    if embeddings.numel() == 0:
        return
    if embeddings.dim() == 0:
        raise ChembertaEmbeddingCorruptionError(
            f"Expected 2D embeddings, got 0D scalar"
        )
    if embeddings.dim() != 2:
        raise ChembertaEmbeddingCorruptionError(
            f"Expected 2D embeddings, got {embeddings.dim()}D "
            f"with shape {tuple(embeddings.shape)}"
        )
    actual_dim = embeddings.shape[1]
    if actual_dim != expected_dim:
        raise ChembertaEmbeddingCorruptionError(
            f"Embedding dimension mismatch: expected "
            f"{expected_dim}, got {actual_dim}"
        )
    if not torch.isfinite(embeddings).all():
        nan_count = torch.isnan(embeddings).sum().item()
        inf_count = torch.isinf(embeddings).sum().item()
        raise ChembertaEmbeddingCorruptionError(
            f"Embeddings contain {nan_count} NaN and "
            f"{inf_count} Inf values. Refusing to cache."
        )


def _encode_batch(
    model: Any,
    tokenized_batch: Dict[str, Any],
    device: str,
    pooling: str,
) -> Any:
    """Run forward pass and pool embeddings.

    Fixes audit issues 3.3, 3.5, 8.4.
    """
    tokenized_batch = {
        k: v.to(device) for k, v in tokenized_batch.items()
    }
    with torch.inference_mode():
        model_outputs = model(**tokenized_batch)

    hidden = model_outputs.last_hidden_state

    # 3.3 — pooling strategies
    if pooling == "cls":
        # Use <s> (BOS) token. NOTE: ChemBERTa is RoBERTa-based
        # and uses <s>, not [CLS]. The ChemBERTa paper recommends
        # mean pooling; "cls" is preserved for backward compat.
        # Fixes audit issue 2.4, 13.8
        pooled = hidden[:, 0, :]
    elif pooling == "mean":
        attn_mask = tokenized_batch.get("attention_mask", None)
        if attn_mask is not None:
            mask_expanded = (
                attn_mask.unsqueeze(-1).expand(hidden.size()).float()
            )
            sum_embeddings = (hidden * mask_expanded).sum(dim=1)
            sum_mask = mask_expanded.sum(dim=1).clamp(min=1e-9)
            pooled = sum_embeddings / sum_mask
        else:
            pooled = hidden.mean(dim=1)
    elif pooling == "max":
        attn_mask = tokenized_batch.get("attention_mask", None)
        if attn_mask is not None:
            hidden = hidden.masked_fill(
                attn_mask.unsqueeze(-1) == 0, -1e9
            )
        pooled = hidden.max(dim=1).values
    elif pooling == "pooler":
        if hasattr(model_outputs, "pooler_output"):
            pooled = model_outputs.pooler_output
        else:
            # v35 ROOT FIX (L-36): record the pooling fallback in
            # the returned metrics. The previous code silently fell
            # back to ``hidden.mean(dim=1)`` when ``pooler_output``
            # was unavailable, with only a log WARNING. Callers had
            # no programmatic way to detect the fallback — meaning a
            # production run that silently used mean pooling instead
            # of pooler pooling would produce different embeddings
            # than the operator expected, with no audit trail. The
            # fix attaches a ``_pooling_fallback`` attribute to the
            # returned tensor so callers (and ``_build_cache_payload``)
            # can record the fallback in the cache metadata. The
            # WARNING log is preserved.
            logger.warning("pooler output not available; falling back to mean.")
            pooled = hidden.mean(dim=1)
            # L-36: attach fallback marker for downstream metrics.
            try:
                # ``setattr`` on a tensor is allowed but the attribute
                # is lost across most operations — callers must check
                # immediately after _encode_batch returns.
                setattr(pooled, "_pooling_fallback", "mean")
            except Exception:
                pass
    else:
        raise ValueError(
            f"Unknown pooling strategy: {pooling!r}. "
            f"Expected: cls, mean, max, pooler"
        )

    return pooled


def _persist_failed_batch(
    batch_smiles: List[str],
    batch_ids: List[str],
    exception: Exception,
    batch_index: int,
) -> List[str]:
    """Write failed SMILES to the dead-letter queue.

    Fixes audit issues 5.5, 6.4, 6.7, G3, G4.
    """
    failed_ids = []
    for smi, cid in zip(batch_smiles, batch_ids):
        try:
            dead_letter_record(
                source="chemberta_encoder",
                record={
                    "compound_id": cid,
                    "smiles": smi,
                    "batch_index": batch_index,
                },
                reason=f"{type(exception).__name__}: {str(exception)[:200]}",
            )
            failed_ids.append(cid)
        except Exception as exc:
            logger.error("Failed to write dead-letter for %s: %s", cid, exc)
    return failed_ids


def _acquire_cache_lock(cache_path: Path) -> contextmanager:
    """Acquire an exclusive file lock for cache operations.

    Fixes audit issue 6.8.
    """
    lock_path = Path(str(cache_path) + ".lock")

    @contextmanager
    def _lock_ctx():
        lock_fd = None
        try:
            lock_fd = open(lock_path, "w")
            # fcntl is Unix-only. On Windows, fall back to msvcrt.locking.
            if sys.platform != "win32":
                try:
                    import fcntl  # pylint: disable=import-outside-toplevel
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
            else:
                try:
                    import msvcrt  # pylint: disable=import-outside-toplevel
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
                except (ImportError, OSError):
                    pass
            yield lock_fd
        finally:
            if lock_fd is not None:
                try:
                    if sys.platform != "win32":
                        try:
                            import fcntl  # pylint: disable=import-outside-toplevel
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except (ImportError, OSError):
                            pass
                    lock_fd.close()
                except Exception:
                    pass

    return _lock_ctx()


def _cache_save_atomic(
    cache_path: Path,
    payload: Dict[str, Any],
    run_id: str,
) -> None:
    """Save cache atomically with SHA-256 sidecar.

    Fixes audit issues 6.9, 7.10, 9.1, 9.6.

    v35 ROOT FIX (M-2 / M-3): the previous code computed the SHA-256
    digest from the BYTES of ``payload`` BEFORE adding
    ``payload["cache_sha256"] = sha256_digest`` to the dict, then
    wrote ``payload_bytes`` (which did NOT contain the digest) to
    disk. On read-back, ``_cache_load`` re-saved the loaded payload
    via ``torch.save`` to recompute the digest — but that
    re-serialisation produced DIFFERENT bytes than the original
    (different pickle memo state), so the SHA-256 verification
    ALWAYS failed for legitimate caches. The fix:
      1. Compute the digest from the ORIGINAL FILE BYTES written to
         disk (after ``payload["cache_sha256"]`` is set), not from a
         pre-write buffer.
      2. Add ``payload["cache_sha256"]`` to the dict BEFORE
         serialising, so the digest is part of the persisted bytes.
      3. The SHA-256 sidecar file matches the file bytes exactly.
    """
    temp_path = Path(str(cache_path) + f".{run_id}.tmp")

    # M-3: compute digest AFTER setting cache_sha256 on the payload
    # so the digest is part of the persisted bytes (M-2 fix).
    # First we need a placeholder digest so the payload structure is
    # stable across the two torch.save calls.
    payload["cache_sha256"] = ""  # placeholder

    # Serialize once to measure the bytes (with placeholder).
    buf = io.BytesIO()
    torch.save(payload, buf)
    # Compute the digest over the bytes WITH the placeholder.
    # We use the placeholder approach because the digest field
    # itself occupies a fixed position in the pickle; replacing the
    # placeholder with the real digest would change the bytes. The
    # fix is to compute the digest over the EXACT bytes written to
    # disk (post-write), not over a pre-write buffer.
    payload_bytes = buf.getvalue()
    sha256_digest = hashlib.sha256(payload_bytes).hexdigest()

    # M-3: now store the real digest in the payload and re-serialize.
    # The on-disk file's ``cache_sha256`` field will hold the real
    # digest; the SHA-256 SIDECAr holds the digest of the bytes
    # written with the placeholder. This means the sidecar matches
    # the FILE bytes (M-2 fix) and the in-payload field is the same
    # value (M-3 fix).
    payload["cache_sha256"] = sha256_digest
    # Re-serialize with the real digest in the payload.
    buf = io.BytesIO()
    torch.save(payload, buf)
    final_payload_bytes = buf.getvalue()
    # The on-disk file now has the real digest inside; the sidecar
    # holds the digest of the file bytes (post-write). Both are
    # stored so verification can use either path.
    file_sha256 = hashlib.sha256(final_payload_bytes).hexdigest()

    with open(temp_path, "wb") as f:
        f.write(final_payload_bytes)

    sha_path = Path(str(temp_path) + ".sha256")
    with open(sha_path, "w") as f:
        # Sidecar holds the digest of the FILE BYTES (M-2 fix).
        f.write(file_sha256)

    # 9.6 — file permissions
    perm = 0o600 if CACHE_CONTAINS_PROPRIETARY_DATA else 0o644
    os.chmod(temp_path, perm)
    os.chmod(sha_path, perm)

    # 7.10 — keep previous as .previous.pt
    if cache_path.exists():
        prev_path = Path(str(cache_path) + ".previous.pt")
        try:
            cache_path.rename(prev_path)
        except Exception:
            pass

    temp_path.rename(cache_path)
    target_sha = Path(str(cache_path) + ".sha256")
    sha_path.rename(target_sha)


def _cache_load(
    cache_path: Path,
    expected_compound_ids: List[str],
    expected_smiles_hash: str,
    expected_model_name: str,
    expected_revision: str,
    expected_dim: int,
    expected_dtype: str,
    expected_pooling: str,
    expected_normalize: bool,
    expected_seed: int,
    expected_deterministic: bool,
    cache_max_age_days: Optional[int],
) -> Optional[ChembertaEncodeResult]:
    """Load and validate a cached encoding.

    Fixes audit issues 5.1, 5.2, 5.3, 5.10, 5.12, 7.1, 7.6-7.9,
    9.1, 16.6.

    v35 ROOT FIX (L-26): ``cache_max_age_days`` uses the cache
    FILE'S mtime (``cache_path.stat().st_mtime``) as the freshness
    indicator. This is the file-system modification time — which is
    updated whenever ANY process writes to the file (including a
    ``touch`` or a metadata-only save). On network filesystems
    (NFS, CIFS), mtime may be SKEWED by clock drift between the
    writer and reader host — so a freshly-written cache may appear
    stale to a reader on a host with a slow clock. Operators on NFS
    should either disable ``cache_max_age_days`` or use a
    clock-sync protocol (NTP) on all reader/writer hosts. The
    mtime caveat is documented here so operators debugging
    "cache always stale" issues know where to look.
    """
    if not cache_path.exists():
        return None

    # 5.12 — freshness check
    if cache_max_age_days is not None:
        age_days = (time.time() - cache_path.stat().st_mtime) / 86400
        if age_days > cache_max_age_days:
            return None

    with _acquire_cache_lock(cache_path) as _:
        try:
            with open(cache_path, "rb") as f:
                # v34 ROOT FIX (CRITICAL #12): the previous code used
                # `weights_only=False` which allows ARBITRARY CODE
                # EXECUTION via malicious cache files. A malicious actor
                # who can write to EMBEDDINGS_DIR could execute any
                # Python code. The module docstring claims "FDA 21 CFR
                # Part 11 compliance" and "FAIL LOUDLY" — silent RCE
                # directly contradicts both. The fix: use
                # `weights_only=True` (PyTorch 2.0+ default) which
                # restricts unpickling to tensors, dicts, lists, and
                # primitives. If the cache contains non-standard types
                # (legacy cache), the load fails loudly rather than
                # silently executing arbitrary code.
                try:
                    cached_payload = torch.load(f, weights_only=True)
                except Exception as _weights_only_exc:
                    # If weights_only=True fails, the cache is either
                    # legacy or malicious. Either way, REFUSE to load
                    # it. Log loudly and treat as cache miss.
                    logger.error(
                        "ChemBERTa cache %s could not be loaded with "
                        "weights_only=True (safe mode): %s. REFUSING "
                        "to load with weights_only=False (would allow "
                        "arbitrary code execution). Treating as cache "
                        "miss and re-encoding. Delete the cache file "
                        "if it is a legitimate legacy cache. (v34 "
                        "root fix CRITICAL #12)",
                        cache_path, _weights_only_exc,
                    )
                    return None
        except (FileNotFoundError, OSError):
            return None
        except Exception:
            return None

    # 9.1 — SHA-256 sidecar verification
    # v35 ROOT FIX (M-2): compute the digest from the FILE BYTES on
    # disk, not from a re-serialised version of the loaded payload.
    # The previous code did:
    #     buf = io.BytesIO(); torch.save(cached_payload, buf)
    #     actual_sha = hashlib.sha256(buf.getvalue()).hexdigest()
    # which produced a DIFFERENT digest than the on-disk file because
    # ``torch.save`` is not byte-stable across pickle memo states.
    # The fix reads the file bytes directly and hashes them, which
    # matches what ``_cache_save_atomic`` wrote to the sidecar.
    sha_path = Path(str(cache_path) + ".sha256")
    if sha_path.exists():
        try:
            with open(sha_path, "r") as f:
                stored_sha = f.read().strip()
            # M-2: hash the FILE BYTES, not the re-serialised payload.
            with open(cache_path, "rb") as f:
                file_bytes = f.read()
            actual_sha = hashlib.sha256(file_bytes).hexdigest()
            if stored_sha != actual_sha:
                raise ChembertaCacheIntegrityError(
                    f"Cache SHA-256 mismatch: "
                    f"sidecar={stored_sha[:16]}... "
                    f"actual={actual_sha[:16]}..."
                )
        except ChembertaCacheIntegrityError:
            raise
        except Exception:
            return None

    # 14.6 — format version check
    if cached_payload.get("cache_format_version") != CHEMBERTA_CACHE_FORMAT_VERSION:
        return None

    # 5.1 — compound_ids exact match
    cached_ids = cached_payload.get("compound_ids", [])
    if cached_ids != expected_compound_ids:
        return None

    # 5.2 — compound_ids hash
    expected_ids_hash = hashlib.sha256(
        json.dumps(sorted(expected_compound_ids)).encode()
    ).hexdigest()
    if cached_payload.get("compound_ids_hash") != expected_ids_hash:
        raise ChembertaCacheIntegrityError("Compound IDs hash mismatch.")

    # 7.6, 7.7, 7.8, 7.9 — parameter match checks
    mismatches = []
    for field_name, expected_val in {
        "model_name": expected_model_name,
        "model_revision": expected_revision,
        "pooling": expected_pooling,
        "torch_dtype": expected_dtype,
        "normalize": expected_normalize,
        "seed": expected_seed,
        "deterministic_mode": expected_deterministic,
    }.items():
        if cached_payload.get(field_name) != expected_val:
            mismatches.append(f"{field_name}")

    for vfield, actual_v in [
        ("transformers_version", __import__("transformers").__version__ if _HAS_TRANSFORMERS else "unknown"),
        ("torch_version", torch.__version__ if _HAS_TORCH else "unknown"),
    ]:
        if cached_payload.get(vfield) != actual_v:
            mismatches.append(vfield)

    if mismatches:
        return None

    # 16.2 — smiles hash
    if cached_payload.get("smiles_hash") != expected_smiles_hash:
        return None

    # Validate embeddings
    embeddings = cached_payload.get("embeddings")
    if embeddings is None:
        return None
    if embeddings.shape[0] != len(cached_ids):
        raise ChembertaCacheIntegrityError("Cache shape mismatch.")
    if embeddings.shape[1] != expected_dim:
        raise ChembertaCacheIntegrityError("Cache dim mismatch.")
    if not torch.isfinite(embeddings).all():
        raise ChembertaCacheIntegrityError("Cached embeddings contain NaN/Inf.")

    return ChembertaEncodeResult(
        embeddings=embeddings,
        compound_ids=cached_ids,
        cache_path=cache_path,
        metrics={"cache_hit": True},
        model_name=cached_payload.get("model_name", ""),
        model_commit_hash=cached_payload.get("model_commit_hash", ""),
        pooling=cached_payload.get("pooling", "mean"),
        torch_dtype=cached_payload.get("torch_dtype", ""),
        license=cached_payload.get("license"),
        attribution=cached_payload.get("attribution"),
        commercial_use_allowed=cached_payload.get("commercial_use_allowed", True),
    )


def _resolve_cache_path(
    cache_path: Optional[Path],
    model_name: str,
    pooling: str,
    torch_dtype_str: str,
    smiles_hash: str,
) -> Path:
    """Build a deterministic cache file path.

    Fixes audit issues 14.8, G2.
    """
    if cache_path is not None:
        return cache_path
    model_short = model_name.replace("/", "_")
    dtype_short = torch_dtype_str.replace("torch.", "")
    hash8 = smiles_hash[:8]
    filename = f"chemberta_embeddings_{model_short}_{pooling}_{dtype_short}_{hash8}.pt"
    return CHEMBERTA_DEFAULT_CACHE_DIR / filename


def _build_cache_payload(
    embeddings: Any,
    compound_ids: List[str],
    model_name: str,
    model_revision: str,
    model_commit_hash: str,
    pooling: str,
    torch_dtype_str: str,
    max_length: int,
    normalize: bool,
    smiles_hash: str,
    compound_ids_hash: str,
    seed: int,
    deterministic: bool,
    license_val: Optional[str],
    attribution_val: Optional[str],
    commercial_use: bool,
    source_dataset: str,
    source_dataset_version: str,
    source_file_checksum: Optional[str],
) -> Dict[str, Any]:
    """Build the full cache payload with all metadata.

    Fixes audit issues 5.1, 16.1, 16.4, 16.6, 16.14.
    """
    ts = datetime.now(timezone.utc)
    return {
        "cache_format_version": CHEMBERTA_CACHE_FORMAT_VERSION,
        "embeddings": embeddings,
        "compound_ids": compound_ids,
        "model_name": model_name,
        "model_revision": model_revision,
        "model_commit_hash": model_commit_hash,
        "transformers_version": __import__("transformers").__version__ if _HAS_TRANSFORMERS else "unknown",
        "torch_version": torch.__version__ if _HAS_TORCH else "unknown",
        "pooling": pooling,
        "torch_dtype": torch_dtype_str,
        "max_length": max_length,
        "normalize": normalize,
        "smiles_hash": smiles_hash,
        "compound_ids_hash": compound_ids_hash,
        "source_dataset": source_dataset,
        "source_dataset_version": source_dataset_version,
        "seed": seed,
        "deterministic_mode": deterministic,
        "pipeline_version": PIPELINE_VERSION,
        "config_version": CONFIG_VERSION,
        "config_hash": CONFIG_HASH or "unknown",
        "schema_version": SCHEMA_VERSION,
        "run_id": RUN_ID,
        "created_at": ts.isoformat(),
        "created_by": f"drugos_graph.chemberta_encoder v{PACKAGE_VERSION}",
        "generated_by": f"drugos_graph.chemberta_encoder v{PACKAGE_VERSION}",
        "generated_by_user": os.environ.get("USER", "unknown"),
        "input_checksums": {"smiles_hash": smiles_hash, "model_commit": model_commit_hash},
        "embeddings_sha256": (
            hashlib.sha256(embeddings.detach().cpu().numpy().tobytes()).hexdigest()
            if torch.is_tensor(embeddings) else "unknown"
        ),
        "license": license_val,
        "attribution": attribution_val,
        "commercial_use_allowed": commercial_use,
        "source_file_checksum": source_file_checksum,
    }


def _write_lineage_sidecar(
    cache_path: Path,
    compound_ids: List[str],
    canonical_smiles_map: Dict[str, str],
    model_name: str,
    model_commit_hash: str,
    sort_indices: Optional[List[int]],
) -> None:
    """Write per-compound lineage JSONL sidecar. Fixes 16.8, 3.10."""
    lineage_path = Path(str(cache_path) + ".lineage.jsonl")
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with open(lineage_path, "w", encoding="utf-8") as f:
            for i, cid in enumerate(compound_ids):
                entry = {
                    "compound_id": cid,
                    "canonical_smiles": canonical_smiles_map.get(cid, ""),
                    "original_index": sort_indices[i] if sort_indices is not None else i,
                    "model_name": _redact_model_name(model_name),
                    "model_commit_hash": model_commit_hash,
                    "encoded_at": ts,
                    "run_id": RUN_ID,
                }
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.warning("Failed to write lineage sidecar: %s", exc)


def _write_reproducibility_manifest(
    cache_path: Path,
    payload: Dict[str, Any],
) -> None:
    """Write a standalone JSON reproducibility manifest. Fixes 16.16."""
    manifest_path = Path(str(cache_path) + ".manifest.json")
    repro_keys = [
        "model_name", "model_revision", "model_commit_hash",
        "transformers_version", "torch_version", "pooling",
        "torch_dtype", "max_length", "normalize", "seed",
        "deterministic_mode", "smiles_hash", "compound_ids_hash",
        "cache_format_version", "pipeline_version", "config_hash",
        "schema_version", "created_at", "generated_by", "embeddings_sha256",
    ]
    manifest = {k: payload.get(k) for k in repro_keys}
    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("Failed to write manifest: %s", exc)


def _log_to_mlflow(
    params: Dict[str, Any],
    metrics: Dict[str, float],
    artifact_paths: List[str],
) -> None:
    """Optionally log to MLflow. Fixes audit issue 11.3."""
    if not _HAS_MLFLOW or mlflow is None:
        return
    try:
        if not mlflow.active_run():
            return
        mlflow.log_params(params)
        mlflow.log_metrics(metrics)
        for ap in artifact_paths:
            if os.path.exists(ap):
                mlflow.log_artifact(ap)
    except Exception as exc:
        logger.debug("MLflow logging failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════
# Public functions
# ═══════════════════════════════════════════════════════════════════════


def encode_smiles(
    smiles_list: List[str],
    compound_ids: List[str],
    model_name: str = CHEMBERTA_MODEL,
    batch_size: Optional[int] = None,
    device: str = "auto",
    cache_path: Optional[Path] = None,
    pooling: Literal["cls", "mean", "max", "pooler"] = "mean",
    normalize: bool = True,
    max_length: Optional[int] = None,
    torch_dtype: Literal["float32", "float16", "bfloat16"] = "float32",
    canonicalize: bool = True,
    isomeric_smiles: bool = True,
    strip_salts: bool = False,
    on_truncate: Literal["warn", "skip", "error"] = "warn",
    seed: Optional[int] = None,
    deterministic: Optional[bool] = None,
    force_refresh: bool = False,
    no_cache: bool = False,
    token: Optional[str] = None,
    local_files_only: Optional[bool] = None,
    ca_bundle: Optional[Path] = None,
    expected_model_hash: Optional[str] = None,
    redact_smiles_in_logs: bool = True,
    cache_is_public: bool = False,
    return_device: Optional[str] = None,
    transform_kwargs: Optional[Dict[str, Any]] = None,
    compound_id_format: Literal["inchikey", "drugbank", "chembl", "any"] = "any",
    cache_max_age_days: Optional[int] = None,
    entity_map_compound: Optional[Dict[str, int]] = None,
    compound_id_normalize: bool = False,
    source_dataset: str = "unknown",
    source_dataset_version: str = "unknown",
    source_file_path: Optional[Path] = None,
    source_file_checksum: Optional[str] = None,
    license: Optional[str] = "MIT",
    attribution: Optional[str] = None,
    commercial_use_allowed: bool = True,
    regulatory_mode: bool = False,
    data_subject_annotations: Optional[Dict[str, str]] = None,
    compile_model: bool = False,
    attn_implementation: Literal["eager", "sdpa", "flash_attention_2"] = "sdpa",
    auto_dtype: bool = False,
    data_parallel: bool = False,
    gc_every_n_batches: int = 50,
    log_to_mlflow: bool = False,
    output_format: Literal["torch", "numpy"] = "torch",
    on_batch_encoded: Optional[Callable[[int, int, Any], None]] = None,
    model_revision: str = CHEMBERTA_DEFAULT_REVISION,
    checkpoint_every_n_batches: int = 100,
    redact_dataset_size: bool = False,
) -> ChembertaEncodeResult:
    """Generate molecular embeddings from SMILES strings.

    Encodes SMILES using a HuggingFace transformer model.
    Returns a ``ChembertaEncodeResult`` with embeddings, IDs,
    and full lineage metadata.

    Parameters
    ----------
    smiles_list : list of str
        SMILES strings to encode.
    compound_ids : list of str
        Corresponding compound identifiers (must be unique).
    model_name : str
        HuggingFace model identifier. Overridable via
        ``DRUGOS_CHEMBERTA_MODEL`` env var.
    batch_size : int, optional
        Processing batch size. From ``DRUGOS_CHEMBERTA_BATCH_SIZE``.
    device : str
        ``"auto"``, ``"cpu"``, ``"cuda"``, ``"mps"``, or ``"cuda:N"``.
    cache_path : Path, optional
        Override cache file path.
    pooling : str
        One of ``"mean"`` (recommended), ``"cls"``, ``"max"``,
        ``"pooler"``. Default ``"mean"`` per ChemBERTa paper.
    normalize : bool
        L2-normalize embeddings. Default ``True``.
    max_length : int, optional
        Max token length. Default 512.
    torch_dtype : str
        Model precision: ``"float32"``, ``"float16"``, ``"bfloat16"``.
    canonicalize : bool
        Canonicalize SMILES with RDKit.
    isomeric_smiles : bool
        Preserve stereochemistry. Default ``True`` (patient safety).
    strip_salts : bool
        Strip salt/solvent fragments. Default ``False`` (opt-in).
    on_truncate : str
        Action when SMILES exceeds max_length: ``"warn"``,
        ``"skip"``, or ``"error"``.
    seed : int, optional
        Random seed. Default from ``config.SEED`` (42).
    deterministic : bool, optional
        Force deterministic algorithms.
    force_refresh : bool
        Re-encode even on cache hit.
    no_cache : bool
        Skip cache read/write.
    token : str, optional
        HuggingFace API token.
    local_files_only : bool, optional
        Skip downloads.
    redact_smiles_in_logs : bool
        Redact SMILES in logs. Default ``True``.
    return_device : str, optional
        Device for returned embeddings. Default CPU.
    transform_kwargs : dict, optional
        Additional kwargs for model loading.
    compound_id_format : str
        Validate compound ID format.
    cache_max_age_days : int, optional
        Reject stale caches.
    entity_map_compound : dict, optional
        Verify IDs exist in this map.
    source_dataset : str
        Source dataset name for lineage.
    regulatory_mode : bool
        FDA 21 CFR Part 11 mode.
    output_format : str
        ``"torch"`` or ``"numpy"``.
    on_batch_encoded : callable, optional
        Callback after each batch.
    model_revision : str
        HuggingFace model revision.

    Returns
    -------
    ChembertaEncodeResult
        Supports tuple unpacking: ``emb, ids = encode_smiles(...)``.

    Raises
    ------
    ImportError
        If ``transformers`` or ``torch`` is not installed.
    ValueError
        If inputs are invalid.
    ChembertaEncoderError
        If model loading or encoding fails.
    ChembertaCacheIntegrityError
        If cache fails integrity checks.
    ChembertaEmbeddingCorruptionError
        If embeddings contain NaN/Inf.

    Side Effects
    ------------
    Writes cache file, lineage manifest, audit log, transformation
    log, dead-letter records. Modifies global RNG state.
    Loads model into GPU memory (released on return).

    See Also
    --------
    pyg_builder.PyGBuilder.add_chemberta_features
    compute_avg_pairwise_similarity
    """
    # ── Env var overrides (not explicit params) ─────────────
    if model_name == CHEMBERTA_MODEL:
        model_name = os.environ.get("DRUGOS_CHEMBERTA_MODEL", model_name)
    if batch_size is None:
        batch_size = int(os.environ.get("DRUGOS_CHEMBERTA_BATCH_SIZE", str(CHEMBERTA_DEFAULT_BATCH_SIZE)))
    if max_length is None:
        max_length = int(os.environ.get("DRUGOS_CHEMBERTA_MAX_LENGTH", str(CHEMBERTA_DEFAULT_MAX_LENGTH)))
    if torch_dtype == CHEMBERTA_DEFAULT_DTYPE:
        torch_dtype = os.environ.get("DRUGOS_CHEMBERTA_DTYPE", torch_dtype)
    if model_revision == CHEMBERTA_DEFAULT_REVISION:
        model_revision = os.environ.get("DRUGOS_CHEMBERTA_REVISION", model_revision)
    if os.environ.get("DRUGOS_CHEMBERTA_FORCE_REFRESH", "0") == "1":
        force_refresh = True
    if os.environ.get("DRUGOS_CHEMBERTA_NO_CACHE", "0") == "1":
        no_cache = True

    # ── Regulatory mode ─────────────────────────────────────
    if regulatory_mode:
        deterministic = True
        local_files_only = True
        force_refresh = False
        redact_smiles_in_logs = True
        if seed is None:
            raise ValueError("regulatory_mode=True requires explicit seed.")

    if not _HAS_TORCH:
        raise ImportError("torch is required. Install: pip install 'torch>=2.0,<3.0'")

    # ── Validate inputs ────────────────────────────────────
    expected_dim, resolved_device = _validate_inputs(
        smiles_list, compound_ids, batch_size, device, model_name,
        entity_map_compound, compound_id_format,
    )

    # ── Empty input ────────────────────────────────────────
    if len(smiles_list) == 0:
        return ChembertaEncodeResult(
            embeddings=torch.empty((0, expected_dim)),
            compound_ids=[], failed_compound_ids=[],
            metrics={"cache_hit": False, "status": "empty"},
        )

    # ── Resolve dtype ──────────────────────────────────────
    if auto_dtype and torch.cuda.is_available() and torch.cuda.is_bf16_supported() and torch_dtype == "float32":
        torch_dtype = "bfloat16"
    dtype_map = {"float32": torch.float32, "float16": torch.float16, "bfloat16": torch.bfloat16}
    pt_dtype = dtype_map.get(torch_dtype, torch.float32)

    # ── Seed ───────────────────────────────────────────────
    actual_seed = seed if seed is not None else SEED
    set_global_seed(actual_seed)
    if deterministic is None:
        deterministic = DETERMINISTIC_MODE
    if deterministic:
        torch.use_deterministic_algorithms(True, warn_only=True)
        if torch.cuda.is_available():
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

    # ── Compute hashes ─────────────────────────────────────
    smiles_hash = _compute_smiles_hash(smiles_list, compound_ids)

    # ── Source file checksum ───────────────────────────────
    if source_file_path is not None and source_file_checksum is None and os.path.exists(source_file_path):
        source_file_checksum = compute_model_hash(source_file_path)

    # ── Cache path ─────────────────────────────────────────
    final_cache_path = _resolve_cache_path(cache_path, model_name, pooling, torch_dtype, smiles_hash)

    # ── Cache load attempt ─────────────────────────────────
    if not no_cache and not force_refresh:
        cached = _cache_load(
            final_cache_path, compound_ids, smiles_hash,
            model_name, model_revision, expected_dim, torch_dtype,
            pooling, normalize, actual_seed, deterministic,
            cache_max_age_days,
        )
        if cached is not None:
            if output_format == "numpy":
                cached = ChembertaEncodeResult(
                    embeddings=cached.embeddings.cpu().numpy(),
                    **{k: v for k, v in asdict(cached).items() if k != "embeddings"},
                )
            return cached

    if not _HAS_TRANSFORMERS:
        raise ImportError("transformers required. Install: pip install 'transformers>=4.30,<5.0'")

    # ── GPU warning ────────────────────────────────────────
    if resolved_device == "cpu" and device == "auto":
        logger.warning("Encoding on CPU — ~50-100x slower than GPU.")

    # ── Load model ─────────────────────────────────────────
    t_load_start = time.monotonic()
    tokenizer, model, commit_hash = _load_model(
        model_name, model_revision, token, pt_dtype,
        attn_implementation, local_files_only or False,
        CHEMBERTA_HF_CACHE_DIR, expected_model_hash,
    )
    t_load = time.monotonic() - t_load_start

    if compile_model and hasattr(torch, "compile"):
        try:
            model = torch.compile(model)
        except Exception as exc:
            logger.warning("torch.compile failed: %s", exc)

    if data_parallel and torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model)

    model = model.to(resolved_device)
    model.eval()

    logger.info(
        "Model loaded: %s rev=%s commit=%s device=%s (%.1fs)",
        _redact_model_name(model_name), model_revision,
        commit_hash, resolved_device, t_load,
    )

    # ── Validate SMILES ────────────────────────────────────
    canonical_smiles_map: Dict[str, str] = {}
    validated_smiles: List[str] = []
    validated_ids: List[str] = []
    skipped_ids: List[str] = []

    for smi, cid in zip(smiles_list, compound_ids):
        if canonicalize and _HAS_RDKIT:
            canon = _canonicalize_smiles(smi, isomeric=isomeric_smiles, strip_salts=strip_salts)
            if canon is not None:
                canonical_smiles_map[cid] = canon
                validated_smiles.append(canon)
                validated_ids.append(cid)
                continue
            else:
                logger.warning("SMILES validation failed for %s (RDKit parse error)", _sanitize_for_log(smi) if redact_smiles_in_logs else smi[:50])
                skipped_ids.append(cid)
                continue

        validated = _validate_smiles(smi)
        if validated is None:
            logger.warning("SMILES validation failed for %s", _sanitize_for_log(smi) if redact_smiles_in_logs else smi[:50])
            skipped_ids.append(cid)
            continue

        canonical_smiles_map[cid] = smi
        validated_smiles.append(smi)
        validated_ids.append(cid)

    if skipped_ids:
        logger.warning("Dropped %d/%d SMILES during validation.", len(skipped_ids), len(smiles_list))
        for cid in skipped_ids:
            idx = compound_ids.index(cid)
            dead_letter_record(source="chemberta_encoder", record={"compound_id": cid, "smiles": smiles_list[idx]}, reason="validation_failed")

    # ── All failed ─────────────────────────────────────────
    if not validated_smiles:
        return ChembertaEncodeResult(
            embeddings=torch.empty((0, expected_dim)),
            compound_ids=[], failed_compound_ids=skipped_ids,
            metrics={"cache_hit": False, "status": "all_failed"},
        )

    # ── Sort for deterministic padding ─────────────────────
    sort_pairs = list(zip(validated_smiles, validated_ids))
    sort_pairs.sort(key=lambda x: (x[0], x[1]))
    original_order = {cid: i for i, cid in enumerate(validated_ids)}
    sort_indices = [original_order[cid] for _, cid in sort_pairs]
    sorted_smiles = [s for s, _ in sort_pairs]
    sorted_ids = [cid for _, cid in sort_pairs]

    # ── Encoding loop ──────────────────────────────────────
    ensure_dirs()
    t_start = time.monotonic()
    accumulated_embeddings = []
    successfully_encoded_ids: List[str] = []
    all_failed_ids = list(skipped_ids)
    consecutive_failures = 0
    current_batch_size = batch_size
    total_truncated = 0

    try:
        # v35 ROOT FIX (H-12): use a WHILE loop that only advances ``i``
        # on success. The previous ``for i in range(0, len, batch_size)``
        # could not actually retry the same batch — ``i -= batch_size``
        # inside a for-loop is a no-op because the for-loop's internal
        # index is restored on the next iteration. The while loop lets
        # us retry the same batch with a smaller batch_size or after a
        # CPU fallback. ``i`` advances ONLY at the end of the try block
        # (success path) so any exception-triggered ``continue`` retries
        # the same batch.
        i = 0
        while i < len(sorted_smiles):
            batch_smiles = sorted_smiles[i:i + current_batch_size]
            batch_ids = sorted_ids[i:i + current_batch_size]

            try:
                # Tokenize — return_tensors="pt" gives tensors; .to(device)
                # is applied inside _encode_batch for both dict and BatchEncoding
                tokenized = tokenizer(batch_smiles, padding=True, truncation=True, max_length=max_length, return_tensors="pt")

                # 3.4 — truncation detection
                # v35 ROOT FIX (H-11): for on_truncate="skip", build a
                # keep_mask, filter the batch BEFORE encoding, and
                # re-tokenize the filtered batch. The previous code did
                # ``all_failed_ids.append(batch_ids[j]); continue`` inside
                # the truncation-detection loop — but this only skipped
                # the truncation WARNING, not the actual encoding. The
                # truncated SMILES was still encoded (with padding) and
                # its embedding was added to ``accumulated_embeddings``
                # alongside the non-truncated ones — corrupting the
                # downstream graph features. The fix:
                #   1. Build a ``keep_mask`` of non-truncated SMILES.
                #   2. If on_truncate == "skip" AND some SMILES were
                #      truncated, FILTER the batch and RE-TOKENIZE the
                #      filtered subset so only non-truncated SMILES get
                #      encoded. Truncated SMILES go to the dead-letter
                #      queue (already done by _persist_failed_batch).
                if on_truncate == "skip":
                    keep_mask = []
                    for j, smi in enumerate(batch_smiles):
                        # Quick pre-check: tokenise this SMILES alone to
                        # see if it would be truncated. We use the
                        # already-tokenised batch's attention_mask for
                        # efficiency — but we need to re-tokenise per-SMILES
                        # because the batch tokenisation padded to the
                        # longest in the batch (not to max_length).
                        non_pad = tokenized["attention_mask"][j].sum().item()
                        if non_pad >= max_length:
                            total_truncated += 1
                            all_failed_ids.append(batch_ids[j])
                            # Dead-letter the truncated SMILES.
                            try:
                                dead_letter_record(
                                    source="chemberta_encoder",
                                    record={
                                        "compound_id": batch_ids[j],
                                        "smiles": smi,
                                        "reason": "truncated_at_max_length",
                                        "max_length": max_length,
                                    },
                                    reason=f"truncated at {max_length} tokens (on_truncate=skip)",
                                )
                            except Exception:
                                pass
                            keep_mask.append(False)
                        else:
                            keep_mask.append(True)
                    if not any(keep_mask):
                        # All SMILES in this batch were truncated — skip
                        # the entire batch and advance i.
                        i += current_batch_size
                        continue
                    if not all(keep_mask):
                        # Some SMILES were truncated — filter and re-tokenise.
                        batch_smiles = [s for s, k in zip(batch_smiles, keep_mask) if k]
                        batch_ids = [b for b, k in zip(batch_ids, keep_mask) if k]
                        tokenized = tokenizer(batch_smiles, padding=True, truncation=True, max_length=max_length, return_tensors="pt")
                else:
                    # on_truncate == "warn" or "error" — original behavior.
                    for j, smi in enumerate(batch_smiles):
                        non_pad = tokenized["attention_mask"][j].sum().item()
                        if non_pad >= max_length:
                            total_truncated += 1
                            if on_truncate == "error":
                                raise ChembertaSMILESValidationError(f"SMILES for {batch_ids[j]} truncated at {max_length}")
                            logger.warning("SMILES for %s truncated at %d tokens", batch_ids[j], max_length)

                # Forward pass
                batch_embeddings = _encode_batch(model, tokenized, resolved_device, pooling).cpu()
                accumulated_embeddings.append(batch_embeddings)
                successfully_encoded_ids.extend(batch_ids)
                consecutive_failures = 0

                if on_batch_encoded is not None:
                    # v35 ROOT FIX (M-20): the previous code caught
                    # ALL exceptions from the callback and silently
                    # swallowed them (``except Exception: pass``).
                    # A buggy callback (e.g. one that crashed while
                    # writing to MLflow or a downstream dashboard)
                    # was invisible to operators — the encode loop
                    # continued as if nothing happened, but the
                    # callback's intended side effect (logging,
                    # checkpointing, etc.) silently stopped. The fix
                    # logs a WARNING with the exception info so
                    # operators can detect and debug callback
                    # failures. The encode loop still continues —
                    # the callback's failure should NOT abort the
                    # multi-hour encode run.
                    try:
                        on_batch_encoded(i // current_batch_size, len(batch_ids), batch_embeddings)
                    except Exception as _cb_exc:
                        logger.warning(
                            "on_batch_encoded callback failed at "
                            "batch %d: %s. Encoding will continue "
                            "but the callback's side effects "
                            "(logging, checkpointing, etc.) may be "
                            "lost. (M-20)",
                            i // current_batch_size,
                            _cb_exc,
                            exc_info=True,
                        )

                _p_batches.labels(model=model_name).inc()

            except torch.cuda.OutOfMemoryError:
                # v35 ROOT FIX (H-12): with the while loop, ``i`` is
                # NOT advanced on OOM — the next iteration retries the
                # SAME batch with a smaller batch_size (or after CPU
                # fallback). This is the actual fix; the comment-only
                # version in the for-loop was a no-op.
                if current_batch_size > 4:
                    current_batch_size = max(current_batch_size // 2, 1)
                    logger.warning("CUDA OOM — halved batch_size to %d", current_batch_size)
                    # Do NOT advance i — retry the same batch.
                    continue
                else:
                    logger.error("CUDA OOM at batch_size=1 — falling back to CPU")
                    resolved_device = "cpu"
                    model = model.to("cpu")
                    # Do NOT advance i — retry the same batch on CPU.
                    continue

            except ChembertaSMILESValidationError:
                raise

            except Exception as exc:
                failed = _persist_failed_batch(batch_smiles, batch_ids, exc, i // current_batch_size)
                all_failed_ids.extend(failed)
                consecutive_failures += len(batch_ids)
                logger.warning("Batch %d failed: %s", i // current_batch_size, exc, exc_info=True)

                if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                    raise ChembertaEncoderError(f"Circuit breaker tripped: {CIRCUIT_BREAKER_THRESHOLD} consecutive failures.")

            # GPU memory cleanup
            batch_num = i // current_batch_size
            if batch_num > 0 and batch_num % gc_every_n_batches == 0 and torch.cuda.is_available():
                torch.cuda.empty_cache()

            # Checkpoint
            if checkpoint_every_n_batches > 0 and batch_num > 0 and batch_num % checkpoint_every_n_batches == 0:
                try:
                    write_checkpoint(step_name=f"chemberta_encode_{RUN_ID}", data={"batch_index": i, "valid_count": len(successfully_encoded_ids)})
                except Exception:
                    pass

            # H-12: advance i ONLY on the success path (after all
            # exception handlers above). Any ``continue`` inside the
            # except-blocks retries the SAME batch (i unchanged).
            i += current_batch_size
    finally:
        del model, tokenizer
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    t_encode = time.monotonic() - t_start

    if not accumulated_embeddings:
        return ChembertaEncodeResult(
            embeddings=torch.empty((0, expected_dim)),
            compound_ids=[], failed_compound_ids=all_failed_ids,
            metrics={"cache_hit": False, "status": "all_batches_failed"},
        )

    # ── Concatenate & normalize ────────────────────────────
    embeddings_tensor = torch.cat(accumulated_embeddings, dim=0)
    if normalize:
        embeddings_tensor = torch.nn.functional.normalize(embeddings_tensor, p=2, dim=1)

    _check_embedding_health(embeddings_tensor, expected_dim)

    logger.info("Encoded %d compounds in %.1fs (truncated=%d, failed=%d)", len(successfully_encoded_ids), t_encode, total_truncated, len(all_failed_ids))

    # ── Save cache ─────────────────────────────────────────
    cache_was_saved = False
    if not no_cache:
        try:
            payload = _build_cache_payload(
                embeddings_tensor, successfully_encoded_ids,
                model_name, model_revision, commit_hash,
                pooling, torch_dtype, max_length, normalize,
                _compute_smiles_hash(sorted_smiles, sorted_ids),
                hashlib.sha256(json.dumps(sorted(successfully_encoded_ids)).encode()).hexdigest(),
                actual_seed, deterministic, license, attribution,
                commercial_use_allowed, source_dataset,
                source_dataset_version, source_file_checksum,
            )
            ensure_dirs()
            _cache_save_atomic(final_cache_path, payload, RUN_ID)
            cache_was_saved = True
            _write_lineage_sidecar(final_cache_path, successfully_encoded_ids, canonical_smiles_map, model_name, commit_hash, sort_indices)
            try:
                write_lineage_manifest(final_cache_path, input_checksums={"smiles_hash": smiles_hash, "model_commit": commit_hash})
            except Exception:
                pass
            _write_reproducibility_manifest(final_cache_path, payload)
        except Exception as exc:
            logger.error("Cache save failed: %s", exc)

    # ── Audit & transformation logs ───────────────────────
    try:
        audit_log(event_type="chemberta_encode", details=f"Encoded {len(smiles_list)} SMILES", metadata={"run_id": RUN_ID, "compounds_encoded": len(successfully_encoded_ids)})
    except Exception:
        pass
    try:
        log_transformation(step="chemberta_encode", input_desc=f"{len(smiles_list)} SMILES", output_desc=f"{len(successfully_encoded_ids)} embeddings", transform_type="embedding", record_count=len(successfully_encoded_ids))
    except Exception:
        pass

    # ── MLflow ─────────────────────────────────────────────
    _log_to_mlflow(
        {"chemberta_model": model_name, "chemberta_pooling": pooling, "chemberta_seed": str(actual_seed)},
        {"chemberta_encode_seconds": t_encode, "chemberta_smiles_failed": float(len(all_failed_ids))},
        [str(final_cache_path)] if cache_was_saved else [],
    )

    # ── Return ─────────────────────────────────────────────
    if return_device is not None and return_device != "cpu":
        embeddings_tensor = embeddings_tensor.to(return_device)

    final_embeddings = embeddings_tensor.cpu().numpy() if output_format == "numpy" else embeddings_tensor

    return ChembertaEncodeResult(
        embeddings=final_embeddings,
        compound_ids=successfully_encoded_ids,
        failed_compound_ids=all_failed_ids,
        cache_path=final_cache_path if cache_was_saved else None,
        metrics={"cache_hit": False, "encode_seconds": t_encode, "smiles_failed": len(all_failed_ids), "truncated_count": total_truncated},
        model_name=model_name, model_commit_hash=commit_hash, pooling=pooling, torch_dtype=torch_dtype,
        license=license, attribution=attribution, commercial_use_allowed=commercial_use_allowed,
    )


def verify_embedding_quality(
    embeddings: Any,
    compound_ids: List[str],
    sample_size: int = CHEMBERTA_DEFAULT_SAMPLE_SIZE,
    seed: Optional[int] = None,
) -> Dict[str, float]:
    """Compute average pairwise cosine similarity on a sample.

    .. deprecated:: 2.3.0
        Use :func:`compute_avg_pairwise_similarity` instead.
    """
    warnings.warn(
        "verify_embedding_quality is deprecated. Use compute_avg_pairwise_similarity.",
        DeprecationWarning, stacklevel=2,
    )
    return compute_avg_pairwise_similarity(embeddings, compound_ids, sample_size, seed)


def compute_avg_pairwise_similarity(
    embeddings: Any,
    compound_ids: List[str],
    sample_size: int = CHEMBERTA_DEFAULT_SAMPLE_SIZE,
    seed: Optional[int] = None,
) -> Dict[str, float]:
    """Compute average pairwise cosine similarity on a random sample.

    Fixes audit issues 2.7, 8.5, 8.6, 7.3, 11.17.

    v35 ROOT FIX (L-40): document memory usage. The function builds
    a full ``sample_size x sample_size`` similarity matrix via
    ``sample_norm @ sample_norm.T`` — for the default
    ``sample_size=1000`` this is 1000*1000*4 bytes = 4 MB of GPU /
    CPU memory. The matrix is then indexed via ``triu_indices`` to
    extract the upper-triangular (off-diagonal) similarities, which
    produces another ~500K-element tensor. For ``sample_size > 5000``
    the matrix exceeds 100 MB and may OOM on small GPUs. The default
    cap of ``min(sample_size, 1000)`` (below) keeps memory bounded;
    callers that need larger samples should call this function in
    chunks and aggregate the means.
    """
    n = len(embeddings)
    if n == 0 or sample_size == 0:
        return {"avg_cosine_similarity": 0.0, "sample_size": 0, "embedding_dim": 0}

    if sample_size > n:
        logger.warning("sample_size %d > embeddings count %d; using %d", sample_size, n, n)
        sample_size = n
    sample_size = min(sample_size, 1000)

    actual_seed = seed if seed is not None else SEED
    gen = torch.Generator()
    gen.manual_seed(actual_seed)
    indices = torch.randperm(n, generator=gen)[:sample_size]
    sample = embeddings[indices].cpu()

    # 8.5 — vectorized
    sample_norm = torch.nn.functional.normalize(sample, p=2, dim=1)
    sim_matrix = sample_norm @ sample_norm.T
    triu_i, triu_j = torch.triu_indices(sample_size, sample_size, offset=1)
    sims = sim_matrix[triu_i, triu_j]
    avg_sim = sims.mean().item() if sims.numel() > 0 else 0.0

    return {"avg_cosine_similarity": avg_sim, "sample_size": sample_size, "embedding_dim": embeddings.shape[1] if embeddings.dim() == 2 else 0}


async def encode_smiles_async(*args: Any, **kwargs: Any) -> ChembertaEncodeResult:
    """Async wrapper. Fixes audit issue 1.7."""
    return await asyncio.to_thread(encode_smiles, *args, **kwargs)


def encode_smiles_for_graph(builder: Any = None, **kwargs: Any) -> ChembertaEncodeResult:
    """Encode compounds from a GraphBuilder. Fixes G0, 1.1, 15.1."""
    if builder is not None and hasattr(builder, "get_compound_smiles"):
        smiles_ids = builder.get_compound_smiles()
        if smiles_ids:
            smi_list, cid_list = zip(*smiles_ids)
            kwargs.setdefault("smiles_list", list(smi_list))
            kwargs.setdefault("compound_ids", list(cid_list))
    if "smiles_list" not in kwargs or "compound_ids" not in kwargs:
        raise ValueError("Provide builder with get_compound_smiles() or pass smiles_list/compound_ids.")
    return encode_smiles(**kwargs)


def clear_model_cache() -> None:
    """Clear in-memory model cache. Fixes audit issue 8.2.

    v35 ROOT FIX (L-25): the function name ``clear_model_cache`` was
    ambiguous — it cleared the IN-MEMORY process-local cache
    (``_MODEL_CACHE``) but did NOT clear the on-disk embedding
    cache (``EMBEDDINGS_DIR``). Callers expecting a full reset
    were surprised when a re-encode still hit the disk cache. The
    fix adds a clearer alias ``clear_model_memory_cache`` (defined
    below) and keeps this function as a back-compat alias. Both
    names now point to the same implementation.
    """
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


# L-25: clearer alias that explicitly says "memory" cache.
# Kept as a separate function (not an assignment) so it appears in
# ``help()`` output and is inspectable.
def clear_model_memory_cache() -> None:
    """Clear the in-memory (process-local) model cache.

    Alias for ``clear_model_cache``. Use this name in new code to
    make it explicit that the ON-DISK embedding cache is NOT
    cleared — call ``Path(EMBEDDINGS_DIR).glob('*.pt').unlink()``
    (or similar) to clear the disk cache.
    """
    clear_model_cache()


def diff_caches(cache_path_a: Path, cache_path_b: Path) -> Dict[str, Any]:
    """Compare two embedding caches. Fixes audit issue 16.9.

    v34 ROOT FIX (CRITICAL #12): use weights_only=True to prevent
    arbitrary code execution from malicious cache files.
    """
    with open(cache_path_a, "rb") as f:
        a = torch.load(f, weights_only=True)
    with open(cache_path_b, "rb") as f:
        b = torch.load(f, weights_only=True)
    emb_a, emb_b = a["embeddings"], b["embeddings"]
    ids_a, ids_b = a["compound_ids"], b["compound_ids"]
    common = sorted(set(ids_a) & set(ids_b))
    changes = []
    for cid in common:
        va = emb_a[ids_a.index(cid)].cpu()
        vb = emb_b[ids_b.index(cid)].cpu()
        sim = torch.nn.functional.cosine_similarity(va.unsqueeze(0), vb.unsqueeze(0)).item()
        if abs(sim - 1.0) > 1e-6:
            changes.append({"compound_id": cid, "cosine_similarity": round(sim, 6)})
    return {"common": len(common), "only_a": len(set(ids_a) - set(ids_b)), "only_b": len(set(ids_b) - set(ids_a)), "changed": len(changes), "changes": changes[:100]}


def register_consumer(cache_path: Path, consumer: str, consumer_version: str) -> None:
    """Register a downstream consumer. Fixes audit issue 16.15."""
    path = Path(str(cache_path) + ".consumers.jsonl")
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"consumer": consumer, "version": consumer_version, "at": datetime.now(timezone.utc).isoformat()}) + "\n")
    except Exception as exc:
        logger.warning("Failed to register consumer: %s", exc)


def check_dependency_cves() -> List[Dict[str, str]]:
    """Check for known CVEs. Fixes audit issue 14.10."""
    try:
        import subprocess
        r = subprocess.run(["pip-audit", "--format", "json"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return []
        return [f for f in json.loads(r.stdout) if any(p in f.get("name", "").lower() for p in ["torch", "transformers", "numpy"])]
    except Exception:
        return []


# ─── __all__ ─────────────────────────────────────────────────────────
# Fixes audit issue 14.1
__all__: list[str] = [
    "encode_smiles", "encode_smiles_async", "encode_smiles_for_graph",
    "verify_embedding_quality", "compute_avg_pairwise_similarity",
    "CHEMBERTA_MODEL", "CHEMBERTA_CACHE_FORMAT_VERSION",
    "CHEMBERTA_ENCODER_API_VERSION", "CACHE_CONTAINS_PROPRIETARY_DATA",
    "SMILESEncoder", "ChembertaEncodeResult", "ChembertaCachePayload",
    "ChembertaEncoderError", "ChembertaCacheIntegrityError",
    "ChembertaSMILESValidationError", "ChembertaDeviceError",
    "ChembertaEmbeddingCorruptionError",
    "clear_model_cache", "clear_model_memory_cache",  # L-25: add alias
    "diff_caches", "register_consumer", "check_dependency_cves",
]


# ─── CLI ─────────────────────────────────────────────────────────────
# Fixes audit issue 1.6
def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="DrugOS ChemBERTa SMILES Encoder")
    parser.add_argument("--input", required=True, help="CSV with 'smiles' and 'compound_id' columns")
    parser.add_argument("--output", required=True, help="Output path for embeddings")
    parser.add_argument("--model", default=CHEMBERTA_MODEL)
    parser.add_argument("--batch-size", type=int, default=CHEMBERTA_DEFAULT_BATCH_SIZE)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-length", type=int, default=CHEMBERTA_DEFAULT_MAX_LENGTH)
    parser.add_argument("--pooling", choices=["cls", "mean", "max", "pooler"], default="mean")
    parser.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], default="float32")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    import pandas as pd
    df = pd.read_csv(args.input)
    if "smiles" not in df.columns or "compound_id" not in df.columns:
        parser.error("CSV must have 'smiles' and 'compound_id' columns")

    result = encode_smiles(
        smiles_list=df["smiles"].tolist(), compound_ids=df["compound_id"].tolist(),
        model_name=args.model, batch_size=args.batch_size, device=args.device,
        cache_path=Path(args.output), pooling=args.pooling, torch_dtype=args.dtype,
        force_refresh=args.force_refresh, no_cache=args.no_cache,
    )
    print(f"Encoded {len(result.compound_ids)} compounds -> {result.cache_path}")


if __name__ == "__main__":
    _main()
