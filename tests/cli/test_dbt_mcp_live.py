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

"""Stage 2 / live: the dbt MCP delegate against the REAL dbt-labs/dbt-mcp server.

``tests/cli/test_dbt_mcp.py`` exercises the bridge with an in-memory MCP server
(zero network). This file proves the production path: ``DbtMcpClient`` launching
the real ``dbt-labs/dbt-mcp`` server as a stdio subprocess (via ``uvx``) against
a local dbt-duckdb project, listing its tools and calling one — i.e. a genuine
forge-agent → MCP → dbt round-trip.

**Triple-gated** so a plain ``pytest`` never touches the network / a subprocess:

* ``FLUID_DBT_MCP_LIVE=1`` — explicit opt-in;
* ``uvx`` on PATH (launches dbt-mcp in an isolated env, no repo dep);
* a dbt executable resolvable via ``FLUID_DBT_E2E_DBT_PATH`` or ``dbt`` on PATH
  (dbt-mcp's *CLI* tool group needs a real dbt binary + a project).

Run it locally with::

    pip install dbt-core dbt-duckdb            # provides the dbt binary
    FLUID_DBT_MCP_LIVE=1 \\
    FLUID_DBT_E2E_DBT_PATH=$(command -v dbt) \\
    pytest tests/cli/test_dbt_mcp_live.py -q

The full ``fluid forge`` → LLM → real dbt **Cloud** e2e remains a Stage-3
follow-up (Semantic-Layer / Discovery tool groups need a dbt Cloud token); this
covers the self-managed dbt-CLI group, which is the locally reproducible part.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

_TRUE = {"1", "true", "yes", "on"}


def _dbt_path() -> str | None:
    return os.environ.get("FLUID_DBT_E2E_DBT_PATH") or shutil.which("dbt")


def _skip_reason() -> str | None:
    if os.environ.get("FLUID_DBT_MCP_LIVE", "").strip().lower() not in _TRUE:
        return "live dbt MCP test is opt-in — set FLUID_DBT_MCP_LIVE=1"
    if shutil.which("uvx") is None:
        return "uvx not on PATH (needed to launch dbt-labs/dbt-mcp)"
    if _dbt_path() is None:
        return "no dbt binary — set FLUID_DBT_E2E_DBT_PATH or put dbt on PATH"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


@pytest.fixture()
def dbt_project(tmp_path: Path) -> Path:
    """Copy the committed dbt fixture into a temp dir + write a duckdb profile.

    ``profiles.yml`` is generated here (not committed) because it needs an
    absolute duckdb path, and the project is copied so dbt's ``target/`` writes
    never touch the source tree.
    """
    src = Path(__file__).parent.parent / "fixtures" / "dbt_demo"
    proj = tmp_path / "dbt_demo"
    shutil.copytree(src, proj)
    (proj / "profiles.yml").write_text(
        "fluid_dbt_demo:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: {proj / 'demo.duckdb'}\n"
        "      threads: 1\n",
        encoding="utf-8",
    )
    return proj


def _client(project: Path):
    from fluid_build.cli.dbt_mcp import DbtMcpClient

    # The dbt-mcp server reads its dbt config from the inherited env; set it for
    # this process so DbtMcpClient._subprocess_env passes it through.
    os.environ["DBT_PROJECT_DIR"] = str(project)
    os.environ["DBT_PROFILES_DIR"] = str(project)
    os.environ["DBT_PATH"] = _dbt_path()  # type: ignore[index]
    # Local round-trip needs only the CLI tool group; the SL/Discovery groups
    # require a dbt Cloud token (Stage 3).
    os.environ["DISABLE_SEMANTIC_LAYER"] = "true"
    os.environ["DISABLE_DISCOVERY"] = "true"
    return DbtMcpClient(command="uvx", args=["--from", "dbt-mcp", "dbt-mcp"])


def test_live_dbt_mcp_lists_cli_tools(dbt_project):
    """The real dbt-mcp server, over stdio, exposes its dbt-CLI tool group."""
    names = {n for n, _d, _s in _client(dbt_project).list_tools()}
    # The self-managed dbt-CLI group — present whenever DBT_PROJECT_DIR + a dbt
    # binary resolve. (Don't over-pin the exact set; dbt-mcp evolves it.)
    assert {"compile", "list", "run"} <= names, sorted(names)


def test_live_dbt_mcp_call_list_returns_the_model(dbt_project):
    """Calling the dbt ``list`` tool surfaces the fixture's ``orders`` model —
    a real forge-agent → MCP → dbt round-trip, not a mock."""
    out = _client(dbt_project).call_tool("list", {})
    text = out if isinstance(out, str) else str(out)
    assert "orders" in text, text[:500]
