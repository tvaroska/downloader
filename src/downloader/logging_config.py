"""Structured logging configuration with separate access and error log handlers."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from copy import copy
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pythonjsonlogger.json import JsonFormatter

from .config import LoggingConfig
from .telemetry import get_current_trace_context
from .validation import format_origin_url

_UVICORN_ACCESS_RE = re.compile(r'"([A-Z]+)\s+(\S+)\s+(HTTP/[^"]+)"\s+(\d{3})')
_SCHEME_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_INTERNAL_EXACT_PATHS = frozenset(
    {
        "/",
        "/health",
        "/render",
        "/batch",
        "/metrics",
        "/schedules",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)
_INTERNAL_PATH_PREFIXES = ("/metrics/", "/jobs/", "/schedules/", "/docs/")
_STANDARD_RECORD_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "stack_info",
        "taskName",
    }
)


def _redact_target_url_segment(raw_segment: str) -> str:
    decoded = unquote(unquote(raw_segment)).strip()
    base = decoded.split("?", 1)[0].split("#", 1)[0]
    if not base or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in base):
        return "/<redacted-target>"
    candidate = base if _SCHEME_PREFIX_RE.match(base) else f"http://{base}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return "/<redacted-target>"
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return "/<redacted-target>"
    scheme = (parsed.scheme or "http").lower()
    clean_path = quote(parsed.path or "/", safe="/!$&'()*+,;=-._~")
    if scheme in {"http", "https"}:
        eff_port = port if port is not None else (443 if scheme == "https" else 80)
        return f"/{format_origin_url(scheme, host, eff_port)}{clean_path}"
    host_part = f"[{host}]" if ":" in host else host
    port_part = f":{port}" if port is not None else ""
    return f"/{scheme}://{host_part}{port_part}{clean_path}"


def redact_access_path(raw_path: str) -> str:
    """Strip outer queries and embedded target-URL userinfo/query tokens from an access path."""
    path_only = raw_path.split("?", 1)[0].split("#", 1)[0]
    if not path_only.startswith("/"):
        return "/<redacted-target>"
    if path_only in _INTERNAL_EXACT_PATHS:
        return path_only
    if path_only.startswith(_INTERNAL_PATH_PREFIXES) and "@" not in unquote(path_only):
        return path_only
    return _redact_target_url_segment(path_only.lstrip("/"))


def _sanitize_access_record(record: logging.LogRecord) -> logging.LogRecord:
    if record.name != "uvicorn.access":
        return record
    sanitized = copy(record)
    sanitized.__dict__.pop("message", None)
    sanitized.__dict__.pop("color_message", None)
    if isinstance(sanitized.args, tuple) and len(sanitized.args) >= 5:
        args_list = list(sanitized.args)
        args_list[2] = redact_access_path(str(args_list[2]))
        sanitized.args = tuple(args_list)
    elif isinstance(sanitized.msg, str):
        sanitized.msg = _UVICORN_ACCESS_RE.sub(
            lambda m: f'"{m.group(1)} {redact_access_path(m.group(2))} {m.group(3)}" {m.group(4)}',
            sanitized.msg,
        )
    return sanitized


def _attach_uvicorn_access_fields(log_record: dict[str, Any], record: logging.LogRecord) -> None:
    if isinstance(record.args, tuple) and len(record.args) >= 5:
        method = str(record.args[1])
        path = redact_access_path(str(record.args[2]))
        raw_status = record.args[4]
        try:
            status_code: int | str = int(str(raw_status))
        except (TypeError, ValueError):
            status_code = str(raw_status)
        log_record.setdefault("method", method)
        log_record.setdefault("path", path)
        log_record.setdefault("status_code", status_code)
        return

    match = _UVICORN_ACCESS_RE.search(record.getMessage())
    if match:
        log_record.setdefault("method", match.group(1))
        log_record.setdefault("path", redact_access_path(match.group(2)))
        log_record.setdefault("status_code", int(match.group(4)))


class RedactingAccessFormatter(logging.Formatter):
    """Plain-text access log formatter redacting target URL credentials and query tokens."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(_sanitize_access_record(record))


class CustomJsonFormatter(JsonFormatter):
    """JSON log formatter enriching records with W3C trace context and HTTP access fields."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(_sanitize_access_record(record))

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)

        log_record["logger"] = record.name
        log_record["level"] = record.levelname
        log_record["timestamp"] = self.formatTime(record, self.datefmt)

        trace_id, span_id = get_current_trace_context()
        if trace_id:
            log_record.setdefault("trace_id", trace_id)
        if span_id:
            log_record.setdefault("span_id", span_id)

        if record.name == "uvicorn.access":
            _attach_uvicorn_access_fields(log_record, record)

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS:
                log_record[key] = value


def _build_stream_or_file_handler(
    file_path: str | None,
    fallback_stream: Any,
    level: str,
    formatter: logging.Formatter,
    rotation: tuple[int, int],
) -> logging.Handler:
    rotation_size, rotation_count = rotation
    handler: logging.Handler
    if file_path:
        path_obj = Path(file_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            filename=str(path_obj),
            maxBytes=rotation_size,
            backupCount=rotation_count,
            encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler(fallback_stream)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def setup_logging(config: LoggingConfig) -> None:
    """Configure application error and Uvicorn access logging."""
    formatter: logging.Formatter = (
        CustomJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        if config.json_logs
        else logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    access_formatter: logging.Formatter = (
        formatter
        if config.json_logs
        else RedactingAccessFormatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    rotation = (config.log_rotation_size, config.log_rotation_count)
    root_logger = logging.getLogger()
    root_logger.setLevel(config.log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(
        _build_stream_or_file_handler(
            config.error_log_file,
            sys.stderr,
            config.log_level,
            formatter,
            rotation,
        )
    )

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(config.access_log_level)
    access_logger.propagate = False
    access_logger.handlers.clear()
    access_logger.addHandler(
        _build_stream_or_file_handler(
            config.access_log_file,
            sys.stdout,
            config.access_log_level,
            access_formatter,
            rotation,
        )
    )

    logging.getLogger("uvicorn").setLevel(config.log_level)
    logging.getLogger("uvicorn.error").setLevel(config.log_level)
    logging.getLogger("downloader").setLevel(config.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    root_logger.info("Logging configured successfully")
    root_logger.info(f"Log level: {config.log_level}")
    root_logger.info(f"Access log level: {config.access_log_level}")
    root_logger.info(f"JSON logs: {config.json_logs}")
    root_logger.info(f"Error logs: {config.error_log_file or 'stderr'}")
    root_logger.info(f"Access logs: {config.access_log_file or 'stdout'}")
