"""
JWT authentication for the DrugOS backend.

Mirrors frontend/src/lib/auth/server.ts::verifyAccessToken exactly so a
cookie minted by the Next.js app is valid here too:

  - HS256, shared JWT_SECRET env var.
  - issuer must be "drugos".
  - payload.type must be "access".
  - Claims: sub (userId), email, role, orgId (optional).

Reads the token from either:
  - the "drugos_access" cookie (same cookie name the frontend sets), or
  - an "Authorization: Bearer <token>" header.

On success, sets request.state.user = {"userId", "email", "role", "orgId"}.
On failure, returns 401 with {"error": "unauthorized", "message": "..."}.

NOTE (per PERFECT_FIX_PROMPT.md 1.1): API-key auth (Authorization: Bearer
drugos_<hex>) is intentionally NOT handled here yet, because the
frontend's own authenticateApiKey() path only became live after the B-3
fix and API keys are hashed/stored in the Postgres ApiKey table that only
the frontend (Prisma) owns. Wire this backend to the same Postgres
instance and extend `jwt_middleware` if/when API-key auth needs to reach
Python-only routes.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from jose import jwt, JWTError

JWT_ISSUER = "drugos"
ACCESS_COOKIE = "drugos_access"

# Public routes that don't require auth (health check, docs).
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


def _resolve_jwt_secret() -> str:
    """Mirrors server.ts::resolveJwtSecret(). Must match the frontend's
    JWT_SECRET exactly or every token verification will fail."""
    secret = os.getenv("JWT_SECRET")
    if not secret or len(secret) < 32:
        if os.getenv("DRUGOS_ENVIRONMENT") == "production" or os.getenv("NODE_ENV") == "production":
            raise RuntimeError(
                "JWT_SECRET must be set to a >=32-char random string in production."
            )
        return "dev-only-insecure-secret-change-me-MINIMUM-32-CHARS-FOR-HS256!!"
    return secret


def _extract_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        # Only treat as JWT if it doesn't look like an API key.
        if token and not token.startswith("drugos_"):
            return token
    cookie_token = request.cookies.get(ACCESS_COOKIE)
    if cookie_token:
        return cookie_token
    return None


def verify_access_token(token: str) -> Optional[dict]:
    secret = _resolve_jwt_secret()
    try:
        decoded = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=JWT_ISSUER,
            options={"require_exp": True, "require_iat": False},
        )
    except JWTError:
        return None
    if decoded.get("type") != "access" or not decoded.get("sub"):
        return None
    return {
        "userId": decoded["sub"],
        "email": decoded.get("email"),
        "role": decoded.get("role"),
        "orgId": decoded.get("orgId"),
    }


async def jwt_middleware(request: Request, call_next):
    """FastAPI HTTP middleware: verifies the access token on every request
    except PUBLIC_PATHS, and injects request.state.user."""
    if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
        request.state.user = None
        return await call_next(request)

    token = _extract_token(request)
    if not token:
        return JSONResponse(
            {"error": "unauthorized", "message": "Missing access token."}, status_code=401
        )
    user = verify_access_token(token)
    if not user:
        return JSONResponse(
            {"error": "unauthorized", "message": "Invalid or expired access token."},
            status_code=401,
        )
    request.state.user = user
    return await call_next(request)
