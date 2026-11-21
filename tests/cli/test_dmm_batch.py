# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid datamesh-manager publish`` over a folder of contracts: upstreams first, per-product
outcomes, a lifecycle status.

The provider is a stand-in that records what it is asked; the contracts are real files that
go through the real loader and validator.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union
from unittest.mock import patch

import pytest
import yaml

import fluid_build.cli.datamesh_manager as dmm
from fluid_build._contract_loader import CLIError
from fluid_build.cli._dmm_batch import (
    BatchItem,
    needs_expansion,
    refuse_duplicate_ids,
    resolve_contract_paths,
    upstream_first,
)
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager._ids import dmm_product_id
from fluid_build.providers.datamesh_manager._lifecycle import ProductRetiredError, status_to_send

# ── fixtures ──────────────────────────────────────────────────────────────


def _contract(product_id: str, upstreams: Sequence[str] = ()) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
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
                    "location": {"path": f"out/{product_id}.parquet"},
                },
                "contract": {"schema": [{"name": "order_id", "type": "VARCHAR"}]},
            }
        ],
    }
    if upstreams:
        contract["consumes"] = [{"productId": u, "exposeId": "orders"} for u in upstreams]
    return contract


def _write(folder: Path, product_id: str, upstreams: Sequence[str] = (), sub: str = "") -> Path:
    target = folder / sub / f"{product_id}.fluid.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(_contract(product_id, upstreams)), encoding="utf-8")
    return target


def _items(graph: Dict[str, Iterable[str]]) -> List[BatchItem]:
    return [
        BatchItem(f"{pid}.fluid.yaml", _contract(pid, tuple(ups)), dmm_product_id(pid), tuple(ups))
        for pid, ups in graph.items()
    ]


class FakeProvider:
    """What the batch needs of the provider, recording what it is asked."""

    def __init__(
        self,
        held: Union[Dict[str, Optional[str]], Iterable[str]] = (),
        fail: Iterable[str] = (),
        retired: Iterable[str] = (),
        held_unreadable: bool = False,
        odcs_failed: Iterable[str] = (),
    ) -> None:
        # What Entropy holds: ids, or ids with their status.
        self.statuses: Dict[str, Optional[str]] = (
            dict(held) if isinstance(held, dict) else {pid: None for pid in held}
        )
        self.fail = set(fail)
        self.retired = set(retired)
        self.held_unreadable = held_unreadable
        self.odcs_failed = set(odcs_failed)
        self.held_reads = 0
        self.applied: List[Dict[str, Any]] = []

    def _require_api_key(self) -> None:
        return None

    def held_products(self) -> Optional[Dict[str, Optional[str]]]:
        self.held_reads += 1
        return None if self.held_unreadable else dict(self.statuses)

    def apply(self, contract: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
        pid = dmm_product_id(contract["id"])
        handed = kw.get("held_product_ids")
        self.applied.append(
            {"id": pid, **kw, "held_seen": set(handed) if handed is not None else None}
        )
        if pid in self.fail:
            raise ProviderError(f"Entropy Data API error 422 on PUT /api/dataproducts/{pid}")
        if pid in self.retired:
            raise ProductRetiredError(pid)
        if kw.get("dry_run"):
            return {"dry_run": True, "url": f"https://api.example.test/api/dataproducts/{pid}"}
        result: Dict[str, Any] = {
            "success": True,
            "product_id": pid,
            "url": f"https://app.example.test/dataproducts/{pid}",
        }
        if kw.get("status"):
            sent = status_to_send(kw["status"], self.statuses.get(pid))
            result["status"] = sent
            if sent != kw["status"]:
                result["status_kept"] = sent
        if pid in self.odcs_failed:
            result["odcs_contracts"] = [{"contract_id": f"{pid}.orders", "success": False}]
        return result

    @property
    def order(self) -> List[str]:
        return [a["id"] for a in self.applied]


def _run_full(argv: Sequence[str], provider: FakeProvider, capsys, **patches: Any) -> tuple:
    parser = argparse.ArgumentParser()
    dmm.add_parser(parser.add_subparsers())
    args = parser.parse_args(["dmm", *argv])
    with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider", return_value=provider):
        code = args.func(args, logging.getLogger("test"))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _run(argv: Sequence[str], provider: FakeProvider, capsys) -> tuple:
    code, out, _err = _run_full(argv, provider, capsys)
    return code, out


# ── the order ─────────────────────────────────────────────────────────────


class TestUpstreamFirst:
    def test_a_chain_publishes_each_product_after_the_one_it_consumes(self):
        ordered = upstream_first(_items({"c": ["b"], "b": ["a"], "a": []}))
        assert [i.product_id for i in ordered] == ["a", "b", "c"]

    def test_a_wave_is_sorted_by_id_and_waves_follow_dependencies(self):
        graph = {"gold": ["silver_b", "silver_a"], "silver_b": ["bronze"], "silver_a": ["bronze"]}
        graph["bronze"] = []
        ordered = [i.product_id for i in upstream_first(_items(graph))]
        assert ordered == ["bronze", "silver_a", "silver_b", "gold"]

    def test_the_same_products_give_the_same_order_whatever_the_input_order(self):
        graph = {"e": ["c", "d"], "d": ["b"], "c": ["a"], "b": [], "a": [], "f": []}
        expected = [i.product_id for i in upstream_first(_items(graph))]
        for seed in range(8):
            keys = list(graph)
            random.Random(seed).shuffle(keys)
            shuffled = _items({k: graph[k] for k in keys})
            assert [i.product_id for i in upstream_first(shuffled)] == expected

    def test_an_upstream_outside_the_batch_is_not_waited_for(self):
        ordered = upstream_first(_items({"mine": ["held.elsewhere"], "other": []}))
        assert [i.product_id for i in ordered] == ["mine", "other"]

    def test_a_product_that_consumes_itself_is_not_a_loop(self):
        assert [i.product_id for i in upstream_first(_items({"a": ["a"]}))] == ["a"]

    def test_an_upstream_is_matched_by_the_id_entropy_knows_it_by(self):
        # ``consumes[].productId`` is as written; the product is known by its slug.
        ordered = upstream_first(_items({"consumer": ["Silver.Orders"], "Silver.Orders": []}))
        assert [i.product_id for i in ordered] == ["silver.orders", "consumer"]

    def test_a_loop_is_refused_and_named(self):
        with pytest.raises(CLIError) as exc:
            upstream_first(_items({"a": ["c"], "b": ["a"], "c": ["b"], "free": []}))
        assert exc.value.event == "dmm_batch_dependency_cycle"
        assert exc.value.error_slug == "ERR_DMM_BATCH_DEPENDENCY_CYCLE"
        message = exc.value.context["error"]
        for pid in ("a", "b", "c"):
            assert f"{pid} reads from" in message
        assert "free" not in message
        assert exc.value.suggestions

    def test_a_pair_that_consume_each_other_is_a_loop(self):
        with pytest.raises(CLIError, match="dmm_batch_dependency_cycle"):
            upstream_first(_items({"x": ["y"], "y": ["x"]}))

    def test_two_files_declaring_one_product_are_refused(self):
        items = _items({"a": [], "b": []})
        items.append(BatchItem("copy.fluid.yaml", _contract("A"), "a"))
        with pytest.raises(CLIError) as exc:
            refuse_duplicate_ids(items)
        assert exc.value.event == "dmm_batch_duplicate_product"
        assert "a in a.fluid.yaml, copy.fluid.yaml" in exc.value.context["error"]


# ── which contracts ───────────────────────────────────────────────────────


class TestContractPaths:
    def test_files_named_outright_are_used_as_given_once_each(self, tmp_path):
        a = _write(tmp_path, "a")
        assert resolve_contract_paths([str(a), str(a)]) == [str(a)]
        assert not needs_expansion([str(a)])

    def test_a_folder_is_searched_at_any_depth_in_name_order(self, tmp_path):
        _write(tmp_path, "b", sub="gold")
        _write(tmp_path, "a", sub="bronze/deep")
        (tmp_path / "notes.yaml").write_text("not: a contract\n", encoding="utf-8")
        found = resolve_contract_paths([str(tmp_path)])
        assert [Path(p).name for p in found] == ["a.fluid.yaml", "b.fluid.yaml"]
        assert needs_expansion([str(tmp_path)])

    def test_ignored_and_build_folders_are_not_searched(self, tmp_path):
        _write(tmp_path, "keep")
        _write(tmp_path, "vendored", sub="node_modules/pkg")
        _write(tmp_path, "built", sub="dist")
        assert [Path(p).name for p in resolve_contract_paths([str(tmp_path)])] == [
            "keep.fluid.yaml"
        ]

    def test_a_glob_pattern_is_expanded(self, tmp_path):
        _write(tmp_path, "a", sub="p")
        _write(tmp_path, "b", sub="p")
        found = resolve_contract_paths([str(tmp_path / "p" / "*.fluid.yaml")])
        assert [Path(p).name for p in found] == ["a.fluid.yaml", "b.fluid.yaml"]

    def test_a_file_whose_name_holds_glob_characters_is_a_file_not_a_pattern(self, tmp_path):
        weird = _write(tmp_path, "a[1]")
        assert not needs_expansion([str(weird)])
        assert resolve_contract_paths([str(weird)]) == [str(weird)]
        sub = tmp_path / "sub"
        _write(sub, "b")
        found = resolve_contract_paths([str(weird), str(sub)])
        assert [Path(p).name for p in found] == ["a[1].fluid.yaml", "b.fluid.yaml"]

    def test_the_same_file_spelled_two_ways_is_named_once(self, tmp_path):
        a = _write(tmp_path, "a")
        again = str(tmp_path / "." / "a.fluid.yaml")
        assert resolve_contract_paths([str(a), again, str(a)]) == [str(a)]

    def test_a_path_that_does_not_exist_is_left_to_the_loader_as_it_always_was(self, tmp_path):
        missing = str(tmp_path / "missing.fluid.yaml")
        assert resolve_contract_paths([missing]) == [missing]
        assert not needs_expansion([missing])

    def test_a_glob_that_matches_nothing_is_the_not_found_error(self, tmp_path):
        with pytest.raises(CLIError) as exc:
            resolve_contract_paths([str(tmp_path / "nothing*.fluid.yaml")])
        assert exc.value.event == "contract_file_not_found"

    def test_a_folder_with_no_contract_is_the_batch_error_that_names_it(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(CLIError) as exc:
            resolve_contract_paths([str(tmp_path / "empty")])
        assert exc.value.event == "dmm_batch_no_contracts"
        assert "holds no" in exc.value.context["error"]
        assert exc.value.suggestions


# ── the batch run ─────────────────────────────────────────────────────────


@pytest.fixture
def products(tmp_path) -> Path:
    """gold consumes silver, silver consumes bronze; one loose product."""
    _write(tmp_path, "gold.revenue", ["silver.orders"])
    _write(tmp_path, "silver.orders", ["bronze.raw_orders"])
    _write(tmp_path, "bronze.raw_orders")
    _write(tmp_path, "loose.report")
    return tmp_path


class TestBatchRun:
    def test_every_product_follows_the_products_it_consumes(self, products, capsys):
        provider = FakeProvider()
        code, out = _run(["publish", str(products)], provider, capsys)
        assert code == 0
        order = provider.order
        assert order.index("bronze.raw_orders") < order.index("silver.orders")
        assert order.index("silver.orders") < order.index("gold.revenue")
        assert "4 published" in out
        assert "upstream products first" in out

    def test_the_product_list_is_read_once_for_the_whole_batch(self, products, capsys):
        provider = FakeProvider(held={"already.there"})
        _run(["publish", str(products)], provider, capsys)
        assert provider.held_reads == 1
        # Each publish is told what Entropy holds, including what this run published before it.
        by_id = {a["id"]: a["held_seen"] for a in provider.applied}
        assert by_id["bronze.raw_orders"] >= {"already.there"}
        assert "bronze.raw_orders" in by_id["silver.orders"]
        assert {"bronze.raw_orders", "silver.orders"} <= by_id["gold.revenue"]

    def test_when_the_list_cannot_be_read_the_provider_is_handed_nothing(self, products, capsys):
        # An empty set would read as "Entropy holds no products" and the access-agreement
        # pre-flight would then skip every upstream outside the batch.
        provider = FakeProvider(held_unreadable=True)
        code, _ = _run(["publish", str(products)], provider, capsys)
        assert code == 0
        assert provider.held_reads == 1
        assert [a["held_seen"] for a in provider.applied] == [None] * 4

    def test_the_status_and_the_other_flags_reach_every_publish(self, products, capsys):
        provider = FakeProvider()
        code, out = _run(
            [
                "publish",
                str(products),
                "--status",
                "draft",
                "--team-id",
                "retail",
                "--with-contract",
            ],
            provider,
            capsys,
        )
        assert code == 0
        assert {a["status"] for a in provider.applied} == {"draft"}
        assert {a["team_id"] for a in provider.applied} == {"retail"}
        assert all(a["publish_contract"] for a in provider.applied)
        assert "(draft)" in out

    def test_a_failed_product_blocks_only_the_products_that_consume_it(self, products, capsys):
        provider = FakeProvider(fail={"silver.orders"})
        code, out = _run(["publish", str(products)], provider, capsys)
        assert code == 1
        assert "gold.revenue" not in provider.order  # blocked: never sent
        assert {"bronze.raw_orders", "silver.orders", "loose.report"} <= set(provider.order)
        assert "blocked" in out and "upstream silver.orders did not publish" in out
        assert "FAILED" in out
        assert "2 published, 1 failed, 1 blocked" in out
        assert "run resumes" in out

    def test_blocking_follows_the_chain_to_the_end(self, products, capsys):
        provider = FakeProvider(fail={"bronze.raw_orders"})
        code, out = _run(["publish", str(products)], provider, capsys)
        assert code == 1
        assert provider.order.count("silver.orders") == 0
        assert provider.order.count("gold.revenue") == 0
        assert "upstream bronze.raw_orders did not publish" in out
        assert "upstream silver.orders did not publish" in out

    def test_a_retired_product_is_skipped_and_the_run_stays_green(self, products, capsys):
        provider = FakeProvider(retired={"loose.report"})
        code, out = _run(["publish", str(products), "--status", "draft"], provider, capsys)
        assert code == 0
        assert "skipped" in out and "keeps it retired" in out
        assert "1 skipped" in out
        assert "cannot be published again" in out

    def test_a_retired_upstream_does_not_block_its_consumers(self, products, capsys):
        provider = FakeProvider(retired={"bronze.raw_orders"})
        code, _ = _run(["publish", str(products), "--status", "draft"], provider, capsys)
        assert code == 0
        assert "silver.orders" in provider.order and "gold.revenue" in provider.order

    def test_a_dry_run_previews_every_product_and_sends_none(self, products, capsys):
        provider = FakeProvider()
        code, out = _run(["publish", str(products), "--dry-run"], provider, capsys)
        assert code == 0
        assert all(a["dry_run"] for a in provider.applied)
        assert "Previewing" in out and "would publish" in out
        assert "Next: verify" not in out

    def test_json_is_one_report_with_the_order_and_every_outcome(self, products, capsys):
        provider = FakeProvider(fail={"loose.report"})
        code, out = _run(["publish", str(products), "--format", "json"], provider, capsys)
        report = json.loads(out)
        assert code == 1
        assert report["dry_run"] is False
        assert report["order"].index("bronze.raw_orders") < report["order"].index("gold.revenue")
        outcomes = {r["product_id"]: r["outcome"] for r in report["results"]}
        assert outcomes["loose.report"] == "failed"
        assert outcomes["gold.revenue"] == "published"
        assert report["summary"]["failed"] == 1
        assert report["summary"]["published"] == 3

    def test_an_upstream_nothing_holds_is_named_not_failed(self, tmp_path, capsys):
        _write(tmp_path, "consumer", ["ghost.product"])
        _write(tmp_path, "other")
        provider = FakeProvider(held={"unrelated"})
        code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 0
        assert "consumer consumes ghost.product" in out
        assert "points at nothing yet" in out

    def test_an_upstream_entropy_holds_is_not_reported(self, tmp_path, capsys):
        _write(tmp_path, "consumer", ["held.upstream"])
        _write(tmp_path, "other")
        provider = FakeProvider(held={"held.upstream"})
        _, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert "points at nothing yet" not in out

    def test_when_the_list_cannot_be_read_no_upstream_is_called_missing(self, tmp_path, capsys):
        _write(tmp_path, "consumer", ["ghost.product"])
        _write(tmp_path, "other")
        provider = FakeProvider(held_unreadable=True)
        code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 0
        assert "points at nothing yet" not in out

    def test_a_contract_that_cannot_be_loaded_fails_alone(self, tmp_path, capsys):
        _write(tmp_path, "good")
        _write(tmp_path, "good2")
        (tmp_path / "broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        provider = FakeProvider()
        code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 1
        assert provider.order == ["good", "good2"]
        assert "FAILED" in out and "broken.fluid.yaml" in out

    def test_a_contract_without_an_id_fails_alone(self, tmp_path, capsys):
        _write(tmp_path, "good")
        _write(tmp_path, "good2")
        noid = _contract("x")
        del noid["id"]
        (tmp_path / "noid.fluid.yaml").write_text(yaml.safe_dump(noid), encoding="utf-8")
        provider = FakeProvider()
        code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 1
        assert sorted(provider.order) == ["good", "good2"]

    def test_two_files_with_one_id_stop_the_run_before_anything_is_sent(self, tmp_path, capsys):
        _write(tmp_path, "a", sub="one")
        _write(tmp_path, "a", sub="two")
        _write(tmp_path, "b")
        provider = FakeProvider()
        with pytest.raises(CLIError) as exc:
            _run(["publish", str(tmp_path)], provider, capsys)
        assert exc.value.event == "dmm_batch_duplicate_product"
        assert provider.applied == []

    def test_a_loop_stops_the_run_before_anything_is_sent(self, tmp_path, capsys):
        _write(tmp_path, "a", ["b"])
        _write(tmp_path, "b", ["a"])
        provider = FakeProvider()
        with pytest.raises(CLIError) as exc:
            _run(["publish", str(tmp_path)], provider, capsys)
        assert exc.value.event == "dmm_batch_dependency_cycle"
        assert provider.applied == []

    def test_an_odcs_contract_failure_leaves_the_product_published_and_the_run_red(
        self, products, capsys
    ):
        provider = FakeProvider(odcs_failed={"loose.report"})
        code, out = _run(
            ["publish", str(products), "--with-contract", "--fail-on-contract-error"],
            provider,
            capsys,
        )
        assert code == 1
        assert "4 published" in out

    def test_several_files_named_outright_are_a_batch_too(self, tmp_path, capsys):
        gold = _write(tmp_path, "gold", ["silver"])
        silver = _write(tmp_path, "silver")
        provider = FakeProvider()
        code, _ = _run(["publish", str(gold), str(silver)], provider, capsys)
        assert code == 0
        assert provider.order == ["silver", "gold"]


class TestBatchLifecycleAndBlocking:
    def test_a_dry_run_says_what_the_real_run_skips_and_keeps(self, products, capsys):
        provider = FakeProvider(
            held={"loose.report": "retired", "bronze.raw_orders": "active", "x": "deprecated"}
        )
        code, out = _run(
            ["publish", str(products), "--status", "draft", "--dry-run"], provider, capsys
        )
        assert code == 0
        assert "loose.report" not in provider.order  # the retired product is not even previewed
        assert "a real run skips it" in out
        assert "bronze.raw_orders (active) - kept active: draft never demotes" in out
        assert "2 skipped" not in out and "1 skipped" in out

    def test_a_dry_run_without_a_status_predicts_nothing(self, products, capsys):
        provider = FakeProvider(held={"loose.report": "retired", "bronze.raw_orders": "active"})
        code, out = _run(["publish", str(products), "--dry-run"], provider, capsys)
        assert code == 0
        assert "a real run skips it" not in out and "kept" not in out
        assert "loose.report" in provider.order

    def test_a_kept_deprecated_status_says_so(self, products, capsys):
        provider = FakeProvider(held={"loose.report": "deprecated"})
        code, out = _run(["publish", str(products), "--status", "active"], provider, capsys)
        assert code == 0
        assert "kept deprecated: forge does not revive a deprecated product" in out

    def test_a_contract_that_fails_strict_validation_blocks_the_products_that_consume_it(
        self, tmp_path, capsys
    ):
        # No exposes: it loads and declares an id, but is not a valid contract.
        broken = _contract("silver.orders", ["bronze.raw_orders"])
        del broken["exposes"]
        (tmp_path / "silver.fluid.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
        _write(tmp_path, "bronze.raw_orders")
        _write(tmp_path, "gold.revenue", ["silver.orders"])
        provider = FakeProvider()
        code, out = _run(
            ["publish", str(tmp_path), "--validation-mode", "strict"], provider, capsys
        )
        assert code == 1
        assert provider.order == ["bronze.raw_orders"]  # silver failed, gold is blocked
        assert "upstream silver.orders did not publish" in out
        assert "1 published, 1 failed, 1 blocked" in out


class TestBatchLoadFailures:
    def test_a_contract_the_loader_refuses_blocks_its_consumers_by_the_id_it_declares(
        self, tmp_path, capsys
    ):
        real_load = dmm.load_contract_with_overlay

        def loader(path, env, log):
            if Path(path).name == "up.fluid.yaml":
                raise RuntimeError("cannot resolve a $ref")
            return real_load(path, env, log)

        (tmp_path / "up.fluid.yaml").write_text(
            yaml.safe_dump(_contract("bronze.raw_orders")), encoding="utf-8"
        )
        _write(tmp_path, "gold.revenue", ["bronze.raw_orders"])
        _write(tmp_path, "free.report")
        provider = FakeProvider()
        with patch.object(dmm, "load_contract_with_overlay", side_effect=loader):
            code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 1
        assert provider.order == ["free.report"]
        assert "upstream bronze.raw_orders did not publish" in out
        assert "1 published, 1 failed, 1 blocked" in out

    def test_a_file_that_does_not_parse_names_no_product_and_fails_alone(self, tmp_path, capsys):
        (tmp_path / "broken.fluid.yaml").write_text("id: [unclosed\n", encoding="utf-8")
        _write(tmp_path, "good")
        _write(tmp_path, "good2", ["nothing.here"])
        provider = FakeProvider()
        code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 1 and provider.order == ["good", "good2"]

    def test_a_contract_whose_id_is_not_a_string_fails_alone(self, tmp_path, capsys):
        bad = _contract("x")
        bad["id"] = ["not", "a", "string"]
        (tmp_path / "bad.fluid.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
        _write(tmp_path, "good")
        _write(tmp_path, "good2")
        provider = FakeProvider()
        code, out = _run(["publish", str(tmp_path)], provider, capsys)
        assert code == 1
        assert provider.order == ["good", "good2"]
        assert "the contract's id is not a string" in out

    def test_what_the_validator_found_is_shown_with_its_file(self, tmp_path, capsys):
        # No exposes: it loads and publishes in the default warn mode, with the findings shown.
        broken = _contract("silver.orders")
        del broken["exposes"]
        (tmp_path / "silver.fluid.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
        _write(tmp_path, "gold.revenue")
        provider = FakeProvider()
        code, out, _err = _run_full(["publish", str(tmp_path)], provider, capsys)
        assert code == 0
        assert "silver.fluid.yaml:" in out
        assert "'exposes' is a required property" in out
        assert "Valid FLUID contract" not in out  # the all-clear banner stays out

    def test_in_json_mode_the_findings_go_to_stderr_and_stdout_is_the_document(
        self, tmp_path, capsys
    ):
        broken = _contract("silver.orders")
        del broken["exposes"]
        (tmp_path / "silver.fluid.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
        _write(tmp_path, "gold.revenue")
        code, out, err = _run_full(
            ["publish", str(tmp_path), "--format", "json"], FakeProvider(), capsys
        )
        assert code == 0
        assert json.loads(out)["summary"]["published"] == 2
        assert "'exposes' is a required property" in err

    def test_a_dry_run_predicts_that_a_deprecated_product_keeps_its_status(self, products, capsys):
        provider = FakeProvider(held={"loose.report": "deprecated"})
        code, out = _run(
            ["publish", str(products), "--status", "active", "--dry-run"], provider, capsys
        )
        assert code == 0
        assert "loose.report (deprecated) - kept deprecated" in out


# ── the single-contract path and the parser ───────────────────────────────


class TestSingleAndParser:
    def test_one_file_keeps_the_single_publish_output(self, tmp_path, capsys):
        only = _write(tmp_path, "solo")
        provider = FakeProvider()
        code, out = _run(["publish", str(only), "--status", "active"], provider, capsys)
        assert code == 0
        assert provider.applied[0]["status"] == "active"
        assert "Published" in out
        assert "upstream products first" not in out

    def test_a_folder_is_a_batch_even_when_it_holds_one_contract(self, tmp_path, capsys):
        # The output and the exit codes must not change when a folder grows from one
        # contract to two.
        _write(tmp_path, "solo")
        provider = FakeProvider(retired={"solo"})
        code, out = _run(["publish", str(tmp_path), "--status", "draft"], provider, capsys)
        assert code == 0
        assert provider.order == ["solo"]
        assert "upstream products first" in out
        assert "1 skipped" in out

    def test_a_glob_is_a_batch_too(self, tmp_path, capsys):
        _write(tmp_path, "solo")
        provider = FakeProvider()
        code, out = _run(["publish", str(tmp_path / "*.fluid.yaml")], provider, capsys)
        assert code == 0 and "upstream products first" in out

    def test_a_typed_error_reaches_the_cli_handler_instead_of_being_printed_flat(
        self, tmp_path, capsys
    ):
        (tmp_path / "empty").mkdir()
        with pytest.raises(CLIError) as exc:
            _run(["publish", str(tmp_path / "empty")], FakeProvider(), capsys)
        assert exc.value.event == "dmm_batch_no_contracts"

    def test_a_file_that_does_not_exist_is_one_error_line_as_before(self, tmp_path, capsys):
        code, _ = _run(["publish", str(tmp_path / "missing.fluid.yaml")], FakeProvider(), capsys)
        assert code == 1

    def test_in_a_batch_a_file_that_does_not_exist_fails_alone(self, tmp_path, capsys):
        good = _write(tmp_path, "good")
        provider = FakeProvider()
        code, out = _run(
            ["publish", str(good), str(tmp_path / "missing.fluid.yaml")], provider, capsys
        )
        assert code == 1 and provider.order == ["good"]
        assert "missing.fluid.yaml" in out and "FAILED" in out

    def test_a_retired_product_on_a_single_publish_is_an_error_that_says_why(
        self, tmp_path, capsys
    ):
        only = _write(tmp_path, "solo")
        code, _ = _run(
            ["publish", str(only), "--status", "draft"], FakeProvider(retired={"solo"}), capsys
        )
        assert code == 1

    def test_single_publish_json_is_the_provider_result(self, tmp_path, capsys):
        only = _write(tmp_path, "solo")
        code, out = _run(["publish", str(only), "--format", "json"], FakeProvider(), capsys)
        assert code == 0
        assert json.loads(out)["product_id"] == "solo"

    def test_publish_takes_several_contracts_a_status_and_a_format(self):
        parser = argparse.ArgumentParser()
        dmm.add_parser(parser.add_subparsers())
        args = parser.parse_args(["dmm", "publish", "a", "b/", "c*", "--status", "draft"])
        assert args.contract == ["a", "b/", "c*"]
        assert args.status == "draft"
        assert args.format == "text"
        with pytest.raises(SystemExit):
            parser.parse_args(["dmm", "publish", "a", "--status", "retired"])
        with pytest.raises(SystemExit):
            parser.parse_args(["dmm", "publish"])

    def test_the_cold_help_path_does_not_load_the_batch_modules(self):
        import subprocess
        import sys

        code = (
            "import sys; import fluid_build.cli.datamesh_manager as m; "
            "import argparse; p = argparse.ArgumentParser(); m.add_parser(p.add_subparsers()); "
            "heavy = [n for n in ('fluid_build.cli._dmm_batch', 'fluid_build.cli._dmm_promote', "
            "'httpx', 'requests') if n in sys.modules]; print(heavy)"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        ).stdout.strip()
        assert out == "[]"
