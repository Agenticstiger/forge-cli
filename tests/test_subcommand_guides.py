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

"""Lock the no-subcommand → friendly-guide UX for the Tier-1 commands.

PR 50 shipped ``fluid_build/cli/_subcommand_guide.py`` and used it on
``fluid forge data-model`` to replace the bare argparse error
``the following arguments are required: data_model_action`` with a
Rich-rendered panel.  The follow-up applies the same pattern to five
more commands — ``fluid memory``, ``fluid auth``, ``fluid mcp``,
``fluid policy``, ``fluid config`` (a.k.a. ``fluid context``).

Each test invokes the command's dispatcher with ``argparse.Namespace``
shaped exactly like argparse would emit when the operator types the
bare command, captures stdout via ``capsys``, and asserts:

1. Exit code is ``0`` (no error condition; the user just hasn't
   chosen a subcommand yet).
2. The rendered panel includes the qualified command path
   (``fluid memory``, ``fluid auth``, …) and at least one of the
   subcommand verbs the operator might pick.

The assertions are intentionally loose on formatting so the tests
don't break when ``_subcommand_guide.py`` rewords its panel.
"""

from __future__ import annotations

import argparse
import logging
from typing import Optional

import pytest

LOGGER = logging.getLogger("test_subcommand_guides")


def _strip(text: str) -> str:
    """Normalise rendered output for substring assertions.

    Rich wraps long descriptions across line boundaries in a way that
    breaks naive ``in`` checks (``"data_model_action"`` becomes
    ``"data_\\nmodel_action"``).  Collapsing whitespace keeps the
    assertions readable without coupling them to terminal width.
    """

    return " ".join(text.split())


# ---------------------------------------------------------------------
# fluid memory
# ---------------------------------------------------------------------


def test_memory_bare_invocation_renders_guide(capsys, monkeypatch) -> None:
    from fluid_build.cli import memory_cmd

    rc = memory_cmd.run(
        argparse.Namespace(memory_action=None),
        LOGGER,
    )
    assert rc == 0
    out = _strip(capsys.readouterr().out)
    assert "fluid memory" in out
    assert "status" in out
    assert "show" in out
    assert "search" in out


# ---------------------------------------------------------------------
# fluid auth
# ---------------------------------------------------------------------


def test_auth_bare_invocation_renders_guide(capsys) -> None:
    from fluid_build.cli import auth as auth_mod

    rc = auth_mod.run(argparse.Namespace(verb=None), LOGGER)
    assert rc == 0
    out = _strip(capsys.readouterr().out)
    assert "fluid auth" in out
    assert "login" in out
    assert "status" in out
    assert "doctor" in out


# ---------------------------------------------------------------------
# fluid mcp
# ---------------------------------------------------------------------


def test_mcp_bare_invocation_renders_guide(capsys) -> None:
    from fluid_build.cli import mcp as mcp_mod

    rc = mcp_mod.run(argparse.Namespace(mcp_action=None), LOGGER)
    assert rc == 0
    out = _strip(capsys.readouterr().out)
    assert "fluid mcp" in out
    assert "serve" in out
    # The mcp guide's quick-start touts the safe-default ``--read-only``
    # flag; lock that in so the panel can't silently regress to a
    # less-safe recommendation.
    assert "--read-only" in out


# ---------------------------------------------------------------------
# fluid policy
# ---------------------------------------------------------------------


def test_policy_bare_invocation_renders_guide(capsys) -> None:
    from fluid_build.cli import policy as policy_mod

    rc = policy_mod._dispatch(
        argparse.Namespace(policy_cmd=None),
        LOGGER,
    )
    assert rc == 0
    out = _strip(capsys.readouterr().out)
    assert "fluid policy" in out
    assert "check" in out
    assert "compile" in out
    assert "apply" in out


# ---------------------------------------------------------------------
# fluid config / fluid context
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb_attr",
    [None, "missing"],
    ids=["verb=None", "no_verb_attribute"],
)
def test_context_bare_invocation_renders_guide(
    capsys, monkeypatch, verb_attr: Optional[str], tmp_path
) -> None:
    """Both ``fluid config`` and the ``fluid context`` alias share the
    same ``run`` function, so the test exercises the same code path
    for both — once with ``verb=None`` (canonical argparse output)
    and once with no ``verb`` attribute at all (defensive)."""

    monkeypatch.chdir(tmp_path)
    from fluid_build.cli import context as context_mod

    if verb_attr is None:
        ns = argparse.Namespace(verb=None)
    else:
        # Defensive: an older / external dispatcher might call run
        # without the ``verb`` attribute set at all.
        ns = argparse.Namespace()

    rc = context_mod.run(ns, LOGGER)
    assert rc == 0
    out = _strip(capsys.readouterr().out)
    assert "fluid config" in out
    assert "list" in out
    assert "get" in out
    assert "set" in out
