# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate artifacts --emit spark``: each forge Spark build's ``job.json``.

The artifact is the object OpenTofu uploads to
``_fluid/spark/<product>/<build>/job.json`` (``iac/providers/aws_ingest.py``),
byte for byte, so its MANIFEST hash is the hash of the deployed config. Each
test renders the same contract's OpenTofu module (``build_module``) and compares
against the ``aws_s3_object`` it uploads, rather than a copy of either.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.forge.core.artifact_fanout import (
    DEFAULT_EMIT,
    SPARK_ACCOUNT_PLACEHOLDER,
    FanoutError,
    run_fanout,
)
from fluid_build.forge.core.artifact_validators import (
    validate_artifacts,
    validate_spark_job_json,
)
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import build_module
from fluid_build.iac.providers import aws_identity
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV, MSK_BROKERS_PLACEHOLDER
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

pytestmark = [pytest.mark.unit]

LOG = logging.getLogger("test.artifact_fanout_spark")

CLUSTER = "j-ABCDEFGH1234"
MSK_ARN = (
    "arn:aws:kafka:eu-central-1:000000000005:cluster/events/0a1b2c3d-4e5f-6789-abcd-ef0123456789-2"
)
ACCOUNT_REF = "${data.aws_caller_identity.fluid_lf_caller.account_id}"

#: A Kafka stream to raw Iceberg and a daily Spark SQL batch to curated, both on an
#: existing EMR cluster: the forge-native shape of an EMR + Structured Streaming product.
CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "platform.events",
    "name": "Events",
    "metadata": {"owner": {"team": "data-platform"}},
    "builds": [
        {
            "id": "stream-raw",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["events_raw"],
            "properties": {
                "stream": {
                    "kind": "kafka",
                    "topic": "source.events",
                    "format": "json",
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "user_id", "type": "string"},
                        {"name": "event_time", "type": "timestamp_ltz(3)"},
                    ],
                    "trigger": {"processingTime": "PT1M"},
                    "watermark": {"column": "event_time", "delay": "PT10M"},
                    "startingOffsets": "earliest",
                },
                "mapping": {"columns": [{"name": "user_id", "from": "user_id"}]},
                "parameters": {
                    "spark": {
                        "runtime": "emr-ec2",
                        "clusterId": CLUSTER,
                        "mode": "streaming",
                        "source": {
                            "bootstrapServers": "kafka.example.internal:9092",
                            "auth": "none",
                        },
                    }
                },
            },
        },
        {
            "id": "daily-curated",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["events_daily"],
            "execution": {"trigger": {"type": "schedule", "schedule": "0 2 * * *"}},
            "properties": {
                "sql": (
                    "SELECT CAST(event_time AS DATE) AS event_date, COUNT(*) AS events "
                    "FROM raw.events GROUP BY CAST(event_time AS DATE)"
                ),
                "parameters": {
                    "spark": {"runtime": "emr-ec2", "clusterId": CLUSTER, "mode": "batch"}
                },
            },
        },
    ],
    "exposes": [
        {
            "exposeId": "events_raw",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "raw",
                    "table": "events",
                    "bucket": "lake",
                    "path": "iceberg/raw/events/",
                    "region": "eu-central-1",
                },
            },
            "contract": {
                "schema": [
                    {"name": "event_id", "type": "string", "required": True},
                    {"name": "user_id", "type": "string"},
                    {"name": "event_time", "type": "timestamp_ltz(3)"},
                ]
            },
        },
        {
            "exposeId": "events_daily",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "curated",
                    "table": "events_daily",
                    "bucket": "lake",
                    "path": "iceberg/curated/events_daily/",
                    "region": "eu-central-1",
                },
            },
            "contract": {
                "schema": [
                    {"name": "event_date", "type": "date", "required": True},
                    {"name": "events", "type": "bigint"},
                ]
            },
        },
    ],
}


#: The operator policy the IaC emitter needs to render the uploads, and no more.
POLICY = {
    CONTRACT_SQL_ALLOWLIST_ENV: "platform.events",
    HOST_ALLOWLIST_ENV: "kafka.example.internal",
    aws_identity.TARGET_ALLOWLIST_ENV: f"emr-ec2:{CLUSTER},msk:{MSK_ARN}",
    aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV: None,
    aws_identity.SPARK_CONF_ALLOWLIST_ENV: None,
}


@pytest.fixture(autouse=True)
def operator_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in POLICY.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.fixture
def contract() -> Dict[str, Any]:
    return copy.deepcopy(CONTRACT)


def _write(tmp_path: Path, contract: Dict[str, Any]) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _uploads(contract: Dict[str, Any]) -> Dict[str, str]:
    """``{build id: content}`` of each ``job.json`` object the OpenTofu module uploads."""
    module = json.loads(build_module(get_iac_plugin("aws"), contract))
    objects = module["resource"]["aws_s3_object"]
    prefix = f"_fluid/spark/{contract['id']}/"
    return {
        obj["key"][len(prefix) : -len("/job.json")]: obj["content"]
        for obj in objects.values()
        if obj["key"].startswith(prefix) and obj["key"].endswith("/job.json")
    }


#: ``run_fanout(source, out)`` for the spark key alone.
_fan_out = functools.partial(run_fanout, emit_raw="spark", manifest_path=None, logger=LOG)


class TestTheBytesAreTheUploadedObject:
    def test_each_build_s_job_json_is_the_object_opentofu_uploads(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        out = tmp_path / "artifacts"
        manifest = _fan_out(_write(tmp_path, contract), out)
        uploads = _uploads(contract)
        assert sorted(uploads) == ["daily-curated", "stream-raw"]
        for build_id, content in uploads.items():
            data = (out / "spark" / build_id / "job.json").read_bytes()
            assert data == content.encode("utf-8")
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            assert manifest["files"][f"spark/{build_id}/job.json"] == digest

    def test_a_bundle_gives_the_same_bytes(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        from fluid_build.forge.core.bundle import build_bundle_tgz

        bundle = tmp_path / "bundle.tgz"
        build_bundle_tgz(contract, bundle, contract_id=contract["id"])
        from_bundle, from_contract = tmp_path / "b", tmp_path / "c"
        _fan_out(bundle, from_bundle)
        _fan_out(_write(tmp_path, contract), from_contract)
        for build_id in ("daily-curated", "stream-raw"):
            rel = Path("spark") / build_id / "job.json"
            assert (from_bundle / rel).read_bytes() == (from_contract / rel).read_bytes()

    def test_an_account_bucket_keeps_the_account_as_a_placeholder(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        """OpenTofu writes the deploying account where the artifact says <account>."""
        for expose in contract["exposes"]:
            location = expose["binding"]["location"]
            del location["bucket"]
            # The shared account bucket: the product writes its own prefix only.
            location["path"] = f"platform.events/{location['table']}/"
        out = tmp_path / "artifacts"
        _fan_out(_write(tmp_path, contract), out)
        for build_id, content in _uploads(contract).items():
            text = (out / "spark" / build_id / "job.json").read_text(encoding="utf-8")
            assert f"{SPARK_ACCOUNT_PLACEHOLDER}-fluid-data" in text
            assert text.replace(SPARK_ACCOUNT_PLACEHOLDER, ACCOUNT_REF) == content

    def test_an_msk_source_keeps_the_broker_list_as_the_compiler_s_placeholder(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        spark = contract["builds"][0]["properties"]["parameters"]["spark"]
        spark["source"] = {"mskClusterArn": MSK_ARN, "auth": "msk-iam"}
        out = tmp_path / "artifacts"
        _fan_out(_write(tmp_path, contract), out)
        text = (out / "spark" / "stream-raw" / "job.json").read_text(encoding="utf-8")
        assert MSK_BROKERS_PLACEHOLDER in text
        key = "platform_events_stream_raw_spark"
        brokers = f"${{data.aws_msk_bootstrap_brokers.{key}.bootstrap_brokers_sasl_iam}}"
        assert text.replace(MSK_BROKERS_PLACEHOLDER, brokers) == _uploads(contract)["stream-raw"]


def _uploads_as_generate_iac_reads(path: Path, env: Optional[str] = None) -> Dict[str, str]:
    """The uploads of the contract at ``path`` loaded as ``fluid generate iac`` loads it."""
    from fluid_build.cli._common import (
        load_contract_with_overlay,
        resolve_env_templates_in_contract,
    )

    return _uploads(
        resolve_env_templates_in_contract(load_contract_with_overlay(str(path), env, LOG))
    )


class TestTheContractIsReadAsApplyReadsIt:
    def _same(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        source = _write(tmp_path, contract)
        out = tmp_path / "artifacts"
        _fan_out(source, out)
        uploads = _uploads_as_generate_iac_reads(source)
        assert uploads
        for build_id, content in uploads.items():
            assert (out / "spark" / build_id / "job.json").read_text(encoding="utf-8") == content

    def test_an_aliased_binding_format(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        for expose in contract["exposes"]:
            expose["binding"]["format"] = "iceberg_table"
        self._same(tmp_path, contract)

    def test_env_templates_are_resolved(
        self, tmp_path: Path, contract: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTS_TOPIC", "source.events")
        monkeypatch.setenv("RAW_SCHEMA", "raw")
        contract["builds"][0]["properties"]["stream"]["topic"] = "{{ env.EVENTS_TOPIC }}"
        sql = contract["builds"][1]["properties"]["sql"]
        contract["builds"][1]["properties"]["sql"] = sql.replace(
            "raw.events", "{{ env.RAW_SCHEMA }}.events"
        )
        self._same(tmp_path, contract)
        text = (tmp_path / "artifacts" / "spark" / "daily-curated" / "job.json").read_text(
            encoding="utf-8"
        )
        assert "{{" not in text

    def test_a_singular_build_key(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        contract["build"] = contract.pop("builds")[1]
        self._same(tmp_path, contract)

    def test_the_env_overlay_is_applied(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        source = _write(tmp_path, contract)
        sql = contract["builds"][1]["properties"]["sql"].replace("COUNT(*)", "COUNT(1)")
        overlay = {"builds": [{"id": "daily-curated", "properties": {"sql": sql}}]}
        (tmp_path / "overlays").mkdir()
        (tmp_path / "overlays" / "dev.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
        out = tmp_path / "artifacts"
        _fan_out(source, out, env="dev")
        uploads = _uploads_as_generate_iac_reads(source, "dev")
        assert "COUNT(1)" in uploads["daily-curated"]
        for build_id, content in uploads.items():
            assert (out / "spark" / build_id / "job.json").read_text(encoding="utf-8") == content


class TestTheEmitKey:
    def test_spark_is_in_the_default_set(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        assert "spark" in DEFAULT_EMIT
        manifest = run_fanout(
            _write(tmp_path, contract),
            tmp_path / "a",
            emit_raw=None,
            manifest_path=None,
            logger=LOG,
        )
        assert "spark/stream-raw/job.json" in manifest["files"]

    def test_a_contract_without_a_spark_build_writes_nothing(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        contract["builds"] = [
            {"id": "b", "pattern": "embedded-logic", "engine": "sql", "properties": {"sql": "1"}}
        ]
        out = tmp_path / "artifacts"
        manifest = _fan_out(_write(tmp_path, contract), out)
        assert manifest["files"] == {}
        assert not (out / "spark").exists()

    def test_a_contract_before_0_7_7_writes_nothing(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        contract["fluidVersion"] = "0.7.6"
        assert _fan_out(_write(tmp_path, contract), tmp_path / "a")["files"] == {}

    def test_a_build_the_compiler_refuses_names_the_build(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        contract["builds"][1]["properties"]["parameters"]["spark"]["runtime"] = "mainframe"
        with pytest.raises(FanoutError) as exc:
            _fan_out(_write(tmp_path, contract), tmp_path / "a")
        assert exc.value.key == "spark"
        assert str(exc.value).startswith("Spark build daily-curated: ")

    def test_a_parameters_block_that_is_not_an_object_is_a_typed_error(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        contract["builds"][1]["properties"]["parameters"]["spark"] = "x"
        with pytest.raises(FanoutError) as exc:
            _fan_out(_write(tmp_path, contract), tmp_path / "a")
        assert exc.value.key == "spark"

    @pytest.mark.parametrize("second", ["stream-raw", "Stream-Raw", "stream_raw"])
    def test_two_builds_that_would_write_one_file(
        self, tmp_path: Path, contract: Dict[str, Any], second: str
    ) -> None:
        contract["builds"][1]["id"] = second
        with pytest.raises(FanoutError, match="would write one job.json"):
            _fan_out(_write(tmp_path, contract), tmp_path / "a")

    def test_a_config_the_runner_refuses_is_not_written(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        props = contract["builds"][1]["properties"]
        props["sql"] = props["sql"].replace("COUNT(*)", "COUNT('${x}')")
        out = tmp_path / "a"
        with pytest.raises(FanoutError, match="holds '\\$\\{'"):
            _fan_out(_write(tmp_path, contract), out)
        assert not (out / "spark").exists()

    @pytest.mark.parametrize("msk_source", [False, True], ids=["no-msk", "msk-source"])
    def test_the_broker_placeholder_outside_the_broker_list_is_refused(
        self, tmp_path: Path, contract: Dict[str, Any], msk_source: bool
    ) -> None:
        # OpenTofu fills the placeholder only in an MSK source's bootstrapServers.
        if msk_source:
            spark = contract["builds"][0]["properties"]["parameters"]["spark"]
            spark["source"] = {"mskClusterArn": MSK_ARN, "auth": "msk-iam"}
        props = contract["builds"][1]["properties"]
        props["sql"] = props["sql"].replace("COUNT(*)", f"COUNT('{MSK_BROKERS_PLACEHOLDER}')")
        with pytest.raises(UnsupportedBindingError, match="holds '\\$\\{'"):
            _uploads(contract)
        out = tmp_path / "a"
        with pytest.raises(FanoutError, match="daily-curated: the job config holds '\\$\\{'"):
            _fan_out(_write(tmp_path, contract), out)
        assert not (out / "spark").exists()

    def test_a_spark_folder_of_the_project_s_own_is_kept(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        out = tmp_path / "project"
        (out / "spark" / "jobs").mkdir(parents=True)
        (out / "spark" / "jobs" / "etl.py").write_text("print(1)", encoding="utf-8")
        _fan_out(_write(tmp_path, contract), out, emit_raw="odcs")
        assert (out / "spark" / "jobs" / "etl.py").is_file()

    def test_a_stale_spark_directory_is_cleared(
        self, tmp_path: Path, contract: Dict[str, Any]
    ) -> None:
        out = tmp_path / "artifacts"
        (out / "spark" / "gone").mkdir(parents=True)
        (out / "spark" / "gone" / "job.json").write_text("{}", encoding="utf-8")
        _fan_out(_write(tmp_path, contract), out)
        assert sorted(p.name for p in (out / "spark").iterdir()) == ["daily-curated", "stream-raw"]


class TestValidateArtifacts:
    def test_the_spark_artifacts_pass(self, tmp_path: Path, contract: Dict[str, Any]) -> None:
        out = tmp_path / "artifacts"
        _fan_out(_write(tmp_path, contract), out)
        report = validate_artifacts(out, strict=True)
        assert report.status == "pass", [i.to_dict() for i in report.issues]

    @pytest.mark.parametrize(
        "path, content, code",
        [
            ("spark/b/job.json", b"{not json", "SPARK-PARSE"),
            ("spark/b/job.json", b"[]", "SPARK-SHAPE"),
            ("spark/job.json", b"{}", "SPARK-PATH"),
            ("spark/b/x.json", b"{}", "SPARK-PATH"),
            (
                "spark/b/job.json",
                b'{"configVersion": 1, "build": "other", "product": "p", "mode": "batch", '
                b'"sink": {}}',
                "SPARK-SHAPE",
            ),
            ("spark/b/job.json", b'{"configVersion": true, "build": "b"}', "SPARK-SHAPE"),
        ],
    )
    def test_a_job_json_that_is_not_one_is_an_error(
        self, path: str, content: bytes, code: str
    ) -> None:
        issues = validate_spark_job_json(path, content)
        assert issues and all(i.severity == "error" for i in issues)
        assert code in {i.code for i in issues}

    def test_a_well_formed_job_json_passes(self) -> None:
        doc = {"configVersion": 1, "build": "b", "product": "p", "mode": "batch", "sink": {}}
        assert validate_spark_job_json("spark/b/job.json", json.dumps(doc).encode()) == []
