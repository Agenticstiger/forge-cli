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

"""Tests for fluid_build.cli.init — should_generate_dag and detect_mode."""

from unittest.mock import MagicMock, patch

from fluid_build.cli.init import should_generate_dag


class TestShouldGenerateDag:
    def test_has_orchestration_config(self):
        contract = {"orchestration": {"schedule": "@daily"}}
        assert should_generate_dag(contract) is True

    def test_orchestration_focused_template(self):
        contract = {}
        assert should_generate_dag(contract, template="customer-360") is True
        assert should_generate_dag(contract, template="sales-analytics") is True
        assert should_generate_dag(contract, template="ml-features") is True
        assert should_generate_dag(contract, template="data-quality") is True

    def test_multiple_provider_actions(self):
        contract = {"binding": {"providerActions": [{"name": "a"}, {"name": "b"}]}}
        assert should_generate_dag(contract) is True

    def test_single_provider_action(self):
        contract = {"binding": {"providerActions": [{"name": "a"}]}}
        assert should_generate_dag(contract) is False

    def test_empty_contract(self):
        assert should_generate_dag({}) is False

    def test_non_orchestrated_template(self):
        assert should_generate_dag({}, template="starter") is False


class TestDetectMode:
    """Test detect_mode with mocked args and filesystem."""

    def _args(self, **kwargs):
        defaults = dict(
            quickstart=False,
            blank=False,
            template=None,
            name=None,
        )
        defaults.update(kwargs)
        a = MagicMock()
        for k, v in defaults.items():
            setattr(a, k, v)
        return a

    def test_explicit_quickstart(self):
        """--quickstart is now an alias for --template customer-360."""
        from fluid_build.cli.init import detect_mode

        args = self._args(quickstart=True)
        result = detect_mode(args, MagicMock())
        assert result == "template"
        assert args.template == "customer-360"

    def test_explicit_blank(self):
        from fluid_build.cli.init import detect_mode

        result = detect_mode(self._args(blank=True), MagicMock())
        assert result == "blank"

    def test_explicit_template(self):
        from fluid_build.cli.init import detect_mode

        result = detect_mode(self._args(template="starter"), MagicMock())
        assert result == "template"

    def test_existing_contract_returns_none(self, tmp_path):
        """If contract.fluid.yaml already exists, detect_mode returns None."""
        from fluid_build.cli.init import detect_mode

        (tmp_path / "contract.fluid.yaml").write_text("name: test")
        with (
            patch("fluid_build.cli.init.Path") as mock_path_cls,
            patch("fluid_build.cli.init.find_workspace_root", return_value=None),
        ):
            mock_cwd = MagicMock()
            mock_path_cls.cwd.return_value = mock_cwd
            (tmp_path / "contract.fluid.yaml").exists()  # pre-check
            # Mock the (cwd / "contract.fluid.yaml").exists() to return True
            mock_cwd.__truediv__ = lambda self, x: tmp_path / x
            mock_path_cls.home.return_value = tmp_path
            result = detect_mode(self._args(), MagicMock())
            assert result is None

    def test_first_time_user_shows_menu(self, tmp_path):
        """Non-existent ~/.fluid dir means first-time user → creation menu."""
        from fluid_build.cli.init import detect_mode

        with (
            patch("fluid_build.cli.init.Path") as mock_path_cls,
            patch("fluid_build.cli.init.find_workspace_root", return_value=None),
            patch("fluid_build.cli.init._ask_creation_mode", return_value="ai") as mock_menu,
        ):
            mock_cwd = MagicMock()
            # Nothing exists in cwd
            mock_cwd.__truediv__ = lambda self, x: tmp_path / x  # nothing exists
            mock_path_cls.cwd.return_value = mock_cwd
            # home dir has no .fluid
            mock_home = tmp_path / "fakehome"
            mock_path_cls.home.return_value = mock_home
            mock_cwd.glob = MagicMock(return_value=[])
            result = detect_mode(self._args(), MagicMock())
            mock_menu.assert_called_once()
            assert result == "ai"

    def test_menu_quickstart_normalizes_to_template_mode(self, tmp_path):
        """Menu option 'Quickstart' should route to template_mode, same as --quickstart flag.

        Both the CLI flag and the interactive menu label must produce the
        same artifacts (bare customer-360 scaffold). ``_resolve_menu_choice``
        rewrites the menu's ``"quickstart"`` return value to
        ``--template customer-360 --yes`` so both paths go through
        ``template_mode``.
        """
        from fluid_build.cli.init import detect_mode

        args = self._args()  # no explicit mode
        with (
            patch("fluid_build.cli.init.Path") as mock_path_cls,
            patch("fluid_build.cli.init.find_workspace_root", return_value=None),
            patch("fluid_build.cli.init._ask_creation_mode", return_value="quickstart"),
        ):
            mock_cwd = MagicMock()
            mock_cwd.__truediv__ = lambda self, x: tmp_path / x
            mock_path_cls.cwd.return_value = mock_cwd
            mock_path_cls.home.return_value = tmp_path / "fakehome"
            mock_cwd.glob = MagicMock(return_value=[])
            result = detect_mode(args, MagicMock())

        assert result == "template"
        assert args.template == "customer-360"
        assert args.yes is True


# ===========================================================================
# _print_templates_list — `fluid init --list-templates`
# ===========================================================================


class TestPrintTemplatesList:
    def test_lists_templates_when_available(self, capsys):
        from fluid_build.cli.init import _print_templates_list

        with (
            patch(
                "fluid_build.forge.simple_forge.list_templates",
                return_value=["customer-360", "hello-world"],
            ),
            patch(
                "fluid_build.forge.simple_forge.get_template_info",
                return_value={"description": "Example description"},
            ),
        ):
            rc = _print_templates_list()
        assert rc == 0

    def test_returns_0_when_no_templates(self):
        from fluid_build.cli.init import _print_templates_list

        with patch("fluid_build.forge.simple_forge.list_templates", return_value=[]):
            rc = _print_templates_list()
        assert rc == 0

    def test_returns_1_when_module_missing(self):
        from fluid_build.cli.init import _print_templates_list

        # Force the lazy import to fail.
        with patch.dict("sys.modules", {"fluid_build.forge.simple_forge": None}):
            rc = _print_templates_list()
        assert rc == 1

    def test_does_not_shadow_info_logger(self):
        """Regression: `_print_templates_list` used to rebind `info` to a dict,
        shadowing the `_logging.info` helper imported at module level.
        """
        from fluid_build.cli import init as init_mod

        with (
            patch(
                "fluid_build.forge.simple_forge.list_templates",
                return_value=["t1"],
            ),
            patch(
                "fluid_build.forge.simple_forge.get_template_info",
                return_value={"description": "d"},
            ),
        ):
            init_mod._print_templates_list()
        # After the call, `info` at module level should still be the callable helper.
        assert callable(init_mod.info)
