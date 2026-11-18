# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A test's own environment: HOME, working directory and plugin policy.

``fluid`` reads the process environment (state backend, provider and plugin
policy, telemetry, colour) and the user's home directory. A test that runs it must
not inherit those from the developer's shell, and must not leak what it sets. The
pattern is pytest's ``monkeypatch.context()`` (save, change, undo), written with the
standard library so a plugin package needs no fixture to use it.

::

    with isolated_env(tmp_path, drop_prefixes=("ACME_",), set_vars={"ACME_HOST": url}):
        code, out, err = run_fluid(capfd, "plan", "contract.fluid.yaml")
"""

from __future__ import annotations

import os
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Sequence, Tuple, Union

__all__ = ["DROPPED_PREFIXES", "QUIET_VARS", "isolated_env", "isolated_environ"]

#: Name prefixes that are always dropped: the state backend, the provider
#: selection, the plugin allow list, block list and compatibility switch, and
#: OpenTelemetry's settings (an exporter endpoint in the developer's shell would
#: send the test's spans out).
DROPPED_PREFIXES: Tuple[str, ...] = ("FLUID_STATE", "FLUID_PROVIDER", "FLUID_PLUGIN", "OTEL_")

#: Set in every isolated environment: no telemetry, and no colour to wrap output.
QUIET_VARS: Dict[str, str] = {"FLUID_NO_TELEMETRY": "1", "NO_COLOR": "1"}


def isolated_environ(
    root: Union[str, Path],
    *,
    base: Optional[Mapping[str, str]] = None,
    drop_prefixes: Union[str, Sequence[str]] = (),
    set_vars: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The environment an isolated run sees, as a new dict; nothing is applied.

    Starts from ``base`` (default: ``os.environ``), drops every variable whose name
    starts with a prefix in :data:`DROPPED_PREFIXES` or ``drop_prefixes``, points
    ``HOME`` and ``USERPROFILE`` (the home POSIX and Windows read) at ``<root>/home``
    (created here) and drops ``HOMEDRIVE`` and ``HOMEPATH``, adds :data:`QUIET_VARS`, and
    last adds ``set_vars``, which wins over any drop. Use it as the ``env=`` of a
    subprocess, or to see what :func:`isolated_env` would apply.

    Everything else stays, the developer's cloud credentials included. Name a prefix to
    drop more: ``FLUID_`` drops every variable of the CLI, ``AWS_`` an AWS credential.
    """
    extra = (drop_prefixes,) if isinstance(drop_prefixes, str) else tuple(drop_prefixes)
    if "" in extra:
        raise ValueError("an empty prefix in drop_prefixes would drop every variable")
    for name, value in (set_vars or {}).items():
        if not isinstance(value, str):
            raise TypeError(f"set_vars[{name!r}] must be a str, not {type(value).__name__}")
    home = Path(root).absolute() / "home"  # absolute: a relative HOME moves with the chdir
    home.mkdir(parents=True, exist_ok=True)
    prefixes = (*DROPPED_PREFIXES, *extra)
    env = {
        name: value
        for name, value in (os.environ if base is None else base).items()
        if not name.startswith(prefixes)
    }
    for name in ("HOMEDRIVE", "HOMEPATH"):  # Windows reads these when USERPROFILE is unset
        env.pop(name, None)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env.update(QUIET_VARS)
    env.update(set_vars or {})
    return env


@contextmanager
def isolated_env(
    root: Union[str, Path],
    *,
    drop_prefixes: Union[str, Sequence[str]] = (),
    set_vars: Optional[Mapping[str, str]] = None,
) -> Iterator[Path]:
    """Run the body in :func:`isolated_environ` and ``root`` as the working directory.

    Yields the new HOME. On exit the working directory and the whole process
    environment are put back exactly as they were, including variables the body
    set, changed or removed, and also when the body raises. If the directory the
    process stood in was removed meanwhile (before or during the body), the working
    directory is left where the body put it.
    """
    root = Path(root).absolute()
    env = isolated_environ(root, drop_prefixes=drop_prefixes, set_vars=set_vars)
    saved_env = dict(os.environ)
    try:
        saved_cwd: Optional[str] = os.getcwd()
    except OSError:  # the directory the process stood in no longer exists
        saved_cwd = None
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(root)
        yield Path(env["HOME"])
    finally:
        try:
            if saved_cwd is not None:
                with suppress(
                    OSError
                ):  # the directory may be gone; the body's error must still show
                    os.chdir(saved_cwd)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
