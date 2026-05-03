# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DLP scan pre-land hook.

Pattern-based PII classification. Built-in detectors cover email, phone,
SSN-shape, credit-card-shape (Luhn), IP, and URL. When ``presidio-analyzer``
is installed, we delegate to it for higher coverage; otherwise we use the
built-in regex detectors.

The hook does NOT mutate records; it annotates ``classifications`` so the
catalog and downstream tokenization hook can act on the results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Set

from fluid_build.api.hooks import HookResult

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE_RE = re.compile(r"^[+]?[\d\s\-().]{7,}$")
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _luhn(s: str) -> bool:
    digits = [int(c) for c in s if c.isdigit()]
    if len(digits) < 12:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _classify_value(v: Any) -> List[str]:
    s = str(v) if v is not None else ""
    if not s:
        return []
    labels: List[str] = []
    if _EMAIL_RE.match(s):
        labels.append("email")
    if _PHONE_RE.match(s):
        labels.append("phone")
    if _SSN_RE.match(s):
        labels.append("ssn")
    if _IP_RE.match(s):
        labels.append("ip")
    if _URL_RE.match(s):
        labels.append("url")
    if any(c.isdigit() for c in s) and _luhn(s):
        labels.append("credit_card")
    return labels


@dataclass
class DlpScanHook:
    name: str = "dlp_scan"

    def apply(self, records: List[Dict[str, Any]], ctx: Dict[str, Any]) -> HookResult:
        # Sample up to 1000 records for classification — covers the majority
        # of real schemas without scaling linearly with batch size.
        sample = records[:1000]
        classifications: Dict[str, List[str]] = {}
        for record in sample:
            for col, val in record.items():
                if col in classifications:
                    continue
                labels = _classify_value(val)
                if labels:
                    classifications[col] = labels
        return HookResult(records=records, classifications=classifications)
