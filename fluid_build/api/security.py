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

"""Supply-chain image-signature verification + sovereignty enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class SignatureResult:
    image_ref: str
    signed: bool
    public_key: Optional[str] = None
    slsa_provenance_present: bool = False
    digest: Optional[str] = None
    error: Optional[str] = None


class ImageSignatureVerifier(Protocol):
    """Cosign-style image signature verifier. Implementations may shell out to
    the ``cosign`` CLI or use ``sigstore-python`` in-process.
    """

    def verify(
        self,
        image_ref: str,
        public_key: str,
        require_slsa_provenance: bool = False,
    ) -> SignatureResult: ...


@dataclass(frozen=True)
class SovereigntyViolation:
    rule: str
    detail: str
    actor: str  # "plan" | "runtime"


class SovereigntyChecker(Protocol):
    """Plan-time + runtime jurisdiction / data-residency enforcement."""

    def check_plan(self, contract: Dict[str, Any]) -> List[SovereigntyViolation]: ...

    def check_runtime(
        self,
        contract: Dict[str, Any],
        source_region: Optional[str],
        destination_region: Optional[str],
    ) -> List[SovereigntyViolation]: ...
