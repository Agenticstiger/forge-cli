# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the Iceberg table maintenance tests.

The base contract is the Spark compiler tests' (a 0.7.7 contract whose build ``ingest-orders``
writes the Iceberg expose ``order_lines``, table ``commerce.order_lines``); ``maintained`` puts an
``iceberg.maintenance`` block on it.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from fluid_build.maintenance.plugins import reset_maintenance_plugin_cache
from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.spark.conftest import CONTRACT

EXPOSE = "order_lines"
TABLE = "fluid.commerce.order_lines"
ROLE_ARN = "arn:aws:iam::123456789012:role/glue-optimizer"


def maintained(
    block: Optional[Dict[str, Any]] = None, *, legacy: Optional[Dict[str, Any]] = None, base=None
) -> Dict[str, Any]:
    """A copy of the example contract whose expose carries ``iceberg.maintenance: block``
    (``legacy``: the other ``iceberg`` keys, such as ``compaction``)."""
    contract = copy.deepcopy(base if base is not None else CONTRACT)
    iceberg: Dict[str, Any] = dict(legacy or {})
    if block is not None:
        iceberg["maintenance"] = block
    contract["exposes"][0]["iceberg"] = iceberg
    return contract


def spark_block(**extra: Any) -> Dict[str, Any]:
    """A valid backend-spark block (compaction + a schedule) with ``extra`` merged over it."""
    block: Dict[str, Any] = {"schedule": "0 3 * * *", "compaction": {}}
    block.update(extra)
    return block


def glue_block(**extra: Any) -> Dict[str, Any]:
    """A valid backend-glue block with ``extra`` merged over it."""
    block: Dict[str, Any] = {"backend": "glue", "roleArn": ROLE_ARN, "compaction": {}}
    block.update(extra)
    return block


class FakeEntryPoint:
    """An ``importlib.metadata.EntryPoint`` stand-in: ``load`` returns ``target`` or raises."""

    def __init__(self, name: str, target: Any = None, dist: str = "acme-pack", error=None):
        self.name = name
        self.target = target
        self.error = error
        self.loads = 0
        self.dist = SimpleNamespace(name=dist, version="1.0")

    def load(self) -> Any:
        self.loads += 1
        if self.error is not None:
            raise self.error
        return self.target


@pytest.fixture(autouse=True)
def operator_policy(monkeypatch):
    """The Spark tests' operator policy, no plugin policy from the machine, a clean plugin
    cache before and after each test."""
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(SECRET_REF_ALLOWLIST_ENV, raising=False)
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example,example.internal")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    reset_maintenance_plugin_cache()
    yield
    reset_maintenance_plugin_cache()


@pytest.fixture
def entry_points(monkeypatch):
    """Install fake entry points of the maintenance group: ``entry_points(*eps)``."""
    from fluid_build import plugin_manager

    def install(*eps: FakeEntryPoint) -> None:
        def scanner(group: str):
            return list(eps) if group == "fluid_build.maintenance_actions" else []

        monkeypatch.setattr(plugin_manager, "_entry_points", scanner)
        reset_maintenance_plugin_cache()

    return install
