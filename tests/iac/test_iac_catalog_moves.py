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

"""The pre-plan guard for resources of an Iceberg table that moved catalogs.

A contract the previous release applied with ``catalog: lakekeeper`` (or rest,
polaris, nessie...) on AWS has a Glue database and table in its OpenTofu state,
because that release created them whatever the catalog said; on Snowflake,
``lakekeeper`` / ``bigquery`` got an EXTERNAL VOLUME. The emitters no longer
create them, so the next plan would DESTROY them, and destroying a Glue
database deletes every table in it. ``fluid apply`` fails closed before
``tofu plan`` with the ``tofu state rm`` commands instead
(``iac/catalog_moves.py``). That the apply engine runs the guard is pinned
through ``apply_via_opentofu`` in ``test_iac_catalog_move_wiring.py``.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import runner
from fluid_build.iac.catalog_moves import (
    AWS_GLUE_MOVES,
    SNOWFLAKE_VOLUME_MOVES,
    CatalogMoveError,
    catalog_move_spec,
    detect_catalog_moves,
    guard_catalog_moves,
    moved_iceberg_exposes,
)
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

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

    def test_a_database_a_removed_parquet_expose_created_is_not_a_move(self):
        """SEC-707-4: this release applied a Lakekeeper table and a parquet table
        on one Glue database, then the parquet expose was removed. The database
        is no longer declared, but the old release never created it for the
        Iceberg expose (its Glue table is not in state), so destroying it is a
        plain removal for the data-loss gate, not a catalog move."""
        contract = _contract(_expose("iceberg", "lakekeeper"))
        state = [BUCKET, DB, FACTS]

        assert detect_catalog_moves(AwsIacPlugin(), contract, state) == ()
        guard_catalog_moves(AwsIacPlugin(), contract, state)

    def test_a_database_is_flagged_for_the_expose_whose_table_is_in_state(self):
        """Two moved exposes on one database, only one applied by the old
        release: the database and that table are flagged, and the message names
        only that expose."""
        contract = _contract(
            _expose("iceberg", "lakekeeper"),
            _expose("iceberg", "rest", expose_id="facts", table="facts"),
        )
        state = [BUCKET, DB, ORDERS]

        with pytest.raises(CatalogMoveError) as excinfo:
            guard_catalog_moves(AwsIacPlugin(), contract, state)

        assert excinfo.value.addresses == (DB, ORDERS)
        assert excinfo.value.exposes == (("orders", "lakekeeper"),)

    @pytest.mark.parametrize("catalog", ["lakekeeper", "rest", "polaris", "nessie", "bigquery"])
    def test_a_table_less_expose_s_database_is_flagged(self, catalog):
        """RT-707-1: a namespace-level Iceberg expose names a database and no
        table (a dynamic-routing sink's). The old release created the Glue
        database for it and nothing else, so no Glue table can be in state to
        prove it, and destroying the database is Glue ``DeleteDatabase``."""
        expose = _expose("iceberg", catalog)
        del expose["binding"]["location"]["table"]
        contract = _contract(expose)
        state = [BUCKET, DB]

        assert detect_catalog_moves(AwsIacPlugin(), contract, state) == (DB,)
        with pytest.raises(CatalogMoveError) as excinfo:
            guard_catalog_moves(AwsIacPlugin(), contract, state)
        assert excinfo.value.exposes == (("orders", catalog),)

    def test_a_table_less_expose_s_database_a_parquet_expose_uses_is_not_flagged(self):
        """The database stays declared, so the plan does not destroy it."""
        expose = _expose("iceberg", "lakekeeper")
        del expose["binding"]["location"]["table"]
        contract = _contract(_expose("parquet", expose_id="facts", table="facts"), expose)

        assert detect_catalog_moves(AwsIacPlugin(), contract, [BUCKET, DB, FACTS]) == ()

    def test_a_table_less_expose_does_not_vouch_for_another_s_database(self):
        """The evidence rule still holds for an expose that declares a table:
        a table-less sibling on another database flags only its own."""
        table_less = _expose("iceberg", "rest", expose_id="raw", table="raw")
        table_less["binding"]["location"].update(database="raw")
        del table_less["binding"]["location"]["table"]
        contract = _contract(_expose("iceberg", "lakekeeper"), table_less)
        raw_db = "aws_glue_catalog_database.analytics_lake_raw"

        with pytest.raises(CatalogMoveError) as excinfo:
            guard_catalog_moves(AwsIacPlugin(), contract, [BUCKET, DB, FACTS, raw_db])

        assert excinfo.value.addresses == (raw_db,)
        assert excinfo.value.exposes == (("raw", "rest"),)

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
# Snowflake: the EXTERNAL VOLUME an old release created
# ---------------------------------------------------------------------------

VOLUME = "snowflake_external_volume.analytics_lake_vol_FLUID_ANALYTICS_LAKE_VOL"
SF_TABLE = "snowflake_table.analytics_lake_ANALYTICS_SALES_ORDERS"


def _sf_expose(
    catalog: Optional[str] = None,
    *,
    expose_id: str = "orders",
    table: str = "ORDERS",
    fmt: str = "iceberg",
) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": "ANALYTICS",
        "schema": "SALES",
        "table": table,
        "warehouse": "s3://lake/warehouse",
        "iam_role_arn": "arn:aws:iam::123456789012:role/snowflake-lake",
    }
    if catalog is not None:
        location.update(catalog=catalog, uri="http://lakekeeper:8181/catalog")
    return {
        "exposeId": expose_id,
        "binding": {"platform": "snowflake", "format": fmt, "location": location},
        "contract": {"schema": copy.deepcopy(SCHEMA)},
    }


class TestSnowflakeVolume:
    """ARCH-1: 0.19.0 tested the raw catalog against a list that missed
    ``lakekeeper``, ``bigquery`` and the ``iceberg-rest`` spelling, so those
    exposes got an EXTERNAL VOLUME. The emitter now gives them none."""

    @pytest.mark.parametrize(
        "catalog, kind",
        [
            ("lakekeeper", "lakekeeper"),
            ("Lakekeeper", "lakekeeper"),
            ("bigquery", "bigquery"),
            ("iceberg-rest", "rest"),
        ],
    )
    def test_a_volume_the_old_release_created_is_flagged(self, catalog, kind):
        contract = _contract(_sf_expose(catalog))

        assert moved_iceberg_exposes(contract, spec=SNOWFLAKE_VOLUME_MOVES) == (("orders", kind),)
        assert detect_catalog_moves(SnowflakeIacPlugin(), contract, [SF_TABLE, VOLUME]) == (VOLUME,)

    @pytest.mark.parametrize(
        "catalog", [None, "snowflake", "rest", "iceberg_rest", "polaris", "unity", "nessie", "glue"]
    )
    def test_a_catalog_whose_volume_did_not_change_is_not_a_move(self, catalog):
        """No volume before and none now (rest, polaris...), or one both times
        (Snowflake-managed): the old list and the kind table agree."""
        contract = _contract(_sf_expose(catalog))
        assert moved_iceberg_exposes(contract, spec=SNOWFLAKE_VOLUME_MOVES) == ()
        assert detect_catalog_moves(SnowflakeIacPlugin(), contract, [SF_TABLE, VOLUME]) == ()

    def test_an_operator_named_volume_never_moves(self):
        expose = _sf_expose("lakekeeper")
        expose["binding"]["icebergConfig"] = {"properties": {"external_volume": "OPS_VOL"}}
        assert moved_iceberg_exposes(_contract(expose), spec=SNOWFLAKE_VOLUME_MOVES) == ()

    def test_a_volume_a_managed_expose_still_uses_is_not_flagged(self):
        """The volume is named per contract, so a Snowflake-managed expose
        alongside keeps it declared."""
        contract = _contract(
            _sf_expose("lakekeeper"), _sf_expose(None, expose_id="facts", table="FACTS")
        )
        assert detect_catalog_moves(SnowflakeIacPlugin(), contract, [SF_TABLE, VOLUME]) == ()

    @pytest.mark.parametrize(
        "changes",
        [{"warehouse": "analytics"}, {"iam_role_arn": None}],
        ids=["warehouse-now-a-catalog-name", "no-iam-role-arn"],
    )
    def test_a_volume_is_flagged_after_an_upgrade_that_changed_the_location(self, changes):
        """JRN-707-1: the before image re-runs today's emitter over today's
        location, which derives no volume once the warehouse is a Lakekeeper
        warehouse NAME or the role is gone. The volume the old release created
        is keyed from the contract id alone, so it is still found."""
        expose = _sf_expose("lakekeeper")
        location = expose["binding"]["location"]
        for key, value in changes.items():
            if value is None:
                del location[key]
            else:
                location[key] = value
        contract = _contract(expose)

        assert detect_catalog_moves(SnowflakeIacPlugin(), contract, [SF_TABLE, VOLUME]) == (VOLUME,)
        assert detect_catalog_moves(SnowflakeIacPlugin(), contract, [SF_TABLE]) == ()

    @pytest.mark.parametrize(
        "contract_id", ["analytics.lake", "gold.hr.employee_360_v1", "9-lives"]
    )
    def test_the_derived_volume_address_is_the_one_the_emitter_writes(self, contract_id):
        """The one address this module derives instead of emitting: pinned to
        the Snowflake emitter's own key, so the two cannot drift apart."""
        contract = {**_contract(_sf_expose(None)), "id": contract_id}
        binding = contract["exposes"][0]["binding"]
        emitted = SnowflakeIacPlugin().emit(contract)["snowflake_external_volume"]

        assert set(SNOWFLAKE_VOLUME_MOVES.addresses_before(contract, binding)) == {
            f"snowflake_external_volume.{key}" for key in emitted
        }

    def test_raises_with_the_snowflake_remedy(self):
        with pytest.raises(CatalogMoveError) as excinfo:
            guard_catalog_moves(
                SnowflakeIacPlugin(),
                _contract(_sf_expose("lakekeeper")),
                [SF_TABLE, VOLUME],
                workdir="/w/snowflake/analytics.lake",
            )

        exc = excinfo.value
        assert exc.kind == "iceberg-catalog-move"
        assert exc.remediation == (f"tofu -chdir=/w/snowflake/analytics.lake state rm {VOLUME}",)
        message = str(exc)
        assert "1 Snowflake EXTERNAL VOLUME(s)" in message
        assert "exposes[orders]: location.catalog lakekeeper" in message
        assert "the resources stay in Snowflake" in message
        assert "DROP EXTERNAL VOLUME" in message


class TestSpecLookup:
    def test_the_in_tree_clouds(self):
        assert catalog_move_spec(AwsIacPlugin()) is AWS_GLUE_MOVES
        assert catalog_move_spec(SnowflakeIacPlugin()) is SNOWFLAKE_VOLUME_MOVES
        assert catalog_move_spec(object(), "gcp") is None

    def test_a_plugin_s_own_hook_wins(self):
        class _OutOfTree:
            name = "aws"

            def catalog_move_spec(self):
                return SNOWFLAKE_VOLUME_MOVES

        assert catalog_move_spec(_OutOfTree(), "aws") is SNOWFLAKE_VOLUME_MOVES


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

    def _pull(self, monkeypatch, engine, result) -> None:
        monkeypatch.setattr(engine.runner, "tofu_state_pull", lambda *_a, **_k: result)

    def test_an_empty_state_passes_quietly(self, monkeypatch, engine, caplog):
        """A fresh workdir: ``state pull`` prints nothing, so there is nothing
        to release and nothing to warn about."""
        self._listing(monkeypatch, engine, [])
        self._pull(monkeypatch, engine, runner.TofuResult("state-pull", 0, "", ""))
        plugin = _SpyPlugin()

        with caplog.at_level(logging.WARNING, logger="t"):
            engine._guard_catalog_moves(
                plugin,
                _contract(_expose("iceberg", "lakekeeper")),
                "aws",
                "/w",
                {},
                logging.getLogger("t"),
            )

        assert plugin.emits == 0
        assert caplog.records == []

    @pytest.mark.parametrize(
        "pulled, reason",
        [
            (
                runner.TofuResult("state-pull", 1, "", "Error acquiring the state lock"),
                "the state could not be read: `tofu state pull` failed: Error acquiring",
            ),
            (
                runner.TofuResult(
                    "state-pull",
                    0,
                    '{"lineage": "l", "serial": 3, "resources": [{"type": "aws_s3_bucket"}]}',
                    "",
                ),
                "listed nothing, but the state holds 1 resource(s)",
            ),
        ],
        ids=["unreadable", "listing-lost"],
    )
    def test_a_failed_state_listing_is_a_warning(self, monkeypatch, engine, caplog, pulled, reason):
        """SEC-707-6: ``tofu state list`` answers [] when it fails, which used
        to skip the guard as if the workdir were fresh."""
        self._listing(monkeypatch, engine, [])
        self._pull(monkeypatch, engine, pulled)

        with caplog.at_level(logging.WARNING, logger="t"):
            engine._guard_catalog_moves(
                _SpyPlugin(),
                _contract(_expose("iceberg", "lakekeeper")),
                "aws",
                "/w",
                {},
                logging.getLogger("t"),
            )

        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        assert "iceberg_catalog_move_probe_skipped" in record.getMessage()
        assert reason in record.getMessage()
        assert "do NOT pass --allow-data-loss" in record.getMessage()

    def test_a_failed_probe_is_a_warning_not_a_failure(self, monkeypatch, engine, caplog):
        """T4 / ARCH-6: the probe stays fail-open (the data-loss gate stands
        behind it), but the operator is told, at WARNING, why it did not run."""

        class _Broken(AwsIacPlugin):
            def emit(self, contract, actions=()):
                raise RuntimeError("probe exploded")

        self._listing(monkeypatch, engine, [DB, ORDERS])
        with caplog.at_level(logging.WARNING, logger="t"):
            engine._guard_catalog_moves(
                _Broken(),
                _contract(_expose("iceberg", "lakekeeper")),
                "aws",
                "/w",
                {},
                logging.getLogger("t"),
            )

        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "iceberg_catalog_move_probe_skipped" in message
        assert "the probe failed: RuntimeError: probe exploded" in message
        assert '"expose": "orders"' in message and '"catalog": "lakekeeper"' in message
        assert "do NOT pass --allow-data-loss" in message

    def test_snowflake_translates_to_the_same_cli_error(self, monkeypatch, engine):
        from fluid_build.cli._common import CLIError

        self._listing(monkeypatch, engine, [SF_TABLE, VOLUME])
        with pytest.raises(CLIError) as excinfo:
            engine._guard_catalog_moves(
                SnowflakeIacPlugin(),
                _contract(_sf_expose("lakekeeper")),
                "snowflake",
                "/w",
                {},
                logging.getLogger("t"),
            )

        assert excinfo.value.event == "iceberg_catalog_move_blocked"
        assert excinfo.value.context["remediation"] == [f"tofu -chdir=/w state rm {VOLUME}"]
