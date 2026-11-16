#!/usr/bin/env python3
# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Fill the derived fields of new vectors in ``fluid_build/fluid_sql/fluid-sql-vectors.json``.

Add a vector by hand with its ``id``, ``text``, ``site``, ``model`` (and
``derive``), and its ``expect``: ``verdict`` plus ``reason`` for a refusal, or
``eval`` cases with hand-computed results for an accepted text. Then run::

    python scripts/fill_fluid_sql_vectors.py

It writes the ``tree``, ``canonical``, ``type`` and ``scale`` of each accepted
vector that lacks them, from this implementation, and never changes a field
that is already there: the verdicts and the values stay hand-written, and the
trees and texts it fills are reviewed in the diff. It exits 1 when any vector
disagrees with the implementation. ``--check`` writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "fluid_build" / "fluid_sql" / "fluid-sql-vectors.json"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from fluid_build.fluid_sql import FluidSqlError, canonical, check, parse, to_json
    from fluid_build.fluid_sql._vectors import build_models, run_vector

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="write nothing; report only")
    args = parser.parse_args()

    document = json.loads(VECTORS.read_text("utf-8"))
    models = build_models(document)
    filled = 0
    for vector in document["vectors"]:
        expect = vector["expect"]
        if expect.get("verdict") != "accepted":
            continue
        model = models[vector["model"]]
        derive = model.columns[vector["derive"]] if vector.get("derive") else None
        try:
            tree = parse(vector["text"])
            checked = check(tree, model, vector.get("site", "predicate"), derive)
        except FluidSqlError:
            continue  # run_vector reports it below
        derived = {
            "tree": to_json(tree),
            "canonical": canonical(tree),
            "type": checked.type.kind,
        }
        if checked.type.kind == "number":
            derived["scale"] = "unbounded" if checked.type.scale is None else checked.type.scale
        for key, value in derived.items():
            if key not in expect:
                expect[key] = value
                filled += 1
        # Keep a stable key order: verdict, tree, canonical, type, scale, eval.
        order = ["verdict", "tree", "canonical", "type", "scale", "eval"]
        vector["expect"] = {k: expect[k] for k in order if k in expect} | {
            k: v for k, v in expect.items() if k not in order
        }

    failures = 0
    for vector in document["vectors"]:
        problems = run_vector(vector, models)
        for problem in problems:
            failures += 1
            print(f"{vector['id']}: {problem}")

    if not args.check and filled:
        VECTORS.write_text(json.dumps(document, indent=1, ensure_ascii=True) + "\n", "utf-8")
    print(f"{len(document['vectors'])} vectors, {filled} fields filled, {failures} disagreements")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
