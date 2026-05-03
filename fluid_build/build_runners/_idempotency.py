# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Idempotency-key formatting for the data path.

The default template is ``{run_id}:{stream}:{record_pk}``. Runners format
the key with per-record substitutions; destinations honor it via MERGE
or upsert semantics where supported.
"""

from __future__ import annotations

import hashlib
from string import Formatter
from typing import Any, Dict

DEFAULT_KEY_TEMPLATE = "{run_id}:{stream}:{record_pk}"


def format_key(template: str, run_id: str, stream: str, record: Dict[str, Any]) -> str:
    """Format an idempotency key against a record. Falls back to a hash of
    the record JSON when ``record_pk`` is referenced but missing.
    """
    fields = {fname for _, fname, _, _ in Formatter().parse(template) if fname}
    values: Dict[str, str] = {"run_id": run_id, "stream": stream}
    if "record_pk" in fields:
        pk = record.get("id") or record.get("pk") or record.get("primary_key")
        if pk is None:
            # Stable hash fallback.
            blob = repr(sorted(record.items())).encode("utf-8")
            pk = hashlib.sha256(blob).hexdigest()[:16]
        values["record_pk"] = str(pk)
    for f in fields:
        if f not in values:
            v = record.get(f)
            values[f] = "" if v is None else str(v)
    return template.format(**values)
