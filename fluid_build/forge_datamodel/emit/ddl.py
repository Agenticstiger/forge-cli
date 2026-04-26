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

"""DDL emission for forged logical models."""

from __future__ import annotations

from typing import Dict

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft


def emit_ddl_files(logical: LogicalDraft) -> Dict[str, str]:
    files: Dict[str, str] = {}
    if logical.technique == "data_vault_2" and logical.dv2 is not None:
        for hub in logical.dv2.hubs:
            columns = ",\n  ".join(
                [f"{column} STRING" for column in hub.business_key_columns] or ["hub_id STRING"]
            )
            files[f"{hub.hub_table_name}.sql"] = (
                f"create table {hub.hub_table_name} (\n  {columns}\n);\n"
            )
        for link in logical.dv2.links:
            columns = ",\n  ".join(
                [f"{hub}_hk STRING" for hub in link.hubs_involved] or ["link_id STRING"]
            )
            files[f"{link.link_table_name}.sql"] = (
                f"create table {link.link_table_name} (\n  {columns}\n);\n"
            )
        for sat in logical.dv2.satellites:
            columns = ",\n  ".join(
                [f"{attr} STRING" for attr in sat.attributes] or ["hash_diff STRING"]
            )
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
