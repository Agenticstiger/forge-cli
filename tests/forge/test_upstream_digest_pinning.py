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

"""Pin tests for upstream digest pinning + version anchoring (Plan 2.1).

Three layers of coverage:

* **Composition shape** — what the composition pipeline writes into
  ``consumes[]``. Today: ``{productId, exposeId}``. Future plan-item
  extension: also ``upstreamDigest`` + ``version``. The current rows
  are pinned as the canonical shape; the digest/version fields are
  marked as expected future additions (xfail until shipped).

* **Apply gate (federation)** — drift between the pinned
  ``upstreamDigest`` and the live upstream digest must abort apply
  with ``CLIError("apply_consumes_drift", kind="upstream-mismatch")``.
  This mirrors the ``PlanBindingError(kind="bundle-mismatch")`` shape
  so CI templates can match both with one regex.

* **Escape hatch** — ``--no-verify-digest`` bypasses the gate but
  must log a WARNING so audit trails record the override.

Sister fixture ``tests/forge/test_federation_apply_gate.py`` covers
the runtime patching surface; this module pins the *shape contract*
upstream-digest plumbing rests on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. Composition shape — what to_consumes_block emits today.
# ---------------------------------------------------------------------------


class TestCompositionConsumesShape:
    """The composition pipeline projects upstreams into the schema-correct
    ``consumes[]`` shape. Today every row carries ``productId`` and
    ``exposeId``."""

    def _make_context(self):
        from fluid_build.forge_datamodel.from_data_products.pipeline import (
            CompositionContext,
            UpstreamProduct,
        )

        upstreams = (
            UpstreamProduct(
                id="demo.customers_sdp",
                name="Customers SDP",
                product_type="SDP",
                layer="Bronze",
                domain="customer",
                contract_path="/tmp/demo/customers_sdp/contract.fluid.yaml",
                exposes=(
                    {"exposeId": "customers", "kind": "Table", "schema": []},
                    {"exposeId": "customers_audit", "kind": "Table", "schema": []},
                ),
            ),
            UpstreamProduct(
                id="demo.orders_sdp",
                name="Orders SDP",
                product_type="SDP",
                layer="Bronze",
                domain="orders",
                contract_path="/tmp/demo/orders_sdp/contract.fluid.yaml",
                exposes=({"exposeId": "orders", "kind": "Table", "schema": []},),
            ),
        )
        return CompositionContext(
            target_type="ADP",
            upstream_products=upstreams,
            violations=(),
        )

    def test_emits_one_row_per_upstream_expose(self):
        """One consumes[] row per (upstream, expose) tuple — duplicates
        are NOT collapsed; the LLM can dedupe later if it likes."""
        ctx = self._make_context()
        rows = ctx.to_consumes_block()
        assert len(rows) == 3  # 2 customers exposes + 1 orders expose

    def test_each_row_has_productId_and_exposeId(self):
        ctx = self._make_context()
        for row in ctx.to_consumes_block():
            assert "productId" in row
            assert "exposeId" in row
            assert row["productId"]  # non-empty
            assert row["exposeId"]  # non-empty

    def test_productId_matches_upstream_id_verbatim(self):
        ctx = self._make_context()
        rows = ctx.to_consumes_block()
        ids = {row["productId"] for row in rows}
        assert ids == {"demo.customers_sdp", "demo.orders_sdp"}


@pytest.mark.xfail(
    reason=(
        "Plan 2.1 future extension: the composition pipeline should "
        "auto-capture each upstream contract's bundleDigest into "
        "``consumes[i].upstreamDigest`` and the upstream contract's "
        "version into ``consumes[i].version``. Today the operator must "
        "set these manually (and only the federation gate validates "
        "them — for ``upstreamWorkspace``-tagged rows). When this "
        "auto-capture lands, remove the xfail marker."
    ),
    strict=False,
)
class TestCompositionEmitsDigest:
    """xfail until the auto-capture feature lands."""

    def test_consumes_row_carries_upstream_digest(self):
        from fluid_build.forge_datamodel.from_data_products.pipeline import (
            CompositionContext,
            UpstreamProduct,
        )

        ctx = CompositionContext(
            target_type="ADP",
            upstream_products=(
                UpstreamProduct(
                    id="demo.customers_sdp",
                    name="Customers SDP",
                    product_type="SDP",
                    layer="Bronze",
                    domain="customer",
                    contract_path="/tmp/demo/customers_sdp/contract.fluid.yaml",
                    exposes=({"exposeId": "customers", "kind": "Table", "schema": []},),
                ),
            ),
            violations=(),
        )
        row = ctx.to_consumes_block()[0]
        assert row.get("upstreamDigest", "").startswith("sha256:")
        assert row.get("version") is not None


# ---------------------------------------------------------------------------
# 2. Federation drift gate — typed error shape.
# ---------------------------------------------------------------------------


class TestFederationViolationShape:
    """The drift gate's typed error must carry the documented field
    set so CI templates can parse it with a single regex."""

    def test_violation_dataclass_has_required_fields(self):
        from fluid_build.forge.federation import FederatedConsumeViolation

        v = FederatedConsumeViolation(
            consume_index=0,
            upstream_workspace_id="ws",
            upstream_product_id="p",
            expected_digest="sha256:STALE",
            actual_digest="sha256:LIVE",
            reason="drift",
        )
        # Stable fields the apply CLIError payload references.
        assert v.consume_index == 0
        assert v.upstream_workspace_id == "ws"
        assert v.upstream_product_id == "p"
        assert v.expected_digest == "sha256:STALE"
        assert v.actual_digest == "sha256:LIVE"
        assert v.reason == "drift"

    def test_validate_federated_consumes_returns_list(self, tmp_path: Path):
        """Local-only consumes[] (no ``upstreamWorkspace``) returns []
        cleanly — federation gate is a no-op for in-workspace mesh."""
        from fluid_build.forge.federation import validate_federated_consumes

        contract = {
            "fluidVersion": "0.7.3",
            "id": "demo.consumer",
            "consumes": [
                {"productId": "demo.upstream", "exposeId": "rows"},  # local
            ],
        }
        violations = validate_federated_consumes(contract, workspace_root=tmp_path)
        assert violations == []

    def test_missing_pin_on_federated_consume_is_a_violation(self, tmp_path: Path):
        """A ``consumes[]`` row that declares ``upstreamWorkspace`` but
        not ``upstreamDigest`` produces a violation (operator must
        pin)."""
        from fluid_build.forge.federation import (
            FederatedWorkspace,
            FederationManifest,
            validate_federated_consumes,
        )

        manifest = FederationManifest(
            workspaces=(
                FederatedWorkspace(
                    id="telco",
                    kind="git_registry",
                    endpoint="https://example.invalid",
                    auth_mode=None,
                    auth_secret_ref=None,
                ),
            )
        )
        contract = {
            "fluidVersion": "0.7.3",
            "id": "demo.consumer",
            "consumes": [
                {
                    "productId": "ext.upstream",
                    "exposeId": "rows",
                    "upstreamWorkspace": "telco",
                    # NO upstreamDigest — this is the violation.
                }
            ],
        }
        violations = validate_federated_consumes(
            contract, workspace_root=tmp_path, manifest=manifest
        )
        assert len(violations) == 1
        v = violations[0]
        assert v.upstream_workspace_id == "telco"
        # Reason should be self-explanatory for the operator.
        assert "upstreamDigest" in v.reason or "pin" in v.reason.lower()


# ---------------------------------------------------------------------------
# 3. Apply gate — drift produces typed CLIError("apply_consumes_drift").
# ---------------------------------------------------------------------------


class TestApplyConsumesDriftErrorShape:
    """The apply CLI must raise ``CLIError(event="apply_consumes_drift")``
    with ``kind="upstream-mismatch"`` when the federation gate finds
    drift. The error shape mirrors PlanBindingError so log parsers can
    match both with one regex."""

    def test_apply_raises_typed_error_on_drift(self, tmp_path: Path, monkeypatch):
        """Stub the federation validator to return a synthetic violation;
        confirm apply.run aborts with ``apply_consumes_drift`` BEFORE
        any DDL is emitted."""
        from fluid_build.cli import apply as apply_cli
        from fluid_build.cli._common import CLIError
        from fluid_build.forge.federation import FederatedConsumeViolation

        monkeypatch.chdir(tmp_path)
        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(
            "fluidVersion: 0.7.3\nid: demo.consumer\nconsumes:\n"
            "  - productId: demo.upstream\n    upstreamWorkspace: telco\n"
            "    upstreamDigest: sha256:STALE\n",
            encoding="utf-8",
        )
        from types import SimpleNamespace

        args = SimpleNamespace(
            contract=str(contract_path),
            env="dev",
            no_verify_digest=False,
            mode="amend",
            target=None,
            dry_run=True,
            timeout=60,
            parallel_phases=False,
            rollback_strategy="manual",
            allow_data_loss=False,
            config_override=None,
            verbose=False,
            provider=None,
            no_validate=True,
        )
        violation = FederatedConsumeViolation(
            consume_index=0,
            upstream_workspace_id="telco",
            upstream_product_id="ext.upstream",
            expected_digest="sha256:STALE",
            actual_digest="sha256:LIVE",
            reason="cached drift",
        )

        import logging

        logger = logging.getLogger("fluid.test")

        with patch(
            "fluid_build.forge.federation.validate_federated_consumes",
            return_value=[violation],
        ):
            with pytest.raises(CLIError) as excinfo:
                apply_cli.run(args, logger)

        # The CLIError event must be the documented stable string.
        assert excinfo.value.event == "apply_consumes_drift"
        # And carry kind="upstream-mismatch" — same posture as
        # PlanBindingError(kind="bundle-mismatch").
        assert excinfo.value.context.get("kind") == "upstream-mismatch"
        # Violations list propagates so CI parsers can render rich UX.
        assert len(excinfo.value.context.get("violations", [])) == 1


class TestApplyNoVerifyDigestEscapeHatch:
    """``--no-verify-digest`` skips the federation gate but logs at
    WARNING so audit trails record the override."""

    def test_no_verify_digest_skips_gate_and_warns(self, tmp_path: Path, monkeypatch, caplog):
        """When ``--no-verify-digest`` is set, apply must NOT call the
        federation validator AND must log a WARNING. We assert by
        patching the validator with a sentinel that records calls and
        confirming it stays uncalled even with a drifted contract on
        disk."""
        from fluid_build.cli import apply as apply_cli

        monkeypatch.chdir(tmp_path)
        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(
            "fluidVersion: 0.7.3\nid: demo.consumer\nconsumes:\n"
            "  - productId: demo.upstream\n    upstreamWorkspace: telco\n"
            "    upstreamDigest: sha256:STALE\n",
            encoding="utf-8",
        )
        from types import SimpleNamespace

        args = SimpleNamespace(
            contract=str(contract_path),
            env="dev",
            no_verify_digest=True,  # <-- escape hatch
            mode="amend",
            target=None,
            dry_run=True,
            timeout=60,
            parallel_phases=False,
            rollback_strategy="manual",
            allow_data_loss=False,
            config_override=None,
            verbose=False,
            provider=None,
            no_validate=True,
        )

        import logging

        logger = logging.getLogger("fluid.test")
        validator_called = {"count": 0}

        def _spy(*a, **kw):
            validator_called["count"] += 1
            return []

        with caplog.at_level(logging.WARNING, logger="fluid.test"):
            with patch(
                "fluid_build.forge.federation.validate_federated_consumes",
                side_effect=_spy,
            ):
                # Apply will fail downstream (no provider configured),
                # but the federation gate must already be skipped by
                # the time anything else runs.
                try:
                    apply_cli.run(args, logger)
                except Exception:
                    pass

        assert (
            validator_called["count"] == 0
        ), "validate_federated_consumes was called despite --no-verify-digest"
        # WARNING line must mention the skip so audit log parsers see it.
        warning_messages = [r.getMessage() for r in caplog.records]
        assert any(
            "no-verify-digest" in m.lower() or "skipped" in m.lower() for m in warning_messages
        ), f"no SKIP/WARN log found; got: {warning_messages}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
