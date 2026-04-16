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

"""Tests for fluid_build.cli.plugins — plugin system data structures & manager."""

import json
from pathlib import Path

from fluid_build.cli.plugins import (
    InstalledPlugin,
    PluginManager,
    PluginMetadata,
    PluginStatus,
    PluginType,
)


class TestPluginType:
    def test_values(self):
        assert PluginType.COMMAND.value == "command"
        assert PluginType.PROVIDER.value == "provider"
        assert PluginType.VALIDATOR.value == "validator"
        assert PluginType.FORMATTER.value == "formatter"
        assert PluginType.INTEGRATION.value == "integration"


class TestPluginStatus:
    def test_values(self):
        assert PluginStatus.ACTIVE.value == "active"
        assert PluginStatus.INACTIVE.value == "inactive"
        assert PluginStatus.ERROR.value == "error"
        assert PluginStatus.LOADING.value == "loading"


class TestPluginMetadata:
    def test_defaults(self):
        meta = PluginMetadata(
            name="my-plugin",
            version="1.0.0",
            description="A plugin",
            author="Test",
        )
        assert meta.plugin_type == PluginType.COMMAND
        assert meta.dependencies == []
        assert meta.permissions == []
        assert meta.homepage is None

    def test_to_dict(self):
        meta = PluginMetadata(
            name="p",
            version="1.0",
            description="d",
            author="a",
            homepage="https://example.com",
            plugin_type=PluginType.PROVIDER,
            dependencies=["dep1"],
        )
        d = meta.to_dict()
        assert d["name"] == "p"
        assert d["plugin_type"] == "provider"
        assert d["dependencies"] == ["dep1"]
        assert d["homepage"] == "https://example.com"


class TestInstalledPlugin:
    def test_defaults(self):
        meta = PluginMetadata(name="x", version="1", description="d", author="a")
        ip = InstalledPlugin(metadata=meta, path=Path("/tmp/x"))
        assert ip.status == PluginStatus.INACTIVE
        assert ip.module is None
        assert ip.instance is None
        assert ip.config == {}


class TestPluginManager:
    def test_init_creates_plugin_dir(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        pm = PluginManager(plugin_dir=plugin_dir)
        assert plugin_dir.exists()
        assert pm.installed_plugins == {}

    def test_install_missing_source(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        result = pm.install_plugin(tmp_path / "nonexistent")
        assert result is False

    def test_install_no_plugin_json(self, tmp_path):
        source = tmp_path / "bad_plugin"
        source.mkdir()
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        result = pm.install_plugin(source)
        assert result is False

    def test_install_and_uninstall(self, tmp_path):
        # Create a fake plugin source
        source = tmp_path / "my_plugin_src"
        source.mkdir()
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "test-plugin",
                    "version": "1.0",
                    "description": "Test",
                    "author": "Tester",
                    "main_module": "main",
                }
            )
        )
        (source / "main.py").write_text("pass")

        plugin_dir = tmp_path / "plugins"
        pm = PluginManager(plugin_dir=plugin_dir)

        # Install (without a real PluginInterface class, it won't activate)
        result = pm.install_plugin(source)
        assert result is True
        assert (plugin_dir / "test-plugin").exists()

        # Uninstall
        result = pm.uninstall_plugin("test-plugin")
        assert result is True
        assert not (plugin_dir / "test-plugin").exists()

    def test_uninstall_nonexistent(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        assert pm.uninstall_plugin("nope") is False

    def test_install_already_installed_no_force(self, tmp_path):
        source = tmp_path / "p"
        source.mkdir()
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "dup",
                    "version": "1.0",
                    "description": "d",
                    "author": "a",
                    "main_module": "main",
                }
            )
        )
        (source / "main.py").write_text("pass")

        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        pm.install_plugin(source)
        # Second install without force should fail
        result = pm.install_plugin(source, force=False)
        assert result is False

    def test_install_force_overwrites(self, tmp_path):
        source = tmp_path / "p"
        source.mkdir()
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "forceable",
                    "version": "1.0",
                    "description": "d",
                    "author": "a",
                    "main_module": "main",
                }
            )
        )
        (source / "main.py").write_text("pass")

        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        pm.install_plugin(source)
        result = pm.install_plugin(source, force=True)
        assert result is True

    def test_enable_disable_nonexistent(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        assert pm.enable_plugin("nope") is False
        assert pm.disable_plugin("nope") is False

    def test_load_plugin_config(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        pm = PluginManager(plugin_dir=plugin_dir)

        # No config file -> empty dict
        fake_path = tmp_path / "fake"
        fake_path.mkdir()
        assert pm._load_plugin_config(fake_path) == {}

        # With config file
        (fake_path / "config.json").write_text(json.dumps({"key": "val"}))
        assert pm._load_plugin_config(fake_path) == {"key": "val"}
