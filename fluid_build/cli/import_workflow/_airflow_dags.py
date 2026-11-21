# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What an Airflow DAG file says, read without importing Airflow or running the file.

:func:`read_dags` takes one parsed module and returns its DAGs: id, schedule, owner, tags,
the tasks (operator, owner) and the script each Spark-submitting task submits with its
arguments, and the dependencies between tasks. A DAG file is never imported, so Airflow is
never loaded and no code in the file runs: Airflow's own ``DagBag`` imports every file, and
that is exactly what this reader exists to avoid.

How a DAG and a task are found. Names resolve through the file's import table (an aliased
``from airflow.providers... import SparkSubmitOperator as Submit`` is still seen, as in ruff's
Airflow rules), and only what is literal is read. A construct that cannot be read statically
is recorded, not guessed, with the names of the markers the ``ferro-airflow-dag-parser``
(Apache-2.0) lists: an id that is not a constant, a DAG or an operator built in a loop, ``chain``
over a list, dynamic task mapping, a DAG under a condition, a schedule that is not a constant.

* DAGs: ``DAG(...)`` and ``with DAG(...) as dag:`` from ``airflow`` (``airflow.DAG``,
  ``airflow.models``, ``airflow.sdk``), and ``@dag`` functions (the TaskFlow API).
* The operators that submit Spark work, with the parameters that carry the script (Airflow 2.10
  and 3.x spell them alike; the paths differ for ``BashOperator``, which moved to
  ``airflow.providers.standard`` in Airflow 3):

  ==================================  =====================================================
  ``SparkSubmitOperator``             ``application``, ``application_args``
  ``EmrAddStepsOperator``             ``steps[].HadoopJarStep.Args`` (``spark-submit`` ...)
  ``EmrCreateJobFlowOperator``        ``job_flow_overrides["Steps"][]`` as above
  ``EmrContainerOperator``            ``job_driver.sparkSubmitJobDriver.entryPoint``
  ``EmrServerlessStartJobOperator``   ``job_driver.sparkSubmit.entryPoint|entryPointArguments``
  ``GlueJobOperator``                 ``script_location``, ``script_args``
  ``DatabricksSubmitRunOperator``     ``spark_python_task.python_file|parameters``
  ``BashOperator``                    a ``spark-submit ... script.py`` in ``bash_command``
  ==================================  =====================================================

  Any other Airflow operator is listed as a task of kind ``other`` and read no further. An
  operator class of the project's own is read as one of these when the conventions say so.
* Dependencies: ``a >> b``, ``a << b`` (also with lists), ``set_downstream``, ``set_upstream``,
  ``chain`` and ``cross_downstream``, between tasks bound to names or written inline.
"""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ._fold import ABSENT, Folder, Unresolved, lookup
from ._pysource import Module, ancestors, qualified_name, snippet

#: Known operator class names to the kind of work they submit.
OPERATOR_KINDS: Dict[str, str] = {
    "SparkSubmitOperator": "spark_submit",
    "EmrAddStepsOperator": "emr_add_steps",
    "EmrCreateJobFlowOperator": "emr_create_job_flow",
    "EmrContainerOperator": "emr_containers",
    "EmrServerlessStartJobOperator": "emr_serverless",
    "GlueJobOperator": "glue_job",
    "DatabricksSubmitRunOperator": "databricks_submit",
    "BashOperator": "bash",
    "PythonOperator": "python",
    "PythonVirtualenvOperator": "python",
}
#: Kinds the conventions may map a project's own operator to.
SUBMIT_KINDS = frozenset(
    {
        "spark_submit",
        "emr_add_steps",
        "emr_create_job_flow",
        "emr_containers",
        "emr_serverless",
        "glue_job",
        "databricks_submit",
        "bash",
    }
)

_LOOPS = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_MAX_STEPS = 200


@dataclass(frozen=True)
class Submit:
    """The script a task submits and the arguments it passes."""

    #: ``spark-submit``, ``emr-step``, ``emr-eks``, ``emr-serverless``, ``glue``, ``databricks``
    #: or ``bash``.
    runtime: str
    #: The script (path or URI) as written; ``None`` when it did not resolve.
    script: Optional[str]
    why: str
    #: What a person reads when ``script`` is ``None``.
    shown: str
    args: Tuple[str, ...]
    line: int

    @property
    def templated(self) -> bool:
        return bool(self.script) and ("{{" in (self.script or "") or "${" in (self.script or ""))


@dataclass
class Task:
    key: str
    task_id: Optional[str]
    shown: str
    var: Optional[str]
    operator: str
    kind: str
    owner: Optional[str]
    file: str
    line: int
    submits: List[Submit] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class Dag:
    dag_id: Optional[str]
    shown: str
    file: str
    line: int
    #: ``cron`` (a cron expression or an ``@preset``), ``none`` (``schedule=None``),
    #: ``expression`` (``timedelta``, a timetable, datasets), ``default`` (not given) or
    #: ``unresolved``.
    schedule_kind: str = "default"
    schedule: Optional[str] = None
    owner: Optional[str] = None
    tags: Tuple[str, ...] = ()
    description: Optional[str] = None
    catchup: Optional[bool] = None
    start_date: Optional[str] = None
    tasks: List[Task] = field(default_factory=list)
    #: ``(upstream task key, downstream task key)``.
    edges: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class ModuleDags:
    dags: List[Dag] = field(default_factory=list)
    #: Findings that belong to the file, not to one DAG.
    notes: List[str] = field(default_factory=list)


@dataclass
class _DagDef:
    node: ast.AST  # the DAG(...) call, or the @dag function
    dag: Dag
    var: Optional[str] = None


def _is_airflow(qualified: Optional[str]) -> bool:
    return qualified is not None and qualified.split(".")[0] == "airflow"


def _last(qualified: str) -> str:
    return qualified.rsplit(".", 1)[-1]


class _Reader:
    def __init__(self, module: Module, folder: Folder, operators: Mapping[str, str]) -> None:
        self.module = module
        self.folder = folder
        self.operators = dict(operators)
        self.result = ModuleDags()
        self.defs: List[_DagDef] = []
        self.tasks_by_node: Dict[int, Tuple[Task, _DagDef]] = {}
        self.tasks_by_var: Dict[str, Task] = {}

    # ── entry ───────────────────────────────────────────────────────

    def run(self) -> ModuleDags:
        tree = self.module.tree
        nodes = sorted(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.Call, ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for node in nodes:
            if isinstance(node, ast.Call):
                if self._is_dag_call(node):
                    self._add_dag_call(node)
            elif any(self._is_dag_decorator(d) for d in node.decorator_list):
                self._add_dag_function(node)
        if not self.defs:
            self._no_dag_notes(nodes)
            return self.result
        for node in nodes:
            if isinstance(node, ast.Call):
                self._maybe_task(node)
            elif any(self._is_task_decorator(d) for d in node.decorator_list):
                self._add_taskflow_task(node)
        self._edges()
        for dag_def in self.defs:
            self.result.dags.append(dag_def.dag)
        return self.result

    def _no_dag_notes(self, nodes: Sequence[ast.AST]) -> None:
        for node in nodes:
            if isinstance(node, ast.Call):
                qualified = qualified_name(node.func, self.module.imports) or ""
                if _last(qualified) in {"load_yaml_dags", "DagFactory", "generate_dags"} and (
                    qualified.startswith("dagfactory")
                ):
                    self.result.notes.append(
                        f"{self.module.rel}:{node.lineno} DAGs generated from YAML by dag-factory: "
                        "not read"
                    )

    # ── DAG definitions ─────────────────────────────────────────────

    def _is_dag_call(self, call: ast.Call) -> bool:
        qualified = qualified_name(call.func, self.module.imports)
        return _is_airflow(qualified) and _last(qualified or "") == "DAG"

    def _is_dag_decorator(self, decorator: ast.expr) -> bool:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        qualified = qualified_name(target, self.module.imports)
        return _is_airflow(qualified) and _last(qualified or "") == "dag"

    def _is_task_decorator(self, decorator: ast.expr) -> bool:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        qualified = qualified_name(target, self.module.imports)
        if not _is_airflow(qualified):
            return False
        last = _last(qualified or "")
        return last == "task" or (qualified or "").endswith(".task.pyspark")

    @staticmethod
    def _kw(call: ast.Call, name: str, position: Optional[int] = None) -> Optional[ast.expr]:
        for keyword in call.keywords:
            if keyword.arg == name:
                return keyword.value
        if position is not None and len(call.args) > position:
            candidate = call.args[position]
            return None if isinstance(candidate, ast.Starred) else candidate
        return None

    def _text(self, node: Optional[ast.expr]) -> Any:
        return None if node is None else self.folder.fold(node)

    def _add_dag_call(self, call: ast.Call) -> None:
        id_node = self._kw(call, "dag_id", 0)
        dag_id = self._text(id_node)
        shown = (
            dag_id
            if isinstance(dag_id, str)
            else (dag_id.text if isinstance(dag_id, Unresolved) else snippet(call))
        )
        dag = Dag(
            dag_id=dag_id if isinstance(dag_id, str) and dag_id.strip() else None,
            shown=str(shown),
            file=self.module.rel,
            line=call.lineno,
        )
        if dag.dag_id is None:
            why = dag_id.why if isinstance(dag_id, Unresolved) else "no dag_id is given"
            dag.notes.append(f"the dag_id is not a constant ({why}): this DAG is not read further")
        self._dag_facts(dag, call)
        self._context_notes(dag, call)
        self.defs.append(_DagDef(node=call, dag=dag, var=self._bound_name(call)))

    def _add_dag_function(self, function: ast.AST) -> None:
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        decorator = next(d for d in function.decorator_list if self._is_dag_decorator(d))
        call = decorator if isinstance(decorator, ast.Call) else None
        id_node = self._kw(call, "dag_id") if call is not None else None
        dag_id = self._text(id_node) if id_node is not None else function.name
        dag = Dag(
            dag_id=dag_id if isinstance(dag_id, str) and dag_id.strip() else None,
            shown=(
                function.name
                if id_node is None
                else str(
                    dag_id if isinstance(dag_id, str) else getattr(dag_id, "text", function.name)
                )
            ),
            file=self.module.rel,
            line=function.lineno,
        )
        if dag.dag_id is None:
            dag.notes.append("the dag_id is not a constant: this DAG is not read further")
        if call is not None:
            self._dag_facts(dag, call)
        if not self._called_at_module_level(function.name):
            dag.notes.append(
                f"the @dag function {function.name}() is never called at module level, so Airflow "
                "does not create this DAG from it"
            )
        self.defs.append(_DagDef(node=function, dag=dag))

    def _called_at_module_level(self, name: str) -> bool:
        for statement in self.module.tree.body:
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == name
                ):
                    return True
        return False

    def _bound_name(self, call: ast.Call) -> Optional[str]:
        parent = self.module.parents.get(call)
        if (
            isinstance(parent, ast.Assign)
            and parent.targets
            and isinstance(parent.targets[0], ast.Name)
        ):
            return parent.targets[0].id
        if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
            return parent.target.id
        if isinstance(parent, ast.withitem) and isinstance(parent.optional_vars, ast.Name):
            return parent.optional_vars.id
        return None

    def _dag_facts(self, dag: Dag, call: ast.Call) -> None:
        # schedule: Airflow 3 has ``schedule`` only; 2.x also ``schedule_interval`` and ``timetable``
        for name in ("schedule", "schedule_interval", "timetable"):
            node = self._kw(call, name)
            if node is None:
                continue
            value = self._text(node)
            if value is None:
                dag.schedule_kind, dag.schedule = "none", None
            elif isinstance(value, str):
                dag.schedule_kind, dag.schedule = "cron", value
            elif isinstance(node, (ast.Call, ast.List, ast.Tuple)) or not isinstance(
                value, Unresolved
            ):
                # timedelta(...), a timetable, a list of datasets: written out, not a constant
                dag.schedule_kind, dag.schedule = "expression", snippet(node)
            else:
                dag.schedule_kind, dag.schedule = "unresolved", value.text or snippet(node)
                dag.notes.append(f"the schedule is not a constant ({value.why})")
            break
        default_args = self._text(self._kw(call, "default_args"))
        if default_args is not None:
            owner = lookup(default_args, "owner")
            if isinstance(owner, str):
                dag.owner = owner
            elif isinstance(owner, Unresolved):
                dag.notes.append(f"the default_args owner is not a constant ({owner.why})")
        tags = self._text(self._kw(call, "tags"))
        if isinstance(tags, list):
            dag.tags = tuple(t for t in tags if isinstance(t, str))
        description = self._text(self._kw(call, "description"))
        dag.description = description if isinstance(description, str) else None
        catchup = self._text(self._kw(call, "catchup"))
        dag.catchup = catchup if isinstance(catchup, bool) else None
        start = self._kw(call, "start_date")
        dag.start_date = snippet(start, 60) if start is not None else None

    def _context_notes(self, dag: Dag, call: ast.Call) -> None:
        for ancestor in ancestors(call, self.module.parents):
            if isinstance(ancestor, _LOOPS):
                dag.notes.append("this DAG is built in a loop: its id and tasks depend on the loop")
                return
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not any(self._is_dag_decorator(d) for d in ancestor.decorator_list):
                    dag.notes.append(
                        f"this DAG is built by the function {ancestor.name}(): its id and tasks may "
                        "depend on how it is called"
                    )
                return
            if isinstance(ancestor, ast.If):
                dag.notes.append("this DAG is defined under a condition")
                return

    # ── tasks ───────────────────────────────────────────────────────

    def _operator_kind(self, call: ast.Call) -> Optional[Tuple[str, str, str, bool]]:
        """``(class name, qualified name, kind, mapped)`` when ``call`` builds an operator we
        read; ``mapped`` for ``Operator.partial(...)``, whose ``.expand(...)`` makes the runs."""
        func = call.func
        mapped = isinstance(func, ast.Attribute) and func.attr == "partial"
        qualified = qualified_name(func.value if mapped else func, self.module.imports)  # type: ignore[attr-defined]
        if qualified is None:
            return None
        name = _last(qualified)
        if name in self.operators:
            return name, qualified, self.operators[name], mapped
        if _is_airflow(qualified) and name.endswith(("Operator", "Sensor")):
            return name, qualified, OPERATOR_KINDS.get(name, "other"), mapped
        return None

    def _owner_def(self, node: ast.AST, call: Optional[ast.Call]) -> Optional[_DagDef]:
        for ancestor in ancestors(node, self.module.parents):
            if isinstance(ancestor, (ast.With, ast.AsyncWith)):
                for item in ancestor.items:
                    for dag_def in self.defs:
                        if item.context_expr is dag_def.node:
                            return dag_def
                        if (
                            isinstance(item.context_expr, ast.Name)
                            and dag_def.var is not None
                            and item.context_expr.id == dag_def.var
                        ):
                            return dag_def
            elif isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dag_def in self.defs:
                    if ancestor is dag_def.node:
                        return dag_def
        if call is not None:
            dag_arg = self._kw(call, "dag")
            if isinstance(dag_arg, ast.Name):
                for dag_def in self.defs:
                    if dag_def.var == dag_arg.id:
                        return dag_def
        return self.defs[0] if len(self.defs) == 1 else None

    def _maybe_task(self, call: ast.Call) -> None:
        found = self._operator_kind(call)
        if found is None:
            return
        name, _qualified, kind, mapped = found
        dag_def = self._owner_def(call, call)
        if dag_def is None:
            self.result.notes.append(
                f"{self.module.rel}:{call.lineno} {name} is built outside any DAG this file "
                "defines, so it is not attached to one"
            )
            return
        task_id = self._text(self._kw(call, "task_id", 0))
        resolved_id = task_id if isinstance(task_id, str) and task_id.strip() else None
        key = f"{call.lineno}:{call.col_offset}"
        task = Task(
            key=key,
            task_id=resolved_id,
            shown=resolved_id
            or (task_id.text if isinstance(task_id, Unresolved) else snippet(call, 40)),
            var=self._bound_name(call),
            operator=name,
            kind=kind,
            owner=None,
            file=self.module.rel,
            line=call.lineno,
        )
        if resolved_id is None:
            task.notes.append("the task_id is not a constant")
        if mapped:
            task.notes.append(
                "this task is mapped with .partial() and .expand(): the number of runs is not "
                "known statically"
            )
        owner = self._text(self._kw(call, "owner"))
        if isinstance(owner, str):
            task.owner = owner
        for ancestor in ancestors(call, self.module.parents):
            if isinstance(ancestor, _LOOPS):
                task.notes.append("this operator is built in a loop: there may be several tasks")
                break
        if kind in SUBMIT_KINDS:
            task.submits = self._submits(kind, call)
        dag_def.dag.tasks.append(task)
        self.tasks_by_node[id(call)] = (task, dag_def)
        if task.var:
            self.tasks_by_var[task.var] = task

    def _add_taskflow_task(self, function: ast.AST) -> None:
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        dag_def = self._owner_def(function, None)
        if dag_def is None:
            return
        decorator = next(d for d in function.decorator_list if self._is_task_decorator(d))
        task_id = function.name
        if isinstance(decorator, ast.Call):
            explicit = self._text(self._kw(decorator, "task_id"))
            if isinstance(explicit, str):
                task_id = explicit
        task = Task(
            key=f"{function.lineno}:{function.col_offset}",
            task_id=task_id,
            shown=task_id,
            var=None,
            operator="task",
            kind="python",
            owner=None,
            file=self.module.rel,
            line=function.lineno,
        )
        dag_def.dag.tasks.append(task)
        self.tasks_by_node[id(function)] = (task, dag_def)
        for node in ast.walk(self.module.tree):  # ``extracted = extract()`` binds the task
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == function.name
                and node.targets
                and isinstance(node.targets[0], ast.Name)
            ):
                self.tasks_by_var[node.targets[0].id] = task

    # ── what a task submits ─────────────────────────────────────────

    def _submits(self, kind: str, call: ast.Call) -> List[Submit]:
        reader = {
            "spark_submit": self._spark_submit,
            "bash": self._bash,
            "emr_add_steps": self._emr_steps,
            "emr_create_job_flow": self._emr_job_flow,
            "emr_containers": self._emr_containers,
            "emr_serverless": self._emr_serverless,
            "glue_job": self._glue,
            "databricks_submit": self._databricks,
        }[kind]
        try:
            return reader(call)
        except RecursionError:
            return []

    def _submit(
        self, runtime: str, script: Any, args: Any, call: ast.Call, shown: str = ""
    ) -> Submit:
        if isinstance(script, str) and script.strip():
            return Submit(
                runtime, script.strip(), "", script.strip(), _arg_texts(args), call.lineno
            )
        why = script.why if isinstance(script, Unresolved) else "no script is named"
        text = script.text if isinstance(script, Unresolved) else shown
        return Submit(runtime, None, why, text, _arg_texts(args), call.lineno)

    def _spark_submit(self, call: ast.Call) -> List[Submit]:
        script = self._text(self._kw(call, "application"))
        args = self._text(self._kw(call, "application_args"))
        return [self._submit("spark-submit", script, args, call, "application")]

    def _glue(self, call: ast.Call) -> List[Submit]:
        script = self._text(self._kw(call, "script_location"))
        raw = self._text(self._kw(call, "script_args"))
        args: List[Any] = []
        if isinstance(raw, dict):
            for key, value in raw.items():
                args.extend([str(key), value])
        return [self._submit("glue", script, args, call, "script_location")]

    def _databricks(self, call: ast.Call) -> List[Submit]:
        task = self._text(self._kw(call, "spark_python_task"))
        if task is None:
            body = self._text(self._kw(call, "json"))
            task = lookup(body, "spark_python_task") if isinstance(body, dict) else None
        if not isinstance(task, dict):
            return []
        script = lookup(task, "python_file")
        args = lookup(task, "parameters")
        return [
            self._submit(
                "databricks",
                None if script is ABSENT else script,
                None if args is ABSENT else args,
                call,
                "spark_python_task.python_file",
            )
        ]

    def _bash(self, call: ast.Call) -> List[Submit]:
        command = self._text(self._kw(call, "bash_command"))
        if isinstance(command, Unresolved) and "spark-submit" in command.text:
            command = command.text  # a literal script survives a field that did not fold
        if not isinstance(command, str):
            return []
        found = _spark_submit_in_text(command)
        if found is None:
            return []
        script, args = found
        return [self._submit("bash", script, args, call, "bash_command")]

    def _steps_of(self, steps: Any, call: ast.Call, runtime: str) -> List[Submit]:
        if isinstance(steps, str):
            return [
                self._submit(
                    runtime,
                    Unresolved("the steps are in a file, which is not read", steps),
                    None,
                    call,
                )
            ]
        if not isinstance(steps, list):
            if isinstance(steps, Unresolved):
                return [self._submit(runtime, Unresolved(steps.why, steps.text), None, call)]
            return []
        out: List[Submit] = []
        for step in steps[:_MAX_STEPS]:
            hadoop = lookup(step, "HadoopJarStep") if isinstance(step, dict) else None
            argv = lookup(hadoop, "Args") if isinstance(hadoop, dict) else None
            if not isinstance(argv, list):
                continue
            tokens = [a for a in argv if isinstance(a, (str, Unresolved))]
            found = _spark_submit_in_tokens(tokens)
            if found is not None:
                out.append(self._submit(runtime, found[0], found[1], call))
        return out

    def _emr_steps(self, call: ast.Call) -> List[Submit]:
        return self._steps_of(self._text(self._kw(call, "steps")), call, "emr-step")

    def _emr_job_flow(self, call: ast.Call) -> List[Submit]:
        overrides = self._text(self._kw(call, "job_flow_overrides"))
        if isinstance(overrides, str):
            return self._steps_of(overrides, call, "emr-step")
        if not isinstance(overrides, dict):
            return []
        steps = lookup(overrides, "Steps")
        return [] if steps is ABSENT else self._steps_of(steps, call, "emr-step")

    def _driver(self, call: ast.Call, name: str, runtime: str) -> List[Submit]:
        driver = self._text(self._kw(call, "job_driver"))
        spark = lookup(driver, name) if isinstance(driver, dict) else None
        if not isinstance(spark, dict):
            return []
        entry = lookup(spark, "entryPoint")
        args = lookup(spark, "entryPointArguments")
        return [
            self._submit(
                runtime,
                None if entry is ABSENT else entry,
                None if args is ABSENT else args,
                call,
                "entryPoint",
            )
        ]

    def _emr_containers(self, call: ast.Call) -> List[Submit]:
        return self._driver(call, "sparkSubmitJobDriver", "emr-eks")

    def _emr_serverless(self, call: ast.Call) -> List[Submit]:
        return self._driver(call, "sparkSubmit", "emr-serverless")

    # ── dependencies ────────────────────────────────────────────────

    def _operand_tasks(self, node: ast.AST) -> Optional[List[Task]]:
        """The tasks an operand of ``>>`` stands for; ``None`` when it is not a task."""
        if isinstance(node, ast.Name):
            task = self.tasks_by_var.get(node.id)
            return [task] if task else None
        if isinstance(node, ast.Call):
            entry = self.tasks_by_node.get(id(node))
            if entry:
                return [entry[0]]
            if isinstance(node.func, ast.Name):  # a @task function called inline
                for definition in ast.walk(self.module.tree):
                    if (
                        isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and definition.name == node.func.id
                        and id(definition) in self.tasks_by_node
                    ):
                        return [self.tasks_by_node[id(definition)][0]]
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            return self._operand_tasks(node.right)  # ``a >> b`` evaluates to ``b``
        if isinstance(node, (ast.List, ast.Tuple)):
            tasks: List[Task] = []
            for element in node.elts:
                part = self._operand_tasks(element)
                if part is None:
                    return None
                tasks.extend(part)
            return tasks
        return None

    def _link(self, upstream: Sequence[Task], downstream: Sequence[Task]) -> None:
        for up in upstream:
            for down in downstream:
                for dag_def in self.defs:
                    keys = {t.key for t in dag_def.dag.tasks}
                    if up.key in keys and down.key in keys:
                        edge = (up.key, down.key)
                        if edge not in dag_def.dag.edges and up.key != down.key:
                            dag_def.dag.edges.append(edge)

    def _edges(self) -> None:
        for node in ast.walk(self.module.tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
                # ``a >> b >> c`` is ``(a >> b) >> c``: the chain is read from its outermost node
                parent = self.module.parents.get(node)
                if (
                    isinstance(parent, ast.BinOp)
                    and parent.left is node
                    and isinstance(parent.op, (ast.RShift, ast.LShift))
                ):
                    continue
                self._chain(node)
            elif isinstance(node, ast.Call):
                self._call_edges(node)

    def _chain(self, node: ast.BinOp) -> None:
        operands: List[ast.AST] = []
        ops: List[ast.operator] = []
        current: ast.AST = node
        while isinstance(current, ast.BinOp) and isinstance(current.op, (ast.RShift, ast.LShift)):
            operands.append(current.right)
            ops.append(current.op)
            current = current.left
        operands.append(current)
        operands.reverse()
        ops.reverse()
        for index, op in enumerate(ops):
            left = self._operand_tasks(operands[index])
            right = self._operand_tasks(operands[index + 1])
            if left is None or right is None:
                continue
            if isinstance(op, ast.RShift):
                self._link(left, right)
            else:
                self._link(right, left)

    def _call_edges(self, call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in {"set_downstream", "set_upstream"}:
            owner = self._operand_tasks(func.value)
            other = self._operand_tasks(call.args[0]) if call.args else None
            if owner is not None and other is not None:
                if func.attr == "set_downstream":
                    self._link(owner, other)
                else:
                    self._link(other, owner)
            return
        qualified = qualified_name(func, self.module.imports) or ""
        if not _is_airflow(qualified):
            return
        last = _last(qualified)
        if last == "chain":
            groups = [self._operand_tasks(a) for a in call.args]
            if any(isinstance(a, ast.Starred) for a in call.args):
                self.result.notes.append(
                    f"{self.module.rel}:{call.lineno} chain(*...) takes its tasks from a list: "
                    "its dependencies are not read"
                )
                return
            for left, right in zip(groups, groups[1:], strict=False):
                if left is not None and right is not None:
                    self._link(left, right)
        elif last == "cross_downstream" and len(call.args) >= 2:
            left, right = self._operand_tasks(call.args[0]), self._operand_tasks(call.args[1])
            if left is not None and right is not None:
                self._link(left, right)


_SPARK_SUBMIT = re.compile(r"(^|/)spark-submit(\.sh)?$")


def _arg_texts(args: Any) -> Tuple[str, ...]:
    """Argument values as text; one that did not fold shows as ``<not constant>``."""
    if not isinstance(args, (list, tuple)):
        return ()
    out: List[str] = []
    for item in args:
        if isinstance(item, Unresolved):
            out.append("<not constant>")
        elif isinstance(item, (str, int, float, bool)) or item is None:
            out.append(str(item))
        else:
            out.append("<not constant>")
    return tuple(out)


def _spark_submit_in_tokens(tokens: Sequence[Any]) -> Optional[Tuple[Any, List[Any]]]:
    """``(script, args)`` of a ``spark-submit`` command given as tokens: the first ``.py``
    token after ``spark-submit`` is the script, and what follows it the arguments. ``None``
    when the command is not a ``spark-submit``."""
    start = None
    for index, token in enumerate(tokens):
        if isinstance(token, str) and _SPARK_SUBMIT.search(token):
            start = index
            break
    if start is None:
        for token in tokens:  # ["bash", "-c", "spark-submit job.py ..."]
            if isinstance(token, str) and " " in token and "spark-submit" in token:
                return _spark_submit_in_text(token)
        return None
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if isinstance(token, str) and token.endswith(".py"):
            return token, list(tokens[index + 1 :])
        if isinstance(token, Unresolved):
            return Unresolved("the script is not a constant", token.text), list(tokens[index + 1 :])
    return Unresolved("the spark-submit command names no Python script", ""), []


def _spark_submit_in_text(command: str) -> Optional[Tuple[Any, List[str]]]:
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None
    return _spark_submit_in_tokens(tokens)


def read_dags(
    module: Module, folder: Folder, *, operators: Optional[Mapping[str, str]] = None
) -> ModuleDags:
    """The DAGs of ``module``. ``operators`` maps a project's own operator class name to one
    of :data:`SUBMIT_KINDS` (from the conventions)."""
    return _Reader(module, folder, operators or {}).run()


__all__ = [
    "Dag",
    "ModuleDags",
    "OPERATOR_KINDS",
    "SUBMIT_KINDS",
    "Submit",
    "Task",
    "read_dags",
]
