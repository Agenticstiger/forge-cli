# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""How the embedded runner starts Debezium Server: argv, environment and working directory.

The server must never start in the project. Quarkus reads ``.env`` and
``config/application.properties`` from its working directory, and the stock
``run.sh`` sources ``./jmx/*.sh`` and loads jars from the working directory,
so a project could plant files the server would run. These tests replace
``subprocess.run`` with a recorder; the live proof against a stock distribution
is ``tests/integration/test_debezium_server_runner_live.py``.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.api.runner import RunState
from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
from fluid_build.build_runners.debezium import runner as dbz
from fluid_build.build_runners.debezium.runner import DebeziumRunner

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and file modes")

_CONTRACT_ID = "bronze.dbz_launch"


def _contract(server_binary: Optional[str]) -> Dict[str, Any]:
    dbz_props: Dict[str, Any] = {
        "deployment": {"mode": "embedded"},
        "server": {"sink": {"type": "http", "config": {"url": "http://recorder.example/"}}},
    }
    if server_binary is not None:
        dbz_props["server_binary"] = server_binary
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": _CONTRACT_ID,
        "name": "Debezium launch",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "debezium",
                "capabilities": ["cdc", "streaming"],
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "mode": "cdc",
                        "streams": ["public.orders"],
                        "connection": {
                            "host": "db",
                            "port": 5432,
                            "database": "d",
                            "user": "u",
                            "password": "pw",  # pragma: allowlist secret
                        },
                    },
                    "sink": {"format": "iceberg"},
                    "debezium": dbz_props,
                },
                "outputs": ["data"],
            }
        ],
        # The expose the build's ``outputs`` name: an output must be an exposeId
        # (``build_output_exposes``). The hand-written http sink ``config`` keeps
        # Iceberg derivation off, so the binding only has to pass the sink preflight.
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "iceberg",
                    "location": {
                        "database": "bronze",
                        "table": "orders",
                        "uri": "http://rest:8181/catalog",
                        "warehouse": "analytics",
                    },
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def _links(cwd: Optional[str]) -> Dict[str, str]:
    """Every symlink directly under ``cwd`` and under its ``config``, with its target."""
    out: Dict[str, str] = {}
    for base in (cwd, os.path.join(cwd, "config") if cwd else None):
        if base and os.path.isdir(base):
            for name in os.listdir(base):
                path = os.path.join(base, name)
                if os.path.islink(path):
                    out[os.path.relpath(path, cwd)] = os.readlink(path)
    return out


def _entries(cwd: Optional[str], *parts: str) -> Optional[List[str]]:
    path = os.path.join(cwd, *parts) if cwd else None
    return sorted(os.listdir(path)) if path and os.path.isdir(path) else None


def _read(cwd: Optional[str], *parts: str) -> Optional[str]:
    path = os.path.join(cwd, *parts) if cwd else None
    return Path(path).read_text() if path and os.path.isfile(path) else None


class _FakeRun:
    """Stands in for ``subprocess.run``: records the call, then "times out", which
    is how the runner reads a server that booted."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        cwd = kwargs.get("cwd")
        self.calls.append(
            {
                "argv": list(argv),
                "env": kwargs.get("env"),
                "cwd": cwd,
                "cwd_existed": bool(cwd) and os.path.isdir(cwd),
                "cwd_mode": stat.S_IMODE(os.stat(cwd).st_mode) if cwd else None,
                "cwd_entries": sorted(os.listdir(cwd)) if cwd and os.path.isdir(cwd) else None,
                "links": _links(cwd),
                "config_entries": _entries(cwd, "config"),
                "config_text": _read(cwd, "config", "application.properties"),
            }
        )
        raise subprocess.TimeoutExpired(argv, 1)


@pytest.fixture
def fake_run(monkeypatch) -> _FakeRun:
    recorder = _FakeRun()
    monkeypatch.setattr(dbz.subprocess, "run", recorder)
    monkeypatch.delenv("JAVA_OPTS", raising=False)
    monkeypatch.delenv("DEBEZIUM_OPTS", raising=False)
    return recorder


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def _install(tmp_path: Path, *, with_jar: bool, name: str = "dbz-install") -> Path:
    """An install directory outside the project holding a ``run.sh``."""
    root = tmp_path / name
    root.mkdir()
    if with_jar:
        (root / "debezium-server-3.7.1.Final-runner.jar").write_bytes(b"jar")
    script = root / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


def _run(project: Path, server_binary: Optional[str]):
    contract = _contract(server_binary)
    ctx = build_acquisition_run_context(contract["builds"][0], contract, project)
    return DebeziumRunner().run(ctx)


def _plant_binary(directory: Path, name: str = "run.sh") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


# ── a binary inside the project is refused ─────────────────────────────


def _assert_refused(result, fake_run: _FakeRun) -> None:
    assert result.state == RunState.FAILED
    assert "properties.debezium.server_binary" in (result.error or "")
    assert "outside the project" in (result.error or "")
    assert fake_run.calls == []


def test_a_relative_path_into_the_project_is_refused(project, fake_run, monkeypatch):
    _plant_binary(project / "tools")
    monkeypatch.chdir(project)
    _assert_refused(_run(project, "./tools/run.sh"), fake_run)


def test_an_absolute_path_into_the_project_is_refused(project, fake_run):
    script = _plant_binary(project / "tools")
    _assert_refused(_run(project, str(script)), fake_run)


def test_a_symlink_into_the_project_is_refused(project, tmp_path, fake_run):
    script = _plant_binary(project / "tools")
    link_dir = tmp_path / "elsewhere"
    link_dir.mkdir()
    link = link_dir / "debezium-server"
    link.symlink_to(script)
    _assert_refused(_run(project, str(link)), fake_run)


def test_a_path_hit_inside_the_project_is_refused(project, fake_run, monkeypatch):
    _plant_binary(project / "bin", name="debezium-server")
    monkeypatch.setenv("PATH", str(project / "bin"))
    _assert_refused(_run(project, None), fake_run)


def test_a_binary_elsewhere_in_the_projects_checkout_is_refused(tmp_path, fake_run):
    """The contract sits in a subdirectory of the repository that holds the binary."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    contract_dir = repo / "fluid" / "contracts"
    contract_dir.mkdir(parents=True)
    script = _plant_binary(repo / "vendor" / "dbz")
    (script.parent / "debezium-server-3.7.1.Final-runner.jar").write_bytes(b"jar")
    _assert_refused(_run(contract_dir, str(script)), fake_run)


def test_a_binary_outside_the_project_is_accepted(project, tmp_path, fake_run):
    script = _install(tmp_path, with_jar=True)
    assert _run(project, str(script)).state == RunState.SUCCEEDED
    assert len(fake_run.calls) == 1


def test_a_project_named_like_a_prefix_of_the_install_is_not_inside(tmp_path, fake_run):
    """``/x/proj`` does not contain ``/x/proj-tools``: the check is by path part."""
    project = tmp_path / "proj"
    project.mkdir()
    script = _install(tmp_path, with_jar=True, name="proj-tools")
    assert _run(project, str(script)).state == RunState.SUCCEEDED


# ── argv and working directory ─────────────────────────────────────────


def test_argv_is_the_real_binary_and_has_no_config_flag(project, tmp_path, fake_run):
    script = _install(tmp_path, with_jar=True)
    link = tmp_path / "debezium-server"
    link.symlink_to(script)
    _run(project, str(link))
    (call,) = fake_run.calls
    assert call["argv"] == [str(script.resolve())]
    assert "--config" not in call["argv"]


def test_for_the_stock_launcher_the_working_directory_links_the_install_and_holds_forges_file(
    project, tmp_path, fake_run
):
    """Debezium Server 3.7 reads its engine settings from config/application.properties
    under the working directory, and run.sh finds everything else relative to it."""
    script = _install(tmp_path, with_jar=True)
    install = script.resolve().parent
    (install / "config").mkdir()
    (install / "config" / "metrics.yml").write_text("metrics")
    (install / "config" / "application.properties").write_text("debezium.sink.type=operator\n")
    (install / "lib").mkdir()
    _run(project, str(script))
    (call,) = fake_run.calls
    cwd = Path(call["cwd"])
    assert cwd.name.startswith("fluid-debezium-") and call["cwd_mode"] == 0o700
    assert cwd != install and project.resolve() not in (cwd, *cwd.parents)
    assert call["env"]["PWD"] == call["cwd"]
    links = call["links"]
    assert links["run.sh"] == str(install / "run.sh")
    assert links["lib"] == str(install / "lib")
    assert links["debezium-server-3.7.1.Final-runner.jar"] == str(
        install / "debezium-server-3.7.1.Final-runner.jar"
    )
    assert links[os.path.join("config", "metrics.yml")] == str(install / "config" / "metrics.yml")
    # forge's file, a copy, in place of the install's own application.properties
    assert os.path.join("config", "application.properties") not in links
    assert call["config_text"] == _forge_config(project).read_text()
    assert "debezium.sink.type=http" in call["config_text"]
    assert not cwd.exists()


def test_without_the_runner_jar_the_working_directory_is_a_temporary_one_removed_afterwards(
    project, tmp_path, fake_run
):
    script = _install(tmp_path, with_jar=False)
    _run(project, str(script))
    (call,) = fake_run.calls
    cwd = Path(call["cwd"])
    assert cwd.name.startswith("fluid-debezium-")
    assert cwd != script.resolve().parent
    assert project.resolve() not in (cwd, *cwd.parents)
    assert call["cwd_existed"]
    assert call["cwd_mode"] == 0o700
    assert call["cwd_entries"] == ["config"]
    assert call["config_entries"] == ["application.properties"]
    assert call["config_text"] == _forge_config(project).read_text()
    assert call["links"] == {}
    assert not cwd.exists()


def test_the_temporary_directory_is_removed_when_the_run_raises(project, tmp_path, monkeypatch):
    seen: List[str] = []

    def boom(argv, **kwargs):
        seen.append(kwargs["cwd"])
        raise RuntimeError("the launch failed")

    monkeypatch.setattr(dbz.subprocess, "run", boom)
    script = _install(tmp_path, with_jar=False)
    with pytest.raises(RuntimeError):
        dbz._execute_debezium_server(
            _ctx(project),
            _contract(str(script))["builds"][0],
            {"mode": "embedded"},
            {"server": {"sink": {"type": "http", "config": {}}}, "server_binary": str(script)},
            "initial",
            "2026-11-24T00:00:00Z",
            0.0,
        )
    assert seen and not Path(seen[0]).exists()


def _ctx(project: Path):
    return SimpleNamespace(
        workdir=str(project),
        product_id="p",
        build_id="b",
        run_id="r",
        source=SimpleNamespace(
            kind="postgres", connection=SimpleNamespace(raw={"host": "db"}), streams=[]
        ),
        contract={},
        sink=None,
        env={},
    )


# ── environment ────────────────────────────────────────────────────────


def _forge_config(project: Path) -> Path:
    return project / ".fluid" / "debezium" / _CONTRACT_ID / "ingest" / "application.properties"


def _config_uri(call: Dict[str, Any]) -> str:
    """The URI of forge's file as the server gets it: the copy in its working directory."""
    return (Path(call["cwd"]).resolve() / "config" / "application.properties").as_uri()


def test_java_opts_keeps_the_operators_value_and_ends_with_the_system_property(
    project, tmp_path, fake_run, monkeypatch
):
    monkeypatch.setenv(
        "JAVA_OPTS", "-Xmx256m -Dsmallrye.config.locations=file:///hostile.properties"
    )
    monkeypatch.setenv("DEBEZIUM_OPTS", "-Dquarkus.log.level=DEBUG")
    script = _install(tmp_path, with_jar=True)
    _run(project, str(script))
    (call,) = fake_run.calls
    uri = _config_uri(call)
    java_opts = call["env"]["JAVA_OPTS"]
    assert java_opts.startswith("-Xmx256m -Dsmallrye.config.locations=file:///hostile.properties ")
    assert java_opts.endswith(f"-Dquarkus.config.locations={uri}")
    assert f"-Dsmallrye.config.locations={uri}" in java_opts
    # the launcher's jmx/enable_jmx.sh can replace JAVA_OPTS: the property is in
    # DEBEZIUM_OPTS too, after the operator's value
    debezium_opts = call["env"]["DEBEZIUM_OPTS"]
    assert debezium_opts.startswith("-Dquarkus.log.level=DEBUG ")
    assert debezium_opts.endswith(f"-Dquarkus.config.locations={uri}")


def test_the_server_gets_the_inherited_environment_and_the_credentials(
    project, tmp_path, fake_run, monkeypatch
):
    monkeypatch.setenv("FLUID_TEST_INHERITED", "yes")
    script = _install(tmp_path, with_jar=True)
    _run(project, str(script))
    (call,) = fake_run.calls
    assert call["env"]["FLUID_TEST_INHERITED"] == "yes"
    assert call["env"]["FLUID_DBZ_SOURCE_DATABASE_PASSWORD"] == "pw"  # pragma: allowlist secret


def test_a_config_path_with_a_space_and_a_comma_is_encoded(
    project, tmp_path, fake_run, monkeypatch
):
    import tempfile

    weird = tmp_path / "tmp dir, v2"
    weird.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(weird))
    script = _install(tmp_path, with_jar=True)
    _run(project, str(script))
    (call,) = fake_run.calls
    tokens = call["env"]["JAVA_OPTS"].split()
    # run.sh word-splits JAVA_OPTS and the locations are comma-separated: neither may remain
    assert len(tokens) == 2
    for token in tokens:
        assert token.startswith("-D") and "," not in token.split("=", 1)[1]
    assert "tmp%20dir%2C%20v2" in tokens[0]
    assert tokens[0].split("=", 1)[1] == _config_uri(call)


def test_a_uri_that_stays_unsafe_is_refused(tmp_path):
    class _Odd:
        def resolve(self):
            return self

        def as_uri(self):
            return "file:///a b,c"

    with pytest.raises(ValueError, match="whitespace or a comma"):
        dbz._server_environment(_Odd(), {}, tmp_path)  # type: ignore[arg-type]


# ── state files ────────────────────────────────────────────────────────


def _properties(project: Path) -> Dict[str, str]:
    path = project / ".fluid" / "debezium" / _CONTRACT_ID / "ingest" / "application.properties"
    out = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_the_offset_and_history_files_are_absolute_and_under_forges_directory(
    project, tmp_path, fake_run
):
    script = _install(tmp_path, with_jar=True)
    _run(project, str(script))
    props = _properties(project)
    state = (project / ".fluid" / "debezium" / _CONTRACT_ID / "ingest").resolve()
    assert props["debezium.source.offset.storage.file.filename"] == str(state / "offsets.dat")
    assert props["debezium.source.schema.history.internal.file.filename"] == str(
        state / "schema_history.dat"
    )


# ── migration of the old offsets file ──────────────────────────────────


def _offsets_path(project: Path) -> Path:
    return project / ".fluid" / "debezium" / _CONTRACT_ID / "ingest" / "offsets.dat"


def test_an_old_offsets_file_is_copied_once_and_logged(project, tmp_path, fake_run, caplog):
    old = project / "data" / "offsets.dat"
    old.parent.mkdir()
    old.write_bytes(b"offsets-v1")
    script = _install(tmp_path, with_jar=True)
    with caplog.at_level(logging.INFO, logger="fluid.acquire.debezium"):
        _run(project, str(script))
    new = _offsets_path(project)
    assert new.read_bytes() == b"offsets-v1"
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    assert old.read_bytes() == b"offsets-v1"  # left in place
    messages = [r.getMessage() for r in caplog.records if "offsets.migrated" in r.getMessage()]
    assert len(messages) == 1
    assert str(old) in messages[0] and str(new.resolve()) in messages[0]

    # a second run does not copy again, even if the old file changed
    old.write_bytes(b"offsets-v2")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="fluid.acquire.debezium"):
        _run(project, str(script))
    assert new.read_bytes() == b"offsets-v1"
    assert not [r for r in caplog.records if "offsets.migrated" in r.getMessage()]


def test_an_existing_new_offsets_file_is_never_overwritten(project, tmp_path, fake_run):
    old = project / "data" / "offsets.dat"
    old.parent.mkdir()
    old.write_bytes(b"old")
    new = _offsets_path(project)
    new.parent.mkdir(parents=True)
    new.write_bytes(b"current")
    _run(project, str(_install(tmp_path, with_jar=True)))
    assert new.read_bytes() == b"current"


def test_a_symlinked_old_offsets_file_is_ignored(project, tmp_path, fake_run):
    target = tmp_path / "somewhere-else.dat"
    target.write_bytes(b"not-ours")
    (project / "data").mkdir()
    (project / "data" / "offsets.dat").symlink_to(target)
    _run(project, str(_install(tmp_path, with_jar=True)))
    assert not _offsets_path(project).exists()


def test_a_symlinked_old_data_directory_is_ignored(project, tmp_path, fake_run):
    elsewhere = tmp_path / "elsewhere-data"
    elsewhere.mkdir()
    (elsewhere / "offsets.dat").write_bytes(b"not-ours")
    (project / "data").symlink_to(elsewhere, target_is_directory=True)
    _run(project, str(_install(tmp_path, with_jar=True)))
    assert not _offsets_path(project).exists()


def test_a_project_without_an_old_offsets_file_gets_none(project, tmp_path, fake_run):
    _run(project, str(_install(tmp_path, with_jar=True)))
    assert not _offsets_path(project).exists()


# ── a server that exits says why ───────────────────────────────────────


def test_a_failed_server_reports_its_exit_code_and_output_with_secrets_masked(
    project, tmp_path, monkeypatch
):
    """Quarkus logs to standard output: the failure names the code and both tails."""

    def exits(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            3,
            stdout="booting\nERROR io.debezium: cannot connect with password pw\n",
            stderr="",
        )

    monkeypatch.setattr(dbz.subprocess, "run", exits)
    result = _run(project, str(_install(tmp_path, with_jar=True)))

    assert result.state == RunState.FAILED
    assert "debezium-server exited with code 3" in result.error
    assert "cannot connect" in result.error
    assert " pw" not in result.error  # the source password handed to the server


def test_a_failed_server_that_printed_nothing_says_so(project, tmp_path, monkeypatch):
    monkeypatch.setattr(
        dbz.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")
    )
    result = _run(project, str(_install(tmp_path, with_jar=True)))
    assert result.error == "debezium-server exited with code 1 and printed nothing"


def test_a_failure_names_the_error_lines_above_a_long_stack_trace(project, tmp_path, monkeypatch):
    """The exception message comes before its frames, far above the end of the output."""
    frames = "".join(f"\tat io.example.Frame{i}.call(Frame.java:{i})\n" for i in range(100))
    stdout = (
        "2026-11-24 10:04:23,267 ERROR [io.quarkus.runtime.Application] (main) Failed to start\n"
        "Caused by: java.util.NoSuchElementException: SRCFG00014: The config property "
        "debezium.format.value is required but it could not be found in any config source\n"
        + frames
    )
    monkeypatch.setattr(
        dbz.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout, "")
    )
    result = _run(project, str(_install(tmp_path, with_jar=True)))

    error = result.error or ""
    assert error.index("--- errors ---") < error.index("--- stdout (last lines) ---")
    assert "ERROR [io.quarkus.runtime.Application] (main) Failed to start" in error
    assert "SRCFG00014: The config property debezium.format.value is required" in error


def test_a_failure_masks_the_password_in_its_url_encoded_spellings():
    """A URL the server prints holds the password escaped: ``quote`` in a path or a userinfo,
    ``quote_plus`` or Java's ``URLEncoder`` in a query."""
    from urllib.parse import quote, quote_plus

    held = "".join(("Zk4q@", "R9 tW/", "vL2x~*", "Pn8m"))
    spellings = [
        quote(held),
        quote(held, safe=""),
        quote_plus(held),
        quote_plus(held, safe="*").replace("~", "%7E"),
    ]
    stdout = "".join(
        f"ERROR io.debezium: POST http://sink.example/{s}/x refused\n" for s in spellings
    )
    proc = subprocess.CompletedProcess(["run.sh"], 1, stdout, "")

    error = dbz._server_failure(proc, {"FLUID_DBZ_SOURCE_PASSWORD": held})

    assert "debezium-server exited with code 1" in error
    assert error.count("POST http://sink.example/") == 2 * len(spellings)  # errors and tail
    assert not any(spelling in error for spelling in spellings)


# ── the event format ───────────────────────────────────────────────────


def test_the_generated_file_names_the_json_event_format():
    """The http sink reads debezium.format.value with no default: forge names it."""
    from fluid_build.build_runners.debezium.runner import render_application_properties

    text, _env = render_application_properties(
        {"connector.class": "io.debezium.connector.postgresql.PostgresConnector"},
        "http",
        {"url": "http://recorder.example/"},
    )
    lines = text.splitlines()
    assert "debezium.format.key=json" in lines
    assert "debezium.format.value=json" in lines
