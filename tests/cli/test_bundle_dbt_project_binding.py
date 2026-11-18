# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The dbt code a build runs is bundled, bound by the plan, and checked before it runs.

Pipeline stages 1-7 through the real command ``run()``s and the real parser:

* ``fluid bundle --format tgz`` packs the build's dbt project under
  ``sources/dbt/<build id>/``: the files dbt reads, never ``profiles.yml``,
  ``target/``, ``logs/``, ``dbt_packages/`` or a hidden file; twice on the same
  tree, byte-identical;
* ``fluid validate`` and ``fluid generate artifacts`` / ``validate-artifacts``
  accept the bundle;
* ``fluid plan`` records the tree under ``planDigest`` (``dbtProjects``);
* ``fluid apply`` refuses a checkout whose dbt files changed after the bundle
  was made, with ``apply_plan_digest_bundle_mismatch``, before dbt starts, and
  the dbt runner re-checks the directory it runs just before it does.

dbt itself is a stub named by ``DBT_EXECUTABLE`` that records each run, so
"dbt did not run" is asserted directly.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tarfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import build_parser
from fluid_build.cli._common import CLIError

LOG = logging.getLogger("test.bundle_dbt_project_binding")

CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.orders_dbt
name: Orders (dbt)
description: A dbt build whose project the bundle binds.
domain: Demo
metadata:
  layer: Silver
  owner: {team: data-platform, email: data-platform@acme-corp.io}
builds:
  - id: transform
    description: Orders model.
    pattern: hybrid-reference
    engine: dbt-duckdb
    repository: ./dbt
    properties:
      model: orders
    execution:
      trigger: {type: manual}
      runtime: {platform: local}
    outputs: [orders]
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: local
      format: parquet
      location: {path: ./out/orders.parquet}
    contract:
      schema:
        - {name: id, type: INTEGER, required: true}
        - {name: amount, type: DOUBLE, required: false}
"""

#: The files dbt reads, as the bundle must hold them.
DBT_FILES = {
    "dbt_project.yml": "name: orders_dbt\nversion: '1.0.0'\nprofile: orders_dbt\n",
    "packages.yml": "packages: []\n",
    "models/orders.sql": "select 1 as id, 9.5 as amount\n",
    "macros/cents.sql": "{% macro cents(x) %}({{ x }} * 100){% endmacro %}\n",
    "seeds/rates.csv": "code,rate\nEUR,1\n",
    "tests/no_negative.sql": "select * from {{ ref('orders') }} where amount < 0\n",
}

#: What the checkout also holds, and the bundle never takes.
NEVER_PACKED = {
    "profiles.yml": "orders_dbt: {outputs: {dev: {type: duckdb, password: s3cret}}}\n",
    "target/manifest.json": "{}\n",
    "logs/dbt.log": "log\n",
    "dbt_packages/pkg/macros/x.sql": "select 1\n",
    ".user.yml": "id: 1\n",
    "models/.scratch.sql": "select 2\n",
}


def _write(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
    (tmp_path / "contract.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    _write(tmp_path / "dbt", {**DBT_FILES, **NEVER_PACKED})
    (tmp_path / "runtime").mkdir()
    # dbt: a stub that records each run.
    stub = tmp_path / "bin" / "dbt"
    stub.parent.mkdir()
    stub.write_text('#!/bin/sh\necho "$@" >> "$FLUID_TEST_DBT_RUNS"\nexit 0\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("DBT_EXECUTABLE", str(stub))
    monkeypatch.setenv("FLUID_TEST_DBT_RUNS", str(tmp_path / "dbt_runs.log"))
    return tmp_path


def _fluid(*argv: str) -> int:
    args = build_parser().parse_args(list(argv))
    return args.func(args, LOG)


def _bundle(out: str = "runtime/bundle.tgz") -> Path:
    assert _fluid("bundle", "contract.fluid.yaml", "--format", "tgz", "--out", out) == 0
    return Path(out)


def _plan(bundle: Path) -> Dict[str, Any]:
    out = "runtime/plan.json"
    assert _fluid("plan", str(bundle), "--mode", "amend-and-build", "--out", out) == 0
    return json.loads(Path(out).read_text(encoding="utf-8"))


def _apply(*extra: str) -> int:
    return _fluid(
        "apply",
        "runtime/plan.json",
        "--bundle",
        "runtime/bundle.tgz",
        "--mode",
        "amend-and-build",
        "--yes",
        *extra,
    )


def _dbt_runs(ws: Path) -> List[str]:
    log = ws / "dbt_runs.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _names(tgz: Path) -> List[str]:
    with tarfile.open(tgz, "r:gz") as tar:
        return sorted(m.name for m in tar.getmembers() if m.isfile())


class TestBundle:
    def test_packs_the_files_dbt_reads_and_nothing_else(self, ws: Path) -> None:
        names = _names(_bundle())
        packed = sorted(n[len("sources/dbt/transform/") :] for n in names if "/dbt/" in n)
        assert packed == sorted(DBT_FILES)
        assert names == sorted(
            ["MANIFEST.json", "contract.resolved.json", "contract.resolved.yaml"]
            + [f"sources/dbt/transform/{rel}" for rel in DBT_FILES]
        )

    def test_rebundling_the_same_tree_is_byte_identical(self, ws: Path) -> None:
        first = _bundle("runtime/a.tgz").read_bytes()
        (ws / "dbt" / "target" / "run_results.json").write_text("{}", encoding="utf-8")
        assert _bundle("runtime/b.tgz").read_bytes() == first

    def test_says_what_it_packed(self, ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _bundle()
        assert "dbt project: transform (6 files, sources/dbt/transform/)" in (
            capsys.readouterr().err
        )

    def test_a_missing_project_fails_the_bundle(
        self, ws: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (ws / "dbt" / "dbt_project.yml").unlink()
        rc = _fluid("bundle", "contract.fluid.yaml", "--format", "tgz", "--out", "runtime/b.tgz")
        assert rc == 1
        err = capsys.readouterr().err
        assert "build 'transform': no dbt_project.yml" in err
        assert not Path("runtime/b.tgz").exists()

    def test_a_linked_file_fails_the_bundle(
        self, ws: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (ws / "secret.sql").write_text("select 'secret'", encoding="utf-8")
        os.symlink(ws / "secret.sql", ws / "dbt" / "models" / "leak.sql")
        rc = _fluid("bundle", "contract.fluid.yaml", "--format", "tgz", "--out", "runtime/b.tgz")
        assert rc == 1
        assert "models/leak.sql in the dbt project is a symbolic link" in capsys.readouterr().err


class TestLaterStagesAcceptTheBundle:
    def test_validate_strict_passes_and_reports_the_project(
        self, ws: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bundle = _bundle()
        capsys.readouterr()
        assert _fluid("validate", str(bundle), "--strict", "--format", "json") == 0
        report = json.loads(capsys.readouterr().out)
        codes = [issue["code"] for issue in report["issues"]]
        assert "DBT-PROJECT-BOUND" in codes
        assert "BUNDLE-UNEXPECTED" not in codes

    def test_generate_artifacts_and_validate_artifacts_pass(self, ws: Path) -> None:
        bundle = _bundle()
        out = "dist/artifacts"
        assert _fluid("generate", "artifacts", str(bundle), "--out", out, "--emit", "odcs") == 0
        assert _fluid("validate-artifacts", out) == 0


class TestPlanBindsTheTree:
    def test_the_plan_records_each_file(self, ws: Path) -> None:
        plan = _plan(_bundle())
        assert plan["bindingMode"] == "bound"
        assert sorted(plan["dbtProjects"]["transform"]["files"]) == sorted(DBT_FILES)


class TestApplyRefusesAChangedCheckout:
    def test_an_unchanged_checkout_runs_dbt(self, ws: Path) -> None:
        _plan(_bundle())
        assert _apply() == 0
        assert len(_dbt_runs(ws)) == 1

    @pytest.mark.parametrize(
        "edit, named",
        [
            (lambda d: (d / "models" / "orders.sql").write_text("select 2", "utf-8"), "changed"),
            (lambda d: (d / "models" / "new.sql").write_text("select 3", "utf-8"), "added"),
            (lambda d: (d / "macros" / "cents.sql").unlink(), "removed"),
        ],
        ids=["model-edited", "model-added", "macro-removed"],
    )
    def test_a_change_after_the_plan_is_refused_before_dbt(
        self, ws: Path, edit, named: str
    ) -> None:
        _plan(_bundle())
        edit(ws / "dbt")
        with pytest.raises(CLIError) as exc:
            _apply()
        assert exc.value.event == "apply_plan_digest_bundle_mismatch"
        assert exc.value.context["kind"] == "bundle-mismatch"
        assert f"{named} " in exc.value.context["error"]
        assert _dbt_runs(ws) == []

    def test_a_change_to_a_file_dbt_never_reads_is_not(self, ws: Path) -> None:
        _plan(_bundle())
        (ws / "dbt" / "profiles.yml").write_text("rotated: true\n", encoding="utf-8")
        (ws / "dbt" / "target" / "manifest.json").write_text('{"new": 1}', encoding="utf-8")
        assert _apply() == 0

    def test_a_bundle_applied_directly_is_checked_too(self, ws: Path) -> None:
        bundle = _bundle()
        (ws / "dbt" / "models" / "orders.sql").write_text("select 2", encoding="utf-8")
        with pytest.raises(CLIError) as exc:
            _fluid("apply", str(bundle), "--mode", "amend-and-build", "--yes")
        assert exc.value.event == "apply_plan_digest_bundle_mismatch"
        assert _dbt_runs(ws) == []

    def test_the_waiver_skips_the_check_and_says_so(
        self, ws: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _plan(_bundle())
        (ws / "dbt" / "models" / "orders.sql").write_text("select 2", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert _apply("--no-verify-plan-binding") == 0
        assert "plan-binding verification was SKIPPED" in caplog.text
        assert len(_dbt_runs(ws)) == 1

    def test_the_runner_rechecks_the_directory_it_runs(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that changes after the gate passed (while the apply provisions) is
        refused by the dbt runner itself, with the gate's event."""
        from fluid_build.cli import apply as apply_mod

        _plan(_bundle())
        gate = apply_mod._bind_dbt_projects

        def gate_then_edit(*args: Any, **kwargs: Any) -> None:
            gate(*args, **kwargs)
            (ws / "dbt" / "models" / "orders.sql").write_text("select 2", encoding="utf-8")

        monkeypatch.setattr(apply_mod, "_bind_dbt_projects", gate_then_edit)
        with pytest.raises(CLIError) as exc:
            _apply()
        assert exc.value.event == "apply_plan_digest_bundle_mismatch"
        assert "dbt did not run" in exc.value.context["error"]
        assert _dbt_runs(ws) == []

    def test_a_project_gone_from_the_checkout_is_refused(self, ws: Path) -> None:
        _plan(_bundle())
        (ws / "dbt" / "dbt_project.yml").unlink()
        with pytest.raises(CLIError) as exc:
            _apply()
        assert exc.value.event == "apply_plan_digest_bundle_mismatch"
        assert "there is none where the build runs it" in exc.value.context["error"]

    def test_a_plan_whose_checkout_cannot_be_found_is_refused(self, ws: Path) -> None:
        _plan(_bundle())
        (ws / "contract.fluid.yaml").rename(ws / "moved.fluid.yaml")
        with pytest.raises(CLIError) as exc:
            _apply()
        assert exc.value.event == "apply_plan_digest_bundle_mismatch"
        assert "cannot be found: the plan records" in exc.value.context["error"]
        assert _dbt_runs(ws) == []

    def test_the_binding_ends_with_the_apply(self, ws: Path) -> None:
        from fluid_build.build_runners.dbt.project_files import bound_project

        _plan(_bundle())
        assert _apply() == 0
        assert bound_project("transform") is None


class TestGeneratedTestsAreBound:
    """CI runs ``fluid generate dbt-tests`` before ``fluid bundle``: the generated
    schema.yml lands in the project's models, so the bundle packs it and the plan
    binds it like any model."""

    def test_generated_tests_are_packed_and_bound(self, ws: Path) -> None:
        schema = ws / "dbt" / "models" / "schema.yml"
        assert _fluid("generate", "dbt-tests", "contract.fluid.yaml", "-o", str(schema)) == 0
        assert "not_null" in schema.read_text(encoding="utf-8")
        bundle = _bundle()
        assert "sources/dbt/transform/models/schema.yml" in _names(bundle)
        plan = _plan(bundle)
        assert "models/schema.yml" in plan["dbtProjects"]["transform"]["files"]
        assert _apply() == 0

        # Dropping a generated test after the plan is a change like any other.
        schema.write_text(schema.read_text(encoding="utf-8").replace("not_null", "unique"), "utf-8")
        with pytest.raises(CLIError) as exc:
            _apply()
        assert exc.value.event == "apply_plan_digest_bundle_mismatch"
        assert "changed models/schema.yml" in exc.value.context["error"]
