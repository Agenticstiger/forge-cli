# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile`` on a flux-stats whose reader predates profile format 1-4-0.

The ``[profile]`` extra asks for flux-stats 0.11 and later; an environment that kept an
earlier release still gets a clear answer. A 0.9 or 0.10 request reader returns objects without the 1-2-0 asks, and has no rank correlations or follows reach.
Both cases run here on stand-ins, so they hold on any installed release: an earlier
request reads as it always did, and a 1-4-0 feature says what it needs.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from fluid_build.profiler import _stats

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats")


def _earlier_request() -> SimpleNamespace:
    """What a flux-stats 0.10 ``load_request_text`` returns: no 1-2-0 attribute anywhere."""
    column = SimpleNamespace(name="amount", domain="decimal", metrics=["min/1"], statistics=[])
    table = SimpleNamespace(expose_id="orders", row_count=3, columns=[column])
    end = SimpleNamespace(table="orders", columns=["id"])
    link = SimpleNamespace(
        parent=end, child=SimpleNamespace(table="lines", columns=["order"]), statistics=[]
    )
    return SimpleNamespace(
        contract_digest="sha256:" + "0" * 64,
        fractional_bits=8,
        tables=[table],
        relationships=[link],
        facts=[],
    )


def test_a_request_from_an_earlier_reader_reads_with_no_1_2_0_ask(monkeypatch):
    request_module = pytest.importorskip("flux_stats.request")
    monkeypatch.setattr(request_module, "load_request_text", lambda text, name: _earlier_request())
    read = _stats.load_backend().read_request("ignored", name="one.profile-request.yml")
    (table,) = read.tables
    assert table.expose_id == "orders" and table.columns[0].name == "amount"
    assert table.rank_correlations == () and table.clique_columns == ()
    (link,) = read.relationships
    assert link.group_by == () and link.linked == ()


@pytest.mark.parametrize(
    "call",
    [
        lambda backend: backend.rank_group("kendallTau/1", [(1, 2), (2, 1)]),
        lambda backend: backend.rank_value("kendallTau/1", [(1, 2), (2, 1)], 2),
        lambda backend: backend.rank_sensitivity("kendallTau/1", 2),
    ],
)
def test_a_rank_correlation_without_the_newer_release_says_what_it_needs(monkeypatch, call):
    monkeypatch.setitem(sys.modules, "flux_stats.rank_correlation", None)
    with pytest.raises(_stats.StatisticsUnavailable) as refused:
        call(_stats.load_backend())
    assert "needs flux-stats 0.11 (profile format 1-4-0)" in str(refused.value)


def test_a_follow_without_the_newer_release_says_what_it_needs(monkeypatch):
    backend = _stats.load_backend()
    monkeypatch.setitem(sys.modules, "flux_stats.noise", types.ModuleType("flux_stats.noise"))
    follow = _stats.FollowEntry("orders", ("id",), "lines", ("order",), 2)
    with pytest.raises(_stats.StatisticsUnavailable) as refused:
        backend.follow_reach("orders", [follow])
    assert "--follow needs flux-stats 0.11 (profile format 1-4-0)" in str(refused.value)
