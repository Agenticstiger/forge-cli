# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build/logging_utils.py — JSON logging setup."""

import json
import logging

from fluid_build.logging_utils import JsonFormatter, log_json, setup_logger


class TestJsonFormatter:
    def test_basic_format(self):
        fmt = JsonFormatter()
        record = logging.LogRecord("fluid", logging.INFO, "", 0, "hello world", (), None)
        output = fmt.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "hello world"
        assert "time" in data

    def test_extra_dict(self):
        fmt = JsonFormatter()
        record = logging.LogRecord("fluid", logging.INFO, "", 0, "msg", (), None)
        record.extra = {"key": "value"}
        output = fmt.format(record)
        data = json.loads(output)
        assert data["key"] == "value"

    def test_exception_info(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord("fluid", logging.ERROR, "", 0, "err", (), exc_info)
        output = fmt.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestSetupLogger:
    def test_returns_logger(self):
        logger = setup_logger("DEBUG")
        assert isinstance(logger, logging.Logger)

    def test_clears_handlers(self):
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())
        len(root.handlers)
        setup_logger("INFO")
        # Should have exactly one handler (stderr)
        assert len(root.handlers) == 1


class TestLogJson:
    def test_basic_log(self, capfd):
        setup_logger("DEBUG")
        log_json("fluid.test", "INFO", "test message", foo="bar")
        # stderr capture
        captured = capfd.readouterr()
        # Output goes to stderr via JsonFormatter
        assert "test message" in captured.err

    def test_level_mapping(self, capfd):
        setup_logger("DEBUG")
        log_json("fluid.test", "warning", "warn msg")
        captured = capfd.readouterr()
        assert "warn msg" in captured.err
