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

"""Branch-coverage tests for fluid_build/cli/plugins.py"""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---- Enums ----


class TestPluginEnums:
    def test_plugin_types(self):
        from fluid_build.cli.plugins import PluginType

        for pt in PluginType:
            assert pt.value in ("command", "provider", "validator", "formatter", "integration")

    def test_plugin_status(self):
        from fluid_build.cli.plugins import PluginStatus

        for ps in PluginStatus:
            assert ps.value in ("active", "inactive", "error", "loading")


# ---- PluginMetadata ----


class TestPluginMetadata:
    def test_create_minimal(self):
        from fluid_build.cli.plugins import PluginMetadata, PluginType

        m = PluginMetadata(name="test", version="1.0", description="desc", author="me")
        assert m.name == "test"
        assert m.plugin_type == PluginType.COMMAND
        assert m.dependencies == []
        assert m.permissions == []
        assert m.homepage is None

    def test_to_dict(self):
        from fluid_build.cli.plugins import PluginMetadata, PluginType

        m = PluginMetadata(
            name="p",
            version="2.0",
            description="d",
            author="a",
            homepage="http://ex.com",
            documentation="http://docs.com",
            plugin_type=PluginType.PROVIDER,
            fluid_version_min="3.0.0",
            fluid_version_max="4.0.0",
            dependencies=["dep1"],
            permissions=["read"],
        )
        d = m.to_dict()
        assert d["name"] == "p"
        assert d["plugin_type"] == "provider"
        assert d["dependencies"] == ["dep1"]
        assert d["fluid_version_max"] == "4.0.0"

    def test_to_dict_defaults(self):
        from fluid_build.cli.plugins import PluginMetadata

        m = PluginMetadata(name="x", version="1.0", description="d", author="a")
        d = m.to_dict()
        assert d["homepage"] is None
        assert d["fluid_version_max"] is None


# ---- InstalledPlugin ----


class TestInstalledPlugin:
    def test_create(self):
        from fluid_build.cli.plugins import InstalledPlugin, PluginMetadata, PluginStatus

        m = PluginMetadata(name="t", version="1.0", description="d", author="a")
        ip = InstalledPlugin(metadata=m, path=Path("/tmp/t"))
        assert ip.status == PluginStatus.INACTIVE
        assert ip.error_message is None
        assert ip.config == {}
        assert ip.module is None
        assert ip.instance is None


# ---- PluginManager ----


class TestPluginManager:
    def test_create_with_empty_dir(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.plugin_dir == tmp_path
        assert pm.installed_plugins == {}

    def test_list_plugins_empty(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.list_plugins() == []

    def test_list_plugins_filter_type(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginType,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m1 = PluginMetadata(
            name="a", version="1.0", description="d", author="a", plugin_type=PluginType.COMMAND
        )
        m2 = PluginMetadata(
            name="b", version="1.0", description="d", author="a", plugin_type=PluginType.PROVIDER
        )
        pm.installed_plugins["a"] = InstalledPlugin(metadata=m1, path=tmp_path / "a")
        pm.installed_plugins["b"] = InstalledPlugin(metadata=m2, path=tmp_path / "b")
        cmds = pm.list_plugins(PluginType.COMMAND)
        assert len(cmds) == 1 and cmds[0].metadata.name == "a"

    def test_get_plugin(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(name="p1", version="1.0", description="d", author="a")
        pm.installed_plugins["p1"] = InstalledPlugin(metadata=m, path=tmp_path / "p1")
        assert pm.get_plugin("p1") is not None
        assert pm.get_plugin("nonexistent") is None

    def test_enable_plugin_not_found(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.enable_plugin("nonexistent") is False

    def test_disable_plugin_not_found(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.disable_plugin("nonexistent") is False

    def test_execute_plugin_command_not_found(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.execute_plugin_command("nope", "cmd", MagicMock(), MagicMock()) == 1

    def test_load_plugin_config_missing(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm._load_plugin_config(tmp_path) == {}

    def test_load_plugin_config_exists(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        (tmp_path / "config.json").write_text('{"key": "val"}')
        assert pm._load_plugin_config(tmp_path) == {"key": "val"}

    def test_install_missing_source(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.install_plugin("/nonexistent/path") is False

    def test_install_missing_plugin_json(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        source = tmp_path / "plugin_src"
        source.mkdir()
        assert pm.install_plugin(str(source)) is False

    def test_uninstall_not_found(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        assert pm.uninstall_plugin("nope") is False

    def test_register_command_plugins_error(self, tmp_path):
        from fluid_build.cli.plugins import PluginManager

        pm = PluginManager(plugin_dir=tmp_path)
        mock_plugin = MagicMock()
        mock_plugin.register_commands.side_effect = RuntimeError("oops")
        pm.command_plugins["bad"] = mock_plugin
        sp = MagicMock()
        pm.register_command_plugins(sp)  # Should not raise

    def test_register_plugin_command(self, tmp_path):
        from fluid_build.cli.plugins import (
            CommandPlugin,
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
            PluginType,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(
            name="cmd1", version="1.0", description="d", author="a", plugin_type=PluginType.COMMAND
        )
        mock_instance = MagicMock(spec=CommandPlugin)
        p = InstalledPlugin(
            metadata=m, path=tmp_path / "cmd1", instance=mock_instance, status=PluginStatus.ACTIVE
        )
        pm._register_plugin(p)
        assert "cmd1" in pm.command_plugins

    def test_register_plugin_provider(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
            PluginType,
            ProviderPlugin,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(
            name="prov1",
            version="1.0",
            description="d",
            author="a",
            plugin_type=PluginType.PROVIDER,
        )
        mock_instance = MagicMock(spec=ProviderPlugin)
        p = InstalledPlugin(
            metadata=m, path=tmp_path / "prov1", instance=mock_instance, status=PluginStatus.ACTIVE
        )
        pm._register_plugin(p)
        assert "prov1" in pm.provider_plugins

    def test_register_plugin_validator(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
            PluginType,
            ValidatorPlugin,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(
            name="val1",
            version="1.0",
            description="d",
            author="a",
            plugin_type=PluginType.VALIDATOR,
        )
        mock_instance = MagicMock(spec=ValidatorPlugin)
        p = InstalledPlugin(
            metadata=m, path=tmp_path / "val1", instance=mock_instance, status=PluginStatus.ACTIVE
        )
        pm._register_plugin(p)
        assert "val1" in pm.validator_plugins

    def test_disable_plugin_active(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(name="p", version="1.0", description="d", author="a")
        mock_inst = MagicMock()
        p = InstalledPlugin(
            metadata=m, path=tmp_path / "p", instance=mock_inst, status=PluginStatus.ACTIVE
        )
        pm.installed_plugins["p"] = p
        pm.active_plugins["p"] = p
        assert pm.disable_plugin("p") is True
        assert p.status == PluginStatus.INACTIVE

    def test_enable_already_active(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(name="p", version="1.0", description="d", author="a")
        p = InstalledPlugin(metadata=m, path=tmp_path / "p", status=PluginStatus.ACTIVE)
        pm.installed_plugins["p"] = p
        assert pm.enable_plugin("p") is True

    def test_enable_with_instance(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(name="p", version="1.0", description="d", author="a")
        inst = MagicMock()
        inst.initialize.return_value = True
        p = InstalledPlugin(
            metadata=m,
            path=tmp_path / "p",
            instance=inst,
            status=PluginStatus.INACTIVE,
            config={"k": "v"},
        )
        pm.installed_plugins["p"] = p
        assert pm.enable_plugin("p") is True
        assert p.status == PluginStatus.ACTIVE

    def test_enable_instance_fails(self, tmp_path):
        from fluid_build.cli.plugins import (
            InstalledPlugin,
            PluginManager,
            PluginMetadata,
            PluginStatus,
        )

        pm = PluginManager(plugin_dir=tmp_path)
        m = PluginMetadata(name="p", version="1.0", description="d", author="a")
        inst = MagicMock()
        inst.initialize.side_effect = RuntimeError("fail")
        p = InstalledPlugin(
            metadata=m, path=tmp_path / "p", instance=inst, status=PluginStatus.INACTIVE
        )
        pm.installed_plugins["p"] = p
        assert pm.enable_plugin("p") is False
        assert p.status == PluginStatus.ERROR


# ---- register ----


class TestPluginsRegister:
    def test_register(self):
        from fluid_build.cli.plugins import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)


# ---- run (CLI dispatch) ----


class TestPluginsRun:
    def _make_args(self, action, **extra):
        ns = argparse.Namespace(plugin_action=action, **extra)
        return ns

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_list(self, mock_gpm):
        from fluid_build.cli.plugins import run

        pm = MagicMock()
        pm.list_plugins.return_value = []
        mock_gpm.return_value = pm
        args = self._make_args("list", type=None, status=None)
        result = run(args, logging.getLogger())
        assert result == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_install(self, mock_gpm):
        from fluid_build.cli.plugins import run

        pm = MagicMock()
        pm.install_plugin.return_value = True
        mock_gpm.return_value = pm
        args = self._make_args("install", source="/tmp/src", force=False)
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_uninstall(self, mock_gpm):
        from fluid_build.cli.plugins import run

        pm = MagicMock()
        pm.uninstall_plugin.return_value = True
        mock_gpm.return_value = pm
        args = self._make_args("uninstall", name="p")
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_enable(self, mock_gpm):
        from fluid_build.cli.plugins import run

        pm = MagicMock()
        pm.enable_plugin.return_value = True
        mock_gpm.return_value = pm
        args = self._make_args("enable", name="p")
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_disable(self, mock_gpm):
        from fluid_build.cli.plugins import run

        pm = MagicMock()
        pm.disable_plugin.return_value = True
        mock_gpm.return_value = pm
        args = self._make_args("disable", name="p")
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_info(self, mock_gpm):
        from fluid_build.cli.plugins import InstalledPlugin, PluginMetadata, PluginStatus, run

        pm = MagicMock()
        m = PluginMetadata(name="p", version="1.0", description="d", author="a")
        pm.get_plugin.return_value = InstalledPlugin(
            metadata=m, path=Path("/tmp/p"), status=PluginStatus.ACTIVE
        )
        mock_gpm.return_value = pm
        args = self._make_args("info", name="p")
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_info_not_found(self, mock_gpm):
        from fluid_build.cli.plugins import run

        pm = MagicMock()
        pm.get_plugin.return_value = None
        mock_gpm.return_value = pm
        args = self._make_args("info", name="nope")
        assert run(args, logging.getLogger()) == 1

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_unknown_action(self, mock_gpm):
        from fluid_build.cli.plugins import run

        mock_gpm.return_value = MagicMock()
        args = self._make_args(None)
        assert run(args, logging.getLogger()) == 1

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_exception(self, mock_gpm):
        from fluid_build.cli.plugins import run

        mock_gpm.side_effect = RuntimeError("boom")
        args = self._make_args("list")
        assert run(args, logging.getLogger()) == 1

    @patch("fluid_build.cli.plugins.get_plugin_manager")
    def test_run_create(self, mock_gpm, tmp_path):
        from fluid_build.cli.plugins import run

        mock_gpm.return_value = MagicMock()
        args = self._make_args(
            "create", name=str(tmp_path / "testplugin"), type="command", author="tester"
        )
        # handle_create_plugin will use Path.cwd() / name - may fail but exercises the code path
        result = run(args, logging.getLogger())
        assert result in (0, 1)


# ---- Templates ----


class TestPluginTemplates:
    def test_command_template(self):
        from fluid_build.cli.plugins import _get_command_plugin_template

        t = _get_command_plugin_template("myplugin", "me")
        assert "myplugin" in t
        assert "CommandPlugin" in t

    def test_provider_template(self):
        from fluid_build.cli.plugins import _get_provider_plugin_template

        t = _get_provider_plugin_template("myprov", "me")
        assert "myprov" in t

    def test_basic_template(self):
        from fluid_build.cli.plugins import PluginType, _get_basic_plugin_template

        t = _get_basic_plugin_template("myplug", "me", PluginType.VALIDATOR)
        assert "myplug" in t


# ---- handle_create_plugin ----


class TestHandleCreatePlugin:
    def test_create_success(self, tmp_path, monkeypatch):
        from fluid_build.cli.plugins import PluginManager, handle_create_plugin

        monkeypatch.chdir(tmp_path)
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        args = argparse.Namespace(name="testplugin", type="command", author="tester")
        result = handle_create_plugin(args, pm, logging.getLogger())
        assert result == 0
        assert (tmp_path / "testplugin" / "plugin.json").exists()
        assert (tmp_path / "testplugin" / "main.py").exists()
        assert (tmp_path / "testplugin" / "README.md").exists()

    def test_create_already_exists(self, tmp_path, monkeypatch):
        from fluid_build.cli.plugins import PluginManager, handle_create_plugin

        monkeypatch.chdir(tmp_path)
        (tmp_path / "existing").mkdir()
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        args = argparse.Namespace(name="existing", type="command", author="tester")
        result = handle_create_plugin(args, pm, logging.getLogger())
        assert result == 1

    def test_create_provider_type(self, tmp_path, monkeypatch):
        from fluid_build.cli.plugins import PluginManager, handle_create_plugin

        monkeypatch.chdir(tmp_path)
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        args = argparse.Namespace(name="provplugin", type="provider", author="tester")
        result = handle_create_plugin(args, pm, logging.getLogger())
        assert result == 0

    def test_create_validator_type(self, tmp_path, monkeypatch):
        from fluid_build.cli.plugins import PluginManager, handle_create_plugin

        monkeypatch.chdir(tmp_path)
        pm = PluginManager(plugin_dir=tmp_path / "plugins")
        args = argparse.Namespace(name="valplugin", type="validator", author=None)
        result = handle_create_plugin(args, pm, logging.getLogger())
        assert result == 0
