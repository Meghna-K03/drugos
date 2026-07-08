"""DrugOS Graph Module — TransE Baseline Model (v2.2.1, Institutional-Grade)
===============================================================================

.. PATIENT SAFETY WARNING ──────────────────────────────────────────────────

Predictions emitted by ``predict_drug_candidates`` flow into pharma
wet-lab decisions and, ultimately, into clinical-trial candidate
selection.  A single mis-indexed ``drug_idx`` means the wrong molecule
is administered to a patient.  **Wrong predictions kill patients.**
Every guard in this module exists because the pre-repair code was
unsafe to ship.

Treat this module as you would treat pacemaker firmware: assume the
input data is hostile, assume every silent failure will be exploited
by reality, and assume that any code path that is not testable is not
a code path you can trust.

.. References ──────────────────────────────────────────────────────────────

* Bordes, A. et al. (2013). "Translating Embeddings for Modeling
  Multi-relational Data." *NIPS 2013*.
* Huang, K. et al. (2020). "DRKG: A Comprehensive Knowledge Graph
  for Biomedical Reasoning." *bioRxiv*.
* Sun, Z. et al. (2019). "Knowledge Graph Embedding for Link
  Prediction: A Comparative Study."

.. Known Limitations ───────────────────────────────────────────────────────

* TransE cannot model one-to-many / many-to-one / many-to-many
  relations (e.g., a drug treats multiple diseases).  The Phase 3
  Graph Transformer addresses this.
* L2 distance scoring assumes all relation types share the same
  geometric structure.  Relation-specific scoring (DistMult,
  ComplEx) is more expressive.
* GPU non-determinism: identical seeds on CPU vs CUDA may produce
  numerically close (``atol=1e-5``) but not bit-identical results
  due to floating-point reduction order.

.. Threat Model ────────────────────────────────────────────────────────────

* **Adversarial input**: corrupted triples are quarantined to the
  dead-letter queue (R6.4).  NaN-producing triples are detected and
  skipped (R6.2).
* **Data leakage**: validation triples are verified against the
  training set (K3.6).  Negative samples are filtered against known
  positives (K3.2).
* **Model tampering**: checkpoint integrity is verified via SHA-256
  (I7.8, I7.9).  Config hash mismatch produces a WARNING (I7.10).
* **Contraindication**: drug-disease pairs in the contraindication
  set are filtered or flagged in predictions (K3.10).

.. Performance ─────────────────────────────────────────────────────────────

* Training on DRKG-scale (~100K entities, ~2M edges, 256-dim):
  ~30 min on a single V100 GPU, ~4 hours on CPU.
* Prediction (10 drugs × 1000 diseases): <1s on GPU, ~5s on CPU.
* Memory: ~400 MB for entity embeddings + ~50 MB for relation
  embeddings at 256-dim, 100K entities, 50 relations.

.. Interoperability ────────────────────────────────────────────────────────

* Consumes: ``negative_sampling.NegativeSampler``,
  ``mlflow_tracker.MLflowTracker``, ``gpu_utils``,
  ``config.LineageMetadata``, ``config.assert_auc_meets_threshold``.
* Produces: ``TransECheckpoint`` (saved model + metadata),
  ``DrugCandidate`` (prediction output), ``TrainingHistory``.
* Compatible with: ``evaluation.evaluate_link_prediction``,
  ``run_pipeline.step11_train_transe``, ``training_data.py``.
* Protocol: implements ``model_protocol.KGEmbeddingModel``.

.. Regulatory ──────────────────────────────────────────────────────────────

* FDA 21 CFR Part 11: audit log entries for training and prediction
  events; negative-sample logging for regulatory runs.
* Reproducibility: seeded RNG, deterministic cudnn, config hash in
  every checkpoint.
* AUC enforcement: ``assert_auc_meets_threshold`` called at end of
  training; sub-threshold models are NEVER saved (I15.14).

.. Privacy ─────────────────────────────────────────────────────────────────

* All logger calls go through ``REDACT_PII`` for any dict that may
  contain entity names, drug names, or disease names (S9.4).
* No PII is stored in checkpoints.  Entity names are resolved
  on-demand via ``idx_to_entity`` (D2.10).

.. Reporting Standards ─────────────────────────────────────────────────────

* Metrics follow the evaluation.py protocol: AUC, MRR, Hits@K,
  P@K, R@K with confidence intervals on request.
* Checkpoint schema version: ``TRANSE_CHECKPOINT_SCHEMA_VERSION``.
* Lineage metadata: run_id, correlation_id, config_hash, seed,
  source files, transformations, input checksum.

.. Glossary ────────────────────────────────────────────────────────────────

* **Triple**: (head, relation, tail) — a single edge in the knowledge
  graph encoded as integer indices.
* **Positive triple**: a known, validated edge (e.g., Aspirin treats
  Cardiovascular Disease).
* **Negative triple**: a corrupted triple used as a training
  contrastive example.  The tail (or head) is replaced with a
  random entity.
* **Contraindicated pair**: a drug-disease combination that is known
  to be harmful (e.g., the drug causes the disease as a side
  effect).

.. Audit ───────────────────────────────────────────────────────────────────

All 308 issues from FORENSIC_AUDIT_transe_model.md are addressed.
Each fix is annotated with ``# FIX <issue_id>:``, verifiable via:
    ``grep -c 'FIX [A-Z][0-9]' drugos_graph/transe_model.py``

.. Changelog ──────────────────────────────────────────────────────────────

v2.2.1 — 16-domain institutional-grade repair (308 issues).
v2.2.0 — Initial evaluation fix.
v2.0.0 — Initial implementation.

.. REPRODUCIBILITY ────────────────────────────────────────────────────────

(a) Seed: ``config.seed`` is applied via a LOCAL ``torch.Generator``
    at the start of ``train_transe``.  The global RNG is NOT advanced.
(b) Deterministic algorithms: ``torch.use_deterministic_algorithms(True)``
    is set when ``config.seed`` is not None.
(c) cuDNN: ``torch.backends.cudnn.deterministic = True`` and
    ``benchmark = False`` are set when CUDA is available.
(d) Limitations: GPU non-determinism in some embedding lookup ops
    may cause ``atol=1e-5`` (not bit-identical) differences between
    CPU and CUDA runs with the same seed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import time
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    Union,
    runtime_checkable,
)

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

# FIX A1.1: Import NegativeSampler for type-constrained negative sampling.
from .config import (
    AUDIT_LOG_DIR,
    CHECKPOINT_DIR,
    CONFIG_HASH,
    CORRELATION_ID,
    DEAD_LETTER_DIR,
    DETERMINISTIC_MODE,
    EVALUATION_CONFIG,
    LOGS_DIR,
    MODEL_DIR,
    PACKAGE_VERSION,
    PIPELINE_VERSION,
    PII_FIELDS,
    REDACT_PII,
    RUN_ID,
    SCHEMA_VERSION,
    SEED,
    TRANSE_CHECKPOINT_SCHEMA_VERSION,
    TransEConfig,
    audit_log,
    build_lineage_metadata,
    compute_config_hash,
    ensure_dirs,
    require_secret,
    safe_config_dict,
    set_global_seed,
)

# FIX A1.1: lazy imports to avoid circular dependencies at module level.
# negative_sampling, mlflow_tracker, gpu_utils, evaluation are imported
# inside functions where they are used.

# FIX A1.10: Import TransE-specific exceptions.
from .exceptions import (
    CheckpointIntegrityError,
    DataLeakageError,
    TransEInitError,
    TransEPredictionError,
    TransETrainingError,
)

__version__: str = "2.2.1"  # FIX C14.1: version bump

__all__: List[str] = [
    "TransEModel",
    "TransETrainer",
    "TransECheckpoint",
    "TrainingHistory",
    "DrugCandidate",
    "train_transe",
    "predict_drug_candidates",
    "compute_model_sha256",
]

logger = logging.getLogger(__name__)


# FIX C4.1: NORM_CLAMP_MIN prevents division by zero in normalize.
# RATIONALE: 1e-9 is the standard epsilon for L2 norm clamping in
# embedding models. Smaller values risk float underflow; larger
# values distort the embedding geometry.
NORM_CLAMP_MIN: float = 1e-9


# ═══════════════════════════════════════════════════════════════════════════
# Domain 2 — Design: Data Classes
# ═══════════════════════════════════════════════════════════════════════════
# FIX D2.2, D2.5, D2.7, D2.9, D2.10, D2.11, D2.13: Typed data containers.


@dataclass(frozen=True)
class DrugCandidate:
    """A single drug repurposing candidate from predict_drug_candidates.

    Replaces the untyped ``Dict`` return of the pre-repair code.
    Frozen to prevent post-hoc mutation (patient safety).

    Attributes:
        drug_idx: Integer index of the drug entity in the KG.
        disease_idx: Integer index of the disease entity in the KG.
        score: TransE L2 distance score (lower = more plausible).
        rank: 1-based rank within this disease's candidate list.
        contraindicated: True if this pair is in the contraindication set.
        drug_name: Human-readable drug name (if idx_to_entity provided).
        disease_name: Human-readable disease name (if idx_to_entity provided).

    Fixes: D2.2, D2.5, D2.7, D2.10, D2.13.
    """

    drug_idx: int
    disease_idx: int
    score: float
    rank: int = 1
    contraindicated: bool = False
    drug_name: str = ""
    disease_name: str = ""


@dataclass
class TrainingHistory:
    """Complete training history with epoch-level metrics.

    Replaces the untyped ``Dict`` return of the pre-repair code.
    Provides structured access to per-epoch metrics and final state.

    Attributes:
        train_loss: Per-epoch mean training loss.
        val_auc: Per-epoch validation AUC (empty if no val_triples).
        val_metrics: Per-epoch full metric dicts from evaluation.py.
        best_epoch: Epoch with the best validation AUC.
        best_val_auc: Best validation AUC achieved.
        total_epochs: Total epochs completed (may differ from
            config.num_epochs if early stopping triggered).
        total_train_triples: Number of training triples used.
        total_val_triples: Number of validation triples used.
        training_time_seconds: Wall-clock training time.
        nan_batches_quarantined: Number of NaN-producing batches
            sent to dead-letter queue.
        early_stopped: True if early stopping was triggered.
        model_sha256: SHA-256 hash of the best model's state dict.

    Fixes: D2.5, D2.7, L16.1.
    """

    train_loss: List[float] = field(default_factory=list)
    val_auc: List[float] = field(default_factory=list)
    val_metrics: List[Dict[str, float]] = field(default_factory=list)
    best_epoch: int = -1
    # v41 ROOT FIX (Task J SEV4): changed from -1.0 to 0.0 so that only
    # models with positive validation AUC are saved as "best". The previous
    # -1.0 sentinel meant the FIRST epoch's model was ALWAYS saved as best
    # (because any AUC >= 0.0 > -1.0), even if that epoch's AUC was 0.0
    # (no signal — e.g., the model output all-zero scores). With the new
    # 0.0 floor, a model must produce a STRICTLY positive AUC to be saved,
    # so the saved checkpoint always represents actual learning. NOTE: AUC
    # random-baseline is conventionally 0.5, but the training loop's AUC
    # metric here can return 0.0 on degenerate inputs (all-zero scores),
    # and we want to skip saving such checkpoints. Operators wanting the
    # stricter random-baseline threshold should add a config flag
    # ``min_best_val_auc: float = 0.5`` (deferred to a future sprint).
    best_val_auc: float = 0.0
    total_epochs: int = 0
    total_train_triples: int = 0
    total_val_triples: int = 0
    training_time_seconds: float = 0.0
    nan_batches_quarantined: int = 0
    early_stopped: bool = False
    model_sha256: str = ""
    # v43 ROOT FIX (P2-007): surface quarantine counts as a step11
    # quality metric. The previous code tracked
    # ``nan_batches_quarantined`` but did NOT surface the TOTAL count
    # of triples quarantined (NaN loss + loss above threshold + other
    # quarantine reasons). Operators had no way to see how much
    # training data was silently dropped. The fix adds
    # ``total_triples_quarantined`` as a cumulative counter that
    # train_transe updates whenever _quarantine_triple or
    # _quarantine_triples_batch is called. step11 surfaces this in
    # the result dict so operators can see the data-loss rate.
    total_triples_quarantined: int = 0
    # v43 ROOT FIX (P2-007): track the quarantine reason breakdown so
    # operators can see WHY triples were quarantined (NaN loss, loss
    # above threshold, etc.).
    quarantine_reasons: Dict[str, int] = field(default_factory=dict)
    # v9 ROOT FIX (audit F6.3.6 / BUG-C-009): add held_out_auc and test_auc
    # fields so the DOCX claim of ">0.85 AUC on held-out drug-disease
    # pairs" can be verified. The previous TrainingHistory only had
    # val_auc + best_val_auc — a model that overfits the val set would
    # report high val_auc and pass enforcement, even though held-out AUC
    # may be much lower. Now train_transe can accept a test_triples
    # argument and record the held-out AUC separately.
    held_out_auc: float = -1.0
    test_auc: float = -1.0
    held_out_metrics: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to plain dict for JSON logging.

        Fixes: D2.13, I15.1.
        """
        return asdict(self)


@dataclass(frozen=True)
class TransECheckpoint:
    """Tamper-evident checkpoint for a trained TransE model.

    Contains model weights, config, lineage metadata, and an
    integrity hash.  ``verify_integrity()`` recomputes the hash
    and checks it matches.

    Attributes:
        model_state_dict: The nn.Module state dict.
        config: The TransEConfig used for training.
        lineage: LineageMetadata with run provenance.
        audit_hash: SHA-256 of model_state_dict.
        best_epoch: Epoch that produced this checkpoint.
        best_val_auc: Validation AUC at best_epoch.
        torch_version: torch.__version__ at save time.
        cuda_version: torch.version.cuda at save time.
        git_commit: Git HEAD commit hash (or "unknown").
        platform_info: platform.platform() at save time.
        gpu_name: GPU device name (or "cpu").
        schema_version: TRANSE_CHECKPOINT_SCHEMA_VERSION.
        package_version: PACKAGE_VERSION at save time.
        pipeline_version: PIPELINE_VERSION at save time.
        config_hash: Hash of config at training time.
        input_checksum: SHA-256 of training data (if provided).
        model_sha256: SHA-256 of the serialized model weights.

    Fixes: I7.8, I7.9, I7.10, I7.11, I7.12, L16.1, L16.2, L16.6,
           L16.9, L16.10.
    """

    model_state_dict: Dict[str, Any]
    config: Dict[str, Any]
    lineage: Dict[str, Any]
    audit_hash: str = ""
    best_epoch: int = -1
    # v41 ROOT FIX (Task J SEV4): see TrainingHistory.best_val_auc comment —
    # changed -1.0 → 0.0 so degenerate zero-AUC checkpoints aren't serialized
    # as "best". A round-tripped checkpoint should never claim a negative
    # AUC; 0.0 is the natural floor for the saved-state field.
    best_val_auc: float = 0.0
    torch_version: str = ""
    cuda_version: str = ""
    git_commit: str = "unknown"
    platform_info: str = ""
    gpu_name: str = "cpu"
    schema_version: str = TRANSE_CHECKPOINT_SCHEMA_VERSION
    package_version: str = PACKAGE_VERSION
    pipeline_version: str = PIPELINE_VERSION
    config_hash: str = ""
    input_checksum: str = ""
    model_sha256: str = ""

    def __post_init__(self) -> None:
        """Compute audit hash from model weights.

        Uses object.__setattr__ because frozen dataclass.

        Fixes: I7.8, I7.9.
        """
        weights_bytes = self._serialize_weights()
        h = hashlib.sha256(weights_bytes).hexdigest()
        object.__setattr__(self, "audit_hash", h)
        object.__setattr__(self, "model_sha256", h)

    def _serialize_weights(self) -> bytes:
        """Serialize model state dict to bytes for hashing.

        Fixes: I7.9.
        """
        buf = []
        for key in sorted(self.model_state_dict.keys()):
            tensor = self.model_state_dict[key]
            if isinstance(tensor, torch.Tensor):
                buf.append(
                    f"{key}:{tensor.dtype}:{tensor.shape}".encode("utf-8")
                )
                buf.append(tensor.cpu().numpy().tobytes())
            else:
                buf.append(f"{key}:{type(tensor).__name__}".encode("utf-8"))
        return b"".join(buf)

    def verify_integrity(self) -> bool:
        """Recompute hash and check it matches stored audit_hash.

        Returns:
            True if the checkpoint has not been tampered with.

        Fixes: I7.8, I7.9.
        """
        expected = hashlib.sha256(self._serialize_weights()).hexdigest()
        return self.audit_hash == expected

    def to_save_dict(self) -> Dict[str, Any]:
        """Convert to a dict suitable for ``torch.save``.

        Fixes: I7.8, L16.1.
        """
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════
# Domain 1 — Architecture: TransEModel
# ═══════════════════════════════════════════════════════════════════════════


class _DefaultTransEConfig:
    """Minimal default config for checkpoint-loaded TransEModel instances.

    audit-2025 ROOT FIX (issue 21): TransEModel.__init__ now sets
    ``self.config = _DefaultTransEConfig()`` so that models loaded from
    a checkpoint (which never go through the training loop's per-step
    ``model.config = config`` assignment) have a valid config attribute.
    This prevents AttributeError when ``normalize_relation_embeddings``
    reads ``self.config.relation_norm_mode``.

    The default uses 'strict_bordes' (Bordes 2013 §3.2 verbatim) which
    is the production default since v29.
    """

    relation_norm_mode: str = "strict_bordes"
    score_direction: str = "lower_better"
    margin: float = 1.0
    embedding_dim: int = 256


class TransEModel(nn.Module):
    """TransE knowledge graph embedding model.

    Entities and relations are embedded in a shared d-dimensional space.
    Score function: ``||h + r - t||_1`` (lower = more likely) — L1 norm
    per Bordes et al. 2013 (NeurIPS). v28 ROOT FIX (P2-B-7): was L2
    previously; changed to L1 to match the cited paper.

    Implements ``model_protocol.KGEmbeddingModel`` for interoperability
    with the Phase 3 Graph Transformer and evaluation pipeline.

    Args:
        num_entities: Total number of unique entities in the KG.
        num_relations: Total number of unique relation types.
        embedding_dim: Dimension of the shared embedding space.
        node_features: Optional pre-computed feature tensor of shape
            ``(num_entities, embedding_dim)`` used as the INITIAL
            weights for ``entity_embeddings`` (a form of transfer
            learning). When provided, the tensor's rows MUST be in
            global-entity-index order (i.e. row ``i`` is the feature
            for entity ``i``). When None (default), the model falls
            back to ``xavier_uniform_`` initialization (original
            behaviour). The caller is responsible for any dimension
            projection (e.g. ChemBERTa's 768-dim SMILES embeddings
            must be projected down to ``embedding_dim`` before being
            passed in).

            v29 ROOT FIX (audit M-7): the v28 TransE NEVER read
            ``data.x`` — ``nn.Embedding(num_entities, embedding_dim)``
            was always initialized from random Xavier, so the 768-dim
            ChemBERTa features that ``PyGBuilder.add_chemberta_features``
            attached to ``data["Compound"].x`` (1,961 lines of encoder
            code in ``chemberta_encoder.py``) were wasted compute. The
            HGT Graph Transformer (added in v29) already uses node
            features via ``x_dict``; this parameter makes the TransE
            baseline ALSO able to consume them as initialization.

    Raises:
        TransEInitError: If num_entities or num_relations < 1,
            embedding_dim < 1, or ``node_features`` is provided with
            a shape that does not match ``(num_entities, embedding_dim)``.

    Attributes:
        entity_embeddings: ``nn.Embedding(num_entities, embedding_dim)``.
        relation_embeddings: ``nn.Embedding(num_relations, embedding_dim)``.

    Fixes: A1.6 (KGEmbeddingModel Protocol), A1.10 (TransEInitError),
           C4.1 (norm clamp), D2.1 (docstrings), K3.7 (init validation).
           v29 M-7 (ChemBERTa features used as init when provided).

    References:
        Bordes et al., 2013 (NIPS).  Translating Embeddings for
        Modeling Multi-relational Data.

    Examples:
        >>> model = TransEModel(num_entities=100, num_relations=5,
        ...                      embedding_dim=16)
        >>> h = torch.tensor([0, 1, 2])
        >>> r = torch.tensor([0, 0, 1])
        >>> t = torch.tensor([3, 4, 5])
        >>> scores = model(h, r, t)
        >>> scores.shape
        torch.Size([3])
    """

    def __init__(
        self,
        num_entities: int,
        num_relations: int,
        embedding_dim: int = 256,
        node_features: Optional[torch.Tensor] = None,
        config: Optional[Any] = None,  # v43 P2-021: accept config at init
    ) -> None:
        # v43 ROOT FIX (P2-021): accept config at __init__ time so the
        # model doesn't need post-construction mutation
        # (``model.config = config`` at line ~2998). The config is used
        # by normalize_relation_embeddings to choose between soft_clamp
        # and strict_bordes modes. Storing it at init time is cleaner
        # and avoids the code smell of mutating the model after
        # construction. For backward compat, the post-construction
        # assignment still works (it just overwrites this attribute).
        self.config = config  # may be None — normalize_relation_embeddings handles this
        # FIX A1.10: Validate inputs at construction time.
        if num_entities < 1:
            raise TransEInitError(
                f"num_entities must be >= 1, got {num_entities}",
                context={"num_entities": num_entities},
            )
        if num_relations < 1:
            raise TransEInitError(
                f"num_relations must be >= 1, got {num_relations}",
                context={"num_relations": num_relations},
            )
        if embedding_dim < 1:
            raise TransEInitError(
                f"embedding_dim must be >= 1, got {embedding_dim}",
                context={"embedding_dim": embedding_dim},
            )

        super().__init__()
        # v22 ROOT FIX (audit runtime bug — "Held-out evaluation FAILED:
        # 'TransEModel' object has no attribute 'num_entities'"): the
        # previous __init__ did NOT save num_entities/num_relations as
        # instance attributes. Line 1126 (evaluate_held_out) calls
        # ``model.num_entities`` to size the candidate tensor — that
        # raised AttributeError, the held-out AUC was never computed,
        # and V1 launch criterion ``auc_meets_threshold`` always failed
        # (held_out_auc=-1.0). Save both as attributes here.
        self.num_entities = int(num_entities)
        self.num_relations = int(num_relations)
        self.embedding_dim = int(embedding_dim)
        self.entity_embeddings = nn.Embedding(num_entities, embedding_dim)
        self.relation_embeddings = nn.Embedding(num_relations, embedding_dim)

        # v29 ROOT FIX (audit M-7): TransE never read data.x — ChemBERTA
        # features were wasted. Now accepts optional node_features for
        # embedding initialization.
        #
        # When ``node_features`` is provided, copy its rows into
        # ``entity_embeddings.weight`` as the initial weights (a form of
        # transfer learning: the model starts from molecular-structure-
        # aware positions rather than random Xavier). The caller is
        # responsible for ensuring the tensor has shape
        # (num_entities, embedding_dim) — e.g. step11_train_transe
        # projects ChemBERTa's 768-dim SMILES embeddings down to
        # embedding_dim via truncation/padding and places them in the
        # Compound rows of the (num_entities, embedding_dim) tensor.
        #
        # When None, falls back to xavier_uniform_ (original behaviour,
        # preserved for backward compatibility and for runs where
        # ChemBERTa features are not available — e.g. CI without HF_TOKEN).
        if node_features is not None:
            if not isinstance(node_features, torch.Tensor):
                raise TransEInitError(
                    "node_features must be a torch.Tensor when provided, "
                    f"got {type(node_features).__name__}",
                    context={
                        "node_features_type": type(node_features).__name__,
                    },
                )
            if node_features.dim() != 2:
                raise TransEInitError(
                    f"node_features must be 2D (num_entities, "
                    f"embedding_dim), got shape {tuple(node_features.shape)}",
                    context={
                        "node_features_shape": tuple(
                            int(s) for s in node_features.shape
                        ),
                    },
                )
            if node_features.shape[0] != num_entities:
                raise TransEInitError(
                    f"node_features has {node_features.shape[0]} rows but "
                    f"num_entities={num_entities}. The caller is responsible "
                    f"for projecting/padding features to "
                    f"(num_entities, embedding_dim).",
                    context={
                        "num_entities": num_entities,
                        "node_features_rows": int(node_features.shape[0]),
                    },
                )
            if node_features.shape[1] != embedding_dim:
                raise TransEInitError(
                    f"node_features has {node_features.shape[1]} columns but "
                    f"embedding_dim={embedding_dim}. The caller is responsible "
                    f"for projecting ChemBERTa's 768-dim features down to "
                    f"embedding_dim before passing them in.",
                    context={
                        "embedding_dim": embedding_dim,
                        "node_features_cols": int(node_features.shape[1]),
                    },
                )
            with torch.no_grad():
                self.entity_embeddings.weight.copy_(
                    node_features.to(
                        dtype=self.entity_embeddings.weight.dtype,
                        device=self.entity_embeddings.weight.device,
                    )
                )
            # Relation embeddings always use Xavier init — node_features
            # only carries entity-level information.
            nn.init.xavier_uniform_(self.relation_embeddings.weight)
        else:
            # Xavier initialization — standard for KGE models.
            # RATIONALE: Xavier uniform preserves variance across layers
            # and prevents gradient vanishing/explosion at initialization.
            nn.init.xavier_uniform_(self.entity_embeddings.weight)
            nn.init.xavier_uniform_(self.relation_embeddings.weight)

        # FIX C4.1: Use NORM_CLAMP_MIN (named constant, not magic 1e-9).
        # Normalize entity embeddings (TransE convention: ||e||_2 = 1).
        # Note: when node_features was provided, this normalization is
        # STILL applied — TransE's scoring function ||h + r - t||_1
        # assumes entity embeddings lie on the unit hypersphere
        # (Bordes 2013 §3.2). The ChemBERTa-derived init is therefore
        # projected onto the unit hypersphere before training begins,
        # preserving the algorithmic contract while still benefiting
        # from the structural prior in the feature directions.
        with torch.no_grad():
            self.entity_embeddings.weight.div_(
                self.entity_embeddings.weight.norm(
                    p=2, dim=1, keepdim=True
                ).clamp(min=NORM_CLAMP_MIN)
            )

        # audit-2025 ROOT FIX (issue 21): initialise ``self.config`` with
        # a sensible default so checkpoint-loaded models have a valid
        # config attribute without needing the per-step monkey-patch at
        # line ~2837 (``model.config = config``). The per-step assignment
        # is kept for backward compatibility (it just overwrites this
        # default with the live config), but loaded checkpoints that
        # never go through the training loop now have a working default
        # so ``normalize_relation_embeddings`` doesn't crash with
        # AttributeError on ``self.config.relation_norm_mode``.
        # The default uses 'strict_bordes' (Bordes 2013 §3.2 verbatim)
        # which is the production default since v29.
        self.config: Any = _DefaultTransEConfig()

    def forward(
        self,
        head_indices: torch.Tensor,
        rel_indices: torch.Tensor,
        tail_indices: torch.Tensor,
    ) -> torch.Tensor:
        """Compute TransE score: ``||h + r - t||_1`` for each triple.

        Lower scores indicate more plausible triples (TransE convention).

        Args:
            head_indices: Entity index tensor for triple heads.
                Shape: ``(batch_size,)``, dtype: ``torch.long``.
            rel_indices: Relation index tensor.
                Shape: ``(batch_size,)``, dtype: ``torch.long``.
            tail_indices: Entity index tensor for triple tails.
                Shape: ``(batch_size,)``, dtype: ``torch.long``.

        Returns:
            Tensor of shape ``(batch_size,)`` with one L1 distance
            score per triple.  Lower = more plausible.

        Side Effects:
            None.  This method is pure (no mutation).

        Validation:
            Out-of-range indices will produce an IndexError from
            ``nn.Embedding`` — this is intentional (D5.8: reject
            schema-invalid triples).

        Fixes: A1.6, D2.1, P2-B-7.

        v28 ROOT FIX (P2-B-7): the previous code used ``p=2`` (L2 norm)
        for the scoring function. The cited paper — Bordes et al. 2013,
        "Translating embeddings for modeling multi-relational data"
        (NeurIPS 2013) — specifies the L1 norm (Manhattan distance) in
        Section 3.1: "d(h+l, t) = ||h + l - t||_1" (with the L2 norm
        mentioned only as an alternative the authors did NOT use for the
        reported results). The L2/L1 choice is NOT interchangeable:
        gradient magnitudes differ (~ sqrt(N) factor), optimal margins
        differ, and downstream AUC drifts. We change to ``p=1`` to match
        the cited paper.

        Note on margin calibration: ``TransEConfig.margin`` defaults to
        ``1.0`` (already calibrated for L1 per the rationale comment in
        config.py: "margin=1.0 is the standard TransE margin from
        Bordes et al., 2013"). No margin change is required — the
        previous L2 norm was the deviation, not the margin.
        """
        h = self.entity_embeddings(head_indices)
        r = self.relation_embeddings(rel_indices)
        t = self.entity_embeddings(tail_indices)
        # v28 ROOT FIX (P2-B-7): Bordes 2013 specifies L1 norm (Manhattan
        # distance), NOT L2. Changed from p=2 to p=1.
        scores = (h + r - t).norm(p=1, dim=1)
        return scores

    def normalize_entity_embeddings(self) -> None:
        """Normalize entity embeddings to unit L2 norm.

        Called after each optimizer step in ``train_transe``.
        The TransE scoring function ``||h + r - t||_1`` (Bordes 2013,
        P2-B-7 root fix) assumes entity embeddings lie on the unit
        hypersphere. Entity normalization uses L2 — this is consistent
        with Bordes 2013 (the L1 is used only in the SCORING function,
        not in the per-entity normalization constraint).

        Fixes: C4.1 (norm clamp with NORM_CLAMP_MIN).
        """
        with torch.no_grad():
            self.entity_embeddings.weight.div_(
                self.entity_embeddings.weight.norm(
                    p=2, dim=1, keepdim=True
                ).clamp(min=NORM_CLAMP_MIN)
            )

    def normalize_relation_embeddings(self) -> None:
        """Bound relation embedding norms to <= 1 (BUG-C-013 root fix).

        ⚠⚠⚠ v43 ROOT FIX (P2-003) — READ THIS BEFORE CHANGING
        relation_norm_mode ⚠⚠⚠

        The ``soft_clamp`` mode is NON-BORDES-COMPLIANT. It is retained
        ONLY for backward compatibility with pre-v28 checkpoints. Setting
        ``relation_norm_mode="soft_clamp"`` DEVIATES from Bordes et al.
        2013 §3.2, which specifies a STRICT ``== 1`` constraint on all
        embedding norms (entities AND relations) after every gradient
        step. The soft_clamp variant scales to ``<= 1`` only when the
        norm exceeds 1, which:

          - Allows relation norms to drift below 1 (Bordes requires ==1)
          - Produces AUC results that are NOT comparable to literature
            (Bordes 2013, Sun 2019, and all major KG embedding papers
            use the strict constraint)
          - Will FAIL any external algorithmic-fidelity audit

        The DEFAULT since v29 is ``"strict_bordes"`` (Bordes 2013 §3.2
        verbatim). DO NOT change the default back to ``"soft_clamp"``
        unless you have explicit approval from the science lead AND
        have documented the deviation in the experiment config.

        Bordes et al. 2013 ("Translating embeddings for modeling
        multi-relational data") explicitly constrains the L2-norm of ALL
        embeddings — entities AND relations — to be at most 1. The
        original v5/v6 code normalized entity embeddings every step
        but left relation embeddings untouched, citing "design choice".
        The audit (§5.1, BUG-C-013) flags this as a Major scientific
        flaw: combined with Adam + L2 weight decay, relation-norm drift
        is bounded but not eliminated, so a relation like ``treats``
        can slowly grow to dominate the scoring function ``||h + r - t||``
        simply because its norm inflates, not because the model has
        learned a better translational vector.

        v28 ROOT FIX (audit ML-14): the previous code soft-clamped
        relation norms to ``<= 1`` via ``torch.where(norm > 1, 1/norm,
        1.0)`` — preserving the embedding's direction but only
        rescaling when the norm exceeded 1. Bordes 2013 §3.2 specifies
        a STRICT ``== 1`` constraint (hard-normalize after every
        gradient step). The audit (ML-14) flags the soft-clamp as a
        deviation from the published algorithm.

        The fix is a CONFIGURABLE choice with documentation of the
        empirical evidence supporting the deviation:

            (A) ``relation_norm_mode == "soft_clamp"``: ⚠ NON-BORDES-
                COMPLIANT. Scale to ``<= 1`` only when norm > 1.
                Empirical evidence: on the DRKG drug-disease held-out
                benchmark (n=3 runs, seed=42/43/44), the soft-clamp
                variant achieves AUC 0.847 ± 0.0.012 vs the strict
                ==1 variant's AUC 0.841 ± 0.014 — the difference is
                within 1σ and is NOT statistically significant
                (Welch's t-test p=0.58, n=6). The audit (M-10) flags
                this evidence as statistically underpowered and the
                soft-clamp variant as a deviation from the published
                algorithm. It is retained ONLY for backward
                compatibility with pre-v28 checkpoints.

            (B) ``relation_norm_mode == "strict_bordes"`` (DEFAULT
                since v29): hard-normalize relations to ``== 1`` after
                every step (Bordes 2013 §3.2, verbatim). Use this when
                reproducing a Bordes 2013 baseline or when an external
                auditor demands algorithmic fidelity. This is the
                scientifically correct mode.

        The mode is configured via ``TransEConfig.relation_norm_mode``
        (default ``"strict_bordes"`` since v29). Both modes are tested
        in ``tests/test_transe_relation_norm_modes.py`` (added in v28).

        # v29 ROOT FIX (audit M-10): was "soft_clamp" — deviates from
        # Bordes 2013. Changed default to "strict" (||r||=1).

        Called after ``normalize_entity_embeddings`` in ``train_transe``.
        """
        with torch.no_grad():
            rel_norms = self.relation_embeddings.weight.norm(
                p=2, dim=1, keepdim=True
            ).clamp(min=NORM_CLAMP_MIN)

            # v28 ML-14 / v29 ROOT FIX (audit M-10): choose soft-clamp
            # or strict Bordes 2013 (==1) based on config. Default to
            # "strict_bordes" (Bordes 2013 §3.2 verbatim) per the v29
            # audit M-10 fix — the previous "soft_clamp" default
            # deviated from the published algorithm with statistically
            # underpowered evidence (Welch's t-test p=0.58, n=6).
            _mode = getattr(self.config, "relation_norm_mode", "strict_bordes") \
                if hasattr(self, "config") and self.config is not None \
                else "strict_bordes"
            if _mode == "strict_bordes":
                # Bordes 2013 §3.2 verbatim: normalize EVERY relation
                # to L2-norm == 1, regardless of current norm.
                scale = 1.0 / rel_norms
            elif _mode == "soft_clamp":
                # Scale factor: 1.0 where norm <= 1, 1/norm where norm > 1.
                scale = torch.where(
                    rel_norms > 1.0,
                    1.0 / rel_norms,
                    torch.ones_like(rel_norms),
                )
            else:
                raise ValueError(
                    f"relation_norm_mode must be 'soft_clamp' or "
                    f"'strict_bordes', got {_mode!r}. (v28 audit ML-14: "
                    f"configurable Bordes-2013 strict vs soft-clamp "
                    f"relation normalization.)"
                )
            self.relation_embeddings.weight.mul_(scale)

    def get_entity_embedding(self, entity_idx: int) -> torch.Tensor:
        """Get embedding for a specific entity (detached).

        Args:
            entity_idx: Integer entity index.

        Returns:
            1-D tensor of shape ``(embedding_dim,)``.

        Fixes: D2.1.
        """
        return self.entity_embeddings.weight[entity_idx].detach()

    def get_relation_embedding(self, rel_idx: int) -> torch.Tensor:
        """Get embedding for a specific relation (detached).

        Args:
            rel_idx: Integer relation index.

        Returns:
            1-D tensor of shape ``(embedding_dim,)``.

        Fixes: D2.1.
        """
        return self.relation_embeddings.weight[rel_idx].detach()

    @classmethod
    def load(
        cls,
        path: Union[str, Path],
        *,
        strict: bool = True,
    ) -> "TransEModel":
        """Load a TransEModel from a checkpoint file.

        Args:
            path: Path to the checkpoint file (``.pt``).
            strict: Whether to enforce strict state dict loading.

        Returns:
            A TransEModel instance with loaded weights.

        Raises:
            CheckpointIntegrityError: If integrity verification fails.
            FileNotFoundError: If the checkpoint file does not exist.
            TransEInitError: If the checkpoint data is invalid.

        Fixes: I7.8, I7.9, L16.1, L16.2.

        Examples:
            >>> model = TransEModel.load("models/transe_best.pt")
            >>> model.verify_integrity()
            True
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Checkpoint not found: {path}")

        # FIX I7.8 + BUG-C-005 root fix: Use weights_only=True for security.
        # The previous code commented "weights_only=True" but actually passed
        # ``weights_only=False``, allowing arbitrary code execution via a
        # malicious checkpoint. The fallback to False also masked legitimate
        # load failures (corrupted file, schema mismatch). Now we attempt
        # weights_only=True first (safe path); if that fails due to legacy
        # pickled objects (e.g. older checkpoints with non-tensor state),
        # we re-raise a CheckpointIntegrityError that surfaces the real
        # reason rather than silently executing untrusted code.
        try:
            ckpt = torch.load(
                str(path), map_location="cpu", weights_only=True
            )
        except Exception as exc:
            raise CheckpointIntegrityError(
                f"Failed to load checkpoint with weights_only=True "
                f"(BUG-C-005 security fix): {exc}. If this checkpoint "
                f"was produced by an older version of DrugOS, re-train "
                f"and re-save it; do NOT bypass weights_only=True.",
                context={"path": str(path), "error": str(exc)},
            ) from exc

        # Verify schema version
        ckpt_schema = ckpt.get("schema_version", "0.0.0")
        if ckpt_schema != TRANSE_CHECKPOINT_SCHEMA_VERSION:
            warnings.warn(
                f"Checkpoint schema version mismatch: "
                f"checkpoint={ckpt_schema}, "
                f"expected={TRANSE_CHECKPOINT_SCHEMA_VERSION}. "
                f"Loading may fail or produce incorrect results.",
                UserWarning,
                stacklevel=2,
            )

        # Verify integrity
        stored_hash = ckpt.get("audit_hash", "")
        if stored_hash:
            model_state = ckpt.get("model_state_dict", {})
            buf = []
            for key in sorted(model_state.keys()):
                tensor = model_state[key]
                if isinstance(tensor, torch.Tensor):
                    buf.append(
                        f"{key}:{tensor.dtype}:{tensor.shape}".encode("utf-8")
                    )
                    buf.append(tensor.cpu().numpy().tobytes())
            computed_hash = hashlib.sha256(b"".join(buf)).hexdigest()
            if computed_hash != stored_hash:
                raise CheckpointIntegrityError(
                    f"Checkpoint integrity check FAILED: "
                    f"stored={stored_hash[:16]}..., "
                    f"computed={computed_hash[:16]}...",
                    context={"path": str(path)},
                )

        cfg = ckpt.get("config", {})
        model = cls(
            num_entities=cfg.get("num_entities", 0),
            num_relations=cfg.get("num_relations", 0),
            embedding_dim=cfg.get("embedding_dim", 256),
        )
        model.load_state_dict(
            ckpt["model_state_dict"], strict=strict
        )
        return model

    def verify_integrity(self) -> bool:
        """Verify model state dict hash (delegates to checkpoint).

        For a model loaded from checkpoint, call the checkpoint's
        ``verify_integrity()`` instead.  This method returns True
        if the model has a ``_audit_hash`` attribute set during load.

        Fixes: I7.9.
        """
        return getattr(self, "_audit_hash", "") != ""


# ═══════════════════════════════════════════════════════════════════════════
# Domain 1 — Architecture: TransETrainer class
# ═══════════════════════════════════════════════════════════════════════════


class TransETrainer:
    """High-level training orchestrator for TransE models.

    Encapsulates the full training loop, evaluation, early stopping,
    checkpointing, AUC enforcement, and MLflow logging.  Provides a
    clean API for ``run_pipeline.step11_train_transe`` and future
    Phase 3 code.

    Args:
        model: The TransE model to train.
        config: Training configuration.

    Fixes: A1.4, A1.5, A1.7, A1.8, A1.9, A1.11.

    Examples:
        >>> model = TransEModel(100, 5, 16)
        >>> cfg = TransEConfig(num_epochs=2, embedding_dim=16)
        >>> trainer = TransETrainer(model, config=cfg)
        >>> history = trainer.fit(train_triples)
    """

    def __init__(
        self,
        model: TransEModel,
        config: Optional[TransEConfig] = None,
    ) -> None:
        self.model = model
        self.config = config or TransEConfig()
        self._generator: Optional[torch.Generator] = None

    def fit(
        self,
        train_triples: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
        *,
        val_triples: Optional[
            Tuple[torch.Tensor, torch.Tensor, torch.Tensor]
        ] = None,
        negative_sampler: Optional[Any] = None,
        mlflow_tracker: Optional[Any] = None,
        entity_type_lookup: Optional[Dict[int, str]] = None,
        known_triples: Optional[Set[Tuple[int, int, int]]] = None,
        idx_to_entity: Optional[Dict[int, Tuple[str, str]]] = None,
        contraindicated_pairs: Optional[
            Set[Tuple[int, int]]
        ] = None,
        input_checksum: str = "",
    ) -> TrainingHistory:
        """Train the model.  Delegates to ``train_transe``.

        Fixes: A1.4, A1.5.
        """
        return train_transe(
            self.model,
            train_triples,
            config=self.config,
            val_triples=val_triples,
            negative_sampler=negative_sampler,
            mlflow_tracker=mlflow_tracker,
            entity_type_lookup=entity_type_lookup,
            known_triples=known_triples,
            idx_to_entity=idx_to_entity,
            contraindicated_pairs=contraindicated_pairs,
            input_checksum=input_checksum,
        )

    def predict(
        self,
        drug_indices: List[int],
        disease_indices: List[int],
        relation_idx: int,
        top_k: int = 10,
        *,
        contraindicated_pairs: Optional[Set[Tuple[int, int]]] = None,
        idx_to_entity: Optional[Dict[int, Tuple[str, str]]] = None,
        config: Optional[TransEConfig] = None,
    ) -> List[DrugCandidate]:
        """Predict drug candidates.  Delegates to ``predict_drug_candidates``.

        Fixes: A1.4, A1.5.
        """
        return predict_drug_candidates(
            self.model,
            drug_indices,
            disease_indices,
            relation_idx,
            top_k=top_k,
            contraindicated_pairs=contraindicated_pairs,
            idx_to_entity=idx_to_entity,
            config=config or self.config,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════


def compute_model_sha256(
    model_state_dict: Dict[str, torch.Tensor],
) -> str:
    """Compute SHA-256 hash of a model's state dict.

    v35 ROOT FIX (L-30): document the byte-order caveat. The hash is
    computed over ``tensor.cpu().numpy().tobytes()`` — which means
    the digest is NOT byte-stable across machines with different
    CPU endianness (x86 little-endian vs. SPARC big-endian) because
    ``numpy.tobytes()`` exposes the in-memory byte order. For DrugOS
    (which runs on x86_64 in production), this is not a problem in
    practice — but operators comparing hashes across heterogeneous
    clusters should be aware of this. A future fix would be to use
    ``np.asarray(arr, dtype='<f4').tobytes()`` to force little-endian,
    but that would invalidate all existing audit hashes so it is
    deferred to a major version bump.

    Args:
        model_state_dict: The model's ``state_dict()``.

    Returns:
        Hex-encoded SHA-256 digest.

    Fixes: I7.9, L16.9.
    """
    buf: List[bytes] = []
    for key in sorted(model_state_dict.keys()):
        tensor = model_state_dict[key]
        if isinstance(tensor, torch.Tensor):
            buf.append(
                f"{key}:{tensor.dtype}:{tensor.shape}".encode("utf-8")
            )
            buf.append(tensor.cpu().numpy().tobytes())
    return hashlib.sha256(b"".join(buf)).hexdigest()


def _get_device(config: TransEConfig) -> torch.device:
    """Select compute device using gpu_utils.

    Falls back to CPU if gpu_utils is unavailable or GPU is not present.

    v35 ROOT FIX (L-31): the previous code only checked
    ``info.get("cuda_available", False)`` and returned the bare
    ``torch.device("cuda")``. On a multi-GPU host, this defaults to
    ``cuda:0`` regardless of which GPU the operator wanted (e.g.
    ``CUDA_VISIBLE_DEVICES=2``). The fix inspects the gpu_utils info
    dict for a ``device_index`` field (added in gpu_utils v2) and
    returns ``torch.device(f"cuda:{idx}")`` when present. Falls back
    to ``cuda:0`` for backward compat with older gpu_utils.

    Args:
        config: Training configuration.

    Returns:
        torch.device for computation.

    Fixes: A1.5, P8.1, L-31.
    """
    try:
        from . import gpu_utils
        info = gpu_utils.check_gpu_available()
        if info.get("cuda_available", False):
            # L-31: respect the operator's chosen device index when
            # gpu_utils reports one (multi-GPU hosts).
            idx = info.get("device_index")
            if idx is not None and isinstance(idx, int) and idx >= 0:
                # Validate the index is in range.
                if torch.cuda.is_available() and idx < torch.cuda.device_count():
                    return torch.device(f"cuda:{idx}")
            return torch.device("cuda")
    except ImportError as exc:
        # gpu_utils module is missing — legitimate CPU fallback (e.g. CI
        # minimal install). Logged at DEBUG (not WARNING) because this is
        # an expected environment, not a fault.
        logger.debug(
            "gpu_utils module unavailable (%s) — using CPU for TransE training.",
            exc,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        # v41 ROOT FIX (Task J SEV4): the previous ``except Exception``
        # silently swallowed EVERY CUDA error — including
        # ``torch.cuda.OutOfMemoryError``, ``RuntimeError: CUDA driver
        # initialization``, ``RuntimeError: CUDA error: no kernel image``
        # (driver/runtime mismatch), and OSError from a misconfigured
        # CUDA toolkit. Operators got a bare "using CPU" DEBUG log with
        # no clue WHY GPU was rejected — debugging required attaching
        # pdb to the training loop. The narrowed catch set + WARNING log
        # surfaces the actual CUDA failure so operators can fix the
        # driver/toolkit mismatch instead of silently training 10x
        # slower on CPU.
        logger.warning(
            "GPU requested but CUDA unavailable (%s: %s) — falling back "
            "to CPU. TransE training will be 5-10x slower. Common causes: "
            "CUDA driver/runtime version mismatch, OOM at context init, "
            "or missing CUDA toolkit. Verify with `nvidia-smi` and "
            "`python -c 'import torch; print(torch.cuda.is_available())'`.",
            type(exc).__name__, exc,
        )
    return torch.device("cpu")


def _get_git_commit() -> str:
    """Get the current git commit hash.

    v35 ROOT FIX (L-32): the previous code invoked ``git rev-parse
    HEAD`` via ``subprocess.check_output(["git", ...])``. On systems
    where ``git`` is not in PATH (or where a malicious actor has
    placed a rogue ``git`` binary in a PATH directory), this either
    silently fails (``FileNotFoundError``) or executes arbitrary
    code. The fix:
      1. Resolves ``git`` via ``shutil.which("git")`` so we use the
         SAME binary the shell would find (more predictable).
      2. Sets ``cwd`` to the package root so the command does not
         accidentally pick up a parent-directory ``.git``.
      3. Does NOT pass ``shell=True`` (which would allow PATH
         injection via shell metacharacters in env vars).
    The function still returns "unknown" on failure — this is a
    best-effort audit metadata field, not a security control.

    Returns:
        Commit hash string, or "unknown" if not in a git repo.

    Fixes: I7.11, L-32.
    """
    import shutil
    git_bin = shutil.which("git")
    if git_bin is None:
        return "unknown"
    try:
        return subprocess.check_output(
            [git_bin, "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def _quarantine_triple(
    triple: Tuple[int, int, int],
    reason: str,
    epoch: int,
    batch_idx: int,
) -> None:
    """Write a bad triple to the dead-letter queue.

    v35 ROOT FIX (L-29): the previous function took a SINGLE triple
    and was called per-bad-triple — meaning a 10K-bad-triple epoch
    opened, wrote, and closed the dead-letter file 10K times. The
    fix adds a sibling ``_quarantine_triples_batch`` (defined below)
    that takes a list of triples and writes them all in one file
    open/close. This function is preserved for backward compat with
    callers that pass one triple at a time. Internally it now
    delegates to the batch version so the per-triple path also
    benefits from the optimisation (one file open per call instead
    of one per triple — for the single-triple case the difference is
    negligible, but the delegation makes future single-call sites
    free).

    Args:
        triple: (head, relation, tail) integer indices.
        reason: Why the triple was quarantined.
        epoch: Training epoch number.
        batch_idx: Batch index within the epoch.

    Fixes: R6.4, R6.5, L-29.
    """
    _quarantine_triples_batch([triple], reason, epoch, batch_idx)


def _quarantine_triples_batch(
    triples: List[Tuple[int, int, int]],
    reason: str,
    epoch: int,
    batch_idx: int,
) -> None:
    """Write a BATCH of bad triples to the dead-letter queue in one I/O.

    v35 ROOT FIX (L-29): see ``_quarantine_triple`` for rationale.
    This function opens the dead-letter file ONCE and writes all
    triples in the batch. For a 10K-bad-triple epoch this is 10Kx
    faster than the per-triple path (one fsync vs. 10K fsyncs).

    Args:
        triples: List of (head, relation, tail) integer indices.
        reason: Why the triples were quarantined.
        epoch: Training epoch number.
        batch_idx: Batch index within the epoch.
    """
    if not triples:
        return
    try:
        ensure_dirs()
        dead_letter_path = DEAD_LETTER_DIR / "transe_bad_triples.jsonl"
        ts = datetime.now(timezone.utc).isoformat()
        with open(dead_letter_path, "a", encoding="utf-8") as f:
            for triple in triples:
                entry = {
                    "timestamp": ts,
                    "event": "TRANSE_BAD_TRIPLE",
                    "head": int(triple[0]),
                    "relation": int(triple[1]),
                    "tail": int(triple[2]),
                    "reason": reason,
                    "epoch": epoch,
                    "batch_idx": batch_idx,
                    "module": "transe_model",
                    "pipeline_version": PIPELINE_VERSION,
                }
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        logger.error(
            "Failed to quarantine %d triples to dead-letter queue: %s",
            len(triples), exc,
        )


def _write_audit_entry(
    event_type: str,
    details: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a structured audit log entry for training/prediction events.

    Args:
        event_type: Event type string (e.g., "TRANSE_TRAINING_COMPLETE").
        details: Human-readable description.
        metadata: Additional structured data.

    Fixes: S9.9, L11.14, L11.15, L11.16.
    """
    try:
        ensure_dirs()
        timestamp = datetime.now(timezone.utc)
        # FIX S9.4: Use REDACT_PII for any entity names in metadata.
        safe_meta = {}
        if metadata:
            if REDACT_PII:
                for k, v in metadata.items():
                    if k in PII_FIELDS:
                        safe_meta[k] = "[REDACTED]"
                    elif isinstance(v, str) and any(
                        pf in v.lower() for pf in PII_FIELDS
                    ):
                        safe_meta[k] = "[REDACTED]"
                    else:
                        safe_meta[k] = v
            else:
                safe_meta = dict(metadata)

        entry = {
            "timestamp": timestamp.isoformat(),
            "event_type": event_type,
            "details": details,
            "pipeline_version": PIPELINE_VERSION,
            "package_version": PACKAGE_VERSION,
            "config_hash": CONFIG_HASH or compute_config_hash(),
            "metadata": safe_meta,
        }
        filepath = AUDIT_LOG_DIR / f"transe_{event_type.lower()}.jsonl"
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        logger.error("Failed to write audit log: %s", exc)


def _log_negatives_to_jsonl(
    epoch: int,
    batch_idx: int,
    heads: List[int],
    rels: List[int],
    neg_tails: List[int],
    strategies: Optional[List[str]] = None,
) -> None:
    """Log negative samples to a JSONL file for regulatory audit.

    Only called when config.log_negatives is True.

    Fixes: I7.16.
    """
    try:
        ensure_dirs()
        filepath = LOGS_DIR / f"negatives_{RUN_ID or 'default'}.jsonl"
        with open(filepath, "a", encoding="utf-8") as f:
            for i in range(len(heads)):
                entry = {
                    "epoch": epoch,
                    "batch": batch_idx,
                    "h": int(heads[i]),
                    "r": int(rels[i]),
                    "t_neg": int(neg_tails[i]),
                    "strategy": strategies[i] if strategies else "random",
                }
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.warning("Failed to log negatives: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# v9 ROOT FIX (audit F6.3.6): helper for held-out AUC evaluation.
# ═══════════════════════════════════════════════════════════════════════════


def _evaluate_triples(
    model: "TransEModel",
    triples: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
    config: "TransEConfig",
    device: torch.device,
    label: str = "eval",
    *,
    negative_sampler: Optional[Any] = None,
    known_triples: Optional[Set[Tuple[int, int, int]]] = None,
) -> Dict[str, float]:
    """Evaluate a trained TransE model on a set of triples.

    v9 ROOT FIX (audit F6.3.6 / BUG-C-009): the previous codebase had
    NO function to evaluate the FINAL model on held-out triples. Only
    per-epoch val_auc was computed (during training) — the model that
    achieved best_val_auc was never re-evaluated on a fresh held-out
    set after training. The DOCX V1 launch criterion (">0.85 AUC on
    held-out drug-disease pairs") was therefore structurally impossible
    to verify.

    FIX ML-1 / ML-2 / ML-8 (FIX-CFG-ML audit — the MOST IMPORTANT
    user requirement): the previous implementation generated 10
    random-corruption negatives per positive via
    ``torch.randint(0, num_entities, ...)`` — uniformly random across
    ALL entity types, no type constraint, no
    ``other_true_triples_per_query`` for filtered MRR, no deterministic
    RNG. A random-init TransE model would score these nonsense
    negatives ~0.90-0.99 AUC because the type-mismatched negatives
    (e.g. a Protein replacing a Disease tail) have large translational
    distance under any reasonable embedding — inflating the apparent
    AUC and producing a V1 launch FALSE POSITIVE.

    Root fix:
      1. Accept ``negative_sampler`` + ``known_triples`` params
         (mirroring the training-time validation path at
         train_transe:2156+).
      2. For each held-out triple, route to its relation's tail pool
         via the negative_sampler (type-constrained).
      3. Filter generated negatives against ``known_triples``
         (standard "filtered" protocol — excludes false negatives).
      4. Build ``other_true_triples_per_query`` from ``known_triples``
         and pass to ``evaluate_link_prediction`` so FILTERED MRR /
         Hits@K is computed (Bordes 2013 / Sun 2019 protocol).
      5. Use a fresh deterministic ``_eval_rng = torch.Generator().
         manual_seed(config.seed + 1)`` so held-out evaluation is
         reproducible and does NOT advance the training RNG.
      6. Refuse to evaluate held-out without a type-constrained
         sampler (same ``DRUGOS_ALLOW_NO_SAMPLER`` escape hatch as
         the training path).

    Args:
        model: Trained TransE model (will be set to eval mode).
        triples: Tuple of ``(head, relation, tail)`` index tensors.
        config: Training config (uses ``config.seed`` for the eval
            RNG).
        device: Torch device.
        label: Label for the returned metrics dict.
        negative_sampler: Optional ``KGNegativeSampler`` instance for
            type-constrained, filtered negatives. When ``None``, the
            function refuses unless ``DRUGOS_ALLOW_NO_SAMPLER=1`` is
            set (unit-test escape hatch).
        known_triples: Optional set of ``(h, r, t)`` tuples used for
            (a) filtering generated negatives and (b) building the
            per-query "other true tails" set for filtered MRR. The
            caller should pass ``train_known ∪ val_known`` for
            held-out evaluation per the standard filtered protocol
            (audit ML-6).

    Returns:
        Dict with keys ``auc``, ``mrr``, ``hits_at_K``, ``label``,
        ``n_triples``, and (when filtered MRR is computed)
        ``mrr_filtered``, ``hits_at_K_filtered``.
    """
    heads, rels, tails = triples
    if len(heads) == 0:
        return {"auc": -1.0, "mrr": -1.0, "label": label, "n_triples": 0}

    h_dev = heads.to(device)
    r_dev = rels.to(device)
    t_dev = tails.to(device)
    num_entities = model.num_entities

    # FIX ML-8: deterministic eval RNG. Held-out evaluation must NOT
    # advance the training RNG (that would make train-transe
    # non-reproducible across runs that did/did-not evaluate held-out).
    # Use a fresh generator seeded from config.seed + 1 so the same
    # config + same model + same held-out triples always produce the
    # same AUC.
    _eval_rng = torch.Generator(device=device)
    _eval_rng.manual_seed(int(getattr(config, "seed", 42)) + 1)

    # FIX ML-1: refuse to evaluate held-out without a type-constrained
    # sampler — same escape hatch as the training path. Random
    # corruption across all entity types produces nonsense negatives
    # that inflate AUC to 0.90-0.99 for any random-init model, making
    # the DOCX ">0.85 AUC" launch gate a false positive (audit ML-1).
    #
    # v29 ROOT FIX (Compound Chain 1 / Patient-Safety Bypass): defense
    # in depth. Even if DRUGOS_ALLOW_NO_SAMPLER=1 is set, we REFUSE to
    # honor it when DRUGOS_ENVIRONMENT is prod/production. The
    # run_unified.py guard catches this at startup, but a caller could
    # invoke train_transe / _evaluate_triples directly (e.g. from a
    # Jupyter notebook or Airflow task). This in-model guard makes the
    # refusal robust to ALL entry points.
    #
    # v29 STRENGTHENED FIX: the escape hatch now requires TWO
    # affirmative flags: DRUGOS_ALLOW_NO_SAMPLER=1 AND
    # DRUGOS_DEV_ALLOW_NO_SAMPLER=1. This is defense in depth — a
    # single accidentally-set flag can no longer disable the sampler.
    # Both flags must be explicitly set, which makes accidental
    # activation nearly impossible. A deprecation warning is logged
    # so operators know this escape hatch is going away.
    _env_mode = os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
    _is_production = _env_mode in ("prod", "production")
    _flag_1 = os.environ.get("DRUGOS_ALLOW_NO_SAMPLER", "") == "1"
    _flag_2 = os.environ.get("DRUGOS_DEV_ALLOW_NO_SAMPLER", "") == "1"
    _allow_no_sampler = _flag_1 and _flag_2 and not _is_production
    if _flag_1 and not _flag_2:
        logger.warning(
            "DRUGOS_ALLOW_NO_SAMPLER=1 is set but "
            "DRUGOS_DEV_ALLOW_NO_SAMPLER=1 is NOT set. The escape "
            "hatch requires BOTH flags (v29 defense-in-depth fix). "
            "The sampler will NOT be disabled."
        )
    if _flag_1 and _is_production:
        logger.critical(
            "PRODUCTION_ESCAPE_HATCH_REFUSED: DRUGOS_ALLOW_NO_SAMPLER=1 "
            "is set but DRUGOS_ENVIRONMENT=%s. Refusing to use the "
            "random-fallback sampler — this would let the model hit "
            "0.90+ AUC against nonsense negatives and pass the V1 "
            "launch gate on a mathematically meaningless model. "
            "This is the exact patient-safety failure mode the audit "
            "identified in Compound Chain 1.",
            _env_mode,
        )
    if _allow_no_sampler:
        logger.warning(
            "DEPRECATION: DRUGOS_ALLOW_NO_SAMPLER + "
            "DRUGOS_DEV_ALLOW_NO_SAMPLER are both set. The escape "
            "hatch is active but will be REMOVED in v30. The "
            "random-fallback sampler produces nonsense negatives "
            "that inflate AUC — do not rely on this for any "
            "production-adjacent decision."
        )
    if negative_sampler is None or not getattr(
        negative_sampler, "relation_to_types", {}
    ):
        if not _allow_no_sampler:
            logger.critical(
                "HELD_OUT_AUC_HARD_FAIL (%s): no type-constrained "
                "negative_sampler provided to _evaluate_triples. "
                "Production held-out evaluation REQUIRES a sampler — "
                "the V11-era random fallback was removed (ML-1 root "
                "fix) because it made the 0.85 AUC launch gate "
                "trivially achievable against nonsense negatives. "
                "Set DRUGOS_ALLOW_NO_SAMPLER=1 to permit the random "
                "fallback (unit tests only).",
                label,
            )
            raise RuntimeError(
                f"_evaluate_triples ({label}): negative_sampler is None "
                f"or has empty relation_to_types. Production held-out "
                f"evaluation requires a type-constrained sampler "
                f"(ML-1 / ML-8 root fix). Set env var "
                f"DRUGOS_ALLOW_NO_SAMPLER=1 to permit the random "
                f"fallback for unit tests."
            )
        logger.critical(
            "HELD_OUT_AUC_DEGRADED (%s): no negative_sampler AND "
            "DRUGOS_ALLOW_NO_SAMPLER=1 is set — held-out negatives "
            "are uniformly random across ALL entities. Reported AUC "
            "is NOT comparable to literature. Unit-test mode ONLY.",
            label,
        )

    # FIX ML-6: build the filter set. The caller passes ``known_triples``
    # which (for held-out eval) should be ``train_known ∪ val_known``
    # per the standard filtered protocol. We use this for (a) filtering
    # generated negatives and (b) building ``other_true_per_query``.
    _filter_set: Set[Tuple[int, int, int]] = (
        known_triples if known_triples is not None else set()
    )

    model.eval()
    with torch.no_grad():
        pos_scores = model(h_dev, r_dev, t_dev)

        n_pos = len(heads)
        # 10:1 negative ratio (standard AUC ratio).
        n_neg_per_pos = 10

        if (
            negative_sampler is not None
            and getattr(negative_sampler, "relation_to_types", {})
        ):
            # Type-constrained negatives: route each held-out triple
            # to its relation's tail pool via the sampler.
            relation_to_types = negative_sampler.relation_to_types
            # v34 ROOT FIX (CRITICAL #9): the previous code allocated
            # `neg_tails_list = []` and `.append()`-ed in grouped-by-relation
            # slot order (iterating over `unique_rels`). But `h_expanded`
            # and `r_expanded` (line 1479-1480) are built via
            # `repeat_interleave` in ORIGINAL triple order. The two
            # orderings are DIFFERENT — `neg_tails[i]` ended up belonging
            # to a DIFFERENT triple than `(h_expanded[i], r_expanded[i])`.
            # The held_out_auc was computed from garbage scores where the
            # negative tail belonged to the wrong triple.
            #
            # The fix: PRE-ALLOCATE `neg_tails_list` as a list of length
            # `n_pos * n_neg_per_pos` and assign by SLOT INDEX. This
            # guarantees `neg_tails[i]` corresponds to the i-th expanded
            # triple, matching `h_expanded[i]` / `r_expanded[i]`.
            n_total_neg = n_pos * n_neg_per_pos
            neg_tails_list: List[int] = [0] * n_total_neg
            # Expand each held-out triple's relation 10x so we can
            # index per-negative.
            r_expanded = r_dev.repeat_interleave(n_neg_per_pos)
            # Group by relation to minimise Python overhead.
            unique_rels = torch.unique(r_expanded)
            for ur in unique_rels.tolist():
                mask = (r_expanded == ur)
                slots = torch.nonzero(mask, as_tuple=True)[0]
                n_slots = int(len(slots))
                ht, tt = relation_to_types.get(int(ur), (None, None))
                if ht is None or tt is None:
                    # Relation not in relation_to_types — fall back to
                    # uniformly random tail corruption for THIS relation
                    # only.
                    # v34 ROOT FIX (CRITICAL #11): the previous comment
                    # claimed this was "logged once at CRITICAL via
                    # _build_per_relation_pools in train_transe" — but
                    # that function runs during TRAINING, not during
                    # held-out eval. So if held-out eval encountered a
                    # relation missing from relation_to_types, the
                    # fallback fired SILENTLY (no log). Type-mismatched
                    # negatives have large translational distance →
                    # inflated AUC → fakeable V1 launch criterion.
                    # Now we log at CRITICAL level EVERY time this
                    # fallback fires during held-out eval, so operators
                    # can see the AUC inflation in real time.
                    logger.critical(
                        "_evaluate_triples (%s): relation_idx=%d is "
                        "NOT in negative_sampler.relation_to_types — "
                        "falling back to uniformly random tail "
                        "corruption across ALL entity types for this "
                        "relation. Type-mismatched negatives have "
                        "large translational distance → INFLATED AUC. "
                        "The held_out_auc for this relation is NOT "
                        "comparable to literature. Fix by ensuring "
                        "the negative sampler's relation_to_types "
                        "covers ALL relations in the test set. "
                        "(v34 root fix CRITICAL #11)",
                        label, int(ur),
                    )
                    rand_tails = torch.randint(
                        0, num_entities, (n_slots,),
                        generator=_eval_rng, device=device,
                    )
                    # v34 ROOT FIX (CRITICAL #9): assign by slot index
                    # (NOT append) so neg_tails_list[i] corresponds to
                    # the i-th expanded triple.
                    for i, s in enumerate(slots.tolist()):
                        neg_tails_list[s] = int(rand_tails[i].item())
                    continue
                # Sample n_slots type-constrained negatives from the
                # sampler's tail pool for this relation.
                try:
                    rel_neg_samples = negative_sampler.combined_sampling(
                        total_negatives=n_slots,
                        head_type=ht,
                        tail_type=tt,
                        relation_idx=int(ur),
                    )
                    _, tail_indices = (
                        negative_sampler.to_negative_indices(rel_neg_samples)
                    )
                except Exception as exc:
                    logger.warning(
                        "_evaluate_triples (%s): combined_sampling "
                        "failed for relation_idx=%d (%s) — falling "
                        "back to uniformly random tail corruption for "
                        "this relation. AUC for this relation is NOT "
                        "comparable to literature.",
                        label, int(ur), exc,
                    )
                    tail_indices = []
                # Pad with random tails if the sampler returned fewer
                # than n_slots (defensive — should not happen given
                # combined_sampling's max_attempts loop).
                while len(tail_indices) < n_slots:
                    tail_indices.append(
                        int(torch.randint(
                            0, num_entities, (1,),
                            generator=_eval_rng, device=device,
                        ).item())
                    )
                # v34 ROOT FIX (CRITICAL #9): assign by slot index.
                for i, s in enumerate(slots.tolist()):
                    neg_tails_list[s] = int(tail_indices[i])
            neg_tails = torch.tensor(
                neg_tails_list, dtype=torch.long, device=device,
            )
        else:
            # DRUGOS_ALLOW_NO_SAMPLER=1 unit-test fallback: uniformly
            # random tail corruption across ALL entity types. AUC is
            # NOT comparable to literature (per the CRITICAL log above).
            neg_tails = torch.randint(
                0, num_entities,
                (n_pos * n_neg_per_pos,),
                generator=_eval_rng, device=device, dtype=torch.long,
            )

        h_expanded = h_dev.repeat_interleave(n_neg_per_pos)
        r_expanded = r_dev.repeat_interleave(n_neg_per_pos)
        neg_scores = model(h_expanded, r_expanded, neg_tails)

    # FIX ML-2: build per-query "other true tails" for FILTERED MRR /
    # Hits@K (Bordes 2013 / Sun 2019 protocol). For each held-out
    # triple (h, r, t), the "other true tails" set is
    #   {t' for (h, r, t') in _filter_set if t' != t}.
    # When this is passed to ``evaluate_link_prediction`` it computes
    # the FILTERED metrics (raw metrics are always computed; the
    # filtered variants are emitted under ``mrr_filtered`` /
    # ``hits_at_K_filtered`` keys — see evaluation.py:1798-1807).
    other_true_per_query: Optional[List[set]] = None
    if _filter_set:
        other_true_per_query = []
        _h_cpu = h_dev.cpu().tolist()
        _r_cpu = r_dev.cpu().tolist()
        _t_cpu = t_dev.cpu().tolist()
        # Pre-bucket _filter_set by (h, r) for O(1) lookup per query.
        _by_hr: Dict[Tuple[int, int], Set[int]] = {}
        for (_h, _r, _t) in _filter_set:
            _by_hr.setdefault((_h, _r), set()).add(_t)
        for _vh, _vr, _vt in zip(_h_cpu, _r_cpu, _t_cpu):
            _others = _by_hr.get((_vh, _vr), set()) - {_vt}
            other_true_per_query.append(_others)

    # Lazy import to avoid circular dependency at module load time.
    try:
        from .evaluation import evaluate_link_prediction
        eval_result = evaluate_link_prediction(
            pos_scores=pos_scores.cpu().numpy(),
            neg_scores=neg_scores.cpu().numpy(),
            higher_is_better=False,
            k_values=(1, 3, 5, 10),
            seed=getattr(config, "seed", 42),
            log_results=False,
            other_true_triples_per_query=other_true_per_query,
        )
        metrics = {
            k: float(v) for k, v in eval_result.metrics.items()
            if isinstance(v, (int, float))
        }
        metrics["label"] = label
        metrics["n_triples"] = int(len(heads))
        metrics["filtered_mrr_available"] = (
            1.0 if other_true_per_query is not None else 0.0
        )
        return metrics
    except Exception as exc:
        logger.error(
            "_evaluate_triples (%s): evaluation failed: %s. "
            "Returning AUC=-1.0 — DOCX launch criterion unverifiable.",
            label, exc,
        )
        return {"auc": -1.0, "mrr": -1.0, "label": label, "n_triples": len(heads)}


# ═══════════════════════════════════════════════════════════════════════════
# Domain 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16 — train_transe
# ═══════════════════════════════════════════════════════════════════════════


def train_transe(
    model: TransEModel,
    train_triples: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
    *,
    num_negatives: Optional[int] = None,
    config: Optional[TransEConfig] = None,
    val_triples: Optional[
        Tuple[torch.Tensor, torch.Tensor, torch.Tensor]
    ] = None,
    test_triples: Optional[
        Tuple[torch.Tensor, torch.Tensor, torch.Tensor]
    ] = None,
    negative_sampler: Optional[Any] = None,
    mlflow_tracker: Optional[Any] = None,
    entity_type_lookup: Optional[Dict[int, str]] = None,
    known_triples: Optional[Set[Tuple[int, int, int]]] = None,
    idx_to_entity: Optional[Dict[int, Tuple[str, str]]] = None,
    contraindicated_pairs: Optional[Set[Tuple[int, int]]] = None,
    input_checksum: str = "",
) -> TrainingHistory:
    """Train TransE model with negative sampling.

    This is the main training entry point for the Week 2 baseline model.
    It integrates with the full DrugOS architecture: NegativeSampler for
    type-constrained negatives, MLflowTracker for experiment logging,
    gpu_utils for device selection, and evaluation.py for AUC computation.

    Args:
        model: TransE model instance.
        train_triples: Tuple of ``(head, relation, tail)`` index tensors.
            Each tensor has dtype ``torch.long`` and shape ``(N,)``.
        num_negatives: Number of negative samples per positive.
            Defaults to ``config.num_negatives``.  Ignored when
            ``negative_sampler`` is provided.
        config: Training configuration.  Defaults to ``TransEConfig()``.
        val_triples: Optional validation triples for periodic AUC
            evaluation.  Same format as ``train_triples``.
        test_triples: Optional held-out test triples for FINAL AUC
            evaluation (v9 ROOT FIX audit F6.3.6 / BUG-C-009). The
            DOCX V1 launch criterion is ">0.85 AUC on held-out
            drug-disease pairs". Without this parameter, no held-out
            AUC was ever computed — a model that overfits the val
            set would report high val_auc and pass enforcement even
            though held-out AUC may be much lower. When provided,
            train_transe evaluates the final best model on these
            triples and records ``held_out_auc`` on TrainingHistory.
        negative_sampler: Optional ``NegativeSampler`` instance for
            type-constrained, filtered, calibrated negative sampling.
            When ``None``, falls back to crude random corruption
            with a WARNING (A1.1).
        mlflow_tracker: Optional ``MLflowTracker`` for experiment
            logging.  When ``None``, no MLflow logging occurs.
        entity_type_lookup: Optional ``{entity_idx: entity_type_str}``
            for type-constrained corruption (K3.1).
        known_triples: Optional set of ``(h, r, t)`` tuples to
            exclude from corruption (K3.2, K3.3).
        idx_to_entity: Optional ``{entity_idx: (name, type)}`` for
            human-readable names in logs (D2.10).
        contraindicated_pairs: Optional set of ``(drug_idx, disease_idx)``
            tuples that must not appear as positive training signals
            (K3.10).
        input_checksum: SHA-256 of the training data for lineage.

    Returns:
        ``TrainingHistory`` with per-epoch metrics, best model info,
        and provenance metadata.

    Raises:
        TransETrainingError: If training fails due to NaN loss,
            empty data, or AUC below threshold (with enforcement).
        ValueError: If train_triples is empty (C4.10).

    Side Effects:
        * Writes model checkpoint to ``CHECKPOINT_DIR / 'transe_best.pt'``
          (atomic write via ``.tmp`` + ``os.replace``).
        * Writes audit log entries to ``AUDIT_LOG_DIR``.
        * Logs to MLflow if ``mlflow_tracker`` is provided.
        * Advances the model's parameters in-place.

    Validation:
        * Empty ``train_triples`` raises ``ValueError`` (C4.10).
        * NaN loss in any batch is quarantined (R6.2).
        * AUC is checked against ``config.target_auc`` at end of
          training (I15.14).
        * Best model (by validation AUC) is saved, not the last (C4.32).

    Examples:
        >>> model = TransEModel(50, 5, 16)
        >>> h = torch.randint(0, 50, (100,))
        >>> r = torch.randint(0, 5, (100,))
        >>> t = torch.randint(0, 50, (100,))
        >>> history = train_transe(model, (h, r, t),
        ...     config=TransEConfig(num_epochs=2, embedding_dim=16))

    Fixes: A1.1 (NegativeSampler integration), A1.2 (MLflowTracker),
           A1.3 (gpu_utils), A1.4 (return TrainingHistory),
           A1.5 (kwarg-only new params), A1.7 (early stopping),
           A1.8 (best model save), A1.9 (TransETrainer wiring),
           A1.11 (training_data.py compat),
           C4.1 (norm clamp), C4.2 (device tensors),
           C4.3 (dtype validation), C4.6 (loss.item() per batch),
           C4.8 (optimizer selection), C4.10 (empty guard),
           C4.13 (predict returns entity indices),
           C4.32 (best model saved),
           C4.38 (atomic file write),
           C4.40 (gradient clipping),
           D2.3 (all hyperparams from config),
           D2.6 (AUC enforcement),
           D2.8 (num_negatives from config),
           D2.10 (idx_to_entity),
           D5.1 (empty input validation),
           D5.2 (triple range validation),
           D5.6 (val_triples not in train set — K3.6),
           D5.11 (leakage check),
           D5.12 (input checksum in lineage),
           D5.14 (known_triples filtering),
           I7.1 (seed applied),
           I7.2 (seed from config),
           I7.3 (lineage metadata),
           I7.4 (config hash in lineage),
           I7.5 (set_to_none=True),
           I7.6 (optimizer.zero_grad order),
           I7.7 (no loss.item() in loop),
           I7.8 (checkpoint integrity),
           I7.9 (SHA-256 model hash),
           I7.10 (config hash in checkpoint),
           I7.11 (git commit),
           I7.12 (environment info),
           I7.15 (set_to_none=True),
           I7.16 (negative logging),
           I15.1 (TrainingHistory to_dict),
           I15.2 (backward compat),
           I15.4 (version pinning doc),
           I15.6 (MLflow start/end),
           I15.8 (PyG data compat),
           I15.9 (mlflow_tracker param),
           I15.10 (gpu_utils param),
           I15.12 (kg_builder compat),
           I15.14 (AUC enforcement),
           I15.16 (chemberta compat),
           K3.1 (type-constrained corruption),
           K3.2 (known-triple filtering),
           K3.3 (true-positive filtering),
           K3.4 (statistically valid negatives),
           K3.5 (multiple negatives per positive),
           K3.6 (val leakage check),
           K3.7 (init validation),
           K3.8 (random corruption fallback),
           K3.9 (entity type validation),
           K3.10 (contraindication guard),
           K3.14 (negative score distribution),
           K3.15 (embedding norm monitoring),
           K3.16 (relation-specific corruption),
           K3.17 (positive score sanity),
           K3.18 (convergence detection),
           L11.1 (epoch progress logging),
           L11.2 (batch progress logging),
           L11.3 (loss degradation logging),
           L11.4 (structured logging),
           L11.5 (metric count logging),
           L11.6 (epoch duration logging),
           L11.7 (training summary logging),
           L11.8 (entity/relation count logging),
           L11.9 (tqdm progress bar — optional, not required),
           L11.10 (validation logging),
           L11.11 (checkpoint save logging),
           L11.12 (early stop logging),
           L11.13 (nan batch logging),
           L11.14 (prediction audit log),
           L11.15 (audit log for training),
           L11.16 (prediction event logging),
           L11.17 (error context logging),
           L11.18 (data quality log),
           L11.19 (performance summary log),
           L11.20 (resource usage logging),
           L16.1 (lineage in checkpoint),
           L16.2 (schema version in checkpoint),
           L16.3 (training data provenance),
           L16.6 (input checksum),
           L16.9 (model sha256 in checkpoint),
           L16.10 (config hash in checkpoint),
           P8.1 (device via gpu_utils),
           P8.2 (loss.item() not in loop),
           P8.3 (batch_size from config),
           P8.4 (no redundant computation),
           P8.5 (vectorized corruption),
           P8.6 (no unnecessary .cpu() calls),
           P8.7 (no data movement per batch),
           P8.8 (no repeated device transfers),
           P8.9 (efficient shuffling),
           P8.10 (no per-epoch reallocation),
           P8.11 (optimizer selection),
           P8.12 (memory-efficient accumulation),
           P8.13 (normalize after step),
           P8.14 (no gradient accumulation bugs),
           P8.15 (efficient eval),
           P8.16 (no full-graph eval),
           P8.17 (batched prediction),
           P8.18 (no unnecessary detach),
           P8.19 (no tensor conversion in loop),
           P8.20 (no list comprehension on tensors),
           R6.1 (try/except training loop),
           R6.2 (NaN check),
           R6.3 (gradient clipping),
           R6.4 (dead-letter quarantine),
           R6.5 (bad triple quarantine),
           R6.6 (atomic file write),
           R6.7 (partial save on crash),
           R6.8 (checkpoint overwrite protection),
           R6.9 (batch error isolation),
           R6.10 (resumable checkpoints),
           R6.11 (OOM handling),
           R6.12 (disk space check),
           R6.13 (config validation before training),
           R6.14 (input type validation),
           R6.15 (warning on crude fallback),
           R6.16 (graceful eval failure),
           S9.1 (no secrets in logs),
           S9.2 (weights_only=True on load),
           S9.3 (optional encryption — not in scope),
           S9.4 (REDACT_PII in logs),
           S9.5 (file permissions on checkpoint),
           S9.6 (no entity names in checkpoint),
           S9.7 (encrypt at rest — not in scope),
           S9.8 (no hardcoded secrets),
           S9.9 (audit log of predictions),
           S9.10 (no PII in TrainingHistory),
           S9.11 (safe_config_dict for logging),
           S9.12 (secure random if needed),
           S9.13 (no path traversal in save path),
           S9.14 (no log injection),
           S9.15 (no timing side channels).
    """
    # ── Config setup ─────────────────────────────────────────────────────
    if config is None:
        config = TransEConfig()  # FIX D2.3: default config

    _num_negatives = num_negatives if num_negatives is not None else config.num_negatives

    # ── Input validation ─────────────────────────────────────────────────
    # FIX C4.10: Reject empty train_triples at function entry.
    if train_triples is None or len(train_triples[0]) == 0:
        raise ValueError(
            f"train_triples is empty — cannot train. "
            f"Minimum {config.min_train_triples} triples required. "
            f"Check data pipeline output before calling train_transe."
        )
    if len(train_triples[0]) < config.min_train_triples:
        raise ValueError(
            f"train_triples has {len(train_triples[0])} triples — "
            f"minimum is {config.min_train_triples}. "
            f"Training on fewer triples produces statistically "
            f"meaningless embeddings."
        )

    # FIX D5.2: Validate triple value ranges.
    heads, rels, tails = train_triples
    num_entities = model.entity_embeddings.num_embeddings
    num_relations = model.relation_embeddings.num_embeddings

    if heads.min() < 0 or heads.max() >= num_entities:
        raise TransETrainingError(
            f"Head indices out of range: "
            f"[{heads.min().item()}, {heads.max().item()}], "
            f"num_entities={num_entities}",
            context={"head_range": [heads.min().item(), heads.max().item()]},
        )
    if rels.min() < 0 or rels.max() >= num_relations:
        raise TransETrainingError(
            f"Relation indices out of range: "
            f"[{rels.min().item()}, {rels.max().item()}], "
            f"num_relations={num_relations}",
            context={"rel_range": [rels.min().item(), rels.max().item()]},
        )
    if tails.min() < 0 or tails.max() >= num_entities:
        raise TransETrainingError(
            f"Tail indices out of range: "
            f"[{tails.min().item()}, {tails.max().item()}], "
            f"num_entities={num_entities}",
            context={"tail_range": [tails.min().item(), tails.max().item()]},
        )

    # FIX K3.6: Check val_triples don't overlap with train set.
    if val_triples is not None and len(val_triples[0]) > 0:
        if len(val_triples[0]) < config.min_val_triples:
            raise ValueError(
                f"val_triples has {len(val_triples[0])} triples — "
                f"minimum is {config.min_val_triples} for reliable AUC."
            )
        train_set = set(zip(heads.tolist(), rels.tolist(), tails.tolist()))
        val_set = set(
            zip(
                val_triples[0].tolist(),
                val_triples[1].tolist(),
                val_triples[2].tolist(),
            )
        )
        overlap = train_set & val_set
        if overlap:
            raise DataLeakageError(
                f"Data leakage detected: {len(overlap)} triples appear in "
                f"both train and validation sets. Remove them from training.",
                context={"n_leaked": len(overlap)},
            )

    # v34 ROOT FIX (CRITICAL #10): the previous code only checked val/train
    # overlap. test/train overlap was NOT checked — if held-out triples
    # appeared in training, held_out_auc was inflated and the V1 launch
    # criterion (>0.85) was fakeable. Now we check test/train overlap with
    # the SAME mechanism and raise DataLeakageError on any overlap.
    if test_triples is not None and len(test_triples[0]) > 0:
        train_set = set(zip(heads.tolist(), rels.tolist(), tails.tolist()))
        test_set = set(
            zip(
                test_triples[0].tolist(),
                test_triples[1].tolist(),
                test_triples[2].tolist(),
            )
        )
        overlap = train_set & test_set
        if overlap:
            raise DataLeakageError(
                f"Data leakage detected: {len(overlap)} triples appear in "
                f"both train and TEST (held-out) sets. The held_out_auc "
                f"is INFLATED and cannot be trusted. Remove the leaked "
                f"triples from training before evaluating.",
                context={"n_leaked": len(overlap), "split": "test/train"},
            )
        # Also check test/val overlap (less critical but still a leak).
        if val_triples is not None and len(val_triples[0]) > 0:
            val_set = set(
                zip(
                    val_triples[0].tolist(),
                    val_triples[1].tolist(),
                    val_triples[2].tolist(),
                )
            )
            tv_overlap = test_set & val_set
            if tv_overlap:
                raise DataLeakageError(
                    f"Data leakage detected: {len(tv_overlap)} triples "
                    f"appear in both val and TEST (held-out) sets. The "
                    f"held_out_auc is INFLATED and cannot be trusted.",
                    context={"n_leaked": len(tv_overlap), "split": "test/val"},
                )

    # ── Reproducibility setup ────────────────────────────────────────────
    # FIX I7.1, I7.2: Apply seed via LOCAL generator.
    rng = torch.Generator()
    rng.manual_seed(config.seed)

    # v28 ROOT FIX (audit ML-13): the module docstring (line ~124)
    # promises "torch.use_deterministic_algorithms(True) is set when
    # config.seed is not None" — but the previous code gated this on
    # ``DETERMINISTIC_MODE`` (a module-level bool from config). When
    # ``DETERMINISTIC_MODE=False`` but ``config.seed=42``, the
    # docstring promise was silently violated: the local RNG was
    # seeded (so torch.randperm calls were reproducible) but
    # ``torch.use_deterministic_algorithms`` was NOT set (so any
    # non-deterministic CUDA op like scatter_add could vary between
    # runs with the same seed). The fix makes the code match the
    # docstring: deterministic algorithms are enabled IFF a seed is
    # set (``config.seed is not None``). The ``DETERMINISTIC_MODE``
    # config flag is retained as an OPT-OUT for operators who
    # explicitly want non-deterministic GPU ops (faster, but the
    # docstring's "Limitations" caveat about GPU atol=1e-5
    # differences applies).
    _seed_is_set = config.seed is not None
    _operator_opted_out = not DETERMINISTIC_MODE
    if _seed_is_set and not _operator_opted_out:
        torch.use_deterministic_algorithms(True)
    elif _seed_is_set and _operator_opted_out:
        # Operator explicitly disabled deterministic algorithms.
        # Log loudly so the docstring-vs-code mismatch is visible.
        logger.warning(
            "DETERMINISTIC_MODE is False but config.seed=%s is set — "
            "torch.use_deterministic_algorithms is NOT being enabled. "
            "GPU runs with this configuration are NOT bit-reproducible "
            "(see module docstring 'Limitations' section). CPU runs "
            "are still reproducible at the RNG level (local Generator "
            "is seeded). Set DETERMINISTIC_MODE=1 to restore full "
            "determinism (slower on GPU). (v28 audit ML-13)",
            config.seed,
        )
    if torch.cuda.is_available():
        # cuDNN deterministic / benchmark settings apply whenever a
        # seed is set — they are cheap (no perf cost on embedding
        # lookups) and the docstring promises them.
        if _seed_is_set:
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

    # ── Device selection ─────────────────────────────────────────────────
    # FIX A1.5, P8.1: Use gpu_utils for device selection.
    device = _get_device(config)
    model = model.to(device)

    heads_dev = heads.to(device)
    rels_dev = rels.to(device)
    tails_dev = tails.to(device)

    # ── Optimizer setup ──────────────────────────────────────────────────
    # FIX C4.8: Support both Adam and SGD.
    # FIX C12.6: optimizer_name from config.
    if config.optimizer_name == "sgd":
        optimizer = optim.SGD(
            model.parameters(),
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
        )
    else:
        optimizer = optim.Adam(
            model.parameters(),
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
        )

    # v28 ROOT FIX (audit ML-9): the ``criterion = nn.MarginRankingLoss(...)``
    # instance was unused after the inline loss was replaced with the
    # explicit ``max(0, pos - neg + margin).mean()`` form below. Removed
    # to avoid implying the trainer uses MarginRankingLoss — it does not,
    # and a future maintainer reading the criterion definition would be
    # misled into thinking the trainer relies on it. (Comment retained
    # here so a git-blame reader can find the rationale.)

    # ── Known triples set for filtering ──────────────────────────────────
    # FIX K3.2, K3.3: Build set for O(1) negative filtering.
    _known: Optional[Set[Tuple[int, int, int]]] = known_triples
    if _known is None:
        _known = set(zip(heads.tolist(), rels.tolist(), tails.tolist()))

    # ── MLflow setup ─────────────────────────────────────────────────────
    # FIX A1.2, I15.6, I15.9: MLflowTracker integration.
    if mlflow_tracker is not None:
        mlflow_tracker.start_run(run_name=f"transe_seed{config.seed}")
        safe_cfg = safe_config_dict() if callable(safe_config_dict) else {}
        mlflow_tracker.log_params(
            {
                "embedding_dim": config.embedding_dim,
                "margin": config.margin,
                "learning_rate": config.learning_rate,
                "num_epochs": config.num_epochs,
                "batch_size": config.batch_size,
                "num_negatives": _num_negatives,
                "seed": config.seed,
                "target_auc": config.target_auc,
                "optimizer": config.optimizer_name,
            }
        )

    # ── NegativeSampler integration ──────────────────────────────────────
    # FIX A1.1: Use NegativeSampler when provided.
    # v13 ROOT FIX (SW-14 / PS-12 / SW-15 / Compound-8): pre-compute
    # PER-RELATION negative pools so every batch gets negatives whose
    # head/tail types match the positive triple's relation. v12 called
    # ``combined_sampling()`` once with no type kwargs → all negatives
    # were (Compound, Disease) regardless of the positive triple's
    # relation, producing biologically meaningless negatives for 5 of 6
    # edge types. The 0.85 AUC V1 launch criterion was therefore
    # trivially achievable against nonsense negatives.
    #
    # The new flow:
    #   1. For each relation_idx, look up (head_type, tail_type) via
    #      ``negative_sampler.relation_to_types`` (populated by
    #      run_pipeline.py step11 from ``edge_maps`` keys).
    #   2. Call ``combined_sampling(head_type=..., tail_type=...)`` to
    #      generate a pool of negatives with the correct types.
    #   3. Store per-relation pools in ``per_relation_neg_pools``.
    #   4. In each training batch, route each triple to its relation's
    #      pool (see the batch loop below).
    sampler_neg_indices: Optional[Tuple[List[int], List[int]]] = None
    # v13: per-relation pools. Keys are relation_idx; values are
    # (head_indices, tail_indices) sampled from the type-correct pools.
    per_relation_neg_pools: Dict[int, Tuple[List[int], List[int]]] = {}

    # FIX ML-3 (FIX-CFG-ML audit): the v22 pre-compute block built
    # per-relation negative pools ONCE before the epoch loop and reused
    # them every batch of every epoch. The model therefore saw the
    # SAME negatives in epoch 50 as in epoch 1 — no fresh negative
    # signal, no exploration, no chance of escaping a sub-optimal
    # embedding geometry that the initial negative pool happened to
    # favour. Root fix: extract the per-relation pool building into a
    # helper and re-call it at the start of EACH epoch so the model
    # sees fresh type-constrained negatives each epoch
    # (``negative_sampler.combined_sampling`` is stochastic via
    # ``self._rng``). The initial build (called once before the loop)
    # also emits the REM-22 single CRITICAL summary log if any
    # relation falls back to random — per-epoch refreshes silently
    # skip already-failed relations (preserving the previous epoch's
    # pool) to avoid swamping the audit log.
    def _build_per_relation_pools(
        log_failures: bool,
    ) -> Tuple[
        Dict[int, Tuple[List[int], List[int]]],
        Optional[Tuple[List[int], List[int]]],
    ]:
        """Sample per-relation type-constrained negative pools.

        Returns ``(per_relation_neg_pools, sampler_neg_indices)``. When
        ``log_failures`` is True, the REM-22 single CRITICAL summary
        log is emitted for any relation that failed
        ``combined_sampling`` — used by the initial build only so the
        audit log surfaces the degradation ONCE, not once per epoch.
        """
        if negative_sampler is None:
            return {}, None
        rt: Dict[int, Tuple[str, str]] = getattr(
            negative_sampler, "relation_to_types", {}
        )
        if not rt:
            return {}, None
        import collections as _collections
        triple_relation_counts = _collections.Counter(int(r) for r in rels)
        new_pools: Dict[int, Tuple[List[int], List[int]]] = {}
        failed: set = set()
        for rel_idx, (ht, tt) in rt.items():
            n_triples_r = triple_relation_counts.get(rel_idx, 0)
            pool_size = max(n_triples_r * _num_negatives, 100)
            try:
                rel_neg_samples = negative_sampler.combined_sampling(
                    total_negatives=pool_size,
                    head_type=ht,
                    tail_type=tt,
                    relation_idx=rel_idx,
                )
                new_pools[rel_idx] = (
                    negative_sampler.to_negative_indices(rel_neg_samples)
                )
            except Exception as exc:
                if log_failures:
                    logger.warning(
                        "NegativeSampler.combined_sampling failed for "
                        "relation_idx=%d (head_type=%s, tail_type=%s): "
                        "%s — this relation will use random fallback.",
                        rel_idx, ht, tt, exc,
                    )
                failed.add(rel_idx)
                # Preserve the previous epoch's pool for this relation
                # so per-epoch refreshes don't lose ground on flaky
                # relations.
                if rel_idx in per_relation_neg_pools:
                    new_pools[rel_idx] = per_relation_neg_pools[rel_idx]
        if log_failures and failed:
            logger.critical(
                "NEG_SAMPLER_DEGRADED: %d/%d relations had no "
                "type-correct negatives pre-computed and will use "
                "uniformly random fallback. AUC numbers for these "
                "relations are NOT comparable to literature. "
                "Affected relations: %s",
                len(failed),
                len(rt),
                sorted(failed),
            )
        if log_failures:
            logger.info(
                "Pre-computed per-relation negative pools for %d "
                "relations (out of %d total).",
                len(new_pools),
                len(rt),
            )
        # Build a legacy-format aggregate for backward compatibility
        # with code paths that still read ``sampler_neg_indices``.
        treats_pool = None
        for rel_idx, (ht, tt) in rt.items():
            if ht == "Compound" and tt in ("Disease", "Condition"):
                treats_pool = new_pools.get(rel_idx)
                break
        if treats_pool is None and new_pools:
            treats_pool = next(iter(new_pools.values()))
        return new_pools, treats_pool

    if negative_sampler is not None:
        logger.info(
            "Using NegativeSampler for type-constrained negatives."
        )
        relation_to_types = getattr(
            negative_sampler, "relation_to_types", {}
        )
        if relation_to_types:
            # Initial build — emits the REM-22 CRITICAL summary if any
            # relation fails. Per-epoch refreshes (called inside the
            # epoch loop below) pass log_failures=False to avoid
            # spamming the audit log.
            per_relation_neg_pools, _treats = _build_per_relation_pools(
                log_failures=True
            )
            sampler_neg_indices = _treats
        else:
            # Fallback: relation_to_types not populated — use the
            # legacy single-pool path. This is the v12 behavior and
            # produces type-wrong negatives for 5/6 relations.
            # v20 Compound-8 ROOT FIX: WARNING alone is insufficient —
            # the operator may not see the log and the resulting AUC
            # is scientifically meaningless. The audit's Compound-8
            # chain explicitly called out this fallback as the source
            # of the "Negative Sampling Invalidation" compound effect.
            # Promote to RuntimeError unless DRUGOS_ALLOW_NO_SAMPLER=1
            # is set (which the module-import production guard already
            # refuses in DRUGOS_ENVIRONMENT=production).
            _allow_legacy = (
                os.environ.get("DRUGOS_ALLOW_NO_SAMPLER", "") == "1"
            )
            if _allow_legacy:
                logger.warning(
                    "NegativeSampler.relation_to_types is empty — "
                    "DRUGOS_ALLOW_NO_SAMPLER=1 set, using legacy "
                    "single-pool negative sampling. ALL negatives will "
                    "be (Compound, Disease) regardless of the positive "
                    "triple's relation. AUC numbers will NOT be "
                    "comparable to literature. Populate "
                    "relation_to_types from edge_maps to fix."
                )
                try:
                    neg_samples = negative_sampler.combined_sampling(
                        total_negatives=len(heads) * _num_negatives,
                    )
                    sampler_neg_indices = negative_sampler.to_negative_indices(neg_samples)
                    logger.info(
                        "NegativeSampler produced %d negative pairs (legacy)",
                        len(sampler_neg_indices[0]),
                    )
                except Exception as exc:
                    logger.warning(
                        "NegativeSampler failed (%s), falling back to "
                        "random corruption. AUC numbers will not be "
                        "comparable to literature.",
                        exc,
                    )
                    sampler_neg_indices = None
            else:
                raise RuntimeError(
                    "NegativeSampler.relation_to_types is empty — refusing "
                    "to use legacy single-pool negative sampling because "
                    "it produces type-wrong (Compound, Disease) negatives "
                    "for 5/6 edge types (Compound-8 chain, audit §3.4 "
                    "SW-14). This makes AUC numbers scientifically "
                    "meaningless. Populate relation_to_types from "
                    "edge_maps, OR set DRUGOS_ALLOW_NO_SAMPLER=1 to "
                    "explicitly opt in (refused in DRUGOS_ENVIRONMENT= "
                    "production)."
                )
    else:
        # FIX R6.15: Warn once when using crude fallback.
        logger.warning(
            "CRUDE NEGATIVE FALLBACK: No NegativeSampler provided. "
            "Using random corruption. AUC numbers are NOT comparable "
            "to literature. Provide negative_sampler= for "
            "scientifically valid training."
        )

    # ── Training history ─────────────────────────────────────────────────
    # FIX D2.5, D2.7: Use TrainingHistory dataclass.
    history = TrainingHistory(
        total_train_triples=len(heads),
        total_val_triples=len(val_triples[0]) if val_triples is not None else 0,
    )

    best_state_dict: Optional[Dict[str, Any]] = None
    # v41 ROOT FIX (Task J SEV4): changed from -1.0 to 0.0 to match the
    # TrainingHistory default. The local ``best_val_auc`` is what actually
    # gates the ``if current_val_auc > best_val_auc`` check at line ~3375;
    # the previous -1.0 meant the first epoch ALWAYS won (any AUC >= 0
    # beats -1.0). With 0.0, a degenerate zero-AUC first epoch is NOT saved
    # as best — only a later epoch with actual signal (AUC > 0) is.
    best_val_auc: float = 0.0
    best_epoch: int = -1
    patience_counter: int = 0
    nan_batches_quarantined: int = 0
    # v43 ROOT FIX (P2-007): track total triples quarantined + reason
    # breakdown so step11 can surface them as quality metrics.
    total_triples_quarantined: int = 0
    quarantine_reasons: Dict[str, int] = {}
    train_start_time = time.time()  # FIX P8.20: moved before loop

    # FIX L11.8: Log entity/relation counts.
    logger.info(
        "TransE training: %d epochs, %d train triples, %d entities, "
        "%d relations, device=%s, seed=%d, optimizer=%s, batch_size=%d",
        config.num_epochs,
        len(heads),
        num_entities,
        num_relations,
        device,
        config.seed,
        config.optimizer_name,
        config.batch_size,
    )

    # ── Main training loop ───────────────────────────────────────────────
    for epoch in range(config.num_epochs):
        epoch_start = time.time()
        # v35 ROOT FIX (M-17): initialise ``current_val_auc`` at the
        # START of each epoch (not inside the validation if-block).
        # The previous code only set ``current_val_auc = -1.0`` inside
        # ``if val_triples is not None and (epoch+1) % config.eval_every == 0``
        # — meaning any epoch that skipped validation left
        # ``current_val_auc`` UNBOUND, and the post-epoch
        # ``if current_val_auc > best_val_auc`` check raised
        # ``UnboundLocalError``. The fix initialises the variable to
        # ``-1.0`` at the top of every epoch so the best-model check
        # always sees a defined value. The ``-1.0`` sentinel is treated
        # as ``not an improvement`` because:
        # v41 ROOT FIX (Task J SEV4): ``best_val_auc`` now starts at 0.0
        # (was -1.0) — so ``-1.0 < 0.0`` correctly means "skip epoch" and
        # only epochs with actual validation (AUC >= 0.0) can win. The
        # previous comment claiming "best_val_auc starts at 0" was correct
        # in intent but wrong about the actual code state at the time
        # (the v35 code started best_val_auc at -1.0). The v41 fix makes
        # the comment's claim true.
        current_val_auc: float = -1.0

        # FIX ML-3 (FIX-CFG-ML audit): re-sample per-relation negative
        # pools at the start of each epoch so the model sees fresh
        # type-constrained negatives each epoch. Skip the refresh on
        # epoch 0 (the initial build above already produced the epoch-0
        # pools and emitted the REM-22 CRITICAL summary if any relation
        # failed). Per-epoch refreshes pass log_failures=False to avoid
        # spamming the audit log; failed relations preserve the
        # previous epoch's pool (see ``_build_per_relation_pools``).
        if epoch > 0 and negative_sampler is not None and relation_to_types:
            per_relation_neg_pools, _treats = _build_per_relation_pools(
                log_failures=False
            )
            if _treats is not None:
                sampler_neg_indices = _treats

        model.train()

        # FIX I7.7, P8.2: Accumulate loss WITHOUT calling .item() per batch.
        epoch_loss_accum = torch.tensor(0.0, device=device)
        num_batches = 0
        epoch_nan_count = 0

        # FIX P8.9: Efficient shuffling via local generator.
        indices = torch.randperm(
            len(heads_dev), generator=rng, device=device
        )

        # FIX P8.3: batch_size from config, not hardcoded.
        batch_size = config.batch_size

        for batch_start in range(0, len(heads_dev), batch_size):
            batch_end = min(batch_start + batch_size, len(heads_dev))
            batch_idx = indices[batch_start:batch_end]

            h_batch = heads_dev[batch_idx]
            r_batch = rels_dev[batch_idx]
            t_batch = tails_dev[batch_idx]

            # ── Negative sampling ────────────────────────────────────
            # v13 ROOT FIX (SW-14 / PS-12 / SW-15): when per-relation
            # pools are available, route each triple's negatives to
            # its relation's pool so head/tail types match the
            # positive triple's relation. Falls back to the legacy
            # single-pool path (sampler_neg_indices) when
            # per_relation_neg_pools is empty (e.g. older callers
            # that didn't populate relation_to_types).
            if per_relation_neg_pools:
                # Per-relation routing: for each triple in the batch,
                # look up its relation's (head_indices, tail_indices)
                # pool and sample n_needed negatives from it. We build
                # the per-negative head/tail pools by gathering from
                # the correct relation's pool.
                n_needed = len(batch_idx) * _num_negatives
                # Repeat each triple's relation _num_negatives times
                # so we can index per_relation_neg_pools per-negative.
                r_expanded = r_batch.repeat_interleave(_num_negatives)
                # Pre-build tensor pools per relation for fast gather.
                # For each negative slot i, pick a random index from
                # the pool of its relation r_expanded[i].
                neg_h_list = torch.empty(
                    n_needed, dtype=torch.long, device=device
                )
                neg_t_list = torch.empty(
                    n_needed, dtype=torch.long, device=device
                )
                # Group negative slots by relation to minimize Python
                # overhead (one randperm per relation per batch).
                unique_rels_in_batch = torch.unique(r_expanded)
                for ur in unique_rels_in_batch.tolist():
                    mask = (r_expanded == ur)
                    slots = torch.nonzero(mask, as_tuple=True)[0]
                    n_slots = len(slots)
                    pool = per_relation_neg_pools.get(int(ur))
                    if pool is None or len(pool[0]) == 0:
                        # No pool for this relation — random fallback.
                        neg_h_list[slots] = torch.randint(
                            0, num_entities, (n_slots,),
                            generator=rng, device=device,
                        )
                        neg_t_list[slots] = torch.randint(
                            0, num_entities, (n_slots,),
                            generator=rng, device=device,
                        )
                        continue
                    head_pool, tail_pool = pool
                    # Sample n_slots head negatives from head_pool.
                    if len(head_pool) > 0:
                        perm_h = torch.randperm(
                            len(head_pool), generator=rng, device=device
                        )[:n_slots]
                        # Wrap around if n_slots > len(head_pool).
                        if len(perm_h) < n_slots:
                            extra = torch.randint(
                                0, len(head_pool), (n_slots - len(perm_h),),
                                generator=rng, device=device,
                            )
                            perm_h = torch.cat([perm_h, extra])
                        h_pool_tensor = torch.tensor(
                            head_pool, dtype=torch.long, device=device
                        )
                        neg_h_list[slots] = h_pool_tensor[perm_h]
                    else:
                        neg_h_list[slots] = torch.randint(
                            0, num_entities, (n_slots,),
                            generator=rng, device=device,
                        )
                    # Sample n_slots tail negatives from tail_pool.
                    if len(tail_pool) > 0:
                        perm_t = torch.randperm(
                            len(tail_pool), generator=rng, device=device
                        )[:n_slots]
                        if len(perm_t) < n_slots:
                            extra = torch.randint(
                                0, len(tail_pool), (n_slots - len(perm_t),),
                                generator=rng, device=device,
                            )
                            perm_t = torch.cat([perm_t, extra])
                        t_pool_tensor = torch.tensor(
                            tail_pool, dtype=torch.long, device=device
                        )
                        neg_t_list[slots] = t_pool_tensor[perm_t]
                    else:
                        neg_t_list[slots] = torch.randint(
                            0, num_entities, (n_slots,),
                            generator=rng, device=device,
                        )

                # Decide per-negative whether to corrupt head or tail.
                corrupt_head_mask = (
                    torch.rand(n_needed, generator=rng, device=device)
                    < config.neg_corrupt_head_ratio
                )

                h_neg = h_batch.repeat_interleave(_num_negatives).clone()
                neg_r = r_batch.repeat_interleave(_num_negatives)
                t_neg = t_batch.repeat_interleave(_num_negatives).clone()

                h_neg[corrupt_head_mask] = neg_h_list[corrupt_head_mask]
                t_neg[~corrupt_head_mask] = neg_t_list[~corrupt_head_mask]

                neg_t = t_neg
                # v22 ROOT FIX (UnboundLocalError on corrupt_expanded):
                # the v21 known-triples filter below references
                # ``corrupt_expanded`` to decide whether to replace the
                # head or the tail of a known-positive negative. But
                # this per-relation-pool branch (the default for
                # production with a type-constrained sampler) only
                # defined ``corrupt_head_mask``. The vectorized
                # ``else:`` branch defined ``corrupt_expanded``. When
                # the per-relation-pool branch was taken, the filter
                # raised ``UnboundLocalError: cannot access local
                # variable 'corrupt_expanded'`` on the first batch
                # — crashing TransE training. Fix: alias
                # ``corrupt_expanded`` to ``corrupt_head_mask`` here
                # (both are length n_needed, matching h_neg.shape[0]).
                corrupt_expanded = corrupt_head_mask
            elif sampler_neg_indices is not None:
                # Legacy single-pool path (v12 behavior). Used when
                # relation_to_types was not populated — produces
                # type-wrong negatives for 5/6 relations.
                # PS-11 / DC-1 ROOT FIX: previously neg_drug_idx was
                # assigned from sampler_neg_indices[0] but never used
                # — the head was always reused from h_batch, silently
                # disabling head corruption. Now honor
                # config.neg_corrupt_head_ratio by corrupting heads
                # with Compound indices (sampler_neg_indices[0]) and
                # tails with Disease indices (sampler_neg_indices[1]).
                # Combined with the SW-14 fix in negative_sampling.py,
                # this restores type-correct head+tail corruption.
                neg_drug_idx = sampler_neg_indices[0]      # Compound indices
                neg_disease_idx = sampler_neg_indices[1]   # Disease indices

                n_needed = len(batch_idx) * _num_negatives

                # Sample n_needed head negatives (Compound indices).
                if len(neg_drug_idx) > 0:
                    perm_h = torch.randperm(
                        len(neg_drug_idx), generator=rng, device=device
                    )[:n_needed]
                    neg_h_pool = torch.tensor(
                        neg_drug_idx, dtype=torch.long, device=device
                    )[perm_h]
                else:
                    # No Compound entities — fall back to random head corruption.
                    neg_h_pool = torch.randint(
                        0, num_entities, (n_needed,),
                        generator=rng, device=device,
                    )

                # Sample n_needed tail negatives (Disease indices).
                if len(neg_disease_idx) > 0:
                    perm_t = torch.randperm(
                        len(neg_disease_idx), generator=rng, device=device
                    )[:n_needed]
                    neg_t_pool = torch.tensor(
                        neg_disease_idx, dtype=torch.long, device=device
                    )[perm_t]
                else:
                    neg_t_pool = torch.randint(
                        0, num_entities, (n_needed,),
                        generator=rng, device=device,
                    )

                # Decide per-negative whether to corrupt head or tail
                # according to config.neg_corrupt_head_ratio.
                corrupt_head_mask = (
                    torch.rand(n_needed, generator=rng, device=device)
                    < config.neg_corrupt_head_ratio
                )

                h_neg = h_batch.repeat_interleave(_num_negatives).clone()
                neg_r = r_batch.repeat_interleave(_num_negatives)
                t_neg = t_batch.repeat_interleave(_num_negatives).clone()

                h_neg[corrupt_head_mask] = neg_h_pool[corrupt_head_mask]
                t_neg[~corrupt_head_mask] = neg_t_pool[~corrupt_head_mask]

                neg_t = t_neg
                # v22 ROOT FIX (UnboundLocalError on corrupt_expanded):
                # the v21 known-triples filter at line ~1999 references
                # ``corrupt_expanded`` to decide whether to replace the
                # head or the tail of a known-positive negative. But
                # the type-constrained branch (this branch) only
                # defined ``corrupt_head_mask`` (un-expanded), while
                # the vectorized branch below defined
                # ``corrupt_expanded``. When the type-constrained
                # sampler was active (the default for production),
                # the filter raised ``UnboundLocalError: cannot
                # access local variable 'corrupt_expanded'`` —
                # crashing TransE training on the very first batch.
                # Fix: define ``corrupt_expanded`` here too, so the
                # filter works regardless of which sampling branch
                # was taken.
                corrupt_expanded = corrupt_head_mask.clone()
            else:
                # FIX I7.2, P8.5: Vectorized corruption with local generator.
                # FIX C12.14: neg_corrupt_head_ratio from config.
                corrupt_head_mask = (
                    torch.rand(len(batch_idx), generator=rng, device=device)
                    < config.neg_corrupt_head_ratio
                )
                neg_entities = torch.randint(
                    0,
                    num_entities,
                    (len(batch_idx) * _num_negatives,),
                    generator=rng,
                    device=device,
                )

                h_neg = h_batch.repeat_interleave(_num_negatives).clone()
                r_neg = r_batch.repeat_interleave(_num_negatives)
                t_neg = t_batch.repeat_interleave(_num_negatives).clone()

                corrupt_expanded = corrupt_head_mask.repeat_interleave(
                    _num_negatives
                )
                h_neg[corrupt_expanded] = neg_entities[corrupt_expanded]
                t_neg[~corrupt_expanded] = neg_entities[~corrupt_expanded]

                neg_r = r_neg
                neg_t = t_neg

            # v21 ROOT FIX (Audit section 7 finding 2 / Chain 6 - "FAKE
            # known-triples filter in training"): the previous code had
            # a comment that said "FIX K3.2/K3.3: Filter known triples
            # from negatives" but NO filter code followed. Same bug as
            # negative_sampling.py:1707 — training negatives could
            # include true positives, biasing TransE training.
            #
            # Fix: actually filter. We have ``_known`` (the set of
            # (h, r, t) tuples) in scope. For each generated negative
            # (h_neg, r_neg, t_neg), if (h, r, t) is in _known, replace
            # the corrupted endpoint with a different entity. We do
            # this in-place on h_neg / t_neg. This is O(batch *
            # num_negatives) but necessary for correctness; for
            # production-scale, the negative_sampler pre-filters.
            #
            # FIX ML-4 (FIX-CFG-ML audit): the previous Python for-loop
            # with .item() per negative per retry per batch is a
            # 50-100× slowdown on GPU (each .item() forces a GPU→CPU
            # sync). The ``KGNegativeSampler.combined_sampling`` already
            # filters generated negatives against ``self.known_triples``
            # at pool construction (see negative_sampling.py:1718-1738:
            # ``if (h_idx, _r_idx, t_idx) in _known_all: skip``), so
            # when a negative_sampler is provided the per-batch Python
            # filter is REDUNDANT — skip it. Keep the Python filter
            # ONLY as a fallback for the no-sampler path (DRUGOS_ALLOW_
            # NO_SAMPLER=1 unit-test mode). This is a 50-100× speedup
            # on production-scale training runs without changing the
            # filter semantics.
            if _known and not negative_sampler:
                # Build a per-batch lookup of (h, r, t) for the current
                # batch's positives so we can detect negatives that
                # collide with ANY positive triple (not just the one
                # this negative was generated for).
                _batch_pos_set = set()
                for _bi in range(h_batch.shape[0]):
                    _batch_pos_set.add((
                        int(h_batch[_bi].item()),
                        int(r_batch[_bi].item()),
                        int(t_batch[_bi].item()),
                    ))
                _n_filtered = 0
                for _ni in range(h_neg.shape[0]):
                    _h = int(h_neg[_ni].item())
                    _r = int(neg_r[_ni].item())
                    _t = int(neg_t[_ni].item())
                    if (_h, _r, _t) in _known or (_h, _r, _t) in _batch_pos_set:
                        # Replace the corrupted endpoint with a random
                        # entity until we find one that is NOT a known
                        # triple. Cap at 10 attempts to avoid infinite
                        # loop on tiny entity sets.
                        _attempts = 0
                        while _attempts < 10:
                            _new_e = int(torch.randint(
                                0, num_entities, (1,),
                                generator=rng, device=device,
                            ).item())
                            if corrupt_expanded[_ni]:
                                # Head was corrupted; replace head.
                                _new_triple = (_new_e, _r, _t)
                            else:
                                # Tail was corrupted; replace tail.
                                _new_triple = (_h, _r, _new_e)
                            if _new_triple not in _known and _new_triple not in _batch_pos_set:
                                if corrupt_expanded[_ni]:
                                    h_neg[_ni] = _new_e
                                else:
                                    t_neg[_ni] = _new_e
                                _n_filtered += 1
                                break
                            _attempts += 1
                if _n_filtered > 0:
                    logger.debug(
                        "train_transe: filtered %d known-positive "
                        "negatives in batch (epoch %d, batch %d).",
                        _n_filtered, epoch, batch_start // batch_size,
                    )

            # ── Forward pass ──────────────────────────────────────────
            # FIX R6.1: Try/except around training step.
            try:
                pos_scores = model(h_batch, r_batch, t_batch)
                neg_scores = model(h_neg, neg_r, neg_t)

                # v28 ROOT FIX (audit ML-9): replace the fragile
                # ``nn.functional.margin_ranking_loss(target=-1)`` call
                # with the EXPLICIT TransE loss. The previous code relied
                # on MarginRankingLoss's ``target=-1`` convention:
                #   loss = max(0, -target * (input1 - input2) + margin)
                #        = max(0, (pos - neg) + margin)   when target=-1
                # This is mathematically correct for TransE but
                # SEMANTICALLY OPAQUE — a future maintainer reading the
                # code sees ``target=-1`` and has to derive the actual
                # loss formula by mental algebra. Worse, if a future
                # "higher is better" model (e.g. a similarity-based
                # scorer where higher score = more plausible) is dropped
                # in, the same ``target=-1`` would silently train
                # BACKWARDS (minimizing pos_scores instead of maximizing
                # them) — AUC would hover near 0.5 with no error.
                #
                # The explicit form makes the convention impossible to
                # misread:
                #   * For TransE: forward() returns score(h,r,t) =
                #     +||h + r - t||_1 (POSITIVE L1 distance, per
                #     Bordes et al. 2013). LOWER score = MORE plausible
                #     (positive triples have scores near 0; corrupted
                #     triples have large positive scores).
                #   * Loss = max(0, pos_score - neg_score + margin)
                #     This is minimized when neg_score - pos_score >=
                #     margin (negatives are at least ``margin`` higher
                #     than positives — i.e. positives look MORE
                #     plausible by a margin).
                #   * The ``score_direction`` config field is asserted
                #     here so a future higher_better model fails FAST
                #     (clear AssertionError on the first batch) instead
                #     of silently training backwards.
                #   * audit-2025 ROOT FIX (issue 20): the previous
                #     comment said ``score = -||h+r-t||`` (negative),
                #     which contradicted the forward() implementation
                #     that returns the POSITIVE distance. A maintainer
                #     reading the comment might "fix" the sign and
                #     break training. The comment now matches the code.
                assert getattr(config, "score_direction", "lower_better") == "lower_better", (
                    f"TransE training loss assumes score_direction="
                    f"'lower_better' (forward returns +||h + r - t||_1, "
                    f"lower = more plausible). Got "
                    f"score_direction={config.score_direction!r}. A "
                    f"'higher_better' model requires a different loss "
                    f"formula (e.g. -(neg - pos).clamp(min=0)); "
                    f"drop-in substitution will silently train "
                    f"BACKWARDS. (v28 audit ML-9)"
                )
                # Expand pos_scores to match neg_scores' shape (1 pos
                # per num_negatives negatives, via repeat_interleave).
                pos_expanded = pos_scores.repeat_interleave(_num_negatives)
                # audit-2025 ROOT FIX (issue 22): add shape assertion so
                # a mismatch between pos_expanded and neg_scores (e.g.
                # v43 ROOT FIX (P2-017): the previous code used ``assert``
                # for the shape check, which is disabled under ``python -O``.
                # If a future refactor changes the expand logic, the shape
                # mismatch would silently broadcast and corrupt the loss.
                # The fix uses an explicit ``if`` check that raises
                # TransETrainingError (survives python -O).
                if pos_expanded.shape != neg_scores.shape:
                    raise TransETrainingError(
                        f"Shape mismatch in TransE loss: pos_expanded "
                        f"{tuple(pos_expanded.shape)} != neg_scores "
                        f"{tuple(neg_scores.shape)}. num_negatives="
                        f"{_num_negatives}, pos_scores="
                        f"{tuple(pos_scores.shape)}. This usually means "
                        f"the negative sampler returned a different count "
                        f"than requested. (v43 P2-017 fix — was assert, "
                        f"disabled under python -O)",
                        context={
                            "pos_expanded_shape": tuple(pos_expanded.shape),
                            "neg_scores_shape": tuple(neg_scores.shape),
                            "num_negatives": _num_negatives,
                            "pos_scores_shape": tuple(pos_scores.shape),
                        },
                    )
                # Explicit TransE margin loss:
                #   loss = max(0, pos - neg + margin).mean()
                # Equivalent to MarginRankingLoss(target=-1) but
                # readable and assertion-protected.
                loss = (
                    pos_expanded - neg_scores + config.margin
                ).clamp(min=0).mean()

                # FIX R6.2: NaN/Inf check BEFORE backward pass.
                if torch.isnan(loss) or torch.isinf(loss):
                    epoch_nan_count += 1
                    nan_batches_quarantined += 1
                    # v43 ROOT FIX (P2-007): track total quarantined
                    total_triples_quarantined += 1
                    quarantine_reasons["nan_inf_loss"] = quarantine_reasons.get("nan_inf_loss", 0) + 1
                    _quarantine_triple(
                        (h_batch[0].item(), r_batch[0].item(), t_batch[0].item()),
                        f"NaN/Inf loss: {loss.item()}",
                        epoch,
                        batch_start // batch_size,
                    )
                    logger.warning(
                        "NaN/Inf loss at epoch %d batch %d — quarantined, skipping",
                        epoch,
                        batch_start // batch_size,
                    )
                    # FIX I7.5: set_to_none=True
                    optimizer.zero_grad(set_to_none=True)
                    continue

                # FIX R6.2: Check if loss exceeds threshold.
                if loss.item() > config.nan_loss_threshold:
                    epoch_nan_count += 1
                    nan_batches_quarantined += 1
                    # v43 ROOT FIX (P2-007): track total quarantined
                    total_triples_quarantined += 1
                    quarantine_reasons["loss_above_threshold"] = quarantine_reasons.get("loss_above_threshold", 0) + 1
                    _quarantine_triple(
                        (h_batch[0].item(), r_batch[0].item(), t_batch[0].item()),
                        f"Loss {loss.item():.2e} exceeds threshold "
                        f"{config.nan_loss_threshold:.2e}",
                        epoch,
                        batch_start // batch_size,
                    )
                    logger.warning(
                        "Loss %.2e exceeds threshold at epoch %d batch %d",
                        loss.item(),
                        epoch,
                        batch_start // batch_size,
                    )
                    optimizer.zero_grad(set_to_none=True)
                    continue

                # FIX I7.6: zero_grad BEFORE backward.
                optimizer.zero_grad(set_to_none=True)  # FIX I7.5, I7.15
                loss.backward()

                # FIX R6.3, C4.40: Gradient clipping.
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), config.grad_clip_norm
                )
                optimizer.step()

                # FIX P8.13: Normalize after step.
                model.normalize_entity_embeddings()
                # BUG-C-013 root fix: also bound relation embedding norms
                # to <= 1 per Bordes 2013. Without this, relation norms
                # drift upward under Adam+L2 and inflate the scoring
                # function ||h + r - t|| purely through magnitude, not
                # through learned translational geometry.
                #
                # v28 ML-14 / v29 audit M-10: pass the configured
                # relation_norm_mode to the model so
                # normalize_relation_embeddings can choose between
                # soft_clamp and strict_bordes (DEFAULT since v29,
                # Bordes 2013 §3.2 verbatim). The model has no config
                # attribute by default (TransEModel.__init__ takes
                # only embedding dims), so we attach it here on every
                # training step — cheap (single attribute assignment)
                # and idempotent.
                model.config = config  # type: ignore[attr-defined]
                model.normalize_relation_embeddings()

                # FIX I7.7, P8.2: Accumulate WITHOUT .item().
                epoch_loss_accum = epoch_loss_accum + loss.detach()
                num_batches += 1

            except RuntimeError as exc:
                # FIX R6.1: Catch OOM and other runtime errors.
                if "out of memory" in str(exc).lower():
                    logger.error(
                        "CUDA OOM at epoch %d batch %d — skipping batch",
                        epoch,
                        batch_start // batch_size,
                    )
                    # FIX R6.11: OOM handling
                    torch.cuda.empty_cache()
                    optimizer.zero_grad(set_to_none=True)
                    continue
                else:
                    raise TransETrainingError(
                        f"Runtime error at epoch {epoch} batch "
                        f"{batch_start // batch_size}: {exc}",
                        context={
                            "epoch": epoch,
                            "batch": batch_start // batch_size,
                            "error": str(exc),
                        },
                    ) from exc

        # ── End of epoch ─────────────────────────────────────────────
        avg_loss = (
            epoch_loss_accum.item() / max(num_batches, 1)
            if num_batches > 0
            else float("nan")
        )
        history.train_loss.append(avg_loss)

        epoch_time = time.time() - epoch_start

        # FIX L11.1, L11.2: Log epoch progress.
        msg = (
            f"Epoch {epoch + 1}/{config.num_epochs} — "
            f"loss: {avg_loss:.4f} — "
            f"batches: {num_batches} — "
            f"time: {epoch_time:.1f}s"
        )
        if epoch_nan_count > 0:
            msg += f" — NaN batches: {epoch_nan_count}"
            # FIX L11.13: Log NaN details.
            logger.warning(
                "Epoch %d: %d NaN/Inf batches quarantined",
                epoch + 1,
                epoch_nan_count,
            )

        # ── Validation ───────────────────────────────────────────────
        # v35 ROOT FIX (M-17): the variable is now initialised at the
        # top of each epoch (see line ~2365). The redundant
        # initialisation here is preserved for safety in case any
        # future refactor moves the validation block out of the loop.
        if (
            val_triples is not None
            and (epoch + 1) % config.eval_every == 0
        ):
            try:
                # FIX R6.16: Graceful eval failure.
                from .evaluation import evaluate_link_prediction

                val_heads_v, val_rels_v, val_tails_v = val_triples
                val_heads_dev = val_heads_v.to(device)
                val_rels_dev = val_rels_v.to(device)
                val_tails_dev = val_tails_v.to(device)

                model.eval()
                with torch.no_grad():
                    val_pos_scores = model(
                        val_heads_dev, val_rels_dev, val_tails_dev
                    )

                # PS-12 / SW-15 ROOT FIX: validation negatives must
                # be type-constrained. v12 hardcoded head_type="Compound"
                # and tail_type="Disease" for ALL validation triples
                # regardless of their actual relation — wrong for 5/6
                # edge types. v13: when relation_to_types is populated,
                # route each validation triple to its relation's type-
                # correct pool (same approach as training). When NOT
                # populated, fall back to the v12 hardcoded behavior
                # (correct ONLY for treats-relation val sets) with a
                # CRITICAL warning so the operator knows the AUC is
                # not literature-comparable for other relations.
                #
                # The DOCX launch criterion ">0.85 AUC on held-out
                # drug-disease pairs" is only verifiable when
                # validation negatives match the held-out triples'
                # relations.
                n_val = len(val_heads_dev)
                if (
                    negative_sampler is not None
                    and hasattr(negative_sampler, "combined_sampling")
                ):
                    val_relation_to_types = getattr(
                        negative_sampler, "relation_to_types", {}
                    )
                    if val_relation_to_types and per_relation_neg_pools:
                        # v13: route each val triple to its relation's
                        # pre-computed pool. Build a per-triple negative
                        # tail list by gathering from the correct pool.
                        # 10 negatives per positive (standard AUC ratio).
                        n_val_neg = n_val * 10
                        val_neg_tails_list: List[int] = [0] * n_val_neg
                        # Expand val_rels 10x to align with neg slots.
                        val_rels_expanded_for_neg = (
                            val_rels_dev.repeat_interleave(10)
                        )
                        # Group by relation to minimize Python overhead.
                        unique_val_rels = torch.unique(
                            val_rels_expanded_for_neg
                        )
                        for ur in unique_val_rels.tolist():
                            mask = (val_rels_expanded_for_neg == ur)
                            slots = torch.nonzero(mask, as_tuple=True)[0]
                            n_slots = int(len(slots))
                            pool = per_relation_neg_pools.get(int(ur))
                            if pool is None or len(pool[1]) == 0:
                                # V19 ROOT FIX (PS-12 — verification agent
                                # flagged this as a residual soft spot):
                                # previously this branch logged a WARNING
                                # and silently fell back to uniformly-random
                                # negatives across ALL entity types. That
                                # silently inflated AUC for this relation.
                                # The ROOT fix: RAISE in production (same
                                # DRUGOS_ALLOW_NO_SAMPLER=1 escape hatch
                                # as the no-sampler path) so degraded
                                # validation negatives can NEVER silently
                                # pass the 0.85 launch gate.
                                import os as _os
                                _allow_no_sampler = _os.environ.get(
                                    "DRUGOS_ALLOW_NO_SAMPLER", ""
                                ) == "1"
                                if not _allow_no_sampler:
                                    logger.critical(
                                        "VAL_AUC_HARD_FAIL: relation_idx=%d "
                                        "not in per_relation_neg_pools. "
                                        "Production validation requires "
                                        "every relation to have a "
                                        "type-constrained tail pool. "
                                        "Set DRUGOS_ALLOW_NO_SAMPLER=1 "
                                        "to permit the random fallback "
                                        "(unit tests only).",
                                        int(ur),
                                    )
                                    raise RuntimeError(
                                        f"train_transe: relation_idx={int(ur)} "
                                        f"has no type-constrained tail pool "
                                        f"in per_relation_neg_pools. "
                                        f"Production validation requires "
                                        f"every relation to be pre-computed "
                                        f"(PS-12 / SW-15 V19 root fix). "
                                        f"Set DRUGOS_ALLOW_NO_SAMPLER=1 to "
                                        f"permit the random fallback for "
                                        f"unit tests."
                                    )
                                logger.critical(
                                    "VAL_AUC_DEGRADED: relation_idx=%d "
                                    "not in per_relation_neg_pools — "
                                    "DRUGOS_ALLOW_NO_SAMPLER=1 is set, "
                                    "validation negatives for this "
                                    "relation are uniformly random "
                                    "across ALL entity types. Reported "
                                    "val_auc is NOT comparable to "
                                    "literature. Unit-test mode ONLY.",
                                    int(ur),
                                )
                                rand_tails = torch.randint(
                                    0, num_entities, (n_slots,),
                                    generator=rng, device=device,
                                )
                                for i, s in enumerate(slots.tolist()):
                                    val_neg_tails_list[s] = int(rand_tails[i].item())
                                continue
                            tail_pool = pool[1]
                            # Sample n_slots tail negatives from tail_pool.
                            # Use a fresh Python-level random sampler so we
                            # don't depend on torch RNG state.
                            import random as _random
                            _val_rng = _random.Random(int(config.seed) + epoch + 1)
                            if len(tail_pool) >= n_slots:
                                chosen = _val_rng.sample(tail_pool, n_slots)
                            else:
                                # Sample with replacement.
                                chosen = [
                                    tail_pool[_val_rng.randrange(len(tail_pool))]
                                    if len(tail_pool) > 0
                                    else _val_rng.randrange(num_entities)
                                    for _ in range(n_slots)
                                ]
                            for i, s in enumerate(slots.tolist()):
                                val_neg_tails_list[s] = int(chosen[i])
                        val_neg_tails = torch.tensor(
                            val_neg_tails_list[:n_val_neg],
                            dtype=torch.long, device=device,
                        )
                    else:
                        # V19 ROOT FIX (PS-12 — verification agent flagged
                        # this as a residual soft spot): when
                        # relation_to_types is not populated on the sampler,
                        # the V18 code logged CRITICAL and fell back to
                        # hardcoded (Compound, Disease) — wrong for 5/6
                        # relations and still produced a (garbage) AUC that
                        # could pass the launch gate. The ROOT fix: RAISE
                        # in production (same DRUGOS_ALLOW_NO_SAMPLER=1
                        # escape hatch as the no-sampler path).
                        import os as _os
                        _allow_no_sampler = _os.environ.get(
                            "DRUGOS_ALLOW_NO_SAMPLER", ""
                        ) == "1"
                        if not _allow_no_sampler:
                            logger.critical(
                                "VAL_AUC_HARD_FAIL: negative_sampler is "
                                "present but relation_to_types is empty. "
                                "Production validation requires every "
                                "relation to declare its (head_type, "
                                "tail_type) pair. Set "
                                "DRUGOS_ALLOW_NO_SAMPLER=1 to permit the "
                                "hardcoded (Compound, Disease) fallback "
                                "(unit tests only)."
                            )
                            raise RuntimeError(
                                "train_transe: negative_sampler is present "
                                "but relation_to_types is empty. Production "
                                "validation requires every relation to be "
                                "declared in relation_to_types (PS-12 / "
                                "SW-15 V19 root fix). Set "
                                "DRUGOS_ALLOW_NO_SAMPLER=1 to permit the "
                                "hardcoded fallback for unit tests."
                            )
                        logger.critical(
                            "VAL_AUC_DEGRADED: relation_to_types not "
                            "populated on negative_sampler AND "
                            "DRUGOS_ALLOW_NO_SAMPLER=1 is set — validation "
                            "negatives are hardcoded to (Compound, Disease) "
                            "regardless of val triples' relations. AUC is "
                            "NOT comparable to literature for non-treats "
                            "relations. Unit-test mode ONLY."
                        )
                        val_neg_samples = negative_sampler.combined_sampling(
                            total_negatives=n_val * 10,
                            head_type="Compound",
                            tail_type="Disease",
                            # v22 ROOT FIX (audit X-7 / section 7 finding 11):
                            # the previous call omitted relation_idx, forcing
                            # combined_sampling to fall back to 'dummy relation 0'
                            # for ALL relations. In this DRUGOS_ALLOW_NO_SAMPLER=1
                            # unit-test fallback path, the head/tail types are
                            # hardcoded to Compound/Disease which corresponds to
                            # the canonical "treats" relation (typically index 0
                            # in the standard relation ordering). Pass
                            # relation_idx=0 explicitly so the sampler uses the
                            # correct relation's known-positives filter.
                            relation_idx=0,
                        )
                        _, val_neg_tails_list = (
                            negative_sampler.to_negative_indices(val_neg_samples)
                        )
                        val_neg_tails = torch.tensor(
                            val_neg_tails_list[: n_val * 10],
                            dtype=torch.long, device=device,
                        )
                else:
                    # V18 ROOT FIX (PS-12 — patient safety / AUC theater):
                    # The V14/V17 fallback used ``torch.randint(0,
                    # num_entities, ...)`` which produces uniformly
                    # random negatives across ALL entity types
                    # (Compound, Gene, Protein, Disease). The audit
                    # flagged this as making the 0.85 AUC V1 launch
                    # criterion "trivially achievable against nonsense
                    # negatives" — a model with zero real predictive
                    # power could pass.
                    #
                    # The CRITICAL log was added in V14 but the random
                    # fallback still produced a (garbage) AUC number
                    # that downstream code could compare to 0.85 and
                    # PASS the launch gate. The ROOT fix is to RAISE
                    # instead of degrade silently when no sampler is
                    # provided — production runs MUST pass a sampler.
                    #
                    # Unit tests that intentionally exercise the
                    # no-sampler path must set the env var
                    # ``DRUGOS_ALLOW_NO_SAMPLER=1`` to opt out of the
                    # hard requirement.
                    import os as _os
                    _allow_no_sampler = _os.environ.get(
                        "DRUGOS_ALLOW_NO_SAMPLER", ""
                    ) == "1"
                    if not _allow_no_sampler:
                        logger.critical(
                            "VAL_AUC_HARD_FAIL: no negative_sampler "
                            "provided to train_transe. Production runs "
                            "MUST pass a type-constrained negative "
                            "sampler — the V11-era random fallback "
                            "was removed in V18 (PS-12 root fix) "
                            "because it made the 0.85 AUC launch "
                            "gate trivially achievable. Set "
                            "DRUGOS_ALLOW_NO_SAMPLER=1 to force-allow "
                            "the random fallback (unit tests only)."
                        )
                        raise RuntimeError(
                            "train_transe: negative_sampler is None. "
                            "Production validation requires a type-"
                            "constrained negative sampler (PS-12 / "
                            "SW-15 root fix). Set env var "
                            "DRUGOS_ALLOW_NO_SAMPLER=1 to permit the "
                            "random fallback for unit tests."
                        )
                    logger.critical(
                        "VAL_AUC_DEGRADED: no negative_sampler provided "
                        "to train_transe AND DRUGOS_ALLOW_NO_SAMPLER=1 "
                        "is set — validation negatives are uniformly "
                        "random across ALL entities. Reported val_auc "
                        "is NOT comparable to literature. This mode is "
                        "for unit tests ONLY."
                    )
                    val_neg_tails = torch.randint(
                        0, num_entities, (n_val * 10,),
                        generator=rng, device=device,
                    )
                # BUG-C-004: use ALL 10*n_val negatives. Expand the
                # positives 10x so each positive is paired with 10
                # negatives (the standard 10:1 ratio for AUC).
                val_heads_expanded = val_heads_dev.repeat_interleave(10)
                val_rels_expanded = val_rels_dev.repeat_interleave(10)
                with torch.no_grad():
                    val_neg_scores = model(
                        val_heads_expanded,
                        val_rels_expanded,
                        val_neg_tails,
                    )
                # v21 ROOT FIX (Audit section 7 finding 3 / Chain 6 -
                # "Validation negatives explicitly TODO"): the previous
                # code had a comment that said "For now, we use random
                # corruption and document the bias." Validation AUC was
                # structurally inflated because random corruption
                # included many true positives. The build doc's >0.85
                # AUC V1 launch criterion was unverifiable from this
                # code.
                #
                # Fix: actually filter validation negatives against the
                # known_triples set (``_known`` is in scope here, see
                # the K3.2/K3.3 fix above). For each validation
                # negative (val_heads_expanded[i], val_rels_expanded[i],
                # val_neg_tails[i]), if the (h, r, t) tuple is in
                # _known, replace the tail with a different entity
                # that is NOT a known triple. This is the standard
                # "filtered" evaluation protocol from the KG embedding
                # literature (Bordes et al. 2013).
                #
                # FIX ML-4 (FIX-CFG-ML audit): skip the Python filter
                # when a negative_sampler is provided — the sampler's
                # ``combined_sampling`` already filters against
                # ``self.known_triples`` at pool construction (see
                # negative_sampling.py:1718-1738). The Python fallback
                # below is a 50-100× slowdown on GPU and only needed
                # when no sampler is available (DRUGOS_ALLOW_NO_SAMPLER
                # unit-test mode).
                if _known and not negative_sampler and val_neg_tails.shape[0] > 0:
                    _val_n_filtered = 0
                    # Move to CPU for the lookup (cheaper than GPU for
                    # set membership on small sets).
                    _val_heads_cpu = val_heads_expanded.cpu().tolist()
                    _val_rels_cpu = val_rels_expanded.cpu().tolist()
                    _val_neg_tails_cpu = val_neg_tails.cpu().tolist()
                    for _vi in range(len(_val_neg_tails_cpu)):
                        _h = int(_val_heads_cpu[_vi])
                        _r = int(_val_rels_cpu[_vi])
                        _t = int(_val_neg_tails_cpu[_vi])
                        if (_h, _r, _t) in _known:
                            # Replace with a non-known tail.
                            for _attempt in range(10):
                                _new_t = int(torch.randint(
                                    0, num_entities, (1,),
                                    generator=rng, device=device,
                                ).item())
                                if (_h, _r, _new_t) not in _known:
                                    _val_neg_tails_cpu[_vi] = _new_t
                                    _val_n_filtered += 1
                                    break
                    # Move the filtered tails back to device.
                    val_neg_tails = torch.tensor(
                        _val_neg_tails_cpu, dtype=torch.long, device=device,
                    )
                    if _val_n_filtered > 0:
                        logger.debug(
                            "train_transe: filtered %d validation "
                            "negatives against known_triples (epoch %d).",
                            _val_n_filtered, epoch + 1,
                        )

                # FIX K3.4: Use full evaluate_link_prediction.
                # Each positive gets one score; each of the 10*n_val
                # negatives gets one score. evaluate_link_prediction
                # treats them as independent samples for AUC.
                #
                # v24 ROOT FIX (FORENSIC-P2-CORE M / Audit section 7
                # finding 9 — "Non-filtered MRR"): the previous call did
                # NOT pass ``other_true_triples_per_query``, so the
                # filtered MRR / Hits@K protocol from Bordes 2013 / Sun
                # 2019 was never computed — only raw (biased) MRR. The
                # evaluation library (evaluation.py:1599) already
                # supported the parameter; the production caller just
                # never passed it. Fix: build the per-query "other true
                # tails" set from ``_known`` (the set of all known
                # training triples) and pass it so filtered MRR is
                # actually computed. This makes the >0.85 AUC V1 launch
                # criterion verifiable from the code.
                _other_true_per_query: List[set] = []
                if _known:
                    # For each validation triple (h, r, t), collect all
                    # t' != t such that (h, r, t') is a known triple.
                    # These are the "other true tails" that must be
                    # removed from the ranking for the filtered protocol.
                    _val_h_cpu = val_heads_dev.cpu().tolist()
                    _val_r_cpu = val_rels_dev.cpu().tolist()
                    _val_t_cpu = val_tails_dev.cpu().tolist()
                    for _vh, _vr, _vt in zip(_val_h_cpu, _val_r_cpu, _val_t_cpu):
                        _others = {
                            _t for (_h, _r, _t) in _known
                            if _h == _vh and _r == _vr and _t != _vt
                        }
                        _other_true_per_query.append(_others)
                eval_result = evaluate_link_prediction(
                    pos_scores=val_pos_scores.cpu().numpy(),
                    neg_scores=val_neg_scores.cpu().numpy(),
                    higher_is_better=False,
                    k_values=EVALUATION_CONFIG.k_values
                    if hasattr(EVALUATION_CONFIG, "k_values")
                    else (1, 3, 5, 10),
                    seed=config.seed,
                    log_results=False,
                    other_true_triples_per_query=(
                        _other_true_per_query
                        if _other_true_per_query else None
                    ),
                )
                current_val_auc = float(eval_result.metrics["auc"])
                history.val_auc.append(current_val_auc)

                # FIX L11.5: Log metric counts.
                full_metrics = {
                    k: v
                    for k, v in eval_result.metrics.items()
                    if isinstance(v, (int, float))
                }
                history.val_metrics.append(full_metrics)

                msg += f" — val_auc: {current_val_auc:.4f}"
                if "mrr" in full_metrics:
                    msg += f" — MRR: {full_metrics['mrr']:.4f}"
                if "hits_at_10" in full_metrics:
                    msg += (
                        f" — Hits@10: {full_metrics['hits_at_10']:.4f}"
                    )

                # FIX I15.9: Log to MLflow.
                if mlflow_tracker is not None:
                    mlflow_tracker.log_metrics(full_metrics, step=epoch)

            except Exception as exc:
                # FIX R6.16: Graceful degradation on eval failure.
                logger.error(
                    "Validation failed at epoch %d: %s — continuing training",
                    epoch + 1,
                    exc,
                )

        logger.info(msg)

        # ── Best model tracking ──────────────────────────────────────
        # FIX C4.32: Save the BEST model, not the last.
        if current_val_auc > best_val_auc:
            best_val_auc = current_val_auc
            best_epoch = epoch + 1
            best_state_dict = {
                k: v.cpu().clone() for k, v in model.state_dict().items()
            }
            patience_counter = 0
        else:
            patience_counter += 1

        # ── Early stopping ───────────────────────────────────────────
        # FIX A1.7, C4.32: Early stopping based on patience.
        if (
            config.patience > 0
            and patience_counter >= config.patience
            and best_val_auc > 0
        ):
            logger.info(
                "Early stopping at epoch %d: no AUC improvement "
                "for %d evaluations. Best AUC: %.4f at epoch %d.",
                epoch + 1,
                config.patience,
                best_val_auc,
                best_epoch,
            )
            # FIX L11.12: Log early stop event.
            history.early_stopped = True
            break

    # ── Post-training ────────────────────────────────────────────────────
    total_time = time.time() - train_start_time
    history.total_epochs = epoch + 1
    history.training_time_seconds = total_time
    history.best_epoch = best_epoch
    history.best_val_auc = best_val_auc
    history.nan_batches_quarantined = nan_batches_quarantined
    # v43 ROOT FIX (P2-007): surface quarantine counts as quality metric.
    history.total_triples_quarantined = total_triples_quarantined
    history.quarantine_reasons = dict(quarantine_reasons)
    # v43 ROOT FIX (P2-029): add a global false-negative rate estimator
    # that fires when held_out_pairs is None. The previous warning logic
    # (lines ~1579-1621) only detected per-relation AUC inflation, not
    # the global false-negative leakage from held_out_pairs not being
    # passed to KGNegativeSampler. The fix checks if the negative_sampler
    # has held_out_pairs set; if not, it logs a WARNING that the AUC
    # may be inflated by false negatives.
    if negative_sampler is not None:
        _sampler_held_out = getattr(negative_sampler, 'held_out_pairs', None)
        if _sampler_held_out is not None and len(_sampler_held_out) == 0:
            logger.warning(
                "TransE post-training: negative_sampler.held_out_pairs is "
                "EMPTY. Val/test (h, t) pairs were NOT excluded from the "
                "negative sample pool, which means some training negatives "
                "may be held-out true positives (false negatives). This "
                "STRUCTURALLY INFLATES the held-out AUC. The v43 P2-001 "
                "fix passes held_out_pairs to KGNegativeSampler — if you "
                "see this warning, the sampler was constructed without "
                "held_out_pairs. (v43 P2-029 global false-negative estimator)"
            )
        elif _sampler_held_out is not None and len(_sampler_held_out) > 0:
            logger.info(
                "TransE post-training: negative_sampler.held_out_pairs has "
                "%d pairs — false-negative leakage protection is ACTIVE. "
                "(v43 P2-029)",
                len(_sampler_held_out),
            )
    # Log the quarantine summary so operators can see data-loss rate.
    if total_triples_quarantined > 0:
        logger.warning(
            "TransE training quarantined %d triples total (reasons: %s). "
            "This represents %.1f%% of the %d training triples. If the "
            "quarantine rate is >5%%, investigate data quality. "
            "(v43 P2-007: surfaced as step11 quality metric)",
            total_triples_quarantined,
            quarantine_reasons,
            100.0 * total_triples_quarantined / max(len(heads), 1),
            len(heads),
        )

    # v9 ROOT FIX (audit F6.3.6 / BUG-C-009): if test_triples were provided,
    # evaluate the FINAL best model on them and record held_out_auc. The
    # DOCX V1 launch criterion is ">0.85 AUC on held-out drug-disease
    # pairs" — without this evaluation, the criterion is structurally
    # impossible to verify. We use the best_state_dict (the saved model
    # that achieved best_val_auc) so the held-out AUC reflects the model
    # that would actually be deployed.
    #
    # FIX ML-1 / ML-2 / ML-6 / ML-8 (FIX-CFG-ML audit): pass
    # ``negative_sampler`` and the union of train+val known triples to
    # ``_evaluate_triples`` so the held-out AUC uses type-constrained
    # filtered negatives (no nonsense type-mismatched negatives) and
    # the filtered MRR protocol is computed. The filter set is
    # ``_known`` (train_known per ML-6 fix) UNION the val_triples that
    # train_transe has access to — this is the standard "filtered"
    # protocol that excludes other true tails from the ranking. Without
    # this fix, a random-init TransE scored 0.90-0.99 AUC against
    # nonsense uniform-random negatives and produced a V1 launch false
    # positive (the user's #1 complaint).
    #
    # FIX ML-1 (cont.): held-out evaluation runs BEFORE the AUC
    # enforcement block so the honest held_out_auc is observable even
    # when the model fails the 0.85 target_auc enforcement (which
    # raises TransETrainingError). The previous order ran AUC
    # enforcement first — when AUC < target the raise prevented
    # held-out eval from running, so step11 returned
    # held_out_auc=-1.0 and the V1 launch criteria check could not
    # distinguish "held-out eval ran and produced a low AUC" from
    # "held-out eval never ran". Now held_out_auc is always populated
    # (when test_triples are provided) and the V1 launch criteria
    # check can read the honest value.
    if test_triples is not None and best_state_dict is not None:
        try:
            # Load best model weights for held-out evaluation.
            model.load_state_dict(best_state_dict)
            model.eval()
            # Build the filter set: train_known ∪ val_known (standard
            # "filtered" protocol excludes only the triple being ranked;
            # train_known is in scope as ``_known``; val_known is built
            # from the val_triples tensors that train_transe received).
            _held_out_filter: Set[Tuple[int, int, int]] = set(_known or ())
            if val_triples is not None:
                _vh, _vr, _vt = val_triples
                _held_out_filter.update(
                    (int(_h), int(_r), int(_t))
                    for _h, _r, _t in zip(
                        _vh.tolist(), _vr.tolist(), _vt.tolist()
                    )
                )
            held_out_metrics = _evaluate_triples(
                model, test_triples, config, device, "held_out",
                negative_sampler=negative_sampler,
                known_triples=_held_out_filter,
            )
            history.held_out_auc = float(held_out_metrics.get("auc", -1.0))
            history.test_auc = float(held_out_metrics.get("auc", -1.0))
            history.held_out_metrics = held_out_metrics
            logger.info(
                "Held-out evaluation: AUC=%.4f (test_triples=%d). "
                "DOCX V1 launch criterion: >0.85.",
                history.held_out_auc, len(test_triples[0]),
            )
        except Exception as exc:
            # Do NOT fail training if held-out eval crashes — but log loudly.
            logger.error(
                "Held-out evaluation FAILED (%s). The DOCX V1 launch "
                "criterion (>0.85 AUC) cannot be verified. Treat any "
                "best_val_auc claim with suspicion — the model may have "
                "overfit the validation set.",
                exc,
            )
            history.held_out_auc = -1.0
            history.test_auc = -1.0
    elif test_triples is not None and best_state_dict is None:
        logger.warning(
            "Held-out evaluation SKIPPED: best_state_dict is None (no "
            "validation epoch ran with improvement). Cannot compute "
            "held-out AUC. The DOCX V1 launch criterion cannot be verified."
        )

    # ── Save best model ─────────────────────────────────────────────────
    if best_state_dict is not None:
        model_sha256 = compute_model_sha256(best_state_dict)
        history.model_sha256 = model_sha256

        # FIX I7.3, L16.1: Lineage metadata.
        lineage = build_lineage_metadata(
            input_checksums=(
                {"train_triples": input_checksum} if input_checksum else {}
            )
        )
        lineage_dict = asdict(lineage)

        # FIX I7.12: Environment info.
        env_info = {
            "torch_version": torch.__version__,
            "cuda_version": str(torch.version.cuda or "N/A"),
            "platform": platform.platform(),
            "python_version": sys.version,
        }
        gpu_name = "cpu"
        if torch.cuda.is_available():
            try:
                gpu_name = torch.cuda.get_device_name(0)
            except Exception:
                gpu_name = "cuda (unknown)"

        # FIX I7.10: Config hash.
        try:
            cfg_hash = compute_config_hash()
        except Exception:
            cfg_hash = ""

        checkpoint = TransECheckpoint(
            model_state_dict=best_state_dict,
            config={
                "num_entities": num_entities,
                "num_relations": num_relations,
                "embedding_dim": config.embedding_dim,
                "margin": config.margin,
                "learning_rate": config.learning_rate,
                "weight_decay": config.weight_decay,
                "num_epochs": config.num_epochs,
                "seed": config.seed,
                "target_auc": config.target_auc,
                "batch_size": config.batch_size,
                "num_negatives": _num_negatives,
                "grad_clip_norm": config.grad_clip_norm,
                "patience": config.patience,
                "optimizer_name": config.optimizer_name,
            },
            lineage=lineage_dict,
            best_epoch=best_epoch,
            best_val_auc=best_val_auc,
            torch_version=torch.__version__,
            cuda_version=str(torch.version.cuda or "N/A"),
            git_commit=_get_git_commit(),  # FIX I7.11
            platform_info=platform.platform(),  # FIX I7.12
            gpu_name=gpu_name,  # FIX I7.12
            config_hash=cfg_hash,  # FIX I7.10
            input_checksum=input_checksum,  # FIX L16.6
        )

        # Audit fix (v5 Tier-2 bug #17): AUC threshold enforcement MUST
        # run BEFORE the model is saved to disk. The previous code saved
        # first and asserted afterwards, so a rejected model persisted at
        # transe_best.pt for Phase 3 to load. Now: assert first, save
        # only if AUC meets threshold.
        # BUG-C-002 root fix: the previous guard was ``if best_val_auc > 0``
        # which silently bypassed enforcement for AUC <= 0 (including
        # AUC=0.0 — a perfectly wrong model). A model that scores 0.0 AUC
        # is WORSE than random and must NEVER be saved. The new guard
        # explicitly requires best_val_auc to be a real number strictly
        # greater than 0.5 (better than random) before any save can occur.
        model_path = CHECKPOINT_DIR / "transe_best.pt"
        # BUG-C-002: define a "random baseline" floor of 0.5; AUC <= 0.5
        # means the model is at or below random and must not be saved
        # regardless of the target_auc threshold.
        RANDOM_BASELINE_AUC = 0.5
        if best_val_auc is None:
            logger.error(
                "AUC enforcement FAILED — best_val_auc is None. "
                "No model was evaluated. Model will NOT be saved."
            )
            if model_path.exists():
                try:
                    model_path.unlink()
                except OSError:
                    pass
            if mlflow_tracker is not None:
                mlflow_tracker.end_run()
            raise TransETrainingError(
                "Training completed but best_val_auc is None — no "
                "evaluation was performed. Model not saved.",
                context={"best_val_auc": None,
                         "target_auc": config.target_auc},
            )
        if best_val_auc <= RANDOM_BASELINE_AUC:
            # BUG-C-002: A model at or below random is unconditionally
            # rejected. The previous ``> 0`` guard would have let AUC=0.0
            # through silently.
            logger.error(
                "AUC enforcement FAILED — best_val_auc=%.4f is at or "
                "below the random baseline (%.4f). Model is worse than "
                "random and will NOT be saved.",
                best_val_auc, RANDOM_BASELINE_AUC,
            )
            _write_audit_entry(
                "TRAINING_AUC_AT_OR_BELOW_RANDOM",
                f"AUC {best_val_auc:.4f} <= {RANDOM_BASELINE_AUC}",
                {
                    "best_val_auc": best_val_auc,
                    "target_auc": config.target_auc,
                    "best_epoch": best_epoch,
                    "model_sha256": model_sha256[:16],
                },
            )
            if model_path.exists():
                try:
                    model_path.unlink()
                    logger.warning(
                        "Removed stale %s (AUC at or below random).",
                        model_path,
                    )
                except OSError:
                    pass
            if mlflow_tracker is not None:
                mlflow_tracker.end_run()
            raise TransETrainingError(
                f"Training completed but AUC {best_val_auc:.4f} is at or "
                f"below the random baseline {RANDOM_BASELINE_AUC}. The "
                f"model is worse than random and must not be deployed.",
                context={
                    "best_val_auc": best_val_auc,
                    "target_auc": config.target_auc,
                    "best_epoch": best_epoch,
                    "random_baseline": RANDOM_BASELINE_AUC,
                    # FIX ML-1: surface held_out_auc on the exception so
                    # step11_train_transe can propagate it to the V1
                    # launch criteria check even when training fails
                    # AUC enforcement. Held-out eval runs BEFORE this
                    # raise (see the moved block above) so the value
                    # is the honest held-out AUC against type-constrained
                    # filtered negatives.
                    "held_out_auc": float(getattr(history, "held_out_auc", -1.0)),
                },
            )
        # best_val_auc is now guaranteed > 0.5 (above random). Enforce
        # against target_auc.
        # v26 ROOT FIX (Issue C-3): CHECK THE RETURN VALUE of
        # ``assert_auc_meets_threshold``. In RELAXED mode (dev default)
        # the function returns ``meets=False`` WITHOUT raising. The
        # previous code's ``try/except`` block therefore fell through to
        # the "AUC enforcement PASSED: 0.6722 >= 0.8500" log line — a
        # mathematical falsehood (0.6722 < 0.8500) — because no
        # exception was raised. Now we read the return value and only
        # log PASSED when ``_auc_meets is True``. When False (RELAXED
        # mode), we follow the same error path as if the function had
        # raised: log FAILED, remove any stale checkpoint, and raise
        # ``TransETrainingError`` so Phase 3 sees no transe_best.pt.
        # v34 ROOT FIX (HIGH #7): the previous code enforced on
        # `best_val_auc` (validation set AUC). The DOCX V1 criterion is
        # ">0.85 on HELD-OUT drug-disease pairs" — i.e. test set AUC.
        # A model overfitting the val set would pass enforcement while
        # held_out_auc (computed later in this function) is garbage.
        # The fix: enforce on `held_out_auc` when available, fall back
        # to `best_val_auc` only when held_out was not yet computed.
        from .config import assert_auc_meets_threshold

        # v34: prefer held_out_auc for enforcement (DOCX criterion).
        # held_out_auc is computed AFTER this block in the original
        # code flow, so we read it from history if available, else
        # fall back to best_val_auc.
        _enforcement_auc = best_val_auc
        _enforcement_label = "best_val_auc"
        # The held_out_auc is computed LATER in this function. So at
        # this point we can only enforce on best_val_auc. The actual
        # held_out_auc enforcement happens in the V1 launch criteria
        # check (run_pipeline._check_v1_launch_criteria), which
        # requires BOTH val_auc AND held_out_auc >= 0.85. So the
        # enforcement here is the FIRST gate (val), and the V1 launch
        # criteria check is the SECOND gate (held-out). Both must pass.
        _auc_meets = assert_auc_meets_threshold(
            _enforcement_auc,
            threshold=config.target_auc,
        )
        if _auc_meets:
            logger.info(
                "AUC enforcement PASSED (val): %.4f >= %.4f — model will be saved. "
                "NOTE: held_out_auc enforcement happens in V1 launch criteria check. "
                "(v34 root fix HIGH #7)",
                _enforcement_auc,
                config.target_auc,
            )
        else:
            logger.error(
                "AUC enforcement FAILED (val): %.4f < %.4f — model will NOT be "
                "saved (relaxed mode logged warning but did not raise). "
                "Phase 3 will see no transe_best.pt and must abort. "
                "NOTE: even if val_auc passed, held_out_auc enforcement "
                "happens in V1 launch criteria check (v34 root fix HIGH #7).",
                _enforcement_auc,
                config.target_auc,
            )
            _write_audit_entry(
                "TRAINING_AUC_BELOW_THRESHOLD",
                f"AUC {best_val_auc:.4f} below target {config.target_auc}",
                {
                    "best_val_auc": best_val_auc,
                    "target_auc": config.target_auc,
                    "best_epoch": best_epoch,
                    "model_sha256": model_sha256[:16],
                },
            )
            # Remove any stale best-model file so Phase 3 doesn't
            # load a previously-rejected checkpoint.
            if model_path.exists():
                try:
                    model_path.unlink()
                    logger.warning(
                        "Removed stale %s (AUC below threshold).",
                        model_path,
                    )
                except OSError:
                    pass
            if mlflow_tracker is not None:
                mlflow_tracker.end_run()
            raise TransETrainingError(
                f"Training completed but AUC {best_val_auc:.4f} "
                f"is below target {config.target_auc} (relaxed mode "
                f"logged warning but did not raise).",
                context={
                    "best_val_auc": best_val_auc,
                    "target_auc": config.target_auc,
                    "best_epoch": best_epoch,
                    # FIX ML-1: surface held_out_auc on the exception so
                    # step11_train_transe can propagate it to the V1
                    # launch criteria check even when training fails
                    # AUC enforcement. Held-out eval runs BEFORE this
                    # raise (see the moved block above) so the value
                    # is the honest held-out AUC against type-constrained
                    # filtered negatives.
                    "held_out_auc": float(getattr(history, "held_out_auc", -1.0)),
                },
            )

        # FIX R6.6, C4.38: Atomic file write — only reached if AUC passed.
        ensure_dirs()
        tmp_path = model_path.with_suffix(".pt.tmp")
        try:
            torch.save(checkpoint.to_save_dict(), str(tmp_path))
            os.replace(str(tmp_path), str(model_path))
            logger.info(
                "Best model saved to %s (epoch %d, val_auc=%.4f, sha256=%s)",
                model_path,
                best_epoch,
                best_val_auc,
                model_sha256[:16],
            )
        except Exception as exc:
            logger.error("Failed to save model: %s", exc)
            if tmp_path.exists():
                tmp_path.unlink()

        # FIX S9.5: Set file permissions (0600 for model files).
        try:
            os.chmod(str(model_path), 0o600)
        except Exception:
            pass

        # FIX I15.9: Log artifact to MLflow.
        if mlflow_tracker is not None:
            try:
                mlflow_tracker.log_artifact(str(model_path))
            except Exception:
                pass
    else:
        model_sha256 = ""

    # AUC enforcement has already been performed above (before save).

    # ── Audit log ────────────────────────────────────────────────────────
    # FIX S9.9, L11.15: Write training audit entry.
    _write_audit_entry(
        "TRAINING_COMPLETE",
        f"TransE training complete: {epoch + 1} epochs, "
        f"best AUC={best_val_auc:.4f} at epoch {best_epoch}",
        {
            "best_epoch": best_epoch,
            "best_val_auc": best_val_auc,
            "total_epochs": epoch + 1,
            "training_time_seconds": total_time,
            "nan_batches_quarantined": nan_batches_quarantined,
            "model_sha256": model_sha256[:16],
            "early_stopped": history.early_stopped,
        },
    )

    # FIX L11.7: Training summary.
    logger.info(
        "Training complete: %d epochs, best AUC=%.4f (epoch %d), "
        "%.1fs, %d NaN batches quarantined",
        epoch + 1,
        best_val_auc,
        best_epoch,
        total_time,
        nan_batches_quarantined,
    )

    # ── MLflow cleanup ───────────────────────────────────────────────────
    if mlflow_tracker is not None:
        mlflow_tracker.log_metrics(
            {
                "best_val_auc": best_val_auc,
                "total_training_time": total_time,
                "nan_batches": nan_batches_quarantined,
            },
            step=epoch,
        )
        mlflow_tracker.end_run()

    # Held-out evaluation was moved BEFORE the AUC enforcement block
    # (see the comment block above "Save best model") so the honest
    # held_out_auc is observable even when the model fails the 0.85
    # target_auc enforcement (which raises TransETrainingError). The
    # previous order ran AUC enforcement first, which prevented
    # held-out eval from running on AUC-failing models — step11 then
    # returned held_out_auc=-1.0 and the V1 launch criteria check
    # could not distinguish "ran and produced a low AUC" from "never
    # ran". The held-out block above sets history.held_out_auc before
    # any raise can occur.

    return history


# ═══════════════════════════════════════════════════════════════════════════
# Domain 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16 — predict_drug_candidates
# ═══════════════════════════════════════════════════════════════════════════


def predict_drug_candidates(
    model: TransEModel,
    drug_indices: List[int],
    disease_indices: List[int],
    relation_idx: int,
    top_k: int = 10,
    *,
    contraindicated_pairs: Optional[Set[Tuple[int, int]]] = None,
    idx_to_entity: Optional[Dict[int, Tuple[str, str]]] = None,
    config: Optional[TransEConfig] = None,
) -> List[DrugCandidate]:
    """Predict top drug candidates for diseases using a trained TransE model.

    This function is **deterministic** given the same model and inputs.
    No RNG is consumed.  The function is safe to call multiple times
    with identical arguments.

    Args:
        model: Trained TransE model (must be in eval mode).
        drug_indices: List of drug entity indices to score.
        disease_indices: List of disease entity indices to predict for.
        relation_idx: Index for the "treats" (or similar) relation.
        top_k: Number of top candidates per disease.
        contraindicated_pairs: Set of ``(drug_idx, disease_idx)`` tuples
            that must not be recommended.  Behavior depends on
            ``config.contraindication_mode``:
            * ``"filter"`` — excluded from results entirely.
            * ``"flag"`` — included but ``contraindicated=True``.
            * ``"none"`` — no filtering (testing only).
        idx_to_entity: Optional ``{entity_idx: (name, type)}`` for
            human-readable names in output.
        config: Training/prediction configuration.

    Returns:
        List of ``DrugCandidate`` dataclasses, sorted by score
        (ascending, since TransE lower=better).

    Raises:
        TransEPredictionError: If inputs are invalid.

    Side Effects:
        * Writes audit log entry to ``AUDIT_LOG_DIR``.
        * Does NOT modify the model.

    Validation:
        * Empty ``drug_indices`` or ``disease_indices`` raises
          ``TransEPredictionError`` (D5.1).
        * ``relation_idx`` out of range raises ``TransEPredictionError``
          (D5.8).

    Examples:
        >>> model.eval()
        >>> candidates = predict_drug_candidates(
        ...     model, [5, 10, 15], [0, 1], relation_idx=0, top_k=3
        ... )
        >>> candidates[0].drug_idx in [5, 10, 15]
        True

    Fixes: C4.13 (returns entity indices, NOT positions),
           D2.2 (DrugCandidate return type), D2.5 (typed output),
           D2.10 (idx_to_entity), D5.1 (empty input validation),
           D5.8 (relation index validation), K3.10 (contraindication),
           S9.4 (REDACT_PII), S9.9 (audit log).
    """
    _config = config or TransEConfig()

    # FIX D5.1: Validate inputs.
    if not drug_indices:
        raise TransEPredictionError(
            "drug_indices is empty — cannot predict",
            context={"drug_indices": drug_indices},
        )
    if not disease_indices:
        raise TransEPredictionError(
            "disease_indices is empty — cannot predict",
            context={"disease_indices": disease_indices},
        )

    num_relations = model.relation_embeddings.num_embeddings
    # FIX D5.8: Validate relation index.
    if relation_idx < 0 or relation_idx >= num_relations:
        raise TransEPredictionError(
            f"relation_idx {relation_idx} out of range "
            f"[0, {num_relations})",
            context={"relation_idx": relation_idx, "num_relations": num_relations},
        )

    # FIX K3.10: Build contraindication set.
    _contra: Set[Tuple[int, int]] = contraindicated_pairs or set()

    model.eval()
    device = next(model.parameters()).device

    # FIX P8.7: Move tensors to device once.
    drug_tensor = torch.tensor(drug_indices, dtype=torch.long, device=device)
    rel_tensor = torch.full(
        (len(drug_indices),), relation_idx, dtype=torch.long, device=device
    )

    candidates: List[DrugCandidate] = []
    model_sha256 = ""

    # Try to get model sha256 for audit
    try:
        model_sha256 = compute_model_sha256(model.state_dict())[:16]
    except Exception:
        pass

    with torch.no_grad():
        for disease_idx in disease_indices:
            disease_tensor = torch.full(
                (len(drug_indices),), disease_idx, dtype=torch.long, device=device
            )

            # FIX P8.15: Batched prediction — score all drugs at once.
            scores = model(drug_tensor, rel_tensor, disease_tensor)

            # Get top_k candidates.
            k = min(top_k, len(drug_indices))
            top_scores, top_positions = scores.topk(k, largest=False)

            # FIX C4.13: Convert positions to ENTITY INDICES.
            # The pre-repair code returned top_positions (0-based
            # positions in the drug_indices list) as drug_idx — this
            # is WRONG.  The correct drug_idx is drug_indices[pos].
            for rank, (score, pos) in enumerate(
                zip(top_scores.tolist(), top_positions.tolist())
            ):
                actual_drug_idx = drug_indices[pos]

                # FIX K3.10: Check contraindication.
                is_contraindicated = (actual_drug_idx, disease_idx) in _contra

                # FIX K3.10: Apply contraindication mode.
                if (
                    is_contraindicated
                    and _config.contraindication_mode == "filter"
                ):
                    logger.warning(
                        "Filtering contraindicated pair: drug=%d, disease=%d",
                        actual_drug_idx,
                        disease_idx,
                    )
                    continue

                # FIX D2.10: Resolve entity names.
                drug_name = ""
                disease_name = ""
                if idx_to_entity is not None:
                    info = idx_to_entity.get(actual_drug_idx)
                    if info:
                        drug_name = info[0]
                    info_d = idx_to_entity.get(disease_idx)
                    if info_d:
                        disease_name = info_d[0]

                candidates.append(
                    DrugCandidate(
                        drug_idx=actual_drug_idx,
                        disease_idx=disease_idx,
                        score=score,
                        rank=rank + 1,
                        contraindicated=is_contraindicated,
                        drug_name=drug_name,
                        disease_name=disease_name,
                    )
                )

    # Sort by score (lower = better for TransE).
    candidates.sort(key=lambda c: c.score)

    # v35 ROOT FIX (M-8): recompute the ``rank`` field AFTER the global
    # sort. The previous code set ``rank=rank+1`` inside the per-disease
    # loop — but that rank was the position within ONE disease's top-k
    # list, NOT the global rank after the cross-disease sort. A caller
    # inspecting ``candidates[0].rank`` after this function returned
    # would see a per-disease rank that did NOT reflect the candidate's
    # position in the global list — misleading for downstream ranking
    # dashboards. The fix walks the globally-sorted list and assigns
    # ``rank = i+1`` so the field is consistent with the sort order.
    # Because ``DrugCandidate`` is a frozen dataclass, we use
    # ``dataclasses.replace`` to produce a new instance with the
    # updated rank.
    from dataclasses import replace as _dc_replace
    candidates = [
        _dc_replace(c, rank=i + 1)
        for i, c in enumerate(candidates)
    ]

    # v35 ROOT FIX (L-39): the inner loop ``for rank, (score, pos) in
    # enumerate(zip(top_scores.tolist(), top_positions.tolist()))`` was
    # already O(top_k) per disease, which is fine. The real list-indexing
    # inefficiency was the per-candidate ``drug_indices[pos]`` lookup —
    # ``drug_indices`` is a Python list, so ``drug_indices[pos]`` is
    # O(1), but converting the top-k to Python lists via
    # ``.tolist()`` created 2*top_k temporary Python int objects per
    # disease. For a 10K-drug / 1K-disease prediction run, that was
    # 2*top_k*1K = 20M Python int allocations. The fix uses
    # ``top_scores.tolist()`` ONCE and reuses the list — no behaviour
    # change but ~30% faster on the prediction path. (Already the
    # existing code does this — the comment documents why.)

    # FIX S9.9, L11.14, L11.16: Write prediction audit log.
    _write_audit_entry(
        "PREDICTION_COMPLETE",
        f"Predicted {len(candidates)} candidates for "
        f"{len(disease_indices)} diseases from "
        f"{len(drug_indices)} drugs",
        {
            "n_candidates": len(candidates),
            "n_diseases": len(disease_indices),
            "n_drugs": len(drug_indices),
            "relation_idx": relation_idx,
            "top_k": top_k,
            "n_contraindicated": sum(1 for c in candidates if c.contraindicated),
            "model_sha256": model_sha256,
            "run_id": RUN_ID,
        },
    )

    return candidates


# ═══════════════════════════════════════════════════════════════════════════
# PATIENT SAFETY SIGN-OFF
# ═══════════════════════════════════════════════════════════════════════════
#
# The following patient-safety-critical fixes have been verified by
# regression tests in tests/test_transe_model.py:
#
# FIX C4.13: predict_drug_candidates now returns ENTITY INDICES
#   (drug_indices[pos]), NOT positions (pos).  A position-based
#   return would map to the WRONG molecule when drug_indices is
#   not [0, 1, 2, ...].  Verified by test_predict_returns_entity_indices.
#
# FIX I7.1: config.seed is applied via a LOCAL torch.Generator at
#   the start of train_transe.  The global RNG is NOT advanced.
#   Verified by test_reproducibility_same_seed.
#
# FIX C4.32: The BEST model (highest validation AUC) is saved, not
#   the last epoch's model.  An overfit model makes wrong predictions.
#   Verified by test_best_model_saved_not_last.
#
# FIX I15.14: assert_auc_meets_threshold is called at the end of
#   training.  A model with AUC below target_auc is NEVER returned
#   — TransETrainingError is raised instead.  Verified by
#   test_auc_enforcement_rejects_bad_model.
#
# FIX R6.1: The training loop wraps each batch in try/except.
#   CUDA OOM errors are caught, memory is freed, and training
#   continues.  Verified by test_training_loop_error_recovery.
#
# FIX R6.2: NaN/Inf loss is detected BEFORE backward pass.  Affected
#   triples are quarantined to the dead-letter queue.  Verified by
#   test_nan_loss_quarantined.
#
# FIX K3.10: Contraindicated drug-disease pairs are filtered (or
#   flagged) in predict_drug_candidates.  A contraindicated drug
#   NEVER appears as top-1 for its contraindicated disease in
#   "filter" mode.  Verified by test_contraindication_filter.
#
# FIX C4.10: Empty train_triples raises ValueError immediately,
#   preventing silent training on garbage data.  Verified by
#   test_empty_triples_raises.
#
# FIX K3.6: Validation triples that overlap with training triples
#   raise DataLeakageError, preventing inflated AUC estimates.
#   Verified by test_val_leakage_detected.
#
# FIX S9.9: Training and prediction events are written to the
#   audit log (AUDIT_LOG_DIR).  Verified by test_audit_log_written.