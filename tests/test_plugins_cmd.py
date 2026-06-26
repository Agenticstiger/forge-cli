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

"""The `fluid plugins` inspection command + plugin_manager.installed_plugins()."""

from __future__ import annotations

import argparse
import logging

from fluid_build import plugin_manager as PM
from fluid_build.cli import plugins_cmd


class _FakeEP:
    def __init__(self, name):
        self.name = name


# ── installed_plugins() ───────────────────────────────────────────────


def test_installed_plugins_reports_allow_block_status(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("good"), _FakeEP("evil")])
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "evil")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    data = PM.installed_plugins("validator")
    by_name = {e["name"]: e for e in data["validator"]}
    assert by_name["good"]["allowed"] is True
    assert by_name["evil"]["allowed"] is False  # surfaced as blocked, not hidden
    assert by_name["good"]["group"] == "fluid_build.validators"


def test_installed_plugins_covers_all_governed_groups(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [])
    data = PM.installed_plugins()
    # Now covers the SDK roles AND the CLI-internal governed groups.
    assert set(data) == set(PM.governed_groups())


# ── the `fluid plugins` command is wired into the CLI ─────────────────


def test_plugins_command_is_registered_in_parser():
    from fluid_build.cli import build_parser

    parser = build_parser()
    choices = set()
    for action in parser._subparsers._group_actions:
        choices |= set(getattr(action, "choices", {}) or {})
    assert "plugins" in choices  # `fluid plugins` (and `fluid plugins list`) exist


# ── run() rendering ───────────────────────────────────────────────────


def _args(**kw):
    ns = argparse.Namespace(role=None, json=False, plugins_action=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_run_text_renders_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        PM,
        "installed_plugins",
        lambda role=None: {"validator": [{"name": "steward", "group": "g", "allowed": True}]},
    )
    rc = plugins_cmd.run(_args(), logging.getLogger("t"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "steward" in out and "validator" in out


def test_run_json_emits_machine_readable(monkeypatch, capsys):
    payload = {"catalog": [{"name": "datahub", "group": "g", "allowed": False}]}
    monkeypatch.setattr(PM, "installed_plugins", lambda role=None: payload)
    rc = plugins_cmd.run(_args(json=True), logging.getLogger("t"))
    assert rc == 0
    import json as _json

    out = capsys.readouterr().out
    assert _json.loads(out) == payload


def test_run_handles_no_plugins(monkeypatch, capsys):
    monkeypatch.setattr(PM, "installed_plugins", lambda role=None: {r: [] for r in PM.ROLE_GROUPS})
    rc = plugins_cmd.run(_args(), logging.getLogger("t"))
    assert rc == 0
    assert "No third-party FLUID plugins installed" in capsys.readouterr().out
