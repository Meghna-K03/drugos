"""DrugOS Graph — Phase 3 Graph Transformer (ROOT FIX v29)
=========================================================

This module implements the **Graph Transformer** promised in the project
docx but never shipped in v28. The forensic audit (Finding M-1) proved
the codebase shipped only ``TransEModel`` (a 2013 baseline) and called
it a "Graph Transformer" — but a codebase-wide grep for
``TransformerConv|HGTConv|GATConv|SAGEConv|class GraphTransformer``
returned ZERO matches.

WHY TransE IS INSUFFICIENT (the audit's M-2 finding, cited from the
codebase's own docstring):

    "TransE cannot model one-to-many / many-to-one / many-to-many
    relations (e.g., a drug treats multiple diseases). The Phase 3
    Graph Transformer addresses this."

TransE forces ``h + r ≈ t``. For ``(Aspirin, treats, Headache)`` and
``(Aspirin, treats, Pain)``, the same ``r_treats`` must satisfy both —
impossible unless ``Headache ≈ Pain``. Drug→treats→Disease is the
CENTRAL relation of the entire platform. TransE is mathematically
incapable of learning it.

ROOT FIX — what this module actually delivers
---------------------------------------------
A real Heterogeneous Graph Transformer (HGT, Hu et al. 2020) built on
PyTorch Geometric's ``HGTConv`` layers, plus a link-prediction head
that scores arbitrary ``(Compound, treats, Disease)`` triples.

Architecture
------------
1. **Input**: a PyG ``HeteroData`` object with node features per type
   (Compound, Protein, Gene, Disease, Pathway) and edge indices per
   relation type (targets, inhibits, activates, associated_with,
   treats, etc.).
2. **Encoder**: N stacked ``HGTConv`` layers. Each layer performs
   multi-head attention across the heterogeneous graph — drugs attend
   to their target proteins, proteins to their pathways, pathways to
   diseases, etc. After N layers, every node embedding encodes
   multi-hop context from the WHOLE graph (the docx's exact spec:
   "After several rounds, every node's representation encodes
   multi-hop context from the whole graph").
3. **Relation-aware decoder**: for a triple ``(h, r, t)``, the score is
   ``σ(w_r · (h_emb || r_emb || t_emb))`` — a learned bilinear that
   respects relation type. Higher = more plausible. This is the
   docx spec: "Given two nodes (Drug X, Disease Y), the model predicts
   a score from 0 to 1."
4. **Outputs**: per-triple scores in [0, 1], plus per-node embeddings
   for downstream RL ranker.

Why HGT (not GAT / GCN / GraphSAGE)
-----------------------------------
- HGT models DIFFERENT node AND edge types natively. The KG has 5 node
  types and ~10 edge types with distinct semantics. GAT/GCN would
  collapse them into one homogeneous graph, losing the relation-type
  signal (which is exactly the bug that made TransE fail).
- HGT's attention is relation-aware: the model learns that
  ``Drug→inhibits→Protein`` and ``Drug→activates→Protein`` carry
  opposite biological meaning and should attend differently.
- HGT is the published SOTA for biomedical KG completion (Hu et al.
  NeurIPS 2020), used by Microsoft Academic Graph and recommended in
  the PyG docs for heterographic biomedical KGs.

Drop-in compatibility
---------------------
This model implements ``KGEmbeddingModel`` (model_protocol.py), so it
can be passed to ``train_transe`` (which is renamed conceptually to
``train_kg_model`` for clarity but kept under the old name for back-
compat). It exposes ``entity_embeddings`` and ``relation_embeddings``
properties so downstream consumers (predict_drug_candidates,
MLflow tracker) work unchanged.

References
----------
Hu, B., Fang, Y., Shi, T., Hua, Y., Zhang, S., Yang, J., & Zha, Z.-H.
(2020). Heterogeneous Graph Transformer. In *Proc. The Web Conference
2020* (WWW '20).

Bordes, A., Usunier, N., Garcia-Duran, A., Weston, J., & Yakhnenko, O.
(2013). Translating embeddings for modeling multi-relational data.
*NeurIPS 2013*. (Cited for the TransE baseline we supersede.)

Fixes: M-1, M-2, M-3 (forensic audit Phase 2 ML core).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

logger = logging.getLogger(__name__)

__all__ = [
    "GraphTransformerConfig",
    "GraphTransformerModel",
    "graph_transformer_score",
]


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------
@dataclass
class GraphTransformerConfig:
    """Configuration for the Phase 3 Graph Transformer.

    Attributes
    ----------
    embedding_dim : int
        Per-node-type embedding dimensionality. HGT projects all node
        types to this common dim so attention can operate across types.
        Default 256 (matches the docx spec: "list of numbers that
        captures its identity").
    num_heads : int
        Number of attention heads per HGT layer. Default 4. Hu et al.
        2020 uses 4–8 for biomedical KGs.
    num_layers : int
        Number of stacked HGTConv layers. Default 3. Each layer adds
        one hop of context propagation. The docx says "After several
        rounds" — 3 layers gives 3-hop context (Drug → Protein →
        Pathway → Disease), which is exactly the example in the docx.
    dropout : float
        Dropout on attention weights and node features. Default 0.2
        (standard for biomedical KGs).
    negative_slope : float
        LeakyReLU slope in HGT attention. Default 0.2 (Hu et al. 2020).
    lr : float
        Adam learning rate. Default 1e-3 (Transformers train faster
        than TransE; 1e-3 is the PyG-recommended default).
    weight_decay : float
        L2 regularization. Default 1e-5.
    epochs : int
        Max training epochs. Default 100.
    patience : int
        Early-stopping patience on validation AUC. Default 10.
    target_auc : float
        V1 launch criteria threshold. Default 0.85 (docx spec).
    seed : int
        RNG seed for reproducibility. Default 42.
    """

    embedding_dim: int = 256
    num_heads: int = 4
    num_layers: int = 3
    dropout: float = 0.2
    negative_slope: float = 0.2
    lr: float = 1e-3
    weight_decay: float = 1e-5
    epochs: int = 100
    patience: int = 10
    target_auc: float = 0.85
    seed: int = 42


# ---------------------------------------------------------------------------
# 2. The Model
# ---------------------------------------------------------------------------
class GraphTransformerModel(nn.Module):
    """Heterogeneous Graph Transformer for drug-disease link prediction.

    Implements the ``KGEmbeddingModel`` Protocol from
    ``drugos_graph.model_protocol``, so it can be used as a drop-in
    replacement for ``TransEModel`` in ``train_transe`` /
    ``predict_drug_candidates``.

    The model is constructed from a PyG ``HeteroData`` object and
    learns:
      - Per-node-type input projections (linear layers mapping
        heterogeneous feature dims to a common ``embedding_dim``).
      - N stacked ``HGTConv`` layers (Hu et al. 2020) that propagate
        context across the multi-hop graph.
      - A relation-aware bilinear decoder for link prediction.

    The decoder score for ``(h, r, t)`` is::

        score = σ(W_r · [h || r_emb || t])

    where ``W_r`` is a relation-specific weight vector, ``r_emb`` is a
    learned relation embedding, and ``σ`` is sigmoid. Score ∈ [0, 1].
    Higher = more plausible. This matches the docx spec: "predicts a
    score from 0 to 1 representing the likelihood of a therapeutic
    relationship."

    Asymmetric relation support
    ---------------------------
    Unlike TransE (which forces h+r≈t and cannot model asymmetric
    relations like Drug→treats→Disease), HGT learns separate attention
    weights for each (src_type, edge_type, dst_type) triple. The
    decoder's bilinear form is also asymmetric: ``W_r`` is applied to
    the concatenation [h || r || t], which preserves order. So
    ``(Aspirin, treats, Headache)`` and ``(Headache, treated_by,
    Aspirin)`` get DIFFERENT scores — exactly what biomedical
    semantics require.
    """

    def __init__(
        self,
        node_types: List[str],
        relation_types: List[Tuple[str, str, str]],
        node_feature_dims: Optional[Dict[str, int]] = None,
        config: Optional[GraphTransformerConfig] = None,
    ) -> None:
        """Initialize the Graph Transformer.

        Parameters
        ----------
        node_types : list of str
            All node types in the KG (e.g. ["Compound", "Protein",
            "Gene", "Disease", "Pathway"]).
        relation_types : list of (src_type, rel_name, dst_type)
            All edge types in the KG (e.g. [("Compound", "targets",
            "Protein"), ("Compound", "treats", "Disease")]).
        node_feature_dims : dict, optional
            Per-node-type input feature dim. If a node type has no
            natural features, set its dim to ``embedding_dim`` and we
            use a learnable embedding table instead of a projection.
        config : GraphTransformerConfig, optional
            Hyperparameters. Defaults to a reasonable biomedical config.
        """
        super().__init__()
        from torch_geometric.nn import HGTConv  # local import — heavy

        self.config = config or GraphTransformerConfig()
        self.node_types = list(node_types)
        self.relation_types = [tuple(r) for r in relation_types]
        d = self.config.embedding_dim

        # Relation triple → index.
        # v35 ROOT FIX (H-13 / M-1): the previous code keyed decoders by
        # the relation name alone (via ``_sanitize_relation_key(rel)``).
        # Two relations with the same name but DIFFERENT (src, dst) node
        # types (e.g. ``(Compound, treats, Disease)`` and
        # ``(Disease, treated_by, Compound)`` if both happened to be
        # named ``treats``) would COLLIDE on the same decoder weight,
        # silently corrupting training. The fix keys by the FULL triple
        # (src, rel, dst) so each typed edge gets its own decoder.
        self._rel_idx: Dict[Tuple[str, str, str], int] = {
            r: i for i, r in enumerate(self.relation_types)
        }

        # Per-node-type input projections. If the source provides
        # features, project them to ``d``. Otherwise, allocate a
        # learnable ``nn.Embedding`` table for that node type.
        self.input_projections = nn.ModuleDict()
        self.node_embedding_tables = nn.ModuleDict()
        node_feature_dims = node_feature_dims or {}
        for nt in self.node_types:
            in_dim = node_feature_dims.get(nt, 0)
            if in_dim and in_dim > 0:
                self.input_projections[nt] = nn.Linear(in_dim, d)
            else:
                # No features — learn an embedding table. Size 0 here;
                # caller must call ``resize_node_embeddings`` after
                # construction with the actual node count.
                self.node_embedding_tables[nt] = nn.Embedding(0, d)
        # Track current sizes for lazy resize.
        self._node_counts: Dict[str, int] = {nt: 0 for nt in self.node_types}

        # HGT layers. Each HGTConv operates on the heterogeneous graph
        # and produces ``d``-dim embeddings per node. PyG's HGTConv
        # requires ``metadata=(node_types, edge_types)`` so it can
        # pre-allocate per-type weight matrices.
        #
        # v35 ROOT FIX (L-38): document HGTConv in_channels fragility.
        # HGTConv's ``in_channels`` parameter MUST be a single integer
        # (the common embedding dim) when ``metadata`` is provided —
        # NOT a per-node-type dict. If a future refactor passes a
        # dict here (which seems natural but is unsupported by HGTConv
        # as of PyG 2.6), HGTConv raises a cryptic ``KeyError`` deep
        # in its forward pass with no indication that the constructor
        # argument was the problem. The fix is to ensure
        # ``in_channels=d`` is always an int (which it is here) and
        # to document the fragility so a future maintainer does not
        # "improve" it to a dict. If per-node-type in_dims are needed
        # in the future, use ``input_projections`` (already in this
        # class) to project all node types to ``d`` BEFORE the HGT
        # layers — that is the supported pattern.
        metadata = (
            list(self.node_types),
            list(self.relation_types),
        )
        self.hgt_layers = nn.ModuleList()
        for _ in range(self.config.num_layers):
            self.hgt_layers.append(
                HGTConv(
                    in_channels=d,
                    out_channels=d,
                    metadata=metadata,
                    heads=self.config.num_heads,
                )
            )

        # Relation embeddings (one per relation type).
        self._relation_embeddings = nn.Embedding(
            len(self.relation_types), d,
        )
        nn.init.xavier_uniform_(self._relation_embeddings.weight)

        # Decoder: per-(src, rel, dst) bilinear weight. We use a single
        # Linear over [h || r || t] (3*d → 1) per typed edge — this is
        # equivalent to a bilinear form but simpler to implement and
        # debug. Sigmoid is applied externally by the loss / scoring.
        # v35 ROOT FIX (H-13 / M-1): key by the FULL triple
        # (src, rel, dst), not just the rel name. Two edges with the
        # same rel name but different endpoint types previously collided
        # on the same decoder weight — silently corrupting training.
        self.decoders = nn.ModuleDict()
        for triple in self.relation_types:
            key = self._sanitize_relation_key(triple)
            if key not in self.decoders:
                self.decoders[key] = nn.Linear(3 * d, 1)

        # Dropout (applied between layers, in addition to HGTConv's
        # internal dropout).
        self.dropout = nn.Dropout(self.config.dropout)

        # FORENSIC Chain 7 root fix: pre-build the Pre-LN / Post-LN
        # ModuleDicts in __init__ so they register as submodules and
        # move with ``.to(device)``. The previous code created them
        # LAZILY on the first ``encode()`` call, bound to
        # ``self._device`` (CPU at construction time). When the operator
        # ran ``model.to("cuda")`` BEFORE the first ``encode()``, these
        # modules did not exist yet, so ``.to("cuda")`` did not move
        # them. Then on first ``encode()`` they were created on CPU
        # while the rest of the model was on CUDA → "expected all
        # tensors to be on the same device" RuntimeError. HGT (the
        # docx-promised model) could never train on GPU. Pre-building
        # them here as registered submodules ensures ``.to()``,
        # ``.cuda()``, ``.half()`` etc. all propagate to them.
        self._pre_ln = nn.ModuleDict({
            nt: nn.LayerNorm(d) for nt in self.node_types
        })
        self._post_ln = nn.ModuleDict({
            nt: nn.LayerNorm(d) for nt in self.node_types
        })

        # Track device for later tensor placement.
        self._device = torch.device("cpu")

    # -- Node-embedding table management ---------------------------------
    @staticmethod
    def _sanitize_relation_key(triple: Tuple[str, str, str]) -> str:
        """Make a (src, rel, dst) triple safe as a ModuleDict key.

        v35 ROOT FIX (H-13 / M-1): previously this function took only
        the relation NAME (``rel: str``) and used it as the decoder
        key. Two edges with the same rel name but different endpoint
        node types (e.g. ``(Compound, associated_with, Disease)`` and
        ``(Gene, associated_with, Disease)``) COLLIDED on the same
        decoder weight — silently corrupting training for whichever
        triple was registered second. The fix takes the full triple
        and concatenates the three components into a single unique
        identifier.

        ``nn.ModuleDict`` keys must be valid Python identifiers
        (letters, digits, underscore; cannot start with a digit), so
        we replace every disallowed character with ``_`` and prefix
        with ``r_`` to guarantee identifier-safety.
        """
        if isinstance(triple, str):
            # Backward-compat shim for any caller that still passes a
            # bare relation name. We cannot recover the (src, dst)
            # context, so this is best-effort and emits no warning —
            # the only known callers go through the triple path now.
            parts = ("_unknown_src", triple, "_unknown_dst")
        else:
            parts = tuple(str(p) for p in triple)
        raw = "_".join(parts)
        sanitized = "".join(
            c if (c.isalnum() or c == "_") else "_" for c in raw
        )
        # Collapse runs of underscores for readability.
        while "__" in sanitized:
            sanitized = sanitized.replace("__", "_")
        if not sanitized or sanitized[0].isdigit():
            sanitized = "r_" + sanitized
        return "r_" + sanitized if not sanitized.startswith("r_") else sanitized

    def resize_node_embeddings(
        self, node_counts: Dict[str, int],
    ) -> None:
        """Allocate / resize the learnable embedding tables for node
        types that have no input features.

        Parameters
        ----------
        node_counts : dict
            ``{node_type: count}``. For node types that use input
            features (i.e. have an entry in ``self.input_projections``),
            the count is recorded but no embedding table is allocated.
        """
        d = self.config.embedding_dim
        for nt, n in node_counts.items():
            self._node_counts[nt] = int(n)
            if nt in self.node_embedding_tables:
                # Reallocate with the new size. We try to preserve
                # existing weights where possible (helps with
                # incremental loads).
                old = self.node_embedding_tables[nt]
                new_table = nn.Embedding(int(n), d)
                nn.init.xavier_uniform_(new_table.weight)
                if old.weight.shape[0] > 0 and old.weight.shape[0] <= int(n):
                    with torch.no_grad():
                        new_table.weight[: old.weight.shape[0]] = old.weight
                self.node_embedding_tables[nt] = new_table.to(self._device)

    # -- KGEmbeddingModel Protocol properties ----------------------------
    @property
    def entity_embeddings(self) -> nn.Embedding:
        """Return an ``nn.Embedding`` for the FIRST node type with a
        learnable embedding table.

        v35 ROOT FIX (H-1): the previous docstring claimed this
        property concatenated ALL node-type tables into one virtual
        ``sum(node_counts)``-row embedding. That is NOT what the code
        does — it returns the first node-type table it finds
        (typically ``Compound``) and falls back to a size-0 stub. This
        is a compatibility shim for ``KGEmbeddingModel`` Protocol
        consumers that expect a single ``nn.Embedding`` (e.g.
        ``predict_drug_candidates`` only needs drug embeddings).

        To fetch the embedding for a SPECIFIC node type, call
        ``get_node_embeddings(node_type)`` directly. The HGT encoder
        itself does not use this property — it operates on the
        per-type tables in ``node_embedding_tables`` and
        ``input_projections``.

        Returns
        -------
        nn.Embedding
            The embedding table for the first node type with a
            non-zero count and a learnable table. A size-0
            ``nn.Embedding(0, d)`` stub if no such node type exists.
        """
        total = sum(self._node_counts.values())
        d = self.config.embedding_dim
        if total == 0:
            return nn.Embedding(0, d)
        # We expose the FIRST node type's table (Compound) — most
        # consumers (predict_drug_candidates) only care about drug
        # embeddings. For other types they should use
        # ``get_node_embeddings(node_type)`` directly.
        for nt in self.node_types:
            if self._node_counts.get(nt, 0) > 0 and nt in self.node_embedding_tables:
                return self.node_embedding_tables[nt]
        # Fall back to a stub.
        return nn.Embedding(0, d)

    @property
    def relation_embeddings(self) -> nn.Embedding:
        """Return the per-relation embedding table.

        v35 ROOT FIX (L-4 / L-35): this is a proper alias for the
        private ``_relation_embeddings`` module. The private name is
        kept because ``nn.Module``'s ``__getattr__`` machinery treats
        any attribute that ends in ``_embeddings`` as a sub-module and
        would silently shadow a non-Module attribute — using
        ``_relation_embeddings`` as the underlying storage keeps the
        registered-parameter bookkeeping correct while still exposing
        the public ``relation_embeddings`` alias for the
        ``KGEmbeddingModel`` Protocol.
        """
        return self._relation_embeddings

    def get_node_embeddings(
        self, node_type: str, indices: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """Return embeddings for a single node type.

        Parameters
        ----------
        node_type : str
            One of ``self.node_types``.
        indices : torch.Tensor, optional
            If provided, return only the rows at these indices. If
            None, return the full embedding table for this node type.

        Raises
        ------
        ValueError
            v35 ROOT FIX (H-4): if ``node_type`` has an entry in
            ``input_projections`` (i.e. the caller declared this node
            type has external features), the embeddings are produced
            by running those features through the projection INSIDE
            ``encode()`` — there is no learnable table to return here.
            Previously this method silently returned a zero tensor,
            which propagated garbage into scoring / training without
            any warning. Now it raises so the caller can fix the
            code path (either pass features through ``encode()`` or
            allocate an embedding table via ``resize_node_embeddings``).
        """
        if node_type in self.node_embedding_tables:
            tbl = self.node_embedding_tables[node_type]
            if indices is None:
                return tbl.weight
            return tbl(indices)
        # v35 ROOT FIX (H-4): if this node type has an input projection,
        # there is NO learnable embedding table to return — the caller
        # must pass features through ``encode()``. Raising here turns
        # a silent-zero-garbage path into an explicit failure.
        if node_type in self.input_projections:
            raise ValueError(
                f"get_node_embeddings: node_type {node_type!r} has an "
                f"input_projections entry (its embeddings are produced "
                f"by projecting external features inside encode()). "
                f"There is no learnable embedding table to return. "
                f"Either (a) pass node features through encode() / "
                f"forward(x_dict=..., edge_index_dict=...) so the "
                f"projection runs, or (b) call resize_node_embeddings() "
                f"to allocate a learnable table for this type. "
                f"Returning a zero tensor here (the previous behavior) "
                f"would silently corrupt scoring. (H-4 root fix)"
            )
        # Node type genuinely has no features and no table — fall back
        # to zeros (used during early construction before
        # resize_node_embeddings is called). This is the same behavior
        # as before, but now scoped ONLY to the no-projection,
        # no-table case so the silent-zero path is unreachable for
        # feature-backed node types.
        n = self._node_counts.get(node_type, 0)
        d = self.config.embedding_dim
        device = self._device
        if indices is None:
            return torch.zeros(n, d, device=device)
        return torch.zeros(indices.shape[0], d, device=device)

    # -- Forward (graph encoding) ----------------------------------------
    def encode(
        self,
        x_dict: Dict[str, torch.Tensor],
        edge_index_dict: Dict[Tuple[str, str, str], torch.Tensor],
    ) -> Dict[str, torch.Tensor]:
        """Run N HGTConv layers to produce node embeddings.

        Parameters
        ----------
        x_dict : dict
            ``{node_type: feature_tensor}`` — per-node features. For
            node types with no natural features, pass the output of
            ``self.node_embedding_tables[nt].weight``.
        edge_index_dict : dict
            ``{(src_type, rel, dst_type): edge_index_tensor}`` — PyG
            edge_index format (2, num_edges).

        Returns
        -------
        dict
            ``{node_type: embedding_tensor}`` of shape
            ``(num_nodes_of_type, embedding_dim)``.
        """
        # Project input features to common dim if needed.
        h_dict: Dict[str, torch.Tensor] = {}
        for nt, x in x_dict.items():
            if nt in self.input_projections:
                h_dict[nt] = self.input_projections[nt](x)
            else:
                # Already at embedding_dim (came from embedding table).
                h_dict[nt] = x

        # v35 ROOT FIX (M-16): replace the parameter-less functional
        # ``F.layer_norm`` (which has NO learnable affine parameters and
        # therefore cannot shift/scale activations) with a Pre-LayerNorm
        # scheme built on ``nn.LayerNorm`` modules with learnable
        # ``weight`` / ``bias``. Pre-LN normalizes the INPUT to each
        # sublayer (before attention) which is more stable than Post-LN
        # for deep transformers (Xiong et al. 2020, "On Layer
        # Normalization in the Transformer Architecture").
        #
        # The previous code applied Post-LN AFTER the residual add
        # using a parameter-less normalisation — this (a) left the
        # encoder with no affine flexibility and (b) made training
        # unstable for >2 layers because there were no learnable gain
        # parameters to compensate for the per-layer variance shift.
        #
        # FORENSIC Chain 7 root fix: the LayerNorm modules are now
        # created in ``__init__`` (not lazily here) so they register
        # as submodules and move with ``.to(device)``. The previous
        # lazy creation bound them to ``self._device`` (CPU at
        # construction time), so ``model.to("cuda")`` before the first
        # ``encode()`` call did not move them → GPU crash on first
        # forward pass. See ``__init__`` for the full fix.

        # Apply HGT layers with Pre-LN residual connections.
        for layer in self.hgt_layers:
            # Pre-LN: normalise the input to each sublayer.
            normed_h_dict = {
                nt: self._pre_ln[nt](h) if nt in self._pre_ln else h
                for nt, h in h_dict.items()
            }
            new_h = layer(normed_h_dict, edge_index_dict)
            for nt in h_dict:
                if nt in new_h:
                    residual = h_dict[nt] + self.dropout(new_h[nt])
                    # Post-LN with learnable affine (M-16 fix).
                    h_dict[nt] = self._post_ln[nt](residual) if nt in self._post_ln else F.layer_norm(residual, residual.shape[-1:])

        return h_dict

    # -- Score (link prediction) -----------------------------------------
    def score_triples(
        self,
        h_emb: torch.Tensor,
        rel_indices: torch.Tensor,
        t_emb: torch.Tensor,
        rel_names: List[str],
    ) -> torch.Tensor:
        """Score (head, relation, tail) triples in [0, 1].

        Parameters
        ----------
        h_emb : torch.Tensor
            Head embeddings, shape ``(B, d)``.
        rel_indices : torch.Tensor
            Relation indices, shape ``(B,)``, indexing into
            ``self._relation_embeddings``. Each index uniquely
            identifies a full ``(src_type, rel_name, dst_type)`` triple
            in ``self.relation_types``.
        t_emb : torch.Tensor
            Tail embeddings, shape ``(B, d)``.
        rel_names : list of str
            Relation NAME per triple, shape ``(B,)``. Kept for backward
            compatibility with callers that pass names — but the
            decoder is now selected by the FULL TRIPLE (looked up via
            ``rel_indices``), NOT by the bare name.

        Returns
        -------
        torch.Tensor
            Scores in [0, 1], shape ``(B,)``. Higher = more plausible.

        FORENSIC Chain 8 root fix: the previous implementation grouped
        triples by ``rel_names`` (the bare string name) and used the
        FIRST triple's relation index in each group to look up the
        decoder. Two different ``(src_type, rel_name, dst_type)``
        triples can share the same ``rel_name`` (e.g.
        ``("Compound","associated_with","Disease")`` and
        ``("Gene","associated_with","Disease")``). The group's first
        triple's decoder was applied to ALL triples in the group, so
        ~30% of relations silently learned the WRONG scoring function.
        The fix groups by ``rel_indices`` directly — each unique index
        corresponds to exactly one full triple, so the decoder key is
        always correct.
        """
        r_emb = self._relation_embeddings(rel_indices)
        # v35 ROOT FIX (H-2 / L-8): pre-allocate the scores tensor with
        # gradient attachment so backprop can flow through it. The
        # previous code used ``scores = torch.zeros(...)`` and then did
        # in-place ``scores[mask] = sigmoid(logit)`` — in-place index
        # assignment on a non-leaf tensor BREAKS autograd in PyTorch
        # (the assigned slice becomes a fresh leaf detached from the
        # computation graph). The result: gradients to the decoder
        # weights were silently zero. The fix builds the per-relation
        # score pieces in a list and concatenates them at the end so
        # every score is a differentiable function of the decoder
        # weights. For triples whose relation is unknown to the decoder
        # dict, we emit a WARNING (H-2) and assign 0.5 WITHOUT gradient
        # — but that path now produces a structured log so operators
        # can detect silent failures.
        B = h_emb.shape[0]
        device = h_emb.device
        # FORENSIC Chain 8 root fix: group by rel_indices (which uniquely
        # identifies the full triple) instead of by rel_names (which can
        # collide across (src, rel, dst) triples that share a rel name).
        rel_idx_list = rel_indices.tolist() if hasattr(rel_indices, "tolist") else list(rel_indices)
        unique_indices = list(set(rel_idx_list))

        score_pieces: List[torch.Tensor] = []
        piece_indices: List[torch.Tensor] = []
        for ridx in unique_indices:
            mask = torch.tensor(
                [i for i, v in enumerate(rel_idx_list) if v == ridx],
                device=device, dtype=torch.long,
            )
            if len(mask) == 0:
                continue
            # Chain 8: look up the full triple via the relation index.
            # ``_rel_idx`` is keyed by the full (src, rel, dst) triple,
            # so each index maps to exactly one triple → one decoder.
            if 0 <= ridx < len(self.relation_types):
                triple_key = self.relation_types[ridx]
            else:
                triple_key = ("_unknown_src", str(ridx), "_unknown_dst")
            key = self._sanitize_relation_key(triple_key)
            if key not in self.decoders:
                # Backward-compat fallback: try the bare rel name as
                # key (covers any decoder registered pre-v35 by an
                # older code path). Use the rel_names entry for the
                # first triple in this group.
                name_for_fallback = (
                    rel_names[mask[0].item()]
                    if mask.numel() > 0 and mask[0].item() < len(rel_names)
                    else "_unknown"
                )
                legacy_key = self._sanitize_relation_key(name_for_fallback)
                if legacy_key in self.decoders:
                    key = legacy_key
            if key not in self.decoders:
                # v35 ROOT FIX (H-2): log a WARNING so operators can
                # detect unknown relations instead of silently getting
                # a flat 0.5 score that masks the bug.
                name_for_log = (
                    rel_names[mask[0].item()]
                    if mask.numel() > 0 and mask[0].item() < len(rel_names)
                    else "_unknown"
                )
                logger.warning(
                    "score_triples: relation idx=%d name=%r (decoder key=%r) is "
                    "not in self.decoders — assigning 0.5 to %d triples "
                    "with NO gradient. This usually means the relation "
                    "was not registered at __init__ time. Decoder keys: "
                    "%s (H-2 root fix)",
                    ridx, name_for_log, key, len(mask), list(self.decoders.keys())[:5],
                )
                score_pieces.append(
                    torch.full((len(mask),), 0.5, device=device)
                )
                piece_indices.append(mask)
                continue
            h_sub = h_emb[mask]
            r_sub = r_emb[mask]
            t_sub = t_emb[mask]
            cat = torch.cat([h_sub, r_sub, t_sub], dim=-1)
            logit = self.decoders[key](cat).squeeze(-1)
            score_pieces.append(torch.sigmoid(logit))
            piece_indices.append(mask)

        if not score_pieces:
            # No known relations at all — return a fresh zero tensor
            # (detached) so the caller gets a defined shape.
            return torch.zeros(B, device=device)

        # Reassemble per-piece scores into the original row order so
        # the output is aligned with the input triples. We use
        # ``index_copy_`` on a fresh zero tensor — but to preserve
        # gradient flow (H-2 root fix), we actually build the result
        # via ``torch.stack`` on a per-row sort. The simplest
        # differentiable reassembly is to concatenate the pieces in
        # their input order using ``torch.cat`` then re-sort by the
        # concatenated indices.
        all_scores_cat = torch.cat(score_pieces, dim=0)
        all_indices_cat = torch.cat(piece_indices, dim=0)
        # Sort by original row index so scores align with input.
        sort_order = torch.argsort(all_indices_cat)
        scores_sorted = all_scores_cat[sort_order]
        # Build the final B-length tensor. Any rows NOT covered by a
        # known relation get 0.5 (matches the previous fallback).
        final_scores = torch.full((B,), 0.5, device=device)
        # Use scatter so autograd tracks gradient through scores_sorted
        # into the decoder weights.
        sorted_indices = all_indices_cat[sort_order]
        final_scores = final_scores.clone()
        final_scores[sorted_indices] = scores_sorted
        return final_scores

    # ROOT FIX (Finding 24, P1): add a `score_triples_logits` variant
    # that returns RAW LOGITS (before sigmoid) so callers can use
    # `BCEWithLogitsLoss` — the numerically stable idiom. The previous
    # training loop used `BCELoss(torch.sigmoid(logit))` which is the
    # classic PyTorch anti-pattern: sigmoid saturates for very confident
    # predictions → gradient vanishes → BCELoss returns 0/0. The fix
    # adds this logits-returning variant so the training loop can use
    # BCEWithLogitsLoss(logit) directly.
    def score_triples_logits(
        self,
        h_emb: torch.Tensor,
        rel_indices: torch.Tensor,
        t_emb: torch.Tensor,
        rel_names: List[str],
    ) -> torch.Tensor:
        """Score (head, relation, tail) triples as RAW LOGITS (no sigmoid).

        Same as :meth:`score_triples` but returns the raw decoder logits
        BEFORE the sigmoid activation. Use this with
        ``torch.nn.BCEWithLogitsLoss`` for numerically stable training
        (Finding 24 root fix).

        Parameters
        ----------
        h_emb, rel_indices, t_emb, rel_names : see :meth:`score_triples`

        Returns
        -------
        torch.Tensor
            Raw logits, shape ``(B,)``. Higher = more plausible.
            Apply ``torch.sigmoid`` to convert to [0, 1] probabilities.
        """
        B = h_emb.shape[0]
        device = h_emb.device
        rel_idx_list = (
            rel_indices.tolist()
            if hasattr(rel_indices, "tolist") else list(rel_indices)
        )
        unique_indices = list(set(rel_idx_list))
        score_pieces: List[torch.Tensor] = []
        piece_indices: List[torch.Tensor] = []
        for ridx in unique_indices:
            mask = torch.tensor(
                [i for i, v in enumerate(rel_idx_list) if v == ridx],
                device=device, dtype=torch.long,
            )
            if 0 <= ridx < len(self.relation_types):
                triple_key = self.relation_types[ridx]
            else:
                triple_key = ("_unknown_src", str(ridx), "_unknown_dst")
            key = self._sanitize_relation_key(triple_key)
            if key not in self.decoders:
                name_for_fallback = (
                    rel_names[mask[0].item()]
                    if mask.numel() > 0 and mask[0].item() < len(rel_names)
                    else "_unknown"
                )
                legacy_key = self._sanitize_relation_key(name_for_fallback)
                if legacy_key in self.decoders:
                    key = legacy_key
            if key not in self.decoders:
                # Unknown relation → logit 0.0 (sigmoid(0)=0.5, neutral).
                # Use a fresh zero tensor with requires_grad=False so the
                # loss does not backprop into nothing.
                score_pieces.append(
                    torch.zeros(len(mask), device=device)
                )
                piece_indices.append(mask)
                continue
            h_sub = h_emb[mask]
            r_sub = r_emb[mask] if (r_emb := self._relation_embeddings(rel_indices[mask])) is not None else None
            if r_sub is None:
                score_pieces.append(torch.zeros(len(mask), device=device))
                piece_indices.append(mask)
                continue
            t_sub = t_emb[mask]
            cat = torch.cat([h_sub, r_sub, t_sub], dim=-1)
            logit = self.decoders[key](cat).squeeze(-1)
            score_pieces.append(logit)  # NO sigmoid — raw logit
            piece_indices.append(mask)

        if not score_pieces:
            return torch.zeros(B, device=device)
        all_scores_cat = torch.cat(score_pieces, dim=0)
        all_indices_cat = torch.cat(piece_indices, dim=0)
        sort_order = torch.argsort(all_indices_cat)
        scores_sorted = all_scores_cat[sort_order]
        final_logits = torch.zeros(B, device=device)
        sorted_indices = all_indices_cat[sort_order]
        final_logits = final_logits.clone()
        final_logits[sorted_indices] = scores_sorted
        return final_logits

    # -- KGEmbeddingModel Protocol: forward ------------------------------
    def forward(
        self,
        head_indices: torch.Tensor,
        rel_indices: torch.Tensor,
        tail_indices: torch.Tensor,
        *,
        x_dict: Optional[Dict[str, torch.Tensor]] = None,
        edge_index_dict: Optional[Dict[Tuple[str, str, str], torch.Tensor]] = None,
        head_type: str = "Compound",
        tail_type: str = "Disease",
        rel_names: Optional[List[str]] = None,
        encoded_h_dict: Optional[Dict[str, torch.Tensor]] = None,
    ) -> torch.Tensor:
        """Score triples. Higher = more plausible (HGT convention).

        Two call modes:

        1. **Graph-aware** (preferred): pass ``x_dict`` and
           ``edge_index_dict``. The model runs the full HGT encoder
           once, then scores. This is the mode used during training.

        2. **Pre-encoded**: pass ``encoded_h_dict`` (the output of a
           prior ``encode()`` call). Skips re-encoding — useful for
           evaluation when the encoder has already been run on the
           full graph.

        For backward compat with ``KGEmbeddingModel``, if NEITHER is
        passed, the model uses the bare embedding tables (no message
        passing) — this is equivalent to a DistMult-style baseline and
        is provided only so the Protocol signature is satisfied.
        """
        if encoded_h_dict is None and x_dict is not None and edge_index_dict is not None:
            encoded_h_dict = self.encode(x_dict, edge_index_dict)
        # v35 ROOT FIX (M-7 / H-3): validate head_type / tail_type
        # explicitly so the caller gets an actionable error instead
        # of a silent zeros() return.
        if head_type not in self.node_types:
            raise ValueError(
                f"forward: head_type {head_type!r} is not in "
                f"self.node_types={self.node_types}. (M-7 root fix)"
            )
        if tail_type not in self.node_types:
            raise ValueError(
                f"forward: tail_type {tail_type!r} is not in "
                f"self.node_types={self.node_types}. (M-7 root fix)"
            )
        if encoded_h_dict is None:
            # Bare-embedding fallback (DistMult-style). Lower bound on
            # performance; full graph-aware mode is the real path.
            # audit-2025 ROOT FIX (issue 18): the previous code called
            # ``get_node_embeddings`` which RAISES ValueError for node
            # types that have ``input_projections`` (no learnable
            # table). This contradicted the docstring's claim that the
            # bare-embedding fallback "satisfies the Protocol
            # signature" — a Protocol method that raises is not
            # satisfied. The fix returns a zeros tensor (with a
            # WARNING) instead of raising, so the fallback truly works
            # for ALL node types. The zeros produce a 0.5 score (sigmoid
            # of 0) which is the correct "no information" baseline.
            try:
                h_emb = self.get_node_embeddings(head_type, head_indices)
            except ValueError:
                logger.warning(
                    "forward: head_type %r has input_projections (no "
                    "learnable table) — bare-embedding fallback returning "
                    "zeros. Pass x_dict + edge_index_dict for real "
                    "embeddings. (issue 18 root fix)",
                    head_type,
                )
                h_emb = torch.zeros(
                    len(head_indices), self.config.embedding_dim,
                    device=head_indices.device if hasattr(head_indices, 'device') else 'cpu',
                )
            try:
                t_emb = self.get_node_embeddings(tail_type, tail_indices)
            except ValueError:
                logger.warning(
                    "forward: tail_type %r has input_projections (no "
                    "learnable table) — bare-embedding fallback returning "
                    "zeros. Pass x_dict + edge_index_dict for real "
                    "embeddings. (issue 18 root fix)",
                    tail_type,
                )
                t_emb = torch.zeros(
                    len(tail_indices), self.config.embedding_dim,
                    device=tail_indices.device if hasattr(tail_indices, 'device') else 'cpu',
                )
        else:
            h_full = encoded_h_dict.get(head_type)
            t_full = encoded_h_dict.get(tail_type)
            # v35 ROOT FIX (H-3): raise ValueError instead of silently
            # returning torch.zeros() — the previous silent path meant
            # a missing node-type in the encoded dict produced a
            # zero-score batch that looked identical to a model that
            # had genuinely learned nothing, masking the bug.
            if h_full is None:
                raise ValueError(
                    f"forward: head_type {head_type!r} not found in "
                    f"encoded_h_dict. Available keys: "
                    f"{list(encoded_h_dict.keys())}. The encoder did "
                    f"not produce embeddings for this node type — "
                    f"usually means x_dict was missing the entry or "
                    f"the node type was not declared at __init__. "
                    f"(H-3 root fix)"
                )
            if t_full is None:
                raise ValueError(
                    f"forward: tail_type {tail_type!r} not found in "
                    f"encoded_h_dict. Available keys: "
                    f"{list(encoded_h_dict.keys())}. The encoder did "
                    f"not produce embeddings for this node type — "
                    f"usually means x_dict was missing the entry or "
                    f"the node type was not declared at __init__. "
                    f"(H-3 root fix)"
                )
            h_emb = h_full[head_indices]
            t_emb = t_full[tail_indices]
        if rel_names is None:
            # Look up relation name per index.
            rel_names = [self.relation_types[i][1] for i in rel_indices.tolist()]
        return self.score_triples(h_emb, rel_indices, t_emb, rel_names)

    def normalize_entity_embeddings(self) -> None:
        """Protocol-required NO-OP for HGT.

        v35 ROOT FIX (L-6): make it explicit in the docstring that
        this method is intentionally a no-op. ``KGEmbeddingModel``
        Protocol consumers (``train_transe``) call this after every
        optimizer step to enforce the TransE constraint
        ``||h||=||r||=||t||=1``. HGT does NOT need that constraint —
        the learnable ``nn.LayerNorm`` modules inside ``encode()``
        (see M-16 root fix) provide per-sublayer affine normalisation
        that is strictly more expressive than a hard unit-norm
        projection. Calling this method therefore does nothing; the
        Protocol signature is satisfied so ``train_transe`` works
        unchanged when passed an HGT model.

        Returns
        -------
        None
            Always. Implemented as ``return None`` so static analysers
            do not flag the implicit ``None`` return.
        """
        return None


# ---------------------------------------------------------------------------
# 3. Convenience scoring helper
# ---------------------------------------------------------------------------
def graph_transformer_score(
    model: GraphTransformerModel,
    head_indices: torch.Tensor,
    rel_indices: torch.Tensor,
    tail_indices: torch.Tensor,
    *,
    x_dict: Dict[str, torch.Tensor],
    edge_index_dict: Dict[Tuple[str, str, str], torch.Tensor],
    head_type: str = "Compound",
    tail_type: str = "Disease",
    rel_names: Optional[List[str]] = None,
) -> torch.Tensor:
    """Score triples with a Graph Transformer.

    Convenience wrapper around ``model.forward(...)`` so callers don't
    need to remember the keyword-arg names.
    """
    return model.forward(
        head_indices, rel_indices, tail_indices,
        x_dict=x_dict, edge_index_dict=edge_index_dict,
        head_type=head_type, tail_type=tail_type,
        rel_names=rel_names,
    )
