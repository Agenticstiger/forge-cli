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

"""V2 validator agent wrapper."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fluid_build.copilot.industry.pack import IndustryPack
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    ValidationFinding,
    ValidationReport,
)
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

# Item 5 — claims with confidence below this threshold get
# escalated to ``severity="warning"`` findings so the operator
# sees them in the validation report.
LOW_CONFIDENCE_THRESHOLD = 0.50


class ValidatorAgent:
    def __init__(self) -> None:
        self._validator = FluidContractValidator()

    def run(
        self,
        *,
        logical: Optional[LogicalDraft] = None,
        contract: Optional[Dict[str, Any]] = None,
        industry_pack: Optional[IndustryPack] = None,
        scratchpad: Optional[Any] = None,
    ) -> ValidationReport:
        report = self._validator.validate(
            logical=logical,
            contract=contract,
            industry_pack=industry_pack,
        )
        # Item 5 — escalate low-confidence claims as warnings so
        # operators see them in the validation report and the cost
        # summary footer.
        if scratchpad is not None:
            try:
                self._escalate_low_confidence(report, scratchpad)
            except Exception:  # pragma: no cover — defensive
                pass
        return report

    @staticmethod
    def _escalate_low_confidence(
        report: ValidationReport,
        scratchpad: Any,
    ) -> None:
        """Append a ``warning`` finding per low-confidence claim."""
        get_annotations = getattr(scratchpad, "get_annotations", None)
        if not callable(get_annotations):
            return
        log = get_annotations()
        for path, ann in (log.by_path or {}).items():
            confidence = getattr(ann, "confidence", None)
            if not confidence or confidence.score >= LOW_CONFIDENCE_THRESHOLD:
                continue
            report.issues.append(
                ValidationFinding(
                    message=(
                        f"Low-confidence claim at {path!r} "
                        f"(score={confidence.score:.2f}): "
                        f"{confidence.rationale or 'no rationale recorded'}"
                    ),
                    severity="warning",
                    field=path,
                )
            )
