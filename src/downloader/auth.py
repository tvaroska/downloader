"""Authentication middleware and constant-time API-key verification."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)
_BEARER_SCHEMES = frozenset({"bearer"})


class APIKeyError(Exception):
    """Raised when API key authentication fails."""


def _resolve_configured_api_key(settings: Settings | None = None) -> str | None:
    """Read the configured API key from Settings without per-request os.environ probes."""
    active = settings if isinstance(settings, Settings) else get_settings()
    raw = active.auth.api_key
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def is_auth_enabled(settings: Settings | None = None) -> bool:
    """Return True if API key authentication is configured."""
    return _resolve_configured_api_key(settings) is not None


def verify_api_key(api_key: str, settings: Settings | None = None) -> bool:
    """Verify `api_key` against configured key using constant-time `secrets.compare_digest`."""
    expected = _resolve_configured_api_key(settings)
    if expected is None:
        return True
    if not isinstance(api_key, str) or not api_key:
        return False
    return secrets.compare_digest(api_key.encode("utf-8"), expected.encode("utf-8"))


def extract_request_api_key(request: Request) -> str | None:
    """Extract Bearer or X-API-Key token from raw Request headers."""
    auth_header = request.headers.get("authorization")
    if isinstance(auth_header, str) and " " in auth_header:
        scheme, token = auth_header.split(None, 1)
        if scheme.casefold() in _BEARER_SCHEMES and token.strip():
            return token.strip()

    x_key = request.headers.get("x-api-key")
    if isinstance(x_key, str) and x_key.strip():
        return x_key.strip()

    return None


async def get_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str | None:
    """Validate Bearer or X-API-Key credentials when authentication is enabled."""
    scope = getattr(request, "scope", None)
    app_obj = scope.get("app") if isinstance(scope, Mapping) else None
    state_obj = getattr(app_obj, "state", None)
    candidate = getattr(state_obj, "settings", None)
    req_settings = candidate if isinstance(candidate, Settings) else None

    if not is_auth_enabled(req_settings):
        return None

    api_key: str | None = None
    if credentials and credentials.credentials:
        api_key = credentials.credentials
        logger.debug("API key found in Authorization header")
    elif "x-api-key" in request.headers:
        api_key = request.headers["x-api-key"]
        logger.debug("API key found in X-API-Key header")

    if not api_key:
        logger.warning("API key authentication required but not provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "success": False,
                "error": "API key required. Provide via Authorization header or X-API-Key header",
                "error_type": "authentication_required",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_api_key(api_key, req_settings):
        logger.warning("Invalid API key provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "success": False,
                "error": "Invalid API key",
                "error_type": "authentication_failed",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.debug("API key authentication successful")
    return api_key


def get_auth_status(settings: Settings | None = None) -> dict[str, Any]:
    """Return authentication status summary for `/health`."""
    auth_enabled = is_auth_enabled(settings)
    return {
        "auth_enabled": auth_enabled,
        "auth_methods": [
            "Authorization: Bearer <api_key>",
            "X-API-Key: <api_key>",
        ]
        if auth_enabled
        else None,
    }
