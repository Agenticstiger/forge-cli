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

"""DDL emission for forged logical models.

H3 fix (Snowflake e2e finding 06-snowflake-e2e.md): the DV2 DDL
emitter used to hard-code ``STRING`` for every column, throwing
away the source-system data types the catalog adapter populated
on :attr:`OSIField.data_type`. Operators saw ``AMOUNT_CHF STRING``,
``INVOICE_DATE STRING`` etc. instead of the correct
``NUMBER(15,2)`` / ``TIMESTAMP_TZ``. The pipeline KNEW the right
answer (every type is in ``.model.json`` under
``osi.datasets[].fields[]``) — it just discarded it at the DDL
emit boundary.

This module now builds a name → ``data_type`` lookup map from the
OSI sidecar (which the modeler populated from ``ColumnDefinition
.logical_type`` — itself populated from ``CatalogColumn.data_type``)
and honours it for every emitted column. Fallback is still
``STRING`` so callers that wire a logical draft without OSI fields
(rare in practice; only some unit tests) keep working.
"""

from __future__ import annotations

from typing import Dict, Iterable

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft

_FALLBACK_TYPE = "STRING"


def _build_type_lookup(logical: LogicalDraft) -> Dict[str, str]:
    """Index OSI field types by lower-cased column name.

    The DV2 IR (:class:`HubDefinition` / :class:`SatelliteDefinition`
    / :class:`LinkDefinition`) carries only column *names*, no
    types. The types live on :attr:`OSIField.data_type` which the
    modeler populated from ``ColumnDefinition.logical_type``.
    Index them once so the emitter can do O(1) lookups instead of
    re-walking the OSI tree for every hub/link/sat.

    Case-folded keys: catalog APIs upper-case identifiers
    (Snowflake's ``INFORMATION_SCHEMA`` returns ``CUSTOMER_ID``)
    while DV2-IR-side identifiers from the modeler are often
    lower-case (``customer_id``). Case-folding lets the lookup
    work across both shapes without forcing the modeler to
    normalise.

    The map only carries non-empty types — a missing entry should
    fall through to the ``STRING`` default, not look up an empty
    string and emit ``column ``.
    """
    out: Dict[str, str] = {}
    osi = getattr(logical, "osi", None)
    if osi is None:
        return out
    for dataset in osi.datasets or []:
        for field in dataset.fields or []:
            if not field.data_type:
                continue
            out[field.name.lower()] = field.data_type
    return out


def _columns_with_types(
    column_names: Iterable[str],
    type_lookup: Dict[str, str],
    *,
    default: str = _FALLBACK_TYPE,
) -> list[str]:
    """Render ``name <type>`` pairs, honouring the OSI type map."""
    rendered: list[str] = []
    for column in column_names:
        data_type = type_lookup.get(column.lower(), default)
        rendered.append(f"{column} {data_type}")
    return rendered


def emit_ddl_files(logical: LogicalDraft) -> Dict[str, str]:
    files: Dict[str, str] = {}
    type_lookup = _build_type_lookup(logical)
    if logical.technique == "data_vault_2" and logical.dv2 is not None:
        for hub in logical.dv2.hubs:
            rendered_cols = _columns_with_types(
                hub.business_key_columns,
                type_lookup,
            ) or [f"hub_id {_FALLBACK_TYPE}"]
            columns = ",\n  ".join(rendered_cols)
            files[f"{hub.hub_table_name}.sql"] = (
                f"create table {hub.hub_table_name} (\n  {columns}\n);\n"
            )
        for link in logical.dv2.links:
            # Link tables store hash-keys for each member hub; the
            # canonical DV2 convention is ``<hub>_hk STRING``. Hash
            # keys are fixed-width hex digests so the source-column
            # type is irrelevant — STRING / VARCHAR / TEXT all work
            # and the type_lookup wouldn't carry ``<hub>_hk`` anyway.
            rendered_cols = [f"{hub}_hk {_FALLBACK_TYPE}" for hub in link.hubs_involved] or [
                f"link_id {_FALLBACK_TYPE}"
            ]
            columns = ",\n  ".join(rendered_cols)
            files[f"{link.link_table_name}.sql"] = (
                f"create table {link.link_table_name} (\n  {columns}\n);\n"
            )
        for sat in logical.dv2.satellites:
            rendered_cols = _columns_with_types(
                sat.attributes,
                type_lookup,
            ) or [f"hash_diff {_FALLBACK_TYPE}"]
            columns = ",\n  ".join(rendered_cols)
            files[f"{sat.satellite_table_name}.sql"] = (
                f"create table {sat.satellite_table_name} (\n  {columns}\n);\n"
            )
    elif logical.dimensional is not None:
        for dimension in logical.dimensional.dimensions:
            columns = ",\n  ".join(
                [f"{field.name} {field.data_type}" for field in dimension.attributes]
                or ["id STRING"]
            )
            files[f"{dimension.name}.sql"] = f"create table {dimension.name} (\n  {columns}\n);\n"
        for fact in logical.dimensional.facts:
            columns = (
                ",\n  ".join(
                    [f"{measure.name} {measure.data_type}" for measure in fact.measures]
                    + [f"{key} STRING" for key in fact.foreign_keys]
                )
                or "id STRING"
            )
            files[f"{fact.name}.sql"] = f"create table {fact.name} (\n  {columns}\n);\n"
    return files
