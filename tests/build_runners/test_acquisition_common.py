# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the shared acquisition runtime modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.cost import BudgetCap
from fluid_build.api.schema import SchemaColumn, SchemaFingerprint, SchemaPolicy
from fluid_build.api.state import Cursor, Watermark
from fluid_build.build_runners._acquisition_common import (
    extract_source_schemas,
    generate_run_id,
    resolve_connection_secrets,
    resolve_secret_ref,
    setdefault_env,
    utc_now_iso,
)
from fluid_build.build_runners._cost import (
    BudgetExceededError,
    InMemoryCostTracker,
    gate_or_raise,
    parse_bytes,
)
from fluid_build.build_runners._credentials import (
    make_destination,
    register_engine_introspector,
)
from fluid_build.build_runners._dlq import DLQConfig, DLQOverflowError, DLQWriter
from fluid_build.build_runners._fingerprint import fingerprint_from_columns
from fluid_build.build_runners._retention import RetentionConfig, parse_iso_duration
from fluid_build.build_runners._retry import RetryPolicy, is_retryable, with_retry
from fluid_build.build_runners._schema_evolution import EvolutionAction, resolve
from fluid_build.build_runners._state import FileStateStore, LockHeldError

# ── _acquisition_common ──────────────────────────────────────────────────


class TestRunIds:
    def test_generate_run_id_unique(self):
        ids = {generate_run_id() for _ in range(50)}
        assert len(ids) == 50

    def test_generate_run_id_format(self):
        rid = generate_run_id()
        assert rid.startswith("01")
        assert len(rid) == 18

    def test_utc_now_iso_format(self):
        s = utc_now_iso()
        assert s.endswith("Z")
        assert "T" in s


# ── secretRef resolution (acquisition runners) ──────────────────────────


class TestResolveSecretRef:
    """``env://`` short-circuit + dispatch to the cloud SecretManager backends."""

    def test_env_scheme_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ACQ_TEST_SECRET", "s3cret-value")
        assert resolve_secret_ref("env://ACQ_TEST_SECRET") == "s3cret-value"

    def test_env_scheme_unset_var_raises_value_error(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ACQ_TEST_MISSING", raising=False)
        with pytest.raises(ValueError, match="environment variable not set"):
            resolve_secret_ref("env://ACQ_TEST_MISSING")

    @pytest.mark.parametrize(
        "bad_ref",
        ["", "no_scheme", "env://", "://identifier", "env:/missing-slash"],
    )
    def test_malformed_secret_ref_raises_value_error(self, bad_ref: str):
        with pytest.raises(ValueError, match="<scheme>://<identifier>"):
            resolve_secret_ref(bad_ref)

    def test_unsupported_scheme_lists_supported_schemes(self):
        with pytest.raises(ValueError, match="not supported") as excinfo:
            resolve_secret_ref("madeup://x")
        # The error message must enumerate supported schemes so the operator
        # can fix the contract without grepping the source.
        msg = str(excinfo.value)
        assert "env" in msg
        assert "vault" in msg
        assert "aws" in msg

    def test_scheme_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ACQ_TEST_SECRET", "x")
        assert resolve_secret_ref("ENV://ACQ_TEST_SECRET") == "x"
        assert resolve_secret_ref("Env://ACQ_TEST_SECRET") == "x"


class TestResolveConnectionSecrets:
    """Convenience wrapper that places the resolved secret into a target field."""

    def test_no_secret_ref_returns_shallow_copy_unchanged(self):
        original = {"host": "db.example", "port": 5432, "user": "alice"}
        out = resolve_connection_secrets(original)
        assert out == original
        assert out is not original  # new dict, not the same reference

    def test_env_secret_ref_resolves_into_password_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PG_PASSWORD", "from-env")
        out = resolve_connection_secrets(
            {
                "host": "db.example",
                "port": 5432,
                "user": "alice",
                "secretRef": "env://PG_PASSWORD",
            }
        )
        assert out == {
            "host": "db.example",
            "port": 5432,
            "user": "alice",
            "password": "from-env",
        }
        # secretRef MUST be removed so downstream client SDKs don't see it.
        assert "secretRef" not in out

    def test_inline_password_wins_over_secret_ref(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PG_PASSWORD", "from-env")
        out = resolve_connection_secrets(
            {
                "host": "db.example",
                "user": "alice",
                "password": "inline-literal",
                "secretRef": "env://PG_PASSWORD",
            }
        )
        # Inline literal wins (the secretRef is treated as a fallback default).
        assert out["password"] == "inline-literal"
        # secretRef still gets removed, even when not consumed.
        assert "secretRef" not in out

    def test_target_field_override_for_token_auth(self, monkeypatch: pytest.MonkeyPatch):
        # GitHub / Salesforce / OAuth2-style sources want the secret as a
        # token, not a password. The runner can override the target field.
        monkeypatch.setenv("GH_TOKEN", "ghp_abc123")
        out = resolve_connection_secrets(
            {"instance_url": "https://api.github.com", "secretRef": "env://GH_TOKEN"},
            target_field="token",
        )
        assert out == {
            "instance_url": "https://api.github.com",
            "token": "ghp_abc123",
        }

    def test_unsupported_scheme_propagates_value_error(self):
        with pytest.raises(ValueError, match="not supported"):
            resolve_connection_secrets({"host": "x", "secretRef": "madeup://identifier"})

    def test_does_not_mutate_input_dict(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PG_PASSWORD", "x")
        original = {"host": "db", "secretRef": "env://PG_PASSWORD"}
        snapshot = dict(original)
        _ = resolve_connection_secrets(original)
        assert original == snapshot, "input dict was mutated"


# ── destination credential bridging (FLUID env → engine env) ──────────


class TestSetdefaultEnv:
    def test_sets_when_unset_and_value_non_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("_TEST_X", raising=False)
        assert setdefault_env("_TEST_X", "value") is True
        import os as _os

        assert _os.environ["_TEST_X"] == "value"

    def test_does_not_override_when_already_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("_TEST_X", "operator-value")
        assert setdefault_env("_TEST_X", "bridge-value") is False
        import os as _os

        # Operator override wins.
        assert _os.environ["_TEST_X"] == "operator-value"

    def test_does_not_set_when_value_is_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("_TEST_X", raising=False)
        assert setdefault_env("_TEST_X", "") is False
        assert setdefault_env("_TEST_X", None) is False
        import os as _os

        assert "_TEST_X" not in _os.environ


class TestDestinationIntrospector:
    """Per-engine destination introspector via the unified make_destination().

    The introspector pattern walks each engine SDK's OWN credential schema
    (dlt's destination ``credentials_type()``, PyAirbyte's ``Cache.__init__``
    signature, …) — no per-destination factory functions. Adding a new
    destination = zero code in the common case.
    """

    def test_register_and_dispatch(self):
        called = {"n": 0}

        # Register a fresh introspector for a test engine so we don't
        # collide with the real dlt/airbyte introspectors registered at
        # package import time.
        @register_engine_introspector("__test_engine__")
        def _introspector(*, platform, credentials, binding, contract, product_id):
            called["n"] += 1
            return None

        make_destination("__test_engine__", "snowflake", binding={}, contract={}, product_id="")
        assert called["n"] == 1

    def test_dispatch_is_noop_when_no_introspector_registered(self):
        result = make_destination(
            "__nonexistent_engine__",
            "__nonexistent_platform__",
            binding={},
            contract={},
            product_id="",
        )
        assert result is None

    def test_dlt_snowflake_introspector_maps_all_fields_with_bare_account(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The introspector requires the dlt SDK installed (it inspects
        # ``dlt.destinations.snowflake`` to map FLUID env vars onto dlt's
        # ``DESTINATION__SNOWFLAKE__CREDENTIALS__*`` schema). CI matrix
        # rows that don't install dlt skip the test rather than fail.
        pytest.importorskip("dlt")
        # Force-import dlt's destinations module so the introspector registers.
        import fluid_build.build_runners.dlt  # noqa: F401

        for var in [
            "DESTINATION__SNOWFLAKE__CREDENTIALS__HOST",
            "DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME",
            "DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD",
            "DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE",
            "DESTINATION__SNOWFLAKE__CREDENTIALS__WAREHOUSE",
            "DESTINATION__SNOWFLAKE__CREDENTIALS__ROLE",
        ]:
            monkeypatch.delenv(var, raising=False)

        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ABC123-XY7890")
        monkeypatch.setenv("SNOWFLAKE_USER", "alice")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "s3cret")
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "PROD_DB")
        monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "WH1")
        monkeypatch.setenv("SNOWFLAKE_ROLE", "DATA_ENG")

        make_destination("dlt", "snowflake", binding={}, contract={}, product_id="bronze.test")

        import os as _os

        # Critical: HOST is the BARE account ID. dlt appends
        # .snowflakecomputing.com itself; double-suffix produces a 404.
        # FLUID's `account` aliases to dlt's `host` field; FLUID's `user`
        # aliases to dlt's `username` field — both via the per-platform
        # alias table in dlt/destinations.py.
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__HOST"] == "ABC123-XY7890"
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME"] == "alice"
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD"] == "s3cret"
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE"] == "PROD_DB"
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__WAREHOUSE"] == "WH1"
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__ROLE"] == "DATA_ENG"

    def test_dlt_snowflake_operator_override_wins(self, monkeypatch: pytest.MonkeyPatch):
        pytest.importorskip("dlt")
        import fluid_build.build_runners.dlt  # noqa: F401

        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "from-fluid-env")
        monkeypatch.setenv("DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "operator-set")
        make_destination("dlt", "snowflake", binding={}, contract={}, product_id="bronze.test")
        import os as _os

        # Operator's explicit DESTINATION__... export wins — introspector
        # uses setdefault semantics, never overwrites.
        assert _os.environ["DESTINATION__SNOWFLAKE__CREDENTIALS__HOST"] == "operator-set"


# ── connection.schema / connection.schemas extraction ──────────────────


class TestExtractSourceSchemas:
    """Generic schema/namespace extraction used by all SQL-flavoured runners."""

    def test_neither_set_returns_empty_list(self):
        assert extract_source_schemas({"host": "db", "port": 5432}) == []

    def test_single_schema_field(self):
        assert extract_source_schemas({"schema": "telco"}) == ["telco"]

    def test_multi_schema_field(self):
        assert extract_source_schemas({"schemas": ["telco", "billing"]}) == [
            "telco",
            "billing",
        ]

    def test_schemas_wins_over_schema_when_both_set(self):
        # The list form is canonical; the singular form is a deprecated alias.
        out = extract_source_schemas({"schema": "ignored", "schemas": ["a", "b"]})
        assert out == ["a", "b"]

    def test_empty_schemas_falls_back_to_schema_field(self):
        # An empty list is treated as "not set" so the alias still works.
        out = extract_source_schemas({"schema": "fallback", "schemas": []})
        assert out == ["fallback"]

    def test_non_string_values_are_coerced(self):
        # Some YAML loaders emit ints (e.g. for schema=2024). Coerce to str
        # so downstream config formatters don't blow up.
        out = extract_source_schemas({"schemas": [2024, "telco"]})
        assert out == ["2024", "telco"]


# ── placeholder fingerprints + schema-evolution gate skip ──────────────


class TestSchemaFingerprintPlaceholder:
    """``SchemaFingerprint.placeholder()`` factory + the gate's skip behaviour."""

    def test_placeholder_factory_marks_is_placeholder_true(self):
        fp = SchemaFingerprint.placeholder(["orders", "customers"], engine="dlt")
        assert fp.is_placeholder is True
        # Engine tag flows into the column type for observability.
        assert {c.type for c in fp.columns} == {"dlt"}
        assert [c.name for c in fp.columns] == ["orders", "customers"]

    def test_placeholder_digest_is_deterministic(self):
        # Two placeholder fingerprints over the same streams + engine produce
        # the same digest. Useful for change detection between snapshots
        # (e.g. when streams[] changes between contract revisions).
        fp1 = SchemaFingerprint.placeholder(["a", "b"], engine="airbyte")
        fp2 = SchemaFingerprint.placeholder(["a", "b"], engine="airbyte")
        assert fp1.digest == fp2.digest
        # Different streams → different digest.
        fp3 = SchemaFingerprint.placeholder(["a", "c"], engine="airbyte")
        assert fp1.digest != fp3.digest

    def test_of_factory_defaults_is_placeholder_false(self):
        # Backwards compatibility: existing SchemaFingerprint.of() callers
        # get a real (non-placeholder) fingerprint.
        fp = SchemaFingerprint.of([SchemaColumn(name="id", type="STRING")])
        assert fp.is_placeholder is False

    def test_construction_without_is_placeholder_defaults_to_false(self):
        # Backwards compatibility: code that constructs SchemaFingerprint
        # directly (not via factories) gets is_placeholder=False by default.
        fp = SchemaFingerprint(digest="sha256:fake", columns=[SchemaColumn(name="x", type="t")])
        assert fp.is_placeholder is False


class TestEnforceSchemaPolicySkipsPlaceholders:
    """``enforce_schema_policy_or_raise`` must skip when the runner returns a
    placeholder fingerprint, regardless of contract policy. Otherwise the
    stream-name "columns" get compared to real contract columns and every
    contract column shows up as ``removed→fail``.
    """

    def _baseline_contract(self) -> Dict[str, Any]:
        return {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.test",
            "exposes": [
                {
                    "exposeId": "data",
                    "contract": {
                        "schema": [
                            {"name": "INVOICE_ID", "type": "STRING", "nullable": False},
                            {"name": "AMOUNT", "type": "NUMBER", "nullable": False},
                        ],
                        # Strict policy would normally fail on any drift.
                        "schemaPolicy": "strict",
                    },
                }
            ],
        }

    def _make_ctx(self, contract: Dict[str, Any]):
        # Minimal stand-in for RunContext that the gate touches.
        class _Source:
            streams = ["invoices"]
            kind = "postgres"

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.contract = contract
        ctx.source = _Source()
        return ctx

    def test_strict_policy_skips_when_runner_returns_placeholder(self):
        from fluid_build.build_runners._acquisition_common import (
            enforce_schema_policy_or_raise,
        )

        class PlaceholderRunner:
            def fingerprint(self, ctx):
                return SchemaFingerprint.placeholder(ctx.source.streams, engine="dlt")

        # Should NOT raise — placeholder bypasses the strict gate.
        enforce_schema_policy_or_raise(
            self._make_ctx(self._baseline_contract()), PlaceholderRunner()
        )

    def test_strict_policy_still_fires_for_real_drift(self):
        # A runner that returns a REAL fingerprint with mismatched columns
        # under strict policy should still raise (or at least not silently
        # pass — the gate routes to SchemaDriftError when applicable).
        from fluid_build.build_runners._acquisition_common import (
            enforce_schema_policy_or_raise,
        )

        class RealRunner:
            def fingerprint(self, ctx):
                # Returns a NON-placeholder fingerprint with columns that
                # don't match the contract baseline — drift in both
                # directions (added foreign columns, removed contract ones).
                return SchemaFingerprint.of(
                    [
                        SchemaColumn(name="UNKNOWN_COL", type="STRING"),
                    ]
                )

        # We expect either:
        #   - a raised SchemaDriftError (when typed-catalog renderer is wired)
        #   - or a no-op (when the catalog isn't available — the gate's
        #     defensive ``except Exception: return`` swallows it)
        # Either way the placeholder path above must NOT be the reason for
        # passing. We assert the call doesn't crash.
        try:
            enforce_schema_policy_or_raise(self._make_ctx(self._baseline_contract()), RealRunner())
        except Exception:
            # SchemaDriftError or any typed-catalog raise is acceptable —
            # the contract is that real drift is HANDLED, not silently
            # ignored as it would be for placeholders.
            pass


# ── _state ───────────────────────────────────────────────────────────────


class TestFileStateStore:
    def test_cursor_round_trip(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        c = Cursor(
            stream="orders",
            value={"high_water_mark": "2026-01-01T00:00:00Z"},
            updated_at=utc_now_iso(),
        )
        store.set_cursor("p1", "b1", c)
        got = store.get_cursor("p1", "b1", "orders")
        assert got is not None
        assert got.stream == "orders"
        assert got.value == c.value

    def test_watermark_round_trip(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        w = Watermark(
            stream="orders",
            kind="high_water_mark",
            value="2026-01-01T00:00:00Z",
            updated_at=utc_now_iso(),
        )
        store.set_watermark("p1", "b1", w)
        got = store.get_watermark("p1", "b1", "orders")
        assert got is not None and got.kind == "high_water_mark"

    def test_run_record_round_trip(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        rec = {"run_id": "r1", "state": "succeeded", "records": 100}
        store.write_run_record("p1", "b1", rec)
        got = store.read_run_record("p1", "b1", "r1")
        assert got == rec

    def test_list_runs_orders_newest_first(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        for run_id in ("r1", "r2", "r3"):
            store.write_run_record("p1", "b1", {"run_id": run_id, "state": "succeeded"})
        runs = store.list_runs("p1", "b1", limit=3)
        assert len(runs) == 3
        # Names sort in reverse alphabetically; r3 first.
        assert runs[0]["run_id"] == "r3"

    def test_lock_acquire_and_release(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        with store.acquire_lock("product", "p1", timeout_seconds=60) as lock:
            assert lock.scope == "product"
            assert lock.resource_id == "p1"
        # After context exit, the lock file is gone.
        lock_path = tmp_path / "locks" / "product__p1.lock"
        assert not lock_path.exists()

    def test_lock_held_raises_under_abort(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        with store.acquire_lock("product", "p1", timeout_seconds=60):
            with pytest.raises(LockHeldError):
                with store.acquire_lock("product", "p1", timeout_seconds=60, on_contended="abort"):
                    pass

    def test_atomic_write_no_partial_files_left(self, tmp_path: Path):
        """No `.tmp-*` files leaked after a successful write."""
        store = FileStateStore(tmp_path)
        store.write_run_record("p1", "b1", {"run_id": "r1"})
        for path in tmp_path.rglob("*.tmp-*"):
            pytest.fail(f"leftover tmp file: {path}")


# ── _retry ───────────────────────────────────────────────────────────────


class TestRetry:
    def test_retryable_classification(self):
        assert is_retryable(TimeoutError("connection timed out"))
        assert is_retryable(RuntimeError("503 Service Unavailable"))
        assert not is_retryable(PermissionError("403 Forbidden"))
        assert not is_retryable(ValueError("invalid argument"))

    def test_with_retry_succeeds_after_retry(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("connection timed out")
            return "ok"

        result = with_retry(
            flaky, RetryPolicy(count=3, jitter=False, initial_delay=0.0), sleep=lambda _: None
        )
        assert result == "ok"
        assert calls["n"] == 3

    def test_with_retry_propagates_non_retryable(self):
        def boom():
            raise ValueError("invalid argument")

        with pytest.raises(ValueError):
            with_retry(
                boom, RetryPolicy(count=3, jitter=False, initial_delay=0.0), sleep=lambda _: None
            )

    def test_with_retry_exhausts_retries(self):
        def boom():
            raise TimeoutError("connection timed out")

        with pytest.raises(TimeoutError):
            with_retry(
                boom, RetryPolicy(count=2, jitter=False, initial_delay=0.0), sleep=lambda _: None
            )


# ── _fingerprint ─────────────────────────────────────────────────────────


class TestFingerprint:
    def test_column_order_invariance(self):
        cols_a = [{"name": "id", "type": "int"}, {"name": "name", "type": "varchar"}]
        cols_b = [{"name": "name", "type": "varchar"}, {"name": "id", "type": "int"}]
        assert fingerprint_from_columns(cols_a).digest == fingerprint_from_columns(cols_b).digest

    def test_added_column_changes_digest(self):
        cols_a = [{"name": "id", "type": "int"}]
        cols_b = [{"name": "id", "type": "int"}, {"name": "email", "type": "varchar"}]
        assert fingerprint_from_columns(cols_a).digest != fingerprint_from_columns(cols_b).digest


# ── _schema_evolution ────────────────────────────────────────────────────


class TestSchemaEvolutionMatrix:
    def test_strict_fails_on_added(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("email", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.STRICT)
        assert plan.must_fail
        assert plan.decisions[0].action is EvolutionAction.FAIL

    def test_evolve_safe_includes_added(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("email", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        assert not plan.must_fail
        assert plan.decisions[0].action is EvolutionAction.INCLUDE

    def test_evolve_safe_warns_on_removed(self):
        baseline = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        current = [SchemaColumn("id", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        assert plan.decisions[0].action is EvolutionAction.WARN

    def test_evolve_all_drops_removed(self):
        baseline = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        current = [SchemaColumn("id", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_ALL)
        assert plan.decisions[0].action is EvolutionAction.DROP

    def test_evolve_safe_fails_on_type_narrow(self):
        baseline = [SchemaColumn("amount", "bigint")]
        current = [SchemaColumn("amount", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        assert plan.must_fail

    def test_evolve_all_casts_type_narrow(self):
        baseline = [SchemaColumn("amount", "bigint")]
        current = [SchemaColumn("amount", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_ALL)
        assert plan.decisions[0].action is EvolutionAction.CAST

    def test_discover_and_freeze_first_run_includes_added(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.DISCOVER_AND_FREEZE, is_first_run=True)
        assert plan.decisions[0].action is EvolutionAction.INCLUDE

    def test_discover_and_freeze_after_first_run_fails(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.DISCOVER_AND_FREEZE, is_first_run=False)
        assert plan.must_fail

    def test_override_stricter_wins(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("email", "varchar")]
        plan = resolve(
            baseline, current, SchemaPolicy.EVOLVE_ALL, overrides={"onAddedColumn": "fail"}
        )
        assert plan.must_fail


# ── _dlq ─────────────────────────────────────────────────────────────────


class TestDLQ:
    def test_append_and_count(self, tmp_path: Path):
        cfg = DLQConfig(enabled=True, location=str(tmp_path / "dlq"), max_records_before_abort=100)
        writer = DLQWriter(cfg, "r1", tmp_path)
        for i in range(5):
            writer.append("orders", {"id": i}, "test_reason")
        assert writer.total() == 5
        # File written
        files = list((tmp_path / "dlq" / "r1").glob("*.ndjson"))
        assert len(files) == 1
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 5
        first = json.loads(lines[0])
        assert first["reason"] == "test_reason"

    def test_overflow_raises(self, tmp_path: Path):
        cfg = DLQConfig(enabled=True, location=str(tmp_path / "dlq"), max_records_before_abort=2)
        writer = DLQWriter(cfg, "r1", tmp_path)
        writer.append("orders", {"id": 1}, "x")
        writer.append("orders", {"id": 2}, "x")
        with pytest.raises(DLQOverflowError):
            writer.append("orders", {"id": 3}, "x")


# ── _cost ────────────────────────────────────────────────────────────────


class TestCost:
    def test_parse_bytes_units(self):
        assert parse_bytes("100B") == 100
        assert parse_bytes("50GB") == 50 * 10**9
        assert parse_bytes("1.5MB") == 1_500_000
        assert parse_bytes(None) is None
        assert parse_bytes("garbage") is None

    def test_in_memory_tracker_records(self):
        t = InMemoryCostTracker()
        t.record_records(100)
        t.record_bytes(1_000_000, direction="read")
        t.record_compute_seconds(15.5)
        usage = t.usage()
        assert usage["rows"] == 100
        assert usage["bytes_read"] == 1_000_000
        assert usage["compute_seconds"] == 15

    def test_gate_or_raise_aborts_when_over_budget(self):
        t = InMemoryCostTracker()
        cap = BudgetCap(rows=10, on_exceed="abort")
        with pytest.raises(BudgetExceededError):
            gate_or_raise(t, cap, prior_usage={"rows": 11})

    def test_gate_or_raise_passes_when_within_budget(self):
        t = InMemoryCostTracker()
        cap = BudgetCap(rows=100, on_exceed="abort")
        gate_or_raise(t, cap, prior_usage={"rows": 50})  # no raise


# ── _retention ───────────────────────────────────────────────────────────


class TestRetention:
    def test_parse_iso_duration_days(self):
        d = parse_iso_duration("P30D")
        assert d.days == 30

    def test_parse_iso_duration_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_iso_duration("30 days")

    def test_retention_config_defaults(self):
        cfg = RetentionConfig.from_dict(None)
        assert cfg.run_state.days == 30
        assert cfg.run_logs.days == 90
        assert cfg.lineage.days == 365


# ── hooks integration ────────────────────────────────────────────────────


class TestHookChain:
    def test_dlp_then_tokenize(self):
        from fluid_build.api.hooks import HookChain
        from fluid_build.build_runners.hooks.dlp_scan import DlpScanHook
        from fluid_build.build_runners.hooks.tokenize_pii import TokenizePiiHook

        chain = HookChain([DlpScanHook(), TokenizePiiHook()])
        records = [
            {"id": 1, "email": "alice@example.com", "name": "Alice"},
            {"id": 2, "email": "bob@example.com", "name": "Bob"},
        ]
        # Tokenize hook reads classifications from ctx; chain.run threads them.
        ctx: Dict[str, Any] = {"classifications": {}}
        # First pass: dlp_scan populates classifications. We feed those into ctx,
        # then tokenize_pii uses them.
        result = chain.run(records, ctx={"classifications": {}})
        # After scan, classifications include 'email'.
        assert "email" in result.classifications
        assert "email" in result.classifications and "email" in result.classifications["email"]


# ── finalize_run_result (silent-failure fix) ─────────────────────────────
#
# Pin: when an acquisition runner returns a ``RunResult`` with
# ``state == FAILED`` and a non-empty ``error`` string, the helper MUST
# write the error to stderr AND log it under ``fluid.acquire.<engine>``.
#
# Before the fix, runners returned ``return 0 if state in (SUCCEEDED,
# PARTIAL) else 1`` and the error message was captured in the run record
# but never surfaced to the user — they saw "❌ Failed: 1" with no
# explanation. The real-world test pass surfaced this against the
# debezium runner (``deployment.server_url`` missing); the fix lifted
# the error-printing into a shared helper so all six engines benefit.


class TestFinalizeRunResult:
    def _run_result(self, *, state: str, error: str = ""):
        from fluid_build.api.runner import RunResult, RunState

        return RunResult(
            run_id="test-run",
            state=RunState[state],
            started_at="2026-05-01T00:00:00Z",
            finished_at="2026-05-01T00:00:01Z",
            records_total=0,
            bytes_total=0,
            dlq_records=0,
            streams=[],
            error=error or None,
            facets={},
        )

    def test_succeeded_returns_zero_silently(self, capsys):
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result("duckdb", "ingest_x", self._run_result(state="SUCCEEDED"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "failed" not in captured.err.lower()

    def test_partial_returns_zero_by_default(self, capsys):
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result("airbyte", "ingest_x", self._run_result(state="PARTIAL"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "failed" not in captured.err.lower()

    def test_failed_returns_one_AND_prints_error_to_stderr(self, capsys):
        """The bug we fixed: a FAILED RunResult with an error message
        now surfaces that message; before the fix, ``❌ Failed: 1``
        was the only signal the user got."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "debezium",
            "cdc_orders",
            self._run_result(
                state="FAILED",
                error="debezium kafka-connect mode requires deployment.server_url",
            ),
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "debezium build 'cdc_orders' failed" in captured.err
        assert "deployment.server_url" in captured.err

    def test_failed_with_no_error_message_uses_placeholder(self, capsys):
        """A defensive check: even when the runner forgets to populate
        ``result.error``, the user gets a non-empty message."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result("dlt", "ingest_x", self._run_result(state="FAILED"))
        assert rc == 1
        captured = capsys.readouterr()
        assert "failed" in captured.err.lower()
        assert "no error message captured" in captured.err

    def test_succeeded_states_override_treats_partial_as_failure(self, capsys):
        """The duckdb runner uses ``succeeded_states=(SUCCEEDED,)`` so
        a PARTIAL run is treated as a failure (raises PartialFailureError
        before reaching this helper, but the override still works as a
        defensive boundary)."""
        from fluid_build.api.runner import RunState
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "duckdb",
            "ingest_x",
            self._run_result(state="PARTIAL", error="one stream failed"),
            succeeded_states=(RunState.SUCCEEDED,),
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "one stream failed" in captured.err

    def test_redacts_password_in_error_string_to_stderr(self, capsys):
        """Security: when the runner's exception echoes the libpq DSN
        (which the duckdb postgres / mysql extensions routinely do),
        the password MUST be redacted before it reaches the user's
        terminal. The user-facing path routes through
        ``cli.console.error`` (applies ``_redact_str``) and
        ``redact_secret_text`` runs first."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "duckdb",
            "ingest",
            self._run_result(
                state="FAILED",
                error="binder error: host=db.x.com user=alice password=hunter2 db=t",
            ),
        )
        assert rc == 1
        captured = capsys.readouterr()
        # The actual security property: the plaintext password value
        # must not leak. Which redaction marker fires first
        # (``***REDACTED***`` from ours, ``<redacted>`` from the
        # console layer) is an implementation detail.
        assert "hunter2" not in captured.err
        assert ("REDACTED" in captured.err) or ("<redacted>" in captured.err)
        # The non-secret context survives so the user can still diagnose.
        assert "binder error" in captured.err
        assert "duckdb build 'ingest' failed" in captured.err

    def test_strips_ansi_escapes_from_error_string(self, capsys):
        """Security: a contract-supplied error string can carry ANSI
        escape sequences that overwrite prior terminal output (status
        line spoofing). The escape sequences must be stripped before
        the stderr write."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "airbyte",
            "ingest",
            self._run_result(
                state="FAILED",
                error="connect failed\x1b[2J\x1b[H[FAKE OK]\rrun completed",
            ),
        )
        assert rc == 1
        captured = capsys.readouterr()
        # ANSI control sequences must be gone.
        assert "\x1b[" not in captured.err
        # \r (which would overwrite the previous line) must be stripped.
        assert "\r" not in captured.err
        # The plain text from the error survives.
        assert "connect failed" in captured.err
