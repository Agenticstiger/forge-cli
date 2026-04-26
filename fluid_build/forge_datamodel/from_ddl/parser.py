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

"""DDL parsing helpers with sqlglot-first and regex fallback behavior."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ColumnDefinition:
    """Represents a parsed source column."""

    name: str
    logical_type: str
    qualifiers: Dict[str, Any] = field(default_factory=dict)
    nullable: bool = True
    primary_key: bool = False
    comment: Optional[str] = None


@dataclass
class TableDefinition:
    """Represents a parsed source table."""

    name: str
    columns: List[ColumnDefinition] = field(default_factory=list)
    primary_keys: List[str] = field(default_factory=list)
    comment: Optional[str] = None


class DDLParser:
    """Parse DDL content using ``sqlglot`` when available and a regex fallback otherwise."""

    _IDENTIFIER = r'(?:[`"\[][^`"\]]+[`"\]]|\w+)' r'(?:\s*\.\s*(?:[`"\[][^`"\]]+[`"\]]|\w+))*'
    CREATE_TABLE_PATTERN = re.compile(
        rf"CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?"
        rf"(?:(?:TEMP|TEMPORARY|TRANSIENT|VOLATILE)\s+)?TABLE\s+"
        rf"(?:IF\s+NOT\s+EXISTS\s+)?({_IDENTIFIER})\s*\(",
        re.IGNORECASE,
    )
    COLUMN_PATTERN = re.compile(
        r'^\s*([`"\[]?\w+[`"\]]?)\s+(\w+(?:<[^>]+>)?(?:\([^)]+\))?)\s*(.*)',
        re.IGNORECASE | re.MULTILINE,
    )
    OPTIONS_COMMENT_PATTERN = re.compile(
        r"OPTIONS\s*\(\s*description\s*=\s*'(.*?)'\s*\)", re.IGNORECASE
    )
    COMMENT_PATTERN = re.compile(r"--\s*(.+)$", re.MULTILINE)

    def parse_ddl_file(
        self, file_path: str, *, dialect: Optional[str] = None
    ) -> List[TableDefinition]:
        return self.parse_ddl_content(Path(file_path).read_text(encoding="utf-8"), dialect=dialect)

    def parse_ddl_content(
        self, content: str, *, dialect: Optional[str] = None
    ) -> List[TableDefinition]:
        tables = self._parse_with_sqlglot(content, dialect=dialect)
        if tables:
            return tables
        return self._parse_with_fallback(content)

    def _parse_with_sqlglot(
        self, content: str, *, dialect: Optional[str] = None
    ) -> List[TableDefinition]:
        try:
            import sqlglot
            from sqlglot import exp
        except Exception:
            return []

        tables: List[TableDefinition] = []
        try:
            expressions = sqlglot.parse(content, read=dialect)
        except Exception:
            return []

        for expression in expressions:
            if not isinstance(expression, exp.Create):
                continue
            if expression.args.get("kind") != "TABLE":
                continue
            schema = expression.this
            table_name = getattr(schema.this, "name", None) or getattr(schema, "name", None)
            if not table_name:
                continue
            columns: List[ColumnDefinition] = []
            primary_keys: List[str] = []
            for item in schema.expressions or []:
                if isinstance(item, exp.ColumnDef):
                    col_name = item.this.name
                    kind = item.args.get("kind")
                    logical_type = kind.sql(dialect=dialect) if kind is not None else "STRING"
                    constraints = item.args.get("constraints") or []
                    nullable = True
                    primary_key = False
                    for constraint in constraints:
                        constraint_kind = type(constraint.this).__name__ if constraint.this else ""
                        if constraint_kind == "NotNullColumnConstraint":
                            nullable = False
                        if constraint_kind == "PrimaryKeyColumnConstraint":
                            primary_key = True
                    columns.append(
                        ColumnDefinition(
                            name=col_name,
                            logical_type=logical_type.upper(),
                            nullable=nullable,
                            primary_key=primary_key,
                        )
                    )
                    if primary_key:
                        primary_keys.append(col_name)
                elif isinstance(item, exp.PrimaryKey):
                    for expr in item.expressions or []:
                        if getattr(expr, "name", None):
                            primary_keys.append(expr.name)
            deduped_keys = list(dict.fromkeys(primary_keys))
            tables.append(
                TableDefinition(name=table_name, columns=columns, primary_keys=deduped_keys)
            )
        return tables

    def _parse_with_fallback(self, content: str) -> List[TableDefinition]:
        tables: List[TableDefinition] = []
        table_matches = list(self.CREATE_TABLE_PATTERN.finditer(content))
        for index, match in enumerate(table_matches):
            table_name = match.group(1).strip('`"[]')
            start_pos = match.end()
            end_pos = (
                table_matches[index + 1].start() if index + 1 < len(table_matches) else len(content)
            )
            table_content = content[start_pos:end_pos]
            tables.append(self._parse_table_definition(table_name, table_content))
        return tables

    def _parse_table_definition(self, table_name: str, content: str) -> TableDefinition:
        simple_table_name = table_name.split(".")[-1].strip().strip('`"[]')
        table = TableDefinition(name=simple_table_name)
        balance = 0
        column_block_end_idx = -1
        for index, char in enumerate(content):
            if char == "(":
                balance += 1
            elif char == ")":
                if balance == 0:
                    column_block_end_idx = index
                    break
                balance -= 1

        columns_block = content if column_block_end_idx == -1 else content[:column_block_end_idx]
        lines = columns_block.split("\n")
        cleaned: List[tuple[str, Optional[str]]] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            comment_match = self.COMMENT_PATTERN.search(line)
            current_comment = None
            if comment_match:
                current_comment = comment_match.group(1).strip()
                line = self.COMMENT_PATTERN.sub("", line).strip()
            cleaned.append((line, current_comment))

        for line, comment in cleaned:
            if not line:
                continue
            upper_line = line.upper()
            if upper_line.startswith(("PARTITION BY", "CLUSTER BY", "OPTIONS")):
                continue
            if upper_line.startswith("PRIMARY KEY") or (
                upper_line.startswith("CONSTRAINT") and "PRIMARY KEY" in upper_line
            ):
                pk_match = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", line, re.IGNORECASE)
                if pk_match:
                    table.primary_keys.extend(
                        [col.strip().strip('`"[]') for col in pk_match.group(1).split(",")]
                    )
                continue
            column = self._parse_column_definition(line, comment)
            if column:
                table.columns.append(column)
                if column.primary_key and column.name not in table.primary_keys:
                    table.primary_keys.append(column.name)
        return table

    def _parse_column_definition(
        self, line: str, comment: Optional[str]
    ) -> Optional[ColumnDefinition]:
        line = line.strip().rstrip(",")
        if not line:
            return None
        match = self.COLUMN_PATTERN.match(line)
        if not match:
            return None

        column_name = match.group(1).strip('`"[]')
        type_spec = match.group(2)
        rest_of_line = match.group(3)
        col_comment = comment
        comment_match = self.OPTIONS_COMMENT_PATTERN.search(rest_of_line)
        if comment_match:
            col_comment = comment_match.group(1).strip()

        qualifiers: Dict[str, Any] = {}
        type_match = re.match(r"(\w+)(?:<([^>]+)>)?(?:\(([^)]+)\))?", type_spec)
        if not type_match:
            logical_type = type_spec.upper()
        else:
            logical_type = type_match.group(1).upper()
            angle_params = type_match.group(2)
            paren_params = type_match.group(3)
            if angle_params:
                qualifiers["nested_type"] = angle_params.strip()
            if paren_params:
                params = [part.strip() for part in paren_params.split(",")]
                if logical_type in {"VARCHAR", "STRING", "CHAR"} and params:
                    qualifiers["length"] = int(params[0]) if params[0].isdigit() else params[0]
                elif logical_type in {"DECIMAL", "NUMERIC"}:
                    if params:
                        qualifiers["precision"] = int(params[0]) if params[0].isdigit() else 18
                    if len(params) > 1:
                        qualifiers["scale"] = int(params[1]) if params[1].isdigit() else 4

        constraints = rest_of_line.upper()
        return ColumnDefinition(
            name=column_name,
            logical_type=logical_type,
            qualifiers=qualifiers,
            nullable="NOT NULL" not in constraints,
            primary_key="PRIMARY KEY" in constraints,
            comment=col_comment,
        )


def infer_sqlglot_dialect(source_type: Optional[str]) -> Optional[str]:
    """Map a forge source type to a ``sqlglot`` dialect name."""
    if not source_type:
        return None
    return {
        "bigquery": "bigquery",
        "snowflake": "snowflake",
        "postgres": "postgres",
        "postgresql": "postgres",
        "mysql": "mysql",
        "oracle": "oracle",
    }.get(source_type.lower())


@dataclass
class ParsedDDL:
    """Convenience wrapper returned by ``parse_ddl_text``."""

    tables: List[TableDefinition]
    dialect: Optional[str] = None


def parse_ddl_text(content: str, dialect: Optional[str] = None) -> ParsedDDL:
    """Parse DDL text and return a structured result."""
    parser = DDLParser()
    return ParsedDDL(
        tables=parser.parse_ddl_content(content, dialect=dialect),
        dialect=dialect,
    )
