# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
