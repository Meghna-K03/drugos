"""DrugOS Graph Module -- KG Embedding Model Protocol
=====================================================
Defines the structural interface (Protocol) for any knowledge graph
embedding model used in the DrugOS pipeline.

Why a Protocol?
  * ``TransEModel`` (Week 2 baseline) and the Phase 3 Graph Transformer
    both produce entity/relation embeddings and scores. This Protocol
    lets ``train_transe``, ``predict_drug_candidates``, and evaluation
    code accept ANY model that conforms to the interface -- enabling
    drop-in replacement without changing downstream consumers.
  * Fixes A1.6 -- transe_model.py was not interchangeable with future models.

Interoperability:
  * Any model implementing this Protocol can be passed to
    ``train_transe(model=..., ...)``, ``predict_drug_candidates(model=..., ...)``,
    and ``evaluate_link_prediction(...)`` without code changes.

Fixes: A1.6, I15.13.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable, Tuple

import torch


@runtime_checkable
class KGEmbeddingModel(Protocol):
    """Structural interface for knowledge graph embedding models.

    Any model that provides entity/relation embeddings and a score
    function ``(head, relation, tail) -> scores`` satisfies this Protocol.
    ``@runtime_checkable`` enables ``isinstance(model, KGEmbeddingModel)``
    checks at runtime (for validation guards, not for branching logic).

    Attributes:
        entity_embeddings: An ``nn.Embedding`` whose ``.weight`` tensor
            has shape ``(num_entities, embedding_dim)``.
        relation_embeddings: An ``nn.Embedding`` whose ``.weight`` tensor
            has shape ``(num_relations, embedding_dim)``.

    Methods:
        forward(head_indices, rel_indices, tail_indices) -> Tensor:
            Compute plausibility scores for triples.
        normalize_entity_embeddings() -> None:
            Normalize entity embeddings (TransE-specific convention).
    """

    @property
    def entity_embeddings(self) -> torch.nn.Embedding:
        """Entity embedding lookup table: (num_entities, embedding_dim)."""
        ...

    @property
    def relation_embeddings(self) -> torch.nn.Embedding:
        """Relation embedding lookup table: (num_relations, embedding_dim)."""
        ...

    def forward(
        self,
        head_indices: torch.Tensor,
        rel_indices: torch.Tensor,
        tail_indices: torch.Tensor,
    ) -> torch.Tensor:
        """Compute plausibility score for each (h, r, t) triple.

        Args:
            head_indices: Entity index tensor for triple heads.
            rel_indices: Relation index tensor.
            tail_indices: Entity index tensor for triple tails.

        Returns:
            Tensor of shape ``(batch_size,)`` with one score per triple.
            Convention varies by model: TransE uses L2 distance (lower=better);
            the Phase 3 Graph Transformer may use dot product (higher=better).
        """
        ...

    def normalize_entity_embeddings(self) -> None:
        """Normalize entity embeddings to unit L2 norm (TransE convention).

        Called after each optimizer step. Models that do not require
        normalization (e.g., DistMult, ComplEx) can implement this as
        a no-op.
        """
        ...


__all__: list[str] = ["KGEmbeddingModel"]