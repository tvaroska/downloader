"""URL validation and sanitization utilities with comprehensive SSRF protection."""

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class URLValidationError(Exception):
    """Raised when URL validation fails."""

    pass


class SSRFProtectionError(URLValidationError):
    """Raised when URL is blocked by SSRF protection."""

    pass


def validate_url(url: str, settings: Settings | None = None) -> str:
    """
    Validate and sanitize a URL with comprehensive SSRF protection.

    This function performs multiple layers of validation:
    1. Basic URL format validation
    2. Scheme validation (http/https only)
    3. Hostname validation
    4. DNS resolution (if enabled)
    5. IP address blocking (private, loopback, link-local, cloud metadata)

    Args:
        url: The URL to validate
        settings: Optional settings instance (loads from get_settings() if not provided)

    Returns:
        The sanitized URL

    Raises:
        URLValidationError: If the URL is invalid
        SSRFProtectionError: If the URL is blocked by SSRF protection
    """
    if settings is None:
        settings = get_settings()

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

    # Basic hostname validation (allow alphanumeric, dots, hyphens, underscores, colons for IPv6)
    # IPv6 addresses are already unbracketed by urlparse (parsed.hostname has no brackets)
    if not re.match(r"^[a-zA-Z0-9._:-]+$", parsed.hostname):
        raise URLValidationError("Invalid hostname format")

    # SSRF Protection - check if hostname/IP is blocked
    _check_ssrf_protection(parsed.hostname, settings)

    return url


def _check_ssrf_protection(hostname: str, settings: Settings) -> None:
    """
    Comprehensive SSRF protection - validates hostname/IP is not restricted.

    This function performs multi-layered checks:
    1. Check if hostname is already an IP address
    2. Resolve DNS to get actual IP address(es) if enabled
    3. Validate IP addresses are not in restricted ranges:
       - Private IPs (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
       - Loopback (127.0.0.0/8, ::1)
       - Link-local (169.254.0.0/16, fe80::/10) - includes cloud metadata!
       - Multicast, reserved, unspecified addresses

    Args:
        hostname: The hostname or IP address to check
        settings: Settings instance with SSRF configuration

    Raises:
        SSRFProtectionError: If the hostname/IP is blocked
    """
    # Try to parse hostname as IP address first
    ip_addresses = []

    try:
        # If hostname is already an IP address, use it directly
        ip_obj = ipaddress.ip_address(hostname)
        ip_addresses.append(ip_obj)
        logger.debug(f"Hostname '{hostname}' is already an IP address")
    except ValueError:
        # Hostname is a domain name, needs DNS resolution
        if settings.ssrf.resolve_dns:
            # Resolve DNS to get actual IP address(es)
            try:
                # Get all IP addresses for this hostname (IPv4 and IPv6)
                addr_info = socket.getaddrinfo(hostname, None)
                for info in addr_info:
                    ip_str = info[4][0]
                    ip_obj = ipaddress.ip_address(ip_str)
                    ip_addresses.append(ip_obj)

                logger.debug(f"Resolved '{hostname}' to {len(ip_addresses)} IP address(es)")
            except (OSError, socket.gaierror) as e:
                logger.warning(f"DNS resolution failed for '{hostname}': {e}")
                raise SSRFProtectionError(
                    f"Cannot resolve hostname '{hostname}'. DNS resolution failed."
                )
        else:
            # DNS resolution disabled - do basic string matching on hostname
            logger.debug(f"DNS resolution disabled, checking hostname '{hostname}' directly")
            if _is_hostname_blocked(hostname):
                raise SSRFProtectionError(
                    f"Hostname '{hostname}' appears to be a restricted address"
                )
            return  # No IP to check, return early

    # Check each resolved IP address
    for ip_obj in ip_addresses:
        _validate_ip_address(ip_obj, hostname, settings)


def _validate_ip_address(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    original_hostname: str,
    settings: Settings,
) -> None:
    """
    Validate that an IP address is not in a restricted range.

    Args:
        ip_obj: IP address object to validate
        original_hostname: Original hostname (for error messages)
        settings: Settings instance with SSRF configuration

    Raises:
        SSRFProtectionError: If the IP is in a restricted range
    """
    ip_str = str(ip_obj)

    # Check order is important! Many IPs fall into multiple categories (e.g., 0.0.0.0 is both
    # unspecified AND private, 169.254.x.x is link-local AND private, 240.x.x.x is reserved AND private).
    # We check most specific to least specific to get the most accurate error message.

    # 1. Check for loopback (127.0.0.0/8, ::1)
    if ip_obj.is_loopback:
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} (loopback)")
        raise SSRFProtectionError(
            f"Access to loopback private addresses is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    # 2. Check for unspecified (0.0.0.0, ::) - must come before private check
    if ip_obj.is_unspecified:
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} (unspecified)")
        raise SSRFProtectionError(
            f"Access to unspecified addresses is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    # 3. Specific check for cloud metadata IP (169.254.169.254) - must come before link-local and private
    if settings.ssrf.block_cloud_metadata:
        if ip_str == "169.254.169.254" or ip_str == "fd00:ec2::254":
            logger.warning(
                f"SSRF attempt blocked: {original_hostname} -> {ip_str} (cloud metadata)"
            )
            raise SSRFProtectionError(
                f"Access to cloud metadata endpoints is not allowed (hostname: {original_hostname}, IP: {ip_str})"
            )

    # 4. Check for link-local (169.254.0.0/16, fe80::/10) - must come before private check
    #    This is CRITICAL - includes cloud metadata endpoint range!
    if ip_obj.is_link_local:
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} (link-local)")
        raise SSRFProtectionError(
            f"Access to link-local addresses is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    # 5. Check for multicast (224.0.0.0/4, ff00::/8) - must come before reserved check
    if ip_obj.is_multicast:
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} (multicast)")
        raise SSRFProtectionError(
            f"Access to multicast addresses is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    # 6. Check for reserved IPs (240.0.0.0/4, etc) - must come before private check
    if ip_obj.is_reserved:
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} (reserved)")
        raise SSRFProtectionError(
            f"Access to reserved IP addresses is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    # 7. Check for private IPs (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fd00::/8)
    #    This is the broadest category and catches many IPs, so it must come LAST
    if settings.ssrf.block_private_ips and ip_obj.is_private:
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} (private IP)")
        raise SSRFProtectionError(
            f"Access to private IP and private addresses is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    logger.debug(f"IP address {ip_str} (from {original_hostname}) passed SSRF checks")


def _is_hostname_blocked(hostname: str) -> bool:
    """
    Check if hostname string matches known restricted patterns.

    This is a fallback when DNS resolution is disabled.
    Less secure than DNS resolution + IP checking.

    Args:
        hostname: The hostname to check

    Returns:
        True if the hostname should be blocked
    """
    hostname_lower = hostname.lower()

    # Check for localhost variations
    localhost_patterns = [
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
    ]
    if hostname_lower in localhost_patterns:
        return True

    # Check for private IP patterns (regex-based, less reliable)
    private_patterns = [
        r"^127\.",  # Loopback
        r"^10\.",  # Private class A
        r"^172\.(1[6-9]|2[0-9]|3[01])\.",  # Private class B
        r"^192\.168\.",  # Private class C
        r"^169\.254\.",  # Link-local (cloud metadata!)
        r"^0\.",  # Reserved
        r"^224\.",  # Multicast
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
