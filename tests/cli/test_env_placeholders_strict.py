# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An unset ``{{ env.NAME }}`` stops the stages that would deploy it, and ``validate`` says so.

``fluid generate iac`` wrote ``"bucket": "{{ env.S3_BUCKET }}"`` into ``main.tf.json`` and
exited 0; ``fluid diff`` read a path whose first half was missing as a file "to be created";
``fluid validate`` said nothing. Each site below is one of the places the resolver
(:mod:`fluid_build.util.env_templates`) now decides, run through the real command.
"""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_BASE = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "demo.orders",
    "name": "Orders",
    "domain": "sales",
    "metadata": {"layer": "Bronze", "owner": {"team": "demo", "email": "data-team@telco.org"}},
}


def _contract(tmp_path: Path, binding: dict, name: str = "contract.fluid.yaml") -> Path:
    doc = {
        **_BASE,
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "version": "1.0.0",
                "binding": binding,
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _aws(tmp_path: Path) -> Path:
    return _contract(
        tmp_path,
        {
            "platform": "aws",
            "format": "parquet",
            "location": {"bucket": "{{ env.S3_BUCKET }}", "path": "orders/", "region": "eu-west-1"},
        },
    )


def _local(tmp_path: Path) -> Path:
    return _contract(
        tmp_path,
        {"platform": "local", "format": "parquet", "location": {"path": "{{ env.DIR }}/x.parquet"}},
        name="local.fluid.yaml",
    )


@pytest.fixture(autouse=True)
def _no_ambient_variables(monkeypatch):
    for name in ("S3_BUCKET", "DIR"):
        monkeypatch.delenv(name, raising=False)


def _run(argv, capsys):
    from fluid_build.cli import main

    rc = main(argv)
    out = capsys.readouterr()
    return rc, out.out + out.err


# ── generate iac ───────────────────────────────────────────────────────────


def test_generate_iac_refuses_an_unset_variable_and_writes_nothing(tmp_path, capsys):
    out_dir = tmp_path / "iac"
    rc, shown = _run(["generate", "iac", str(_aws(tmp_path)), "--out", str(out_dir)], capsys)

    assert rc == 1
    assert "S3_BUCKET" in shown and "exposes[0].binding.location.bucket" in shown
    assert not (out_dir / "main.tf.json").exists()


def test_generate_iac_refuses_an_empty_variable(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "")
    rc, shown = _run(
        ["generate", "iac", str(_aws(tmp_path)), "--out", str(tmp_path / "iac")], capsys
    )
    assert rc == 1 and "S3_BUCKET" in shown


def test_generate_iac_renders_a_variable_that_is_set(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    out_dir = tmp_path / "iac"
    rc, _ = _run(["generate", "iac", str(_aws(tmp_path)), "--out", str(out_dir)], capsys)

    assert rc == 0
    document = json.loads((out_dir / "main.tf.json").read_text())
    assert '"bucket": "my-bucket"' in json.dumps(document, indent=2)
    assert "{{ env." not in json.dumps(document)


# ── diff ───────────────────────────────────────────────────────────────────


def test_diff_refuses_an_unset_variable_instead_of_comparing_the_wrong_file(tmp_path, capsys):
    rc, shown = _run(["diff", str(_local(tmp_path))], capsys)
    assert rc == 1
    assert "DIR" in shown and "absent (to be created)" not in shown


# ── apply, OpenTofu engine ─────────────────────────────────────────────────


def test_the_opentofu_engine_refuses_an_unset_variable_before_it_emits(tmp_path):
    from fluid_build.cli._apply_opentofu_engine import _load_contract
    from fluid_build.util.env_templates import EnvTemplateError

    args = Namespace(contract=str(_aws(tmp_path)), env=None)
    with pytest.raises(EnvTemplateError) as info:
        _load_contract(args, logging.getLogger("test"))
    assert info.value.extras["missing"] == ["S3_BUCKET"]


def test_the_opentofu_engine_renders_a_variable_that_is_set(tmp_path, monkeypatch):
    from fluid_build.cli._apply_opentofu_engine import _load_contract

    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    contract = _load_contract(
        Namespace(contract=str(_aws(tmp_path)), env=None), logging.getLogger("t")
    )
    assert contract["exposes"][0]["binding"]["location"]["bucket"] == "my-bucket"


# ── apply, native engine ───────────────────────────────────────────────────


def test_apply_on_the_native_engine_refuses_an_unset_variable_instead_of_writing_to_the_root(
    tmp_path, capsys, monkeypatch
):
    """``{{ env.DIR }}/x.parquet`` with ``DIR`` unset was ``/x.parquet``: apply wrote there."""
    monkeypatch.chdir(tmp_path)
    rc, shown = _run(["apply", str(_local(tmp_path)), "--yes"], capsys)

    assert rc == 1
    assert "DIR" in shown and "not set or empty" in shown
    assert not Path("/x.parquet").exists()
    assert not (tmp_path / "x.parquet").exists()


def test_apply_on_a_cloud_contract_refuses_an_unset_variable_instead_of_routing_to_the_native_engine(
    tmp_path, capsys
):
    """Choosing the engine renders the contract: a failure to classify it falls back to the
    native engine, which applied the contract with the placeholder unrendered and exit 0."""
    rc, shown = _run(["apply", str(_aws(tmp_path)), "--dry-run", "--yes"], capsys)

    assert rc == 1
    assert "S3_BUCKET" in shown and "not set or empty" in shown


def test_the_engine_choice_does_not_swallow_an_unset_variable(tmp_path):
    from fluid_build.cli._apply_opentofu_engine import resolve_apply_engine
    from fluid_build.util.env_templates import EnvTemplateError

    args = Namespace(contract=str(_aws(tmp_path)), env=None, provider=None)
    with pytest.raises(EnvTemplateError):
        resolve_apply_engine(args, logging.getLogger("test"))


def test_a_dry_run_on_the_native_engine_refuses_an_unset_variable_too(
    tmp_path, capsys, monkeypatch
):
    """The provider's planner failure is not a reason to fall back and show a plan for a
    contract whose path has its first half missing."""
    monkeypatch.chdir(tmp_path)
    rc, shown = _run(["apply", str(_local(tmp_path)), "--dry-run", "--yes"], capsys)

    assert rc == 1
    assert "DIR" in shown and "not set or empty" in shown


def test_generate_vector_keeps_the_error_whole(tmp_path, capsys):
    """The field list and the fix are in what the user sees, not only the first line."""
    rc, shown = _run(
        ["generate", "vector", str(_aws(tmp_path)), "--out", str(tmp_path / "v")], capsys
    )

    assert rc == 1
    assert "exposes[0].binding.location.bucket" in shown and "Export S3_BUCKET" in shown


def test_verify_makes_an_unset_variable_that_exposes_result_and_checks_the_rest(
    tmp_path, capsys, monkeypatch
):
    """One expose naming an unset variable does not end the run, lose the other exposes' results
    or the --out report."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.csv").write_text("id,name\n1,a\n", encoding="utf-8")
    doc = {
        **_BASE,
        "exposes": [
            {
                "exposeId": expose_id,
                "kind": "table",
                "version": "1.0.0",
                "binding": {"platform": "local", "format": "csv", "location": {"path": path}},
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
            for expose_id, path in (("unset_one", "{{ env.DIR }}/x.csv"), ("fine_one", "data.csv"))
        ],
    }
    contract = tmp_path / "two.fluid.yaml"
    contract.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    report = tmp_path / "report.json"

    rc, shown = _run(["verify", str(contract), "--out", str(report)], capsys)

    assert rc == 1
    assert "unset_one" in shown and "DIR" in shown and "fine_one" in shown
    assert report.exists()
    results = json.loads(report.read_text())
    assert "fine_one" in json.dumps(results) and "unset_one" in json.dumps(results)


# ── readers of a local path ────────────────────────────────────────────────


def test_a_binding_path_with_an_unset_variable_is_an_error_not_a_path_with_its_first_half_missing(
    tmp_path, monkeypatch
):
    from fluid_build.util.binding_paths import resolve_binding_path
    from fluid_build.util.env_templates import EnvTemplateError

    with pytest.raises(EnvTemplateError) as info:
        resolve_binding_path("{{ env.DIR }}/x.parquet", tmp_path)
    assert info.value.extras["missing"] == ["DIR"]
    monkeypatch.setenv("DIR", "data")
    assert resolve_binding_path("{{ env.DIR }}/x.parquet", tmp_path) == str(
        tmp_path / "data/x.parquet"
    )


# ── validate ───────────────────────────────────────────────────────────────


def test_validate_warns_about_an_unset_variable_and_names_where(tmp_path, capsys):
    rc, shown = _run(["validate", str(_aws(tmp_path))], capsys)
    assert rc == 0
    assert "{{ env.S3_BUCKET }}" in shown
    assert "exposes[0].binding.location.bucket" in shown


def test_validate_strict_makes_it_an_error(tmp_path, capsys):
    rc, shown = _run(["validate", str(_aws(tmp_path)), "--strict"], capsys)
    assert rc != 0 and "S3_BUCKET" in shown


def test_validate_says_nothing_when_the_variable_is_set(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    rc, shown = _run(["validate", str(_aws(tmp_path)), "--strict"], capsys)
    assert rc == 0 and "env.S3_BUCKET" not in shown


def test_validate_does_not_ask_for_a_credential_shaped_variable(tmp_path, capsys, monkeypatch):
    """Policy refuses to render one, so a missing value is not what is wrong with it."""
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)
    path = _contract(
        tmp_path,
        {
            "platform": "aws",
            "format": "parquet",
            "location": {"bucket": "b", "path": "{{ env.WAREHOUSE_PASSWORD }}/x"},
        },
    )
    rc, shown = _run(["validate", str(path), "--strict"], capsys)
    assert rc == 0 and "WAREHOUSE_PASSWORD" not in shown
