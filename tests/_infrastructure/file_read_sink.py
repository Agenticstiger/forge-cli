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

"""Record every attempt to open a given set of files, in this process or a child.

Tests that must prove a credential file on the host was never read watch it
here and assert that nothing opened it. The record comes from the interpreter's
``open`` audit event (PEP 578), which ``open()``, ``io.open``, ``os.open`` and
``pathlib`` all raise before touching the file, so it sees an SDK's read however
the SDK spells the path: absolute, relative, through ``~`` or a symlink.

* In this process, :func:`watch_opens` records into a list.
* In a child process (a real ``fluid mcp serve`` over stdio),
  :func:`child_env` returns the environment that makes the child record into a
  log file, through a ``sitecustomize`` module; :func:`read_child_log` reads it.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Set, Tuple

_WATCH_ENV = "FLUID_TEST_WATCH_FILES"
_LOG_ENV = "FLUID_TEST_OPEN_LOG"

_lock = threading.Lock()
_watches: List[Tuple[Set[str], List[str]]] = []
_installed = False


def _resolved(paths: Iterable[Path]) -> Set[str]:
    return {os.path.realpath(path) for path in paths}


def _audit(event: str, args: Tuple[Any, ...]) -> None:
    if event != "open" or not _watches:
        return
    target = args[0] if args else None
    if not isinstance(target, (str, bytes, os.PathLike)):
        return  # an already-open descriptor
    opened = os.path.realpath(os.fsdecode(target))
    with _lock:
        for files, seen in _watches:
            if opened in files:
                seen.append(opened)


@contextmanager
def watch_opens(*files: Path) -> Iterator[List[str]]:
    """Yield a list that collects every open of ``files`` while the block runs.

    Audit hooks cannot be removed, so one hook is installed for the process and
    each block registers its own watch with it.
    """
    global _installed
    seen: List[str] = []
    watch = (_resolved(files), seen)
    with _lock:
        if not _installed:
            sys.addaudithook(_audit)
            _installed = True
        _watches.append(watch)
    try:
        yield seen
    finally:
        with _lock:
            _watches.remove(watch)


_SITECUSTOMIZE = f'''\
"""Test-only: log every open of the files named in ${_WATCH_ENV}."""
import os
import sys

_files = {{
    os.path.realpath(p) for p in os.environ.get("{_WATCH_ENV}", "").split(os.pathsep) if p
}}
_log = open(os.environ["{_LOG_ENV}"], "a", buffering=1, encoding="utf-8")


def _audit(event, args):
    if event != "open" or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    opened = os.path.realpath(os.fsdecode(args[0]))
    if opened in _files:
        _log.write(opened + "\\n")


sys.addaudithook(_audit)
'''


def child_env(base: Dict[str, str], hook_dir: Path, log: Path, *files: Path) -> Dict[str, str]:
    """``base`` plus what makes a child Python process log opens of ``files`` to ``log``."""
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    log.write_text("", encoding="utf-8")
    env = dict(base)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(hook_dir), base.get("PYTHONPATH")]))
    env[_WATCH_ENV] = os.pathsep.join(str(path) for path in files)
    env[_LOG_ENV] = str(log)
    return env


def read_child_log(log: Path) -> List[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


__all__ = ["child_env", "read_child_log", "watch_opens"]
