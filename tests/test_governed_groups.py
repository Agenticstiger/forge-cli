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

"""Every entry-point group the CLI executes is governed by ONE allow/block policy.

Previously six CLI-internal groups (commands / apply_hooks / extension_schemas /
extension_validators / modeling_techniques / source_adapters) bypassed the gate
and were invisible to `fluid plugins`.
"""

from __future__ import annotations

import importlib.metadata as md

from fluid_build import extension_schemas as ES
from fluid_build import plugin_manager as PM


class _FakeEP:
    def __init__(self, name, obj):
        self.name = name
        self._obj = obj

    def load(self):
        return self._obj


# ── governed-group surface ────────────────────────────────────────────


def test_governed_groups_covers_roles_and_cli_internal():
    g = PM.governed_groups()
    # the five SDK roles + the four CLI-internal groups
    assert set(PM.ROLE_GROUPS) <= set(g)
    assert set(PM.EXTRA_GROUPS) <= set(g)
    assert g["apply_hook"] == "fluid_build.apply_hooks"
    assert g["extension_validator"] == "fluid_build.extension_validators"


def test_installed_plugins_surfaces_cli_internal_groups(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [])
    keys = set(PM.installed_plugins())
    # `fluid plugins` now sees commands / apply_hooks / extension_* too.
    assert {"command", "apply_hook", "extension_schema", "extension_validator"} <= keys


# ── extension_schemas / extension_validators honour allow/block ───────


def test_extension_schema_provider_blocked_is_not_loaded(monkeypatch):
    loaded = {"called": False}

    def _provider(fluid_version=None):
        loaded["called"] = True
        return {"type": "object"}

    monkeypatch.setattr(md, "entry_points", lambda **kw: [_FakeEP("myext", _provider)])
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "myext")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    schemas = ES.iter_extension_schemas()
    assert "myext" not in schemas
    assert loaded["called"] is False  # blocked BEFORE load — code never ran


def test_extension_schema_provider_allowed_loads(monkeypatch):
    monkeypatch.setattr(
        md,
        "entry_points",
        lambda **kw: [_FakeEP("myext", lambda fluid_version=None: {"type": "object"})],
    )
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    assert "myext" in ES.iter_extension_schemas()


def test_extension_validator_blocked_does_not_run(monkeypatch):
    ran = {"called": False}

    def _validator(extensions, errors):
        ran["called"] = True
        errors.append("nope")

    monkeypatch.setattr(md, "entry_points", lambda **kw: [_FakeEP("myext", _validator)])
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "myext")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    errors = ES.run_extension_validators({"extensions": {"myext": {}}})
    assert ran["called"] is False  # blocked before load
    assert errors == []


# ── source_adapters: lazy load gated by allow/block ───────────────────


def test_source_adapter_blocked_before_lazy_load(monkeypatch):
    import pytest

    from fluid_build.copilot.catalog import source_registry as SR

    class _Target:
        def load(self):
            raise AssertionError("must not load a blocked source adapter")

    monkeypatch.setattr(SR, "_ensure_discovered", lambda: None)
    monkeypatch.setitem(
        SR._REGISTRY,
        "mysrc",
        SR.SourceAdapterSpec(name="mysrc", kind="catalog", target=_Target(), origin="plugin"),
    )
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "mysrc")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    with pytest.raises(RuntimeError, match="blocked by the operator allow/block"):
        SR.resolve_catalog_adapter_class("mysrc")


# ── modeling_techniques: entry-point load gated by allow/block ────────


def test_modeling_technique_blocked_before_load(monkeypatch):
    import importlib.metadata as _md

    from fluid_build.copilot import modeling_techniques as MT

    loaded = {"called": False}

    def _loader():
        loaded["called"] = True
        return MT.ModelingTechnique(name="mytech", description="x", origin="plugin")

    monkeypatch.setattr(
        _md, "entry_points", lambda **kw: {MT.EP_GROUP: [_FakeEP("mytech", _loader)]}
    )
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "mytech")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    MT._discover_entrypoints(None)
    assert "mytech" not in MT._REGISTRY  # blocked → not registered
    assert loaded["called"] is False  # and never loaded
