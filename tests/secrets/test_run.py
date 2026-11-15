# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run a secret belongs to: nesting, cleanups and the throwaway lookup state."""

from __future__ import annotations

import pytest

from fluid_build.secrets import run as run_mod
from fluid_build.secrets.run import (
    RunState,
    active_state,
    current_stage,
    secret_run,
    state_for_lookup,
)


@pytest.mark.unit
def test_no_stage_outside_a_run():
    assert current_stage() is None
    assert active_state() is None


@pytest.mark.unit
def test_stage_name_is_normalised():
    with secret_run("  Policy_Apply "):
        assert current_stage() == "policy-apply"
    assert current_stage() is None


@pytest.mark.unit
def test_nested_runs_share_state_and_innermost_stage_is_current():
    with secret_run("apply") as outer:
        with secret_run("plan") as inner:
            assert inner is outer
            assert current_stage() == "plan"
            assert active_state() is outer
        assert current_stage() == "apply"
        assert active_state() is outer
    assert active_state() is None


@pytest.mark.unit
def test_inner_exit_does_not_close_the_outer_run():
    ran = []
    with secret_run("apply") as state:
        state.cache["k"] = "v"
        state.on_close(lambda: ran.append("x"))
        with secret_run("plan"):
            pass
        assert ran == []
        assert state.cache == {"k": "v"}
    assert ran == ["x"]


@pytest.mark.unit
def test_outermost_exit_runs_cleanups_newest_first_and_clears_state():
    order = []
    with secret_run("apply") as state:
        state.cache["a"] = object()
        state.scratch["t"] = "token"
        state.handed = {"x": object()}
        state.on_close(lambda: order.append(1))
        state.on_close(lambda: order.append(2))
        state.on_close(lambda: order.append(3))
    assert order == [3, 2, 1]
    assert state.cache == {}
    assert state.scratch == {}
    assert state.handed is None


@pytest.mark.unit
def test_failing_cleanup_does_not_raise_and_others_still_run():
    ran = []

    def boom():
        raise RuntimeError("revoke failed")

    with secret_run("apply") as state:
        state.on_close(lambda: ran.append("first"))
        state.on_close(boom)
        state.on_close(lambda: ran.append("last"))
    assert ran == ["last", "first"]
    assert state.cache == {}


@pytest.mark.unit
def test_cleanups_run_when_the_body_raises():
    ran = []
    with pytest.raises(ValueError):
        with secret_run("apply") as state:
            state.on_close(lambda: ran.append("done"))
            raise ValueError("stage failed")
    assert ran == ["done"]
    assert current_stage() is None
    assert run_mod._STATE is None


@pytest.mark.unit
def test_close_is_idempotent():
    state = RunState()
    ran = []
    state.on_close(lambda: ran.append(1))
    state.close()
    state.close()
    assert ran == [1]


@pytest.mark.unit
def test_new_run_gets_a_fresh_state():
    with secret_run("apply") as first:
        first.cache["k"] = "v"
    with secret_run("apply") as second:
        assert second is not first
        assert second.cache == {}


@pytest.mark.unit
def test_lookups_outside_a_run_share_one_process_state_closed_at_exit():
    from fluid_build.secrets import run

    ran = []
    with state_for_lookup() as first:
        assert isinstance(first, RunState)
        assert active_state() is None
        assert first.keep_values is False  # it never holds a value
        first.on_close(lambda: ran.append(1))
    with state_for_lookup() as second:
        assert second is first  # a minted token is reused, not minted again
    assert ran == []
    run._close_orphan()  # what atexit runs
    assert ran == [1]
    with state_for_lookup() as third:
        assert third is not first


@pytest.mark.unit
def test_state_for_lookup_inside_a_run_is_the_runs_state():
    ran = []
    with secret_run("apply") as run_state:
        with state_for_lookup() as state:
            assert state is run_state
            state.on_close(lambda: ran.append(1))
        assert ran == []
    assert ran == [1]
