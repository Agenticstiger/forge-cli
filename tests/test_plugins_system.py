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

"""Tests for cli/plugins.py — plugin system enums, dataclasses, manager, CLI handlers."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.plugins import (
    CommandPlugin,
    InstalledPlugin,
    PluginInterface,
    PluginManager,
    PluginMetadata,
    PluginStatus,
    PluginType,
    ProviderPlugin,
    ValidatorPlugin,
    handle_disable_plugin,
    handle_enable_plugin,
    handle_install_plugin,
    handle_uninstall_plugin,
    run,
)


# ── Enums ────────────────────────────────────────────────────────────
class TestPluginType:
    def test_values(self):
        assert PluginType.COMMAND.value == "command"
        assert PluginType.PROVIDER.value == "provider"
        assert PluginType.VALIDATOR.value == "validator"
        assert PluginType.FORMATTER.value == "formatter"
        assert PluginType.INTEGRATION.value == "integration"

    def test_from_value(self):
        assert PluginType("command") is PluginType.COMMAND


class TestPluginStatus:
    def test_values(self):
        assert PluginStatus.ACTIVE.value == "active"
        assert PluginStatus.INACTIVE.value == "inactive"
        assert PluginStatus.ERROR.value == "error"
        assert PluginStatus.LOADING.value == "loading"


# ── PluginMetadata ───────────────────────────────────────────────────
class TestPluginMetadata:
    def test_defaults(self):
        m = PluginMetadata("n", "1.0", "desc", "author")
        assert m.homepage is None
        assert m.documentation is None
        assert m.plugin_type is PluginType.COMMAND
        assert m.fluid_version_min == "2.0.0"
        assert m.fluid_version_max is None
        assert m.dependencies == []
        assert m.permissions == []

    def test_to_dict(self):
        m = PluginMetadata("n", "1.0", "desc", "author", homepage="http://x")
        d = m.to_dict()
        assert d["name"] == "n"
        assert d["homepage"] == "http://x"
        assert d["plugin_type"] == "command"
        assert d["dependencies"] == []


# ── InstalledPlugin ──────────────────────────────────────────────────
class TestInstalledPlugin:
    def test_defaults(self):
        meta = PluginMetadata("x", "0.1", "d", "a")
        ip = InstalledPlugin(metadata=meta, path=Path("/tmp/x"))
        assert ip.status is PluginStatus.INACTIVE
        assert ip.module is None
        assert ip.instance is None
        assert ip.error_message is None
        assert ip.config == {}


# ── PluginManager ────────────────────────────────────────────────────
class TestPluginManager:
    def test_init_creates_dir(self, tmp_path):
        pdir = tmp_path / "plugins"
        pm = PluginManager(plugin_dir=pdir)
        assert pdir.exists()
        assert pm.installed_plugins == {}

    def test_list_plugins_empty(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.list_plugins() == []

    def test_list_plugins_with_type_filter(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta_cmd = PluginMetadata("cmd", "1", "d", "a", plugin_type=PluginType.COMMAND)
        meta_prov = PluginMetadata("prov", "1", "d", "a", plugin_type=PluginType.PROVIDER)
        pm.installed_plugins["cmd"] = InstalledPlugin(metadata=meta_cmd, path=tmp_path)
        pm.installed_plugins["prov"] = InstalledPlugin(metadata=meta_prov, path=tmp_path)
        result = pm.list_plugins(PluginType.PROVIDER)
        assert len(result) == 1
        assert result[0].metadata.name == "prov"

    def test_get_plugin_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta = PluginMetadata("x", "1", "d", "a")
        pm.installed_plugins["x"] = InstalledPlugin(metadata=meta, path=tmp_path)
        assert pm.get_plugin("x") is not None

    def test_get_plugin_not_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.get_plugin("missing") is None

    def test_install_nonexistent_source(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.install_plugin(tmp_path / "nonexistent") is False

    def test_install_missing_plugin_json(self, tmp_path):
        src = tmp_path / "src_plugin"
        src.mkdir()
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.install_plugin(src) is False

    def test_uninstall_not_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.uninstall_plugin("ghost") is False

    def test_enable_not_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.enable_plugin("ghost") is False

    def test_disable_not_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.disable_plugin("ghost") is False

    def test_enable_already_active(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta = PluginMetadata("x", "1", "d", "a")
        ip = InstalledPlugin(metadata=meta, path=tmp_path, status=PluginStatus.ACTIVE)
        pm.installed_plugins["x"] = ip
        assert pm.enable_plugin("x") is True

    def test_disable_active_plugin(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta = PluginMetadata("x", "1", "d", "a")
        mock_instance = MagicMock(spec=PluginInterface)
        ip = InstalledPlugin(
            metadata=meta, path=tmp_path, status=PluginStatus.ACTIVE, instance=mock_instance
        )
        pm.installed_plugins["x"] = ip
        pm.active_plugins["x"] = ip
        assert pm.disable_plugin("x") is True
        assert "x" not in pm.active_plugins
        mock_instance.cleanup.assert_called_once()

    def test_register_command_plugin(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta = PluginMetadata("c1", "1", "d", "a", plugin_type=PluginType.COMMAND)
        mock_cmd = MagicMock(spec=CommandPlugin)
        ip = InstalledPlugin(metadata=meta, path=tmp_path, instance=mock_cmd)
        pm._register_plugin(ip)
        assert "c1" in pm.command_plugins

    def test_register_provider_plugin(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta = PluginMetadata("p1", "1", "d", "a", plugin_type=PluginType.PROVIDER)
        mock_prov = MagicMock(spec=ProviderPlugin)
        ip = InstalledPlugin(metadata=meta, path=tmp_path, instance=mock_prov)
        pm._register_plugin(ip)
        assert "p1" in pm.provider_plugins

    def test_register_validator_plugin(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        meta = PluginMetadata("v1", "1", "d", "a", plugin_type=PluginType.VALIDATOR)
        mock_val = MagicMock(spec=ValidatorPlugin)
        ip = InstalledPlugin(metadata=meta, path=tmp_path, instance=mock_val)
        pm._register_plugin(ip)
        assert "v1" in pm.validator_plugins

    def test_load_plugin_config_exists(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        plugin_dir = tmp_path / "myplugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text('{"key": "val"}')
        cfg = pm._load_plugin_config(plugin_dir)
        assert cfg == {"key": "val"}

    def test_load_plugin_config_missing(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        cfg = pm._load_plugin_config(tmp_path)
        assert cfg == {}

    def test_execute_plugin_command_not_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        assert pm.execute_plugin_command("nope", "cmd", MagicMock(), logging.getLogger()) == 1

    def test_execute_plugin_command_found(self, tmp_path):
        pm = PluginManager(plugin_dir=tmp_path / "p")
        mock_cmd = MagicMock(spec=CommandPlugin)
        mock_cmd.execute.return_value = 0
        pm.command_plugins["c1"] = mock_cmd
        result = pm.execute_plugin_command("c1", "do", MagicMock(), logging.getLogger())
        assert result == 0
        mock_cmd.execute.assert_called_once()


# ── CLI run() dispatcher ─────────────────────────────────────────────
class TestRunDispatcher:
    def _make_args(self, **kwargs):
        args = MagicMock()
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_unknown_action(self, mock_gpm):
        mock_gpm.return_value = MagicMock()
        args = self._make_args(plugin_action="unknown_xyz")
        result = run(args, logging.getLogger())
        assert result == 1

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    @patch("fluid_build.cli.plugins.handle_list_plugins", return_value=0)
    def test_list_action(self, _mock_handler, mock_gpm):
        mock_gpm.return_value = MagicMock()
        args = self._make_args(plugin_action="list")
        result = run(args, logging.getLogger())
        assert result == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    @patch("fluid_build.cli.plugins.handle_install_plugin", return_value=0)
    def test_install_action(self, _mock_handler, mock_gpm):
        mock_gpm.return_value = MagicMock()
        args = self._make_args(plugin_action="install")
        result = run(args, logging.getLogger())
        assert result == 0


# ── CLI handler wrappers ─────────────────────────────────────────────
class TestHandlers:
    def test_handle_install_success(self):
        pm = MagicMock()
        pm.install_plugin.return_value = True
        args = MagicMock()
        args.source = "/some/path"
        args.force = False
        assert handle_install_plugin(args, pm, logging.getLogger()) == 0

    def test_handle_install_failure(self):
        pm = MagicMock()
        pm.install_plugin.return_value = False
        args = MagicMock()
        args.source = "/x"
        args.force = True
        assert handle_install_plugin(args, pm, logging.getLogger()) == 1

    def test_handle_uninstall_success(self):
        pm = MagicMock()
        pm.uninstall_plugin.return_value = True
        args = MagicMock()
        args.name = "p"
        assert handle_uninstall_plugin(args, pm, logging.getLogger()) == 0

    def test_handle_enable(self):
        pm = MagicMock()
        pm.enable_plugin.return_value = True
        args = MagicMock()
        args.name = "p"
        assert handle_enable_plugin(args, pm, logging.getLogger()) == 0

    def test_handle_disable(self):
        pm = MagicMock()
        pm.disable_plugin.return_value = True
        args = MagicMock()
        args.name = "p"
        assert handle_disable_plugin(args, pm, logging.getLogger()) == 0
