"""Authentication middleware and utilities."""

import logging
import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)


# Get API key from environment variable - read dynamically
def get_api_key_from_env() -> str | None:
    """Get API key from environment variable."""
    return os.getenv("DOWNLOADER_KEY")


# Security scheme for API key authentication
security = HTTPBearer(auto_error=False)


class APIKeyError(Exception):
    """Raised when API key authentication fails."""

    pass


def is_auth_enabled() -> bool:
    """
    Check if API key authentication is enabled.

    Returns:
        True if DOWNLOADER_KEY environment variable is set
    """
    api_key = get_api_key_from_env()
    return api_key is not None and api_key.strip() != ""


def verify_api_key(api_key: str) -> bool:
    """
    Verify if the provided API key is valid.

    Args:
        api_key: The API key to verify

    Returns:
        True if the API key is valid, False otherwise
    """
    env_api_key = get_api_key_from_env()
    if not env_api_key:
        return True  # No authentication required if no key is set

    return api_key == env_api_key


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


def get_auth_status() -> dict:
    """
    Get current authentication status information.

    Returns:
        Dictionary with authentication status information
    """
    return {
        "auth_enabled": is_auth_enabled(),
        "auth_methods": ["Authorization: Bearer <api_key>", "X-API-Key: <api_key>"]
        if is_auth_enabled()
        else None,
    }
