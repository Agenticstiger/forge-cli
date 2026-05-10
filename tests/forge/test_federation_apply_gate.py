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

"""Pin the federation drift gate inside ``cli/apply.py``.

This is the load-bearing test: without it, federated drift would
silently apply against stale upstreams. The validator was already
unit-tested; this fixture verifies the call is **wired** into apply
and that ``--no-verify-digest`` is the documented escape hatch.

We don't run a full apply — that needs a provider, plan, a real
contract, etc. Instead we patch
:func:`validate_federated_consumes` to return a synthetic violation
and assert apply.run raises ``CLIError(event="apply_consumes_drift")``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.forge.federation import FederatedConsumeViolation


@pytest.fixture
def sample_args(tmp_path: Path):
    """Minimal argparse Namespace shape that gets us to the federation
    gate without errors from the upstream code paths."""
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(
        "fluidVersion: 0.7.3\nid: ext.consumer\nconsumes:\n"
        "  - productId: ext.upstream\n    upstreamWorkspace: telco\n"
        "    upstreamDigest: sha256:STALE\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
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


def test_federation_gate_aborts_on_drift(monkeypatch, tmp_path, sample_args):
    """When ``validate_federated_consumes`` returns a violation, apply
    must raise CLIError before any DDL is emitted."""
    monkeypatch.chdir(tmp_path)

    violation = FederatedConsumeViolation(
        consume_index=0,
        upstream_workspace_id="telco",
        upstream_product_id="ext.upstream",
        expected_digest="sha256:STALE",
        actual_digest="sha256:LIVE",
        reason="cached drift detected",
    )

    # Sanity: the validator function exists at the documented path.
    from fluid_build.forge import federation as _fed

    assert hasattr(_fed, "validate_federated_consumes")

    # Run apply.run with the validator stubbed to return our violation.
    # The federation gate runs before downstream DDL emission, so we
    # expect CLIError("apply_consumes_drift") to abort first.
    import logging

    from fluid_build.cli import apply as apply_mod

    with patch(
        "fluid_build.forge.federation.validate_federated_consumes",
        return_value=[violation],
    ):
        with pytest.raises(CLIError) as exc_info:
            apply_mod.run(sample_args, logging.getLogger("test"))

    err = exc_info.value
    # Stable event-name for CI log parsers.
    assert err.event == "apply_consumes_drift"
    ctx = err.context or {}
    assert ctx.get("kind") == "upstream-mismatch"
    violations = ctx.get("violations") or []
    assert len(violations) == 1
    assert violations[0]["upstream_workspace_id"] == "telco"
    assert violations[0]["expected_digest"] == "sha256:STALE"
    assert violations[0]["actual_digest"] == "sha256:LIVE"


def test_federation_gate_bypassed_with_no_verify_digest(monkeypatch, tmp_path, sample_args):
    """``--no-verify-digest`` must skip the federation gate AND log at
    WARNING level so audit trails catch the operator override.

    We don't use pytest's ``caplog`` fixture here because it interacts
    badly with other tests that install root-logger filters. Instead
    we install a one-shot list-handler on the explicit logger we pass
    into ``apply_mod.run``, so the assertion is self-contained.
    """
    monkeypatch.chdir(tmp_path)
    sample_args.no_verify_digest = True

    import logging

    from fluid_build.cli import apply as apply_mod

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    test_logger = logging.getLogger("fluid.test.federation_gate_skip")
    handler = _ListHandler(level=logging.WARNING)
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.WARNING)
    # Don't propagate to root — we want a clean isolated capture
    # regardless of other tests' root-logger handlers.
    prior_propagate = test_logger.propagate
    test_logger.propagate = False

    try:
        with patch("fluid_build.forge.federation.validate_federated_consumes") as mock_validate:
            mock_validate.side_effect = AssertionError(
                "validate_federated_consumes should be skipped under --no-verify-digest"
            )
            try:
                apply_mod.run(sample_args, test_logger)
            except Exception:
                # apply will fail later (no provider, no plan); the
                # only thing we care about is that the gate skipped
                # before any DDL was attempted.
                pass

            assert not mock_validate.called, (
                "--no-verify-digest must skip the federation digest gate"
            )
    finally:
        test_logger.removeHandler(handler)
        test_logger.propagate = prior_propagate

    warning_messages = [
        record.getMessage() for record in captured if record.levelno >= logging.WARNING
    ]
    assert any("federation digest gate was SKIPPED" in m for m in warning_messages), (
        f"Expected WARNING about skip; got {warning_messages!r}"
    )
