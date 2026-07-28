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

"""Container image signature verification (Cosign-compatible).

Two implementations:
- ``CosignCliVerifier`` — shells out to ``cosign verify``. Production path.
- ``NullVerifier`` — used in tests / dev to assert the call sites without
  requiring cosign on the host. Logs a warning so the absence is visible.

Both satisfy ``api.security.ImageSignatureVerifier``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from fluid_build.api.security import ImageSignatureVerifier, SignatureResult

LOG = logging.getLogger("fluid.acquire.signature")


class CosignNotInstalledError(RuntimeError):
    """Raised when Cosign verification is required but the binary is missing."""


@dataclass
class CosignCliVerifier(ImageSignatureVerifier):
    """Production verifier — invokes ``cosign verify`` as a subprocess."""

    binary: str = "cosign"
    timeout_seconds: int = 30

    def verify(
        self,
        image_ref: str,
        public_key: str,
        require_slsa_provenance: bool = False,
    ) -> SignatureResult:
        if shutil.which(self.binary) is None:
            raise CosignNotInstalledError(
                f"Cosign binary '{self.binary}' not found on PATH. "
                "Install via `brew install cosign` or pin a SLSA-provenance image."
            )
        cmd = [self.binary, "verify", "--key", public_key, image_ref]
        try:
            r = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return SignatureResult(image_ref=image_ref, signed=False, error=f"timeout: {exc}")
        if r.returncode != 0:
            return SignatureResult(
                image_ref=image_ref,
                signed=False,
                public_key=public_key,
                error=(r.stderr or r.stdout).strip()[:512],
            )
        slsa_ok = True
        if require_slsa_provenance:
            slsa_ok = self._verify_slsa_provenance(image_ref, public_key)
        return SignatureResult(
            image_ref=image_ref,
            signed=True,
            public_key=public_key,
            slsa_provenance_present=slsa_ok,
        )

    def _verify_slsa_provenance(self, image_ref: str, public_key: str) -> bool:
        cmd = [
            self.binary,
            "verify-attestation",
            "--key",
            public_key,
            "--type",
            "slsaprovenance",
            image_ref,
        ]
        try:
            r = subprocess.run(
                cmd, check=False, capture_output=True, text=True, timeout=self.timeout_seconds
            )
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0


@dataclass
class NullVerifier(ImageSignatureVerifier):
    """Logs a warning and treats every image as signed. Test/dev only."""

    log_warnings: bool = True

    def verify(
        self,
        image_ref: str,
        public_key: str,
        require_slsa_provenance: bool = False,
    ) -> SignatureResult:
        if self.log_warnings:
            LOG.warning(
                "NullVerifier in use; image %s NOT actually verified. "
                "Use CosignCliVerifier in production.",
                image_ref,
            )
        return SignatureResult(
            image_ref=image_ref,
            signed=True,
            public_key=public_key,
            slsa_provenance_present=require_slsa_provenance,
        )


def make_default_verifier(*, allow_null: bool = False) -> ImageSignatureVerifier:
    """Factory that picks the right verifier for the current environment.

    By default this raises ``CosignNotInstalledError`` when the ``cosign``
    binary is not on PATH — silent fallback to ``NullVerifier`` is a
    supply-chain bypass: the runner would think every image was signed.

    Tests and dev environments that explicitly want the null path must pass
    ``allow_null=True``. Production callers should never set this.
    """
    if shutil.which("cosign") is not None:
        return CosignCliVerifier()
    if allow_null:
        return NullVerifier()
    raise CosignNotInstalledError(
        "Cosign binary not found on PATH. Install via `brew install cosign` "
        "or pass allow_null=True for explicit dev-mode bypass. Refusing to "
        "silently degrade to NullVerifier in production."
    )
