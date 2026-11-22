# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A live table the plan would update in place, left as it is: what a plugin says of one.

An Iceberg table's catalog entry points at the table's current metadata file, and every
commit an engine makes moves that pointer. A catalog that guards the pointer takes the
version a writer read and refuses a write made from an older one: Iceberg's own Glue
catalog passes the ``VersionId`` it read to ``UpdateTable`` (``GlueTableOperations``),
and Glue then refuses a stale write with ``ConcurrentModificationException``. An
OpenTofu provider that updates the entry in place without that version can put an older
pointer back, and the commit made in between is lost (hashicorp/aws 6.73,
``resourceCatalogTableUpdate``). So a plugin names the attributes of such a table that
the plan must leave as they are (:class:`TableHold`), and the engine plans the table
again with them in ``ignore_changes`` (``cli/_apply_table_hold.py``). What the contract
wanted of them is reported, and waits for a change made through the table's own catalog,
or for :data:`ALLOW_ICEBERG_UPDATE_FLAG` once the table's writers are paused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from fluid_build.iac.adoption import event_fields

#: The flag of ``fluid apply``, ``fluid plan --tofu`` and ``fluid ship`` that lets a run
#: update a live Iceberg table in place.
ALLOW_ICEBERG_UPDATE_FLAG = "--allow-iceberg-update"


@dataclass(frozen=True)
class TableHold:
    """One table the plan would update in place that the run leaves as it is."""

    #: ``<database>.<table>``, as the plugin names it.
    table: str
    #: The attributes the plan made again leaves alone (its ``ignore_changes``).
    attributes: Tuple[str, ...]
    #: What the contract wants of them that the table does not have, one line each.
    drift: Tuple[str, ...] = ()
    #: Why the table is one the run leaves as it is.
    why: str = ""

    def as_event(self) -> Dict[str, Any]:
        """The hold for a structured log event or an error's context (lists, as JSON)."""
        return event_fields(self)
