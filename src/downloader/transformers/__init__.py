"""Content transformation utilities for converting between formats."""

from typing import TYPE_CHECKING, Any

from ..content_encoding import format_ocr_markdown, is_ocr_image_content_type
from ..ports import OCRError, OCRProcessingError, OCRUnavailableError
from .common import DEFAULT_STRIP_TAGS, MAIN_CONTENT_SELECTORS, extract_main_content_node
from .markdown import html_to_markdown
from .plaintext import html_to_plaintext

if TYPE_CHECKING:
    from .ocr import extract_text_from_image, shutdown_ocr_executor, validate_ocr_language

__all__ = [
    "DEFAULT_STRIP_TAGS",
    "MAIN_CONTENT_SELECTORS",
    "OCRError",
    "OCRProcessingError",
    "OCRUnavailableError",
    "extract_main_content_node",
    "extract_text_from_image",
    "format_ocr_markdown",
    "html_to_markdown",
    "html_to_plaintext",
    "is_ocr_image_content_type",
    "shutdown_ocr_executor",
    "validate_ocr_language",
]


def __getattr__(name: str) -> Any:
    if name in {"extract_text_from_image", "shutdown_ocr_executor", "validate_ocr_language"}:
        from . import ocr

        return getattr(ocr, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
