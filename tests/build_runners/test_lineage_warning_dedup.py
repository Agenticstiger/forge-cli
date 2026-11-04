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
