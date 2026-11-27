# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Sealed releases that pin a bundle digest, and the report block ``fluid verify --release``
writes for them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.api import release as api_release
from fluid_build.cli import _release_receipt as receipts
from fluid_build.util import release_record
from tests.cli import _fake_s3

#: The bundle digest the fixture releases pin.
BUNDLE_DIGEST = "sha256:" + "1" * 64


def sealed(directory: Path, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """``_fake_s3.sealed``, its identity also pinning ``BUNDLE_DIGEST`` as the bundle's merkle
    digest, as ``fluid ship`` always does."""
    index = _fake_s3.sealed(directory, *args, **kwargs)
    identity = index["identity"]
    identity["bundle"]["manifest_digest"] = BUNDLE_DIGEST
    index["identity_digest"] = release_record.identity_digest(identity)
    index["code_digest"] = release_record.code_digest(identity)
    (directory / "release.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return index


def release_block(source: Any, product: str, *, clients: Optional[Any] = None) -> Dict[str, Any]:
    """The block of a verify run on the contract and environment ``source`` was sealed from."""
    checked = api_release.verify_release(str(source), clients=clients)
    return receipts.report_release_block(
        str(source),
        product,
        environment=checked.environment,
        contract_digest=str(checked.bundle_digest),
        clients=clients,
    )
