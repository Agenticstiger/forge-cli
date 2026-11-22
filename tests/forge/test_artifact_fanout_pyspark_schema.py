# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate artifacts --emit pyspark-schema``: the PySpark schema modules in stage 3.

The modules are the ones ``fluid generate pyspark-schema`` writes, byte for byte (the same
renderer, planner and writer, ``fluid_build/spark/pyspark_schema.py``), under
``<out>/pyspark-schema/`` and in the MANIFEST, and stage 4 re-checks each one with the
module's own allowlist.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.forge.core.artifact_fanout import DEFAULT_EMIT, EMIT_KEYS, FanoutError, run_fanout
from fluid_build.forge.core.artifact_validators import (
    validate_artifacts,
    validate_pyspark_schema_module,
)
from fluid_build.spark import pyspark_schema

pytestmark = [pytest.mark.unit]

LOG = logging.getLogger("test.artifact_fanout_pyspark_schema")


def _port(port: str, columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "exposeId": port,
        "kind": "table",
        "binding": {"platform": "local", "format": "parquet", "location": {"path": f"./{port}"}},
        "contract": {"schema": columns},
    }


ORDERS = _port(
    "orders",
    [
        {"name": "order_id", "type": "string", "required": True, "description": "Primary key"},
        {"name": "amount", "type": "decimal(12,2)"},
        {"name": "placed_at", "type": "timestamp"},
    ],
)
EVENTS = _port("events", [{"name": "id", "type": "bigint"}])


def _write(directory: Path, *ports: Dict[str, Any], env: Optional[dict] = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "acme.sales.orders",
        "name": "Orders",
        "metadata": {"owner": {"team": "sales-data"}},
        "exposes": list(ports),
    }
    path = directory / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _fanout(contract: Path, out: Path, emit: Optional[str] = "pyspark-schema") -> Dict[str, Any]:
    return run_fanout(contract, out, emit_raw=emit, manifest_path=None, logger=LOG)


def _rows(manifest: Dict[str, Any]) -> List[str]:
    return sorted(p for p in manifest["files"] if p.startswith("pyspark-schema/"))


def test_pyspark_schema_is_an_emit_key_left_out_of_the_default_set() -> None:
    assert "pyspark-schema" in EMIT_KEYS
    assert "pyspark-schema" not in DEFAULT_EMIT


def test_the_modules_are_the_commands_own_and_in_the_manifest(tmp_path: Path) -> None:
    from fluid_build.cli import main

    contract = _write(tmp_path / "p", ORDERS, EVENTS)
    out = tmp_path / "out"
    manifest = _fanout(contract, out)
    assert _rows(manifest) == ["pyspark-schema/events_schema.py", "pyspark-schema/orders_schema.py"]

    command_out = tmp_path / "command"
    assert main(["generate", "pyspark-schema", str(contract), "--out", str(command_out)]) == 0
    for rel in _rows(manifest):
        data = (out / rel).read_bytes()
        assert data == (command_out / Path(rel).name).read_bytes()
        assert manifest["files"][rel] == "sha256:" + hashlib.sha256(data).hexdigest()


def test_the_default_run_writes_no_modules(tmp_path: Path) -> None:
    manifest = _fanout(_write(tmp_path / "p", ORDERS), tmp_path / "out", emit=None)
    assert _rows(manifest) == []
    assert not (tmp_path / "out" / "pyspark-schema").exists()


def test_stage_four_passes_strict_on_the_modules(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", ORDERS, EVENTS), out)
    report = validate_artifacts(out, strict=True)
    assert report.status == "pass", [(i.code, i.file, i.message) for i in report.issues]


def test_a_column_spark_cannot_hold_fails_the_stage_and_names_it(tmp_path: Path) -> None:
    contract = _write(tmp_path / "p", _port("geo", [{"name": "where", "type": "geography"}]))
    with pytest.raises(FanoutError) as raised:
        _fanout(contract, tmp_path / "out")
    assert raised.value.key == "pyspark-schema"
    assert raised.value.code == "unsupported_binding"
    assert "where" in str(raised.value)


def test_a_port_without_columns_is_skipped(tmp_path: Path) -> None:
    bare = {"exposeId": "bare", "kind": "table", "binding": ORDERS["binding"]}
    manifest = _fanout(_write(tmp_path / "p", ORDERS, bare), tmp_path / "out")
    assert _rows(manifest) == ["pyspark-schema/orders_schema.py"]


def test_a_file_this_command_did_not_write_is_left_alone_and_refused(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "pyspark-schema").mkdir(parents=True)
    mine = out / "pyspark-schema" / "orders_schema.py"
    mine.write_text("# my own module\nSCHEMA = None\n")
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", ORDERS), out)
    assert raised.value.code == "generate_pyspark_schema_foreign_file"
    assert mine.read_text() == "# my own module\nSCHEMA = None\n"


def test_an_earlier_runs_modules_are_cleared_and_other_files_kept(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", ORDERS, EVENTS), out)
    helper = out / "pyspark-schema" / "helpers.py"
    helper.write_text("X = 1\n")
    manifest = _fanout(_write(tmp_path / "p", ORDERS), out)
    assert _rows(manifest) == ["pyspark-schema/orders_schema.py"]
    assert not (out / "pyspark-schema" / "events_schema.py").exists()
    assert helper.read_text() == "X = 1\n"


def test_a_symlinked_module_left_in_the_directory_is_never_followed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", ORDERS), out)
    target = tmp_path / "elsewhere_schema.py"
    target.write_text(pyspark_schema.HEADER + "\n")
    link = out / "pyspark-schema" / "events_schema.py"
    link.symlink_to(target)
    with pytest.raises(FanoutError) as raised:
        _fanout(_write(tmp_path / "p", ORDERS, EVENTS), out)
    assert raised.value.code == "generate_pyspark_schema_foreign_file"
    assert target.read_text() == pyspark_schema.HEADER + "\n"


def test_a_link_planted_at_the_output_directory_is_removed_not_written_through(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    keep = victim / "orders_schema.py"
    keep.write_text(pyspark_schema.HEADER + "\n# data product: acme.sales.orders\nmine\n")
    (out / "pyspark-schema").symlink_to(victim, target_is_directory=True)
    manifest = _fanout(_write(tmp_path / "p", ORDERS), out)
    assert sorted(p.name for p in victim.iterdir()) == ["orders_schema.py"]
    assert keep.read_text().endswith("mine\n")
    assert not (out / "pyspark-schema").is_symlink()
    assert (out / "pyspark-schema" / "orders_schema.py").is_file()
    assert _rows(manifest) == ["pyspark-schema/orders_schema.py"]


def test_a_bundle_gives_the_modules_its_raw_contract_gives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from fluid_build.cli.bundle import run as bundle_run

    contract = _write(tmp_path / "p", ORDERS)
    monkeypatch.chdir(tmp_path / "p")
    tgz = tmp_path / "p" / "bundle.tgz"
    args = argparse.Namespace(contract=str(contract), out=str(tgz), env=None, format="tgz")
    assert bundle_run(args, logging.getLogger("test.bundle")) == 0
    from_bundle = _fanout(tgz, tmp_path / "a")
    from_contract = _fanout(contract, tmp_path / "b")
    assert {k: v for k, v in from_bundle["files"].items() if k.startswith("pyspark-schema/")} == {
        k: v for k, v in from_contract["files"].items() if k.startswith("pyspark-schema/")
    }


_PROD_OVERLAY = """\
exposes:
  - exposeId: orders
    contract:
      schema:
        - name: order_id
          type: string
          required: true
        - name: amount
          type: decimal(12,2)
        - name: placed_at
          type: timestamp
        - name: extra
          type: timestamp
"""


def test_fluid_env_does_not_overlay_the_modules_and_env_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$FLUID_ENV steers the DAGs and Spark job configs only; ``--env`` overlays the input, so
    the modules match the run's ODCS and ``fluid generate pyspark-schema`` either way."""
    from fluid_build.cli import main

    contract = _write(tmp_path / "p", ORDERS)
    (contract.parent / "overlays").mkdir()
    (contract.parent / "overlays" / "prod.yaml").write_text(_PROD_OVERLAY, encoding="utf-8")
    monkeypatch.chdir(contract.parent)
    module = "orders_schema.py"

    assert main(["generate", "pyspark-schema", str(contract), "--out", "base"]) == 0
    assert (
        main(["generate", "pyspark-schema", str(contract), "--env", "prod", "--out", "prod"]) == 0
    )
    base, prod = Path("base", module).read_bytes(), Path("prod", module).read_bytes()
    assert base != prod and b"extra" in prod

    monkeypatch.setenv("FLUID_ENV", "prod")
    emit = ["--emit", "pyspark-schema,odcs"]
    assert main(["generate", "artifacts", str(contract), "--out", "a1", *emit]) == 0
    assert Path("a1", "pyspark-schema", module).read_bytes() == base
    assert b"extra" not in Path("a1", "odcs", "product.odcs.orders.yaml").read_bytes()

    assert (
        main(["generate", "artifacts", str(contract), "--env", "prod", "--out", "a2", *emit]) == 0
    )
    assert Path("a2", "pyspark-schema", module).read_bytes() == prod


# ── stage 4 ───────────────────────────────────────────────────────────────


def _module(tmp_path: Path) -> bytes:
    out = tmp_path / "m"
    _fanout(_write(tmp_path / "mp", ORDERS), out)
    return (out / "pyspark-schema" / "orders_schema.py").read_bytes()


def test_stage_four_accepts_a_generated_module(tmp_path: Path) -> None:
    assert (
        validate_pyspark_schema_module("pyspark-schema/orders_schema.py", _module(tmp_path)) == []
    )


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda b: b + b"import os\nos.system('id')\n", "PYSPARK-SCHEMA-SHAPE"),
        (lambda b: b.replace(b"StructField(", b"eval(", 1), "PYSPARK-SCHEMA-SHAPE"),
        (lambda b: b.split(b"\n", 1)[1], "PYSPARK-SCHEMA-SHAPE"),
        (lambda b: b"\xff\xfe" + b, "PYSPARK-SCHEMA-ENCODING"),
        (lambda b: b + b"(", "PYSPARK-SCHEMA-SHAPE"),
    ],
)
def test_stage_four_refuses_a_module_that_holds_more_than_its_schema(
    tmp_path: Path, change: Any, code: str
) -> None:
    issues = validate_pyspark_schema_module(
        "pyspark-schema/orders_schema.py", change(_module(tmp_path))
    )
    assert [i.code for i in issues] == [code]
    assert all(i.severity == "error" for i in issues)


@pytest.mark.parametrize("path", ["pyspark-schema/orders.py", "pyspark-schema/x/orders_schema.py"])
def test_stage_four_refuses_a_file_this_emitter_never_writes(tmp_path: Path, path: str) -> None:
    issues = validate_pyspark_schema_module(path, _module(tmp_path))
    assert [i.code for i in issues] == ["PYSPARK-SCHEMA-PATH"]


def test_a_tampered_module_with_a_rewritten_manifest_still_fails_stage_four(
    tmp_path: Path,
) -> None:
    """Re-hashing the MANIFEST hides a changed module from the tamper gate, not from this check."""
    from fluid_build.forge.core.bundle import build_manifest

    out = tmp_path / "out"
    _fanout(_write(tmp_path / "p", ORDERS), out)
    module = out / "pyspark-schema" / "orders_schema.py"
    module.write_bytes(module.read_bytes() + b"__import__('os').system('id')\n")
    files = {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in out.rglob("*")
        if p.is_file() and p.name != "MANIFEST.json"
    }
    (out / "MANIFEST.json").write_text(json.dumps(build_manifest(files)))
    report = validate_artifacts(out, strict=True)
    assert report.status == "fail"
    assert "PYSPARK-SCHEMA-SHAPE" in {i.code for i in report.issues}


def test_the_cli_writes_the_modules_through_the_real_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    contract = _write(tmp_path / "p", ORDERS)
    out = tmp_path / "out"
    rc = main(
        ["generate", "artifacts", str(contract), "--out", str(out), "--emit", "pyspark-schema"]
    )
    assert rc == 0, capsys.readouterr()
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert _rows(manifest) == ["pyspark-schema/orders_schema.py"]


def test_the_cli_reports_a_column_spark_cannot_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    contract = _write(tmp_path / "p", _port("geo", [{"name": "where", "type": "geography"}]))
    rc = main(
        [
            "generate",
            "artifacts",
            str(contract),
            "--out",
            str(tmp_path / "o"),
            "--emit",
            "pyspark-schema",
        ]
    )
    text = " ".join(capsys.readouterr().out.split())
    assert rc == 1
    assert "unsupported_binding" in text and "where" in text
