# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The pre-plan guard for Glue resources of an Iceberg table that moved catalogs.

A contract the previous release applied with ``catalog: lakekeeper`` (or rest,
polaris, nessie...) has a Glue database and table in its OpenTofu state, because
that release created them whatever the catalog said. The emitter no longer
does, so the next plan would DESTROY them, and destroying a Glue database
deletes every table in it. ``fluid apply`` fails closed before ``tofu plan``
with the ``tofu state rm`` commands instead (``iac/catalog_moves.py``).
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac.catalog_moves import (
    CatalogMoveError,
    detect_catalog_moves,
    guard_catalog_moves,
    moved_iceberg_exposes,
)
from fluid_build.iac.providers.aws import AwsIacPlugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]

SCHEMA = [{"name": "order_id", "type": "string"}, {"name": "qty", "type": "integer"}]
DB = "aws_glue_catalog_database.analytics_lake_sales"
ORDERS = "aws_glue_catalog_table.analytics_lake_sales_orders"
FACTS = "aws_glue_catalog_table.analytics_lake_sales_facts"
BUCKET = "aws_s3_bucket.analytics_lake_lake"


def _expose(
    fmt: str = "iceberg",
    catalog: Optional[str] = None,
    *,
    expose_id: str = "orders",
    table: str = "orders",
) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": "sales",
        "table": table,
        "bucket": "lake",
        "path": f"{table}/",
    }
    if catalog is not None:
        location["catalog"] = catalog
    return {
        "exposeId": expose_id,
        "binding": {"platform": "aws", "format": fmt, "location": location},
        "contract": {"schema": copy.deepcopy(SCHEMA)},
    }


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": "analytics.lake", "name": "Lake", "exposes": list(exposes)}


class _SpyPlugin(AwsIacPlugin):
    """The real AWS plugin, counting its emits (the fast path makes none)."""

    def __init__(self) -> None:
        self.emits = 0

    def emit(self, contract, actions=()):
        self.emits += 1
        return super().emit(contract, actions)


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


class TestDetect:
    @pytest.mark.parametrize(
        "catalog", ["lakekeeper", "rest", "iceberg-rest", "iceberg_rest", "polaris", "nessie"]
    )
    def test_the_old_glue_resources_in_state_are_found(self, catalog):
        contract = _contract(_expose("iceberg", catalog))
        state = [BUCKET, DB, ORDERS]

        assert detect_catalog_moves(AwsIacPlugin(), contract, state) == (DB, ORDERS)

    def test_only_what_the_state_holds_is_reported(self):
        contract = _contract(_expose("iceberg", "lakekeeper"))
        assert detect_catalog_moves(AwsIacPlugin(), contract, [BUCKET, ORDERS]) == (ORDERS,)

    def test_a_state_without_them_is_a_no_op(self):
        """Applied by this release from the start: nothing to release."""
        contract = _contract(_expose("iceberg", "lakekeeper"))
        assert detect_catalog_moves(AwsIacPlugin(), contract, [BUCKET]) == ()

    def test_an_empty_state_emits_nothing(self):
        plugin = _SpyPlugin()
        contract = _contract(_expose("iceberg", "lakekeeper"))
        assert detect_catalog_moves(plugin, contract, []) == ()
        assert plugin.emits == 0

    @pytest.mark.parametrize("catalog", [None, "glue", "GLUE"])
    def test_a_glue_contract_is_a_no_op_without_an_emit(self, catalog):
        plugin = _SpyPlugin()
        contract = _contract(_expose("iceberg", catalog), _expose("parquet", "lakekeeper"))

        assert moved_iceberg_exposes(contract) == ()
        assert detect_catalog_moves(plugin, contract, [BUCKET, DB, ORDERS]) == ()
        assert plugin.emits == 0

    def test_a_glue_database_a_parquet_expose_still_uses_is_not_flagged(self):
        contract = _contract(
            _expose("parquet", expose_id="facts", table="facts"),
            _expose("iceberg", "lakekeeper"),
        )
        state = [BUCKET, DB, FACTS, ORDERS]

        assert detect_catalog_moves(AwsIacPlugin(), contract, state) == (ORDERS,)

    def test_a_module_prefixed_address_is_not_this_module_s(self):
        """``fluid`` writes a root module: a child module's resource is not one
        this emit change removes, so it is matched exactly, never by name."""
        contract = _contract(_expose("iceberg", "lakekeeper"))
        state = [f"module.other.{DB}", f"module.other.{ORDERS}"]
        assert detect_catalog_moves(AwsIacPlugin(), contract, state) == ()

    def test_off_aws_and_without_a_database_nothing_moves(self):
        gcp = _expose("iceberg", "lakekeeper")
        gcp["binding"]["platform"] = "gcp"
        no_db = _expose("iceberg", "lakekeeper")
        del no_db["binding"]["location"]["database"]
        assert moved_iceberg_exposes(_contract(gcp, no_db)) == ()

    def test_the_caller_s_contract_is_not_mutated(self):
        contract = _contract(_expose("iceberg", "lakekeeper"))
        before = copy.deepcopy(contract)
        detect_catalog_moves(AwsIacPlugin(), contract, [DB, ORDERS])
        assert contract == before


# ---------------------------------------------------------------------------
# the guard and its message
# ---------------------------------------------------------------------------


class TestGuard:
    def test_raises_with_copy_pasteable_state_rm_commands(self):
        contract = _contract(_expose("iceberg", "lakekeeper"))

        with pytest.raises(CatalogMoveError) as excinfo:
            guard_catalog_moves(
                AwsIacPlugin(), contract, [BUCKET, DB, ORDERS], workdir="/w/aws/analytics lake"
            )

        exc = excinfo.value
        assert exc.kind == "iceberg-catalog-move"
        assert exc.addresses == (DB, ORDERS)
        assert exc.remediation == (
            f"tofu -chdir='/w/aws/analytics lake' state rm {DB}",
            f"tofu -chdir='/w/aws/analytics lake' state rm {ORDERS}",
        )
        message = str(exc)
        assert "exposes[orders]: location.catalog lakekeeper" in message
        assert "DESTROY" in message and "deletes every table in it" in message
        assert "only this contract's claim on them is released" in message
        assert exc.event_fields() == {
            "kind": "iceberg-catalog-move",
            "addresses": [DB, ORDERS],
            "exposes": [{"expose": "orders", "catalog": "lakekeeper"}],
            "remediation": list(exc.remediation),
        }

    def test_without_a_workdir_the_commands_run_where_they_are(self):
        with pytest.raises(CatalogMoveError) as excinfo:
            guard_catalog_moves(
                AwsIacPlugin(), _contract(_expose("iceberg", "rest")), [ORDERS], workdir=None
            )
        assert excinfo.value.remediation == (f"tofu state rm {ORDERS}",)

    def test_passes_when_nothing_would_be_destroyed(self):
        assert (
            guard_catalog_moves(AwsIacPlugin(), _contract(_expose("iceberg", "rest")), [BUCKET])
            is None
        )


# ---------------------------------------------------------------------------
# the apply engine adapter
# ---------------------------------------------------------------------------


class TestEngineAdapter:
    @pytest.fixture
    def engine(self):
        from fluid_build.cli import _apply_opentofu_engine as engine

        return engine

    def _listing(self, monkeypatch, engine, state: List[str]) -> List[str]:
        calls: List[str] = []

        def _list(*_a, **_k):
            calls.append("listed")
            return list(state)

        monkeypatch.setattr(engine.runner, "tofu_state_list", _list)
        return calls

    def test_translates_to_a_cli_error_with_remediation(self, monkeypatch, engine, caplog):
        from fluid_build.cli._common import CLIError

        self._listing(monkeypatch, engine, [BUCKET, DB, ORDERS])
        logger = logging.getLogger("test.catalog_moves")

        with caplog.at_level(logging.DEBUG, logger="test.catalog_moves"):
            with pytest.raises(CLIError) as excinfo:
                engine._guard_catalog_moves(
                    AwsIacPlugin(),
                    _contract(_expose("iceberg", "lakekeeper")),
                    "aws",
                    "/w",
                    {},
                    logger,
                )

        assert excinfo.value.event == "iceberg_catalog_move_blocked"
        context = excinfo.value.context
        assert context["kind"] == "iceberg-catalog-move"
        assert context["remediation"] == [
            f"tofu -chdir=/w state rm {DB}",
            f"tofu -chdir=/w state rm {ORDERS}",
        ]
        # The audit event is recorded before the raise.
        assert "iceberg_catalog_move_blocked" in caplog.text

    def test_a_contract_with_nothing_moved_lists_no_state(self, monkeypatch, engine):
        calls = self._listing(monkeypatch, engine, [BUCKET, DB, ORDERS])
        engine._guard_catalog_moves(
            AwsIacPlugin(), _contract(_expose("iceberg")), "aws", "/w", {}, logging.getLogger("t")
        )
        assert calls == []

    def test_another_provider_is_untouched(self, monkeypatch, engine):
        calls = self._listing(monkeypatch, engine, [DB, ORDERS])
        engine._guard_catalog_moves(
            AwsIacPlugin(),
            _contract(_expose("iceberg", "lakekeeper")),
            "gcp",
            "/w",
            {},
            logging.getLogger("t"),
        )
        assert calls == []

    def test_an_empty_state_passes(self, monkeypatch, engine):
        self._listing(monkeypatch, engine, [])
        engine._guard_catalog_moves(
            _SpyPlugin(),
            _contract(_expose("iceberg", "lakekeeper")),
            "aws",
            "/w",
            {},
            logging.getLogger("t"),
        )

    def test_a_failed_probe_does_not_fail_the_apply(self, monkeypatch, engine):
        class _Broken(AwsIacPlugin):
            def emit(self, contract, actions=()):
                raise RuntimeError("probe exploded")

        self._listing(monkeypatch, engine, [DB, ORDERS])
        engine._guard_catalog_moves(
            _Broken(),
            _contract(_expose("iceberg", "lakekeeper")),
            "aws",
            "/w",
            {},
            logging.getLogger("t"),
        )

    def test_the_engine_runs_it_after_the_packaging_guard_and_before_plan(self, engine):
        import inspect

        source = inspect.getsource(engine.apply_via_opentofu)
        packaging_at = source.index("_guard_packaging_transitions(")
        moves_at = source.index("_guard_catalog_moves(")
        adopt_at = source.index("_adopt_existing(")
        plan_at = source.index("runner.tofu_plan(")
        assert packaging_at < moves_at < adopt_at < plan_at
