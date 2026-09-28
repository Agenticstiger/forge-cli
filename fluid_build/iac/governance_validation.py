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

"""Validate-time gate for the governance a cloud binding cannot apply.

``fluid generate iac``, ``fluid plan`` and ``fluid apply`` refuse a principal that
is unmapped or a placeholder, a column restriction nothing on the binding enforces,
a Cloud KMS key on AWS or an AWS key on GCP, and retention or encryption on a GCP
target that is not a BigQuery table. This runs the SAME derivations
(``iac/providers/gcp_governance.py``, ``iac/column_access.py``,
``iac/principals.py``) so ``fluid validate`` reports each refusal at stage 2, with
the same message, instead of at apply. The same shape as ``validate_gcp_binding``.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Tuple

from .access import normalize_access_grants
from .base import UnsupportedBindingError
from .principals import gcp_grants, principal_map
from .provider_match import is_cloud


def _message(exc: UnsupportedBindingError) -> str:
    remedy = " ".join(exc.remediation)
    return f"{exc} {remedy}".strip()


def validate_governance(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(errors, warnings)`` for the contract's cloud bindings."""
    from .providers import gcp as _gcp
    from .providers import gcp_governance as _gov

    errors: List[str] = []
    warnings: List[str] = []
    grants = normalize_access_grants(contract)
    aws_bindings = 0
    for index, exposure in enumerate(contract.get("exposes") or []):
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        if not isinstance(binding, Mapping):
            continue
        target = _gcp.resolve_gcp_target(binding)
        try:
            principal_map(binding)
            if target in (_gcp.BIGQUERY_TABLE, _gcp.BIGQUERY_VIEW):
                _gov.validate_bigquery_governance(
                    contract, exposure, index, is_view=(target == _gcp.BIGQUERY_VIEW)
                )
            elif target is not None:
                _gov.refuse_unsupported_target(exposure, index, target)
                if target in (_gcp.GCS_BUCKET, _gcp.ICEBERG_STORAGE):
                    gcp_grants(grants, binding, where=f"exposes[{index}] accessPolicy")
            elif is_cloud(binding, "aws"):
                from .providers.aws import lf_column_exclusions

                aws_bindings += 1
                lf_column_exclusions(exposure, binding, index)
        except UnsupportedBindingError as exc:
            errors.append(_message(exc))
    if aws_bindings and grants:
        warnings.append(
            "accessPolicy.grants are not emitted for aws bindings: on AWS, access is the "
            "binding's governance.lakeFormation.grants (column restrictions narrow those "
            "grants). The grants apply on gcp bindings, through binding.principals."
        )
    return errors, warnings


__all__ = ["validate_governance"]
