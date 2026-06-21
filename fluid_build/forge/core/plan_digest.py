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
- ``planDigest``  — SHA-256 over the plan body itself (with the derived
  digest fields and the volatile ``generated_at`` timestamp masked out),
  canonicalized via sorted-keys JSON. Catches tampering of the plan file
  between stages 6 and 7, while staying identical across two identical runs.

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
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

# Plan fields excluded from the planDigest hash input.
#
# ``bundleDigest`` / ``planDigest`` are *derived* values (``planDigest`` IS
# the hash) — including them would create a self-referential dependency with
# no fixed point.
_DIGEST_FIELDS = ("bundleDigest", "planDigest")

# Volatile metadata fields legitimately differ between two otherwise-identical
# ``fluid plan`` runs, so hashing them makes ``planDigest`` non-deterministic
# and breaks the "apply consumes the exact approved plan" guarantee.
# ``generated_at`` is the wall-clock time the plan was produced
# (``cli/plan.py`` stamps ``time.time()``); it is audit metadata, not plan
# content, and is still written to ``plan.json`` — just masked out of the hash.
_VOLATILE_FIELDS = ("generated_at",)

# Everything masked out of the planDigest input.
_NON_DIGEST_FIELDS = frozenset(_DIGEST_FIELDS + _VOLATILE_FIELDS)


def _nfc_normalise(obj: Any) -> Any:
    """Recursively apply Unicode NFC normalisation to every string in a
    plan structure. Without it, two Unicode-equivalent encodings of the
    same text (composed vs decomposed accents) canonicalise to different
    bytes and produce different digests — a spurious plan-tamper verdict.
    """
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, dict):
        return {_nfc_normalise(k): _nfc_normalise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nfc_normalise(x) for x in obj]
    return obj


def coerce_keys_to_str(obj: Any) -> Any:
    """Recursively coerce every non-``str`` dict key to ``str``.

    ``json.dumps(..., sort_keys=True)`` cannot sort a dict whose keys mix
    types (``TypeError: '<' not supported between instances of 'bool' and
    'str'``). PyYAML parses the YAML magic words ``on``/``off``/``yes``/
    ``no`` (and bare numerics) as Python ``bool``/``int`` keys when they
    appear inside an ``additionalProperties: true`` block, so a contract
    can legitimately produce such a dict. Coercing keys to strings before
    serialisation keeps both the idempotent plan write and the plan
    digest deterministic — and a ``bool``/``int`` key already round-trips
    through JSON as a string anyway, so this only makes explicit what the
    encoder would do.
    """
    if isinstance(obj, dict):
        return {
            (k if isinstance(k, str) else str(k)): coerce_keys_to_str(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [coerce_keys_to_str(x) for x in obj]
    return obj


def compute_plan_digest(plan: Dict[str, Any]) -> str:
    """SHA-256 over the plan body, canonicalised so it is byte-stable.

    This digest is the plan-binding primitive ``fluid apply`` verifies, so it
    MUST be identical for two ``fluid plan`` runs over the same contract.
    Determinism rests on three things:

    - ``json.dumps(sort_keys=True, separators=(",", ":"))`` — key order and
      whitespace cannot perturb the bytes. Matches the Phase-2 bundle
      MANIFEST algorithm so operators can reproduce the hash with a single
      ``jq`` + ``sha256sum`` pipeline.
    - Unicode NFC normalisation of every string, so composed/decomposed
      accents canonicalise identically.
    - Two field classes are masked OUT of the hash input
      (``_NON_DIGEST_FIELDS``): the *derived* digest fields
      (``bundleDigest`` / ``planDigest`` — self-referential) and *volatile*
      metadata (``generated_at`` — a wall-clock timestamp that differs every
      run). Everything ELSE is part of the hash — including the **order** of
      ``actions`` — so action order must itself be deterministic upstream
      (see ``ProviderActionParser.get_execution_order``).

    Returns a ``sha256:<hex>`` string (64 hex chars after the prefix).
    """
    stripped = _nfc_normalise({k: v for k, v in plan.items() if k not in _NON_DIGEST_FIELDS})
    # Coerce non-str keys to str BEFORE sort_keys serialisation: a contract
    # carrying a YAML magic-word key (on/off/yes/no) parses it as a Python
    # bool, and ``sort_keys=True`` cannot order a mixed bool/str key set.
    stripped = coerce_keys_to_str(stripped)
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_bundle_digest(tgz_path: Path) -> str:
    """Extract — and independently re-verify — the MANIFEST merkle root.

    Three layers of defence, each producing a *distinct, stable* failure
    so CI log parsers can classify the cause (J8):

    1. ``validate_manifest`` runs first as the canonical tamper gate. Its
       failures are generic ``ValueError`` strings; we re-raise them as a
       typed :class:`PlanBindingError` with one of two stable ``kind``
       tags so a missing-manifest case never blurs into a hash-mismatch
       case:

       - ``"bundle-manifest-missing"`` — the archive has no
         ``MANIFEST.json`` (or the archive itself is unreadable).
       - ``"bundle-manifest-invalid"`` — the manifest is present but the
         archive contents diverge from it (per-file SHA, extra/missing
         files, or the declared merkle root).

    2. The merkle root is then recomputed *locally and independently*
       using the same per-file-SHA-then-merkle algorithm
       ``validate_manifest`` / ``build_manifest`` use, and cross-checked
       against the value declared inside ``MANIFEST.json``. This is
       defence-in-depth: ``validate_manifest`` already proved consistency
       in step 1, so a divergence here means the algorithm or the
       manifest format drifted between modules — surfaced explicitly as
       ``PlanBindingError("bundle-merkle-mismatch", ...)`` rather than
       silently trusting the declared field.

    Returns the ``sha256:<hex>`` digest recorded in MANIFEST.json (which,
    by the time we return, has been proven to equal the recomputed root).
    Caller is responsible for handling ``FileNotFoundError`` when the path
    is missing entirely.
    """
    # Lazy import — avoid circular dep at module load time.
    import tarfile

    from fluid_build.forge.core.bundle import (
        build_manifest,
        read_tar_member_bounded,
        validate_manifest,
    )

    # --- Layer 1: canonical tamper gate, with classified failures (J8) ---
    try:
        validate_manifest(tgz_path)  # raises ValueError on any mismatch
    except FileNotFoundError:
        # Path missing entirely — let it propagate per the contract.
        raise
    except ValueError as exc:
        msg = str(exc)
        # ``validate_manifest`` collapses every problem into a bare
        # ValueError. Differentiate "no manifest at all" from "manifest
        # present but contents drifted" so the two are independently
        # greppable. The substrings keyed on here are stable parts of
        # ``validate_manifest``'s own error strings ("MANIFEST.json
        # missing from ..."); guard with a broad fallback so a future
        # message reword degrades to the generic-invalid tag rather
        # than crashing.
        if "MANIFEST.json missing" in msg:
            raise PlanBindingError(
                "bundle-manifest-missing",
                f"bundle {tgz_path} has no MANIFEST.json — it is not a "
                f"valid fluid bundle (or was truncated): {msg}",
            ) from exc
        raise PlanBindingError(
            "bundle-manifest-invalid",
            f"bundle {tgz_path} failed manifest verification "
            f"(contents diverge from MANIFEST.json): {msg}",
        ) from exc

    with tarfile.open(tgz_path, "r:gz") as tar:
        manifest = json.loads(read_tar_member_bounded(tar, "MANIFEST.json").decode("utf-8"))
        declared_files = manifest.get("files") or {}
        # Re-read each declared member so we can recompute the merkle
        # root from raw bytes, independent of the digest field.
        member_bytes: Dict[str, bytes] = {
            path: read_tar_member_bounded(tar, path) for path in declared_files
        }

    declared_digest = manifest.get("digest", "")
    if not isinstance(declared_digest, str) or not declared_digest.startswith("sha256:"):
        raise PlanBindingError(
            "bundle-manifest-invalid",
            f"MANIFEST.json at {tgz_path} has malformed digest: {declared_digest!r}",
        )

    # --- Layer 2: independent local merkle recompute + cross-check (J5) ---
    # ``build_manifest`` is the single source of truth for the
    # per-file-SHA-then-merkle algorithm; reusing it (rather than
    # re-implementing the hash chain inline) keeps this check from going
    # stale if the algorithm is ever revised.
    recomputed_digest = build_manifest(member_bytes).get("digest", "")
    if recomputed_digest != declared_digest:
        raise PlanBindingError(
            "bundle-merkle-mismatch",
            f"locally recomputed merkle root for {tgz_path} "
            f"({recomputed_digest!r}) does not match the value declared "
            f"in its MANIFEST.json ({declared_digest!r}). The bundle's "
            f"declared digest cannot be trusted — re-build the bundle.",
        )
    return declared_digest


def is_bundle_path(path: str) -> bool:
    """True when a path looks like a Phase-2 bundle (.tgz / .tar.gz)."""
    lowered = str(path).lower()
    return lowered.endswith(".tgz") or lowered.endswith(".tar.gz")


def inject_digests(
    plan: Dict[str, Any],
    *,
    bundle_path: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Return ``plan`` with ``bundleDigest`` + ``planDigest`` populated.

    Input dict is not mutated. Digest order matters: compute ``planDigest``
    AFTER ``bundleDigest`` is set so the digest reflects the bundle binding.

    ``bundle_path=None`` → ``bundleDigest`` is empty string. Plan against
    raw ``.fluid.yaml`` has no bundle to pin; only ``planDigest`` is
    meaningful in that case.

    Overwrite guard (J7). A plan that *already* carries a non-empty
    ``bundleDigest`` or ``planDigest`` has been bound once. Silently
    re-binding it is a footgun: it would mint a fresh, valid-looking
    signature over a body that may have been mutated since the first
    binding — laundering a tampered plan past the stage-7 gate. So:

    - ``force=False`` (default): if either digest field is already
      non-empty, raise :class:`ValueError`. This is the safe default for
      first-time binding.
    - ``force=True``: re-bind unconditionally. Use this only when the
      caller has *legitimately and deliberately* mutated the plan body
      after the first injection and needs the digest to catch up — e.g.
      ``cli/plan.py`` appending a ``cost_estimate`` block. The caller
      owns the correctness of that mutation.
    """
    already_bound = bool(plan.get("bundleDigest")) or bool(plan.get("planDigest"))
    if already_bound and not force:
        raise ValueError(
            "inject_digests: plan already carries digest fields "
            f"(bundleDigest={plan.get('bundleDigest')!r}, "
            f"planDigest={plan.get('planDigest')!r}). Refusing to "
            "silently overwrite an existing plan binding — that would "
            "re-sign a possibly-mutated plan body. Pass force=True only "
            "if you deliberately mutated the plan after the first "
            "injection and need the digest recomputed."
        )
    out = dict(plan)
    out["bundleDigest"] = read_bundle_digest(bundle_path) if bundle_path else ""
    # bindingMode (J6): make the raw-vs-bound decision explicit and
    # tamper-evident. It is NOT a digest field, so it IS folded into
    # planDigest below — an attacker who blanks ``bundleDigest`` to skip
    # the bundle check (bundleDigest is masked out of planDigest) cannot
    # also flip bindingMode to "raw" without breaking the plan-tamper
    # check. ``verify_plan_binding`` rejects a bundleDigest/bindingMode
    # contradiction.
    out["bindingMode"] = "bound" if bundle_path else "raw"
    out["planDigest"] = compute_plan_digest(out)
    return out


# ---------------------------------------------------------------------------
# Verification (stage-7 apply gate)
# ---------------------------------------------------------------------------


class PlanBindingError(ValueError):
    """Raised when a plan.json's digests don't match the bundle or don't
    match the plan body itself. ``kind`` carries one of these stable tags
    (each is a distinct, greppable CI event — see J8):

    - ``"bundle-mismatch"`` — plan's ``bundleDigest`` disagrees with the
      bundle on disk. Either the bundle was swapped after plan ran, or the
      contract was re-bundled without re-running plan.
    - ``"bundle-missing"``  — plan carries a non-empty ``bundleDigest`` but
      no bundle was supplied to verify against. Fails closed rather than
      silently skipping the bundle half of the binding.
    - ``"bundle-manifest-missing"`` — the bundle archive has no
      ``MANIFEST.json`` (truncated / not a fluid bundle at all).
    - ``"bundle-manifest-invalid"`` — the bundle has a ``MANIFEST.json``
      but its contents (per-file SHA / file set / declared merkle) do not
      verify, or the declared digest field is malformed.
    - ``"bundle-merkle-mismatch"`` — the merkle root recomputed locally
      from the bundle's raw bytes disagrees with the root declared inside
      its ``MANIFEST.json`` (defence-in-depth recompute, see J5).
    - ``"binding-mode-missing"`` — plan has no ``bindingMode`` field
      (older fluid, or manually edited).
    - ``"binding-mode-invalid"`` — ``bindingMode`` is neither ``"bound"``
      nor ``"raw"``.
    - ``"binding-mode-mismatch"`` — ``bindingMode`` contradicts the
      presence/absence of ``bundleDigest`` — e.g. a ``"bound"`` plan whose
      ``bundleDigest`` was stripped to skip the bundle check (J6).
    - ``"plan-tamper"``     — plan's ``planDigest`` disagrees with the
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

    1. If plan carries a non-empty ``bundleDigest``: the bundle MUST be
       supplied and MUST match. ``bundle_path=None`` raises
       ``PlanBindingError("bundle-missing", ...)`` — failing closed rather
       than silently skipping the bundle half of the binding (that silent
       skip was the apply-time bypass this gate now closes). A digest
       mismatch raises ``PlanBindingError("bundle-mismatch", ...)``. A plan
       with an empty ``bundleDigest`` (built against a raw ``.fluid.yaml``)
       skips this check; the plan-tamper check still runs.

    2. Recompute ``planDigest`` over the plan body. Compare against the
       stored value. Mismatch → raise ``PlanBindingError("plan-tamper",
       ...)``. Missing or empty ``planDigest`` in the plan is treated as
       a tamper signal — legitimate plans always carry one.

    Called from ``cli/apply.py`` before any provider DDL.
    """
    expected_bundle = plan.get("bundleDigest", "")

    # 0. bindingMode consistency (J6). The mode is part of the plan body
    #    (folded into planDigest), so it cannot be flipped without
    #    tripping the plan-tamper check below. A mode that contradicts the
    #    presence/absence of bundleDigest means the plan was edited — in
    #    particular, blanking ``bundleDigest`` (which is masked out of
    #    planDigest) to skip the bundle check is caught here.
    binding_mode = plan.get("bindingMode", "")
    if not binding_mode:
        raise PlanBindingError(
            "binding-mode-missing",
            "plan.json has no bindingMode field. Re-generate via "
            "``fluid plan`` — this file was produced by an older version "
            "of fluid or was manually edited.",
        )
    if binding_mode not in ("bound", "raw"):
        raise PlanBindingError(
            "binding-mode-invalid",
            f"plan.json has an unrecognised bindingMode {binding_mode!r} "
            "(expected 'bound' or 'raw').",
        )
    if binding_mode == "bound" and not expected_bundle:
        raise PlanBindingError(
            "binding-mode-mismatch",
            "plan.json declares bindingMode='bound' but carries no "
            "bundleDigest — the bundle binding was stripped. Re-run "
            "``fluid plan``.",
        )
    if binding_mode == "raw" and expected_bundle:
        raise PlanBindingError(
            "binding-mode-mismatch",
            "plan.json declares bindingMode='raw' but carries a "
            "bundleDigest — inconsistent binding. Re-run ``fluid plan``.",
        )

    # 1. Bundle digest check — fail closed. A plan that carries a
    #    bundleDigest MUST be verified against that bundle; a missing
    #    bundle is no longer a silent skip (that was the apply-time bypass).
    if expected_bundle:
        if bundle_path is None:
            raise PlanBindingError(
                "bundle-missing",
                "plan.json carries a bundleDigest but no bundle was "
                "supplied to verify it against. Pass --bundle <path-to.tgz>, "
                "or --no-verify-plan-binding for an emergency apply.",
            )
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
            f"``--no-verify-plan-binding`` to force apply with an unverified "
            f"plan (emergency hotfix only; logged prominently).",
        )


__all__ = [
    "PlanBindingError",
    "coerce_keys_to_str",
    "compute_plan_digest",
    "inject_digests",
    "is_bundle_path",
    "read_bundle_digest",
    "verify_plan_binding",
]
