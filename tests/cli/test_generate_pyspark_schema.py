# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate pyspark-schema``: a StructType module for each output port.

Through the real entry point: the files it writes, what it leaves alone, ``--check`` for CI,
and the refusals. The emitter itself is covered in ``tests/spark/test_pyspark_schema.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

pytestmark = pytest.mark.unit


def _column(name: str, type_: str = "string", **extra: Any) -> Dict[str, Any]:
    return {"name": name, "type": type_, **extra}


def _port(port: str, *columns: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "exposeId": port,
        "kind": "table",
        "binding": {"platform": "local", "format": "parquet", "location": {"path": "./x"}},
        "contract": {"schema": list(columns)},
    }


def _write(
    tmp_path: Path,
    *ports: Dict[str, Any],
    name: str = "c.fluid.yaml",
    product: str = "acme.sales.orders",
) -> Path:
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": product,
        "name": "Orders",
        "exposes": list(ports),
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _main(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def _text(capsys: pytest.CaptureFixture) -> str:
    return " ".join(capsys.readouterr().out.split())


ORDERS = _port(
    "orders",
    _column("order_id", required=True, description="Primary key"),
    _column("amount", "decimal(12,2)"),
)


def test_the_subcommand_is_registered_with_its_defaults():
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["generate", "pyspark-schema", "c.fluid.yaml"])

    assert callable(args.func)
    assert (args.out, args.check, args.env) == ("runtime/pyspark_schema", False, None)


def test_it_writes_a_module_for_each_port_and_says_where(tmp_path: Path, capsys):
    contract = _write(tmp_path, ORDERS, _port("events", _column("id", "bigint")))
    out = tmp_path / "schemas"

    rc = _main("generate", "pyspark-schema", str(contract), "--out", str(out))

    text = _text(capsys)
    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ["events_schema.py", "orders_schema.py"]
    assert "Wrote 2 PySpark schema module(s)" in text
    assert "orders_schema.py output port 'orders', 2 columns (new)" in text
    assert "events_schema.py output port 'events', 1 column (new)" in text
    assert "from orders_schema import SCHEMA" in text and "spark-submit --py-files" in text
    assert 'StructField("order_id", StringType(), nullable=False)' in (
        out / "orders_schema.py"
    ).read_text(encoding="utf-8")


def test_a_second_run_changes_nothing_and_a_changed_contract_updates_the_module(
    tmp_path: Path, capsys
):
    contract = _write(tmp_path, ORDERS)
    out = tmp_path / "schemas"
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    module = out / "orders_schema.py"
    first = module.read_bytes()
    capsys.readouterr()

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    assert "(unchanged)" in _text(capsys) and module.read_bytes() == first

    _write(tmp_path, _port("orders", _column("order_id"), _column("added", "bigint")))
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    assert "(updated)" in _text(capsys) and b'"added"' in module.read_bytes()


# ── --check ─────────────────────────────────────────────────────────────


def test_check_fails_for_a_module_that_is_missing_and_writes_nothing(tmp_path: Path, capsys):
    contract = _write(tmp_path, ORDERS)
    out = tmp_path / "schemas"

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check")

    text = _text(capsys)
    assert rc == 1
    assert "ERR_GENERATE_PYSPARK_SCHEMA_DRIFT" in text and "missing: orders_schema.py" in text
    assert not out.exists()


def test_check_passes_for_current_modules_and_fails_for_one_that_differs(tmp_path: Path, capsys):
    contract = _write(tmp_path, ORDERS)
    out = tmp_path / "schemas"
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    capsys.readouterr()

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check") == 0
    assert "are up to date (1)" in _text(capsys)

    _write(tmp_path, _port("orders", _column("order_id", "bigint")))
    before = (out / "orders_schema.py").read_bytes()
    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check")

    assert rc == 1
    assert "differs: orders_schema.py" in _text(capsys)
    assert (out / "orders_schema.py").read_bytes() == before  # --check never writes


def test_check_refuses_a_file_it_did_not_write_like_a_run_does(tmp_path: Path, capsys):
    contract = _write(tmp_path, ORDERS)
    out = tmp_path / "schemas"
    out.mkdir()
    (out / "orders_schema.py").write_text("SCHEMA = 1\n", encoding="utf-8")

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check")

    assert rc == 1 and "ERR_GENERATE_PYSPARK_SCHEMA_FOREIGN_FILE" in _text(capsys)


# ── What it leaves alone ────────────────────────────────────────────────


def test_a_file_it_did_not_write_is_never_overwritten_and_nothing_else_is_written(
    tmp_path: Path, capsys
):
    contract = _write(tmp_path, ORDERS, _port("events", _column("id")))
    out = tmp_path / "schemas"
    out.mkdir()
    mine = out / "orders_schema.py"
    mine.write_text("# written by hand\nSCHEMA = None\n", encoding="utf-8")

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out))

    text = _text(capsys)
    assert rc == 1
    assert "ERR_GENERATE_PYSPARK_SCHEMA_FOREIGN_FILE" in text and "orders_schema.py" in text
    assert mine.read_text(encoding="utf-8") == "# written by hand\nSCHEMA = None\n"
    assert [p.name for p in out.iterdir()] == ["orders_schema.py"]  # events_schema.py waits


def test_two_products_that_share_an_out_and_a_port_id_never_replace_each_other(
    tmp_path: Path, capsys
):
    sales = _write(tmp_path, ORDERS, name="sales.yaml", product="sales.orders")
    billing = _write(
        tmp_path,
        _port("orders", _column("invoice_id"), _column("total", "decimal(12,2)")),
        _port("events", _column("id", "bigint")),
        name="billing.yaml",
        product="billing.invoices",
    )
    out = tmp_path / "runtime" / "pyspark_schema"  # the default --out, shared by a monorepo
    assert _main("generate", "pyspark-schema", str(sales), "-o", str(out)) == 0
    module = out / "orders_schema.py"
    written = module.read_bytes()
    capsys.readouterr()

    rc = _main("generate", "pyspark-schema", str(billing), "-o", str(out))

    text = _text(capsys)
    assert rc == 1
    assert "ERR_GENERATE_PYSPARK_SCHEMA_OTHER_PRODUCT" in text
    assert "orders_schema.py" in text and "generated for sales.orders" in text
    assert "own --out" in text
    assert module.read_bytes() == written  # sales' module is still sales'
    assert [p.name for p in out.iterdir()] == ["orders_schema.py"]  # nothing of billing's written
    # sales still checks clean, and billing's check says why it cannot pass here
    assert _main("generate", "pyspark-schema", str(sales), "-o", str(out), "--check") == 0
    capsys.readouterr()
    assert _main("generate", "pyspark-schema", str(billing), "-o", str(out), "--check") == 1
    assert "ERR_GENERATE_PYSPARK_SCHEMA_OTHER_PRODUCT" in _text(capsys)
    # each product keeps its own directory
    own = tmp_path / "billing"
    assert _main("generate", "pyspark-schema", str(billing), "-o", str(own)) == 0
    assert b"invoice_id" in (own / "orders_schema.py").read_bytes()


def test_it_writes_no_package_marker_and_deletes_nothing(tmp_path: Path):
    contract = _write(tmp_path, ORDERS)
    out = tmp_path / "schemas"
    out.mkdir()
    (out / "__init__.py").write_text("# mine\n", encoding="utf-8")
    (out / "old_schema.py").write_text("# an old port's module\n", encoding="utf-8")

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0

    assert (out / "__init__.py").read_text(encoding="utf-8") == "# mine\n"
    assert (out / "old_schema.py").exists()
    assert sorted(p.name for p in out.iterdir()) == [
        "__init__.py",
        "old_schema.py",
        "orders_schema.py",
    ]


# ── What it refuses and what it warns about ─────────────────────────────


def test_a_column_spark_cannot_hold_refuses_the_run_and_writes_nothing(tmp_path: Path, capsys):
    contract = _write(
        tmp_path,
        ORDERS,
        _port("places", _column("where", "geography"), _column("tree", "struct")),
    )
    out = tmp_path / "schemas"

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out))

    text = _text(capsys)
    assert rc == 1
    assert "ERR_UNSUPPORTED_BINDING" in text and "kind: column-type" in text
    assert "'where' is declared 'geography'" in text and "'tree' is declared 'struct'" in text
    assert "Declare string (WKT) or binary (WKB)" in text
    assert not out.exists()


def test_a_type_with_no_spark_form_is_written_as_text_and_warned_about(tmp_path: Path, capsys):
    contract = _write(tmp_path, _port("orders", _column("id"), _column("payload", "json")))
    out = tmp_path / "schemas"

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out))

    text = _text(capsys)
    assert rc == 0
    assert "warning: expose 'orders': column 'payload' is declared 'json'" in text
    assert 'StructField("payload", StringType(), nullable=True)' in (
        out / "orders_schema.py"
    ).read_text(encoding="utf-8")


def test_ports_without_a_schema_are_named_and_a_contract_with_none_writes_nothing(
    tmp_path: Path, capsys
):
    both = _write(tmp_path, ORDERS, {"exposeId": "stream", "kind": "topic"}, name="a.fluid.yaml")
    out = tmp_path / "schemas"

    assert _main("generate", "pyspark-schema", str(both), "-o", str(out)) == 0
    assert "no module for the output port(s) without contract.schema columns: stream" in _text(
        capsys
    )

    none = _write(tmp_path, {"exposeId": "stream", "kind": "topic"}, name="b.fluid.yaml")
    empty = tmp_path / "empty"
    for extra in ([], ["--check"]):
        assert _main("generate", "pyspark-schema", str(none), "-o", str(empty), *extra) == 0
        assert "no output port declares contract.schema columns" in _text(capsys)
    assert not empty.exists()


def test_a_name_in_square_brackets_is_printed_as_written(tmp_path: Path, capsys):
    contract = _write(tmp_path, _port("a[bold]b", _column("c[red]", "json")))

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(tmp_path / "o")) == 0

    text = _text(capsys)
    assert "a[bold]b" in text and "c[red]" in text
    assert (tmp_path / "o" / "a_bold_b_schema.py").exists()


def test_an_out_that_is_a_file_is_a_typed_failure(tmp_path: Path, capsys):
    contract = _write(tmp_path, ORDERS)
    target = tmp_path / "not-a-directory"
    target.write_text("x", encoding="utf-8")

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(target))

    assert rc == 1 and "ERR_GENERATE_PYSPARK_SCHEMA_FAILED" in _text(capsys)
    assert target.read_text(encoding="utf-8") == "x"


def test_a_module_for_a_port_the_contract_dropped_is_noted_by_a_run_and_fails_a_check(
    tmp_path: Path, capsys
):
    contract = _write(tmp_path, ORDERS, _port("events", _column("id")))
    out = tmp_path / "schemas"
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    capsys.readouterr()
    _write(tmp_path, ORDERS)  # the events port is gone

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    text = _text(capsys)
    assert "not generated from this contract any more" in text and "events_schema.py" in text

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check")
    text = _text(capsys)
    assert rc == 1 and "ERR_GENERATE_PYSPARK_SCHEMA_DRIFT" in text
    assert "not in the contract any more: events_schema.py" in text
    assert (out / "events_schema.py").exists()  # never deleted


def test_two_contracts_can_share_one_directory_and_each_check_passes(tmp_path: Path, capsys):
    one = _write(tmp_path, ORDERS, name="one.fluid.yaml")
    two = tmp_path / "two.fluid.yaml"
    document = yaml.safe_load(
        _write(tmp_path, _port("events", _column("id")), name="two.fluid.yaml").read_text()
    )
    document["id"] = "acme.sales.events"
    two.write_text(yaml.safe_dump(document), encoding="utf-8")
    out = tmp_path / "schemas"

    for contract in (one, two):
        assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    assert "not generated from this contract any more" not in _text(capsys)
    for contract in (one, two):
        assert _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check") == 0
    assert sorted(p.name for p in out.iterdir()) == ["events_schema.py", "orders_schema.py"]


def test_a_check_fails_when_every_port_lost_its_schema_but_a_module_is_still_there(
    tmp_path: Path, capsys
):
    contract = _write(tmp_path, ORDERS)
    out = tmp_path / "schemas"
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    capsys.readouterr()
    _write(tmp_path, {"exposeId": "orders", "kind": "table"})

    rc = _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check")

    assert rc == 1 and "not in the contract any more: orders_schema.py" in _text(capsys)
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0  # a run notes it


@pytest.mark.parametrize(
    "document",
    [
        "exposes: {a: 1}\n",
        "exposes: [x]\n",
        "exposes:\n  - {exposeId: a, contract: {schema: {a: int}}}\n",
        "exposes:\n  - {exposeId: a, contract: not-a-mapping}\n",
    ],
)
def test_a_contract_it_cannot_read_fails_a_run_and_a_check(tmp_path: Path, capsys, document):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("fluidVersion: '0.7.7'\nkind: DataProduct\nid: p\n" + document)
    out = tmp_path / "schemas"

    for extra in ([], ["--check"]):
        assert _main("generate", "pyspark-schema", str(contract), "-o", str(out), *extra) == 1
        assert "kind: contract-shape" in _text(capsys)
    assert not out.exists()


def test_a_lone_surrogate_in_a_name_does_not_crash_the_run_or_a_check(tmp_path: Path, capsys):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text(
        "fluidVersion: '0.7.7'\nkind: DataProduct\nid: p\nexposes:\n"
        '  - exposeId: "orders\\ud800"\n    contract:\n      schema:\n'
        '        - {name: "a\\ud800", type: json}\n',
        encoding="utf-8",
    )
    out = tmp_path / "schemas"

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0
    assert "warning: expose" in _text(capsys)
    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out), "--check") == 0
    [module] = out.iterdir()
    module.read_bytes().decode("utf-8")


def test_the_env_overlay_decides_the_schema(tmp_path: Path):
    contract = _write(tmp_path, _port("orders", _column("order_id")))
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "dev.yaml").write_text(
        yaml.safe_dump(
            {"exposes": [_port("orders", _column("order_id"), _column("dev_only", "bigint"))]}
        ),
        encoding="utf-8",
    )

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(tmp_path / "base")) == 0
    assert (
        _main(
            "generate", "pyspark-schema", str(contract), "--env", "dev", "-o", str(tmp_path / "dev")
        )
        == 0
    )

    assert "dev_only" not in (tmp_path / "base" / "orders_schema.py").read_text(encoding="utf-8")
    assert "dev_only" in (tmp_path / "dev" / "orders_schema.py").read_text(encoding="utf-8")


def test_the_default_out_is_runtime_pyspark_schema(tmp_path: Path, monkeypatch):
    contract = _write(tmp_path, ORDERS)
    monkeypatch.chdir(tmp_path)

    assert _main("generate", "pyspark-schema", str(contract)) == 0

    assert (tmp_path / "runtime" / "pyspark_schema" / "orders_schema.py").exists()


def test_a_description_that_declares_an_encoding_changes_nothing_about_how_the_file_reads(
    tmp_path: Path,
):
    contract = _write(
        tmp_path,
        _port(
            "orders",
            _column("\u00e9_col", description="vim: set fileencoding=utf-7 coding=latin-1"),
        ),
    )
    out = tmp_path / "schemas"
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    document["id"] = "sales.orders vim: set fileencoding=utf-7 coding: latin-1"
    contract.write_text(yaml.safe_dump(document), encoding="utf-8")

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(out)) == 0

    import io
    import tokenize

    data = (out / "orders_schema.py").read_bytes()
    assert tokenize.detect_encoding(io.BytesIO(data).readline)[0] == "utf-8"
    assert "\u00e9_col" in data.decode("utf-8")  # read as UTF-8, not as latin-1 or UTF-7


def test_a_missing_contract_is_said_so(capsys):
    assert _main("generate", "pyspark-schema") == 1
    assert "contract path is required" in _text(capsys)


def test_pyspark_is_never_imported(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pyspark", None)  # an import of it would raise
    contract = _write(tmp_path, ORDERS)

    assert _main("generate", "pyspark-schema", str(contract), "-o", str(tmp_path / "o")) == 0
    assert (
        _main("generate", "pyspark-schema", str(contract), "-o", str(tmp_path / "o"), "--check")
        == 0
    )


def test_the_help_says_what_it_writes(capsys):
    with pytest.raises(SystemExit) as raised:
        _main("generate", "pyspark-schema", "--help")

    text = _text(capsys)
    assert raised.value.code == 0
    assert "StructType" in text and "--check" in text and "never imports pyspark" in text
