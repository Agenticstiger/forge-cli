# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The one pipeline shape every generated CI file wraps.

Each generated step runs ``fluid ship --stage N``. Ship is the only
implementation of the 11 stages (``cli/_ship_stages.py``), so a wrapper holds
no stage logic of its own: it names the stages, gates them where the CI
system can (a step per stage, a switch per stage, an environment before
apply) and passes the knobs through the job environment, never into a
script body, where a value would be code.

A pull request runs the plan phase (stages 1-6) and keeps ``runtime/`` as an
artifact. A push to the default branch runs both phases, the apply phase
(stages 7-11) in a second job bound to a CI environment, whose protection
rules can require an approval. The shape follows GitLab's OpenTofu CI/CD
component (plan on merge requests, apply on the default branch behind an
environment) and the Jenkins ``input`` / ``milestone`` / ``lock`` gate.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, List, Tuple

from ._base import check_pipeline_literal

DEFAULT_CONTRACT = "contract.fluid.yaml"
PLAN_STAGES: Tuple[int, ...] = (1, 2, 3, 4, 5, 6)
APPLY_STAGES: Tuple[int, ...] = (7, 8, 9, 10, 11)
OPT_IN_STAGES: Tuple[int, ...] = (10, 11)

#: The opt-in jobs ``--add`` puts beside the pipeline. Each carries what the
#: retired advanced and enterprise tiers emitted.
ADD_ONS: Tuple[str, ...] = ("security", "compliance", "sbom")

#: What a phase keeps for the next one: the frozen bundle and the reviewed
#: plan (apply refuses anything else), the run report the stages share, and
#: the generated artifacts stages 8 and 11 read.
CARRIED_PATHS: Tuple[str, ...] = ("runtime/", "dist/artifacts/")


@dataclass(frozen=True)
class Stage:
    """One pipeline stage as a wrapper shows it."""

    num: int
    name: str  # ship's name: bundle, validate, generate-artifacts, ...
    toggle: str  # RUN_STAGE_<N>_<NAME>, the switch ship reads

    @property
    def title(self) -> str:
        """The step or stage label: ``"6 plan"``."""
        return f"{self.num} {self.name}"

    @property
    def opt_in(self) -> bool:
        return self.num in OPT_IN_STAGES

    def command(self, contract: str = DEFAULT_CONTRACT) -> str:
        """The one line the step runs. Stage 7 passes ``--yes``: the gate
        before apply is the CI environment's, not a prompt nobody can answer."""
        return ship_command(str(self.num), contract=contract, yes=self.num == 7)


def stages() -> List[Stage]:
    """The 11 stages, from ship's own table."""
    from fluid_build.cli._ship_table import STAGE_NAMES, STAGE_TOGGLES

    return [Stage(num, STAGE_NAMES[num], STAGE_TOGGLES[num]) for num in sorted(STAGE_NAMES)]


def phase(numbers: Tuple[int, ...]) -> List[Stage]:
    wanted = set(numbers)
    return [s for s in stages() if s.num in wanted]


def ship_command(spec: str, *, contract: str = DEFAULT_CONTRACT, yes: bool = False) -> str:
    """``fluid ship [<contract>] --stage <spec> [--yes]``. The contract is named
    only when it is not the one ship finds by itself; it is a checked literal
    (:func:`check_pipeline_literal`), quoted for the shell."""
    words = ["fluid", "ship"]
    if contract != DEFAULT_CONTRACT:
        words.append(quoted_contract(contract))
    words += ["--stage", spec]
    if yes:
        words.append("--yes")
    return " ".join(words)


#: What the install step reads: which package pip installs and from where.
#: Never a run-time input or prompt of a generated pipeline. Whoever starts a
#: run could otherwise point pip at their own package and run it in the
#: default-branch apply job, with its deploy credentials. A maintainer sets
#: them as a project, repository or library variable; the install falls back
#: to the version that generated the file.
INSTALL_VARIABLES: Tuple[str, ...] = (
    "FLUID_PACKAGE_SPEC",
    "FLUID_PIP_INDEX_URL",
    "FLUID_PIP_EXTRA_INDEX_URL",
    "FLUID_ALLOW_PRERELEASE",
)

#: The job title each add-on gets.
ADD_ON_TITLES = {
    "security": "security scan",
    "compliance": "compliance report",
    "sbom": "SBOM",
}


def add_on_script(add_on: str, contract: str = DEFAULT_CONTRACT) -> List[str]:
    """The shell lines of an add-on job, for every system but GitHub (which
    runs the published actions instead). An optional scanner that is not on
    the runner is skipped with a line saying so, as the retired tiers did."""
    target = quoted_contract(contract)
    if add_on == "security":
        return [
            "mkdir -p runtime",
            f"fluid policy-check {target} --strict",
            "if command -v osv-scanner >/dev/null 2>&1; then "
            "osv-scanner scan source -r . --format sarif --output runtime/osv-results.sarif "
            "|| true; else echo 'osv-scanner is not on the runner: vulnerability scan skipped'; fi",
        ]
    if add_on == "compliance":
        return [
            "mkdir -p runtime",
            f"fluid policy-check {target} --format json --output runtime/compliance-report.json",
            f"fluid verify {target} --reconcile-lineage --warn-only",
        ]
    if add_on == "sbom":  # advisory, as the vulnerability scan is
        return [
            "mkdir -p runtime",
            "if command -v syft >/dev/null 2>&1; then "
            "syft dir:. -o spdx-json=runtime/sbom.spdx.json "
            "|| echo 'syft failed: no SBOM written'; "
            "else echo 'syft is not on the runner: no SBOM written'; fi",
        ]
    raise ValueError(f"unknown add-on {add_on!r}: one of {', '.join(ADD_ONS)}")


def check_add_ons(add_ons: Any) -> Tuple[str, ...]:
    """The add-ons in ``ADD_ONS`` order, without repeats; an unknown one is refused."""
    wanted = set(add_ons or ())
    unknown = sorted(wanted - set(ADD_ONS))
    if unknown:
        raise ValueError(f"unknown add-on {', '.join(unknown)}: one of {', '.join(ADD_ONS)}")
    return tuple(a for a in ADD_ONS if a in wanted)


def quoted_contract(contract: str) -> str:
    """The contract path as one shell word: a checked literal, quoted."""
    return shlex.quote(check_pipeline_literal("contract path", contract))


def render_yaml(document: Any) -> str:
    """Block-style YAML in insertion order: every repeated value written out
    (no anchors), a :class:`LiteralStr` script as a literal block."""
    import yaml

    from ._keyless import LiteralStr

    class _Dumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    def _literal(dumper: Any, value: Any) -> Any:
        return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style="|")

    _Dumper.add_representer(LiteralStr, _literal)
    return str(
        yaml.dump(document, Dumper=_Dumper, sort_keys=False, width=4096, default_flow_style=False)
    )


def ship_variables() -> Tuple[str, ...]:
    """Every variable ship reads, the switches included: what a wrapper
    passes through its job environment."""
    from fluid_build.cli._ship_table import SHIP_VARIABLES, STAGE_TOGGLES

    return tuple(SHIP_VARIABLES) + tuple(STAGE_TOGGLES[n] for n in sorted(STAGE_TOGGLES))
