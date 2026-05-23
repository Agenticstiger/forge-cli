# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the OpenTofu runner."""

from __future__ import annotations

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner


class TestEventParsing:
    pytestmark = pytest.mark.unit

    def test_parse_json_events_skips_blank_and_nonjson(self):
        stdout = '{"@level":"info","type":"version"}\n\nnot json\n{"type":"change_summary"}\n'
        events = runner._parse_json_events(stdout)
        assert len(events) == 2
        assert events[0]["type"] == "version"
        assert events[1]["type"] == "change_summary"

    def test_change_summary_extracts_counts(self):
        result = runner.TofuResult(
            "plan",
            0,
            "",
            "",
            events=[
                {"type": "version"},
                {"type": "change_summary", "changes": {"add": 3, "change": 1, "remove": 0}},
            ],
        )
        assert runner.change_summary(result) == {"add": 3, "change": 1, "remove": 0}

    def test_change_summary_defaults_when_absent(self):
        result = runner.TofuResult("plan", 0, "", "", events=[])
        assert runner.change_summary(result) == {"add": 0, "change": 0, "remove": 0}

    def test_result_ok_reflects_returncode(self):
        assert runner.TofuResult("init", 0, "", "").ok is True
        assert runner.TofuResult("init", 1, "", "boom").ok is False


class TestTofuAvailability:
    pytestmark = pytest.mark.unit

    def test_tofu_path_returns_path_or_none(self):
        path = runner.tofu_path()
        assert path is None or isinstance(path, str)


@pytest.mark.integration
@pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed")
def test_runner_init_and_validate_on_emitted_module(tmp_path):
    contract = {
        "id": "demo.gcp",
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "format": "bigquery_table",
                    "location": {"dataset": "demo", "table": "events"},
                },
            }
        ],
    }
    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("gcp"), contract))

    init = runner.tofu_init(str(tmp_path), backend=False)
    assert init.ok, init.stderr or init.stdout

    validate = runner.tofu_validate(str(tmp_path))
    assert validate.ok, validate.stderr or validate.stdout


class TestStateList:
    pytestmark = pytest.mark.unit

    def test_state_list_parses_addresses(self, monkeypatch):
        monkeypatch.setattr(
            runner,
            "_run",
            lambda *a, **k: runner.TofuResult(
                "state-list", 0, "snowflake_database.x\nsnowflake_schema.y\n\n", ""
            ),
        )
        assert runner.tofu_state_list("/wd") == ["snowflake_database.x", "snowflake_schema.y"]

    def test_state_list_empty_when_no_state(self, monkeypatch):
        # `tofu state list` exits non-zero when there is no state yet.
        monkeypatch.setattr(
            runner, "_run", lambda *a, **k: runner.TofuResult("state-list", 1, "", "no state")
        )
        assert runner.tofu_state_list("/wd") == []
