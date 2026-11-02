"""URL validation and sanitization utilities with comprehensive SSRF protection."""

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

_FORBIDDEN_CUSTOM_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-authenticate",
    }
)


class URLValidationError(Exception):
    """Raised when URL validation fails."""

    pass


class SSRFProtectionError(URLValidationError):
    """Raised when URL is blocked by SSRF protection."""

    pass


def _parse_and_normalize_url(url: str) -> tuple[str, str, int | None]:
    """Normalize and validate URL syntax, returning (normalized_url, hostname, port)."""
    if not url or not isinstance(url, str):
        raise URLValidationError("URL must be a non-empty string")

    url = url.strip()

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"http://{url}"

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise URLValidationError(f"Invalid URL format: {e}")

    if parsed.scheme == "file":
        raise URLValidationError("Access to local file system via file:// URLs is not allowed")
    if parsed.scheme not in ("http", "https"):
        raise URLValidationError("URL must use http or https scheme")

    if not parsed.hostname:
        raise URLValidationError("URL must have a valid hostname")

    # Basic hostname validation (allow alphanumeric, dots, hyphens, underscores, colons for IPv6)
    # IPv6 addresses are already unbracketed by urlparse (parsed.hostname has no brackets)
    if not re.match(r"^[a-zA-Z0-9._:-]+$", parsed.hostname):
        raise URLValidationError("Invalid hostname format")

    try:
        port = parsed.port
    except ValueError as e:
        raise URLValidationError(f"Invalid URL port: {e}")

    return url, parsed.hostname, port


def _try_parse_ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse standard IPv4/IPv6 as well as hex, octal, integer, and shorthand IPv4 literals."""
    clean = hostname.strip().strip("[]").split("%", 1)[0].rstrip(".").lower()
    if not clean:
        return None

    try:
        return ipaddress.ip_address(clean)
    except ValueError:
        pass

    parts = clean.split(".")
    if not (1 <= len(parts) <= 4):
        return None

    if not all(re.fullmatch(r"(0x[0-9a-f]+|0[0-7]*|[1-9]\d*)", p) for p in parts):
        return None

    try:
        nums: list[int] = []
        for p in parts:
            if p.startswith("0x"):
                nums.append(int(p, 16))
            elif len(p) > 1 and p.startswith("0"):
                nums.append(int(p, 8))
            else:
                nums.append(int(p, 10))
    except ValueError:
        return None

    if len(nums) == 1:
        if not (0 <= nums[0] <= 0xFFFFFFFF):
            return None
        packed = nums[0]
    elif len(nums) == 2:
        if not (0 <= nums[0] <= 0xFF and 0 <= nums[1] <= 0xFFFFFF):
            return None
        packed = (nums[0] << 24) | nums[1]
    elif len(nums) == 3:
        if not (0 <= nums[0] <= 0xFF and 0 <= nums[1] <= 0xFF and 0 <= nums[2] <= 0xFFFF):
            return None
        packed = (nums[0] << 24) | (nums[1] << 16) | nums[2]
    else:
        if not all(0 <= x <= 0xFF for x in nums):
            return None
        packed = (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]

    return ipaddress.IPv4Address(packed)


def _is_malformed_ip_or_numeric_tld(hostname: str) -> bool:
    """Detect malformed IPv6 or invalid numeric/hex TLD hostnames (RFC 1123 §2.1 / RFC 3696 §2)."""
    clean = hostname.strip().strip("[]").split("%", 1)[0].rstrip(".").lower()
    if not clean:
        return True
    if ":" in clean:
        return True
    return bool(re.fullmatch(r"(0x[0-9a-f]+|\d+)", clean.rsplit(".", 1)[-1]))


def validate_url(url: str, settings: Settings | None = None) -> str:
    """
    Validate and sanitize a URL with comprehensive SSRF protection.

    Args:
        url: The URL to validate
        settings: Optional settings instance (loads from get_settings() if not provided)

    Returns:
        The sanitized URL

    Raises:
        URLValidationError: If the URL is invalid
        SSRFProtectionError: If the URL is blocked by SSRF protection
    """
    normalized_url, hostname, port = _parse_and_normalize_url(url)
    resolve_and_validate_host(hostname, port, settings)
    return normalized_url


async def validate_url_async(url: str, settings: Settings | None = None) -> str:
    """Asynchronously validate and sanitize a URL without blocking the event loop on DNS lookup."""
    normalized_url, hostname, port = _parse_and_normalize_url(url)
    await resolve_and_validate_host_async(hostname, port, settings)
    return normalized_url


def _validate_resolved_addr_info(
    addr_info: Sequence[tuple[object, ...]],
    hostname: str,
    cfg: Settings,
) -> list[str]:
    """Validate all resolved getaddrinfo records and return deduplicated IP literals."""
    validated_ips: list[str] = []
    seen: set[str] = set()
    for info in addr_info:
        sockaddr = info[4]
        raw_ip = str(sockaddr[0]).split("%", 1)[0]  # type: ignore[index]
        ip_obj = ipaddress.ip_address(raw_ip)
        _validate_ip_address(ip_obj, hostname, cfg)
        ip_str = str(ip_obj)
        if ip_str not in seen:
            seen.add(ip_str)
            validated_ips.append(ip_str)

    if not validated_ips:
        raise SSRFProtectionError(f"Cannot resolve hostname '{hostname}'. DNS resolution failed.")

    logger.debug(f"Resolved '{hostname}' to {len(validated_ips)} IP address(es)")
    return validated_ips


def resolve_and_validate_host(
    hostname: str,
    port: int | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Synchronously resolve and validate a target hostname or IP literal against SSRF rules."""
    cfg = settings or get_settings()

    ip_obj = _try_parse_ip_literal(hostname)
    if ip_obj is not None:
        logger.debug(f"Hostname '{hostname}' parsed as IP literal {ip_obj}")
        _validate_ip_address(ip_obj, hostname, cfg)
        return [str(ip_obj)]

    if _is_malformed_ip_or_numeric_tld(hostname):
        raise URLValidationError("Invalid hostname format")

    if not cfg.ssrf.resolve_dns:
        logger.debug(f"DNS resolution disabled, checking hostname '{hostname}' directly")
        if _is_hostname_blocked(hostname, cfg):
            raise SSRFProtectionError(f"Hostname '{hostname}' appears to be a restricted address")
        return []

    try:
        addr_info = socket.getaddrinfo(hostname, port)
    except (OSError, socket.gaierror) as e:
        logger.warning(f"DNS resolution failed for '{hostname}': {e}")
        raise SSRFProtectionError(f"Cannot resolve hostname '{hostname}'. DNS resolution failed.")

    return _validate_resolved_addr_info(addr_info, hostname, cfg)


async def resolve_and_validate_host_async(
    hostname: str,
    port: int | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Asynchronously resolve and validate a target hostname or IP literal off the event loop."""
    cfg = settings or get_settings()

    ip_obj = _try_parse_ip_literal(hostname)
    if ip_obj is not None:
        logger.debug(f"Hostname '{hostname}' parsed as IP literal {ip_obj}")
        _validate_ip_address(ip_obj, hostname, cfg)
        return [str(ip_obj)]

    if _is_malformed_ip_or_numeric_tld(hostname):
        raise URLValidationError("Invalid hostname format")

    if not cfg.ssrf.resolve_dns:
        logger.debug(f"DNS resolution disabled, checking hostname '{hostname}' directly")
        if _is_hostname_blocked(hostname, cfg):
            raise SSRFProtectionError(f"Hostname '{hostname}' appears to be a restricted address")
        return []

    try:
        addr_info = await asyncio.to_thread(socket.getaddrinfo, hostname, port)
    except (OSError, socket.gaierror) as e:
        logger.warning(f"DNS resolution failed for '{hostname}': {e}")
        raise SSRFProtectionError(f"Cannot resolve hostname '{hostname}'. DNS resolution failed.")

    return _validate_resolved_addr_info(addr_info, hostname, cfg)


def _check_ssrf_protection(hostname: str, settings: Settings) -> None:
    """Validate hostname/IP is not restricted by SSRF rules."""
    resolve_and_validate_host(hostname, None, settings)


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
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        _validate_ip_address(ip_obj.ipv4_mapped, original_hostname, settings)
        return

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


def _is_hostname_blocked(hostname: str, settings: Settings | None = None) -> bool:
    """
    Check if hostname string matches known restricted patterns when DNS resolution is disabled.

    Args:
        hostname: The hostname to check
        settings: Optional Settings instance

    Returns:
        True if the hostname should be blocked
    """
    clean = hostname.strip().strip("[]").rstrip(".").lower()

    if clean in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if clean.endswith(".localhost"):
        return True

    ip_obj = _try_parse_ip_literal(clean)
    if ip_obj is not None:
        try:
            _validate_ip_address(ip_obj, hostname, settings or get_settings())
        except SSRFProtectionError:
            return True
        return False

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
        if sanitized:
            return sanitized[:200]  # Limit length

    # Default user agent
    return "REST-API-Downloader/1.0"


def sanitize_custom_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """Validate and sanitize caller-supplied custom HTTP headers while preserving key casing."""
    if headers is None or len(headers) == 0:
        return None

    sanitized: dict[str, str] = {}
    for raw_name, raw_val in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_val, str):
            raise URLValidationError("Invalid custom header name or value")

        if any(c in raw_name or c in raw_val for c in ("\r", "\n", "\x00")):
            raise URLValidationError("Invalid custom header name or value")

        name = raw_name.strip()
        if not name or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
            raise URLValidationError("Invalid custom header name or value")

        if len(raw_val) > 4096:
            raise URLValidationError("Invalid custom header name or value")

        lower_name = name.lower()
        if lower_name in _FORBIDDEN_CUSTOM_HEADERS:
            raise URLValidationError(f"Custom header '{name}' is not allowed")

        if lower_name == "user-agent":
            sanitized[name] = sanitize_user_agent(raw_val)
        else:
            sanitized[name] = raw_val.strip()

    return sanitized or None
