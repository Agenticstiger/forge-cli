# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid test --results-dashboard``: path parsing, the dashboard definition's structural
invariants, and ensure_dashboard's create/update/unchanged flows against a fake client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fluid_build.cli import _test_dashboard_databricks as dash
from fluid_build.cli._common import CLIError

TABLE = ("main", "dq", "results")
REF = "`main`.`dq`.`results`"
PATH = "/Workspace/Shared/fluid/Data quality"
FAKE_TOKEN = "fake-test-token-0000"  # pragma: allowlist secret


class NotFound(Exception):
    status_code = 404


# ------------------------------------------------------------ path parsing


def test_parse_dashboard_path_splits_folder_and_name():
    assert dash.parse_dashboard_path(PATH) == ("/Workspace/Shared/fluid", "Data quality")


def test_parse_dashboard_path_strips_lvdash_suffix():
    assert dash.parse_dashboard_path(PATH + ".lvdash.json") == (
        "/Workspace/Shared/fluid",
        "Data quality",
    )


def test_parse_dashboard_path_accepts_a_name_directly_under_workspace():
    # Pins current behaviour: the leading empty segment counts toward the minimum of 3.
    assert dash.parse_dashboard_path("/Workspace/x") == ("/Workspace", "x")


@pytest.mark.parametrize(
    "bad",
    [
        "Workspace/Shared/x",
        "/Workspace/../x",
        "/Workspace//x",
        "/Workspace/Shared/",
        "/Workspace/./x",
        "/Workspace/Shared/a\x00b",
        "/Workspace/Shared/a\nb",
        "/x",
        "",
        None,
    ],
)
def test_parse_dashboard_path_rejects(bad):
    with pytest.raises(CLIError) as ei:
        dash.parse_dashboard_path(bad)
    assert ei.value.exit_code == 2 and ei.value.event == "test_results_dashboard_invalid"


# ------------------------------------------------------ definition shape


@pytest.fixture(scope="module")
def definition():
    return dash.dashboard_definition(TABLE)


def widgets(defn):
    return [item for page in defn["pages"] for item in page["layout"]]


def field_names(widget):
    return {f["name"] for q in widget.get("queries", []) for f in q["query"]["fields"]}


def encoded_fields(node):
    found = []
    if isinstance(node, dict):
        if "fieldName" in node:
            found.append(node["fieldName"])
        for v in node.values():
            found.extend(encoded_fields(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(encoded_fields(v))
    return found


def test_definition_is_json_serialisable_and_deterministic(definition):
    assert json.dumps(definition) == json.dumps(dash.dashboard_definition(TABLE))


def test_every_query_points_at_a_dataset(definition):
    names = {d["name"] for d in definition["datasets"]}
    used = {
        q["query"]["datasetName"]
        for w in widgets(definition)
        for q in w["widget"].get("queries", [])
    }
    assert used and used <= names


def test_every_encoding_field_is_a_query_field(definition):
    for item in widgets(definition):
        w = item["widget"]
        if "queries" not in w:
            continue
        assert set(encoded_fields(w["spec"]["encodings"])) <= field_names(w), w["name"]


def test_positions_fit_six_column_grid_without_overlap(definition):
    boxes = [i["position"] for i in widgets(definition)]
    for p in boxes:
        assert p["x"] >= 0 and p["x"] + p["width"] <= 6

    def overlap(a, b):
        return (
            a["x"] < b["x"] + b["width"]
            and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"]
            and b["y"] < a["y"] + a["height"]
        )

    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not overlap(a, b), (a, b)


def test_spec_versions_by_widget_type(definition):
    seen = {}
    for item in widgets(definition):
        spec = item["widget"].get("spec")
        if spec:
            seen.setdefault(spec["widgetType"], set()).add(spec["version"])
    assert seen == {"counter": {2}, "bar": {3}, "line": {3}, "table": {1}}


def test_datasets_read_the_backticked_table(definition):
    for d in definition["datasets"]:
        assert REF in "".join(d["queryLines"]), d["name"]


def test_structure_filter_on_rule_datasets_but_not_on_failing_list(definition):
    sql = {d["name"]: "".join(d["queryLines"]) for d in definition["datasets"]}
    for name in ("fluid_dq_overall", "fluid_dq_products", "fluid_dq_trend"):
        assert "COALESCE(rule_type, '') <> 'structure'" in sql[name]
    # last_run's CTE names 'structure'; the rows listed (after FROM latest) are not filtered.
    assert "structure" not in sql["fluid_dq_failing"].split("FROM latest", 1)[1]
    assert "WHERE NOT passed" in sql["fluid_dq_failing"]


def test_definition_names_the_table_in_the_text_box(definition):
    box = widgets(definition)[0]["widget"]["multilineTextboxSpec"]["lines"]
    assert "main.dq.results" in "".join(box)


# ---------------------------------------------------------- ensure_dashboard


class FakeApi:
    def __init__(self, status=None, dashboard=None, published=None, create=None):
        self.status = status if status is not None else NotFound("nope")
        self.dashboard = dashboard
        self.published = published if published is not None else {"revision_create_time": "t"}
        self.create = create if create is not None else {"dashboard_id": "dash-1"}
        self.calls = []

    def do(self, method, path, query=None, body=None, **kw):
        self.calls.append((method, path, query, body))
        if (method, path) == ("GET", dash.GET_STATUS_PATH):
            result = self.status
        elif (method, path) == ("POST", dash.MKDIRS_PATH):
            result = {}
        elif (method, path) == ("POST", dash.DASHBOARDS_PATH):
            result = self.create
        elif method == "GET" and path.endswith("/published"):
            result = self.published
        elif method == "GET" and path.startswith(dash.DASHBOARDS_PATH + "/"):
            result = self.dashboard
        elif method in ("PATCH", "POST"):
            result = {}
        else:
            raise AssertionError(f"unexpected {method} {path}")
        if isinstance(result, Exception):
            raise result
        return result

    def verbs(self):
        return [(c[0], c[1]) for c in self.calls]


class FakeClient:
    def __init__(self, api, host="https://adb-1.azuredatabricks.net/"):
        self.api_client = api
        self.config = SimpleNamespace(host=host)


def ensure(api, **kw):
    return dash.ensure_dashboard(
        PATH, TABLE, client_factory=lambda: FakeClient(api), warehouse_id="wh", **kw
    )


def existing_status(object_type="DASHBOARD"):
    return {
        "object_type": object_type,
        "path": "/Workspace/Shared/fluid/Data quality.lvdash.json",
        "resource_id": "dash-1",
    }


def live_dashboard(serialized=None, warehouse="wh"):
    return {
        "create_time": "t",
        "dashboard_id": "dash-1",
        "display_name": "Data quality",
        "etag": "etag-7",
        "lifecycle_state": "ACTIVE",
        "parent_path": "/Workspace/Shared/fluid",
        "path": "/Workspace/Shared/fluid/Data quality.lvdash.json",
        "serialized_dashboard": (
            serialized if serialized is not None else json.dumps(dash.dashboard_definition(TABLE))
        ),
        "update_time": "t",
        "warehouse_id": warehouse,
    }


def test_missing_dashboard_is_created_and_published(capsys):
    api = FakeApi()
    result = ensure(api)
    assert result["action"] == "created" and result["dashboard_id"] == "dash-1"
    assert api.verbs() == [
        ("GET", dash.GET_STATUS_PATH),
        ("POST", dash.MKDIRS_PATH),
        ("POST", dash.DASHBOARDS_PATH),
        ("POST", f"{dash.DASHBOARDS_PATH}/dash-1/published"),
    ]
    assert api.calls[0][2] == {"path": "/Workspace/Shared/fluid/Data quality.lvdash.json"}
    assert api.calls[1][3] == {"path": "/Workspace/Shared/fluid"}
    create = api.calls[2][3]
    assert create["display_name"] == "Data quality"
    assert create["parent_path"] == "/Workspace/Shared/fluid" and create["warehouse_id"] == "wh"
    assert json.loads(create["serialized_dashboard"]) == dash.dashboard_definition(TABLE)
    assert api.calls[3][3] == {"embed_credentials": False, "warehouse_id": "wh"}
    err = capsys.readouterr()
    assert err.out == "" and "Dashboard created" in err.err
    assert result["url"] == "https://adb-1.azuredatabricks.net/dashboardsv3/dash-1/published"


def test_same_definition_and_published_is_left_alone():
    api = FakeApi(status=existing_status(), dashboard=live_dashboard())
    result = ensure(api)
    assert result["action"] == "unchanged"
    assert [v[0] for v in api.verbs()] == ["GET", "GET", "GET"]
    assert not any(v[0] in ("PATCH", "POST") for v in api.verbs())


def test_unchanged_but_never_published_publishes_only():
    api = FakeApi(status=existing_status(), dashboard=live_dashboard(), published=NotFound("x"))
    result = ensure(api)
    assert result["action"] == "unchanged"
    assert api.verbs()[-1] == ("POST", f"{dash.DASHBOARDS_PATH}/dash-1/published")
    assert "PATCH" not in [v[0] for v in api.verbs()]


def test_different_definition_is_patched_with_etag_then_published():
    api = FakeApi(
        status=existing_status(),
        dashboard=live_dashboard(serialized='{"datasets": [], "pages": []}'),
    )
    result = ensure(api)
    assert result["action"] == "updated"
    verbs = api.verbs()
    assert verbs[-2:] == [
        ("PATCH", f"{dash.DASHBOARDS_PATH}/dash-1"),
        ("POST", f"{dash.DASHBOARDS_PATH}/dash-1/published"),
    ]
    patch = [c for c in api.calls if c[0] == "PATCH"][0][3]
    assert patch["etag"] == "etag-7" and patch["warehouse_id"] == "wh"
    assert json.loads(patch["serialized_dashboard"]) == dash.dashboard_definition(TABLE)


def test_different_warehouse_counts_as_changed():
    api = FakeApi(status=existing_status(), dashboard=live_dashboard(warehouse="other"))
    assert ensure(api)["action"] == "updated"


def test_unparseable_stored_definition_is_replaced():
    api = FakeApi(status=existing_status(), dashboard=live_dashboard(serialized="not json"))
    assert ensure(api)["action"] == "updated"


def test_create_race_already_exists_is_treated_as_existing():
    class Api(FakeApi):
        def __init__(self):
            super().__init__(dashboard=live_dashboard())
            self.lookups = 0

        def do(self, method, path, query=None, body=None, **kw):
            if (method, path) == ("GET", dash.GET_STATUS_PATH):
                self.lookups += 1
                self.calls.append((method, path, query, body))
                if self.lookups == 1:
                    raise NotFound("missing")
                return existing_status()
            if (method, path) == ("POST", dash.DASHBOARDS_PATH):
                self.calls.append((method, path, query, body))
                raise RuntimeError("RESOURCE_ALREADY_EXISTS: dashboard exists")
            return super().do(method, path, query=query, body=body)

    api = Api()
    result = ensure(api)
    assert api.lookups == 2 and result["dashboard_id"] == "dash-1"
    assert result["action"] == "unchanged"


def test_already_exists_but_refind_fails_surfaces_error():
    api = FakeApi(create=RuntimeError("RESOURCE_ALREADY_EXISTS: x"))
    with pytest.raises(CLIError) as ei:
        ensure(api)
    assert ei.value.event == "test_results_dashboard_failed"


def test_notebook_at_the_path_is_refused():
    api = FakeApi(status=existing_status("NOTEBOOK"))
    with pytest.raises(CLIError) as ei:
        ensure(api)
    assert ei.value.event == "test_results_dashboard_path_taken"
    assert ei.value.context["found"] == "NOTEBOOK"
    assert [v[0] for v in api.verbs()] == ["GET"]


def test_other_failure_is_exit_1_with_scrubbed_message(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)
    api = FakeApi(create=PermissionError(f"denied for token {FAKE_TOKEN}"))
    with pytest.raises(CLIError) as ei:
        ensure(api)
    assert ei.value.exit_code == 1 and ei.value.event == "test_results_dashboard_failed"
    error = ei.value.context["error"]
    assert error.startswith("PermissionError:") and FAKE_TOKEN not in error


def test_get_status_non_404_failure_is_reported():
    api = FakeApi(status=RuntimeError("503 unavailable"))
    with pytest.raises(CLIError) as ei:
        ensure(api)
    assert ei.value.event == "test_results_dashboard_failed" and "503" in ei.value.context["error"]


def test_invalid_path_is_rejected_before_any_call():
    api = FakeApi()
    with pytest.raises(CLIError) as ei:
        dash.ensure_dashboard(
            "relative", TABLE, client_factory=lambda: FakeClient(api), warehouse_id="w"
        )
    assert ei.value.exit_code == 2 and api.calls == []


def test_no_host_means_no_url():
    api = FakeApi()
    result = dash.ensure_dashboard(
        PATH, TABLE, client_factory=lambda: FakeClient(api, host=""), warehouse_id="wh"
    )
    assert result["url"] is None


def test_dashboard_without_the_sdk_names_the_extra():
    def no_sdk():
        raise ModuleNotFoundError("No module named 'databricks'")

    with pytest.raises(CLIError) as ei:
        dash.ensure_dashboard(PATH, TABLE, client_factory=no_sdk, warehouse_id="wh")
    assert ei.value.event == "test_results_dashboard_failed"
    assert "data-product-forge[databricks]" in ei.value.context["fix"]


def test_latest_run_is_the_latest_one_that_tested_rules(definition):
    sql = {d["name"]: "".join(d["queryLines"]) for d in definition["datasets"]}
    latest_users = ("fluid_dq_overall", "fluid_dq_products", "fluid_dq_failing")
    for name in latest_users:
        assert "WITH last_run AS" in sql[name], name
        assert "MAX(CASE WHEN" in sql[name] and "JOIN last_run" in sql[name], name
        assert "COALESCE(" in sql[name] and "MAX(run_time)" in sql[name], name
    assert not any("QUALIFY" in s for s in sql.values())


def stale_published(update_time, revision_create_time):
    dashboard = live_dashboard()
    dashboard["update_time"] = update_time
    return FakeApi(
        status=existing_status(),
        dashboard=dashboard,
        published={"revision_create_time": revision_create_time},
    )


PUBLISH_CALL = ("POST", f"{dash.DASHBOARDS_PATH}/dash-1/published")


def test_unchanged_draft_newer_than_published_revision_is_published():
    api = stale_published("2026-11-12T10:05:00Z", "2026-11-12T10:00:00Z")
    result = ensure(api)
    assert result["action"] == "unchanged"
    assert PUBLISH_CALL in api.verbs()
    assert "PATCH" not in [v[0] for v in api.verbs()]


def test_unchanged_draft_older_than_published_revision_is_not_republished():
    api = stale_published("2026-11-12T10:00:00Z", "2026-11-12T10:05:00Z")
    assert ensure(api)["action"] == "unchanged"
    assert PUBLISH_CALL not in api.verbs()


def test_unparseable_timestamps_do_not_republish():
    api = stale_published("garbage", "also garbage")
    assert ensure(api)["action"] == "unchanged"
    assert PUBLISH_CALL not in api.verbs()
