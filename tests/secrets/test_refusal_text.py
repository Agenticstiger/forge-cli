# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a destination refusal quotes: only what the contract wrote, never a secret.

``fluid apply`` renders ``{{ env.NAME }}`` placeholders before a runner asks for
the secret, so the host the policy judges there may be the value of any variable
the contract named. A refusal quotes a host only when the contract wrote it as
it stands. A keyword connection string shows its host part alone, never the
password written beside it.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from fluid_build.secrets import resolve_secret, secret_run
from fluid_build.secrets.policy import (
    ALLOWLIST_ENV,
    PREFIXES_ENV,
    SCHEMES_ENV,
    STAGES_ENV,
    AcquisitionSecretPolicyError,
    refuse_unless_allowed,
)

REMOTE = "db.example.com"
#: What a variable the contract names might hold: it must never reach a message.
SECRET_VALUE = "s3cr3t-value-7f2a9c"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _operator_policy_unset(monkeypatch):
    for name in (ALLOWLIST_ENV, SCHEMES_ENV, PREFIXES_ENV, STAGES_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FLUID_TEST_RENDERED", SECRET_VALUE)


def _apply_refusal(connection):
    with secret_run("apply"):
        with pytest.raises(AcquisitionSecretPolicyError) as info:
            resolve_secret("env://PG", connection=connection)
    return info.value


def _as_written(connection):
    from fluid_build.secrets.policy import destinations_as_written

    return destinations_as_written(
        {"builds": [{"properties": {"source": {"connection": connection}}}]}
    )


# ── a rendered host ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_apply_never_quotes_a_host_it_cannot_trace_to_the_contract():
    refusal = _apply_refusal({"host": SECRET_VALUE})
    assert SECRET_VALUE not in str(refusal)
    assert "connection.host" in refusal.what
    assert '"PG@<host>"' in refusal.fix


@pytest.mark.unit
def test_apply_quotes_a_host_the_contract_wrote():
    with _as_written({"host": REMOTE}):
        refusal = _apply_refusal({"host": REMOTE})
    assert REMOTE in refusal.what
    assert f"PG@{REMOTE}" in refusal.fix


@pytest.mark.unit
def test_apply_names_the_placeholder_a_host_was_rendered_from():
    with _as_written({"host": "{{ env.FLUID_TEST_RENDERED }}"}):
        refusal = _apply_refusal({"host": SECRET_VALUE})
    assert SECRET_VALUE not in str(refusal)
    assert "{{ env.FLUID_TEST_RENDERED }}" in refusal.what
    assert "<host>" in refusal.fix


@pytest.mark.unit
def test_apply_quotes_the_written_uri_not_the_rendered_one():
    written = {"uri": "postgresql://u@{{ env.FLUID_TEST_RENDERED }}:5432/db"}
    with _as_written(written):
        refusal = _apply_refusal({"uri": f"postgresql://u@{SECRET_VALUE}:5432/db"})
    assert SECRET_VALUE not in str(refusal)
    assert "{{ env.FLUID_TEST_RENDERED }}" in refusal.what


@pytest.mark.unit
def test_the_acquisition_dispatcher_hands_the_policy_the_contract_as_written(
    monkeypatch, tmp_path: Path
):
    from fluid_build.build_runners import base

    seen = {}

    def run(build, contract, contract_dir, *, dry_run, sample_rows=None):
        connection = dict(build["properties"]["source"]["connection"])
        connection.pop("secretRef")
        for host in (connection["host"], REMOTE):
            try:
                with secret_run("apply"):
                    resolve_secret("env://PG", connection={**connection, "host": host})
            except AcquisitionSecretPolicyError as exc:
                seen[host] = exc
        return 0

    module = types.ModuleType("fluid_test_policy_runner")
    module.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fluid_test_policy_runner", module)
    monkeypatch.setitem(
        base._ACQUISITION_RUNNER_REGISTRY, "policy-test", ("fluid_test_policy_runner", "run")
    )
    written_connection = {"host": "{{ env.FLUID_TEST_RENDERED }}"}
    written_connection["secretRef"] = "env://PG"  # pragma: allowlist secret
    build = {
        "id": "b",
        "engine": "policy-test",
        "properties": {
            "source": {"connection": written_connection},
            "note": REMOTE,
        },
    }
    assert base._execute_acquisition_build(build, {"builds": [build]}, tmp_path, dry_run=False) == 0
    assert SECRET_VALUE not in str(seen[SECRET_VALUE])
    assert "{{ env.FLUID_TEST_RENDERED }}" in seen[SECRET_VALUE].what
    assert REMOTE in seen[REMOTE].what


# ── a keyword connection string ──────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        f"Server={REMOTE};Database=d;User Id=u;Password=hunter2",
        f"host={REMOTE} dbname=d user=u password=hunter2",
        f"Data Source=tcp:{REMOTE},1433;Initial Catalog=d;Password=hunter2;",
    ],
)
def test_a_keyword_string_shows_only_its_host_part(text):
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection={"connection_string": text}, environ={})
    assert "hunter2" not in str(info.value)
    assert REMOTE in info.value.what
    assert "connection.connection_string" in info.value.what


@pytest.mark.unit
def test_a_keyword_string_without_a_host_key_shows_no_part_of_it():
    text = "Database=d;Password=hunter2"
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", connection={"connection_string": text}, environ={})
    assert "hunter2" not in str(info.value)
    assert "Database" not in str(info.value)


@pytest.mark.unit
def test_apply_quotes_a_written_host_whose_password_is_a_placeholder():
    written = {"uri": "postgresql://u:{{ env.FLUID_TEST_RENDERED }}@db.example.com:5432/db"}
    with _as_written(written):
        refusal = _apply_refusal({"uri": f"postgresql://u:{SECRET_VALUE}@db.example.com:5432/db"})
    assert SECRET_VALUE not in str(refusal)
    assert "host 'db.example.com' (connection.uri)" in refusal.what
    assert "PG@db.example.com" in refusal.fix


@pytest.mark.unit
def test_apply_names_an_untraced_host_without_quoting_it():
    with secret_run("apply"):
        with pytest.raises(AcquisitionSecretPolicyError) as info:
            resolve_secret("env://PG", host=SECRET_VALUE)
    assert SECRET_VALUE not in str(info.value)
    assert "to the host it is sent to as a credential" in info.value.what


@pytest.mark.unit
def test_a_long_keyword_string_is_shown_in_linear_time():
    import time

    started = time.perf_counter()
    with pytest.raises(AcquisitionSecretPolicyError):
        refuse_unless_allowed("env://PG", connection={"server": "=" + " " * 3000}, environ={})
    assert time.perf_counter() - started < 0.5
