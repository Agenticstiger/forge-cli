# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the engine pip-spec registry shared by all CI emitters.

These cover the full (engine × source × sink) matrix the registry
supports plus the rendering helpers each CI emitter (jenkins,
github_actions, gitlab_ci, tekton, ...) calls. Adding a new engine
or source/sink kind in ``_engine_specs.py`` should add a row here.
"""

from __future__ import annotations

import pytest

from fluid_build.forge.core.pipeline_systems._engine_specs import (
    EngineBootstrap,
    EngineRuntime,
    render_bootstrap_shell_section,
    render_pip_install_command,
    render_runner_env_vars,
    render_runtime_notes,
    resolve_engine_bootstrap,
    resolve_engine_runtime,
)

# ── resolve_engine_bootstrap (engine × source × sink matrix) ────────


class TestResolveEngineBootstrap:
    """One test per (engine, common-source, common-sink) combo."""

    def test_dlt_postgres_to_snowflake(self):
        b = resolve_engine_bootstrap("dlt", source_kind="postgres", sink_platform="snowflake")
        assert "dlt[snowflake,sql_database]>=1.0" in b.packages
        assert "psycopg[binary]>=3.1" in b.packages
        assert b.notes == []

    def test_dlt_mysql_to_bigquery(self):
        b = resolve_engine_bootstrap("dlt", source_kind="mysql", sink_platform="bigquery")
        assert "dlt[bigquery,sql_database]>=1.0" in b.packages
        assert "pymysql>=1.1" in b.packages

    def test_dlt_s3_to_redshift(self):
        b = resolve_engine_bootstrap("dlt", source_kind="s3", sink_platform="redshift")
        assert "dlt[filesystem,redshift]>=1.0" in b.packages

    def test_dlt_no_source_no_sink(self):
        b = resolve_engine_bootstrap("dlt")
        assert b.packages == ["dlt>=1.0"]

    def test_dlt_unknown_source_falls_back(self):
        b = resolve_engine_bootstrap("dlt", source_kind="totally_unknown", sink_platform="snowflake")
        # Should still produce a valid spec with just the sink extra.
        assert any(p.startswith("dlt[snowflake]") for p in b.packages)

    def test_airbyte_single_package(self):
        b = resolve_engine_bootstrap("airbyte")
        assert b.packages == ["airbyte>=0.20,<1"]

    def test_airbyte_ignores_source_sink(self):
        # PyAirbyte source connectors ship as Docker images; no per-source
        # pip extras. The bootstrap should be identical regardless of source.
        b1 = resolve_engine_bootstrap("airbyte", source_kind="postgres", sink_platform="snowflake")
        b2 = resolve_engine_bootstrap("airbyte", source_kind="mysql", sink_platform="bigquery")
        assert b1.packages == b2.packages == ["airbyte>=0.20,<1"]

    def test_meltano_postgres_to_snowflake(self):
        b = resolve_engine_bootstrap("meltano", source_kind="postgres", sink_platform="snowflake")
        assert "meltanolabs-tap-postgres>=0.8" in b.packages
        assert "meltanolabs-target-snowflake>=0.18" in b.packages

    def test_meltano_unknown_source_emits_note(self):
        b = resolve_engine_bootstrap("meltano", source_kind="totally_unknown", sink_platform="snowflake")
        assert any("totally_unknown" in n for n in b.notes)
        # Sink package should still be present.
        assert "meltanolabs-target-snowflake>=0.18" in b.packages

    def test_dbt_snowflake(self):
        b = resolve_engine_bootstrap("dbt", sink_platform="snowflake")
        assert "dbt-core>=1.7" in b.packages
        assert "dbt-snowflake>=1.7" in b.packages

    def test_dbt_bigquery(self):
        b = resolve_engine_bootstrap("dbt", sink_platform="bigquery")
        assert "dbt-bigquery>=1.7" in b.packages

    def test_dbt_databricks(self):
        b = resolve_engine_bootstrap("dbt", sink_platform="databricks")
        assert "dbt-databricks>=1.7" in b.packages

    def test_dbt_unknown_platform_emits_note(self):
        b = resolve_engine_bootstrap("dbt", sink_platform="exotic_warehouse")
        assert b.packages == ["dbt-core>=1.7"]
        assert any("exotic_warehouse" in n for n in b.notes)

    def test_duckdb_standalone(self):
        b = resolve_engine_bootstrap("duckdb")
        assert b.packages == ["duckdb>=1.0"]

    def test_debezium_jvm_no_pip(self):
        b = resolve_engine_bootstrap("debezium")
        assert b.packages == []
        assert any("JVM" in n for n in b.notes)

    def test_kafka_connect_jvm_no_pip(self):
        b = resolve_engine_bootstrap("kafka_connect")
        assert b.packages == []
        assert any("JVM" in n for n in b.notes)

    def test_unknown_engine_emits_note(self):
        b = resolve_engine_bootstrap("totally_unknown_engine")
        assert b.packages == []
        assert any("not in _ENGINE_SPECS registry" in n for n in b.notes)

    def test_none_engine_returns_empty(self):
        b = resolve_engine_bootstrap(None)
        assert b == EngineBootstrap()

    def test_engine_case_insensitive(self):
        # Engines may be authored with different casing in contracts;
        # the resolver should normalise.
        b1 = resolve_engine_bootstrap("DLT", source_kind="postgres", sink_platform="snowflake")
        b2 = resolve_engine_bootstrap("dlt", source_kind="postgres", sink_platform="snowflake")
        assert sorted(b1.packages) == sorted(b2.packages)


# ── render_pip_install_command ──────────────────────────────────────


class TestRenderPipInstallCommand:
    def test_empty_bootstrap_returns_empty_string(self):
        assert render_pip_install_command(EngineBootstrap()) == ""

    def test_single_package(self):
        b = EngineBootstrap(packages=["airbyte>=0.20,<1"])
        assert render_pip_install_command(b) == 'pip install --quiet "airbyte>=0.20,<1"'

    def test_multiple_packages_dedup_and_sort(self):
        # Duplicates collapse, output order is alphabetical.
        b = EngineBootstrap(packages=["dlt>=1.0", "psycopg[binary]>=3.1", "dlt>=1.0"])
        out = render_pip_install_command(b)
        assert out.count("dlt>=1.0") == 1
        assert out.index("dlt") < out.index("psycopg")

    def test_quotes_extras_brackets(self):
        # Shell would interpret [ as glob; the helper must quote each spec.
        b = EngineBootstrap(packages=["dlt[sql_database,snowflake]>=1.0"])
        assert '"dlt[sql_database,snowflake]>=1.0"' in render_pip_install_command(b)

    def test_custom_pip_executable(self):
        b = EngineBootstrap(packages=["airbyte>=0.20,<1"])
        assert render_pip_install_command(b, pip_executable="/opt/venv/bin/pip").startswith(
            "/opt/venv/bin/pip install"
        )


# ── render_bootstrap_shell_section ──────────────────────────────────


class TestRenderBootstrapShellSection:
    def test_dlt_postgres_snowflake_full_section(self):
        s = render_bootstrap_shell_section("dlt", "postgres", "snowflake", indent="  ")
        assert "pydantic-settings" in s
        assert "dlt[snowflake,sql_database]>=1.0" in s
        assert "psycopg[binary]>=3.1" in s
        assert "engine='dlt', source='postgres', sink='snowflake'" in s

    def test_unknown_engine_includes_note_as_comment(self):
        s = render_bootstrap_shell_section("exotic_engine", None, None, indent="  ")
        # Notes render as shell comments (lines starting with ``#``).
        assert "# " in s
        assert "exotic_engine" in s

    def test_no_engine_and_no_pydantic_returns_empty(self):
        s = render_bootstrap_shell_section(None, None, None, include_pydantic_settings=False)
        assert s == ""

    def test_no_engine_with_pydantic_only(self):
        s = render_bootstrap_shell_section(None, None, None, include_pydantic_settings=True)
        assert "pydantic-settings" in s
        assert "Per-engine" not in s

    def test_indent_applied_to_every_line(self):
        s = render_bootstrap_shell_section("dlt", "postgres", "snowflake", indent="    XX ")
        for line in s.split("\n"):
            assert line.startswith("    XX ") or line == ""


# ── render_runner_env_vars ──────────────────────────────────────────


class TestRenderRunnerEnvVars:
    def test_empty_when_no_override(self):
        assert render_runner_env_vars() == {}
        assert render_runner_env_vars("") == {}

    def test_single_override(self):
        assert render_runner_env_vars("host.docker.internal") == {
            "FLUID_RUNNER_HOST_OVERRIDE": "host.docker.internal"
        }

    def test_arbitrary_host_value(self):
        # The helper doesn't validate the host — operators may set it
        # to bridge IPs, K8s service names, etc.
        assert (
            render_runner_env_vars("10.42.0.1").get("FLUID_RUNNER_HOST_OVERRIDE")
            == "10.42.0.1"
        )

    def test_airbyte_engine_injects_project_dir(self):
        # Engine-declared env vars come from the runtime registry.
        # render_runner_env_vars should delegate, not hardcode.
        env = render_runner_env_vars(engine="airbyte")
        assert env.get("AIRBYTE_PROJECT_DIR") == "/tmp/airbyte"

    def test_airbyte_engine_injects_temp_dir_for_dind_volume_share(self):
        # PyAirbyte's connector executor mounts host tempfile.gettempdir()
        # into the spawned container at /airbyte/tmp. With DinD the host
        # daemon needs the same path on both sides, so we override to a
        # path INSIDE AIRBYTE_PROJECT_DIR (which the operator bind-mounts
        # symmetrically). Without this, NoSuchFileException at runtime.
        env = render_runner_env_vars(engine="airbyte")
        assert env.get("AIRBYTE_TEMP_DIR") == "/tmp/airbyte/tmp"

    def test_airbyte_engine_combined_with_host_override(self):
        env = render_runner_env_vars(
            "host.docker.internal", engine="airbyte"
        )
        assert env == {
            "AIRBYTE_PROJECT_DIR": "/tmp/airbyte",
            "AIRBYTE_TEMP_DIR": "/tmp/airbyte/tmp",
            "FLUID_RUNNER_HOST_OVERRIDE": "host.docker.internal",
        }

    def test_dlt_meltano_dbt_no_runtime_env_vars(self):
        # Pure-Python engines have no exec-time env-var requirements
        # beyond what the operator sets.
        for engine in ("dlt", "meltano", "dbt", "duckdb"):
            assert render_runner_env_vars(engine=engine) == {}

    def test_engine_case_insensitive(self):
        # Operators may write 'Airbyte' or 'AIRBYTE' in the contract.
        assert render_runner_env_vars(engine="AIRBYTE") == {
            "AIRBYTE_PROJECT_DIR": "/tmp/airbyte",
            "AIRBYTE_TEMP_DIR": "/tmp/airbyte/tmp",
        }

    def test_operator_override_wins_on_key_clash(self):
        # If a future engine ever sets FLUID_RUNNER_HOST_OVERRIDE in
        # its runtime spec, the operator's CLI flag should still win
        # (operator intent > engine default).
        env = render_runner_env_vars(
            "operator-host", engine="airbyte"
        )
        assert env["FLUID_RUNNER_HOST_OVERRIDE"] == "operator-host"


# ── resolve_engine_runtime (per-engine exec-time requirements) ──────


class TestResolveEngineRuntime:
    def test_airbyte_needs_docker_socket_and_project_dir(self):
        rt = resolve_engine_runtime("airbyte")
        assert rt.needs_docker_socket is True
        # Both env vars present; AIRBYTE_TEMP_DIR is a subdir of the
        # project dir so a single bind-mount covers both.
        assert rt.env_vars == {
            "AIRBYTE_PROJECT_DIR": "/tmp/airbyte",
            "AIRBYTE_TEMP_DIR": "/tmp/airbyte/tmp",
        }
        assert rt.notes  # operator-facing rationale present
        # No external services for PyAirbyte — connectors run in-cluster.
        assert rt.needs_external_services == []

    def test_airbyte_temp_dir_is_subpath_of_project_dir(self):
        # Invariant: a single bind-mount of AIRBYTE_PROJECT_DIR covers
        # the temp dir too. If a future change breaks this (e.g. moves
        # AIRBYTE_TEMP_DIR to /var/cache/...), the lab compose mount
        # has to grow to match — flag it via this test.
        rt = resolve_engine_runtime("airbyte")
        from pathlib import PurePosixPath
        proj = PurePosixPath(rt.env_vars["AIRBYTE_PROJECT_DIR"])
        tmp = PurePosixPath(rt.env_vars["AIRBYTE_TEMP_DIR"])
        assert tmp.is_relative_to(proj), (
            f"AIRBYTE_TEMP_DIR {tmp} must be a subpath of "
            f"AIRBYTE_PROJECT_DIR {proj} so a single bind-mount suffices."
        )

    def test_debezium_needs_kafka_connect_cluster(self):
        rt = resolve_engine_runtime("debezium")
        assert rt.needs_docker_socket is True
        assert "kafka_connect_cluster" in rt.needs_external_services
        assert rt.env_vars == {}  # operator wires KAFKA_CONNECT_URL externally

    def test_kafka_connect_needs_kafka_connect_cluster(self):
        rt = resolve_engine_runtime("kafka_connect")
        assert rt.needs_docker_socket is True
        assert "kafka_connect_cluster" in rt.needs_external_services

    def test_dlt_no_runtime_requirements(self):
        rt = resolve_engine_runtime("dlt")
        assert rt == EngineRuntime()

    def test_meltano_no_runtime_requirements(self):
        rt = resolve_engine_runtime("meltano")
        assert rt == EngineRuntime()

    def test_dbt_no_runtime_requirements(self):
        rt = resolve_engine_runtime("dbt")
        assert rt == EngineRuntime()

    def test_duckdb_no_runtime_requirements(self):
        rt = resolve_engine_runtime("duckdb")
        assert rt == EngineRuntime()

    def test_unknown_engine_returns_empty(self):
        # Bootstrap resolver emits a note for unknowns; runtime stays
        # empty (caller treats as "no extra runtime needs").
        assert resolve_engine_runtime("totally_unknown") == EngineRuntime()

    def test_none_engine_returns_empty(self):
        assert resolve_engine_runtime(None) == EngineRuntime()

    def test_engine_case_insensitive(self):
        rt1 = resolve_engine_runtime("AIRBYTE")
        rt2 = resolve_engine_runtime("airbyte")
        assert rt1 == rt2

    def test_engine_runtime_is_frozen(self):
        # Frozen dataclass — generators may stash these in caches
        # without worrying about mutation.
        rt = resolve_engine_runtime("airbyte")
        with pytest.raises(Exception):  # FrozenInstanceError, but we don't import it
            rt.needs_docker_socket = False  # type: ignore[misc]


# ── render_runtime_notes ────────────────────────────────────────────


class TestRenderRuntimeNotes:
    def test_returns_empty_for_engines_with_no_runtime_needs(self):
        for engine in (None, "", "dlt", "meltano", "dbt", "duckdb"):
            assert render_runtime_notes(engine) == ""

    def test_airbyte_includes_docker_socket_requirement(self):
        s = render_runtime_notes("airbyte")
        assert "docker.sock" in s
        assert "REQUIRES" in s
        # Engine-spec note about PyAirbyte should appear too.
        assert "PyAirbyte" in s

    def test_debezium_includes_external_service(self):
        s = render_runtime_notes("debezium")
        assert "kafka_connect_cluster" in s
        assert "REQUIRES SERVICE" in s

    def test_default_indent_is_jenkins_groovy_comment(self):
        s = render_runtime_notes("airbyte")
        for line in s.split("\n"):
            assert line.startswith("// ")

    def test_yaml_indent_for_gha_gitlab_tekton(self):
        s = render_runtime_notes("airbyte", indent="# ")
        for line in s.split("\n"):
            assert line.startswith("# ")

    def test_external_services_can_be_suppressed(self):
        s = render_runtime_notes(
            "kafka_connect", include_external_services=False
        )
        assert "REQUIRES SERVICE" not in s
        # Notes still present even when service line is suppressed.
        assert "JVM" in s

    def test_unknown_engine_returns_empty(self):
        assert render_runtime_notes("totally_unknown_engine") == ""

    def test_indent_applied_to_every_line(self):
        s = render_runtime_notes("airbyte", indent="    XX | ")
        for line in s.split("\n"):
            assert line.startswith("    XX | ")


# ── Snapshot-style tests: every registered combo → non-empty bootstrap ──


@pytest.mark.parametrize(
    "engine,source,sink",
    [
        ("dlt", "postgres", "snowflake"),
        ("dlt", "mysql", "bigquery"),
        ("dlt", "s3", "snowflake"),
        ("airbyte", "postgres", "snowflake"),
        ("airbyte", "mysql", "bigquery"),
        ("meltano", "postgres", "snowflake"),
        ("meltano", "mysql", "bigquery"),
        ("dbt", None, "snowflake"),
        ("dbt", None, "bigquery"),
        ("dbt", None, "redshift"),
        ("dbt", None, "postgres"),
        ("dbt", None, "databricks"),
        ("dbt", None, "duckdb"),
        ("duckdb", None, None),
    ],
)
def test_common_combos_produce_non_empty_install(engine, source, sink):
    """Every common (engine, source, sink) combo should yield a pip
    install line. Catches regressions where a registry entry gets
    accidentally deleted."""
    b = resolve_engine_bootstrap(engine, source_kind=source, sink_platform=sink)
    assert b.packages, f"{engine}/{source}/{sink} produced no packages"
    pip_line = render_pip_install_command(b)
    assert pip_line.startswith("pip install"), pip_line
