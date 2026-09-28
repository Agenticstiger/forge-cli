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

"""The GCP provider's sovereignty hook: refuse a placement outside the policy.

AWS refuses an out-of-jurisdiction region inside its planner
(``AwsProvider._validate_sovereignty``), and ``fluid generate iac`` /
``fluid apply`` recognise that refusal through ``native_actions``. GCP had no
such hook, so only ``fluid validate`` stood between a contract and a dataset
in the wrong jurisdiction, and a binding with no region went through every
stage and landed in BigQuery's ``US`` multi-region.

The check is the policy engine's own (``policy.sovereignty``,
``check_placements``), not a second rule set, applied to where the data
actually goes rather than to what the binding says:

* each gcp expose whose binding names no region (the engine's check 0: the
  mode decides, strict refuses);
* each ``location`` / ``region`` a resource carries: every resource the
  OpenTofu plugin emits (``GcpIacPlugin.emit``, the chokepoint of ``fluid
  apply`` and ``fluid generate iac``, which runs whether or not a native
  provider can be built), and every action the native planner produced
  (``GcpProvider.plan``). That is where a region the provider filled in by
  default (the planner's ``US`` dataset default, the provider region a
  Cloud Scheduler job or a staging bucket inherits, ``--region``'s
  ``europe-west3`` or the SDK's ``us-central1``) meets ``allowedRegions``.

Error-severity findings raise :class:`~fluid_build._errors.SovereigntyViolationError`
(the typed error ``generate_iac._is_sovereignty_refusal`` recognises through
a ``ProviderError`` cause chain); warnings and info are logged and pass,
which is what ``advisory`` and ``audit`` mean.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from fluid_build.policy.sovereignty import (
    SovereigntyValidator,
    SovereigntyViolation,
    binding_region,
)

_LOG = logging.getLogger(__name__)

Placement = Tuple[str, Optional[str]]


def unplaced_gcp_exposes(contract: Mapping[str, Any]) -> List[Placement]:
    """``(exposeId, None)`` for each gcp expose whose binding names no region."""
    out: List[Placement] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        binding = expose.get("binding") or {}
        if not isinstance(binding, Mapping):
            continue
        if str(binding.get("platform") or "").lower() != "gcp":
            continue
        if binding_region(binding) is None:
            out.append((str(expose.get("exposeId", "unknown")), None))
    return out


def resource_placements(resources: Mapping[str, Any]) -> List[Placement]:
    """``(address, location)`` for every emitted resource that names one.

    ``resources`` is the plugin's ``{type: {name: body}}``. A value that is an
    OpenTofu reference (``${...}``) is not a place and is skipped.
    """
    out: List[Placement] = []
    for rtype, by_name in (resources or {}).items():
        if not isinstance(by_name, Mapping):
            continue
        for name, body in by_name.items():
            if not isinstance(body, Mapping):
                continue
            for key in ("location", "region"):
                value = body.get(key)
                if isinstance(value, str) and value and not value.startswith("${"):
                    out.append((f"{rtype}.{name}", value))
                    break
    return out


def action_placements(actions: Iterable[Mapping[str, Any]]) -> List[Placement]:
    """``(action id, location)`` for every planned action that names one."""
    out: List[Placement] = []
    for index, action in enumerate(actions or ()):
        if not isinstance(action, Mapping):
            continue
        for key in ("location", "region"):
            value = action.get(key)
            if isinstance(value, str) and value:
                where = str(action.get("id") or action.get("op") or f"action[{index}]")
                out.append((where, value))
                break
    return out


def gcp_sovereignty_violations(
    contract: Mapping[str, Any], placements: Sequence[Placement]
) -> List[SovereigntyViolation]:
    """The engine's findings for this contract's gcp exposes and ``placements``."""
    sovereignty = contract.get("sovereignty")
    if not isinstance(sovereignty, Mapping) or not sovereignty:
        return []
    everything = unplaced_gcp_exposes(contract) + list(placements)
    if not everything:
        return []
    _, violations = SovereigntyValidator().check_placements(sovereignty, everything)
    return violations


def enforce_gcp_sovereignty(
    contract: Mapping[str, Any],
    placements: Sequence[Placement],
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Raise on an error-severity finding; log the rest."""
    from fluid_build._errors import SovereigntyViolationError, doc_url

    log = logger or _LOG
    violations = gcp_sovereignty_violations(contract, placements)
    errors = [v for v in violations if v.severity == "error"]
    for v in violations:
        if v.severity != "error":
            log.warning("gcp sovereignty (%s): %s: %s", v.severity, v.expose_id, v.message)
    if not errors:
        return
    sovereignty = contract.get("sovereignty") or {}
    allowed = [str(r) for r in (sovereignty.get("allowedRegions") or [])]
    # ``where: message``, de-duplicated in order. Not ``[where]``: the CLI
    # renders errors through rich, which reads square brackets as markup.
    findings = "; ".join(dict.fromkeys(f"{v.expose_id}: {v.message}" for v in errors))
    raise SovereigntyViolationError(
        what=f"GCP placement refused by the sovereignty policy: {findings}",
        why=(
            "contract.sovereignty is enforced "
            f"({sovereignty.get('enforcementMode', 'strict')}) and "
            + (
                f"allows {', '.join(allowed)}"
                if allowed
                else f"requires jurisdiction {sovereignty.get('jurisdiction')!r}"
            )
            + "; this is where the GCP resources would be created."
        ),
        fix=(
            "Set binding.location.region on every gcp expose to an allowed region "
            "(the BigQuery dataset and bucket take it), or change the sovereignty policy."
        ),
        doc=doc_url("sovereignty"),
    )
