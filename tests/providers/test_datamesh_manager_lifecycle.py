# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Data product lifecycle on the Data Mesh Manager provider: draft, active, retired.

``publish(status=...)`` sends a managed status and never demotes or revives; ``promote_product``
moves a draft to active with a read-modify-write that touches nothing else. The provider talks
to a small in-memory stand-in for Entropy's product endpoints.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import pytest

from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.datamesh_manager._lifecycle import (
    ProductRetiredError,
    product_status,
    promotion_blocker,
    set_product_status,
    status_to_send,
)

API = "https://api.example.test"


def _contract(product_id: str = "silver.orders", upstream: Optional[str] = None) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": product_id,
        "name": "Orders",
        "metadata": {"layer": "Silver", "owner": {"team": "retail"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/orders.parquet"},
                },
                "contract": {"schema": [{"name": "order_id", "type": "VARCHAR"}]},
            }
        ],
    }
    if upstream:
        contract["consumes"] = [{"productId": upstream, "exposeId": "orders"}]
    return contract


class FakeEntropy:
    """The product endpoints Entropy serves, in memory: list, get, put."""

    def __init__(
        self,
        products: Optional[Dict[str, Dict[str, Any]]] = None,
        unreadable: Iterable[str] = (),
        list_fails: bool = False,
    ) -> None:
        self.products = copy.deepcopy(products or {})
        self.unreadable = set(unreadable)  # GET of these answers 500
        self.list_fails = list_fails
        self.calls: List[tuple] = []

    def request(self, method: str, path: str, *, json_body: Any = None) -> MagicMock:
        self.calls.append((method, path, copy.deepcopy(json_body)))
        resp = MagicMock(status_code=200)
        if method == "GET" and path.startswith("/api/dataproducts?p="):
            if self.list_fails:
                raise ProviderError(f"Entropy Data API error 500 on GET {path}: boom")
            resp.json.return_value = [
                {"id": pid, "status": body.get("status")} for pid, body in self.products.items()
            ]
        elif path.startswith("/api/dataproducts/"):
            pid = unquote(path.split("/api/dataproducts/", 1)[1])
            if method == "GET":
                if pid in self.unreadable:
                    raise ProviderError(f"Entropy Data API error 500 on GET {path}: boom")
                if pid not in self.products:
                    raise ProviderError(f"Entropy Data API error 404 on GET {path}: not found")
                resp.json.return_value = copy.deepcopy(self.products[pid])
            elif method == "PUT":
                self.products[pid] = copy.deepcopy(json_body)
        elif method == "GET" and path.startswith("/api/datacontracts/"):
            raise ProviderError(f"Entropy Data API error 404 on GET {path}: not found")
        elif method == "GET" and path.startswith("/api/access?"):
            resp.json.return_value = []
        else:
            resp.json.return_value = {}
        return resp

    def puts(self) -> List[tuple]:
        return [c for c in self.calls if c[0] == "PUT" and c[1].startswith("/api/dataproducts/")]

    def lists(self) -> List[tuple]:
        return [c for c in self.calls if c[1].startswith("/api/dataproducts?p=")]


def _publish(entropy: FakeEntropy, contract: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    with (
        patch.object(DataMeshManagerProvider, "_request", side_effect=entropy.request),
        patch.object(DataMeshManagerProvider, "_ensure_team") as ensure_team,
    ):
        _publish.ensure_team = ensure_team  # type: ignore[attr-defined]
        return provider._publish_one(contract, data_product_specification="odps", **kw)


def _promote(entropy: FakeEntropy, product_id: str, **kw: Any) -> Dict[str, Any]:
    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    with patch.object(DataMeshManagerProvider, "_request", side_effect=entropy.request):
        return provider.promote_product(product_id, **kw)


# ── the pure helpers ──────────────────────────────────────────────────────


class TestStatusHelpers:
    def test_status_is_read_from_the_root_for_odps_and_from_info_for_dps(self):
        assert product_status({"status": "Active"}) == "active"
        assert product_status({"info": {"status": "draft"}}) == "draft"
        assert product_status({"info": {}}) is None
        assert product_status("not a body") is None
        assert product_status({"status": "  "}) is None

    def test_status_is_set_where_the_shape_keeps_it(self):
        odps: Dict[str, Any] = {"status": "draft"}
        dps: Dict[str, Any] = {"info": {"status": "draft"}}
        set_product_status(odps, "active")
        set_product_status(dps, "active")
        assert odps == {"status": "active"}
        assert dps == {"info": {"status": "active"}}

    def test_a_status_only_moves_forward(self):
        assert status_to_send("draft", "active") == "active"  # never demotes
        assert status_to_send("draft", None) == "draft"
        assert status_to_send("draft", "draft") == "draft"
        assert status_to_send("active", "draft") == "active"  # promotes
        assert status_to_send("active", "active") == "active"
        assert status_to_send("active", None) == "active"

    def test_a_deprecated_product_keeps_its_status_whatever_is_asked(self):
        # Forge does not revive it, as promote does not either.
        assert status_to_send("draft", "deprecated") == "deprecated"
        assert status_to_send("active", "deprecated") == "deprecated"

    def test_only_a_draft_is_promoted(self):
        assert promotion_blocker("draft") is None
        assert promotion_blocker("proposed") is None
        assert promotion_blocker("active") == "already active"
        assert "stays retired" in (promotion_blocker("retired") or "")
        assert "deprecated" in (promotion_blocker("deprecated") or "")
        assert promotion_blocker(None) is not None


# ── publish(status=...) ───────────────────────────────────────────────────


class TestPublishWithStatus:
    def test_a_new_product_goes_out_as_the_status_asked(self):
        entropy = FakeEntropy()
        result = _publish(entropy, _contract(), status="draft")
        assert entropy.products["silver.orders"]["status"] == "draft"
        assert result["status"] == "draft"
        assert "status_kept" not in result

    def test_draft_never_demotes_a_product_entropy_holds_as_active(self):
        entropy = FakeEntropy({"silver.orders": {"id": "silver.orders", "status": "active"}})
        result = _publish(entropy, _contract(), status="draft")
        assert entropy.products["silver.orders"]["status"] == "active"
        assert result["status"] == "active"
        assert result["status_kept"] == "active"

    def test_active_is_sent_as_active(self):
        entropy = FakeEntropy({"silver.orders": {"id": "silver.orders", "status": "draft"}})
        _publish(entropy, _contract(), status="active")
        assert entropy.products["silver.orders"]["status"] == "active"

    def test_a_retired_product_is_refused_before_anything_is_sent(self):
        entropy = FakeEntropy({"silver.orders": {"id": "silver.orders", "status": "retired"}})
        with pytest.raises(ProductRetiredError) as exc:
            _publish(entropy, _contract(), status="draft")
        assert exc.value.product_id == "silver.orders"
        assert "stays retired" in str(exc.value)
        assert entropy.puts() == []
        _publish.ensure_team.assert_not_called()  # type: ignore[attr-defined]

    def test_without_a_status_a_publish_behaves_as_before(self):
        # The lifecycle rules apply only when --status is asked for: the default publish
        # still sends the contract's own status and does not look at the existing one.
        entropy = FakeEntropy({"silver.orders": {"id": "silver.orders", "status": "retired"}})
        result = _publish(entropy, _contract())
        assert result["success"] is True
        assert "status" not in result
        assert len(entropy.puts()) == 1

    @pytest.mark.parametrize("bad", ["retired", "deprecated", "Draft", ""])
    def test_only_draft_and_active_can_be_asked_for(self, bad):
        with pytest.raises(ProviderError, match="Invalid product status"):
            _publish(FakeEntropy(), _contract(), status=bad)

    def test_a_dry_run_shows_the_status_and_that_draft_never_demotes(self):
        entropy = FakeEntropy()
        result = _publish(entropy, _contract(), status="draft", dry_run=True)
        assert result["payload"]["status"] == "draft"
        assert result["status"] == "draft"
        assert "never demotes" in result["status_note"]
        assert entropy.puts() == []

    def test_a_caller_that_already_read_the_product_list_saves_the_read(self):
        # An upstream spelled with a capital is looked up in Entropy's list; a batch reads
        # that list once and hands it in, so the publish does not read it again.
        contract = _contract(upstream="Acme.Orders")
        entropy = FakeEntropy()
        result = _publish(entropy, contract, held_product_ids={"Acme.Orders"})
        assert entropy.lists() == []
        ports = entropy.products["silver.orders"]["inputPorts"]
        assert ports[0]["contractId"].startswith("Acme.Orders")
        assert result["success"] is True

    def test_without_the_list_the_publish_reads_it_when_an_upstream_needs_it(self):
        entropy = FakeEntropy({"Acme.Orders": {"id": "Acme.Orders"}})
        _publish(entropy, _contract(upstream="Acme.Orders"))
        assert len(entropy.lists()) == 1


class TestPublishNeverGuessesWhatItCannotRead:
    def test_an_unreadable_product_is_an_error_when_a_status_is_asked_for(self):
        # Treated as absent, a draft publish would demote an active product and overwrite a
        # retired one: the rules would fail open on a 500 or a timeout.
        for held in ("active", "retired"):
            entropy = FakeEntropy(
                {"silver.orders": {"id": "silver.orders", "status": held}},
                unreadable={"silver.orders"},
            )
            with pytest.raises(ProviderError, match="Could not read data product"):
                _publish(entropy, _contract(), status="draft")
            assert entropy.puts() == []
            _publish.ensure_team.assert_not_called()  # type: ignore[attr-defined]

    def test_without_a_status_an_unreadable_product_is_published_as_before(self):
        entropy = FakeEntropy(
            {"silver.orders": {"id": "silver.orders", "status": "active"}},
            unreadable={"silver.orders"},
        )
        result = _publish(entropy, _contract())
        assert result["success"] is True
        assert len(entropy.puts()) == 1

    def test_a_deprecated_product_keeps_its_status_whatever_is_asked(self):
        for asked in ("draft", "active"):
            entropy = FakeEntropy(
                {"silver.orders": {"id": "silver.orders", "status": "deprecated"}}
            )
            result = _publish(entropy, _contract(), status=asked)
            assert entropy.products["silver.orders"]["status"] == "deprecated"
            assert result["status"] == result["status_kept"] == "deprecated"


class TestKnownProductIds:
    """A caller that already read the product list hands it to the publish."""

    @staticmethod
    def _access_puts(entropy: FakeEntropy) -> List[str]:
        return [c[1] for c in entropy.calls if c[0] == "PUT" and c[1].startswith("/api/access/")]

    def test_an_upstream_in_the_list_gets_its_access_agreement_with_no_further_read(self):
        entropy = FakeEntropy()
        result = _publish(entropy, _contract(upstream="up.stream"), held_product_ids={"up.stream"})
        assert entropy.lists() == []
        assert len(self._access_puts(entropy)) == 1
        assert result["access_agreements"][0]["success"] is True

    def test_an_upstream_missing_from_a_non_empty_list_is_skipped_with_its_reason(self):
        entropy = FakeEntropy()
        result = _publish(entropy, _contract(upstream="up.stream"), held_product_ids={"other"})
        assert self._access_puts(entropy) == []
        (agreement,) = result["access_agreements"]
        assert agreement["skipped"] is True
        assert agreement["reason"] == "missing_upstream_product"

    def test_a_caller_that_passes_nothing_still_lists_once_for_the_pre_flight(self):
        entropy = FakeEntropy({"up.stream": {"id": "up.stream"}})
        result = _publish(entropy, _contract(upstream="up.stream"))
        assert len(entropy.lists()) == 1
        assert result["access_agreements"][0]["success"] is True

    def test_held_products_is_one_read_with_each_status(self):
        entropy = FakeEntropy(
            {
                "a": {"id": "a", "status": "active"},
                "b": {"id": "b", "status": "draft"},
                "c": {"id": "c"},
            }
        )
        provider = DataMeshManagerProvider(api_key="k", api_url=API)
        with patch.object(DataMeshManagerProvider, "_request", side_effect=entropy.request):
            held = provider.held_products()
        assert held == {"a": "active", "b": "draft", "c": None}
        assert len(entropy.lists()) == 1

    def test_held_products_says_none_when_the_list_cannot_be_read(self):
        provider = DataMeshManagerProvider(api_key="k", api_url=API)
        with patch.object(
            DataMeshManagerProvider, "_request", side_effect=FakeEntropy(list_fails=True).request
        ):
            assert provider.held_products() is None
            assert provider._held_product_ids() is None


# ── promote_product ───────────────────────────────────────────────────────


class TestPromote:
    def test_a_draft_goes_active_and_nothing_else_moves(self):
        body = {
            "id": "silver.orders",
            "status": "draft",
            "name": "Orders",
            "tags": ["retail"],
            "outputPorts": [{"name": "orders", "contractId": "silver.orders.orders"}],
        }
        entropy = FakeEntropy({"silver.orders": body})
        result = _promote(entropy, "silver.orders")
        assert result["outcome"] == "promoted"
        assert result["from"] == "draft"
        assert entropy.products["silver.orders"] == {**body, "status": "active"}

    def test_a_data_product_specification_body_keeps_its_status_in_info(self):
        body = {"id": "p", "info": {"title": "P", "status": "draft"}}
        entropy = FakeEntropy({"p": body})
        _promote(entropy, "p")
        assert entropy.products["p"] == {"id": "p", "info": {"title": "P", "status": "active"}}

    @pytest.mark.parametrize(
        ("current", "fragment"),
        [("active", "already active"), ("deprecated", "deprecated"), ("retired", "retired")],
    )
    def test_anything_but_a_draft_is_left_alone(self, current, fragment):
        entropy = FakeEntropy({"p": {"id": "p", "status": current}})
        result = _promote(entropy, "p")
        assert result["outcome"] == "skipped"
        assert fragment in result["reason"]
        assert entropy.puts() == []

    def test_a_product_entropy_does_not_hold_is_an_error_that_says_to_publish_it(self):
        with pytest.raises(ProviderError, match="publish it first"):
            _promote(FakeEntropy(), "missing")

    def test_a_dry_run_reads_and_writes_nothing(self):
        entropy = FakeEntropy({"p": {"id": "p", "status": "draft"}})
        result = _promote(entropy, "p", dry_run=True)
        assert result["outcome"] == "would-promote"
        assert entropy.puts() == []
        assert entropy.products["p"]["status"] == "draft"

    def test_an_id_with_a_slash_is_escaped_in_the_path(self):
        entropy = FakeEntropy({"a/b": {"id": "a/b", "status": "draft"}})
        _promote(entropy, "a/b")
        assert entropy.puts()[0][1] == "/api/dataproducts/a%2Fb"
