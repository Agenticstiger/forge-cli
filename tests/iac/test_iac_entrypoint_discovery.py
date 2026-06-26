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

"""External IaC cloud plugins are discovered via the ``fluid_build.iac_providers``
entry-point group — adding a cloud needs zero edits to forge-cli core."""

from __future__ import annotations

import pytest

from fluid_build import plugin_manager as PM
from fluid_build.iac import registry as R


@pytest.fixture(autouse=True)
def _isolate_iac_registry():
    saved = dict(R.IAC_PLUGINS)
    try:
        yield
    finally:
        R.IAC_PLUGINS.clear()
        R.IAC_PLUGINS.update(saved)


class _FakeEP:
    def __init__(self, name, obj, *, boom=False):
        self.name = name
        self._obj = obj
        self._boom = boom

    def load(self):
        if self._boom:
            raise RuntimeError("load exploded")
        return self._obj


class _ToyIacPlugin:
    """Minimal duck-typed IacProviderPlugin."""

    name = "mycloud"
    required_providers = {"mycloud": {"source": "acme/mycloud", "version": "~> 1.0"}}
    credential_env_vars = ("MYCLOUD_TOKEN",)

    def emit(self, contract, actions=()):
        return {}

    def emit_data(self, contract, actions=()):
        return {}

    def credential_env(self, env):
        return {}

    def discover_imports(self, contract, actions=()):
        return []

    def provider_block(self):
        return {}


def test_external_iac_plugin_registered_from_entrypoint(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("mycloud", _ToyIacPlugin)])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)

    assert R.get_iac_plugin("mycloud") is None  # not registered yet
    R.discover_iac_entrypoints()
    plugin = R.get_iac_plugin("mycloud")
    assert plugin is not None
    assert plugin.name == "mycloud"
    assert isinstance(plugin, _ToyIacPlugin)  # class was instantiated


def test_instance_entrypoint_used_as_is(monkeypatch):
    instance = _ToyIacPlugin()
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("mycloud", instance)])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    R.discover_iac_entrypoints()
    assert R.get_iac_plugin("mycloud") is instance


def test_blocklist_suppresses_external_iac_plugin(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("mycloud", _ToyIacPlugin)])
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "mycloud")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    R.discover_iac_entrypoints()
    assert R.get_iac_plugin("mycloud") is None  # blocked, never loaded


def test_broken_plugin_is_isolated(monkeypatch):
    monkeypatch.setattr(
        PM,
        "_entry_points",
        lambda group: [_FakeEP("broken", None, boom=True), _FakeEP("mycloud", _ToyIacPlugin)],
    )
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    R.discover_iac_entrypoints()  # must not raise
    assert R.get_iac_plugin("broken") is None
    assert R.get_iac_plugin("mycloud") is not None  # the good one still registers


def test_builtins_still_present_after_discovery():
    # The real built-ins registered on import are intact.
    for cloud in ("aws", "gcp", "snowflake", "confluent"):
        assert R.get_iac_plugin(cloud) is not None
