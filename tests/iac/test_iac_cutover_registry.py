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

"""``default_engine`` derives from the IaC plugin registry.

``iac/registry.py`` advertises that an external package adds a cloud via one
entry point. It could register and be listed, but nothing routed to it:
``default_engine`` read only the in-tree ``OPENTOFU_DEFAULT_PROVIDERS``
frozenset, so a plugin cloud resolved to ``native`` and the OpenTofu path the
plugin exists for was never selected.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import registry as iac_registry
from fluid_build.iac.cutover import OPENTOFU_DEFAULT_PROVIDERS, default_engine, resolve_engine

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_registry():
    plugins_before = dict(iac_registry.IAC_PLUGINS)
    eps_before = set(iac_registry.IAC_ENTRYPOINT_PLUGINS)
    yield
    iac_registry.IAC_PLUGINS.clear()
    iac_registry.IAC_PLUGINS.update(plugins_before)
    iac_registry.IAC_ENTRYPOINT_PLUGINS.clear()
    iac_registry.IAC_ENTRYPOINT_PLUGINS.update(eps_before)


class TestDefaultEngine:
    @pytest.mark.parametrize("cloud", sorted(OPENTOFU_DEFAULT_PROVIDERS))
    def test_in_tree_clouds_are_unchanged(self, cloud):
        assert default_engine(cloud) == "opentofu"

    def test_local_stays_native(self):
        assert default_engine("local") == "native"

    def test_unregistered_name_is_native(self):
        assert default_engine("nosuchcloud") == "native"

    def test_an_entrypoint_plugin_cloud_routes_to_opentofu(self):
        iac_registry.register_iac_plugin("vcloud", object())
        iac_registry.IAC_ENTRYPOINT_PLUGINS.add("vcloud")
        assert default_engine("vcloud") == "opentofu"

    def test_an_in_tree_cloud_absent_from_the_frozenset_stays_native(self, monkeypatch):
        """The frozenset remains the strangler-fig cutover switch for in-tree
        clouds: an emitter exists for ``aws``, but if it is not listed as cut
        over it must keep its native path. Only out-of-tree plugin clouds —
        which can never be listed without a core edit — derive from the
        registry."""
        monkeypatch.setattr(
            "fluid_build.iac.cutover.OPENTOFU_DEFAULT_PROVIDERS", frozenset({"gcp"})
        )
        assert default_engine("gcp") == "opentofu"
        assert default_engine("aws") == "native"

    def test_explicit_override_still_wins(self):
        assert resolve_engine("native", "snowflake") == "native"
        assert resolve_engine("opentofu", "local") == "opentofu"
        assert resolve_engine(None, "snowflake") == "opentofu"

    def test_a_blocked_plugin_cloud_does_not_route(self, monkeypatch):
        """The allow/block policy gates ``register_iac_plugin``, so a blocked
        cloud never enters the registry and never routes."""
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "vcloud")
        iac_registry.register_iac_plugin("vcloud", object())
        assert iac_registry.get_iac_plugin("vcloud") is None
        assert default_engine("vcloud") == "native"
