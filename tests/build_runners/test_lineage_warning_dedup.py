# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A lineage warning is shown once on the console and still reaches log files."""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit


def test_console_handler_skips_the_record_a_file_handler_keeps(tmp_path, monkeypatch, capsys):
    from fluid_build.build_runners import _lineage
    from fluid_build.cli import _setup_enhanced_logging

    message = "OpenLineage emission skipped: boom"
    monkeypatch.setattr(_lineage, "_WARNED", set())
    log_file = tmp_path / "run.log"
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        _setup_enhanced_logging("WARNING", str(log_file))
        _lineage._warn_once("k", message)
        for handler in root.handlers:
            handler.flush()
        captured = capsys.readouterr()
        # The console print is the only console copy; the stderr log handler skipped it.
        assert message not in captured.err
        assert message in " ".join(captured.out.split())
        # The file handler still has the WARNING record.
        assert message in log_file.read_text()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def test_without_logging_handlers_the_warning_is_logged_at_debug(monkeypatch, caplog):
    # Library use: with no handler, logging's last-resort handler would print a
    # WARNING record under the console line, so the user saw it twice.
    from fluid_build.build_runners import _lineage

    monkeypatch.setattr(_lineage, "_WARNED", set())
    monkeypatch.setattr(_lineage.LOG, "hasHandlers", lambda: False)
    caplog.set_level(logging.DEBUG, logger="fluid.acquire.lineage")
    _lineage._warn_once("no-handlers", "lineage is off")
    levels = [r.levelno for r in caplog.records if r.name == "fluid.acquire.lineage"]
    assert levels == [logging.DEBUG]
