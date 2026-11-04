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

"""``fluid doctor`` tells users once that AI and MCP move to extras in 0.28.0."""

import argparse
import logging
from unittest.mock import patch

from fluid_build.cli import doctor
from fluid_build.llm.providers import LlmReadinessCheck

_NOTICE = "pip install 'data-product-forge[standard]'"


def _run(*flags: str) -> int:
    parser = argparse.ArgumentParser()
    doctor.register(parser.add_subparsers())
    args = parser.parse_args(["doctor", *flags])
    with (
        patch.object(doctor, "_check_fluid_features", return_value=(True, [])),
        patch.object(
            doctor, "_check_copilot_readiness", return_value=LlmReadinessCheck(ready=True)
        ),
        patch.object(doctor, "_inspect_store_backend", return_value={"backend": "file"}),
        patch.object(doctor, "_resolve_extended_diagnostic_script", return_value=None),
    ):
        return doctor.run(args, logging.getLogger("test"))


def test_notice_names_the_release_and_the_install_command():
    assert "0.28.0" in doctor.EXTRAS_MOVE_NOTICE
    assert _NOTICE in doctor.EXTRAS_MOVE_NOTICE
    assert "\n" not in doctor.EXTRAS_MOVE_NOTICE


def test_default_run_prints_the_notice_exactly_once(capsys):
    assert _run() == 0
    assert capsys.readouterr().out.count(_NOTICE) == 1


def test_json_output_stays_machine_readable(capsys):
    import json

    assert _run("--json") == 0
    json.loads(capsys.readouterr().out)
