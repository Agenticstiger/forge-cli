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

"""OpenTofu state-backend block generation.

The default is local state; a ``--state-backend s3://… | gcs://…`` spec
machine-generates a remote ``terraform.backend`` block so multi-user /
CI apply has durable, shared state.

**The per-contract state key (RFC-packaging-modes.md file 7).** Historically
every contract defaulted to the *same* ``fluid/terraform.tfstate`` key, so two
contracts pointed at one ``--state-backend`` silently clobbered each other's
state. Pooled infrastructure is exactly the topology that amplifies the bug —
a platform team hands out one state bucket — so the fix ships with the
packaging feature, **gated on it**: a contract carrying a ``packaging`` block
defaults to ``fluid/<safe_ident(id)>/terraform.tfstate``, while a contract
without one keeps the legacy key byte-for-byte (which is what
``tests/iac/test_iac_packaging_default_pin.py`` pins). Flipping the default
for *everyone* is a deliberate follow-up (it relocates state for existing
users and wants its own migration note) — see the RFC's open question 3.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .naming import safe_ident
from .packaging import LEGACY, PackagingError, resolve_packaging

#: The pre-packaging default — one shared key for every contract.
LEGACY_STATE_KEY = "fluid/terraform.tfstate"


def default_state_key(contract: Optional[Mapping[str, Any]]) -> str:
    """The default state key for ``contract`` — legacy unless packaging is declared.

    Returns :data:`LEGACY_STATE_KEY` when ``contract`` is ``None`` or resolves
    to the ``packaging.LEGACY`` sentinel, and the per-contract
    ``fluid/<safe_ident(id)>/terraform.tfstate`` otherwise.

    A malformed ``packaging`` block falls back to the legacy key rather than
    raising: this runs *before* the emit path, which resolves the same block
    moments later and reports the failure as a typed ``CLIError`` with a
    useful message. Raising here would surface the same problem as a raw
    traceback from state-key derivation, which names the wrong culprit.
    """
    if contract is None:
        return LEGACY_STATE_KEY
    try:
        resolution = resolve_packaging(contract)
    except PackagingError:
        return LEGACY_STATE_KEY
    if resolution is LEGACY:
        return LEGACY_STATE_KEY
    cid = safe_ident(contract.get("id") or contract.get("name") or "contract")
    return f"fluid/{cid}/terraform.tfstate"


def parse_backend(
    spec: Optional[str], contract: Optional[Mapping[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Parse a backend spec into a ``terraform.backend`` block.

    ``None`` / empty → local state (returns ``None``). Supported specs:

    * ``s3://<bucket>/<key>``     — AWS S3 backend
    * ``gcs://<bucket>/<prefix>`` — Google Cloud Storage backend

    An explicit key / prefix in the spec always wins. When the spec omits
    one, ``contract`` (optional) selects the default via
    :func:`default_state_key` — per-contract for packaging-bearing
    contracts, the shared legacy key otherwise.

    The backend block carries no credentials — ``tofu`` reads those from
    the environment (``AWS_*`` / ``GOOGLE_*``).
    """
    if not spec:
        return None
    spec = spec.strip()

    if spec.startswith("s3://"):
        bucket, _, key = spec[len("s3://") :].partition("/")
        if not bucket:
            raise ValueError(f"s3 backend spec needs a bucket: {spec!r}")
        return {"s3": {"bucket": bucket, "key": key or default_state_key(contract)}}

    if spec.startswith("gcs://"):
        bucket, _, prefix = spec[len("gcs://") :].partition("/")
        if not bucket:
            raise ValueError(f"gcs backend spec needs a bucket: {spec!r}")
        block: Dict[str, Any] = {"gcs": {"bucket": bucket}}
        if not prefix:
            # The GCS backend namespaces by object *prefix*, not a full key;
            # derive it from the same per-contract default (sans filename) so
            # both backends isolate identically. Legacy contracts emit no
            # prefix at all, exactly as before.
            default = default_state_key(contract)
            prefix = default.rsplit("/", 1)[0] if default != LEGACY_STATE_KEY else ""
        if prefix:
            block["gcs"]["prefix"] = prefix
        return block

    raise ValueError(f"unsupported state backend {spec!r} — use s3:// or gcs://")
