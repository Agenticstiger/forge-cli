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

"""End-to-end: a real ``tofu apply`` of plugin-emitted ``.tf.json``.

Proves the autogenerator concept end to end — a FLUID contract compiled
to ``.tf.json`` by the AWS plugin, then provisioned by a real ``tofu``
init/plan/apply/destroy cycle that creates real (emulated) AWS
resources — with no AWS account and no credentials.

``tofu`` is a Go binary, so it cannot use moto's ``mock_aws`` (which
patches the in-process Python ``botocore``); it needs a real HTTP
endpoint. moto's ``ThreadedMotoServer`` provides exactly that — an
in-process AWS API on a real ``localhost`` port, with no Docker, no
auth token, and no credentials. The emitted ``.tf.json`` is aimed at it
with a sidecar ``provider`` override.

Skipped unless ``tofu`` is on PATH AND moto's server extra is installed
(``pip install 'moto[glue,server]'`` — the ``server`` extra pulls in
Flask). The integration CI lane installs both.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.aws,
    pytest.mark.provider,
]


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — Flask (the `server` extra) may be absent
        return False


_SKIP = runner.tofu_path() is None or not _have_moto_server()
_SKIP_REASON = "needs `tofu` on PATH + moto server extra (pip install 'moto[glue,server]')"

# A full AWS exposure — the plugin emits all three resource kinds it
# supports: the backing S3 bucket, a Glue catalog database, and a Glue
# catalog table carrying the contract schema.
_REGION = "us-east-1"
_BUCKET = "fluid-moto-e2e"
_DATABASE = "analytics"
_TABLE = "orders"
_CONTRACT = {
    "id": "demo.lake",
    "exposes": [
        {
            "exposeId": "orders",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {
                    "database": _DATABASE,
                    "table": _TABLE,
                    "bucket": _BUCKET,
                    "path": "orders/",
                },
            },
            "contract": {
                "schema": [
                    {"name": "id", "type": "bigint", "required": True},
                    {"name": "amount", "type": "double"},
                ]
            },
        }
    ],
}


@pytest.fixture(scope="session")
def _tofu_plugin_cache(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A session-shared ``TF_PLUGIN_CACHE_DIR`` — the ``hashicorp/aws``
    provider is ~50 MB and downloading it per-test multiplies the runtime
    by an order of magnitude (~10× without this cache)."""
    return str(tmp_path_factory.mktemp("tofu-plugin-cache"))


@pytest.fixture
def tofu_env(_tofu_plugin_cache: str) -> Dict[str, str]:
    """The environment for ``tofu`` child processes — inherits PATH +
    AWS_*; overlays the shared plugin cache so the ~50 MB
    ``hashicorp/aws`` provider downloads exactly once per session."""
    env = build_tofu_env()
    env["TF_PLUGIN_CACHE_DIR"] = _tofu_plugin_cache
    return env


@pytest.fixture
def moto_endpoint() -> Iterator[str]:
    """Start an in-process moto AWS server; yield its ``http://`` endpoint."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def _provider_override(endpoint: str) -> Dict[str, Any]:
    """A sidecar ``provider`` block aiming the AWS provider at moto.

    ``tofu`` merges every ``*.tf.json`` in the directory, so this
    overlays endpoint + dummy-credential config onto the plugin's
    credential-free ``main.tf.json`` — the plugin output stays portable
    and secret-free; only the test rig knows about the emulator.
    """
    # Every AWS service the plugin or its test paths touch must point at
    # moto — adding a service later is a one-line addition to this tuple.
    services = (
        "s3",
        "glue",
        "sts",
        "iam",
        "kinesis",
        "lambda",
        "events",
        "scheduler",
        "stepfunctions",
        "redshiftserverless",
        "redshiftdata",
    )
    return {
        "provider": {
            "aws": {
                "region": _REGION,
                "access_key": "testing",
                "secret_key": "testing",
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: endpoint for svc in services},
            }
        }
    }


def _boto(service: str, endpoint: str) -> Any:
    import boto3

    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 — dummy, moto only
        region_name=_REGION,
    )


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_apply_destroy_cycle_against_moto(
    moto_endpoint: str, tofu_env: Dict[str, str], tmp_path: Any
) -> None:
    """contract -> .tf.json -> tofu init/plan/apply -> real resources -> destroy."""
    # 1. Compile the contract through the AWS plugin into .tf.json.
    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), _CONTRACT))
    (tmp_path / "provider.tf.json").write_text(json.dumps(_provider_override(moto_endpoint)))
    workdir = str(tmp_path)
    env = tofu_env  # Session-shared provider cache via ``TF_PLUGIN_CACHE_DIR``.

    # 2. init + plan — the plan must schedule all three resources.
    init = runner.tofu_init(workdir, env=env)
    assert init.ok, init.stderr or init.stdout

    plan = runner.tofu_plan(workdir, env=env)
    assert plan.ok, plan.stderr or plan.stdout
    assert runner.change_summary(plan)["add"] == 3

    # 3. apply — really creates the resources on the moto server.
    try:
        applied = runner.tofu_apply(workdir, env=env)
        assert applied.ok, applied.stderr or applied.stdout
        assert runner.change_summary(applied)["add"] == 3

        # Independently confirm the resources exist: a boto3 client hitting
        # the same moto server proves `tofu apply` did the work, not the test.
        buckets = [b["Name"] for b in _boto("s3", moto_endpoint).list_buckets()["Buckets"]]
        assert _BUCKET in buckets
        glue = _boto("glue", moto_endpoint)
        assert glue.get_database(Name=_DATABASE)["Database"]["Name"] == _DATABASE
        table = glue.get_table(DatabaseName=_DATABASE, Name=_TABLE)["Table"]
        assert table["Name"] == _TABLE
        assert [c["Name"] for c in table["StorageDescriptor"]["Columns"]] == ["id", "amount"]
    finally:
        # 4. destroy — tear everything down; the rollback path depends on this.
        destroyed = runner.tofu_destroy(workdir, env=env)
        assert destroyed.ok, destroyed.stderr or destroyed.stdout
        assert runner.change_summary(destroyed)["remove"] == 3

    # 5. confirm the teardown really removed the resources.
    import botocore.exceptions

    with pytest.raises(botocore.exceptions.ClientError):
        _boto("glue", moto_endpoint).get_database(Name=_DATABASE)


# ---------------------------------------------------------------------------
# Stage 1 — moto round-trips for the rest of the AWS surface
#
# moto's ThreadedMotoServer mocks most AWS services in-process; tests stay
# fast (no Docker, no real cloud). Tests that need behaviours moto does not
# faithfully reproduce — Athena query execution against Iceberg, Redshift
# Spectrum CREATE EXTERNAL SCHEMA, dbt-athena materialised writes — are
# deferred to Stage 2 (LocalStack) and Stage 3 (real AWS).
# ---------------------------------------------------------------------------


def _apply_module(
    plugin,
    contract: Dict[str, Any],
    moto_endpoint: str,
    workdir: Any,
    env: Dict[str, str],
    *,
    actions=(),
):
    """Emit + provider-override + init/plan/apply against moto.

    Returns the apply :class:`~fluid_build.iac.runner.TofuResult` so the
    test can assert on counts and read state afterwards. Teardown is the
    caller's job; ``env`` is the session-shared ``tofu_env`` (carries the
    provider cache).
    """
    (workdir / "main.tf.json").write_text(
        build_module(plugin, contract, actions=actions), encoding="utf-8"
    )
    (workdir / "provider.tf.json").write_text(
        json.dumps(_provider_override(moto_endpoint)), encoding="utf-8"
    )
    init = runner.tofu_init(str(workdir), env=env)
    assert init.ok, init.stderr or init.stdout
    plan = runner.tofu_plan(str(workdir), env=env)
    assert plan.ok, plan.stderr or plan.stdout
    applied = runner.tofu_apply(str(workdir), env=env)
    assert applied.ok, applied.stderr or applied.stdout
    return applied


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_iceberg_on_glue_round_trip(
    moto_endpoint: str, tofu_env: Dict[str, str], tmp_path: Any
) -> None:
    """Iceberg-on-Glue — the mesh-interface table. ``tofu apply`` creates a
    Glue catalog table whose ``Parameters.table_type`` is ``ICEBERG``, the
    hint Athena uses to query the Iceberg metadata layer.
    """
    contract = {
        "id": "silver.mesh.iceberg",
        "name": "Mesh Iceberg",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",  # → table_type=ICEBERG parameter
                    "location": {
                        "database": "mesh_silver",
                        "table": "events",
                        "bucket": "fluid-mesh-iceberg",
                        "path": "silver/events/",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "ts", "type": "timestamp"},
                    ]
                },
            }
        ],
    }
    applied = _apply_module(get_iac_plugin("aws"), contract, moto_endpoint, tmp_path, tofu_env)
    assert runner.change_summary(applied)["add"] == 3  # S3 + Glue db + Glue table

    try:
        glue = _boto("glue", moto_endpoint)
        table = glue.get_table(DatabaseName="mesh_silver", Name="events")["Table"]
        # The Iceberg hint AWS uses to recognise the table as an Iceberg
        # table over the underlying S3 metadata — the architectural premise.
        assert table["Parameters"]["table_type"] == "ICEBERG"
        assert table["Parameters"]["classification"] == "iceberg"
        # The Glue table top-level type stays EXTERNAL_TABLE — Iceberg
        # tables ride on top of the external-table abstraction.
        assert table["TableType"] == "EXTERNAL_TABLE"
        # The contract-derived schema lands in the storage descriptor.
        cols = {c["Name"]: c["Type"] for c in table["StorageDescriptor"]["Columns"]}
        assert cols == {"event_id": "string", "ts": "timestamp"}
    finally:
        runner.tofu_destroy(str(tmp_path), env=tofu_env)


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_kinesis_stream_round_trip(
    moto_endpoint: str, tofu_env: Dict[str, str], tmp_path: Any
) -> None:
    """``aws_kinesis_stream`` — a contract whose binding declares a Kinesis
    stream provisions one via ``tofu apply``."""
    contract = {
        "id": "bronze.events.stream",
        "name": "Bronze Events Stream",
        "exposes": [
            {
                "exposeId": "events_stream",
                "binding": {
                    "platform": "aws",
                    "format": "kafka_topic",
                    "location": {"stream": "bronze-events", "shard_count": 2},
                },
            }
        ],
    }
    applied = _apply_module(get_iac_plugin("aws"), contract, moto_endpoint, tmp_path, tofu_env)
    assert runner.change_summary(applied)["add"] >= 1
    try:
        ks = _boto("kinesis", moto_endpoint)
        desc = ks.describe_stream(StreamName="bronze-events")["StreamDescription"]
        assert desc["StreamName"] == "bronze-events"
    finally:
        runner.tofu_destroy(str(tmp_path), env=tofu_env)


@pytest.mark.skip(
    reason=(
        "moto 5.x's ThreadedMotoServer routes Redshift Serverless to a "
        "backend dispatcher that crashes on the AWS provider's request "
        "shape (``backends.get_backend`` AttributeError) — moto-side bug, "
        "not an emitter issue. Stage 2 (LocalStack) covers Redshift "
        "Serverless live testing properly. Emitter unit-tested in "
        "test_iac_aws.py::TestAwsRedshiftServerless; module-shape valid "
        "via test_iac_aws_validate.py::test_redshift_serverless_contract_validates."
    )
)
def test_redshift_serverless_namespace_and_workgroup_round_trip(
    moto_endpoint: str, tofu_env: Dict[str, str], tmp_path: Any
) -> None:
    """Deferred to Stage 2 (LocalStack) — see skip reason above."""


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_iceberg_plus_kinesis_single_apply(
    moto_endpoint: str, tofu_env: Dict[str, str], tmp_path: Any
) -> None:
    """A multi-resource contract — Iceberg-on-Glue + a Kinesis ingest stream
    — applies in one ``tofu apply``. Proves the AWS plugin emits a coherent
    module spanning catalog + streaming surface (the bronze→silver path)."""
    contract = {
        "id": "silver.mesh.events_with_stream",
        "name": "Mesh Silver Events + Stream",
        "exposes": [
            {
                "exposeId": "events_iceberg",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": "mesh_silver",
                        "table": "events",
                        "bucket": "fluid-mesh-multi",
                        "path": "silver/events/",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "bigint", "required": True}]},
            },
            {
                "exposeId": "events_stream",
                "binding": {
                    "platform": "aws",
                    "format": "kafka_topic",
                    "location": {"stream": "mesh-events-stream"},
                },
            },
        ],
    }
    applied = _apply_module(get_iac_plugin("aws"), contract, moto_endpoint, tmp_path, tofu_env)
    # S3 bucket + Glue db + Glue Iceberg table + Kinesis stream.
    assert runner.change_summary(applied)["add"] == 4
    try:
        glue = _boto("glue", moto_endpoint)
        assert (
            glue.get_table(DatabaseName="mesh_silver", Name="events")["Table"]["Parameters"][
                "table_type"
            ]
            == "ICEBERG"
        )
        ks = _boto("kinesis", moto_endpoint)
        assert (
            ks.describe_stream(StreamName="mesh-events-stream")["StreamDescription"]["StreamName"]
            == "mesh-events-stream"
        )
    finally:
        destroyed = runner.tofu_destroy(str(tmp_path), env=tofu_env)
        assert destroyed.ok, destroyed.stderr or destroyed.stdout
