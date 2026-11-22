# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The store token ``fluid ship`` hands its stages, kept out of every environment block.

A response-wrapped token (``FLUID_VAULT_WRAPPED_TOKEN``) unwraps once, and ship
runs each stage as its own ``fluid`` process. So ship unwraps it before the
first stage and hands every stage the token it yields (``cli/ship.py``).

How it travels. An environment variable would not do: systemd.exec(5) warns
that the environment propagates to every child process, and a same-user process
can read a process's starting environment even after the variable is removed
(``/proc/<pid>/environ`` on Linux, ``KERN_PROCARGS2`` on macOS). A stage runs
project code (a python build step, dbt) as the same user. So ship writes the
token into a pipe and starts the stage with the read end open
(``subprocess`` ``pass_fds``) and its number in :data:`SHIP_TOKEN_FD_ENV`, as
OpenSSL's ``-pass fd:<n>`` and GnuPG's ``--passphrase-fd`` take a secret. The
stage reads and closes it as the first step of ``main()``, before anything can
start a subprocess; the token then lives in this process's memory only, as a
single ``fluid apply``'s does. Windows has no ``pass_fds`` (OpenSSL's ``fd:``
is not supported there either), so there the token itself travels in
:data:`SHIP_TOKEN_ENV`, which the stage takes out of its environment at once.

The vault resolver reads the token from here first (``_vault.token_for``). A
stage never revokes it; ship does when its run ends. Both names are protected
(``credentials/protected_env.py``, the ``FLUID_VAULT_`` prefix), so a project
dotenv file cannot set them.

Stdlib only: ``main()`` imports this module on every run.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import threading
from typing import Dict, Iterator, Mapping, Optional, Tuple

#: Set by ``fluid ship`` for its stages: the number of an inherited pipe that holds the token.
SHIP_TOKEN_FD_ENV = "FLUID_VAULT_SHIP_TOKEN_FD"
#: Set by ``fluid ship`` for its stages where pipes cannot be inherited (Windows): the token.
SHIP_TOKEN_ENV = "FLUID_VAULT_SHIP_TOKEN"

#: A store token is short printable ASCII (OpenBao's are about a hundred characters).
#: A token this size fits an empty pipe on Linux and macOS, so writing it never blocks.
_MAX_TOKEN_BYTES = 16 * 1024
_TOKEN = re.compile(r"[\x21-\x7e]+")
#: The lowest descriptor the pipe may have: never stdin, stdout or stderr.
_LOWEST_FD = 3

_LOCK = threading.Lock()
_TAKEN: Optional[str] = None


def _pipes_supported() -> bool:
    """Whether a child can inherit a pipe (``subprocess`` ``pass_fds`` is POSIX only)."""
    return os.name != "nt"


def _valid(text: str) -> Optional[str]:
    text = text.strip()
    return text if text and _TOKEN.fullmatch(text) else None


def can_hand(token: str) -> bool:
    """Whether ``token`` can reach a stage whole: printable ASCII, at most 16 KiB."""
    return _valid(token) == token and len(token.encode("utf-8")) <= _MAX_TOKEN_BYTES


def _read_pipe(number: str) -> Optional[str]:
    """The token in the inherited pipe ``number``, read once and closed; ``None`` if unusable.

    Only a pipe above stdin, stdout and stderr is read, and without blocking:
    ship writes the whole token and closes its end before the stage starts, so
    all of it is there at once.
    """
    if not (number.isascii() and number.isdigit() and len(number) <= 9):
        return None
    if int(number) < _LOWEST_FD:
        return None
    fd = int(number)
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return None
    except (OSError, OverflowError):
        return None
    data = b""
    try:
        os.set_blocking(fd, False)
        while len(data) <= _MAX_TOKEN_BYTES:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    if len(data) > _MAX_TOKEN_BYTES:
        return None
    try:
        return _valid(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def take() -> Optional[str]:
    """The token ship handed this process; taken out of the environment on first sight.

    Every call removes both variables again, so a value set later in the
    process is taken too. Returns ``None`` when this process was handed none.
    """
    global _TAKEN
    with _LOCK:
        number = os.environ.pop(SHIP_TOKEN_FD_ENV, None)
        value = os.environ.pop(SHIP_TOKEN_ENV, None)
        if number is not None:
            got = _read_pipe(number.strip())
            if got is not None:
                _TAKEN = got
        if value is not None:
            got = _valid(value)
            if got is not None:
                _TAKEN = got
        return _TAKEN


def _above_stdio(fd: int) -> int:
    """``fd``, or a copy of it at :data:`_LOWEST_FD` or above when it is lower.

    ``os.pipe()`` returns 0, 1 or 2 when this process runs with stdio closed,
    and a stage reads no pipe there (:func:`_read_pipe`).
    """
    if fd >= _LOWEST_FD:
        return fd
    import fcntl

    moved = fcntl.fcntl(fd, getattr(fcntl, "F_DUPFD_CLOEXEC", fcntl.F_DUPFD), _LOWEST_FD)
    os.set_inheritable(moved, False)
    os.close(fd)
    return moved


@contextlib.contextmanager
def handed_to_child(
    token: str, env: Mapping[str, str]
) -> Iterator[Tuple[Dict[str, str], Tuple[int, ...]]]:
    """``(env, pass_fds)`` that hand ``token`` to one child process, for the time it starts.

    On POSIX the token goes into a new pipe whose write end is closed before
    this yields, and the child gets the read end; it is closed here once the
    child has ended. On Windows it goes in :data:`SHIP_TOKEN_ENV`. A token
    :func:`can_hand` refuses raises ``ValueError``: the child would drop it.
    """
    if not can_hand(token):
        raise ValueError("a store token must be printable ASCII of at most 16 KiB")
    child = dict(env)
    child.pop(SHIP_TOKEN_FD_ENV, None)
    child.pop(SHIP_TOKEN_ENV, None)
    if not _pipes_supported():
        child[SHIP_TOKEN_ENV] = token
        yield child, ()
        return
    read_end, write_end = os.pipe()
    try:
        try:
            read_end = _above_stdio(read_end)
            data = memoryview(token.encode("utf-8"))
            while data:
                data = data[os.write(write_end, data) :]
        finally:
            os.close(write_end)
        child[SHIP_TOKEN_FD_ENV] = str(read_end)
        yield child, (read_end,)
    finally:
        os.close(read_end)
