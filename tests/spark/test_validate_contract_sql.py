# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid validate`` warns about a Spark build's own SQL only while apply would refuse it.

The warning used to be printed for every build with ``properties.sql``, listed or not, so
``fluid validate --strict`` (stage 2 of ``fluid ship``) failed every Spark SQL batch even
after the operator listed it. It now asks the rule apply uses
(``spark/policy.py::refuse_unless_contract_sql_allowed``), under the same environment.
"""

from __future__ import annotations

import copy
import logging

import pytest
import yaml

import fluid_build.cli as cli_mod
from fluid_build.cli.validate import run as validate_run
from fluid_build.iac.providers.aws_identity import EXECUTION_ROLE_ALLOWLIST_ENV
from fluid_build.spark.compiler import (
    CONTRACT_SQL_ALLOWLIST_ENV,
    TARGET_ALLOWLIST_ENV,
    validation_warnings,
)
from fluid_build.spark.policy import SparkPolicyError, refuse_unless_contract_sql_allowed
from fluid_build.streaming.contract_sql import contract_sql_entry
from tests.spark.conftest import CONTRACT
from tests.spark.test_compiler import _sql_build

LOG = logging.getLogger("test.spark.validate_contract_sql")
SQL = "SELECT order_id, sku FROM commerce.order_lines"
#: A batch job that reads catalog tables runs as a role the operator scoped to them.
ROLE = "arn:aws:iam::000000000005:role/commerce-orders-batch"
PRODUCT, BUILD = CONTRACT["id"], CONTRACT["builds"][0]["id"]
PINNED = contract_sql_entry(PRODUCT, {"id": BUILD, "properties": {"sql": SQL}})
OTHER_SQL = contract_sql_entry(PRODUCT, {"id": BUILD, "properties": {"sql": "SELECT 1"}})

CASES = [
    pytest.param(None, "unlisted", id="unset"),
    pytest.param("other.product", "unlisted", id="another-product"),
    pytest.param(OTHER_SQL, "stale", id="pinned-to-other-sql"),
    pytest.param(PINNED, None, id="pinned-to-this-sql"),
    pytest.param(f"other.product, {PINNED}", None, id="pinned-in-a-list"),
    pytest.param(f"{PRODUCT}/{BUILD}", None, id="the-build"),
    pytest.param(PRODUCT, None, id="the-product"),
]


def sql_contract():
    """The compiler tests' batch build that runs its own SQL, as the operator scoped it."""
    contract = copy.deepcopy(CONTRACT)
    _sql_build(contract, SQL)["properties"]["parameters"]["spark"]["executionRoleArn"] = ROLE
    return contract


@pytest.mark.parametrize(("entries", "warning"), CASES)
def test_validate_warns_exactly_when_apply_refuses(monkeypatch, entries, warning):
    contract = sql_contract()
    if entries is not None:  # conftest unsets the list for every test
        monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, entries)
    sql_warnings = [w for w in validation_warnings(contract) if "properties.sql" in w]
    try:
        refuse_unless_contract_sql_allowed(PRODUCT, contract["builds"][0])
        refused = False
    except SparkPolicyError:
        refused = True
    assert bool(sql_warnings) == refused == (warning is not None)
    if warning:
        (text,) = sql_warnings
        assert text.startswith(f"Build {BUILD}: properties.sql is run only if the operator lists")
        assert CONTRACT_SQL_ALLOWLIST_ENV in text
        assert ("approves other SQL" in text) == (warning == "stale")


@pytest.mark.parametrize(("entries", "warning"), CASES)
def test_validate_strict_fails_only_while_the_build_is_not_listed(
    entries, warning, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))  # bundled schemas, never a stale cache
    # Everything else the build needs is listed: the SQL entry is what --strict judges.
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, "emr-eks:abc")
    monkeypatch.setenv(EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    if entries is not None:
        monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, entries)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(sql_contract(), sort_keys=False), encoding="utf-8")
    argv = ["validate", str(path), "--offline", "--strict"]
    rc = validate_run(cli_mod.build_parser().parse_args(argv), LOG)
    out = capsys.readouterr().out
    assert rc == (1 if warning else 0), out
    assert ("properties.sql is run only if" in " ".join(out.split())) == bool(warning)
