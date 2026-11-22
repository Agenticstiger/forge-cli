# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A task that runs jobs by name: ``driver.py --job-list silver_orders``.

A package of jobs often has one entry point that takes the name of a list (a pipeline, a stage) and
runs the jobs the list holds: the DAG submits ``driver.py`` and the arguments say which list, so the
task's script is a file that does no Spark IO of its own and no job file has its name. The
conventions' ``drivers`` say where the lists and the job files are (see
:mod:`._conventions`), and this module reads them: the argument that names the list, the list
file (bounded and safe YAML, never run), and the job file of each name in it.

Nothing is guessed and nothing runs. A value or a name that is not a plain file name is refused
before any path is made of it, a path that leaves the directory it is looked for in (a link out
included) is not read, a list is a file of at most 16 KB read once (all of them together at most
128 KB), counted against the same budget as the source of the scan, and a job is joined only when
it is one of the job files the scan found.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ._conventions import Driver, unique_key_loader
from ._pysource import SourceBudget

#: A job list's name, and a job's: a plain file name stem.
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}")
#: Names one list may hold, lists one scan reads, and the bytes of them: a hostile repository does
#: not get more. A list of 500 names of 40 characters is 20 KB.
MAX_JOBS_PER_LIST = 500
MAX_LISTS = 1_000
MAX_LIST_BYTES = 16_384
MAX_LIST_BYTES_TOTAL = 131_072


def segments(script: str) -> List[str]:
    """The directories and the file name of a submitted script (a scheme, a query string and the
    ``.`` and ``..`` parts are dropped)."""
    text = script.split("?")[0]
    if "://" in text:
        text = text.split("://", 1)[1]
    return [s for s in text.replace("\\", "/").split("/") if s and s not in {".", ".."}]


def driver_for(script: str, drivers: Sequence[Driver]) -> Optional[Driver]:
    """The driver whose ``script`` ends the submitted script's path (by whole segments)."""
    have = segments(script)
    for driver in drivers:
        wanted = driver.script.split("/")
        if have[-len(wanted) :] == wanted:
            return driver
    return None


def _flag_value(driver: Driver, args: Sequence[str]) -> Union[str, Tuple[str]]:
    """What the argument names, from the task's arguments; a one-item tuple holds why not."""
    if isinstance(driver.argument, int):
        if driver.argument >= len(args):
            return (f"the task passes no argument {driver.argument}, which names the job list",)
        return args[driver.argument]
    flag = driver.argument
    for index, arg in enumerate(args):
        if arg == flag:
            if index + 1 >= len(args):
                return (f"the task passes {flag} with no value",)
            return args[index + 1]
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1 :]
    return (f"the task passes no {flag}, which names the job list",)


def _inside(path: Path, directory: Path) -> bool:
    try:
        resolved, root = path.resolve(), directory.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return root in resolved.parents


def _names_in(document: object, driver: Driver) -> Union[List[str], str]:
    found = document
    if driver.list_key is not None:
        if not isinstance(document, dict) or driver.list_key not in document:
            return f"the list has no key {driver.list_key}"
        found = document[driver.list_key]
    if not isinstance(found, list):
        return "the jobs are not a list"
    if len(found) > MAX_JOBS_PER_LIST:
        return f"the list has more than {MAX_JOBS_PER_LIST} jobs"
    names: List[str] = []
    for item in found:
        if isinstance(item, str) and _NAME.fullmatch(item) and ".." not in item:
            names.append(item)
        else:
            return "a job in the list is not a plain name"
    return names


class Lists:
    """The job lists one scan reads, each once."""

    def __init__(self, budget: Optional[SourceBudget] = None) -> None:
        self.budget = budget
        self._read: Dict[Tuple[str, str, Optional[str], str], Union[List[str], str]] = {}
        self._bytes = 0

    def names(self, driver: Driver, value: str) -> Union[List[str], str]:
        """The job names list ``value`` holds, or why they could not be read."""
        if not _NAME.fullmatch(value) or ".." in value:
            return f"{value!r} is not a plain name for a job list"
        # what the file is called and where its names are, not only the name asked for: two
        # drivers may share a directory and name their lists differently
        key = (str(driver.lists_dir), driver.list_file, driver.list_key, value)
        if key in self._read:
            return self._read[key]
        if len(self._read) >= MAX_LISTS:
            return f"the scan reads at most {MAX_LISTS:,} job lists"
        self._read[key] = result = self._load(driver, value)
        return result

    def _load(self, driver: Driver, value: str) -> Union[List[str], str]:
        path = driver.lists_dir / driver.list_file.replace("{value}", value)
        if not _inside(path, driver.lists_dir):
            return "the list is not inside the directory the conventions name"
        try:
            regular = path.is_file()
        except OSError:  # a directory that cannot be entered, a name the system refuses
            return f"the job list {path.name} could not be looked at"
        if not regular:
            return f"there is no job list {path.name} in the directory the conventions name"
        try:
            size = path.stat().st_size
        except OSError:
            return f"the job list {path.name} could not be looked at"
        if self._bytes + size > MAX_LIST_BYTES_TOTAL:
            return (
                f"the scan has read {self._bytes // 1024} of the {MAX_LIST_BYTES_TOTAL // 1024} KB "
                "of job lists it reads, and this one would pass it"
            )
        from fluid_build.util.safe_yaml import load_yaml_safe, read_bounded_file

        try:
            data = read_bounded_file(path, MAX_LIST_BYTES)
        except OSError as exc:
            return f"the job list {path.name} was not read ({type(exc).__name__})"
        if data is None:
            return f"the job list {path.name} is larger than {MAX_LIST_BYTES // 1024} KB"
        if self.budget is not None and not self.budget.take(len(data)):
            return "the scan has read all the source it reads, so the job list was not read"
        self._bytes += len(data)
        loader = unique_key_loader()
        try:
            document = load_yaml_safe(data.decode("utf-8"), max_bytes=len(data) + 1, loader=loader)
        except loader.DuplicateKey:
            return f"the job list {path.name} gives a key twice"
        except Exception:  # noqa: BLE001 - UnsafeYamlError, yaml's own errors, bad UTF-8
            return f"the job list {path.name} is not valid YAML"
        return _names_in(document, driver)


def job_file(driver: Driver, name: str) -> Path:
    """Where the job file of ``name`` is. Nothing is looked at: the scan found the job files, and
    a job joins a task only when it is one of them, at exactly this path."""
    return driver.jobs_dir / driver.job_file.replace("{job}", name)


def argument_value(driver: Driver, args: Sequence[str]) -> Union[str, Tuple[str]]:
    """The value of the argument that names the job list, or a one-item tuple with why not."""
    value = _flag_value(driver, args)
    if isinstance(value, tuple):
        return value
    if "{{" in value or "<not constant>" in value:
        return ("the job list the task names is not a constant",)
    return value
