# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema pins for ``exposes[].iceberg.maintenance.runtime`` in 0.7.7 (``$defs.sparkRuntime``).

The block names the runtime that maintains a table no forge build writes, in place of
``build``: one of ``emr-serverless``, ``emr-ec2`` or ``emr-eks`` with its target and the role
the run executes as, or ``local`` with neither. ``build`` and ``runtime`` exclude each other, ``runtime: glue``
is no runtime of it, and a contract with a writer build and no ``runtime`` stays valid.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.schema_manager import FluidSchemaManager
from tests.spark.conftest import CONTRACT

pytestmark = pytest.mark.unit

ROLE = "arn:aws:iam::123456789012:role/orders-maint"


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def runtime(**extra: Any) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "runtime": "emr-serverless",
        "applicationId": "app0000000001",
        "executionRoleArn": ROLE,
    }
    block.update(extra)
    return block


def contract(maintenance: Dict[str, Any] | None, *, writer: bool = False) -> Dict[str, Any]:
    """The Spark example, with ``maintenance`` on its expose and no writer build unless asked."""
    out = copy.deepcopy(CONTRACT)
    if not writer:
        out["builds"] = []
    if maintenance is not None:
        out["exposes"][0]["iceberg"] = {"maintenance": maintenance}
    return out


def block(rt: Any = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "schedule": "0 3 * * *",
        "runtime": rt if rt is not None else runtime(),
        "compaction": {},
        "removeOrphanFiles": {"olderThan": "P7D"},
    }
    out.update(extra)
    return out


def check(manager: FluidSchemaManager, doc: Dict[str, Any]):
    return manager.validate_contract(doc, schema_version="0.7.7", offline_only=True)


def errors(result) -> List[str]:
    return list(result.errors)


def test_the_example_without_the_block_is_valid(manager) -> None:
    result = check(manager, contract(None, writer=True))
    assert result.is_valid, errors(result)


def test_a_writer_build_contract_with_a_build_block_stays_valid(manager) -> None:
    result = check(manager, contract({"schedule": "0 3 * * *", "compaction": {}}, writer=True))
    assert result.is_valid, errors(result)


def test_a_valid_runtime_block_passes(manager) -> None:
    result = check(manager, contract(block()))
    assert result.is_valid, errors(result)


@pytest.mark.parametrize(
    "rt",
    [
        {"runtime": "emr-ec2", "clusterId": "j-1ABCDEF", "executionRoleArn": ROLE},
        {
            "runtime": "emr-eks",
            "virtualClusterId": "vc0000000001",
            "executionRoleArn": ROLE,
        },
        runtime(releaseLabel="emr-7.12.0", sparkConf={"spark.executor.memory": "4g"}),
    ],
)
def test_each_runtime_with_its_target_passes(manager, rt) -> None:
    result = check(manager, contract(block(rt)))
    assert result.is_valid, errors(result)


def test_build_and_runtime_together_fail(manager) -> None:
    result = check(manager, contract(block(build="ingest-orders"), writer=True))
    assert not result.is_valid


@pytest.mark.parametrize("name", ["glue", "emr-lambda"])
def test_a_runtime_that_is_not_a_listed_one_fails(manager, name) -> None:
    rt = runtime()
    rt["runtime"] = name
    assert not check(manager, contract(block(rt))).is_valid


def test_local_needs_no_role_and_no_target(manager) -> None:
    result = check(manager, contract(block({"runtime": "local"})))
    assert result.is_valid, errors(result)


def test_local_accepts_and_ignores_the_aws_keys(manager) -> None:
    result = check(manager, contract(block(runtime(runtime="local"))))
    assert result.is_valid, errors(result)


def test_a_missing_execution_role_fails(manager) -> None:
    rt = runtime()
    del rt["executionRoleArn"]
    assert not check(manager, contract(block(rt))).is_valid


@pytest.mark.parametrize(
    "rt",
    [
        {"runtime": "emr-serverless", "executionRoleArn": ROLE},
        {"runtime": "emr-ec2", "executionRoleArn": ROLE},
        {"runtime": "emr-eks", "executionRoleArn": ROLE},
    ],
)
def test_each_runtime_needs_its_own_target(manager, rt) -> None:
    assert not check(manager, contract(block(rt))).is_valid


def test_a_target_of_another_runtime_does_not_stand_in(manager) -> None:
    rt = {"runtime": "emr-ec2", "applicationId": "app0000000001", "executionRoleArn": ROLE}
    assert not check(manager, contract(block(rt))).is_valid


def test_an_unknown_runtime_key_fails(manager) -> None:
    assert not check(manager, contract(block(runtime(unknownKey="x")))).is_valid


def test_a_runtime_that_is_not_an_object_fails(manager) -> None:
    assert not check(manager, contract(block("emr-serverless"))).is_valid
