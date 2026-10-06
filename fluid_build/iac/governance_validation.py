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

``fluid generate iac`` and ``fluid apply`` (and the state check of ``fluid diff``,
which emits the same module) refuse a principal that is unmapped or a placeholder, a
column restriction, mask or row filter nothing on the binding enforces, a Cloud KMS
key on AWS or an AWS key on GCP, and retention or encryption on a GCP target that is
not a BigQuery table; ``fluid plan`` does not run the emitter. This runs the SAME
derivations (``iac/providers/gcp_governance.py``, ``lf_governance`` in
``iac/providers/aws.py``, ``iac/column_access.py``, ``iac/principals.py``) so
``fluid validate`` reports each refusal at stage 2, with the same message, instead of
at apply. The same shape as ``validate_gcp_binding``.
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


def _lf_grants(binding: Mapping[str, Any]) -> List[Any]:
    governance = binding.get("governance") if isinstance(binding, Mapping) else None
    lake = governance.get("lakeFormation") if isinstance(governance, Mapping) else None
    grants = lake.get("grants") if isinstance(lake, Mapping) else None
    return list(grants) if isinstance(grants, list) else []


def _aws_refusal(exposure: Mapping[str, Any], binding: Mapping[str, Any], index: int) -> str:
    """What the AWS emitter's ``lf_governance`` refuses this expose with, or ``""``.

    A name the masked view's SQL cannot quote raises a plain ``ValueError``
    (``validate_ident``), on which the emitter stops too. It is reported here as the
    expose's error: escaping, it would end :func:`validate_governance`, and
    ``fluid validate`` would report no governance finding at all.
    """
    from .providers.aws import lf_governance

    try:
        lf_governance(exposure, binding, index)
    except UnsupportedBindingError as exc:
        return _message(exc)
    except ValueError as exc:
        return f"exposes[{exposure.get('exposeId') or index}]: {exc}"
    return ""


def validate_governance(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(errors, warnings)`` for the contract's cloud bindings.

    Each binding is dispatched on its platform first: an ``aws`` binding is checked
    against what the AWS emitter writes, whatever its format resolves to on GCP
    (``resolve_gcp_target`` resolves an Iceberg format with a bucket to GCP Iceberg
    storage on any platform).
    """
    from .providers import gcp as _gcp
    from .providers import gcp_governance as _gov

    errors: List[str] = []
    warnings: List[str] = []
    grants = normalize_access_grants(contract)
    unenforced: List[str] = []
    try:
        _gov.refuse_mixed_dataset_encryption(contract)
    except UnsupportedBindingError as exc:
        errors.append(_message(exc))
    for index, exposure in enumerate(contract.get("exposes") or []):
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        if not isinstance(binding, Mapping):
            continue
        try:
            principal_map(binding)
            if is_cloud(binding, "aws"):
                refusal = _aws_refusal(exposure, binding, index)
                if refusal:
                    errors.append(refusal)
                elif not _lf_grants(binding):
                    unenforced.append(str(exposure.get("exposeId") or index))
                continue
            if not _gov.gcp_owned(binding):
                continue
            target = _gcp.resolve_gcp_target(binding)
            if target in (_gcp.BIGQUERY_TABLE, _gcp.BIGQUERY_VIEW):
                _gov.validate_bigquery_governance(
                    contract, exposure, index, is_view=(target == _gcp.BIGQUERY_VIEW)
                )
            elif target is not None:
                _gov.refuse_unsupported_target(exposure, index, target)
                if target in (_gcp.GCS_BUCKET, _gcp.ICEBERG_STORAGE):
                    gcp_grants(grants, binding, where=f"exposes[{index}] accessPolicy")
        except UnsupportedBindingError as exc:
            errors.append(_message(exc))
    if grants and unenforced:
        # Only here is the contract's access intent unenforced on AWS: a binding
        # with Lake Formation grants is how the aws overlay says who reads, and
        # warning for it too failed `fluid validate --strict` for every contract
        # that carries accessPolicy for its gcp deployment.
        warnings.append(
            f"accessPolicy.grants are not enforced on aws binding(s) {', '.join(unenforced)}: "
            "the AWS emitter does not write accessPolicy, and these bindings declare no "
            "governance.lakeFormation.grants, the AWS form of who may read the table. Add "
            "them to the aws overlay's binding (column restrictions then narrow them)."
        )
    return errors, warnings


__all__ = ["validate_governance"]
