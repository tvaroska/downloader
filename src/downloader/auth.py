"""Authentication middleware and utilities."""

import logging
import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

logger = logging.getLogger(__name__)


# Security scheme for API key authentication
security = HTTPBearer(auto_error=False)


class APIKeyError(Exception):
    """Raised when API key authentication fails."""

    pass


def is_auth_enabled(settings: Settings | None = None) -> bool:
    """
    Check if API key authentication is enabled.

    Args:
        settings: Optional settings instance (will load from get_settings() if not provided)

    Returns:
        True if API key is configured in settings
    """
    if settings is None:
        # Check environment variable directly for testing compatibility
        api_key = os.environ.get("DOWNLOADER_KEY")
        return api_key is not None and api_key.strip() != ""
    return settings.auth.api_key is not None and settings.auth.api_key.strip() != ""


def verify_api_key(api_key: str, settings: Settings | None = None) -> bool:
    """
    Verify if the provided API key is valid.

    Args:
        api_key: The API key to verify
        settings: Optional settings instance (will load from get_settings() if not provided)

    Returns:
        True if the API key is valid, False otherwise
    """
    if settings is None:
        # Check environment variable directly for testing compatibility
        env_key = os.environ.get("DOWNLOADER_KEY")
        if not env_key:
            return True  # No authentication required if no key is set
        return api_key == env_key

    if not settings.auth.api_key:
        return True  # No authentication required if no key is set

    return api_key == settings.auth.api_key


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
    # Skip authentication if not enabled
    if not is_auth_enabled():
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

    if not verify_api_key(api_key):
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
