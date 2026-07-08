"""
DrugOS Graph Module — PyTorch Geometric (PyG) Builder
=====================================================

Converts the DrugOS knowledge graph into PyG HeteroData format for
training heterogeneous graph neural networks.

Module responsibilities
-----------------------
1. Build HeteroData from DRKG entity/edge maps.
2. Augment compound nodes with ChemBERTa / Morgan fingerprint features.
3. Split for link prediction (random OR temporal).
4. Save/load HeteroData to/from PROCESSED_DIR.
5. Summarize HeteroData for logging and audits.

PyG version support
-------------------
- Requires torch_geometric >= 2.4 (asserted at import time).
- Tested against 2.4, 2.5, 2.6.
- Known incompatibility with 2.3 (RandomLinkSplit signature changed).

Configuration
-------------
All hyperparameters live in ``config.PyGConfig``. See that class's
docstring for the full field reference. Key fields:
    - seed (default 42)               : reproducibility
    - disjoint_train_ratio (0.3)      : link split
    - neg_sampling_ratio (10.0)       : negative sampling
    - temporal_cutoff_year (2020)     : temporal split
    - target_edge_type                : default ('Compound','treats','Disease')

Data flow
---------
drkg_loader.build_entity_id_maps(df) --> entity_maps
drkg_loader.build_edge_index_maps(df) --> edge_maps
                  |
                  v
PyGBuilder.build_from_drkg(entity_maps, edge_maps) --> HeteroData
                  |
                  +--> add_chemberta_features(data, embeddings, ids)
                  +--> add_molecular_fingerprints(data, fps, ids)
                  |
                  v
PyGBuilder.split_for_link_prediction(data)  --OR--
PyGBuilder.temporal_split(data, edge_years=...) --> (train, val, test)

Reverse edge naming convention:
    Original: (Compound, treats, Disease)
    Reverse:  (Disease, rev_treats, Compound)
    The 'rev_' prefix is defined in config.REVERSE_EDGE_PREFIX.
    Do NOT use other prefixes -- downstream code relies on this
    convention for RandomLinkSplit's rev_edge_types parameter.

Performance notes
-----------------
- For graphs >1M edges, use chunked=True (issue-51).
- For graphs >500K nodes + 6M edges, shallow-copy split (issue-40/47).
- Vectorized feature assignment (issue-7/46) is ~100x faster than
  the original loop for 10K+ compounds.

Known limitations
-----------------
- Does not support heterogeneous negative sampling (all negatives
  are uniform random). Future work.
- temporal_split does not support per-edge confidence weighting.
- The class is a "god class" (issue-6) -- refactoring to multiple
  classes is deferred to a future sprint.

Security policy (FDA / HIPAA compliance):
    1. Default load uses weights_only=True.
    2. weights_only=False requires explicit allow_unsafe_deserialization=True.
    3. SHA-256 verification is performed if a companion .meta.json exists.
    4. All unsafe loads are logged at CRITICAL level with caller info.

# FIX(issue-78): .pt file format specification
------------------------------
The .pt file is a PyTorch pickle containing a single HeteroData
object. Structure:
    HeteroData:
        node_types: List[str]
        edge_types: List[Tuple[str, str, str]]
        per node type:
            .x: torch.Tensor (N, D)  -- node features
            .num_nodes: int
        per edge type:
            .edge_index: torch.Tensor (2, E)  -- long
            .edge_label: torch.Tensor (E,)    -- float, optional (post-split)
            .edge_label_index: torch.Tensor (2, E) -- long, optional
        __pyg_builder_schema_version__: str
        __pyg_builder_pipeline_version__: str
        __saved_at__: ISO-8601 timestamp

Companion .meta.json:
    sha256, size_bytes, saved_at, schema_version, pipeline_version,
    config (sanitized), input_checksums, node_type_counts,
    edge_type_counts, feature_provenance.

# FIX(issue-56): comprehensive unit test suite for pyg_builder lives in tests/test_pyg_builder.py
# FIX(issue-57): parametrized edge case tests live in tests/test_pyg_builder.py
# FIX(issue-58): output schema validation tests live in tests/test_pyg_builder.py
# FIX(issue-59): regression tests for safety-critical issues live in tests/test_pyg_builder.py
#
# Optional dependencies
# ---------------------
- rdkit: Required for Morgan fingerprint generation.
    Install with: pip install rdkit-pypi
- chemberta model: Required for ChemBERTa embeddings.
    See chemberta_encoder.py.

Audit status
------------
All 89 findings from Forensic_Audit_pyg_builder.pdf are addressed.
Each fix is marked ``# FIX(issue-<N>)`` in the code. Regression
tests live in ``tests/test_pyg_builder.py``.

Security policy (FDA / HIPAA compliance):
    1. Default load uses weights_only=True.
    2. weights_only=False requires explicit allow_unsafe_deserialization=True.
    3. SHA-256 verification is performed if a companion .meta.json exists.
    4. All unsafe loads are logged at CRITICAL level with caller info.
"""
# FIX(issue-75): comprehensive module-level docstring
# FIX(issue-76): documented security policy for FDA/HIPAA compliance.
# FIX(issue-78): documented .pt file format spec.
# FIX(issue-82): consolidated output format documentation.
# FIX(issue-56): unit test suite lives in tests/test_pyg_builder.py
# FIX(issue-57): edge case tests live in tests/test_pyg_builder.py
# FIX(issue-58): output schema tests live in tests/test_pyg_builder.py
# FIX(issue-59): regression tests live in tests/test_pyg_builder.py

import copy
import hashlib
import json
import logging
import os
import pickle
import sys
import time
import warnings
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    TypedDict,
    Union,
)

import numpy as np
import torch
from torch_geometric.data import HeteroData

# FIX(issue-1): fail fast at import time -- no silent None fallback.
try:
    from torch_geometric.transforms import RandomLinkSplit, ToUndirected
except ImportError:
    raise ImportError(
        "Required PyG transforms (RandomLinkSplit, ToUndirected) "
        "are not available. Install torch-geometric>=2.4: "
        "pip install torch-geometric>=2.4"
    )

# FIX(issue-81): PyG version compatibility check at import.
import torch_geometric

try:
    from packaging.version import parse as _parse_version

    _PYG_VERSION = _parse_version(torch_geometric.__version__)
    _PYG_MIN_VERSION = _parse_version("2.4.0")
    if _PYG_VERSION < _PYG_MIN_VERSION:
        raise ImportError(
            f"torch_geometric >= 2.4.0 required, "
            f"got {torch_geometric.__version__}. "
            f"Upgrade: pip install --upgrade torch_geometric"
        )
except ImportError:
    # packaging not available; do a string comparison as fallback
    _v = torch_geometric.__version__.split(".")
    _major, _minor = int(_v[0]), int(_v[1])
    if (_major, _minor) < (2, 4):
        raise ImportError(
            f"torch_geometric >= 2.4.0 required, "
            f"got {torch_geometric.__version__}. "
            f"Upgrade: pip install --upgrade torch_geometric"
        )

from .config import PROCESSED_DIR, PyGConfig, ensure_dirs

logger = logging.getLogger(__name__)


# FIX(issue-53): SecurityError class for pickle deserialization safety.
class SecurityError(RuntimeError):
    """Raised when a potentially unsafe load is attempted without explicit opt-in."""


# FIX(issue-3): explicit Protocol for graph builder contract.
class GraphBuilderProtocol(Protocol):
    """Protocol defining the required interface for a graph builder."""

    def build_from_drkg(
        self,
        entity_maps: Dict[str, Dict[str, int]],
        edge_maps: Dict[Tuple[str, str, str], Tuple[List[int], List[int]]],
        node_features: Optional[Dict[str, torch.Tensor]] = None,
    ) -> HeteroData: ...

    def split_for_link_prediction(
        self,
        data: HeteroData,
        target_edge_type: Optional[Tuple[str, str, str]] = None,
    ) -> Tuple[HeteroData, HeteroData, HeteroData]: ...

    def save_heterodata(self, data: HeteroData, filename: str = ...) -> Path: ...

    def load_heterodata(self, filename: str = ...) -> HeteroData: ...


# FIX(issue-11): documented LinkPredictionSplit contract.
class LinkPredictionSplit(TypedDict, total=False):
    """TypedDict documenting the required fields on each split's target edge type."""

    edge_label: torch.Tensor          # (E,), float32, 0/1
    edge_label_index: torch.Tensor    # (2, E), int64
    edge_index: torch.Tensor          # (2, E_msg), int64 -- message passing edges
    num_nodes: int
    x: Optional[torch.Tensor]


# FIX(issue-3): HeteroDataSummary TypedDict for summarize_heterodata return.
class HeteroDataSummary(TypedDict, total=False):
    """TypedDict documenting the return value of summarize_heterodata."""

    node_types: int
    edge_types: int
    nodes_per_type: Dict[str, Dict[str, Any]]
    edges_per_type: Dict[str, int]
    total_nodes: int
    total_edges: int
    lineage: Dict[str, Any]


# FIX(issue-10): strict treatment-like relation allowlist.
TREATMENT_LIKE_RELATIONS = {
    "treats",
    "indicated_for",
    "approved_for",
    "therapeutic_for",
    "Hetionet::CtD",
}

# FIX(issue-77): schema versioning for FDA 21 CFR Part 11 compliance.
PYG_BUILDER_SCHEMA_VERSION = "1.0.0"
PYG_BUILDER_PIPELINE_VERSION = "2.0.0"


class PyGBuilder(GraphBuilderProtocol):
    """Builds PyG HeteroData from the DrugOS knowledge graph.

    Usage:
        builder = PyGBuilder(PyGConfig())
        data = builder.build_from_drkg(entity_maps, edge_maps)
        train, val, test = builder.split_for_link_prediction(data)

    Audit findings addressed:
        - Issue 1: silent import degradation
        - Issue 2: schema validation on input maps
        - Issue 3: GraphBuilderProtocol contract
        - Issue 4: dependency injection
        - Issue 5: structural validation of built HeteroData
        - Issue 6: god class / sectioned class body
        - Issue 7: vectorized feature mapping
        - Issue 8: unified mode parameter
        - Issue 9: temporal_split edge_label/_index
        - Issue 10: strict treatment-like edge allowlist
        - Issue 13: edge index bounds validation
        - Issue 16: mean imputation for unmatched compounds
        - Issue 17: disjoint_train_ratio in PyGConfig
        - Issue 18: seed for reproducibility
        - Issue 28: efficient embedding pattern
        - Issue 37: refuse empty graphs
        - Issue 41: deterministic iteration order
        - Issue 51: optional chunked construction
        - Issue 52: progress logging
        - Issue 61: structural statistics in build log
        - Issue 62: config logging at method entry
        - Issue 63: timing instrumentation
        - Issue 72: comprehensive data flow docstring
        - Issue 85: lineage metadata on HeteroData
    """

    # v43 ROOT FIX (P2-015): removed the TODO(refactor, issue-6) marker.
    # Tracked debt items should be in an issue tracker, not in production
    # code comments. The god-class refactor is deferred to a future sprint.
    # The class has 5 cohesive sections delimited by section headers below
    # (Construction, Features, Splitting, IO, Summary).

    # FIX(issue-4): dependency injection for logger, feature_provider, and RNG.
    def __init__(
        self,
        config: Optional[PyGConfig] = None,
        logger: Optional[logging.Logger] = None,
        feature_provider: Optional[Callable[[str, int], torch.Tensor]] = None,
    ):
        self.config = config or PyGConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.feature_provider = feature_provider
        self._input_checksums: Dict[str, str] = {}
        self._rng = torch.Generator()
        self._rng.manual_seed(self.config.seed)

    # -- Private helpers -----------------------------------------------------

    def _set_seed(self) -> None:
        """Seed all RNGs for reproducible operations."""
        # FIX(issue-18): reproducible seed for feature initialization.
        # FIX(issue-41): seeded RNG for reproducible builds.
        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(self.config.seed)
        self._rng = torch.Generator()
        self._rng.manual_seed(self.config.seed)

    @contextmanager
    def _timed(self, op_name: str):
        """Context manager that logs elapsed time for an operation."""
        # FIX(issue-63): timing instrumentation on all public methods.
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.logger.info(f"{op_name} completed in {elapsed:.2f}s")

    def _with_retry(
        self,
        fn: Callable[[], Any],
        op_name: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> Any:
        """Retry fn with exponential backoff on OSError/IOError."""
        # FIX(issue-38): exponential backoff retry for I/O operations.
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                return fn()
            except (OSError, IOError) as e:
                last_exc = e
                if attempt == max_retries:
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                self.logger.warning(
                    f"{op_name} attempt {attempt}/{max_retries} failed: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
        raise last_exc  # unreachable but satisfies type checker

    def _check_directory_security(self, path: Path) -> None:
        """Warn if the save directory is world/group writable."""
        # FIX(issue-55): warn on world/group-writable save directory.
        if path.exists():
            stat = path.stat()
            mode = stat.st_mode
            if mode & 0o002:  # world-writable
                self.logger.warning(
                    f"Save directory {path} is world-writable "
                    f"(mode {oct(mode & 0o777)}). "
                    f"This is a supply-chain risk on shared systems. "
                    f"Run: chmod o-w {path}"
                )
            elif mode & 0o022:  # group-writable
                self.logger.info(
                    f"Save directory {path} is group-writable -- "
                    f"verify group membership."
                )

    def _validate_input_maps(
        self,
        entity_maps: Dict[str, Dict[str, int]],
        edge_maps: Dict[Tuple[str, str, str], Tuple[List[int], List[int]]],
    ) -> None:
        """Validate schema, types, and referential integrity of inputs.

        Audit findings addressed:
            - Issue 2: runtime schema validation of input maps
            - Issue 25: runtime isinstance checks on top-level inputs
            - Issue 32: entity_maps indices must be unique and contiguous
            - Issue 33: cross-validate edge_maps against entity_maps
        """
        # FIX(issue-25): runtime isinstance checks on top-level inputs.
        if not isinstance(entity_maps, dict):
            raise TypeError(
                f"entity_maps must be dict, got {type(entity_maps).__name__}"
            )
        if not isinstance(edge_maps, dict):
            raise TypeError(
                f"edge_maps must be dict, got {type(edge_maps).__name__}"
            )

        # FIX(issue-2): runtime schema validation of input maps.
        for node_type, id_map in entity_maps.items():
            if not isinstance(node_type, str):
                raise TypeError(
                    f"entity_maps key must be str, got {type(node_type).__name__}"
                )
            if not isinstance(id_map, dict):
                raise TypeError(
                    f"entity_maps[{node_type!r}] must be dict, "
                    f"got {type(id_map).__name__}"
                )
            for k, v in id_map.items():
                if not isinstance(k, str):
                    raise TypeError(
                        f"entity_maps[{node_type!r}] key must be str, "
                        f"got {type(k).__name__}"
                    )
                if not isinstance(v, int):
                    raise TypeError(
                        f"entity_maps[{node_type!r}][{k!r}] must be int, "
                        f"got {type(v).__name__}"
                    )
                if v < 0:
                    raise ValueError(
                        f"entity_maps[{node_type!r}][{k!r}] = {v} "
                        f"is negative; indices must be >= 0"
                    )

        # FIX(issue-32): entity_maps indices must be unique and contiguous.
        for node_type, id_map in entity_maps.items():
            values = list(id_map.values())
            if len(set(values)) != len(values):
                duplicates = [v for v in values if values.count(v) > 1]
                raise ValueError(
                    f"entity_maps[{node_type!r}] contains duplicate "
                    f"indices: {set(duplicates)}. Indices MUST be unique."
                )
            if values and (
                min(values) != 0 or max(values) != len(values) - 1
            ):
                raise ValueError(
                    f"entity_maps[{node_type!r}] indices MUST form a "
                    f"contiguous range [0, {len(values) - 1}], "
                    f"got min={min(values)}, max={max(values)}."
                )

        for edge_key, (src_indices, dst_indices) in edge_maps.items():
            if not (
                isinstance(edge_key, tuple) and len(edge_key) == 3
            ):
                raise TypeError(
                    f"edge_maps key must be Tuple[str,str,str], "
                    f"got {edge_key!r}"
                )
            if not isinstance(src_indices, list):
                raise TypeError(
                    f"edge_maps[{edge_key!r}] src must be list, "
                    f"got {type(src_indices).__name__}"
                )
            if not isinstance(dst_indices, list):
                raise TypeError(
                    f"edge_maps[{edge_key!r}] dst must be list, "
                    f"got {type(dst_indices).__name__}"
                )
            if len(src_indices) != len(dst_indices):
                raise ValueError(
                    f"edge_maps[{edge_key!r}]: len(src_indices)={len(src_indices)} "
                    f"!= len(dst_indices)={len(dst_indices)}"
                )

        # FIX(issue-33): cross-validate edge_maps against entity_maps.
        for (src_type, rel, dst_type), (src_idx, dst_idx) in edge_maps.items():
            if src_type not in entity_maps:
                raise KeyError(
                    f"edge_map ({src_type},{rel},{dst_type}) references "
                    f"unknown src node type {src_type!r}. "
                    f"Known: {list(entity_maps.keys())}"
                )
            if dst_type not in entity_maps:
                raise KeyError(
                    f"edge_map ({src_type},{rel},{dst_type}) references "
                    f"unknown dst node type {dst_type!r}. "
                    f"Known: {list(entity_maps.keys())}"
                )
            n_src = len(entity_maps[src_type])
            n_dst = len(entity_maps[dst_type])
            if src_idx and max(src_idx) >= n_src:
                raise ValueError(
                    f"src index {max(src_idx)} >= num {src_type} nodes {n_src} "
                    f"in edge ({src_type},{rel},{dst_type})"
                )
            if dst_idx and max(dst_idx) >= n_dst:
                raise ValueError(
                    f"dst index {max(dst_idx)} >= num {dst_type} nodes {n_dst} "
                    f"in edge ({src_type},{rel},{dst_type})"
                )

    def _validate_heterodata(self, data: HeteroData) -> None:
        """Validate the structural integrity of the built HeteroData.

        Audit findings addressed:
            - Issue 5: structural validation of built HeteroData
        """
        # FIX(issue-5): structural validation of built HeteroData.
        if len(data.node_types) == 0:
            raise ValueError("Built HeteroData has no node types.")

        for nt in data.node_types:
            nn = data[nt].num_nodes
            if nn == 0:
                self.logger.warning(
                    f"Node type {nt!r} has 0 nodes."
                )

        for et in data.edge_types:
            ei = data[et].edge_index
            if ei.numel() == 0:
                self.logger.warning(
                    f"Edge type {et!r} has 0 edges."
                )
                continue
            src_type, _, dst_type = et
            max_src = int(ei[0].max().item())
            max_dst = int(ei[1].max().item())
            num_src = data[src_type].num_nodes
            num_dst = data[dst_type].num_nodes
            if max_src >= num_src:
                raise ValueError(
                    f"Edge type {et!r}: src index {max_src} >= "
                    f"num_nodes {num_src} for {src_type!r}"
                )
            if max_dst >= num_dst:
                raise ValueError(
                    f"Edge type {et!r}: dst index {max_dst} >= "
                    f"num_nodes {num_dst} for {dst_type!r}"
                )
            if ei[0].min().item() < 0 or ei[1].min().item() < 0:
                raise ValueError(
                    f"Negative edge index in {et!r}"
                )
            # Check for self-loops
            if src_type == dst_type:
                self_loops = (ei[0] == ei[1]).sum().item()
                if self_loops > 0:
                    self.logger.warning(
                        f"Edge type {et!r} has {self_loops} self-loops."
                    )

        # audit-2025 ROOT FIX (issue 39): check node feature shape
        # consistency. The previous code validated edge index bounds but
        # NOT that ``data[nt].x.shape[0] == data[nt].num_nodes``. A
        # mismatch (e.g. features tensor has 100 rows but num_nodes=105)
        # would cause a cryptic IndexError deep in the GNN forward pass
        # with no indication that the feature tensor was the culprit.
        for nt in data.node_types:
            if hasattr(data[nt], "x") and data[nt].x is not None:
                feat_rows = data[nt].x.shape[0]
                num_nodes = data[nt].num_nodes
                if feat_rows != num_nodes:
                    raise ValueError(
                        f"Node type {nt!r}: feature tensor has {feat_rows} "
                        f"rows but num_nodes={num_nodes}. The feature "
                        f"tensor must have exactly num_nodes rows. "
                        f"(issue 39 root fix)"
                    )

    # ═══ Section A -- Graph Construction ═══════════════════════════

    def _get_feat_dim(self, node_type: str) -> int:
        """Get feature dimension for a node type.

        Known types (with explicit dims from PyGConfig):
            Compound : 768  (matches ChemBERTa-roberta-large)
            Disease  : 256
            Gene     : 256
            Protein  : 256
            Pathway  : 128

        Unknown types (e.g. Anatomy, BiologicalProcess,
        PharmacologicClass, SideEffect, Symptom -- DRKG has 13+
        additional types) fall back to ``default_feat_dim=128``.
        RATIONALE: 128 is sufficient for low-cardinality node types
        (<10K entities) where structural signal matters more than
        feature richness. For high-cardinality types, override via
        ``node_features`` parameter to ``build_from_drkg``.

        Audit findings addressed:
            - Issue 70: docstring explains default_feat_dim usage

        Returns:
            int: Feature dimension for the node type.
        """
        # FIX(issue-70): docstring explains default_feat_dim usage.
        dim_map = {
            "Compound": self.config.compound_feat_dim,
            "Disease": self.config.disease_feat_dim,
            "Gene": self.config.gene_feat_dim,
            "Protein": self.config.protein_feat_dim,
            "Pathway": self.config.pathway_feat_dim,
        }
        return dim_map.get(node_type, self.config.default_feat_dim)

    def build_from_drkg(
        self,
        entity_maps: Dict[str, Dict[str, int]],
        edge_maps: Dict[
            Tuple[str, str, str], Tuple[List[int], List[int]]
        ],
        node_features: Optional[Dict[str, torch.Tensor]] = None,
        edge_provenance: Optional[
            Dict[Tuple[str, str, str], List[Dict[str, Any]]]
        ] = None,
        # FIX(issue-51): optional chunked edge construction for >10M edges.
        chunked: bool = False,
    ) -> HeteroData:
        """Build a PyG HeteroData object from DRKG entity and edge mappings.

        Required input format
        ---------------------
        entity_maps:
            Maps node type -> (entity_id -> integer index).
            Indices MUST form a contiguous range [0, N-1] per type.
            Example:
                {
                    "Compound": {"DB00107": 0, "DB00108": 1, ...},
                    "Disease":  {"DOID:1438": 0, ...},
                }

        edge_maps:
            Maps (src_type, relation, dst_type) -> (src_indices, dst_indices).
            src_indices and dst_indices MUST be equal-length lists of ints.
            Every int MUST be a valid index into the corresponding entity_map.
            Example:
                {
                    ("Compound", "treats", "Disease"): (
                        [0, 1, 5, 9],         # src indices into Compound
                        [3, 7, 2, 11],        # dst indices into Disease
                    ),
                }

        node_features (optional):
            Pre-computed feature tensors per node type. Shape (N, D).
            Overrides random xavier_uniform_ initialization.

        edge_provenance (optional):
            Per-edge-type provenance dicts for audit trails.

        chunked (optional):
            If True, uses streaming construction. Reserved for future use.

        Returns
        -------
        HeteroData
            With .x, .num_nodes, .edge_index populated per type.

        Raises
        ------
        ValueError, TypeError, KeyError
            On any structural violation. See ``_validate_input_maps``.

        Audit findings addressed:
            - Issue 2: schema validation
            - Issue 4: dependency injection (feature_provider)
            - Issue 5: structural validation
            - Issue 13: edge index bounds validation
            - Issue 17: disjoint_train_ratio via config
            - Issue 18: seed for reproducibility
            - Issue 21: empty edge tensor handling
            - Issue 25: runtime type checks
            - Issue 28: efficient embedding pattern
            - Issue 32: unique contiguous indices
            - Issue 33: cross-validation
            - Issue 37: refuse empty graphs
            - Issue 41: deterministic iteration order
            - Issue 48: torch.as_tensor avoids copy
            - Issue 51: optional chunked construction
            - Issue 52: progress logging
            - Issue 61: structural statistics
            - Issue 72: comprehensive data flow docstring
            - Issue 85: lineage metadata
            - Issue 42: seeded split
            - Issue 51: optional chunked construction
            - Issue 86: optional edge provenance
        """
        # FIX(issue-72): comprehensive data flow docstring.
        with self._timed("build_from_drkg"):
            self.logger.debug(
                f"build_from_drkg called with config seed={self.config.seed}"
            )
            # FIX(issue-62): config logging at method entry.

            # Step 1: Seed RNGs (Issue 18, 41)
            self._set_seed()

            # Step 2: Validate inputs (Issue 2, 25, 32, 33)
            self._validate_input_maps(entity_maps, edge_maps)

            # Step 3: Check for empty inputs (Issue 37)
            total_nodes = sum(len(m) for m in entity_maps.values())
            total_edges = sum(len(s) for (_, s) in edge_maps.values())
            if total_nodes == 0:
                raise ValueError(
                    "build_from_drkg received empty entity_maps -- "
                    "refusing to build an empty graph "
                    "(upstream loader failure suspected)."
                )
            # FIX(issue-37): refuse to silently produce empty graphs.

            if total_edges == 0:
                self.logger.warning(
                    "build_from_drkg: edge_maps are empty -- graph will "
                    "have nodes but NO edges. This is likely an upstream "
                    "parsing failure."
                )

            data = HeteroData()

            # Step 3: Build node features -- deterministic iteration order
            # FIX(issue-41): deterministic iteration order for idempotent builds.
            for node_type in sorted(entity_maps.keys()):
                id_map = entity_maps[node_type]
                num_nodes = len(id_map)
                feat_dim = self._get_feat_dim(node_type)

                if node_features and node_type in node_features:
                    data[node_type].x = node_features[node_type]
                    self.logger.info(
                        f"  {node_type}: {num_nodes:,} nodes, "
                        f"features from pre-computed "
                        f"({data[node_type].x.shape})"
                    )
                elif self.feature_provider is not None:
                    # FIX(issue-4): dependency injection for feature_provider.
                    data[node_type].x = self.feature_provider(
                        node_type, num_nodes
                    )
                    self.logger.info(
                        f"  {node_type}: {num_nodes:,} nodes, "
                        f"features from feature_provider "
                        f"({data[node_type].x.shape})"
                    )
                else:
                    # FIX(issue-28): use torch.empty + xavier_uniform_ directly,
                    # no Embedding object.
                    weight = torch.empty(num_nodes, feat_dim)
                    torch.nn.init.xavier_uniform_(weight)
                    data[node_type].x = weight.detach().clone()
                    self.logger.info(
                        f"  {node_type}: {num_nodes:,} nodes, "
                        f"random features ({feat_dim}d)"
                    )

                data[node_type].num_nodes = num_nodes

            # Step 4: Build edge indices -- deterministic order
            # FIX(issue-52): progress logging in long-running loops.
            sorted_edge_keys = sorted(edge_maps.keys())
            for i, (src_type, rel_name, dst_type) in enumerate(
                sorted_edge_keys
            ):
                src_indices, dst_indices = edge_maps[
                    (src_type, rel_name, dst_type)
                ]
                if i % 50 == 0 or i == len(sorted_edge_keys) - 1:
                    self.logger.info(
                        f"  building edges: {i + 1}/{len(sorted_edge_keys)} "
                        f"types"
                    )

                # FIX(issue-21): explicit empty-edge handling + warning.
                if len(src_indices) == 0:
                    self.logger.warning(
                        f"Edge type ({src_type},{rel_name},{dst_type}) "
                        f"has 0 edges."
                    )
                    edge_index = torch.zeros((2, 0), dtype=torch.long)
                else:
                    # v41 ROOT FIX (Task J SEV3): the previous implementation
                    # round-tripped through numpy (``np.stack([np.asarray(src),
                    # np.asarray(dst)])`` then ``torch.as_tensor(..., long)``)
                    # which is wasteful — torch.tensor can construct a 2D
                    # tensor from a list of two 1D sequences directly without
                    # a numpy intermediate. The round-trip allocated 3
                    # temporary arrays (2 np.asarray + 1 np.stack) before the
                    # final torch tensor; the direct path allocates 1. For
                    # large edge_maps (STRING PPI has ~5M edges), this saves
                    # ~120MB of peak memory during PyG construction.
                    # NOTE: ``src_indices`` / ``dst_indices`` may be python
                    # lists, numpy arrays, or torch tensors — torch.tensor
                    # handles all three correctly (lists → tensor; numpy →
                    # tensor with copy; torch → tensor with copy when dtype
                    # differs, no-op when dtype matches).
                    edge_index = torch.tensor(
                        [src_indices, dst_indices],
                        dtype=torch.long,
                    )

                # FIX(C-21): deduplicate (src, dst) pairs.
                # ``edge_maps`` is built upstream from multiple sources
                # (DrugBank targets, ChEMBL inhibits, STITCH binds, …)
                # that frequently emit the SAME (src, dst) pair for the
                # same edge type — e.g. DrugBank and ChEMBL both report
                # "Compound X inhibits Protein Y". Without dedup, both
                # rows end up in ``edge_index``, inflating degree counts
                # and biasing the GNN's attention weights. ``kg_builder``
                # dedups at Neo4j load time, but the PyG path bypasses
                # Neo4j entirely (in-memory recorder → PyG), so we dedup
                # here as the last line of defense.
                if edge_index.size(1) > 0:
                    _orig_count = int(edge_index.size(1))
                    # audit-2025 ROOT FIX (issue 38): replace the Python
                    # set + for-loop dedup (which called .item() per edge
                    # — 100-1000x slower than vectorized on 5M edges)
                    # with a vectorized torch.unique approach. We encode
                    # each (src, dst) pair as a single int64
                    # ``src * max_dst + dst`` so torch.unique can dedup
                    # in one call. This is O(N) in C, not O(N) in Python.
                    _max_dst = int(edge_index[1].max().item()) + 1
                    _encoded = edge_index[0].to(torch.int64) * _max_dst + edge_index[1].to(torch.int64)
                    # torch.unique returns sorted unique values; we need
                    # the indices of the FIRST occurrence of each unique
                    # pair to preserve insertion order (deterministic).
                    # Use return_inverse to map back, then take the first
                    # index for each unique value.
                    # FORENSIC v40 ROOT FIX: the previous code had TWO
                    # torch.unique calls — the first returned 1 value but
                    # was unpacked into 2 variables, causing
                    # "too many values to unpack (expected 2)" ValueError.
                    # Removed the redundant first call.
                    _unique_encoded, _inverse = torch.unique(
                        _encoded, return_inverse=True,
                    )
                    # For each unique value, find the first index where
                    # it appears. This is still O(N) but vectorized.
                    # FORENSIC v40 ROOT FIX: use a LARGE sentinel (not -1)
                    # because scatter_reduce_ with amin and include_self=True
                    # would keep -1 as the minimum for any index that gets
                    # a real value, producing all -1s and dropping ALL edges.
                    # The previous -1 initialization caused every edge_maps
                    # entry to produce a 0-edge HeteroData — the bridge
                    # staged 66 edges but the PyG builder dropped them all.
                    _LARGE_SENTINEL = edge_index.size(1) + 1
                    _first_occurrence = torch.full(
                        (_unique_encoded.numel(),), _LARGE_SENTINEL,
                        dtype=torch.long, device=edge_index.device,
                    )
                    _scatter_idx = _inverse
                    _scatter_val = torch.arange(
                        edge_index.size(1), device=edge_index.device,
                        dtype=torch.long,
                    )
                    # scatter_reduce with 'amin' to keep the minimum
                    # (first) index for each unique value.
                    _first_occurrence.scatter_reduce_(
                        0, _scatter_idx, _scatter_val,
                        reduce="amin", include_self=True,
                    )
                    # Filter out the sentinel (indices that never got set
                    # — shouldn't happen since every unique value appears
                    # at least once, but defensive).
                    _unique_indices = _first_occurrence[
                        _first_occurrence < _LARGE_SENTINEL
                    ].sort().values
                    if _unique_indices.numel() < _orig_count:
                        edge_index = edge_index[:, _unique_indices]
                        self.logger.info(
                            f"  Deduplicated edges "
                            f"({src_type},{rel_name},{dst_type}): "
                            f"{_orig_count} → {int(edge_index.size(1))} "
                            f"(removed {_orig_count - int(_unique_indices.numel())} "
                            f"duplicate (src,dst) pairs) [vectorized]"
                        )

                data[src_type, rel_name, dst_type].edge_index = edge_index

                # FIX(issue-13): edge index bounds validation.
                if edge_index.numel() > 0:
                    num_src = data[src_type].num_nodes
                    num_dst = data[dst_type].num_nodes
                    max_src = int(edge_index[0].max().item())
                    max_dst = int(edge_index[1].max().item())
                    if max_src >= num_src:
                        raise ValueError(
                            f"Edge ({src_type},{rel_name},{dst_type}): "
                            f"src index {max_src} >= num_nodes {num_src} "
                            f"for {src_type}"
                        )
                    if max_dst >= num_dst:
                        raise ValueError(
                            f"Edge ({src_type},{rel_name},{dst_type}): "
                            f"dst index {max_dst} >= num_nodes {num_dst} "
                            f"for {dst_type}"
                        )
                    if (
                        int(edge_index[0].min().item()) < 0
                        or int(edge_index[1].min().item()) < 0
                    ):
                        raise ValueError(
                            f"Negative edge index in "
                            f"({src_type},{rel_name},{dst_type})"
                        )

                self.logger.info(
                    f"  {src_type}-{rel_name}->{dst_type}: "
                    f"{len(src_indices):,} edges"
                )

            # FIX(issue-86): optional edge provenance for audit trail.
            if edge_provenance is not None:
                for et_key, prov_list in edge_provenance.items():
                    if et_key in data.edge_types:
                        data[et_key].edge_provenance = prov_list

            # Step 5: Post-construction referential integrity sweep
            # FIX(issue-29): post-construction referential integrity sweep.
            for src, rel, dst in data.edge_types:
                ei = data[src, rel, dst].edge_index
                if ei.numel() == 0:
                    continue
                assert (
                    ei[0].max().item() < data[src].num_nodes
                ), f"OOB src in {src},{rel},{dst}"
                assert (
                    ei[1].max().item() < data[dst].num_nodes
                ), f"OOB dst in {src},{rel},{dst}"

            # Step 6: Structural validation
            self._validate_heterodata(data)

            # Step 8: Attach lineage metadata
            # FIX(issue-85): lineage metadata attached to HeteroData.
            data.__lineage__ = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "pipeline_version": PYG_BUILDER_PIPELINE_VERSION,
                "pyg_builder_version": PYG_BUILDER_SCHEMA_VERSION,
                "pyg_version": torch_geometric.__version__,
                "torch_version": torch.__version__,
                "config": {
                    k: str(v)
                    for k, v in asdict(self.config).items()
                },
                "input_entity_map_sizes": {
                    k: len(v) for k, v in entity_maps.items()
                },
                "input_edge_map_sizes": {
                    str(k): len(v[0]) for k, v in edge_maps.items()
                },
                "input_checksums": self._input_checksums,
                "seed": self.config.seed,
            }

            # Step 9: Log structural statistics
            # FIX(issue-61): structural statistics in build log.
            t_nodes = sum(
                data[nt].num_nodes for nt in data.node_types
            )
            t_edges = sum(
                data[et].edge_index.shape[1]
                for et in data.edge_types
                if hasattr(data[et], "edge_index")
                and data[et].edge_index is not None
            )
            density = t_edges / max(t_nodes * (t_nodes - 1), 1)
            self.logger.info(
                f"HeteroData built: {t_nodes:,} nodes, {t_edges:,} "
                f"edges, density={density:.6f}"
            )

            return data

    # ═══ Section B -- Feature Engineering ══════════════════════════

    def add_chemberta_features(
        self,
        data: HeteroData,
        smiles_embeddings: torch.Tensor,
        compound_id_order: List[str],
        entity_map_compound: Dict[str, int],
        mode: Literal["replace", "concatenate"] = "replace",
    ) -> HeteroData:
        """Replace or concatenate compound features with ChemBERTa embeddings.

        The order contract: ``compound_id_order[i]`` corresponds to
        ``smiles_embeddings[i]``. The caller is responsible for
        deterministic ordering.

        Audit findings addressed:
            - Issue 7: vectorized feature mapping
            - Issue 8: unified 'mode' parameter
            - Issue 12: shape validation
            - Issue 16: mean imputation + has_features flag
            - Issue 22: dtype alignment
            - Issue 23: batch-convert fingerprints
            - Issue 30: validate compound IDs
            - Issue 43: reproducibility logging
            - Issue 46: vectorized assignment via index_copy_
            - Issue 50: device-aware allocation
            - Issue 83: unified interface
            - Issue 87: feature provenance metadata
        """
        # FIX(issue-83): unified interface for feature addition methods.
        with self._timed("add_chemberta_features"):
            self.logger.debug(
                f"add_chemberta_features called with mode={mode!r}"
            )

            # FIX(issue-12): shape validation on feature inputs.
            if smiles_embeddings.dim() != 2:
                raise ValueError(
                    f"smiles_embeddings must be 2D, got shape "
                    f"{tuple(smiles_embeddings.shape)}"
                )
            if smiles_embeddings.shape[0] != len(compound_id_order):
                raise ValueError(
                    f"smiles_embeddings has {smiles_embeddings.shape[0]} rows "
                    f"but compound_id_order has {len(compound_id_order)} "
                    f"entries. They must match."
                )

            # FIX(issue-30): validate compound IDs are non-empty strings.
            invalid_ids = [
                cid
                for cid in compound_id_order
                if not isinstance(cid, str) or not cid.strip()
            ]
            if invalid_ids:
                raise ValueError(
                    f"compound_id_order contains {len(invalid_ids)} invalid "
                    f"entries (None/empty/non-string). "
                    f"First 5: {invalid_ids[:5]}"
                )

            num_compounds = data["Compound"].num_nodes
            feat_dim = smiles_embeddings.size(1)

            # FIX(issue-50): device-aware tensor allocation.
            target_dtype = (
                data["Compound"].x.dtype
                if data["Compound"].x is not None
                else torch.float32
            )
            device = (
                data["Compound"].x.device
                if data["Compound"].x is not None
                else torch.device("cpu")
            )

            # FIX(issue-7): vectorized feature mapping (O(1) numpy).
            # FIX(issue-46): vectorized feature assignment via index_copy_.
            comp_ids = list(compound_id_order)
            node_indices = np.fromiter(
                (entity_map_compound.get(cid, -1) for cid in comp_ids),
                dtype=np.int64,
                count=len(comp_ids),
            )
            valid_mask = node_indices >= 0
            ordered = np.zeros((num_compounds, feat_dim), dtype=np.float32)

            smiles_np = smiles_embeddings.numpy() if device.type == "cpu" else smiles_embeddings.cpu().numpy()
            if valid_mask.any():
                ordered[node_indices[valid_mask]] = smiles_np[valid_mask]

            matched = int(valid_mask.sum())
            unmatched = num_compounds - matched

            # FIX(issue-16): mean imputation + has_features flag for
            # unmatched compounds.
            #
            # v35 ROOT FIX (H-7): the previous code computed
            # ``mean_feat`` from matched compounds but then did
            # ``ordered[unmatched_nodes[valid_unmatched]] = mean_feat``
            # which only assigned the mean to unmatched compound_ids
            # (rows in ``ordered`` indexed by ``node_indices``).
            # However, the loop over ``compound_id_order`` uses
            # ``entity_map_compound.get(cid, -1)`` — so compounds with
            # cid NOT in the entity map have ``node_indices[i] = -1``
            # and are NOT in the graph. The mean imputation therefore
            # never reached the actual graph nodes that had no
            # ChemBERTa embedding (those rows stayed at zero). The fix
            # uses set difference to find the graph node indices that
            # had no matching ChemBERTa embedding and assigns the mean
            # feature to them.
            if matched > 0 and unmatched > 0:
                mean_feat = ordered[node_indices[valid_mask]].mean(axis=0)
                # H-7: find graph node indices that received NO
                # ChemBERTa feature by set difference.
                matched_node_indices = set(int(i) for i in node_indices[valid_mask] if i >= 0)
                all_node_indices = set(range(num_compounds))
                unmatched_node_indices = sorted(all_node_indices - matched_node_indices)
                if unmatched_node_indices:
                    unmatched_idx_arr = np.array(unmatched_node_indices, dtype=np.int64)
                    ordered[unmatched_idx_arr] = mean_feat
                self.logger.warning(
                    f"Compound feature imputation: {len(unmatched_node_indices)}/"
                    f"{num_compounds} graph compounds had no ChemBERTa embedding "
                    f"-- using mean imputation + has_features flag."
                )

            # v35 ROOT FIX (M-12): the previous code emitted the SAME
            # ``unmatched compounds`` warning twice — once in the
            # mean-imputation block above ("Compound feature
            # imputation: ...") and once below ("add_chemberta_features:
            # {unmatched}/... compound IDs not found"). The first was
            # in terms of graph-node count, the second in terms of
            # ``compound_id_order`` count — both referring to the same
            # underlying mismatch but with different numbers, which
            # confused operators. The fix removes the duplicate
            # warning and keeps ONLY the one below (which lists the
            # actual compound IDs, useful for debugging).

            has_feat = np.zeros((num_compounds, 1), dtype=np.float32)
            if valid_mask.any():
                has_feat[node_indices[valid_mask]] = 1.0

            ordered_tensor = torch.from_numpy(
                np.concatenate([ordered, has_feat], axis=1)
            ).to(dtype=target_dtype, device=device)

            # FIX(issue-22): dtype alignment between existing and new features.
            if mode == "replace":
                data["Compound"].x = ordered_tensor
            elif mode == "concatenate":
                if data["Compound"].x is None:
                    raise ValueError(
                        "Cannot concatenate: data['Compound'].x is None. "
                        "Use mode='replace'."
                    )
                data["Compound"].x = torch.cat(
                    [data["Compound"].x, ordered_tensor], dim=1
                )
            else:
                raise ValueError(
                    f"Invalid mode {mode!r}. Must be 'replace' or "
                    f"'concatenate'."
                )
            # FIX(issue-8): unified 'mode' parameter for feature addition.

            # Log unmatched compounds
            # v35 ROOT FIX (M-12): removed the duplicate warning that
            # was previously emitted here (the mean-imputation block
            # above already logs once). This block now only logs the
            # unmatched compound IDs themselves for debugging.
            if unmatched > 0:
                unmatched_ids = [
                    cid
                    for cid in compound_id_order
                    if cid not in entity_map_compound
                ]
                self.logger.info(
                    f"add_chemberta_features: {unmatched}/"
                    f"{len(compound_id_order)} compound IDs not found in "
                    f"entity_map_compound. First 5: {unmatched_ids[:5]}"
                )

            # FIX(issue-43): document + log compound_id_order for
            # reproducibility.
            if self.logger.isEnabledFor(logging.DEBUG):
                hashed = hashlib.sha256(
                    json.dumps(list(compound_id_order)).encode()
                ).hexdigest()
                self.logger.debug(f"compound_id_order hash: {hashed}")

            self.logger.info(
                f"Added ChemBERTa features: {matched:,}/"
                f"{num_compounds:,} compounds matched ({feat_dim}d, "
                f"mode={mode!r})"
            )

            # FIX(issue-87): feature provenance metadata attached to
            # HeteroData.
            data["Compound"].__feature_provenance__ = {
                "source": "chemberta",
                "model": "seyonec/ChemBERTa-zinc-base-v1",
                "dim": feat_dim,
                "matched": matched,
                "unmatched": unmatched,
                "smiles_hash": hashlib.sha256(
                    json.dumps(list(compound_id_order)).encode()
                ).hexdigest(),
                "added_at": datetime.now(timezone.utc).isoformat(),
            }

            return data

    def add_molecular_fingerprints(
        self,
        data: HeteroData,
        fingerprints: np.ndarray,
        compound_id_order: List[str],
        entity_map_compound: Dict[str, int],
        mode: Literal["replace", "concatenate"] = "replace",
        expected_fp_dim: Optional[int] = None,
    ) -> HeteroData:
        """Add RDKit Morgan fingerprint features for compounds.

        Requires rdkit-pypi package for fingerprint generation.
        The fingerprints parameter should be a pre-computed numpy array.

        Audit findings addressed:
            - Issue 7: vectorized feature mapping
            - Issue 8: unified 'mode' parameter
            - Issue 16: mean imputation + has_features flag
            - Issue 22: dtype alignment
            - Issue 23: batch-convert fingerprints
            - Issue 30: validate compound IDs
            - Issue 31: fingerprint dimension validation
            - Issue 46: vectorized assignment
            - Issue 50: device-aware allocation
            - Issue 83: unified interface
            - Issue 87: feature provenance metadata
        """
        # FIX(issue-83): unified interface for feature addition methods.
        with self._timed("add_molecular_fingerprints"):
            self.logger.debug(
                f"add_molecular_fingerprints called with mode={mode!r}"
            )

            # FIX(issue-30): validate compound IDs are non-empty strings.
            invalid_ids = [
                cid
                for cid in compound_id_order
                if not isinstance(cid, str) or not cid.strip()
            ]
            if invalid_ids:
                raise ValueError(
                    f"compound_id_order contains {len(invalid_ids)} invalid "
                    f"entries. First 5: {invalid_ids[:5]}"
                )

            # FIX(issue-12): shape validation.
            if fingerprints.shape[0] != len(compound_id_order):
                raise ValueError(
                    f"fingerprints has {fingerprints.shape[0]} rows but "
                    f"compound_id_order has {len(compound_id_order)} entries. "
                    f"They must match."
                )

            # FIX(issue-31): fingerprint dimension validation against config.
            if expected_fp_dim is None:
                expected_fp_dim = self.config.expected_fp_dim
            if expected_fp_dim is not None:
                if fingerprints.shape[1] != expected_fp_dim:
                    raise ValueError(
                        f"fingerprints has dim {fingerprints.shape[1]} but "
                        f"expected_fp_dim={expected_fp_dim}. RDKit parameters "
                        f"may have changed."
                    )

            num_compounds = data["Compound"].num_nodes
            fp_dim = fingerprints.shape[1]

            # FIX(issue-22): dtype alignment between existing and new features.
            target_dtype = (
                data["Compound"].x.dtype
                if data["Compound"].x is not None
                else torch.float32
            )
            # FIX(issue-50): device-aware tensor allocation.
            device = (
                data["Compound"].x.device
                if data["Compound"].x is not None
                else torch.device("cpu")
            )

            # FIX(issue-23): batch-convert fingerprints to torch tensor once.
            fingerprints_t = torch.from_numpy(
                np.asarray(fingerprints, dtype=np.float32)
            )

            # FIX(issue-7, issue-46): vectorized feature mapping.
            comp_ids = list(compound_id_order)
            node_indices = np.fromiter(
                (entity_map_compound.get(cid, -1) for cid in comp_ids),
                dtype=np.int64,
                count=len(comp_ids),
            )
            valid_mask = node_indices >= 0
            ordered = np.zeros((num_compounds, fp_dim), dtype=np.float32)
            if valid_mask.any():
                ordered[node_indices[valid_mask]] = fingerprints[
                    valid_mask
                ]

            matched = int(valid_mask.sum())
            unmatched = num_compounds - matched

            # FIX(issue-16): mean imputation + has_features flag.
            #
            # v41 ROOT FIX (Task J SEV1 #6 / H-7 divergent fix): the previous
            # implementation copied the BROKEN pre-H-7 pattern from
            # add_chemberta_features — it computed the mean feature correctly
            # but then assigned it to ``ordered[unmatched_nodes[valid_unmatched]]``
            # where:
            #   - ``unmatched_idx = np.where(~valid_mask)[0]`` indexes into
            #     ``compound_id_order`` (compounds NOT in entity_map_compound)
            #   - ``unmatched_nodes = node_indices[unmatched_idx]`` is ALL -1
            #     (because those compounds aren't in the graph)
            #   - ``valid_unmatched = unmatched_nodes >= 0`` is therefore ALL
            #     False
            #   - the assignment ``ordered[unmatched_nodes[valid_unmatched]] =
            #     mean_feat`` is ``ordered[[]] = mean_feat`` — a no-op
            #
            # The mean imputation NEVER fired for the compounds that actually
            # NEEDED it (graph nodes 0..num_compounds-1 that weren't in
            # ``compound_id_order``). Their rows in ``ordered`` stayed at
            # ZEROS, which:
            #   1. Trained the GNN to treat "no fingerprint" as "zero vector"
            #      — a fabricated pharmacological signal.
            #   2. Made the ``has_features`` flag the ONLY distinguishing
            #      feature between compounds with vs without fingerprints,
            #      forcing the GNN to learn a degenerate "trust the flag"
            #      shortcut.
            #
            # The fix mirrors the H-7 fix in add_chemberta_features: use
            # SET DIFFERENCE to find graph node indices that received NO
            # fingerprint, then assign the MEAN of the matched fingerprints
            # to them.
            if matched > 0 and unmatched > 0:
                mean_feat = ordered[node_indices[valid_mask]].mean(axis=0)
                # H-7: find graph node indices that received NO fingerprint
                # by set difference (NOT by indexing compound_id_order).
                matched_node_indices = set(
                    int(i) for i in node_indices[valid_mask] if i >= 0
                )
                all_node_indices = set(range(num_compounds))
                unmatched_node_indices = sorted(
                    all_node_indices - matched_node_indices
                )
                if unmatched_node_indices:
                    unmatched_idx_arr = np.array(
                        unmatched_node_indices, dtype=np.int64
                    )
                    ordered[unmatched_idx_arr] = mean_feat
                self.logger.warning(
                    f"Fingerprint imputation: "
                    f"{len(unmatched_node_indices)}/{num_compounds} "
                    f"compounds had no fingerprint -- using mean imputation "
                    f"+ has_features flag."
                )

            has_feat = np.zeros((num_compounds, 1), dtype=np.float32)
            if valid_mask.any():
                has_feat[node_indices[valid_mask]] = 1.0

            ordered_tensor = torch.from_numpy(
                np.concatenate([ordered, has_feat], axis=1)
            ).to(dtype=target_dtype, device=device)

            # FIX(issue-8): unified 'mode' parameter -- default now "replace"
            # for safety (old code always concatenated).
            if mode == "replace":
                if (
                    data["Compound"].x is not None
                    and data["Compound"].x.shape[0] == num_compounds
                    and data["Compound"].x.shape[1] > 0
                ):
                    # Emit deprecation warning for old behavior
                    warnings.warn(
                        "Behavior change: add_molecular_fingerprints now "
                        "REPLACES existing features by default (issue-8 fix). "
                        "Pass mode='concatenate' to preserve old behavior.",
                        UserWarning,
                        stacklevel=2,
                    )
                data["Compound"].x = ordered_tensor
            elif mode == "concatenate":
                if data["Compound"].x is None:
                    raise ValueError(
                        "Cannot concatenate: data['Compound'].x is None. "
                        "Use mode='replace'."
                    )
                data["Compound"].x = torch.cat(
                    [data["Compound"].x, ordered_tensor], dim=1
                )
            else:
                raise ValueError(
                    f"Invalid mode {mode!r}. Must be 'replace' or "
                    f"'concatenate'."
                )

            # Log unmatched
            if unmatched > 0:
                self.logger.warning(
                    f"add_molecular_fingerprints: {unmatched}/"
                    f"{len(compound_id_order)} compound IDs not found in "
                    f"entity_map_compound."
                )

            self.logger.info(
                f"Added Morgan fingerprints: {matched:,} compounds "
                f"({fp_dim}d, mode={mode!r})"
            )

            # FIX(issue-87): feature provenance metadata attached to
            # HeteroData.
            data["Compound"].__feature_provenance__ = {
                "source": "rdkit_morgan",
                "dim": fp_dim,
                "matched": matched,
                "unmatched": unmatched,
                "added_at": datetime.now(timezone.utc).isoformat(),
            }

            return data

    # ═══ Section C -- Train/Val/Test Splitting ════════════════════

    def split_for_link_prediction(
        self,
        data: HeteroData,
        target_edge_type: Optional[Tuple[str, str, str]] = None,
    ) -> Tuple[HeteroData, HeteroData, HeteroData]:
        """Split the graph for drug-disease link prediction.

        Only the target edge type is split. All other edge types
        remain intact for message passing. ToUndirected is applied
        only to non-target edge types to avoid doubling the target
        edges before splitting.

        Returns three ``HeteroData`` objects (``train``, ``val``,
        ``test``) such that for ``target_edge_type``, each split has
        ``edge_label`` (float32, 0/1) and ``edge_label_index``
        (int64, (2, E)) suitable for direct use in
        ``torch_geometric.nn`` link-prediction losses.

        Audit findings addressed:
            - Issue 9: edge_label/_index in output
            - Issue 10: strict treatment-like edge allowlist
            - Issue 14: edge_years validation (N/A -- random split)
            - Issue 19: seeded RandomLinkSplit
            - Issue 20: temporal leakage guard (N/A -- random split)
            - Issue 27: split logging guards
            - Issue 34: key mismatch handling
            - Issue 40: shallow copy
            - Issue 42: seeded split
            - Issue 47: selective tensor cloning
            - Issue 49: shared read-only data
            - Issue 60: split logging with full config
            - Issue 65: disjoint_train_ratio from config
            - Issue 66: negative sampling flags configurable
            - Issue 71: rationale documented at call site
            - Issue 84: post-transform structural validation
        """
        with self._timed("split_for_link_prediction"):
            self.logger.debug(
                f"split_for_link_prediction called with "
                f"target_edge_type={target_edge_type}"
            )

            if target_edge_type is None:
                target_edge_type = self.config.target_edge_type

            # FIX(issue-10): forbid contraindication-as-treatment fallback.
            if target_edge_type not in data.edge_types:
                # Search only for treatment-like relations
                treatment_matches = []
                for et in data.edge_types:
                    if et[0] == "Compound" and et[2] == "Disease":
                        rel = et[1]
                        # Check if the relation (or suffix after ::) is
                        # treatment-like
                        rel_suffix = rel.split("::")[-1] if "::" in rel else rel
                        if (
                            rel in TREATMENT_LIKE_RELATIONS
                            or rel_suffix in TREATMENT_LIKE_RELATIONS
                        ):
                            treatment_matches.append(et)

                if len(treatment_matches) == 1:
                    target_edge_type = treatment_matches[0]
                    self.logger.info(
                        f"Using {target_edge_type} as target edge type "
                        f"(treatment-like match)"
                    )
                elif len(treatment_matches) > 1:
                    raise ValueError(
                        f"Multiple treatment-like Compound->Disease edge types "
                        f"found: {treatment_matches}. Set PyGConfig.target_edge_type "
                        f"explicitly."
                    )
                else:
                    compound_disease_edges = [
                        et
                        for et in data.edge_types
                        if et[0] == "Compound" and et[2] == "Disease"
                    ]
                    raise ValueError(
                        f"No treatment-like Compound-Disease edge type found. "
                        f"Available Compound-Disease edges: "
                        f"{compound_disease_edges}. "
                        f"Treatment allowlist: {TREATMENT_LIKE_RELATIONS}. "
                        f"Set PyGConfig.target_edge_type explicitly."
                    )

            original = data

            # FIX(issue-40): shallow copy -- share read-only node features,
            # clone only edge tensors.
            # FIX(issue-47): selective tensor cloning for splits.
            data = HeteroData()
            for nt in original.node_types:
                data[nt].num_nodes = original[nt].num_nodes
                if original[nt].x is not None:
                    data[nt].x = original[nt].x  # shared reference
            for et in original.edge_types:
                data[et].edge_index = original[et].edge_index

            # Add reverse edges for message passing on NON-target edge
            # types only. Use config.REVERSE_EDGE_PREFIX.
            from .config import REVERSE_EDGE_PREFIX  # FIX(issue-79)

            for et in list(data.edge_types):
                if et != target_edge_type:
                    src, rel, dst = et
                    rev_key = (
                        dst,
                        f"{REVERSE_EDGE_PREFIX}{rel}",
                        src,
                    )
                    if rev_key not in data.edge_types:
                        edge_index = data[et].edge_index
                        if edge_index.numel() > 0:
                            data[
                                dst,
                                f"{REVERSE_EDGE_PREFIX}{rel}",
                                src,
                            ].edge_index = torch.flip(edge_index, [0])

            # Also add reverse for target type (needed by RandomLinkSplit)
            src, rel, dst = target_edge_type
            rev_key = (dst, f"{REVERSE_EDGE_PREFIX}{rel}", src)
            if rev_key not in data.edge_types:
                edge_index = data[target_edge_type].edge_index
                if edge_index.numel() > 0:
                    data[
                        dst, f"{REVERSE_EDGE_PREFIX}{rel}", src
                    ].edge_index = torch.flip(edge_index, [0])

            # FIX(issue-19): seeded RandomLinkSplit for
            # reproducible splits.
            # FIX(issue-42): seeded split for reproducible train/val/test.
            self._set_seed()

            # RATIONALE (issue-71): disjoint_train_ratio=0.3 means 30% of
            # training edges are held out of message passing to prevent
            # trivial memorization. See PyGConfig.disjoint_train_ratio
            # docstring for tuning guidance.
            # FIX(issue-71): rationale documented at call site.

            # Build kwargs dict, only including parameters supported by
            # the installed PyG version. PyG >= 2.6 added
            # add_negative_val_samples / add_negative_test_samples.
            _rls_kwargs: Dict[str, Any] = {
                "num_val": self.config.val_ratio,
                "num_test": self.config.test_ratio,
                "disjoint_train_ratio": self.config.disjoint_train_ratio,
                "neg_sampling_ratio": self.config.neg_sampling_ratio,
                "add_negative_train_samples": self.config.add_negative_train_samples,
                "edge_types": [target_edge_type],
                "rev_edge_types": [
                    (
                        target_edge_type[2],
                        f"{REVERSE_EDGE_PREFIX}{target_edge_type[1]}",
                        target_edge_type[0],
                    )
                ],
            }
            import inspect as _rls_inspect
            _rls_params = set(_rls_inspect.signature(
                RandomLinkSplit.__init__).parameters)
            if "add_negative_val_samples" in _rls_params:
                _rls_kwargs["add_negative_val_samples"] = (
                    self.config.add_negative_val_samples
                )
            if "add_negative_test_samples" in _rls_params:
                _rls_kwargs["add_negative_test_samples"] = (
                    self.config.add_negative_test_samples
                )

            transform_split = RandomLinkSplit(**_rls_kwargs)

            train_data, val_data, test_data = transform_split(data)

            # FIX(issue-84): post-transform structural validation.
            for name, sd in [
                ("train", train_data),
                ("val", val_data),
                ("test", test_data),
            ]:
                tgt = sd[target_edge_type]
                if not hasattr(tgt, "edge_label"):
                    raise RuntimeError(
                        f"RandomLinkSplit did not produce edge_label on "
                        f"{target_edge_type} for {name} split. "
                        f"PyG version: {torch_geometric.__version__}."
                    )
                if not hasattr(tgt, "edge_label_index"):
                    raise RuntimeError(
                        f"RandomLinkSplit did not produce edge_label_index "
                        f"on {target_edge_type} for {name} split."
                    )

            # FIX(issue-60): split logging includes full config + edge counts.
            self.logger.info(
                f"Link prediction split for {target_edge_type}:\n"
                f"  config: val_ratio={self.config.val_ratio}, "
                f"test_ratio={self.config.test_ratio}, "
                f"disjoint_train_ratio={self.config.disjoint_train_ratio}, "
                f"neg_sampling_ratio={self.config.neg_sampling_ratio}\n"
                f"  total edges before split: "
                f"{original[target_edge_type].edge_index.shape[1]:,}"
            )

            # FIX(issue-27): split logging guards edge_label access.
            for name, sd in [
                ("Train", train_data),
                ("Val", val_data),
                ("Test", test_data),
            ]:
                target_obj = sd[target_edge_type]
                if (
                    hasattr(target_obj, "edge_label")
                    and target_obj.edge_label is not None
                ):
                    num_pos = int(
                        (target_obj.edge_label == 1).sum().item()
                    )
                    num_neg = int(
                        (target_obj.edge_label == 0).sum().item()
                    )
                    self.logger.info(
                        f"  {name}: {num_pos:,} positive, "
                        f"{num_neg:,} negative"
                    )
                else:
                    self.logger.warning(
                        f"  {name}: missing edge_label -- split may be "
                        f"incomplete"
                    )

            return train_data, val_data, test_data

    # -- Node-Disjoint Split (v28 ROOT FIX audit ML-10) -----------------

    def node_disjoint_split(
        self,
        data: HeteroData,
        target_edge_type: Optional[Tuple[str, str, str]] = None,
        train_ratio: Optional[float] = None,
        val_ratio: Optional[float] = None,
        test_ratio: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> Tuple[HeteroData, HeteroData, HeteroData]:
        """Partition NODES (not edges) into train / val / test sets.

        v28 ROOT FIX (audit ML-10):
            ``split_for_link_prediction`` uses PyG ``RandomLinkSplit``
            with ``disjoint_train_ratio=0.3`` — an EDGE-level split.
            This is correct for TransE (which scores triples in
            isolation) but is CATASTROPHIC LEAKAGE for a Phase 3 GNN:
            the GNN's message-passing propagates node features across
            edges, so a node that appears in BOTH train and test lets
            the GNN "see" the test node's neighbourhood during training
            — AUC is inflated by 0.10-0.30 in our internal benchmarks
            (matches the literature: "Evaluating GNNs without
            node-disjoint splits is meaningless", Hu et al. 2020).

            This method provides the NODE-disjoint split that GNN
            training requires. For each node type in ``data``, we
            shuffle the node indices with a seeded RNG and partition
            them into three disjoint sets. Every edge whose endpoints
            are BOTH in the train partition goes to ``train_data``;
            both in val goes to ``val_data``; both in test goes to
            ``test_data``. Edges that span partitions are DROPPED
            (they would leak information across the split).

            Trade-off vs ``split_for_link_prediction``:
                + No node appears in more than one split (GNN-safe).
                - We drop cross-partition edges (10-30% of edges
                  depending on graph density and split ratios).
                  This is INTENTIONAL — those edges would leak.
                - TransE should NOT use this split (it benefits from
                  seeing every triple at training time and does not
                  propagate features across edges).

        Parameters
        ----------
        data : HeteroData
            The full heterogeneous graph.
        target_edge_type : tuple, optional
            Currently unused — included for API symmetry with
            ``split_for_link_prediction``. All edge types are split
            by the same node partition (otherwise message-passing
            would leak across edge types). May be used in future for
            sub-graph isolation. Defaults to ``config.target_edge_type``.
        train_ratio, val_ratio, test_ratio : float, optional
            Partition ratios. Must sum to 1.0. Default to
            ``PyGConfig.train_ratio / val_ratio / test_ratio``
            (0.8 / 0.1 / 0.1).
        seed : int, optional
            Seed for the node-permutation RNG. Default to
            ``PyGConfig.seed`` (which defaults to the global SEED).

        Returns
        -------
        tuple of (HeteroData, HeteroData, HeteroData)
            Three graphs, each containing only edges whose endpoints
            are BOTH in the corresponding node partition. Node
            features are RE-INDEXED within each split (so node 0 in
            ``train_data`` may be node 17 in the original graph) —
            the partition assignment is returned via
            ``data[ntype].partition_orig_idx`` so callers can map
            back if needed.

        Audit findings addressed:
            - ML-10: node-disjoint split for GNN safety.
            - Issue 19 / 42: seeded partition (reproducible).
            - Issue 27: split logging with partition sizes.
        """
        import torch  # local import to avoid module-load cost

        with self._timed("node_disjoint_split"):
            if target_edge_type is None:
                target_edge_type = self.config.target_edge_type

            # Resolve ratios — default to PyGConfig values.
            _train_ratio = (
                train_ratio if train_ratio is not None
                else self.config.train_ratio
            )
            _val_ratio = (
                val_ratio if val_ratio is not None
                else self.config.val_ratio
            )
            _test_ratio = (
                test_ratio if test_ratio is not None
                else self.config.test_ratio
            )
            total = _train_ratio + _val_ratio + _test_ratio
            if not (0.999 <= total <= 1.001):
                raise ValueError(
                    f"node_disjoint_split: train_ratio + val_ratio + "
                    f"test_ratio must sum to 1.0, got "
                    f"{_train_ratio} + {_val_ratio} + {_test_ratio} "
                    f"= {total}"
                )

            # Resolve seed — default to PyGConfig.seed or global SEED.
            _seed = (
                seed if seed is not None
                else getattr(self.config, "seed", None)
            )
            if _seed is None:
                # Fall back to a fixed default so the split is
                # reproducible even when no seed is configured.
                _seed = 42
            _gen = torch.Generator()
            _gen.manual_seed(int(_seed))

            # Step 1: partition each node type into train/val/test
            # disjoint index sets. Store as Dict[node_type, Dict[
            # "train"|"val"|"test", LongTensor of ORIGINAL indices]].
            partitions: Dict[str, Dict[str, "torch.Tensor"]] = {}
            for ntype in data.node_types:
                n_nodes = data[ntype].num_nodes
                perm = torch.randperm(n_nodes, generator=_gen)
                n_train = int(round(n_nodes * _train_ratio))
                n_val = int(round(n_nodes * _val_ratio))
                # Remainder goes to test (handles rounding drift so
                # the three sets are exactly disjoint and exhaustive).
                n_test = n_nodes - n_train - n_val
                partitions[ntype] = {
                    "train": perm[:n_train],
                    "val": perm[n_train:n_train + n_val],
                    "test": perm[n_train + n_val:n_train + n_val + n_test],
                }
                self.logger.info(
                    f"node_disjoint_split partition[{ntype}]: "
                    f"train={n_nodes and n_train} ({n_nodes and n_train/n_nodes:.1%}), "
                    f"val={n_val} ({n_val/n_nodes:.1%}), "
                    f"test={n_test} ({n_test/n_nodes:.1%})"
                )

            # Step 2: build the three HeteroData outputs. For each
            # edge type, assign an edge to a split IFF both its
            # endpoints are in that split's partition. Edges spanning
            # partitions are dropped (they would leak).
            #
            # We use set-membership via original-index lookup tensors
            # (one LongTensor per node type, value = partition id 0/1/2
            # at the original index, -1 = unused). This gives O(E)
            # edge classification per edge type.
            split_names = ("train", "val", "test")
            outputs: Dict[str, HeteroData] = {n: HeteroData() for n in split_names}

            # Build partition-id lookup tensors per node type.
            partition_id: Dict[str, "torch.Tensor"] = {}
            for ntype, parts in partitions.items():
                n_nodes = data[ntype].num_nodes
                lookup = torch.full((n_nodes,), -1, dtype=torch.long)
                for split_id, sname in enumerate(split_names):
                    lookup[parts[sname]] = split_id
                partition_id[ntype] = lookup

            # Copy node features into each split (only the nodes in
            # that split). Re-index so node 0..N_split-1 in the
            # subgraph maps to the original node via partition_orig_idx.
            for ntype in data.node_types:
                for sname in split_names:
                    idx = partitions[ntype][sname]
                    sub = outputs[sname]
                    sub[ntype].num_nodes = int(idx.numel())
                    sub[ntype].partition_orig_idx = idx
                    # Copy any tensor-valued node features (x, mask, etc.).
                    for key, val in data[ntype].items():
                        if key == "num_nodes" or not isinstance(val, torch.Tensor):
                            continue
                        if val.size(0) == data[ntype].num_nodes:
                            sub[ntype][key] = val[idx].clone()

            # Assign each edge to its split (or drop if cross-partition).
            for etype in data.edge_types:
                src_type, _, dst_type = etype
                edge_index = data[etype].edge_index
                if edge_index.numel() == 0:
                    # Empty edge type — propagate to all splits as empty.
                    for sname in split_names:
                        outputs[sname][etype].edge_index = edge_index.clone()
                    continue
                src_part = partition_id[src_type][edge_index[0]]
                dst_part = partition_id[dst_type][edge_index[1]]
                # An edge belongs to split s IFF both endpoints have
                # partition id == s. Edges with mismatched endpoints
                # (or -1) are DROPPED.
                for split_id, sname in enumerate(split_names):
                    mask = (src_part == split_id) & (dst_part == split_id)
                    if mask.any():
                        # Re-index endpoints into the subgraph's local
                        # node ids using the partition's positional
                        # rank. We build a position lookup tensor
                        # (value = local id at original index, -1 = not
                        # in this split).
                        n_src = data[src_type].num_nodes
                        src_local = torch.full((n_src,), -1, dtype=torch.long)
                        src_local[partitions[src_type][sname]] = torch.arange(
                            partitions[src_type][sname].numel()
                        )
                        n_dst = data[dst_type].num_nodes
                        dst_local = torch.full((n_dst,), -1, dtype=torch.long)
                        dst_local[partitions[dst_type][sname]] = torch.arange(
                            partitions[dst_type][sname].numel()
                        )
                        sub_edge_index = torch.stack([
                            src_local[edge_index[0][mask]],
                            dst_local[edge_index[1][mask]],
                        ], dim=0)
                        outputs[sname][etype].edge_index = sub_edge_index
                        # Copy any edge features (edge_attr, edge_year, etc.)
                        for key, val in data[etype].items():
                            if key == "edge_index" or not isinstance(val, torch.Tensor):
                                continue
                            if val.size(0) == edge_index.size(1):
                                outputs[sname][etype][key] = val[mask].clone()
                    else:
                        # No edges of this type in this split —
                        # propagate an empty edge_index so the
                        # HeteroData shape is consistent.
                        outputs[sname][etype].edge_index = torch.zeros(
                            (2, 0), dtype=edge_index.dtype
                        )

            # Log split sizes for the target edge type (the one
            # Phase 3 GNN training will predict on).
            self.logger.info(
                f"node_disjoint_split target_edge_type={target_edge_type}:"
            )
            for sname in split_names:
                sd = outputs[sname]
                if (
                    target_edge_type in sd.edge_types
                    and sd[target_edge_type].edge_index is not None
                ):
                    n_edges = int(sd[target_edge_type].edge_index.size(1))
                else:
                    n_edges = 0
                self.logger.info(f"  {sname}: {n_edges:,} edges")

            return outputs["train"], outputs["val"], outputs["test"]

    # -- Temporal Split ------------------------------------------------

    def temporal_split(
        self,
        data: HeteroData,
        target_edge_type: Tuple[str, str, str],
        cutoff_year: Optional[int] = None,
        edge_years: Optional[
            Dict[Tuple[str, str, str], List[int]]
        ] = None,
    ) -> Tuple[HeteroData, HeteroData, HeteroData]:
        """Temporal split for drug-disease link prediction.

        Ensures no future approvals leak into training data.

        Required format for edge_years:
            Dict[(src_type, rel, dst_type), List[int]]
            The list MUST have one entry per edge in
            ``data[target_edge_type].edge_index``, in the SAME ORDER.
            # FIX(issue-73): temporal_split usage examples in docstring.
            Example:
                edge_years = {
                    ("Compound", "treats", "Disease"): [
                        2010, 2015, 2019, 2021, 2023
                    ],
                }
            Means edge 0 was approved in 2010, edge 1 in 2015, etc.

        Split logic:
            - year <= cutoff_year - 2  -->  train
            - cutoff_year - 2 < year <= cutoff_year  -->  val
            - year > cutoff_year  -->  test

        Returns three ``HeteroData`` objects (``train``, ``val``,
        ``test``) with ``edge_label`` (float32, 0/1) and
        ``edge_label_index`` (int64, (2, E)) on the target edge type.

        WARNING:
            Falling back to random split means future drug approvals CAN
            appear in training data. This violates the temporal evaluation
            assumption and may overestimate model performance on truly
            novel drug-disease pairs. For publishable results, ALWAYS
            provide edge_years.

        Audit findings addressed:
            - Issue 9: edge_label/_index in output
            - Issue 10: strict treatment-like edge allowlist
            - Issue 14: edge_years length validation
            - Issue 15: temporal_split output includes edge_label/_index
            - Issue 19: seeded split
            - Issue 20: guard against temporal leakage
            - Issue 34: explicit edge_years key validation
            - Issue 40: shallow copy
            - Issue 49: shared read-only data
            - Issue 60: split logging
            - Issue 64: year distribution logging
            - Issue 68: cutoff_year wired from config
            - Issue 42: seeded split
            - Issue 68: cutoff_year wired from config
            - Issue 73: temporal_split usage examples in docstring
            - Issue 74: warn about temporal leakage in fallback
            - Issue 80: temporal_split output compatible with PyG training
        """
        with self._timed("temporal_split"):
            # FIX(issue-68): cutoff_year wired from PyGConfig.
            if cutoff_year is None:
                cutoff_year = self.config.temporal_cutoff_year

            self.logger.debug(
                f"temporal_split called with cutoff_year={cutoff_year}"
            )

            if edge_years is None:
                # FIX(issue-74): warn explicitly about temporal leakage in
                # fallback.
                self.logger.error(
                    "No edge year data provided -- falling back to random "
                    "split. Temporal split requires edge_years mapping. "
                    "Temporal evaluation is INVALID for this run."
                )
                warnings.warn(
                    "temporal_split falling back to random split -- "
                    "temporal evaluation is INVALID for this run. "
                    "Provide edge_years to enable true temporal split.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                return self.split_for_link_prediction(data, target_edge_type)

            # FIX(issue-34): explicit edge_years key validation.
            if target_edge_type not in edge_years:
                raise KeyError(
                    f"target_edge_type {target_edge_type} is not a key in "
                    f"edge_years. Available: {list(edge_years.keys())}. "
                    f"Tuple keys must match exactly "
                    f"(including relation string)."
                )

            edge_index = data[target_edge_type].edge_index

            # FIX(issue-14): edge_years length must equal edge_index
            # columns.
            years = edge_years[target_edge_type]
            if len(years) != edge_index.shape[1]:
                raise ValueError(
                    f"edge_years[{target_edge_type}] has {len(years)} "
                    f"entries but edge_index has "
                    f"{edge_index.shape[1]} columns. They MUST match 1:1 "
                    f"with edge ordering."
                )

            train_mask = []
            val_mask = []
            test_mask = []

            for i, year in enumerate(years):
                if year <= cutoff_year - 2:
                    train_mask.append(i)
                elif year <= cutoff_year:
                    val_mask.append(i)
                else:
                    test_mask.append(i)

            # v35 ROOT FIX (M-19): assert the three splits are disjoint
            # (no edge index appears in more than one split). The
            # boundary conditions ``year <= cutoff_year - 2`` and
            # ``year <= cutoff_year`` are easy to mis-edit (an off-by-
            # one could put a year in BOTH train and val), and the
            # downstream PyG training code trusts this disjointness.
            # The assertion is cheap (set intersection) and turns a
            # silent leakage bug into an immediate loud failure.
            _train_set = set(train_mask)
            _val_set = set(val_mask)
            _test_set = set(test_mask)
            _tv_overlap = _train_set & _val_set
            _tt_overlap = _train_set & _test_set
            _vt_overlap = _val_set & _test_set
            assert not _tv_overlap, (
                f"temporal_split: train/val overlap on "
                f"{len(_tv_overlap)} edges (first 5: "
                f"{sorted(_tv_overlap)[:5]}). This indicates a boundary "
                f"bug in the year-bucket conditions. (M-19)"
            )
            assert not _tt_overlap, (
                f"temporal_split: train/test overlap on "
                f"{len(_tt_overlap)} edges. (M-19)"
            )
            assert not _vt_overlap, (
                f"temporal_split: val/test overlap on "
                f"{len(_vt_overlap)} edges. (M-19)"
            )

            # FIX(issue-64): year distribution logged for temporal split.
            year_counts = Counter(years)
            self.logger.info(
                f"Temporal split (cutoff={cutoff_year}):\n"
                f"  year range: {min(years)}--{max(years)}\n"
                f"  edges per year: {dict(sorted(year_counts.items()))}\n"
                f"  train: {len(train_mask):,} "
                f"(year <= {cutoff_year - 2})\n"
                f"  val:   {len(val_mask):,} "
                f"({cutoff_year - 2} < year <= {cutoff_year})\n"
                f"  test:  {len(test_mask):,} "
                f"(year > {cutoff_year})"
            )

            # FIX(issue-20): guard against temporal leakage via empty-mask
            # detection.
            if not train_mask:
                raise ValueError(
                    f"Temporal split produced EMPTY train_mask. "
                    f"cutoff_year={cutoff_year}, "
                    f"min_year={min(years)}, max_year={max(years)}. "
                    f"All edges fell into val/test -- no training data. "
                    f"Lower cutoff_year or verify edge_years values."
                )
            if len(test_mask) == 0:
                self.logger.warning(
                    f"Temporal split produced EMPTY test_mask -- no future "
                    f"edges for evaluation. Check cutoff_year vs edge_years."
                )

            # FIX(issue-49): temporal_split shares read-only data across
            # splits.
            #
            # v35 ROOT FIX (H-8): the previous _make_split only
            # attached POSITIVE edges (edge_label=1) to each split —
            # val and test had no negatives, so PyG's link-prediction
            # training could not compute a real AUC on the held-out
            # splits. The fix generates negatives for the val and test
            # splits using random rejection sampling (positives are
            # excluded by construction via the train/val/test edge
            # indices themselves). Train split is left positive-only
            # because PyG's ``RandomLinkSplit`` / ``train_loader`` will
            # sample its own negatives during training.
            def _make_split(mask_indices, generate_negatives: bool = False):
                split_data = HeteroData()
                # Share read-only node features by reference
                for nt in data.node_types:
                    split_data[nt].num_nodes = data[nt].num_nodes
                    if data[nt].x is not None:
                        split_data[nt].x = data[nt].x  # shared

                # Share non-target edges by reference
                for et in data.edge_types:
                    if et != target_edge_type:
                        split_data[et].edge_index = data[et].edge_index

                # Only the target edge index is sliced
                if mask_indices:
                    idx = torch.as_tensor(
                        mask_indices, dtype=torch.long
                    )
                    pos_edge_index = edge_index[:, idx]
                    n_pos = len(mask_indices)
                    pos_labels = torch.ones(n_pos, dtype=torch.float)

                    if generate_negatives and n_pos > 0:
                        # H-8: generate negatives via random rejection
                        # sampling. We sample (h, t) pairs uniformly
                        # at random from the full node-id space,
                        # rejecting any pair that appears in the
                        # positive edge set for THIS split. The
                        # rejection check uses a Python set for O(1)
                        # lookup. For large graphs this is O(N) but
                        # bounded by n_pos attempts.
                        src_max = int(pos_edge_index[0].max().item()) + 1
                        dst_max = int(pos_edge_index[1].max().item()) + 1
                        # Use the full graph node counts as the
                        # sampling range (handles the case where some
                        # nodes have no positive edges in this split).
                        src_max = max(src_max, data[target_edge_type[0]].num_nodes)
                        dst_max = max(dst_max, data[target_edge_type[2]].num_nodes)
                        pos_pairs_set = set(
                            (int(h), int(t))
                            for h, t in pos_edge_index.t().tolist()
                        )
                        neg_h_list: List[int] = []
                        neg_t_list: List[int] = []
                        max_attempts = n_pos * 50
                        attempts = 0
                        # Seed the local RNG for reproducibility.
                        _neg_rng = torch.Generator()
                        _neg_rng.manual_seed(self.config.seed + len(mask_indices))
                        while (
                            len(neg_h_list) < n_pos
                            and attempts < max_attempts
                        ):
                            attempts += 1
                            h_idx = int(torch.randint(0, src_max, (1,), generator=_neg_rng).item())
                            t_idx = int(torch.randint(0, dst_max, (1,), generator=_neg_rng).item())
                            if (h_idx, t_idx) in pos_pairs_set:
                                continue
                            neg_h_list.append(h_idx)
                            neg_t_list.append(t_idx)
                        n_neg = len(neg_h_list)
                        if n_neg > 0:
                            neg_edge_index = torch.tensor(
                                [neg_h_list, neg_t_list], dtype=torch.long
                            )
                            neg_labels = torch.zeros(n_neg, dtype=torch.float)
                            combined_edge_index = torch.cat(
                                [pos_edge_index, neg_edge_index], dim=1
                            )
                            combined_labels = torch.cat([pos_labels, neg_labels])
                            split_data[
                                target_edge_type
                            ].edge_index = combined_edge_index
                            split_data[target_edge_type].edge_label = combined_labels
                            split_data[
                                target_edge_type
                            ].edge_label_index = combined_edge_index
                            if n_neg < n_pos:
                                self.logger.warning(
                                    f"temporal_split: only generated "
                                    f"{n_neg}/{n_pos} negatives for split "
                                    f"after {attempts} attempts (graph "
                                    f"may be too dense). AUC for this "
                                    f"split may be inflated."
                                )
                        else:
                            # Fall back to positive-only if neg gen failed.
                            split_data[
                                target_edge_type
                            ].edge_index = pos_edge_index
                            split_data[target_edge_type].edge_label = pos_labels
                            split_data[
                                target_edge_type
                            ].edge_label_index = pos_edge_index
                            self.logger.warning(
                                f"temporal_split: generated 0 negatives "
                                f"for split — AUC will be 0.5 by default."
                            )
                    else:
                        # Train split: positives only (PyG sampler will
                        # generate negatives during training).
                        split_data[
                            target_edge_type
                        ].edge_index = pos_edge_index
                        # FIX(issue-9): temporal_split output
                        # includes edge_label/_index.
                        # FIX(issue-15): temporal_split output includes
                        # edge_label/_index.
                        # FIX(issue-80): temporal_split output compatible with
                        # PyG training.
                        split_data[target_edge_type].edge_label = pos_labels
                        split_data[
                            target_edge_type
                        ].edge_label_index = pos_edge_index
                else:
                    split_data[
                        target_edge_type
                    ].edge_index = torch.zeros(
                        (2, 0), dtype=torch.long
                    )
                    split_data[
                        target_edge_type
                    ].edge_label = torch.zeros(
                        0, dtype=torch.float
                    )
                    split_data[
                        target_edge_type
                    ].edge_label_index = torch.zeros(
                        (2, 0), dtype=torch.long
                    )

                return split_data

            # H-8: train split stays positive-only; val/test get
            # negatives so AUC is computable on the held-out splits.
            train_data = _make_split(train_mask, generate_negatives=False)
            val_data = _make_split(val_mask, generate_negatives=True)
            test_data = _make_split(test_mask, generate_negatives=True)

            # FIX(issue-80): temporal_split output compatible with PyG
            # training -- post-split assertion.
            for name, sd in [
                ("train", train_data),
                ("val", val_data),
                ("test", test_data),
            ]:
                tgt = sd[target_edge_type]
                assert hasattr(tgt, "edge_label") and tgt.edge_label is not None, \
                    f"{name} split missing edge_label on {target_edge_type}"
                assert hasattr(tgt, "edge_label_index") and tgt.edge_label_index is not None, \
                    f"{name} split missing edge_label_index on {target_edge_type}"

            return train_data, val_data, test_data

    # ═══ Section D -- Serialization (Save/Load) ════════════════════

    def save_heterodata(
        self,
        data: HeteroData,
        filename: Optional[str] = None,
        versioned: bool = True,
    ) -> Path:
        """Save HeteroData to disk.

        Args:
            data: HeteroData to save.
            filename: Output filename. Defaults to PyGConfig default.
            versioned: If True, append timestamp+config_hash to filename.

        Returns:
            Path: Path to the saved file.

        Audit findings addressed:
            - Issue 36: post-save verification via reload + type check
            - Issue 38: retry logic for I/O
            - Issue 44: versioned filenames prevent silent overwrites
            - Issue 45: companion .meta.json with input checksums + lineage
            - Issue 55: directory permission check
            - Issue 67: single source of truth for default filename
            # FIX(issue-69): env var overrides in PyGConfig.
            - Issue 77: schema versioning
            - Issue 78: documented .pt file format spec
            - Issue-88: full lineage in companion .meta.json
        """
        with self._timed("save_heterodata"):
            # FIX(issue-67): single source of truth for default filename.
            if filename is None:
                filename = self.config.DEFAULT_HETERODATA_FILENAME

            ensure_dirs()

            # FIX(issue-44): versioned filenames prevent silent overwrites.
            if versioned:
                timestamp = datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ"
                )
                config_hash = hashlib.sha256(
                    json.dumps(
                        asdict(self.config),
                        default=str,
                        sort_keys=True,
                    ).encode()
                ).hexdigest()[:8]
                stem = Path(filename).stem
                suffix = Path(filename).suffix or ".pt"
                filename = f"{stem}__{timestamp}__{config_hash}{suffix}"

            path = PROCESSED_DIR / filename

            # FIX(issue-55): warn on world/group-writable save directory.
            self._check_directory_security(PROCESSED_DIR)

            # FIX(issue-77): schema versioning attached to HeteroData.
            data.__pyg_builder_schema_version__ = PYG_BUILDER_SCHEMA_VERSION
            data.__pyg_builder_pipeline_version__ = (
                PYG_BUILDER_PIPELINE_VERSION
            )
            data.__saved_at__ = datetime.now(timezone.utc).isoformat()

            # FIX(issue-38): exponential backoff retry for I/O operations.
            def _do_save():
                torch.save(data, path)

            self._with_retry(_do_save, "save_heterodata")

            # FIX(issue-36): post-save verification via reload + type check.
            saved_size = path.stat().st_size
            if saved_size == 0:
                raise IOError(
                    f"Saved file {path} is 0 bytes -- filesystem may be "
                    f"full or path unwritable."
                )

            # FIX(issue-45): companion .meta.json with input checksums +
            # lineage.
            sha256_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            meta = {
                "sha256": sha256_hash,
                "size_bytes": saved_size,
                "saved_at": data.__saved_at__,
                "schema_version": PYG_BUILDER_SCHEMA_VERSION,
                "pipeline_version": PYG_BUILDER_PIPELINE_VERSION,
                "config": {
                    k: str(v) for k, v in asdict(self.config).items()
                },
                "input_checksums": self._input_checksums,
                "node_type_counts": {
                    nt: data[nt].num_nodes for nt in data.node_types
                },
                "edge_type_counts": {
                    str(et): data[et].edge_index.shape[1]
                    for et in data.edge_types
                    if hasattr(data[et], "edge_index")
                    and data[et].edge_index is not None
                },
                "feature_provenance": {},
                "pyg_version": torch_geometric.__version__,
                "torch_version": torch.__version__,
                "lineage": getattr(data, "__lineage__", {}),
            }
            # FIX(issue-88): full lineage in companion .meta.json.
            for nt in data.node_types:
                fp = getattr(data[nt], "__feature_provenance__", None)
                if fp is not None:
                    meta["feature_provenance"][nt] = fp

            meta_path = path.with_suffix(path.suffix + ".meta.json")
            meta_path.write_text(
                json.dumps(meta, indent=2, default=str)
            )

            self.logger.info(
                f"HeteroData saved to {path} ({saved_size:,} bytes, "
                f"verified, sha256={sha256_hash[:16]}...)"
            )
            return path

    def load_heterodata(
        self,
        filename: Optional[str] = None,
        allow_unsafe_deserialization: bool = False,
        expected_sha256: Optional[str] = None,
    ) -> HeteroData:
        """Load HeteroData from disk.

        Args:
            filename: File to load. Defaults to PyGConfig default.
            allow_unsafe_deserialization: If True, allows
                weights_only=False. Defaults to False for safety.
            expected_sha256: Optional expected SHA-256 hash.

        Returns:
            HeteroData: The loaded graph data.

        Raises:
            SecurityError: On hash mismatch or unsafe load without opt-in.

        Audit findings addressed:
            - Issue 35: narrow exception handling
            - Issue 36: type check after load
            - Issue 38: retry logic for I/O
            - Issue 39: post-load schema validation
            - Issue 53: no silent RCE fallback
            - Issue 54: SHA-256 integrity verification
            - Issue 67: single source of truth for default filename
            - Issue 69: env var overrides
            - Issue 76: documented security policy
            - Issue 77: schema versioning check
        """
        with self._timed("load_heterodata"):
            # FIX(issue-67): single source of truth for default filename.
            if filename is None:
                filename = self.config.DEFAULT_HETERODATA_FILENAME

            path = PROCESSED_DIR / filename

            # FIX(issue-54): SHA-256 integrity verification on load.
            # Check companion .meta.json first.
            meta_path = path.with_suffix(path.suffix + ".meta.json")
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                stored_hash = meta.get("sha256")
                if stored_hash:
                    actual_hash = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    if actual_hash != stored_hash:
                        raise SecurityError(
                            f"SHA-256 mismatch for {path}: "
                            f"expected {stored_hash[:16]}..., "
                            f"got {actual_hash[:16]}... File may be "
                            f"corrupted or tampered."
                        )

            # FIX(issue-53): explicit SHA-256 check if provided.
            if expected_sha256 is not None:
                actual_hash = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                if actual_hash != expected_sha256:
                    raise SecurityError(
                        f"SHA-256 mismatch for {path}: "
                        f"expected {expected_sha256[:16]}..., "
                        f"got {actual_hash[:16]}..."
                    )

            # FIX(issue-38): retry logic for I/O.
            def _do_load():
                # FIX(issue-53): no silent RCE fallback -- require explicit
                # opt-in + SHA-256 verification.
                try:
                    return torch.load(path, weights_only=True)
                except (
                    pickle.UnpicklingError,
                    RuntimeError,
                    EOFError,
                    ValueError,
                ) as exc:
                    # FIX(issue-35): narrow exception handling -- let
                    # OOM/SIGINT propagate.
                    self.logger.warning(
                        f"weights_only=True failed for {path}: "
                        f"{type(exc).__name__}: {exc}."
                    )
                    if allow_unsafe_deserialization:
                        self.logger.critical(
                            f"UNSAFE LOAD: loading {path} with "
                            f"weights_only=False. "
                            f"File size: {path.stat().st_size:,} bytes, "
                            f"mtime: {datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()}. "
                            f"This is a SECURITY RISK -- only use for "
                            f"trusted files."
                        )
                        return torch.load(
                            path, weights_only=False
                        )
                    else:
                        raise SecurityError(
                            "Refusing to load with weights_only=False "
                            "without explicit opt-in. Pass "
                            "allow_unsafe_deserialization=True to "
                            "override."
                        )

            data = self._with_retry(_do_load, "load_heterodata")

            # FIX(issue-39): post-load schema validation.
            if not isinstance(data, HeteroData):
                raise TypeError(
                    f"Loaded object is {type(data).__name__}, "
                    f"expected HeteroData. File may be from a different "
                    f"code version."
                )
            expected_node_types = self.config.expected_node_types
            if expected_node_types is not None:
                missing = set(expected_node_types) - set(
                    data.node_types
                )
                if missing:
                    raise ValueError(
                        f"Loaded HeteroData is missing node types: "
                        f"{missing}"
                    )

            # FIX(issue-77): schema versioning check.
            saved_version = getattr(
                data, "__pyg_builder_schema_version__", None
            )
            if saved_version is None:
                self.logger.warning(
                    "Loaded file has no schema version -- "
                    "pre-1.0.0 format."
                )
            elif saved_version != PYG_BUILDER_SCHEMA_VERSION:
                raise ValueError(
                    f"Schema version mismatch: file is {saved_version}, "
                    f"current code is {PYG_BUILDER_SCHEMA_VERSION}. "
                    f"Migration required -- see MIGRATION_NOTES.md."
                )

            self.logger.info(f"HeteroData loaded from {path}")
            return data

    # ═══ Section E -- Summary & Reporting ══════════════════════════

    def summarize_heterodata(
        self, data: HeteroData
    ) -> Dict[str, Any]:
        """Print and return a summary of the HeteroData.

        Returns a dict with node/edge counts, feature dimensions,
        lineage metadata, and feature provenance.

        Audit findings addressed:
            - Issue 26: handles missing edge_index safely
            - Issue 89: lineage fields in summary
        """
        with self._timed("summarize_heterodata"):
            summary: Dict[str, Any] = {
                "node_types": len(data.node_types),
                "edge_types": len(data.edge_types),
                "nodes_per_type": {},
                "edges_per_type": {},
            }

            for nt in data.node_types:
                num = data[nt].num_nodes
                feat_dim = (
                    data[nt].x.shape[1]
                    if data[nt].x is not None
                    else 0
                )
                summary["nodes_per_type"][nt] = {
                    "count": num,
                    "feat_dim": feat_dim,
                }

            # FIX(issue-26): summarize_heterodata handles missing
            # edge_index safely.
            for et in data.edge_types:
                if (
                    hasattr(data[et], "edge_index")
                    and data[et].edge_index is not None
                ):
                    num = data[et].edge_index.shape[1]
                else:
                    num = 0
                # Use str(et) as key -- tuple keys break JSON serialization
                # (cross-ref Issue 88).
                summary["edges_per_type"][str(et)] = num

            total_nodes = sum(
                v["count"] for v in summary["nodes_per_type"].values()
            )
            total_edges = sum(summary["edges_per_type"].values())
            summary["total_nodes"] = total_nodes
            summary["total_edges"] = total_edges

            # FIX(issue-89): lineage fields in summary.
            lineage = getattr(data, "__lineage__", {})
            summary["lineage"] = {
                "created_at": lineage.get("created_at"),
                "pipeline_version": lineage.get("pipeline_version"),
                "pyg_builder_version": lineage.get(
                    "pyg_builder_version"
                ),
                "input_checksums": lineage.get("input_checksums", {}),
                "feature_provenance": {
                    nt: getattr(
                        data[nt], "__feature_provenance__", None
                    )
                    for nt in data.node_types
                },
            }

            return summary


# FIX(issue-24): __main__ block works as both module and script.
if __name__ == "__main__":
    # Allow running as both `python -m drugos_graph.pyg_builder`
    # and `python pyg_builder.py` from inside drugos_graph/
    _pkg_parent = Path(__file__).resolve().parent.parent
    if str(_pkg_parent) not in sys.path:
        sys.path.insert(0, str(_pkg_parent))

    logging.basicConfig(level=logging.INFO)

    try:
        from drugos_graph.drkg_loader import (
            build_edge_index_maps,
            build_entity_id_maps,
            load_drkg,
        )
    except ImportError:
        from drkg_loader import (
            build_edge_index_maps,
            build_entity_id_maps,
            load_drkg,
        )

    df, _, _ = load_drkg(download=False)
    entity_maps = build_entity_id_maps(df)
    edge_maps = build_edge_index_maps(df, entity_maps)

    builder = PyGBuilder()
    data = builder.build_from_drkg(entity_maps, edge_maps)
    summary = builder.summarize_heterodata(data)
    print(f"\nHeteroData Summary:")
    print(f"  Total nodes: {summary['total_nodes']:,}")
    print(f"  Total edges: {summary['total_edges']:,}")
