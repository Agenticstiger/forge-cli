# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""CI-native log structure for ``fluid ship``.

One stage is one collapsible group in the CI system's log viewer: GitHub
Actions ``::group::``, GitLab collapsible sections, Azure Pipelines
``##[group]``. Anywhere else the stage gets a one-line banner.

GitHub Actions parses workflow commands out of every line a step prints,
including a child process's. So the child runs between
``::stop-commands::<token>`` and ``::<token>::`` with a fresh random token per
stage: text the child prints (a contract field, a provider error) cannot issue
a workflow command.

Standard library only; imported lazily by ``fluid ship``.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import time
from typing import IO, Callable, List, Mapping, Optional, Sequence, Tuple

MODES = ("auto", "github", "gitlab", "azure", "plain")


def detect(mode: str, env: Optional[Mapping[str, str]] = None) -> str:
    """``--ci-output`` -> the concrete flavour: github, gitlab, azure or plain."""
    if mode != "auto":
        return mode
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS") == "true":
        return "github"
    if env.get("GITLAB_CI") == "true":
        return "gitlab"
    if env.get("TF_BUILD") == "True":
        return "azure"
    return "plain"


def escape_data(text: str) -> str:
    """GitHub toolkit ``escapeData``: ``%`` first, so the others are not double-escaped."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    """GitHub toolkit ``escapeProperty``: data escapes plus ``:`` and ``,``."""
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def oneline(text: str) -> str:
    """``text`` made safe to print as ONE log line: every control character
    (CR, LF, ESC, ...) becomes a visible escape, so a value a user influences
    (an env name, a contract path, a skip reason) can never begin a line of
    its own, and so never forge a workflow command or a log marker.

    ``fluid_build._console._show_controls`` already spells the C0/C1 controls
    as ``\\xNN`` for the console, but keeps LF and TAB on purpose (it prints
    multi-line text). A log line must not break, so CR, LF, TAB and the Unicode
    line and paragraph separators are spelled out first.
    """
    from fluid_build._console import _show_controls

    flat = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return _show_controls(flat).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _section_id(num: int, name: str) -> str:
    return f"fluid_ship_{num}_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


class CiOutput:
    """Writes the per-stage markers for one flavour to stdout."""

    def __init__(
        self,
        flavour: str,
        env: Optional[Mapping[str, str]] = None,
        out: Optional[IO[str]] = None,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.flavour = flavour
        self.env = os.environ if env is None else env
        self._out = out
        self._clock = clock
        self._token_factory = token_factory
        self._token: Optional[str] = None

    @property
    def out(self) -> IO[str]:
        return self._out if self._out is not None else sys.stdout

    def line(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def info(self, text: str) -> None:
        """A message of ship's own: always a single line (see :func:`oneline`)."""
        self.line(oneline(text))

    def flush(self) -> None:
        """Called before every child spawn so markers and child output keep order."""
        self.out.flush()
        sys.stderr.flush()

    def stage_start(self, num: int, name: str, total: int = 11) -> None:
        title = f"{num} · {name}"
        if self.flavour == "github":
            self.line(f"::group::{title}")
        elif self.flavour == "gitlab":
            ts = int(self._clock())
            self.line(
                f"\x1b[0Ksection_start:{ts}:{_section_id(num, name)}[collapsed=true]"
                f"\r\x1b[0K{title}"
            )
        elif self.flavour == "azure":
            self.line(f"##[group]{title}")
        else:
            self.line(f"── stage {num}/{total} · {name} " + "─" * 40)

    def before_child(self) -> None:
        if self.flavour == "github":
            self._token = self._token_factory()
            self.line(f"::stop-commands::{self._token}")
        self.flush()

    def after_child(self) -> None:
        if self.flavour == "github" and self._token is not None:
            self.line(f"::{self._token}::")
            self._token = None

    def stage_end(self, num: int, name: str) -> None:
        if self.flavour == "github":
            self.line("::endgroup::")
        elif self.flavour == "gitlab":
            ts = int(self._clock())
            self.line(f"\x1b[0Ksection_end:{ts}:{_section_id(num, name)}\r\x1b[0K")
        elif self.flavour == "azure":
            self.line("##[endgroup]")

    def failure(self, num: int, name: str, rc: int) -> None:
        message = f"stage {num} {name} failed (exit {rc})"
        if self.flavour == "github":
            self.line(f"::error title={escape_property('fluid ship')}::{escape_data(message)}")
        elif self.flavour == "azure":
            self.line(f"##vso[task.logissue type=error]{message}")
        else:
            self.line(f"ERROR: {message}")

    def summary(self, rows: Sequence[Tuple[int, str, str, float]]) -> None:
        """Append the stage table to ``$GITHUB_STEP_SUMMARY`` (GitHub only)."""
        path = self.env.get("GITHUB_STEP_SUMMARY")
        if self.flavour != "github" or not path:
            return
        lines: List[str] = [
            "### fluid ship",
            "",
            "| Stage | Status | Duration |",
            "| --- | --- | --- |",
        ]
        for num, name, status, seconds in rows:
            lines.append(f"| {num} · {name} | {status} | {seconds:.1f}s |")
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError:
            pass  # a summary is a courtesy; never fail the pipeline over it
