# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The names and the shape of a release receipt (``fluid-receipt/1``), shared by what writes one
(``fluid runs receipt record``), what gates on one (``fluid runs receipt check``) and what lists
them (``fluid runs status --release-dir``).

A receipt says that one sealed release was checked in one environment: which release (its
``identity_digest``, and the ``code_digest`` it shares with the other environments' releases of
the same commit), the ``fluid verify`` report that checked it, the deploy targets, and the
result, ``PASSED`` or ``FAILED``. Its fields follow SLSA's verification summary attestation
(``verifier``, ``time_verified``, ``resource_uri``, ``input_attestations``, the result); with
``--sign`` the same content goes into an in-toto Statement of that predicate type.

Receipts sit beside the release they name, one directory per environment:
``<code hex>/receipts/<env>/<utc>-<identity16>.json``, and each is written once. The name
carries the time, so name order is time order.

Standard library only: ``fluid runs status`` imports this.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

RECEIPT_SCHEMA = "fluid-receipt/1"
RECEIPTS_DIR = "receipts"
PASSED = "PASSED"
FAILED = "FAILED"
#: What a receipt records: a ``fluid verify`` check (the gate takes only these), or a ``fluid
#: runs deploy`` (a record of what was copied where, never a check). Absent: a check.
VERIFY = "verify"
DEPLOY = "deploy"
CHECKS = (VERIFY, DEPLOY)
#: Bytes of one receipt, and of the ``fluid verify`` report stored beside it.
MAX_RECEIPT_BYTES = 1_048_576
MAX_REPORT_BYTES = 16 * 1_048_576
#: Entries one environment's directory may hold: a receipt with its report and, signed, its
#: Statement, signature and certificate. A listing past this decides nothing, since it could
#: leave out the newest receipt.
MAX_ENTRIES = 50_000
#: What the stored report beside a receipt is called: ``<receipt stem>.report.json``.
REPORT_SUFFIX = ".report.json"

#: ``20261126T072555.123456Z-0123456789abcdef.json``
_NAME = re.compile(r"(?P<stamp>[0-9]{8}T[0-9]{6}\.[0-9]{6}Z)-(?P<identity>[0-9a-f]{16})\.json")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_STAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def receipt_name(moment: datetime, identity_digest: str) -> str:
    """The file name of a receipt written at ``moment`` for the release ``identity_digest``."""
    stamp = moment.astimezone(timezone.utc).strftime(_STAMP_FORMAT)
    return f"{stamp}-{identity_digest.split(':', 1)[1][:16]}.json"


def iso(moment: datetime) -> str:
    """``moment`` as a receipt's ``time_verified`` holds it (UTC, microseconds)."""
    return moment.astimezone(timezone.utc).strftime(_ISO_FORMAT)


def is_receipt_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


def report_name(name: str) -> str:
    """The stored report's name beside the receipt ``name``."""
    return name[: -len(".json")] + REPORT_SUFFIX


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def parse_receipt(name: str, data: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The receipt ``data`` holds, or ``None`` and why it is not one. A receipt is someone
    else's file: every field the gate reads is checked here, and its name must say what it
    holds (the time it was verified and the start of the release's identity digest)."""
    matched = _NAME.fullmatch(name)
    if matched is None:
        return None, "its name is not <utc>-<identity16>.json"
    try:
        stamp = datetime.strptime(matched.group("stamp"), _STAMP_FORMAT)
    except ValueError:
        return None, "its name carries no real time"
    stamp = stamp.replace(tzinfo=timezone.utc)
    if len(data) > MAX_RECEIPT_BYTES:
        return None, f"it is larger than {MAX_RECEIPT_BYTES:,} bytes"
    try:
        receipt = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "it is not JSON"
    if not isinstance(receipt, dict):
        return None, "it is not a JSON object"
    if receipt.get("schema") != RECEIPT_SCHEMA:
        return None, f"it is not a {RECEIPT_SCHEMA} record"
    for key in ("code_digest", "identity_digest"):
        if not is_digest(receipt.get(key)):
            return None, f"its {key} is not sha256:<64 hex>"
    if str(receipt["identity_digest"])[7:23] != matched.group("identity"):
        return None, "its name does not start with its identity digest"
    if receipt.get("result") not in (PASSED, FAILED):
        return None, f"its result is not {PASSED} or {FAILED}"
    for key in ("environment", "product_id"):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            return None, f"it names no {key}"
    if receipt.get("time_verified") != iso(stamp):
        return None, "its time_verified is not the time its name carries"
    report = receipt.get("report")
    if not isinstance(report, Mapping) or not is_digest(report.get("sha256")):
        return None, "it names no report with its sha256"
    if receipt.get("check", VERIFY) not in CHECKS:
        return None, f"its check is not {' or '.join(CHECKS)}"
    return receipt, None


def check_of(receipt: Mapping[str, Any]) -> str:
    """What a parsed receipt records: :data:`VERIFY` or :data:`DEPLOY`."""
    return str(receipt.get("check", VERIFY))


def newest_first(names: Sequence[str]) -> List[str]:
    """Receipt names, the newest first (the name carries the time)."""
    return sorted((n for n in names if is_receipt_name(n)), reverse=True)
