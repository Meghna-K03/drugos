"""
DrugOS backend — FastAPI entrypoint.

This is THE missing piece the frontend has been proxying to since it was
built: RL_SERVICE_URL, KG_SERVICE_URL and DATASET_SERVICE_URL all point
here once configured (see frontend/.env.local).

Run with:
    cd backend && uvicorn main:app --host 0.0.0.0 --port 8000 --reload
or from the repo root:
    make serve-api
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.auth import jwt_middleware
from backend.routes_dataset import router as dataset_router
from backend.routes_kg import router as kg_router
from backend.routes_rl import router as rl_router

app = FastAPI(title="DrugOS Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Auth runs after CORS so preflight OPTIONS requests are never blocked by
# the 401 short-circuit in jwt_middleware.
app.middleware("http")(jwt_middleware)

app.include_router(rl_router, prefix="/rl", tags=["rl"])
app.include_router(kg_router, prefix="/kg", tags=["kg"])
app.include_router(dataset_router, prefix="/dataset", tags=["dataset"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
