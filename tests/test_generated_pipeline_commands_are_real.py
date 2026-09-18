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

"""Every ``fluid`` command a generated CI pipeline runs must actually exist.

`fluid generate ci` writes pipelines that other people's CI executes. A command
with a flag we removed -- or never had -- fails there, not here, and the failure
looks like a FLUID bug to whoever is reading the log. This module walks the
*executable* positions of every generated pipeline (``run:`` / ``script:`` /
``commands:`` / ``before_script:`` / ``after_script:``) and parses each ``fluid``
invocation against the real argparse tree.

It catches three distinct regressions, all of which were live when it was added:
unknown flags (``--check``, ``--security-only``, ``--type``, ``--coverage``,
``--output``), commands that do not exist at all (``fluid audit``,
``fluid benchmark``, ``fluid lineage``), and missing required positionals
(``fluid test`` and ``fluid viz-plan`` both take one).
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.forge.core.pipeline_templates import (
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

#: Keys whose values a CI runner executes as shell. CircleCI nests the real
#: command a level deeper (``- run: {command: ...}``), hence ``command``.
_SHELL_KEYS = {"run", "script", "commands", "before_script", "after_script", "command"}
#: Shell operators that terminate one command.
_SHELL_STOP = re.compile(r"&&|\|\||[;|\n]")
#: ``${VAR}``, ``${VAR:-default}``, ``$VAR`` and ``${{ ci.expr }}``.
_SHELL_VAR = re.compile(r"\$\{\{[^}]*\}\}|\$\{[^}]*\}|\$[A-Za-z_]\w*")


def _shell_snippets(node):
    """Yield only the strings a CI runner actually executes.

    Handles all three shapes in use: a plain string (GitHub ``run:``), a list
    of strings (GitLab ``script:``), and a nested mapping (CircleCI
    ``- run: {command: ...}``) -- the last of which an earlier version of this
    scanner silently skipped, so CircleCI looked clean by not being read.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SHELL_KEYS:
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    yield from (v for v in value if isinstance(v, str))
            yield from _shell_snippets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _shell_snippets(item)


def _strip_groovy_comments(text: str) -> str:
    """Remove ``/* ... */`` and ``// ...`` from Groovy source."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _fluid_invocations(snippet: str):
    """Yield each ``fluid ...`` command line in *snippet*, skipping comments."""
    for line in snippet.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # a shell comment is documentation, not a command
        for match in re.finditer(r"\bfluid\s+\S.*", stripped):
            piece = match.group(0)
            stop = _SHELL_STOP.search(piece)
            yield (piece[: stop.start()] if stop else piece).strip()


def _defect(command: str):
    """Return ``(kind, detail)`` if *command* would not run, else ``None``."""
    # Shell/CI variables are opaque here; a placeholder keeps argparse honest
    # about arity without pretending to know the value.
    normalised = _SHELL_VAR.sub("PLACEHOLDER", command).replace('"', "").replace("'", "")
    try:
        argv = shlex.split(normalised)[1:]  # drop the leading ``fluid``
    except ValueError:
        return None  # unbalanced quotes across a line break -- not our concern
    if not argv:
        return None

    # ``fluid schedule-sync "$@"`` is the tail of a `set -- "$@" --flag value`
    # builder chain: its arguments are assembled by the surrounding shell, so a
    # static "missing required argument" verdict here would be wrong. Unknown
    # *flags* are still worth checking, so only the arity check is waived.
    forwards_args = "$@" in command

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
            _, extras = build_parser().parse_known_args(argv)
    except SystemExit:
        stderr = buffer.getvalue()
        for pattern, kind in (
            (r"unrecognized arguments: (.+)", "unknown flag"),
            (r"the following arguments are required: (.+)", "missing required argument"),
            (r"invalid choice: '([^']+)'", "no such command"),
        ):
            found = re.search(pattern, stderr)
            if found:
                if kind == "missing required argument" and forwards_args:
                    return None
                return kind, found.group(1).strip()
        return None
    # ``parse_known_args`` returns unknown *positionals* too; only a stray
    # option is a defect, since positionals are often runtime-substituted.
    unknown = [extra for extra in extras if extra.startswith("-")]
    return ("unknown flag", " ".join(unknown)) if unknown else None


def _generated_documents(provider, complexity):
    """Yield ``(name, snippets)`` for every generated file.

    YAML files are parsed so only true executable positions are read. Files
    that are not YAML -- Jenkins emits a Groovy ``Jenkinsfile`` -- fall back to
    raw text, which over-reads slightly but is far better than the previous
    behaviour of skipping them entirely and reporting the system as clean.
    """
    config = PipelineConfig(provider=provider, complexity=complexity)
    files = PipelineTemplateGenerator().generate_pipeline(config)
    for name, content in files.items():
        text = str(content)
        if name.endswith((".yml", ".yaml")):
            try:
                # ``safe_load_all``: Tekton emits a multi-document stream, on
                # which ``safe_load`` raises and an earlier version of this
                # scanner skipped the whole file -- reporting the system clean
                # because it had read nothing.
                documents = [d for d in yaml.safe_load_all(text) if d is not None]
            except yaml.YAMLError:
                continue  # templated YAML that is not valid standalone
            snippets = []
            for document in documents:
                snippets.extend(_shell_snippets(document))
            if snippets:
                yield name, snippets
        elif "fluid " in text:
            # Jenkins emits a Groovy Jenkinsfile, which is not YAML. Strip its
            # comments so documentation prose mentioning a command is not read
            # as a command.
            yield name, _strip_groovy_comments(text).splitlines()


@pytest.mark.parametrize("provider", list(PipelineProvider), ids=lambda p: p.name)
@pytest.mark.parametrize("complexity", list(PipelineComplexity), ids=lambda c: c.name)
def test_generated_pipeline_runs_only_real_fluid_commands(provider, complexity):
    defects = []
    seen = 0
    for name, snippets in _generated_documents(provider, complexity):
        for snippet in snippets:
            for command in _fluid_invocations(snippet):
                seen += 1
                found = _defect(command)
                if found:
                    defects.append(f"{name}: `{command}` -> {found[0]}: {found[1]}")
    # Coverage guard: a green result must mean "read and checked", never
    # "read nothing". A structural change to a template that moved commands
    # out of the keys above would otherwise silently zero out this system.
    assert seen, f"{provider.name}/{complexity.name}: no fluid commands found to check"
    assert not defects, "generated pipeline would fail at runtime:\n  " + "\n  ".join(defects)


def test_the_scanner_actually_detects_a_broken_command():
    """Guard the guard: a known-bad command must be reported."""
    assert _defect("fluid generate speed-transformation --check") == (
        "unknown flag",
        "--check",
    )
    assert _defect("fluid audit --compliance")[0] == "no such command"
    assert _defect("fluid viz-plan --out x.html")[0] == "missing required argument"


def test_the_scanner_ignores_shell_comments():
    """Prose mentioning a command inside a ``#`` comment is not a defect."""
    assert list(_fluid_invocations("# run fluid audit --compliance later\nfluid doctor")) == [
        "fluid doctor"
    ]
