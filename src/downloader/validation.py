"""URL validation, SSRF protection, canonical origin helpers, and EgressPolicy port."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit

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
_OCTET_RE = re.compile(r"(0x[0-9a-f]+|0[0-7]*|[1-9]\d*)")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class URLValidationError(Exception):
    """Raised when URL validation fails."""


class SSRFProtectionError(URLValidationError):
    """Raised when URL is blocked by SSRF protection."""


class DNSResolutionError(SSRFProtectionError):
    """Raised when target hostname DNS resolution fails."""

    def __init__(self, hostname: str, detail: str | None = None) -> None:
        self.hostname = hostname
        msg = f"Cannot resolve hostname '{hostname}'. DNS resolution failed."
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


@dataclass(frozen=True)
class EgressPolicy:
    """Port binding outbound URL and DNS SSRF validation to explicit Settings."""

    settings: Settings

    async def validate_url(self, url: str) -> str:
        return await validate_url_async(url, self.settings)

    async def resolve_host(self, hostname: str, port: int | None = None) -> list[str]:
        return await resolve_and_validate_host_async(hostname, port, self.settings)


def canonical_origin(url: str) -> tuple[str, str, int] | None:
    """Return canonical `(scheme, host, port)` stripped of userinfo and trailing dots."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = (parsed.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return None
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise URLValidationError(f"Invalid URL port: {exc}") from exc
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def format_origin_url(scheme: str, host: str, port: int) -> str:
    """Format canonical `(scheme, host, port)` as an origin URL without userinfo."""
    host_part = f"[{host}]" if ":" in host else host
    is_default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    return f"{scheme}://{host_part}" if is_default else f"{scheme}://{host_part}:{port}"


def canonical_host(domain_or_url: str) -> str:
    """Extract canonical lowercase hostname stripped of scheme, userinfo, port, and trailing dot."""
    raw = domain_or_url.strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"//{raw}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    return (parsed.hostname or "").rstrip(".").lower()


def redact_url_for_log(url: str) -> str:
    """Strip userinfo credentials from a URL before logging."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if not parsed.hostname:
        return url
    scheme = (parsed.scheme or "http").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    eff_port = port if port is not None else (443 if scheme == "https" else 80)
    origin = format_origin_url(scheme, parsed.hostname.rstrip(".").lower(), eff_port)
    path = parsed.path or "/"
    return f"{origin}{path}"


def _parse_and_normalize_url(url: str) -> tuple[str, str, int | None]:
    """Normalize and validate URL syntax, returning `(normalized_url, hostname, port)`."""
    if not url or not isinstance(url, str):
        raise URLValidationError("URL must be a non-empty string")

    normalized = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", normalized):
        normalized = f"http://{normalized}"

    try:
        parsed = urlparse(normalized)
    except ValueError as exc:
        raise URLValidationError(f"Invalid URL format: {exc}") from exc

    if parsed.scheme == "file":
        raise URLValidationError("Access to local file system via file:// URLs is not allowed")
    if parsed.scheme not in ("http", "https"):
        raise URLValidationError("URL must use http or https scheme")
    if not parsed.hostname:
        raise URLValidationError("URL must have a valid hostname")
    if not re.match(r"^[a-zA-Z0-9._:-]+$", parsed.hostname):
        raise URLValidationError("Invalid hostname format")

    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError(f"Invalid URL port: {exc}") from exc

    return normalized, parsed.hostname, port


def _parse_ipv4_part(part: str) -> int | None:
    if not _OCTET_RE.fullmatch(part):
        return None
    if part.startswith("0x"):
        return int(part, 16)
    if len(part) > 1 and part.startswith("0"):
        return int(part, 8)
    return int(part, 10)


def _pack_shorthand_ipv4(nums: list[int]) -> int | None:
    count = len(nums)
    if count == 1 and 0 <= nums[0] <= 0xFFFFFFFF:
        return nums[0]
    if count == 2 and 0 <= nums[0] <= 0xFF and 0 <= nums[1] <= 0xFFFFFF:
        return (nums[0] << 24) | nums[1]
    if count == 3 and 0 <= nums[0] <= 0xFF and 0 <= nums[1] <= 0xFF and 0 <= nums[2] <= 0xFFFF:
        return (nums[0] << 24) | (nums[1] << 16) | nums[2]
    if count == 4 and all(0 <= x <= 0xFF for x in nums):
        return (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
    return None


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
    parsed_parts = [_parse_ipv4_part(p) for p in parts]
    if any(val is None for val in parsed_parts):
        return None
    packed = _pack_shorthand_ipv4([val for val in parsed_parts if val is not None])
    return ipaddress.IPv4Address(packed) if packed is not None else None


def _is_malformed_ip_or_numeric_tld(hostname: str) -> bool:
    """Detect malformed IPv6 or invalid numeric/hex TLD hostnames (RFC 1123 §2.1 / RFC 3696 §2)."""
    clean = hostname.strip().strip("[]").split("%", 1)[0].rstrip(".").lower()
    if not clean or ":" in clean:
        return True
    return bool(re.fullmatch(r"(0x[0-9a-f]+|\d+)", clean.rsplit(".", 1)[-1]))


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
        raise DNSResolutionError(hostname)

    logger.debug(f"Resolved '{hostname}' to {len(validated_ips)} IP address(es)")
    return validated_ips


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
    except (OSError, socket.gaierror) as exc:
        logger.warning(f"DNS resolution failed for '{hostname}': {exc}")
        raise DNSResolutionError(hostname) from exc

    return _validate_resolved_addr_info(addr_info, hostname, cfg)


def _classify_restricted_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    settings: Settings,
) -> tuple[str, str] | None:
    """Return `(log_tag, message_fragment)` when `ip_obj` falls in a blocked range.

    Specificity order matters because ranges overlap in stdlib `ipaddress`
    (for example `0.0.0.0` is both unspecified and private, and `169.254.169.254`
    is cloud metadata, link-local, and private).
    """
    ip_str = str(ip_obj)
    checks: tuple[tuple[bool, str, str], ...] = (
        (ip_obj.is_loopback, "loopback", "loopback private addresses"),
        (ip_obj.is_unspecified, "unspecified", "unspecified addresses"),
        (
            settings.ssrf.block_cloud_metadata and ip_str in {"169.254.169.254", "fd00:ec2::254"},
            "cloud metadata",
            "cloud metadata endpoints",
        ),
        (ip_obj.is_link_local, "link-local", "link-local addresses"),
        (ip_obj.is_multicast, "multicast", "multicast addresses"),
        (ip_obj.is_reserved, "reserved", "reserved IP addresses"),
        (
            settings.ssrf.block_private_ips and ip_obj.is_private,
            "private IP",
            "private IP and private addresses",
        ),
        # RFC 6598 100.64.0.0/10 has is_private=False in stdlib ipaddress; deny all non-global IPs.
        (
            settings.ssrf.block_private_ips and not ip_obj.is_global,
            "non-global IP",
            "non-global IP addresses",
        ),
    )
    for matched, tag, label in checks:
        if matched:
            return (tag, label)
    return None


def _validate_ip_address(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    original_hostname: str,
    settings: Settings,
) -> None:
    """Validate that an IP address is not in a restricted range."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        _validate_ip_address(ip_obj.ipv4_mapped, original_hostname, settings)
        return

    ip_str = str(ip_obj)
    blocked = _classify_restricted_ip(ip_obj, settings)
    if blocked is not None:
        tag, label = blocked
        logger.warning(f"SSRF attempt blocked: {original_hostname} -> {ip_str} ({tag})")
        raise SSRFProtectionError(
            f"Access to {label} is not allowed (hostname: {original_hostname}, IP: {ip_str})"
        )

    logger.debug(f"IP address {ip_str} (from {original_hostname}) passed SSRF checks")


def _is_hostname_blocked(hostname: str, settings: Settings | None = None) -> bool:
    """Check if hostname string matches restricted patterns when DNS resolution is disabled."""
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


def sanitize_user_agent(user_agent: str | None = None) -> str:
    """Sanitize or provide default User-Agent header."""
    if user_agent:
        sanitized = re.sub(r"[^\w\s\-\.\(\)/;:,]", "", user_agent)
        if sanitized:
            return sanitized[:200]
    return "REST-API-Downloader/1.0"


def sanitize_custom_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """Validate and sanitize caller-supplied custom HTTP headers while preserving key casing."""
    if not headers:
        return None

    sanitized: dict[str, str] = {}
    for raw_name, raw_val in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_val, str):
            raise URLValidationError("Invalid custom header name or value")
        if any(c in raw_name or c in raw_val for c in ("\r", "\n", "\x00")):
            raise URLValidationError("Invalid custom header name or value")
        name = raw_name.strip()
        if not name or not _HEADER_NAME_RE.fullmatch(name) or len(raw_val) > 4096:
            raise URLValidationError("Invalid custom header name or value")
        lower_name = name.lower()
        if lower_name in _FORBIDDEN_CUSTOM_HEADERS:
            raise URLValidationError(f"Custom header '{name}' is not allowed")
        sanitized[name] = (
            sanitize_user_agent(raw_val) if lower_name == "user-agent" else raw_val.strip()
        )

    return sanitized or None
