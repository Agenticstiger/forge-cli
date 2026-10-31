# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
        self.fail_on: Optional[str] = None
        self.page_size = 100
        self.api_client = StubApiClient(self)
        self.calls: List[Any] = []

    def add_jobs(self, name: str, count: int) -> None:
        for _ in range(count):
            job_id = len(self.jobs) + 1
            self.jobs.append({"job_id": job_id, "settings": {"name": name}})

    def answer(self, method: str, path: str, query: Dict[str, Any]) -> Dict[str, Any]:
        assert method == "GET"
        if self.fail_on and self.fail_on in path:
            raise StubHttpError(500, f"server error with token {FAKE_TOKEN} in it")
        if path.startswith("/api/2.1/unity-catalog/tables/"):
            full = path.rsplit("/", 1)[1]
            if full not in self.tables:
                raise StubHttpError(404, f"Table '{full}' does not exist.")
            return {"full_name": full, "columns": self.tables[full]}
        if path.startswith("/api/2.1/unity-catalog/effective-permissions/catalog/"):
            principal = query["principal"]
            privileges = self.catalog_privileges.get(principal, [])
            assignments = (
                [{"principal": principal, "privileges": [{"privilege": p} for p in privileges]}]
                if privileges
                else []
            )
            return {"privilege_assignments": assignments}
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


def test_names_in_the_grant_have_their_backticks_doubled():
    assert grant_statement("we`ird", "a`b") == "GRANT USE CATALOG ON CATALOG `we``ird` TO `a``b`"


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


def _run_verify(tmp_path, monkeypatch, capfd, workspace, *flags: str):
    from fluid_build.cli import main

    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(CONTRACT, sort_keys=False), encoding="utf-8")
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
