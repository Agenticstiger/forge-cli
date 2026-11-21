# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A prebuilt package build compiled into the job config the runner reads."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.spark import names
from fluid_build.spark.compiler import (
    compile_job_config,
    validation_errors,
    validation_warnings,
)
from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV, parse_package
from fluid_build.spark.policy import SparkPolicyError

from .package_fixtures import PACKAGE_CONTRACT, SHA, SOURCE

LISTED = "s3://commerce-artifacts/code/"


@pytest.fixture
def contract():
    return copy.deepcopy(PACKAGE_CONTRACT)


@pytest.fixture(autouse=True)
def listed(monkeypatch):
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, LISTED)


def _build(contract):
    return contract["builds"][0]


def _package(contract):
    return _build(contract)["properties"]["package"]


def _spark(contract):
    return _build(contract)["properties"]["parameters"]["spark"]


def _config(contract, **kw):
    return compile_job_config(contract, _build(contract), **kw).config


def test_the_config_carries_the_digest_the_entry_point_and_the_checks(contract):
    config = _config(contract)
    package = config["package"]
    assert config["mode"] == "batch"
    assert config["source"] is None
    assert package["sha256"] == SHA
    assert package["entryPoint"] == "orders_pkg.jobs:merge_orders"
    assert package["arguments"] == {"source": "commerce.orders_raw"}
    assert package["schema"] == (
        "`order_id` STRING, `customer` STRUCT<`id`: STRING, `email`: STRING>, "
        "`amount` DECIMAL(18,2), `order_date` DATE"
    )
    assert package["deadLetter"] == {
        "table": "commerce.orders__dead_letter",
        "maxRecordsBeforeAbort": 100,
    }
    check = config["sql"]["check"]
    assert set(config["sql"]) == {"check"}
    assert "FROM `package_output`" in check
    assert "WHEN `order_id` IS NULL THEN 'required field order_id is null'" in check
    assert "WHEN `customer` IS NULL THEN 'required field customer is null'" in check
    assert "`customer`.`id` IS NULL" in check
    assert "amount" not in check  # optional: no check


def test_the_config_never_names_where_the_team_published_it(contract):
    """The runner reads forge's own copy beside the job config, found by the digest."""
    text = compile_job_config(contract, _build(contract)).text
    assert SOURCE not in text and "commerce-artifacts" not in text


def test_the_copy_sits_beside_the_job_config_keyed_by_the_digest():
    key = names.package_key("commerce.orders-silver", "merge-orders", SHA)
    config = names.job_config_key("commerce.orders-silver", "merge-orders")
    assert key.rpartition("/package/")[0] == config.rpartition("/")[0]
    assert key.endswith(f"/package/{SHA[:16]}/package.zip")


def test_another_digest_is_another_config(contract):
    before = compile_job_config(contract, _build(contract)).digest
    _package(contract)["sha256"] = "0" * 64
    assert compile_job_config(contract, _build(contract)).digest != before


def test_without_dead_letters_the_config_has_none(contract):
    _package(contract).pop("deadLetter")
    assert _config(contract)["package"]["deadLetter"] is None
    _package(contract)["deadLetter"] = {"enabled": False, "table": "x"}
    assert _config(contract)["package"]["deadLetter"] is None


def test_the_dead_letters_take_a_table_of_their_own_name(contract):
    _package(contract)["deadLetter"] = {"table": "orders_rejects"}
    dead = _config(contract)["package"]["deadLetter"]
    assert dead == {"table": "commerce.orders_rejects", "maxRecordsBeforeAbort": 10_000}


def test_the_dead_letters_cannot_be_the_output(contract):
    _package(contract)["deadLetter"] = {"table": "orders"}
    with pytest.raises(ValueError, match="the output's own table"):
        _config(contract)


def test_an_output_column_named_like_the_runner_s_reason_is_refused(contract):
    contract["exposes"][0]["contract"]["schema"].append({"name": "FLUID_ERROR", "type": "string"})
    with pytest.raises(ValueError, match="fluid_error"):
        _config(contract)


def test_arguments_default_to_none(contract):
    _package(contract).pop("arguments")
    assert _config(contract)["package"]["arguments"] == {}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"sha256": SHA.upper()}, "64 lowercase hex"),
        ({"sha256": "sha256:" + SHA}, "64 lowercase hex"),
        ({"entryPoint": "orders_pkg.jobs"}, "<module>:<attribute>"),
        ({"entryPoint": "orders-pkg:run"}, "<module>:<attribute>"),
        ({"arguments": {"window": 1}}, "strings"),
        ({"arguments": ["x"]}, "strings"),
        ({"uri": "https://example.com/p.whl"}, "s3://"),
        ({"uri": "s3://commerce-artifacts/code/../p.whl"}, "s3://"),
        ({"uri": "s3://commerce-artifacts/code/p.tar.gz"}, "s3://"),
        ({"spare": 1}, "unsupported"),
        ({"deadLetter": {"sink": "topic"}}, "unsupported"),
        ({"deadLetter": {"maxRecordsBeforeAbort": -1}}, "whole number"),
        ({"deadLetter": {"table": "a.b"}}, "table name"),
    ],
)
def test_a_package_the_runner_cannot_run_is_refused(contract, change, message):
    _package(contract).update(change)
    with pytest.raises(ValueError, match=message):
        _config(contract)


@pytest.mark.parametrize("key", ["uri", "sha256", "entryPoint"])
def test_uri_digest_and_entry_point_are_required(contract, key):
    _package(contract).pop(key)
    with pytest.raises(ValueError, match=key):
        _config(contract)


@pytest.mark.parametrize(
    ("key", "value"),
    [("sql", "SELECT 1"), ("mapping", {"columns": []})],
)
def test_a_package_replaces_sql_and_mapping(contract, key, value):
    _build(contract)["properties"][key] = value
    with pytest.raises(ValueError, match="in place of"):
        _config(contract)


def test_a_package_build_is_batch(contract):
    _spark(contract)["mode"] = "streaming"
    with pytest.raises(ValueError, match="batch"):
        _config(contract)


def test_a_streaming_package_is_refused(contract):
    _build(contract)["properties"]["stream"] = {"kind": "kafka", "topic": "t"}
    with pytest.raises(ValueError, match="in place of"):
        _config(contract)


def test_a_package_runs_pyspark(contract):
    _build(contract)["properties"]["language"] = "scala"
    with pytest.raises(ValueError, match="pyspark"):
        _config(contract)


def test_an_aws_runtime_reads_an_s3_package(contract):
    _package(contract)["uri"] = "file:///home/dev/dist/orders_pkg.zip"
    with pytest.raises(ValueError, match="s3://"):
        _config(contract)


def test_a_local_run_reads_a_package_from_this_machine(contract, monkeypatch):
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, "file:///home/dev/dist/")
    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    _spark(contract).clear()
    _spark(contract).update({"runtime": "local"})
    binding = contract["exposes"][0]["binding"]
    binding["platform"] = "local"
    binding["location"] = {"catalog": "lakekeeper", "database": "commerce", "table": "orders"}
    _package(contract)["uri"] = "s3://commerce-artifacts/code/orders_pkg.whl"
    with pytest.raises(ValueError, match="file:///"):
        _config(contract)
    _package(contract)["uri"] = "file:///home/dev/dist/orders_pkg.zip"
    config = _config(contract)
    dead = config["package"]["deadLetter"]
    # Nothing creates a local run's tables but the job: the output's and the dead letters'.
    assert config["sink"]["create"] and dead["create"]
    assert any("`orders__dead_letter`" in s for s in dead["create"])


# --- the operator's list -------------------------------------------------------------


def test_an_unlisted_package_is_refused(contract, monkeypatch):
    monkeypatch.delenv(PACKAGE_ALLOWLIST_ENV)
    with pytest.raises(SparkPolicyError) as caught:
        _config(contract)
    message = caught.value.as_json()
    assert PACKAGE_ALLOWLIST_ENV in message
    assert "merge-orders" in message and "commerce.orders-silver" in caught.value.what
    # The store every release sits under, never this release's own folder.
    assert caught.value.fix.endswith(
        'export FLUID_SPARK_PACKAGE_ALLOWLIST="s3://commerce-artifacts/code/orders/".'
    )


@pytest.mark.parametrize(
    ("uri", "prefix"),
    [
        (SOURCE, "s3://commerce-artifacts/code/orders/"),
        ("s3://b/code/orders/1.4.0/orders.whl", "s3://b/code/orders/1.4.0/"),
        (f"s3://b/{SHA}/orders.whl", "s3://b/"),  # a store keyed by digest at its root
        ("file:///home/dev/dist/orders.zip", "file:///home/dev/dist/"),
        (f"file:///srv/code/{SHA[:40]}/orders.zip", "file:///srv/code/"),
    ],
)
def test_the_suggested_entry_is_the_store_above_any_digest(uri, prefix):
    from fluid_build.spark.package import store_prefix

    assert store_prefix(uri) == prefix


@pytest.mark.parametrize(
    ("listed", "allowed"),
    [
        ("s3://commerce-artifacts/code/", True),
        ("s3://commerce-artifacts/code", True),  # a whole segment
        ("s3://commerce-artifacts/", True),
        (SOURCE, True),  # the object itself
        ("s3://commerce-artifacts/cod", False),  # never part of a segment
        ("s3://commerce-artifacts-2/code/", False),
        ("s3://other/,s3://commerce-artifacts/code/", True),
        ("", False),
        (" , ", False),
    ],
)
def test_the_list_names_whole_prefixes(contract, monkeypatch, listed, allowed):
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, listed)
    if allowed:
        _config(contract)
    else:
        with pytest.raises(SparkPolicyError):
            _config(contract)


def test_the_list_is_the_operator_s_alone():
    """A project's .env cannot set it (credentials/protected_env.py)."""
    from fluid_build.credentials.protected_env import is_protected_env_key

    assert is_protected_env_key(PACKAGE_ALLOWLIST_ENV)


def test_the_gate_is_left_to_the_caller_when_asked(contract, monkeypatch):
    monkeypatch.delenv(PACKAGE_ALLOWLIST_ENV)
    assert _config(contract, contract_sql_gate=False)["package"]["sha256"] == SHA


def test_validate_accepts_the_build_and_warns_until_it_is_listed(contract, monkeypatch):
    assert validation_errors(contract) == []
    assert validation_warnings(contract) == []
    monkeypatch.delenv(PACKAGE_ALLOWLIST_ENV)
    (warning,) = validation_warnings(contract)
    assert PACKAGE_ALLOWLIST_ENV in warning and "merge-orders" in warning


def test_validate_reports_a_package_it_cannot_compile(contract):
    _package(contract)["sha256"] = "x"
    (error,) = validation_errors(contract)
    assert "64 lowercase hex" in error


def test_parse_reads_the_declared_package(contract):
    spec = parse_package(_package(contract), "emr-ec2", "Spark build merge-orders")
    assert spec.uri == SOURCE and spec.sha256 == SHA
    assert spec.bucket == "commerce-artifacts"
    assert spec.key == SOURCE.removeprefix("s3://commerce-artifacts/")


def test_builds_without_a_package_compile_as_before(contract):
    """A SQL batch build's config gains no key: its digest, and every running build's
    config hash, stays what it was."""
    from .conftest import CONTRACT

    stream = copy.deepcopy(CONTRACT)
    config = compile_job_config(stream, stream["builds"][0]).config
    assert "package" not in config and set(config["sql"]) == {"source", "select"}
    assert json.loads(json.dumps(config)) == config
