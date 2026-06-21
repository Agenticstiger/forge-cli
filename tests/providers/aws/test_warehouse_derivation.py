# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""PR1 — the native planner and the OpenTofu emitter must derive the SAME
Iceberg warehouse ``s3://`` location for the same binding.

This is the RED-then-GREEN divergence test for RFC-streaming-extension §7. It is
*expected to FAIL on current main* for the leading-slash, env-template, and
no-bucket cases (the three sites diverge today) and to pass once a single
``get_iceberg_warehouse`` writer is wired into both paths.
"""

from __future__ import annotations

import logging

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.providers.aws.plan.planner import _plan_exposures

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCT = "123456789012"
# The account placeholder the credential-free IaC path uses at apply time.
TOKEN = "${data.aws_caller_identity.fluid_lf_caller.account_id}"


def _contract(location):
    return {
        "id": "analytics.lake",
        "name": "Lake",
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": location,
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "string"},
                        {"name": "qty", "type": "integer"},
                    ]
                },
            }
        ],
    }


def _planner_location(location):
    acts = _plan_exposures(_contract(location), ACCT, "us-east-1", logging.getLogger("wh"))
    return next(a["location"] for a in acts if a.get("op") == "glue.ensure_iceberg_table")


def _iac_location(location):
    res = get_iac_plugin("aws").emit(_contract(location))
    tables = res.get("aws_glue_catalog_table") or {}
    if not tables:
        return None
    tbl = next(iter(tables.values()))
    loc = (tbl.get("storage_descriptor") or {}).get("location")
    return None if loc is None else str(loc)


def _acct_norm(s):
    """Logical warehouse — the literal account id and the apply-time
    ``aws_caller_identity`` token are equivalent (same account at apply)."""
    if s is None:
        return None
    return s.replace(TOKEN, "<ACCT>").replace(ACCT, "<ACCT>")


@pytest.mark.parametrize(
    "location, why",
    [
        (
            {"database": "sales", "table": "orders", "bucket": "lake", "path": "sales/orders/"},
            "control: explicit bucket, no leading slash — already agrees",
        ),
        (
            {"database": "sales", "table": "orders", "bucket": "lake", "path": "/sales/orders/"},
            "leading-slash path: planner emits s3://lake//... (double slash)",
        ),
    ],
)
def test_explicit_bucket_byte_identical(location, why):
    planner = _planner_location(location)
    iac = _iac_location(location)
    assert iac is not None, f"{why}: IaC emitted no location"
    assert planner == iac, f"{why}\n  planner={planner!r}\n  iac    ={iac!r}"


def test_env_template_bucket_resolves_consistently(monkeypatch):
    monkeypatch.setenv("FLUID_TEST_WH_BUCKET", "resolved-lake")
    location = {
        "database": "sales",
        "table": "orders",
        "bucket": "{{ env.FLUID_TEST_WH_BUCKET }}",
        "path": "sales/orders/",
    }
    planner = _planner_location(location)
    iac = _iac_location(location)
    assert iac is not None, "IaC emitted no location for env-template bucket"
    assert planner == iac, f"planner={planner!r} iac={iac!r}"


def test_no_bucket_account_fallback_agrees():
    location = {"database": "sales", "table": "orders"}  # no bucket
    planner = _planner_location(location)
    iac = _iac_location(location)
    assert iac is not None, "IaC emitted no location for bucket-less binding"
    assert _acct_norm(planner) == _acct_norm(iac), f"planner={planner!r} iac={iac!r}"


def test_iac_account_fallback_is_tofuexpr_and_unescaped():
    """Bucket-less IaC warehouse uses the apply-time account token: a TofuExpr
    (so ``${...}`` survives escaping) backed by an emitted caller-identity
    data source."""
    from fluid_build.iac import build_module, get_iac_plugin
    from fluid_build.iac.naming import TofuExpr

    contract = _contract({"database": "sales", "table": "orders"})  # no bucket
    plugin = get_iac_plugin("aws")
    tbl = next(iter(plugin.emit(contract)["aws_glue_catalog_table"].values()))
    loc = tbl["storage_descriptor"]["location"]
    assert isinstance(loc, TofuExpr)
    assert "aws_caller_identity" in loc
    # the backing data source is emitted so the token resolves at apply
    assert "fluid_lf_caller" in plugin.emit_data(contract).get("aws_caller_identity", {})
    # rendered .tf.json leaves the interpolation un-escaped (no `$${`)
    rendered = build_module(plugin, contract)
    assert "${data.aws_caller_identity.fluid_lf_caller.account_id}" in rendered
    assert "$${" not in rendered


def test_explicit_bucket_byte_stable_no_extra_data_source():
    """The digest-scoping boundary: an explicit-bucket binding stays a plain
    literal and emits NO caller-identity data source — so its main.tf.json
    (and bundle/plan digests) are unchanged by PR1."""
    from fluid_build.iac import get_iac_plugin
    from fluid_build.iac.naming import TofuExpr

    contract = _contract({"database": "sales", "table": "orders", "bucket": "lake"})
    plugin = get_iac_plugin("aws")
    tbl = next(iter(plugin.emit(contract)["aws_glue_catalog_table"].values()))
    loc = tbl["storage_descriptor"]["location"]
    assert loc == "s3://lake/sales/orders/"
    assert not isinstance(loc, TofuExpr)
    assert "aws_caller_identity" not in plugin.emit_data(contract)


def test_same_warehouse_logical_equality():
    from fluid_build.providers.aws.util.warehouse import same_warehouse

    literal = f"s3://{ACCT}-fluid-data/sales/orders/"
    token = f"s3://{TOKEN}-fluid-data/sales/orders"  # no trailing slash + token
    assert same_warehouse(literal, token, account_id=ACCT)
    assert not same_warehouse(literal, "s3://other/sales/orders/", account_id=ACCT)
    assert same_warehouse(None, None)


def _has_raw_injection(rendered, payload="${file("):
    """True if a raw (un-escaped) OpenTofu interpolation payload survived into
    the rendered .tf.json. Collapses the escaped form ($${ -> '') first so only
    a genuinely un-escaped ${file( is detected."""
    collapsed = rendered.replace("$${", "").replace("%%{", "")
    return payload in collapsed


@pytest.mark.parametrize(
    "location, why",
    [
        (
            {"database": "s", "table": "t", "bucket": '${file("/etc/passwd")}'},
            "via bucket (explicit)",
        ),
        (
            {"database": "s", "table": "t", "bucket": "lake", "path": '${file("/x")}'},
            "via path (explicit bucket)",
        ),
        (
            {"database": "s", "table": '${file("/y")}'},
            "via table on the no-bucket fallback (TofuExpr branch)",
        ),
    ],
)
def test_contract_cannot_inject_tofu_interpolation(location, why):
    """SECURITY: a malicious ${...} in any contract-derived location field must
    render ESCAPED ($${), never as a live OpenTofu expression. Mirrors the
    Snowflake plugin's test_contract_strings_cannot_inject_tofu_interpolation."""
    from fluid_build.iac import build_module, get_iac_plugin

    rendered = build_module(get_iac_plugin("aws"), _contract(location))
    assert not _has_raw_injection(rendered), f"{why}: un-escaped ${{file( reached the module"
    assert "$${file(" in rendered, f"{why}: payload was not escaped as expected"
    # the emitter's own apply-time account token (a deliberate TofuExpr) must
    # still interpolate on the fallback path — escaping must not neutralise it.
    if not location.get("bucket"):
        assert "${data.aws_caller_identity.fluid_lf_caller.account_id}" in rendered
