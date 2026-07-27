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


# ── detailed_plugins() — loads ALLOWED plugins to surface declared metadata ──


class _LoadableEP:
    def __init__(self, name, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _meta_class(**fields):
    class _Meta:
        @classmethod
        def get_plugin_info(cls):
            class _Info:
                def to_dict(self):
                    return dict(fields)

            return _Info()

    return _Meta


def test_detailed_plugins_surfaces_declared_metadata(monkeypatch):
    ep = _LoadableEP(
        "steward",
        lambda: _meta_class(version="1.2.0", author="ACME", license="Apache-2.0"),
    )
    monkeypatch.setattr(PM, "_entry_points", lambda group: [ep])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    data = PM.detailed_plugins("validator")
    entry = next(e for e in data["validator"] if e["name"] == "steward")
    assert entry["allowed"] is True
    assert entry["metadata"] == {"version": "1.2.0", "author": "ACME", "license": "Apache-2.0"}


def test_detailed_plugins_never_loads_blocked(monkeypatch):
    loaded = []

    def _boom():
        loaded.append(True)
        raise AssertionError("a BLOCKED plugin must never be loaded")

    monkeypatch.setattr(PM, "_entry_points", lambda group: [_LoadableEP("evil", _boom)])
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "evil")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    data = PM.detailed_plugins("validator")
    entry = next(e for e in data["validator"] if e["name"] == "evil")
    assert entry["allowed"] is False
    assert entry["metadata"] is None
    assert not loaded, "trust boundary violated: a blocked plugin was loaded"


def test_detailed_plugins_isolates_metadata_error(monkeypatch):
    class _Bad:
        @classmethod
        def get_plugin_info(cls):
            raise RuntimeError("boom with maybe-secret text")

    monkeypatch.setattr(PM, "_entry_points", lambda group: [_LoadableEP("flaky", lambda: _Bad)])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    data = PM.detailed_plugins("validator")  # must not raise
    entry = next(e for e in data["validator"] if e["name"] == "flaky")
    assert entry["allowed"] is True
    assert entry["metadata"] is None


# ── inspection honesty: dead roles, distribution attribution, compat ──
#
# `fluid plugins` is an operator inspection/security command, so what it does
# NOT say matters as much as what it does:
#
#  * `custom_scaffold` is in ROLE_GROUPS but nothing in the package ever walks
#    `fluid_build.custom_scaffolds` — a plugin registered there was rendered
#    exactly like a live one ("allowed", with metadata).
#  * declared PluginMetadata is self-reported and unverifiable: a probe
#    declaring version 9.9.9 was printed verbatim while the distribution that
#    actually ships the entry point was `mprobekit 1.0.0`, and nothing named it.
#  * a plugin declaring `requires_cli: ">=99.0.0"` was shown as plain "allowed"
#    even though FLUID_PLUGIN_STRICT_COMPAT=1 refuses to register it.


def test_custom_scaffold_is_the_only_declared_role_without_a_dispatch_site():
    """If a dispatch site lands, delete the entry — don't grow the exception list."""
    assert PM.UNDISPATCHED_GROUPS == frozenset({"custom_scaffold"})
    assert PM.is_dispatched("custom_scaffold") is False
    for key in ("provider", "validator", "catalog", "iac_provider"):
        assert PM.is_dispatched(key) is True


def test_the_custom_scaffolds_group_really_has_no_walk_site():
    """The pin that makes the flag above a fact rather than a claim."""
    import re
    from pathlib import Path

    import fluid_build

    root = Path(fluid_build.__file__).resolve().parent
    pattern = re.compile(r"fluid_build\.custom_scaffolds")
    hits = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "plugin_manager.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == [], f"custom_scaffolds now has a reader ({hits}) — update UNDISPATCHED_GROUPS"


def test_installed_plugins_marks_an_undispatched_role(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("scaf")])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    assert PM.installed_plugins("custom_scaffold")["custom_scaffold"][0]["dispatched"] is False
    assert PM.installed_plugins("validator")["validator"][0]["dispatched"] is True


def test_installed_plugins_names_the_owning_distribution(monkeypatch):
    class _Dist:
        name = "mprobekit"
        version = "1.0.0"

    class _EPWithDist(_FakeEP):
        dist = _Dist()

    monkeypatch.setattr(PM, "_entry_points", lambda group: [_EPWithDist("probe")])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    entry = PM.installed_plugins("validator")["validator"][0]
    assert entry["distribution"] == "mprobekit 1.0.0"


def test_distribution_is_none_when_unattributable():
    assert PM.entry_point_distribution(_FakeEP("orphan")) is None


def test_detailed_plugins_flags_a_requires_cli_mismatch(monkeypatch):
    bad = _LoadableEP("toonew", lambda: _meta_class(version="9.9.9", requires_cli=">=99.0.0"))
    ok = _LoadableEP("fine", lambda: _meta_class(version="1.0.0", requires_cli=">=0.1.0"))
    silent = _LoadableEP("quiet", lambda: _meta_class(version="1.0.0"))
    monkeypatch.setattr(PM, "_entry_points", lambda group: [bad, ok, silent])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    by_name = {e["name"]: e for e in PM.detailed_plugins("provider")["provider"]}
    assert by_name["toonew"]["compatible"] is False
    assert by_name["fine"]["compatible"] is True
    assert by_name["quiet"]["compatible"] is None  # nothing declared — fail open


def test_the_renderer_surfaces_undispatched_and_incompatible(monkeypatch, capsys):
    monkeypatch.setattr(
        PM,
        "detailed_plugins",
        lambda role=None, logger=None: {
            "custom_scaffold": [
                {
                    "name": "scaf",
                    "group": "fluid_build.custom_scaffolds",
                    "allowed": True,
                    "dispatched": False,
                    "distribution": "mprobekit 1.0.0",
                    "metadata": {"version": "9.9.9"},
                    "compatible": None,
                }
            ],
            "provider": [
                {
                    "name": "toonew",
                    "group": "fluid_build.providers",
                    "allowed": True,
                    "dispatched": True,
                    "distribution": "mprobekit 1.0.0",
                    "metadata": {"requires_cli": ">=99.0.0"},
                    "compatible": False,
                }
            ],
        },
    )
    assert plugins_cmd.run(_args(detailed=True), logging.getLogger("t")) == 0
    out = capsys.readouterr().out
    assert "NOT DISPATCHED" in out
    assert "never invoked" in out
    assert "INCOMPATIBLE" in out
    assert "mprobekit 1.0.0" in out
    # The declared version must be visibly attributed to the plugin, not stated
    # as fact next to the distribution that really ships it.
    assert "declares-version=9.9.9" in out
