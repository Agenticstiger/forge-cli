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

"""Compatibility gate (scenario catalog area M): a contract with no ``$ref`` is read as it was.

Spec (catalog section 1, item 13): a contract with no ``$ref`` composes and
exports byte-identically to v0.24.0.

The byte-for-byte scenarios (M-01, M-08, M-09, M-15, M-18) are differential:
the same files go through the CLI of the v0.24.0 release (``git archive
v0.24.0 fluid_build`` into a temp directory, nothing checked out) and through
this checkout, and the masked outputs are compared. They skip when the tag is
not in the checkout's history (a shallow clone). The corpus is every contract
under ``examples/`` and ``fluid_build/templates/`` that holds no ``$ref``. An
ODCS export may differ by the one line the 0.7.6 promotion (#752) moved, and
only as the default moved (see ``_ODCS_OUTPUTS``).

The baseline-harness scenarios (M-02 to M-07, M-13) are not here: they are run
by ``repro/baseline/run_baseline.sh`` outside pytest.

A test marked ``xfail(strict=True)`` pins the spec where the code disagrees;
it turns into a failure the day the code is fixed, so the marker gets removed.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

import pytest
import yaml

from fluid_build import loader

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_TAG = "v0.24.0"


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _the_checkout_under_test_is_the_one_that_runs() -> None:
    loaded = Path(loader.__file__).resolve()
    assert loaded.is_relative_to(REPO_ROOT), f"{loaded} is not inside the checkout {REPO_ROOT}"


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")  # ``main()`` sets it; restored on teardown
    _clear_registries()
    yield
    _clear_registries()


def _clear_registries() -> None:
    for name, value in list(vars(loader).items()):
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture(scope="session")
def release_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ``fluid_build`` package of v0.24.0, extracted from git into a temp directory."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed, so v0.24.0 cannot be extracted")
    probe = subprocess.run(
        [
            git,
            "-C",
            str(REPO_ROOT),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{RELEASE_TAG}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"{RELEASE_TAG} is not in this checkout's history (a shallow clone?)")
    archive = subprocess.run(
        [git, "-C", str(REPO_ROOT), "archive", "--format=tar", RELEASE_TAG, "fluid_build"],
        capture_output=True,
        check=True,
    )
    dest = tmp_path_factory.mktemp("release_v0_24_0")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python without extraction filters
            tar.extractall(dest)  # nosec B202 - a git archive of this repository
    assert (dest / "fluid_build" / "loader.py").is_file()
    return dest


# ── the differential run ────────────────────────────────────────────────

#: Runs CLI jobs in ONE process (a fresh interpreter per job would take ten minutes) and
#: records each job's exit code, stdout and stderr (captured at the file-descriptor level).
_DRIVER = r"""
import json, os, sys, tempfile, time, traceback


def run_job(argv, cwd):
    from fluid_build.cli import main

    os.chdir(cwd)
    out_f = tempfile.TemporaryFile(mode="w+b")
    err_f = tempfile.TemporaryFile(mode="w+b")
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    os.dup2(out_f.fileno(), 1)
    os.dup2(err_f.fileno(), 2)
    rc, exc = None, None
    try:
        try:
            rc = main(list(argv))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except BaseException as e:  # noqa: BLE001
            exc = "".join(traceback.format_exception_only(type(e), e)).strip()
            rc = -1
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
    out_f.seek(0)
    err_f.seek(0)
    return {
        "rc": rc,
        "exc": exc,
        "stdout": out_f.read().decode("utf-8", "replace"),
        "stderr": err_f.read().decode("utf-8", "replace"),
    }


def drive(jobs_file, results_file):
    import fluid_build

    jobs = json.load(open(jobs_file))
    results = {"fluid_build_file": fluid_build.__file__, "jobs": {}}
    started = time.time()
    for job in jobs:
        results["jobs"][job["name"]] = run_job(job["argv"], job["cwd"])
    results["seconds"] = time.time() - started
    json.dump(results, open(results_file, "w"))


drive(sys.argv[1], sys.argv[2])
"""

#: name -> (the file the command writes, argv with ``{c}`` the contract and ``{o}`` the out dir)
_COMMANDS: Dict[str, Tuple[Optional[str], List[str]]] = {
    "bundle": ("bundle.yaml", ["bundle", "{c}", "--out", "{o}/bundle.yaml"]),
    "odcs": (
        "odcs.yaml",
        ["generate", "standard", "{c}", "--format", "odcs", "--out", "{o}/odcs.yaml"],
    ),
    "odps": (
        "odps.yaml",
        ["generate", "standard", "{c}", "--format", "odps", "--out", "{o}/odps.yaml"],
    ),
    "odps41": (
        "odps-v4.1.json",
        ["generate", "standard", "{c}", "--format", "odps-v4.1", "--out", "{o}/odps-v4.1.json"],
    ),
    "validate": (None, ["validate", "{c}", "--offline", "--format", "json"]),
    "validate-text": (None, ["validate", "{c}", "--offline"]),
    "plan": ("plan.json", ["plan", "{c}", "--out", "{o}/plan.json"]),
    # the same, for the prod environment
    "bundle-prod": (
        "bundle-prod.yaml",
        ["bundle", "{c}", "--env", "prod", "--out", "{o}/bundle-prod.yaml"],
    ),
    "odcs-prod": (
        "odcs-prod.yaml",
        ["generate", "standard", "{c}", "--format", "odcs", "--env", "prod"]
        + ["--out", "{o}/odcs-prod.yaml"],
    ),
    "validate-prod": (None, ["validate", "{c}", "--offline", "--env", "prod", "--format", "json"]),
    "validate-text-prod": (None, ["validate", "{c}", "--offline", "--env", "prod"]),
    "plan-prod": (
        "plan-prod.json",
        ["plan", "{c}", "--env", "prod", "--out", "{o}/plan-prod.json"],
    ),
}
_CORPUS_COMMANDS = ("bundle", "odcs", "odps", "odps41", "validate", "plan")
_CUSTOM_COMMANDS = ("bundle", "odcs", "validate", "validate-text")
_OVERLAY_COMMANDS = (
    "bundle-prod",
    "odcs-prod",
    "validate-prod",
    "validate-text-prod",
    "plan-prod",
)
_OVERLAY_FIXTURES = ("overlay-076", "overlay-075")


def _min() -> Dict[str, Any]:
    """A valid 0.7.6 contract with no ``$ref`` (the catalog's MIN, with two columns)."""
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "compat.min",
        "name": "Compat min",
        "domain": "ops",
        "description": {"purpose": "p", "usage": "u"},
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "builds": [
            {
                "id": "transform",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1 AS id"},
            }
        ],
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "runtime/out/out.csv"},
                },
                "contract": {
                    "schema": [{"name": "id", "type": "integer"}, {"name": "n", "type": "string"}]
                },
            }
        ],
    }


def _yaml(doc: Dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False)


def _json(doc: Any) -> str:
    return json.dumps(doc, indent=2) + "\n"


_ANCHORS = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: compat.anchors
name: Compat anchors
domain: ops
metadata:
  layer: Silver
  owner: &owner {team: t, email: t@example.com}
exposes:
  - exposeId: first
    kind: table
    binding: &binding {platform: local, format: csv, location: {path: runtime/out/a.csv}}
    contract: &contract
      schema: [{name: id, type: integer}]
  - exposeId: second
    kind: table
    binding: *binding
    contract: *contract
"""


def _overlaid(version: str) -> Dict[str, str]:
    """Two builds and two exposes, and an overlay that lists them in REVERSE order.

    From 0.7.6 an overlay entry patches the entry with its id; before that it
    patches the entry at its position, so the same overlay retargets (and warns).
    """
    doc = _min()
    doc["fluidVersion"] = version
    if version == "0.7.5":
        doc["description"] = "Compat min"
    doc["builds"].append(
        {
            "id": "rollup",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1 AS id"},
        }
    )
    second = json.loads(json.dumps(doc["exposes"][0]))
    second["exposeId"] = "summary"
    second["binding"]["location"]["path"] = "runtime/out/summary.csv"
    doc["exposes"].append(second)
    overlay = {
        "exposes": [
            {
                "exposeId": "summary",
                "binding": {"location": {"path": "runtime/out/summary-prod.csv"}},
            },
            {"exposeId": "out", "binding": {"location": {"path": "runtime/out/out-prod.csv"}}},
        ],
        "builds": [{"id": "rollup", "properties": {"sql": "SELECT 2 AS id"}}],
    }
    return {"contract.fluid.yaml": _yaml(doc), "overlays/prod.yaml": _yaml(overlay)}


def _custom_fixtures() -> Dict[str, Dict[str, str]]:
    """name -> {relative path: text}; the first path is the contract."""

    def bad(edit: Any) -> Dict[str, str]:
        doc = _min()
        edit(doc)
        return {"contract.fluid.yaml": _yaml(doc)}

    flat_json = _min()
    split_json = _min()
    split_json["metadata"]["owner"] = {"$ref": "./owner.json"}
    split_json["exposes"] = [{"$ref": "./fragments/expose.json"}]
    return {
        # M-09: schema errors in a contract with no ref and no overlay.
        "bad-type": bad(lambda d: d["exposes"][0]["contract"]["schema"][1].update(type=42)),
        "bad-root-key": bad(lambda d: d.update(bogus=1)),
        "missing-expose-id": bad(lambda d: d["exposes"][0].pop("exposeId")),
        "bad-kind": bad(lambda d: d["exposes"][0].update(kind="not_a_kind")),
        "bad-build-property": bad(
            lambda d: d["builds"][0].update(properties={"query": "SELECT 1 AS id"})
        ),
        # M-15: a .json root with .json fragments, none of the new features.
        "json-flat": {"contract.fluid.json": _json(flat_json)},
        "json-split": {
            "contract.fluid.json": _json(split_json),
            "owner.json": _json(_min()["metadata"]["owner"]),
            "fragments/expose.json": _json(_min()["exposes"][0]),
        },
        # M-18: a root written with YAML anchors and aliases.
        "anchors": {"contract.fluid.yaml": _ANCHORS},
        # No ref, but an overlay: keyed merge from 0.7.6, positional (with warnings) before.
        "overlay-076": _overlaid("0.7.6"),
        "overlay-075": _overlaid("0.7.5"),
    }


def _has_ref_key(node: Any) -> bool:
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "$ref" in item:
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _corpus(source_root: Path) -> List[Path]:
    """Every contract under ``examples/`` and ``templates/`` with no ``$ref``, sorted."""
    found: List[Path] = []
    for top in ("examples", "templates"):
        for path in sorted((source_root / top).rglob("*")):
            if path.suffix not in (".yaml", ".yml", ".json") or not path.is_file():
                continue
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a template with placeholders is not a contract
                continue
            if isinstance(doc, dict) and "fluidVersion" in doc and not _has_ref_key(doc):
                found.append(path)
    return found


class Side:
    """One tree's run: its jobs' results and the files they wrote."""

    def __init__(self, tag: str, base: Path, payload: Dict[str, Any]) -> None:
        self.tag = tag
        self.base = base
        self.jobs: Dict[str, Dict[str, Any]] = payload["jobs"]
        self.loaded_from = Path(payload["fluid_build_file"])

    def mask(self, text: str) -> str:
        for variant in {str(self.base), str(self.base.resolve())}:
            text = text.replace(variant, "<W>")
        text = re.sub(r'("(?:created|updated)": ")\d{4}-\d{2}-\d{2}(")', r"\1<DATE>\2", text)
        text = re.sub(r'("validation_time": )[0-9.eE+-]+', r"\1<SECONDS>", text)
        text = re.sub(r'("generated_at": )[0-9.eE+-]+', r"\1<TS>", text)
        text = re.sub(r'("time": ")\d{4}-\d{2}-\d{2}[T ][0-9:,.]+Z?(")', r"\1<TIME>\2", text)
        return re.sub(r"completed in [0-9.]+s", "completed in <SECONDS>s", text)

    def job(self, name: str) -> Dict[str, Any]:
        raw = self.jobs[name]
        return {**raw, "stdout": self.mask(raw["stdout"]), "stderr": self.mask(raw["stderr"])}

    def out_file(self, directory: str, filename: str) -> Optional[str]:
        path = self.base / "out" / directory / filename
        return self.mask(path.read_text(encoding="utf-8")) if path.is_file() else None

    def raw_out_file(self, directory: str, filename: str) -> Optional[str]:
        path = self.base / "out" / directory / filename
        return path.read_text(encoding="utf-8") if path.is_file() else None


class Differential(NamedTuple):
    old: Side
    new: Side
    corpus: List[str]  # relative paths, index = the job prefix


def _diff(a: str, b: str, limit: int = 12) -> str:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "v0.24.0", "checkout", n=1))
    return "\n".join(lines[:limit]) or "(whitespace only)"


# v0.24.0's ODCS export recorded the contract's ``fluidVersion`` in its ``fluidExtras`` blob
# only when it was not the exporting CLI's default, 0.7.5. Since #752 promoted 0.7.6 it is
# recorded always (a re-import takes a missing one from the READER's default), so a 0.7.5
# contract's export now carries the line. That is a deliberate change of the promotion, not of
# anything this gate guards. The ODCS comparisons take the line out of both sides, require
# that it appeared exactly where v0.24.0 left it out, and compare the rest byte for byte: any
# other difference, or the line moving any other way, still fails. Masking the line outright,
# or re-recording the outputs, would have accepted either.
_ODCS_OUTPUTS = frozenset({"odcs.yaml", "odcs-prod.yaml"})
_DEFAULT_THEN = "0.7.5"  # v0.24.0's latest_bundled_version()
_EXTRAS_ROOT = ["- property: fluidExtras\n", "  value:\n", "    root:\n"]


def _indent(lines: List[str], at: int) -> int:
    """The indentation of ``lines[at]``; -1 past the end, which closes every block."""
    return len(lines[at]) - len(lines[at].lstrip(" ")) if at < len(lines) else -1


def _without_recorded_version(document: str) -> Tuple[str, Optional[str]]:
    """One ODCS document without its fluidExtras ``root.fluidVersion``, and that version.

    The line goes with whatever it alone kept there: ``root:`` when the version was all
    ``root`` held, and the whole fluidExtras property when ``root`` was all it held.
    """
    lines = document.splitlines(keepends=True)
    for first in range(3, len(lines)):
        if lines[first - 3 : first] != _EXTRAS_ROOT:
            continue
        after = first  # one past root's block: its children sit at indentation 6 or more
        while _indent(lines, after) >= 6:
            after += 1
        at = next(
            (i for i in range(first, after) if lines[i].startswith("      fluidVersion: ")), None
        )
        if at is None:
            break
        start, end = at, at + 1
        if after - first == 1:  # root held nothing else
            start, end = first - 1, after
            if _indent(lines, end) <= 2:  # nor did the property's value
                start = first - 3
                while _indent(lines, end) > 0:
                    end += 1
        return "".join(lines[:start] + lines[end:]), lines[at].split(": ", 1)[1].strip()
    return document, None


def _recorded_version_moved_with_the_default(
    old: Optional[str], new: Optional[str]
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Both ODCS exports without the recorded version, and how it moved otherwise."""
    if old is None or new is None:
        return old, new, []
    sides = []
    for text in (old, new):
        documents = [_without_recorded_version(d) for d in re.split(r"(?m)^(?=---\n)", text)]
        sides.append(("".join(d for d, _ in documents), [v for _, v in documents]))
    (old_rest, old_versions), (new_rest, new_versions) = sides
    # Not strict: a different number of documents fails the byte comparison of the rest.
    pairs = zip(old_versions, new_versions, strict=False)
    moved = [
        f"fluidExtras root.fluidVersion {then} -> {now}: only one v0.24.0 left out "
        f"({_DEFAULT_THEN}) may appear"
        for then, now in pairs
        if then != now and (then, now) != (None, _DEFAULT_THEN)
    ]
    return old_rest, new_rest, moved


def mismatches(
    diff: Differential,
    job: str,
    directory: str,
    filename: Optional[str],
    *,
    stderr_in_any_order: bool = False,
) -> List[str]:
    """How the v0.24.0 and the checkout's results for one job differ, masked.

    ``stderr_in_any_order`` compares the log lines as a set: the INFO line
    ``overlay_applied`` now comes from inside the one composition, so it moved
    from after ``compile_done`` to before it. Nothing else may differ. An ODCS
    export may also differ by the one line #752 moved (``_ODCS_OUTPUTS``).
    """
    found: List[str] = []
    old, new = diff.old.job(job), diff.new.job(job)
    for key in ("rc", "exc", "stdout", "stderr"):
        a, b = old[key], new[key]
        if key == "stderr" and stderr_in_any_order:
            a, b = "\n".join(sorted(a.splitlines())), "\n".join(sorted(b.splitlines()))
        if a != b:
            found.append(f"{job}: {key} differs\n{_diff(str(a), str(b))}")
    if filename:
        old_file, new_file = diff.old.out_file(directory, filename), diff.new.out_file(
            directory, filename
        )
        if filename in _ODCS_OUTPUTS:
            old_file, new_file, moved = _recorded_version_moved_with_the_default(old_file, new_file)
            found += [f"{job}: {m}" for m in moved]
        if old_file != new_file:
            found.append(
                f"{job}: {filename} differs\n{_diff(old_file or '<missing>', new_file or '<missing>')}"
            )
    return found


def _job_list(base: Path, corpus: Sequence[Path], custom: Dict[str, Path]) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []

    def add(prefix: str, contract: Path, commands: Sequence[str]) -> None:
        out_dir = base / "out" / prefix
        out_dir.mkdir(parents=True, exist_ok=True)
        for command in commands:
            argv = [part.format(c=str(contract), o=str(out_dir)) for part in _COMMANDS[command][1]]
            jobs.append({"name": f"{prefix}.{command}", "cwd": str(base), "argv": argv})

    for index, contract in enumerate(corpus):
        add(f"{index:03d}", contract, _CORPUS_COMMANDS)
    for name, contract in custom.items():
        extra = _OVERLAY_COMMANDS if name in _OVERLAY_FIXTURES else ()
        add(f"c-{name}", contract, (*_CUSTOM_COMMANDS, *extra))
    return jobs


def _run_side(
    tag: str, tree: Path, base: Path, jobs: List[Dict[str, Any]]
) -> "subprocess.Popen[str]":
    (base / "home").mkdir(exist_ok=True)
    (base / "driver.py").write_text(_DRIVER, encoding="utf-8")
    (base / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_")}
    env.pop("PYTHON_DOTENV_DISABLED", None)
    env.update(
        PYTHONPATH=str(tree),
        PYTHONDONTWRITEBYTECODE="1",
        HOME=str(base / "home"),
        NO_COLOR="1",
        COLUMNS="400",
        TERM="dumb",
        LC_ALL="en_US.UTF-8",
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.Popen(
        [
            sys.executable,
            str(base / "driver.py"),
            str(base / "jobs.json"),
            str(base / "results.json"),
        ],
        cwd=str(base),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.fixture(scope="module")
def differential(release_tree: Path, tmp_path_factory: pytest.TempPathFactory) -> Differential:
    """The corpus and the custom fixtures, run through v0.24.0 and through this checkout.

    Both runs read the SAME files, at the same absolute paths: a plan carries
    the contract's directory and its digest covers it, so two copies would
    differ for a reason that has nothing to do with the code. Each run writes
    only into its own directory (``--out``, and its cwd).
    """
    work = tmp_path_factory.mktemp("compat")
    corpus_root = work / "corpus"
    shutil.copytree(REPO_ROOT / "examples", corpus_root / "examples")
    shutil.copytree(REPO_ROOT / "fluid_build" / "templates", corpus_root / "templates")
    custom: Dict[str, Path] = {}
    for name, files in _custom_fixtures().items():
        for relative, text in files.items():
            target = corpus_root / "custom" / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        custom[name] = corpus_root / "custom" / name / next(iter(files))
    corpus = _corpus(corpus_root)
    assert len(corpus) >= 30, f"the corpus shrank to {len(corpus)} contracts"
    before = _tree_state(corpus_root)

    processes: Dict[str, "subprocess.Popen[str]"] = {}
    bases: Dict[str, Path] = {}
    for tag, tree in (("old", release_tree), ("new", REPO_ROOT)):
        bases[tag] = work / tag
        bases[tag].mkdir()
        processes[tag] = _run_side(tag, tree, bases[tag], _job_list(bases[tag], corpus, custom))
    sides: Dict[str, Side] = {}
    for tag, process in processes.items():
        try:
            _, stderr = process.communicate(timeout=1500)
        except subprocess.TimeoutExpired:
            process.kill()
            pytest.fail(f"the {tag} run did not finish in 25 minutes")
        base = bases[tag]
        if process.returncode != 0 or not (base / "results.json").is_file():
            pytest.fail(f"the {tag} driver failed ({process.returncode}):\n{stderr[-3000:]}")
        sides[tag] = Side(tag, base, json.loads((base / "results.json").read_text("utf-8")))
    assert sides["old"].loaded_from.is_relative_to(release_tree), sides["old"].loaded_from
    assert sides["new"].loaded_from.is_relative_to(REPO_ROOT), sides["new"].loaded_from
    assert _tree_state(corpus_root) == before, "a command wrote into the shared corpus"
    return Differential(
        sides["old"], sides["new"], [str(p.relative_to(corpus_root)) for p in corpus]
    )


def _tree_state(root: Path) -> Dict[str, Tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── M-01 ────────────────────────────────────────────────────────────────

_OUTPUT_OF = {name: spec[0] for name, spec in _COMMANDS.items()}


@pytest.mark.slow
@pytest.mark.parametrize("command", _CORPUS_COMMANDS)
def test_m01_a_contract_without_refs_exports_as_v0_24_0_did(
    differential: Differential, command: str
) -> None:
    """bundle, ODCS, Bitol ODPS, ODPS v4.1, validate --format json and plan, byte for byte."""
    found: List[str] = []
    succeeded = 0
    for index, name in enumerate(differential.corpus):
        prefix = f"{index:03d}"
        job = f"{prefix}.{command}"
        found += [
            f"[{name}] {m}" for m in mismatches(differential, job, prefix, _OUTPUT_OF[command])
        ]
        succeeded += differential.new.jobs[job]["rc"] == 0

    assert not found, f"{len(found)} difference(s) from v0.24.0:\n" + "\n\n".join(found[:6])
    assert succeeded >= 25, f"only {succeeded} contracts ran `{command}`: the comparison is hollow"


@pytest.mark.slow
@pytest.mark.parametrize("fixture", _OVERLAY_FIXTURES)
def test_m01_a_contract_without_refs_and_an_overlay_exports_as_v0_24_0_did(
    differential: Differential, fixture: str
) -> None:
    """The overlay path was re-plumbed (one composition); for a contract with no ref it must not move."""
    prefix = f"c-{fixture}"
    found: List[str] = []
    for command in _OVERLAY_COMMANDS:
        found += mismatches(
            differential,
            f"{prefix}.{command}",
            prefix,
            _OUTPUT_OF[command],
            stderr_in_any_order=True,
        )

    assert not found, "\n\n".join(found)
    bundled = differential.new.raw_out_file(prefix, "bundle-prod.yaml")
    assert bundled is not None and "summary-prod.csv" in bundled, "the overlay was not applied"


# ── M-08 ────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_m08_the_plan_digests_of_contracts_without_refs_are_unchanged(
    differential: Differential,
) -> None:
    compared = 0
    changed: List[str] = []
    for index, name in enumerate(differential.corpus):
        prefix = f"{index:03d}"
        old = differential.old.raw_out_file(prefix, "plan.json")
        new = differential.new.raw_out_file(prefix, "plan.json")
        assert (old is None) == (new is None), f"{name}: a plan exists on one side only"
        if old is None or new is None:
            continue
        old_plan, new_plan = json.loads(old), json.loads(new)
        compared += 1
        for key in ("planDigest", "bundleDigest"):
            if old_plan.get(key) != new_plan.get(key):
                changed.append(f"{name}: {key} {old_plan.get(key)} -> {new_plan.get(key)}")

    assert not changed, "\n".join(changed)
    assert compared >= 25, f"only {compared} plans were comparable"


# ── M-09 ────────────────────────────────────────────────────────────────

_BAD = ["bad-type", "bad-root-key", "missing-expose-id", "bad-kind", "bad-build-property"]


def _json_of(stdout: str) -> Dict[str, Any]:
    return json.loads(stdout[stdout.index("{") :])


@pytest.mark.slow
@pytest.mark.parametrize("fixture", _BAD)
def test_m09_a_schema_error_in_a_contract_without_refs_reads_as_it_did(
    differential: Differential, fixture: str
) -> None:
    prefix = f"c-{fixture}"
    found: List[str] = []
    for command in ("validate", "validate-text"):
        found += mismatches(differential, f"{prefix}.{command}", prefix, None)
    assert not found, "\n\n".join(found)

    new = differential.new.jobs[f"{prefix}.validate"]
    assert new["rc"] == 1, "the fixture must be invalid, or the comparison proves nothing"
    errors = _json_of(new["stdout"])["errors"]
    assert errors, "no errors to compare"
    assert not any(
        re.match(r"[^\s:]+\.ya?ml:\d+ \(", e) for e in errors
    ), "a root-file error carries no file:line prefix: " + repr(errors)


@pytest.mark.slow
def test_m09_the_h02_message_is_exactly_the_0_24_0_one(differential: Differential) -> None:
    errors = _json_of(differential.new.jobs["c-bad-type.validate"]["stdout"])["errors"]

    assert errors == [
        "exposes[0].contract.schema[1].type: 42 is not valid under any of the given schemas"
    ]
    text = differential.new.job("c-bad-type.validate-text")["stdout"]
    assert (
        "exposes[0].contract.schema[1].type: 42 is not valid under any of the given schemas" in text
    )


# ── M-15 ────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_m15_a_json_root_with_json_fragments_exports_as_it_did(differential: Differential) -> None:
    found = mismatches(differential, "c-json-split.bundle", "c-json-split", "bundle.yaml")
    found += mismatches(differential, "c-json-split.odcs", "c-json-split", "odcs.yaml")

    assert not found, "\n\n".join(found)
    assert differential.new.jobs["c-json-split.bundle"]["rc"] == 0
    assert differential.new.jobs["c-json-split.odcs"]["rc"] == 0


@pytest.mark.slow
def test_m15_the_json_fragments_export_what_the_flat_json_contract_does(
    differential: Differential,
) -> None:
    for filename in ("bundle.yaml", "odcs.yaml"):
        split = differential.new.out_file("c-json-split", filename)
        flat = differential.new.out_file("c-json-flat", filename)
        assert split is not None and flat is not None, filename
        assert split.replace("c-json-split", "c-json-flat") == flat, _diff(flat, split)


# ── M-18 ────────────────────────────────────────────────────────────────


def _anchors_in(text: str) -> List[str]:
    return [
        f"{type(event).__name__}:{getattr(event, 'anchor', None)}"
        for event in yaml.parse(text)
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None)
    ]


@pytest.mark.slow
def test_m18_a_root_written_with_yaml_anchors_bundles_without_them(
    differential: Differential,
) -> None:
    found = mismatches(differential, "c-anchors.bundle", "c-anchors", "bundle.yaml")

    assert not found, "\n\n".join(found)
    bundled = differential.new.raw_out_file("c-anchors", "bundle.yaml")
    assert bundled is not None
    assert _anchors_in(bundled) == [], "the bundle wrote &anchors / *aliases"
    doc = yaml.safe_load(bundled)
    assert doc["exposes"][0]["binding"] == doc["exposes"][1]["binding"], "an alias lost its value"
    assert doc["metadata"]["owner"] == {"team": "t", "email": "t@example.com"}


# ── M-10 ────────────────────────────────────────────────────────────────

#: change -> (a regex a CHANGELOG [Unreleased] line states it with,
#:            the test file, a regex for the name of a test that pins it)
_INTENTIONAL_CHANGES: Dict[str, Tuple[str, str, str]] = {
    "keys beside $ref are merged over the target, not dropped (B-23)": (
        r"sibling",
        "tests/test_contract_refs.py",
        r"^test_sibling_keys_are_merged",
    ),
    "same-document #/ pointers are resolved (C-01)": (
        r"same-document|pointer",
        "tests/test_contract_refs.py",
        r"^test_a_pointer_names_a_place_in_the_contract",
    ),
    "a .sql / .md / ... ref is its text, never parsed (A-13)": (
        r"\.sql|text file|text target|\.md\b",
        "tests/test_contract_refs.py",
        r"^test_a_sql_target_is_its_text",
    ),
    "a YAML fragment may be a list or a scalar (A-02)": (
        r"(list|scalar)[^.\n]{0,60}(root|fragment)|(fragment|root)[^.\n]{0,60}(list|scalar)",
        "tests/test_contract_refs.py",
        r"^test_a_yaml_fragment_may_be_a_list_or_a_scalar",
    ),
    "no $ref is left in a loaded contract (a leftover is an error)": (
        r"left[- ]?over|no `?\$ref`? (is |remains|survives)|unresolved `?\$ref",
        "tests/test_contract_refs.py",
        r"^test_no_ref_survives",
    ),
    "an overlay is never dropped (F-10)": (
        r"overlay[^.\n]{0,100}(dropp|ignor|never|always)",
        "tests/test_contract_refs.py",
        r"^test_a_ref_in_the_base_no_longer_drops_the_overlay",
    ),
    "generate artifacts without --env composes fragments (F-35)": (
        r"generate artifacts",
        "tests/test_contract_refs.py",
        r"^test_generate_artifacts_reads_a_split_contract_whole",
    ),
    "an error from a fragment or an overlay names its file and line (H-03)": (
        r"file and line|names? (the|its) (fragment|file|overlay)|provenance",
        "tests/test_contract_refs.py",
        r"^test_an_error_the_overlay_brought_in_names_the_overlay",
    ),
    "fluid split keeps every comment of the root": (
        r"split[^.\n]{0,100}comment|comment[^.\n]{0,100}split",
        "tests/test_split_parts.py",
        r"^test_every_comment_of_the_root_is_kept",
    ),
    "fluid docs reads a contract whole, and the output changes": (
        r"fluid docs|`docs`",
        "tests/test_docs_multi_contract.py",
        r"^test_docs_reads_a_076_contract_whole",
    ),
}


def _test_names(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


@pytest.mark.parametrize("change", sorted(_INTENTIONAL_CHANGES))
def test_m10_every_intentional_change_has_a_test_that_pins_it(change: str) -> None:
    _, relative, pattern = _INTENTIONAL_CHANGES[change]
    path = REPO_ROOT / relative

    assert path.is_file(), f"{relative} is gone"
    assert any(
        re.search(pattern, name) for name in _test_names(path)
    ), f"no test in {relative} matches /{pattern}/: {change} is unpinned"


def _unreleased(changelog: str) -> str:
    match = re.search(r"^## \[Unreleased\][^\n]*\n(.*?)(?=^## \[)", changelog, re.S | re.M)
    assert match, "CHANGELOG.md has no [Unreleased] section"
    return match.group(1)


def _repo_text(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_m11_the_overlay_that_the_engine_dropped_is_gone_from_code_docs_and_tests() -> None:
    """The reviewer list: what 0.24.0 pinned about an overlay next to a ``$ref`` was deliberately removed."""
    event = "contract_overlay" + "_not_applied"
    scanned = [
        p
        for root in ("fluid_build", "docs")
        for p in (REPO_ROOT / root).rglob("*")
        if p.is_file() and p.suffix in {".py", ".md", ".yaml", ".json"}
    ]
    scanned += [
        REPO_ROOT / "tests" / name
        for name in (
            "api/test_contract_load.py",
            "test_loader_refs.py",
            "test_loader_ref_confinement.py",
            "test_docs_multi_contract.py",
        )
    ]

    leftovers = [
        str(p.relative_to(REPO_ROOT))
        for p in scanned
        if event in p.read_text(encoding="utf-8", errors="replace")
    ]

    assert leftovers == []
    assert "_OVERLAY_DROPPED" not in _repo_text("tests/api/test_contract_load.py")
    assert "Known engine behaviour" not in _repo_text("docs/CONTRACT_LOADING_API.md")


def test_m11_the_docs_no_longer_say_a_ref_node_has_no_siblings() -> None:
    refs = _repo_text("docs/contract-refs.md")

    assert "whose only key is `$ref`" not in refs
    assert "left in place as written" not in refs
    assert "Keys written beside `$ref` are merged" in refs, "the doc states the new rule"
    assert "composed contract" in refs, "the doc states that pointers resolve last"


def test_m11_the_pinned_tests_state_the_new_behaviour() -> None:
    refs = _repo_text("tests/test_loader_refs.py")
    confinement = _repo_text("tests/test_loader_ref_confinement.py")

    assert "def test_ref_with_extra_keys_is_not_ref" not in refs
    assert "def test_ref_with_sibling_keys_is_a_ref" in refs
    assert "def test_same_document_refs_are_left_in_place" not in confinement
    assert "test_same_document_refs_resolve_without_confinement" in confinement
    docs_tests = _repo_text("tests/test_docs_multi_contract.py")
    assert "test_docs_reads_a_076_contract_whole" in docs_tests


# ── M-12 ────────────────────────────────────────────────────────────────

_HEAVY = ("httpx", "jsonschema", "litellm", "mcp", "requests", "google.protobuf", "pydantic")


def _clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_")}
    env.pop("PYTHON_DOTENV_DISABLED", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def test_m12_the_ref_resolver_imports_nothing_heavy_at_module_scope() -> None:
    probe = (
        "import sys, json;"
        "import fluid_build.contract_refs, fluid_build.loader, fluid_build.util.ref_confinement;"
        f"print(json.dumps(sorted(m for m in {_HEAVY!r} if m in sys.modules)))"
    )

    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


@pytest.mark.slow
def test_m12_sub_the_startup_budget_tests_pass() -> None:
    budget = REPO_ROOT / "tests" / "perf" / "test_startup_budget.py"
    if not budget.is_file():
        pytest.skip("tests/perf/test_startup_budget.py is not in this checkout")

    out = subprocess.run(
        # The wall-clock backstop is left out: it is meaningless beside other work (CI runs
        # tests/perf serially for that reason), while the module counts are deterministic.
        [
            sys.executable,
            "-m",
            "pytest",
            str(budget),
            "-p",
            "no:cacheprovider",
            "-q",
            "-k",
            "not wall_time",
        ],
        cwd=str(REPO_ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]


# ── M-14 ────────────────────────────────────────────────────────────────


def test_m14_the_api_version_moved_with_the_provenance_field_and_overlay_dir() -> None:
    """SG-28 (characterised): the governed API gained members since 0.24.0 shipped 1.1, so it is 1.2."""
    import fluid_build.api as api

    major, minor = (int(part) for part in api.__api_version__.split("."))

    assert (major, minor) > (1, 1)
    assert hasattr(api.LoadedContract, "origin_of")
    assert "overlay_dir" in inspect.signature(api.load_contract_from_text).parameters
    assert "overlay_dir" in inspect.signature(api.load_contract_from_dict).parameters
    assert "added in 1.2" in _repo_text("docs/CONTRACT_LOADING_API.md")


def test_m14_the_provenance_field_names_the_file_and_line_of_a_value(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract

    (tmp_path / "owner.yaml").write_text("# the owner\nteam: t\nemail: t@example.com\n")
    doc = _min()
    doc["metadata"]["owner"] = {"$ref": "./owner.yaml"}
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_yaml(doc), encoding="utf-8")
    lines = path.read_text().splitlines()
    id_line = next(i for i, line in enumerate(lines, 1) if line.startswith("id:"))

    loaded = api_load_contract(path)

    assert loaded.origin_of(["id"]) == (path.resolve(), id_line)
    assert loaded.origin_of(["metadata", "owner", "team"]) == (
        (tmp_path / "owner.yaml").resolve(),
        2,
    )


def test_m14_unresolved_refs_lists_refs_only_for_an_in_memory_form_that_cannot_read_files(
    tmp_path: Path,
) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract
    from fluid_build.api.contract import load_contract_from_dict

    (tmp_path / "owner.yaml").write_text("team: t\nemail: t@example.com\n", encoding="utf-8")
    doc = _min()
    doc["metadata"]["owner"] = {"$ref": "./owner.yaml"}
    doc["description"] = {"purpose": {"$ref": "#/name"}, "usage": "u"}
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_yaml(doc), encoding="utf-8")

    from_file = api_load_contract(path)
    unreadable = load_contract_from_dict(doc)  # no base_dir: nothing can be read
    pointers_only = _min()
    pointers_only["description"] = {"purpose": {"$ref": "#/name"}, "usage": "u"}
    from_pointers = load_contract_from_dict(pointers_only)

    assert from_file.unresolved_refs == ()
    assert from_file.contract["description"]["purpose"] == "Compat min"
    assert set(unreadable.unresolved_refs) == {"./owner.yaml", "#/name"}, "every ref stays, listed"
    assert from_pointers.unresolved_refs == ()
    assert from_pointers.contract["description"]["purpose"] == "Compat min"


def test_m14_files_list_the_source_its_fragments_then_the_overlay_and_its_fragments(
    tmp_path: Path,
) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract

    (tmp_path / "fragments").mkdir()
    (tmp_path / "overlays").mkdir()
    (tmp_path / "fragments" / "owner.yaml").write_text("team: t\nemail: t@example.com\n")
    (tmp_path / "fragments" / "prod-owner.yaml").write_text("team: prod\n")
    doc = _min()
    doc["metadata"]["owner"] = {"$ref": "./fragments/owner.yaml"}
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_yaml(doc), encoding="utf-8")
    overlay = tmp_path / "overlays" / "prod.yaml"
    overlay.write_text(
        yaml.safe_dump({"metadata": {"owner": {"$ref": "../fragments/prod-owner.yaml"}}})
    )

    loaded = api_load_contract(path, env="prod")

    assert loaded.files == (
        path.resolve(),
        (tmp_path / "fragments" / "owner.yaml").resolve(),
        overlay.resolve(),
        (tmp_path / "fragments" / "prod-owner.yaml").resolve(),
    )
    assert loaded.overlay == overlay.resolve()
    assert loaded.contract["metadata"]["owner"]["team"] == "prod"


# ── M-16 ────────────────────────────────────────────────────────────────


class Spies(NamedTuple):
    opened: List[str]
    parsed: List[str]
    finder_calls: List[Any]
    #: Whether patching ``io.open`` sees ``Path.read_text`` on this Python (3.10 binds it early).
    opens_visible: bool


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Spies:
    import fluid_build.util.workspace_root as workspace_root

    opened: List[str] = []
    parsed: List[str] = []
    finder_calls: List[Any] = []
    real_open, real_parse, real_find = (
        io.open,
        loader._parse_file,
        workspace_root.find_workspace_root,
    )

    def open_spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def parse_spy(path: Path) -> Any:
        parsed.append(str(path))
        return real_parse(path)

    def find_spy(start: Optional[Path] = None) -> Any:
        finder_calls.append(start)
        return real_find(start)

    monkeypatch.setattr(io, "open", open_spy)
    monkeypatch.setattr(builtins, "open", open_spy)
    monkeypatch.setattr(loader, "_parse_file", parse_spy)
    monkeypatch.setattr(workspace_root, "find_workspace_root", find_spy)
    canary = tmp_path / "io-spy-canary.txt"
    canary.write_text("x", encoding="utf-8")
    canary.read_text(encoding="utf-8")
    visible = any(Path(p).name == canary.name for p in opened)
    opened.clear()
    return Spies(opened, parsed, finder_calls, visible)


def _no_ref_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "overlays").mkdir(parents=True)
    (root / "contract.fluid.yaml").write_text(_yaml(_min()), encoding="utf-8")
    (root / "overlays" / "prod.yaml").write_text("metadata: {owner: {team: prod}}\n")
    (tmp_path / "fluid.workspace.yaml").write_text("refRoot: .\n", encoding="utf-8")
    return (root / "contract.fluid.yaml").resolve()


@pytest.mark.parametrize("entry", ["load_contract", "load_with_overlay", "api.load_contract"])
@pytest.mark.parametrize("env", [None, "prod"])
def test_m16_a_contract_without_refs_reads_and_parses_only_its_own_files_once(
    tmp_path: Path, spies: Spies, entry: str, env: Optional[str]
) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract

    path = _no_ref_project(tmp_path)

    def call() -> Any:
        if entry == "load_contract":
            return loader.load_contract(path)
        if entry == "load_with_overlay":
            return loader.load_with_overlay(path, env)
        return api_load_contract(path, env=env)

    call()  # warm the lazy imports
    spies.opened.clear()
    spies.parsed.clear()
    spies.finder_calls.clear()

    call()

    expected = ["contract.fluid.yaml"]
    if env and entry != "load_contract":
        expected.append("prod.yaml")
    assert sorted(Path(p).name for p in spies.parsed) == sorted(
        expected
    ), "each file is parsed once, and none is the workspace's"
    assert spies.finder_calls == [], "the workspace file was looked for"
    if spies.opens_visible:
        read = [Path(p).name for p in spies.opened if str(p).startswith(str(path.parent.parent))]
        assert sorted(read) == sorted(expected), "only the contract (and its overlay) is read"
        assert "fluid.workspace.yaml" not in read


def test_m16_compile_contract_parses_a_contract_without_refs_once(
    tmp_path: Path, spies: Spies
) -> None:
    path = _no_ref_project(tmp_path)
    loader.compile_contract(path)
    spies.parsed.clear()

    loader.compile_contract(path)

    assert [Path(p).name for p in spies.parsed] == ["contract.fluid.yaml"]


# ── M-17 ────────────────────────────────────────────────────────────────


def test_m17_the_loader_helpers_third_parties_import_are_still_there(tmp_path: Path) -> None:
    from fluid_build.loader import (
        _MAX_REF_DEPTH,
        REF_ROOT_ENV,
        RefConfinementError,
        RefResolutionError,
        _is_ref_node,
        _parse_ref,
        _resolve_pointer,
        _resolve_refs,
    )

    assert _MAX_REF_DEPTH == 20
    assert REF_ROOT_ENV == "FLUID_REF_ROOT"
    assert issubclass(RefConfinementError, RefResolutionError)
    assert _is_ref_node({"$ref": "./x.yaml"}) is True
    assert _is_ref_node({"x": 1}) is False
    assert _parse_ref("./a.yaml#/b/c") == ("./a.yaml", "/b/c")
    assert _parse_ref("./a.yaml") == ("./a.yaml", None)
    assert _parse_ref("#/definitions/x") == ("", "/definitions/x")
    assert _resolve_pointer({"a": [{"b": 7}]}, "/a/0/b") == 7
    with pytest.raises(RefResolutionError):
        _resolve_pointer({"a": 1}, "/missing")

    (tmp_path / "part.yaml").write_text("k: v\n", encoding="utf-8")
    tree = {"x": {"$ref": "./part.yaml"}, "list": [{"$ref": "./part.yaml"}], "keep": 1}
    assert _resolve_refs(tree, tmp_path) == {"x": {"k": "v"}, "list": [{"k": "v"}], "keep": 1}
    assert _resolve_refs(tree, tmp_path, ref_root=tmp_path) == _resolve_refs(tree, tmp_path)
    with pytest.raises(RefConfinementError):
        _resolve_refs({"x": {"$ref": "../escape.yaml"}}, tmp_path)
