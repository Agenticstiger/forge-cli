# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --privacy-unit`` with rank correlations and cliques over binned columns."""

from __future__ import annotations

import csv
import json
import random
import re

import pytest
import yaml

from fluid_build.cli import main
from fluid_build.profiler import _stats, document
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _rank_run as ranked
from tests.profiler.dp import _release_data as data
from tests.profiler.dp._privacy import need

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """The orders with a ``quantity``, a contract and a request that asks for ranks and cliques."""
    monkeypatch.chdir(tmp_path)
    contract = ranked.contract()
    text = yaml.safe_dump(contract, sort_keys=False)
    (tmp_path / "orders.fluid.yaml").write_text(text, encoding="utf-8")
    digest = FakeBackend().contract_digest(text, name="orders.fluid.yaml")
    asked = yaml.safe_load((data.FIXTURES / "orders.profile-request.yml").read_text("utf-8"))
    asked["_schemaURL"] = (
        "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/"
        "FluxProfileRequest.json"
    )
    asked["contract"] = {"digest": digest}
    table = asked["tables"]["orders"]
    table["columns"]["quantity"] = {"domain": "number", "statistics": ["marginalHistogram/1"]}
    table["rankCorrelations"] = [
        {"definition": ranked.KENDALL, "columns": ["amount", "quantity"]},
        {"definition": ranked.SPEARMAN, "columns": ["amount", "quantity"]},
    ]
    table["cliqueColumns"] = list(ranked.JOINING)
    (tmp_path / "orders.profile-request.yml").write_text(
        yaml.safe_dump(asked, sort_keys=False), encoding="utf-8"
    )
    rng = random.Random(5)
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "customer_id", "status", "channel", "amount", "quantity"])
        for row in data.orders():
            writer.writerow([*row, rng.randint(1, 10)])
    return tmp_path


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def _argv(*more, record="rec.json", rho="1"):
    return [
        "profile",
        "orders.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders=orders.csv",
        "--privacy-unit",
        "orders",
        "--unit-key",
        "customer_id",
        "--max-rows-per-unit",
        "3",
        "--rho",
        rho,
        "--release-record",
        record,
        *more,
    ]


def _again(*more, record="rec.json"):
    return [
        "profile",
        "orders.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders=orders.csv",
        "--release-record",
        record,
        "--rederive",
        *more,
    ]


def _out(capsys):
    seen = capsys.readouterr()
    return " ".join((seen.out + seen.err).split())


def _need_selection():
    for module in ("opendp", "mbi", "polars"):
        need(module)


@pytest.mark.parametrize("method", ["aim", "mst"])
def test_a_release_of_ranks_and_mixed_cliques_is_drawn_again_to_the_same_bytes(
    workspace, registry, capsys, method
):
    _need_selection()
    assert main(_argv("--clique-method", method, "--row-floor", "250")) == 0
    out = _out(capsys)
    profile = (workspace / "orders.profile.json").read_bytes()
    body = json.loads(profile)
    assert body["_schemaURL"] == document.SCHEMA_URL_1_4_0
    table = body["tables"]["orders"]
    assert table["cliqueSelection"]["method"] == method
    assert all(
        statistic["rowFloor"] == 250
        for pair in table["columnPairs"]
        for statistic in pair["statistics"]
    )
    record = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))
    expected = {"rowFloor": 250} if method == "aim" else {"rowFloor": 250, "cliqueMethod": "mst"}
    assert record["options"] == expected
    assert [each["kind"] for each in record["tables"] if "kind" in each] == ["rank", "rank"]
    # what the report says: each part's sensitivity, scale and share, and the mechanism
    assert "rank correlation kendallTau/1 of amount and quantity" in out
    assert "row floor 250" in out and "sensitivity 12/251 for one entity" in out
    assert "squared L2 sensitivity 9 for one entity" in out
    assert "exact discrete Gaussian is added on the lattice" in out
    assert f"chosen with {method.upper()}" in out
    assert "measuring" in out and "rank correlations" in out
    # nothing data-dependent: no count of the records appears in it
    rows = len(data.orders())
    assert str(rows) not in set(re.findall(r"(?<![\w.])\d+(?![\w.])", out))
    # the same release again, from the record alone
    (workspace / "orders.profile.json").unlink()
    capsys.readouterr()
    assert main(_again()) == 0
    assert (workspace / "orders.profile.json").read_bytes() == profile
    # flags that agree with the record are taken; one that disagrees is refused
    assert main(_again("--row-floor", "250", "--clique-method", method)) == 0
    capsys.readouterr()
    assert main(_again("--row-floor", "251")) == 1
    assert "profile_release_rederive_disagrees" in _out(capsys)
    assert main(_again("--clique-method", "mst" if method == "aim" else "aim")) == 1
    assert "profile_release_rederive_disagrees" in _out(capsys)


def test_the_default_row_floor_of_a_release_is_a_thousand(workspace, registry, capsys):
    _need_selection()
    assert main(_argv()) == 0
    body = json.loads((workspace / "orders.profile.json").read_bytes())
    (pair,) = body["tables"]["orders"]["columnPairs"]
    assert {each["rowFloor"] for each in pair["statistics"]} == {1000}
    assert json.loads((workspace / "rec.json").read_text("utf-8"))["options"] == {"rowFloor": 1000}


def test_a_row_floor_is_refused_in_a_rederivation_of_a_release_that_has_none(
    workspace, registry, capsys
):
    """A record that states no row floor (no rank correlation) gives none to agree with."""
    _need_selection()
    request = yaml.safe_load((workspace / "orders.profile-request.yml").read_text("utf-8"))
    table = request["tables"]["orders"]
    del table["rankCorrelations"], table["cliqueColumns"]
    (workspace / "orders.profile-request.yml").write_text(yaml.safe_dump(request), "utf-8")
    assert main(_argv()) == 0
    capsys.readouterr()
    body = json.loads((workspace / "orders.profile.json").read_bytes())
    assert body["_schemaURL"] == document.SCHEMA_URL_1_2_0
    assert "options" not in json.loads((workspace / "rec.json").read_text("utf-8"))
    assert main(_again("--row-floor", "5")) == 1
    assert "profile_release_rederive_disagrees" in _out(capsys)


def test_the_clique_method_and_the_row_floor_are_in_the_help():
    import argparse

    from fluid_build.cli import build_parser

    parser = build_parser(["profile"])
    (actions,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    text = " ".join(actions.choices["profile"].format_help().split())
    assert "--clique-method aim|mst" in text
    assert "a noisy kendallTau/1 or spearmanRho/1 of each pair of released number" in text
    assert "row floor --row-floor N (default 1000" in text
