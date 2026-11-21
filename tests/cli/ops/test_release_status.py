# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs status <product> --release-dir DIR``: which sealed release was in force.

A job Forge does not run leaves no run record; the sealed release ``fluid ship --release-dir``
publishes at each deploy is what it leaves. The records are read like any file someone else may
have written: one level deep, no link followed, bounded, and every text cleaned before it is shown.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.cli import _ship_release as release
from fluid_build.cli.ops import release_status as rs
from fluid_build.cli.ops._cli import register_runs

PRODUCT = "commerce.orders"
LOG = logging.getLogger("test_release_status")
SHA = "4458aaa4" * 5


def seal(
    root: Path,
    name: str,
    *,
    sealed: str = "2026-11-18T09:12:03+00:00",
    product: str = PRODUCT,
    versions: Optional[Dict[str, str]] = None,
    source: Optional[Dict[str, str]] = None,
    run_id: str = "3f9a1c0b7e2d",
    tamper: bool = False,
    **extra: Any,
) -> Path:
    """A release directory with a ``release.json`` as ``fluid ship`` writes it."""
    identity = {
        "product_id": product,
        "environment": "prod",
        "forge_version": "0.37.0",
        "bundle": {"manifest_digest": "sha256:bundle"},
        "expose_versions": versions if versions is not None else {"orders": "1.4.0"},
    }
    index = {
        "schema": release.RELEASE_SCHEMA,
        "identity": identity,
        "identity_digest": release.identity_digest(identity),
        "gates": [],
        "files": {},
        "provenance": {
            "run_id": run_id,
            "started_at": sealed,
            "finished_at": sealed,
            **({"source": source} if source is not None else {}),
        },
        **extra,
    }
    directory = root / name
    directory.mkdir(parents=True)
    if tamper:
        identity["expose_versions"] = {"orders": "9.9.9"}  # after the digest was taken
    (directory / "release.json").write_text(json.dumps(index), encoding="utf-8")
    return directory


@pytest.fixture
def releases(tmp_path: Path) -> Path:
    root = tmp_path / "releases"
    seal(root, "r1", sealed="2026-11-18T09:12:03+00:00", versions={"orders": "1.4.0"},
         source={"revision": SHA, "repository": "https://github.com/org/orders", "ref": "refs/tags/v1.4.0"})  # fmt: skip
    seal(
        root, "r2", sealed="2026-11-20T15:02:11+00:00", versions={"orders": "1.5.0"}, run_id="aaaa"
    )
    seal(
        root, "r3", sealed="2026-11-22T08:00:00+00:00", versions={"orders": "2.0.0"}, run_id="bbbb"
    )
    return root


# --- finding the releases -------------------------------------------------------------


def test_the_releases_of_a_product_are_found_in_the_order_they_were_sealed(releases):
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.expose_versions["orders"] for r in found.releases] == ["1.4.0", "1.5.0", "2.0.0"]
    assert found.skipped == {} and all(r.intact for r in found.releases)


def test_a_directory_that_is_itself_a_release_is_one_release(releases):
    found = rs.find_releases([str(releases / "r2")], PRODUCT)
    assert [r.run_id for r in found.releases] == ["aaaa"]


def test_another_products_release_and_what_is_no_release_are_counted_not_listed(releases):
    seal(releases, "other", product="retail.clicks")
    (releases / "empty").mkdir()
    (releases / "note.txt").write_text("x")
    (releases / ".hidden").mkdir()
    (releases / "r4.partial-abc-1234").mkdir()  # a run that did not publish
    found = rs.find_releases([str(releases)], PRODUCT)
    assert len(found.releases) == 3
    assert found.skipped == {
        "a release of another product": 1,
        "a directory with no readable release.json": 1,
    }


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    with pytest.raises(NotADirectoryError, match="--release-dir"):
        rs.find_releases([str(tmp_path / "nope")], PRODUCT)


def test_several_roots_are_read_and_a_release_reached_twice_by_one_path_is_listed_once(releases):
    found = rs.find_releases([str(releases), str(releases / "r1")], PRODUCT)
    ids = [r.release_id for r in found.releases]
    assert len(found.releases) == 3 and len(set(ids)) == 3


def test_a_directory_holding_more_than_the_limit_is_read_to_the_limit(releases, monkeypatch):
    monkeypatch.setattr(rs, "MAX_ENTRIES", 2)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert len(found.releases) == 2
    assert any("past the first 2" in why for why in found.skipped)


# --- records are not trusted ----------------------------------------------------------


def test_a_link_is_never_followed(releases, tmp_path):
    outside = tmp_path / "outside"
    seal(outside, "inner", versions={"orders": "6.6.6"})
    os.symlink(outside / "inner", releases / "linked-dir")
    (releases / "r2" / "release.json").unlink()
    os.symlink(outside / "inner" / "release.json", releases / "r2" / "release.json")
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.expose_versions["orders"] for r in found.releases] == ["1.4.0", "2.0.0"]


@pytest.mark.parametrize(
    "text",
    ["not json", "[1, 2]", "{}", '{"schema": "other/1"}', '{"schema": "fluid-release/1"}'],
)
def test_a_release_json_that_is_not_a_record_is_skipped(releases, text):
    (releases / "r2" / "release.json").write_text(text)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.run_id for r in found.releases] == ["3f9a1c0b7e2d", "bbbb"]
    assert sum(found.skipped.values()) == 1


def test_a_record_with_no_time_it_was_sealed_at_is_skipped(releases):
    index = json.loads((releases / "r2" / "release.json").read_text())
    index["provenance"] = {"run_id": "aaaa", "finished_at": "yesterday-ish"}
    (releases / "r2" / "release.json").write_text(json.dumps(index))
    assert len(rs.find_releases([str(releases)], PRODUCT).releases) == 2


def test_a_record_larger_than_the_limit_is_not_read(releases, monkeypatch):
    monkeypatch.setattr(rs, "MAX_INDEX_BYTES", 50)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert found.releases == []
    assert found.skipped == {"a directory with no readable release.json": 3}


def test_a_record_that_was_changed_after_it_was_sealed_is_shown_as_not_intact(releases):
    seal(releases, "r9", sealed="2026-11-19T00:00:00+00:00", tamper=True)
    found = rs.find_releases([str(releases)], PRODUCT)
    changed = next(r for r in found.releases if r.path.endswith("r9"))
    assert changed.intact is False
    text = "\n".join(rs.describe(changed))
    assert "NOT INTACT" in text and "9.9.9" in text


def test_texts_from_a_record_cannot_rewrite_the_terminal(releases):
    esc, bidi = "\x1b[31m", "‮"
    seal(
        releases,
        "r5",
        sealed="2026-11-21T00:00:00+00:00",
        versions={f"orders{esc}": f"1.0{bidi}0\n"},
        source={"revision": SHA, "repository": f"https://h/{esc}x{bidi}", "ref": "a\nb"},
        run_id=f"run{esc}\r\nforged: ok",
        environment=None,
    )
    found = rs.find_releases([str(releases)], PRODUCT)
    shown = "\n".join(line for r in found.releases for line in rs.describe(r))
    assert esc not in shown and bidi not in shown and "\r" not in shown
    assert "\nforged" not in shown
    assert len(rs.clean("x" * 500)) == 200 and rs.clean("x" * 500).endswith("...")


# --- which release was in force -------------------------------------------------------


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_the_release_in_force_at_the_start_of_a_day_and_the_ones_sealed_during_it(releases):
    found = rs.find_releases([str(releases)], PRODUCT).releases
    on_the_20th = rs.in_force(found, date(2026, 11, 20), moment("2026-11-25T00:00:00"))
    assert on_the_20th.at_start.expose_versions["orders"] == "1.4.0"
    assert [r.expose_versions["orders"] for r in on_the_20th.during] == ["1.5.0"]
    quiet = rs.in_force(found, date(2026, 11, 19), moment("2026-11-25T00:00:00"))
    assert quiet.at_start.expose_versions["orders"] == "1.4.0" and quiet.during == []
    before = rs.in_force(found, date(2026, 11, 17), moment("2026-11-25T00:00:00"))
    assert before.at_start is None and before.during == []


def test_without_a_day_it_is_the_latest_release_sealed_by_now(releases):
    found = rs.find_releases([str(releases)], PRODUCT).releases
    now = rs.in_force(found, None, moment("2026-11-21T12:00:00"))
    assert now.at_start.expose_versions["orders"] == "1.5.0" and now.during == []
    assert rs.in_force(found, None, moment("2026-11-01T00:00:00")).at_start is None


def test_a_time_with_an_offset_or_none_is_read_as_utc(releases):
    seal(releases, "r6", sealed="2026-11-23T01:30:00+02:00", versions={"orders": "3.0.0"})
    seal(releases, "r7", sealed="2026-11-23T10:00:00Z", versions={"orders": "3.1.0"})
    seal(releases, "r8", sealed="2026-11-24T10:00:00", versions={"orders": "3.2.0"})
    found = rs.find_releases([str(releases)], PRODUCT).releases
    times = {r.expose_versions["orders"]: rs._stamp(r.sealed_at) for r in found}
    assert times["3.0.0"] == "2026-11-22T23:30:00Z"
    assert times["3.1.0"] == "2026-11-23T10:00:00Z" and times["3.2.0"] == "2026-11-24T10:00:00Z"


def test_the_text_answer_names_the_version_the_release_the_run_and_the_code(releases):
    found = rs.find_releases([str(releases)], PRODUCT)
    answer = rs.in_force(found.releases, date(2026, 11, 20), moment("2026-11-25T00:00:00"))
    text = "\n".join(rs.report(PRODUCT, answer, found, date(2026, 11, 20)))
    assert "orders 1.4.0" in text and "orders 1.5.0" in text
    assert f"release  {found.releases[0].release_id}" in text
    assert "run 3f9a1c0b7e2d" in text and "env prod" in text and "forge 0.37.0" in text
    assert f"code     {SHA} https://github.com/org/orders refs/tags/v1.4.0" in text
    assert "(the record names no source)" in text  # the second release names none
    assert "At the start" not in text and "In force at the start of 2026-11-20" in text
    assert "Sealed during 2026-11-20:" in text


# --- the command ----------------------------------------------------------------------


def runs_status(*argv: str) -> int:
    parser = argparse.ArgumentParser()
    register_runs(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["runs", "status", PRODUCT, *argv])
    return args.func(args, LOG)


def test_the_command_says_which_release_was_in_force_on_a_day(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-20") == 0
    out = capsys.readouterr().out
    assert "orders 1.4.0" in out and "orders 1.5.0" in out and "orders 2.0.0" not in out


def test_json_carries_the_same_answer(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-20", "--json") == 0
    got = json.loads(capsys.readouterr().out)
    assert got["on"] == "2026-11-20" and got["releases_read"] == 3 and got["not_read"] == {}
    assert got["in_force"]["expose_versions"] == {"orders": "1.4.0"}
    assert got["in_force"]["sealed_at"] == "2026-11-18T09:12:03Z"
    assert got["in_force"]["source"]["revision"] == SHA
    assert [r["run_id"] for r in got["during"]] == ["aaaa"]


def test_no_release_by_then_is_exit_1(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-01") == 1
    assert "No release of commerce.orders was sealed on or before on 2026-11-01" in (
        capsys.readouterr().out
    )


def test_a_day_that_is_not_one_and_a_missing_directory_are_usage_errors(releases, tmp_path, caplog):
    assert runs_status("--release-dir", str(releases), "--on", "yesterday") == 2
    assert runs_status("--release-dir", str(tmp_path / "nope")) == 2
    assert runs_status("--on", "2026-11-20") == 2  # a day answers for a release directory
    assert "pass the directory of releases too" in caplog.text


def test_a_hostile_day_is_not_echoed_whole(releases, caplog):
    assert runs_status("--release-dir", str(releases), "--on", "\x1b[31m" + "x" * 200) == 2
    assert "\x1b" not in caplog.text and "x" * 100 not in caplog.text


def test_without_release_dir_the_status_reads_run_records_as_before(tmp_path, caplog):
    assert runs_status("--state-root", str(tmp_path)) == 1
    assert "no builds found for product commerce.orders" in caplog.text


def test_the_module_imports_only_the_standard_library_when_it_is_loaded():
    import ast

    source = Path(rs.__file__).read_text(encoding="utf-8")
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {
        "__future__", "os", "stat", "dataclasses", "datetime", "pathlib", "typing",
    }  # fmt: skip
