# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""S3 helpers shared by the commands that read or write S3: the Spark job runs, ``fluid ship
--release-to`` and ``fluid runs verify-release``.

Each caller takes a ``clients`` factory, ``(service, region) -> client``, so a test injects fake
clients and never reaches AWS. boto3 is the ``[aws]`` extra and is imported only by
:func:`default_clients`; everything else here is standard library.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Optional, Tuple

#: ``(service, region) -> client``: :func:`default_clients`, or a test's fakes.
ClientFactory = Callable[[str, Optional[str]], Any]

#: The S3 codes for a request the identity may not make (a missing key reads as one without
#: ``s3:ListBucket``).
DENIED_CODES = ("AccessDenied", "403", "Forbidden")
#: The S3 codes for a key that is not there.
MISSING_CODES = ("NoSuchKey", "404", "NotFound")
#: A conditional write (``If-None-Match: *``) met an object already at the key.
PRECONDITION_CODES = ("PreconditionFailed", "412")
#: A conditional write raced a delete of the key; S3 says the write may be retried.
CONFLICT_CODES = ("ConditionalRequestConflict", "409")


class S3Unavailable(RuntimeError):
    """boto3 is not installed: the ``[aws]`` extra is missing."""


def default_clients(service: str, region: Optional[str], *, config: Any = None) -> Any:
    """A boto3 client, from the default credential chain (``AWS_ENDPOINT_URL`` included);
    ``config``, a ``botocore.config.Config``, bounds its calls. ``region`` None: the profile's.

    Raises:
        S3Unavailable: boto3 is not installed.
    """
    try:
        import boto3
    except ImportError as exc:
        raise S3Unavailable(
            "boto3 is not installed: pip install 'data-product-forge[aws]'"
        ) from exc
    return boto3.client(service, region_name=region, config=config)


def error_code(exc: BaseException) -> str:
    """The S3 error code botocore's error carries, or ""."""
    return str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code") or "")


def http_status(exc: BaseException) -> Optional[int]:
    """The HTTP status botocore's error carries, or None."""
    meta = (getattr(exc, "response", None) or {}).get("ResponseMetadata") or {}
    status = meta.get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def is_denied_code(code: str) -> bool:
    """Whether an S3 error code is a refused request (:data:`DENIED_CODES`)."""
    return code in DENIED_CODES


def is_missing_code(code: str) -> bool:
    """Whether an S3 error code is a key that is not there (:data:`MISSING_CODES`)."""
    return code in MISSING_CODES


def split_uri(uri: str) -> Tuple[str, str]:
    """``s3://bucket/key`` as ``(bucket, key)``."""
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def read_back(clients: ClientFactory, region: Optional[str], uri: str, limit: int) -> bytes:
    """An object's bytes from S3, at most ``limit`` of them. Raises what boto3 raises."""
    bucket, key = split_uri(uri)
    data: bytes = clients("s3", region).get_object(Bucket=bucket, Key=key)["Body"].read(limit)
    return data


def sha256_b64(hex_digest: str) -> str:
    """A hex sha256 as S3's ``ChecksumSHA256`` writes it: the digest's bytes in base64."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
