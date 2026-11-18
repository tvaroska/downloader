"""Pure Accept-header content negotiation and canonical format/MIME mapping table."""

from __future__ import annotations

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
