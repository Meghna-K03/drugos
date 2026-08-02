"""
POST /rank — RL hypothesis ranking endpoint.

Response shape matches exactly what frontend/src/app/api/rl/route.ts's
proxy path expects to receive from RL_SERVICE_URL/rank:

  {
    candidates: [{
      drug, disease, reward, rank, policyProb,
      plausibilityScore (gnnScore), safetyScore, marketScore, overallScore,
      literatureSupport, isKnownPositive, confidence,
      pathwayScore, unmetNeedScore, efficacyScore, admeScore
    }],
    modelVersion, generatedAt, count
  }

Two data paths, in priority order:

  1. Trained PPO checkpoint under rl/checkpoints/*.zip, loaded with
     stable-baselines3 if the dependency is installed and the checkpoint
     loads cleanly. If anything about this fails, we do NOT crash the
     request — we fall through to (2) and mark the response accordingly.
  2. CSV fallback: rl/validated_hypotheses.csv. This file only has
     `drug,disease` columns today (4 validated pairs) — every score field
     is explicitly defaulted to 0 and the response is tagged
     `source: "csv_fallback"` so the frontend/UI can show a "not a model
     prediction" badge. We NEVER invent scores.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.rbac import get_current_user

router = APIRouter()

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "rl" / "validated_hypotheses.csv"
CHECKPOINT_DIR = REPO_ROOT / "rl" / "checkpoints"


class RankRequest(BaseModel):
    drug: Optional[str] = None
    disease: Optional[str] = None
    limit: int = 50


def _load_csv_fallback(drug: Optional[str], disease: Optional[str], limit: int) -> List[dict]:
    if not CSV_PATH.exists():
        return []
    rows: List[dict] = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            d = (row.get("drug") or "").strip()
            dis = (row.get("disease") or "").strip()
            if drug and drug.lower() not in d.lower():
                continue
            if disease and disease.lower() not in dis.lower():
                continue
            rows.append(
                {
                    "drug": d,
                    "disease": dis,
                    "reward": 0.0,
                    "rank": idx + 1,
                    "policyProb": 0.0,
                    "plausibilityScore": 0.0,
                    "gnnScore": 0.0,
                    "safetyScore": 0.0,
                    "marketScore": 0.0,
                    "overallScore": 0.0,
                    "literatureSupport": False,
                    "isKnownPositive": True,  # these 4 pairs are curated known-positives
                    "confidence": 0.0,
                    "pathwayScore": 0.0,
                    "unmetNeedScore": 0.0,
                    "efficacyScore": 0.0,
                    "admeScore": 0.0,
                    "source": "csv_fallback",
                }
            )
    return rows[:limit]


def _try_checkpoint_inference(drug: Optional[str], disease: Optional[str], limit: int) -> Optional[List[dict]]:
    """Attempt real PPO inference from a trained checkpoint. Returns None
    (never raises) if no checkpoint exists or inference isn't possible, so
    the caller can fall back to the CSV path. This project's own docs
    note the shipped orphan checkpoint was removed as incompatible
    (rl/rl_drug_ranker.py B23) — so today this will almost always return
    None until a fresh checkpoint is trained with `make train-rl` /
    `python rl/rl_drug_ranker.py`."""
    if not CHECKPOINT_DIR.exists():
        return None
    checkpoints = sorted(glob(str(CHECKPOINT_DIR / "*.zip")))
    if not checkpoints:
        return None
    try:
        from stable_baselines3 import PPO  # type: ignore
    except ImportError:
        return None
    try:
        # NOTE: real inference requires reconstructing the exact
        # Gymnasium observation space the model was trained with (see
        # rl/rl_drug_ranker.py). That environment setup is intentionally
        # left to be wired by whoever trains the next checkpoint — until
        # then we refuse to guess an observation space, since a wrong
        # guess could produce silently-wrong (not just missing) scores,
        # which is exactly the "fake output" failure mode this platform
        # must never produce for a patient-facing prediction.
        return None
    except Exception:
        return None


@router.post("/rank")
async def rank(body: RankRequest, user: dict = Depends(get_current_user)):
    drug = (body.drug or "").strip() or None
    disease = (body.disease or "").strip() or None
    limit = max(1, min(body.limit or 50, 200))

    candidates = _try_checkpoint_inference(drug, disease, limit)
    model_version = None
    if candidates is None:
        candidates = _load_csv_fallback(drug, disease, limit)
        model_version = "csv_fallback-v1"
    else:
        model_version = "ppo-checkpoint-v1"

    return {
        "candidates": candidates,
        "modelVersion": model_version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(candidates),
    }
