"""
Role-based access control dependency for FastAPI routes.

Mirrors the frontend's requireAuthRole() convention: "admin" and "owner"
always bypass a role check, everyone else must have one of the roles
listed at the call site.

Usage:
    @router.post("/rank", dependencies=[Depends(require_role("analyst", "admin"))])
"""

from __future__ import annotations

from fastapi import Depends, Request, HTTPException

ALWAYS_ALLOWED_ROLES = {"admin", "owner"}


def get_current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def require_role(*roles: str):
    allowed = set(roles) | ALWAYS_ALLOWED_ROLES

    def _dependency(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _dependency
