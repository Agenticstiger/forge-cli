# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The local (DuckDB) engine runs the stages of a ``pattern: multi-stage`` build.

Eight bundled templates keep their SQL in ``properties.stages[]``. The local
planner used to skip such a build ("has no SQL"), no expose got a source table, and
``fluid apply`` refused with ``local_plan_invalid``. The stages now run in
``dependsOn`` order in one session, each kept as a table under its ``outputs``
names, and an expose lands from the stage output it is named by.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build._errors import FluidUserError
from fluid_build.cli import build_parser
from fluid_build.cli.apply import run as apply_run
from fluid_build.cli.plan import run as plan_run
from fluid_build.providers.local.local import LocalProvider
from fluid_build.providers.local.planner import (
    LocalPlanError,
    ordered_stages,
    plan_actions,
)
from fluid_build.util.contract import build_output_exposes

pytestmark = [pytest.mark.integration]

LOG = logging.getLogger("test.local_multi_stage")

#: Both routes of ``fluid apply`` on the local engine.
ROUTES = [(), ("--mode", "amend-and-build")]


def _stage(
    name: str,
    sql: Optional[str] = "SELECT 1 AS a",
    outputs: Optional[List[str]] = None,
    depends_on: Optional[List[str]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    stage: Dict[str, Any] = {"name": name, "properties": {"sql": sql} if sql is not None else {}}
    if outputs is not None:
        stage["outputs"] = outputs
    if depends_on is not None:
        stage["dependsOn"] = depends_on
    stage.update(extra)
    return stage


def _expose(expose_id: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": f"out/{expose_id}.csv"},
        },
        "contract": {"schema": [{"name": "a", "type": "integer"}]},
    }


def _contract(stages: List[Dict[str, Any]], exposes: List[str], **build: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "repro.multi_stage_v1",
        "name": "Multi stage",
        "domain": "repro",
        "metadata": {"layer": "Silver", "owner": {"team": "repro-team"}},
        "builds": [
            {
                "id": "pipeline",
                "pattern": "multi-stage",
                "engine": "sql",
                "properties": {"stages": stages},
                **build,
            }
        ],
        "exposes": [_expose(e) for e in exposes],
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("FLUID_ENV", "FLUID_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def _write(project: Path, contract: Dict[str, Any]) -> None:
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(contract, sort_keys=False), "utf-8")


def _parse(*argv: str) -> Any:
    return build_parser().parse_args(list(argv))


def _apply(project: Path, contract: Dict[str, Any], *flags: str) -> int:
    _write(project, contract)
    return apply_run(_parse("apply", "contract.fluid.yaml", *flags, "--yes"), LOG)


def _landed(project: Path) -> Dict[str, str]:
    out = project / "out"
    return {p.stem: p.read_text("utf-8") for p in sorted(out.glob("*.csv"))} if out.is_dir() else {}


def _refusal(contract: Dict[str, Any]) -> LocalPlanError:
    with pytest.raises(LocalPlanError) as refused:
        plan_actions(contract)
    return refused.value


# ----------------------------- ordering ----------------------------- #


def test_stages_run_in_dependson_order_not_listed_order(project: Path) -> None:
    # ``final`` is listed first and reads what the two stages it depends on made.
    stages = [
        _stage("final", "SELECT a + b AS a FROM mid", ["final"], ["middle"]),
        _stage("middle", "SELECT a, 10 AS b FROM base", ["mid"], ["first"]),
        _stage("first", "SELECT 1 AS a", ["base"]),
    ]
    assert [s.name for s in ordered_stages(_contract(stages, ["final"])["builds"][0])] == [
        "first",
        "middle",
        "final",
    ]
    assert _apply(project, _contract(stages, ["final"])) == 0
    assert _landed(project) == {"final": "a\n11\n"}


def test_independent_stages_keep_their_listed_order() -> None:
    stages = [
        _stage("z_last_alpha", outputs=["t1"]),
        _stage("a_first_alpha", outputs=["t2"]),
        _stage("joined", "SELECT * FROM t1", ["t3"], ["z_last_alpha", "a_first_alpha"]),
        _stage("m_other", outputs=["t4"]),
    ]
    build = _contract(stages, ["t3"])["builds"][0]
    assert [s.name for s in ordered_stages(build)] == [
        "z_last_alpha",
        "a_first_alpha",
        "joined",
        "m_other",
    ]
    # ... and the plan runs them in that order.
    planned = [
        a["payload"]["stage"]
        for a in plan_actions(_contract(stages, ["t3"]))
        if a["op"] == "execute_sql"
    ]
    assert planned == ["z_last_alpha", "a_first_alpha", "joined", "m_other"]


# ----------------------------- refusals ----------------------------- #


def _cycle() -> Dict[str, Any]:
    return _contract(
        [
            _stage("a", outputs=["ta"], depends_on=["b"]),
            _stage("b", outputs=["A"], depends_on=["a"]),
        ],
        ["A"],
    )


def _unknown_dependency() -> Dict[str, Any]:
    return _contract([_stage("a", outputs=["A"], depends_on=["ghost"])], ["A"])


def _no_sql() -> Dict[str, Any]:
    return _contract([_stage("a", outputs=["A"]), _stage("b", None, ["B"], ["a"])], ["A", "B"])


def _unsupported_other_properties() -> Dict[str, Any]:
    stage = _stage("a", None, ["A"])
    stage["properties"] = {"model": "models/a.sql"}
    return _contract([stage], ["A"])


def _unsupported_language() -> Dict[str, Any]:
    stage = _stage("a", "SELECT 1 AS a", ["A"])
    stage["properties"]["language"] = "python"
    return _contract([stage], ["A"])


def _unsupported_key() -> Dict[str, Any]:
    return _contract([_stage("a", "SELECT 1 AS a", ["A"], engine="spark")], ["A"])


def _unsupported_both_sql_and_stages() -> Dict[str, Any]:
    contract = _contract([_stage("a", "SELECT 1 AS a", ["A"])], ["A"])
    contract["builds"][0]["properties"]["sql"] = "SELECT 2 AS a"
    return contract


def _unsupported_duplicate_table() -> Dict[str, Any]:
    return _contract([_stage("a", outputs=["A"]), _stage("b", outputs=["a"])], ["A"])


def _unsafe_name() -> Dict[str, Any]:
    return _contract([_stage("a", outputs=["bad-name"])], ["bad-name"])


REFUSALS = [
    (_cycle, "build_stage_cycle"),
    (_unknown_dependency, "build_stage_unknown_dependency"),
    (_no_sql, "build_stage_has_no_sql"),
    (_unsupported_other_properties, "build_stage_unsupported"),
    (_unsupported_language, "build_stage_unsupported"),
    (_unsupported_duplicate_table, "build_stage_unsupported"),
    (_unsafe_name, "build_stage_unsafe_name"),
]


#: Shapes the schema already rejects before the engine is reached (``fluid apply``
#: stops at the contract gate); the planner refuses them by name for the callers
#: that hand it a contract without that gate.
PLANNER_ONLY = [
    (_unsupported_key, "build_stage_unsupported"),
    (_unsupported_both_sql_and_stages, "build_stage_unsupported"),
]


@pytest.mark.parametrize(
    "make,code",
    [*REFUSALS, *PLANNER_ONLY],
    ids=[m.__name__[1:] for m, _ in [*REFUSALS, *PLANNER_ONLY]],
)
def test_a_stage_the_local_engine_cannot_run_is_refused_by_name(make: Any, code: str) -> None:
    assert _refusal(make()).code == code


@pytest.mark.parametrize("flags", ROUTES, ids=["default", "amend-and-build"])
@pytest.mark.parametrize("make,code", REFUSALS, ids=[f"{m.__name__[1:]}" for m, _ in REFUSALS])
def test_a_refused_build_exits_non_zero_and_writes_nothing(
    project: Path, make: Any, code: str, flags: Any
) -> None:
    try:
        rc = _apply(project, make(), *flags)
    except FluidUserError as refused:
        assert refused.code == code
        rc = 1
    assert rc != 0
    assert not (project / "out").exists()
    assert not (project / "runtime" / "out").exists()


def test_the_cycle_names_its_stages() -> None:
    assert set(_refusal(_cycle()).extras["cycle"]) == {"a", "b"}


# ----------------------------- landing ----------------------------- #


@pytest.mark.parametrize("flags", ROUTES, ids=["default", "amend-and-build"])
def test_an_expose_lands_from_the_stage_output_it_is_named_by(project: Path, flags: Any) -> None:
    # Two exposes from two different stages; a third stage's output is only intermediate.
    stages = [
        _stage("load", "SELECT 1 AS a UNION ALL SELECT 2", ["raw"]),
        _stage("double", "SELECT a * 2 AS a FROM raw", ["doubled"], ["load"]),
        _stage("keep", "SELECT a FROM raw WHERE a = 1", ["first_only"], ["load"]),
        _stage("sum", "SELECT SUM(a) AS a FROM doubled", ["total"], ["double"]),
    ]
    assert _apply(project, _contract(stages, ["total", "first_only"]), *flags) == 0
    landed = _landed(project)
    assert set(landed) == {"total", "first_only"}  # not raw, doubled: no expose of that name
    assert landed["total"] == "a\n6\n"
    assert landed["first_only"] == "a\n1\n"


@pytest.mark.parametrize("flags", ROUTES, ids=["default", "amend-and-build"])
def test_a_stage_output_named_like_its_expose_works(project: Path, flags: Any) -> None:
    stages = [
        _stage("one", "SELECT 7 AS a", ["A"]),
        _stage("two", "SELECT a + 1 AS a FROM A", ["B"], ["one"]),
    ]
    assert _apply(project, _contract(stages, ["A", "B"]), *flags) == 0
    assert _landed(project) == {"A": "a\n7\n", "B": "a\n8\n"}


def test_the_expose_action_does_not_depend_on_itself(project: Path) -> None:
    contract = _contract([_stage("one", "SELECT 7 AS a", ["A"])], ["A"])
    actions = plan_actions(contract)
    ids = [a["resource_id"] for a in actions]
    assert len(ids) == len(set(ids))
    assert ids.index("pipeline__stage__one") < ids.index("A")  # the stage runs first


def test_a_stage_with_several_outputs_keeps_each_and_lands_either(project: Path) -> None:
    stages = [
        _stage("make", "SELECT 3 AS a", ["first_out", "second_out"]),
        _stage("read", "SELECT a + 1 AS a FROM second_out", ["third"], ["make"]),
    ]
    for flags in ROUTES:
        assert _apply(project, _contract(stages, ["second_out", "third"]), *flags) == 0
        assert _landed(project) == {"second_out": "a\n3\n", "third": "a\n4\n"}


def test_a_stage_without_outputs_is_kept_under_its_stage_name(project: Path) -> None:
    stages = [
        _stage("check-me", "SELECT 5 AS a"),
        _stage("final", "SELECT a FROM check_me", ["F"], ["check-me"]),
    ]
    assert _apply(project, _contract(stages, ["F"])) == 0
    assert _landed(project) == {"F": "a\n5\n"}


def test_an_intermediate_stage_writes_no_preview_file(project: Path) -> None:
    stages = [_stage("one", "SELECT 7 AS a", ["mid"]), _stage("two", "SELECT * FROM mid", ["A"])]
    assert _apply(project, _contract(stages, ["A"])) == 0
    assert not list((project / "runtime" / "out").glob("preview_*"))


def test_a_stage_reads_a_file_with_the_sql_as_written(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "n.csv").write_text("n\n1\n2\n3\n", "utf-8")
    stages = [
        _stage("read", "SELECT n AS a FROM read_csv_auto('data/n.csv', header=true)", ["nums"]),
        _stage("total", "SELECT SUM(a) AS a FROM nums;", ["A"], ["read"]),
    ]
    assert _apply(project, _contract(stages, ["A"])) == 0
    assert _landed(project) == {"A": "a\n6\n"}


# ----------------------- exposes no stage produces ----------------------- #


def test_an_expose_no_stage_produces_is_refused_and_nothing_is_written(project: Path) -> None:
    stages = [_stage("one", "SELECT 1 AS a", ["A"])]
    with pytest.raises(FluidUserError) as refused:
        _apply(project, _contract(stages, ["A", "B"]))
    assert refused.value.code == "local_plan_invalid"
    assert "B: materialize missing source_table" in refused.value.why
    assert not (project / "out").exists()


def test_build_level_outputs_name_an_expose_no_stage_produces(project: Path) -> None:
    stages = [_stage("one", "SELECT 1 AS a", ["A"])]
    contract = _contract(stages, ["A", "B"], outputs=["A", "B"])
    with pytest.raises(FluidUserError) as refused:
        _apply(project, contract)
    assert refused.value.code == "local_plan_invalid"
    assert not (project / "out").exists()
    # The amend-and-build route refuses the same build before anything runs.
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    with pytest.raises(FluidUserError) as derived:
        provider._derive_actions_from_contract(contract)
    assert derived.value.code == "local_plan_invalid"
    assert not (project / "out").exists()


def test_build_level_outputs_win_and_are_still_validated(project: Path) -> None:
    stages = [_stage("one", "SELECT 1 AS a", ["A"]), _stage("two", "SELECT 2 AS a", ["B"])]
    contract = _contract(stages, ["A", "B"], outputs=["A"])
    assert [e["exposeId"] for e in build_output_exposes(contract, contract["builds"][0])] == ["A"]
    contract["builds"][0]["outputs"] = ["nope"]
    with pytest.raises(FluidUserError) as refused:
        build_output_exposes(contract, contract["builds"][0])
    assert refused.value.code == "build_output_unknown_expose"


def test_without_build_outputs_the_stage_outputs_name_the_exposes() -> None:
    stages = [_stage("x", outputs=["B", "tmp"]), _stage("y", outputs=["A"])]
    contract = _contract(stages, ["A", "B", "C"])
    names = [e["exposeId"] for e in build_output_exposes(contract, contract["builds"][0])]
    assert names == ["B", "A"]  # in the order the stages name them; C is no stage's


# ----------------------------- the plan route ----------------------------- #


def test_a_plan_applied_from_another_directory_runs_the_stages(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = [
        _stage("two", "SELECT a + 1 AS a FROM mid", ["B"], ["one"]),
        _stage("one", "SELECT 1 AS a", ["mid"]),
    ]
    _write(project, _contract(stages, ["B"]))
    assert plan_run(_parse("plan", "contract.fluid.yaml", "--out", "plan.json"), LOG) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert apply_run(_parse("apply", str(project / "plan.json"), "--yes"), LOG) == 0
    assert _landed(project) == {"B": "a\n2\n"}


# ------------------- the review's follow-up: landing, gates, precedence ------------------- #


def _rc(project: Path, contract: Dict[str, Any], *flags: str) -> int:
    """The exit code of an apply, a refusal raised as an error included (non-zero)."""
    try:
        return _apply(project, contract, *flags)
    except (FluidUserError, SystemExit, Exception):  # noqa: BLE001 - any refusal is non-zero
        return 1


def _with_binding(contract: Dict[str, Any], expose_id: str, **binding: Any) -> Dict[str, Any]:
    for expose in contract["exposes"]:
        if expose["exposeId"] == expose_id:
            expose["binding"] = {**expose["binding"], **binding}
    return contract


def test_the_amend_route_refuses_a_masked_expose(
    project: Path, capsys: pytest.CaptureFixture
) -> None:
    contract = _contract([_stage("one", "SELECT 1 AS a", ["A"])], ["A"])
    contract["exposes"][0]["policy"] = {
        "privacy": {"masking": [{"column": "a", "strategy": "hash"}]}
    }
    assert _rc(project, contract, "--mode", "amend-and-build") != 0
    assert "masking" in capsys.readouterr().out
    assert not (project / "out").exists()


@pytest.mark.parametrize(
    "binding",
    [
        {"platform": "aws", "location": {"path": "s3://bucket/a.parquet"}},
        {"platform": "local", "location": {"path": "gs://bucket/a.csv"}},
        {"platform": "gcp", "location": {"table": "t"}},
        {"platform": "snowflake", "location": {}},
    ],
    ids=["aws", "remote-path", "bigquery-no-path", "snowflake-no-path"],
)
def test_the_amend_route_refuses_an_expose_that_is_not_a_local_file(
    project: Path, binding: Dict[str, Any], capsys: pytest.CaptureFixture
) -> None:
    stages = [_stage("one", "SELECT 1 AS a", ["A"]), _stage("two", "SELECT 2 AS a", ["B"])]
    contract = _with_binding(_contract(stages, ["A", "B"]), "B", **binding)
    assert _rc(project, contract, "--mode", "amend-and-build") != 0
    assert "land local files only" in " ".join(capsys.readouterr().out.split())
    assert not (project / "out").exists()
    assert not (project / "runtime" / "out" / "B.csv").exists()


def test_a_non_local_expose_without_a_path_gets_no_default_local_file(project: Path) -> None:
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    expose = {"exposeId": "B", "binding": {"platform": "gcp", "location": {}}}
    assert provider._stage_expose_spec({}, expose) is None
    local = {"exposeId": "B", "binding": {"platform": "local", "location": {}}}
    assert provider._stage_expose_spec({}, local) == {"path": "runtime/out/B.csv", "format": "csv"}


@pytest.mark.parametrize("make,code", REFUSALS[:5], ids=[m.__name__[1:] for m, _ in REFUSALS[:5]])
def test_dry_run_refuses_a_stage_build_and_prints_what_why_fix(
    project: Path, make: Any, code: str, capsys: pytest.CaptureFixture
) -> None:
    assert _rc(project, make(), "--mode", "amend-and-build", "--dry-run") != 0
    out = capsys.readouterr().out
    assert "why:" in out and "fix:" in out
    assert "Traceback" not in out
    assert not (project / "out").exists()


def test_a_stage_output_named_like_a_declared_input_is_refused() -> None:
    stage = _stage("one", "SELECT 1 AS a", ["Raw"])
    contract = _contract([stage], ["Raw"])
    contract["builds"][0]["properties"]["parameters"] = {
        "inputs": [{"name": "raw", "path": "data/raw.csv"}]
    }
    assert _refusal(contract).code == "build_stage_unsupported"
    provider = LocalProvider(project="t", region="local")
    with pytest.raises(LocalPlanError) as refused:
        provider._derive_actions_from_contract(contract)
    assert refused.value.code == "build_stage_unsupported"


def test_a_build_that_lands_no_expose_is_refused_by_name(
    project: Path, capsys: pytest.CaptureFixture
) -> None:
    contract = _contract([_stage("one", "SELECT 1 AS a", ["Aa"])], ["A"])  # misspelt
    assert _refusal(contract).code == "build_stage_lands_nothing"
    with pytest.raises(FluidUserError) as refused:
        _apply(project, contract)
    assert refused.value.code == "build_stage_lands_nothing"
    assert "'Aa'" in refused.value.why and "'A'" in refused.value.why
    assert _rc(project, contract, "--mode", "amend-and-build") != 0
    out = capsys.readouterr().out
    assert "nothing would land" in " ".join(out.split()), out
    assert not (project / "out").exists()


def _two_builds(stage_first: bool) -> Dict[str, Any]:
    staged = {
        "id": "staged",
        "pattern": "multi-stage",
        "engine": "sql",
        "properties": {
            "stages": [
                _stage("mk_a", "SELECT 1 AS a", ["A"]),
                _stage("mk_b", "SELECT 99 AS a", ["B"], ["mk_a"]),  # B: only intermediate here
            ]
        },
    }
    explicit = {
        "id": "explicit",
        "pattern": "embedded-logic",
        "engine": "sql",
        "properties": {"sql": "SELECT 2 AS a"},
        "outputs": ["B"],
    }
    contract = _contract([], ["A", "B"])
    contract["builds"] = [staged, explicit] if stage_first else [explicit, staged]
    return contract


@pytest.mark.parametrize("stage_first", [True, False], ids=["stage-first", "explicit-first"])
def test_an_explicit_outputs_mapping_beats_a_stage_derived_one(
    project: Path, stage_first: bool
) -> None:
    assert _apply(project, _two_builds(stage_first)) == 0
    assert _landed(project)["B"] == "a\n2\n"
    assert _landed(project)["A"] == "a\n1\n"


def test_a_stage_table_named_like_an_expose_the_outputs_omit_does_not_land_it(
    project: Path,
) -> None:
    stages = [_stage("one", "SELECT 1 AS a", ["A"]), _stage("two", "SELECT 2 AS a", ["B"])]
    contract = _contract(stages, ["A", "B"], outputs=["A"])
    with pytest.raises(FluidUserError) as refused:
        _apply(project, contract)
    assert refused.value.code == "local_plan_invalid"  # as the amend route, B is not landed
    assert not (project / "out").exists()


@pytest.mark.parametrize("derive", [False, True], ids=["plan", "amend"])
def test_later_stages_of_a_build_do_not_run_after_a_stage_fails(
    project: Path, derive: bool
) -> None:
    stages = [
        _stage("broken", "SELECT * FROM no_such_table", ["mid"]),
        _stage("later", "SELECT 5 AS a", ["A"], ["broken"]),
    ]
    contract = _contract(stages, ["A"])
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    actions = provider._derive_actions_from_contract(contract) if derive else plan_actions(contract)
    result = provider.apply(actions=actions, plan={"contract": contract})
    assert result["failed"] >= 1
    later = [r for r in result["results"] if r.get("reason") == "earlier_stage_failed"]
    assert len(later) == 1 and later[0]["skipped"] is True
    assert not (project / "out" / "A.csv").exists()


def test_a_non_stage_action_still_runs_after_a_failure(project: Path) -> None:
    provider = LocalProvider(project="t", region="local", anchor_dir=project)
    result = provider.apply(
        actions=[
            {"op": "sql", "sql": "SELECT * FROM nope", "output_table": "t1"},
            {"op": "sql", "sql": "SELECT 1 AS a", "output_table": "t2", "preview": False},
        ]
    )
    assert result["failed"] == 1 and result["results"][1]["status"] == "ok"
    assert not result["results"][1].get("skipped")


def test_stages_of_another_engine_are_left_to_its_runner() -> None:
    contract = _contract([_stage("a", "SELECT 1 AS a", ["A"])], ["A"], engine="dbt")
    build = contract["builds"][0]
    from fluid_build.providers.local.planner import is_multi_stage_build

    assert not is_multi_stage_build(build)
    assert not [a for a in plan_actions(contract) if a["op"] == "execute_sql"]
    provider = LocalProvider(project="t", region="local")
    with pytest.raises(FluidUserError) as derived:
        provider._derive_actions_from_contract(contract)
    assert derived.value.code == "build_has_no_sql"  # as before: not the stages' to run
    for engine in ("SQL", "", None):
        assert is_multi_stage_build({**build, "engine": engine})
