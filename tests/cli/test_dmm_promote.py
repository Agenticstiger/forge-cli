# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid datamesh-manager promote``: draft to active, optionally gated on ``fluid verify``.

The reports are written the way ``fluid verify --out`` writes them; the provider is a
stand-in that records what it is asked.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from unittest.mock import patch

import pytest
import yaml

from fluid_build._contract_loader import CLIError
from fluid_build.cli._dmm_promote import load_verdicts, report_verdict
from fluid_build.cli.datamesh_manager import add_parser
from fluid_build.providers.base import ProviderError

# ── fixtures ──────────────────────────────────────────────────────────────


def _report(
    contract_id: str = "silver.orders",
    *,
    total: int = 2,
    match: int = 2,
    mismatch: int = 0,
    error: int = 0,
    unsupported: int = 0,
    not_built: int = 0,
    timestamp: Optional[str] = "2026-11-20T10:00:00+00:00",
    **sections: Any,
) -> Dict[str, Any]:
    """A ``fluid verify --out`` report (the shape cli/verify.py writes)."""
    report: Dict[str, Any] = {
        "contract": f"{contract_id}.fluid.yaml",
        "contract_id": contract_id,
        "summary": {
            "total": total,
            "match": match,
            "mismatch": mismatch,
            "error": error,
            "unsupported": unsupported,
            "not_built": not_built,
        },
        "results": [],
    }
    if timestamp:
        report["timestamp"] = timestamp
    report.update(sections)
    return report


def _put(folder: Path, name: str, report: Any) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _contract(folder: Path, product_id: str) -> Path:
    path = folder / f"{product_id}.fluid.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "fluidVersion": "0.7.7",
                "kind": "DataProduct",
                "id": product_id,
                "name": product_id,
                "metadata": {"layer": "Silver", "owner": {"team": "retail"}},
                "exposes": [
                    {
                        "exposeId": "orders",
                        "kind": "table",
                        "binding": {
                            "platform": "local",
                            "format": "parquet",
                            "location": {"path": "out/o.parquet"},
                        },
                        "contract": {"schema": [{"name": "order_id", "type": "VARCHAR"}]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeProvider:
    def __init__(
        self,
        status: Optional[Dict[str, str]] = None,
        fail: Iterable[str] = (),
    ) -> None:
        self.status = status or {}
        self.fail = set(fail)
        self.promoted: List[Dict[str, Any]] = []

    def _require_api_key(self) -> None:
        return None

    def promote_product(self, product_id: str, *, dry_run: bool = False) -> Dict[str, Any]:
        self.promoted.append({"id": product_id, "dry_run": dry_run})
        if product_id in self.fail:
            raise ProviderError(f"Data product {product_id!r} is not in Entropy Data")
        current = self.status.get(product_id, "draft")
        if current != "draft":
            return {
                "product_id": product_id,
                "outcome": "skipped",
                "from": current,
                "reason": "already active",
            }
        outcome = "would-promote" if dry_run else "promoted"
        return {"product_id": product_id, "outcome": outcome, "from": "draft"}


def _run(argv: Sequence[str], provider: FakeProvider, capsys) -> tuple:
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    args = parser.parse_args(["dmm", *argv])
    with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider", return_value=provider):
        code = args.func(args, logging.getLogger("test"))
    return code, capsys.readouterr().out


# ── the verdict ───────────────────────────────────────────────────────────


class TestReportVerdict:
    def test_a_clean_report_passes(self):
        assert report_verdict(_report()) == (True, "")

    def test_a_mismatch_or_an_error_does_not(self):
        assert report_verdict(_report(match=1, mismatch=1)) == (
            False,
            "1 output port(s) differ from the contract",
        )
        passed, reason = report_verdict(_report(match=1, error=1))
        assert not passed and "could not be verified" in reason

    def test_a_report_that_verified_nothing_proves_nothing(self):
        passed, reason = report_verdict(_report(total=2, match=0, unsupported=1, not_built=1))
        assert not passed and reason == "verify checked no output port"

    def test_a_port_that_is_not_built_yet_does_not_count_as_checked(self):
        assert report_verdict(_report(total=3, match=2, not_built=1))[0] is True

    @pytest.mark.parametrize(
        ("section", "reason"),
        [
            ({"reconcile": {"has_drift": True}}, "reconcile found drift"),
            ({"state_drift": {"has_drift": True}}, "state drift found drift"),
            (
                {"reconcile_lineage": {"has_drift": True, "has_critical_drift": True}},
                "reconcile lineage found critical drift",
            ),
            # verify fails this run ("state could not be refreshed") though has_drift is false.
            (
                {"state_drift": {"status": "error", "has_drift": False}},
                "state drift could not refresh the apply's state",
            ),
        ],
    )
    def test_drift_in_an_optional_section_is_not_a_pass(self, section, reason):
        assert report_verdict(_report(**section)) == (False, reason)

    def test_soft_lineage_drift_and_clean_sections_still_pass(self):
        report = _report(
            reconcile={"has_drift": False},
            state_drift={"status": "not_checked", "has_drift": False},
            reconcile_lineage={"has_drift": True, "has_critical_drift": False},
        )
        assert report_verdict(report) == (True, "")

    def test_something_that_is_not_a_report_is_not_a_pass(self):
        assert report_verdict({"results": []})[0] is False
        assert report_verdict({"summary": "nope"})[0] is False


class TestLoadVerdicts:
    def test_the_newest_report_for_a_product_decides(self, tmp_path):
        _put(tmp_path, "old.json", _report(mismatch=1, match=1, timestamp="2026-11-19T09:00:00Z"))
        _put(tmp_path, "new.json", _report(timestamp="2026-11-20T09:00:00Z"))
        verdicts = load_verdicts([str(tmp_path)])
        assert verdicts["silver.orders"].passed is True
        assert verdicts["silver.orders"].path.endswith("new.json")

    def test_a_newer_failing_report_beats_an_older_pass(self, tmp_path):
        _put(tmp_path, "a.json", _report(timestamp="2026-11-19T09:00:00Z"))
        _put(tmp_path, "b.json", _report(error=1, match=1, timestamp="2026-11-20T09:00:00Z"))
        assert load_verdicts([str(tmp_path)])["silver.orders"].passed is False

    def test_without_a_timestamp_the_file_time_decides(self, tmp_path):
        old = _put(tmp_path, "old.json", _report(mismatch=1, match=1, timestamp=None))
        new = _put(tmp_path, "new.json", _report(timestamp=None))
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        assert load_verdicts([str(tmp_path)])["silver.orders"].passed is True

    def test_the_product_is_matched_by_the_id_entropy_knows_it_by(self, tmp_path):
        _put(tmp_path, "r.json", _report("Silver.Orders"))
        assert "silver.orders" in load_verdicts([str(tmp_path)])

    def test_a_folder_skips_json_that_is_not_a_verify_report(self, tmp_path):
        _put(tmp_path, "plan.json", {"actions": []})
        _put(tmp_path, "list.json", [1, 2, 3])
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        _put(tmp_path, "nested/r.json", _report("gold.revenue"))
        assert list(load_verdicts([str(tmp_path)])) == ["gold.revenue"]

    def test_a_folder_does_not_follow_a_symlink(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        real = _put(outside, "r.json", _report("gold.revenue"))
        inside = tmp_path / "reports"
        inside.mkdir()
        (inside / "link.json").symlink_to(real)
        assert load_verdicts([str(inside)]) == {}

    def test_a_file_named_outright_that_is_not_json_is_an_error(self, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(CLIError) as exc:
            load_verdicts([str(broken)])
        assert exc.value.event == "dmm_verify_report_unreadable"
        assert exc.value.suggestions

    def test_a_file_named_outright_that_does_not_exist_is_the_path_validators_error(self, tmp_path):
        with pytest.raises(CLIError) as exc:
            load_verdicts([str(tmp_path / "missing.json")])
        assert exc.value.event == "file_not_found"

    def test_a_file_named_outright_that_is_not_a_report_is_an_error(self, tmp_path):
        path = _put(tmp_path, "plan.json", {"actions": []})
        with pytest.raises(CLIError) as exc:
            load_verdicts([str(path)])
        assert "not a fluid verify report" in exc.value.context["error"]


# ── the command ───────────────────────────────────────────────────────────


class TestPromoteCommand:
    def test_the_products_of_the_contracts_and_each_id_are_promoted_once(self, tmp_path, capsys):
        _contract(tmp_path, "silver.orders")
        _contract(tmp_path, "gold.revenue")
        provider = FakeProvider()
        code, out = _run(
            ["promote", str(tmp_path), "--id", "gold.revenue", "--id", "extra.one"],
            provider,
            capsys,
        )
        assert code == 0
        assert [p["id"] for p in provider.promoted] == [
            "gold.revenue",
            "silver.orders",
            "extra.one",
        ]
        assert "3 promoted" in out

    def test_a_product_that_passed_verify_is_promoted_and_the_rest_are_held_back(
        self, tmp_path, capsys
    ):
        contracts = tmp_path / "products"
        contracts.mkdir()
        _contract(contracts, "silver.orders")
        _contract(contracts, "gold.revenue")
        _contract(contracts, "loose.report")
        reports = tmp_path / "reports"
        _put(reports, "silver.json", _report("silver.orders"))
        _put(reports, "gold.json", _report("gold.revenue", match=1, mismatch=1))
        provider = FakeProvider()
        code, out = _run(
            ["promote", str(contracts), "--verify-report", str(reports)], provider, capsys
        )
        assert code == 1
        assert [p["id"] for p in provider.promoted] == ["silver.orders"]
        assert "fluid verify did not pass (1 output port(s) differ from the contract)" in out
        assert "no fluid verify report names this product" in out
        assert "1 promoted" in out and "2 held-back" in out

    def test_with_every_report_clean_the_run_is_green(self, tmp_path, capsys):
        reports = tmp_path / "reports"
        _put(reports, "a.json", _report("a"))
        _put(reports, "b.json", _report("b"))
        provider = FakeProvider()
        code, _ = _run(
            ["promote", "--id", "a", "--id", "b", "--verify-report", str(reports)],
            provider,
            capsys,
        )
        assert code == 0
        assert [p["id"] for p in provider.promoted] == ["a", "b"]

    def test_an_id_is_matched_to_its_report_by_the_id_entropy_knows_it_by(self, tmp_path, capsys):
        reports = tmp_path / "reports"
        _put(reports, "r.json", _report("silver.orders"))
        provider = FakeProvider()
        code, _ = _run(
            ["promote", "--id", "Silver.Orders", "--verify-report", str(reports)], provider, capsys
        )
        assert code == 0
        # The report is found by the slug; the id is sent as given.
        assert [p["id"] for p in provider.promoted] == ["Silver.Orders"]

    def test_an_active_product_is_left_alone_and_is_not_an_error(self, tmp_path, capsys):
        provider = FakeProvider(status={"a": "active"})
        code, out = _run(["promote", "--id", "a"], provider, capsys)
        assert code == 0
        assert "skipped" in out and "already active" in out

    def test_a_product_entropy_does_not_hold_fails_and_the_others_go_on(self, capsys):
        provider = FakeProvider(fail={"ghost"})
        code, out = _run(["promote", "--id", "ghost", "--id", "real"], provider, capsys)
        assert code == 1
        assert [p["id"] for p in provider.promoted] == ["ghost", "real"]
        assert "failed" in out and "1 promoted" in out

    def test_a_dry_run_reads_only(self, capsys):
        provider = FakeProvider()
        code, out = _run(["promote", "--id", "a", "--dry-run"], provider, capsys)
        assert code == 0
        assert provider.promoted == [{"id": "a", "dry_run": True}]
        assert "would-promote" in out

    def test_json_is_one_report(self, capsys):
        code, out = _run(["promote", "--id", "a", "--format", "json"], FakeProvider(), capsys)
        report = json.loads(out)
        assert code == 0
        assert report["results"] == [
            {"product_id": "a", "outcome": "promoted", "detail": "", "previous": "draft"}
        ]
        assert report["summary"] == {"promoted": 1}

    def test_naming_nothing_is_a_usage_error(self, capsys):
        with pytest.raises(CLIError) as exc:
            _run(["promote"], FakeProvider(), capsys)
        assert exc.value.event == "dmm_promote_nothing_named"
        assert exc.value.exit_code == 2

    def test_a_contract_with_no_id_is_an_error(self, tmp_path, capsys):
        path = _contract(tmp_path, "x")
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        del body["id"]
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
        with pytest.raises(CLIError) as exc:
            _run(["promote", str(path)], FakeProvider(), capsys)
        assert exc.value.event == "dmm_promote_contract_without_id"

    def test_the_parser_reads_ids_and_reports(self):
        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers())
        args = parser.parse_args(
            [
                "dmm",
                "promote",
                "p/",
                "--id",
                "a",
                "--id",
                "b",
                "--verify-report",
                "r1",
                "--verify-report",
                "r2",
            ]
        )
        assert args.contract == ["p/"]
        assert args.product_ids == ["a", "b"]
        assert args.verify_report == ["r1", "r2"]
        assert args.dry_run is False
