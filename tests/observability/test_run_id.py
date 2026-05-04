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

"""Cross-CLI run-id correlation — pin the resolution order.

The 11-stage pipeline (bundle → plan → apply → verify → publish)
emits OTel spans from separate CLI invocations. Without a shared
run-id, dashboards can't group "Monday's deploy of orders_v1" by
ID. ``observability/run_id.py`` defines the resolution order:

1. ``$FLUID_RUN_ID`` env var (operator override / CI injection).
2. ``.fluid/run-id.txt`` (persisted between stages).
3. Newly-generated id (first stage of a run, persisted for next).

Pin every layer so a future contributor can't silently break the
correlation guarantee.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fluid_build.observability.run_id import (
    RUN_ID_ENV_VAR,
    RUN_ID_FILE,
    clear_run_id,
    get_or_create_run_id,
    run_id_span_attribute,
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """Hermetic workspace with no env / persisted state."""
    monkeypatch.delenv(RUN_ID_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestEnvOverride:
    def test_env_var_wins_over_persisted(self, workspace, monkeypatch):
        # Persist one id then set env to a different one.
        first = get_or_create_run_id(workspace)
        monkeypatch.setenv(RUN_ID_ENV_VAR, "ci-build-42")
        assert get_or_create_run_id(workspace) == "ci-build-42"
        # Persisted value still on disk untouched.
        persisted = (workspace / ".fluid" / RUN_ID_FILE).read_text().strip()
        assert persisted == first

    def test_env_var_strips_whitespace(self, workspace, monkeypatch):
        monkeypatch.setenv(RUN_ID_ENV_VAR, "  padded-id  \n")
        assert get_or_create_run_id(workspace) == "padded-id"

    def test_empty_env_var_falls_through(self, workspace, monkeypatch):
        # When env is set to empty string, fall through to persisted/new id.
        monkeypatch.setenv(RUN_ID_ENV_VAR, "")
        rid = get_or_create_run_id(workspace)
        # Generated id is 12 hex chars.
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestPersisted:
    def test_first_call_generates_and_persists(self, workspace):
        rid = get_or_create_run_id(workspace)
        persisted = (workspace / ".fluid" / RUN_ID_FILE).read_text().strip()
        assert rid == persisted

    def test_second_call_returns_persisted(self, workspace):
        rid1 = get_or_create_run_id(workspace)
        rid2 = get_or_create_run_id(workspace)
        assert rid1 == rid2

    def test_create_persisted_file_false_skips_disk(self, workspace):
        rid = get_or_create_run_id(workspace, create_persisted_file=False)
        assert len(rid) == 12
        # ``.fluid/run-id.txt`` should NOT exist.
        assert not (workspace / ".fluid" / RUN_ID_FILE).exists()


class TestClear:
    def test_clear_removes_persisted_file(self, workspace):
        rid = get_or_create_run_id(workspace)
        path = workspace / ".fluid" / RUN_ID_FILE
        assert path.exists()
        clear_run_id(workspace)
        assert not path.exists()

    def test_clear_is_idempotent(self, workspace):
        # Calling clear before any id was created shouldn't raise.
        clear_run_id(workspace)
        clear_run_id(workspace)


class TestSpanAttribute:
    def test_returns_canonical_dict(self, workspace):
        attr = run_id_span_attribute(workspace)
        assert "fluid.run_id" in attr
        assert len(attr["fluid.run_id"]) == 12

    def test_env_override_flows_through(self, workspace, monkeypatch):
        monkeypatch.setenv(RUN_ID_ENV_VAR, "ci-id-99")
        attr = run_id_span_attribute(workspace)
        assert attr == {"fluid.run_id": "ci-id-99"}
