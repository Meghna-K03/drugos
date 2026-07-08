"""DrugOS Graph Module — GPU Utilities
========================================
GPU memory validation and batch size testing for PyG data loading.
"""

import logging
from typing import Any, Dict, Optional

import torch

logger = logging.getLogger(__name__)


def check_gpu_available() -> Dict[str, Any]:
    """Check GPU availability and memory."""
    info = {"cuda_available": torch.cuda.is_available()}

    if torch.cuda.is_available():
        info["device_name"] = torch.cuda.get_device_name(0)
        info["device_count"] = torch.cuda.device_count()
        info["total_memory_gb"] = torch.cuda.get_device_properties(0).total_mem / 1e9
        info["allocated_memory_gb"] = torch.cuda.memory_allocated(0) / 1e9
        info["free_memory_gb"] = info["total_memory_gb"] - info["allocated_memory_gb"]

    logger.info(f"GPU check: {info}")
    return info


def test_batch_memory(
    num_nodes: int = 100000,
    num_edges: int = 6000000,
    feat_dim: int = 256,
    batch_size: int = 512,
) -> Dict[str, Any]:
    """Test if GPU memory can fit a mini-batch.

    Args:
        num_nodes: Approximate total node count.
        num_edges: Approximate total edge count.
        feat_dim: Node feature dimension.
        batch_size: Mini-batch size to test.

    Returns:
        Dict with memory estimates and pass/fail.
    """
    # Estimate memory per node feature
    node_feat_bytes = num_nodes * feat_dim * 4  # float32
    # Estimate memory per edge (2 int64 indices)
    edge_index_bytes = num_edges * 2 * 8  # int64

    total_estimated_gb = (node_feat_bytes + edge_index_bytes) / 1e9

    result = {
        "estimated_total_gb": round(total_estimated_gb, 2),
        "node_feat_gb": round(node_feat_bytes / 1e9, 2),
        "edge_index_gb": round(edge_index_bytes / 1e9, 2),
        "batch_size": batch_size,
    }

    if torch.cuda.is_available():
        free_gb = (torch.cuda.get_device_properties(0).total_mem -
                   torch.cuda.memory_allocated(0)) / 1e9
        result["gpu_free_gb"] = round(free_gb, 2)
        result["fits_gpu"] = total_estimated_gb < free_gb

        # Test actual mini-batch allocation
        try:
            test_batch = torch.randn(batch_size, feat_dim, device="cuda")
            result["batch_test"] = "PASS"
            del test_batch
            torch.cuda.empty_cache()
        except torch.cuda.OutOfMemoryError:
            result["batch_test"] = "FAIL — OOM"
    else:
        result["fits_gpu"] = False
        result["batch_test"] = "SKIP — no GPU"

    logger.info(f"GPU memory test: {result}")
    return result


def recommend_batch_size(
    total_memory_gb: float,
    feat_dim: int = 256,
    safety_factor: float = 0.7,
    # v34 ROOT FIX (HIGH #9): the previous default was `num_negatives=1`
    # "for backward compat with callers that don't pass it." But
    # TransEConfig.num_negatives defaults to 10. Callers that didn't
    # pass `num_negatives` got a batch size recommendation 11× too large
    # → OOM on GPUs the function claimed were safe. The fix: default to
    # 10 (matching TransEConfig) so callers get the CORRECT memory
    # estimate out of the box. Callers that want the old behavior can
    # explicitly pass `num_negatives=1`.
    num_negatives: int = 10,
) -> int:
    """Recommend maximum batch size based on available GPU memory.

    v28 ROOT FIX (audit ML-11): the previous formula
    ``bytes_per_sample = feat_dim * 4 * 2`` assumed exactly 2 nodes
    per sample (src + dst of a positive edge). But the TransE trainer
    (and any link-prediction trainer using negative sampling) actually
    loads ``1 + num_negatives`` nodes per sample — the positive's src
    and dst, PLUS one tail embedding per negative sample. With the
    default ``num_negatives=10``, the true memory cost per positive
    sample is ``feat_dim * 4 * 2 * (1 + 10) = 22 * feat_dim`` bytes —
    11× what the old formula assumed. The recommended batch size was
    therefore 11× too large, causing OOM crashes on GPUs that the
    function claimed were safe.

    The fix adds an explicit ``num_negatives`` parameter and corrects
    the formula to ``bytes_per_sample = feat_dim * 4 * 2 * (1 +
    num_negatives)``.

    v34 ROOT FIX (HIGH #9): default changed from 1 to 10 to match
    TransEConfig.num_negatives. Callers that don't pass `num_negatives`
    now get the CORRECT memory estimate (11× smaller batch) instead of
    an OOM-inducing 11× over-estimate.

    Parameters
    ----------
    total_memory_gb : float
        Total GPU memory in GB.
    feat_dim : int
        Node feature / embedding dimension. Default 256 (matches
        TransEConfig.embedding_dim default).
    safety_factor : float
        Fraction of total memory to use. Default 0.7 (leaves 30% for
        gradients, activations, and framework overhead).
    num_negatives : int
        Number of negative samples per positive sample. Default 10
        (matches TransEConfig.num_negatives).
    """
    available_bytes = total_memory_gb * 1e9 * safety_factor
    # v28 ML-11: (1 + num_negatives) factor for negative sampling.
    # * 4  : float32 bytes per scalar.
    # * 2  : src + dst of the positive triple.
    # * (1 + num_negatives): the positive itself + one tail embedding
    #   per negative sample.
    # audit-2025 ROOT FIX (issue 3): the previous formula omitted the
    # memory for gradients (1x params) and Adam optimizer state (2x
    # params — momentum + variance). Total factor = 1 (params) + 1
    # (grads) + 2 (Adam state) = 4x the embedding-table memory. Without
    # this factor, the formula recommended batch sizes that OOM'd on
    # "safe" GPUs as soon as the optimizer.step() ran. The fix multiplies
    # the per-sample cost by 4 to account for the full training-time
    # memory footprint. (SGD would use 2x instead of 4x, but TransE
    # defaults to Adam per config.py.)
    _ADAM_MEMORY_FACTOR = 4  # params + grads + Adam momentum + Adam variance
    bytes_per_sample = feat_dim * 4 * 2 * (1 + num_negatives) * _ADAM_MEMORY_FACTOR
    max_batch = int(available_bytes / bytes_per_sample)

    # Cap at reasonable values
    recommended = min(max_batch, 8192)
    logger.info(
        f"Recommended batch size: {recommended} "
        f"(GPU: {total_memory_gb:.1f}GB, feat_dim={feat_dim}, "
        f"num_negatives={num_negatives}, "
        f"bytes_per_sample={bytes_per_sample})"
    )
    return recommended
