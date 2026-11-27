"""URL validation, SSRF protection, canonical origin helpers, and EgressPolicy port."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

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
_CLOUD_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200"})
_CLOUD_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})


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


def origin_key(url: str) -> str:
    """Return canonical `scheme://host[:port]` origin key without userinfo or trailing dots."""
    origin = canonical_origin(url)
    if origin is None:
        raise URLValidationError(f"Invalid URL for origin key: {url}")
    return format_origin_url(*origin)


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


def _encode_ascii_host(raw_host: str) -> str:
    """Punycode IDN hostnames and validate allowed ASCII hostname characters."""
    ascii_host = raw_host
    if not raw_host.isascii():
        try:
            ascii_host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise URLValidationError("Invalid hostname format") from exc
    if not re.match(r"^[a-zA-Z0-9._:-]+$", ascii_host):
        raise URLValidationError("Invalid hostname format")
    return ascii_host


def _build_canonical_ascii_url(
    scheme: str,
    parsed: object,
    ascii_host: str,
    port: int | None,
) -> str:
    """Reconstruct an ASCII-safe URL with percent-encoded userinfo, path, query, and fragment."""
    from urllib.parse import SplitResult

    assert isinstance(parsed, SplitResult)
    userinfo = ""
    if parsed.username is not None:
        user = quote(parsed.username, safe="")
        userinfo = (
            f"{user}:{quote(parsed.password, safe='')}@"
            if parsed.password is not None
            else f"{user}@"
        )
    host_part = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    port_part = f":{port}" if port is not None else ""
    netloc = f"{userinfo}{host_part}{port_part}"
    return urlunsplit(
        (
            scheme,
            netloc,
            quote(parsed.path, safe="/:@!$&'()*+,;=-._~%"),
            quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%"),
            quote(parsed.fragment, safe="/?:@!$&'()*+,;=-._~%"),
        )
    )


def _validate_raw_url_string(url: str) -> str:
    """Validate raw URL string for emptiness, control chars, whitespace, and backslashes."""
    if not url or not isinstance(url, str):
        raise URLValidationError("URL must be a non-empty string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise URLValidationError("URL must not contain control characters")
    normalized = url.strip()
    if not normalized:
        raise URLValidationError("URL must be a non-empty string")
    if any(ch.isspace() or ch == "\\" for ch in normalized):
        raise URLValidationError("URL must not contain whitespace or backslashes")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", normalized):
        return f"http://{normalized}"
    return normalized


def _parse_and_normalize_url(url: str) -> tuple[str, str, int | None]:
    """Normalize and validate URL syntax, returning `(ascii_canonical_url, hostname, port)`."""
    normalized = _validate_raw_url_string(url)

    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise URLValidationError(f"Invalid URL format: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme == "file":
        raise URLValidationError("Access to local file system via file:// URLs is not allowed")
    if scheme not in ("http", "https"):
        raise URLValidationError("URL must use http or https scheme")
    if not parsed.hostname:
        raise URLValidationError("URL must have a valid hostname")

    ascii_host = _encode_ascii_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError(f"Invalid URL port: {exc}") from exc

    canonical_url = _build_canonical_ascii_url(scheme, parsed, ascii_host, port)
    return canonical_url, ascii_host, port


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
    addr_info: Sequence[tuple[object, object, object, object, tuple[object, ...]]],
    hostname: str,
    cfg: Settings,
) -> list[str]:
    """Validate all resolved getaddrinfo records and return deduplicated IP literals."""
    validated_ips: list[str] = []
    seen: set[str] = set()
    for info in addr_info:
        sockaddr = info[4]
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
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

    clean_host = hostname.strip().strip("[]").rstrip(".").lower()
    if cfg.ssrf.block_cloud_metadata and clean_host in _CLOUD_METADATA_HOSTS:
        raise SSRFProtectionError(
            f"Access to cloud metadata endpoints is not allowed (hostname: {hostname})"
        )

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
    if ip_str in _CLOUD_METADATA_IPS:
        if settings.ssrf.block_cloud_metadata:
            return ("cloud metadata", "cloud metadata endpoints")
        if not settings.ssrf.block_private_ips:
            return None
    checks: tuple[tuple[bool, str, str], ...] = (
        (ip_obj.is_loopback, "loopback", "loopback private addresses"),
        (ip_obj.is_unspecified, "unspecified", "unspecified addresses"),
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
    cfg = settings or get_settings()
    clean = hostname.strip().strip("[]").rstrip(".").lower()
    if clean in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if clean.endswith(".localhost"):
        return True
    if cfg.ssrf.block_cloud_metadata and clean in _CLOUD_METADATA_HOSTS:
        return True

    ip_obj = _try_parse_ip_literal(clean)
    if ip_obj is not None:
        try:
            _validate_ip_address(ip_obj, hostname, cfg)
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
