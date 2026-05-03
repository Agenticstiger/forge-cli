# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Cosign / Sigstore signature simulation for tests.

Tests can dial in: signed image, unsigned image, wrong-key signed, signed-but-no-SLSA-provenance, signed-with-provenance. The mock fulfills the
``api.security.ImageSignatureVerifier`` Protocol so runners can be unit-tested
without a real cosign binary or KMS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from fluid_build.api.security import ImageSignatureVerifier, SignatureResult


@dataclass
class CosignMock(ImageSignatureVerifier):
    """A configurable mock verifier.

    ``signed_images`` maps image_ref -> public_key it was signed with.
    ``slsa_attestations`` maps image_ref -> bool (True if SLSA provenance present).

    On ``verify(image_ref, public_key, require_slsa_provenance)``:
    - returns signed=True when image is in signed_images AND public_key matches
    - returns signed=False otherwise
    - slsa_provenance_present reflects the attestation map
    """

    signed_images: Dict[str, str] = field(default_factory=dict)
    slsa_attestations: Dict[str, bool] = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def sign(self, image_ref: str, public_key: str, *, slsa: bool = False) -> None:
        """Pre-stage a signed image fixture."""
        self.signed_images[image_ref] = public_key
        if slsa:
            self.slsa_attestations[image_ref] = True

    def verify(
        self,
        image_ref: str,
        public_key: str,
        require_slsa_provenance: bool = False,
    ) -> SignatureResult:
        self.calls.append((image_ref, public_key, require_slsa_provenance))
        signed_key = self.signed_images.get(image_ref)
        if signed_key is None:
            return SignatureResult(
                image_ref=image_ref,
                signed=False,
                public_key=public_key,
                error="no signature found",
            )
        if signed_key != public_key:
            return SignatureResult(
                image_ref=image_ref,
                signed=False,
                public_key=public_key,
                error=f"signature was created with a different key (got={public_key}, signed_with={signed_key})",
            )
        slsa_ok = self.slsa_attestations.get(image_ref, False)
        if require_slsa_provenance and not slsa_ok:
            return SignatureResult(
                image_ref=image_ref,
                signed=False,
                public_key=public_key,
                error="SLSA provenance required but missing",
            )
        return SignatureResult(
            image_ref=image_ref,
            signed=True,
            public_key=public_key,
            slsa_provenance_present=slsa_ok,
        )
