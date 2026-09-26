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

A spec taken from :data:`STATE_BACKEND_ENV` is the exception. One CI job sets
it once for every product it applies, so the legacy key would put all of them
in one state, and each apply would plan to destroy the resources of the
others. When it names no key, every contract gets its own key, packaging
block or not: ``fluid/<id>/terraform.tfstate``, with the contract id exactly
as written. ``safe_ident`` would not do there: it folds ``.`` and ``-`` into
``_``, so ``a.b``, ``a-b`` and ``a_b``, all valid ids, would share one state
again. An id outside the FLUID identifier grammar cannot key a state and is
refused. The variable is new, so no state lives at a key it chose before.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .naming import safe_ident
from .packaging import LEGACY, PackagingError, resolve_packaging

#: The pre-packaging default — one shared key for every contract.
LEGACY_STATE_KEY = "fluid/terraform.tfstate"

#: The FLUID schema's ``identifier`` grammar (0.7.3 on; 0.7.1-0.7.2 accept a
#: lowercase subset). Such an id holds only ``[A-Za-z0-9_.-]``, which S3 keys
#: and GCS object names take unchanged, and can be no ``.`` or ``..`` segment.
#: ``fullmatch``, so a trailing newline is refused too.
_CONTRACT_ID_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?")

#: What an S3 or GCS bucket name is made of (older S3 names allow upper case
#: and ``_``). Anything else, a ``user:secret@`` prefix for one, is refused,
#: so the state location ``fluid apply`` prints can carry no credential.
_BUCKET_RE = re.compile(r"[A-Za-z0-9._-]+")

#: Control characters: refused in a key or prefix, which ``fluid apply``
#: prints on its state line (a newline there would forge a line of output).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Environment variable ``fluid apply`` reads for the state backend when
#: ``--state-backend`` is not on the command line. A CI job sets it once so
#: OpenTofu state lives in a bucket rather than the workspace, which CI
#: wipes after every run (a wiped local state re-plans every resource as
#: new on the next run).
STATE_BACKEND_ENV = "FLUID_STATE_BACKEND"


def resolve_state_backend_spec(
    flag_value: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> Tuple[Optional[str], str]:
    """Pick the backend spec and say where it came from: ``(spec, origin)``.

    Precedence is flag, then environment, then local state:

    * ``--state-backend`` given (``flag_value`` is not ``None``) always wins.
      An empty value is an explicit request for local state, so one job can
      opt out of a ``FLUID_STATE_BACKEND`` its environment sets.
    * otherwise a non-blank :data:`STATE_BACKEND_ENV`;
    * otherwise ``(None, "default")``: local state.

    ``origin`` is ``"--state-backend"``, ``"FLUID_STATE_BACKEND"`` or
    ``"default"``, for messages. The spec itself is parsed by
    :func:`parse_backend`.
    """
    if flag_value is not None:
        return (flag_value.strip() or None, "--state-backend")
    env_value = (os.environ if environ is None else environ).get(STATE_BACKEND_ENV, "")
    if env_value and env_value.strip():
        return (env_value.strip(), STATE_BACKEND_ENV)
    return (None, "default")


def default_state_key(contract: Optional[Mapping[str, Any]], *, per_contract: bool = False) -> str:
    """The default state key for ``contract`` — legacy unless packaging is declared.

    Returns :data:`LEGACY_STATE_KEY` when ``contract`` is ``None`` or resolves
    to the ``packaging.LEGACY`` sentinel, and the per-contract
    ``fluid/<safe_ident(id)>/terraform.tfstate`` otherwise.

    ``per_contract=True`` (the spec came from :data:`STATE_BACKEND_ENV`; see
    the module docstring) skips the packaging test and keys every contract by
    its id as written, ``fluid/<id>/terraform.tfstate``, so two distinct ids
    never share a state. It raises ``ValueError`` for an id outside the FLUID
    identifier grammar, the error :func:`parse_backend` gives for a spec it
    cannot use.

    A malformed ``packaging`` block falls back to the legacy key rather than
    raising: this runs *before* the emit path, which resolves the same block
    moments later and reports the failure as a typed ``CLIError`` with a
    useful message. Raising here would surface the same problem as a raw
    traceback from state-key derivation, which names the wrong culprit.
    """
    if contract is None:
        return LEGACY_STATE_KEY
    if per_contract:
        return f"fluid/{_state_id(contract)}/terraform.tfstate"
    try:
        resolution = resolve_packaging(contract)
    except PackagingError:
        return LEGACY_STATE_KEY
    if resolution is LEGACY:
        return LEGACY_STATE_KEY
    cid = safe_ident(contract.get("id") or contract.get("name") or "contract")
    return f"fluid/{cid}/terraform.tfstate"


def _state_id(contract: Mapping[str, Any]) -> str:
    """The contract id, verbatim, once it is proven to fit a state key."""
    raw = contract.get("id")
    if isinstance(raw, str) and _CONTRACT_ID_RE.fullmatch(raw):
        return raw
    raise ValueError(
        f"contract id {raw!r} cannot name its own state: a bucket-only "
        f"{STATE_BACKEND_ENV} keys state by the id as written, which must be a "
        "FLUID identifier ([A-Za-z0-9_.-], starting and ending with a letter, "
        "digit or '_'). Fix the id, or put a key in the spec"
    )


def parse_backend(
    spec: Optional[str],
    contract: Optional[Mapping[str, Any]] = None,
    *,
    per_contract_default: bool = False,
) -> Optional[Dict[str, Any]]:
    """Parse a backend spec into a ``terraform.backend`` block.

    ``None`` / empty → local state (returns ``None``). Supported specs:

    * ``s3://<bucket>/<key>``     — AWS S3 backend
    * ``gcs://<bucket>/<prefix>`` — Google Cloud Storage backend

    An explicit key / prefix in the spec always wins. When the spec omits
    one, ``contract`` (optional) selects the default via
    :func:`default_state_key` — per-contract for packaging-bearing
    contracts, the shared legacy key otherwise. ``per_contract_default``
    gives every contract its own default, keyed by its id as written
    (``fluid apply`` sets it for a spec from :data:`STATE_BACKEND_ENV`).

    The backend block carries no credentials — ``tofu`` reads those from
    the environment (``AWS_*`` / ``GOOGLE_*``). A bucket name holding
    anything but ``[A-Za-z0-9._-]``, a key or prefix holding a control
    character, and an unsupported spec are refused without echoing the spec.
    """
    if not spec:
        return None
    spec = spec.strip()

    if spec.startswith("s3://"):
        bucket, _, key = spec[len("s3://") :].partition("/")
        if not bucket:
            raise ValueError(f"s3 backend spec needs a bucket: {spec!r}")
        _check_bucket(bucket, "s3")
        _check_path(key, "s3", "key")
        if not key:
            key = default_state_key(contract, per_contract=per_contract_default)
        return {"s3": {"bucket": bucket, "key": key}}

    if spec.startswith("gcs://"):
        bucket, _, prefix = spec[len("gcs://") :].partition("/")
        if not bucket:
            raise ValueError(f"gcs backend spec needs a bucket: {spec!r}")
        _check_bucket(bucket, "gcs")
        _check_path(prefix, "gcs", "prefix")
        block: Dict[str, Any] = {"gcs": {"bucket": bucket}}
        if not prefix:
            # The GCS backend namespaces by object *prefix*, not a full key;
            # derive it from the same per-contract default (sans filename) so
            # both backends isolate identically. Legacy contracts emit no
            # prefix at all, exactly as before.
            default = default_state_key(contract, per_contract=per_contract_default)
            prefix = default.rsplit("/", 1)[0] if default != LEGACY_STATE_KEY else ""
        if prefix:
            block["gcs"]["prefix"] = prefix
        return block

    # Only the scheme is named: the rest of a spec in the wrong form may be a
    # URL with a credential in it.
    scheme, sep, _ = spec.partition("://")
    named = f"scheme {scheme!r}" if sep and scheme.isalnum() else "spec"
    raise ValueError(f"unsupported state backend {named} — use s3:// or gcs://")


def _check_bucket(bucket: str, scheme: str) -> None:
    if not _BUCKET_RE.fullmatch(bucket):
        # Not echoed: what is not a bucket name may be a credential.
        raise ValueError(
            f"{scheme} backend bucket may only hold [A-Za-z0-9._-]; credentials "
            "never go in the spec (tofu reads them from the environment)"
        )


def _check_path(value: str, scheme: str, what: str) -> None:
    if _CONTROL_RE.search(value):
        raise ValueError(f"{scheme} backend {what} may not hold a control character")


def backend_location(block: Mapping[str, Any]) -> str:
    """Where ``block`` keeps state, written as the spec that selects it.

    ``s3://<bucket>/<key>``, or ``gcs://<bucket>/<prefix>`` (no ``/<prefix>``
    when the block has none). Given back as ``--state-backend`` it names the
    same state, so an apply's output shows which state it used however the
    spec reached it: a bucket-only ``s3://b`` is ``s3://b/fluid/terraform.tfstate``
    from the flag and ``s3://b/fluid/<id>/terraform.tfstate`` from
    :data:`STATE_BACKEND_ENV`. Bucket names and keys are not secrets, and
    :func:`parse_backend` refuses a bucket that could hide one.
    """
    if "s3" in block:
        s3 = block["s3"]
        return f"s3://{s3['bucket']}/{s3['key']}"
    if "gcs" in block:
        gcs = block["gcs"]
        prefix = gcs.get("prefix")
        return f"gcs://{gcs['bucket']}" + (f"/{prefix}" if prefix else "")
    return str(next(iter(block)))
