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
and that ``--no-verify-federation`` is the documented escape hatch.

We don't run a full apply — that needs a provider, plan, a real
contract, etc. Instead we patch
:func:`validate_federated_consumes` to return a synthetic violation
and assert apply.run raises ``CLIError(event="apply_consumes_drift")``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.forge.federation import FederatedConsumeViolation

FEDERATED_CONTRACT_YAML = """fluidVersion: "0.7.6"
kind: DataProduct
id: ext.consumer
name: External Consumer
domain: sales
metadata:
  layer: Silver
  productType: ADP
  owner:
    team: platform-team
    email: platform@example.com
consumes:
  - productId: ext.upstream
    exposeId: orders
    upstreamWorkspace: telco
    upstreamDigest: sha256:0000000000000000000000000000000000000000000000000000000000000000
exposes:
  - exposeId: consumer_out
    kind: table
    version: "1.0.0"
    binding:
      platform: local
      format: parquet
      location:
        database: silver
        table: consumer_out
    contract:
      schema:
        - name: id
          type: integer
          required: true
"""


@pytest.fixture
def sample_args(tmp_path: Path):
    """Minimal argparse Namespace shape that gets us to the federation
    gate without errors from the upstream code paths."""
    contract_path = tmp_path / "contract.fluid.yaml"
    # A COMPLETE, schema-valid 0.7.6 contract -- not a stub. The federated
    # consume fields (``upstreamWorkspace``/``upstreamDigest``) are new in
    # 0.7.6, so a 0.7.3 header fails additionalProperties on consumes[].
    # Keeping this contract genuinely valid is what lets these tests run
    # the REAL pre-apply schema gate rather than stubbing it out, so the
    # gate and the schema stay honest about each other.
    contract_path.write_text(
        FEDERATED_CONTRACT_YAML,
        encoding="utf-8",
    )
    return SimpleNamespace(
        contract=str(contract_path),
        env="dev",
        no_verify_plan_binding=False,
        no_verify_federation=False,
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


def _capture_warnings(name: str):
    """Isolated WARNING capture on an explicit logger.

    Deliberately not ``caplog``: it installs root-logger handlers, and
    other tests in this suite install root-logger *filters*, so the two
    interfere. Returns ``(logger, records, restore)``.
    """
    import logging

    records: list = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(name)
    handler = _ListHandler(level=logging.WARNING)
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    prior = logger.propagate
    logger.propagate = False

    def restore():
        logger.removeHandler(handler)
        logger.propagate = prior

    return logger, records, restore


def test_federation_gate_warns_on_drift_and_does_not_abort(monkeypatch, tmp_path, sample_args):
    """Drift WARNS; it does not abort the apply.

    This gate's verdict depends on a *third party's* registry being
    reachable and honest, unlike the plan-binding gate which compares
    two artifacts we produced ourselves. Hard-failing here would let
    another team's git outage block our production applies, and the
    predictable response is a permanent ``--no-verify-federation``.
    So: warn loudly, name the workspace, keep applying.

    The escape hatch staying unused is the point -- assert the warning
    is emitted AND that no ``apply_consumes_drift`` CLIError escapes.
    """
    monkeypatch.chdir(tmp_path)

    violation = FederatedConsumeViolation(
        consume_index=0,
        upstream_workspace_id="telco",
        upstream_product_id="ext.upstream",
        expected_digest="sha256:STALE",
        actual_digest="sha256:LIVE",
        reason="cached drift detected",
        kind="drift",
    )

    from fluid_build.forge import federation as _fed

    assert hasattr(_fed, "validate_federated_consumes")

    from fluid_build.cli import apply as apply_mod

    logger, records, restore = _capture_warnings("fluid.test.federation_gate_warn")
    try:
        # NOTE: no ``_gate_contract_for_apply`` stub. The bundled 0.7.6
        # schema now models ``upstreamWorkspace``/``upstreamDigest``, so
        # this contract passes the real pre-apply schema gate. If that
        # regresses, this test fails here -- which is the point.
        with patch(
            "fluid_build.forge.federation.validate_federated_consumes",
            return_value=[violation],
        ):
            try:
                apply_mod.run(sample_args, logger)
            except CLIError as exc:
                assert exc.event != "apply_consumes_drift", (
                    "federation drift must WARN, not abort -- got a CLIError "
                    f"with event={exc.event!r}"
                )
            except Exception:
                # apply fails later for unrelated reasons (no provider,
                # no plan). Only the gate's behaviour is under test.
                pass
    finally:
        restore()

    messages = [r.getMessage() for r in records]
    drift_lines = [m for m in messages if "apply_consumes_drift" in m]
    assert drift_lines, f"expected an apply_consumes_drift WARNING; got {messages!r}"

    # The payload must stay machine-parseable: CI templates match this
    # gate and the plan-binding gate with one regex, and a warning that
    # only a human can read would silently drop federated drift out of
    # every dashboard that watches for it.
    blob = drift_lines[0]
    payload = json.loads(blob[blob.index("{") :])
    assert payload["kind"] == "upstream-mismatch"
    assert payload["drift_count"] == 1
    assert payload["unreachable_count"] == 0
    assert payload["violations"][0]["upstream_workspace_id"] == "telco"
    assert payload["violations"][0]["expected_digest"] == "sha256:STALE"
    assert payload["violations"][0]["actual_digest"] == "sha256:LIVE"
    assert payload["violations"][0]["violation_kind"] == "drift"

    # The per-row line names the workspace so an operator reading the
    # log knows *who* to chase without decoding JSON.
    assert any("telco/ext.upstream" in m for m in messages), messages


def test_federation_gate_bypassed_with_no_verify_federation(monkeypatch, tmp_path, sample_args):
    """``--no-verify-federation`` must skip the federation gate AND log at
    WARNING level so audit trails catch the operator override.

    We don't use pytest's ``caplog`` fixture here because it interacts
    badly with other tests that install root-logger filters. Instead
    we install a one-shot list-handler on the explicit logger we pass
    into ``apply_mod.run``, so the assertion is self-contained.
    """
    monkeypatch.chdir(tmp_path)
    sample_args.no_verify_federation = True

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
        # Stub the pre-apply schema gate — the federation contract uses
        # consume fields not modelled by the bundled schema, and this test
        # exercises the --no-verify-federation skip path, not schema
        # validity.
        with (
            patch("fluid_build.forge.federation.validate_federated_consumes") as mock_validate,
            patch("fluid_build.cli.apply._gate_contract_for_apply"),
        ):
            mock_validate.side_effect = AssertionError(
                "validate_federated_consumes should be skipped under --no-verify-federation"
            )
            try:
                apply_mod.run(sample_args, test_logger)
            except Exception:
                # apply will fail later (no provider, no plan); the
                # only thing we care about is that the gate skipped
                # before any DDL was attempted.
                pass

            assert (
                not mock_validate.called
            ), "--no-verify-federation must skip the federation digest gate"
    finally:
        test_logger.removeHandler(handler)
        test_logger.propagate = prior_propagate

    warning_messages = [
        record.getMessage() for record in captured if record.levelno >= logging.WARNING
    ]
    assert any(
        "federation digest gate was SKIPPED" in m for m in warning_messages
    ), f"Expected WARNING about skip; got {warning_messages!r}"
