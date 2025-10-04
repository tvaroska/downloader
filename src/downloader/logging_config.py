"""Structured logging configuration with separate access and error log handlers.

This module configures Python logging with:
- JSON structured logging for production
- Separate handlers for access logs (uvicorn) and error logs (application)
- Log rotation support
- Environment-specific configuration
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger

from .config import LoggingConfig


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter that adds additional context fields."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        """Add custom fields to the log record."""
        super().add_fields(log_record, record, message_dict)

        # Add standard fields
        log_record["logger"] = record.name
        log_record["level"] = record.levelname
        log_record["timestamp"] = self.formatTime(record, self.datefmt)

        # Add exception info if present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        # Add extra fields from record
        for key, value in record.__dict__.items():
            if key not in [
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
            ]:
                log_record[key] = value


def setup_logging(config: LoggingConfig) -> None:
    """Configure logging with separate access and error handlers.

    Args:
        config: Logging configuration settings
    """
    # Determine formatters
    if config.json_logs:
        # JSON structured logging for production
        formatter = CustomJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        # Human-readable logging for development
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(config.log_level)
    root_logger.handlers.clear()

    # ============================================
    # ERROR LOG HANDLER (Application Logs)
    # ============================================
    if config.error_log_file:
        # File handler with rotation for error logs
        error_log_path = Path(config.error_log_file)
        error_log_path.parent.mkdir(parents=True, exist_ok=True)

        error_handler = logging.handlers.RotatingFileHandler(
            filename=str(error_log_path),
            maxBytes=config.log_rotation_size,
            backupCount=config.log_rotation_count,
            encoding="utf-8",
        )
        error_handler.setLevel(config.log_level)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)
    else:
        # Console handler for error logs (stderr)
        error_handler = logging.StreamHandler(sys.stderr)
        error_handler.setLevel(config.log_level)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)

    # ============================================
    # ACCESS LOG HANDLER (Uvicorn Access Logs)
    # ============================================
    # Configure uvicorn access logger separately
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(config.access_log_level)
    access_logger.propagate = False  # Don't propagate to root logger
    access_logger.handlers.clear()

    if config.access_log_file:
        # Separate file handler for access logs
        access_log_path = Path(config.access_log_file)
        access_log_path.parent.mkdir(parents=True, exist_ok=True)

        access_handler = logging.handlers.RotatingFileHandler(
            filename=str(access_log_path),
            maxBytes=config.log_rotation_size,
            backupCount=config.log_rotation_count,
            encoding="utf-8",
        )
        access_handler.setLevel(config.access_log_level)

        # Access logs typically use a different format
        if config.json_logs:
            access_handler.setFormatter(formatter)
        else:
            # Standard access log format
            access_formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            access_handler.setFormatter(access_formatter)

        access_logger.addHandler(access_handler)
    else:
        # Console handler for access logs (stdout)
        access_handler = logging.StreamHandler(sys.stdout)
        access_handler.setLevel(config.access_log_level)

        if config.json_logs:
            access_handler.setFormatter(formatter)
        else:
            access_formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            access_handler.setFormatter(access_formatter)

        access_logger.addHandler(access_handler)

    # ============================================
    # CONFIGURE OTHER LOGGERS
    # ============================================

    # Uvicorn server logger (startup, shutdown messages)
    server_logger = logging.getLogger("uvicorn")
    server_logger.setLevel(config.log_level)

    # Uvicorn error logger
    error_logger = logging.getLogger("uvicorn.error")
    error_logger.setLevel(config.log_level)

    # Application loggers
    app_logger = logging.getLogger("src.downloader")
    app_logger.setLevel(config.log_level)

    # Suppress overly verbose third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)

    # Log configuration summary
    root_logger.info("Logging configured successfully")
    root_logger.info(f"Log level: {config.log_level}")
    root_logger.info(f"Access log level: {config.access_log_level}")
    root_logger.info(f"JSON logs: {config.json_logs}")

    if config.error_log_file:
        root_logger.info(f"Error logs: {config.error_log_file}")
    else:
        root_logger.info("Error logs: stderr")

    if config.access_log_file:
        root_logger.info(f"Access logs: {config.access_log_file}")
    else:
        root_logger.info("Access logs: stdout")


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name.

    Args:
        name: Logger name (typically __name__ of the module)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


def log_with_context(logger: logging.Logger, level: int, message: str, **context) -> None:
    """Log a message with additional context fields.

    This is particularly useful with JSON logging, where context fields
    become searchable/filterable attributes.

    Args:
        logger: Logger instance
        level: Log level (logging.INFO, logging.ERROR, etc.)
        message: Log message
        **context: Additional context fields to include in the log

    Example:
        log_with_context(
            logger, logging.INFO,
            "Download completed",
            url="https://example.com",
            status_code=200,
            size_bytes=1024,
            duration_ms=234
        )
    """
    logger.log(level, message, extra=context)
