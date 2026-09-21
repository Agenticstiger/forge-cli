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

"""Find every CI install of dbt, and say whether dbt-core can reach 2.x.

Separate from the test module so the scanner can be driven directly
against adversarial inputs, rather than only against the repo's own
workflows. Three previous attempts at this guard shipped false-green
paths precisely because they were only ever exercised against the real
tree, where the awkward spellings do not appear.

WHY PARSE RATHER THAN TOKENISE
------------------------------
Every earlier attempt treated a ``run:`` block as text -- regex, then
line-joining, then ``shlex.split`` -- and each lost a different real
spelling:

===========================================  =========================
input                                        what tokenising did
===========================================  =========================
``dbt-core>=1.10,<2``                        read as admitting 2.x
``dbt-core>=1.10, <2`` (space)               specifier truncated
``"dbt-duckdb; python_version<'3.13'"``      line vanished entirely
``pip install \\`` + newline                  install invisible
``# comment ending in \\``                    swallowed the next line
``pip install dbt-duckdb;;``                 token unparseable, dropped
``pip install dbt-duckdb; dbt --version``    *certified exempt*
``pip3`` / ``pip -q`` / ``uv pip``           not recognised as installs
===========================================  =========================

The last one is the reason this module exists: ``dbt-duckdb;`` failed to
parse and was discarded, then the bare ``dbt`` COMMAND WORD parsed as the
PyPI ``dbt`` distribution, which is in the v2-exemption set -- so the
guard affirmatively certified an uncapped install as safe.

``bashlex`` makes that class of bug structurally impossible: ``dbt
--version`` is a separate command node, not an argument to ``pip
install``. Quoting, continuations and ``;``/``&&``/``|``/``(...)`` are
the parser's job, not a regex's.

WHAT COUNTS AS AN INSTALL SITE
------------------------------
Two kinds, with different invariants, because they are different claims:

* **direct** -- a dbt distribution named on the command line. The
  adapters we install this way declare no dbt-core ceiling of their own
  (measured on PyPI 2026-09-20: ``dbt-duckdb`` 1.11.0 is
  ``dbt-core>=1.8.0`` with no upper bound; ``dbt-athena-community``
  1.11.1 declares no dbt-core constraint at all), so the site must state
  ``dbt-core<2`` itself.

* **extras** -- ``pip install -e ".[dev,gcp]"``, which pulls
  ``dbt-bigquery`` and therefore dbt-core with no dbt token anywhere on
  the line. Four live jobs do this and no previous attempt could see
  them. Here the ceiling legitimately comes from *our* pyproject pin, so
  the invariant is that every dbt distribution the extra resolves to is
  itself capped below 2.0.

UNPARSEABLE BLOCKS FAIL CLOSED
------------------------------
``bashlex`` cannot parse every block in this repo (``case`` statements
raise ``NotImplementedError``; a few blocks are not valid standalone
bash). Silently skipping those would be a new false-green, so they are
returned as :class:`UnparsedBlock` and the caller fails on any that
mention dbt at all. Measured on the current tree: 14 of 141 blocks do
not parse, and none of them mentions dbt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

import bashlex
import tomllib
import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

#: The release a floating install must not be able to reach. Compared with
#: ``prereleases=True`` because dbt-core already publishes 2.0.0rc6/rc7/rc8:
#: a specifier that admits the release candidate admits what follows it.
DBT_V2 = Version("2.0.0")

#: The dbt v2 distributions. A site installing ONLY these is exempt -- they
#: *are* the v2 engine, so a dbt-core ceiling on them is meaningless, and
#: the dbt-v2-canary job exists precisely to run one.
#:
#: Safe to keep ``dbt`` here only because commands are parsed: a bare ``dbt``
#: command word can never reach this set, because it is not an argument to a
#: pip install. Under the previous ``shlex`` scanner it could, and did.
V2_DISTRIBUTIONS: Set[str] = {canonicalize_name(n) for n in ("dbt-oss", "dbt")}

#: pip flags that consume the following argument, which must therefore not
#: be mistaken for a package specifier (``-r requirements.txt``).
_FLAGS_WITH_VALUE = {
    "-r",
    "--requirement",
    "-c",
    "--constraint",
    "-i",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--target",
    "-t",
    "--platform",
    "--python-version",
    "--implementation",
    "--abi",
    "--prefix",
    "--root",
    "--src",
    "--upgrade-strategy",
    "--no-binary",
    "--only-binary",
    "--progress-bar",
    "--cache-dir",
    "--timeout",
    "--retries",
    "--proxy",
    "--cert",
    "--client-cert",
    "--log",
    "--config-settings",
    "-C",
}

#: Matches ``name[extra1,extra2]`` on a local path argument (``.[dev,gcp]``,
#: ``-e .[dev]``), which is how the extras-derived sites are spelled.
_LOCAL_EXTRAS = re.compile(r"^(?P<path>[^\[\]]*)\[(?P<extras>[^\]]+)\]$")

#: Any dbt-ish token, used ONLY to decide whether an unparseable block is
#: worth failing over. Never used to decide whether an install is capped.
_DBT_MENTION = re.compile(r"(?<![\w.-])dbt[\w-]*", re.IGNORECASE)


@dataclass(frozen=True)
class InstallSite:
    """One pip invocation that pulls a dbt distribution."""

    workflow: str
    job: str
    #: "direct" (dbt named on the line) or "extras" (via a pyproject extra).
    kind: str
    command: str
    #: Every dbt requirement this site resolves to, direct or via extras.
    requirements: Tuple[Requirement, ...] = field(default=())
    #: Extras named on the line, for the message.
    extras: Tuple[str, ...] = field(default=())

    @property
    def names(self) -> Set[str]:
        return {canonicalize_name(r.name) for r in self.requirements}

    @property
    def is_v2_only(self) -> bool:
        return bool(self.names) and self.names <= V2_DISTRIBUTIONS

    def admitting_v2(self) -> List[Requirement]:
        """dbt requirements whose specifier still admits 2.0.0."""
        return [r for r in self.requirements if r.specifier.contains(DBT_V2, prereleases=True)]

    def dbt_core_requirements(self) -> List[Requirement]:
        return [r for r in self.requirements if canonicalize_name(r.name) == "dbt-core"]


@dataclass(frozen=True)
class UnparsedBlock:
    """A ``run:`` block bashlex could not parse.

    Surfaced rather than skipped. ``mentions_dbt`` is what the caller
    fails on: a block we cannot read AND that talks about dbt is exactly
    the blind spot this guard exists to remove.
    """

    workflow: str
    job: str
    error: str
    first_line: str
    mentions_dbt: bool


def load_extra_requirements(pyproject: Path) -> Dict[str, List[Requirement]]:
    """Map each pyproject extra to the dbt requirements it pulls.

    ``pip install -e ".[dev,gcp]"`` installs dbt-bigquery -- and therefore
    dbt-core -- without a dbt token anywhere on the command line. Four
    live jobs are spelled that way, and no text-scanning attempt could
    see them.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {}) or {}
    out: Dict[str, List[Requirement]] = {}
    for extra, specs in (project.get("optional-dependencies") or {}).items():
        dbt_reqs = []
        for spec in specs or []:
            try:
                req = Requirement(spec)
            except InvalidRequirement:
                continue
            if canonicalize_name(req.name).startswith("dbt"):
                dbt_reqs.append(req)
        out[extra] = dbt_reqs
    return out


def iter_run_blocks(workflow_dir: Path) -> Iterator[Tuple[str, str, str]]:
    """Yield ``(workflow, job, run_text)`` for every ``run:`` step."""
    files = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    for path in files:
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        for document in documents:
            if not isinstance(document, dict):
                continue
            for job_name, job in (document.get("jobs") or {}).items():
                if not isinstance(job, dict):
                    continue
                for step in job.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    run = step.get("run")
                    if isinstance(run, str) and run.strip():
                        yield path.name, str(job_name), run


def parse_commands(script: str) -> List[List[str]]:
    """Every simple command in ``script``, as argv lists.

    Raises whatever bashlex raises. Callers must treat that as a signal,
    never as an empty result -- returning ``[]`` on a parse failure is how
    an unreadable block silently becomes a passing one.
    """
    commands: List[List[str]] = []

    def walk(node: Any) -> None:
        if getattr(node, "kind", None) == "command":
            words = [part.word for part in node.parts if getattr(part, "kind", None) == "word"]
            if words:
                commands.append(words)
        for attr in ("parts", "list", "commands"):
            for child in getattr(node, attr, None) or []:
                walk(child)

    for tree in bashlex.parse(script):
        walk(tree)
    return commands


def is_pip_install(argv: Sequence[str]) -> bool:
    """True for every spelling of a pip install this repo might use.

    ``pip``/``pip3``/``pip3.12``, ``python -m pip``, ``uv pip``, a venv
    path like ``.venv/bin/pip``, and flags in any position
    (``pip -q install``). The previous regex required the literal word
    ``pip`` immediately followed by ``install``, so ``pip3 install`` and
    ``pip --quiet install`` were both invisible.
    """
    if not argv:
        return False
    words = list(argv)

    # `python -m pip ...` / `python3.12 -m pip ...`
    if len(words) >= 3 and Path(words[0]).name.startswith("python") and words[1] == "-m":
        if Path(words[2]).name.split("=")[0] != "pip":
            return False
        words = words[2:]
    # `uv pip ...`
    elif Path(words[0]).name == "uv" and len(words) >= 2 and words[1] == "pip":
        words = words[1:]

    executable = Path(words[0]).name
    if not re.fullmatch(r"pip[\d.]*", executable):
        return False
    return "install" in words[1:]


def install_arguments(argv: Sequence[str]) -> List[str]:
    """The package specifiers in a pip install argv.

    Skips flags and the values they consume, so ``-r requirements.txt``
    does not read as a package named ``requirements.txt``.
    """
    words = list(argv)
    try:
        start = words.index("install") + 1
    except ValueError:
        return []

    args: List[str] = []
    skip_next = False
    for word in words[start:]:
        if skip_next:
            skip_next = False
            continue
        if word.startswith("-"):
            # `-e` is a flag whose value we DO want: it carries the extras.
            if word in ("-e", "--editable"):
                continue
            if word in _FLAGS_WITH_VALUE or word.split("=")[0] in _FLAGS_WITH_VALUE:
                skip_next = "=" not in word
            continue
        args.append(word)
    return args


def _requirement_or_none(spec: str) -> Optional[Requirement]:
    try:
        return Requirement(spec)
    except InvalidRequirement:
        return None


def scan(workflow_dir: Path, pyproject: Path) -> Tuple[List[InstallSite], List[UnparsedBlock]]:
    """Find every dbt install site, and every block we could not read."""
    extra_requirements = load_extra_requirements(pyproject)
    sites: List[InstallSite] = []
    unparsed: List[UnparsedBlock] = []

    for workflow, job, script in iter_run_blocks(workflow_dir):
        try:
            commands = parse_commands(script)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a signal
            body = "\n".join(line.split("#", 1)[0] for line in script.splitlines())
            unparsed.append(
                UnparsedBlock(
                    workflow=workflow,
                    job=job,
                    error=type(exc).__name__,
                    first_line=script.strip().splitlines()[0][:80],
                    mentions_dbt=bool(_DBT_MENTION.search(body)),
                )
            )
            continue

        for argv in commands:
            if not is_pip_install(argv):
                continue
            direct: List[Requirement] = []
            resolved: List[Requirement] = []
            named_extras: List[str] = []

            for arg in install_arguments(argv):
                local = _LOCAL_EXTRAS.match(arg)
                if local:
                    for extra in local.group("extras").split(","):
                        extra = extra.strip()
                        named_extras.append(extra)
                        resolved.extend(extra_requirements.get(extra, []))
                    continue
                req = _requirement_or_none(arg)
                if req is not None and canonicalize_name(req.name).startswith("dbt"):
                    direct.append(req)

            if direct:
                sites.append(
                    InstallSite(
                        workflow=workflow,
                        job=job,
                        kind="direct",
                        command=" ".join(argv),
                        requirements=tuple(direct),
                    )
                )
            elif resolved:
                sites.append(
                    InstallSite(
                        workflow=workflow,
                        job=job,
                        kind="extras",
                        command=" ".join(argv),
                        requirements=tuple(resolved),
                        extras=tuple(named_extras),
                    )
                )

    return sites, unparsed


def violations(sites: Iterable[InstallSite]) -> List[Tuple[InstallSite, str]]:
    """``(site, reason)`` for every site that lets dbt-core reach 2.x."""
    found: List[Tuple[InstallSite, str]] = []
    for site in sites:
        if site.is_v2_only:
            continue

        if site.kind == "direct":
            core = site.dbt_core_requirements()
            if not core:
                found.append(
                    (
                        site,
                        f"installs {sorted(site.names)} without naming dbt-core, so the "
                        "ceiling is whatever the adapters happen to declare -- and "
                        "dbt-duckdb / dbt-athena-community declare none. "
                        'Add "dbt-core<2".',
                    )
                )
                continue
            admits = [r for r in core if r.specifier.contains(DBT_V2, prereleases=True)]
            if admits:
                found.append(
                    (
                        site,
                        f"dbt-core specifier admits 2.x ({[str(r) for r in admits]}). "
                        'Use "dbt-core<2".',
                    )
                )
        else:  # extras
            admits = site.admitting_v2()
            if admits:
                found.append(
                    (
                        site,
                        f"extras {list(site.extras)} pull {[str(r) for r in admits]}, "
                        "whose specifier admits 2.x. Cap it in pyproject.toml.",
                    )
                )
    return found
