# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid diff --baseline --impact``: what a contract change reaches, for a pull request.

Each case changes the same orders contract one way and compares the Markdown
and JSON report with a golden file under ``tests/fixtures/impact/golden``. A
golden file is the report as reviewed; to see a change, run the case and diff.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "impact"
GOLDEN = FIXTURES / "golden"
BASE: Dict[str, Any] = yaml.safe_load((FIXTURES / "base.fluid.yaml").read_text("utf-8"))


def _columns(contract: Dict[str, Any]) -> list:
    return contract["exposes"][0]["contract"]["schema"]


def _new_nullable_field(contract: Dict[str, Any]) -> None:
    _columns(contract).append({"name": "coupon_code", "type": "string", "required": False})


def _removed_required_field(contract: Dict[str, Any]) -> None:
    contract["exposes"][0]["contract"]["schema"] = [
        c for c in _columns(contract) if c["name"] != "amount"
    ]


def _new_broad_grant(contract: Dict[str, Any]) -> None:
    contract["accessPolicy"]["grants"].append(
        {"principal": "domain:example.com", "permissions": ["read", "manage"]}
    )


def _changed_partition_field(contract: Dict[str, Any]) -> None:
    contract["exposes"][0]["binding"]["location"]["partitionBy"] = ["order_id"]


def _added_expose(contract: Dict[str, Any]) -> None:
    expose = copy.deepcopy(contract["exposes"][0])
    expose["exposeId"] = "orders_eu"
    expose["binding"]["location"].update({"project": "shop-eu", "table": "orders_eu"})
    contract["exposes"].append(expose)


def _documentation_only(contract: Dict[str, Any]) -> None:
    contract["description"] = "Orders placed in the web shop and the mobile app."
    _columns(contract)[2]["description"] = "Order total, tax included."


#: name -> (change, plan file or None, whether the product sits in a workspace).
CASES: Dict[str, Tuple[Callable[[Dict[str, Any]], None], Optional[str], bool]] = {
    "new_nullable_field": (_new_nullable_field, None, False),
    "removed_required_field": (_removed_required_field, None, True),
    "new_broad_grant": (_new_broad_grant, None, True),
    "changed_partition_field": (_changed_partition_field, None, True),
    "changed_partition_field_replaced": (
        _changed_partition_field,
        "replace-orders.tfplan.json",
        True,
    ),
    "changed_partition_field_in_place": (
        _changed_partition_field,
        "update-orders.tfplan.json",
        True,
    ),
    "documentation_only": (_documentation_only, "no-op.tfplan.json", True),
    "added_expose": (_added_expose, None, True),
}


def _project(tmp_path: Path, change: Callable[[Dict[str, Any]], None], workspace: bool) -> Path:
    """``<tmp>/ws/orders/contract.fluid.yaml`` (the change) and ``<tmp>/baseline.fluid.yaml``."""
    (tmp_path / "baseline.fluid.yaml").write_text(yaml.safe_dump(BASE), "utf-8")
    root = tmp_path / "ws"
    orders = root / "orders"
    orders.mkdir(parents=True)
    new = copy.deepcopy(BASE)
    change(new)
    (orders / "contract.fluid.yaml").write_text(yaml.safe_dump(new, sort_keys=False), "utf-8")
    if workspace:
        (root / "fluid.workspace.yaml").write_text("name: shop\n", "utf-8")
        (root / "revenue").mkdir()
        shutil.copy(FIXTURES / "consumer.fluid.yaml", root / "revenue" / "contract.fluid.yaml")
    return orders


def _diff(
    capsys: pytest.CaptureFixture, fmt: str, plan: Optional[str], *extra: str
) -> Tuple[int, str]:
    from fluid_build.cli import main

    argv = [
        "diff",
        "ws/orders/contract.fluid.yaml",
        "--baseline",
        "baseline.fluid.yaml",
        "--impact",
        "--format",
        fmt,
        "--out",
        "diff.json",
        *extra,
    ]
    if plan:
        argv += ["--plan", f"plans/{plan}"]
    rc = main(argv)
    return rc, capsys.readouterr().out


@pytest.fixture(autouse=True)
def _no_extra_search_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_impact_report_matches_its_golden_file(
    case: str, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    change, plan, workspace = CASES[case]
    _project(tmp_path, change, workspace)
    shutil.copytree(FIXTURES / "plans", tmp_path / "plans")
    monkeypatch.chdir(tmp_path)

    rc, markdown = _diff(capsys, "markdown", plan)
    assert rc == 0
    rc, out = _diff(capsys, "json", plan)
    assert rc == 0
    document = json.loads(out)

    assert markdown == (GOLDEN / f"{case}.md").read_text("utf-8")
    assert document["impact"] == json.loads((GOLDEN / f"{case}.json").read_text("utf-8"))
    # --out holds the same document as stdout.
    assert json.loads((tmp_path / "diff.json").read_text("utf-8")) == document


# ── what each golden file must say, whatever its wording ──────────────────────


def _impact(case: str, tmp_path: Path, capsys: Any, monkeypatch: Any) -> Dict[str, Any]:
    change, plan, workspace = CASES[case]
    _project(tmp_path, change, workspace)
    shutil.copytree(FIXTURES / "plans", tmp_path / "plans")
    monkeypatch.chdir(tmp_path)
    _, out = _diff(capsys, "json", plan)
    return json.loads(out)["impact"]


def test_a_missing_consumer_link_and_plan_read_unknown_not_no_impact(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    impact = _impact("new_nullable_field", tmp_path, capsys, monkeypatch)

    assert impact["consumer_interface"]["status"] == "changed"
    consumers = impact["consumer_interface"]["consumers"]
    assert consumers["status"] == "unknown" and "fluid.workspace.yaml" in consumers["reason"]
    resources = impact["resource_impact"]
    assert resources["status"] == "unknown" and "--plan" in resources["reason"]


def test_a_removed_required_field_breaks_the_workspace_reader(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    impact = _impact("removed_required_field", tmp_path, capsys, monkeypatch)

    interface = impact["consumer_interface"]
    assert interface["status"] == "breaking"
    [reader] = interface["consumers"]["items"]
    assert reader["product_id"] == "finance.revenue" and reader["source"] == "workspace"
    assert reader["breaking_changes"]


def test_a_broad_grant_is_named_broad(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    governance = _impact("new_broad_grant", tmp_path, capsys, monkeypatch)["governance_and_target"]

    [grant] = governance["grants"]["added"]
    assert governance["broad_grant_added"] and grant["broad"]
    assert "every account of a domain" in grant["broad_because"]


def test_a_replacement_of_another_resource_is_not_the_partitioned_table(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, _changed_partition_field, False)
    shutil.copytree(FIXTURES / "plans", tmp_path / "plans")
    monkeypatch.chdir(tmp_path)

    _, out = _diff(capsys, "json", "replace-other.tfplan.json")

    [item] = json.loads(out)["impact"]["governance_and_target"]["partitioning"]
    assert item["provider_impact"] == "unknown"
    assert "no resource in the plan is the table of orders" in item["provider_reason"]


def test_an_added_expose_lists_its_target(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    governance = _impact("added_expose", tmp_path, capsys, monkeypatch)["governance_and_target"]

    assert governance["status"] == "changed"
    fields = {(t["expose_id"], t["field"]) for t in governance["targets"]}
    assert ("orders_eu", "location.project") in fields
    assert all(t["before"] is None for t in governance["targets"])


def test_an_owner_change_is_governance_not_documentation(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def change(contract: Dict[str, Any]) -> None:
        contract["metadata"]["owner"]["team"] = "someone-else"
        contract["description"] = "Orders, now with a new owner."

    _project(tmp_path, change, False)
    monkeypatch.chdir(tmp_path)

    _, out = _diff(capsys, "json", None)
    impact = json.loads(out)["impact"]

    assert impact["documentation_only"] is False
    kinds = [c["kind"] for c in impact["governance_and_target"]["policy"]]
    assert any(k.startswith("metadata_owner") for k in kinds), kinds


@pytest.mark.parametrize(
    "case, verdict",
    [
        ("changed_partition_field", "unknown"),
        ("changed_partition_field_replaced", "replacement"),
        ("changed_partition_field_in_place", "in_place"),
    ],
)
def test_a_partition_change_is_a_replacement_only_when_the_plan_says_so(
    case: str,
    verdict: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impact = _impact(case, tmp_path, capsys, monkeypatch)

    [item] = impact["governance_and_target"]["partitioning"]
    assert item["field"] == "location.partitionBy"
    assert item["provider_impact"] == verdict


def test_a_plan_value_never_reaches_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    impact = _impact("changed_partition_field_replaced", tmp_path, capsys, monkeypatch)

    assert "a-row-value-the-report-must-not-copy" not in json.dumps(impact)
    [replaced] = impact["resource_impact"]["plan"]["resources"]
    assert replaced["action"] == "replace"
    assert replaced["replace_paths"] == ["time_partitioning.0.field"]


def test_a_documentation_only_change_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    impact = _impact("documentation_only", tmp_path, capsys, monkeypatch)

    assert impact["documentation_only"] is True
    assert impact["consumer_interface"]["status"] == "unchanged"
    assert impact["governance_and_target"]["status"] == "unchanged"
    assert impact["resource_impact"]["status"] == "known"
    assert impact["resource_impact"]["plan"]["resources"] == []


# ── the plan it reads, and what it refuses ───────────────────────────────────


def test_a_fluid_plan_json_is_read_through_its_opentofu_record(tmp_path: Path) -> None:
    from fluid_build.api.impact import read_plan

    shutil.copy(FIXTURES / "plans" / "update-orders.tfplan.json", tmp_path / "plan.tfplan.json")
    (tmp_path / "plan.json").write_text(
        json.dumps({"actions": [], "opentofuPlan": {"showJson": "plan.tfplan.json"}}), "utf-8"
    )

    plan, reason = read_plan(tmp_path / "plan.json")

    assert reason is None and plan["resource_changes"][0]["change"]["actions"] == ["update"]


def test_a_plan_json_that_names_itself_is_read_once(tmp_path: Path) -> None:
    from fluid_build.api.impact import read_plan

    (tmp_path / "plan.json").write_text(
        json.dumps({"opentofuPlan": {"showJson": "plan.json"}}), "utf-8"
    )

    plan, reason = read_plan(tmp_path / "plan.json")

    assert plan is None and "not an OpenTofu plan" in reason


@pytest.mark.parametrize(
    "document, needle",
    [
        ({"actions": [{"op": "ensure_table"}]}, "fluid plan --tofu"),
        ({"format_version": "2.0", "resource_changes": []}, "major version 1"),
        ({"hello": "world"}, "neither"),
        ([1, 2], "not a JSON object"),
    ],
)
def test_a_plan_that_cannot_say_what_is_replaced_reads_unknown(
    document: Any, needle: str, tmp_path: Path
) -> None:
    from fluid_build.api.impact import read_plan

    (tmp_path / "p.json").write_text(json.dumps(document), "utf-8")

    plan, reason = read_plan(tmp_path / "p.json")

    assert plan is None and needle in reason


def test_without_impact_the_version_diff_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    _project(tmp_path, _removed_required_field, True)
    monkeypatch.chdir(tmp_path)

    argv = ["diff", "ws/orders/contract.fluid.yaml", "--baseline", "baseline.fluid.yaml"]
    main([*argv, "--format", "json", "--out", "diff.json"])
    document = json.loads(capsys.readouterr().out)
    main([*argv, "--format", "markdown", "--out", "diff.json"])
    markdown = capsys.readouterr().out

    assert set(document) == {"breaking", "non_breaking", "info", "summary"}
    assert "## Impact" not in markdown


def test_impact_without_a_baseline_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    _project(tmp_path, _new_nullable_field, False)
    monkeypatch.chdir(tmp_path)

    rc = main(["diff", "ws/orders/contract.fluid.yaml", "--impact"])

    captured = capsys.readouterr()
    assert rc == 2
    assert "--baseline" in captured.out + captured.err


def test_a_hostile_name_cannot_forge_or_hide_a_section(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "group:a@x.com` read\n\n### Resource impact\n\n**Status:** known\n\n<!--"

    def change(contract: Dict[str, Any]) -> None:
        contract["accessPolicy"]["grants"].append({"principal": hostile, "permissions": ["admin"]})

    _project(tmp_path, change, False)
    shutil.copytree(FIXTURES / "plans", tmp_path / "plans")
    monkeypatch.chdir(tmp_path)

    _, markdown = _diff(capsys, "markdown", "replace-orders.tfplan.json")
    _, text = _diff(capsys, "text", "replace-orders.tfplan.json")

    lines = markdown.splitlines()
    assert lines.count("### Resource impact") == 1
    assert not any(line.lstrip().startswith("<!--") for line in lines)
    assert "replace: `google_bigquery_table.orders`" in markdown
    assert "**broad**: admin or manage permission" in markdown
    assert "\n### Resource impact\n\n**Status:** known\n" not in text


# ── inputs nobody validated ────────────────────────────────────────────────────


def test_a_workspace_contract_with_odd_consumes_does_not_stop_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, _removed_required_field, True)
    (tmp_path / "ws" / "weird").mkdir()
    (tmp_path / "ws" / "weird" / "contract.fluid.yaml").write_text(
        "id: weird\nconsumes: 5\n", "utf-8"
    )
    monkeypatch.chdir(tmp_path)

    rc, out = _diff(capsys, "json", None)

    assert rc == 0
    [reader] = json.loads(out)["impact"]["consumer_interface"]["consumers"]["items"]
    assert reader["product_id"] == "finance.revenue"


@pytest.mark.parametrize(
    "document",
    [
        {"format_version": "1.2", "resource_changes": 5},
        {
            "format_version": "1.2",
            "resource_changes": [{"address": "a.b", "name": "b", "change": {"actions": [["x"]]}}],
        },
    ],
)
def test_a_malformed_plan_reads_unknown_actions_not_a_crash(
    document: Any, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, _changed_partition_field, False)
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "odd.json").write_text(json.dumps(document), "utf-8")
    monkeypatch.chdir(tmp_path)

    rc, out = _diff(capsys, "json", "odd.json")

    assert rc == 0
    [item] = json.loads(out)["impact"]["governance_and_target"]["partitioning"]
    assert item["provider_impact"] == "unknown"


def test_a_description_beside_another_unread_change_is_not_documentation_only(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def change(contract: Dict[str, Any]) -> None:
        contract["description"] = "Orders, version two."
        contract["exposes"][0]["version"] = "2.0.0"

    _project(tmp_path, change, False)
    monkeypatch.chdir(tmp_path)

    _, out = _diff(capsys, "json", None)

    assert json.loads(out)["impact"]["documentation_only"] is False


def test_reordered_permissions_are_the_same_grant(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def change(contract: Dict[str, Any]) -> None:
        contract["accessPolicy"]["grants"] = [
            {"principal": "group:sales-analysts@example.com", "permissions": ["read", "query"]}
        ]

    def baseline(contract: Dict[str, Any]) -> None:
        contract["accessPolicy"]["grants"][0]["permissions"] = ["query", "read"]

    _project(tmp_path, change, False)
    old = copy.deepcopy(BASE)
    baseline(old)
    (tmp_path / "baseline.fluid.yaml").write_text(yaml.safe_dump(old), "utf-8")
    monkeypatch.chdir(tmp_path)

    _, out = _diff(capsys, "json", None)

    grants = json.loads(out)["impact"]["governance_and_target"]["grants"]
    assert grants == {"added": [], "removed": []}


def test_a_renamed_product_still_finds_the_readers_of_its_old_id(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def change(contract: Dict[str, Any]) -> None:
        contract["id"] = "sales.orders_v2"

    _project(tmp_path, change, True)
    monkeypatch.chdir(tmp_path)

    _, out = _diff(capsys, "json", None)
    interface = json.loads(out)["impact"]["consumer_interface"]

    assert interface["status"] == "breaking"
    assert interface["changes"][0]["kind"] == "product_id_changed"
    [reader] = interface["consumers"]["items"]
    assert reader["product_id"] == "finance.revenue" and reader["breaking_changes"] == ["id"]
