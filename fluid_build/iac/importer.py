# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Config-driven ``import {}`` blocks for brownfield apply.

``tofu apply`` against fresh state errors when a declared resource
already exists in the cloud. A config-driven ``import {}`` block (OpenTofu
1.6+) instead tells ``tofu`` to *adopt* the existing resource into state
— folding forge-cli's native CREATE-IF-NOT-EXISTS tolerance into
OpenTofu's declarative model.

This module is the import-block machinery. Auto-discovery of which
resources already exist (and their cloud ids) reuses each provider's
live introspection and is wired in a follow-up — see ``AUTOGEN_SPIKE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class ImportBlock:
    """Adopt one pre-existing cloud resource into OpenTofu state.

    ``to`` is the resource address (e.g. ``aws_s3_bucket.raw``); ``id`` is
    the provider-specific import identifier (e.g. the bucket name, or
    ``projects/<p>/datasets/<d>`` for a BigQuery dataset).
    """

    to: str
    id: str
    #: An import whose failure is never fatal, even for a plugin with
    #: ``strict_adoption``: the resource's create overwrites what it would have
    #: adopted (a Databricks grant is authoritative for its principal), so a
    #: failed import costs an honest plan, never a silent replace.
    best_effort: bool = False


def import_section(blocks: List[ImportBlock]) -> Dict[str, Any]:
    """Render import blocks as a ``.tf.json`` ``import`` section.

    Returns an empty dict when there is nothing to import (so callers can
    unconditionally merge the result).
    """
    if not blocks:
        return {}
    return {"import": [{"to": block.to, "id": block.id} for block in blocks]}
