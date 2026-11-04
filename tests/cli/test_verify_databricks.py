# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Stage 9 checks a Databricks binding through the SDK's low-level client.

``fluid verify`` answered "unsupported" for ``{platform: databricks, format:
delta_table}``: counted as *not checked*, never failed under ``--strict``. These
tests drive ``verify_databricks_expose`` (and, once, the real ``fluid verify``
entry point) with a stub for ``client.api_client.do``: databricks-sdk is the
``[databricks]`` extra and is not installed in CI. The last test drives the real
SDK against the in-process workspace stand-in and skips without it.
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any, Dict, List, Mapping, Optional

import pytest
import yaml

from fluid_build.cli import _verify_databricks as vdb
from fluid_build.cli._verify_databricks import grant_statement, verify_databricks_expose

pytestmark = pytest.mark.unit

DEPLOYER = "deployer@example.test"
FAKE_TOKEN = "fake-workspace-token"  # pragma: allowlist secret
CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "dbx.fake.orders",
    "name": "Databricks test orders",
    "description": "Databricks verify test contract",
    "metadata": {"owner": {"team": "data-platform"}, "layer": "Gold"},
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "binding": {
                "platform": "databricks",
                "format": "delta_table",
                "location": {"database": "main", "schema": "sales", "table": "orders"},
            },
            "policy": {"authz": {"readers": ["group:analysts", "group:bi"]}},
            "contract": {
                "schema": [
                    {"name": "id", "type": "bigint", "required": True},
                    {"name": "amount", "type": "double"},
                    {"name": "region", "type": "string"},
                ]
            },
        }
    ],
}
TABLE = "main.sales.orders"
LIVE_COLUMNS = [
    {"name": "id", "type_text": "bigint", "nullable": False},
    {"name": "amount", "type_text": "double", "nullable": True},
    {"name": "region", "type_text": "string", "nullable": True},
]


def contract_with_job() -> Dict[str, Any]:
    document = copy.deepcopy(CONTRACT)
    document["builds"] = [
        {
            "id": "dbt_daily",
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": "./dbt",
            "properties": {"model": "orders", "select": "orders+"},
            "outputs": ["orders"],
            "execution": {
                "trigger": {"type": "schedule", "cron": "0 2 * * *"},
                "runtime": {"platform": "databricks"},
            },
        }
    ]
    return document


class StubHttpError(Exception):
    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


class StubApiClient:
    def __init__(self, workspace: "StubWorkspace"):
        self.workspace = workspace

    def do(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None) -> Any:
        self.workspace.calls.append((method, path, dict(query or {})))
        return self.workspace.answer(method, path, dict(query or {}))


class StubWorkspace:
    """A stand-in for ``WorkspaceClient``: canned answers per REST path."""

    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {TABLE: copy.deepcopy(LIVE_COLUMNS)}
        # principal -> privileges held on the catalog
        self.catalog_privileges: Dict[str, List[str]] = {
            "analysts": ["USE_CATALOG"],
            "bi": ["ALL_PRIVILEGES"],
        }
        self.jobs: List[Dict[str, Any]] = []
        self.region = "eu-central-1"
        self.fail_on: Optional[str] = None
        self.page_size = 100
        # (entity type, full name) -> {tag key: value}, served paginated by ``tag_page_size``
        self.tags: Dict[Any, Dict[str, str]] = {}
        self.tag_page_size = 100
        # full name -> the table's ``row_filter`` as GetTable serves it
        self.row_filters: Dict[str, Dict[str, Any]] = {}
        # function full name -> FunctionInfo as GET .../functions/{name} serves it
        self.functions: Dict[str, Dict[str, Any]] = {}
        self.identity = DEPLOYER  # ``userName`` of GET .../scim/v2/Me
        self.table_owner = "table-owner@example.test"
        self.me_fails = False
        self.empty_tag_page_first = False
        self.stuck_tag_token = False
        self.api_client = StubApiClient(self)
        self.calls: List[Any] = []

    def add_jobs(self, name: str, count: int, **settings: Any) -> None:
        for _ in range(count):
            job_id = len(self.jobs) + 1
            self.jobs.append({"job_id": job_id, "settings": {"name": name, **settings}})

    def answer(self, method: str, path: str, query: Dict[str, Any]) -> Dict[str, Any]:
        assert method == "GET"
        if self.fail_on and self.fail_on in path:
            raise StubHttpError(500, f"server error with token {FAKE_TOKEN} in it")
        if path.startswith("/api/2.1/unity-catalog/tables/"):
            full = path.rsplit("/", 1)[1]
            if full not in self.tables:
                raise StubHttpError(404, f"Table '{full}' does not exist.")
            table: Dict[str, Any] = {
                "full_name": full,
                "columns": self.tables[full],
                "owner": self.table_owner,
            }
            if full in self.row_filters:
                table["row_filter"] = self.row_filters[full]
            return table
        if path == "/api/2.0/preview/scim/v2/Me":
            if self.me_fails:
                raise StubHttpError(500, "scim down")
            return {"userName": self.identity}
        if path.startswith("/api/2.1/unity-catalog/functions/"):
            full = path.rsplit("/", 1)[1]
            if full not in self.functions:
                raise StubHttpError(404, f"Function '{full}' does not exist.")
            return self.functions[full]
        if path.startswith("/api/2.1/unity-catalog/effective-permissions/catalog/"):
            principal = query["principal"]
            privileges = self.catalog_privileges.get(principal, [])
            assignments = (
                [{"principal": principal, "privileges": [{"privilege": p} for p in privileges]}]
                if privileges
                else []
            )
            return {"privilege_assignments": assignments}
        if path.startswith("/api/2.1/unity-catalog/entity-tag-assignments/"):
            _, _, _, _, _, kind, name, _tags = path.split("/")
            keys = sorted(self.tags.get((kind, name), {}))
            size = self.tag_page_size
            pages = [keys[i : i + size] for i in range(0, len(keys), size)] or [[]]
            if self.empty_tag_page_first:
                pages.insert(0, [])
            index = int(query.get("page_token") or 0) if not self.stuck_tag_token else 0
            body: Dict[str, Any] = {
                "tag_assignments": [
                    {"entity_type": kind, "entity_name": name, "tag_key": k, "tag_value": v}
                    for k, v in ((k, self.tags[(kind, name)][k]) for k in pages[index])
                ]
            }
            if index + 1 < len(pages):
                body["next_page_token"] = str(index + 1)
            if self.stuck_tag_token:
                body["next_page_token"] = "same"
            return body
        if path == "/api/2.1/unity-catalog/metastore_summary":
            return {"metastore_id": "ms-1", "region": self.region, "cloud": "aws"}
        if path == "/api/2.2/jobs/list":
            hits = [j for j in self.jobs if j["settings"]["name"] == query.get("name")]
            start = int(query.get("page_token") or 0)
            page = hits[start : start + self.page_size]
            out: Dict[str, Any] = {"jobs": page, "has_more": start + self.page_size < len(hits)}
            if out["has_more"]:
                out["next_page_token"] = str(start + self.page_size)
            return out
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def workspace() -> StubWorkspace:
    return StubWorkspace()


def verify(
    workspace: Optional[StubWorkspace],
    contract: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    contract = contract or CONTRACT
    expose = contract["exposes"][0]
    factory = (lambda: workspace) if workspace is not None else None
    return verify_databricks_expose(
        "orders", expose, contract=contract, client_factory=factory, **kwargs
    )


def job_name() -> str:
    from fluid_build.iac.providers.databricks import managed_objects

    _tables, jobs = managed_objects(contract_with_job(), {})
    [job] = jobs
    return job.name


# ── The table ───────────────────────────────────────────────────────────


def test_a_table_that_matches_the_contract_matches(workspace):
    result = verify(workspace)

    assert result["status"] == "match"
    assert result["exists"] is True
    assert result["table_id"] == TABLE and result["target"] == TABLE
    assert result["severity"]["level"] == "SUCCESS"
    assert set(result) >= {"status", "exists", "table_id", "severity", "dimensions", "metadata"}
    dims = result["dimensions"]
    assert dims["structure"]["matching_fields"] == ["id", "amount", "region"]
    assert dims["catalog_access"]["status"] == "pass"
    assert "jobs" not in dims


def test_a_live_type_is_read_from_type_name_when_type_text_is_absent(workspace):
    workspace.tables[TABLE][1] = {"name": "amount", "type_name": "DOUBLE", "nullable": True}

    assert verify(workspace)["status"] == "match"


def test_a_missing_table_is_critical_and_says_to_apply(workspace):
    del workspace.tables[TABLE]

    result = verify(workspace)

    assert result["status"] == "mismatch" and result["exists"] is False
    assert result["severity"]["level"] == "CRITICAL"
    assert "fluid apply" in result["severity"]["reason"] + " ".join(result["severity"]["actions"])
    assert [f["field"] for f in result["dimensions"]["structure"]["missing_fields"]] == [
        "id",
        "amount",
        "region",
    ]


def test_a_missing_table_in_a_reference_only_contract_is_the_usual_info(workspace):
    del workspace.tables[TABLE]

    result = verify(workspace, reference_only=True)

    assert result["status"] == "error" and result["exists"] is False


def test_a_missing_column_is_critical(workspace):
    workspace.tables[TABLE].pop()

    result = verify(workspace)

    assert result["status"] == "mismatch"
    assert result["severity"]["level"] == "CRITICAL"
    assert [f["field"] for f in result["dimensions"]["structure"]["missing_fields"]] == ["region"]


def test_a_changed_type_is_critical(workspace):
    workspace.tables[TABLE][1]["type_text"] = "string"

    result = verify(workspace)

    assert result["severity"]["level"] == "CRITICAL"
    assert result["dimensions"]["types"]["mismatches"] == [
        {"field": "amount", "expected": "double", "actual": "string"}
    ]


def test_an_extra_column_is_info_not_critical(workspace):
    workspace.tables[TABLE].append({"name": "note", "type_text": "string", "nullable": True})

    result = verify(workspace)

    assert result["status"] == "mismatch"
    assert result["severity"]["level"] == "INFO"
    assert [f["field"] for f in result["dimensions"]["structure"]["extra_fields"]] == ["note"]


def test_a_nullability_difference_is_the_constraint_class(workspace):
    workspace.tables[TABLE][0]["nullable"] = True

    result = verify(workspace)

    assert result["severity"]["level"] == "WARNING"
    assert result["dimensions"]["constraints"]["mismatches"] == [
        {"field": "id", "expected": "required", "actual": "nullable"}
    ]


# ── Catalog access ──────────────────────────────────────────────────────


def test_a_reader_without_use_catalog_is_critical_with_the_grant(workspace):
    workspace.catalog_privileges["analysts"] = ["SELECT", "BROWSE"]

    result = verify(workspace)

    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    [finding] = result["dimensions"]["catalog_access"]["findings"]
    assert finding["principal"] == "analysts" and finding["catalog"] == "main"
    assert finding["fix"] == "GRANT USE CATALOG ON CATALOG `main` TO `analysts`"
    assert "analysts" in result["severity"]["reason"] and "main" in result["severity"]["reason"]
    assert finding["fix"] in " ".join(result["severity"]["actions"])


def test_use_catalog_or_all_privileges_passes(workspace):
    workspace.catalog_privileges = {"analysts": ["USE_CATALOG"], "bi": ["ALL_PRIVILEGES"]}

    assert verify(workspace)["dimensions"]["catalog_access"]["status"] == "pass"


def contract_with_region(region: str) -> Dict[str, Any]:
    document = copy.deepcopy(CONTRACT)
    document["exposes"][0]["binding"]["location"]["region"] = region
    return document


def test_a_workspace_in_the_declared_region_passes_residency(workspace):
    # The contract's region is canonical (the emitter refuses any other
    # spelling); the metastore's is compared without case, as the plan's
    # precondition lower()s it.
    workspace.region = "EU-Central-1"

    result = verify(workspace, contract_with_region("eu-central-1"))

    assert result["status"] == "match"
    residency = result["dimensions"]["residency"]
    assert residency["status"] == "pass"
    assert residency["declared"] == "eu-central-1" and residency["actual"] == "EU-Central-1"


def test_a_workspace_in_another_region_fails_residency_with_a_fix(workspace):
    workspace.region = "us-west-2"

    result = verify(workspace, contract_with_region("eu-central-1"))

    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    residency = result["dimensions"]["residency"]
    assert residency["status"] == "fail"
    assert residency["declared"] == "eu-central-1" and residency["actual"] == "us-west-2"
    assert "DATABRICKS_HOST" in residency["fix"] and "eu-central-1" in residency["fix"]
    assert "us-west-2" in result["severity"]["reason"]
    assert residency["fix"] in " ".join(result["severity"]["actions"])


def test_no_declared_region_skips_residency_without_failing(workspace):
    workspace.region = "us-west-2"

    result = verify(workspace)

    assert result["status"] == "match"
    assert result["dimensions"]["residency"]["status"] == "skip"
    assert not any(path.endswith("metastore_summary") for _m, path, _q in workspace.calls)


def test_names_in_the_grant_have_their_backticks_doubled():
    assert grant_statement("we`ird", "a`b") == "GRANT USE CATALOG ON CATALOG `we``ird` TO `a``b`"


def test_a_privilege_a_group_conveys_counts_for_the_reader_asked_about(workspace):
    # Asked about one principal, Unity Catalog answers with the assignment of the
    # group that conveys the privilege (live, 2026-11-01: `account users` held
    # USE_CATALOG under `_workspace_users_workspace_<id>`). It is the reader's,
    # because the call asked about that reader alone.
    original = workspace.answer
    asked: set = set()

    def answer(method: str, path: str, query: Dict[str, Any]) -> Dict[str, Any]:
        if "effective-permissions" in path:
            asked.add(query.get("principal"))
            conveyed = {"privilege": "USE_CATALOG", "inherited_from_type": None}
            return {
                "privilege_assignments": [
                    {"principal": "_workspace_users_workspace_1", "privileges": [conveyed]}
                ]
            }
        return original(method, path, query)

    workspace.answer = answer  # type: ignore[method-assign]

    assert verify(workspace)["dimensions"]["catalog_access"]["status"] == "pass"
    assert asked == {"analysts", "bi"}


def test_effective_permissions_follow_next_page_token(workspace):
    pages = {
        None: {"privilege_assignments": [], "next_page_token": "p2"},
        "p2": {
            "privilege_assignments": [
                {"principal": "analysts", "privileges": [{"privilege": "USE_CATALOG"}]}
            ]
        },
    }
    original = workspace.answer

    def answer(method: str, path: str, query: Dict[str, Any]) -> Dict[str, Any]:
        if "effective-permissions" in path and query["principal"] == "analysts":
            return pages[query.get("page_token")]
        return original(method, path, query)

    workspace.answer = answer  # type: ignore[method-assign]

    assert verify(workspace)["dimensions"]["catalog_access"]["status"] == "pass"


# ── Jobs ────────────────────────────────────────────────────────────────


def test_one_job_for_the_build_is_ok(workspace):
    workspace.add_jobs(job_name(), 1)

    result = verify(workspace, contract_with_job())

    assert result["status"] == "match"
    assert result["dimensions"]["jobs"]["status"] == "pass"


def test_no_job_is_a_mismatch_that_says_to_apply(workspace):
    result = verify(workspace, contract_with_job())

    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    [finding] = result["dimensions"]["jobs"]["findings"]
    assert finding["build"] == "dbt_daily" and "fluid apply" in finding["message"]


def test_two_jobs_of_the_name_are_a_duplicate(workspace):
    workspace.add_jobs(job_name(), 2)

    result = verify(workspace, contract_with_job())

    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    [finding] = result["dimensions"]["jobs"]["findings"]
    assert "duplicate" in finding["message"] and "keep the one in state" in finding["message"]


def test_duplicates_beyond_one_page_are_counted(workspace):
    workspace.page_size = 1
    workspace.add_jobs(job_name(), 3)

    [job] = verify(workspace, contract_with_job())["dimensions"]["jobs"]["jobs"]

    assert job["found"] == 3


def test_a_job_of_another_name_is_not_this_build(workspace):
    workspace.add_jobs(job_name() + "-copy", 1)

    assert verify(workspace, contract_with_job())["status"] == "mismatch"


# ── Failures: a result, never a traceback or a secret ───────────────────


def test_a_missing_sdk_says_to_install_the_extra(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.delenv("DATABRICKS_AUTH_TYPE", raising=False)
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)

    result = verify(None)

    assert result["status"] == "error"
    assert "install the [databricks] extra: pip install 'data-product-forge[databricks]'" in (
        result["error"]
    )


def test_a_credential_refusal_is_an_error_with_its_kind_and_remediation(monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)

    result = verify(None)

    assert result["status"] == "error"
    assert "databricks-host-unset" in result["error"]
    assert "DATABRICKS_HOST" in result["error"]
    assert FAKE_TOKEN not in json.dumps(result)


def test_an_http_failure_is_an_error_without_the_token(workspace, monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)
    workspace.fail_on = "/tables/"

    result = verify(workspace)

    assert result["status"] == "error"
    assert "StubHttpError" in result["error"]
    assert FAKE_TOKEN not in json.dumps(result)


def test_a_failing_permission_read_is_an_error_not_a_pass(workspace):
    workspace.fail_on = "effective-permissions"

    result = verify(workspace)

    assert result["status"] == "error" and result["exists"] is True
    assert "StubHttpError" in result["error"]


def test_a_client_that_cannot_be_built_is_an_error(monkeypatch):
    def broken() -> Any:
        raise RuntimeError("cannot build")

    result = verify_databricks_expose(
        "orders", CONTRACT["exposes"][0], contract=CONTRACT, client_factory=broken
    )

    assert result["status"] == "error" and "RuntimeError" in result["error"]


def test_an_expose_the_emitter_manages_no_table_for_is_not_checked(workspace):
    result = verify_databricks_expose(
        "elsewhere", CONTRACT["exposes"][0], contract=CONTRACT, client_factory=lambda: workspace
    )

    assert result["status"] == "unsupported"


# ── Through the real `fluid verify` ─────────────────────────────────────


def _run_verify(
    tmp_path, monkeypatch, capfd, workspace, *flags: str, contract: Optional[Dict[str, Any]] = None
):
    from fluid_build.cli import main

    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract or CONTRACT, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FLUID_NO_TELEMETRY", "1")
    monkeypatch.setattr(vdb, "_default_client", lambda: workspace)
    try:
        code = main(["verify", str(path), *flags])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out, err = capfd.readouterr()
    return code, " ".join((out + err).split())


def test_fluid_verify_reports_the_databricks_findings_and_strict_fails(
    tmp_path, monkeypatch, capfd, workspace
):
    workspace.catalog_privileges["analysts"] = []

    code, out = _run_verify(tmp_path, monkeypatch, capfd, workspace, "--strict")

    assert "Format: delta_table" in out and f"Target: {TABLE}" in out
    assert "Skipped" not in out
    assert "Catalog access" in out
    assert "GRANT USE CATALOG ON CATALOG `main` TO `analysts`" in out
    assert code != 0


def test_fluid_verify_passes_a_matching_table_under_strict(tmp_path, monkeypatch, capfd, workspace):
    code, out = _run_verify(tmp_path, monkeypatch, capfd, workspace, "--strict")

    assert "Match: 1" in out
    assert code == 0


def test_fluid_verify_prints_the_residency_fix_and_strict_fails(
    tmp_path, monkeypatch, capfd, workspace
):
    workspace.region = "us-west-2"

    code, out = _run_verify(
        tmp_path,
        monkeypatch,
        capfd,
        workspace,
        "--strict",
        contract=contract_with_region("eu-central-1"),
    )

    assert "Residency (workspace region)" in out
    assert "declares region eu-central-1 but the workspace's metastore is in us-west-2" in out
    assert "Point DATABRICKS_HOST at a workspace in eu-central-1" in out
    assert code != 0


def test_fluid_verify_shows_residency_skipped_not_failed_without_a_region(
    tmp_path, monkeypatch, capfd, workspace
):
    code, out = _run_verify(tmp_path, monkeypatch, capfd, workspace, "--strict")

    assert "Residency (workspace region) ⚪ SKIPPED" in out and code == 0


# ── Governance tags ─────────────────────────────────────────────────────


def governed_contract() -> Dict[str, Any]:
    document = copy.deepcopy(CONTRACT)
    document["labels"] = {"data-classification": "confidential", "cost-center": "cx-001"}
    document["exposes"][0]["labels"] = {"sensitivity": "high"}
    return document


def seed_tags(workspace: StubWorkspace, **extra: str) -> None:
    schema = {"data_classification": "confidential", "cost_center": "cx-001"}
    workspace.tags[("schemas", "main.sales")] = dict(schema, **extra)
    workspace.tags[("tables", TABLE)] = dict(schema, sensitivity="high", **extra)


def test_every_expected_tag_on_the_table_and_its_schema_passes(workspace):
    seed_tags(workspace, set_by_someone_else="whatever")

    result = verify(workspace, governed_contract())

    assert result["status"] == "match" and result["severity"]["level"] == "SUCCESS"
    tags = result["dimensions"]["governance_tags"]
    assert tags["status"] == "pass" and "5 governance tag(s)" in tags["message"]
    asked = [path for _m, path, _q in workspace.calls if "entity-tag-assignments" in path]
    assert asked == [
        "/api/2.1/unity-catalog/entity-tag-assignments/schemas/main.sales/tags",
        f"/api/2.1/unity-catalog/entity-tag-assignments/tables/{TABLE}/tags",
    ]


def test_a_missing_tag_fails_with_the_fix_and_is_critical(workspace):
    seed_tags(workspace)
    del workspace.tags[("tables", TABLE)]["sensitivity"]

    result = verify(workspace, governed_contract())

    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    tags = result["dimensions"]["governance_tags"]
    assert tags["status"] == "fail"
    (finding,) = tags["findings"]
    assert finding["tag"] == "sensitivity" and finding["actual"] is None
    assert finding["entity"] == f"table {TABLE}"
    assert "fluid apply" in tags["fix"] and "CREATE OR REPLACE" in tags["fix"]
    assert "sensitivity" in result["severity"]["reason"]
    assert tags["fix"] in " ".join(result["severity"]["actions"])


def test_a_tag_with_another_value_fails_and_names_both(workspace):
    seed_tags(workspace)
    workspace.tags[("schemas", "main.sales")]["data_classification"] = "public"

    result = verify(workspace, governed_contract())

    tags = result["dimensions"]["governance_tags"]
    assert tags["status"] == "fail"
    (finding,) = tags["findings"]
    assert (finding["expected"], finding["actual"]) == ("confidential", "public")
    assert "public" in finding["message"] and "confidential" in finding["message"]


def test_the_tag_list_is_read_across_every_page_an_empty_one_with_a_token_included(workspace):
    seed_tags(workspace)
    workspace.tag_page_size = 1
    workspace.empty_tag_page_first = True

    result = verify(workspace, governed_contract())

    assert result["dimensions"]["governance_tags"]["status"] == "pass", result["dimensions"]
    tokens = [
        q.get("page_token")
        for _m, path, q in workspace.calls
        if path.endswith(f"/tables/{TABLE}/tags")
    ]
    assert tokens == [None, "1", "2", "3"]


def test_a_tag_list_that_never_ends_is_an_error_not_a_pass(workspace):
    seed_tags(workspace)
    workspace.stuck_tag_token = True

    result = verify(workspace, governed_contract())

    assert result["status"] == "error" and "never ended" in result["error"]


def test_a_contract_without_governance_labels_skips_the_tags_and_asks_nothing(workspace):
    result = verify(workspace)

    assert result["status"] == "match"
    assert result["dimensions"]["governance_tags"]["status"] == "skip"
    assert not any("entity-tag-assignments" in path for _m, path, _q in workspace.calls)


def test_fluid_verify_prints_the_tag_fix_and_strict_fails(tmp_path, monkeypatch, capfd, workspace):
    seed_tags(workspace)
    del workspace.tags[("tables", TABLE)]["sensitivity"]

    code, out = _run_verify(
        tmp_path, monkeypatch, capfd, workspace, "--strict", contract=governed_contract()
    )

    assert "Governance tags (Unity Catalog)" in out
    assert "table main.sales.orders has no tag sensitivity" in out
    assert "Run fluid apply" in out and "CREATE OR REPLACE" in out
    assert code != 0


def test_fluid_verify_shows_the_tags_skipped_without_labels(
    tmp_path, monkeypatch, capfd, workspace
):
    code, out = _run_verify(tmp_path, monkeypatch, capfd, workspace, "--strict")

    assert "Governance tags (Unity Catalog) ⚪ SKIPPED" in out and code == 0


# ── Row filter and column masks ─────────────────────────────────────────


def restricted_contract(**overrides: Any) -> Dict[str, Any]:
    """``orders`` with ``amount`` denied and ``region`` masked to ``analysts``, who also see
    only ``eu`` rows (``bi`` is not restricted)."""
    document = copy.deepcopy(CONTRACT)
    authz = document["exposes"][0]["policy"]["authz"]
    authz["columnRestrictions"] = [
        {"principal": "group:analysts", "columns": ["amount"], "access": "deny"},
        {
            "principal": "group:analysts",
            "columns": ["region"],
            "access": "mask",
            "mask": "last_four",
        },
    ]
    authz["rowFilters"] = [
        {"principal": "group:analysts", "where": overrides.get("where", "region = 'eu'")}
    ]
    return document


def expected_enforcement(contract: Dict[str, Any]):
    from fluid_build.iac.providers.databricks import managed_objects

    (table,), _ = managed_objects(contract, {})
    return table


def function_info(fn: Any, owner: str = DEPLOYER, **changes: Any) -> Dict[str, Any]:
    """FunctionInfo for an expected ``Function``, as Unity Catalog serves it."""
    info: Dict[str, Any] = {
        "full_name": fn.full_name,
        "owner": owner,
        "routine_body": "SQL",
        "routine_definition": fn.body,
        "full_data_type": fn.returns.lower(),
        "input_params": {
            "parameters": [
                {"name": n, "position": i, "type_text": t.lower()}
                for i, (n, t) in enumerate(fn.params)
            ]
        },
    }
    info.update(changes)
    return info


def bind_all(workspace: StubWorkspace, contract: Dict[str, Any]) -> None:
    """What ``fluid apply`` leaves on the table: the contract's own filter and masks, and
    their functions, made by the identity that deployed."""
    table = expected_enforcement(contract)
    enforcement = table.enforcement
    for fn in [enforcement.row_filter.function, *(m.function for m in enforcement.masks)]:
        workspace.functions[fn.full_name] = function_info(fn)
    function, columns = table.row_filter
    workspace.row_filters[TABLE] = {"function_name": function, "input_column_names": list(columns)}
    for column, mask in table.masks:
        next(c for c in workspace.tables[TABLE] if c["name"] == column)["mask"] = {
            "function_name": mask
        }


def test_the_expected_names_carry_a_hash_of_the_definition():
    table = expected_enforcement(restricted_contract())
    assert table.row_filter[0].startswith("main.sales.fluid_rf_orders_")
    assert [c for c, _f in table.masks] == ["amount", "region"]
    other = expected_enforcement(restricted_contract(where="region = 'uk'"))
    assert other.row_filter[0] != table.row_filter[0]
    assert other.masks == table.masks


def test_a_table_with_every_expected_binding_passes(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)

    result = verify(workspace, contract)

    assert result["status"] == "match" and result["severity"]["level"] == "SUCCESS"
    dimension = result["dimensions"]["row_filter_masks"]
    assert dimension["status"] == "pass" and "all 3 " in dimension["message"]


def test_a_missing_row_filter_fails_with_the_fix_and_is_critical(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    del workspace.row_filters[TABLE]

    result = verify(workspace, contract)

    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    dimension = result["dimensions"]["row_filter_masks"]
    assert dimension["status"] == "fail"
    (finding,) = dimension["findings"]
    assert finding["kind"] == "row_filter" and finding["actual"] is None
    assert "has no row filter" in finding["message"]
    assert "fluid apply" in dimension["fix"]
    assert "has no row filter" in result["severity"]["reason"]
    assert dimension["fix"] in " ".join(result["severity"]["actions"])


def test_a_row_filter_bound_to_another_function_fails_and_names_both(workspace):
    contract = restricted_contract()
    bind_all(workspace, restricted_contract(where="region = 'uk'"))  # the old predicate's

    result = verify(workspace, contract)

    (finding,) = [
        f for f in result["dimensions"]["row_filter_masks"]["findings"] if f["kind"] == "row_filter"
    ]
    assert finding["actual"] != finding["expected"]
    assert finding["actual"] in finding["message"] and finding["expected"] in finding["message"]
    assert result["severity"]["level"] == "CRITICAL"


def test_a_row_filter_reading_other_columns_fails(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    workspace.row_filters[TABLE]["input_column_names"] = ["id"]

    dimension = verify(workspace, contract)["dimensions"]["row_filter_masks"]

    assert (
        dimension["status"] == "fail" and "reads columns id" in dimension["findings"][0]["message"]
    )


def test_a_missing_mask_and_a_mask_of_another_function_fail(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    columns = {c["name"]: c for c in workspace.tables[TABLE]}
    del columns["amount"]["mask"]
    columns["region"]["mask"] = {"function_name": "main.sales.fluid_mask_orders_region_deadbeef"}

    dimension = verify(workspace, contract)["dimensions"]["row_filter_masks"]

    assert dimension["status"] == "fail" and len(dimension["findings"]) == 2
    by_column = {f["column"]: f for f in dimension["findings"]}
    assert by_column["amount"]["actual"] is None and "has no mask" in by_column["amount"]["message"]
    assert by_column["region"]["actual"].endswith("deadbeef")


def test_function_names_compare_without_case_or_backticks(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    workspace.row_filters[TABLE]["function_name"] = (
        "`" + workspace.row_filters[TABLE]["function_name"].upper() + "`"
    )

    assert verify(workspace, contract)["dimensions"]["row_filter_masks"]["status"] == "pass"


def test_a_contract_without_restrictions_skips_the_dimension(workspace):
    result = verify(workspace)

    assert result["dimensions"]["row_filter_masks"]["status"] == "skip"
    assert result["status"] == "match"


def _first_function(contract: Dict[str, Any]) -> Any:
    return expected_enforcement(contract).enforcement.masks[0].function


def test_a_function_someone_else_made_under_the_hashed_name_fails_verify_though_the_name_matches(
    workspace,
):
    """The name is computable from the contract: ``RETURN val`` under it, bound, passes every
    check of names and shows the column unmasked."""
    contract = restricted_contract()
    bind_all(workspace, contract)
    fn = _first_function(contract)
    workspace.functions[fn.full_name] = function_info(
        fn, owner="attacker@example.test", routine_definition="`val`"
    )

    result = verify(workspace, contract)

    dimension = result["dimensions"]["row_filter_masks"]
    assert dimension["status"] == "fail" and result["severity"]["level"] == "CRITICAL"
    (finding,) = dimension["findings"]
    assert finding["kind"] == "function"
    assert "attacker@example.test" in finding["message"]
    assert "its body is '`val`'" in finding["message"]


def test_a_function_with_the_right_body_but_another_owner_fails(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    fn = expected_enforcement(contract).enforcement.row_filter.function
    workspace.functions[fn.full_name] = function_info(fn, owner="someone@example.test")

    dimension = verify(workspace, contract)["dimensions"]["row_filter_masks"]

    assert dimension["status"] == "fail"
    assert "owner is someone@example.test" in dimension["findings"][0]["message"]


def test_a_function_owned_by_the_table_owner_passes(workspace):
    """Verify may run as someone other than the deployer: the table's owner is its reference too."""
    contract = restricted_contract()
    bind_all(workspace, contract)
    workspace.identity = "reader@example.test"
    for name, info in list(workspace.functions.items()):
        workspace.functions[name] = dict(info, owner=workspace.table_owner)

    assert verify(workspace, contract)["dimensions"]["row_filter_masks"]["status"] == "pass"


@pytest.mark.parametrize(
    "change",
    [
        {"full_data_type": "int"},
        {"routine_body": "EXTERNAL"},
        {"input_params": {"parameters": []}},
        {"routine_definition": "`val` IS NOT NULL"},
    ],
    ids=["returns", "language", "parameters", "body"],
)
def test_a_function_that_differs_in_return_type_language_parameters_or_body_fails(
    workspace, change
):
    contract = restricted_contract()
    bind_all(workspace, contract)
    fn = _first_function(contract)
    workspace.functions[fn.full_name] = function_info(fn, **change)

    assert verify(workspace, contract)["dimensions"]["row_filter_masks"]["status"] == "fail"


def test_a_body_that_differs_only_in_whitespace_passes(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    fn = _first_function(contract)
    spaced = fn.body.replace(" ", "   \n ")
    workspace.functions[fn.full_name] = function_info(fn, routine_definition=spaced)

    assert verify(workspace, contract)["dimensions"]["row_filter_masks"]["status"] == "pass"


def test_a_bound_function_that_does_not_exist_fails(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    fn = _first_function(contract)
    del workspace.functions[fn.full_name]

    dimension = verify(workspace, contract)["dimensions"]["row_filter_masks"]

    assert dimension["status"] == "fail" and "does not exist" in dimension["findings"][0]["message"]


def test_verify_still_checks_the_function_when_the_identity_cannot_be_read(workspace):
    contract = restricted_contract()
    bind_all(workspace, contract)
    workspace.me_fails = True
    for name, info in list(workspace.functions.items()):
        workspace.functions[name] = dict(info, owner=workspace.table_owner)
    assert verify(workspace, contract)["dimensions"]["row_filter_masks"]["status"] == "pass"
    fn = _first_function(contract)
    workspace.functions[fn.full_name] = function_info(fn, owner="attacker@example.test")
    assert verify(workspace, contract)["dimensions"]["row_filter_masks"]["status"] == "fail"


def test_a_binding_the_contract_does_not_ask_for_is_not_a_finding(workspace):
    workspace.row_filters[TABLE] = {
        "function_name": "main.sales.admin_rows",
        "input_column_names": [],
    }

    assert verify(workspace)["status"] == "match"


def test_fluid_verify_prints_the_missing_binding_and_strict_fails(
    tmp_path, monkeypatch, capfd, workspace
):
    code, out = _run_verify(
        tmp_path, monkeypatch, capfd, workspace, "--strict", contract=restricted_contract()
    )

    assert "Row filter and column masks (Unity Catalog)" in out
    assert "main.sales.orders has no row filter" in out
    assert "main.sales.orders.amount has no mask" in out
    assert "Run fluid apply" in out
    assert code != 0


def test_fluid_verify_shows_the_dimension_skipped_without_restrictions(
    tmp_path, monkeypatch, capfd, workspace
):
    code, out = _run_verify(tmp_path, monkeypatch, capfd, workspace, "--strict")

    assert "Row filter and column masks (Unity Catalog) ⚪ SKIPPED" in out and code == 0


# ── The real SDK, against the stand-in workspace ────────────────────────


def test_the_real_sdk_reads_the_stand_in_workspace(tmp_path, monkeypatch):
    pytest.importorskip("databricks.sdk")
    from tests.iac._fake_databricks import FakeDatabricks

    fake = FakeDatabricks().start()
    try:
        fake.create_table(TABLE, [("id", "bigint", False), ("amount", "double", True)])
        fake.grant_catalog("main", "analysts", "USE_CATALOG")
        for name in ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_AUTH_TYPE", "DATABRICKS_CLIENT_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("DATABRICKS_HOST", fake.endpoint)
        monkeypatch.setenv("DATABRICKS_TOKEN", FAKE_TOKEN)

        result = verify(None)
    finally:
        fake.stop()

    # region is not in the stand-in's table, and bi holds no USE CATALOG there.
    assert result["status"] == "mismatch", result
    assert [f["field"] for f in result["dimensions"]["structure"]["missing_fields"]] == ["region"]
    assert [f["principal"] for f in result["dimensions"]["catalog_access"]["findings"]] == ["bi"]


# ── A job this module paused because its table is not protected ─────────


def restricted_contract_with_job() -> Dict[str, Any]:
    document = restricted_contract()
    document["builds"] = contract_with_job()["builds"]
    return document


def test_a_job_paused_while_its_table_is_not_protected_is_a_finding(workspace):
    contract = restricted_contract_with_job()
    workspace.add_jobs(
        job_name(), 1, schedule={"quartz_cron_expression": "0 0 2 * * ?", "pause_status": "PAUSED"}
    )

    result = verify(workspace, contract)

    dimension = result["dimensions"]["jobs"]
    assert dimension["status"] == "fail"
    [finding] = dimension["findings"]
    assert finding["build"] == "dbt_daily"
    assert "is paused" in finding["message"] and "not protected" in finding["message"]
    assert "run fluid apply" in finding["message"]
    assert result["severity"]["level"] == "CRITICAL"


def test_a_paused_job_on_a_protected_table_is_not_a_finding(workspace):
    contract = restricted_contract_with_job()
    bind_all(workspace, contract)
    workspace.add_jobs(
        job_name(), 1, schedule={"quartz_cron_expression": "0 0 2 * * ?", "pause_status": "PAUSED"}
    )

    assert verify(workspace, contract)["dimensions"]["jobs"]["status"] == "pass"


def test_an_unpaused_job_on_an_unprotected_table_has_no_paused_finding(workspace):
    contract = restricted_contract_with_job()
    workspace.add_jobs(
        job_name(),
        1,
        schedule={"quartz_cron_expression": "0 0 2 * * ?", "pause_status": "UNPAUSED"},
    )

    assert verify(workspace, contract)["dimensions"]["jobs"]["status"] == "pass"
