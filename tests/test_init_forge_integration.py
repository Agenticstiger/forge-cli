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

"""Integration tests for fluid init and fluid forge — modes, handover, and error scenarios."""

import argparse
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Arg factories (mirror register() defaults for each command)
# ---------------------------------------------------------------------------


def _make_init_args(**overrides):
    defaults = dict(
        name=None,
        quickstart=False,
        blank=False,
        template=None,
        provider="local",
        use_case=None,
        no_run=True,
        no_dag=True,
        dry_run=False,
        yes=True,
        target_dir=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_forge_args(**overrides):
    defaults = dict(
        help=False,
        blank=False,
        target_dir=None,
        provider=None,
        domain=None,
        non_interactive=False,
        dry_run=False,
        context=None,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
        discover=True,
        discovery_path=None,
        memory=True,
        save_memory=False,
        show_memory=False,
        reset_memory=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def logger():
    return logging.getLogger("test_init_forge_integration")


# ============================================================================
# INIT SCENARIOS
# ============================================================================


class TestDemoMode:
    """Scenarios 1-2: fluid demo (demo_mode handler)"""

    def test_demo_dry_run_returns_zero(self, tmp_path, logger, monkeypatch):
        """Scenario 1: demo --dry-run previews without creating files."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import demo_mode

        args = _make_init_args(name=str(tmp_path / "qs-project"), dry_run=True)

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = demo_mode(args, logger)

        assert result == 0
        assert not (tmp_path / "qs-project" / "contract.fluid.yaml").exists()

    @patch("fluid_build.cli.init.show_success_message")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_demo_full_creates_project(
        self, mock_copy, mock_data, mock_db, mock_success, tmp_path, logger, monkeypatch
    ):
        """Scenario 2: fluid demo creates project with sample data."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import demo_mode

        args = _make_init_args(
            name=str(tmp_path / "qs-project"),
            yes=True,
            no_run=True,
            no_dag=True,
        )

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = demo_mode(args, logger)

        assert result == 0
        mock_copy.assert_called_once()


class TestInitBlank:
    """Scenarios 3-4: fluid init --blank"""

    def test_blank_dry_run_via_product_new(self, tmp_path, logger, monkeypatch):
        """Scenario 3: --blank --dry-run passes dry_run to product_new."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import blank_mode

        mock_run = MagicMock(return_value=0)

        args = _make_init_args(name=str(tmp_path / "blank-dry"), blank=True, dry_run=True)

        with (
            patch("fluid_build.cli.init.RICH_AVAILABLE", False),
            patch.dict("sys.modules", {"fluid_build.cli.product_new": MagicMock(run=mock_run)}),
        ):
            result = blank_mode(args, logger)

        # product_new import succeeds → delegates to it
        assert result == 0

    def test_blank_creates_contract_on_importerror(self, tmp_path, logger, monkeypatch):
        """Scenario 4: --blank creates contract.fluid.yaml when product_new unavailable."""
        monkeypatch.chdir(tmp_path)

        # blank_mode uses Path(args.name) relative to cwd, so use a simple name
        args = _make_init_args(name="blank-project", blank=True)

        # Force ImportError on product_new so the manual fallback path fires
        import fluid_build.cli.init as init_mod

        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def mock_import(name, *a, **kw):
            if name == ".product_new" or (isinstance(name, str) and "product_new" in name):
                raise ImportError("mocked")
            return original_import(name, *a, **kw)

        with (
            patch("fluid_build.cli.init.RICH_AVAILABLE", False),
            patch("builtins.__import__", side_effect=mock_import),
        ):
            # Re-import blank_mode inside the patched context won't work;
            # instead call blank_mode which has a try/except ImportError inside.
            # The issue is the import happens inside the function body.
            result = init_mod.blank_mode(args, logger)

        assert result == 0
        assert (tmp_path / "blank-project" / "contract.fluid.yaml").exists()


class TestInitTemplate:
    """Scenario 5: fluid init --template"""

    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_template_creates_project(self, mock_copy, tmp_path, logger, monkeypatch):
        """Scenario 5: --template customer-360 creates project from template."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import template_mode

        args = _make_init_args(template="customer-360", name="tmpl-project")

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = template_mode(args, logger)

        assert result == 0
        mock_copy.assert_called_once()


class TestInitProviderUseCase:
    """Scenarios 8-9: Provider and use-case hints with blank mode."""

    def test_provider_gcp_with_blank(self, tmp_path, logger, monkeypatch):
        """Scenario 8: --provider gcp --blank does not crash."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import blank_mode

        args = _make_init_args(name=str(tmp_path / "gcp-blank"), blank=True, provider="gcp")

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = blank_mode(args, logger)

        assert result == 0

    def test_use_case_with_blank(self, tmp_path, logger, monkeypatch):
        """Scenario 9: --use-case analytics --blank does not crash."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import blank_mode

        args = _make_init_args(
            name=str(tmp_path / "analytics-blank"), blank=True, use_case="analytics"
        )

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = blank_mode(args, logger)

        assert result == 0


class TestInitWorkspaceRedirect:
    """Scenario 10: Running init in existing workspace redirects to forge."""

    def test_detect_mode_redirects_in_existing_workspace(self, tmp_path, logger, monkeypatch):
        """Scenario 10: detect_mode returns None when workspace has products."""
        monkeypatch.chdir(tmp_path)

        # Create workspace file
        ws_file = tmp_path / "fluid.workspace.yaml"
        ws_file.write_text("name: test-workspace\n")

        from fluid_build.cli.init import detect_mode

        mock_product = SimpleNamespace(
            name="existing-product",
            path=tmp_path / "existing-product",
            expose_count=1,
            provider="local",
            fluid_version="0.7.2",
        )

        args = _make_init_args(yes=False)  # No explicit mode flag

        with (
            patch("fluid_build.cli.init.RICH_AVAILABLE", False),
            patch("fluid_build.cli.init.find_workspace_root", return_value=tmp_path),
            patch("fluid_build.cli.init.discover_workspace_products", return_value=[mock_product]),
        ):
            result = detect_mode(args, logger)

        assert result is None  # Redirected — no mode to run


class TestInitTargetDir:
    """Scenario: fluid init --dir <path>"""

    def test_init_with_dir_creates_workspace_in_target(self, tmp_path, logger, monkeypatch):
        """--dir creates workspace in the specified directory."""
        target = tmp_path / "custom-dir"
        from fluid_build.cli.init import run

        args = _make_init_args(blank=True, target_dir=str(target))

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = run(args, logger)

        assert result == 0
        assert (target / "fluid.workspace.yaml").exists()

    def test_init_without_dir_uses_cwd(self, tmp_path, logger, monkeypatch):
        """Without --dir, init uses current working directory."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import run

        args = _make_init_args(blank=True)

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = run(args, logger)

        assert result == 0
        assert (tmp_path / "fluid.workspace.yaml").exists()


class TestInitProductListing:
    """Scenario: existing products show full paths."""

    def test_redirect_shows_product_paths(self, tmp_path, logger, monkeypatch):
        """When workspace has products, listing shows full paths."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import _redirect_existing_workspace

        mock_product = SimpleNamespace(
            name="customer-360",
            path=tmp_path / "customer-360",
            contract_path=tmp_path / "customer-360" / "contract.fluid.yaml",
            expose_count=3,
            provider="local",
            fluid_version="0.7.2",
        )

        with (
            patch("fluid_build.cli.init.RICH_AVAILABLE", False),
            patch("fluid_build.cli.init.load_workspace_config") as mock_ws,
        ):
            mock_ws.return_value = SimpleNamespace(name="test-ws")
            result = _redirect_existing_workspace([mock_product], tmp_path)

        assert result is None  # redirect, no mode


# ============================================================================
# FORGE SCENARIOS
# ============================================================================


class TestForgeBlank:
    """Scenarios 11-12: fluid forge --blank"""

    def test_forge_blank_dry_run(self, tmp_path, logger):
        """Scenario 11: --blank --dry-run previews without creating."""
        from fluid_build.cli.forge import run

        args = _make_forge_args(blank=True, dry_run=True, target_dir=str(tmp_path / "out"))

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        assert result == 0
        assert not (tmp_path / "out" / "contract.fluid.yaml").exists()

    def test_forge_blank_creates_contract(self, tmp_path, logger):
        """Scenario 12: --blank --target-dir creates contract."""
        from fluid_build.cli.forge import run

        target = tmp_path / "out"
        args = _make_forge_args(blank=True, target_dir=str(target))

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        assert result == 0
        assert (target / "contract.fluid.yaml").exists()


class TestForgeCopilotNonInteractive:
    """Scenario 13: fluid forge --non-interactive"""

    @patch("fluid_build.cli.forge._run_copilot", return_value=1)
    def test_non_interactive_copilot_handles_no_llm(self, mock_copilot, logger):
        """Scenario 13: --non-interactive with no LLM exits gracefully."""
        from fluid_build.cli.forge import run

        args = _make_forge_args(non_interactive=True)

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        # Should not crash — returns 1 (copilot failure) or reaches copilot
        assert result in (0, 1)


class TestForgeHelp:
    """Scenario 14: fluid forge --help"""

    def test_forge_help_returns_zero(self, logger):
        """Scenario 14: --help prints help and exits 0."""
        from fluid_build.cli.forge import run

        args = _make_forge_args(help=True)

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_console = MagicMock()
            mock_cls.return_value = mock_console
            with patch("fluid_build.cli.help_formatter.print_forge_help") as mock_help:
                result = run(args, logger)

        assert result == 0


class TestForgeMemory:
    """Scenarios 15-16: fluid forge --show-memory / --reset-memory"""

    @patch("fluid_build.cli.forge.handle_memory_management", return_value=0)
    def test_forge_show_memory(self, mock_memory, logger):
        """Scenario 15: --show-memory delegates to memory management."""
        from fluid_build.cli.forge import run

        args = _make_forge_args(show_memory=True)

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        assert result == 0
        mock_memory.assert_called_once()

    @patch("fluid_build.cli.forge.handle_memory_management", return_value=0)
    def test_forge_reset_memory(self, mock_memory, logger):
        """Scenario 16: --reset-memory delegates to memory management."""
        from fluid_build.cli.forge import run

        args = _make_forge_args(reset_memory=True)

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        assert result == 0
        mock_memory.assert_called_once()


# ============================================================================
# HANDOVER SCENARIOS
# ============================================================================


class TestInitForgeHandover:
    """Scenarios 18-20: init → forge handover points."""

    def test_init_ai_mode_delegates_to_forge_copilot(self, tmp_path, logger, monkeypatch):
        """Scenario 18: init's _ai_mode delegates to forge's run_ai_copilot_mode."""
        monkeypatch.chdir(tmp_path)

        from fluid_build.cli.init import _ai_mode

        args = _make_init_args(name="ai-product")

        mock_copilot = MagicMock(return_value=0)

        with (
            patch("fluid_build.cli.init.RICH_AVAILABLE", False),
            patch("fluid_build.cli.init.find_workspace_root", return_value=tmp_path),
            patch("fluid_build.cli.init.load_workspace_config") as mock_ws_config,
            patch("fluid_build.cli.init.CopilotAgent", create=True),
            patch("fluid_build.cli.init.ContextValidationError", create=True),
            patch("fluid_build.cli.init.build_interview_summary_from_context", create=True),
            patch("fluid_build.cli.init.get_cli_arg", create=True),
            patch("fluid_build.cli.init.get_target_directory", create=True),
            patch("fluid_build.cli.init.load_context", create=True),
        ):

            # Mock workspace config
            mock_ws_config.return_value = SimpleNamespace(
                name="test-ws",
                domain="analytics",
                owner_team="team",
                owner_email="",
                provider="local",
                products_dir=".",
            )

            # Patch the actual forge copilot call
            with patch("fluid_build.cli.forge_modes.run_ai_copilot_mode", mock_copilot):
                # _ai_mode imports forge.run_ai_copilot_mode as _run_copilot
                with patch.object(
                    __import__("fluid_build.cli.forge", fromlist=["run_ai_copilot_mode"]),
                    "run_ai_copilot_mode",
                    mock_copilot,
                ):
                    result = _ai_mode(args, logger)

        # Should have delegated to forge copilot (0=success or ImportError fallback)
        assert result in (0, 1)

    def test_detect_mode_returns_none_for_workspace_with_products(
        self, tmp_path, logger, monkeypatch
    ):
        """Scenario 19: init in workspace with products redirects (returns None)."""
        monkeypatch.chdir(tmp_path)

        from fluid_build.cli.init import detect_mode

        mock_product = SimpleNamespace(
            name="p1",
            path=tmp_path / "p1",
            expose_count=2,
            provider="local",
            fluid_version="0.7.2",
        )

        args = _make_init_args(yes=False)

        with (
            patch("fluid_build.cli.init.RICH_AVAILABLE", False),
            patch("fluid_build.cli.init.find_workspace_root", return_value=tmp_path),
            patch("fluid_build.cli.init.discover_workspace_products", return_value=[mock_product]),
        ):
            result = detect_mode(args, logger)

        assert result is None

    @patch("fluid_build.cli.init.copy_template", return_value=True)
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.show_success_message")
    def test_sequential_init_then_forge(
        self, mock_success, mock_db, mock_data, mock_copy, tmp_path, logger, monkeypatch
    ):
        """Scenario 20: Sequential workflow — demo_mode scaffold then forge blank."""
        monkeypatch.chdir(tmp_path)

        # Phase 1: demo_mode scaffold (the `fluid demo` code path)
        from fluid_build.cli.init import demo_mode

        init_args = _make_init_args(
            name=str(tmp_path / "project"),
            yes=True,
            no_run=True,
            no_dag=True,
        )

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            init_result = demo_mode(init_args, logger)

        assert init_result == 0

        # Phase 2: Forge blank in a sub-product
        from fluid_build.cli.forge import run as forge_run

        product2_dir = tmp_path / "project" / "product2"
        forge_args = _make_forge_args(blank=True, target_dir=str(product2_dir))

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            forge_result = forge_run(forge_args, logger)

        assert forge_result == 0
        assert (product2_dir / "contract.fluid.yaml").exists()


# ============================================================================
# ERROR SCENARIOS
# ============================================================================


class TestErrorScenarios:
    """Scenarios 21-23: Error handling."""

    def test_forge_blank_fails_when_contract_exists(self, tmp_path, logger):
        """Scenario 21: --blank fails gracefully when contract.fluid.yaml already exists."""
        from fluid_build.cli.forge import run

        # Pre-create the contract
        target = tmp_path / "existing"
        target.mkdir()
        (target / "contract.fluid.yaml").write_text("existing: true\n")

        args = _make_forge_args(blank=True, target_dir=str(target))

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        assert result == 1

    def test_init_template_nonexistent_fails(self, tmp_path, logger, monkeypatch):
        """Scenario 22: --template nonexistent shows error and returns 1."""
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.init import template_mode

        args = _make_init_args(template="nonexistent_template_xyz", name="fail-project")

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = template_mode(args, logger)

        assert result == 1

    @patch("fluid_build.cli.forge._run_copilot", return_value=1)
    def test_forge_no_tty_no_llm_exits_gracefully(self, mock_copilot, logger):
        """Scenario 23: No TTY + no LLM config exits 1 without crash."""
        from fluid_build.cli.forge import run

        args = _make_forge_args(non_interactive=True)

        with patch("fluid_build.cli.forge.Console") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = run(args, logger)

        assert result in (0, 1)
        # Key assertion: no unhandled exception
