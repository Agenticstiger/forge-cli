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

"""Bind a local file to a DuckDB view, as one statement.

Extracted from ``providers/local/local.py::_register_one``, which built this
SQL and executed it in the same breath. The string and the execution now
separate, because two callers need the same statement for different reasons:
the local provider executes it during ``fluid apply``, and the sql engine
writes it into the generated script so the script runs standalone.

Keeping one copy matters: the two would otherwise drift, and `fluid apply`
succeeding while the generated script fails is exactly the split this
module exists to close.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from ._sql_safety import quote_ansi_string_literal, validate_ident

_CSV_SUFFIXES = {".csv", ".tsv"}


def build_register_view_sql(
    table: str,
    path: Path,
    fmt: str,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    """Return ``CREATE OR REPLACE VIEW <table> AS SELECT * FROM read_*(...)``.

    ``table`` is a contract-supplied identifier and ``path`` a
    contract-supplied string, so both route through the central SQL-safety
    helpers -- identifiers through :func:`validate_ident`, the path through
    :func:`quote_ansi_string_literal`.

    Format is taken from ``fmt`` first and the suffix second, matching what
    the local provider has always done; anything unrecognised falls back to
    ``read_csv_auto``, which is DuckDB's own most forgiving reader.
    """
    suffix = path.suffix.lower()
    ident = validate_ident(table)
    literal = quote_ansi_string_literal(str(path))

    if fmt in {"csv", "tsv"} or suffix in _CSV_SUFFIXES:
        delim = "," if (fmt != "tsv" and suffix != ".tsv") else "\t"
        opt: Dict[str, Any] = {"AUTO_DETECT": True, "DELIM": delim}
        opt.update({k.upper(): v for k, v in (options or {}).items()})
        # The option KEY is a DuckDB named-parameter name interpolated as
        # ``KEY:=value``. ``options`` is contract-supplied, so route each key
        # through ``validate_ident`` to reject DDL smuggled via a key -- the
        # value already routes through ``quote_ansi_string_literal``.
        opt_sql = ", ".join(
            f"{validate_ident(k)}:="
            f"{json.dumps(v) if not isinstance(v, str) else quote_ansi_string_literal(v)}"
            for k, v in opt.items()
        )
        return (
            f"CREATE OR REPLACE VIEW {ident} AS SELECT * FROM read_csv_auto({literal}, {opt_sql});"
        )

    if fmt in {"parquet", "pq"} or suffix == ".parquet":
        return f"CREATE OR REPLACE VIEW {ident} AS SELECT * FROM read_parquet({literal});"

    return f"CREATE OR REPLACE VIEW {ident} AS SELECT * FROM read_csv_auto({literal});"
