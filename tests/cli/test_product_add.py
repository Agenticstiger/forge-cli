# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Regression tests for ``fluid product-add``.

Pins the bug where product-add appended top-level ``sources`` / ``exposures``
/ ``dataQuality`` arrays that don't exist in the (closed-root) FLUID schema, so
a previously-valid contract failed ``fluid validate`` afterward. The fix writes
to the canonical homes (``consumes[]`` / ``exposes[]`` /
``exposes[].contract.dq.rules[]``) — these tests assert the output stays valid.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import fluid_build
from fluid_build.cli import product_add
from fluid_build.loader import _parse_file
from fluid_build.schema_manager import FluidSchemaManager

_TEMPLATE = (
    Path(fluid_build.__file__).resolve().parent
    / "templates"
    / "customer-360"
    / "contract.fluid.yaml"
)


def _base_contract() -> dict:
    """A known-valid contract (the bundled customer-360 template) at its native
    fluidVersion — the realistic "existing contract" case. Validating at the
    contract's own (older) version is the strictest check: it catches additions
    that use newer-only optional keys."""
    return _parse_file(_TEMPLATE)


def _args(**kw) -> SimpleNamespace:
    defaults = dict(
        id=None,
        description=None,
        type=None,
        location=None,
        platform=None,
        expose=None,
        severity=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _validate(contract: dict):
    return FluidSchemaManager().validate_contract(contract)


def test_baseline_template_is_valid() -> None:
    assert _validate(_base_contract()).is_valid


def test_add_exposure_stays_valid_and_canonical() -> None:
    c = _base_contract()
    product_add._add_exposure(
        c, _args(id="orders_view", type="view", location="output/orders.parquet")
    )
    res = _validate(c)
    assert res.is_valid, f"exposure add must validate; result={vars(res)}"
    assert any(e["exposeId"] == "orders_view" for e in c["exposes"])
    assert "exposures" not in c  # not the schema-invalid top-level key


def test_add_source_writes_consumes_ref() -> None:
    c = _base_contract()
    product_add._add_source(
        c, _args(id="upstream.crm", location="customers_table", description="raw CRM")
    )
    res = _validate(c)
    assert res.is_valid, f"source add must validate; result={vars(res)}"
    consume = next(x for x in c["consumes"] if x["productId"] == "upstream.crm")
    assert consume["exposeId"] == "customers_table"
    assert consume["purpose"] == "raw CRM"
    assert "sources" not in c


def test_add_dq_rule_attaches_to_expose() -> None:
    c = _base_contract()
    product_add._add_dq_check(c, _args(id="fresh_orders", type="freshness"))
    res = _validate(c)
    assert res.is_valid, f"dq add must validate; result={vars(res)}"
    rule = next(r for r in c["exposes"][0]["contract"]["dq"]["rules"] if r["id"] == "fresh_orders")
    assert rule == {"id": "fresh_orders", "type": "freshness", "severity": "warn"}
    assert "dataQuality" not in c


def test_all_three_together_stay_valid() -> None:
    c = _base_contract()
    product_add._add_exposure(c, _args(id="orders_view", type="view"))
    product_add._add_dq_check(c, _args(id="fresh", type="freshness", severity="error"))
    product_add._add_source(c, _args(id="up.crm", location="customers"))
    res = _validate(c)
    assert res.is_valid, f"combined product-add must validate; result={vars(res)}"
    assert not any(k in c for k in ("sources", "exposures", "dataQuality"))


def test_invalid_type_falls_back_to_safe_default() -> None:
    c = _base_contract()
    # legacy/invalid values must not break schema validity
    product_add._add_exposure(c, _args(id="legacy_exp", type="dashboard"))
    product_add._add_dq_check(c, _args(id="legacy_dq", type="quality"))
    res = _validate(c)
    assert res.is_valid, f"invalid types must fall back; result={vars(res)}"
    exp = next(e for e in c["exposes"] if e["exposeId"] == "legacy_exp")
    assert exp["kind"] == "table"  # invalid 'dashboard' -> default
    assert c["exposes"][0]["contract"]["dq"]["rules"][-1]["type"] == "completeness"


def test_dq_requires_an_expose() -> None:
    from fluid_build.cli._common import CLIError

    c = _base_contract()
    c["exposes"] = []
    try:
        product_add._add_dq_check(c, _args(id="x", type="freshness"))
        raise AssertionError("expected CLIError when no expose exists")
    except CLIError:
        pass
