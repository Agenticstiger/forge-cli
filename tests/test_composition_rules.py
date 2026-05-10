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

"""Composition-rule pinning (Phase 1.4).

The composition pipeline encodes the data-mesh axiom that:

* **SDP** is source-aligned and accepts NO internal upstreams (all data
  must come from external systems via the acquisition pattern).
* **ADP** accepts SDP and ADP upstreams.
* **CDP** accepts SDP, ADP, AND CDP upstreams. Gold-on-gold composition
  is common in practice (a customer-360 CDP feeding an executive
  dashboard CDP, a metrics-mart feeding an ML-feature mart, etc.) so
  the registry intentionally allows it.

These rules drive two distinct surfaces:

* **Picker-time filtering** — when the user is authoring an ADP, the
  upstream picker hides CDPs from the choice list.
* **Validate-time enforcement** — a contract that loaded with an
  illegal ``consumes[]`` reference is rejected by ``validate_composition``.

This file pins both surfaces so a future schema change can't silently
relax the rules.
"""

from __future__ import annotations

import pytest

from fluid_build.forge.product_types import (
    PRODUCT_TYPES,
    CompositionViolation,
    get_product_type,
    validate_composition,
)

# ---------------------------------------------------------------------------
# Picker-time filter — what the upstream chooser shows the user
# ---------------------------------------------------------------------------


def _picker_filter(target_type: str, candidates: list[str]) -> list[str]:
    """Return the ids of candidates whose productType is allowed.

    Mirrors the picker-time filter the interview should use. Each
    candidate is ``"<id>:<productType>"`` so the test reads cleanly.
    """
    target = get_product_type(target_type)
    if target is None:
        return []
    allowed = target.allowed_upstream_types
    out = []
    for cand in candidates:
        cid, ctype = cand.split(":")
        ctype_pt = get_product_type(ctype)
        ctype_code = ctype_pt.code if ctype_pt else ctype
        if ctype_code in allowed:
            out.append(cid)
    return out


def test_picker_for_adp_hides_cdps():
    workspace = ["sdp1:SDP", "sdp2:SDP", "adp1:ADP", "cdp1:CDP"]
    assert _picker_filter("ADP", workspace) == ["sdp1", "sdp2", "adp1"]


def test_picker_for_cdp_includes_cdps():
    """CDP composition allows SDP + ADP + CDP — gold-on-gold (a
    metrics-mart CDP feeding an ML-feature CDP, an exec dashboard
    CDP built on a customer-360 CDP) is a common real-world shape.
    The picker must surface every CDP in the workspace as a candidate
    upstream when the user is authoring a new CDP."""
    workspace = ["sdp1:SDP", "sdp2:SDP", "adp1:ADP", "cdp1:CDP"]
    assert _picker_filter("CDP", workspace) == ["sdp1", "sdp2", "adp1", "cdp1"]


def test_picker_for_sdp_returns_empty():
    """SDPs ingest from external systems only — no internal upstreams."""
    workspace = ["sdp1:SDP", "adp1:ADP", "cdp1:CDP"]
    assert _picker_filter("SDP", workspace) == []


def test_picker_aliases_resolve_to_canonical_codes():
    """Bronze-aliased candidate is treated as SDP."""
    workspace = ["legacy_bronze:Bronze", "sdp1:SDP", "cdp1:CDP"]
    assert _picker_filter("ADP", workspace) == ["legacy_bronze", "sdp1"]


# ---------------------------------------------------------------------------
# Validate-time enforcement
# ---------------------------------------------------------------------------


def test_validate_sdp_rejects_any_upstream():
    """SDPs are source-aligned and accept zero internal upstreams."""
    out = validate_composition(target_type="SDP", upstream_types={"crm.orders": "SDP"})
    assert len(out) == 1
    v = out[0]
    assert isinstance(v, CompositionViolation)
    assert v.upstream_id == "crm.orders"
    assert v.target_type == "SDP"
    assert "does not accept upstream" in v.reason


def test_validate_adp_accepts_sdp_and_adp():
    out = validate_composition(
        target_type="ADP",
        upstream_types={"sdp1": "SDP", "adp1": "ADP"},
    )
    assert out == []


def test_validate_adp_rejects_cdp_upstream():
    out = validate_composition(
        target_type="ADP",
        upstream_types={"sdp1": "SDP", "cdp1": "CDP"},
    )
    # SDP passes, CDP fails.
    assert len(out) == 1
    v = out[0]
    assert v.upstream_id == "cdp1"
    assert v.upstream_type == "CDP"
    assert "ADP accepts" in v.reason
    assert "CDP" in v.reason


def test_validate_cdp_accepts_sdp_and_adp():
    out = validate_composition(
        target_type="CDP",
        upstream_types={"sdp1": "SDP", "adp1": "ADP"},
    )
    assert out == []


def test_validate_cdp_accepts_cdp_upstream():
    """CDP→CDP is allowed — gold-on-gold composition is common in
    practice (executive dashboards built on customer-360 marts,
    ML-feature CDPs built on metrics CDPs). Cycle protection happens
    in the DAG validator, not at the type-allowlist level."""
    out = validate_composition(
        target_type="CDP",
        upstream_types={"cdp1": "CDP"},
    )
    assert out == []


def test_validate_unknown_upstream_type_is_violation():
    """A productId that doesn't carry a known type can't be verified
    and therefore is a violation. Conservative-by-default."""
    out = validate_composition(
        target_type="ADP",
        upstream_types={"mystery": None},
    )
    assert len(out) == 1
    assert out[0].upstream_id == "mystery"
    assert out[0].upstream_type is None
    assert "unknown" in out[0].reason.lower()


def test_validate_aliases_resolve_to_canonical_codes():
    """Upstream typed as Silver should resolve to ADP and pass for CDP."""
    out = validate_composition(
        target_type="CDP",
        upstream_types={"legacy.silver.orders": "Silver"},
    )
    assert out == []


def test_validate_unknown_target_returns_no_violations():
    """Defensive: an unknown target_type returns []. Caller decides
    what to do with a mistyped target (the interview catches that
    elsewhere)."""
    out = validate_composition(
        target_type="Platinum",
        upstream_types={"sdp1": "SDP"},
    )
    assert out == []


# ---------------------------------------------------------------------------
# Parametrized table — every (target × upstream) pair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["SDP", "ADP", "CDP"])
@pytest.mark.parametrize("upstream", ["SDP", "ADP", "CDP"])
def test_table_driven_composition_matrix(target, upstream):
    """Pin every cell of the 3×3 target×upstream matrix.

    SDP: rejects everything (source-aligned, ingests from external).
    ADP: accepts SDP + ADP, rejects CDP (medallion ratchet).
    CDP: accepts SDP + ADP + CDP (gold-on-gold is real-world).
    """
    out = validate_composition(target_type=target, upstream_types={"u": upstream})
    target_pt = get_product_type(target)
    assert target_pt is not None

    expected_violation = upstream not in target_pt.allowed_upstream_types
    assert (
        bool(out) is expected_violation
    ), f"target={target} upstream={upstream}: expected violation={expected_violation}, got {out}"


# ---------------------------------------------------------------------------
# Allowed_upstream_types is exposed at the registry level
# ---------------------------------------------------------------------------


def test_registry_exposes_allowed_upstream_types_per_type():
    """Tests that depend on the registry shape (e.g. picker UI) can
    read the canonical allowlists without parsing strings.

    CDP includes itself — gold-on-gold composition is a first-class
    pattern (mart on mart, dashboard on customer360, etc.). Cycle
    protection lives in the DAG validator, not here.
    """
    by_code = {pt.code: pt for pt in PRODUCT_TYPES}

    assert by_code["SDP"].allowed_upstream_types == frozenset()
    assert by_code["ADP"].allowed_upstream_types == frozenset({"SDP", "ADP"})
    assert by_code["CDP"].allowed_upstream_types == frozenset({"SDP", "ADP", "CDP"})
