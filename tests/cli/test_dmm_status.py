# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid datamesh-manager status``: where the products of a migration stand, read-only.

The provider is a stand-in that allows two calls (the key check and one read of the product
list) and fails on anything else, so a command that wrote something would fail here.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import MagicMock, patch

import pytest
import yaml

import fluid_build.cli.datamesh_manager as dmm
from fluid_build._contract_loader import CLIError
from fluid_build.providers.base import ProviderError

pytestmark = pytest.mark.unit


def _contract(folder: Path, product_id: Any, name: Optional[str] = None) -> Path:
    path = folder / (name or f"{str(product_id).replace('/', '_')}.fluid.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    document: Dict[str, Any] = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "name": "x",
        "metadata": {"layer": "Silver", "owner": {"team": "retail"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/x.parquet"},
                },
                "contract": {"schema": [{"name": "order_id", "type": "VARCHAR"}]},
            }
        ],
    }
    if product_id is not None:
        document["id"] = product_id
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


class ReadOnlyProvider:
    """The key check and one read of the list; any other attribute is an error."""

    def __init__(
        self, products: Any = (), *, fail: Optional[Exception] = None, no_key: bool = False
    ) -> None:
        self.products = products if not isinstance(products, (list, tuple)) else list(products)
        self.fail = fail
        self.no_key = no_key
        self.calls: List[str] = []

    def _require_api_key(self) -> None:
        self.calls.append("key")
        if self.no_key:
            raise ProviderError("No Entropy Data API key: set DMM_API_KEY")

    def list_products(self) -> Any:
        self.calls.append("list")
        if self.fail:
            raise self.fail
        return self.products

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):  # what pytest and the standard library ask of any object
            raise AttributeError(name)
        raise AssertionError(f"status must be read-only, it used provider.{name}")


def _held(**statuses: Optional[str]) -> List[Dict[str, Any]]:
    return [
        {"id": pid.replace("__", "."), **({"status": s} if s is not None else {})}
        for pid, s in statuses.items()
    ]


def _run(argv: Sequence[str], provider: ReadOnlyProvider, capsys) -> tuple:
    parser = argparse.ArgumentParser()
    dmm.add_parser(parser.add_subparsers())
    args = parser.parse_args(["dmm", "status", *argv])
    with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider", return_value=provider):
        code = args.func(args, logging.getLogger("test"))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _text(output: str) -> str:
    return " ".join(output.split())


# ── What it says ────────────────────────────────────────────────────────


def test_each_product_is_reported_with_active_n_of_m(tmp_path: Path, capsys):
    for pid in ("acme.orders", "acme.customers", "acme.events"):
        _contract(tmp_path, pid)
    provider = ReadOnlyProvider(_held(acme__orders="active", acme__customers="draft"))

    code, out, _ = _run([str(tmp_path)], provider, capsys)

    lines = [line.strip() for line in out.strip().splitlines()]
    assert code == 0
    assert "active     acme.orders" in lines
    assert "draft      acme.customers" in lines
    assert "missing    acme.events - not in Entropy Data" in lines
    assert lines[-1] == "Summary: active 1 of 3 (1 draft, 1 missing)."


def test_everything_active_says_so_without_a_breakdown(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders")
    provider = ReadOnlyProvider(_held(acme__orders="active"))

    code, out, _ = _run([str(tmp_path)], provider, capsys)

    assert code == 0 and out.strip().splitlines()[-1] == "Summary: active 1 of 1."


def test_the_product_list_is_read_once_and_nothing_else_is_called(tmp_path: Path, capsys):
    for n in range(6):
        _contract(tmp_path / "nested" / f"d{n}", f"p.{n}")
    provider = ReadOnlyProvider(_held(p__0="active"))

    code, out, _ = _run([str(tmp_path)], provider, capsys)

    assert code == 0 and "active 1 of 6" in out
    assert provider.calls == ["key", "list"]


@pytest.mark.parametrize(
    "status, shown",
    [
        ("deprecated", "deprecated"),
        ("retired", "retired"),
        ("proposed", "proposed"),
        ("ACTIVE", "active"),
        (None, "unknown"),
    ],
)
def test_the_statuses_entropy_holds_are_shown_as_they_are(tmp_path: Path, capsys, status, shown):
    _contract(tmp_path, "acme.orders")
    provider = ReadOnlyProvider(_held(acme__orders=status))

    code, out, _ = _run([str(tmp_path)], provider, capsys)

    assert code == 0 and f"{shown:<10} acme.orders" in out


def test_the_older_status_shape_under_info_is_read_too(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders")
    provider = ReadOnlyProvider([{"id": "acme.orders", "info": {"status": "draft"}}])

    _, out, _ = _run([str(tmp_path)], provider, capsys)

    assert "draft      acme.orders" in out


# ── --strict ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["draft", "deprecated", "retired", None])
def test_strict_fails_unless_every_product_is_active(tmp_path: Path, capsys, status):
    _contract(tmp_path, "acme.orders")
    _contract(tmp_path, "acme.customers")
    provider = ReadOnlyProvider(_held(acme__orders="active", acme__customers=status))

    code, _, _ = _run([str(tmp_path), "--strict"], provider, capsys)
    plain, _, _ = _run([str(tmp_path)], provider, capsys)

    assert code == 1 and plain == 0


def test_strict_passes_when_every_product_is_active_and_fails_on_a_missing_one(
    tmp_path: Path, capsys
):
    _contract(tmp_path, "acme.orders")
    every = ReadOnlyProvider(_held(acme__orders="active"))
    none = ReadOnlyProvider([])

    assert _run([str(tmp_path), "--strict"], every, capsys)[0] == 0
    assert _run([str(tmp_path), "--strict"], none, capsys)[0] == 1


# ── --format json ───────────────────────────────────────────────────────


def test_json_is_the_only_thing_on_stdout(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders")
    _contract(tmp_path, "acme.events")
    provider = ReadOnlyProvider(_held(acme__orders="active"))

    code, out, _ = _run([str(tmp_path), "--format", "json"], provider, capsys)

    document = json.loads(out)
    assert code == 0
    assert document["summary"] == {
        "total": 2,
        "active": 1,
        "by_status": {"active": 1, "missing": 1},
    }
    by_id = {row["product_id"]: row for row in document["products"]}
    assert by_id["acme.orders"]["status"] == "active"
    assert by_id["acme.events"]["status"] == "missing"
    assert by_id["acme.events"]["contract"].endswith("acme.events.fluid.yaml")


def test_json_with_strict_still_exits_one_and_prints_the_report(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders")

    code, out, _ = _run(
        [str(tmp_path), "--format", "json", "--strict"], ReadOnlyProvider([]), capsys
    )

    assert code == 1 and json.loads(out)["summary"]["active"] == 0


# ── Which products ──────────────────────────────────────────────────────


def test_a_product_is_looked_up_under_the_id_forge_publishes_it_under(tmp_path: Path, capsys):
    _contract(tmp_path, "Acme Orders/Gold")
    provider = ReadOnlyProvider([{"id": "acme-orders-gold", "status": "active"}])

    _, out, _ = _run([str(tmp_path)], provider, capsys)

    assert "active     acme-orders-gold" in out


def test_a_product_entropy_holds_only_as_written_says_so(tmp_path: Path, capsys):
    _contract(tmp_path, "Acme.Orders")
    provider = ReadOnlyProvider([{"id": "Acme.Orders", "status": "active"}])

    _, out, _ = _run([str(tmp_path)], provider, capsys)

    text = _text(out)
    assert "missing acme.orders - Entropy Data holds 'Acme.Orders' as written" in text
    assert "forge publishes it as 'acme.orders'" in text


def test_files_globs_and_folders_are_expanded_as_publish_expands_them(tmp_path: Path, capsys):
    one = _contract(tmp_path / "a", "p.one")
    _contract(tmp_path / "b" / "deep", "p.two")
    _contract(tmp_path / "c", "p.three")
    provider = ReadOnlyProvider([])

    _, out, _ = _run(
        [str(one), str(tmp_path / "b"), str(tmp_path / "c" / "*.fluid.yaml")], provider, capsys
    )

    assert all(f"{p}" in out for p in ("p.one", "p.two", "p.three"))
    assert "active 0 of 3" in out


def test_one_product_declared_twice_is_refused_before_anything_is_reported(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders", "a.fluid.yaml")
    _contract(tmp_path, "acme.orders", "b.fluid.yaml")

    with pytest.raises(CLIError) as raised:
        _run([str(tmp_path)], ReadOnlyProvider([]), capsys)

    assert raised.value.event == "dmm_batch_duplicate_product"


def test_the_overlay_decides_the_id(tmp_path: Path, capsys):
    contract = _contract(tmp_path, "base.orders")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "dev.yaml").write_text("id: dev.orders\n", encoding="utf-8")
    provider = ReadOnlyProvider(_held(dev__orders="active", base__orders="draft"))

    _, base, _ = _run([str(contract)], provider, capsys)
    _, dev, _ = _run([str(contract), "--overlay", "dev"], provider, capsys)

    assert "draft      base.orders" in base and "active     dev.orders" in dev


# ── A contract that cannot be read is a row ─────────────────────────────


def test_an_unreadable_contract_is_a_row_beside_the_others(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders")
    (tmp_path / "broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    ref = _contract(tmp_path, "acme.refs")
    document = yaml.safe_load(ref.read_text(encoding="utf-8"))
    document["exposes"] = [{"$ref": "./does-not-exist.yaml"}]
    ref.write_text(yaml.safe_dump(document), encoding="utf-8")
    provider = ReadOnlyProvider(_held(acme__orders="active"))

    code, out, _ = _run([str(tmp_path)], provider, capsys)
    strict, _, _ = _run([str(tmp_path), "--strict"], provider, capsys)

    lines = [line.strip() for line in out.strip().splitlines()]
    assert code == 0 and strict == 1
    assert "active     acme.orders" in lines
    assert any(line.startswith("unreadable") and "acme.refs" in line for line in lines)
    assert any(line.startswith("unreadable") and "broken.fluid.yaml" in line for line in lines)
    assert lines[-1] == "Summary: active 1 of 3 (2 unreadable)."


def test_a_contract_without_an_id_is_a_row(tmp_path: Path, capsys):
    _contract(tmp_path, None, "noid.fluid.yaml")
    _contract(tmp_path, 5, "numeric.fluid.yaml")

    code, out, _ = _run([str(tmp_path), "--strict"], ReadOnlyProvider([]), capsys)

    assert code == 1
    assert out.count("unnamed") >= 2 or "unreadable" in out
    assert "active 0 of 2" in out


# ── What it does when Entropy does not answer ───────────────────────────


@pytest.mark.parametrize(
    "provider",
    [
        ReadOnlyProvider(fail=ProviderError("401 Unauthorized")),
        ReadOnlyProvider(no_key=True),
        ReadOnlyProvider({"error": "nope"}),
        ReadOnlyProvider("oops"),
    ],
)
def test_a_list_that_cannot_be_read_is_an_error_not_a_report_of_missing_products(
    tmp_path: Path, capsys, provider
):
    _contract(tmp_path, "acme.orders")

    code, out, err = _run([str(tmp_path)], provider, capsys)

    assert code == 1
    assert "Summary" not in out and "missing" not in out


# ── Text that came from elsewhere ───────────────────────────────────────


def test_a_status_from_the_server_cannot_forge_output_or_run_long(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.orders")
    hostile = "active\x1b[2K\nforged line [bold]" + "x" * 500
    provider = ReadOnlyProvider([{"id": "acme.orders", "status": hostile}])

    code, out, _ = _run([str(tmp_path), "--strict"], provider, capsys)
    _, as_json, _ = _run([str(tmp_path), "--format", "json"], provider, capsys)

    assert code == 1  # it is not "active", whatever it starts with
    assert len(out.splitlines()) == 2  # the row and the summary: no injected line
    assert "\x1b" not in out and "\\x1b" not in out and "[bold]" not in out
    assert all(len(line) < 200 for line in out.splitlines())
    status = json.loads(as_json)["products"][0]["status"]
    assert re.fullmatch(r"active-2k-forged-line-bold-x+…?", status) and len(status) <= 40


@pytest.mark.parametrize(
    "odd, shown",
    [("\n", "unknown"), ("   ", "unknown"), ("\u202e", "unknown"), ("\x1b[0m", "0m"), ("[x]", "x")],
)
def test_only_the_safe_characters_of_a_server_status_are_shown(tmp_path: Path, capsys, odd, shown):
    _contract(tmp_path, "acme.orders")
    provider = ReadOnlyProvider([{"id": "acme.orders", "status": odd}])

    _, out, _ = _run([str(tmp_path)], provider, capsys)

    assert f"{shown:<10} acme.orders" in out and len(out.splitlines()) == 2


def test_a_name_in_square_brackets_is_printed_as_written(tmp_path: Path, capsys):
    _contract(tmp_path, "a[bold]b")

    _, out, _ = _run([str(tmp_path)], ReadOnlyProvider([]), capsys)

    assert "a[bold]b" in out


# ── Text that came from the contract files ──────────────────────────────


def test_a_newline_in_an_id_or_a_file_name_cannot_start_a_row_of_its_own(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.real\n  active     acme.forged")
    (tmp_path / "x\n  active     acme.pathforge.fluid.yaml").write_text(
        "fluidVersion: '0.7.7'\nkind: DataProduct\nname: x\nexposes: []\n", encoding="utf-8"
    )

    _, out, _ = _run([str(tmp_path)], ReadOnlyProvider([]), capsys)

    lines = out.strip().splitlines()
    assert len(lines) == 3  # the two rows and the summary
    assert not any(line.strip().startswith("active") for line in lines)
    assert "acme.forged" in lines[0] + lines[1] and "acme.pathforge" in lines[0] + lines[1]


def test_json_still_parses_for_an_id_that_looks_like_a_credential(tmp_path: Path, capsys):
    _contract(tmp_path, "acme.token:x")
    _contract(tmp_path, "acme.password=y")

    _, out, _ = _run([str(tmp_path), "--format", "json"], ReadOnlyProvider([]), capsys)

    ids = {row["product_id"] for row in json.loads(out)["products"]}
    assert ids == {"acme.token:x", "acme.password=y"}


def test_json_is_the_only_thing_on_stdout_even_when_the_loader_prints(
    tmp_path: Path, capsys, monkeypatch
):
    contract = _contract(tmp_path, "acme.orders")
    real = dmm.load_contract_with_overlay

    def noisy(label, overlay, log):
        print("loader noise on stdout")
        return real(label, overlay, log)

    monkeypatch.setattr(dmm, "load_contract_with_overlay", noisy)

    code, out, err = _run([str(contract), "--format", "json"], ReadOnlyProvider([]), capsys)

    assert code == 0 and json.loads(out)["summary"]["total"] == 1
    assert "loader noise" in err and "loader noise" not in out


def test_an_id_that_is_not_a_string_is_not_called_missing(tmp_path: Path, capsys):
    _contract(tmp_path, 123, "num.fluid.yaml")
    _contract(tmp_path, None, "none.fluid.yaml")

    _, out, _ = _run([str(tmp_path), "--format", "json"], ReadOnlyProvider([]), capsys)

    details = {
        row["contract"].rsplit("/", 1)[1]: row["detail"] for row in json.loads(out)["products"]
    }
    assert details == {
        "num.fluid.yaml": "the contract's id is not a string",
        "none.fluid.yaml": "the contract declares no id",
    }


def test_an_unreadable_contract_is_named_by_its_path_with_the_id_it_declares(
    tmp_path: Path, capsys
):
    ref = _contract(tmp_path, "acme.orders")
    document = yaml.safe_load(ref.read_text(encoding="utf-8"))
    document["exposes"] = [{"$ref": "./nope.yaml"}]
    ref.write_text(yaml.safe_dump(document), encoding="utf-8")

    _, out, _ = _run([str(tmp_path)], ReadOnlyProvider(_held(acme__orders="active")), capsys)

    row = out.strip().splitlines()[0]
    assert row.startswith("unreadable ") and "acme.orders.fluid.yaml (declares acme.orders)" in row
    assert "active 0 of 1" in out  # the contract is what is wrong, whatever Entropy holds


@pytest.mark.parametrize("word", ["missing", "unreadable", "unnamed"])
def test_a_status_the_server_spells_like_ours_is_not_taken_for_ours(tmp_path: Path, capsys, word):
    _contract(tmp_path, "acme.orders")

    _, out, _ = _run(
        [str(tmp_path)], ReadOnlyProvider([{"id": "acme.orders", "status": word}]), capsys
    )

    assert f"status-{word}" in out and "not in Entropy Data" not in out


# ── Order of work, and the real provider's paging ───────────────────────


def test_what_is_wrong_with_the_arguments_is_said_before_the_key_is_checked(tmp_path: Path, capsys):
    (tmp_path / "empty").mkdir()
    provider = ReadOnlyProvider([], no_key=True)

    with pytest.raises(CLIError) as raised:
        _run([str(tmp_path / "empty")], provider, capsys)

    assert raised.value.event == "dmm_batch_no_contracts"
    assert provider.calls == []  # no key check, no request


def test_a_glob_a_folder_and_the_file_itself_name_one_product_once(tmp_path: Path, capsys):
    one = _contract(tmp_path, "acme.orders")

    _, out, _ = _run(
        [str(one), str(tmp_path), str(tmp_path / "*.fluid.yaml")], ReadOnlyProvider([]), capsys
    )

    assert "active 0 of 1" in out


def test_a_later_page_that_cannot_be_read_is_an_error_not_a_partial_report(tmp_path: Path, capsys):
    from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

    _contract(tmp_path, "late.product")
    pages: List[Any] = [
        [{"id": f"p{n}", "status": "active"} for n in range(1000)],
        {"error": "rate limited"},
    ]

    def request(method: str, path: str, *, json_body: Any = None):
        response = MagicMock(status_code=200)
        response.json.return_value = pages[int(path.rsplit("p=", 1)[1])]
        return response

    provider = DataMeshManagerProvider(api_key="k", api_url="https://api.example.test")
    provider._request = request  # type: ignore[method-assign]

    code, out, err = _run([str(tmp_path)], provider, capsys)

    assert code == 1
    assert "missing" not in out and "page 1" in (out + err)


def test_an_answer_that_is_not_json_is_a_typed_error(tmp_path: Path, capsys):
    from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

    _contract(tmp_path, "acme.orders")

    def request(method: str, path: str, *, json_body: Any = None):
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("Expecting value")
        return response

    provider = DataMeshManagerProvider(api_key="k", api_url="https://api.example.test")
    provider._request = request  # type: ignore[method-assign]

    code, out, err = _run([str(tmp_path)], provider, capsys)

    assert code == 1 and "not JSON" in (out + err) and "Expecting value" not in (out + err)


# ── Where it is registered ──────────────────────────────────────────────


def test_it_is_a_subcommand_of_both_spellings_and_needs_a_contract():
    parser = argparse.ArgumentParser()
    dmm.add_parser(parser.add_subparsers())

    for spelling in ("dmm", "datamesh-manager"):
        args = parser.parse_args([spelling, "status", "products/", "--strict", "-f", "json"])
        assert (args.contract, args.strict, args.format) == (["products/"], True, "json")
        assert callable(args.func)
    with pytest.raises(SystemExit):
        parser.parse_args(["dmm", "status"])
