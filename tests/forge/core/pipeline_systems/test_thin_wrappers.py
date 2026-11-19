# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every generated CI file is a thin wrapper around ``fluid ship``.

``fluid ship`` (``cli/_ship_stages.py``) is the only implementation of the 11
stages. A wrapper names the stages, gates apply natively where its CI system
can, and passes every value to ship through the job environment, never through
a script body. These tests hold all seven systems to that one shape:

1. every ``fluid`` command is ``fluid ship`` (or an add-on's own), the plan
   phase runs stages 1-6 and the apply phase 7-11 with ``--yes``;
2. no value is expanded inside a script body (the injection fence);
3. the install, the workdir and the run id;
4. keyless cloud auth on GitHub and GitLab;
5. one switch per stage, with its default;
6. the ``--add`` jobs;
7. a validator gate: the vendored GitLab and CircleCI schemas, actionlint and
   zizmor on the GitHub files, structural checks for Azure, Bitbucket and
   Tekton;
8. the Jenkinsfile's structure;
9. GitHub's ``env:``;
10. ``default_branch``.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

from fluid_build.cli._ship_table import SHIP_VARIABLES, STAGE_TOGGLES
from fluid_build.forge.core.pipeline_systems import _thin
from fluid_build.forge.core.pipeline_systems._thin import ADD_ON_TITLES, ADD_ONS
from fluid_build.forge.core.pipeline_templates import (
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

pytestmark = pytest.mark.unit

_SCHEMAS = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "ci_schemas"
_ALL_CLOUDS = ("aws", "gcp", "snowflake", "databricks", "openbao")
_CONTRACT = "contracts/my product/contract.fluid.yaml"  # a space: the shell needs quotes

P = PipelineProvider
SYSTEMS = list(PipelineProvider)
_IDS = {p: p.name.lower() for p in PipelineProvider}


def _system_params(systems: List[PipelineProvider]) -> Any:
    return pytest.mark.parametrize("provider", systems, ids=[_IDS[p] for p in systems])


every_system = _system_params(SYSTEMS)

#: The generator configurations the gate runs against.
_CONFIGS: Dict[str, Dict[str, Any]] = {
    "plain": {},
    "workdir+cloud": {"workdir": "sub/dir", "cloud_auth": _ALL_CLOUDS},
    "all-add-ons": {"add_ons": ADD_ONS},
}


def _files(provider: PipelineProvider, **kwargs: Any) -> Dict[str, str]:
    return PipelineTemplateGenerator().generate_pipeline(
        PipelineConfig(provider=provider, **kwargs)
    )


def _documents(files: Dict[str, str]) -> Iterator[Tuple[str, Any]]:
    """Every YAML document of every generated YAML file, with its path."""
    for path, text in files.items():
        if path == "Jenkinsfile":
            continue
        for document in yaml.safe_load_all(text):
            if document is not None:
                yield path, document


def _walk_shell(node: Any) -> Iterator[str]:
    """The strings a CI runner executes: ``run``, ``script``, ``bash``, ``command``,
    ``before_script`` and ``after_script`` values (a string, or a list of them)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"run", "script", "bash", "command", "before_script", "after_script"}:
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    yield from (v for v in value if isinstance(v, str))
            yield from _walk_shell(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_shell(item)


def _snippets(provider: PipelineProvider, files: Dict[str, str]) -> List[str]:
    """Every script body in the generated files."""
    if provider is P.JENKINS:
        return re.findall(r"sh '''(.*?)'''", files["Jenkinsfile"], re.S)
    return [s for _, document in _documents(files) for s in _walk_shell(document)]


#: ``fluid <word>`` in command position (not ``command -v fluid || ...``).
_FLUID_COMMAND = re.compile(r"(?:^|&&|\|\||[;(|]|\bthen\b|\belse\b|\bdo\b)\s*fluid\s+(\S+)")
_SHIP = re.compile(
    r"fluid ship(?: (?P<contract>'[^']*'|\"[^\"]*\"|[^\s-]\S*))? --stage "
    r"(?P<spec>\"[^\"]*\"|\S+)(?P<yes> --yes)?"
)


_HEREDOC = re.compile(r"<<-?\s*'?(\w+)'?[^\n]*\n.*?\n\s*\1\n", re.S)


def _fluid_words(snippets: List[str]) -> List[str]:
    words: List[str] = []
    for snippet in snippets:
        # A heredoc is text for a person (the dev-source mount hint), not a command.
        for line in _HEREDOC.sub("\n", snippet).splitlines():
            if line.strip().startswith("#"):
                continue
            words += _FLUID_COMMAND.findall(line)
    return words


def _expand(spec: str) -> List[int]:
    low, _, high = spec.strip("\"'").partition("-")
    return list(range(int(low), int(high or low) + 1))


def _ship_commands(snippets: List[str]) -> List["re.Match[str]"]:
    return [m for snippet in snippets for m in _SHIP.finditer(snippet)]


# ---------------------------------------------------------------------------
# 1. Every command is fluid ship; the phases
# ---------------------------------------------------------------------------


@every_system
def test_every_fluid_command_is_fluid_ship(provider):
    words = _fluid_words(_snippets(provider, _files(provider)))
    assert words, "no fluid command found: the scanner read nothing"
    assert set(words) <= {"ship", "--version"}, sorted(set(words))
    assert "ship" in words


@every_system
@pytest.mark.parametrize("install_mode", ["pypi", "dev-source"])
def test_with_every_add_on_the_only_other_commands_are_policy_check_and_verify(
    provider, install_mode
):
    files = _files(provider, add_ons=ADD_ONS, install_mode=install_mode)
    words = set(_fluid_words(_snippets(provider, files)))
    assert words <= {"ship", "--version", "policy-check", "verify"}, sorted(words)
    assert {"policy-check", "verify"} <= words


def _phase_ship_commands(provider: PipelineProvider, files: Dict[str, str]) -> Dict[str, List[Any]]:
    """``{"plan": [(spec, yes), ...], "apply": [...]}`` as the wrapper runs them."""
    plan: List[Tuple[str, bool]] = []
    apply: List[Tuple[str, bool]] = []

    def commands(text_list: List[str]) -> List[Tuple[str, bool]]:
        return [(m.group("spec"), bool(m.group("yes"))) for m in _ship_commands(text_list)]

    if provider is P.JENKINS:
        text = files["Jenkinsfile"]
        split = text.index("stage('apply (stages 7-11)')")
        plan = commands(re.findall(r"sh '''(.*?)'''", text[:split], re.S))
        apply = commands(re.findall(r"sh '''(.*?)'''", text[split:], re.S))
    elif provider is P.TEKTON:
        for _, doc in _documents({"tekton/pipeline.yaml": files["tekton/pipeline.yaml"]}):
            if doc["kind"] != "Pipeline" or doc["metadata"]["name"] not in (
                "fluid-plan",
                "fluid-apply",
            ):
                continue
            params = {p["name"]: p["value"] for p in doc["spec"]["tasks"][0]["params"]}
            entry = (params["ship-stage"], params["ship-yes"] == "true")
            (plan if doc["metadata"]["name"] == "fluid-plan" else apply).append(entry)
    else:
        docs = [doc for _, doc in _documents(files)]
        main = next(d for d in docs if isinstance(d, dict))
        if provider is P.GITHUB_ACTIONS:
            workflow = next(d for d in docs if d.get("name") == "fluid pipeline")
            for name, target in (("plan", plan), ("apply", apply)):
                target += commands(list(_walk_shell(workflow["jobs"][name])))
        elif provider is P.GITLAB_CI:
            plan = commands(list(_walk_shell(main["fluid-plan"])))
            apply = commands(list(_walk_shell(main["fluid-apply"])))
        elif provider is P.AZURE_DEVOPS:
            stages = {s["stage"]: s for s in main["stages"]}
            plan = commands(list(_walk_shell(stages["plan"])))
            apply = commands(list(_walk_shell(stages["apply"])))
        elif provider is P.CIRCLE_CI:
            plan = commands(list(_walk_shell(main["jobs"]["plan"])))
            apply = commands(list(_walk_shell(main["jobs"]["apply"])))
        elif provider is P.BITBUCKET:
            steps = [
                item["step"] for item in main["pipelines"]["custom"]["fluid"] if "step" in item
            ]
            plan = commands(list(_walk_shell(steps[0])))
            branch = main["pipelines"]["branches"]["main"]
            apply = commands(list(_walk_shell(branch[-1]["step"])))
    return {"plan": plan, "apply": apply}


@every_system
def test_plan_runs_stages_1_to_6_and_apply_7_to_11_with_yes(provider):
    phases = _phase_ship_commands(provider, _files(provider))
    plan_stages = sorted(n for spec, _ in phases["plan"] for n in _expand(spec))
    apply_stages = sorted(n for spec, _ in phases["apply"] for n in _expand(spec))
    assert plan_stages == [1, 2, 3, 4, 5, 6], phases
    assert apply_stages == [7, 8, 9, 10, 11], phases
    assert not any(yes for _, yes in phases["plan"]), "a plan never passes --yes"
    assert any(yes for _, yes in phases["apply"]), "apply must not stop to ask"
    # Stage 7 is the one that applies: its own command carries --yes.
    stage_seven = [(spec, yes) for spec, yes in phases["apply"] if 7 in _expand(spec)]
    assert stage_seven and all(yes for _, yes in stage_seven)


@every_system
def test_a_non_default_contract_path_is_quoted_in_every_ship_command(provider):
    files = _files(provider, contract_path=_CONTRACT, add_ons=ADD_ONS)
    snippets = _snippets(provider, files)
    quoted = shlex.quote(_CONTRACT)
    ships = _ship_commands(snippets)
    assert ships
    if provider is P.JENKINS:
        # Jenkins exports CONTRACT (with the path as its fallback); ship reads it.
        assert {m.group("contract") for m in ships} == {'"$CONTRACT"'}
        assert f"params.CONTRACT == null ? '{_CONTRACT}'" in files["Jenkinsfile"]
        assert f"name: 'CONTRACT', defaultValue: '{_CONTRACT}'" in files["Jenkinsfile"]
    else:
        assert {m.group("contract") for m in ships} == {quoted}
    # The add-on commands name it too, quoted.
    lines = [
        line
        for snippet in snippets
        for line in snippet.splitlines()
        if re.match(r"\s*(?:\w+: )?fluid (?:policy-check|verify)\b", line)
        or re.search(r"&& fluid (?:policy-check|verify)\b", line)
    ]
    assert lines
    for line in lines:
        assert quoted in line, line


@every_system
def test_the_default_contract_is_left_to_ship_to_find(provider):
    ships = _ship_commands(_snippets(provider, _files(provider)))
    if provider is P.JENKINS:
        assert {m.group("contract") for m in ships} == {'"$CONTRACT"'}
    else:
        assert {m.group("contract") for m in ships} == {None}


def test_a_contract_path_that_could_end_the_quoting_is_refused():
    for bad in ("a'b", 'a"b', "a$(id)", "a`id`", "a\\b", "a{b}"):
        with pytest.raises(ValueError, match="CONTRACT|contract path"):
            _files(P.GITHUB_ACTIONS, contract_path=bad)


# ---------------------------------------------------------------------------
# 2. The injection fence
# ---------------------------------------------------------------------------

_FENCE_CONFIG = {
    "cloud_auth": _ALL_CLOUDS,
    "add_ons": ADD_ONS,
    "workdir": "sub/dir",
    "contract_path": _CONTRACT,
    "scheduler_default": "airflow",
    "scheduler_destination_default": "file:///opt/airflow/dags",
}


def test_no_github_run_script_expands_an_expression():
    files = _files(P.GITHUB_ACTIONS, **_FENCE_CONFIG)
    scripts = [s for s in _snippets(P.GITHUB_ACTIONS, files)]
    assert scripts
    for script in scripts:
        assert "${{" not in script, script[:120]


def test_no_tekton_script_expands_a_parameter():
    files = _files(P.TEKTON, **_FENCE_CONFIG)
    scripts = _snippets(P.TEKTON, files)
    assert scripts
    for script in scripts:
        assert "$(params." not in script, script[:120]
        assert "$(workspaces." not in script, script[:120]


def test_no_circleci_run_command_expands_a_pipeline_parameter():
    files = _files(P.CIRCLE_CI, **_FENCE_CONFIG)
    commands = _snippets(P.CIRCLE_CI, files)
    assert commands
    for command in commands:
        assert "<< pipeline.parameters" not in command, command[:120]
        assert "<<" not in command or "<<'" in command or "<<EOF" in command, command[:120]


def test_no_jenkins_sh_body_reads_params():
    files = _files(P.JENKINS, **_FENCE_CONFIG)
    bodies = _snippets(P.JENKINS, files)
    assert bodies
    for body in bodies:
        assert "params." not in body, body[:120]


def test_no_azure_bash_step_expands_a_template_expression():
    files = _files(P.AZURE_DEVOPS, **_FENCE_CONFIG)
    for script in _snippets(P.AZURE_DEVOPS, files):
        assert "${{" not in script, script[:120]


def test_a_hostile_default_cannot_reach_a_script_body():
    """A default is written into the file, so one that could expand or end a
    quote is refused at generation time rather than escaped."""
    for provider in SYSTEMS:
        with pytest.raises(ValueError):
            _files(provider, fluid_package_spec="x$(touch pwned)")
        with pytest.raises(ValueError):
            _files(provider, scheduler_default="airflow", scheduler_destination_default="a`id`")


# ---------------------------------------------------------------------------
# 3. Install, workdir, run id
# ---------------------------------------------------------------------------


@every_system
def test_the_pypi_install_runs_before_any_stage(provider):
    snippets = _snippets(provider, _files(provider))
    text = "\n".join(snippets)
    assert text.index("pip install") < text.index("fluid ship")
    assert "FLUID_PACKAGE_SPEC" in text
    assert "fluid --version" in text
    assert f"data-product-forge=={_version()}" in "\n".join(_files(provider).values())


def _version() -> str:
    from fluid_build import __version__

    return __version__


@every_system
def test_the_dev_source_install_mounts_the_checkout(provider):
    files = _files(provider, install_mode="dev-source")
    text = "\n".join(
        line
        for snippet in _snippets(provider, files)
        for line in snippet.splitlines()
        if not line.strip().startswith("#")  # a comment may explain what is not done
    )
    assert "/forge-cli-src" in text
    assert "fluid --version" in text
    if provider is P.JENKINS:
        # The Jenkins agent image already has the console script; PYTHONPATH,
        # set for the whole pipeline, points its imports at the checkout.
        assert "PYTHONPATH = '/forge-cli-src'" in files["Jenkinsfile"]
    else:
        # Everywhere else nothing is installed: the editable install puts
        # `fluid` on PATH. A PYTHONPATH export dies with its step or subshell.
        assert "python -m pip install --quiet -e /forge-cli-src" in text
        assert "PYTHONPATH" not in text
    # Never the published package, and never an uninstall: that deletes the
    # `fluid` command itself.
    assert "data-product-forge" not in text
    assert "pip uninstall" not in text


def test_an_unknown_install_mode_is_refused_at_generation():
    with pytest.raises(ValueError, match="install_mode"):
        _files(P.JENKINS, install_mode="testpypi")


@every_system
def test_the_workdir_is_honoured(provider):
    workdir = "sub/dir"
    files = _files(provider, workdir=workdir, add_ons=ADD_ONS)
    text = "\n".join(files.values())
    if provider is P.GITHUB_ACTIONS:
        for path, document in _documents(files):
            for name, job in document["jobs"].items():
                if name == "sbom":
                    continue  # runs no fluid command: no workdir to be in
                assert job["defaults"]["run"]["working-directory"] == workdir, (path, name)
        workflow = next(d for p, d in _documents(files) if p.endswith("fluid-pipeline.yml"))
        apply_steps = workflow["jobs"]["apply"]["steps"]
        download = next(s for s in apply_steps if "download-artifact" in s.get("uses", ""))
        assert download["with"]["path"] == workdir
        for job_name in ("plan", "apply"):
            upload = [
                s
                for s in workflow["jobs"][job_name]["steps"]
                if "upload-artifact" in s.get("uses", "")
            ]
            assert upload
            for step in upload:
                assert all(
                    line.startswith(f"{workdir}/")
                    for line in step["with"]["path"].strip().splitlines()
                ), step
    elif provider is P.GITLAB_CI:
        ci = yaml.safe_load(files[".gitlab-ci.yml"])
        for job in ("fluid-plan", "fluid-apply", *(f"fluid-{a}" for a in ADD_ONS)):
            assert f'cd "{workdir}"' in ci[job]["before_script"], job
            assert all(p.startswith(f"{workdir}/") for p in ci[job]["artifacts"]["paths"]), job
    elif provider is P.TEKTON:
        tasks = [d for p, d in _documents(files) if p == "tekton/tasks.yaml"]
        assert tasks
        for task in tasks:
            assert f'cd "{workdir}"' in task["spec"]["steps"][0]["script"]
    elif provider is P.JENKINS:
        for body in _snippets(provider, files):
            if "fluid ship" in body or "policy-check" in body:
                assert body.startswith(f'cd "{workdir}" && set -eu'), body[:80]
        assert f"archiveArtifacts artifacts: '{workdir}/runtime/plan.json" in text
    else:
        ships = [s for s in _snippets(provider, files) if "fluid ship" in s]
        assert ships
        for script in ships:
            assert f'cd "{workdir}"' in script, script[:120]
        assert f"{workdir}/runtime" in text or provider is P.AZURE_DEVOPS
        if provider is P.AZURE_DEVOPS:
            assert f"/{workdir}" in text


_RUN_IDS = {
    P.GITHUB_ACTIONS: "github.run_id",
    P.GITLAB_CI: "$CI_PIPELINE_ID",
    P.JENKINS: "env.BUILD_TAG",
    P.AZURE_DEVOPS: "$(Build.BuildId)",
    P.BITBUCKET: "$BITBUCKET_BUILD_NUMBER",
    P.CIRCLE_CI: "<< pipeline.number >>",
    P.TEKTON: "$(context.pipelineRun.name)",
}


@every_system
def test_plan_and_apply_share_one_run_id(provider):
    """Ship merges the stage reports of one run under FLUID_RUN_ID, so both
    phases of a pipeline set it from the same CI value."""
    files = _files(provider)
    text = "\n".join(files.values())
    assert "FLUID_RUN_ID" in text
    assert _RUN_IDS[provider] in text
    if provider is P.GITHUB_ACTIONS:
        workflow = yaml.safe_load(files[".github/workflows/fluid-pipeline.yml"])
        assert workflow["env"]["FLUID_RUN_ID"] == "${{ github.run_id }}-${{ github.run_attempt }}"
    if provider is P.GITLAB_CI:
        assert yaml.safe_load(files[".gitlab-ci.yml"])["variables"]["FLUID_RUN_ID"] == (
            "$CI_PIPELINE_ID"
        )
    if provider is P.CIRCLE_CI:
        jobs = yaml.safe_load(files[".circleci/config.yml"])["jobs"]
        assert jobs["plan"]["environment"]["FLUID_RUN_ID"] == (
            jobs["apply"]["environment"]["FLUID_RUN_ID"]
        )


# ---------------------------------------------------------------------------
# 4. Keyless (GitHub, GitLab)
# ---------------------------------------------------------------------------


def _github_jobs(files: Dict[str, str]) -> Dict[str, Any]:
    return yaml.safe_load(files[".github/workflows/fluid-pipeline.yml"])["jobs"]


def test_github_both_phases_authenticate_when_a_cloud_is_bound():
    jobs = _github_jobs(_files(P.GITHUB_ACTIONS, cloud_auth=("aws",)))
    for name in ("plan", "apply"):
        job = jobs[name]
        assert job["permissions"] == {"contents": "read", "id-token": "write"}, name
        uses = [s.get("uses", "") for s in job["steps"]]
        auth = next(i for i, u in enumerate(uses) if u.startswith("aws-actions/"))
        first_ship = next(i for i, s in enumerate(job["steps"]) if "fluid ship" in s.get("run", ""))
        assert auth < first_ship, name


def test_github_neither_phase_authenticates_with_no_cloud_bound():
    files = _files(P.GITHUB_ACTIONS)
    for name, job in _github_jobs(files).items():
        assert job["permissions"] == {"contents": "read"}, name
        assert "env" not in job, name
        assert not [s for s in job["steps"] if s.get("uses", "").startswith(("aws-", "google-"))]
    text = files[".github/workflows/fluid-pipeline.yml"]
    assert "ACTIONS_ID_TOKEN_REQUEST" not in text and "id-token" not in text
    assert "No cloud platform was detected" in text


def test_github_snowflake_is_wif_from_a_masked_token_file():
    jobs = _github_jobs(_files(P.GITHUB_ACTIONS, cloud_auth=("snowflake",)))
    for name in ("plan", "apply"):
        step = next(s for s in jobs[name]["steps"] if "Snowflake" in s.get("name", ""))
        run = step["run"]
        assert "audience=snowflakecomputing.com" in run
        assert "::add-mask::" in run and "umask 077" in run
        assert "SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY" in run
        assert "SNOWFLAKE_TOKEN_FILE_PATH=$token_file" in run
        assert "SNOWFLAKE_TOKEN=" not in run  # only the file path reaches the environment
        assert "${{" not in run
        assert subprocess.run(["bash", "-n"], input=run, text=True).returncode == 0
        assert jobs[name]["env"]["SNOWFLAKE_ACCOUNT"] == "${{ vars.SNOWFLAKE_ACCOUNT }}"


def test_github_openbao_audience_is_a_variable():
    jobs = _github_jobs(_files(P.GITHUB_ACTIONS, cloud_auth=("openbao",)))
    for name in ("plan", "apply"):
        step = next(s for s in jobs[name]["steps"] if "OpenBao" in s.get("name", ""))
        assert step["env"] == {"OIDC_AUDIENCE": "${{ vars.OPENBAO_JWT_AUDIENCE }}"}
        assert "FLUID_VAULT_JWT_FILE=$token_file" in step["run"]
        assert jobs[name]["env"]["FLUID_VAULT_JWT_ROLE"] == "${{ vars.OPENBAO_JWT_ROLE }}"


def test_github_banner_names_the_keyless_model_and_the_immutable_subject():
    text = _files(P.GITHUB_ACTIONS, cloud_auth=("aws", "snowflake"))[
        ".github/workflows/fluid-pipeline.yml"
    ]
    assert "Credential model: keyless" in text
    assert "repo:OWNER@OWNER-ID/REPO@REPO-ID" in text
    assert "https://token.actions.githubusercontent.com" in text
    assert "AWS_ROLE_ARN" in text and "SNOWFLAKE_ACCOUNT" in text


def test_gitlab_both_phases_get_a_token_per_platform_when_a_cloud_is_bound():
    ci = yaml.safe_load(_files(P.GITLAB_CI, cloud_auth=_ALL_CLOUDS)[".gitlab-ci.yml"])
    for name in ("fluid-plan", "fluid-apply"):
        job = ci[name]
        assert job["id_tokens"]["AWS_OIDC_TOKEN"] == {"aud": "sts.amazonaws.com"}
        assert job["id_tokens"]["SNOWFLAKE_OIDC_TOKEN"] == {"aud": "snowflakecomputing.com"}
        assert job["id_tokens"]["OPENBAO_OIDC_TOKEN"] == {"aud": "$OPENBAO_JWT_AUDIENCE"}
        assert job["variables"]["DATABRICKS_AUTH_TYPE"] == "env-oidc"
        script = "\n".join(job["script"])
        assert "umask 077" in script and "mktemp -d" in script
        assert "SNOWFLAKE_TOKEN_FILE_PATH" in script
        assert script.index("AWS_WEB_IDENTITY_TOKEN_FILE") < script.index("fluid ship")


def test_gitlab_neither_phase_authenticates_with_no_cloud_bound():
    files = _files(P.GITLAB_CI)
    ci = yaml.safe_load(files[".gitlab-ci.yml"])
    for name in ("fluid-plan", "fluid-apply"):
        assert "id_tokens" not in ci[name] and "variables" not in ci[name]
        assert "mktemp" not in "\n".join(ci[name]["script"])
    assert "id_tokens" not in files[".gitlab-ci.yml"]
    assert "No cloud platform was detected" in files[".gitlab-ci.yml"]


def test_gitlab_banner_names_the_keyless_model():
    text = _files(P.GITLAB_CI, cloud_auth=("aws",))[".gitlab-ci.yml"]
    assert "Credential model: keyless" in text
    assert "https://gitlab.com" in text


@pytest.mark.parametrize(
    "provider", [p for p in SYSTEMS if p not in (P.GITHUB_ACTIONS, P.GITLAB_CI)]
)
def test_the_other_systems_hold_no_cloud_key_either(provider):
    files = _files(provider, cloud_auth=_ALL_CLOUDS, add_ons=ADD_ONS)
    text = "\n".join(files.values())
    for needle in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SNOWFLAKE_PASSWORD", "secrets."):
        assert needle not in text, needle


# ---------------------------------------------------------------------------
# 5. One switch per stage, with its default
# ---------------------------------------------------------------------------


def _toggle_defaults(provider: PipelineProvider, files: Dict[str, str]) -> Dict[str, str]:
    """``{RUN_STAGE_<N>_<NAME>: default}`` as the wrapper's own parameter surface declares it."""
    names = list(STAGE_TOGGLES.values())
    found: Dict[str, str] = {}
    if provider is P.GITHUB_ACTIONS:
        workflow = yaml.safe_load(files[".github/workflows/fluid-pipeline.yml"])
        on = workflow.get("on", workflow.get(True))
        inputs = on["workflow_dispatch"]["inputs"]
        for name in names:
            # A manual run's switch starts at "default": the variable, else the
            # generated default the env expression ends with.
            assert inputs[name.lower()]["default"] == "default", name
            found[name] = re.search(r"\|\| '(true|false)' \}\}$", workflow["env"][name]).group(1)
    elif provider is P.GITLAB_CI:
        variables = yaml.safe_load(files[".gitlab-ci.yml"])["variables"]
        found = {name: variables[name]["value"] for name in names}
    elif provider is P.AZURE_DEVOPS:
        parameters = {
            p["name"]: p for p in yaml.safe_load(files["azure-pipelines.yml"])["parameters"]
        }
        found = {name: parameters[name]["default"] for name in names}
    elif provider is P.BITBUCKET:
        prompt = yaml.safe_load(files["bitbucket-pipelines.yml"])["pipelines"]["custom"]["fluid"][0]
        by_name = {v["name"]: v for v in prompt["variables"]}
        found = {name: by_name[name]["default"] for name in names}
        # The steps apply the same default where the variable is unset.
        for name, default in found.items():
            assert f'export {name}="${{{name}:-{default}}}"' in files["bitbucket-pipelines.yml"]
    elif provider is P.CIRCLE_CI:
        parameters = yaml.safe_load(files[".circleci/config.yml"])["parameters"]
        found = {name: parameters[name.lower()]["default"] for name in names}
    elif provider is P.JENKINS:
        text = files["Jenkinsfile"]
        for name in names:
            match = re.search(rf"name: '{name}', defaultValue: (true|false)", text)
            assert match, name
            found[name] = match.group(1)
            fallback = re.search(rf"{name} = \"\$\{{params\.{name} == null \? '(\w+)'", text)
            assert fallback and fallback.group(1) == found[name], name
    elif provider is P.TEKTON:
        for _, doc in _documents(files):
            if doc["kind"] == "Task" and doc["metadata"]["name"] == "fluid-ship":
                params = {p["name"]: p.get("default") for p in doc["spec"]["params"]}
                found = {name: params[name] for name in names}
    return found


@every_system
def test_every_stage_has_its_switch_with_its_default(provider):
    defaults = _toggle_defaults(provider, _files(provider))
    assert set(defaults) == set(STAGE_TOGGLES.values())
    assert [d for n, d in defaults.items() if n.startswith("RUN_STAGE_10_")] == ["false"]
    assert {n: d for n, d in defaults.items()} == {
        **{name: "true" for name in STAGE_TOGGLES.values()},
        "RUN_STAGE_10_PUBLISH": "false",
        "RUN_STAGE_11_SCHEDULE_SYNC": "false",
    }


@every_system
@pytest.mark.parametrize(
    ("kwargs", "expected_off", "expected_on"),
    [
        ({"generates_artifacts": False}, {"RUN_STAGE_3_GENERATE_ARTIFACTS"}, set()),
        (
            {"publish_stage_default": True},
            {"RUN_STAGE_11_SCHEDULE_SYNC"},
            {"RUN_STAGE_10_PUBLISH"},
        ),
        (
            {"schedule_sync_default": True, "scheduler_default": "airflow"},
            {"RUN_STAGE_10_PUBLISH"},
            {"RUN_STAGE_11_SCHEDULE_SYNC"},
        ),
        (
            {"publish_stage_default": True, "schedule_sync_default": True},
            set(),
            {"RUN_STAGE_10_PUBLISH", "RUN_STAGE_11_SCHEDULE_SYNC"},
        ),
    ],
    ids=["no-artifacts", "publish", "schedule-sync", "both-opt-in"],
)
def test_stage_3_10_and_11_defaults_follow_their_config(
    provider, kwargs, expected_off, expected_on
):
    defaults = _toggle_defaults(provider, _files(provider, **kwargs))
    off = {n for n, d in defaults.items() if d == "false"}
    on = {n for n, d in defaults.items() if d == "true"}
    default_off = {"RUN_STAGE_10_PUBLISH", "RUN_STAGE_11_SCHEDULE_SYNC"}
    assert off == (default_off - expected_on) | expected_off, (off, kwargs)
    assert expected_on <= on


# ---------------------------------------------------------------------------
# 6. Add-ons
# ---------------------------------------------------------------------------


def _has_add_on(provider: PipelineProvider, files: Dict[str, str], add_on: str) -> bool:
    if provider is P.GITHUB_ACTIONS:
        return f".github/workflows/fluid-{add_on}.yml" in files
    if provider is P.GITLAB_CI:
        return f"fluid-{add_on}" in yaml.safe_load(files[".gitlab-ci.yml"])
    if provider is P.JENKINS:
        return f"stage('{ADD_ON_TITLES[add_on]}')" in files["Jenkinsfile"]
    if provider is P.AZURE_DEVOPS:
        stages = yaml.safe_load(files["azure-pipelines.yml"])["stages"]
        return add_on in {s["stage"] for s in stages}
    if provider is P.BITBUCKET:
        return f"name: {ADD_ON_TITLES[add_on]}\n" in files["bitbucket-pipelines.yml"]
    if provider is P.CIRCLE_CI:
        return add_on in yaml.safe_load(files[".circleci/config.yml"])["jobs"]
    if provider is P.TEKTON:
        docs = [d for _, d in _documents(files)]
        return {
            ("Task", f"fluid-ship-{add_on}"),
            ("Pipeline", f"fluid-{add_on}"),
        } <= {(d["kind"], d["metadata"]["name"]) for d in docs}
    raise AssertionError(provider)


@every_system
def test_no_add_on_by_default(provider):
    files = _files(provider)
    assert not [a for a in ADD_ONS if _has_add_on(provider, files, a)]


@every_system
@pytest.mark.parametrize("add_on", ADD_ONS)
def test_an_add_on_adds_its_job_and_only_its_job(provider, add_on):
    files = _files(provider, add_ons=(add_on,))
    assert [a for a in ADD_ONS if _has_add_on(provider, files, a)] == [add_on]


@every_system
def test_every_add_on_together(provider):
    files = _files(provider, add_ons=ADD_ONS)
    assert [a for a in ADD_ONS if _has_add_on(provider, files, a)] == list(ADD_ONS)
    # Order and repeats do not matter.
    again = _files(provider, add_ons=("sbom", "security", "sbom", "compliance"))
    assert again == files


def test_github_add_ons_add_their_documented_files():
    files = _files(P.GITHUB_ACTIONS, add_ons=ADD_ONS)
    assert sorted(files) == [
        ".github/workflows/fluid-compliance.yml",
        ".github/workflows/fluid-pipeline.yml",
        ".github/workflows/fluid-sbom.yml",
        ".github/workflows/fluid-security.yml",
    ]
    security = yaml.safe_load(files[".github/workflows/fluid-security.yml"])
    assert security["permissions"] == {}
    assert security["jobs"]["security"]["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }
    text = files[".github/workflows/fluid-security.yml"]
    assert "osv-scanner-action" in text and "upload-sarif" in text
    assert "fluid policy-check" in text and "--strict" in text
    compliance = files[".github/workflows/fluid-compliance.yml"]
    assert "fluid policy-check" in compliance and "runtime/compliance-report.json" in compliance
    assert "fluid verify" in compliance and "--reconcile-lineage --warn-only" in compliance
    sbom = files[".github/workflows/fluid-sbom.yml"]
    assert "anchore/sbom-action" in sbom and "attest-build-provenance" in sbom


@pytest.mark.parametrize("provider", [p for p in SYSTEMS if p is not P.GITHUB_ACTIONS])
def test_the_other_systems_run_the_add_on_scripts(provider):
    text = "\n".join(_snippets(provider, _files(provider, add_ons=ADD_ONS)))
    assert "fluid policy-check" in text and "--strict" in text
    assert "osv-scanner scan source" in text and "osv-scanner is not on the runner" in text
    assert "runtime/compliance-report.json" in text
    assert "fluid verify" in text and "--reconcile-lineage --warn-only" in text
    assert "syft dir:." in text and "runtime/sbom.spdx.json" in text


def test_github_compliance_and_gitlab_compliance_stay_off_pull_requests():
    """``fluid verify`` reaches the cloud: it runs on a schedule or by hand only."""
    workflow = yaml.safe_load(
        _files(P.GITHUB_ACTIONS, add_ons=("compliance",))[".github/workflows/fluid-compliance.yml"]
    )
    assert set(workflow.get("on", workflow.get(True))) == {"schedule", "workflow_dispatch"}
    rules = yaml.safe_load(_files(P.GITLAB_CI, add_ons=("compliance",))[".gitlab-ci.yml"])[
        "fluid-compliance"
    ]["rules"]
    assert all("merge_request_event" not in rule["if"] for rule in rules)


@every_system
def test_an_unknown_add_on_is_refused(provider):
    with pytest.raises(ValueError, match="unknown add-on"):
        _files(provider, add_ons=("nope",))
    with pytest.raises(ValueError, match="unknown add-on"):
        _files(provider, add_ons=("security", "nope"))


def test_the_add_on_helpers_refuse_an_unknown_name():
    with pytest.raises(ValueError, match="unknown add-on"):
        _thin.add_on_script("nope")
    with pytest.raises(ValueError, match="unknown add-on"):
        _thin.check_add_ons(["nope"])
    assert _thin.check_add_ons(["sbom", "security", "sbom"]) == ("security", "sbom")


# ---------------------------------------------------------------------------
# 7. The gate
# ---------------------------------------------------------------------------


def _schema_errors(schema_file: str, document: Any) -> List[str]:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((_SCHEMAS / schema_file).read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}"
        for e in validator.iter_errors(document)
    ]


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_gitlab_validates_against_the_vendored_schema(config):
    document = yaml.safe_load(_files(P.GITLAB_CI, **_CONFIGS[config])[".gitlab-ci.yml"])
    assert _schema_errors("gitlab-ci.json", document) == []


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_circleci_validates_against_the_vendored_schema(config):
    document = yaml.safe_load(_files(P.CIRCLE_CI, **_CONFIGS[config])[".circleci/config.yml"])
    assert _schema_errors("circleci.json", document) == []


def test_the_schemas_do_reject_a_broken_file():
    """Guard the gate: a schema that accepts anything would pass every file."""
    assert _schema_errors("gitlab-ci.json", {"fluid-plan": {"script": 5, "stage": ["x"]}})
    assert _schema_errors("circleci.json", {"version": 2.1, "jobs": {"a": {"steps": "nope"}}})
    assert _schema_errors("circleci.json", {"version": 2.1, "workflows": {"w": {"jobs": 3}}})


def test_the_vendored_schemas_are_documented():
    readme = (_SCHEMAS / "README.md").read_text(encoding="utf-8")
    for needle in ("2026-11-17", "ci.json", "circleci-yaml-language-server", "MIT", "Apache-2.0"):
        assert needle in readme, needle
    assert (_SCHEMAS / "gitlab-ci.json").stat().st_size > 10_000
    assert (_SCHEMAS / "circleci.json").stat().st_size > 10_000


def _write_workflows(root: Path, config: str) -> Path:
    files = _files(P.GITHUB_ACTIONS, **_CONFIGS[config])
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_github_files_pass_actionlint(tmp_path, config):
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        pytest.skip(
            "actionlint is not on PATH (brew install actionlint); the GitHub gate did not run"
        )
    root = _write_workflows(tmp_path, config)
    workflows = sorted(str(p.relative_to(root)) for p in (root / ".github/workflows").glob("*.yml"))
    assert workflows
    # Named files: without a git repository actionlint finds no project to scan.
    result = subprocess.run(
        [actionlint, "-no-color", *workflows], cwd=root, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _zizmor_command() -> Optional[List[str]]:
    zizmor = shutil.which("zizmor")
    if zizmor:
        return [zizmor]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--quiet", "zizmor==1.32.0"]
    return None


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_github_files_pass_zizmor_offline(tmp_path, config):
    command = _zizmor_command()
    if command is None:
        pytest.skip("neither zizmor nor uvx is on PATH; the GitHub security gate did not run")
    root = _write_workflows(tmp_path, config)
    result = subprocess.run(
        [*command, "--offline", "--no-progress", ".github/workflows"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode not in (0, 10, 11, 12, 13, 14):
        pytest.skip(f"zizmor could not run (rc={result.returncode}): {result.stderr[-200:]}")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_azure_structure(config):
    files = _files(P.AZURE_DEVOPS, **_CONFIGS[config])
    document = yaml.safe_load(files["azure-pipelines.yml"])
    assert {"trigger", "pr", "parameters", "variables", "stages"} <= set(document)
    for entry in document["parameters"]:
        assert {"name", "type", "default"} <= set(entry), entry
    stages = {s["stage"]: s for s in document["stages"]}
    assert {"plan", "apply"} <= set(stages)
    plan_job = stages["plan"]["jobs"][0]
    assert plan_job["pool"] == {"vmImage": "ubuntu-latest"} and plan_job["steps"]
    apply_job = stages["apply"]["jobs"][0]
    assert stages["apply"]["dependsOn"] == "plan"
    assert "deployment" in apply_job and apply_job["environment"] == "${{ parameters.FLUID_ENV }}"
    assert apply_job["strategy"]["runOnce"]["deploy"]["steps"]
    assert "refs/heads/main" in stages["apply"]["condition"]  # a pull request never applies
    for add_on in (s for name, s in stages.items() if name in ADD_ONS):
        assert add_on["dependsOn"] == [] and add_on["jobs"][0]["steps"]
    for stage in document["stages"]:
        for job in stage["jobs"]:
            assert "job" in job or "deployment" in job


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_bitbucket_structure(config):
    files = _files(P.BITBUCKET, **_CONFIGS[config])
    document = yaml.safe_load(files["bitbucket-pipelines.yml"])
    assert {"image", "pipelines"} <= set(document)
    pipelines = document["pipelines"]
    assert {"pull-requests", "branches", "custom"} <= set(pipelines)
    assert list(pipelines["branches"]) == ["main"]

    def steps(items: List[Any]) -> Iterator[Dict[str, Any]]:
        for item in items:
            if "parallel" in item:
                yield from steps(item["parallel"])
            elif "step" in item:
                yield item["step"]

    seen = 0
    for items in (pipelines["pull-requests"]["**"], pipelines["branches"]["main"]):
        for step in steps(items):
            seen += 1
            assert {"name", "image", "script"} <= set(step), step
    assert seen
    on_branch = list(steps(pipelines["branches"]["main"]))
    apply = on_branch[-1]
    assert apply["trigger"] == "manual" and apply["deployment"] == "test"
    # A pull request never reaches the apply step.
    assert not [s for s in steps(pipelines["pull-requests"]["**"]) if "deployment" in s]
    custom = list(steps(pipelines["custom"]["fluid"][1:]))
    # The manual run plans only: apply stays in the default branch's pipeline.
    assert [s["name"] for s in custom] == ["fluid plan (stages 1-6)"]
    assert pipelines["custom"]["fluid"][0]["variables"]


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_tekton_structure(config):
    files = _files(P.TEKTON, **_CONFIGS[config])
    assert sorted(files) == ["tekton/pipeline.yaml", "tekton/tasks.yaml"]
    documents = [doc for _, doc in _documents(files)]
    assert documents
    for doc in documents:
        assert doc["apiVersion"] == "tekton.dev/v1"
        assert doc["kind"] in ("Task", "Pipeline")
        assert doc["metadata"]["name"]
    tasks = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Task"}
    pipelines = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Pipeline"}
    assert {"fluid-ship"} <= set(tasks) and {"fluid-plan", "fluid-apply"} <= set(pipelines)
    assert {d["kind"] for d in documents if d in pipelines.values()} == {"Pipeline"}
    # The two files hold what their names say.
    assert {
        d["kind"] for _, d in _documents({"tekton/tasks.yaml": files["tekton/tasks.yaml"]})
    } == {"Task"}
    assert {
        d["kind"] for _, d in _documents({"tekton/pipeline.yaml": files["tekton/pipeline.yaml"]})
    } == {"Pipeline"}

    for name, pipeline in pipelines.items():
        spec = pipeline["spec"]
        workspaces = {w["name"] for w in spec["workspaces"]}
        declared = {p["name"] for p in spec["params"]}
        for ref in re.findall(r"\$\(params\.([\w.-]+)\)", json.dumps(spec["tasks"])):
            assert ref in declared, (name, ref)
        for task in spec["tasks"]:
            target = tasks[task["taskRef"]["name"]]["spec"]
            task_params = {p["name"] for p in target["params"]}
            task_workspaces = {w["name"] for w in target["workspaces"]}
            assert {p["name"] for p in task["params"]} <= task_params, (name, task["name"])
            for binding in task["workspaces"]:
                assert binding["workspace"] in workspaces, (name, binding)
                assert binding["name"] in task_workspaces, (name, binding)
            required = {p["name"] for p in target["params"] if "default" not in p}
            assert required <= {p["name"] for p in task["params"]}, (name, required)
    for name, task in tasks.items():
        spec = task["spec"]
        params = {p["name"] for p in spec["params"]}
        workspaces = {w["name"] for w in spec["workspaces"]}
        for step in spec["steps"]:
            assert step["image"] and step["script"].startswith("#!/bin/sh\nset -eu\n")
            for env in step.get("env", []):
                for ref in re.findall(r"\$\(params\.([\w.-]+)\)", str(env["value"])):
                    assert ref in params, (name, env)
            for ref in re.findall(r"\$\(workspaces\.([\w.-]+)\.path\)", step["workingDir"]):
                assert ref in workspaces, (name, ref)


# ---------------------------------------------------------------------------
# 8. Jenkins
# ---------------------------------------------------------------------------


def _code_only(source: str) -> str:
    """The Groovy source with every comment and string literal blanked out, so the
    braces left are structure. A ``'''``/``\"\"\"`` string spans lines; ``'`` and
    ``\"`` strings end at their closing quote (``\\`` escapes)."""
    out: List[str] = []
    i, n = 0, len(source)
    while i < n:
        two, three = source[i : i + 2], source[i : i + 3]
        if two == "//":
            end = source.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif two == "/*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(" " * (end - i))
            i = end
        elif three in ("'''", '"""'):
            end = source.find(three, i + 3)
            assert end != -1, "an unterminated triple-quoted string"
            out.append(" " * (end + 3 - i))
            i = end + 3
        elif source[i] in "'\"":
            quote, j = source[i], i + 1
            while j < n and source[j] != quote:
                j += 2 if source[j] == "\\" else 1
            assert (
                j < n and "\n" not in source[i:j]
            ), f"an unterminated string near {source[i:i+40]!r}"
            out.append(" " * (j + 1 - i))
            i = j + 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def _block(code: str, start: int) -> Tuple[int, int]:
    """``(open, close)`` of the brace block that opens first at or after ``start``."""
    open_at = code.index("{", start)
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return open_at, i
    raise AssertionError("unbalanced braces")


_JENKINS_CONFIGS = {
    "plain": {},
    "dev-source": {"install_mode": "dev-source"},
    "all": {"add_ons": ADD_ONS, "diff_last_applied": True, "workdir": "sub/dir"},
    "reference-only": {"generates_artifacts": False, "publish_stage_default": True},
}


@pytest.fixture(params=sorted(_JENKINS_CONFIGS))
def jenkinsfile(request) -> str:
    return _files(P.JENKINS, **_JENKINS_CONFIGS[request.param])["Jenkinsfile"]


def test_jenkins_braces_balance_outside_strings_and_comments(jenkinsfile):
    code = _code_only(jenkinsfile)
    depth = 0
    for char in code:
        depth += (char == "{") - (char == "}")
        assert depth >= 0, "a closing brace with no opening one"
    assert depth == 0
    assert code.count("(") == code.count(")")
    assert "pipeline {" in code


def test_jenkins_the_scanner_catches_an_unbalanced_file():
    """Guard the guard: a brace the strings and comments do not hide is counted."""
    code = _code_only("pipeline { // a } in a comment\n stage('x') { sh '''}''' \n")
    assert code.count("{") == 2 and code.count("}") == 0


def test_jenkins_each_pipeline_stage_calls_ship_once(jenkinsfile):
    for num in range(1, 12):
        label = re.search(rf"stage\('({num} - [^']+)'\)", jenkinsfile).group(1)
        start = jenkinsfile.index(f"stage('{label}')")
        nxt = re.search(r"\n\s+stage\('", jenkinsfile[start + 10 :])
        text = jenkinsfile[start : start + 10 + nxt.start() if nxt else len(jenkinsfile)]
        ships = re.findall(r'fluid ship "\$CONTRACT" --stage (\d+)( --yes)?', text)
        assert ships == [(str(num), " --yes" if num == 7 else "")], (label, ships)
    bodies = re.findall(r"sh '''(.*?)'''", jenkinsfile, re.S)
    assert sum(body.count("fluid ship") for body in bodies) == 11


def test_jenkins_approve_apply_and_the_lock_wrap_stages_7_to_11(jenkinsfile):
    code = _code_only(jenkinsfile)
    text = jenkinsfile
    # Stages are blanked of strings in ``code``; positions line up with ``text``.
    assert len(code) == len(text)
    approve_at = text.index("stage('Approve apply')")
    apply_at = text.index("stage('apply (stages 7-11)')")
    assert approve_at < apply_at
    a_open, a_close = _block(code, approve_at)
    approve = text[a_open : a_close + 1]
    assert "input message:" in approve and "milestone(" in approve
    assert "not { changeRequest() }" in approve
    p_open, p_close = _block(code, apply_at)
    apply = text[p_open : p_close + 1]
    assert "lock(resource:" in apply and "fluid-apply-${env.FLUID_ENV}" in apply
    assert "not { changeRequest() }" in apply  # never on a pull request
    for num in range(7, 12):
        assert re.search(rf"stage\('{num} - ", apply), num
    # Stages 1-6 sit outside the gate, before it.
    for num in range(1, 7):
        at = text.index(f"stage('{num} - ")
        assert at < approve_at and not p_open < at < p_close, num
    # Nothing past the gate: no stage 7-11 outside the locked block.
    for num in range(7, 12):
        assert text.count(f"stage('{num} - ") == 1
    # The milestone follows the input, so a newer build supersedes a waiting one.
    assert approve.index("input message:") < approve.index("milestone(")


def test_jenkins_environment_declares_every_ship_variable_with_a_fallback(jenkinsfile):
    code = _code_only(jenkinsfile)
    start = jenkinsfile.index("    environment {")
    open_at, close_at = _block(code, start)
    environment = jenkinsfile[open_at : close_at + 1]
    declared: Dict[str, str] = {}
    parameters = jenkinsfile[jenkinsfile.index("    parameters {") : start]
    for name, value in re.findall(r"name: '(\w+)', defaultValue: '([^']*)'", parameters):
        declared[name] = value
    for name, value in re.findall(r"name: '(\w+)', defaultValue: (true|false)", parameters):
        declared[name] = value
    for name, first in re.findall(r"choice\(name: '(\w+)',\s*choices: \['([^']*)'", parameters):
        declared[name] = first  # Jenkins' default for a choice is its first
    wanted = [n for n in ("CONTRACT", *_thin.ship_variables()) if n in declared]
    assert {"CONTRACT", "FLUID_ENV", "APPLY_MODE", "VERIFY_STRICT", *STAGE_TOGGLES.values()} <= set(
        wanted
    )
    for name in wanted:
        match = re.search(
            rf"^\s+{name} = \"\$\{{params\.{name} == null \? '([^']*)' : params\.{name}\}}\"$",
            environment,
            re.M,
        )
        assert match, f"environment {{}} has no `params.{name} == null ? '<default>'` fallback"
        assert match.group(1) == declared[name], (name, match.group(1), declared[name])
    # What ship reads and no parameter declares (the legacy CATALOG, the baseline
    # file) is never exported from a parameter that does not exist.
    for name in re.findall(r"params\.(\w+) == null", environment):
        assert name in declared, name


def test_jenkins_the_install_stage_comes_first_and_pins_the_cli_it_installed(jenkinsfile):
    first = jenkinsfile.index("stage('0 ")
    assert first < jenkinsfile.index("stage('1 - bundle')")
    assert "fluid --version" in jenkinsfile[first : jenkinsfile.index("stage('1 - bundle')")]


# ---------------------------------------------------------------------------
# 9. GitHub's env:
# ---------------------------------------------------------------------------


def test_github_env_names_every_ship_variable_except_catalog():
    workflow = yaml.safe_load(_files(P.GITHUB_ACTIONS)[".github/workflows/fluid-pipeline.yml"])
    env = workflow["env"]
    assert set(SHIP_VARIABLES) - {"CATALOG"} <= set(env), sorted(
        set(SHIP_VARIABLES) - {"CATALOG"} - set(env)
    )
    assert set(STAGE_TOGGLES.values()) <= set(env)
    assert "CATALOG" not in env  # the legacy name of PUBLISH_TARGETS
    # Each value is an expression: input, else variable, else the default.
    for name in set(SHIP_VARIABLES) - {"CATALOG"}:
        assert env[name].startswith("${{ ") and env[name].endswith(" }}"), name
        assert f"vars.{name}" in env[name], name


def test_github_env_falls_back_to_the_declared_default():
    workflow = yaml.safe_load(
        _files(P.GITHUB_ACTIONS, apply_mode_default="amend-and-build", fluid_env_default="staging")[
            ".github/workflows/fluid-pipeline.yml"
        ]
    )
    assert workflow["env"]["APPLY_MODE"].endswith("|| 'amend-and-build' }}")
    assert workflow["env"]["FLUID_ENV"].endswith("|| 'staging' }}")


# ---------------------------------------------------------------------------
# 10. default_branch
# ---------------------------------------------------------------------------


@every_system
@pytest.mark.parametrize("bad", ["a b", 'a"b', "a'b", "a\nb", "-x", "", "a:b", "a~b", "a^b", "a*b"])
def test_a_default_branch_that_is_not_a_branch_name_is_refused(provider, bad):
    with pytest.raises(ValueError):
        PipelineConfig(provider=provider, default_branch=bad)


@pytest.mark.parametrize(
    "provider", [P.GITHUB_ACTIONS, P.AZURE_DEVOPS, P.BITBUCKET, P.CIRCLE_CI, P.JENKINS]
)
def test_the_default_branch_is_in_the_triggers(provider):
    files = _files(provider, default_branch="release/1.x")
    text = "\n".join(files.values())
    assert "release/1.x" in text
    assert "refs/heads/main" not in text
    if provider is P.GITHUB_ACTIONS:
        workflow = yaml.safe_load(files[".github/workflows/fluid-pipeline.yml"])
        on = workflow.get("on", workflow.get(True))
        assert on["push"] == {"branches": ["release/1.x"]}
        assert on["pull_request"] == {"branches": ["release/1.x"]}
        assert "refs/heads/release/1.x" in workflow["jobs"]["apply"]["if"]
    elif provider is P.AZURE_DEVOPS:
        document = yaml.safe_load(files["azure-pipelines.yml"])
        assert document["trigger"] == {"branches": {"include": ["release/1.x"]}}
        assert document["pr"] == {"branches": {"include": ["release/1.x"]}}
    elif provider is P.BITBUCKET:
        pipelines = yaml.safe_load(files["bitbucket-pipelines.yml"])["pipelines"]
        assert list(pipelines["branches"]) == ["release/1.x"]
    elif provider is P.CIRCLE_CI:
        workflow = yaml.safe_load(files[".circleci/config.yml"])["workflows"]["fluid"]
        only = [
            job[name]["filters"]["branches"]["only"]
            for job in workflow["jobs"]
            if isinstance(job, dict)
            for name in job
            if "filters" in job[name]
        ]
        assert only and set(only) == {"release/1.x"}
    else:
        assert "branch 'release/1.x'" in files["Jenkinsfile"]


@pytest.mark.parametrize("provider", [P.GITLAB_CI, P.TEKTON])
def test_gitlab_and_tekton_follow_the_hosts_own_default_branch(provider):
    """GitLab's rules name ``$CI_DEFAULT_BRANCH``; Tekton has no trigger to name."""
    text = "\n".join(_files(provider, default_branch="release").values())
    if provider is P.GITLAB_CI:
        assert "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH" in text
    assert "refs/heads/main" not in text


def _runtime_inputs(provider: PipelineProvider, files: Dict[str, str]) -> set:
    """Every name a person can set when starting a run."""
    document = next(d for _, d in _documents(files) if isinstance(d, dict))
    if provider is P.AZURE_DEVOPS:
        return {p["name"] for p in document["parameters"]}
    if provider is P.CIRCLE_CI:
        return {name.upper() for name in document["parameters"]}
    names = set()
    for items in document["pipelines"]["custom"].values():
        for item in items:
            names |= {v["name"] for v in item.get("variables", [])}
    return names


@pytest.mark.parametrize(
    "provider", [P.AZURE_DEVOPS, P.BITBUCKET, P.CIRCLE_CI], ids=["azure", "bitbucket", "circleci"]
)
@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_the_install_knobs_are_never_a_run_time_input(provider, config):
    files = _files(provider, **_CONFIGS[config])
    inputs = _runtime_inputs(provider, files)
    assert inputs, "the check needs a run-time surface to look at"
    assert not inputs & set(_thin.INSTALL_VARIABLES)
    # The install step still falls back to the generated pin, for a project variable.
    assert "${FLUID_PACKAGE_SPEC:-" in "".join(files.values())


@pytest.mark.parametrize(
    "env, deployment",
    [
        ("dev", "test"),
        ("test", "test"),
        ("staging", "staging"),
        ("stage", "staging"),
        ("prod", "production"),
    ],
)
def test_bitbucket_deployment_is_one_of_its_default_environments(env, deployment):
    files = _files(P.BITBUCKET, fluid_env_default=env)
    document = yaml.safe_load(files["bitbucket-pipelines.yml"])
    apply = document["pipelines"]["branches"]["main"][-1]["step"]
    assert apply["deployment"] == deployment


def test_circleci_a_scheduled_run_starts_no_plan_and_compliance_is_opt_in():
    files = _files(P.CIRCLE_CI, add_ons=("compliance",))
    document = yaml.safe_load(files[".circleci/config.yml"])
    workflows = document["workflows"]
    assert "scheduled_pipeline" in json.dumps(workflows["fluid"]["when"])
    assert "run_compliance" in json.dumps(workflows["fluid"]["when"])
    assert "run_compliance" in json.dumps(workflows["fluid-compliance"]["when"])
    assert document["parameters"]["run_compliance"]["default"] is False
    steps = [s for job in document["jobs"].values() for s in job["steps"] if isinstance(s, dict)]
    kept = [s for s in steps if "store_artifacts" in s or "store_test_results" in s]
    assert kept and all(next(iter(s.values()))["when"] == "always" for s in kept)


# ---------------------------------------------------------------------------
# Review fixes: who chooses what a run installs, and what a manual run reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config", sorted(_CONFIGS), ids=sorted(_CONFIGS))
def test_github_and_gitlab_never_ask_a_run_for_the_install_knobs(config: str) -> None:
    # Whoever starts a run must not choose the package the apply job installs.
    github = yaml.safe_load(
        _files(PipelineProvider.GITHUB_ACTIONS, **_CONFIGS[config])[
            ".github/workflows/fluid-pipeline.yml"
        ]
    )
    inputs = (
        github[True]["workflow_dispatch"]["inputs"]
        if True in github
        else github["on"]["workflow_dispatch"]["inputs"]
    )
    for name in _thin.INSTALL_VARIABLES:
        assert name.lower() not in inputs
        assert github["env"][name].startswith("${{ vars." + name)
    gitlab = yaml.safe_load(
        _files(PipelineProvider.GITLAB_CI, **_CONFIGS[config])[".gitlab-ci.yml"]
    )
    for name in _thin.INSTALL_VARIABLES:
        # A plain default: not on the Run pipeline form.
        assert isinstance(gitlab["variables"][name], str), name


def test_a_github_manual_run_left_at_its_defaults_reads_the_variables() -> None:
    workflow = yaml.safe_load(
        _files(PipelineProvider.GITHUB_ACTIONS)[".github/workflows/fluid-pipeline.yml"]
    )
    on = workflow[True] if True in workflow else workflow["on"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["fluid_env"]["default"] == ""
    assert inputs["apply_mode"]["default"] == "default"
    assert inputs["apply_mode"]["options"][0] == "default"
    env = workflow["env"]
    assert env["FLUID_ENV"] == "${{ inputs.fluid_env || vars.FLUID_ENV || 'dev' }}"
    assert env["APPLY_MODE"].startswith("${{ (inputs.apply_mode != 'default' && inputs.apply_mode)")
    assert env["APPLY_MODE"].endswith("|| vars.APPLY_MODE || 'amend' }}")
    # A switch too: a box is always ticked or not and would hide the variable.
    for key in ("run_stage_10_publish", "allow_data_loss"):
        assert inputs[key]["type"] == "choice"
        assert inputs[key]["options"] == ["default", "true", "false"]
        assert inputs[key]["default"] == "default"
    assert env["RUN_STAGE_10_PUBLISH"] == (
        "${{ (inputs.run_stage_10_publish != 'default' && inputs.run_stage_10_publish)"
        " || vars.RUN_STAGE_10_PUBLISH || 'false' }}"
    )
    assert not any(i["type"] == "boolean" for i in inputs.values())


def _jenkins(**kwargs: Any) -> str:
    return _files(PipelineProvider.JENKINS, **kwargs)["Jenkinsfile"]


def test_jenkins_checks_when_before_it_takes_the_apply_lock() -> None:
    # A change request must not queue on the lock only to skip the stage.
    text = _jenkins()
    stage = text[text.index("stage('apply (stages 7-11)')") :]
    assert stage.index("beforeOptions true") < stage.index("lock(resource:")


def test_jenkins_approval_does_not_depend_on_stage_7s_switch() -> None:
    # With stage 7 off, stages 8-11 still change the target: approval stays.
    text = _jenkins()
    approve = text[
        text.index("stage('Approve apply')") : text.index("stage('apply (stages 7-11)')")
    ]
    assert "APPLY_APPROVAL" in approve
    assert "RUN_STAGE_7_APPLY" not in approve


def test_jenkins_keeps_the_applied_plan_when_a_later_stage_fails() -> None:
    text = _jenkins(diff_last_applied=True)
    always = text[text.index("always {") : text.index("success {")]
    assert ".fluid-ci/applied/*.json" in always


def test_only_the_default_branch_applies_a_manual_run_included() -> None:
    # A manual run on another branch plans only, as on Azure, Bitbucket,
    # CircleCI and Jenkins: unreviewed code never reaches stages 7-11.
    workflow = yaml.safe_load(
        _files(P.GITHUB_ACTIONS, default_branch="release/1.x")[
            ".github/workflows/fluid-pipeline.yml"
        ]
    )
    condition = workflow["jobs"]["apply"]["if"]
    assert condition.startswith("github.ref == 'refs/heads/release/1.x' && (")
    assert "'push'" in condition and "'workflow_dispatch'" in condition
    rules = yaml.safe_load(_files(P.GITLAB_CI)[".gitlab-ci.yml"])["fluid-apply"]["rules"]
    assert [rule for rule in rules if rule.get("when") != "never"] == [
        {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH", "when": "manual"}
    ]


def test_an_azure_schedule_runs_the_add_ons_only() -> None:
    # A schedule added for the add-ons must not plan or apply (as on GitLab and CircleCI).
    document = yaml.safe_load(
        _files(P.AZURE_DEVOPS, add_ons=("security", "compliance", "sbom"))["azure-pipelines.yml"]
    )
    stages = {stage["stage"]: stage for stage in document["stages"]}
    assert stages["plan"]["condition"] == "ne(variables['Build.Reason'], 'Schedule')"
    assert "ne(variables['Build.Reason'], 'Schedule')" in stages["apply"]["condition"]
    assert stages["compliance"]["condition"] == (
        "in(variables['Build.Reason'], 'Manual', 'Schedule')"
    )
    assert "condition" not in stages["security"]


def test_the_gitlab_header_says_who_may_override_the_install() -> None:
    # The Run pipeline form takes any variable: only the project setting stops a run
    # from passing its own FLUID_PACKAGE_SPEC.
    text = _files(P.GITLAB_CI)[".gitlab-ci.yml"]
    header = text[: text.index("\nworkflow:")]
    assert "Minimum role to use" in header and "pipeline variables" in header
