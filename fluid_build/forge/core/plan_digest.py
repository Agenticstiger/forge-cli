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

"""Plan-binding digests for ``fluid plan`` → ``fluid apply``.

The 11-stage pipeline's stage-6 → stage-7 boundary is the Terraform-style
"apply consumes exact plan" guarantee, enforced cryptographically via two
digest fields emitted into ``plan.json``:

- ``bundleDigest`` — SHA-256 merkle root of the input bundle's MANIFEST.
  When the plan is computed against a ``.tgz`` bundle, this pins the exact
  bundle. When the plan is computed against a raw ``.fluid.yaml``, this is
  the empty string (no bundle to pin).
- ``planDigest``  — SHA-256 over the plan body itself (with the two digest
  fields masked out), canonicalized via sorted-keys JSON. Catches
  tampering of the plan file between stages 6 and 7.

``fluid apply`` re-verifies both before any DDL. Mismatch → hard-fail
with a message pointing at the specific divergence ("bundle swap" vs
"plan tamper").

Two design decisions baked into the digest algorithm:

1. Sort keys + compact JSON separators when hashing. Deterministic across
   runs; matches the Phase-2 bundle MANIFEST hash algorithm so operators
   can verify externally with a single ``jq`` + ``sha256sum`` pipeline.

2. Both digest fields are masked out of the ``planDigest`` input. Self-
   referential hashing (hashing a dict that includes its own hash) is
   impossible, and the natural recursion has no fixed point. The masked
   form computes a stable hash regardless of insertion order.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

# Plan fields excluded from the planDigest hash input. Their values are
# derived from the hash or derived from the bundle, so including them
# would create self-referential dependencies.
_DIGEST_FIELDS = ("bundleDigest", "planDigest")


def compute_plan_digest(plan: Dict[str, Any]) -> str:
    """SHA-256 over the plan body with the digest fields masked out.

    Deterministic across runs: ``json.dumps(sort_keys=True, separators=...)``.
    Matches the canonical-JSON form used by Phase-2 bundle MANIFEST so
    external tools can reproduce the hash with standard utilities.

    Returns a ``sha256:<hex>`` string (64 hex chars after the prefix).
    """
    stripped = {k: v for k, v in plan.items() if k not in _DIGEST_FIELDS}
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_bundle_digest(tgz_path: Path) -> str:
    """Extract the MANIFEST merkle root from a Phase-2 bundle.

    Delegates to ``validate_manifest`` for the tamper-gate side effect —
    if the bundle is corrupt, this raises before returning. Keeps stages
    5/6/7 honest: plan can't compute against a bundle that already drifted
    from its declared contents.

    Returns the ``sha256:<hex>`` digest recorded in MANIFEST.json. Caller
    is responsible for handling ``FileNotFoundError`` when the path is
    missing entirely.
    """
    # Lazy import — avoid circular dep at module load time.
    import tarfile

    from fluid_build.forge.core.bundle import validate_manifest

    validate_manifest(tgz_path)  # raises on any mismatch

    with tarfile.open(tgz_path, "r:gz") as tar:
        member = tar.extractfile("MANIFEST.json")
        if member is None:
            raise ValueError(f"MANIFEST.json is not a regular file in {tgz_path}")
        manifest = json.loads(member.read().decode("utf-8"))

    digest = manifest.get("digest", "")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError(f"MANIFEST.json at {tgz_path} has malformed digest: {digest!r}")
    return digest


def is_bundle_path(path: str) -> bool:
    """True when a path looks like a Phase-2 bundle (.tgz / .tar.gz)."""
    lowered = str(path).lower()
    return lowered.endswith(".tgz") or lowered.endswith(".tar.gz")


def inject_digests(
    plan: Dict[str, Any],
    *,
    bundle_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return ``plan`` with ``bundleDigest`` + ``planDigest`` populated.

    Input dict is not mutated. Digest order matters: compute ``planDigest``
    AFTER ``bundleDigest`` is set so the digest reflects the bundle binding.
    The plan dict going in should NOT already have these fields — if it
    does, they're overwritten (not merged) so re-running plan produces a
    fresh binding.

    ``bundle_path=None`` → ``bundleDigest`` is empty string. Plan against
    raw ``.fluid.yaml`` has no bundle to pin; only ``planDigest`` is
    meaningful in that case.
    """
    out = dict(plan)
    out["bundleDigest"] = read_bundle_digest(bundle_path) if bundle_path else ""
    out["planDigest"] = compute_plan_digest(out)
    return out


# ---------------------------------------------------------------------------
# Verification (stage-7 apply gate)
# ---------------------------------------------------------------------------


class PlanBindingError(ValueError):
    """Raised when a plan.json's digests don't match the bundle or don't
    match the plan body itself. ``kind`` carries one of two stable tags:

    - ``"bundle-mismatch"`` — plan's ``bundleDigest`` disagrees with the
      bundle on disk. Either the bundle was swapped after plan ran, or the
      contract was re-bundled without re-running plan.
    - ``"plan-tamper"``    — plan's ``planDigest`` disagrees with the
      recomputed digest over the plan body. Someone edited ``plan.json``
      between stages 6 and 7.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def verify_plan_binding(
    plan: Dict[str, Any],
    *,
    bundle_path: Optional[Path] = None,
) -> None:
    """Hard-fail if plan's digests don't match actual inputs.

    Rules:

    1. If plan carries ``bundleDigest`` AND ``bundle_path`` is provided:
       recompute the bundle's merkle root, compare. Mismatch → raise
       ``PlanBindingError("bundle-mismatch", ...)``. A non-empty plan
       digest with ``bundle_path=None`` is NOT an error — caller may
       legitimately have invoked apply without the bundle (e.g. emergency
       hotfix against a pre-generated plan); the bundle portion of the
       binding is skipped in that case, but the plan-tamper check still
       runs.

    2. Recompute ``planDigest`` over the plan body. Compare against the
       stored value. Mismatch → raise ``PlanBindingError("plan-tamper",
       ...)``. Missing or empty ``planDigest`` in the plan is treated as
       a tamper signal — legitimate plans always carry one.

    Called from ``cli/apply.py`` before any provider DDL.
    """
    # 1. Bundle digest check (skipped when caller has no bundle in hand)
    expected_bundle = plan.get("bundleDigest", "")
    if expected_bundle and bundle_path is not None:
        actual_bundle = read_bundle_digest(bundle_path)
        if actual_bundle != expected_bundle:
            raise PlanBindingError(
                "bundle-mismatch",
                f"plan.json was computed against bundle "
                f"{expected_bundle!r} but {bundle_path} has digest "
                f"{actual_bundle!r}. Re-run ``fluid plan`` against the "
                f"current bundle before applying.",
            )

    # 2. Plan-body tamper check (always runs)
    stored_plan_digest = plan.get("planDigest", "")
    if not stored_plan_digest:
        raise PlanBindingError(
            "plan-tamper",
            "plan.json has no planDigest field. Re-generate via "
            "``fluid plan`` — this file was either produced by an older "
            "version of fluid or manually edited.",
        )
    recomputed = compute_plan_digest(plan)
    if recomputed != stored_plan_digest:
        raise PlanBindingError(
            "plan-tamper",
            f"plan.json has been modified since it was generated: stored "
            f"planDigest={stored_plan_digest!r}, recomputed={recomputed!r}. "
            f"Re-run ``fluid plan`` to produce a fresh binding, or pass "
            f"``--no-verify-digest`` to force apply with an unverified plan "
            f"(emergency hotfix only; logged prominently).",
        )


__all__ = [
    "PlanBindingError",
    "compute_plan_digest",
    "inject_digests",
    "is_bundle_path",
    "read_bundle_digest",
    "verify_plan_binding",
]
