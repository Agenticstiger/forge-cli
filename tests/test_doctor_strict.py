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

"""``fluid doctor --strict``: the "Action needed" summary becomes the exit code."""

import argparse
import logging
from unittest.mock import patch

import pytest

from fluid_build.cli import doctor
from fluid_build.llm.providers import LlmReadinessCheck


def _run(strict: bool, json_out: bool, ready: bool) -> int:
    parser = argparse.ArgumentParser()
    doctor.register(parser.add_subparsers())
    argv = ["doctor"] + (["--strict"] if strict else []) + (["--json"] if json_out else [])
    args = parser.parse_args(argv)
    readiness = LlmReadinessCheck(ready=ready, error=None if ready else "no provider")
    with (
        patch.object(doctor, "_check_fluid_features", return_value=(True, [])),
        patch.object(doctor, "_check_copilot_readiness", return_value=readiness),
        patch.object(doctor, "_inspect_store_backend", return_value={"backend": "file"}),
        patch.object(doctor, "_resolve_extended_diagnostic_script", return_value=None),
    ):
        return doctor.run(args, logging.getLogger("test"))


def test_json_payload_says_why_strict_would_fail(capsys):
    import json

    assert _run(strict=True, json_out=True, ready=False) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["copilot_ready"] is False
    assert payload["action_needed"] is True


@pytest.mark.parametrize("json_out", [False, True], ids=["text", "json"])
def test_strict_exits_1_when_a_built_in_check_is_not_ready(json_out, capsys):
    assert _run(strict=True, json_out=json_out, ready=False) == 1


@pytest.mark.parametrize("json_out", [False, True], ids=["text", "json"])
def test_default_still_exits_0_when_a_built_in_check_is_not_ready(json_out, capsys):
    assert _run(strict=False, json_out=json_out, ready=False) == 0


@pytest.mark.parametrize("json_out", [False, True], ids=["text", "json"])
def test_strict_exits_0_when_everything_is_ready(json_out, capsys):
    assert _run(strict=True, json_out=json_out, ready=True) == 0
