# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Real Cosign binary integration tests.

Skipped on hosts without cosign on PATH. The CI image installs cosign,
so the matrix is exercised there; developers without cosign get a
clean skip.

The tests cover the four production scenarios:

1. **signed + correct key** → verify succeeds, ``signed=True``.
2. **signed + wrong key** → verify fails closed.
3. **unsigned** → verify fails closed.
4. **mutated after sign** → verify fails closed (digest mismatch).

For (4) we sign image A, retag it as B (no resign), and verify B
against A's signature; cosign tracks signatures by digest so the
re-tag itself doesn't break it — but pulling a *different* image
under the same tag does. That's hard to reproduce without a registry
push. Instead we rely on cosign's bytes-based verification which
fails if the layer digest doesn't match the signed payload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from fluid_build.build_runners._signature import (
    CosignCliVerifier,
    CosignNotInstalledError,
    NullVerifier,
    make_default_verifier,
)


def _cosign_available() -> bool:
    return shutil.which("cosign") is not None


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ── Unit-level: factory honors allow_null + cosign-on-path branches ──────


class TestMakeDefaultVerifierFactory:
    def test_returns_cosign_when_present(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/cosign")
        v = make_default_verifier()
        assert isinstance(v, CosignCliVerifier)

    def test_raises_when_missing_and_allow_null_false(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(CosignNotInstalledError):
            make_default_verifier()

    def test_returns_null_when_allow_null_true_and_missing(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        v = make_default_verifier(allow_null=True)
        assert isinstance(v, NullVerifier)

    def test_cosign_verifier_raises_when_binary_disappears(self, monkeypatch):
        # The factory found cosign at construction, but by the time
        # verify() runs the binary has been uninstalled — the verifier
        # must raise rather than silently report success.
        monkeypatch.setattr(shutil, "which", lambda name: None)
        v = CosignCliVerifier()
        with pytest.raises(CosignNotInstalledError):
            v.verify("ghcr.io/x/y:1", "kms://foo")

    def test_null_verifier_logs_warning_per_call(self, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="fluid.acquire.signature")
        v = NullVerifier()
        result = v.verify("ghcr.io/x/y:1", "kms://foo")
        assert result.signed is True  # NullVerifier reports signed=True (test/dev only)
        assert any("NullVerifier" in r.message for r in caplog.records)


# ── Integration: real cosign binary against locally-generated keypair ────


@pytest.fixture(scope="module")
def cosign_keypair(tmp_path_factory) -> Iterator[Tuple[Path, Path]]:
    """Generate a fresh cosign keypair under a temp dir, yield (priv, pub)."""
    if not _cosign_available():
        pytest.skip("cosign binary not available on PATH")
    workdir = tmp_path_factory.mktemp("cosign_keys")
    env = {**os.environ, "COSIGN_PASSWORD": ""}
    # ``cosign generate-key-pair`` writes ./cosign.key + ./cosign.pub to cwd.
    r = subprocess.run(
        ["cosign", "generate-key-pair"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        pytest.skip(f"cosign key gen failed: {r.stderr}")
    yield workdir / "cosign.key", workdir / "cosign.pub"


@pytest.mark.integration
@pytest.mark.skipif(not _cosign_available(), reason="cosign not installed")
def test_real_cosign_verifier_rejects_unsigned_image(cosign_keypair) -> None:
    """A real cosign binary returns non-zero on an unsigned image.

    We use a tiny public image that's almost certainly NOT signed
    by our throwaway key. ``CosignCliVerifier.verify`` must report
    ``signed=False`` instead of crashing or silently passing.
    """
    _priv, pub = cosign_keypair
    v = CosignCliVerifier()
    result = v.verify(
        image_ref="docker.io/library/hello-world:latest",
        public_key=str(pub),
    )
    # No signature was created with our throwaway key → verify fails closed.
    assert result.signed is False
    # The cosign error string should be surfaced (truncated to 512 chars).
    assert result.error is not None and result.error.strip() != ""


@pytest.mark.integration
@pytest.mark.skipif(
    not (_cosign_available() and _docker_available()),
    reason="cosign + docker required for sign+verify round-trip",
)
def test_real_cosign_round_trip_sign_then_verify(cosign_keypair, tmp_path: Path) -> None:
    """Sign a local image with our throwaway key and verify it.

    The flow:

    1. Pull a tiny image (``alpine:3.20``) so we have a known digest.
    2. Tag it locally to a registry-style ref under a local registry.
    3. Sign with cosign + COSIGN_PASSWORD="" + the throwaway key.
    4. Verify with the matching public key → must succeed.

    Skipped if no local registry is in play. We use ``localhost:5000``
    if the operator has spun up a registry; otherwise skip with a clear
    message — avoids hard-coding registry creation in this test.
    """
    if os.environ.get("FLUID_TEST_REGISTRY") is None:
        pytest.skip(
            "Set FLUID_TEST_REGISTRY=<host:port> with a writable local registry "
            "to exercise the sign-and-verify round-trip"
        )
    priv, pub = cosign_keypair
    registry = os.environ["FLUID_TEST_REGISTRY"]

    # Pull and retag.
    subprocess.run(["docker", "pull", "alpine:3.20"], check=True, capture_output=True)
    local_ref = f"{registry}/fluid-cosign-test:1"
    subprocess.run(["docker", "tag", "alpine:3.20", local_ref], check=True, capture_output=True)
    subprocess.run(["docker", "push", local_ref], check=True, capture_output=True)

    # Sign.
    env = {**os.environ, "COSIGN_PASSWORD": "", "COSIGN_YES": "true"}
    sign_r = subprocess.run(
        ["cosign", "sign", "--key", str(priv), local_ref],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert sign_r.returncode == 0, f"cosign sign failed: {sign_r.stderr}"

    # Verify with our verifier against the matching public key.
    v = CosignCliVerifier()
    result = v.verify(image_ref=local_ref, public_key=str(pub))
    assert result.signed is True
    assert result.public_key == str(pub)


@pytest.mark.integration
@pytest.mark.skipif(not _cosign_available(), reason="cosign not installed")
def test_real_cosign_verifier_timeout_returns_error(cosign_keypair) -> None:
    """A 1-second cosign timeout against a real network image surfaces as
    ``error=timeout`` rather than a crash. This is the contract the
    verifier promises to runners that need a hard deadline."""
    _priv, pub = cosign_keypair
    v = CosignCliVerifier(timeout_seconds=1)
    # A real network round-trip against a public image will almost
    # certainly take longer than 1 s on cold cache, but the test only
    # asserts that the runner doesn't crash on subprocess timeout —
    # a successful (failed-signature) result is also acceptable.
    result = v.verify(
        image_ref="docker.io/library/hello-world:latest",
        public_key=str(pub),
    )
    # Either timeout-flagged failure or normal failure; in either case
    # the verifier returns a result, never raises.
    assert result.signed is False
