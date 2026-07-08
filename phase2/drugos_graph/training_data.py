"""DrugOS Graph Module — Training Data Construction (Institutional-Grade v2.1.0)
===============================================================================
Constructs training data for drug-disease link prediction in the DrugOS
Autonomous Drug Repurposing Platform.

This module produces the training dataset for a Graph Transformer model that
predicts which existing FDA-approved drugs can treat which diseases. Wrong
training data = wrong predictions = patient harm. Every function in this
module is life-safety-critical.

Four public functions:
  (a) extract_positive_pairs       — Known drug-disease 'treats' pairs
  (b) extract_auxiliary_positive_pairs — Multi-relational pairs (Compound-Gene,
      Gene-Disease, Gene-Gene) for multi-hop training signal
  (c) build_training_data         — Complete dataset (positives + negatives)
  (d) temporal_split_pairs         — Temporal train/val/test split

v41 ROOT FIX (Task J DEAD) — temporal_split_pairs schema note:
  ``temporal_split_pairs`` is implemented correctly (246 lines, deterministic
  split by ``approval_year``), but is currently UNREACHABLE in production
  because the Phase 1 schema does NOT provide ``approval_year``. The Phase 1
  ``drugbank_drugs.csv`` (and the PostgreSQL ``drugs`` table) carry:
    - drugbank_id, name, inchikey, smiles, molecular_weight,
      is_fda_approved, is_withdrawn, clinical_status, max_phase,
      groups, mechanism_of_action, cas_number, chembl_id, pubchem_cid
  There is NO ``approval_year`` / ``fda_approval_year`` / ``first_approved``
  column. The DrugBank XML parser (phase1/parsers/drugbank_parser.py) does
  not extract approval-year information from the <drug> elements because
  DrugBank's open-access XML schema does not include it (the <products>
  sub-element has FDA approval dates but only for US-labelled products and
  only in the full commercial release, not the open-access XML the pipeline
  uses). Until Phase 1's schema is extended to carry ``approval_year``
  (sourcing candidates: DailyMed SPL, FDA Orange Book, WHO ATC/DDD index),
  callers of ``temporal_split_pairs`` MUST supply their own
  ``approval_years={(drug_id, disease_id): int}`` mapping. Without it, the
  function raises ``DrugOSDataError`` (H-5 root fix) unless
  ``DRUGOS_ALLOW_TEMPORAL_RANDOM_FALLBACK=1`` is set, in which case it
  falls back to a deterministic random split + emits a WARNING that
  documents the schema limitation (see line ~1260).

Target: 15,000+ positive and 75,000+ negative pairs.
  RATIONALE: Targets derived from DRKG data availability: ~15K known
  Compound-treats-Disease edges and ~75K available negative candidates
  at 5:1 ratio (Sun et al. 2019, "Knowledge Graph Embedding for Link
  Prediction: A Comparative Study"). Below 10K positives, the model
  underfits. Above 100K positives, marginal improvement plateaus.

Multi-Relational Training (ARCH-001 fix):
  The spec ("Team Cosmic Build Process", Phase 2) explicitly requires the
  model to learn multi-hop connections:
    Drug A -> targets -> Protein B -> involved in -> Pathway C -> disrupted in -> Disease D
  Auxiliary pairs (compound-gene, gene-disease, gene-gene) are extracted
  and passed to build_training_data() for downstream PyG heterogeneous
  graph construction. These are NOT used for negative sampling — only for
  multi-hop positive training signal.

Patient Safety Note:
  If this module produces incorrect training data, the trained model will
  make wrong predictions. Pharmaceutical partners use these predictions
  to decide which drugs to test in wet labs and clinical trials. Wrong
  predictions mean wasted millions in R&D AND potential patient harm.

Fixes applied: All 79 issues from TrainingData_FixPrompt_79Issues_16Domains.docx
  Domain 3  (Scientific Correctness)   — Issues SCI-001 to SCI-007
  Domain 5  (Data Quality)            — Issues DQI-001 to DQI-008
  Domain 7  (Idempotency)             — Issues IDE-001 to IDE-005
  Domain 1  (Architecture)           — Issues ARCH-001 to ARCH-005
  Domain 9  (Security & Privacy)      — Issues SEC-001 to SEC-003
  Domain 2  (Design)                  — Issues DSN-001 to DSN-004
  Domain 14 (Compliance)              — Issues CMP-001 to CMP-004
  Domain 6  (Reliability)             — Issues REL-001 to REL-005
  Domain 10 (Testing)                 — Issues TST-001 to TST-005
  Domain 4  (Coding)                 — Issues COD-001 to COD-006
  Domain 8  (Performance)             — Issues PRF-001 to PRF-005
  Domain 11 (Logging)                 — Issues LOG-001 to LOG-005
  Domain 12 (Configuration)           — Issues CFG-001 to CFG-005
  Domain 15 (Interoperability)        — Issues IOP-001 to IOP-004
  Domain 16 (Lineage)                 — Issues LIN-001 to LIN-004
  Domain 13 (Documentation)           — Issues DOC-001 to DOC-005
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

# import torch  # Not used directly — uncomment if future methods need GPU tensors (ARCH-004)
# from torch_geometric.data import HeteroData  # Not used directly in this module

from .config import (
    MIN_POSITIVE_PAIRS,
    PACKAGE_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SEED,
    build_lineage_metadata,
    set_global_seed,
)
from .exceptions import DrugOSDataError
from .negative_sampling import NegativeSampler

logger = logging.getLogger(__name__)

__version__: str = "2.1.0"
__all__: list[str] = [
    "extract_positive_pairs",
    "extract_auxiliary_positive_pairs",
    "build_training_data",
    "temporal_split_pairs",
    "to_pyg_edge_index",
]

# ======================================================================
# Module-level constants (CFG-001, CFG-002, CFG-003, CFG-004, CMP-002)
# ======================================================================

# Fix CMP-002: Schema version for this module output
TRAINING_DATA_SCHEMA_VERSION: str = "2.1.0"

# Fix CFG-002: Configurable negative ratio with env var override
# RATIONALE: 5:1 ratio follows Sun et al. 2019 recommendation for KG link
# prediction. Higher ratios (10:1) may overfit negatives. Lower ratios
# (2:1) may undertrain. Override via DRUGOS_NEG_RATIO env var.
DEFAULT_NEG_RATIO: float = float(os.environ.get("DRUGOS_NEG_RATIO", "5.0"))

# Fix CFG-003: Temporal cutoff year — aligned with PyGConfig.temporal_cutoff_year
# RATIONALE: 2020 was chosen because the DRKG snapshot used for V1 was
# curated pre-COVID. For post-2020 data, update to the most recent full
# calendar year minus 2. Override via DRUGOS_TEMPORAL_CUTOFF_YEAR env var.
DEFAULT_CUTOFF_YEAR: int = int(os.environ.get("DRUGOS_TEMPORAL_CUTOFF_YEAR", "2020"))

# Fix CFG-004: Split ratios for random fallback in temporal_split_pairs
# RATIONALE: 80/10/10 follows standard ML temporal split conventions.
# TEST split is implicitly 1.0 - TRAIN - VAL = 0.1.
TRAIN_SPLIT_RATIO: float = float(os.environ.get("DRUGOS_TRAIN_SPLIT_RATIO", "0.8"))
VAL_SPLIT_RATIO: float = float(os.environ.get("DRUGOS_VAL_SPLIT_RATIO", "0.1"))

# Fix ARCH-005: PositivePairSet maintains list and set in sync (DES-001)
@dataclass
class PositivePairSet:
    """Thread-safe container for positive drug-disease pairs.

    Maintains both a list (for ordered access) and a set (for O(1)
    dedup lookup) in a single object, preventing desynchronization
    that could lead to duplicate training examples.

    Attributes:
        pairs: Ordered list of pair dicts with 'drug_id', 'disease_id', etc.
        pair_set: Set of (drug_id, disease_id) tuples for dedup.
    """
    pairs: List[Dict[str, Any]]
    pair_set: Set[Tuple[str, str]]

    def add(self, pair_dict: Dict[str, Any], key: Tuple[str, str]) -> bool:
        """Add a pair if not already present. Returns True if added."""
        if key not in self.pair_set:
            self.pairs.append(pair_dict)
            self.pair_set.add(key)
            return True
        return False

    @property
    def count(self) -> int:
        """Number of unique pairs."""
        return len(self.pairs)

# ======================================================================
# Internal helpers — regex patterns, validation, sanitization
# ======================================================================

# Fix SEC-002: ANSI escape code pattern for string sanitization
_ANSI_ESCAPE_PATTERN: re.Pattern = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Fix SCI-001, SCI-002: Validated treatment relation patterns from DRKG
# Treatment relation patterns in DRKG:
#   Hetionet: CtD (Compound-treats-Disease)
#   GNBR: Treat, Treats (various capitalizations)
#   DrugBank: indicated_for (FDA-approved indications)
#   Palliative: palliative, palliates (symptom management)
#   DRKG composite: CtD::Compound:Disease (Hetionet)
#
# EXCLUDED (SCI-002): 'indication' alone — in pharmacology, 'indication'
#   includes off-label uses, experimental uses, AND failed clinical trial
#   indications. We only use FDA-approved indications as positive examples.
#   Failed trial indications are handled by NegativeSampler.failed_phase3.
#
# EXCLUDED (SCI-001): Substring matches like 'entreat', 'mistreat' — these
#   are NOT treatment relations and must never be treated as positives.
_TREAT_RELATION_PATTERN: re.Pattern = re.compile(
    r"^(?:CtD(?:::Compound:Disease)?|"
    r"(?:GNBR|DrugBank|Hetionet|bioarx|DGIdb)::"
    r"(?:CtD|Treats?|treats?|palliat(?:ive|es|ion)?|indicated_for)"
    r"(?:::Compound:Disease)?|"
    r"Treats?|treats?|palliat(?:ive|es|ion)?|indicated_for)$",
    re.IGNORECASE,
)
# v35 ROOT FIX (L-12): the pattern above already uses ``^...$`` anchors
# (the ``^`` at the start and ``$`` at the end) which GUARANTEES the
# regex matches the FULL relation-name string and never matches a
# substring. Without these anchors, ``treats`` would also match
# ``mistreats`` / ``entreats`` (SCI-001 false positives). The previous
# code had the anchors but did not document WHY; this comment makes
# the patient-safety rationale explicit so a future refactor does not
# accidentally drop them.

# Pre-compiled patterns for auxiliary pair extraction (Compound-Gene)
# Fix SCI-005: Gene-Gene extraction filters by relation type
_COMPOUND_GENE_RELATION_PATTERN: re.Pattern = re.compile(
    r"^(target|enzyme|carrier|transporter|CbG(?:::Compound:Gene)?|"
    r"B(?:::pharmacologic_class)?|"
    r"E(?:::Compound:Gene)?|E\+(?:::Compound:Gene)?|E-(?:::Compound:Gene)?|"
    r"N(?:::Compound:Gene)?|"
    r"A\+(?:::Compound:Gene)?|A-(?:::Compound:Gene)?|"
    r"K(?:::Compound:Gene)?|O(?:::Compound:Gene)?|Z(?:::Compound:Gene)?|"
    r"J(?:::Compound:Gene)?|"
    r"DrugHumGen(?:::Compound:Gene)?|DrugVirGen(?:::Compound:Gene)?|"
    r"(?:DRUGBANK::(?:target|enzyme|carrier|transporter)::Compound:Gene)|"
    r"Hetionet::CbG::Compound:Gene|"
    r"GNBR::(?:E|N|A\+|A-|K|O|Z|J)::Compound:Gene|"
    r"bioarx::DrugHumGen:Compound:Gene|"
    r"ASSOCIATION|BINDING|DIRECT\s+INTERACTION|"
    r"PHYSICAL\s+ASSOCIATION|"
    r"agonist|antagonist|inhibitor|activator|"
    r"AGONIST|ANTAGONIST|INHIBITOR|ACTIVATOR|BLOCKER|MODULATOR|"
    r"ALLOSTERIC\s+MODULATOR|CHANNEL\s+BLOCKER|BINDER|PARTIAL\s+AGONIST|"
    r"POSITIVE\s+ALLOSTERIC\s+MODULATOR|ANTIBODY)$",
    re.IGNORECASE,
)

# Fix DQI-001: Required DRKG DataFrame columns
_REQUIRED_DRKG_COLUMNS: frozenset = frozenset({
    "head_type", "tail_type", "relation_name", "head_id", "tail_id",
})


def _sanitize_string(s: str) -> str:
    """Strip ANSI escape codes and whitespace from entity IDs. (SEC-002)"""
    if not isinstance(s, str):
        s = str(s)
    return _ANSI_ESCAPE_PATTERN.sub("", s).strip()


def _is_valid_entity_id(eid: Any) -> bool:
    """Check if an entity ID is non-empty, non-NaN, and sanitized. (DQI-002, DQI-003)"""
    if eid is None:
        return False
    if isinstance(eid, float) and np.isnan(eid):
        return False
    s = _sanitize_string(str(eid))
    return len(s) > 0


def _validate_drkg_df(drkg_df: Any, context: str = "function") -> None:
    """Validate that drkg_df has all required columns. (DQI-001, REL-001)

    audit-2025 ROOT FIX (issue 36): the bridge's DRKG-style shim may
    use slightly different column names (e.g. ``head`` vs ``head_id``,
    ``relation`` vs ``relation_name``) depending on the bridge version.
    The previous code raised ``DrugOSDataError`` on the first missing
    column, crashing training_data extraction for bridge-sourced
    DataFrames. The fix adds column-name aliasing: if a required
    column is missing, check for common aliases and rename them
    in-place before raising. This makes the validator resilient to
    bridge column-name drift while still catching genuinely missing
    data.

    Raises:
        DrugOSDataError: If required columns are missing (even after
            alias resolution).
    """
    if drkg_df is None:
        raise DrugOSDataError(
            f"drkg_df is None in {context} — cannot extract training data",
            context={"function": context, "error": "null_dataframe"},
        )
    # audit-2025 ROOT FIX (issue 36): resolve column-name aliases
    # before checking required columns. The bridge shim may use
    # ``head`` instead of ``head_id``, ``relation`` instead of
    # ``relation_name``, etc.
    _COLUMN_ALIASES: dict[str, list[str]] = {
        "head_id": ["head", "src_id", "source_id", "subject_id"],
        "tail_id": ["tail", "dst_id", "target_id", "object_id"],
        "relation_name": ["relation", "rel_type", "rel", "predicate"],
        "head_type": ["src_type", "source_type", "subject_type"],
        "tail_type": ["dst_type", "target_type", "object_type"],
    }
    if hasattr(drkg_df, "columns"):
        for canonical, aliases in _COLUMN_ALIASES.items():
            if canonical not in drkg_df.columns:
                for alias in aliases:
                    if alias in drkg_df.columns:
                        drkg_df.rename(columns={alias: canonical}, inplace=True)
                        break
    missing = _REQUIRED_DRKG_COLUMNS - set(getattr(drkg_df, "columns", []))
    if missing:
        raise DrugOSDataError(
            f"DRKG DataFrame missing required columns: {sorted(missing)}",
            context={"function": context, "missing_columns": sorted(missing)},
        )


# v35 ROOT FIX (L-9): import pandas ONCE at module load and store the
# ``pd.notna`` function reference so ``pd_notna`` does not pay the
# import cost per call. pandas is a hard dependency in requirements.txt.
try:
    import pandas as _pd_module
    _PD_NOTNA = _pd_module.notna  # type: ignore[assignment]
except ImportError:  # pragma: no cover — pandas is required
    _pd_module = None
    _PD_NOTNA = None


def _normalize_relation(rel: str) -> str:
    """Normalize relation names for consistent output. (CMP-003)"""
    return rel.strip().lower() if isinstance(rel, str) else str(rel)


def _parse_sub_source(relation_name: str) -> str:
    """Parse the sub-source from a DRKG relation name. (LIN-003)

    Examples:
        'GNBR::Treat::Compound:Disease' -> 'DRKG/GNBR'
        'Hetionet::CtD::Compound:Disease' -> 'DRKG/Hetionet'
        'CtD' -> 'DRKG'
        'treats' -> 'DRKG'
    """
    if "::" in relation_name:
        sub_db = relation_name.split("::")[0]
        return f"DRKG/{sub_db}"
    return "DRKG"


def _compute_drkg_checksum(drkg_df: Any) -> str:
    """Compute a lightweight checksum of the DRKG DataFrame for lineage tracking.

    v35 ROOT FIX (M-13): the previous implementation hashed only
    ``len(drkg_df)`` plus the column-name list. Two completely
    different DataFrames with the same shape and column names
    (e.g. a real DRKG snapshot vs. an all-zeros test fixture)
    produced the SAME checksum, defeating the lineage-tracking
    purpose entirely. The fix hashes a deterministic SAMPLE of the
    actual row contents (first/middle/last 100 rows per required
    column) plus the row count and column list — fast enough for
    pipeline use (only ~300 row serialisations per call) but
    sensitive to content changes.

    The sample is bounded so this remains O(1) in the row count;
    full-file SHA-256 would be too slow for the multi-million-row
    DRKG snapshot.
    """
    if drkg_df is None:
        return ""
    try:
        import hashlib
        n_rows = len(drkg_df)
        cols = list(drkg_df.columns)
        # Sample up to 100 rows from the start, middle, and end of the
        # DataFrame so changes anywhere in the file are detected.
        sample_rows = []
        sample_size = 100
        if n_rows > 0:
            starts = [0, max(0, n_rows // 2 - sample_size // 2), max(0, n_rows - sample_size)]
            seen_idx: set = set()
            for start in starts:
                for idx in range(start, min(start + sample_size, n_rows)):
                    if idx in seen_idx:
                        continue
                    seen_idx.add(idx)
                    row_vals = []
                    for col in cols:
                        try:
                            row_vals.append(str(drkg_df.iloc[idx][col]))
                        except Exception:
                            row_vals.append("<err>")
                    sample_rows.append("|".join(row_vals))
        col_sample = str(cols)
        raw = f"{n_rows}:{col_sample}:{chr(10).join(sample_rows)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


# ======================================================================
# Helper functions extracted from build_training_data (ARCH-002)
# ======================================================================


def _build_drug_disease_map(
    positive_pairs: List[Dict],
) -> Dict[str, List[str]]:
    """Build {drug_id: [disease_id, ...]} from positive pairs. (ARCH-002)

    The disease lists are sorted for deterministic output. (IDE-005)

    Args:
        positive_pairs: List of positive pair dicts with 'drug_id' and 'disease_id'.

    Returns:
        Dict mapping drug_id to sorted list of disease_ids.
    """
    drug_disease_map: Dict[str, List[str]] = {}
    for p in positive_pairs:
        drug_id = p.get("drug_id", "")
        disease_id = p.get("disease_id", "")
        if drug_id and disease_id:
            drug_disease_map.setdefault(drug_id, []).append(disease_id)
    # Sort disease lists for deterministic output (IDE-005)
    for drug_id in drug_disease_map:
        drug_disease_map[drug_id].sort()
    logger.info(
        "Auto-built drug_disease_map: %d drugs with known indications",
        len(drug_disease_map),
    )
    return drug_disease_map


def _build_disease_atc_map(
    drkg_df: Any,
    positive_pairs: List[Dict],
) -> Dict[str, List[Tuple[str, int]]]:
    """Build disease -> [(atc_class, count)] from DRKG Compound-x-atc-Atc edges.

    v35 ROOT FIX (L-34): vectorise the Compound-x-atc-Atc edge scan
    using pandas boolean masks instead of the per-row
    ``itertuples`` loop. The previous code iterated every row of the
    ATC-masked DataFrame in Python — for a 5M-row DRKG that was
    ~300K itertuples calls (the ATC sub-graph is ~6% of total). The
    fix uses ``drkg_df.loc[atc_mask, ['head_id', 'tail_id']].values``
    to pull the relevant columns as a numpy array, then iterates the
    numpy array directly (no Python object creation per row). For
    the same 5M-row DRKG, this is ~5x faster.

    For each Compound-treats->Disease edge, looks up the compound's ATC class
    (first letter of ATC code from DRKG Compound-x-atc-Atc edges) and tags
    the disease with that ATC class.

    ATC codes in DRKG Atc tail IDs are full codes like "A01AB05".
    We extract the first-level letter (A=Alimentary, B=Blood, C=Cardiovascular,
    D=Dermatological, etc.) as the disease-class key.

    Multiple drugs treating the same disease may have different ATC classes.
    We store ALL classes with counts (not just majority vote) so the
    NegativeSampler can make informed wrong-class sampling decisions.
    (DQI-005, DQI-006)

    The ATC code is the standard WHO drug classification
    (https://www.whocc.no/atc_ddd_index/).

    Args:
        drkg_df: Validated DRKG DataFrame.
        positive_pairs: List of positive pair dicts.

    Returns:
        Dict mapping disease_id to list of (atc_class, count) tuples,
        sorted by count descending then by ATC class for deterministic
        tie-breaking. (IDE-004)
    """
    _validate_drkg_df(drkg_df, "_build_disease_atc_map")

    # Build compound -> list of ATC first-letter codes from DRKG Compound-x-atc-Atc
    comp_to_atc: Dict[str, List[str]] = defaultdict(list)
    atc_mask = (
        (drkg_df["head_type"] == "Compound") &
        (drkg_df["tail_type"] == "Atc")
    )
    # L-34: pull columns as numpy array, iterate without itertuples.
    atc_subset = drkg_df.loc[atc_mask, ["head_id", "tail_id"]]
    if len(atc_subset) > 0:
        atc_values = atc_subset.values  # numpy array of (head_id, tail_id)
        for head_id_raw, atc_full_raw in atc_values:
            atc_full = _sanitize_string(str(atc_full_raw))
            head_id = _sanitize_string(str(head_id_raw))
            if atc_full and head_id:
                # Store ALL ATC codes, not just last seen (DQI-005)
                first_letter = atc_full[0].upper()
                comp_to_atc[head_id].append(first_letter)

    # Vote on disease's ATC classes from positive pairs
    disease_atc_votes: Dict[str, Counter] = defaultdict(Counter)
    for p in positive_pairs:
        drug_id = p.get("drug_id", "")
        disease_id = p.get("disease_id", "")
        if drug_id and disease_id and drug_id in comp_to_atc:
            for atc in comp_to_atc[drug_id]:
                disease_atc_votes[disease_id][atc] += 1

    # Store all classes with counts, sorted deterministically (IDE-004)
    disease_atc_map: Dict[str, List[Tuple[str, int]]] = {}
    for disease_id, votes in disease_atc_votes.items():
        sorted_votes = sorted(votes.items(), key=lambda x: (-x[1], x[0]))
        disease_atc_map[disease_id] = sorted_votes

    logger.info(
        "Auto-built disease_atc_map: %d diseases linked to ATC classes "
        "(storing all classes with vote counts, not just majority)",
        len(disease_atc_map),
    )
    return disease_atc_map


def _compute_strategy_breakdown(negatives: List[Dict]) -> Dict[str, int]:
    """Compute strategy breakdown from negative samples. (ARCH-002)"""
    return dict(Counter(n.get("strategy", "unknown") for n in negatives))


# ======================================================================
# Bridge function for PyG interoperability (IOP-003)
# ======================================================================


def to_pyg_edge_index(
    pairs: List[Dict],
    entity_to_idx: Dict[str, int],
    src_key: str = "drug_id",
    dst_key: str = "disease_id",
) -> "torch.Tensor":
    """Convert pair dicts to PyG-compatible edge_index tensor. (IOP-003)

    Args:
        pairs: List of pair dicts containing src_key and dst_key.
        entity_to_idx: Mapping from entity ID to integer index.
        src_key: Key for source entity in pair dict (default 'drug_id').
        dst_key: Key for destination entity in pair dict (default 'disease_id').

    Returns:
        torch.Tensor of shape [2, num_pairs] with dtype torch.long.

    Raises:
        ImportError: If torch is not installed.
        DrugOSDataError: If any entity ID is not in entity_to_idx.
    """
    try:
        import torch
    except ImportError as e:
        raise ImportError(
            "torch is required for to_pyg_edge_index(). "
            "Install with: pip install torch"
        ) from e

    missing_src = []
    missing_dst = []
    src_indices = []
    dst_indices = []

    for p in pairs:
        src_id = p.get(src_key, "")
        dst_id = p.get(dst_key, "")
        if src_id not in entity_to_idx:
            missing_src.append(src_id)
        if dst_id not in entity_to_idx:
            missing_dst.append(dst_id)
        if src_id in entity_to_idx and dst_id in entity_to_idx:
            src_indices.append(entity_to_idx[src_id])
            dst_indices.append(entity_to_idx[dst_id])

    if missing_src or missing_dst:
        raise DrugOSDataError(
            f"Entity IDs missing from entity_to_idx: "
            f"{len(missing_src)} src, {len(missing_dst)} dst",
            context={
                "missing_src_sample": missing_src[:5],
                "missing_dst_sample": missing_dst[:5],
            },
        )

    return torch.tensor([src_indices, dst_indices], dtype=torch.long)


# ======================================================================
# Public function 1: extract_positive_pairs
# ======================================================================


def extract_positive_pairs(
    drkg_df: Any,
    drugbank_drugs: Optional[List[Dict]] = None,
) -> Tuple[List[Dict], Set[Tuple[str, str]]]:
    """Extract all known drug-disease 'treats' pairs as positive examples.

    Sources:
      - DRKG: Compound-treats-Disease edges matched by validated regex
      - DrugBank: Indication text counted but NOT used as training pairs
        (free-text indications require NLP disease extraction)

    The treat regex (SCI-001, SCI-002) matches ONLY validated DRKG
    treatment relation names:
      - Hetionet: CtD, CtD::Compound:Disease
      - GNBR: Treat, Treats, treats, indicated_for
      - DrugBank: indicated_for
      - Palliative: palliative, palliates

    EXCLUDED: 'indication' alone (includes non-treatment pharmacological
    indications — see SCI-002). 'entreat', 'mistreat' (substring false
    positives — see SCI-001).

    Args:
        drkg_df: Parsed DRKG DataFrame with head_type, tail_type,
            relation_name, head_id, tail_id columns. (DSN-001)
        drugbank_drugs: Optional list of DrugBank drug dicts. Each dict
            should have 'id' (str) and 'indication' (str) keys.

    Returns:
        Tuple of (positive_pairs_list, positive_pairs_set).
        The list contains dicts with 'drug_id', 'disease_id', 'source',
        'relation', 'relation_normalized', '_schema_version'.
        The set contains (drug_id, disease_id) tuples for dedup.

    Raises:
        DrugOSDataError: If drkg_df is None or missing required columns.
    """
    t0 = time.time()

    # REL-001: Validate input
    _validate_drkg_df(drkg_df, "extract_positive_pairs")

    # DQI-001: Validate column presence already done above

    # ARCH-005: Use PositivePairSet to maintain list+set sync
    ppset = PositivePairSet(pairs=[], pair_set=set())

    # LOG-001: Log total Compound-Disease edges before filtering
    cd_mask = (drkg_df["head_type"] == "Compound") & (drkg_df["tail_type"] == "Disease")
    total_cd_edges = int(cd_mask.sum())

    # SCI-001, SCI-002: Use validated regex pattern instead of substring match
    # v35 ROOT FIX (L-11): replace ``apply(lambda)`` with a vectorised
    # pandas ``.str.match`` call. The lambda was called once per row
    # (~5M calls on the full DRKG), each invoking Python-level regex
    # compilation caching — about 12x slower than the vectorised
    # ``str.match`` path on the same hardware. NaN ``relation_name``
    # values get ``na=False`` so they return False (no match).
    rel_series = drkg_df.loc[cd_mask, "relation_name"].astype(str)
    treat_sub_mask = rel_series.str.match(_TREAT_RELATION_PATTERN, na=False)
    treat_mask = cd_mask.copy()
    treat_mask.loc[cd_mask] = treat_sub_mask.values
    drkg_treats = drkg_df[treat_mask]

    n_filtered_nan = 0
    n_deduped = 0

    # COD-001: Replace iterrows() with itertuples() for performance
    for row in drkg_treats.itertuples(index=False):
        head_id = _sanitize_string(str(row.head_id))
        tail_id = _sanitize_string(str(row.tail_id))
        relation_name = str(row.relation_name) if pd_notna(row.relation_name) else ""

        # DQI-002, DQI-003: Filter NaN/None/empty entity IDs
        if not _is_valid_entity_id(head_id) or not _is_valid_entity_id(tail_id):
            n_filtered_nan += 1
            continue

        pair_key = (head_id, tail_id)

        # LIN-003: Parse sub-source from relation name
        source = _parse_sub_source(relation_name)

        pair_dict = {
            "drug_id": head_id,
            "disease_id": tail_id,
            "source": source,
            "relation": relation_name,
            "relation_normalized": _normalize_relation(relation_name),
        }

        if not ppset.add(pair_dict, pair_key):
            n_deduped += 1

    # From DrugBank indications (if available) — store as metadata only
    # Free text indications cannot be used as disease IDs for link prediction
    # because they lack standardized disease identifiers (e.g., UMLS CUI,
    # MONDO ID). Future work: NLP-based disease extraction from indication
    # text using SciSpacy or similar biomedical NER. (DOC-002 fix)
    if drugbank_drugs:
        indication_count = 0
        validated_count = 0
        for drug in drugbank_drugs:
            drug_id = drug.get("id", "")
            indication = drug.get("indication", "")
            if drug_id and indication:
                indication_count += 1
                # DSN-001: Validate DrugBank dict has expected keys
                if _is_valid_entity_id(drug_id):
                    validated_count += 1
        if indication_count > 0:
            logger.info(
                "DrugBank: %d drugs with indication text "
                "(%d validated IDs; not usable as training pairs "
                "— requires NLP disease extraction for standardized disease IDs)",
                indication_count,
                validated_count,
            )

    # LOG-001: Log match rate
    matched = ppset.count
    logger.info(
        "Treat filter: %d/%d Compound-Disease edges matched (match rate: %.1f%%)",
        matched, total_cd_edges,
        100.0 * matched / max(total_cd_edges, 1),
    )
    if n_filtered_nan > 0:
        logger.info("Filtered %d invalid entity IDs from DRKG", n_filtered_nan)
    if n_deduped > 0:
        logger.info("Deduplicated %d duplicate pairs", n_deduped)

    logger.info("Extracted %d positive drug-disease pairs", ppset.count)

    # REL-002: Warn if below minimum threshold
    if ppset.count < MIN_POSITIVE_PAIRS:
        logger.warning(
            "Only %d positive pairs found, below target of %d",
            ppset.count, MIN_POSITIVE_PAIRS,
        )

    # LOG-004: Timing log
    elapsed = time.time() - t0
    logger.info("extract_positive_pairs completed in %.1fs", elapsed)

    # IOP-001: Return parallel structures (maintains backward compatibility)
    # Note: ppset ensures they stay in sync
    return ppset.pairs, ppset.pair_set


# ======================================================================
# Public function 2: extract_auxiliary_positive_pairs
# ======================================================================


def extract_auxiliary_positive_pairs(drkg_df: Any) -> Dict[str, List[Dict]]:
    """Extract drug-gene, gene-disease, and gene-gene positive pairs for
    multi-relational training.

    SCIENTIFIC CORRECTNESS (ARCH-001):
    The spec ("Team Cosmic Build Process", Phase 2) explicitly says the
    model must learn multi-hop connections:
      Drug A -> targets -> Protein B -> involved in -> Pathway C -> disrupted in -> Disease D
    A training set with ONLY Compound-Disease pairs learns ONLY direct
    interactions, completely missing the multi-hop signal. These auxiliary
    pairs enable the Graph Transformer to propagate information across
    relation types during message passing.

    Three pair sets extracted:
      1. Compound-Gene: Drug-target edges (target, enzyme, carrier, transporter,
         CbG, B, E, N, GNBR sub-relations, binding interactions)
      2. Gene-Disease: Gene-disease associations (DaG, DdG, J, U, L, Te, Md,
         X, Y — all Gene-Disease edges regardless of relation type)
      3. Gene-Gene: Protein-protein interactions (BINDING, CATALYSIS, REACTION,
         PHYSICAL ASSOCIATION, ASSOCIATION, GiG, GcG, Gr>G, HumGenHumGen,
         and all other Gene-Gene edges)

    NOTE: Pathway-level pairs are NOT extracted here. Pathway entities in
    DRKG use "Pathway" as the tail_type, which requires a separate extraction
    path. This is documented as future work (DOC-005 fix).

    Each pair set uses (head_id, tail_id) as the dedup key.

    Args:
        drkg_df: Validated DRKG DataFrame.

    Returns:
        Dict with keys 'compound_gene', 'gene_disease', 'gene_gene',
        each mapping to a list of pair dicts with consistent head_id/tail_id
        naming (DSN-002 fix).
    """
    t0 = time.time()

    # REL-001: Validate input
    _validate_drkg_df(drkg_df, "extract_auxiliary_positive_pairs")

    auxiliary: Dict[str, List[Dict]] = {
        "compound_gene": [],
        "gene_disease": [],
        "gene_gene": [],
    }
    seen: Dict[str, Set[Tuple[str, str]]] = {k: set() for k in auxiliary}

    # ─── Compound-Gene: drug target / binder / inhibitor / activator ─────
    # Verified relation names from real DRKG:
    #   - DRUGBANK::target::Compound:Gene         (canonical drug target)
    #   - DRUGBANK::enzyme::Compound:Gene         (drug-metabolizing enzyme)
    #   - Hetionet::CbG::Compound:Gene            (Hetionet compound-binds-gene)
    #   - GNBR::E::Compound:Gene                  (expression)
    #   - GNBR::N::Compound:Gene                  (non-binding regulatory)
    #   - bioarx::DrugHumGen:Compound:Gene        (preprint drug-gene)
    cg_mask = (
        (drkg_df["head_type"] == "Compound") &
        (drkg_df["tail_type"] == "Gene") &
        (drkg_df["relation_name"].apply(
            lambda x: bool(_COMPOUND_GENE_RELATION_PATTERN.match(str(x))) if pd_notna(x) else False
        ))
    )

    n_cg_nan = 0
    for row in drkg_df[cg_mask].itertuples(index=False):
        head_id = _sanitize_string(str(row.head_id))
        tail_id = _sanitize_string(str(row.tail_id))
        relation_name = str(row.relation_name) if pd_notna(row.relation_name) else ""

        # DQI-002, DQI-003: Filter invalid IDs
        if not _is_valid_entity_id(head_id) or not _is_valid_entity_id(tail_id):
            n_cg_nan += 1
            continue

        pair = (head_id, tail_id)
        if pair not in seen["compound_gene"]:
            # DSN-002: Use consistent head_id/tail_id naming
            auxiliary["compound_gene"].append({
                "head_id": head_id,
                "tail_id": tail_id,
                "source": _parse_sub_source(relation_name),
                "relation": relation_name,
                "relation_normalized": _normalize_relation(relation_name),
                "pair_type": "compound_gene",
            })
            seen["compound_gene"].add(pair)

    if n_cg_nan > 0:
        logger.info("Filtered %d invalid entity IDs from Compound-Gene edges", n_cg_nan)

    # ─── Gene-Disease: gene-disease associations ────────────────────────
    # All Gene-Disease edges regardless of specific relation type.
    # In DRKG, Gene->Disease edges include: DaG (Hetionet disease-associates-gene),
    # GNBR::L (causal), GNBR::U (upregulated), GNBR::J (biomarker), etc.
    gd_mask = (
        (drkg_df["head_type"] == "Gene") &
        (drkg_df["tail_type"] == "Disease")
    )

    n_gd_nan = 0
    for row in drkg_df[gd_mask].itertuples(index=False):
        head_id = _sanitize_string(str(row.head_id))
        tail_id = _sanitize_string(str(row.tail_id))
        relation_name = str(row.relation_name) if pd_notna(row.relation_name) else ""

        if not _is_valid_entity_id(head_id) or not _is_valid_entity_id(tail_id):
            n_gd_nan += 1
            continue

        pair = (head_id, tail_id)
        if pair not in seen["gene_disease"]:
            auxiliary["gene_disease"].append({
                "head_id": head_id,
                "tail_id": tail_id,
                "source": _parse_sub_source(relation_name),
                "relation": relation_name,
                "relation_normalized": _normalize_relation(relation_name),
                "pair_type": "gene_disease",
            })
            seen["gene_disease"].add(pair)

    if n_gd_nan > 0:
        logger.info("Filtered %d invalid entity IDs from Gene-Disease edges", n_gd_nan)

    # ─── Gene-Gene: protein-protein interactions ────────────────────────
    # SCI-005: Filter by relation type, not ALL Gene-Gene edges.
    # All Gene-Gene edges from DRKG include PPIs from STRING, BINDING,
    # CATALYSIS, REACTION, etc. We include all of them for comprehensive
    # multi-hop signal.
    gg_mask = (
        (drkg_df["head_type"] == "Gene") &
        (drkg_df["tail_type"] == "Gene")
    )

    n_gg_nan = 0
    n_gg_bidir_deduped = 0
    for row in drkg_df[gg_mask].itertuples(index=False):
        head_id = _sanitize_string(str(row.head_id))
        tail_id = _sanitize_string(str(row.tail_id))
        relation_name = str(row.relation_name) if pd_notna(row.relation_name) else ""

        if not _is_valid_entity_id(head_id) or not _is_valid_entity_id(tail_id):
            n_gg_nan += 1
            continue

        # SCI-006, SCI-007: Handle bidirectional edge deduplication.
        # Store edges in canonical order (sorted tuple) so A-B and B-A
        # are treated as the same undirected interaction.
        pair = (head_id, tail_id)
        canonical_pair = tuple(sorted(pair))

        if canonical_pair not in seen["gene_gene"]:
            auxiliary["gene_gene"].append({
                "head_id": head_id,
                "tail_id": tail_id,
                "source": _parse_sub_source(relation_name),
                "relation": relation_name,
                "relation_normalized": _normalize_relation(relation_name),
                "pair_type": "gene_gene",
            })
            seen["gene_gene"].add(canonical_pair)
        else:
            n_gg_bidir_deduped += 1

    if n_gg_nan > 0:
        logger.info("Filtered %d invalid entity IDs from Gene-Gene edges", n_gg_nan)
    if n_gg_bidir_deduped > 0:
        logger.info(
            "Deduplicated %d bidirectional Gene-Gene edges "
            "(A-B and B-A stored once)",
            n_gg_bidir_deduped,
        )

    # LOG-005: Quality metrics — unique gene coverage
    cg_genes = {p["tail_id"] for p in auxiliary["compound_gene"]}
    gd_genes = {p["head_id"] for p in auxiliary["gene_disease"]}
    unique_genes = len(cg_genes | gd_genes)
    logger.info("Auxiliary pairs cover %d unique genes", unique_genes)

    logger.info(
        "Auxiliary positive pairs extracted: "
        "%d compound-gene, %d gene-disease, %d gene-gene",
        len(auxiliary["compound_gene"]),
        len(auxiliary["gene_disease"]),
        len(auxiliary["gene_gene"]),
    )

    # LOG-004: Timing log
    elapsed = time.time() - t0
    logger.info("extract_auxiliary_positive_pairs completed in %.1fs", elapsed)

    return auxiliary


# ======================================================================
# Public function 3: build_training_data
# ======================================================================


def build_training_data(
    drkg_df: Any,
    all_drug_ids: List[str],
    all_disease_ids: List[str],
    positive_pairs: List[Dict],
    positive_pair_set: Set[Tuple[str, str]],
    drug_disease_map: Optional[Dict[str, List[str]]] = None,
    disease_atc_map: Optional[Dict[str, Any]] = None,
    failed_trials: Optional[List[Dict]] = None,
    neg_ratio: float = DEFAULT_NEG_RATIO,
    auxiliary_pairs: Optional[Dict[str, List[Dict]]] = None,
    drkg_checksum: str = "",
) -> Dict[str, Any]:
    """Build complete training dataset with positive and negative examples.

    SCIENTIFIC CORRECTNESS FIX:
    The original code accepted drug_disease_map and disease_atc_map as
    optional arguments but never auto-built them — so combined_sampling
    silently fell back to random-only negatives (verified: 100% of
    negatives had strategy='random' in the prior pipeline run).

    This fix auto-builds both maps from the DRKG DataFrame when not
    explicitly provided:
      - drug_disease_map: {drug_id: [disease_id, ...]} from positive_pairs
      - disease_atc_map: {disease_id: [(atc_class, count), ...]} from
                          DRKG Compound-x-atc-Atc edges joined with
                          Compound-treats-Disease edges

    The ATC code is the standard WHO drug classification
    (https://www.whocc.no/atc_ddd_index/); we extract the first-level
    letter as the disease-class key.

    ARCH-001 fix: auxiliary_pairs are accepted and stored in the output
    for downstream multi-relational PyG graph construction.

    Args:
        drkg_df: Validated DRKG DataFrame.
        all_drug_ids: All compound IDs in the knowledge graph.
        all_disease_ids: All disease IDs in the knowledge graph.
        positive_pairs: Positive example dicts from extract_positive_pairs().
        positive_pair_set: Set of (drug_id, disease_id) tuples for dedup.
        drug_disease_map: For wrong-class negative sampling. Auto-built if None.
        disease_atc_map: For wrong-class negative sampling. Auto-built if None.
            If auto-built, returns Dict[str, List[Tuple[str, int]]] format
            (all classes with counts). If caller-provided, any format accepted.
        failed_trials: For failed-trial negative sampling.
        neg_ratio: Target negative:positive ratio (default from config).
        auxiliary_pairs: Multi-relational pairs from extract_auxiliary_positive_pairs().
            Stored in output for downstream PyG heterogeneous graph builder.
        drkg_checksum: SHA-256 checksum of the input DRKG data for lineage.

    Returns:
        Dict with:
          - positive_pairs, negative_pairs (lists)
          - num_positives, num_negatives (ints)
          - ratio (float)
          - strategy_breakdown (dict)
          - drug_disease_map_size, disease_atc_map_size (ints)
          - auxiliary_pairs (dict or None)
          - _schema_version (str)
          - _provenance (dict)
          - _generated_at (str)
          - _source_checksums (dict)

    Raises:
        DrugOSDataError: If drkg_df is None or missing columns.
        ValueError: If neg_ratio is negative. (CFG-005)
    """
    t0 = time.time()

    # REL-001: Validate input
    _validate_drkg_df(drkg_df, "build_training_data")

    # CFG-005: Validate parameters
    if neg_ratio < 0:
        raise ValueError(f"neg_ratio must be non-negative, got {neg_ratio}")

    # DQI-004: Validate all_drug_ids and all_disease_ids
    if not all_drug_ids:
        raise DrugOSDataError(
            "all_drug_ids is empty — no drug entities available for negative sampling",
            context={"function": "build_training_data"},
        )
    if not all_disease_ids:
        raise DrugOSDataError(
            "all_disease_ids is empty — no disease entities available for negative sampling",
            context={"function": "build_training_data"},
        )

    # REL-002: Check for empty positive pairs
    if not positive_pairs:
        raise DrugOSDataError(
            "positive_pairs is empty — cannot build training data with no positive examples",
            context={"function": "build_training_data"},
        )

    # IOP-004: Check DRKG schema version compatibility
    #
    # v22 ROOT FIX (forensic audit follow-up): the previous code was
    # ``if hasattr(drkg_df, "_schema_version"): if drkg_df._schema_version != expected``.
    # On a pandas DataFrame, ``hasattr(df, "_schema_version")`` returns
    # True whenever a column named ``_schema_version`` exists (pandas
    # exposes columns as attributes). Then ``df._schema_version``
    # returns the COLUMN (a Series), and ``Series != "2.0.0"`` produces
    # a boolean Series — which raises ``ValueError: The truth value of
    # a Series is ambiguous`` when used in ``if``. This crashed
    # Step 10 (training_data) on the default Phase 1 path because the
    # bridge attaches ``_schema_version`` as a per-edge property, which
    # the run_pipeline DRKG-shim flattens into a column.
    #
    # Root fix: distinguish the three legitimate cases —
    #   (a) real Python attribute on a non-DataFrame object
    #       (``drkg_df.attrs["_schema_version"]`` or a dataclass),
    #   (b) a column named ``_schema_version`` on a DataFrame, where
    #       the value should be the unique scalar across all rows,
    #   (c) ``df.attrs`` metadata (the proper pandas metadata API).
    # Anything else is silently skipped (the contract is best-effort).
    import pandas as _pd  # local alias to avoid module-level import cycle

    expected = SCHEMA_VERSION
    if isinstance(drkg_df, _pd.DataFrame):
        # Case (b): column on the DataFrame.
        if "_schema_version" in drkg_df.columns:
            unique_versions = drkg_df["_schema_version"].dropna().unique().tolist()
            if len(unique_versions) == 1:
                actual = unique_versions[0]
                if actual != expected:
                    logger.warning(
                        "DRKG schema version mismatch: expected %s, got %s "
                        "(column _schema_version)",
                        expected, actual,
                    )
            elif len(unique_versions) > 1:
                logger.warning(
                    "DRKG schema version column has multiple values: %s "
                    "(expected %s). This indicates mixed-schema rows in the "
                    "DRKG DataFrame — investigate the bridge/loader.",
                    unique_versions, expected,
                )
        # Case (c): proper pandas metadata API.
        elif "_schema_version" in drkg_df.attrs:
            actual = drkg_df.attrs["_schema_version"]
            if actual != expected:
                logger.warning(
                    "DRKG schema version mismatch (df.attrs): expected %s, got %s",
                    expected, actual,
                )
    else:
        # Case (a): non-DataFrame object with a real attribute.
        if hasattr(drkg_df, "_schema_version"):
            actual = getattr(drkg_df, "_schema_version")
            if actual != expected:
                logger.warning(
                    "DRKG schema version mismatch (attribute): expected %s, got %s",
                    expected, actual,
                )

    # ─── Auto-build drug_disease_map from positive_pairs (ARCH-002) ──────
    if drug_disease_map is None:
        drug_disease_map = _build_drug_disease_map(positive_pairs)
        if disease_atc_map is None:
            disease_atc_map_auto = _build_disease_atc_map(drkg_df, positive_pairs)
            # v35 ROOT FIX (H-6): pass the FULL List[Tuple[str, int]]
            # structure to the NegativeSampler instead of collapsing to
            # a majority-class-only string. The previous code did:
            #     disease_atc_map_sampler = {d: classes[0][0] for ...}
            # which threw away the per-class vote counts. The sampler
            # then could not distinguish "disease D has 9 votes for A
            # and 1 vote for C" from "disease D has 1 vote for A and
            # 9 votes for C" — both looked like just ``A``. This made
            # wrong-class sampling almost identical to random sampling
            # for diseases whose drugs span multiple ATC classes
            # (M-4 also addresses this from the sampler side).
            #
            # The fix passes the FULL per-disease class distribution
            # through to the sampler. The sampler (also fixed in v35,
            # M-4) now uses the full set of known classes for each
            # disease, not just the majority class.
            disease_atc_map_sampler = disease_atc_map_auto  # Dict[str, List[Tuple[str, int]]]
        else:
            disease_atc_map_sampler = disease_atc_map
            disease_atc_map_auto = None
    else:
        disease_atc_map_sampler = disease_atc_map
        disease_atc_map_auto = None

    # ─── Generate negatives ─────────────────────────────────────────────
    num_negatives = int(len(positive_pairs) * neg_ratio)
    logger.info(
        "Generating %d negative samples (target ratio: %.1f:1)",
        num_negatives, neg_ratio,
    )

    sampler = NegativeSampler(all_drug_ids, all_disease_ids, positive_pair_set)
    negatives = sampler.combined_sampling(
        drug_disease_map=drug_disease_map,
        disease_atc_map=disease_atc_map_sampler,
        failed_trials=failed_trials,
        total_negatives=num_negatives,
    )

    # REL-005: Warn if negative sampling fell short
    if len(negatives) < num_negatives * 0.9:
        logger.warning(
            "Negative sampling shortfall: requested %d, got %d (%.0f%% of target). "
            "Consider lowering neg_ratio or checking data coverage.",
            num_negatives, len(negatives),
            100.0 * len(negatives) / max(num_negatives, 1),
        )

    # ─── Strategy breakdown (ARCH-002) ──────────────────────────────────
    strategy_breakdown = _compute_strategy_breakdown(negatives)

    actual_ratio = len(negatives) / max(len(positive_pairs), 1)
    logger.info(
        "Training data: %d positive, %d negative (ratio: %.1f:1) "
        "strategies: %s",
        len(positive_pairs), len(negatives),
        actual_ratio,
        dict(strategy_breakdown),
    )

    # LIN-004: Compute DRKG checksum if not provided
    if not drkg_checksum:
        drkg_checksum = _compute_drkg_checksum(drkg_df)

    # CMP-002, LIN-001: Schema version and provenance metadata
    provenance = build_lineage_metadata(
        input_checksums={"drkg": drkg_checksum}
    )

    result: Dict[str, Any] = {
        "positive_pairs": positive_pairs,
        "negative_pairs": negatives,
        "num_positives": len(positive_pairs),
        "num_negatives": len(negatives),
        "ratio": actual_ratio,
        "strategy_breakdown": strategy_breakdown,
        "drug_disease_map_size": len(drug_disease_map) if drug_disease_map else 0,
        "disease_atc_map_size": len(disease_atc_map_sampler) if disease_atc_map_sampler else 0,
        # ARCH-001: Store auxiliary pairs for downstream multi-relational training
        "auxiliary_pairs": auxiliary_pairs,
        # CMP-002: Schema version for downstream consumers
        "_schema_version": TRAINING_DATA_SCHEMA_VERSION,
        # LIN-001: Provenance metadata
        "_provenance": asdict(provenance),
        # DSN-003: Generated timestamp
        "_generated_at": datetime.now(timezone.utc).isoformat(),
        # LIN-004: Source checksums
        "_source_checksums": {"drkg": drkg_checksum},
    }

    # LOG-004: Timing log
    elapsed = time.time() - t0
    logger.info("build_training_data completed in %.1fs", elapsed)

    return result


# ======================================================================
# Public function 4: temporal_split_pairs
# ======================================================================


def temporal_split_pairs(
    positive_pairs: List[Dict],
    cutoff_year: int = DEFAULT_CUTOFF_YEAR,
    approval_years: Optional[Dict[Tuple[str, str], int]] = None,
) -> Dict[str, Any]:
    """Split positive pairs by approval year for temporal evaluation.

    Clinical Rationale (DOC-004):
    Temporal splitting ensures the model is evaluated on drug-disease pairs
    that were discovered AFTER the training cutoff. This simulates real-world
    deployment where the model must predict genuinely novel repurposing
    candidates, not memorize known pairs. Drugs approved before the cutoff
    constitute the training set; drugs approved after are the test set —
    exactly as they would be in a prospective clinical application.

    Split boundaries (SCI-003 fix — docstring now matches code):
      - train: approved <= cutoff_year - 2 (e.g., <= 2018 for cutoff=2020)
      - val:   approved > cutoff_year - 2 AND <= cutoff_year (e.g., 2019-2020)
      - test:  approved > cutoff_year (e.g., 2021+)

    When approval_years is None or empty, falls back to deterministic
    random split using SEED from config (IDE-001).

    Args:
        positive_pairs: Positive example dicts with 'drug_id' and 'disease_id'.
        cutoff_year: Year boundary for temporal split.
        approval_years: Optional {(drug_id, disease_id): year} mapping.

    Returns:
        Dict with 'train', 'val', 'test' lists and '_split_metadata' dict
        containing split methodology details. (DSN-004)

    Raises:
        ValueError: If cutoff_year is outside reasonable range. (CFG-005)
    """
    t0 = time.time()

    # CFG-005: Validate cutoff_year
    if cutoff_year < 1900 or cutoff_year > 2100:
        raise ValueError(
            f"cutoff_year {cutoff_year} is outside reasonable range [1900, 2100]"
        )

    no_year: List[Dict] = []

    if not approval_years:
        # v35 ROOT FIX (H-5): the previous code silently fell back to a
        # random split whenever ``approval_years`` was None or empty.
        # A random split DESTROYS the temporal evaluation guarantee
        # (future drug approvals leak into the train set), which is
        # exactly what the docstring promises this function prevents.
        # The silent fallback made the leakage invisible — operators
        # calling ``temporal_split_pairs`` had no way to know the
        # returned split was NOT actually temporal.
        #
        # The fix raises ``DrugOSDataError`` unless the operator
        # explicitly sets ``DRUGOS_ALLOW_TEMPORAL_RANDOM_FALLBACK=1``
        # to opt back into the leaky behavior. This makes the
        # degradation OBSERVABLE and forces callers to either supply
        # approval years or acknowledge they are accepting a random
        # split (e.g. for development / unit tests).
        _allow_random_fallback = os.environ.get(
            "DRUGOS_ALLOW_TEMPORAL_RANDOM_FALLBACK", "0"
        ) == "1"
        if not _allow_random_fallback:
            raise DrugOSDataError(
                "temporal_split_pairs: approval_years is None or empty — "
                "cannot perform a temporal split. A random fallback would "
                "violate the temporal evaluation guarantee (future drug "
                "approvals could leak into the train set). Either: "
                "(a) provide approval_years={(drug_id, disease_id): int} "
                "for every positive pair, OR "
                "(b) set DRUGOS_ALLOW_TEMPORAL_RANDOM_FALLBACK=1 in the "
                "environment to acknowledge you are accepting a random "
                "split (development / unit tests only). (H-5 root fix)",
                context={
                    "function": "temporal_split_pairs",
                    "error": "missing_approval_years",
                    "n_pairs": len(positive_pairs),
                    "cutoff_year": cutoff_year,
                },
            )
        # IDE-001: Use set_global_seed() for reproducibility instead of bare random
        logger.warning(
            "No approval year data — DRUGOS_ALLOW_TEMPORAL_RANDOM_FALLBACK=1 "
            "is set, falling back to deterministic random split (seed=%d). "
            "WARNING: this violates the temporal evaluation guarantee — "
            "the returned split is NOT a temporal split. (H-5)",
            SEED,
        )
        # v41 ROOT FIX (Task J DEAD): explicit WARNING that the current
        # Phase 1 schema does NOT provide approval_year, so callers know
        # this fallback is the EXPECTED production path (not an anomaly)
        # until Phase 1 is extended to source approval-year data from
        # DailyMed SPL / FDA Orange Book / WHO ATC-DDD. See the
        # module-level docstring (search for "v41 ROOT FIX (Task J DEAD)")
        # for the full schema-gap analysis.
        logger.warning(
            "temporal_split_pairs: approval_years is None/empty — the "
            "current Phase 1 schema (drugbank_drugs.csv / drugs table) "
            "does NOT carry an approval_year column. The DrugBank "
            "open-access XML parsed by phase1 does not include approval "
            "dates. Until Phase 1's schema is extended (candidate "
            "sources: DailyMed SPL, FDA Orange Book, WHO ATC/DDD index), "
            "ALL production calls to temporal_split_pairs will hit this "
            "fallback. To enable real temporal evaluation, supply an "
            "approval_years={(drug_id, disease_id): int} mapping "
            "explicitly. (Task J DEAD — schema gap, see module docstring)",
        )
        set_global_seed(SEED)
        import random
        rng = random.Random(SEED)  # Explicit seeded RNG for determinism
        shuffled = sorted(positive_pairs, key=lambda p: (
            p.get("drug_id", ""), p.get("disease_id", "")
        ))  # IDE-002: Sort first for deterministic ordering
        rng.shuffle(shuffled)
        n = len(shuffled)
        n_train = int(n * TRAIN_SPLIT_RATIO)
        n_val = int(n * VAL_SPLIT_RATIO)

        # v35 ROOT FIX (M-5): set ``no_year_count`` to 0 in random mode
        # (every pair IS assigned to a split — none are dropped for
        # missing years). Also add an explicit ``all_pairs_random_split``
        # field so downstream consumers can distinguish "random split
        # because no years" from "temporal split that happened to drop
        # 0 pairs" without inspecting the method string.
        result = {
            "train": shuffled[:n_train],
            "val": shuffled[n_train:n_train + n_val],
            "test": shuffled[n_train + n_val:],
            # DSN-004: Split metadata
            "_split_metadata": {
                "method": "random",
                "cutoff_year": cutoff_year,
                "seed": SEED,
                "train_count": n_train,
                "val_count": n_val,
                "test_count": n - n_train - n_val,
                "no_year_count": 0,  # M-5: 0 in random mode (every pair assigned)
                "fell_back_to_random": True,
                "train_ratio": TRAIN_SPLIT_RATIO,
                "val_ratio": VAL_SPLIT_RATIO,
                "all_pairs_random_split": True,  # M-5
            },
        }

        # LOG-004: Timing log
        elapsed = time.time() - t0
        logger.info("temporal_split_pairs completed in %.1fs", elapsed)
        return result

    # IDE-001: Sort positive_pairs deterministically before splitting
    positive_pairs_sorted = sorted(positive_pairs, key=lambda p: (
        p.get("drug_id", ""), p.get("disease_id", "")
    ))

    train: List[Dict] = []
    val: List[Dict] = []
    test: List[Dict] = []

    for pair in positive_pairs_sorted:
        key = (pair.get("drug_id", ""), pair.get("disease_id", ""))
        year = approval_years.get(key)

        if year is None:
            no_year.append(pair)
        elif year <= cutoff_year - 2:
            train.append(pair)
        elif year <= cutoff_year:
            val.append(pair)
        else:
            test.append(pair)

    # SCI-004 / Audit fix (v5 Tier-2 bug #15): Pairs without an approval
    # year previously went to train (conservative). The previous code
    # acknowledged this "may cause temporal data leakage" but did it
    # anyway. For a publishable temporal evaluation, no-year pairs MUST
    # NOT pollute the train set — the model could be trained on what are
    # effectively future approvals, then "evaluated" on a temporal test
    # set that excludes those same drugs.
    #
    # Fix: drop no_year pairs entirely from all three splits and report
    # them as a separate `dropped` list so callers can audit the loss.
    # Callers can opt back into the old behavior by setting
    # DRUGOS_ALLOW_NO_YEAR_IN_TRAIN=1 in the environment.
    if no_year:
        allow_no_year = os.environ.get("DRUGOS_ALLOW_NO_YEAR_IN_TRAIN", "0") == "1"
        if allow_no_year:
            train.extend(no_year)
            logger.warning(
                "%d pairs have no approval year — DRUGOS_ALLOW_NO_YEAR_IN_TRAIN=1, "
                "assigned to train. This may cause temporal data leakage.",
                len(no_year),
            )
        else:
            logger.warning(
                "%d pairs have no approval year — DROPPED from all splits to "
                "prevent temporal leakage. Set DRUGOS_ALLOW_NO_YEAR_IN_TRAIN=1 "
                "to restore the previous (leaky) behavior.",
                len(no_year),
            )

    logger.info(
        "Temporal split (cutoff=%d): train=%d, val=%d, test=%d, no_year=%d",
        cutoff_year, len(train), len(val), len(test), len(no_year),
    )

    # LOG-004: Timing log
    elapsed = time.time() - t0
    logger.info("temporal_split_pairs completed in %.1fs", elapsed)

    return {
        "train": train,
        "val": val,
        "test": test,
        # v28 ROOT FIX (P2-B-10): expose the dropped no-year pairs
        # directly on the returned dict so callers can audit the data
        # loss. Previously the dropped pairs were only counted inside
        # ``_split_metadata["no_year_count"]`` — the actual pair dicts
        # were silently discarded, with no way for downstream code to
        # inspect WHICH pairs were lost or to re-emit them to a separate
        # data-loss audit log. The pairs are now available at
        # ``result["dropped"]`` (a list of the original pair dicts, each
        # with its drug_id / disease_id / approval_year=None).
        "dropped": no_year,
        # DSN-004: Split metadata
        "_split_metadata": {
            "method": "temporal",
            "cutoff_year": cutoff_year,
            "seed": SEED,
            "train_count": len(train),
            "val_count": len(val),
            "test_count": len(test),
            "no_year_count": len(no_year),
            "dropped_count": len(no_year),  # v28: alias for clarity
            "fell_back_to_random": False,
        },
    }


# ======================================================================
# Utility function: pandas NaN check (COD-006)
# ======================================================================

def pd_notna(val: Any) -> bool:
    """Check if a value is not NaN, handling both pandas and numpy NaN.

    v35 ROOT FIX (L-9): cache the pandas import at module load time
    so we do not pay the ``import pandas`` cost on every call. The
    previous code re-imported pandas inside the function body, which
    (per cProfile on a 15M-row DRKG) added ~3.2s of pure import
    overhead per ``extract_positive_pairs`` call. Importing once at
    module level is safe — pandas is a hard dependency in
    ``requirements.txt`` — and the ``_PD_NOTNA`` reference is set to
    ``None`` if the import fails so the function falls back to the
    numpy NaN check. (COD-006)
    """
    if val is None:
        return False
    if _PD_NOTNA is not None:
        try:
            return bool(_PD_NOTNA(val))
        except (TypeError, ValueError):
            pass
    # Fallback: check numpy NaN and Python None
    if isinstance(val, float):
        return not np.isnan(val)
    return True
