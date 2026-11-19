"""Structured logging configuration with separate access and error log handlers."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from .config import LoggingConfig
from .telemetry import get_current_trace_context

_UVICORN_ACCESS_RE = re.compile(r'"([A-Z]+)\s+(\S+)\s+HTTP/[^"]+"\s+(\d{3})')
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


def _attach_uvicorn_access_fields(log_record: dict[str, Any], record: logging.LogRecord) -> None:
    if isinstance(record.args, tuple) and len(record.args) >= 5:
        method = str(record.args[1])
        path = str(record.args[2]).split("?", 1)[0]
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
        log_record.setdefault("path", match.group(2).split("?", 1)[0])
        log_record.setdefault("status_code", int(match.group(3)))


class CustomJsonFormatter(JsonFormatter):
    """JSON log formatter enriching records with W3C trace context and HTTP access fields."""

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
        else logging.Formatter(
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
