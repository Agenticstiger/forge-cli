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

import copy
import json

import pytest

from fluid_build.streaming.compiler import compile_contract, managed_config, validation_errors


def test_deterministic_artifacts_and_native_table_creation(contract):
    (first,) = compile_contract(contract)
    (second,) = compile_contract(copy.deepcopy(contract))
    assert first == second
    assert len(first.digest) == 64
    assert "CREATE CATALOG `lake`" in first.sql
    assert "CREATE DATABASE IF NOT EXISTS `lake`.`commerce`" in first.sql
    assert "CREATE TABLE IF NOT EXISTS `lake`.`commerce`.`orders`" in first.sql
    assert "MAP<STRING NOT NULL, STRING>" in first.sql
    assert first.statements[-1].startswith("INSERT INTO `lake`.`commerce`.`orders`")
    cm, deployment = first.resources
    assert cm["immutable"]
    assert "statement.4=INSERT INTO" in cm["data"]["job.properties"]
    assert deployment["spec"]["job"]["upgradeMode"] == "savepoint"
    assert (
        deployment["spec"]["flinkConfiguration"]["execution.checkpointing.mode"] == "EXACTLY_ONCE"
    )
    assert deployment["spec"]["podTemplate"]["spec"]["containers"][0]["envFrom"] == [
        {"secretRef": {"name": "flink-storage"}},
    ]


def test_sql_and_runtime_changes_change_digest(contract):
    (original,) = compile_contract(contract)
    contract["builds"][0]["properties"]["sql"] += " WHERE event_id <> ''"
    (changed,) = compile_contract(contract)
    assert original.digest != changed.digest
    cfg = managed_config(contract["builds"][0])
    cfg["deployment"]["parallelism"] = 2
    (parallel,) = compile_contract(contract)
    assert changed.digest != parallel.digest


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda c, f: f["deployment"].update(image="flink:latest"), "invalid configuration"),
        (lambda c, f: f["deployment"].pop("context"), "invalid configuration"),
        (lambda c, f: f.update(typo=True), "invalid configuration"),
        (lambda c, f: f["source"].update(format="protobuf"), "invalid configuration"),
        (lambda c, f: f["source"].update(format="avro-confluent"), "schemaRegistryUrl"),
        (
            lambda c, f: f["deployment"].update(savepointUri=f["deployment"]["checkpointUri"]),
            "overlap",
        ),
        (lambda c, f: f["deployment"].update(haUri="s3://state/"), "dedicated prefix"),
        (lambda c, f: f["deployment"].update(checkpointTimeoutMs=1000), "exceed"),
        (
            lambda c, f: c["builds"][0]["properties"].update(sql="SELECT 1; DROP TABLE x"),
            "single SELECT",
        ),
        (
            lambda c, f: c["builds"][0]["properties"].update(sql="DELETE FROM source_events"),
            "single SELECT",
        ),
        (lambda c, f: c["builds"][0].update(outputs=[]), "exactly one"),
        (lambda c, f: c["builds"][0]["execution"]["trigger"].update(type="manual"), "continuous"),
        (lambda c, f: c["exposes"][0]["binding"]["location"].update(catalog="glue"), "REST"),
        (
            lambda c, f: c["exposes"][0]["binding"]["icebergConfig"].update(
                properties={"write.upsert.enabled": "true"}
            ),
            "append-only",
        ),
        (lambda c, f: c["exposes"][0].update(policy={"privacy": {}}), "policy/quality"),
    ],
)
def test_fail_closed(contract, mutate, match):
    mutate(contract, managed_config(contract["builds"][0]))
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)
    assert validation_errors(contract)


def test_external_flink_unchanged(contract):
    contract["builds"][0]["properties"]["parameters"].pop("flink")
    assert compile_contract(contract) == ()


def test_mixed_contract_refused(contract):
    contract["builds"].append({"id": "batch", "engine": "sql"})
    with pytest.raises(ValueError, match="only managed"):
        compile_contract(contract)


def test_catalog_auth_is_reference_only(contract):
    cfg = managed_config(contract["builds"][0])
    cfg["catalogAuth"] = {"type": "bearer", "tokenEnv": "CATALOG_TOKEN"}
    contract["exposes"][0]["binding"]["location"]["uri"] = "https://catalog.example"
    (artifact,) = compile_contract(contract)
    assert "'token' = 'env:CATALOG_TOKEN'" in artifact.sql
    assert "CATALOG_TOKEN" in json.dumps(artifact.resources)


def test_event_time_and_partition(contract):
    cfg = managed_config(contract["builds"][0])
    cfg["source"]["eventTime"] = {"column": "event_time", "delaySeconds": 5}
    contract["builds"][0]["properties"][
        "sql"
    ] = "SELECT event_id, customer, items, attributes, event_time FROM source_events"
    contract["exposes"][0]["binding"]["icebergConfig"]["partitionSpec"] = [
        {"sourceColumn": "event_time", "transform": "day"},
    ]
    (artifact,) = compile_contract(contract)
    assert "WATERMARK FOR `event_time` AS `event_time` - INTERVAL '5' SECOND" in artifact.sql
    assert "PARTITIONED BY (days(`event_time`))" in artifact.sql


@pytest.mark.parametrize("surface", ["root", "column", "source", "dq"])
def test_uncompiled_governance_is_rejected(contract, surface):
    target = contract["exposes"][0]["contract"]
    if surface == "root":
        contract["accessPolicy"] = {"allow": ["reader"]}
    elif surface == "column":
        target["schema"][1]["fields"][0]["validationRules"] = {"unique": True}
    elif surface == "source":
        managed_config(contract["builds"][0])["source"]["schema"][0]["quality"] = ["check"]
    else:
        target["dq"] = {"tests": ["check"]}
    with pytest.raises(ValueError, match="enforce|policy/quality"):
        compile_contract(contract)


def test_s3_compatible_state_configuration_matches_fileio(contract):
    cfg = managed_config(contract["builds"][0])
    cfg["catalogProperties"] = {
        "s3.endpoint": "http://minio:9000",
        "s3.path-style-access": "true",
        "client.region": "us-east-1",
    }
    (artifact,) = compile_contract(contract)
    config = artifact.resources[1]["spec"]["flinkConfiguration"]
    assert config["s3.endpoint"] == "http://minio:9000"
    assert config["s3.path.style.access"] == "true"
    assert config["s3.endpoint.region"] == "us-east-1"
    assert (
        json.loads(artifact.resources[0]["data"]["deployment.json"])
        == artifact.resources[1]["spec"]
    )


def test_authenticated_catalog_requires_https(contract):
    managed_config(contract["builds"][0])["catalogAuth"] = {
        "type": "bearer",
        "tokenEnv": "CATALOG_TOKEN",
    }
    with pytest.raises(ValueError, match="require HTTPS"):
        compile_contract(contract)


def test_configmap_size_limit_is_enforced(contract):
    contract["builds"][0]["properties"]["sql"] = "SELECT '" + "x" * (1024 * 1024) + "'"
    with pytest.raises(ValueError, match="1 MiB limit"):
        compile_contract(contract)


def test_null_management_config_is_not_silently_external(contract):
    contract["builds"][0]["properties"]["parameters"]["flink"] = None
    with pytest.raises(ValueError, match="must be an object"):
        compile_contract(contract)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda c, f: c["builds"][0].pop("id"), "build id"),
        (
            lambda c, f: c["builds"][0].update(
                engine="sql",
                properties={"parameters": {"flink": f}},
            ),
            "requires engine",
        ),
        (lambda c, f: c["exposes"][0]["binding"].update(format="parquet"), "format"),
        (lambda c, f: c["exposes"][0]["binding"]["location"].pop("uri"), "uri and warehouse"),
        (
            lambda c, f: c["exposes"][0]["binding"]["location"].update(warehouse="gs://bucket/"),
            "warehouse",
        ),
        (lambda c, f: c["exposes"][0]["binding"]["location"].pop("table"), "namespace/database"),
        (lambda c, f: f["source"].update(schemaRegistryUrl="http://registry:8081"), "only to avro"),
        (lambda c, f: f["source"]["eventTime"].update(column="event_id"), "timestamp"),
        (lambda c, f: c["builds"][0]["properties"].pop("sql"), "SELECT"),
        (
            lambda c, f: c["exposes"][0]["binding"]["icebergConfig"].update(writeVersion=1),
            "version 2",
        ),
        (
            lambda c, f: c["exposes"][0]["binding"]["icebergConfig"].update(sortOrder=["event_id"]),
            "sortOrder",
        ),
    ],
)
def test_invalid_semantic_combinations_fail_before_emission(contract, mutate, match):
    mutate(contract, managed_config(contract["builds"][0]))
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)


def test_avro_source_emits_registry_format_without_json_options(contract):
    cfg = managed_config(contract["builds"][0])
    cfg["source"].update(format="avro-confluent", schemaRegistryUrl="https://registry.example")
    (artifact,) = compile_contract(contract)
    assert "'avro-confluent.url' = 'https://registry.example'" in artifact.sql
    assert "json." not in artifact.sql


@pytest.mark.parametrize(
    "partition,sql",
    [
        ({"sourceColumn": "event_id", "transform": "identity"}, "`event_id`"),
        (
            {"sourceColumn": "event_id", "transform": "bucket", "numBuckets": 8},
            "bucket(8, `event_id`)",
        ),
        (
            {"sourceColumn": "event_id", "transform": "truncate", "width": 4},
            "truncate(4, `event_id`)",
        ),
    ],
)
def test_partition_transforms_use_native_iceberg_ddl(contract, partition, sql):
    contract["exposes"][0]["binding"]["icebergConfig"]["partitionSpec"] = [partition]
    (artifact,) = compile_contract(contract)
    assert f"PARTITIONED BY ({sql})" in artifact.sql


@pytest.mark.parametrize(
    "partition,match",
    [
        ({"sourceColumn": "missing", "transform": "identity"}, "not a target"),
        ({"sourceColumn": "event_id", "transform": "bucket", "numBuckets": 0}, "positive"),
        ({"sourceColumn": "event_id", "transform": "unknown"}, "Unsupported partition"),
    ],
)
def test_invalid_partition_transforms_fail(contract, partition, match):
    contract["exposes"][0]["binding"]["icebergConfig"]["partitionSpec"] = [partition]
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)


def test_duplicate_writers_and_cross_build_state_prefixes_are_rejected(contract):
    build = copy.deepcopy(contract["builds"][0])
    build["id"] = "other"
    contract["builds"].append(build)
    with pytest.raises(ValueError, match="unique namespace/name"):
        compile_contract(contract)
    managed_config(build)["deployment"]["name"] = "other"
    with pytest.raises(ValueError, match="one managed Flink writer"):
        compile_contract(contract)
    build["outputs"] = ["other-output"]
    target = copy.deepcopy(contract["exposes"][0])
    target["exposeId"] = "other-output"
    target["binding"]["location"]["table"] = "other_table"
    contract["exposes"].append(target)
    with pytest.raises(ValueError, match="overlap between"):
        compile_contract(contract)
