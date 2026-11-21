# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A product id that differs between two contracts, in ``fluid diff --baseline``.

The id is the product's identity. Consumers, the catalog, apply state and run
cursors all name the product by it, so a contract whose id changed describes a
second product, not a new version of the first. The changelog reports the change
(``diff_identity``) as a breaking one, and ``--fail-on-id-change`` fails on it
alone with the typed ``product_id_changed`` error. Without that flag the diff
only reports, as it did before: Command Center runs it that way.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml

from fluid_build._error_catalog import slug_for, suggestions_for
from fluid_build.api.changelog import compare_contracts, render_markdown, render_text
from fluid_build.api.changelog_rules import diff_identity
from fluid_build.api.changelog_types import KIND_PRODUCT_ID_CHANGED
from fluid_build.cli._common import CLIError
from tests.test_schema_076_preview_additions import _base_contract

pytestmark = pytest.mark.unit

OLD_ID = "gold.sales.orders"
NEW_ID = "gold.sales.orders_v2"
NOTE = {"name": "NOTE", "type": "string", "required": False}


@pytest.fixture(autouse=True)
def _no_extra_search_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)


def _contract(product_id: str = OLD_ID, *, with_note: bool = False) -> Dict[str, Any]:
    contract = _base_contract("0.7.6")
    contract["id"] = product_id
    if with_note:
        contract["exposes"][0]["contract"]["schema"].append(copy.deepcopy(NOTE))
    return contract


def _write(path: Path, contract: Dict[str, Any]) -> str:
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return str(path)


def _pair(tmp_path: Path, *, new_id: str = NEW_ID, drop_note: bool = False) -> Tuple[str, str]:
    """The baseline and the new contract; ``drop_note`` also removes a column (a breaking change)."""
    old = _write(tmp_path / "old.fluid.yaml", _contract(OLD_ID, with_note=drop_note))
    new = _write(tmp_path / "new.fluid.yaml", _contract(new_id))
    return old, new


def _workspace(tmp_path: Path) -> None:
    """A workspace root here, so ``--impact`` looks for readers below it and nowhere above."""
    (tmp_path / "fluid.workspace.yaml").write_text("name: shop\n", encoding="utf-8")


def _main(*argv: str) -> int:
    from fluid_build.cli import main

    return main(["diff", *argv])


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_same_id_is_no_change() -> None:
    assert diff_identity(_contract(), _contract()) == []
    assert compare_contracts(_contract(), _contract()).total == 0


def test_a_new_id_is_one_breaking_change_naming_both_ids() -> None:
    report = compare_contracts(_contract(OLD_ID), _contract(NEW_ID))

    [change] = report.breaking
    assert (change.path, change.kind, change.severity) == (
        "id",
        KIND_PRODUCT_ID_CHANGED,
        "breaking",
    )
    assert (change.before, change.after) == (OLD_ID, NEW_ID)
    assert OLD_ID in change.description and NEW_ID in change.description
    assert report.non_breaking == [] and report.info == []


@pytest.mark.parametrize(
    "new_id",
    [
        pytest.param(OLD_ID.upper(), id="case"),
        pytest.param(OLD_ID + " ", id="trailing-space"),
        pytest.param(OLD_ID[:-1], id="one-character"),
    ],
)
def test_the_comparison_is_exact(new_id: str) -> None:
    """A lookup by id is character for character, so a near miss is another product."""
    [change] = diff_identity(_contract(OLD_ID), _contract(new_id))
    assert change.kind == KIND_PRODUCT_ID_CHANGED


def test_the_id_change_comes_first_among_the_breaking_changes() -> None:
    report = compare_contracts(_contract(OLD_ID, with_note=True), _contract(NEW_ID))

    kinds = [c.kind for c in report.breaking]
    assert kinds[0] == KIND_PRODUCT_ID_CHANGED
    assert "column_removed" in kinds


def test_an_id_missing_on_one_side_is_a_change_when_nothing_validates() -> None:
    """``validate=False`` is the advisory diff of a contract still being repaired."""
    without_id = _contract()
    del without_id["id"]

    [change] = compare_contracts(_contract(), without_id, validate=False).breaking
    assert (change.before, change.after) == (OLD_ID, None)
    assert compare_contracts(without_id, copy.deepcopy(without_id), validate=False).total == 0


def test_both_renderers_show_the_change() -> None:
    report = compare_contracts(_contract(OLD_ID), _contract(NEW_ID))

    assert KIND_PRODUCT_ID_CHANGED in render_text(report)
    assert KIND_PRODUCT_ID_CHANGED in render_markdown(report)


# ---------------------------------------------------------------------------
# The command: a plain diff only reports
# ---------------------------------------------------------------------------


def test_a_plain_diff_reports_a_changed_id_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invocation Command Center makes: no gate flag, so no refusal, and clean JSON."""
    old, new = _pair(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _main(new, "--baseline", old, "--format", "json", "--out", "diff.json")
    document = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert [c["kind"] for c in document["breaking"]] == [KIND_PRODUCT_ID_CHANGED]
    assert json.loads((tmp_path / "diff.json").read_text()) == document


def test_fail_on_breaking_fails_on_a_changed_id_like_on_any_breaking_change(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _pair(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _main(new, "--baseline", old, "--fail-on-breaking", "--out", "diff.json")

    assert rc == 1
    out = capsys.readouterr().out
    assert KIND_PRODUCT_ID_CHANGED in out and "ERR_PRODUCT_ID_CHANGED" not in out


def test_the_same_id_behaves_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _write(tmp_path / "old.fluid.yaml", _contract(OLD_ID, with_note=True))
    new = _write(tmp_path / "new.fluid.yaml", _contract(OLD_ID))
    monkeypatch.chdir(tmp_path)

    assert _main(new, "--baseline", old, "--out", "d.json") == 0
    assert _main(new, "--baseline", old, "--fail-on-breaking", "--out", "d.json") == 1
    assert _main(new, "--baseline", old, "--fail-on-id-change", "--out", "d.json") == 0
    assert "ERR_PRODUCT_ID_CHANGED" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The command: --fail-on-id-change
# ---------------------------------------------------------------------------


def _diff_args(old: str, new: str, out: Path, **extra: Any) -> argparse.Namespace:
    values: Dict[str, Any] = {
        "contract": new,
        "baseline": old,
        "env": None,
        "state": None,
        "out": str(out),
        "exit_on_drift": False,
        "fail_on_breaking": False,
        "format": "json",
        "cmd": "diff",
    }
    values.update(extra)
    return argparse.Namespace(**values)


def test_fail_on_id_change_raises_the_typed_error_after_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import diff as diff_mod

    old, new = _pair(tmp_path)
    out = tmp_path / "diff.json"

    with pytest.raises(CLIError) as raised:
        diff_mod.run(
            _diff_args(old, new, out, fail_on_id_change=True), logging.getLogger("test.diff")
        )

    error = raised.value
    assert error.event == "product_id_changed"
    assert error.exit_code == 1
    assert error.error_slug == "ERR_PRODUCT_ID_CHANGED"
    assert error.suggestions and error.docs_url
    assert OLD_ID in error.context["message"] and NEW_ID in error.context["message"]
    assert error.context["out"] == str(out)
    # The report was printed and written before the error.
    assert json.loads(out.read_text())["breaking"][0]["kind"] == KIND_PRODUCT_ID_CHANGED
    assert json.loads(capsys.readouterr().out)["summary"]["breaking"] == 1


def test_the_error_follows_the_report_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _pair(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _main(new, "--baseline", old, "--fail-on-id-change", "--format", "json", "--out", "d.json")
    out = capsys.readouterr().out

    assert rc == 1
    assert out.index('"breaking"') < out.index("ERR_PRODUCT_ID_CHANGED")
    assert "--fail-on-id-change" in out
    assert (tmp_path / "d.json").exists()


@pytest.mark.parametrize("fmt", ["text", "markdown"])
def test_the_error_follows_every_report_format(
    fmt: str, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _pair(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _main(new, "--baseline", old, "--fail-on-id-change", "--format", fmt, "--out", "d.json")
    out = capsys.readouterr().out

    assert rc == 1
    assert out.index(KIND_PRODUCT_ID_CHANGED) < out.index("ERR_PRODUCT_ID_CHANGED")


def test_fail_on_id_change_ignores_every_other_breaking_change(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration may change anything but the id: the gate is about the id alone."""
    old = _write(tmp_path / "old.fluid.yaml", _contract(OLD_ID, with_note=True))
    new = _write(tmp_path / "new.fluid.yaml", _contract(OLD_ID))
    monkeypatch.chdir(tmp_path)

    rc = _main(new, "--baseline", old, "--fail-on-id-change", "--out", "d.json")

    assert rc == 0
    assert json.loads((tmp_path / "d.json").read_text())["summary"]["breaking"] == 1
    capsys.readouterr()


def test_with_both_gates_the_typed_error_wins(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _pair(tmp_path, drop_note=True)
    monkeypatch.chdir(tmp_path)

    rc = _main(
        new, "--baseline", old, "--fail-on-breaking", "--fail-on-id-change", "--out", "d.json"
    )

    assert rc == 1
    assert "ERR_PRODUCT_ID_CHANGED" in capsys.readouterr().out


def test_an_invalid_contract_is_refused_before_the_id_is_looked_at(tmp_path: Path) -> None:
    """The validity gate runs inside the comparison: a contract nothing can read exits 2."""
    from fluid_build.cli import diff as diff_mod

    broken = _contract(NEW_ID)
    del broken["exposes"]
    old = _write(tmp_path / "old.fluid.yaml", _contract(OLD_ID))
    new = _write(tmp_path / "new.fluid.yaml", broken)

    with pytest.raises(CLIError) as raised:
        diff_mod.run(
            _diff_args(old, new, tmp_path / "d.json", fail_on_id_change=True),
            logging.getLogger("test.diff"),
        )

    assert raised.value.event == "diff_contract_invalid"
    assert raised.value.exit_code == 2


def test_the_flag_is_on_the_diff_parser_and_off_by_default() -> None:
    from fluid_build.cli import build_parser

    parser = build_parser()
    plain = parser.parse_args(["diff", "new.yaml", "--baseline", "old.yaml"])
    gated = parser.parse_args(["diff", "new.yaml", "--baseline", "old.yaml", "--fail-on-id-change"])

    assert plain.fail_on_id_change is False
    assert gated.fail_on_id_change is True


def test_the_span_says_whether_the_id_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import diff as diff_mod

    seen: Dict[str, Any] = {}

    @contextlib.contextmanager
    def recording_span(name: str, attributes: Any = None) -> Iterator[Any]:
        class Span:
            def set_attribute(self, key: str, value: Any) -> None:
                seen[key] = value

        yield Span()

    old, new = _pair(tmp_path)
    same = _write(tmp_path / "same.fluid.yaml", _contract(OLD_ID))
    logger = logging.getLogger("test.diff")
    monkeypatch.setattr(diff_mod, "_traced_span", recording_span)

    diff_mod.run(_diff_args(old, new, tmp_path / "a.json"), logger)
    assert seen["fluid.diff.product_id_changed"] is True
    diff_mod.run(_diff_args(old, same, tmp_path / "b.json"), logger)
    assert seen["fluid.diff.product_id_changed"] is False


# ---------------------------------------------------------------------------
# What reads the report: the impact document, the in-process engine
# ---------------------------------------------------------------------------


def test_the_impact_report_lists_the_id_change_once(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _pair(tmp_path)
    _workspace(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _main(new, "--baseline", old, "--impact", "--format", "json", "--out", "d.json")
    interface = json.loads(capsys.readouterr().out)["impact"]["consumer_interface"]

    assert rc == 0
    assert interface["status"] == "breaking"
    assert [c["kind"] for c in interface["changes"]].count(KIND_PRODUCT_ID_CHANGED) == 1


def test_the_impact_document_is_written_before_the_error(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _pair(tmp_path)
    _workspace(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _main(
        new,
        "--baseline",
        old,
        "--impact",
        "--fail-on-id-change",
        "--format",
        "json",
        "--out",
        "d.json",
    )

    assert rc == 1
    document = json.loads((tmp_path / "d.json").read_text())
    assert document["impact"]["consumer_interface"]["status"] == "breaking"
    assert "ERR_PRODUCT_ID_CHANGED" in capsys.readouterr().out


def test_the_in_process_engine_returns_the_report(tmp_path: Path) -> None:
    """``engine.diff`` returns a report and exposes no gate, so a changed id is in it and nothing raises."""
    from fluid_build import engine

    old, new = _pair(tmp_path)

    result = asyncio.run(engine.diff(new, old, out=tmp_path / "engine-diff.json"))

    assert result.exit_code == 0
    assert result.artifacts["diff"]["breaking"][0]["kind"] == KIND_PRODUCT_ID_CHANGED


# ---------------------------------------------------------------------------
# The catalog entry
# ---------------------------------------------------------------------------


def test_the_catalog_entry_says_what_to_do() -> None:
    assert slug_for("product_id_changed") == "ERR_PRODUCT_ID_CHANGED"

    suggestions = suggestions_for("product_id_changed")
    assert len(suggestions) == 3
    joined = " ".join(suggestions)
    assert "--fail-on-id-change" in joined
    assert "consumes[].productId" in joined
