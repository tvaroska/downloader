"""Pure Accept-header content negotiation and canonical format/MIME mapping table."""

from __future__ import annotations

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


_format_to_mime_type = format_to_mime_type


def parse_accept_header(accept_header: str | None) -> str:
    """Parse a single `Accept` header string into an internal format name."""
    if not accept_header:
        return "text"

    lowered = accept_header.lower()
    for fmt, _, aliases in FORMAT_MIME_SPECS:
        if any(alias in lowered for alias in aliases):
            return fmt
    return "raw"


def parse_accept_headers(accept_headers: str | list[str] | None) -> list[str]:
    """Parse `Accept` header(s) into deduplicated requested format names in caller order."""
    if not accept_headers:
        return []

    header_list = [accept_headers] if isinstance(accept_headers, str) else accept_headers
    seen: set[str] = set()
    unique_formats: list[str] = []

    for header in header_list:
        for part in header.split(","):
            media_type = part.strip().split(";", 1)[0].strip().lower()
            fmt = MIME_TO_FORMAT.get(media_type)
            if fmt is not None and fmt not in seen:
                seen.add(fmt)
                unique_formats.append(fmt)

    return unique_formats


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
