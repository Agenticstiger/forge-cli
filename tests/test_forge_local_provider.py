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

"""Unit tests for fluid_build.forge.providers.local — LocalProvider implementation."""

import unittest
from pathlib import Path
from unittest.mock import patch

from fluid_build.forge.core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    TemplateMetadata,
)
from fluid_build.forge.providers.local import LocalProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(project_name="myproduct", provider_config=None):
    """Build a minimal GenerationContext for testing."""
    if provider_config is None:
        provider_config = {}
    return GenerationContext(
        project_config={"name": project_name},
        target_dir=Path("/tmp/test-project"),
        template_metadata=TemplateMetadata(
            name="stub",
            description="d",
            complexity=ComplexityLevel.BEGINNER,
            provider_support=["local"],
            use_cases=["test"],
            technologies=["python"],
            estimated_time="1 min",
            tags=[],
        ),
        provider_config=provider_config,
        user_selections={},
        forge_version="1.0.0",
        creation_time="2024-01-01T00:00:00",
    )


# ---------------------------------------------------------------------------
# get_metadata
# ---------------------------------------------------------------------------


class TestLocalProviderGetMetadata(unittest.TestCase):
    def setUp(self):
        self.provider = LocalProvider()

    def test_returns_dict(self):
        meta = self.provider.get_metadata()
        self.assertIsInstance(meta, dict)

    def test_has_name(self):
        meta = self.provider.get_metadata()
        self.assertIn("name", meta)
        self.assertIsInstance(meta["name"], str)

    def test_supported_services(self):
        meta = self.provider.get_metadata()
        self.assertIn("supported_services", meta)
        services = meta["supported_services"]
        self.assertIn("docker", services)

    def test_complexity_is_beginner(self):
        meta = self.provider.get_metadata()
        self.assertEqual(meta["complexity"], "beginner")

    def test_cost_is_free(self):
        meta = self.provider.get_metadata()
        self.assertEqual(meta["cost"], "free")


# ---------------------------------------------------------------------------
# get_required_tools / get_environment_variables
# ---------------------------------------------------------------------------


class TestLocalProviderTools(unittest.TestCase):
    def setUp(self):
        self.provider = LocalProvider()

    def test_required_tools_includes_python(self):
        tools = self.provider.get_required_tools()
        self.assertIn("python", tools)

    def test_required_tools_includes_git(self):
        tools = self.provider.get_required_tools()
        self.assertIn("git", tools)

    def test_environment_variables_non_empty(self):
        env_vars = self.provider.get_environment_variables()
        self.assertGreater(len(env_vars), 0)

    def test_environment_variables_contains_fluid_env(self):
        env_vars = self.provider.get_environment_variables()
        combined = " ".join(env_vars)
        self.assertIn("FLUID_ENV", combined)


# ---------------------------------------------------------------------------
# check_prerequisites
# ---------------------------------------------------------------------------


class TestLocalProviderCheckPrerequisites(unittest.TestCase):
    def setUp(self):
        self.provider = LocalProvider()

    @patch("fluid_build.forge.providers.local.shutil.which")
    def test_returns_valid_when_python_and_git_present(self, mock_which):
        def which_side(cmd):
            return "/usr/bin/" + cmd if cmd in ("python3", "git") else None

        mock_which.side_effect = which_side
        is_valid, errors = self.provider.check_prerequisites()
        self.assertTrue(is_valid)
        # No errors about python or git
        error_text = " ".join(errors)
        self.assertNotIn("Python not found", error_text)
        self.assertNotIn("Git not found", error_text)

    @patch("fluid_build.forge.providers.local.shutil.which")
    def test_fails_when_python_missing(self, mock_which):
        def which_side(cmd):
            return None if "python" in cmd else "/usr/bin/" + cmd

        mock_which.side_effect = which_side
        is_valid, errors = self.provider.check_prerequisites()
        self.assertFalse(is_valid)
        error_text = " ".join(errors)
        self.assertIn("Python", error_text)

    @patch("fluid_build.forge.providers.local.shutil.which")
    def test_fails_when_git_missing(self, mock_which):
        def which_side(cmd):
            return "/usr/bin/python3" if "python" in cmd else None

        mock_which.side_effect = which_side
        is_valid, errors = self.provider.check_prerequisites()
        self.assertFalse(is_valid)
        error_text = " ".join(errors)
        self.assertIn("Git", error_text)

    @patch("fluid_build.forge.providers.local.shutil.which")
    def test_docker_missing_is_warning_not_error(self, mock_which):
        def which_side(cmd):
            if "python" in cmd or cmd == "git":
                return "/usr/bin/" + cmd
            return None  # docker not found

        mock_which.side_effect = which_side
        is_valid, messages = self.provider.check_prerequisites()
        # Should be valid despite no docker
        self.assertTrue(is_valid)
        # Should have a docker warning
        combined = " ".join(messages)
        self.assertIn("Docker", combined)


# ---------------------------------------------------------------------------
# validate_configuration
# ---------------------------------------------------------------------------


class TestLocalProviderValidateConfiguration(unittest.TestCase):
    def setUp(self):
        self.provider = LocalProvider()

    @patch("fluid_build.forge.providers.local.shutil.which", return_value=None)
    def test_docker_not_found_is_warning(self, _mock_which):
        config = {"use_docker": True}
        is_valid, messages = self.provider.validate_configuration(config)
        self.assertTrue(is_valid)
        combined = " ".join(messages)
        self.assertIn("Docker", combined)

    @patch("fluid_build.forge.providers.local.shutil.which", return_value="/usr/bin/docker")
    def test_valid_config_with_docker_present(self, _mock_which):
        config = {"use_docker": True, "python_version": "3.9"}
        is_valid, errors = self.provider.validate_configuration(config)
        self.assertTrue(is_valid)

    def test_old_python_version_error(self):
        config = {"python_version": "3.6"}
        is_valid, errors = self.provider.validate_configuration(config)
        self.assertFalse(is_valid)
        error_text = " ".join(errors)
        self.assertIn("not supported", error_text)

    def test_invalid_python_version_format_warning(self):
        config = {"python_version": "latest"}
        is_valid, messages = self.provider.validate_configuration(config)
        self.assertTrue(is_valid)
        combined = " ".join(messages)
        self.assertIn("Invalid Python version", combined)

    def test_use_docker_false_no_docker_check(self):
        config = {"use_docker": False, "python_version": "3.9"}
        is_valid, errors = self.provider.validate_configuration(config)
        self.assertTrue(is_valid)


# ---------------------------------------------------------------------------
# generate_config
# ---------------------------------------------------------------------------


class TestLocalProviderGenerateConfig(unittest.TestCase):
    def setUp(self):
        self.provider = LocalProvider()

    def test_generate_config_with_docker_produces_dockerfile(self):
        ctx = _make_context(provider_config={"use_docker": True, "setup_venv": False})
        files = self.provider.generate_config(ctx)
        self.assertIn("Dockerfile", files)

    def test_generate_config_with_docker_compose(self):
        ctx = _make_context(
            provider_config={"use_docker": True, "docker_compose": True, "setup_venv": False}
        )
        files = self.provider.generate_config(ctx)
        self.assertIn("docker-compose.yml", files)

    def test_generate_config_with_venv_produces_requirements(self):
        ctx = _make_context(provider_config={"use_docker": False, "setup_venv": True})
        files = self.provider.generate_config(ctx)
        self.assertIn("requirements.txt", files)

    def test_generate_config_produces_env_example(self):
        ctx = _make_context(provider_config={"use_docker": False, "setup_venv": False})
        files = self.provider.generate_config(ctx)
        self.assertIn(".env.example", files)

    def test_generate_config_produces_dev_scripts(self):
        ctx = _make_context(provider_config={"use_docker": False, "setup_venv": False})
        files = self.provider.generate_config(ctx)
        # At least one script should be generated
        script_files = [k for k in files if k.startswith("scripts/")]
        self.assertGreater(len(script_files), 0)

    def test_generate_config_sqlite_env_contains_db_path(self):
        ctx = _make_context(
            project_name="testproduct",
            provider_config={"use_docker": False, "setup_venv": False, "database": "sqlite"},
        )
        files = self.provider.generate_config(ctx)
        env_content = files.get(".env.example", "")
        self.assertIn("DB_PATH", env_content)

    def test_generate_config_postgres_env_contains_db_host(self):
        ctx = _make_context(
            project_name="pgproduct",
            provider_config={
                "use_docker": False,
                "setup_venv": False,
                "database": "postgres",
                "postgres": {
                    "host": "localhost",
                    "port": 5432,
                    "database": "pgproduct",
                    "user": "postgres",
                    "password": "secret",
                },
            },
        )
        files = self.provider.generate_config(ctx)
        env_content = files.get(".env.example", "")
        self.assertIn("DB_HOST", env_content)

    def test_generate_config_postgres_docker_compose_includes_service(self):
        ctx = _make_context(
            project_name="pgapp",
            provider_config={
                "use_docker": True,
                "docker_compose": True,
                "setup_venv": False,
                "database": "postgres",
                "postgres": {
                    "host": "localhost",
                    "port": 5432,
                    "database": "pgapp",
                    "user": "postgres",
                    "password": "pass",
                },
            },
        )
        files = self.provider.generate_config(ctx)
        compose_content = files.get("docker-compose.yml", "")
        self.assertIn("postgres", compose_content)

    def test_generate_config_python_setup_produces_pyproject_toml(self):
        ctx = _make_context(
            provider_config={"use_docker": False, "setup_venv": True, "python_version": "3.9"}
        )
        files = self.provider.generate_config(ctx)
        self.assertIn("pyproject.toml", files)

    def test_generate_config_python_setup_produces_setup_py(self):
        ctx = _make_context(
            provider_config={"use_docker": False, "setup_venv": True, "python_version": "3.9"}
        )
        files = self.provider.generate_config(ctx)
        self.assertIn("setup.py", files)

    def test_generate_config_dockerignore_present(self):
        ctx = _make_context(provider_config={"use_docker": True, "setup_venv": False})
        files = self.provider.generate_config(ctx)
        self.assertIn(".dockerignore", files)


if __name__ == "__main__":
    unittest.main()
