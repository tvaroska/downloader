"""URL validation and sanitization utilities."""

import re
from urllib.parse import urlparse

import httpx


class URLValidationError(Exception):
    """Raised when URL validation fails."""

    pass


def validate_url(url: str) -> str:
    """
    Validate and sanitize a URL.

    Args:
        url: The URL to validate

    Returns:
        The sanitized URL

    Raises:
        URLValidationError: If the URL is invalid
    """
    if not url or not isinstance(url, str):
        raise URLValidationError("URL must be a non-empty string")

    # Remove whitespace
    url = url.strip()

    # Add http:// if no scheme is present
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"http://{url}"

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise URLValidationError(f"Invalid URL format: {e}")

    # Validate scheme
    if parsed.scheme not in ("http", "https"):
        raise URLValidationError("URL must use http or https scheme")

    # Validate hostname
    if not parsed.hostname:
        raise URLValidationError("URL must have a valid hostname")

    # Basic hostname validation
    if not re.match(r"^[a-zA-Z0-9.-]+$", parsed.hostname):
        raise URLValidationError("Invalid hostname format")

    # Prevent localhost and private IP ranges (basic SSRF protection)
    if _is_private_address(parsed.hostname):
        raise URLValidationError("Access to private addresses is not allowed")

    return url


def _is_private_address(hostname: str) -> bool:
    """
    Check if hostname points to a private/local address.

    Args:
        hostname: The hostname to check

    Returns:
        True if the hostname is private/local
    """
    # Check for localhost variations
    localhost_patterns = ["localhost", "127.0.0.1", "::1", "0.0.0.0"]

    if hostname.lower() in localhost_patterns:
        return True

    # Check for private IP ranges (simplified)
    private_patterns = [
        r"^10\.",
        r"^172\.(1[6-9]|2[0-9]|3[01])\.",
        r"^192\.168\.",
        r"^169\.254\.",  # Link-local
    ]

    for pattern in private_patterns:
        if re.match(pattern, hostname):
            return True

    return False


def sanitize_user_agent(user_agent: str | None = None) -> str:
    """
    Sanitize or provide default User-Agent header.

    Args:
        user_agent: Optional custom user agent

    Returns:
        Sanitized user agent string
    """
    if user_agent:
        # Remove potentially harmful characters
        sanitized = re.sub(r"[^\w\s\-\.\(\)/;:,]", "", user_agent)
        return sanitized[:200]  # Limit length

    # Default user agent
    return f"REST-API-Downloader/{httpx.__version__}"
