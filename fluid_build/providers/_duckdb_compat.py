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

"""Version-compat seam for the DuckDB Python client (1.5.x and 2.x).

forge-cli takes ``duckdb>=1.5.0`` with no ceiling (the floor is the sandbox's,
:data:`fluid_build.providers._duckdb_sandbox.MIN_DUCKDB_VERSION`). DuckDB 2.0
needs Python 3.11, so a Python 3.10 install stays on 1.5.x while a newer one
gets 2.x: the engine runs on both, and what differs lives here.

Two 2.0 changes reach the engine (duckdb-python v2.0.0 release notes,
"Upgrading from 1.x"; https://duckdb.org/docs/2.0/clients/python/migration.html):

* A ``con.execute()`` result streams, and owns its connection until it is read
  to the end. ``fetchone()`` on a one-row ``COUNT(*)`` leaves it open, and the
  next relation read, ``register`` or ``create_function`` on that connection
  raises ``ResourceInUseException``. Relations lost ``fetchone()`` and
  ``fetchmany()`` outright. :func:`first_row` reads to the end on both.
* A database file 2.0 creates uses storage version 2.0, which no 1.5 process can
  open ("can only read versions between 64 and 68"). :func:`storage_compat_config`
  keeps the files forge-cli creates readable by every DuckDB it supports.

Nothing here imports duckdb: callers pass what they already hold.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

#: The storage format of the oldest DuckDB forge-cli supports. Database files
#: forge-cli creates on DuckDB 2.x are written in it, so a 1.5 install (Python
#: 3.10, or a tool pinned below 2) can still open them.
STORAGE_COMPATIBILITY_VERSION = "v1.5.0"


def first_row(result: Any) -> Optional[Tuple[Any, ...]]:
    """The first row of ``result`` (a ``con.execute()`` result or a relation), or ``None``.

    Reads the whole result, so give it a query of one row (``COUNT(*)``,
    ``LIMIT 1``). ``fetchone()`` does not do: on DuckDB 2.0 a result read only
    partway keeps its connection busy, and a relation has no ``fetchone()``.
    """
    rows = result.fetchall()
    return rows[0] if rows else None


def storage_compat_config(version: Tuple[int, int, int]) -> Dict[str, str]:
    """``duckdb.connect`` config that keeps files DuckDB ``version`` creates readable by 1.5.

    Empty before 2.0, where every file is readable by 1.5 already and the
    client's own default stays. From 2.0, ``storage_compatibility_version``
    set at connect (a ``SET`` once the file is open is too late) makes each
    database file the connection creates, ``ATTACH`` included, use the 1.5
    format. A file that exists keeps the format it has.
    """
    if version >= (2, 0, 0):
        return {"storage_compatibility_version": STORAGE_COMPATIBILITY_VERSION}
    return {}


__all__ = ["STORAGE_COMPATIBILITY_VERSION", "first_row", "storage_compat_config"]
