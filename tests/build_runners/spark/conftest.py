# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the Spark job-run tests: the compiler tests' contract, offline AWS."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from fluid_build.build_runners.spark._desired import DesiredRun, desired_runs
from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.spark.conftest import CONTRACT

ACCOUNT = "123456789012"


@pytest.fixture(autouse=True)
def operator_policy(monkeypatch):
    """Same operator policy as ``tests/spark/conftest.py``, and no AWS from the machine."""
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(SECRET_REF_ALLOWLIST_ENV, raising=False)
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example,example.internal")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


def spark_cfg(contract):
    return contract["builds"][0]["properties"]["parameters"]["spark"]


def runs_of(contract, **kwargs):
    kwargs.setdefault("account", ACCOUNT)
    return desired_runs(contract, **kwargs)


@pytest.fixture
def run(contract) -> DesiredRun:
    """The example's streaming build on EMR Serverless."""
    return runs_of(contract)[0]


def variant(run: DesiredRun, runtime: str, target: dict, **changes) -> DesiredRun:
    """``run`` moved to another runtime: adapters read only the run's fields."""
    return dataclasses.replace(run, runtime=runtime, target=target, **changes)
