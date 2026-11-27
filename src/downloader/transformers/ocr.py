"""Image-to-text OCR transformer backed by Pillow, pytesseract, and a dedicated executor."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytesseract
from PIL import Image, UnidentifiedImageError

from ..config import Settings, get_settings
from ..content_encoding import (
    SUPPORTED_IMAGE_MIME_TYPES as SUPPORTED_IMAGE_MIME_TYPES,
)
from ..content_encoding import (
    format_ocr_html as format_ocr_html,
)
from ..content_encoding import (
    format_ocr_markdown as format_ocr_markdown,
)
from ..content_encoding import (
    is_ocr_image_content_type as is_ocr_image_content_type,
)
from ..ports import (
    OCRError as OCRError,
)
from ..ports import (
    OCRProcessingError as OCRProcessingError,
)
from ..ports import (
    OCRUnavailableError as OCRUnavailableError,
)
from ..telemetry import get_tracer

logger = logging.getLogger(__name__)

# Raise Pillow's process-wide ceiling once at import to match OCRConfig.max_image_pixels
# upper bound (200M); exact per-request caps are enforced deterministically on img.size before load().
if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < 200_000_000:
    Image.MAX_IMAGE_PIXELS = 200_000_000

_LANG_PATTERN = re.compile(r"^[a-zA-Z0-9_+]{1,64}$")

_ocr_executor: ThreadPoolExecutor | None = None
_ocr_executor_lock = threading.Lock()


def _get_ocr_executor() -> ThreadPoolExecutor:
    """Return a dedicated bounded thread pool so CPU-bound OCR never starves DNS lookups."""
    global _ocr_executor
    with _ocr_executor_lock:
        if _ocr_executor is None:
            workers = max(2, min(4, os.cpu_count() or 2))
            _ocr_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="downloader-ocr",
            )
        return _ocr_executor


def shutdown_ocr_executor() -> None:
    """Shut down the dedicated OCR thread pool if initialized."""
    global _ocr_executor
    with _ocr_executor_lock:
        executor = _ocr_executor
        _ocr_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


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
    # Enforce the per-request pixel cap explicitly on lazy img.size before decoding pixels.
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
            pixel_count = width * height
            if pixel_count > max_image_pixels:
                raise OCRProcessingError(
                    f"Image dimensions ({width}x{height} = {pixel_count} pixels) exceed "
                    f"configured maximum ({max_image_pixels} pixels)"
                )
            img.load()
            text: str = pytesseract.image_to_string(
                img,
                lang=language,
                timeout=timeout,
            )
            return _normalize_ocr_text(text)
    except OCRProcessingError:
        raise
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
    """Extract text from image bytes asynchronously in the dedicated OCR thread pool."""
    cfg = settings or get_settings()
    if not cfg.ocr.enabled:
        raise OCRUnavailableError(
            "OCR extraction is disabled by configuration (OCR_ENABLED=false)."
        )

    effective_lang = validate_ocr_language(language, cfg.ocr.default_language)
    loop = asyncio.get_running_loop()
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("ocr.extract_text") as span:
        span.set_attribute("ocr.language", effective_lang)
        span.set_attribute("ocr.input_bytes", len(image_bytes))
        text = await loop.run_in_executor(
            _get_ocr_executor(),
            _run_tesseract_sync,
            image_bytes,
            effective_lang,
            cfg.ocr.timeout,
            cfg.ocr.max_image_pixels,
        )
        span.set_attribute("ocr.output_chars", len(text))
        return text
