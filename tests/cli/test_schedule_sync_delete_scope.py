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
