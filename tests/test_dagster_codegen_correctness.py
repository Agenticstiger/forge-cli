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

"""The generated Dagster pipeline has to actually load and actually run.

`tests/test_prefect_dagster_codegen_injection.py` pins that a contract value
cannot become *code*. This file pins the separate question of whether the
emitted pipeline is *correct* — which it largely was not: an ordinary contract
with a single `dependsOn` produced a file Dagster refused to load, and one
emitter produced ops that logged and returned success without doing anything.

Two tiers, because Dagster is not a declared dependency of this project:

* **AST tier** (always runs, `unit`) — structural properties checkable without
  importing Dagster: no undefined names, `ins` keys matching call kwargs,
  topological emission order, collision handling.
* **Load tier** (`integration`, self-skipping) — imports the generated module
  with real Dagster. This is the only tier that can prove the thing works, so
  where a property is checkable in both, it is checked in both.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest


def _emitters():
    from fluid_build.providers.aws.codegen.dagster import (
        generate_dagster_pipeline as aws,
    )
    from fluid_build.providers.gcp.codegen.dagster import (
        generate_dagster_pipeline as gcp,
    )
    from fluid_build.providers.snowflake.codegen.dagster import (
        generate_dagster_pipeline as sf,
    )

    return {
        "aws": lambda c: aws(c, "123456789012", "us-east-1"),
        "gcp": lambda c: gcp(c, "my-project", "us-central1"),
        "sf": lambda c: sf(c, "acct", "DB", "WH"),
    }


IDS = ["aws", "gcp", "sf"]
ACTIONS = {
    "aws": ("aws.s3.createBucket", "aws.glue.createDatabase"),
    "gcp": ("gcp.gcs.create_bucket", "gcp.bigquery.create_dataset"),
    "sf": ("sf.snowflake.query", "sf.snowflake.query"),
}


def _fn(name):
    return _emitters()[name]


def _linear_contract(emitter, *, dep=True):
    """Two tasks, the second depending on the first. The ordinary case."""
    a, b = ACTIONS[emitter]
    second = {
        "taskId": "load_table",
        "type": "provider_action",
        "action": b,
        "params": {"database": "d", "dataset": "d", "sql": "SELECT 1", "query": "SELECT 1"},
    }
    if dep:
        second["dependsOn"] = ["make_bucket"]
    return {
        "id": "customer_orders",
        "name": "Customer Orders",
        "orchestration": {
            "schedule": "0 2 * * *",
            "tasks": [
                {
                    "taskId": "make_bucket",
                    "type": "provider_action",
                    "action": a,
                    "params": {"bucket": "b", "sql": "SELECT 1", "query": "SELECT 1"},
                },
                second,
            ],
        },
    }


# --------------------------------------------------------------------------- AST tier


def _bound_names(tree):
    bound = set(dir(__import__("builtins")))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                bound.update(
                    a.arg
                    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                    + [args.vararg, args.kwarg]
                    if a is not None
                )
        elif isinstance(node, ast.Lambda):
            a = node.args
            bound.update(
                x.arg
                for x in [*a.posonlyargs, *a.args, *a.kwonlyargs] + [a.vararg, a.kwarg]
                if x is not None
            )
    return bound


def assert_no_undefined_names(code: str) -> None:
    tree = ast.parse(code)
    bound = _bound_names(tree)
    undefined = sorted(
        {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound
        }
    )
    assert not undefined, f"generated pipeline references undefined names {undefined}\n---\n{code}"


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_linear_dependency_emits_no_undefined_names(emitter):
    """The ordinary two-task case. No attacker, no edge case."""
    assert_no_undefined_names(_fn(emitter)(_linear_contract(emitter)))


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_dependency_declared_before_its_upstream_still_works(emitter):
    """Contract order is not dependency order.

    A Python call site cannot reference a result bound later in the module, so
    the job body has to be emitted topologically, not in declaration order.
    """
    c = _linear_contract(emitter)
    c["orchestration"]["tasks"].reverse()  # dependent first
    assert_no_undefined_names(_fn(emitter)(c))


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_dependson_a_non_provider_task_is_skipped_not_dangling(emitter):
    """Only provider_action tasks become ops, so any other dep must be dropped."""
    c = _linear_contract(emitter)
    c["orchestration"]["tasks"].insert(0, {"taskId": "prep", "type": "bash", "command": "echo hi"})
    c["orchestration"]["tasks"][-1]["dependsOn"] = ["make_bucket", "prep"]
    code = _fn(emitter)(c)
    assert_no_undefined_names(code)
    assert "skipped" in code, "a dropped dependency must be visible in the output"


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_colliding_task_ids_keep_both_tasks(emitter):
    """`a-b` and `a.b` sanitise alike; neither task may silently vanish."""
    a, _ = ACTIONS[emitter]
    c = {
        "id": "p",
        "name": "P",
        "orchestration": {
            "schedule": "0 2 * * *",
            "tasks": [
                {
                    "taskId": "make-bucket",
                    "type": "provider_action",
                    "action": a,
                    "params": {"bucket": "x", "sql": "SELECT 1", "query": "SELECT 1"},
                },
                {
                    "taskId": "make.bucket",
                    "type": "provider_action",
                    "action": a,
                    "params": {"bucket": "y", "sql": "SELECT 2", "query": "SELECT 2"},
                },
            ],
        },
    }
    code = _fn(emitter)(c)
    assert_no_undefined_names(code)
    defs = [n.name for n in ast.walk(ast.parse(code)) if isinstance(n, ast.FunctionDef)]
    op_defs = [d for d in defs if "bucket" in d]
    assert len(set(op_defs)) == 2, f"a task was lost to an identifier collision: {op_defs}"


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_null_schedule_and_action_do_not_crash_generation(emitter):
    """A null YAML scalar must not surface as a bare AttributeError."""
    c = _linear_contract(emitter)
    c["orchestration"]["schedule"] = None
    _fn(emitter)(c)  # must not raise


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_generated_file_has_no_literal_brace_artifacts(emitter):
    """Over-escaped braces print `{e}` instead of the exception."""
    code = _fn(emitter)(_linear_contract(emitter))
    for tree_node in ast.walk(ast.parse(code)):
        if isinstance(tree_node, ast.JoinedStr):
            for part in tree_node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    assert (
                        "{" not in part.value
                    ), f"over-escaped brace leaves a literal placeholder: {part.value!r}"


# --------------------------------------------------------------------------- load tier


def _load(code: str, tmp_path, name: str):
    p = pathlib.Path(tmp_path) / f"{name}_pipeline.py"
    p.write_text(code)
    spec = importlib.util.spec_from_file_location(f"gen_{name}", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.mark.integration
@pytest.mark.parametrize("emitter", IDS)
def test_generated_pipeline_loads_under_real_dagster(emitter, tmp_path):
    """The property that actually matters: Dagster can load the file.

    This is what caught that `ins` keys had no matching op parameter and that
    the job graph passed op definitions rather than invocation outputs — both
    invisible to `ast.parse`, both fatal to every pipeline with a dependency.
    """
    pytest.importorskip("dagster", reason="dagster is not a declared dependency")
    _load(_fn(emitter)(_linear_contract(emitter)), tmp_path, emitter)


@pytest.mark.integration
@pytest.mark.parametrize("emitter", IDS)
def test_loaded_job_contains_every_declared_task(emitter, tmp_path):
    pytest.importorskip("dagster", reason="dagster is not a declared dependency")
    mod = _load(_fn(emitter)(_linear_contract(emitter)), tmp_path, f"{emitter}_nodes")
    jobs = [
        v for v in vars(mod).values() if type(v).__name__ in {"JobDefinition", "GraphDefinition"}
    ]
    assert jobs, "generated module defines no job"
    node_names = {n.name for n in jobs[0].graph.nodes}
    assert len(node_names) == 2, f"expected both declared tasks as ops, got {node_names}"


# ---------------------------------------------------------------------------
# Regressions introduced while fixing the above, caught by adversarial review.
# Each one passed the oracle before it was found, which is why they are pinned.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("emitter", IDS)
def test_generated_output_is_reproducible(emitter):
    """Identical contract in, identical file out — apart from the clock.

    `TopologicalSorter.add(node, *predecessors)` unpacks whatever it is given,
    so passing a *set* of predecessors leaked PYTHONHASHSEED into the emitted
    order and made the generated artifact differ run to run.
    """
    import hashlib
    import re

    c = _linear_contract(emitter)
    c["orchestration"]["tasks"].append(
        {
            "taskId": "fan_in",
            "type": "provider_action",
            "action": ACTIONS[emitter][0],
            "params": {"bucket": "z", "sql": "SELECT 3", "query": "SELECT 3"},
            "dependsOn": ["make_bucket", "load_table"],
        }
    )
    digests = set()
    for _ in range(5):
        src = re.sub(r"Generated: .*", "Generated: <ts>", _fn(emitter)(c))
        digests.add(hashlib.sha256(src.encode()).hexdigest())
    assert len(digests) == 1, "generated output is not reproducible across runs"


@pytest.mark.unit
def test_bare_schedule_presets_alias_the_at_prefixed_forms():
    """`weekly` must not silently become the daily-at-02:00 default.

    Note this DOES change AWS's bare `daily` from 02:00 to 00:00, because the
    bare words now alias the `@`-prefixed cron nicknames. That is deliberate —
    it is what makes `weekly` mean weekly — but it is a behaviour change, and
    the AWS *Airflow* emitter still maps bare `daily` to 02:00, so the two
    disagree until that one is aligned too.

    Several emitters carried local converters that knew the bare words while
    the shared helper knew only the `@`-prefixed nicknames, so consolidating
    onto the shared one quietly changed every bare-preset schedule.
    """
    from fluid_build.providers.common.codegen_utils import convert_schedule_to_cron as c

    assert c("hourly") == c("@hourly") == "0 * * * *"
    assert c("weekly") == c("@weekly") == "0 0 * * 0"
    assert c("monthly") == c("@monthly") == "0 0 1 * *"
    assert c("annually") == c("@annually") == "0 0 1 1 *"
    assert c("daily") == "0 0 * * *"


@pytest.mark.unit
def test_snowflake_non_sql_action_does_not_run_bogus_sql():
    """A Snowflake TASK is not a SQL statement.

    Dispatching on the trailing action segment alone made
    `snowflake.task.execute` match the SQL branch, so the op ran `SELECT 1`
    and reported success while the declared work never happened — the same
    silent-false-success this module was being fixed to remove.
    """
    from fluid_build.providers.snowflake.codegen.dagster import generate_dagster_pipeline

    c = {
        "id": "o",
        "name": "O",
        "orchestration": {
            "schedule": "0 2 * * *",
            "tasks": [
                {
                    "taskId": "score",
                    "type": "provider_action",
                    "action": "snowflake.task.execute",
                    "params": {"module": "scoring.main"},
                },
            ],
        },
    }
    body = generate_dagster_pipeline(c, "acct", "DB", "WH")
    assert "NotImplementedError" in body, "an unhandled action must fail loudly, not succeed"
    assert "SELECT 1" not in body, "a non-SQL action must not emit a placeholder query"


@pytest.mark.unit
def test_falsy_but_present_schedule_is_not_silently_defaulted(caplog):
    """`0`, `False`, and YAML 1.1's `off`/`no` are falsy but present.

    `str(raw or "")` collapsed them to the empty string, which took the
    "absent, use the default" path and skipped the warning.
    """
    import logging

    from fluid_build.providers.snowflake.codegen.dagster import generate_dagster_pipeline

    for bad in (False, 0):
        caplog.clear()
        c = {
            "id": "p",
            "name": "N",
            "orchestration": {
                "schedule": bad,
                "tasks": [
                    {
                        "taskId": "a",
                        "type": "provider_action",
                        "action": "snowflake.sql.execute_sql",
                        "params": {"sql": "SELECT 1"},
                    }
                ],
            },
        }
        with caplog.at_level(logging.WARNING):
            generate_dagster_pipeline(c, "acct", "DB", "WH")
        assert any(
            "unrecognised schedule" in r.getMessage() for r in caplog.records
        ), f"a schedule of {bad!r} was silently defaulted with no warning"


@pytest.mark.unit
def test_sql_action_without_sql_fails_loudly():
    """A SQL action carrying no SQL must not run a placeholder query.

    Both the Snowflake and GCP emitters defaulted a missing query to
    `SELECT 1`: the op ran it, logged a row count and reported success while
    the declared work never happened. Routing more actions into the SQL branch
    made that fallback reachable, so it had to go with them.
    """
    from fluid_build.providers.gcp.codegen.dagster import (
        generate_dagster_pipeline as gcp_gen,
    )
    from fluid_build.providers.snowflake.codegen.dagster import (
        generate_dagster_pipeline as sf_gen,
    )

    def _contract(action):
        return {
            "id": "o",
            "name": "O",
            "orchestration": {
                "schedule": "0 2 * * *",
                "tasks": [
                    {"taskId": "a", "type": "provider_action", "action": action, "params": {}}
                ],
            },
        }

    sf = sf_gen(_contract("snowflake.sql.execute_sql"), "acct", "DB", "WH")
    assert "SELECT 1" not in sf
    assert "NotImplementedError" in sf

    gcp = gcp_gen(_contract("gcp.bigquery.query"), "proj", "us-central1")
    assert "SELECT 1" not in gcp
