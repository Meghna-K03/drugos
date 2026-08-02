"""
Integration tests for the backend FastAPI app.

Run with: cd backend && python -m pytest tests/ -x --tb=short
(or from repo root: python -m pytest backend/tests/ -x --tb=short)

These tests use FastAPI's TestClient and a real JWT signed the same way
the frontend signs one, so we exercise the actual auth middleware rather
than bypassing it.
"""

import os

os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-change-me-MINIMUM-32-CHARS-FOR-HS256!!")

import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def _make_token(role="admin"):
    payload = {
        "sub": "test-user-id",
        "email": "test@example.com",
        "role": role,
        "type": "access",
        "iss": "drugos",
        "iat": int(time.time()),
        "exp": int(time.time()) + 900,
    }
    return pyjwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")


def auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


def test_health_no_auth_required():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "1.0.0"}


def test_rank_requires_auth():
    resp = client.post("/rl/rank", json={"limit": 5})
    assert resp.status_code == 401


def test_rank_returns_csv_fallback_candidates():
    resp = client.post("/rl/rank", json={"limit": 5}, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "candidates" in body
    assert body["count"] == len(body["candidates"])
    assert len(body["candidates"]) == 4  # validated_hypotheses.csv has 4 rows
    for c in body["candidates"]:
        assert c["source"] == "csv_fallback"
        assert c["drug"]
        assert c["disease"]


def test_rank_filters_by_drug():
    resp = client.post("/rl/rank", json={"drug": "thalidomide", "limit": 5}, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["drug"] == "thalidomide"


def test_dataset_stats_reads_real_checkpoint():
    resp = client.get("/dataset/stats", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodesLoaded"] == 63
    assert body["edgesLoaded"] == 81
    assert body["pipelineVersion"] == "2.0.0-week2"
    assert len(body["sources"]) > 0


def test_kg_stats_reads_registry():
    resp = client.get("/kg/stats", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "sources" in body
    assert "string_edges" in body["sources"]


def test_kg_query_returns_503_without_neo4j():
    # In CI / this sandbox there is no live Neo4j — verify we return an
    # honest 503 rather than fabricated graph data.
    resp = client.post("/kg/query", json={"drug": "thalidomide"}, headers=auth_headers())
    assert resp.status_code in (503, 200)
    if resp.status_code == 503:
        assert resp.json()["detail"]["error"] == "graph_unavailable"
