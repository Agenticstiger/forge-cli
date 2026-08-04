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

"""Pin ``fluid plan --check-sovereignty`` against fail-open regressions.

The bug these tests exist for: ``AwsProvider`` has no public
``validate_sovereignty`` hook, so ``run_validate_sovereignty`` returned ``[]``,
and ``plan.py`` rendered an empty list as the string ``PASS`` — on a contract
``fluid validate`` rejects with two residency errors. **Absence of a check was
rendered as a pass**, on a governance control, with exit code 0.

The invariant, asserted from several angles below: the word ``PASS`` appears
only when a check actually ran and found nothing.
"""

import argparse
import logging
from typing import Any, Dict

import pytest

from fluid_build.cli.plan import _report_sovereignty

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _contract(
    *,
    region: str = "us-east-1",
    mode: str = "strict",
    denied: bool = True,
    sovereignty: bool = True,
) -> Dict[str, Any]:
    """A v0.7.x contract whose binding sits outside the allowed regions."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "analytics.eu.customer_events_v1",
        "name": "EU Customer Events",
        "description": "Pseudonymised customer interaction events, EU-resident.",
        "domain": "Customer",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "customer-platform", "email": "customer-platform@example.com"},
        },
        "exposes": [
            {
                "exposeId": "customer_events",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "region": region,
                        "database": "customer_analytics",
                        "table": "customer_events",
                        "bucket": "acme-lake",
                        "path": "curated/customer_analytics/customer_events/",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "event_time", "type": "timestamp", "required": True},
                    ]
                },
            }
        ],
    }
    if sovereignty:
        block: Dict[str, Any] = {
            "enforcementMode": mode,
            "jurisdiction": "EU",
            "allowedRegions": ["eu-central-1", "eu-west-1"],
        }
        if denied:
            block["deniedRegions"] = ["us-east-1"]
        contract["sovereignty"] = block
    return contract


class _NoHookProvider:
    """Stands in for ``AwsProvider`` — no ``validate_sovereignty`` hook."""

    name = "aws"


class _CleanHookProvider:
    name = "gcp"

    def validate_sovereignty(self, contract):  # pragma: no cover - trivial
        return []


class _ViolatingHookProvider:
    name = "gcp"

    def validate_sovereignty(self, contract):  # pragma: no cover - trivial
        return ["Data resides outside the EU"]


class _RaisingHookProvider:
    name = "gcp"

    def validate_sovereignty(self, contract):  # pragma: no cover - trivial
        raise RuntimeError("provider exploded")


# ---------------------------------------------------------------------------
# The fail-open itself
# ---------------------------------------------------------------------------


class TestNoProviderHookIsNotAPass:
    pytestmark = pytest.mark.unit

    def test_missing_hook_does_not_print_pass_on_violating_contract(self, capsys):
        """The exact regression: no hook + violating contract used to print PASS."""
        blocked = _report_sovereignty(_contract(), _NoHookProvider(), "aws", LOG)
        out = capsys.readouterr().out

        assert "PASS" not in out
        assert blocked is True

    def test_missing_hook_falls_back_to_the_builtin_policy_engine(self, capsys):
        """A provider without a hook still gets a real answer, not a shrug."""
        _report_sovereignty(_contract(), _NoHookProvider(), "aws", LOG)
        out = capsys.readouterr().out

        assert "us-east-1" in out  # the offending region is named
        assert "built-in policy engine" in out  # and the source is attributed
        assert "no sovereignty hook" in out

    def test_unbuildable_provider_still_runs_the_check(self, capsys):
        """``hook_provider=None`` must not silently skip a requested check.

        The call site used to read ``if check_sovereignty and hook_provider``,
        so a provider that failed to build swallowed the flag entirely and the
        command exited 0 having checked nothing.
        """
        blocked = _report_sovereignty(_contract(), None, "aws", LOG)
        out = capsys.readouterr().out

        assert blocked is True
        assert "PASS" not in out

    def test_raising_hook_is_not_a_pass(self, capsys):
        """A hook that blows up must not read as "checked, clean"."""
        blocked = _report_sovereignty(_contract(), _RaisingHookProvider(), "gcp", LOG)
        out = capsys.readouterr().out

        assert blocked is True
        assert "PASS" not in out


# ---------------------------------------------------------------------------
# The paths that legitimately pass
# ---------------------------------------------------------------------------


class TestLegitimateVerdicts:
    pytestmark = pytest.mark.unit

    def test_compliant_contract_passes_and_names_its_source(self, capsys):
        blocked = _report_sovereignty(
            _contract(region="eu-central-1"), _NoHookProvider(), "aws", LOG
        )
        out = capsys.readouterr().out

        assert blocked is False
        assert "PASS" in out
        assert "built-in policy engine" in out

    def test_provider_hook_clean_result_passes(self, capsys):
        blocked = _report_sovereignty(_contract(), _CleanHookProvider(), "gcp", LOG)
        out = capsys.readouterr().out

        assert blocked is False
        assert "PASS" in out
        assert "provider hook" in out

    def test_provider_hook_violations_block(self, capsys):
        blocked = _report_sovereignty(_contract(), _ViolatingHookProvider(), "gcp", LOG)
        out = capsys.readouterr().out

        assert blocked is True
        assert "Data resides outside the EU" in out
        assert "PASS" not in out

    def test_contract_without_a_sovereignty_block_is_not_checked_not_passed(self, capsys):
        """Nothing to check is reported as such — never as a pass."""
        blocked = _report_sovereignty(_contract(sovereignty=False), _NoHookProvider(), "aws", LOG)
        out = capsys.readouterr().out

        assert blocked is False
        assert "NOT CHECKED" in out
        assert "PASS" not in out


# ---------------------------------------------------------------------------
# enforcementMode semantics — and parity with ``fluid validate``
# ---------------------------------------------------------------------------


class TestEnforcementMode:
    pytestmark = pytest.mark.unit

    def test_advisory_downgrades_an_allowed_region_mismatch_to_a_warning(self, capsys):
        """Advisory mode does real work: a mere mismatch reports without blocking."""
        blocked = _report_sovereignty(
            _contract(mode="advisory", denied=False), _NoHookProvider(), "aws", LOG
        )
        out = capsys.readouterr().out

        assert blocked is False
        assert "finding(s)" in out  # reported...
        assert "us-east-1" in out  # ...and named

    def test_advisory_still_blocks_an_explicitly_denied_region(self, capsys):
        """An explicit deny is an error in any mode — and ``fluid validate``
        exits 1 on it, so ``plan`` must too or the stages disagree."""
        blocked = _report_sovereignty(
            _contract(mode="advisory", denied=True), _NoHookProvider(), "aws", LOG
        )
        assert blocked is True
        assert "PASS" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(region="eu-central-1"),  # compliant
            dict(mode="advisory", denied=False),  # warning only
            dict(mode="advisory", denied=True),  # error under advisory
            dict(mode="strict", denied=True),  # error under strict
        ],
    )
    def test_blocking_verdict_matches_fluid_validate(self, kwargs):
        """``plan --check-sovereignty`` must never be weaker than ``validate``.

        Both consult ``policy.sovereignty``; this pins that they agree on every
        combination, so a contract cannot pass one stage and fail the next.
        """
        from fluid_build.policy.sovereignty import validate_sovereignty

        contract = _contract(**kwargs)
        _, messages = validate_sovereignty(contract)
        validate_would_block = any("❌" in m for m in messages)

        blocked = _report_sovereignty(contract, _NoHookProvider(), "aws", LOG)
        assert blocked == validate_would_block


# ---------------------------------------------------------------------------
# End-to-end through ``plan.run`` — the exit code is the CI gate
# ---------------------------------------------------------------------------


class TestPlanExitCode:
    pytestmark = pytest.mark.unit

    @staticmethod
    def _args(tmp_path, contract_path, **kw):
        defaults = dict(
            contract=str(contract_path),
            env=None,
            out=str(tmp_path / "plan.json"),
            verbose=False,
            validate_actions=False,
            estimate_cost=False,
            check_sovereignty=True,
            provider=None,
            project=None,
            region=None,
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    @staticmethod
    def _write(tmp_path, contract):
        import yaml

        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        return path

    def test_violating_contract_exits_non_zero(self, tmp_path):
        from fluid_build._contract_loader import CLIError
        from fluid_build.cli.plan import run

        path = self._write(tmp_path, _contract())
        with pytest.raises(CLIError) as excinfo:
            run(self._args(tmp_path, path), LOG)

        assert excinfo.value.exit_code == 1
        assert excinfo.value.event == "sovereignty_violation"

    def test_compliant_contract_exits_zero(self, tmp_path):
        from fluid_build.cli.plan import run

        path = self._write(tmp_path, _contract(region="eu-central-1"))
        assert run(self._args(tmp_path, path), LOG) == 0

    def test_flag_off_does_not_gate(self, tmp_path):
        """Without the flag the check does not run, so nothing blocks."""
        from fluid_build.cli.plan import run

        path = self._write(tmp_path, _contract())
        assert run(self._args(tmp_path, path, check_sovereignty=False), LOG) == 0
