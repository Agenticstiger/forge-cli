# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
FLUID Build - Structured Logging Configuration

Provides structured logging with JSON output, correlation IDs, and context.
"""

import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class StructuredFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.

    Outputs log records as JSON with consistent fields for easy parsing.
    """

    def __init__(self, include_context: bool = True):
        """
        Initialize formatter.

        Args:
            include_context: Whether to include extra context fields
        """
        super().__init__()
        self.include_context = include_context

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON"""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation ID if present
        if hasattr(record, "correlation_id"):
            log_data["correlation_id"] = record.correlation_id

        # Add file location
        log_data["location"] = {
            "file": record.pathname,
            "line": record.lineno,
            "function": record.funcName,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info),
            }

        # Add extra context fields
        if self.include_context:
            # Get custom fields (anything added via extra={} in logging call)
            custom_fields = {
                key: value
                for key, value in record.__dict__.items()
                if key
                not in {
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
                    "correlation_id",
                }
            }
            if custom_fields:
                log_data["context"] = custom_fields

        return json.dumps(log_data)


class ContextFilter(logging.Filter):
    """
    Add correlation ID to all log records.

    Helps trace related log messages across the execution.
    """

    def __init__(self, correlation_id: Optional[str] = None):
        """
        Initialize filter.

        Args:
            correlation_id: Correlation ID to use (generates one if not provided)
        """
        super().__init__()
        self.correlation_id = correlation_id or str(uuid.uuid4())

    def filter(self, record: logging.LogRecord) -> bool:
        """Add correlation ID to record"""
        record.correlation_id = self.correlation_id  # type: ignore
        return True


def configure_structured_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    json_output: bool = False,
    correlation_id: Optional[str] = None,
) -> logging.Logger:
    """
    Configure structured logging for FLUID Build.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path for log output
        json_output: Whether to use JSON format (default: False for console)
        correlation_id: Optional correlation ID for request tracing

    Returns:
        Configured root logger
    """
    # Get root logger
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers
    logger.handlers.clear()

    # Add correlation ID filter
    context_filter = ContextFilter(correlation_id)
    logger.addFilter(context_filter)

    # Console handler (human-readable by default)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)

    if json_output:
        console_formatter = StructuredFormatter()
    else:
        # Human-readable format for console
        console_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )

    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler (always JSON)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)  # Capture everything in file
        file_handler.setFormatter(StructuredFormatter())
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the given name.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class LogContext:
    """
    Context manager for adding temporary context to logs.

    Example:
        with LogContext(operation="validate", contract="example.yaml"):
            logger.info("Starting validation")  # Will include operation and contract
    """

    def __init__(self, **context):
        """
        Initialize context.

        Args:
            **context: Key-value pairs to add to log records
        """
        self.context = context
        self.old_factory = None

    def __enter__(self):
        """Add context to log records"""
        old_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            for key, value in self.context.items():
                setattr(record, key, value)
            return record

        self.old_factory = old_factory
        logging.setLogRecordFactory(record_factory)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Restore original factory"""
        if self.old_factory:
            logging.setLogRecordFactory(self.old_factory)


# Convenience functions for common logging patterns


def log_operation_start(logger: logging.Logger, operation: str, **kwargs):
    """Log the start of an operation with context"""
    logger.info(f"Starting {operation}", extra={"operation": operation, "phase": "start", **kwargs})


def log_operation_success(
    logger: logging.Logger, operation: str, duration: Optional[float] = None, **kwargs
):
    """Log successful completion of an operation"""
    msg = f"Completed {operation}"
    if duration is not None:
        msg += f" in {duration:.2f}s"

    logger.info(
        msg, extra={"operation": operation, "phase": "success", "duration": duration, **kwargs}
    )


def log_operation_failure(
    logger: logging.Logger,
    operation: str,
    error: Exception,
    duration: Optional[float] = None,
    **kwargs,
):
    """Log failed operation with error details"""
    msg = f"Failed {operation}"
    if duration is not None:
        msg += f" after {duration:.2f}s"

    logger.error(
        msg,
        extra={
            "operation": operation,
            "phase": "failure",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "duration": duration,
            **kwargs,
        },
        exc_info=True,
    )


def log_metric(
    logger: logging.Logger, metric_name: str, value: float, unit: Optional[str] = None, **tags
):
    """
    Log a metric value.

    Useful for performance monitoring and observability.

    Args:
        logger: Logger instance
        metric_name: Name of the metric
        value: Metric value
        unit: Optional unit (seconds, bytes, count, etc.)
        **tags: Additional tags for the metric
    """
    logger.info(
        f"Metric: {metric_name}={value}{unit or ''}",
        extra={"metric": metric_name, "value": value, "unit": unit, **tags},
    )
