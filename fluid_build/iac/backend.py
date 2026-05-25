# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenTofu state-backend block generation.

The default is local state; a ``--state-backend s3://… | gcs://…`` spec
machine-generates a remote ``terraform.backend`` block so multi-user /
CI apply has durable, shared state.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def parse_backend(spec: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a backend spec into a ``terraform.backend`` block.

    ``None`` / empty → local state (returns ``None``). Supported specs:

    * ``s3://<bucket>/<key>``     — AWS S3 backend
    * ``gcs://<bucket>/<prefix>`` — Google Cloud Storage backend

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
        return {"s3": {"bucket": bucket, "key": key or "fluid/terraform.tfstate"}}

    if spec.startswith("gcs://"):
        bucket, _, prefix = spec[len("gcs://") :].partition("/")
        if not bucket:
            raise ValueError(f"gcs backend spec needs a bucket: {spec!r}")
        block: Dict[str, Any] = {"gcs": {"bucket": bucket}}
        if prefix:
            block["gcs"]["prefix"] = prefix
        return block

    raise ValueError(f"unsupported state backend {spec!r} — use s3:// or gcs://")
