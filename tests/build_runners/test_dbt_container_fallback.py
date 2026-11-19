# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which dbt runs a build, and what the container fallback installs.

* A build's engine names a PyPI distribution (``dbt-athena-community``); dbt
  lists the plugin under its import package (``athena``), so the local dbt is
  checked for that name and used when it has it.
* The fallback runs a digest-pinned image and installs exactly pinned packages
  from wheels only; an override that floats is refused.
* An AWS adapter in a container gets AWS keys from the environment only: a dbt
  profile naming an AWS profile, or no keys at all, is refused before
  ``docker`` starts, and host-only AWS settings are not passed in.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.build_runners.dbt import runner
from fluid_build.build_runners.dbt.runner import (
    CONTAINER_ADAPTER_PINS,
    DBT_CORE_PIN,
    _build_containerized_dbt_command,
    _dbt_command_supports_adapter,
    _dbt_version_output,
    _detect_dbt_engine,
    _infer_dbt_adapter,
    build_dbt_command,
)
from fluid_build.forge.core.pipeline_systems._base import PINNED_IMAGES
from fluid_build.forge.core.pipeline_systems._engine_specs import _DBT_PLATFORM_ADAPTERS

pytestmark = [pytest.mark.unit]

#: ``dbt --version`` of dbt-core 1.11.11 with dbt-athena-community installed.
CORE_WITH_ATHENA = (
    "Core:\n"
    "  - installed: 1.11.11\n"
    "  - latest:    1.12.7  - Update available!\n"
    "\n"
    "Plugins:\n"
    "  - postgres: 1.11.0b1 - Update available!\n"
    "  - athena:   1.10.1   - Update available!\n"
    "\n"
    "  At least one plugin is out of date with dbt-core.\n"
)
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
#: A fake access key id, built at run time so no file holds a key-shaped literal.
FAKE_KEY_ID = "AKIA" + "TESTKEY".ljust(16, "X")
PROFILE = "lake"
AWS_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "DBT_EXECUTABLE",
        "DBT_DOCKER_IMAGE",
        "DBT_BOOTSTRAP_IMAGE",
        "DBT_ADAPTER_PACKAGE",
        "DBT_TARGET",
        *AWS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    for fn in (_dbt_version_output, _detect_dbt_engine, _dbt_command_supports_adapter):
        fn.cache_clear()
    yield
    for fn in (_dbt_version_output, _detect_dbt_engine, _dbt_command_supports_adapter):
        fn.cache_clear()


def _local_dbt(monkeypatch: pytest.MonkeyPatch, version_output: str) -> None:
    def _run(cmd: List[str], **_kwargs: Any) -> Any:
        assert cmd[1:] == ["--version"], cmd
        return SimpleNamespace(returncode=0, stdout=version_output, stderr="")

    monkeypatch.setattr(runner, "_resolve_dbt_executable", lambda: "/venv/bin/dbt")
    monkeypatch.setattr(runner.subprocess, "run", _run)
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: "/usr/local/bin/docker" if name == "docker" else None
    )


def _project(tmp_path: Path, output: Dict[str, Any]) -> Path:
    """A dbt project whose profile ``lake`` has one ``dev`` output."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text(f"name: marts\nprofile: {PROFILE}\n", "utf-8")
    profiles = {PROFILE: {"target": "dev", "outputs": {"dev": output}}}
    (project / "profiles.yml").write_text(yaml.safe_dump(profiles), "utf-8")
    return project


def _athena_output(**extra: Any) -> Dict[str, Any]:
    return {"type": "athena", "s3_staging_dir": "s3://lake/stage/", "schema": "marts", **extra}


class TestAdapterName:
    @pytest.mark.parametrize(
        "engine, plugin",
        [
            ("dbt-athena-community", "athena"),
            ("dbt-athena", "athena"),
            ("dbt-snowflake", "snowflake"),
            ("DBT-DuckDB", "duckdb"),
            ("dbt-layer-bigquery", "layer_bigquery"),
        ],
    )
    def test_the_engine_names_the_plugin(self, engine: str, plugin: str) -> None:
        assert _infer_dbt_adapter({"engine": engine}) == plugin

    def test_the_local_dbt_is_read_by_plugin_name(self, monkeypatch) -> None:
        _local_dbt(monkeypatch, CORE_WITH_ATHENA)
        assert _dbt_command_supports_adapter("/venv/bin/dbt", "athena") is True
        assert _dbt_command_supports_adapter("/venv/bin/dbt", "athena-community") is False

    def test_a_name_elsewhere_in_the_output_is_no_plugin(self, monkeypatch) -> None:
        _local_dbt(monkeypatch, CORE_WITH_ATHENA)
        # "core" and "latest" are Core: lines, not plugins.
        assert _dbt_command_supports_adapter("/venv/bin/dbt", "latest") is False

    @pytest.mark.parametrize(
        "build, plugin",
        [
            (
                {"engine": "dbt-core", "execution": {"runtime": {"platform": "snowflake"}}},
                "snowflake",
            ),
            ({"engine": "dbt-core"}, None),
            ({"engine": "dbt", "execution": {"runtime": {"platform": "aws-athena"}}}, "athena"),
            ({"engine": "dbt", "execution": {"runtime": {"platform": "glue"}}}, "glue"),
            ({"engine": "dbt", "execution": {"runtime": {"platform": "postgresql"}}}, "postgres"),
            ({"engine": "dbt", "execution": "aws"}, None),
        ],
    )
    def test_an_engine_without_an_adapter_reads_the_platform(self, build, plugin) -> None:
        assert _infer_dbt_adapter(build) == plugin

    def test_a_local_dbt_with_the_plugin_runs_the_build(self, tmp_path: Path, monkeypatch) -> None:
        _local_dbt(monkeypatch, CORE_WITH_ATHENA)
        project = _project(tmp_path, _athena_output(aws_profile_name="fluid-demo"))
        cmd = build_dbt_command({"engine": "dbt-athena-community"}, project)
        assert cmd[:2] == ["/venv/bin/dbt", "build"]
        assert "docker" not in cmd


class TestPins:
    def test_every_pin_is_exact_and_names_the_platform_package(self) -> None:
        assert re.fullmatch(r"dbt-core==\d+\.\d+\.\d+", DBT_CORE_PIN)
        for adapter, pin in CONTAINER_ADAPTER_PINS.items():
            name, _, version = pin.partition("==")
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), pin
            assert [name] == [spec.split(">=")[0] for spec in _DBT_PLATFORM_ADAPTERS[adapter]]

    def test_the_image_is_the_pinned_python_slim(self) -> None:
        # The runner keeps a copy (build_runners must not import pipeline_systems,
        # which reaches cli): a refresh of PINNED_IMAGES fails here until it follows.
        assert runner.BOOTSTRAP_IMAGE == PINNED_IMAGES["python:3.12-slim"]
        assert DIGEST.search(runner.BOOTSTRAP_IMAGE)


class TestContainerCommand:
    def _command(self, tmp_path: Path, adapter: str = "duckdb") -> List[str]:
        project = tmp_path / "p"
        project.mkdir(exist_ok=True)
        return _build_containerized_dbt_command(
            adapter, ["build", "--project-dir", str(project)], project, None
        )

    def test_pinned_image_and_packages_from_wheels(self, tmp_path: Path) -> None:
        cmd = self._command(tmp_path, "athena")
        assert cmd[-4] == PINNED_IMAGES["python:3.12-slim"]
        assert cmd[-3:-1] == ["sh", "-lc"]
        script = shlex.split(cmd[-1].split("&&")[0])
        assert "--only-binary=:all:" in script
        assert script[-2:] == [DBT_CORE_PIN, "dbt-athena-community==1.11.1"]

    def test_no_aws_directory_is_mounted(self, tmp_path: Path) -> None:
        cmd = self._command(tmp_path, "athena")
        mounts = [cmd[i + 1] for i, part in enumerate(cmd) if part == "-v"]
        assert mounts and not [m for m in mounts if ".aws" in m]

    def test_host_only_aws_settings_stay_on_the_host(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret-never-in-argv")
        monkeypatch.setenv("AWS_PROFILE", "fluid-demo")
        monkeypatch.setenv("AWS_CONFIG_FILE", "/Users/someone/.aws/config")
        monkeypatch.setenv("AWS_CA_BUNDLE", "/etc/ssl/corp.pem")
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/token")
        cmd = self._command(tmp_path, "athena")
        passed = {cmd[i + 1] for i, part in enumerate(cmd) if part == "-e"}
        assert {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} <= passed
        host_only = {
            "AWS_PROFILE",
            "AWS_CONFIG_FILE",
            "AWS_CA_BUNDLE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
        }
        assert not passed & host_only
        assert "example-secret-never-in-argv" not in " ".join(cmd)

    def test_pip_reads_no_setting_a_project_could_choose(self, tmp_path, monkeypatch) -> None:
        exports = {"PIP_INDEX_URL": "https://index.example/simple", "PIP_EXTRA_INDEX_URL": "x"}
        monkeypatch.setattr(
            "fluid_build.credentials.dotenv_store.project_exports", lambda *_a, **_k: exports
        )
        cmd = self._command(tmp_path, "athena")
        passed = {cmd[i + 1] for i, part in enumerate(cmd) if part == "-e"}
        assert not [name for name in passed if name.startswith("PIP_")]
        script = shlex.split(cmd[-1].split("&&")[0])
        assert "--isolated" in script
        assert script[script.index("--index-url") + 1] == "https://pypi.org/simple"
        # pip reads no config file, and the project directory is not on sys.path.
        assert script[:5] == ["PIP_CONFIG_FILE=/dev/null", "python", "-I", "-m", "pip"]

    def test_a_project_env_cannot_steer_the_python_in_the_container(
        self, tmp_path, monkeypatch
    ) -> None:
        exports = {
            "PYTHONWARNINGS": "error",
            "PYTHONBREAKPOINT": "x.y",
            "PYTHONPYCACHEPREFIX": "/p",
        }
        monkeypatch.setattr(
            "fluid_build.credentials.dotenv_store.project_exports", lambda *_a, **_k: exports
        )
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
        monkeypatch.setenv("FLUID_DBT_FORWARD_ENV", "PYTHONHASHSEED")
        monkeypatch.setenv("PYTHONHASHSEED", "0")
        cmd = self._command(tmp_path, "athena")
        passed = {cmd[i + 1] for i, part in enumerate(cmd) if part == "-e"}
        assert not passed & set(exports)
        # forge's own switch and what the operator forwards on purpose still pass.
        assert {"PYTHON_DOTENV_DISABLED", "PYTHONHASHSEED"} <= passed

    def test_an_adapter_without_a_pin_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="no pinned package for the dbt adapter 'glue'"):
            self._command(tmp_path, "glue")

    @pytest.mark.parametrize(
        "hostile", ["snowflake; rm -rf / #", "duckdb`id`", "duckdb$(id)", "athena-community"]
    )
    def test_a_contract_cannot_choose_what_is_installed(self, tmp_path: Path, hostile) -> None:
        with pytest.raises(RuntimeError, match="no pinned package"):
            self._command(tmp_path, hostile)

    def test_an_exact_override_is_installed_with_dbt_core(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DBT_ADAPTER_PACKAGE", "dbt-glue==1.12.4")
        script = shlex.split(self._command(tmp_path, "glue")[-1].split("&&")[0])
        assert script[-2:] == [DBT_CORE_PIN, "dbt-glue==1.12.4"]

    def test_an_override_naming_dbt_core_keeps_it(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DBT_ADAPTER_PACKAGE", "dbt_core==1.12.6, dbt-trino==1.12.0")
        script = shlex.split(self._command(tmp_path, "trino")[-1].split("&&")[0])
        assert script[-2:] == ["dbt_core==1.12.6", "dbt-trino==1.12.0"]
        assert DBT_CORE_PIN not in script

    @pytest.mark.parametrize(
        "override", ["dbt-glue", "dbt-glue>=1.12", "dbt-glue==1.*", "dbt-glue==1.0;id", " "]
    )
    def test_an_override_that_floats_is_refused(self, tmp_path, monkeypatch, override) -> None:
        monkeypatch.setenv("DBT_ADAPTER_PACKAGE", override)
        with pytest.raises(RuntimeError, match="must list exact pins"):
            self._command(tmp_path, "glue")

    @pytest.mark.parametrize("env_name", ["DBT_BOOTSTRAP_IMAGE", "DBT_DOCKER_IMAGE"])
    def test_an_image_by_tag_is_refused(self, tmp_path, monkeypatch, env_name) -> None:
        monkeypatch.setenv(env_name, "example/dbt:latest")
        with pytest.raises(RuntimeError, match=f"{env_name}=.*names a tag"):
            self._command(tmp_path)

    def test_an_image_by_digest_is_used(self, tmp_path, monkeypatch) -> None:
        image = "registry.local:5000/example/dbt-duckdb:1.11-Slim@sha256:" + "a" * 64
        monkeypatch.setenv("DBT_DOCKER_IMAGE", image)
        cmd = self._command(tmp_path)
        assert cmd[cmd.index(image) + 1] == "dbt"

    @pytest.mark.parametrize(
        "image", ["python:3.12 --privileged", "Python@sha256:" + "a" * 64, "x@sha256:abc"]
    )
    def test_an_image_that_is_no_pinned_reference_is_refused(
        self, tmp_path, monkeypatch, image
    ) -> None:
        monkeypatch.setenv("DBT_BOOTSTRAP_IMAGE", image)
        with pytest.raises(RuntimeError, match="is not an image reference pinned by digest"):
            self._command(tmp_path)


class TestAwsCredentials:
    """The container path: the local dbt has no athena/redshift plugin."""

    def _container(self, monkeypatch) -> None:
        _local_dbt(monkeypatch, CORE_WITH_ATHENA.replace("athena:", "duckdb:"))

    def test_a_named_aws_profile_is_refused_with_the_way_out(self, tmp_path, monkeypatch) -> None:
        self._container(monkeypatch)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example")
        project = _project(tmp_path, _athena_output(aws_profile_name="fluid-demo"))
        with pytest.raises(RuntimeError) as exc:
            build_dbt_command({"engine": "dbt-athena-community"}, project)
        text = str(exc.value)
        assert "names an AWS profile (aws_profile_name)" in text
        assert "pip install 'dbt-athena-community==1.11.1'" in text
        assert "aws configure export-credentials --profile <that profile> --format env" in text
        assert "unset AWS_PROFILE" in text
        # The value may be a template resolved from the environment: never printed.
        assert "fluid-demo" not in text

    def test_no_keys_is_refused(self, tmp_path, monkeypatch) -> None:
        self._container(monkeypatch)
        project = _project(tmp_path, _athena_output())
        with pytest.raises(RuntimeError, match="neither the environment nor the dbt profile"):
            build_dbt_command({"engine": "dbt-athena-community"}, project)

    @pytest.mark.parametrize(
        "output, env",
        [
            ({"aws_access_key_id": FAKE_KEY_ID, "aws_secret_access_key": "x"}, {}),
            ({}, {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/run"}),
            ({"aws_profile_name": "{{ env_var('LAKE_PROFILE', '') }}"}, {"KEYS": "1"}),
            ({"aws_profile_name": "{{ 'x' if false }}"}, {"KEYS": "1"}),
        ],
        ids=["keys-in-profile", "ecs-endpoint", "env-var-unset", "other-template"],
    )
    def test_credentials_the_container_has_are_enough(self, tmp_path, monkeypatch, output, env):
        self._container(monkeypatch)
        for name, value in env.items():
            if name == "KEYS":
                monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
                monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example")
            else:
                monkeypatch.setenv(name, value)
        project = _project(tmp_path, _athena_output(**output))
        assert build_dbt_command({"engine": "dbt-athena-community"}, project)[0] == "docker"

    def test_a_host_variable_the_container_lacks_names_no_profile(self, tmp_path, monkeypatch):
        self._container(monkeypatch)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example")
        monkeypatch.setenv("LAKE_PROFILE", "host-only")  # no forwarded prefix
        output = _athena_output(aws_profile_name="{{ env_var('LAKE_PROFILE', '') }}")
        project = _project(tmp_path, output)
        assert build_dbt_command({"engine": "dbt-athena-community"}, project)[0] == "docker"

    def test_keys_from_the_project_env_file_reach_the_container(self, tmp_path, monkeypatch):
        self._container(monkeypatch)
        exports = {"AWS_ACCESS_KEY_ID": FAKE_KEY_ID, "AWS_SECRET_ACCESS_KEY": "x"}
        monkeypatch.setattr(
            "fluid_build.credentials.dotenv_store.project_exports", lambda *_a, **_k: exports
        )
        project = _project(tmp_path, _athena_output())
        assert build_dbt_command({"engine": "dbt-athena-community"}, project)[0] == "docker"

    def test_a_rendered_env_var_profile_is_refused(self, tmp_path, monkeypatch) -> None:
        self._container(monkeypatch)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example")
        # DBT_* reaches the container, so dbt there renders the name from it.
        monkeypatch.setenv("DBT_LAKE_PROFILE", "secret-looking-name")
        output = _athena_output(aws_profile_name="{{ env_var('DBT_LAKE_PROFILE') }}")
        project = _project(tmp_path, output)
        with pytest.raises(RuntimeError) as exc:
            build_dbt_command({"engine": "dbt-athena-community"}, project)
        assert "names an AWS profile (aws_profile_name)" in str(exc.value)
        assert "secret-looking-name" not in str(exc.value)

    def test_keys_in_the_environment_run_in_the_container(self, tmp_path, monkeypatch) -> None:
        self._container(monkeypatch)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example")
        project = _project(tmp_path, _athena_output())
        cmd = build_dbt_command({"engine": "dbt-athena-community"}, project)
        assert cmd[:3] == ["docker", "run", "--rm"]

    def test_the_target_dbt_reads_is_the_one_checked(self, tmp_path, monkeypatch) -> None:
        self._container(monkeypatch)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example")
        project = _project(tmp_path, _athena_output())
        profiles = yaml.safe_load((project / "profiles.yml").read_text("utf-8"))
        profiles[PROFILE]["outputs"]["laptop"] = _athena_output(aws_profile_name="mine")
        (project / "profiles.yml").write_text(yaml.safe_dump(profiles), "utf-8")
        assert build_dbt_command({"engine": "dbt-athena-community"}, project)[0] == "docker"
        monkeypatch.setenv("DBT_TARGET", "laptop")
        with pytest.raises(RuntimeError, match=r"names an AWS profile \(aws_profile_name\)"):
            build_dbt_command({"engine": "dbt-athena-community"}, project)

    def test_redshift_with_a_password_needs_no_keys(self, tmp_path, monkeypatch) -> None:
        self._container(monkeypatch)
        project = _project(tmp_path, {"type": "redshift", "host": "h", "user": "u"})
        assert build_dbt_command({"engine": "dbt-redshift"}, project)[0] == "docker"

    @pytest.mark.parametrize(
        "output, match",
        [
            ({"method": "iam"}, "neither the environment"),
            ({"method": "iam_role"}, "neither the environment"),
            (
                {"method": "iam", "iam_profile": "warehouse"},
                r"names an AWS profile \(iam_profile\)",
            ),
        ],
    )
    def test_redshift_iam_is_refused_without_keys(self, tmp_path, monkeypatch, output, match):
        self._container(monkeypatch)
        project = _project(tmp_path, {"type": "redshift", **output})
        with pytest.raises(RuntimeError, match=match):
            build_dbt_command({"engine": "dbt-redshift"}, project)

    def test_another_adapter_is_not_checked(self, tmp_path, monkeypatch) -> None:
        _local_dbt(monkeypatch, CORE_WITH_ATHENA)
        project = _project(tmp_path, {"type": "snowflake", "account": "a"})
        assert build_dbt_command({"engine": "dbt-snowflake"}, project)[0] == "docker"


class TestProfileRead:
    @pytest.mark.parametrize(
        "document",
        [
            {"lake": {"target": ["dev"], "outputs": {"dev": {"type": "athena"}}}},
            {"lake": {"target": {"a": 1}, "outputs": {"dev": {"type": "athena"}}}},
            {"lake": {"target": "dev", "outputs": ["dev"]}},
            ["lake"],
        ],
    )
    def test_a_profile_of_another_shape_is_unknown(self, tmp_path: Path, document) -> None:
        (tmp_path / "profiles.yml").write_text(yaml.safe_dump(document), "utf-8")
        assert runner._profile_output(tmp_path, "lake", None) == {}
        assert runner._profile_output(tmp_path, ["lake"], ["dev"]) == {}  # type: ignore[arg-type]

    def test_a_deeply_nested_profile_is_unknown(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.yml").write_text("[" * 5000 + "]" * 5000, "utf-8")
        assert runner._profile_output(tmp_path, "lake", None) == {}
