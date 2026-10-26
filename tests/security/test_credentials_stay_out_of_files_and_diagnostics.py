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
_ENV_PREFIXED = "env:" + _SECRET
_UC_TOKEN_REF = "env:UC_TOKEN"  # pragma: allowlist secret


# ── rename: the redactor's output is not a CodeQL "secret" source ─────────


def test_old_name_still_imports_for_library_callers():
    assert sr.mask_known_secrets is sr.redact_known_values


def test_no_module_calls_the_old_name():
    """CodeQL treats a call to a function named like ``*secret*`` as a source
    of secrets. A call to ``mask_known_secrets`` made the redactor's OUTPUT a
    "secret", so each log line or file that carried redacted text was reported
    as a clear-text leak (14 alerts on main had that source). Call
    ``redact_known_values``."""
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
        f"        settings: {{user: app, auth: '{_SECRET}'}}\n",
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


def _diagnostic(path: str, leaf, op="eq", value="x") -> str:
    from fluid_build.copilot.missions.checks import _check_predicate, _redact_result
    from fluid_build.copilot.missions.spec import CriterionSpec

    contract: dict = {}
    node = contract
    keys = path.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = leaf
    result = _redact_result(
        _check_predicate(
            CriterionSpec(check="predicate", path=path, op=op, value=value, value_provided=True),
            contract,
            contract_path=Path("c.fluid.yaml"),
        )
    )
    return " ".join(result.diagnostics)


@pytest.mark.parametrize(
    "path",
    [
        "a.password",
        "a.passwords",
        "a.secrets",
        "a.secrets.db",
        "a.tokens",
        "a.auth",
        "a.auth.basic",
        "a.bearer",
        "a.jwt",
        "a.secretValue",
        "a.private_key_pem",
        "a.credentials_json",
        "a.credentials.user",
        "a.connection_string",
        "a.connectionString",
        "a.conn_str",
        "a.dsn",
        "a.PGPASSWORD",
        "a.DBPassword",
        "a.AWSSecretAccessKey",
        "a.MYSQL_PWD",
        "a.passwd",
        "a.password1",
        "a.DB Password",
        "a.privateKey",
        "a.private-key",
        "a.apiKey",
        "a.clientSecret",
        "a.sessionToken",
        "a.connection_url",
        "a.credentials.url",
        "a.accountKey",
        "a.sharedAccessKey",
        "a.signingKey",
        "a.sas",
    ],
)
def test_a_credential_key_is_masked(path):
    """The key rule fails closed: anything the shared ``is_sensitive_key_name``
    calls a credential (plurals, containers, ``*Value``, no separator) hides
    the value, and so does a key under it."""
    assert _SECRET not in _diagnostic(path, _SECRET)


@pytest.mark.parametrize(
    "path, leaf, op, value, shown",
    [
        ("policy.agentPolicy.maxTokensPerDay", 500000, "lte", 100000, "got 500000"),
        ("policy.authz.readers", ["group:everyone"], "contains", "group:x", "'group:everyone'"),
        ("binding.secretRef.provider", "vault", "eq", "env", "got 'vault'"),
        ("sink.catalogAuth.tokenEnv", "UC_TOKEN", "eq", "X", "got 'UC_TOKEN'"),
        ("sink.catalogAuth.type", "oauth2", "eq", "bearer", "got 'oauth2'"),
        ("sink.catalogAuth.scope", "lakekeeper", "eq", "x", "got 'lakekeeper'"),
        ("binding.passwordFile", "/run/pw", "eq", "x", "got '/run/pw'"),
        ("metadata.author", "alice", "eq", "bob", "got 'alice'"),
    ],
)
def test_a_key_that_refers_to_or_only_mentions_a_credential_is_shown(path, leaf, op, value, shown):
    """A reference to a credential (``secretRef``, ``tokenEnv``, anything under
    ``catalogAuth`` that names one), a token budget and ``authz``/``author`` are
    not credentials: the diagnostic keeps the value the planner needs."""
    assert shown in _diagnostic(path, leaf, op, value)


@pytest.mark.parametrize(
    "op, value", [("lt", "aaa"), ("lte", "aaa"), ("gt", "zzz"), ("gte", "zzz"), ("contains", "x")]
)
def test_every_comparison_masks_a_credential_leaf(op, value):
    assert _SECRET not in _diagnostic("a.password", _SECRET, op, value)


@pytest.mark.parametrize("nested", ["pwd", "dsn", "connectionString", "accountKey", "auth"])
def test_a_credential_key_inside_a_mapping_leaf_is_masked(nested):
    diagnostic = _diagnostic("a.settings", {"host": "h", nested: _SECRET}, "eq", {"host": "x"})
    assert _SECRET not in diagnostic
    assert "'host': 'h'" in diagnostic


def test_a_self_referential_leaf_does_not_flip_the_predicate(tmp_path):
    from fluid_build.copilot.missions.checks import _check_predicate
    from fluid_build.copilot.missions.spec import CriterionSpec

    cyclic: dict = {"x": 1}
    cyclic["self"] = cyclic  # a YAML alias that refers to itself
    result = _check_predicate(
        CriterionSpec(check="predicate", path="a.s", op="ne", value={"x": 2}, value_provided=True),
        {"a": {"s": cyclic}},
        contract_path=Path("c.fluid.yaml"),
    )
    assert result.passed


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


@pytest.mark.parametrize(
    "error",
    [
        "HTTP 401 https://api/x?token={s}&api_key={s}",
        "auth failed: client_secret={s}",
        "Authorization: Bearer {s}",
        "aws_secret_access_key={s}",
    ],
)
def test_a_failed_sql_action_logs_its_error_redacted(tmp_path, error):
    """``_run_sql_on`` logs ``local_sql_error`` with the exception's text; that
    text gets the full shared redactor, not only the provider's patterns."""
    from unittest import mock

    from fluid_build.providers.local.local import LocalProvider

    provider = LocalProvider(
        logger=logging.getLogger("test.local.sqlerr"), anchor_dir=str(tmp_path)
    )
    with mock.patch.object(provider, "_log_error") as log_error, pytest.raises(RuntimeError):
        provider._run_sql_on(
            _FailingConnection(error.format(s=_SECRET)), 0, {}, "SELECT 1", [], [], 0.0
        )
    assert log_error.call_args_list, "nothing was logged"
    logged = repr([c.args for c in log_error.call_args_list])
    assert _SECRET not in logged


class _FailingConnection:
    def __init__(self, message: str) -> None:
        self.message = message

    def sql(self, _sql):
        raise RuntimeError(self.message)

    def extract_statements(self, _sql):
        # On DuckDB 2.x query_relation parses the SQL first (_duckdb_compat).
        raise RuntimeError(self.message)


def test_no_local_provider_log_call_carries_a_raw_exception_text():
    """Every ``"error": str(e)`` the local provider logs goes through the shared
    redactor (``local.py`` logs a plan, load, SQL, create-table and copy error)."""
    import inspect

    from fluid_build.providers.local.local import LocalProvider

    source = Path(inspect.getsourcefile(LocalProvider) or "").read_text(encoding="utf-8")
    assert '"error": str(e)' not in source
    assert source.count("redact_secret_text(str(e))") >= 6


def test_local_redact_sql_masks_a_postgres_url_password():
    from fluid_build.providers.local.util.logging import redact_sql

    out = redact_sql(f"SELECT * FROM read_csv('postgres://etl:{_SECRET}@nohost/x.csv')")
    assert _SECRET not in out


def test_local_redaction_masks_a_held_secret_whole_before_its_patterns(monkeypatch):
    """A held secret with ``;``/``@`` is masked whole: the local
    ``password=[^\\s;&]+`` pattern must not cut it short first."""
    from fluid_build.providers.local.util.logging import redact_string

    held = "Kq7;zR9@xWv2-held"  # pragma: allowlist secret
    monkeypatch.setattr(sr, "_known_secrets", (held,))
    out = redact_string(f"could not connect: password={held} host=db")
    assert "zR9" not in out and "xWv2" not in out


def test_local_redaction_keeps_dotted_table_names():
    from fluid_build.providers.local.util.logging import redact_dict, redact_sql

    table = "analytics_prod.customer_orders.order_items_daily"
    assert redact_dict({"table": table})["table"] == table
    assert table in redact_sql(f"SELECT * FROM {table} WHERE x=1")


def test_snowflake_twin_masks_the_dotted_spellings_too():
    from fluid_build.providers.snowflake.util.logging import redact_string

    for key in ("aws.secret.access.key", "fs.s3a.secret.key", "secret_key"):
        assert _S3_KEY not in redact_string(f"{key}={_S3_KEY}")


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

    with pytest.raises(ValueError, match="control character") as excinfo:
        render_application_properties({"database.password": _NEWLINE_VALUE}, "kafka", {})
    assert "TAIL" not in str(excinfo.value) and "a\\nb" not in str(excinfo.value)


def test_env_prefix_is_iceberg_syntax_for_the_sink_only():
    from fluid_build.build_runners.debezium.runner import render_application_properties

    text, env = render_application_properties(
        {"database.password": _ENV_PREFIXED}, "iceberg", {"token": _UC_TOKEN_REF}
    )
    assert _SECRET not in text
    assert env == {"FLUID_DBZ_SOURCE_DATABASE_PASSWORD": _ENV_PREFIXED}
    assert "debezium.sink.iceberg.token=env:UC_TOKEN" in text


@pytest.mark.parametrize(
    "key",
    [
        "database.password",
        "mongodb.password",
        "mongodb.connection.string",
        "client.secret",
        "s3.session-token",
        "oauth2.credential",
        "sasl.jaas.config",
        "connectionstring",
        "header.Authorization",
        "client.authParams",
        "adls.auth.shared-key.account.key",
        "s3.sse.key",
        "producer.ssl.keystore.key",
    ],
)
def test_more_credential_property_names_go_to_the_environment(key):
    from fluid_build.build_runners.debezium.runner import render_application_properties

    text, env = render_application_properties({}, "eventhubs", {key: _SECRET})
    assert _SECRET not in text
    assert list(env.values()) == [_SECRET]


def test_every_catalog_auth_secret_name_is_a_credential_property():
    """The renderer's rule and catalogAuth's own list of secret-valued
    properties cannot drift apart."""
    from fluid_build.build_runners.debezium.runner import _CREDENTIAL_PROPERTY_RE
    from fluid_build.providers._iceberg_catalog_auth import (
        AUTHORIZATION_HEADER_PROPERTY,
        INLINE_CHECKED_KEYS,
    )

    for key in (*INLINE_CHECKED_KEYS, AUTHORIZATION_HEADER_PROPERTY):
        assert _CREDENTIAL_PROPERTY_RE.search(key), key


def test_two_properties_on_one_env_name_fail_closed():
    from fluid_build.build_runners.debezium.runner import render_application_properties

    with pytest.raises(ValueError, match="FLUID_DBZ_SINK_KAFKA_A_PASSWORD"):
        render_application_properties({}, "kafka", {"a.password": "x", "a-password": "y"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a shell script")
def test_embedded_server_file_holds_no_password_and_server_gets_it(tmp_path, monkeypatch):
    from fluid_build.build_runners.debezium import runner as dbz

    monkeypatch.setenv("PG_PASSWORD_T211", _SECRET)
    # `env://` to a remote host needs the operator's allowlist now
    # (build_runners/_secret_policy); this fixture's host is the bare name `db`.
    # This suite is about the secret not reaching a FILE or a diagnostic once it
    # is legitimately in use, which is a different control from whether the send
    # is permitted at all.
    monkeypatch.setenv("FLUID_ACQUISITION_SECRET_REF_ALLOWLIST", "PG_PASSWORD_T211")
    monkeypatch.setenv("LAKEKEEPER_CREDENTIAL_T211", "inherited")
    captured = tmp_path / "server-env.txt"
    server = tmp_path / "debezium-server"
    server.write_text(
        "#!/bin/sh\n"
        'printf \'%s|%s\' "$FLUID_DBZ_SOURCE_DATABASE_PASSWORD" "$LAKEKEEPER_CREDENTIAL_T211"'
        f" > '{captured}'\nexit 3\n"
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
    # the credential is added to the inherited environment, not instead of it
    assert captured.read_text() == f"{_SECRET}|inherited"


def _embedded_ctx(tmp_path):
    return SimpleNamespace(
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


def _run_embedded(tmp_path):
    from fluid_build.build_runners.debezium import runner as dbz

    return dbz._execute_debezium_server(
        _embedded_ctx(tmp_path),
        {"engine": "debezium", "properties": {}},
        {"mode": "embedded"},
        {"server": {"sink": {"type": "kafka"}}},
        "initial",
        "2026-10-13T00:00:00Z",
        0.0,
    )


def test_an_unwritable_config_is_a_failed_run_not_a_crash(tmp_path, monkeypatch):
    from fluid_build.build_runners.debezium import runner as dbz

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(dbz, "_write_private_text", refuse)
    result = _run_embedded(tmp_path)
    assert result.state.value == "failed"
    assert "could not write" in (result.error or "")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_an_earlier_releases_world_readable_config_is_truncated_and_tightened(tmp_path):
    config = tmp_path / ".fluid/debezium/p/b/application.properties"
    config.parent.mkdir(parents=True)
    config.write_text(f"debezium.source.database.password={_SECRET}\n")
    config.chmod(0o644)
    _run_embedded(tmp_path)
    assert _SECRET not in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks and O_NOFOLLOW")
def test_a_symlink_in_place_of_the_config_is_refused(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    config = tmp_path / ".fluid/debezium/p/b/application.properties"
    config.parent.mkdir(parents=True)
    config.symlink_to(target)
    result = _run_embedded(tmp_path)
    assert result.state.value == "failed"
    assert target.read_text() == "untouched"


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
        f"with open({str(seen)!r}, 'w') as out:\n"
        "    json.dump({'path': path, 'mode': mode, 'parent': parent, 'body': body}, out)\n"
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

    tap = _fake_tap(tmp_path, 0, sleep=120)
    assert (
        discover_tap_catalog(
            str(tap), config=_tap_config(), workdir=tmp_path / "w", timeout_seconds=8
        )
        is None
    )
    seen = json.loads((tmp_path / "tap-saw.json").read_text())
    assert not Path(seen["path"]).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a script target")
def test_invoke_target_config_is_private_and_removed(tmp_path):
    from fluid_build.build_runners.meltano.runner import invoke_target

    workdir = tmp_path / ".fluid/meltano/p/b"
    workdir.mkdir(parents=True)
    stale = workdir / "target_config.json"
    stale.write_text(json.dumps(_tap_config()))  # an earlier release's copy

    invoke_target(
        str(_fake_tap(tmp_path, 0)), config=_tap_config(), schemas={}, records={}, workdir=workdir
    )

    seen = json.loads((tmp_path / "tap-saw.json").read_text())
    assert json.loads(seen["body"])["password"] == _SECRET
    assert seen["mode"] == 0o600 and seen["parent"] == 0o700
    assert not Path(seen["path"]).exists()
    assert not stale.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and a script tap")
def test_invoke_tap_removes_the_config_after_a_timeout(tmp_path):
    from fluid_build.build_runners.meltano.runner import invoke_tap

    with pytest.raises(subprocess.TimeoutExpired):
        invoke_tap(
            str(_fake_tap(tmp_path, 0, sleep=120)),
            config=_tap_config(),
            workdir=tmp_path / "w",
            timeout_seconds=8,
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
        # each product lives in its own directory under the workspace root
        "customer-360/.fluid/debezium/sales.orders/b1/application.properties",
        "customer-360/.fluid/meltano/sales.orders/b1/target_stderr.log",
    ):
        result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", rel])
        assert result.returncode == 0, rel
