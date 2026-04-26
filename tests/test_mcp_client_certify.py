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

from __future__ import annotations

import sys
from pathlib import Path

from scripts.mcp_client_certify import _claude_project_config, _server_command


def test_certifier_server_command_uses_current_python_module_entrypoint():
    assert _server_command() == [
        sys.executable,
        "-m",
        "fluid_build",
        "mcp",
        "serve",
        "--read-only",
    ]


def test_claude_project_config_is_project_local_and_quiet(tmp_path: Path):
    config = _claude_project_config(tmp_path)
    server = config["mcpServers"]["fluid-forge"]

    assert server["command"] == sys.executable
    assert server["args"] == [
        "-m",
        "fluid_build",
        "mcp",
        "serve",
        "--read-only",
    ]
    assert server["env"]["PYTHONPATH"] == str(tmp_path)
    assert server["env"]["FLUID_QUIET"] == "1"
    assert server["env"]["FLUID_NONINTERACTIVE"] == "1"
