# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every DAG forge's generators write loads in a real Airflow 2 and Airflow 3.

``tests/test_airflow3_dag_compat.py`` checks the source of each generator's DAG (no
``schedule_interval``, no ``days_ago``, each moved name imported from its Airflow 3 home
first); ``test_fluid_apply_airflow_live.py`` loads the ``fluid apply`` DAGs. This loads
the rest, every entry of ``RENDERS``, in the ``DagBag`` of a real Airflow, and checks:

* no import error;
* no warning Airflow raises for a name it removed or moved (``RemovedInAirflow3Warning``
  on 2.x, the deprecation warnings 3.x raises for its old paths);
* the Dag ids Airflow built are the ones ``fluid schedule-sync --airflow-url`` reads from
  the file (``_airflow_sync_check.dag_files``), so the check waits for the right Dags.

Airflow is not a test dependency, so this runs only against an Airflow you name: set
``FLUID_AIRFLOW2_PYTHON`` and/or ``FLUID_AIRFLOW3_PYTHON`` to the interpreter of a
virtualenv outside the repository, with the providers the renders import::

    uv venv --python 3.12 af3
    uv pip install --python af3/bin/python "apache-airflow[amazon,google,snowflake]==3.4.0" \\
        --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt
    FLUID_AIRFLOW3_PYTHON=af3/bin/python pytest tests/schedulers/test_generated_dags_airflow_live.py

A render whose import fails only because a provider's own dependency is missing from that
virtualenv (``apache_beam`` for the Dataflow operator) is reported and skipped, never
passed: any other import error fails.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _airflow_sync_check as chk

# One DagBag per render: renders of different generators share Dag ids (``sales_orders``,
# ``orders``), which one bag would refuse as duplicates.
_PARSE = r"""
import json, os, sys, warnings
caught = []
def _show(message, category, filename, lineno, file=None, line=None):
    caught.append({"category": category.__name__, "message": str(message)[:300],
                   "filename": str(filename)})
warnings.showwarning = _show
warnings.simplefilter("always")
try:
    from airflow.dag_processing.dagbag import DagBag
except ImportError:
    from airflow.models.dagbag import DagBag
from importlib import metadata
import airflow
out = {"version": airflow.__version__, "errors": {}, "dags": {}, "captured": {}}
root = sys.argv[1]
for sub in sorted(os.listdir(root)):
    folder = os.path.join(root, sub)
    try:
        bag = DagBag(folder, include_examples=False)
    except TypeError:  # Airflow 3.4 reads AIRFLOW__CORE__LOAD_EXAMPLES only
        bag = DagBag(folder)
    out["errors"].update({k: str(v)[-1500:] for k, v in bag.import_errors.items()})
    out["dags"].update({f"{sub}:{d}": dag.fileloc for d, dag in bag.dags.items()})
    captured = getattr(bag, "captured_warnings", {}) or {}
    out["captured"].update({k: [str(w)[:300] for w in v] for k, v in captured.items()})
out["warnings"] = [w for w in caught if w["filename"].startswith(root)]
out["providers"] = sorted(
    (d.metadata["Name"] or "").lower() for d in metadata.distributions()
    if (d.metadata["Name"] or "").lower().startswith("apache-airflow-providers-"))
print(json.dumps(out))
"""

#: Airflow's own warnings that a name the DAG uses is gone or going; a deprecation warning
#: from a library underneath (protobuf's notice about Python 3.14) is not the DAG's.
_AIRFLOW_WARNINGS = re.compile(
    r"RemovedInAirflow3Warning|AirflowProviderDeprecationWarning|DeprecatedImportWarning"
)


def _removal(category: str, text: str) -> bool:
    if _AIRFLOW_WARNINGS.search(category) or _AIRFLOW_WARNINGS.search(text):
        return True
    return "Deprecat" in category + text and "airflow" in text.lower()


_MISSING = re.compile(r"No module named '([^']+)'")


def _render_all(tmp_path: Path) -> Dict[str, str]:
    from tests.test_airflow3_dag_compat import RENDERS

    rendered: Dict[str, str] = {}
    for name, render in sorted(RENDERS.items()):
        workdir = tmp_path / "work" / name.replace(" ", "_")
        workdir.mkdir(parents=True)
        file_name = re.sub(r"[^A-Za-z0-9_]", "_", name) + ".py"
        rendered[file_name] = render(workdir)
    return rendered


def _skippable(error: str, providers: List[str]) -> str:
    """Why an import error is the virtualenv's, not the DAG's; ``""`` when it is the DAG's."""
    match = _MISSING.search(error)
    if not match:
        return ""
    module = match.group(1)
    if not module.startswith("airflow"):
        return f"{module} (a provider dependency) is not installed"
    parts = module.split(".")
    if parts[:2] == ["airflow", "providers"] and len(parts) > 2:
        # apache-airflow-providers-<a>[-<b>]: amazon, apache-beam, cncf-kubernetes ...
        names = ["-".join(parts[2:4]), parts[2]] if len(parts) > 3 else [parts[2]]
        dists = ["apache-airflow-providers-" + n.replace("_", "-") for n in names]
        if not any(d in providers for d in dists):
            return f"{module} is not installed (no {' or '.join(dists)})"
    return ""


@pytest.mark.parametrize("major", ["2", "3"])
def test_every_generated_dag_loads_in_airflow(major: str, tmp_path: Path) -> None:
    python = os.environ.get(f"FLUID_AIRFLOW{major}_PYTHON")
    if not python:
        pytest.skip(f"set FLUID_AIRFLOW{major}_PYTHON to an Airflow {major} interpreter")
    dags_dir = tmp_path / "dags"
    dags_dir.mkdir()
    for file_name, source in _render_all(tmp_path).items():
        folder = dags_dir / file_name[: -len(".py")]
        folder.mkdir()
        (folder / file_name).write_text(source, encoding="utf-8")
    env_vars = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "AIRFLOW_HOME": str(tmp_path / "airflow"),
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__DAGS_FOLDER": str(dags_dir),
    }
    proc = subprocess.run(  # noqa: S603 - the interpreter the operator named
        [python, "-I", "-c", _PARSE, str(dags_dir)],
        env=env_vars,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    result: Dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["version"].startswith(f"{major}."), result["version"]

    skipped: Dict[str, str] = {}
    failures: List[str] = []
    for path, error in result["errors"].items():
        name = Path(path).name
        why = _skippable(error, result["providers"])
        if why:
            skipped[name] = why
        else:
            failures.append(f"{name}: {error[-600:]}")
    assert not failures, "\n".join(failures)

    removal = [
        f"{Path(w['filename']).name}: {w['category']}: {w['message']}"
        for w in result["warnings"]
        if _removal(w["category"], w["message"])
    ] + [
        f"{Path(path).name}: {text}"
        for path, texts in result["captured"].items()
        for text in texts
        if _removal("", text)
    ]
    assert not removal, "\n".join(removal)

    built: Dict[str, List[str]] = {}
    for key, fileloc in result["dags"].items():
        built.setdefault(Path(fileloc).name, []).append(key.split(":", 1)[1])
    read = {Path(f.path).name: sorted(f.dag_ids) for f in chk.dag_files(dags_dir)}
    loaded = [name for name in read if name not in skipped]
    assert loaded, f"every render was skipped: {skipped}"
    for name in loaded:
        assert sorted(built.get(name, [])) == read[name], name
    # The renders that need no provider at all always load.
    for name in ("fluid_init.py", "scaffold_composer.py", "schedule_sync.py"):
        assert name in loaded, (name, skipped)
    if skipped:
        print(f"[airflow {result['version']}] skipped: {json.dumps(skipped, sort_keys=True)}")
