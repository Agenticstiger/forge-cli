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

"""enforcementMode must mean what the schema says it means.

The schema is unambiguous:

    "How sovereignty violations are handled.
     strict = block deployment, advisory = warn, audit = log only."

and of `jurisdiction`:

    "Required legal jurisdiction ... Used to validate binding.location
     matches sovereignty intent."

Neither held. Severity was chosen without consulting the mode — check 1
(deniedRegions) hardcoded "error", check 3 (jurisdiction) hardcoded "warning" —
and `fluid validate` re-derives blocking from the message PREFIX
(cli/validate.py:637-645 routes ❌ to add_error unconditionally). So the mode was
honoured in the returned boolean and contradicted by the prefix the only caller
actually routes on, in both directions at once:

* **strict under-enforced.** A contract declaring `jurisdiction: EU` with every
  expose bound to us-east-1 produced zero error messages and validated clean.
  The field whose entire documented purpose is validating binding.location
  against sovereignty intent could not block anything in any mode.

* **advisory over-enforced.** A denied region under `advisory` emitted an
  ❌-prefixed message, which `fluid validate` promoted to a hard error — the
  opposite of "warn".

These tests pin the schema's wording, not the implementation's habits.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.policy.sovereignty import (
    UNCONSTRAINED_JURISDICTIONS,
    SovereigntyValidator,
    validate_sovereignty,
)

US_REGION = "us-east-1"
EU_REGION = "eu-west-1"


def contract(*, region: str = US_REGION, **sovereignty: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "p",
        "name": "P",
        "metadata": {"owner": {"team": "t"}},
        "sovereignty": sovereignty,
        "exposes": [
            {
                "exposeId": "e",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "athena_table",
                    "location": {"region": region, "database": "d", "table": "t"},
                },
                "contract": {"schema": [{"name": "c", "type": "string"}]},
            }
        ],
    }


def severities(doc: Dict[str, Any]) -> List[str]:
    return [v.severity for v in SovereigntyValidator().validate(doc)[1]]


def cli_prefixes(doc: Dict[str, Any]) -> Dict[str, int]:
    """How `fluid validate` will route the messages: ❌ error, ⚠️ warn, else log."""
    _, messages = validate_sovereignty(doc)
    return {
        "error": sum("❌" in m for m in messages),
        "warning": sum("⚠️" in m for m in messages),
        "info": sum("❌" not in m and "⚠️" not in m for m in messages),
    }


# ---------------------------------------------------------------------------
# strict = block deployment
# ---------------------------------------------------------------------------


def test_strict_jurisdiction_mismatch_blocks() -> None:
    """The headline bug: `strict` + `jurisdiction: EU` + a US binding validated clean."""
    doc = contract(enforcementMode="strict", jurisdiction="EU")
    is_valid, _ = SovereigntyValidator().validate(doc)
    assert is_valid is False, (
        "a strict contract declaring EU jurisdiction, bound entirely to us-east-1, "
        "must not validate — the schema says strict blocks deployment"
    )
    assert cli_prefixes(doc)["error"] >= 1


def test_strict_denied_region_still_blocks() -> None:
    """Regression guard: the one thing that already worked must keep working."""
    doc = contract(enforcementMode="strict", deniedRegions=[US_REGION])
    assert SovereigntyValidator().validate(doc)[0] is False
    assert cli_prefixes(doc)["error"] >= 1


def test_strict_allowed_region_violation_still_blocks() -> None:
    doc = contract(enforcementMode="strict", allowedRegions=[EU_REGION])
    assert SovereigntyValidator().validate(doc)[0] is False


# ---------------------------------------------------------------------------
# advisory = warn (not block, and not error)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sov",
    [
        {"allowedRegions": [EU_REGION]},
        {"jurisdiction": "EU"},
    ],
    ids=["allowed-region", "jurisdiction"],
)
def test_advisory_warns_and_never_errors(sov: Dict[str, Any]) -> None:
    """`fluid validate` must warn, not fail, under advisory.

    The bug was upstream of the CLI: an ❌ prefix on an advisory violation, which
    cli/validate.py promotes to a hard error regardless of mode.
    """
    doc = contract(enforcementMode="advisory", **sov)
    assert SovereigntyValidator().validate(doc)[0] is True
    counts = cli_prefixes(doc)
    assert counts["error"] == 0, (
        "advisory emitted an ❌ message; fluid validate routes that to add_error() "
        "unconditionally, so the build fails on a mode documented as 'warn'"
    )
    assert counts["warning"] >= 1, "advisory must still say something"


# ---------------------------------------------------------------------------
# audit = log only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sov",
    [
        {"allowedRegions": [EU_REGION]},
        {"jurisdiction": "EU"},
    ],
    ids=["allowed-region", "jurisdiction"],
)
def test_audit_logs_only(sov: Dict[str, Any]) -> None:
    doc = contract(enforcementMode="audit", **sov)
    assert SovereigntyValidator().validate(doc)[0] is True
    counts = cli_prefixes(doc)
    assert counts["error"] == 0
    assert (
        counts["warning"] == 0
    ), "audit is documented as 'log only'; a ⚠️ prefix makes it a warning surface"
    assert counts["info"] >= 1, "audit must still record something, or it is a no-op"


# ---------------------------------------------------------------------------
# the boolean must be trustworthy on its own
# ---------------------------------------------------------------------------


def test_is_valid_agrees_with_the_severities_it_returns() -> None:
    """No mode may return is_valid=True alongside an error-severity violation.

    Both existing callers independently re-scan the messages instead of trusting
    the boolean (cli/validate.py:637-648, cli/plan.py:592-601) precisely because
    it could not be trusted. A third caller branching only on is_valid would
    silently under-enforce.
    """
    for mode in ("strict", "advisory", "audit"):
        for sov in (
            {"deniedRegions": [US_REGION]},
            {"jurisdiction": "EU"},
            {"allowedRegions": [EU_REGION]},
        ):
            doc = contract(enforcementMode=mode, **sov)
            is_valid, violations = SovereigntyValidator().validate(doc)
            has_error = any(v.severity == "error" for v in violations)
            assert is_valid is not has_error, (
                f"mode={mode} sov={sov}: is_valid={is_valid} with "
                f"error-severity present={has_error}"
            )


def test_default_mode_is_strict() -> None:
    """An omitted enforcementMode must not silently downgrade enforcement."""
    doc = contract(jurisdiction="EU")
    assert SovereigntyValidator().validate(doc)[0] is False


# ---------------------------------------------------------------------------
# things that must NOT change
# ---------------------------------------------------------------------------


def test_compliant_contract_passes_in_every_mode() -> None:
    for mode in ("strict", "advisory", "audit"):
        doc = contract(region=EU_REGION, enforcementMode=mode, jurisdiction="EU")
        is_valid, violations = SovereigntyValidator().validate(doc)
        assert is_valid is True
        assert [v for v in violations if v.severity == "error"] == []


@pytest.mark.parametrize("jurisdiction", sorted(UNCONSTRAINED_JURISDICTIONS))
def test_catch_all_jurisdiction_is_not_a_violation_anywhere(jurisdiction: str) -> None:
    """Neither catch-all constrains where data may sit, in any mode.

    "Multi-Region" is the one that used to fail. Check 3 compared the pinned
    jurisdiction against the region's by equality and special-cased only
    "Global", so a value meaning "several jurisdictions" could never equal any
    real one. That was invisible while check 3 was hardcoded to "warning";
    once the mode decides severity and strict is the default, it refused every
    region a Multi-Region contract could name.
    """
    for mode in ("strict", "advisory", "audit"):
        doc = contract(enforcementMode=mode, jurisdiction=jurisdiction)
        assert SovereigntyValidator().validate(doc)[0] is True


def test_unknown_region_does_not_block_under_strict() -> None:
    """An unmappable region is 'cannot tell', not 'violates'.

    Deliberately unchanged. Failing closed here would be defensible for a
    sovereignty control, but it would break every contract using a region the
    vendored table does not carry, so it is a separate decision rather than a
    side effect of this fix.
    """
    doc = contract(region="mars-central-1", enforcementMode="strict", jurisdiction="EU")
    is_valid, violations = SovereigntyValidator().validate(doc)
    assert is_valid is True
    assert any(v.severity == "warning" for v in violations)


def test_explicit_deny_is_an_error_in_every_mode() -> None:
    """The deliberate carve-out, pinned here so it is not "fixed" by accident.

    An entry in deniedRegions is an operator naming a specific prohibition. It
    outranks the mode default, and tests/cli/test_plan_sovereignty_gate.py
    depends on it so `plan` and `validate` agree about the same contract.
    """
    for mode in ("strict", "advisory", "audit"):
        doc = contract(enforcementMode=mode, deniedRegions=[US_REGION])
        assert "error" in severities(doc)
        assert SovereigntyValidator().validate(doc)[0] is False
