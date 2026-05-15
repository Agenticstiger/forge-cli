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


def _rewrite_bundle(src: Path, dst: Path, *, mutate) -> None:
    """Copy a tgz bundle, applying ``mutate(name, data) -> data | None`` to
    each member. Returning ``None`` drops the member. Used to forge
    corrupt bundles for the J5/J8 negative tests.
    """
    import gzip
    import io
    import tarfile

    out_buf = io.BytesIO()
    with tarfile.open(src, "r:gz") as tin, tarfile.open(fileobj=out_buf, mode="w") as tout:
        for member in tin.getmembers():
            if not member.isfile():
                tout.addfile(member)
                continue
            fh = tin.extractfile(member)
            data = fh.read() if fh else b""
            new_data = mutate(member.name, data)
            if new_data is None:
                continue
            info = tarfile.TarInfo(name=member.name)
            info.size = len(new_data)
            info.mode = member.mode
            tout.addfile(info, io.BytesIO(new_data))
    gz_buf = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(out_buf.getvalue())
    dst.write_bytes(gz_buf.getvalue())


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

    def test_missing_manifest_raises_classified_error(self, tmp_path: Path) -> None:
        """J8: a bundle with no MANIFEST.json must surface as the distinct
        ``bundle-manifest-missing`` kind — not blurred into the generic
        contents-mismatch case, so CI log parsers can classify it."""
        good = _build_bundle(tmp_path / "good")
        forged = tmp_path / "no-manifest.tgz"
        # Drop MANIFEST.json entirely.
        _rewrite_bundle(
            good,
            forged,
            mutate=lambda name, data: None if name == "MANIFEST.json" else data,
        )
        with pytest.raises(PlanBindingError) as exc_info:
            read_bundle_digest(forged)
        assert exc_info.value.kind == "bundle-manifest-missing"

    def test_tampered_member_raises_classified_invalid_error(self, tmp_path: Path) -> None:
        """J8: a bundle whose member bytes diverge from MANIFEST.json
        (but the manifest IS present) surfaces as ``bundle-manifest-invalid``
        — a different stable tag from the missing-manifest case."""
        good = _build_bundle(tmp_path / "good")
        forged = tmp_path / "tampered.tgz"
        # Mutate a non-manifest payload file so its SHA no longer matches.
        _rewrite_bundle(
            good,
            forged,
            mutate=lambda name, data: (
                data + b"\n-- injected" if name != "MANIFEST.json" else data
            ),
        )
        with pytest.raises(PlanBindingError) as exc_info:
            read_bundle_digest(forged)
        assert exc_info.value.kind == "bundle-manifest-invalid"

    def test_missing_and_invalid_have_distinct_tags(self, tmp_path: Path) -> None:
        """J8: the two failure modes must NOT collapse to the same kind —
        that distinction is the whole point of the finding."""
        good = _build_bundle(tmp_path / "good")
        no_manifest = tmp_path / "nm.tgz"
        tampered = tmp_path / "tm.tgz"
        _rewrite_bundle(good, no_manifest, mutate=lambda n, d: None if n == "MANIFEST.json" else d)
        _rewrite_bundle(good, tampered, mutate=lambda n, d: d + b"x" if n != "MANIFEST.json" else d)
        with pytest.raises(PlanBindingError) as nm_exc:
            read_bundle_digest(no_manifest)
        with pytest.raises(PlanBindingError) as tm_exc:
            read_bundle_digest(tampered)
        assert nm_exc.value.kind != tm_exc.value.kind

    def test_merkle_recompute_catches_forged_declared_digest(self, tmp_path: Path) -> None:
        """J5: defence-in-depth. If an attacker rewrites ONLY the declared
        ``digest`` field in MANIFEST.json (leaving per-file hashes intact
        so a naive check passes), the locally-recomputed merkle root must
        catch the divergence.

        ``validate_manifest`` already independently rejects this (its own
        merkle check), so the recompute is belt-and-braces — but the test
        pins that ``read_bundle_digest`` never returns a digest it has not
        itself reproduced from raw bytes."""
        import json as _json

        good = _build_bundle(tmp_path / "good")
        forged = tmp_path / "forged-digest.tgz"

        def _forge(name: str, data: bytes):
            if name != "MANIFEST.json":
                return data
            manifest = _json.loads(data.decode("utf-8"))
            # Rewrite ONLY the top-level merkle root; leave per-file
            # hashes untouched.
            manifest["digest"] = "sha256:" + "e" * 64
            return _json.dumps(manifest).encode("utf-8")

        _rewrite_bundle(good, forged, mutate=_forge)
        with pytest.raises(PlanBindingError) as exc_info:
            read_bundle_digest(forged)
        # Either layer-1 (validate_manifest's own merkle check) or layer-2
        # (the independent recompute) fires — both are merkle-class
        # failures and both must hard-fail. The recompute guarantees we
        # never trust a declared digest we did not reproduce.
        assert exc_info.value.kind in (
            "bundle-merkle-mismatch",
            "bundle-manifest-invalid",
        )

    def test_returned_digest_equals_independent_recompute(self, tmp_path: Path) -> None:
        """J5: the value ``read_bundle_digest`` returns is provably the
        locally-recomputed merkle root — assert it matches a fresh
        ``build_manifest`` over the same member bytes."""
        import tarfile

        from fluid_build.forge.core.bundle import build_manifest

        bundle = _build_bundle(tmp_path)
        returned = read_bundle_digest(bundle)

        with tarfile.open(bundle, "r:gz") as tar:
            members = {
                m.name: tar.extractfile(m).read()
                for m in tar.getmembers()
                if m.isfile() and m.name != "MANIFEST.json"
            }
        assert returned == build_manifest(members)["digest"]


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
        """A forced re-inject over an already-bound plan produces
        identical output — no oscillation between two ``planDigest``
        values. (``force=True`` is required now that a bare re-inject is
        guarded against silent overwrite — see J7.)"""
        plan = _minimal_plan()
        first = inject_digests(plan, bundle_path=None)
        second = inject_digests(first, bundle_path=None, force=True)
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
        # ``force=True`` because the plan is already bound and the J7
        # guard would otherwise (correctly) refuse the silent re-sign;
        # an attacker re-binding a tampered plan is exactly the forced
        # overwrite the guard makes explicit.
        plan_tampered = inject_digests(plan_tampered, bundle_path=bundle_b, force=True)

        # Called with the EVIL bundle: digests agree, verify passes.
        # This is the accepted trade-off — bundle-path is trusted input.
        verify_plan_binding(plan_tampered, bundle_path=bundle_b)

        # Called with the LEGIT bundle: bundle-mismatch fires.
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan_tampered, bundle_path=bundle_a)
        assert exc_info.value.kind == "bundle-mismatch"

    def test_refuses_silent_overwrite_of_existing_digests(self) -> None:
        """J7: a plan that already carries non-empty digest fields has
        been bound once. ``inject_digests`` without ``force`` must refuse
        to re-sign it — silently re-binding could launder a mutated plan
        body past the stage-7 gate."""
        plan = {
            **_minimal_plan(),
            "bundleDigest": "sha256:" + "0" * 64,
            "planDigest": "sha256:" + "1" * 64,
        }
        with pytest.raises(ValueError) as exc_info:
            inject_digests(plan, bundle_path=None)
        assert "already carries digest fields" in str(exc_info.value)

    def test_refuses_overwrite_when_only_plan_digest_present(self) -> None:
        """The guard trips if EITHER digest field is non-empty, not just
        when both are."""
        plan = {**_minimal_plan(), "planDigest": "sha256:" + "1" * 64}
        with pytest.raises(ValueError):
            inject_digests(plan, bundle_path=None)

    def test_force_allows_deliberate_rebind(self) -> None:
        """J7: ``force=True`` is the deliberate-rebind escape hatch — used
        by callers that legitimately mutated the plan body after the first
        injection (e.g. ``fluid plan`` appending a cost estimate)."""
        plan = {
            **_minimal_plan(),
            "bundleDigest": "sha256:" + "0" * 64,
            "planDigest": "sha256:" + "1" * 64,
        }
        out = inject_digests(plan, bundle_path=None, force=True)
        assert out["bundleDigest"] == ""  # overwritten
        assert out["planDigest"] != "sha256:" + "1" * 64
        # The forced rebind produces a self-consistent binding.
        verify_plan_binding(out, bundle_path=None)

    def test_empty_digest_fields_do_not_trip_the_guard(self) -> None:
        """A plan carrying empty-string digest fields (the raw-yaml shape)
        is NOT considered already-bound — first injection still works
        without ``force``."""
        plan = {**_minimal_plan(), "bundleDigest": "", "planDigest": ""}
        out = inject_digests(plan, bundle_path=None)
        assert out["planDigest"].startswith("sha256:")


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
        plan["bindingMode"] = "raw"
        # No planDigest at all.
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=None)
        assert exc_info.value.kind == "plan-tamper"

    def test_inject_digests_records_binding_mode(self, tmp_path: Path) -> None:
        """J6: inject_digests stamps bindingMode (bound vs raw)."""
        raw = inject_digests(_minimal_plan(), bundle_path=None)
        assert raw["bindingMode"] == "raw"
        bound = inject_digests(_minimal_plan(), bundle_path=_build_bundle(tmp_path))
        assert bound["bindingMode"] == "bound"

    def test_binding_mode_mismatch_catches_stripped_bundle_digest(self, tmp_path: Path) -> None:
        """J6: blanking bundleDigest to skip the bundle check — bundleDigest
        is masked from planDigest, so the plan-tamper check alone would NOT
        catch it — is caught by the bindingMode consistency check."""
        plan = inject_digests(_minimal_plan(), bundle_path=_build_bundle(tmp_path))
        assert plan["bindingMode"] == "bound"
        # Attacker strips bundleDigest to skip the bundle half of the gate.
        plan["bundleDigest"] = ""
        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=None)
        assert exc_info.value.kind == "binding-mode-mismatch"

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

    def test_bundle_path_none_fails_closed_when_digest_present(self, tmp_path: Path) -> None:
        """Plan carries a bundleDigest but the caller supplies no bundle —
        the gate now fails closed (``bundle-missing``) instead of silently
        skipping the bundle half of the binding. That silent skip was the
        apply-time bypass closed by finding J1."""
        bundle = _build_bundle(tmp_path)
        plan = inject_digests(_minimal_plan(), bundle_path=bundle)

        with pytest.raises(PlanBindingError) as exc_info:
            verify_plan_binding(plan, bundle_path=None)
        assert exc_info.value.kind == "bundle-missing"

    def test_raw_plan_with_no_bundle_digest_skips_bundle_check(self, tmp_path: Path) -> None:
        """A plan built against a raw .fluid.yaml carries an empty
        bundleDigest — bundle_path=None is then legitimate; the bundle
        check is skipped and only the plan-tamper check runs."""
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        assert plan["bundleDigest"] == ""

        # Must NOT raise — no bundle was ever bound to this plan.
        verify_plan_binding(plan, bundle_path=None)

    def test_plan_binding_error_carries_kind_attribute(self) -> None:
        """Constructor contract: CLI-layer code dispatches on .kind."""
        err = PlanBindingError("plan-tamper", "boom")
        assert err.kind == "plan-tamper"
        # Also a ValueError for standard except-handlers.
        assert isinstance(err, ValueError)
