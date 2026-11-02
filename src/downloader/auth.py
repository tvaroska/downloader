"""Authentication middleware and utilities."""

import logging
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import AuthConfig, Settings, get_settings

logger = logging.getLogger(__name__)


# Security scheme for API key authentication
security = HTTPBearer(auto_error=False)


class APIKeyError(Exception):
    """Raised when API key authentication fails."""

    pass


def _resolve_configured_api_key(settings: Settings | None = None) -> str | None:
    """Resolve the active API key consistently across explicit Settings, os.environ, and .env."""
    active = settings if isinstance(settings, Settings) else get_settings()
    raw: str | None = None
    if isinstance(active, Settings):
        if getattr(active.auth, "_explicit_api_key", False):
            raw = active.auth.api_key
        elif "DOWNLOADER_KEY" in os.environ:
            raw = os.environ.get("DOWNLOADER_KEY")
        elif not getattr(active.auth, "_loaded_from_env", False):
            raw = active.auth.api_key
    elif "DOWNLOADER_KEY" in os.environ:
        raw = os.environ.get("DOWNLOADER_KEY")
    elif "pytest" not in sys.modules and Path(".env").is_file():
        raw = AuthConfig().api_key

    cleaned = raw.strip() if isinstance(raw, str) else ""
    return cleaned or None


def is_auth_enabled(settings: Settings | None = None) -> bool:
    """
    Check if API key authentication is enabled.

    Args:
        settings: Optional settings instance (will load from environment/.env if not provided)

    Returns:
        True if API key is configured
    """
    return _resolve_configured_api_key(settings) is not None


def verify_api_key(api_key: str, settings: Settings | None = None) -> bool:
    """
    Verify if the provided API key is valid using constant-time digest comparison.

    Args:
        api_key: The API key to verify
        settings: Optional settings instance

    Returns:
        True if the API key is valid, False otherwise
    """
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
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()

    x_key = request.headers.get("x-api-key")
    if isinstance(x_key, str) and x_key.strip():
        return x_key.strip()

    return None


async def get_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str | None:
    """
    Extract API key from request headers.

    Supports multiple authentication methods:
    1. Bearer token in Authorization header
    2. X-API-Key header

    Args:
        request: FastAPI request object
        credentials: Bearer token credentials

    Returns:
        API key if found, None otherwise

    Raises:
        HTTPException: If authentication is required but missing/invalid
    """
    scope = getattr(request, "scope", None)
    app_obj = scope.get("app") if isinstance(scope, Mapping) else None
    state_obj = getattr(app_obj, "state", None)
    candidate = getattr(state_obj, "settings", None)
    req_settings = candidate if isinstance(candidate, Settings) else None

    # Skip authentication if not enabled
    if not is_auth_enabled(req_settings):
        return None

    api_key = None

    # Method 1: Bearer token
    if credentials and credentials.credentials:
        api_key = credentials.credentials
        logger.debug("API key found in Authorization header")

    # Method 2: X-API-Key header
    elif "x-api-key" in request.headers:
        api_key = request.headers["x-api-key"]
        logger.debug("API key found in X-API-Key header")

    # Verify the API key
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


def get_auth_status(settings: Settings | None = None) -> dict:
    """
    Get current authentication status information.

    Args:
        settings: Optional settings instance (will load from get_settings() if not provided)

    Returns:
        Dictionary with authentication status information
    """
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
