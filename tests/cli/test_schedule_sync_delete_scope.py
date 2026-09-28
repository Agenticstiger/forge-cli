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

"""``fluid schedule-sync --delete-scope``: deleting stale DAGs at the
destination never reaches another product's DAGs by default.

The file / ssh / git+ssh / s3 / gs transports mirror with deletion. Pointed at
a DAG root that several products share, the old ``rsync -av --delete src/
dest/`` deleted every file there that this product did not ship.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from fluid_build.cli import schedule_sync
from fluid_build.cli._common import CLIError


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: Dict[str, Any] = {
        "scheduler": "airflow",
        "dags_dir": "",
        "destination": None,
        "environment_name": None,
        "location": None,
        "workspace": None,
        "env": "dev",
        "dry_run": True,
        "timeout": 60,
        "report": None,
        "bundle": None,
        "verify_signature": False,
        "verify_key": None,
        "verify_identity_regexp": ".*",
        "verify_oidc_issuer_regexp": ".*",
        "git_commit_author": None,
        "delete_scope": "product",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _which(binary: str) -> str:
    return f"/bin/{binary}"


def _dispatch(dags_dir: Path, **overrides: Any) -> List[List[str]]:
    """Run the scheduler's dispatcher in dry-run mode; return the planned argvs."""
    args = _args(**overrides)
    with (
        patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
        patch.object(schedule_sync.subprocess, "run") as run,
    ):
        results = schedule_sync._DISPATCHERS[args.scheduler](dags_dir, args)
    run.assert_not_called()
    return [r["argv"] for r in results]


@pytest.fixture()
def two_products(tmp_path: Path) -> Path:
    """``schedule/`` as stage 3 writes it, for two products."""
    d = tmp_path / "schedule"
    for product, dag in (("orders", "load_dag.py"), ("bronze.customers", "ingest_dag.py")):
        (d / product).mkdir(parents=True)
        (d / product / dag).write_text("# dag\n", encoding="utf-8")
    return d


@pytest.fixture()
def flat(tmp_path: Path) -> Path:
    """A flat DAG directory, as ``fluid generate schedule`` writes by default."""
    d = tmp_path / "dags"
    d.mkdir()
    (d / "orders_dag.py").write_text("# dag\n", encoding="utf-8")
    return d


#: (scheduler, destination, the argv element that means "delete") for every
#: transport that deletes at the destination.
DELETING = [
    ("airflow", "/opt/airflow/dags", "--delete"),
    ("airflow", "ssh://u@host/opt/dags", "--delete"),
    ("airflow", "s3://b/dags/", "--delete"),
    ("airflow", "gs://b/dags/", "-d"),
    ("airflow", "git+ssh://git@example.com/org/dags.git", "--delete"),
    ("mwaa", "s3://mwaa/dags/", "--delete"),
]


class TestProductScopeIsTheDefault:
    def test_cli_flag_defaults_to_product(self) -> None:
        parser = argparse.ArgumentParser()
        schedule_sync.register(parser.add_subparsers())
        base = ["schedule-sync", "--scheduler", "airflow", "--dags-dir", "x"]
        for scope in ("product", "destination", "none"):
            assert parser.parse_args(base + ["--delete-scope", scope]).delete_scope == scope
        assert parser.parse_args(base).delete_scope == "product"
        with pytest.raises(SystemExit):
            parser.parse_args(base + ["--delete-scope", "everything"])

    def test_file_mirrors_each_product_into_its_own_directory(
        self, two_products: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "airflow" / "dags"
        src = str(two_products)
        root = str(dest.resolve())
        assert _dispatch(two_products, destination=str(dest)) == [
            ["/bin/rsync", "-av", "--delete", "--"]
            + [f"{src}/bronze.customers/", f"{root}/bronze.customers/"],
            ["/bin/rsync", "-av", "--delete", "--", f"{src}/orders/", f"{root}/orders/"],
        ]

    def test_every_deleting_transport_scopes_to_the_product(self, two_products: Path) -> None:
        ssh = _dispatch(two_products, destination="ssh://u@host/opt/dags")
        assert [a[-1] for a in ssh] == [
            "u@host:/opt/dags/bronze.customers/",
            "u@host:/opt/dags/orders/",
        ]
        s3 = _dispatch(two_products, destination="s3://b/dags")
        assert [a[3:] for a in s3] == [
            [f"{two_products}/bronze.customers/", "s3://b/dags/bronze.customers/", "--delete"],
            [f"{two_products}/orders/", "s3://b/dags/orders/", "--delete"],
        ]
        gs = _dispatch(two_products, destination="gs://b/dags/")
        assert [a[-2:] for a in gs] == [
            [f"{two_products}/bronze.customers/", "gs://b/dags/bronze.customers/"],
            [f"{two_products}/orders/", "gs://b/dags/orders/"],
        ]
        assert all("-d" in a for a in gs)
        mwaa = _dispatch(two_products, scheduler="mwaa", destination="s3://mwaa/dags/")
        assert [a[4] for a in mwaa] == [
            "s3://mwaa/dags/bronze.customers/",
            "s3://mwaa/dags/orders/",
        ]
        git = _dispatch(two_products, destination="git+ssh://git@example.com/org/dags.git")
        rsyncs = [a for a in git if a[0] == "/bin/rsync"]
        assert [a[-2:] for a in rsyncs] == [
            [f"{two_products}/bronze.customers/", "./bronze.customers/"],
            [f"{two_products}/orders/", "./orders/"],
        ]
        assert all("--delete" in a for a in rsyncs)

    def test_only_transports_that_delete_refuse_loose_files(self, flat: Path) -> None:
        for scheduler, destination, _flag in DELETING:
            with pytest.raises(CLIError) as exc:
                _dispatch(flat, scheduler=scheduler, destination=destination)
            assert exc.value.event == "schedule_sync_dags_dir_not_product_scoped", destination
            assert exc.value.context["loose_files"] == ["orders_dag.py"]
            assert "--delete-scope" in exc.value.context["hint"]
        # az and scp never delete, so a flat directory is safe for them.
        for destination in ("az://container/dags", "scp://u@host/opt/dags"):
            (argv,) = _dispatch(flat, destination=destination)
            assert "--delete" not in argv

    def test_refuses_a_loose_file_next_to_product_directories(self, two_products: Path) -> None:
        (two_products / "stray_dag.py").write_text("# dag\n", encoding="utf-8")
        for scheduler, destination, _flag in DELETING:
            with pytest.raises(CLIError) as exc:
                _dispatch(two_products, scheduler=scheduler, destination=destination)
            assert exc.value.exit_code == 2, destination
            assert exc.value.event == "schedule_sync_dags_dir_not_product_scoped"
            assert exc.value.context["loose_files"] == ["stray_dag.py"]

    def test_refuses_a_symlinked_product_directory(
        self, two_products: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("not a DAG\n", encoding="utf-8")
        (two_products / "linked").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(CLIError) as exc:
            _dispatch(two_products, destination="/opt/airflow/dags")
        assert exc.value.event == "schedule_sync_dags_dir_symlink"

    @pytest.mark.parametrize("name", [".hidden", "-rf", "white space", "semi;colon"])
    def test_refuses_a_product_directory_name_that_is_not_an_identifier(
        self, two_products: Path, name: str
    ) -> None:
        (two_products / name).mkdir()
        with pytest.raises(CLIError) as exc:
            _dispatch(two_products, destination="/opt/airflow/dags")
        assert exc.value.event == "schedule_sync_invalid_product_dir"

    def test_bytecode_cache_is_not_a_product(self, two_products: Path) -> None:
        (two_products / "__pycache__").mkdir()
        (two_products / "__pycache__" / "x.pyc").write_bytes(b"\0")
        argvs = _dispatch(two_products, destination="/opt/airflow/dags")
        assert [a[-1].rsplit("/", 2)[-2] for a in argvs] == ["bronze.customers", "orders"]

    def test_report_records_the_scope(self, two_products: Path, tmp_path: Path) -> None:
        report = tmp_path / "report.json"
        args = _args(
            dags_dir=str(two_products),
            destination=str(tmp_path / "dest"),
            report=str(report),
        )
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            assert schedule_sync.run(args) == 0
        assert '"delete_scope": "product"' in report.read_text()


class TestOtherScopes:
    def test_the_scope_decides_what_the_file_transport_may_delete(
        self, two_products: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "dags-root"
        root = f"{dest.resolve()}/"
        src = f"{two_products}/"
        assert len(_dispatch(two_products, destination=str(dest))) == 2  # product
        assert _dispatch(two_products, destination=str(dest), delete_scope="destination") == [
            ["/bin/rsync", "-av", "--delete", "--", src, root]
        ]
        assert _dispatch(two_products, destination=str(dest), delete_scope="none") == [
            ["/bin/rsync", "-av", "--", src, root]
        ]

    @pytest.mark.parametrize("scheduler,destination,delete_flag", DELETING)
    def test_none_scope_never_deletes(
        self, flat: Path, scheduler: str, destination: str, delete_flag: str
    ) -> None:
        argvs = _dispatch(flat, scheduler=scheduler, destination=destination, delete_scope="none")
        assert argvs
        assert all(delete_flag not in argv for argv in argvs)


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
class TestSharedDagRootWithRealRsync:
    """The measured failure: a DAG root several products share, synced for real."""

    def _shared_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "airflow-dags"
        (root / "orders").mkdir(parents=True)
        (root / "orders" / "removed_dag.py").write_text("# stale\n", encoding="utf-8")
        (root / "billing").mkdir()
        (root / "billing" / "billing_dag.py").write_text("# theirs\n", encoding="utf-8")
        (root / "legacy_dag.py").write_text("# theirs too\n", encoding="utf-8")
        return root

    def test_default_sync_keeps_every_other_products_dags(self, tmp_path: Path) -> None:
        dags = tmp_path / "dist" / "artifacts" / "schedule"
        (dags / "orders").mkdir(parents=True)
        (dags / "orders" / "load_dag.py").write_text("# ours\n", encoding="utf-8")
        (dags / "new.product").mkdir()
        (dags / "new.product" / "first_dag.py").write_text("# first\n", encoding="utf-8")
        root = self._shared_root(tmp_path)

        args = _args(dags_dir=str(dags), destination=str(root), dry_run=False)
        assert schedule_sync.run(args) == 0

        assert (root / "orders" / "load_dag.py").read_text() == "# ours\n"
        assert (root / "new.product" / "first_dag.py").exists(), "first sync of a product"
        assert not (root / "orders" / "removed_dag.py").exists(), "stale DAG must go"
        assert (root / "billing" / "billing_dag.py").exists(), "another product's DAG deleted"
        assert (root / "legacy_dag.py").exists(), "a DAG at the shared root deleted"

        # Mirroring onto the whole root is still there, when asked for.
        args = _args(
            dags_dir=str(dags), destination=str(root), dry_run=False, delete_scope="destination"
        )
        assert schedule_sync.run(args) == 0
        assert sorted(p.name for p in root.iterdir()) == ["new.product", "orders"]


# ── The DAG an env's directory replaced ───────────────────────────────────
#
# forge-cli 0.16.6 and earlier synced a product's DAGs to ``<product>/`` with
# dag id ``<product>__<build>``, whatever the env. An env's DAGs now live in
# ``<product>__<env>/`` as ``<product>__<env>__<build>``, and ``--delete-scope
# product`` mirrors only that directory. Measured on the demo's bronze product:
# after the first sync on the new release the destination held both DAGs, each
# ``FLUID_ENV_NAME = 'aws'`` and ``SCHEDULE = '0 */4 * * *'``, so Airflow ran two
# ``fluid apply --env aws`` of one product against one state at the same minute.

_PRODUCT = "bronze.customer_subscriptions"
_BUILD = "ingest_subscriptions"


def _dag(env: str, *, legacy: bool = False, build: str = _BUILD) -> str:
    from fluid_build.schedulers.airflow import fluid_apply

    text = fluid_apply.render_dag(
        product_id=_PRODUCT,
        build=fluid_apply.ScheduledBuild(
            build_id=build, schedule="0 */4 * * *", timezone="UTC", retries=1
        ),
        env=env or None,
        contract_path="contracts/customer_subscriptions/contract.fluid.yaml",
        env_names=[],
    )
    if legacy and env:
        # What 0.16.6 rendered: the same file, with no env in its dag id.
        new_id = fluid_apply.py_str_literal(f"{_PRODUCT}__{env}__{build}")
        assert text.count(new_id) == 1
        text = text.replace(new_id, fluid_apply.py_str_literal(f"{_PRODUCT}__{build}"))
    return text


def _env_artifacts(tmp_path: Path, env: str = "aws") -> Path:
    """``schedule/`` as stage 3 now writes it for ``--env <env>``."""
    dags = tmp_path / "dist" / "artifacts" / "schedule"
    scope = dags / f"{_PRODUCT}__{env}"
    scope.mkdir(parents=True)
    (scope / f"{_BUILD}_dag.py").write_text(_dag(env), encoding="utf-8")
    return dags


def _upgraded_root(tmp_path: Path) -> Path:
    """The lab's DAG root after a sync by 0.16.6, plus what must survive the retirement."""
    root = tmp_path / "airflow-dags"
    old = root / _PRODUCT
    old.mkdir(parents=True)
    (old / f"{_BUILD}_dag.py").write_text(_dag("aws", legacy=True), encoding="utf-8")
    (old / "removed_build_dag.py").write_text(
        _dag("aws", legacy=True, build="removed_build"), encoding="utf-8"
    )
    # Not this env's, not a legacy id, not a DAG: all stay.
    (old / "gcp_only_dag.py").write_text(
        _dag("gcp", legacy=True, build="gcp_only"), encoding="utf-8"
    )
    (old / "envless_dag.py").write_text(_dag("", build="envless"), encoding="utf-8")
    (old / "notes.py").write_text("# someone's helper\n", encoding="utf-8")
    (root / "billing").mkdir()
    (root / "billing" / "billing_dag.py").write_text("# theirs\n", encoding="utf-8")
    return root


def _unwrapped(text: str) -> str:
    """The console wraps long lines; compare without whitespace."""
    return "".join(text.split())


def _dag_ids(root: Path) -> List[str]:
    return sorted(
        facts["dag_id"]
        for path in root.rglob("*.py")
        if (facts := schedule_sync._dag_facts(path)) is not None
    )


class TestTheDagAnEnvDirectoryReplaced:
    def test_the_scope_and_the_old_dags_are_read_from_the_dag_files(self, tmp_path: Path) -> None:
        dags = _env_artifacts(tmp_path)
        assert schedule_sync._replaced_scopes(dags) == [(f"{_PRODUCT}__aws", _PRODUCT, "aws")]
        root = _upgraded_root(tmp_path)
        assert schedule_sync._superseded_dags(root / _PRODUCT, _PRODUCT, "aws") == [
            f"{_BUILD}_dag.py",
            "removed_build_dag.py",
        ]
        # A directory of the old layout, or of anything else, replaces nothing.
        assert schedule_sync._replaced_scopes(root) == []

    def test_the_dry_run_plans_the_retirement_after_the_sync(self, tmp_path: Path) -> None:
        dags = _env_artifacts(tmp_path)
        root = _upgraded_root(tmp_path)
        argvs = _dispatch(dags, destination=str(root))
        dest = f"{root.resolve()}/"
        assert argvs[0] == [
            "/bin/rsync",
            "-av",
            "--delete",
            "--",
            f"{dags}/{_PRODUCT}__aws/",
            f"{dest}{_PRODUCT}__aws/",
        ]
        retire = argvs[1]
        assert retire[:3] == ["/bin/rsync", "-rv", "--delete"]
        assert retire[3:6] == [
            f"--include=/{_BUILD}_dag.py",
            "--include=/removed_build_dag.py",
            "--exclude=*",
        ]
        assert retire[-1] == f"{dest}{_PRODUCT}/"
        assert len(argvs) == 2
        assert (root / _PRODUCT / f"{_BUILD}_dag.py").exists(), "a dry run deleted"

    @pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
    def test_the_first_sync_after_the_upgrade_retires_the_old_dag(self, tmp_path: Path) -> None:
        dags = _env_artifacts(tmp_path)
        root = _upgraded_root(tmp_path)
        report = tmp_path / "report.json"
        mode = (root / _PRODUCT).stat().st_mode

        args = _args(
            dags_dir=str(dags), destination=str(root), dry_run=False, env="aws", report=str(report)
        )
        assert schedule_sync.run(args) == 0

        assert _dag_ids(root) == sorted(
            [
                f"{_PRODUCT}__aws__{_BUILD}",
                f"{_PRODUCT}__envless",
                f"{_PRODUCT}__gcp_only",
            ]
        )
        assert (root / _PRODUCT / "notes.py").exists()
        assert (root / "billing" / "billing_dag.py").exists()
        assert (root / _PRODUCT).stat().st_mode == mode
        import json

        recorded = json.loads(report.read_text(encoding="utf-8"))["superseded_scopes"]
        assert recorded == [
            {
                "scope": f"{_PRODUCT}__aws",
                "replaces": _PRODUCT,
                "env": "aws",
                "old_dags_retired": True,
            }
        ]

        # The next sync finds nothing left to retire.
        assert len(_dispatch(dags, destination=str(root))) == 1

    def test_none_scope_retires_nothing_and_says_what_is_left(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dags = _env_artifacts(tmp_path)
        root = _upgraded_root(tmp_path)
        args = _args(dags_dir=str(dags), destination=str(root), delete_scope="none")
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            assert schedule_sync.run(args) == 0
        out = _unwrapped(capsys.readouterr().out)
        assert "--include=/" not in out
        assert _unwrapped(f"Delete {_PRODUCT}/'s DAG files for env aws") in out

    @pytest.mark.parametrize(
        "scheduler,destination",
        [
            ("airflow", "s3://b/dags/"),
            ("airflow", "gs://b/dags/"),
            ("airflow", "ssh://u@host/opt/dags"),
            ("mwaa", "s3://mwaa/dags/"),
        ],
    )
    def test_a_destination_that_cannot_be_read_here_gets_the_step_to_take(
        self, tmp_path: Path, scheduler: str, destination: str, capsys
    ) -> None:
        dags = _env_artifacts(tmp_path)
        report = tmp_path / "report.json"
        args = _args(
            dags_dir=str(dags), scheduler=scheduler, destination=destination, report=str(report)
        )
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            assert schedule_sync.run(args) == 0
        out = _unwrapped(capsys.readouterr().out)
        assert "--include=/" not in out
        assert _unwrapped(f"{_PRODUCT}__<build> beside {_PRODUCT}__aws__<build>") in out
        assert '"old_dags_retired": false' in report.read_text(encoding="utf-8")

    def test_mirroring_the_whole_destination_needs_no_note(self, tmp_path: Path, capsys) -> None:
        dags = _env_artifacts(tmp_path)
        args = _args(dags_dir=str(dags), destination="s3://b/dags/", delete_scope="destination")
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            assert schedule_sync.run(args) == 0
        assert "note:" not in capsys.readouterr().out

    def test_a_git_ssh_destination_retires_in_its_clone_before_the_commit(
        self, tmp_path: Path
    ) -> None:
        dags = _env_artifacts(tmp_path)
        upgraded = _upgraded_root(tmp_path)
        args = _args(
            dags_dir=str(dags),
            destination="git+ssh://git@example.com/org/dags.git",
            dry_run=False,
        )
        cwd_calls: List[List[str]] = []

        def _result(argv: List[str]) -> Dict[str, Any]:
            return {"argv": argv, "exit_code": 0, "stdout_tail": " M x", "stderr_tail": ""}

        def _clone(argv: List[str], **_kwargs: Any) -> Dict[str, Any]:
            # The clone holds what the last sync by 0.16.6 pushed.
            shutil.copytree(upgraded, argv[-1])
            return _result(argv)

        def _in_clone(argv: List[str], *, cwd: str, **_kwargs: Any) -> Dict[str, Any]:
            cwd_calls.append(argv)
            return _result(argv)

        with (
            patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
            patch.object(schedule_sync, "_run_subprocess", side_effect=_clone),
            patch.object(schedule_sync, "_run_subprocess_with_cwd", side_effect=_in_clone),
        ):
            schedule_sync._airflow_dispatch(dags, args)

        assert [argv[:2] for argv in cwd_calls[:3]] == [
            ["/bin/rsync", "-av"],
            ["/bin/rsync", "-rv"],
            ["/bin/git", "add"],
        ]
        retire = cwd_calls[1]
        assert retire[-1] == f"./{_PRODUCT}/"
        assert f"--include=/{_BUILD}_dag.py" in retire
        assert "--include=/envless_dag.py" not in retire
