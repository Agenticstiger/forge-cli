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

"""Credentials stay out of generated files, run records, log context and
mission diagnostics.

One test group per CodeQL alert the 2026-10 triage confirmed on main:

* #241 — ``fluid mission`` quoted a contract's inline credential in a failing
  predicate's diagnostic (terminal, ``--json``, scorecard, LLM prompt).
* #231 — the shared redactor missed ``aws.secret.access.key=`` and the other
  dotted Java/Hadoop spellings, so a Kafka Connect trace kept an S3 key in the
  run record.
* #292 — the local provider's log context carried a raw ``postgres://`` URL
  with its password.
* #211 — Debezium Server's ``application.properties`` held the resolved
  source password, 0o644 before a chmod, under the contract directory.
* #53 / #54 — the Singer ``tap_config.json`` held the resolved password,
  0o644, never deleted, under the contract directory.

Plus the rename that stops CodeQL reading the redactor's own output as a
secret (``mask_known_secrets`` → ``redact_known_values``).
"""

from __future__ import annotations

import ast
import json
import logging
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.observability import secret_redactor as sr

_SECRET = "Hunter2-Sup3rS3cret-0xBEEF"  # pragma: allowlist secret
_S3_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # pragma: allowlist secret
_NEWLINE_VALUE = "a\nb"  # pragma: allowlist secret
_SECRET_REF = "env://PG_PASSWORD_T211"  # pragma: allowlist secret


# ── rename: the redactor's output is not a CodeQL "secret" source ─────────


def test_old_name_still_imports_for_library_callers():
    assert sr.mask_known_secrets is sr.redact_known_values


def test_no_module_calls_the_old_name():
    """CodeQL treats a call to a function named like ``*secret*`` as a source
    of secrets. A call to ``mask_known_secrets`` made the redactor's OUTPUT a
    "secret", and 14 alerts followed it into log lines and files that only
    ever held redacted text. Call ``redact_known_values``."""
    root = Path(sr.__file__).resolve().parents[1]
    callers = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "mask_known_secrets":
                    callers.append(f"{path.relative_to(root)}:{node.lineno}")
    assert callers == []


# ── #231: dotted Java/Hadoop secret key spellings ─────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "aws.secret.access.key",
        "secret.access.key",
        "fs.s3a.secret.key",
        "secret_key",
        "s3.secret-access-key",
    ],
)
def test_dotted_secret_key_assignment_is_masked(key):
    out = sr.redact_secret_text(f"config {{{key}={_S3_KEY}, topic=orders}}")
    assert _S3_KEY not in out
    assert key in out
    assert "topic=orders" in out


def test_prose_about_a_secret_key_is_left_alone():
    text = "rotate the secret key monthly"
    assert sr.redact_secret_text(text) == text


def test_dotted_key_before_a_log_placeholder_is_masked():
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "aws.secret.access.key=%s", (_S3_KEY,), None
    )
    sr.SecretRedactingFilter().filter(record)
    assert _S3_KEY not in record.getMessage()


def test_run_record_keeps_no_s3_key_from_a_connect_trace(tmp_path):
    from fluid_build.build_runners._state import FileStateStore

    record = {
        "run_id": "run_1",
        "state": "failed",
        "facets": {
            "failed_task_traces": [
                "org.apache.kafka.connect.errors.ConnectException: "
                f"{{aws.secret.access.key={_S3_KEY}, s3.bucket.name=lake}}"
            ]
        },
    }
    FileStateStore(tmp_path).write_run_record("prod", "build", record)
    written = (tmp_path / "runs/prod/build/runs/run_1.json").read_text()
    assert _S3_KEY not in written
    assert "s3.bucket.name=lake" in written


# ── #241: mission diagnostics never quote a credential leaf ───────────────


def _mission(tmp_path, criteria):
    from fluid_build.copilot.missions.checks import run_mission_checks
    from fluid_build.copilot.missions.spec import CriterionSpec, MissionSpec

    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(
        "fluidVersion: '0.7.1'\n"
        "kind: DataProduct\n"
        "id: demo.p\n"
        "name: p\n"
        "exposes:\n"
        "  - exposeId: e1\n"
        "    binding:\n"
        "      platform: postgres\n"
        "      location:\n"
        "        database: sales\n"
        f"        password: '{_SECRET}'\n"
        f"        api_token: '{_SECRET}'\n"
        f"        settings: {{user: app, password: '{_SECRET}'}}\n",
        encoding="utf-8",
    )
    spec = MissionSpec(
        name="no-inline-creds",
        description="d",
        goal="use env refs",
        success_criteria=tuple(
            CriterionSpec(check="predicate", path=p, op=op, value=v, value_provided=True)
            for p, op, v in criteria
        ),
    )
    return spec, run_mission_checks(spec, contract)


def test_failing_predicate_on_a_credential_does_not_quote_it(tmp_path, capsys):
    from fluid_build.cli.mission import _render_scorecard
    from fluid_build.copilot.missions.planner import build_plan_prompt

    spec, scorecard = _mission(
        tmp_path,
        [
            ("exposes[*].binding.location.password", "eq", "${PG_PASSWORD}"),
            ("exposes[*].binding.location.api_token", "contains", "{{"),
        ],
    )
    _render_scorecard(spec, scorecard)
    printed = capsys.readouterr().out

    assert "***REDACTED***" in printed
    assert _SECRET not in printed
    assert _SECRET not in json.dumps(scorecard.to_dict())
    assert _SECRET not in build_plan_prompt(spec, scorecard)


def test_a_mapping_leaf_keeps_its_ordinary_keys_readable(tmp_path):
    _spec, scorecard = _mission(
        tmp_path, [("exposes[*].binding.location.settings", "eq", {"user": "app"})]
    )
    diagnostics = " ".join(line for r in scorecard.results for line in r.diagnostics)
    assert _SECRET not in diagnostics
    assert "'user': 'app'" in diagnostics


def test_an_ordinary_leaf_is_still_quoted(tmp_path):
    _spec, scorecard = _mission(
        tmp_path, [("exposes[*].binding.location.database", "eq", "orders")]
    )
    diagnostics = " ".join(line for r in scorecard.results for line in r.diagnostics)
    assert "got 'sales'" in diagnostics


# ── #292: the local provider's log context ───────────────────────────────


def test_local_sql_error_context_masks_a_postgres_url_password(tmp_path):
    from fluid_build.providers.local.local import LocalProvider

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("test.local.ctx")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.addHandler(_Capture())
    url = f"postgres://etl:{_SECRET}@nohost/x.csv"
    LocalProvider(logger=log, anchor_dir=str(tmp_path))._log_error(
        "local_sql_error", {"error": f'Cannot access file "{url}"', "i": 0}
    )
    assert records, "nothing was logged"
    assert _SECRET not in repr(records[-1].__dict__)
    assert "nohost/x.csv" in repr(records[-1].__dict__)


def test_local_redact_sql_masks_a_postgres_url_password():
    from fluid_build.providers.local.util.logging import redact_sql

    out = redact_sql(f"SELECT * FROM read_csv('postgres://etl:{_SECRET}@nohost/x.csv')")
    assert _SECRET not in out


# ── #211: Debezium Server gets its credentials from the environment ──────


def test_credential_properties_render_as_env_expressions():
    from fluid_build.build_runners.debezium.runner import render_application_properties

    text, env = render_application_properties(
        {"database.user": "app", "database.password": _SECRET, "slot.name": "s"},
        "iceberg",
        {
            "s3.secret-access-key": _S3_KEY,
            "credential": "env:LAKEKEEPER_CREDENTIAL",
            "rest.auth.type": "oauth2",
            "jdbc.password": "",
        },
    )
    assert _SECRET not in text and _S3_KEY not in text
    assert "debezium.source.database.password=${FLUID_DBZ_SOURCE_DATABASE_PASSWORD}" in text
    assert (
        "debezium.sink.iceberg.s3.secret-access-key="
        "${FLUID_DBZ_SINK_ICEBERG_S3_SECRET_ACCESS_KEY}" in text
    )
    # Iceberg's own env reference, a non-secret auth key and an empty value
    # are written as they are.
    assert "debezium.sink.iceberg.credential=env:LAKEKEEPER_CREDENTIAL" in text
    assert "debezium.sink.iceberg.rest.auth.type=oauth2" in text
    assert "debezium.sink.iceberg.jdbc.password=\n" in text + "\n"
    assert "debezium.source.database.user=app" in text
    assert env == {
        "FLUID_DBZ_SOURCE_DATABASE_PASSWORD": _SECRET,
        "FLUID_DBZ_SINK_ICEBERG_S3_SECRET_ACCESS_KEY": _S3_KEY,
    }


def test_a_control_character_in_a_credential_still_fails_closed():
    from fluid_build.build_runners.debezium.runner import render_application_properties

    with pytest.raises(ValueError, match="control character"):
        render_application_properties({"database.password": _NEWLINE_VALUE}, "kafka", {})


def test_two_properties_on_one_env_name_fail_closed():
    from fluid_build.build_runners.debezium.runner import render_application_properties

    with pytest.raises(ValueError, match="FLUID_DBZ_SINK_KAFKA_A_PASSWORD"):
        render_application_properties({}, "kafka", {"a.password": "x", "a-password": "y"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a shell script")
def test_embedded_server_file_holds_no_password_and_server_gets_it(tmp_path, monkeypatch):
    from fluid_build.build_runners.debezium import runner as dbz

    monkeypatch.setenv("PG_PASSWORD_T211", _SECRET)
    captured = tmp_path / "server-env.txt"
    server = tmp_path / "debezium-server"
    server.write_text(
        f"#!/bin/sh\nprintf '%s' \"$FLUID_DBZ_SOURCE_DATABASE_PASSWORD\" > '{captured}'\nexit 3\n"
    )
    server.chmod(0o700)
    ctx = SimpleNamespace(
        workdir=str(tmp_path),
        product_id="sales.orders",
        build_id="b1",
        run_id="r1",
        source=SimpleNamespace(
            kind="postgres",
            connection=SimpleNamespace(raw={"host": "db", "user": "app", "secretRef": _SECRET_REF}),
            streams=["public.orders"],
        ),
        contract={},
        sink=None,
        env={},
    )
    dbz._execute_debezium_server(
        ctx,
        {"engine": "debezium", "properties": {}},
        {"mode": "embedded"},
        {"server": {"sink": {"type": "kafka"}}, "server_binary": str(server)},
        "initial",
        "2026-10-13T00:00:00Z",
        0.0,
    )
    config = tmp_path / ".fluid/debezium/sales.orders/b1/application.properties"
    assert _SECRET not in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert captured.read_text() == _SECRET


def test_an_unwritable_config_is_a_failed_run_not_a_crash(tmp_path, monkeypatch):
    from fluid_build.build_runners.debezium import runner as dbz
    from fluid_build.credentials import encrypted_store

    def refuse(*_a, **_k):
        raise FileExistsError(17, "File exists")

    monkeypatch.setattr(encrypted_store, "_atomic_write_bytes", refuse)
    ctx = SimpleNamespace(
        workdir=str(tmp_path),
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
    result = dbz._execute_debezium_server(
        ctx,
        {"engine": "debezium", "properties": {}},
        {"mode": "embedded"},
        {"server": {"sink": {"type": "kafka"}}},
        "initial",
        "2026-10-13T00:00:00Z",
        0.0,
    )
    assert result.state.value == "failed"
    assert "could not write" in (result.error or "")


# ── #53 / #54: the Singer tap config is private and short-lived ──────────


def _fake_tap(tmp_path: Path, exit_code: int, sleep: float = 0.0) -> Path:
    """A tap that records its ``--config`` file's mode and content."""
    tap = tmp_path / "tap-fake"
    seen = tmp_path / "tap-saw.json"
    tap.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys, time\n"
        "path = sys.argv[sys.argv.index('--config') + 1]\n"
        "with open(path) as fh: body = fh.read()\n"
        "mode = stat.S_IMODE(os.stat(path).st_mode)\n"
        "parent = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)\n"
        f"json.dump({{'path': path, 'mode': mode, 'parent': parent, 'body': body}},"
        f" open({str(seen)!r}, 'w'))\n"
        f"time.sleep({sleep})\n"
        f"sys.exit({exit_code})\n"
    )
    tap.chmod(0o700)
    return tap


def _tap_config():
    return {"host": "db", "user": "alice", "password": _SECRET}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a script tap")
def test_invoke_tap_config_is_private_and_removed(tmp_path):
    from fluid_build.build_runners.meltano.runner import invoke_tap

    workdir = tmp_path / ".fluid/meltano/p/b"
    workdir.mkdir(parents=True)
    stale = workdir / "tap_config.json"
    stale.write_text(json.dumps(_tap_config()))  # an earlier release's copy

    invoke_tap(str(_fake_tap(tmp_path, 0)), config=_tap_config(), workdir=workdir)

    seen = json.loads((tmp_path / "tap-saw.json").read_text())
    assert json.loads(seen["body"])["password"] == _SECRET
    assert seen["mode"] == 0o600 and seen["parent"] == 0o700
    assert not Path(seen["path"]).exists()
    assert not Path(seen["path"]).is_relative_to(tmp_path)
    assert not stale.exists()
    assert all(_SECRET not in p.read_text() for p in workdir.rglob("*") if p.is_file())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a script tap")
def test_discover_removes_the_config_after_a_failed_tap(tmp_path):
    from fluid_build.build_runners.meltano.runner import discover_tap_catalog

    workdir = tmp_path / "w"
    assert (
        discover_tap_catalog(str(_fake_tap(tmp_path, 1)), config=_tap_config(), workdir=workdir)
        is None
    )
    seen = json.loads((tmp_path / "tap-saw.json").read_text())
    assert not Path(seen["path"]).exists()
    assert all(_SECRET not in p.read_text() for p in workdir.rglob("*") if p.is_file())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a script tap")
def test_discover_removes_the_config_after_a_timeout(tmp_path):
    from fluid_build.build_runners.meltano.runner import discover_tap_catalog

    tap = _fake_tap(tmp_path, 0, sleep=30)
    assert (
        discover_tap_catalog(
            str(tap), config=_tap_config(), workdir=tmp_path / "w", timeout_seconds=2
        )
        is None
    )
    seen = json.loads((tmp_path / "tap-saw.json").read_text())
    assert not Path(seen["path"]).exists()


# ── fluid init: generated connector config is git-ignored ────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_init_gitignore_block_ignores_generated_connector_config(tmp_path):
    from fluid_build.cli.init import FLUID_GITIGNORE_BLOCK

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(FLUID_GITIGNORE_BLOCK)
    for rel in (
        ".fluid/debezium/sales.orders/b1/application.properties",
        ".fluid/meltano/sales.orders/b1/tap_state.json",
    ):
        result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", rel])
        assert result.returncode == 0, rel
