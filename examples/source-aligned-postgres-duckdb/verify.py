#!/usr/bin/env python3
# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Post-apply verification: assert row count + schema match what was seeded."""

from __future__ import annotations

import sys
from pathlib import Path


EXPECTED_ROWS = 5
EXPECTED_COLUMNS = {"id", "customer", "amount", "placed_at"}
OUT = Path(__file__).resolve().parent / "out" / "orders.parquet"


def main() -> int:
    if not OUT.exists():
        print(f"FAIL: output Parquet missing at {OUT}", file=sys.stderr)
        return 1

    try:
        import duckdb  # type: ignore
    except ImportError:
        print("FAIL: duckdb not installed", file=sys.stderr)
        return 1

    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT}')").fetchone()[0]
        cols = {c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{OUT}')").fetchall()}
    finally:
        con.close()

    ok = True
    if rows != EXPECTED_ROWS:
        print(f"FAIL: expected {EXPECTED_ROWS} rows, got {rows}", file=sys.stderr)
        ok = False
    missing = EXPECTED_COLUMNS - cols
    if missing:
        print(f"FAIL: missing columns: {missing}", file=sys.stderr)
        ok = False

    if ok:
        print(f"OK: {rows} rows, columns={sorted(cols)}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
