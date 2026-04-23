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
        # ``--list`` defaults to off so existing restore tests pick
        # up a sensible value even though they predate the flag.
        "list_snapshots": False,
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


class TestRestoreSnowflakeSqlInjection:
    """SECURITY regression guard — SQL injection on the Snowflake
    restore DDL.

    Pre-fix: ``_restore_snowflake`` built the CLONE DDL via an f-string
    with unvalidated state-file fields. A crafted ``backup_name`` like
    ``"ok; DROP DATABASE production; CREATE DATABASE pwn CLONE ok"``
    would smuggle arbitrary DDL via ``executescript`` (which splits on
    ``;``). The rollback docstring explicitly invites committing the
    state file to the product repo, which is an attacker-authorable
    surface via PR.

    Fix: route ``database`` + ``backup_name`` through
    ``fluid_build.providers._sql_safety.validate_ident`` BEFORE the
    f-string. Regex is ``^[A-Za-z_][A-Za-z0-9_]*$`` — rejects every
    semicolon / whitespace / quote / backtick / wildcard payload at
    the validation layer. Legitimate identifiers
    (``backup_silver_telco_subscriber360_v1_1714000000``,
    ``TELCO_LAB``) match cleanly, so zero false-positive cost.

    These tests lock the fix in. Any regression that removes
    validate_ident from the f-string construction path re-introduces
    the SQL injection.
    """

    @pytest.mark.parametrize(
        "malicious_value",
        [
            # Classic semicolon smuggling — closes one DDL, opens
            # another. Would destroy a named production database.
            "ok; DROP DATABASE production",
            # Multi-statement chain; second statement is arbitrary DDL
            # the attacker controls (CREATE PROCEDURE, CREATE USER,
            # GRANT, etc. are all reachable).
            "ok; CREATE DATABASE pwn CLONE ok",
            # Whitespace / tab variants — separators inside SQL work
            # equally for statement termination.
            "ok;\tDROP DATABASE production;",
            # Comment-based obfuscation — classic SQLi pattern.
            "ok/**/;/**/DROP/**/DATABASE/**/production",
            # Quote-based — could escape identifier context in some
            # dialects or embed a string literal.
            "ok' OR 1=1 --",
            # Backticks — harmless on Snowflake but universally rejected
            # by the ident whitelist (Snowflake uses double quotes; a
            # backtick value is always suspicious).
            "ok`whoami`",
            # Spaces in the middle — unambiguous red flag since
            # legitimate Snowflake identifiers don't have spaces.
            "ok DROP DATABASE production",
            # Whitespace-only — not legal SQL identifier; would have
            # expanded to ``CLONE ;`` with the original code.
            # (Pure empty string is covered separately by
            # ``test_missing_backup_name_raises_distinct_error`` — it
            # hits a different error slug so the operator can
            # distinguish "malformed state file" from "state file
            # contains injection attempt".)
            " ",
            # Starting with a non-alpha char — not a valid SQL identifier
            # in any dialect.
            "1abc",
            "_abc",  # valid start char (_), but let's confirm — actually _ IS allowed
        ],
    )
    def test_backup_name_with_metacharacters_refused(self, malicious_value):
        """A state-file backup_name containing SQL metacharacters must
        raise CLIError BEFORE any DDL is constructed or submitted.
        This is the primary SQL-injection defence."""
        # Skip the one entry that's a valid identifier (underscore
        # start is allowed by the regex).
        if malicious_value == "_abc":
            snap = _snap(backup_name="_abc")
            # Should NOT raise — _abc is a valid ident.
            rollback._restore_snowflake(snap, dry_run=True)
            return

        snap = _snap(backup_name=malicious_value)
        with pytest.raises(CLIError) as exc_info:
            rollback._restore_snowflake(snap, dry_run=True)
        # Must raise specifically the sql-injection gate error slug —
        # NOT a downstream KeyError from the malformed value reaching
        # the executor. If this test catches a different error, the
        # validate_ident check moved or got removed.
        assert "rollback_snowflake_invalid_identifier" in str(exc_info.value), (
            f"expected validate_ident to refuse {malicious_value!r} at "
            "the CLI boundary; got a different error → the injection "
            "gate regressed."
        )

    @pytest.mark.parametrize(
        "malicious_db",
        [
            "target; DROP DATABASE production",
            "target' OR 1=1 --",
            "target`whoami`",
            "target DROP DATABASE production",
        ],
    )
    def test_database_with_metacharacters_refused(self, malicious_db):
        """Same gate must cover the ``database`` field — both flow
        into the same f-string, both are attacker-authorable."""
        snap = _snap()
        snap["location"]["database"] = malicious_db
        with pytest.raises(CLIError) as exc_info:
            rollback._restore_snowflake(snap, dry_run=True)
        assert "rollback_snowflake_invalid_identifier" in str(exc_info.value)

    def test_legitimate_backup_name_accepted(self):
        """Positive control: a realistic backup name (from the
        docstring's own example) must pass the validator. If this
        fails, the gate is over-restrictive and legitimate rollbacks
        break."""
        snap = _snap(backup_name="backup_silver_telco_subscriber360_v1_1714000000")
        # Should not raise.
        result = rollback._restore_snowflake(snap, dry_run=True)
        assert result["status"] == "dry_run"
        assert "backup_silver_telco_subscriber360_v1_1714000000" in result["ddl"]

    def test_legitimate_database_name_accepted(self):
        """Positive control: ``TELCO_LAB`` (our lab DB) must pass."""
        snap = _snap(database="TELCO_LAB")
        result = rollback._restore_snowflake(snap, dry_run=True)
        assert result["status"] == "dry_run"
        assert "TELCO_LAB" in result["ddl"]

    def test_injection_gate_fires_before_provider_construction(self):
        """The validate_ident check runs BEFORE the
        ``SnowflakeProvider(...)`` instantiation. Otherwise a crafted
        value could leak to the connection-pool construction path
        first (not a vuln today, but defence-in-depth ordering)."""
        snap = _snap(backup_name="ok; DROP DATABASE production")
        mock_provider_cls = MagicMock()
        with patch(
            "fluid_build.providers.snowflake.SnowflakeProvider",
            mock_provider_cls,
        ):
            with pytest.raises(CLIError, match="rollback_snowflake_invalid"):
                rollback._restore_snowflake(snap, dry_run=False)
        # Provider NEVER instantiated — the gate tripped first.
        mock_provider_cls.assert_not_called()

    def test_missing_backup_name_raises_distinct_error(self):
        """Empty/missing backup_name must raise the missing-backup-name
        slug (not the invalid-identifier slug). This lets operators
        distinguish "state file is malformed" from "state file
        contains injection attempt" — different failure modes warrant
        different remediation."""
        snap = _snap()
        del snap["backup_name"]
        with pytest.raises(CLIError, match="rollback_snowflake_missing_backup_name"):
            rollback._restore_snowflake(snap, dry_run=True)


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


# ---------------------------------------------------------------------------
# --list discovery (G6)
# ---------------------------------------------------------------------------


class TestListSnapshots:
    """``fluid rollback --list`` mirrors ``terraform state list`` / ``git
    reflog`` — read-only discovery of available snapshots before running
    a destructive restore. These tests pin the behaviour operators
    depend on:

    * no --env / --product required (differs from restore path)
    * works when state file doesn't exist (prints helpful message,
      returns exit 0 — not a hard error)
    * filters on --env + --product when provided
    * ordering is newest-first (most likely restore target on top)
    """

    def test_list_missing_state_file_returns_0(self, tmp_path):
        """A fresh workspace with no prior ``apply --mode replace`` has
        no state file. ``--list`` must not fail — it's a discovery
        verb, and "nothing to list" is a valid outcome."""
        args = _args(
            list_snapshots=True,
            state_file=str(tmp_path / "does-not-exist.json"),
            env=None,
            product=None,
        )
        result = rollback.run(args, None)
        assert result == 0

    def test_list_without_filters_shows_all(self, tmp_path, capsys):
        """Without --env/--product, --list prints every recorded
        snapshot. Uses the full state file so the test exercises the
        table-rendering path end-to-end."""
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                _state(
                    snapshots=[
                        _snap(env="dev", backup_name="backup_v1_1001"),
                        _snap(env="prod", backup_name="backup_v1_1002"),
                    ]
                )
            ),
            encoding="utf-8",
        )
        args = _args(
            list_snapshots=True,
            state_file=str(state_path),
            env=None,
            product=None,
        )
        result = rollback.run(args, None)
        assert result == 0
        out = capsys.readouterr().out
        assert "backup_v1_1001" in out
        assert "backup_v1_1002" in out
        assert "2 snapshot(s)" in out

    def test_list_env_filter_narrows(self, tmp_path, capsys):
        """``--list --env prod`` shows only prod snapshots. Essential
        for operators running across multi-env deployments who want
        to scope discovery before restore."""
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                _state(
                    snapshots=[
                        _snap(env="dev", backup_name="backup_dev_1"),
                        _snap(env="prod", backup_name="backup_prod_1"),
                    ]
                )
            ),
            encoding="utf-8",
        )
        args = _args(list_snapshots=True, state_file=str(state_path), env="prod", product=None)
        rollback.run(args, None)
        out = capsys.readouterr().out
        assert "backup_prod_1" in out
        assert "backup_dev_1" not in out

    def test_list_product_filter_narrows(self, tmp_path, capsys):
        """``--list --product X`` shows only snapshots matching that
        product ID. Required for monorepos with many products in
        the same state file."""
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                _state(
                    snapshots=[
                        _snap(product="silver.telco.a", backup_name="backup_a"),
                        _snap(product="silver.telco.b", backup_name="backup_b"),
                    ]
                )
            ),
            encoding="utf-8",
        )
        args = _args(
            list_snapshots=True,
            state_file=str(state_path),
            env=None,
            product="silver.telco.a",
        )
        rollback.run(args, None)
        out = capsys.readouterr().out
        assert "backup_a" in out
        assert "backup_b" not in out

    def test_list_empty_result_prints_actionable_message(self, tmp_path, capsys):
        """Filters with no matches print a helpful message (not silent
        exit). Gives the operator a hint that filter values may not
        match recorded state — the most common "no results" cause."""
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(_state(snapshots=[_snap(env="dev")])),
            encoding="utf-8",
        )
        args = _args(
            list_snapshots=True,
            state_file=str(state_path),
            env="prod",  # filter doesn't match any snapshot
            product=None,
        )
        result = rollback.run(args, None)
        assert result == 0
        out = capsys.readouterr().out
        assert "no snapshots found" in out
        assert "prod" in out  # the filter value is echoed back

    def test_list_is_read_only_no_restore_invoked(self, tmp_path, monkeypatch):
        """Regression guard: ``--list`` must NOT dispatch to the
        provider restore helpers. If this fires, it means the ``run``
        function fell through to the restore code path — a bug where
        a read-only discovery could trigger a destructive restore.
        """
        called = []

        def _tripwire(snapshot, *, dry_run):
            called.append(snapshot)
            return {}

        monkeypatch.setattr(rollback, "_restore_snowflake", _tripwire)
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(_state(snapshots=[_snap()])), encoding="utf-8")
        args = _args(
            list_snapshots=True,
            state_file=str(state_path),
            env="dev",
            product="silver.telco.subscriber360_v1",
        )
        rollback.run(args, None)
        assert called == [], "rollback --list must not invoke the provider restore path"


class TestRestoreModeRequiresEnvAndProduct:
    """The non-list path enforces ``--env`` and ``--product`` in the
    body of ``run()`` (rather than via ``required=True`` on argparse)
    so ``--list`` can run without them. These tests pin that the
    restore path still fails loud if either is missing."""

    def test_missing_env_raises_clierror(self, state_file):
        args = _args(env=None, state_file=str(state_file), dry_run=True, yes=True)
        with pytest.raises(CLIError) as exc:
            rollback.run(args, None)
        assert exc.value.event == "rollback_env_required"

    def test_missing_product_raises_clierror(self, state_file):
        args = _args(product=None, state_file=str(state_file), dry_run=True, yes=True)
        with pytest.raises(CLIError) as exc:
            rollback.run(args, None)
        assert exc.value.event == "rollback_product_required"
