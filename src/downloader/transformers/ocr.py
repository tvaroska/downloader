"""Image-to-text OCR transformer backed by Pillow and pytesseract."""

from __future__ import annotations

import asyncio
import io
import logging
import re

import pytesseract
from PIL import Image, UnidentifiedImageError

from ..config import Settings, get_settings
from ..telemetry import get_tracer

logger = logging.getLogger(__name__)

_LANG_PATTERN = re.compile(r"^[a-zA-Z0-9_+]{1,64}$")
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/gif",
    }
)


class OCRError(Exception):
    """Base error for image OCR extraction failures."""


class OCRUnavailableError(OCRError):
    """Raised when OCR is disabled or the system tesseract binary is missing."""


class OCRProcessingError(OCRError):
    """Raised when an image payload cannot be decoded or OCR fails/times out."""


def is_ocr_image_content_type(content_type: str | None) -> bool:
    """Return True if the MIME type is a supported raster image for OCR."""
    if not content_type:
        return False
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime in SUPPORTED_IMAGE_MIME_TYPES


def validate_ocr_language(language: str | None, default_language: str) -> str:
    """Validate and return a safe Tesseract language string."""
    effective = (language or default_language).strip()
    if not _LANG_PATTERN.match(effective):
        raise ValueError(
            "Invalid OCR language code; expected alphanumeric characters, '+' or '_' (e.g. 'eng', 'eng+deu')."
        )
    return effective


def _run_tesseract_sync(
    image_bytes: bytes,
    language: str,
    timeout: float,
    max_image_pixels: int,
) -> str:
    # Protect against decompression bombs before decoding arbitrary remote images.
    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_image_pixels
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            text: str = pytesseract.image_to_string(
                img,
                lang=language,
                timeout=timeout,
            )
            return _normalize_ocr_text(text)
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRUnavailableError(
            "OCR engine (tesseract-ocr) is not installed on this runtime."
        ) from exc
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise OCRProcessingError(f"Invalid or unsupported image payload: {exc}") from exc
    except RuntimeError as exc:
        raise OCRProcessingError(f"OCR processing timed out or failed: {exc}") from exc
    except pytesseract.TesseractError as exc:
        raise OCRProcessingError(f"Tesseract OCR failed: {exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit


def _normalize_ocr_text(raw: str) -> str:
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


async def extract_text_from_image(
    image_bytes: bytes,
    *,
    language: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Extract text from image bytes asynchronously in a worker thread."""
    cfg = settings or get_settings()
    if not cfg.ocr.enabled:
        raise OCRUnavailableError(
            "OCR extraction is disabled by configuration (OCR_ENABLED=false)."
        )

    effective_lang = validate_ocr_language(language, cfg.ocr.default_language)
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("ocr.extract_text") as span:
        span.set_attribute("ocr.language", effective_lang)
        span.set_attribute("ocr.input_bytes", len(image_bytes))
        text = await asyncio.to_thread(
            _run_tesseract_sync,
            image_bytes,
            effective_lang,
            cfg.ocr.timeout,
            cfg.ocr.max_image_pixels,
        )
        span.set_attribute("ocr.output_chars", len(text))
        return text


def format_ocr_markdown(text: str, source_url: str | None = None) -> str:
    """Wrap extracted OCR text into clean Markdown paragraphs."""
    body = text.strip()
    if not body:
        return ""
    if source_url:
        return f"<!-- OCR extracted from {source_url} -->\n\n{body}"
    return body
