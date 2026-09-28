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

"""A core command whose module fails to import says so and exits non-zero.

``register_core_commands`` used to register a stand-in for ``plan``,
``apply`` and the graph command when their module raised ``ImportError``.
The stand-ins planned through ``fluid_build.planner`` and drew through
``fluid_build.visualize``; neither module exists, so ``plan`` wrote a
made-up plan, ``apply`` applied it, and the graph was two fixed nodes, each
with exit code 0 on a broken install.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, Iterator, List

import pytest

import fluid_build.cli as cli_pkg
from fluid_build.cli._common import CLIError
from fluid_build.cli.bootstrap import register_core_commands

LOG = logging.getLogger(__name__)


@pytest.fixture()
def break_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Make ``from fluid_build.cli import <module>`` raise ImportError."""
    monkeypatch.delenv("FLUID_BUILD_PROFILE", raising=False)

    def _break(module: str) -> None:
        monkeypatch.delattr(cli_pkg, module, raising=False)
        monkeypatch.setitem(sys.modules, f"fluid_build.cli.{module}", None)

    yield _break


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_core_commands(parser.add_subparsers(dest="cmd"))
    return parser


@pytest.mark.parametrize(
    ("module", "argv", "written"),
    [
        (
            "plan",
            ["plan", "contract.fluid.yaml", "--out", "plan.json", "--mode", "amend"],
            "plan.json",
        ),
        ("apply", ["apply", "contract.fluid.yaml", "--out", "result.json", "--yes"], "result.json"),
        ("viz_graph", ["viz-graph", "contract.fluid.yaml", "--out", "graph.dot"], "graph.dot"),
    ],
    ids=["plan", "apply", "viz-graph"],
)
def test_a_command_whose_module_fails_to_import_raises_and_writes_nothing(
    break_module: Callable[[str], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    argv: List[str],
    written: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    break_module(module)

    args = _parser().parse_args(argv)
    with pytest.raises(CLIError) as err:
        args.func(args, LOG)

    assert err.value.event == "command_unavailable"
    assert err.value.exit_code != 0
    assert err.value.context["module"] == f"fluid_build.cli.{module}"
    # The import error itself reaches the user, not a stand-in result.
    assert "ModuleNotFoundError" in err.value.context["error"]
    assert f"fluid_build.cli.{module}" in err.value.context["error"]
    assert not (tmp_path / written).exists()
    assert not (tmp_path / "runtime").exists()


def test_the_help_line_says_the_command_is_unavailable(
    break_module: Callable[[str], None],
) -> None:
    break_module("plan")
    parser = _parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in subparsers._choices_actions}
    assert "unavailable" in (helps.get("plan") or "")


def test_a_working_install_registers_the_real_commands() -> None:
    parser = _parser()
    for argv in (["plan", "c.yaml"], ["apply", "c.yaml"], ["viz-graph", "c.yaml"]):
        args = parser.parse_args(argv)
        assert args.func.__module__ != "fluid_build.cli.bootstrap", argv
