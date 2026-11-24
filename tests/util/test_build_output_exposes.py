# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``build_output_exposes``: the one place a build is mapped to the exposes it writes.

The local planner, the local provider, the embedded-SQL landing and the
acquisition drift gate each took ``exposes[0]`` or the last build's table, so a
second build wrote its rows into the first build's file. The rule (Dagster's
multi-asset one: outputs are names checked against the declared set) is pinned
here once.
"""

from __future__ import annotations

import pytest

from fluid_build.util.contract import BuildOutputsError, build_output_exposes

pytestmark = pytest.mark.unit


def _contract(*expose_ids: str) -> dict:
    return {"exposes": [{"exposeId": e} for e in expose_ids]}


def test_outputs_resolve_to_the_named_exposes_in_the_order_the_build_lists_them() -> None:
    contract = _contract("A", "B", "C")
    got = build_output_exposes(contract, {"id": "b", "outputs": ["C", "A"]})
    assert [e["exposeId"] for e in got] == ["C", "A"]


def test_a_repeated_output_is_one_expose() -> None:
    got = build_output_exposes(_contract("A", "B"), {"id": "b", "outputs": ["A", "A"]})
    assert [e["exposeId"] for e in got] == ["A"]


@pytest.mark.parametrize("bad", ["Z", 7, None])
def test_an_output_that_is_no_exposeid_is_refused_by_name(bad) -> None:
    with pytest.raises(BuildOutputsError) as refused:
        build_output_exposes(_contract("A", "B"), {"id": "b", "outputs": ["A", bad]})
    assert refused.value.code == "build_output_unknown_expose"
    assert "'b'" in refused.value.what
    assert refused.value.extras["exposeIds"] == ["A", "B"]


@pytest.mark.parametrize("outputs", [None, []], ids=["unset", "empty"])
def test_no_outputs_beside_several_exposes_is_refused(outputs) -> None:
    build = {"id": "b"} if outputs is None else {"id": "b", "outputs": outputs}
    with pytest.raises(BuildOutputsError) as refused:
        build_output_exposes(_contract("A", "B"), build)
    assert refused.value.code == "build_outputs_undeclared"
    assert refused.value.extras["exposeCount"] == 2


def test_a_single_expose_contract_keeps_its_expose_for_a_build_with_no_outputs() -> None:
    assert build_output_exposes(_contract("A"), {"id": "b"}) == [{"exposeId": "A"}]


def test_a_contract_with_no_exposes_resolves_to_none() -> None:
    assert build_output_exposes({}, {"id": "b"}) == []
