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


# --------------------------------------------------------------------------
# Write-back behaviour.
#
# ``run()`` used to redirect YAML input to a sibling ``.json``: the file the
# user named was never touched, so the *next* invocation re-read the untouched
# YAML and rebuilt the JSON from scratch — two ``product-add`` calls kept only
# the second item. It also printed nothing at all (the only success signal was
# an ``info()`` event, routed at DEBUG for the console handler).
# --------------------------------------------------------------------------


def _run_args(contract: str, what: str, **kw):
    import logging

    defaults = dict(contract=contract, what=what)
    defaults.update(kw)
    return product_add.run(_args(**defaults), logging.getLogger("test_product_add"))


def test_yaml_input_is_written_back_in_place(tmp_path, capsys) -> None:
    import yaml

    path = tmp_path / "c.fluid.yaml"
    path.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

    assert _run_args(str(path), "source", id="up_a", location="exp_a") == 0

    assert not (tmp_path / "c.fluid.json").exists(), "must not write an unannounced sibling file"
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert {"productId": "up_a", "exposeId": "exp_a"} in written["consumes"]
    assert "up_a" in capsys.readouterr().out, "success must be visible on stdout"


def test_sequential_calls_accumulate_on_yaml(tmp_path) -> None:
    path = tmp_path / "c.fluid.yaml"
    path.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

    _run_args(str(path), "source", id="up_a", location="exp_a")
    _run_args(str(path), "source", id="up_b", location="exp_b")

    consumes = _parse_file(path)["consumes"]
    pairs = {(c["productId"], c["exposeId"]) for c in consumes}
    assert ("up_a", "exp_a") in pairs, "the first addition must survive the second call"
    assert ("up_b", "exp_b") in pairs


def test_json_input_still_round_trips_as_json(tmp_path) -> None:
    import json

    path = tmp_path / "c.fluid.json"
    path.write_text(json.dumps(_base_contract()), encoding="utf-8")

    _run_args(str(path), "source", id="up_a", location="exp_a")
    _run_args(str(path), "source", id="up_b", location="exp_b")

    consumes = json.loads(path.read_text(encoding="utf-8"))["consumes"]
    pairs = {(c["productId"], c["exposeId"]) for c in consumes}
    assert {("up_a", "exp_a"), ("up_b", "exp_b")} <= pairs


def test_the_rewritten_yaml_still_validates(tmp_path) -> None:
    path = tmp_path / "c.fluid.yaml"
    path.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

    _run_args(str(path), "exposure", id="orders_view", type="view")
    _run_args(str(path), "dq", id="fresh", type="freshness", expose="orders_view")

    assert _validate(_parse_file(path)).is_valid


def test_comment_loss_is_announced_not_silent(tmp_path, capsys) -> None:
    """PyYAML is not a round-trip loader — say so rather than dropping them."""
    path = tmp_path / "c.fluid.yaml"
    path.write_text(
        "# owner: platform team\n" + _TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    _run_args(str(path), "source", id="up_a", location="exp_a")

    out = capsys.readouterr().out
    assert "comments" in out.lower()
