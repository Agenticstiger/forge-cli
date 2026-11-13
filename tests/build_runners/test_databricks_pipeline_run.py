# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply --mode amend-and-build`` starts a Lakeflow pipeline update now and waits for it.

Driven through ``build_runners/_databricks_pipeline_run.py`` with an in-memory fake of the
Databricks REST API (the shape ``client.api_client.do`` has) and a fake clock: no network,
no SDK. Pinned: which builds and state resources count, the update's start body (full refresh
under ``replace-and-build``), the states and per-rule data quality it prints, the table and
column tags it sets, changes and removes after a completed update (and not after a failed
one), the capped wait and Ctrl-C that stop the pipeline, and the build runner's and
``fluid apply``'s hand-over of the pipelines the state holds.
"""

from __future__ import annotations

import argparse
import copy
import re
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.build_runners import _databricks_pipeline_run as pipeline_run

#: The deploying identity the fake workspace answers ``/scim/v2/Me`` with.
ME = "deployer@example.com"
from fluid_build.build_runners._databricks_job_run import TIMEOUT_ENV
from fluid_build.build_runners._databricks_pipeline_run import (
    PipelineRef,
    pipelines_in_state,
    quality,
    run_pipeline_build,
    started_pipeline_builds,
    update_and_wait,
)

pytestmark = pytest.mark.unit

PRODUCT = "silver.test.capacity_v1"
BUILD = "build-capacity"
TABLE = "fluid_iactest_ldp.silver_capacity.capacity"
UNIQUE_DATASET = f"{TABLE}__capacity_key_unique"
PIPELINE_ID = "p-1"
QUERY = "SELECT 'a' AS site_id, CAST(1 AS DECIMAL(10,2)) AS load_kg, 'A' AS method"


def _contract() -> Dict[str, Any]:
    rules = [
        {
            "id": "capacity_required_complete",
            "type": "completeness",
            "selector": "load_kg",
            "severity": "error",
        },
        {
            "id": "capacity_method_valid",
            "type": "valid_values",
            "selector": "method",
            "severity": "warn",
        },
        {
            "id": "capacity_key_unique",
            "type": "uniqueness",
            "selector": "site_id",
            "threshold": 1.0,
            "operator": ">=",
            "severity": "critical",
        },
    ]
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Capacity",
        "metadata": {"owner": {"team": "data-platform"}, "layer": "Silver"},
        "exposes": [
            {
                "exposeId": "capacity",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {
                        "database": "fluid_iactest_ldp",
                        "schema": "silver_capacity",
                        "table": "capacity",
                    },
                },
                "contract": {
                    "schema": [
                        {
                            "name": "site_id",
                            "type": "STRING",
                            "required": True,
                            "sensitivity": "internal",
                        },
                        {"name": "load_kg", "type": "DECIMAL(10,2)", "required": True},
                        {
                            "name": "method",
                            "type": "STRING",
                            "validationRules": [{"type": "enum", "constraint": "A,B,C"}],
                        },
                    ],
                    "dq": {"rules": rules},
                },
            }
        ],
        "builds": [
            {
                "id": BUILD,
                "pattern": "embedded-logic",
                "engine": "lakeflow",
                "outputs": ["capacity"],
                "execution": {"trigger": {"type": "manual"}, "runtime": {"platform": "databricks"}},
                "properties": {"sql": QUERY},
            }
        ],
    }


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-0123456789abcdef")
    for name in (TIMEOUT_ENV, "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def compiled() -> Any:
    found, why = pipeline_run._compiled_pipeline(_contract(), BUILD)
    assert found is not None, why
    return found


def _flow(
    update_id: str = "u1",
    *,
    dataset: str = TABLE,
    expectations: Optional[List[Dict[str, Any]]] = None,
    dropped: int = 0,
    warned: int = 0,
) -> Dict[str, Any]:
    return {
        "event_type": "flow_progress",
        "origin": {"update_id": update_id, "flow_name": dataset},
        "details": {
            "flow_progress": {
                "status": "COMPLETED",
                "data_quality": {
                    "dropped_records": dropped,
                    "warned_records": warned,
                    "expectations": expectations or [],
                },
            }
        },
    }


def _rule(name: str, passed: int, failed: int, dataset: str = TABLE) -> Dict[str, Any]:
    return {"name": name, "dataset": dataset, "passed_records": passed, "failed_records": failed}


def _good_events() -> List[Dict[str, Any]]:
    """Newest first, as the API lists them; update ``u0``'s events must be ignored."""
    return [
        _flow(
            "u0",
            expectations=[_rule("capacity_required_complete", 99, 99)],
            dropped=99,
        ),
        _flow(
            expectations=[
                _rule("capacity_required_complete", 4, 1),
                _rule("capacity_method_valid", 3, 2),
            ],
            dropped=1,
            warned=2,
        ),
        {"event_type": "create_update", "origin": {"update_id": "u1"}},
        {"event_type": "create_update", "origin": {"update_id": "u0"}},
    ]


class _Api:
    """``WorkspaceClient.api_client`` for the Pipelines, tables and entity-tag endpoints."""

    def __init__(
        self,
        states: Optional[List[str]] = None,
        *,
        events: Optional[List[Dict[str, Any]]] = None,
        properties: Optional[Dict[str, str]] = None,
        tags: Optional[Dict[Tuple[str, str], Dict[str, str]]] = None,
        owners: Optional[Dict[Tuple[str, str, str], str]] = None,
        inherited: Optional[set] = None,
        me: Optional[str] = ME,
        no_table: bool = False,
        fail_update_reads: bool = False,
        fail_post_tag: Optional[str] = None,
        tag_page_size: int = 0,
    ) -> None:
        self.states = list(states or ["WAITING_FOR_RESOURCES", "RUNNING", "COMPLETED"])
        self.events = _good_events() if events is None else events
        self.properties = {} if properties is None else properties
        #: Tags per page of a tag list (0: one page), as the real list pages them.
        self.tag_page_size = tag_page_size
        self.tags = tags if tags is not None else {}
        #: ``(kind, name, key) -> updated_by``; a tag not listed was set by ``me``.
        self.owners = dict(owners or {})
        self.inherited = set(inherited or ())
        self.me = me
        self.no_table = no_table
        self.fail_update_reads = fail_update_reads
        self.fail_post_tag = fail_post_tag
        self.calls: List[Tuple[str, str, Any, Any]] = []

    def do(self, method: str, path: str, query: Any = None, body: Any = None, **_: Any) -> Any:
        self.calls.append((method, path, query, body))
        if re.fullmatch(r"/api/2\.0/pipelines/[^/]+/updates", path):
            assert method == "POST"
            return {"update_id": "u1"}
        if re.fullmatch(r"/api/2\.0/pipelines/[^/]+/updates/u1", path):
            if self.fail_update_reads:
                raise RuntimeError("503 service unavailable")
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"update": {"state": state}}
        if re.fullmatch(r"/api/2\.0/pipelines/[^/]+/events", path):
            return {"events": self.events}
        if re.fullmatch(r"/api/2\.0/pipelines/[^/]+/stop", path):
            return {}
        if path == "/api/2.0/preview/scim/v2/Me":
            if self.me is None:
                raise RuntimeError("401 unauthenticated")
            return {"userName": self.me}
        if path.startswith("/api/2.1/unity-catalog/tables/"):
            if self.no_table:
                raise RuntimeError("404 TABLE_DOES_NOT_EXIST")
            return {"properties": self.properties}
        found = re.fullmatch(
            r"/api/2\.1/unity-catalog/entity-tag-assignments/(tables|columns)/([^/]+)/tags(?:/(.+))?",
            path,
        )
        if found:
            kind, name, key = found.groups()
            held = self.tags.setdefault((kind, name), {})
            if method == "GET":
                listed = [
                    {
                        "tag_key": k,
                        "tag_value": v,
                        "updated_by": self.owners.get((kind, name, k), ME),
                        "inherited": (kind, name, k) in self.inherited,
                    }
                    for k, v in held.items()
                ]
                if not self.tag_page_size:
                    return {"tag_assignments": listed}
                start = int((query or {}).get("page_token") or 0)
                end = start + self.tag_page_size
                page: Dict[str, Any] = {"tag_assignments": listed[start:end]}
                if end < len(listed):
                    page["next_page_token"] = str(end)
                return page
            assert method == "DELETE"
            held.pop(key, None)
            return {}
        if path == "/api/2.1/unity-catalog/entity-tag-assignments":
            assert method == "POST"
            if body["tag_key"] == self.fail_post_tag:
                raise RuntimeError("403 permission denied")
            self.tags.setdefault((body["entity_type"], body["entity_name"]), {})[
                body["tag_key"]
            ] = body["tag_value"]
            self.owners.pop((body["entity_type"], body["entity_name"], body["tag_key"]), None)
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def paths(self, method: str, fragment: str = "") -> List[str]:
        return [p for m, p, _q, _b in self.calls if m == method and fragment in p]

    def start_body(self) -> Any:
        [body] = [b for m, p, _q, b in self.calls if m == "POST" and p.endswith("/updates")]
        return body

    def stops(self) -> List[str]:
        return self.paths("POST", "/stop")

    def tag_writes(self) -> List[Tuple[str, str, str]]:
        """``(method, entity, key)`` of every tag write, in order."""
        out = []
        for method, path, _q, body in self.calls:
            if method == "POST" and path == "/api/2.1/unity-catalog/entity-tag-assignments":
                out.append(("POST", body["entity_name"], body["tag_key"]))
            elif method == "DELETE":
                parts = path.split("/")
                out.append(("DELETE", parts[-3], parts[-1]))
        return out


class _Client:
    def __init__(self, api: _Api) -> None:
        self.api_client = api


class _Time:
    """A clock that moves only when the code under test sleeps."""

    def __init__(self, interrupt_after: Optional[int] = None) -> None:
        self.now = 1000.0
        self.slept: List[float] = []
        self.interrupt_after = interrupt_after

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if self.interrupt_after is not None and len(self.slept) >= self.interrupt_after:
            raise KeyboardInterrupt
        self.slept.append(seconds)
        self.now += seconds


def _wait(
    api: _Api,
    compiled: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    full_refresh: bool = False,
    timer: Optional[_Time] = None,
) -> Tuple[int, List[str]]:
    timer = timer or _Time()
    lines: List[str] = []
    rc = update_and_wait(
        _Client(api),
        PipelineRef(PIPELINE_ID, "https://dbc-1.cloud.databricks.com/pipelines/p-1"),
        compiled,
        build_id=BUILD,
        name=f"fluid {PRODUCT} {BUILD}",
        full_refresh=full_refresh,
        env=env or {},
        say=lines.append,
        sleep=timer.sleep,
        clock=timer.clock,
    )
    return rc, lines


def _text(lines: List[str], capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return " ".join(("\n".join(lines) + captured.out + captured.err).split())


def _resource(
    pipeline_id: str = PIPELINE_ID,
    *,
    product: str = PRODUCT,
    build: str = BUILD,
    rtype: str = "databricks_pipeline",
    mode: str = "managed",
) -> Dict[str, Any]:
    return {
        "type": rtype,
        "mode": mode,
        "values": {
            "id": pipeline_id,
            "url": f"https://dbc-1.cloud.databricks.com/pipelines/{pipeline_id}",
            "tags": {"fluid_product": product, "fluid_build": build},
        },
    }


# 1. started_pipeline_builds --------------------------------------------------
def test_started_pipeline_builds_lists_lakeflow_builds_only():
    contract = _contract()
    continuous = copy.deepcopy(contract["builds"][0])
    continuous["id"] = "build-stream"
    continuous["execution"]["trigger"] = {"type": "continuous"}
    dbt = {"id": "build-dbt", "engine": "dbt", "properties": {"model": "x"}}
    contract["builds"] += [continuous, dbt]

    # A continuous one is run too: Databricks keeps it running, so only its tags are set.
    assert [b["id"] for b in started_pipeline_builds(contract, None)] == [BUILD, "build-stream"]
    assert [b["id"] for b in started_pipeline_builds(contract, BUILD)] == [BUILD]
    assert [b["id"] for b in started_pipeline_builds(contract, "build-stream")] == ["build-stream"]
    assert started_pipeline_builds(contract, "build-dbt") == []
    assert started_pipeline_builds(contract, "nope") == []


# 2. pipelines_in_state --------------------------------------------------------
def test_pipelines_in_state_picks_this_products_pipelines_by_tag():
    resources = [
        _resource("p-1"),
        _resource("p-2", product="other.product"),
        _resource("p-3", mode="data"),
        _resource("p-4", rtype="databricks_job"),
        _resource("p-5", build="build-other"),
        "not a mapping",
    ]
    found = pipelines_in_state(resources, PRODUCT)
    assert found == {
        BUILD: [PipelineRef("p-1", "https://dbc-1.cloud.databricks.com/pipelines/p-1")],
        "build-other": [PipelineRef("p-5", "https://dbc-1.cloud.databricks.com/pipelines/p-5")],
    }


def test_two_pipelines_for_one_build_are_a_list_of_two():
    found = pipelines_in_state([_resource("p-1"), _resource("p-9")], PRODUCT)
    assert [ref.pipeline_id for ref in found[BUILD]] == ["p-1", "p-9"]


# 3. run_pipeline_build --------------------------------------------------------
def _refs(*ids: str) -> Dict[str, List[PipelineRef]]:
    return {BUILD: [PipelineRef(i) for i in ids]} if ids else {}


def _factory_must_not_run() -> Any:
    raise AssertionError("no client may be created")


def test_dry_run_says_so_and_calls_nothing(capsys):
    contract = _contract()
    rc = run_pipeline_build(
        contract,
        contract["builds"][0],
        _refs(PIPELINE_ID),
        dry_run=True,
        client_factory=_factory_must_not_run,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and BUILD in out


@pytest.mark.parametrize("ids", [(), ("p-1", "p-2")])
def test_zero_or_two_pipelines_fail_naming_the_tags(ids, capsys):
    contract = _contract()
    rc = run_pipeline_build(
        contract,
        contract["builds"][0],
        _refs(*ids),
        client_factory=_factory_must_not_run,
    )
    text = " ".join(capsys.readouterr().err.split())
    assert rc == 1
    assert f"fluid_product={PRODUCT}" in text and f"fluid_build={BUILD}" in text


def test_missing_sdk_names_the_databricks_extra(capsys):
    contract = _contract()

    def no_sdk() -> Any:
        raise ImportError("databricks")

    rc = run_pipeline_build(
        contract, contract["builds"][0], _refs(PIPELINE_ID), client_factory=no_sdk
    )
    assert rc == 1
    assert "[databricks]" in capsys.readouterr().err


# 4. the happy path ------------------------------------------------------------
def test_completed_update_prints_states_and_data_quality(compiled, capsys):
    api = _Api()
    rc, lines = _wait(api, compiled)
    text = _text(lines, capsys)

    assert rc == 0
    assert api.start_body() == {"full_refresh": False}
    assert "started update u1" in text
    for state in ("WAITING_FOR_RESOURCES", "RUNNING", "COMPLETED"):
        assert sum(1 for line in lines if line.strip().endswith(state)) == 1
    assert re.search(r"capacity_required_complete\s+error\s+capacity: passed 4, failed 1", text)
    assert "(dropped 1)" in text
    assert re.search(r"capacity_method_valid\s+warn\s+capacity: passed 3, failed 2", text)
    # the warn rule drops nothing, and u0's 99s are not u1's
    assert text.count("(dropped") == 1
    assert "99" not in text
    unique = [line for line in lines if "capacity_key_unique" in line]
    assert len(unique) == 1 and "no counts" in unique[0]
    assert api.stops() == []


def test_replace_and_build_asks_for_a_full_refresh(compiled):
    contract = _contract()
    api = _Api()
    rc = run_pipeline_build(
        contract,
        contract["builds"][0],
        _refs(PIPELINE_ID),
        mode="replace-and-build",
        client_factory=lambda: _Client(api),
        env={},
        sleep=lambda s: None,
        clock=_Time().clock,
    )
    assert rc == 0
    assert api.start_body() == {"full_refresh": True}


def test_amend_and_build_does_not_ask_for_a_full_refresh():
    contract = _contract()
    api = _Api()
    rc = run_pipeline_build(
        contract,
        contract["builds"][0],
        _refs(PIPELINE_ID),
        mode="amend-and-build",
        client_factory=lambda: _Client(api),
        env={},
        sleep=lambda s: None,
        clock=_Time().clock,
    )
    assert rc == 0
    assert api.start_body() == {"full_refresh": False}


# 5. a failed update -----------------------------------------------------------
def test_failed_update_says_the_error_and_sets_no_tags(compiled, capsys):
    events = [
        {
            "level": "ERROR",
            "message": "Expectation capacity_key_unique violated",
            "event_type": "update_progress",
            "origin": {"update_id": "u1"},
        },
        {"event_type": "create_update", "origin": {"update_id": "u1"}},
    ]
    api = _Api(["RUNNING", "FAILED"], events=events)
    rc, lines = _wait(api, compiled)
    text = _text(lines, capsys)

    assert rc == 1
    assert "Expectation capacity_key_unique violated" in text
    assert "FAILED" in text
    assert api.tag_writes() == []
    assert api.paths("GET", "entity-tag-assignments") == []


def test_a_failed_expectation_s_record_is_not_printed(compiled, capsys):
    """Databricks' EXPECTATION_VIOLATION error quotes the row that failed it (the documented
    shape below): the flow and the rule are printed, the row is not."""
    row = '{"site_id":"S-0042","owner_email":"someone@example.com"}'
    violation = (
        "[EXPECTATION_VIOLATION.VERBOSITY_ALL] Flow 'capacity' failed to meet the expectation. "
        f"Violated expectations: 'capacity_site_complete'. Input data: '{row}'. "
        f"Output record: '{row}'. Missing input data: false"
    )
    events = [
        {
            "level": "ERROR",
            "message": "Flow 'capacity' has FAILED fatally.",
            "event_type": "flow_progress",
            "origin": {"update_id": "u1"},
            "error": {"exceptions": [{"message": violation}]},
        },
        {
            "level": "ERROR",
            "message": f"Update u1 is FAILED. output record: {row}",
            "event_type": "update_progress",
            "origin": {"update_id": "u1"},
        },
        {"event_type": "create_update", "origin": {"update_id": "u1"}},
    ]
    api = _Api(["RUNNING", "FAILED"], events=events)
    rc, lines = _wait(api, compiled)
    text = _text(lines, capsys)

    assert rc == 1
    assert "Violated expectations: 'capacity_site_complete'." in text
    assert "someone@example.com" not in text and "S-0042" not in text
    assert text.count("the record is not printed here") == 2


# 6. tags after a completed update ---------------------------------------------
def test_tags_are_set_changed_removed_and_left_alone(compiled):
    """Declared tags are set; an undeclared tag the deploying identity set is removed (a
    dropped label, whatever ran the updates since); a steward's tag and an inherited one stay."""
    table = ("tables", TABLE)
    column = ("columns", f"{TABLE}.site_id")
    declassified = ("columns", f"{TABLE}.method")
    api = _Api(
        tags={
            table: {
                "fluid_layer": "Silver",  # declared, unchanged
                "fluid_version": "0.0.1",  # declared, changed
                "old_key": "x",  # set by this identity, no longer declared
                "stray": "keep",  # a steward's
                "domain_tag": "keep",  # inherited from the schema
            },
            declassified: {"fluid_sensitivity": "pii", "other": "keep"},
        },
        owners={
            ("tables", TABLE, "stray"): "steward@example.com",
            ("columns", f"{TABLE}.method", "other"): "steward@example.com",
        },
        inherited={("tables", TABLE, "domain_tag")},
    )
    rc, _lines = _wait(api, compiled)

    assert rc == 0
    writes = api.tag_writes()
    assert ("POST", TABLE, "fluid_layer") not in writes
    assert ("DELETE", TABLE, "fluid_layer") not in writes
    assert ("DELETE", TABLE, "old_key") in writes
    assert ("DELETE", TABLE, "stray") not in writes
    assert ("DELETE", TABLE, "domain_tag") not in writes
    assert ("POST", TABLE, "fluid_owner_team") in writes
    assert ("DELETE", TABLE, "fluid_version") in writes
    assert writes.index(("DELETE", TABLE, "fluid_version")) < writes.index(
        ("POST", TABLE, "fluid_version")
    )
    assert ("DELETE", f"{TABLE}.method", "fluid_sensitivity") in writes
    assert api.tags[column] == {"fluid_sensitivity": "internal"}
    assert api.tags[declassified] == {"other": "keep"}
    # No table-property record is read any more.
    assert api.paths("GET", "/unity-catalog/tables/") == []


def test_every_page_of_a_tag_list_is_read(compiled):
    """A declared tag on the second page is not set again, and a stale one there is removed."""
    api = _Api(
        tags={("tables", TABLE): {"stray": "keep", "fluid_layer": "Silver", "old_key": "x"}},
        owners={("tables", TABLE, "stray"): "steward@example.com"},
        tag_page_size=1,
    )
    rc, _lines = _wait(api, compiled)

    assert rc == 0
    writes = api.tag_writes()
    assert ("POST", TABLE, "fluid_layer") not in writes
    assert ("DELETE", TABLE, "old_key") in writes
    assert ("DELETE", TABLE, "stray") not in writes


def test_a_tag_list_that_never_ends_fails_the_build(compiled, capsys):
    class _Endless(_Api):
        def do(self, method, path, query=None, body=None, **kw):
            if method == "GET" and "entity-tag-assignments" in path:
                self.calls.append((method, path, query, body))
                return {"tag_assignments": [], "next_page_token": "same"}
            return super().do(method, path, query, body, **kw)

    api = _Endless()
    rc, lines = _wait(api, compiled)

    assert rc == 1
    assert "never ended" in _text(lines, capsys)


def test_an_unreadable_identity_removes_nothing_but_still_sets(compiled):
    table = ("tables", TABLE)
    api = _Api(tags={table: {"old_key": "x"}}, me=None)
    rc, lines = _wait(api, compiled)
    assert rc == 0
    assert [w for w in api.tag_writes() if w[0] == "DELETE"] == []
    assert ("POST", TABLE, "fluid_owner_team") in api.tag_writes()
    assert any("left in place" in line for line in lines)


def test_a_continuous_pipeline_gets_its_tags_without_an_update(compiled):
    api = _Api()
    rc = pipeline_run.tag_running_pipeline(
        _Client(api), compiled, build_id=BUILD, name="fluid x b", say=lambda line: None
    )
    assert rc == 0
    assert not [p for m, p, _q, _b in api.calls if p.endswith("/updates")]
    assert ("POST", TABLE, "fluid_owner_team") in api.tag_writes()


def test_a_continuous_pipeline_without_its_table_yet_waits_for_it(compiled):
    api = _Api(no_table=True)
    said: List[str] = []
    rc = pipeline_run.tag_running_pipeline(
        _Client(api), compiled, build_id=BUILD, name="fluid x b", say=said.append
    )
    assert rc == 0
    assert api.tag_writes() == []
    assert any("next apply" in line for line in said)


def test_a_ctrl_c_during_the_start_request_stops_the_pipeline(compiled):
    class Interrupting(_Api):
        def do(self, method, path, query=None, body=None, **kw):
            if method == "POST" and path.endswith("/updates"):
                self.calls.append((method, path, query, body))
                raise KeyboardInterrupt
            return super().do(method, path, query=query, body=body, **kw)

    api = Interrupting()
    with pytest.raises(KeyboardInterrupt):
        _wait(api, compiled)
    assert api.paths("POST", "/stop")


def test_malformed_event_payloads_do_not_raise():
    events = [
        {"event_type": "flow_progress", "details": "x", "origin": "x"},
        {
            "event_type": "flow_progress",
            "details": {
                "flow_progress": {
                    "data_quality": {
                        "expectations": [{"name": "r", "dataset": "d", "passed_records": "x"}],
                        "dropped_records": None,
                    }
                }
            },
            "origin": {"flow_name": "d"},
        },
    ]
    counts, dropped = pipeline_run.quality(events)
    assert counts == {("d", "r"): (0, 0)} and dropped == {"d": 0}


def test_no_table_yet_removes_nothing_and_still_sets_tags(compiled):
    api = _Api(no_table=True)
    rc, _lines = _wait(api, compiled)
    assert rc == 0
    assert [w for w in api.tag_writes() if w[0] == "DELETE"] == []
    assert ("POST", TABLE, "fluid_owner_team") in api.tag_writes()


def test_a_tag_that_cannot_be_set_fails_the_build_and_names_it(compiled, capsys):
    api = _Api(fail_post_tag="fluid_owner_team")
    rc, lines = _wait(api, compiled)
    text = _text(lines, capsys)
    assert rc == 1
    assert "fluid_owner_team" in text and "could not all be set" in text


# 7. the wait is capped; Ctrl-C stops the pipeline -------------------------------
def test_timeout_stops_the_pipeline_and_fails(compiled, capsys):
    api = _Api(["RUNNING"])
    rc, lines = _wait(api, compiled, env={TIMEOUT_ENV: "1"})
    text = _text(lines, capsys)
    assert rc == 1
    assert len(api.stops()) == 1 and api.stops()[0].endswith(f"/{PIPELINE_ID}/stop")
    assert "did not end within" in text and "was stopped" in text
    assert api.tag_writes() == []


def test_ctrl_c_while_waiting_stops_the_pipeline_and_propagates(compiled):
    api = _Api(["RUNNING"])
    with pytest.raises(KeyboardInterrupt):
        _wait(api, compiled, timer=_Time(interrupt_after=2))
    assert len(api.stops()) == 1


# 8. failed reads ----------------------------------------------------------------
def test_five_failed_reads_in_a_row_fail_without_stopping(compiled, capsys):
    api = _Api(fail_update_reads=True)
    rc, lines = _wait(api, compiled)
    text = _text(lines, capsys)
    assert rc == 1
    assert len(api.paths("GET", "/updates/u1")) == 5
    assert api.stops() == []
    assert "5 times in a row" in text


# 9. quality() ------------------------------------------------------------------
def test_quality_keeps_the_max_per_rule_and_dropped_by_flow():
    events = [
        _flow(expectations=[_rule("r", 4, 1)], dropped=1),
        _flow(expectations=[_rule("r", 2, 3)], dropped=3),
        _flow(expectations=[_rule("r", 5, 0)], dropped=0),
        _flow(dataset="c.s.other", expectations=[_rule("r", 7, 0, "c.s.other")], dropped=2),
        {"event_type": "create_update", "origin": {"update_id": "u1"}},
    ]
    counts, dropped = quality(events)
    assert counts == {(TABLE, "r"): (5, 3), ("c.s.other", "r"): (7, 0)}
    assert dropped == {TABLE: 3, "c.s.other": 2}


def test_quality_of_an_event_without_counts_is_empty():
    assert quality([{"event_type": "flow_progress", "details": {}}]) == ({}, {})


# 10. the build runner's lakeflow branch ----------------------------------------
def test_lakeflow_build_without_pipelines_says_it_runs_in_the_workspace(capsys):
    from fluid_build.build_runners.base import _lakeflow_build

    contract = _contract()
    args = argparse.Namespace(dry_run=False, mode="amend")
    rc = _lakeflow_build(contract, contract["builds"][0], None, None, args)
    out = " ".join(capsys.readouterr().out.split())
    assert rc is None
    assert "runs in the Databricks workspace as Lakeflow pipeline" in out
    assert f"fluid {PRODUCT} {BUILD}" in out


def test_lakeflow_build_with_pipelines_runs_the_pipeline_build(monkeypatch):
    from fluid_build.build_runners.base import _lakeflow_build

    seen: Dict[str, Any] = {}

    def fake_run(contract, build, pipelines, **kwargs):
        seen.update(build=build, pipelines=pipelines, **kwargs)
        return 7

    monkeypatch.setattr(pipeline_run, "run_pipeline_build", fake_run)
    contract = _contract()
    refs = _refs(PIPELINE_ID)
    args = argparse.Namespace(dry_run=True, mode="replace-and-build")
    rc = _lakeflow_build(contract, contract["builds"][0], None, refs, args)
    assert rc == 7
    assert seen == {
        "build": contract["builds"][0],
        "pipelines": refs,
        "dry_run": True,
        "mode": "replace-and-build",
        "contract_file": None,
    }


# 11. fluid apply's hand-over ----------------------------------------------------
def test_a_contract_that_does_not_compile_says_why_and_starts_nothing(monkeypatch, capsys):
    contract = _contract()
    contract["builds"][0]["pattern"] = "hybrid-reference"
    api = _Api()
    rc = pipeline_run.run_pipeline_build(
        contract, contract["builds"][0], _refs(PIPELINE_ID), client_factory=lambda: _Client(api)
    )
    assert rc == 1
    captured = capsys.readouterr()
    said = " ".join((captured.out + captured.err).split())
    assert "databricks-lakeflow-pattern" in said
    assert api.calls == []


def test_databricks_job_runs_counts_lakeflow_builds():
    from fluid_build.cli.apply import _databricks_job_runs

    contract = _contract()
    assert _databricks_job_runs(contract, None) is True
    # A continuous one too: the build phase sets its tags, so it needs the state and the SDK.
    contract["builds"][0]["execution"]["trigger"] = {"type": "continuous"}
    assert _databricks_job_runs(contract, None) is True


def test_jobs_from_state_hands_over_the_pipelines_and_no_jobs():
    from fluid_build.cli.apply import _jobs_from_state

    contract = _contract()
    handed = _jobs_from_state({"applied_state": [_resource()]}, contract)
    assert handed["databricks_jobs"] == {}
    assert handed["databricks_pipelines"] == {
        BUILD: [
            PipelineRef(PIPELINE_ID, f"https://dbc-1.cloud.databricks.com/pipelines/{PIPELINE_ID}")
        ]
    }
    assert _jobs_from_state({}, contract) == {}
