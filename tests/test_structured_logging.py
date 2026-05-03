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

"""Tests for fluid_build.structured_logging"""

import json
import logging
from unittest.mock import patch

from fluid_build.structured_logging import (
    ContextFilter,
    LogContext,
    StructuredFormatter,
    configure_structured_logging,
    get_logger,
    log_metric,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)


class TestStructuredFormatter:
    def _make_record(self, msg="hello", level=logging.INFO, exc_info=None, **extra):
        logger = logging.getLogger("test.formatter")
        record = logger.makeRecord("test.formatter", level, "test.py", 42, msg, (), exc_info)
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_basic_json_output(self):
        f = StructuredFormatter()
        record = self._make_record()
        output = f.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "hello"
        assert "timestamp" in data
        assert data["logger"] == "test.formatter"

    def test_includes_location(self):
        f = StructuredFormatter()
        data = json.loads(f.format(self._make_record()))
        assert "location" in data
        assert "line" in data["location"]

    def test_includes_correlation_id(self):
        f = StructuredFormatter()
        record = self._make_record(correlation_id="abc-123")
        data = json.loads(f.format(record))
        assert data["correlation_id"] == "abc-123"

    def test_no_context_when_disabled(self):
        f = StructuredFormatter(include_context=False)
        record = self._make_record(custom_field="val")
        data = json.loads(f.format(record))
        assert "context" not in data

    def test_exception_info(self):
        f = StructuredFormatter()
        try:
            raise ValueError("test err")
        except ValueError:
            import sys

            record = self._make_record(exc_info=sys.exc_info())
        data = json.loads(f.format(record))
        assert "exception" in data
        assert data["exception"]["type"] == "ValueError"
        assert "test err" in data["exception"]["message"]


class TestContextFilter:
    def test_adds_correlation_id(self):
        cf = ContextFilter(correlation_id="my-id")
        record = logging.LogRecord("n", logging.INFO, "", 0, "m", (), None)
        cf.filter(record)
        assert record.correlation_id == "my-id"  # type: ignore

    def test_generates_uuid_if_not_provided(self):
        cf = ContextFilter()
        assert cf.correlation_id  # not empty
        assert len(cf.correlation_id) >= 32  # UUID format

    def test_always_returns_true(self):
        cf = ContextFilter()
        record = logging.LogRecord("n", logging.INFO, "", 0, "m", (), None)
        assert cf.filter(record) is True


class TestLogContext:
    def test_context_adds_fields(self):
        """LogContext should temporarily add fields to log records."""
        original_factory = logging.getLogRecordFactory()
        with LogContext(operation="validate", contract="c.yaml"):
            factory = logging.getLogRecordFactory()
            record = factory("n", logging.INFO, "", 0, "m", (), None)
            assert hasattr(record, "operation")
            assert record.operation == "validate"  # type: ignore
        # After exit, factory restored
        assert logging.getLogRecordFactory() is original_factory


class TestConfigureStructuredLogging:
    def test_returns_logger(self):
        logger = configure_structured_logging(level="WARNING", json_output=False)
        assert isinstance(logger, logging.Logger)

    def test_json_output_mode(self):
        logger = configure_structured_logging(level="DEBUG", json_output=True)
        # Should have at least one handler with StructuredFormatter
        found = any(isinstance(h.formatter, StructuredFormatter) for h in logger.handlers)
        assert found

    def test_file_handler(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        logger = configure_structured_logging(log_file=log_file)
        assert len(logger.handlers) >= 2  # console + file


class TestGetLogger:
    def test_returns_logger(self):
        lg = get_logger("my.module")
        assert isinstance(lg, logging.Logger)
        assert lg.name == "my.module"


class TestConvenienceFunctions:
    """UX hardening pass — these convenience helpers were moved from
    ``logger.info`` / ``logger.error`` to ``logger.debug`` so the
    breadcrumbs (``Starting <op>``, ``Completed <op>``, ``Metric: ...``)
    don't bleed onto user-facing CLI terminals on every command. The
    structured event still flows to a ``--log-file`` sink + surfaces
    with ``--debug`` / ``FLUID_LOG_LEVEL=DEBUG``.

    These tests pin the new posture so a future change can't silently
    flip them back to INFO and resurrect the noise cut.
    """

    def test_log_operation_start(self):
        mock_logger = logging.getLogger("test.op")
        with patch.object(mock_logger, "debug") as mock_debug:
            log_operation_start(mock_logger, "build")
        mock_debug.assert_called_once()
        assert "build" in mock_debug.call_args[0][0]

    def test_log_operation_success_with_duration(self):
        mock_logger = logging.getLogger("test.op2")
        with patch.object(mock_logger, "debug") as mock_debug:
            log_operation_success(mock_logger, "deploy", duration=1.5)
        assert "1.50s" in mock_debug.call_args[0][0]

    def test_log_operation_failure(self):
        """The user-facing CLIError handler prints a clean ``❌``
        message; the structured-log entry is for the observability
        sink only and emits at DEBUG with exc_info=False so it
        doesn't dump a Python traceback before the typed error."""
        mock_logger = logging.getLogger("test.op3")
        with patch.object(mock_logger, "debug") as mock_debug:
            log_operation_failure(mock_logger, "validate", ValueError("fail"), duration=0.3)
        assert "validate" in mock_debug.call_args[0][0]
        # exc_info=False contract: no traceback in the log entry.
        assert mock_debug.call_args.kwargs.get("exc_info") is False

    def test_log_metric(self):
        mock_logger = logging.getLogger("test.metric")
        with patch.object(mock_logger, "debug") as mock_debug:
            log_metric(mock_logger, "latency", 42.0, unit="ms", host="a")
        mock_debug.assert_called_once()
