# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The stage gate through the real ``traced_stage`` decorator."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build.observability.tracing import traced_stage
from fluid_build.secrets import resolve_secret
from fluid_build.secrets.errors import SecretPolicyError
from fluid_build.secrets.run import active_state, current_stage

VALUE = "Rt5Yu9-Io3PaSd7_stage"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FLUID_TEST_STAGE_TOKEN", VALUE)


def _resolver_stage(name, seen):
    @traced_stage(name)
    def run(args):
        seen.append(current_stage())
        seen.append(resolve_secret("env://FLUID_TEST_STAGE_TOKEN", unsent=True).get_secret_value())
        return 0

    return run


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["validate", "plan", "bundle", "diff"])
def test_lint_stages_cannot_resolve(stage):
    seen = []
    with pytest.raises(SecretPolicyError) as info:
        _resolver_stage(stage, seen)(SimpleNamespace())
    assert seen == [stage]
    assert "never fetches" in info.value.what
    assert current_stage() is None


@pytest.mark.unit
def test_apply_resolves():
    seen = []
    assert _resolver_stage("apply", seen)(SimpleNamespace()) == 0
    assert seen == ["apply", VALUE]
    assert current_stage() is None
    assert active_state() is None


@pytest.mark.unit
def test_stage_not_in_the_operator_list_cannot_resolve():
    seen = []
    with pytest.raises(SecretPolicyError):
        _resolver_stage("verify", seen)(SimpleNamespace())


@pytest.mark.unit
def test_operator_can_add_a_stage(monkeypatch):
    monkeypatch.setenv("FLUID_SECRET_STAGES", "apply,policy-apply")
    seen = []
    assert _resolver_stage("policy-apply", seen)(SimpleNamespace()) == 0
    assert seen[-1] == VALUE


@pytest.mark.unit
def test_cleanups_registered_during_apply_run_when_it_returns():
    order = []

    @traced_stage("apply")
    def run(args):
        state = active_state()
        state.on_close(lambda: order.append("first"))
        state.on_close(lambda: order.append("second"))
        state.cache["k"] = "v"
        order.append("body")
        return 0

    assert run(SimpleNamespace()) == 0
    assert order == ["body", "second", "first"]
    assert active_state() is None


@pytest.mark.unit
def test_cleanups_run_when_the_stage_raises():
    order = []

    @traced_stage("apply")
    def run(args):
        active_state().on_close(lambda: order.append("cleanup"))
        raise RuntimeError("stage failed")

    with pytest.raises(RuntimeError):
        run(SimpleNamespace())
    assert order == ["cleanup"]
    assert current_stage() is None


@pytest.mark.unit
def test_a_stage_calling_another_shares_the_run_and_innermost_name_gates():
    inner_seen = []

    @traced_stage("plan")
    def inner(args):
        inner_seen.append(current_stage())
        resolve_secret("env://FLUID_TEST_STAGE_TOKEN", unsent=True)
        return 0

    @traced_stage("apply")
    def outer(args):
        resolve_secret("env://FLUID_TEST_STAGE_TOKEN", unsent=True)
        return inner(args)

    with pytest.raises(SecretPolicyError):
        outer(SimpleNamespace())
    assert inner_seen == ["plan"]
