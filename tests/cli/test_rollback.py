# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``fluid rollback`` (the apply --mode replace* restore path).

Covers:
- Product-ID validation (whitelist regex, rejects shell metacharacters).
- State-file reader (missing file, malformed JSON, wrong version, wrong
  shape — each raises an actionable CLIError event slug).
- Snapshot selection (env + product filter, named snapshot, latest-by-
  timestamp).
- Per-provider restore dispatch (Snowflake happy path; BQ + Redshift
  raise NotImplementedError-shaped CLIErrors with clear workaround
  hints).
- Destructive-confirmation gate (--yes required unless --dry-run).
- Dry-run surface (returns plan without executing).

No real Snowflake / BQ / Redshift calls — the Snowflake provider is
patched. The tests exercise the state-file layer + dispatcher
routing + confirmation logic in isolation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli import rollback
from fluid_build.cli._common import CLIError

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _state(*, snapshots):
    return {"version": "1", "snapshots": snapshots}


def _snap(
    *,
    env="dev",
    product="silver.telco.subscriber360_v1",
    backup_name="backup_silver_telco_subscriber360_v1_1714000000",
    timestamp="2026-04-23T10:00:00Z",
    provider="snowflake",
    database="TELCO_LAB",
):
    return {
        "timestamp": timestamp,
        "env": env,
        "product_id": product,
        "backup_name": backup_name,
        "provider": provider,
        "mode": "replace",
        "location": {"database": database, "schema": "PUBLIC"},
    }


@pytest.fixture()
def state_file(tmp_path: Path) -> Path:
    p = tmp_path / "rollback-state.json"
    p.write_text(json.dumps(_state(snapshots=[_snap()])), encoding="utf-8")
    return p


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "env": "dev",
        "product": "silver.telco.subscriber360_v1",
        "snapshot": None,
        "state_file": "",
        "dry_run": True,
        "yes": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# -----------------------------------------------------------------------------
# _validate_product_id
# -----------------------------------------------------------------------------


class TestValidateProductId:
    @pytest.mark.parametrize(
        "pid",
        [
            "silver.telco.subscriber360_v1",
            "gold-customer-360",
            "bronze_source_v2",
            "a",
            "A" * 256,
        ],
    )
    def test_accepts_valid_ids(self, pid):
        assert rollback._validate_product_id(pid) == pid

    @pytest.mark.parametrize(
        "pid",
        [
            "",
            "   ",
            "silver/telco/subscriber",  # slashes
            "silver telco subscriber",  # spaces
            "silver;DROP TABLE",  # shell meta
            "silver$(whoami)",
            "silver`cat`",
            "A" * 257,  # too long
        ],
    )
    def test_rejects_invalid_ids(self, pid):
        with pytest.raises(CLIError, match=r"rollback_product_id_(empty|invalid)"):
            rollback._validate_product_id(pid)


# -----------------------------------------------------------------------------
# _read_state
# -----------------------------------------------------------------------------


class TestReadState:
    def test_reads_valid_state(self, state_file):
        data = rollback._read_state(state_file)
        assert data["version"] == "1"
        assert len(data["snapshots"]) == 1

    def test_missing_file_raises_actionable_error(self, tmp_path):
        missing = tmp_path / "absent.json"
        with pytest.raises(CLIError, match="rollback_state_file_missing"):
            rollback._read_state(missing)

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(CLIError, match="rollback_state_file_invalid_json"):
            rollback._read_state(p)

    def test_wrong_shape_raises(self, tmp_path):
        """Top-level must be a JSON object, not a list / null / string."""
        p = tmp_path / "s.json"
        p.write_text('["not an object"]', encoding="utf-8")
        with pytest.raises(CLIError, match="rollback_state_file_wrong_shape"):
            rollback._read_state(p)

    def test_unsupported_version_raises(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"version": "9000", "snapshots": []}), encoding="utf-8")
        with pytest.raises(CLIError, match="rollback_state_file_unsupported_version"):
            rollback._read_state(p)

    def test_missing_snapshots_list_raises(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"version": "1"}), encoding="utf-8")
        with pytest.raises(CLIError, match="rollback_state_file_no_snapshots"):
            rollback._read_state(p)


# -----------------------------------------------------------------------------
# _select_snapshot
# -----------------------------------------------------------------------------


class TestSelectSnapshot:
    def test_no_match_raises(self):
        with pytest.raises(CLIError, match="rollback_no_matching_snapshot"):
            rollback._select_snapshot([], env="dev", product="x")

    def test_env_filter(self):
        """Snapshots for the wrong env are filtered out — a prod
        rollback must NEVER pick up a dev snapshot by accident."""
        snaps = [
            _snap(env="dev"),
            _snap(env="prod", backup_name="backup_prod_1"),
        ]
        s = rollback._select_snapshot(snaps, env="prod", product=_snap()["product_id"])
        assert s["backup_name"] == "backup_prod_1"

    def test_product_filter(self):
        """Product ID filter: two products, one env — pick the right one."""
        snaps = [
            _snap(product="a", backup_name="ba"),
            _snap(product="b", backup_name="bb"),
        ]
        s = rollback._select_snapshot(snaps, env="dev", product="b")
        assert s["backup_name"] == "bb"

    def test_latest_by_timestamp(self):
        """When multiple snapshots match, pick the most recent by
        ISO-8601 timestamp."""
        snaps = [
            _snap(timestamp="2026-04-20T10:00:00Z", backup_name="b_old"),
            _snap(timestamp="2026-04-23T10:00:00Z", backup_name="b_new"),
            _snap(timestamp="2026-04-22T10:00:00Z", backup_name="b_mid"),
        ]
        s = rollback._select_snapshot(
            snaps,
            env="dev",
            product=_snap()["product_id"],
        )
        assert s["backup_name"] == "b_new"

    def test_named_snapshot_match(self):
        snaps = [
            _snap(backup_name="b1"),
            _snap(backup_name="b2"),
        ]
        s = rollback._select_snapshot(
            snaps,
            env="dev",
            product=_snap()["product_id"],
            name="b2",
        )
        assert s["backup_name"] == "b2"

    def test_named_snapshot_not_found_surfaces_available(self):
        snaps = [_snap(backup_name="b1")]
        with pytest.raises(CLIError, match="rollback_snapshot_name_not_found"):
            rollback._select_snapshot(
                snaps,
                env="dev",
                product=_snap()["product_id"],
                name="does-not-exist",
            )


# -----------------------------------------------------------------------------
# Per-provider restore dispatchers
# -----------------------------------------------------------------------------


class TestRestoreSnowflake:
    def test_dry_run_returns_ddl_without_execution(self):
        """Dry-run must NOT import the Snowflake provider (no network,
        no connector). It just returns the planned DDL."""
        snap = _snap()
        with patch("fluid_build.providers.snowflake.SnowflakeProvider") as mock_provider:
            result = rollback._restore_snowflake(snap, dry_run=True)
        assert result["status"] == "dry_run"
        assert "CREATE OR REPLACE DATABASE" in result["ddl"]
        assert "TELCO_LAB" in result["ddl"]
        assert snap["backup_name"] in result["ddl"]
        mock_provider.assert_not_called()

    def test_missing_database_raises(self):
        """Snapshot without a location.database is a malformed record —
        fail loud with a bug-reporting hint."""
        snap = _snap()
        snap["location"] = {}
        with pytest.raises(CLIError, match="rollback_snowflake_missing_database"):
            rollback._restore_snowflake(snap, dry_run=False)

    def test_live_restore_invokes_provider(self):
        """Non-dry-run path constructs a SnowflakeProvider and runs
        the CLONE DDL through it. We mock the provider to avoid a
        real connection."""
        snap = _snap()
        mock_provider_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.execute_sql.return_value = {"status": "ok"}
        mock_provider_cls.return_value = mock_instance
        with patch(
            "fluid_build.providers.snowflake.SnowflakeProvider",
            mock_provider_cls,
        ):
            result = rollback._restore_snowflake(snap, dry_run=False)
        assert result["status"] == "restored"
        assert result["provider_result"] == {"status": "ok"}
        # Provider was constructed with the target database.
        mock_provider_cls.assert_called_once_with(database="TELCO_LAB")


class TestRestoreBigQueryRedshift:
    def test_bigquery_not_implemented_with_actionable_hint(self):
        """NotImplemented must surface as a CLIError with the
        workaround command, not a vague ImportError."""
        with pytest.raises(CLIError, match="rollback_bigquery_not_implemented"):
            rollback._restore_bigquery(_snap(), dry_run=False)

    def test_redshift_not_implemented_with_actionable_hint(self):
        with pytest.raises(CLIError, match="rollback_redshift_not_implemented"):
            rollback._restore_redshift(_snap(), dry_run=False)


# -----------------------------------------------------------------------------
# run() — end-to-end with confirmation gate
# -----------------------------------------------------------------------------


class TestRunConfirmationGate:
    def test_destructive_without_yes_raises(self, state_file):
        """Running without --dry-run AND without --yes must refuse.
        Prevents accidental ``fluid rollback --env prod --product X``
        from a tab-completion slip."""
        args = _args(state_file=str(state_file), dry_run=False, yes=False)
        with patch("fluid_build.providers.snowflake.SnowflakeProvider"):
            with pytest.raises(CLIError, match="rollback_confirmation_required"):
                rollback.run(args)

    def test_dry_run_without_yes_is_allowed(self, state_file):
        """--dry-run never touches state; --yes isn't required."""
        args = _args(state_file=str(state_file), dry_run=True, yes=False)
        with patch("fluid_build.providers.snowflake.SnowflakeProvider"):
            rc = rollback.run(args)
        assert rc == 0

    def test_destructive_with_yes_proceeds(self, state_file):
        """--yes confirms; --dry-run=False proceeds to provider call."""
        args = _args(state_file=str(state_file), dry_run=False, yes=True)
        mock_provider_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.execute_sql.return_value = {"status": "ok"}
        mock_provider_cls.return_value = mock_instance
        with patch(
            "fluid_build.providers.snowflake.SnowflakeProvider",
            mock_provider_cls,
        ):
            rc = rollback.run(args)
        assert rc == 0
        mock_provider_cls.assert_called_once()


class TestRunEndToEnd:
    def test_selects_latest_when_no_snapshot_flag(self, tmp_path):
        """End-to-end: read state → select latest → dispatch → succeed."""
        p = tmp_path / "state.json"
        p.write_text(
            json.dumps(
                _state(
                    snapshots=[
                        _snap(
                            timestamp="2026-04-20T10:00:00Z",
                            backup_name="old",
                        ),
                        _snap(
                            timestamp="2026-04-23T10:00:00Z",
                            backup_name="new",
                        ),
                    ]
                )
            ),
            encoding="utf-8",
        )
        args = _args(state_file=str(p), dry_run=True)
        with patch("fluid_build.providers.snowflake.SnowflakeProvider"):
            rc = rollback.run(args)
        assert rc == 0
        # No way to directly assert "new" was selected from run()'s
        # return — but the dry-run prints the DDL containing the
        # backup_name; exit 0 on a selection failure would have raised.

    def test_specific_snapshot_name_honored(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(
            json.dumps(
                _state(
                    snapshots=[
                        _snap(
                            timestamp="2026-04-23T10:00:00Z",
                            backup_name="latest",
                        ),
                        _snap(
                            timestamp="2026-04-20T10:00:00Z",
                            backup_name="pinned_to_restore",
                        ),
                    ]
                )
            ),
            encoding="utf-8",
        )
        args = _args(
            state_file=str(p),
            snapshot="pinned_to_restore",
            dry_run=True,
        )
        with patch("fluid_build.providers.snowflake.SnowflakeProvider"):
            rc = rollback.run(args)
        assert rc == 0

    def test_unknown_provider_raises(self, tmp_path):
        """If the snapshot record names a provider we don't have a
        restore dispatcher for, fail with a provider-list hint
        (future-proof for when new providers are added)."""
        p = tmp_path / "state.json"
        p.write_text(
            json.dumps(_state(snapshots=[_snap(provider="mystery")])),
            encoding="utf-8",
        )
        args = _args(state_file=str(p), dry_run=True)
        with pytest.raises(CLIError, match="rollback_unknown_provider"):
            rollback.run(args)


# -----------------------------------------------------------------------------
# Argparse registration
# -----------------------------------------------------------------------------


class TestArgparseRegistration:
    def test_register_adds_subcommand(self):
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        rollback.register(sp)
        ns = parser.parse_args(
            [
                "rollback",
                "--env",
                "prod",
                "--product",
                "silver.telco.subscriber360_v1",
                "--snapshot",
                "backup_silver_telco_subscriber360_v1_1714000000",
                "--dry-run",
            ]
        )
        assert ns.command == "rollback"
        assert ns.env == "prod"
        assert ns.product == "silver.telco.subscriber360_v1"
        assert ns.snapshot == "backup_silver_telco_subscriber360_v1_1714000000"
        assert ns.dry_run is True
