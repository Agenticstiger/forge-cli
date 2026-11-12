# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``providers/databricks/statements.py``: rows from a SQL warehouse over
``databricks_enforce.SqlRunner`` (request shape with parameters, polling,
timeout/cancel, failure scrubbing, type conversion, chunk paging), against a fake
``api_client.do``."""

from __future__ import annotations

import pytest

from fluid_build.iac.providers.databricks_enforce import StatementFailed
from fluid_build.providers.databricks.statements import (
    STATEMENTS_PATH,
    StatementError,
    StatementRunner,
    param,
    warehouse_id_from_env,
)

FAKE_TOKEN = "fake-test-token-0000"  # pragma: allowlist secret


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def do(self, method, path, query=None, body=None, **kw):
        self.calls.append((method, path, body))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.api_client = FakeApi(responses)


def ok(rows=None, columns=(), **result_extra):
    return {
        "statement_id": "sid-1",
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": n, "type_name": t} for n, t in columns]}},
        "result": {"chunk_index": 0, "data_array": rows or [], **result_extra},
    }


def running(state="RUNNING"):
    return {"statement_id": "sid-1", "status": {"state": state}}


def runner(responses, **kw):
    client = FakeClient(responses)
    kw.setdefault("sleep", lambda s: None)
    return StatementRunner(client, "wh-1", **kw), client.api_client


def test_statement_error_is_the_enforce_runner_failure():
    assert StatementError is StatementFailed


def test_post_body_without_parameters():
    r, api = runner([ok([["1"]], [("one", "INT")])])
    assert r.run("SELECT 1") == [(1,)]
    method, path, body = api.calls[0]
    assert (method, path.rstrip("/")) == ("POST", STATEMENTS_PATH)
    assert body == {
        "warehouse_id": "wh-1",
        "statement": "SELECT 1",
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
    }


def test_parameters_are_bound_not_spliced():
    r, api = runner([ok()])
    r.run("SELECT :v", [param("v", "x'; DROP TABLE t; --")])
    body = api.calls[0][2]
    assert body["statement"] == "SELECT :v"
    assert body["parameters"] == [{"name": "v", "type": "STRING", "value": "x'; DROP TABLE t; --"}]


def test_live_response_shape_is_converted_by_type_name():
    # The shape a Free Edition warehouse returned for
    # SELECT 1.5 AS d, CAST(2 AS BIGINT) AS l, 'x' AS s, true AS b
    live = ok(
        [["1.5", "2", "x", "true"]],
        [("d", "DECIMAL"), ("l", "LONG"), ("s", "STRING"), ("b", "BOOLEAN")],
        row_count=1,
        row_offset=0,
    )
    r, _ = runner([live])
    assert r.run("SELECT ...") == [(1.5, 2, "x", True)]


def test_null_stays_none_and_false_boolean_and_odd_value_kept():
    r, _ = runner([ok([[None, "false", "n/a"]], [("a", "LONG"), ("b", "BOOLEAN"), ("c", "INT")])])
    assert r.run("q") == [(None, False, "n/a")]


def test_pending_then_running_then_succeeded_polls_get():
    r, api = runner([running("PENDING"), running(), ok([["7"]], [("n", "LONG")])])
    assert r.run("q") == [(7,)]
    assert [c[0] for c in api.calls] == ["POST", "GET", "GET"]
    assert api.calls[1][1] == f"{STATEMENTS_PATH}/sid-1"


def test_timeout_cancels_then_raises():
    pending = [running("PENDING")] * 50
    r, api = runner(pending + [{}], timeout_seconds=0)
    with pytest.raises(StatementError, match="did not finish"):
        r.run("q")
    assert any(c[0] == "POST" and c[1].endswith("/sid-1/cancel") for c in api.calls)


def test_failed_state_raises_with_scrubbed_api_message(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)
    failed = {
        "statement_id": "sid-1",
        "status": {
            "state": "FAILED",
            "error": {"message": f"[TABLE_OR_VIEW_NOT_FOUND] t ({FAKE_TOKEN})"},
        },
    }
    r, _ = runner([failed])
    with pytest.raises(StatementError) as exc:
        r.run("q")
    assert "TABLE_OR_VIEW_NOT_FOUND" in str(exc.value)
    assert FAKE_TOKEN not in str(exc.value)


def test_transport_exception_is_wrapped_and_scrubbed(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)
    r, _ = runner([RuntimeError(f"connection reset, token {FAKE_TOKEN}")])
    with pytest.raises(StatementError) as exc:
        r.run("q")
    assert "RuntimeError" in str(exc.value)
    assert FAKE_TOKEN not in str(exc.value)


def test_canceled_state_raises():
    r, _ = runner([{"statement_id": "sid-1", "status": {"state": "CANCELED"}}])
    with pytest.raises(StatementError, match="CANCELED"):
        r.run("q")


def test_next_chunk_index_fetches_further_chunks():
    first = ok([["1"]], [("n", "LONG")], next_chunk_index=1)
    second = {"chunk_index": 1, "data_array": [["2"], ["3"]]}
    r, api = runner([first, second])
    assert r.run("q") == [(1,), (2,), (3,)]
    assert api.calls[1] == ("GET", f"{STATEMENTS_PATH}/sid-1/result/chunks/1", None)


def test_chunk_fetch_failure_is_a_statement_error(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)
    first = ok([["1"]], [("n", "LONG")], next_chunk_index=1)
    r, _ = runner([first, RuntimeError(f"chunk gone {FAKE_TOKEN}")])
    with pytest.raises(StatementError) as exc:
        r.run("q")
    assert FAKE_TOKEN not in str(exc.value)


def test_param_omits_value_for_none_and_takes_type():
    assert param("n", None) == {"name": "n", "type": "STRING"}
    assert param("b", True, "BOOLEAN") == {"name": "b", "type": "BOOLEAN", "value": "True"}


def test_warehouse_id_from_env_unset_or_blank_names_the_variable():
    for env in ({}, {"DATABRICKS_WAREHOUSE_ID": "   "}):
        with pytest.raises(StatementError, match="DATABRICKS_WAREHOUSE_ID"):
            warehouse_id_from_env(env)


def test_warehouse_id_from_env_trims_and_reads_os_environ(monkeypatch):
    assert warehouse_id_from_env({"DATABRICKS_WAREHOUSE_ID": " wh "}) == "wh"
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "from-env")
    assert warehouse_id_from_env() == "from-env"


# ------------------------------------------------------- circuit breaker


def test_timeout_stops_the_statements_after_it_without_an_api_call():
    r, api = runner([running("PENDING")] * 50 + [{}], timeout_seconds=0)
    with pytest.raises(StatementError, match="did not finish"):
        r.run("q1")
    calls = len(api.calls)
    with pytest.raises(StatementError) as exc:
        r.run("q2")
    assert str(exc.value).startswith("not run:")
    assert len(api.calls) == calls


def test_transport_failure_stops_the_statements_after_it_without_an_api_call():
    r, api = runner([RuntimeError("connection reset")])
    with pytest.raises(StatementError, match="RuntimeError"):
        r.run("q1")
    with pytest.raises(StatementError) as exc:
        r.run("q2")
    assert str(exc.value).startswith("not run:")
    assert len(api.calls) == 1


def test_a_sql_error_does_not_stop_the_statements_after_it():
    failed = {
        "statement_id": "sid-1",
        "status": {"state": "FAILED", "error": {"message": "[TABLE_OR_VIEW_NOT_FOUND] t"}},
    }
    r, api = runner([failed, ok([["7"]], [("n", "LONG")])])
    with pytest.raises(StatementError) as first:
        r.run("q1")
    assert not str(first.value).startswith("not run:")
    assert r.run("q2") == [(7,)]
    assert len([c for c in api.calls if c[0] == "POST"]) == 2
