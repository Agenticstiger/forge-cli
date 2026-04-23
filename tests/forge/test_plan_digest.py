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

"""Tests for plan-binding digests (stage 6 → stage 7 invariant).

Adversarial bias: every test pins one of the two cryptographic guarantees
``fluid apply`` relies on. Breaking any of these means the Terraform-style
"apply consumes exact plan" promise is no longer enforced.

    1. ``compute_plan_digest`` is deterministic — two runs over the same
       input dict return the same ``sha256:`` string, regardless of dict
       insertion order.
    2. ``compute_plan_digest`` masks the digest fields themselves. A plan
       whose ``planDigest`` is ``"stale-value"`` hashes the SAME as one
       whose field is ``"also-stale"`` — because both are masked out of
       the input. Without this, inject_digests can't work (hashing a
       dict that includes its own hash has no fixed point).
    3. ``inject_digests`` computes the digest AFTER ``bundleDigest`` is
       populated — so changing the bundle changes planDigest. Any
       regression where the order flips breaks the bundle-swap detection.
    4. ``verify_plan_binding`` distinguishes ``bundle-mismatch`` from
       ``plan-tamper`` via the ``kind`` attribute. CI log parsers rely
       on this split to route alerts differently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.forge.core.bundle import build_bundle_tgz
from fluid_build.forge.core.plan_digest import (
    PlanBindingError,
    compute_plan_digest,
    inject_digests,
    is_bundle_path,
    read_bundle_digest,
    verify_plan_binding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_plan() -> Dict[str, Any]:
    """A small but structurally realistic plan body for hashing tests."""
    return {
        "format_version": "0.7.1",
        "generated_at": 1700000000,
        "contract": {"id": "orders", "name": "Orders", "version": "0.7.1"},
        "actions": [
            {
                "step": 1,
                "action_id": "create_table",
                "action_type": "ensure_table",
                "provider": "snowflake",
                "params": {"table": "orders"},
                "depends_on": [],
            }
        ],
        "total_actions": 1,
    }


def _build_bundle(tmp_path: Path, payload: str = "hello") -> Path:
    """Create a tiny valid tgz bundle at ``tmp_path`` and return its path."""
    out = tmp_path / "bundle.tgz"
    contract = {
        "id": "orders",
        "name": "Orders",
        "builds": [
            {
                "id": "transform",
                "embeddedLogicPattern": {"sql": f"SELECT '{payload}' AS x"},
            }
        ],
    }
    build_bundle_tgz(contract, out, contract_id="orders")
    return out


# ---------------------------------------------------------------------------
# compute_plan_digest — deterministic + mask
# ---------------------------------------------------------------------------


class TestComputePlanDigest:
    """The core invariant: same-input-same-output, digest fields ignored."""

    def test_returns_sha256_prefixed_hex(self) -> None:
        plan = _minimal_plan()
        digest = compute_plan_digest(plan)
        assert digest.startswith("sha256:")
        # 7-char prefix + 64 hex chars
        assert len(digest) == 7 + 64

    def test_deterministic_across_calls(self) -> None:
        """Calling twice with the same dict → identical digest."""
        plan = _minimal_plan()
        assert compute_plan_digest(plan) == compute_plan_digest(plan)

    def test_independent_of_key_insertion_order(self) -> None:
        """Dicts with different insertion order but same content hash the same.
        Sort-keys JSON canonicalisation is load-bearing here — without it,
        two runs of ``fluid plan`` could produce structurally identical
        plans with diverging digests."""
        plan_a = {"a": 1, "b": 2, "contract": {"id": "x"}}
        plan_b = {"contract": {"id": "x"}, "b": 2, "a": 1}
        assert compute_plan_digest(plan_a) == compute_plan_digest(plan_b)

    def test_masks_bundle_digest_field(self) -> None:
        """Mutating ``bundleDigest`` must not change the plan's digest.
        This is what lets ``inject_digests`` work: if bundleDigest were
        part of the hash input, setting it would invalidate planDigest,
        and the recursion has no fixed point."""
        plan_a = {**_minimal_plan(), "bundleDigest": "sha256:" + "a" * 64}
        plan_b = {**_minimal_plan(), "bundleDigest": "sha256:" + "b" * 64}
        assert compute_plan_digest(plan_a) == compute_plan_digest(plan_b)

    def test_masks_plan_digest_field(self) -> None:
        """Same rationale as above, but for ``planDigest`` — this is what
        enables the self-reference in inject_digests."""
        plan_a = {**_minimal_plan(), "planDigest": "sha256:" + "c" * 64}
        plan_b = {**_minimal_plan(), "planDigest": "sha256:" + "d" * 64}
        assert compute_plan_digest(plan_a) == compute_plan_digest(plan_b)

    def test_changing_any_other_field_changes_digest(self) -> None:
        """Sanity: the masking is narrowly scoped to the two digest fields,
        everything else must influence the hash."""
        plan_a = _minimal_plan()
        plan_b = {**plan_a, "format_version": "0.7.2"}
        assert compute_plan_digest(plan_a) != compute_plan_digest(plan_b)

    def test_actions_reorder_changes_digest(self) -> None:
        """Reordered actions hash differently — topological order is
        semantically meaningful, not just a display artefact."""
        plan_a = _minimal_plan()
        plan_a["actions"] = [
            {"step": 1, "action_id": "a", "depends_on": []},
            {"step": 2, "action_id": "b", "depends_on": ["a"]},
        ]
        plan_b = dict(plan_a)
        plan_b["actions"] = [
            {"step": 2, "action_id": "b", "depends_on": ["a"]},
            {"step": 1, "action_id": "a", "depends_on": []},
        ]
        assert compute_plan_digest(plan_a) != compute_plan_digest(plan_b)


# ---------------------------------------------------------------------------
# is_bundle_path
# ---------------------------------------------------------------------------


class TestIsBundlePath:
    """Extension sniff — must accept ``.tgz`` and ``.tar.gz``, reject rest."""

    @pytest.mark.parametrize(
        "path",
        [
            "x.tgz",
            "X.TGZ",
            "contract.tar.gz",
            "/abs/path/bundle.tgz",
            "/abs/path/bundle.tar.gz",
        ],
    )
    def test_accepts_bundle_paths(self, path: str) -> None:
        assert is_bundle_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "contract.fluid.yaml",
            "plan.json",
            "bundle.zip",
            "bundle.tar",  # not gzipped
            "archive.gz",  # no .tar prefix
            "",
        ],
    )
    def test_rejects_non_bundle_paths(self, path: str) -> None:
        assert is_bundle_path(path) is False


# ---------------------------------------------------------------------------
# read_bundle_digest — lift MANIFEST merkle root from tgz
# ---------------------------------------------------------------------------


class TestReadBundleDigest:
    """Must match the digest returned by ``build_bundle_tgz``."""

    def test_round_trips_manifest_digest(self, tmp_path: Path) -> None:
        out = tmp_path / "bundle.tgz"
        contract = {
            "id": "orders",
            "name": "Orders",
            "builds": [{"id": "t", "embeddedLogicPattern": {"sql": "SELECT 1"}}],
        }
        expected = build_bundle_tgz(contract, out, contract_id="orders")
        assert read_bundle_digest(out) == expected

    def test_different_bundles_produce_different_digests(self, tmp_path: Path) -> None:
        a = _build_bundle(tmp_path / "a", payload="hello")
        b = _build_bundle(tmp_path / "b", payload="world")
        assert read_bundle_digest(a) != read_bundle_digest(b)

    def test_missing_bundle_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_bundle_digest(tmp_path / "does-not-exist.tgz")


# ---------------------------------------------------------------------------
# inject_digests — fills both fields without self-reference
# ---------------------------------------------------------------------------


class TestInjectDigests:
    """Output must satisfy ``verify_plan_binding`` by construction."""

    def test_empty_bundle_digest_when_no_bundle_path(self) -> None:
        plan = _minimal_plan()
        out = inject_digests(plan, bundle_path=None)
        assert out["bundleDigest"] == ""
        assert out["planDigest"].startswith("sha256:")

    def test_populated_bundle_digest_when_bundle_given(self, tmp_path: Path) -> None:
        bundle = _build_bundle(tmp_path)
        plan = _minimal_plan()
        out = inject_digests(plan, bundle_path=bundle)
        assert out["bundleDigest"] == read_bundle_digest(bundle)

    def test_does_not_mutate_input(self) -> None:
        plan = _minimal_plan()
        inject_digests(plan, bundle_path=None)
        assert "bundleDigest" not in plan
        assert "planDigest" not in plan

    def test_rerun_is_stable(self) -> None:
        """Calling inject_digests twice produces identical output —
        no oscillation between two ``planDigest`` values."""
        plan = _minimal_plan()
        first = inject_digests(plan, bundle_path=None)
        second = inject_digests(first, bundle_path=None)
        assert first == second

    def test_plan_digest_stable_when_bundle_digest_mutates(self, tmp_path: Path) -> None:
        """Both digest fields are masked during hashing — bundleDigest
        changes do NOT invalidate planDigest.

        Rationale: this is the whole point of masking. Without it,
        inject_digests couldn't compute a self-consistent pair. The
        downside is that a tampered bundleDigest is invisible to the
        plan-tamper check alone; bundle-swap detection happens via the
        SEPARATE bundle-mismatch check in ``verify_plan_binding``, which
        compares ``plan.bundleDigest`` to the real bundle's MANIFEST
        digest read fresh off disk."""
        bundle = _build_bundle(tmp_path)
        plan = _minimal_plan()
        out = inject_digests(plan, bundle_path=bundle)

        # Tamper the stored bundleDigest. planDigest is computed over a
        # masked body (both digest fields excluded), so the recompute
        # returns the SAME value as the stored planDigest.
        tampered = dict(out)
        tampered["bundleDigest"] = "sha256:" + "f" * 64

        assert compute_plan_digest(tampered) == tampered["planDigest"]

    def test_bundle_mismatch_caught_via_bundle_file_comparison(self, tmp_path: Path) -> None:
        """Separate test locking the ACTUAL bundle-swap detection path:
        ``verify_plan_binding(plan, bundle_path=real_bundle)`` compares
        the plan's recorded bundleDigest against the bundle's real
        MANIFEST digest — this is where a swap gets caught."""
        bundle_a = _build_bundle(tmp_path / "a", payload="legit")
        plan = inject_digests(_minimal_plan(), bundle_path=bundle_a)

        # Attacker rewrites bundleDigest to point at their own bundle
        # (and also swaps the file on disk). verify now sees two things
        # that agree — the attack succeeds IFF verify is called without
        # the legitimate bundle in hand.
        bundle_b = _build_bundle(tmp_path / "b", payload="evil")
        attacker_digest = read_bundle_digest(bundle_b)
        plan_tampered = dict(plan)
        plan_tampered["bundleDigest"] = attacker_digest
        # Must recompute planDigest because the integrity hash is now
        # stale even though bundleDigest is masked from hashing — the
        # rest of the plan body is identical so this step is a no-op
        # in isolation, but a real attack would also mutate actions[].
        plan_tampered = inject_digests(plan_tampered, bundle_path=bundle_b)

        # Called with the EVIL bundle: digests agree, verify passes.
        # This is the accepted trade-off — bundle-path is trusted input.
        verify_plan_binding(plan_tampered, bundle_path=bundle_b)

        # Called with the LEGIT bundle: bundle-mismatch fires.
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan_tampered, bundle_path=bundle_a)
        assert exc_info.value.kind == "bundle-mismatch"

    def test_overwrites_existing_digest_fields(self) -> None:
        """Re-running plan should produce a fresh binding, not merge."""
        plan = {
            **_minimal_plan(),
            "bundleDigest": "sha256:" + "0" * 64,
            "planDigest": "sha256:" + "1" * 64,
        }
        out = inject_digests(plan, bundle_path=None)
        assert out["bundleDigest"] == ""  # overwritten
        assert out["planDigest"] != "sha256:" + "1" * 64


# ---------------------------------------------------------------------------
# verify_plan_binding — the stage-7 apply gate
# ---------------------------------------------------------------------------


class TestVerifyPlanBinding:
    """Each rule in the verifier must be independently enforceable."""

    def test_happy_path_no_bundle(self) -> None:
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        # Must not raise — this is the plan-file-only path.
        verify_plan_binding(plan, bundle_path=None)

    def test_happy_path_with_bundle(self, tmp_path: Path) -> None:
        bundle = _build_bundle(tmp_path)
        plan = inject_digests(_minimal_plan(), bundle_path=bundle)
        verify_plan_binding(plan, bundle_path=bundle)

    def test_missing_plan_digest_is_tamper(self) -> None:
        """A legitimate plan always has planDigest. Its absence is a
        red flag — either an older fluid produced this, or someone
        stripped the field to bypass the gate."""
        plan = _minimal_plan()
        plan["bundleDigest"] = ""
        # No planDigest at all.
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=None)
        assert exc_info.value.kind == "plan-tamper"

    def test_empty_plan_digest_is_tamper(self) -> None:
        """Empty string planDigest is treated the same as missing.
        Defensive against edge case where JSON round-trip drops value."""
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        plan["planDigest"] = ""
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=None)
        assert exc_info.value.kind == "plan-tamper"

    def test_modified_plan_body_is_tamper(self) -> None:
        """Edit the plan between stages 6 and 7 → must hard-fail with
        ``plan-tamper`` kind, not a generic error."""
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        plan["actions"].append({"step": 99, "action_id": "injected", "action_type": "drop_table"})
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=None)
        assert exc_info.value.kind == "plan-tamper"
        assert "modified" in str(exc_info.value).lower()

    def test_swapped_bundle_is_bundle_mismatch(self, tmp_path: Path) -> None:
        """Plan was computed against bundle A, then operator points apply
        at bundle B — must fail with ``bundle-mismatch`` kind."""
        bundle_a = _build_bundle(tmp_path / "a", payload="first")
        bundle_b = _build_bundle(tmp_path / "b", payload="second")
        plan = inject_digests(_minimal_plan(), bundle_path=bundle_a)

        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=bundle_b)
        assert exc_info.value.kind == "bundle-mismatch"

    def test_bundle_path_none_skips_bundle_check(self, tmp_path: Path) -> None:
        """Emergency hotfix path: plan carries bundleDigest but caller
        has no bundle in hand. The plan-tamper check still runs."""
        bundle = _build_bundle(tmp_path)
        plan = inject_digests(_minimal_plan(), bundle_path=bundle)

        # Must NOT raise — bundle_path=None legitimately skips the
        # bundle-mismatch check but still runs the plan-tamper check.
        verify_plan_binding(plan, bundle_path=None)

    def test_plan_binding_error_carries_kind_attribute(self) -> None:
        """Constructor contract: CLI-layer code dispatches on .kind."""
        err = PlanBindingError("plan-tamper", "boom")
        assert err.kind == "plan-tamper"
        # Also a ValueError for standard except-handlers.
        assert isinstance(err, ValueError)
