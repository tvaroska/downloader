"""Pure Accept-header content negotiation and canonical format/MIME mapping table."""

from __future__ import annotations

import math
from urllib.parse import quote, urlsplit, urlunsplit

FORMAT_MIME_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("text", "text/plain", ("text/plain",)),
    ("html", "text/html", ("text/html",)),
    ("markdown", "text/markdown", ("text/markdown", "text/x-markdown")),
    ("pdf", "application/pdf", ("application/pdf",)),
    ("screenshot", "image/png", ("image/png",)),
    ("screenshot_jpeg", "image/jpeg", ("image/jpeg", "image/jpg")),
    ("json", "application/json", ("application/json",)),
)

FORMAT_TO_MIME: dict[str, str] = {fmt: primary for fmt, primary, _ in FORMAT_MIME_SPECS}
MIME_TO_FORMAT: dict[str, str] = {
    alias: fmt for fmt, _, aliases in FORMAT_MIME_SPECS for alias in aliases
}


def format_to_mime_type(format_type: str) -> str:
    """Convert internal format name to its canonical MIME type."""
    return FORMAT_TO_MIME.get(format_type, "application/octet-stream")


def _parse_q_value(params: list[str]) -> float:
    """Extract RFC 9110 quality factor `q` (default 1.0; invalid/<=0 treated as 0.0)."""
    for param in params:
        key, sep, val = param.strip().partition("=")
        if sep and key.strip().lower() == "q":
            try:
                q = float(val.strip())
            except ValueError:
                return 0.0
            if math.isnan(q) or q <= 0.0:
                return 0.0
            return min(q, 1.0)
    return 1.0


def parse_accept_header(accept_header: str | None) -> str:
    """Parse a single `Accept` header string into the highest-priority internal format name."""
    if not accept_header:
        return "text"
    ranked = parse_accept_headers(accept_header)
    return ranked[0] if ranked else "raw"


def parse_accept_headers(accept_headers: str | list[str] | None) -> list[str]:
    """Parse `Accept` header(s) into deduplicated requested format names ordered by `q` descending."""
    if not accept_headers:
        return []

    header_list = [accept_headers] if isinstance(accept_headers, str) else accept_headers
    best_by_fmt: dict[str, tuple[float, int]] = {}
    order = 0

    for header in header_list:
        for part in header.split(","):
            raw = part.strip()
            if not raw:
                continue
            media_type, *params = raw.split(";")
            fmt = MIME_TO_FORMAT.get(media_type.strip().lower())
            if fmt is None:
                continue
            q = _parse_q_value(params)
            if q <= 0.0:
                continue
            prev = best_by_fmt.get(fmt)
            if prev is None:
                best_by_fmt[fmt] = (q, order)
            else:
                best_by_fmt[fmt] = (max(prev[0], q), prev[1])
            order += 1

    ranked = sorted(best_by_fmt.items(), key=lambda item: (-item[1][0], item[1][1]))
    return [fmt for fmt, _ in ranked]


def sanitize_original_url_header(url: str) -> str:
    """Ensure an `X-Original-URL` header value is ASCII-safe for Starlette Latin-1 encoding."""
    if url.isascii():
        return url
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if host and not host.isascii():
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = quote(host, safe=".-_:")
    else:
        ascii_host = host
    host_part = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    try:
        port = parsed.port
    except ValueError:
        port = None
    port_part = f":{port}" if port is not None else ""
    userinfo = ""
    if parsed.username is not None:
        user = quote(parsed.username, safe="")
        userinfo = (
            f"{user}:{quote(parsed.password, safe='')}@"
            if parsed.password is not None
            else f"{user}@"
        )
    netloc = f"{userinfo}{host_part}{port_part}" if ascii_host else parsed.netloc
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            quote(parsed.path, safe="/:@!$&'()*+,;=-._~%"),
            quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%"),
            quote(parsed.fragment, safe="/?:@!$&'()*+,;=-._~%"),
        )
    )
