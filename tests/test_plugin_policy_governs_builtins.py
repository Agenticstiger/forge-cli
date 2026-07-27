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

"""The allow/block policy must govern the built-in providers too.

``fluid plugins --role provider`` renders every entry-point name in the
``fluid_build.providers`` group with its ``is_allowed`` status — and
forge-cli declares its own built-ins in that group. But only
``_discover_entrypoints`` consulted the policy; ``_preload_curated`` and
``_discover_subpackages`` import the in-tree modules directly and those
self-register. So with ``FLUID_PLUGINS_BLOCKLIST=snowflake`` set, the CLI
printed ``snowflake  BLOCKED (allow/block policy)`` and then ran a real
Snowflake apply that provisioned tables. A control that renders as enforced
while being inert is worse than no control.

The gate now lives at both registration chokepoints — the provider registry
and the IaC plugin registry (``fluid apply`` reaches the warehouse through
the latter, so gating only the former would have left the block inert on
the path that emits DDL).
"""

from __future__ import annotations

import logging

import pytest

from fluid_build import providers as registry
from fluid_build.iac import registry as iac_registry

LOG = logging.getLogger("test_plugin_policy_governs_builtins")


@pytest.fixture(autouse=True)
def _restore_registries():
    """Both registries are process-global; snapshot and restore them."""
    providers_before = dict(registry.PROVIDERS)
    iac_before = dict(iac_registry.IAC_PLUGINS)
    yield
    registry.PROVIDERS.clear()
    registry.PROVIDERS.update(providers_before)
    iac_registry.IAC_PLUGINS.clear()
    iac_registry.IAC_PLUGINS.update(iac_before)


def _rediscover():
    registry.PROVIDERS.clear()
    registry.discover_providers(LOG, force=True)


class TestBlocklistGovernsBuiltInProviders:
    def test_blocked_builtin_never_registers(self, monkeypatch):
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "snowflake")
        _rediscover()
        assert "snowflake" not in registry.PROVIDERS
        # Siblings are untouched — the block is targeted, not a kill switch.
        assert "local" in registry.PROVIDERS
        assert "aws" in registry.PROVIDERS

    def test_unblocked_builtin_registers_normally(self, monkeypatch):
        monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
        monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
        _rediscover()
        assert "snowflake" in registry.PROVIDERS

    def test_explicit_registration_cannot_bypass_the_policy(self, monkeypatch):
        """``register_provider`` is the chokepoint, so no discovery path — nor
        a direct programmatic call — can re-add a blocked name."""
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "snowflake")
        registry.PROVIDERS.pop("snowflake", None)
        registry.register_provider("snowflake", object, logger=LOG)
        assert "snowflake" not in registry.PROVIDERS

    def test_allowlist_excludes_unlisted_builtins(self, monkeypatch):
        """An allowlist means "only these load" — the built-ins were silently
        exempt while ``fluid plugins`` reported them BLOCKED."""
        monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "local")
        _rediscover()
        assert "snowflake" not in registry.PROVIDERS
        assert "local" in registry.PROVIDERS


class TestBlocklistGovernsIacPlugins:
    def test_blocked_cloud_is_not_registered_as_an_iac_plugin(self, monkeypatch):
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "snowflake")
        iac_registry.IAC_PLUGINS.pop("snowflake", None)
        iac_registry.register_iac_plugin("snowflake", object())
        assert iac_registry.get_iac_plugin("snowflake") is None

    def test_unblocked_cloud_registers(self, monkeypatch):
        monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
        monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
        sentinel = object()
        iac_registry.register_iac_plugin("snowflake", sentinel)
        assert iac_registry.get_iac_plugin("snowflake") is sentinel


class TestBlockedProviderErrorIsHonest:
    def test_blocked_provider_reports_policy_not_unknown(self, monkeypatch):
        """A deliberately blocked provider reported as "unknown" sends the
        operator hunting for a typo or a missing extra."""
        from fluid_build.cli._common import CLIError, build_provider

        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "snowflake")
        _rediscover()
        with pytest.raises(CLIError) as excinfo:
            build_provider("snowflake", None, None, LOG)
        assert excinfo.value.event == "provider_blocked_by_policy"
        assert excinfo.value.exit_code == 2

    def test_genuinely_unknown_provider_still_reports_unknown(self, monkeypatch):
        from fluid_build.cli._common import CLIError, build_provider

        monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
        monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
        _rediscover()
        with pytest.raises(CLIError) as excinfo:
            build_provider("nosuchcloud", None, None, LOG)
        assert excinfo.value.event == "provider_unknown"
