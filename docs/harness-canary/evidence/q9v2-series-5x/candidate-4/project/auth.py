"""Public synthetic tokens; authentication is outside the repair scope."""

from fastapi import Header, HTTPException

TOKENS = {"alpha-token": "alpha", "beta-token": "beta"}


def current_tenant(authorization: str | None = Header(default=None)) -> str:
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if not authorization or not authorization.startswith("Bearer ") or token not in TOKENS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return TOKENS[token]
