# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Small correctness gaps in the Entropy Data commands: a product list that is not a list, a
``wipe`` that loses its last line, a typed error on stdout under ``--format json``, file names
in an error, and a consumer published after an upstream file that named no product."""

from __future__ import annotations

from argparse import Namespace
from typing import Any, List
from unittest.mock import patch

import pytest

import fluid_build.cli.datamesh_manager as dmm
from fluid_build._contract_loader import CLIError
from fluid_build.cli._dmm_batch import BatchItem, refuse_duplicate_ids
from fluid_build.providers.base import ProviderError
from tests.cli.test_dmm_batch import FakeProvider, _contract, _run_full, _write

pytestmark = pytest.mark.unit


class Tenant:
    """A provider holding products; ``lists`` is what each ``list_products`` call answers."""

    api_url = "https://api.example.test"

    def __init__(self, *lists: Any) -> None:
        self.lists = list(lists)
        self.deleted: List[str] = []

    def list_products(self) -> Any:
        answer = self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def delete(self, product_id: str) -> bool:
        self.deleted.append(product_id)
        return True


def _args(**kw: Any) -> Namespace:
    return Namespace(format="table", yes=True, max_passes=0, **kw)


# ── a product list that is not a list ───────────────────────────────────────────────────────


def test_a_page_zero_that_is_not_a_list_is_an_error_for_list_and_wipe(capsys):
    tenant = Tenant({"error": "nope"})
    with patch.object(dmm, "_make_provider", return_value=tenant):
        assert dmm._cmd_list(_args()) == 1
        assert dmm._cmd_wipe(_args()) == 1
    assert tenant.deleted == []
    assert "something other than a list" in " ".join(capsys.readouterr().err.split())


# ── wipe keeps its Done line when the recount fails ─────────────────────────────────────────


def test_wipe_prints_what_it_deleted_when_the_recount_fails(capsys):
    tenant = Tenant([{"id": "a"}, {"id": "b"}], ProviderError("Entropy Data is unreachable"))
    with patch.object(dmm, "_make_provider", return_value=tenant):
        code = dmm._cmd_wipe(_args())
    captured = capsys.readouterr()
    assert code == 1
    assert tenant.deleted == ["a", "b"]
    assert "Done: 2 deleted, remaining unknown" in captured.out
    assert "unreachable" in captured.err


# ── a typed error stays off stdout under --format json ──────────────────────────────────────


def test_a_typed_error_goes_to_stderr_when_the_format_is_json(tmp_path, capsys):
    _write(tmp_path, "acme.orders", sub="a")
    _write(tmp_path, "acme.orders", sub="b")

    code, out, err = _run_full(
        ["status", str(tmp_path), "--format", "json"], FakeProvider(), capsys
    )

    assert code == 1
    assert out == ""  # stdout holds a JSON document or nothing
    assert "dmm_batch_duplicate_product" in err and "acme.orders" in err


def test_a_typed_error_still_reaches_the_top_level_handler_in_text_mode(tmp_path, capsys):
    _write(tmp_path, "acme.orders", sub="a")
    _write(tmp_path, "acme.orders", sub="b")
    with pytest.raises(CLIError) as raised:
        _run_full(["status", str(tmp_path)], FakeProvider(), capsys)
    assert raised.value.event == "dmm_batch_duplicate_product"


# ── file names in the duplicate-id error ────────────────────────────────────────────────────


def test_the_duplicate_id_error_prints_file_names_on_one_line_without_controls():
    items = [
        BatchItem("a\nERROR: forged line.yaml", _contract("p"), "p"),
        BatchItem("b\x1b[2J.yaml", _contract("p"), "p"),
    ]
    with pytest.raises(CLIError) as raised:
        refuse_duplicate_ids(items)
    text = raised.value.context["error"]
    assert "\n" not in text and "\x1b" not in text
    assert "\\x1b[2J" in text
