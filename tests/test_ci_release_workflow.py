# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for ``.github/workflows/release.yml``.

actionlint passes this workflow, and so did every earlier review, while three
defects shipped in it (follow-ups to the review of #702):

A. A manual run started from ``main`` builds the TAG's commit, but the SLSA
   provenance and PyPI's Sigstore attestations take the source ref and sha from
   the run's OIDC claims, so they name ``main``. The quality gate never asked
   whether ``main`` contains the tag.
B. ``docker`` and ``github-release`` did not wait for PyPI. When ``publish-pypi``
   failed (``file already exists`` on a re-dispatched tag), they still cut a
   GitHub Release and moved the GHCR ``latest`` tag.
C. ``github-release`` read the raw ``inputs.tag || ref_name`` instead of the
   validated ``quality-gate`` output.

The job graph and the gate are not checked by grepping for a string, which
would pass while they are wrong. Two models run instead:

* a job-graph evaluator that applies GitHub's documented rules to every job's
  ``needs`` and ``if``: an ``if`` with no status function is ANDed with an
  implicit ``success()``, false when ANY transitive ancestor did not succeed;
  and an ``if`` with any text around its ``${{ }}``, even the newline a ``>``
  block keeps, is a ``format()`` string, never empty, so always true
  (actionlint's if-cond rule reports that one too);
* the real quality-gate script, extracted from the YAML and run under node
  against a fake GitHub API whose commit-compare answers come from a real git
  repository.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"

_STATUS_FNS = {"success", "always", "failure", "cancelled"}
_TESTPYPI_JOBS = {"publish-testpypi", "verify-testpypi"}


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


# --------------------------------------------------------------------------
# A tiny evaluator for the subset of the expression language the file uses.
# --------------------------------------------------------------------------


def _tokenize(src: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i = 0
    while i < len(src):
        c = src[i]
        if c.isspace():
            i += 1
        elif c == "'":
            j, buf = i + 1, ""
            while True:
                if src[j] == "'" and src[j + 1 : j + 2] == "'":
                    buf, j = buf + "'", j + 2
                elif src[j] == "'":
                    break
                else:
                    buf, j = buf + src[j], j + 1
            toks.append(("STR", buf))
            i = j + 1
        elif src.startswith(("&&", "||", "==", "!="), i):
            toks.append(("OP", src[i : i + 2]))
            i += 2
        elif c in "()!":
            toks.append(("OP", c))
            i += 1
        else:
            m = re.match(r"[A-Za-z0-9_.*-]+", src[i:])
            assert m, f"cannot tokenize {src[i:]!r}"
            toks.append(("ID", m.group(0)))
            i += m.end()
    return toks


def _num(v: object) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def _loose_eq(a: object, b: object) -> bool:
    """GitHub `==`: strings compare case-insensitively, mixed types as numbers."""
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b):
        return a == b
    return _num(a) == _num(b)


def _truthy(v: object) -> bool:
    """GitHub truthiness: null, false, 0, NaN and '' are false."""
    if isinstance(v, float) and math.isnan(v):
        return False
    return bool(v)


def _to_str(v: object) -> str:
    """How `format()` renders a value."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class _Parser:
    def __init__(self, toks: list[tuple[str, str]], ctx: dict) -> None:
        self.t, self.i, self.ctx = toks, 0, ctx

    def peek(self) -> tuple[str | None, str | None]:
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def eat(self, val: str | None = None) -> tuple[str, str]:
        tok = self.t[self.i]
        assert val is None or tok[1] == val, f"expected {val!r}, got {tok}"
        self.i += 1
        return tok

    def parse(self) -> object:
        v = self.or_()
        assert self.i == len(self.t), f"trailing tokens {self.t[self.i:]}"
        return v

    def or_(self) -> object:
        v = self.and_()
        while self.peek() == ("OP", "||"):
            self.eat()
            r = self.and_()
            v = v if _truthy(v) else r
        return v

    def and_(self) -> object:
        v = self.eq()
        while self.peek() == ("OP", "&&"):
            self.eat()
            r = self.eq()
            v = r if _truthy(v) else v
        return v

    def eq(self) -> object:
        v = self.unary()
        while self.peek() in (("OP", "=="), ("OP", "!=")):
            op = self.eat()[1]
            same = _loose_eq(v, self.unary())
            v = same if op == "==" else not same
        return v

    def unary(self) -> object:
        if self.peek() == ("OP", "!"):
            self.eat()
            return not _truthy(self.unary())
        return self.primary()

    def primary(self) -> object:
        kind, val = self.peek()
        if (kind, val) == ("OP", "("):
            self.eat()
            v = self.or_()
            self.eat(")")
            return v
        if kind == "STR":
            self.eat()
            return val
        assert kind == "ID", f"unexpected token {kind} {val}"
        self.eat()
        if self.peek() == ("OP", "("):
            self.eat()
            self.eat(")")
            return self.ctx["fn"][val.lower()]()
        if val in ("true", "false"):
            return val == "true"
        if val == "null":
            return None
        return self.ctx["lookup"](val)


def _segments(raw: str) -> list[tuple[str, str]]:
    """Split a value into ("text", ...) and ("expr", ...) parts, as GitHub's
    template reader does. A value with no `${{` is one expression, because an
    `if` is always evaluated as one. Nothing is stripped: the newline a `>` or
    `|` block keeps is text."""
    if "${{" not in raw:
        return [("expr", raw)]
    parts: list[tuple[str, str]] = []
    i = 0
    while i < len(raw):
        start = raw.find("${{", i)
        if start < 0:
            parts.append(("text", raw[i:]))
            break
        if start > i:
            parts.append(("text", raw[i:start]))
        j, quoted = start + 3, False
        while quoted or not raw.startswith("}}", j):
            assert j < len(raw), f"unclosed ${{{{ in {raw!r}"
            quoted ^= raw[j] == "'"
            j += 1
        parts.append(("expr", raw[start + 3 : j]))
        i = j + 2
    return parts


def _condition(raw: str, ctx: dict) -> tuple[bool, bool]:
    """(value, whether it calls a status function) for a job's `if`.

    One expression is evaluated as itself. Anything else is joined into a
    `format()` string, the way GitHub does it, and a non-empty string is true:
    `${{ ... }} && x` and a `${{ ... }}` block that keeps its newline are always
    true, and with a status function inside they run after a failed need."""
    parts = _segments(raw)
    exprs = [_tokenize(src) for kind, src in parts if kind == "expr"]
    explicit = any(
        tok[0] == "ID" and tok[1].lower() in _STATUS_FNS and toks[n + 1 : n + 2] == [("OP", "(")]
        for toks in exprs
        for n, tok in enumerate(toks)
    )
    if len(parts) == 1:
        return _truthy(_Parser(exprs[0], ctx).parse()), explicit
    rendered = iter(_to_str(_Parser(toks, ctx).parse()) for toks in exprs)
    text = "".join(src if kind == "text" else next(rendered) for kind, src in parts)
    return text != "", explicit


# --------------------------------------------------------------------------
# The job-graph model.
# --------------------------------------------------------------------------


def _needs(job: dict) -> list[str]:
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


def _ancestors(jobs: dict, name: str) -> set[str]:
    seen: set[str] = set()
    stack = _needs(jobs[name])
    while stack:
        j = stack.pop()
        if j not in seen:
            seen.add(j)
            stack.extend(_needs(jobs[j]))
    return seen


def _order(jobs: dict) -> list[str]:
    order: list[str] = []
    while len(order) < len(jobs):
        for name, job in jobs.items():
            if name not in order and all(n in order for n in _needs(job)):
                order.append(name)
    return order


def _simulate(
    jobs: dict, *, dispatch: bool, skip: bool, fail: str | None = None, dry: bool = False
) -> dict[str, str]:
    """{job: success | failure | skipped}. `fail` names a job whose work fails
    if the job is allowed to run. `dry` is a pull request's dry run."""
    result: dict[str, str] = {}
    gate_outputs = {
        "skip_testpypi": "true" if dispatch and skip else "false",
        "publish": "false" if dry else "true",
    }
    for name in _order(jobs):
        job = jobs[name]
        ancestors_ok = all(result[a] == "success" for a in _ancestors(jobs, name))
        ancestors_failed = any(result[a] == "failure" for a in _ancestors(jobs, name))

        def lookup(path: str, _job: dict = job, _name: str = name) -> object:
            parts = path.split(".")
            if parts[0] == "needs":
                assert parts[1] in _needs(
                    _job
                ), f"{_name} reads needs.{parts[1]} without needing it"
                if parts[2] == "result":
                    return result[parts[1]]
                # A job that did not succeed exposes no outputs.
                ok = result[parts[1]] == "success"
                return gate_outputs.get(parts[3], "") if ok and parts[1] == "quality-gate" else ""
            if path == "github.event_name":
                return "pull_request" if dry else "workflow_dispatch" if dispatch else "push"
            raise AssertionError(f"{_name}: expression reads unmodelled `{path}`")

        ctx = {
            "lookup": lookup,
            "fn": {
                "success": lambda: ancestors_ok,
                "always": lambda: True,
                "failure": lambda: ancestors_failed,
                "cancelled": lambda: False,
            },
        }
        cond = job.get("if")
        if cond is None:
            runs = ancestors_ok
        else:
            value, explicit = _condition(str(cond), ctx)
            runs = value if explicit else (value and ancestors_ok)
        result[name] = ("failure" if fail == name else "success") if runs else "skipped"
    return result


# --------------------------------------------------------------------------
# The model's two GitHub rules, pinned on graphs small enough to read.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cond, runs",
    [
        ("needs.b.result == 'success'", False),  # implicit success() sees a's skip
        ("${{ !cancelled() && needs.b.result == 'success' }}", True),
    ],
)
def test_the_model_ands_the_implicit_success_over_every_ancestor(cond: str, runs: bool) -> None:
    """#702's trap: an `if` with no status function is ANDed with success(),
    which is false when ANY ancestor was skipped, not only a direct need."""
    jobs = {
        "a": {"if": "false"},
        "b": {"needs": "a", "if": "${{ !cancelled() }}"},
        "c": {"needs": "b", "if": cond},
    }
    got = _simulate(jobs, dispatch=False, skip=False)
    assert (got["a"], got["b"]) == ("skipped", "success"), got
    assert (got["c"] == "success") is runs, got


_GATED = "!cancelled() && needs.a.result == 'success'"


@pytest.mark.parametrize(
    "cond, runs",
    [
        (f"${{{{ {_GATED} }}}}", False),
        (_GATED, False),
        (f"${{{{ {_GATED} }}}}\n", True),  # `if: >` or `if: |` keeps the newline
        (f"${{{{ {_GATED} }}}} && true", True),
        ("${{ !cancelled() }} && ${{ needs.a.result == 'success' }}", True),
    ],
)
def test_the_model_runs_a_format_string_condition_after_a_failed_need(
    cond: str, runs: bool
) -> None:
    """Text around a `${{ }}` makes the `if` a format() string: "true\\n" or
    "false && true", never empty, so true. With a status function inside, no
    implicit success() guards it either."""
    jobs = {"a": {}, "b": {"needs": "a", "if": cond}}
    got = _simulate(jobs, dispatch=False, skip=False, fail="a")
    assert got["a"] == "failure" and (got["b"] != "skipped") is runs, got


@pytest.mark.parametrize("name", [n for n, j in _jobs().items() if "if" in j])
def test_no_job_condition_is_a_format_string(name: str) -> None:
    """The simulation already runs such a job the way GitHub would; this names
    it. actionlint's if-cond rule reports the same trap."""
    cond = str(_jobs()[name]["if"])
    assert [kind for kind, _ in _segments(cond)] == ["expr"], f"{name}: {cond!r} is always true"


_PATHS = {
    "tag push": dict(dispatch=False, skip=False),
    "dispatch": dict(dispatch=True, skip=False),
    "dispatch with skip_testpypi": dict(dispatch=True, skip=True),
    "pull request dry run": dict(dispatch=False, skip=False, dry=True),
}

# What a dry run leaves out: every job that uploads, signs or announces.
_NOT_IN_A_DRY_RUN = {
    "attest",
    "publish-testpypi",
    "verify-testpypi",
    "publish-pypi",
    "verify-pypi",
    "attest-image",
    "github-release",
    "verify-release",
    "verify-image",
}


@pytest.mark.parametrize("path", list(_PATHS))
def test_a_healthy_run_publishes_everything_it_should(path: str) -> None:
    """Guards against over-gating: with nothing failing, every job runs, except
    the two TestPyPI jobs on the outage path and the publishing jobs on a dry
    run."""
    jobs = _jobs()
    got = _simulate(jobs, **_PATHS[path])
    if _PATHS[path].get("dry"):
        skipped_on_purpose = _NOT_IN_A_DRY_RUN
    else:
        skipped_on_purpose = _TESTPYPI_JOBS if _PATHS[path]["skip"] else set()
    assert {j for j, r in got.items() if r == "skipped"} == skipped_on_purpose, got


@pytest.mark.parametrize("path", list(_PATHS))
@pytest.mark.parametrize("failing", list(_jobs()))
def test_no_job_runs_after_a_job_it_needs_failed(path: str, failing: str) -> None:
    jobs = _jobs()
    got = _simulate(jobs, fail=failing, **_PATHS[path])
    for name, job in jobs.items():
        if got[name] != "skipped":
            for need in _needs(job):
                assert got[need] != "failure", f"{name} ran although {need} failed ({path})"


def _after_pypi_failure(path: str) -> dict[str, str]:
    got = _simulate(_jobs(), fail="publish-pypi", **_PATHS[path])
    assert got["publish-pypi"] == "failure", got
    return got


@pytest.mark.parametrize("downstream", ["docker", "github-release", "verify-pypi"])
def test_outage_path_announces_nothing_unless_pypi_accepted_the_upload(downstream: str) -> None:
    """B, as reported. On the skip_testpypi path `docker` and `github-release`
    are gated on `build` alone, so when publish-pypi fails (for example `file
    already exists` when an old tag is re-dispatched) they still cut a GitHub
    Release and move the GHCR `latest`, `X.Y` and `<profile>-latest` tags: a
    version PyPI does not have, and `latest` rolled backwards."""
    got = _after_pypi_failure("dispatch with skip_testpypi")
    assert got[downstream] == "skipped", f"{downstream} ran although publish-pypi failed"


@pytest.mark.parametrize("path", ["tag push", "dispatch"])
@pytest.mark.parametrize("downstream", ["docker", "github-release"])
def test_normal_path_also_waits_for_pypi(path: str, downstream: str) -> None:
    """B, the decision. Same gap on a normal run: `docker` and `github-release`
    hang off `verify-testpypi`, so they run beside `publish-pypi` and ignore its
    result. A job's `needs` is static, so adding publish-pypi for the outage path
    makes the job wait for it on BOTH paths; waiting and then ignoring the result
    is the worst of both. The project's decision is to wait and gate on every
    path, as pypa/pipx and pytest do; this test holds it."""
    got = _after_pypi_failure(path)
    assert got[downstream] == "skipped", f"{downstream} ran although publish-pypi failed ({path})"


@pytest.mark.parametrize("path", [p for p in _PATHS if not _PATHS[p].get("dry")])
def test_verify_pypi_follows_a_successful_pypi_upload(path: str) -> None:
    assert _after_pypi_failure(path)["verify-pypi"] == "skipped"


def _ok(got: dict[str, str], job: str) -> bool:
    return got[job] == "success"


# When each job must run, from the results of the jobs before it and whether
# the run skips TestPyPI. A job that runs when its rule is false acts on a step
# that failed or never ran; one skipped when its rule is true is over-gated.
_RUNS_WHEN = {
    "quality-gate": lambda got, skip, dry: True,
    "build": lambda got, skip, dry: _ok(got, "quality-gate"),
    "attest": lambda got, skip, dry: not dry and _ok(got, "build"),
    "publish-testpypi": lambda got, skip, dry: not skip and _ok(got, "attest"),
    "verify-testpypi": lambda got, skip, dry: not skip and _ok(got, "publish-testpypi"),
    "publish-pypi": lambda got, skip, dry: _ok(got, "attest")
    and (skip or _ok(got, "verify-testpypi")),
    "verify-pypi": lambda got, skip, dry: _ok(got, "publish-pypi"),
    "docker": lambda got, skip, dry: _ok(got, "build") and (dry or _ok(got, "publish-pypi")),
    "attest-image": lambda got, skip, dry: not dry and _ok(got, "docker"),
    "release-notes": lambda got, skip, dry: _ok(got, "quality-gate"),
    "github-release": lambda got, skip, dry: _ok(got, "publish-pypi") and _ok(got, "release-notes"),
    "verify-release": lambda got, skip, dry: not dry and _ok(got, "github-release"),
    "verify-image": lambda got, skip, dry: not dry and _ok(got, "attest-image"),
}


def test_every_job_has_a_rule() -> None:
    assert set(_RUNS_WHEN) == set(_jobs())


@pytest.mark.parametrize("path", list(_PATHS))
@pytest.mark.parametrize("failing", [None, *_jobs()])
def test_every_job_runs_exactly_when_its_rule_holds(path: str, failing: str | None) -> None:
    """B in full, for every job. The tests above look only for a direct need
    that FAILED, so they miss a condition that rules out nothing else, such as
    `needs.publish-pypi.result != 'failure'`: when TestPyPI fails, publish-pypi
    is SKIPPED, and that condition would still cut a GitHub Release and move
    GHCR `latest` for a version PyPI never received."""
    got = _simulate(_jobs(), fail=failing, **_PATHS[path])
    skip, dry = _PATHS[path]["skip"], _PATHS[path].get("dry", False)
    wrong = [
        f"{job} {'ran' if got[job] != 'skipped' else 'was skipped'}"
        for job, rule in _RUNS_WHEN.items()
        if (got[job] != "skipped") is not rule(got, skip, dry)
    ]
    assert not wrong, f"{path}, {failing or 'nothing'} failing: {wrong} in {got}"


# --------------------------------------------------------------------------
# C. One validated tag.
# --------------------------------------------------------------------------


def _steps(job: str) -> list[dict]:
    return _jobs()[job]["steps"]


def test_github_release_uses_the_validated_tag() -> None:
    release = next(s for s in _steps("github-release") if "action-gh-release" in s.get("uses", ""))
    assert release["with"]["tag_name"].strip() == "${{ needs.quality-gate.outputs.tag }}"


def test_the_raw_tag_inputs_are_read_in_one_place() -> None:
    """`github.event.inputs.tag` and `github.ref_name` are read once, by the
    quality gate's shape check; every other job takes the validated output."""
    raw = re.compile(r"github\.event\.inputs\.tag|github\.ref_name|inputs\.tag\b")
    offenders = []
    for name, job in _jobs().items():
        for i, step in enumerate(job.get("steps", [])):
            if name == "quality-gate" and step.get("id") == "meta":
                continue
            if raw.search(json.dumps(step)):
                offenders.append(f"{name} step {i} ({step.get('name') or step.get('uses')})")
    assert not offenders, offenders


# --------------------------------------------------------------------------
# A. The quality gate against a fake GitHub backed by a real git repository.
# --------------------------------------------------------------------------

_RUNNER = r"""
const fs = require("fs");
const fx = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const calls = [];
const out = { failed: null, outputs: {}, warnings: [] };
const core = {
  setFailed: (m) => { out.failed = String(m); },
  setOutput: (k, v) => { out.outputs[k] = v; },
  notice: () => {}, info: () => {},
  warning: (m) => { out.warnings.push(String(m)); },
};
// Octokit throws a RequestError carrying the HTTP status.
const missing = () => Object.assign(new Error(fx.missing.message), { status: fx.missing.status });
const github = { rest: {
  repos: {
    getCommit: async ({ ref }) => {
      calls.push(["getCommit", ref]);
      if (!(ref in fx.refs)) throw missing();
      return { data: { sha: fx.refs[ref] } };
    },
    compareCommitsWithBasehead: async ({ basehead }) => {
      calls.push(["compare", basehead]);
      if (!(basehead in fx.compare)) throw missing();
      return { data: { status: fx.compare[basehead] } };
    },
  },
  actions: { listWorkflowRuns: async ({ head_sha }) => {
    calls.push(["listWorkflowRuns", head_sha]);
    return { data: { workflow_runs: [{ conclusion: "success", html_url: "https://ci/run" }] } };
  } },
} };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction("github", "context", "core", fx.script)(github, fx.context, core)
  .then(() => console.log(JSON.stringify({ ...out, calls })),
        (e) => console.log(JSON.stringify({ ...out, calls, threw: String(e) })));
"""


def _node() -> str:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI: the quality-gate script runs under it")
        pytest.skip("node not installed")
    return node


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    # A developer's commit.gpgsign=true would make every commit ask for a key.
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _status(repo: Path, base: str, head: str) -> str | None:
    """The `status` GitHub's compare API reports for base...head, or None
    where it answers 404: commits with no history in common."""
    try:
        _git(repo, "merge-base", base, head)
    except subprocess.CalledProcessError:
        return None
    ahead = int(_git(repo, "rev-list", "--count", f"{base}..{head}"))
    behind = int(_git(repo, "rev-list", "--count", f"{head}..{base}"))
    if not ahead and not behind:
        return "identical"
    return "ahead" if not behind else "behind" if not ahead else "diverged"


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """main: c1 - c2 - c3.  feature: c1 - f1 (diverged from main).
    orphan: o1 (no history in common with main)."""
    repo = tmp_path_factory.mktemp("repo")
    _git(repo, "init", "-q", "-b", "main")
    shas: dict[str, str] = {}
    for name in ("c1", "c2", "c3"):
        _git(repo, "commit", "-q", "--allow-empty", "-m", name)
        shas[name] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature", shas["c1"])
    _git(repo, "commit", "-q", "--allow-empty", "-m", "f1")
    shas["f1"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "o1")
    shas["o1"] = _git(repo, "rev-parse", "HEAD")
    shas["_repo"] = str(repo)
    return shas


def _run_gate(
    history: dict[str, str],
    tmp_path: Path,
    *,
    event: str,
    tag: str,
    tag_commit: str | None,
    start: str,
    ref: str = "refs/heads/main",
    missing: tuple[int, str] = (404, "Not Found"),
) -> dict:
    """Run the real quality-gate `commit` step. `start` is the commit the run
    starts from (context.sha): main's head for `--ref main`, the tag's own
    commit for `--ref vX.Y.Z` or a tag push. `tag_commit=None` is a tag that
    does not exist; `missing` is what GitHub answers for it, and for a compare
    of commits with no history in common."""
    repo = Path(history["_repo"])
    step = next(s for s in _steps("quality-gate") if s.get("id") == "commit")
    ctx_sha = history[start]
    refs, compare = {}, {}
    if tag_commit is not None:
        refs[f"refs/tags/{tag}"] = history[tag_commit]
        status = _status(repo, history[tag_commit], ctx_sha)
        if status:
            compare[f"{history[tag_commit]}...{ctx_sha}"] = status
    fixture = {
        "script": step["with"]["script"],
        "refs": refs,
        "compare": compare,
        "missing": {"status": missing[0], "message": missing[1]},
        "context": {
            "eventName": event,
            "sha": ctx_sha,
            "ref": ref,
            "repo": {"owner": "o", "repo": "r"},
        },
    }
    (tmp_path / "fx.json").write_text(json.dumps(fixture))
    (tmp_path / "run.js").write_text(_RUNNER)
    proc = subprocess.run(
        [_node(), str(tmp_path / "run.js"), str(tmp_path / "fx.json")],
        env={**os.environ, "TAG": tag},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _compares(got: dict) -> list[list[str]]:
    return [c for c in got["calls"] if c[0] == "compare"]


def test_gate_accepts_a_tag_on_main_dispatched_from_main(history, tmp_path) -> None:
    """v0.19.0's case: its own release.yml has no skip_testpypi, so the run
    starts from main. It passes, and says the provenance names main."""
    got = _run_gate(
        history, tmp_path, event="workflow_dispatch", tag="v1.0.0", tag_commit="c2", start="c3"
    )
    assert got["failed"] is None and got["outputs"]["sha"] == history["c2"], got
    assert _compares(got) == [["compare", f"{history['c2']}...{history['c3']}"]], got["calls"]
    (warning,) = got["warnings"]
    for part in ("refs/heads/main", history["c3"], "v1.0.0", history["c2"], "--ref v1.0.0"):
        assert part in warning, (part, warning)


@pytest.mark.parametrize(
    "event, calls",
    [
        # `--ref vX.Y.Z`: the run's sha is the tag's, so provenance is exact.
        ("workflow_dispatch", ["getCommit", "listWorkflowRuns"]),
        # A tag push is left as it was: no lookup, no compare.
        ("push", ["listWorkflowRuns"]),
    ],
)
def test_gate_has_nothing_to_compare_on_a_run_from_the_tag(
    history, tmp_path, event: str, calls: list[str]
) -> None:
    got = _run_gate(
        history,
        tmp_path,
        event=event,
        tag="v1.0.0",
        tag_commit="c2",
        start="c2",
        ref="refs/tags/v1.0.0",
    )
    assert got["failed"] is None and got["outputs"]["sha"] == history["c2"], got
    assert [c[0] for c in got["calls"]] == calls and not got["warnings"], got


@pytest.mark.parametrize(
    "missing",
    [(404, "Not Found"), (422, "No commit found for SHA: refs/tags/v9.9.9")],
)
def test_gate_names_a_tag_that_does_not_exist(history, tmp_path, missing) -> None:
    """A mistyped or unpushed tag fails the gate with the tag's name, not with
    a bare 'Not Found' thrown out of the script."""
    got = _run_gate(
        history,
        tmp_path,
        event="workflow_dispatch",
        tag="v9.9.9",
        tag_commit=None,
        start="c3",
        missing=missing,
    )
    assert "threw" not in got, got
    assert got["failed"] and "v9.9.9" in got["failed"] and missing[1] in got["failed"], got
    assert "sha" not in got["outputs"], got
    assert [c[0] for c in got["calls"]] == ["getCommit"], got["calls"]


@pytest.mark.parametrize(
    "tag_commit, start, status",
    [
        ("f1", "c3", "diverged"),  # tag on a branch main does not contain
        ("c3", "c2", "behind"),  # tag newer than the ref the run started from
        ("o1", "c3", "Not Found"),  # no history in common: the compare fails
    ],
)
def test_gate_refuses_a_start_ref_that_does_not_contain_the_tag(
    history, tmp_path, tag_commit: str, start: str, status: str
) -> None:
    """A: the build would be of the tag's commit while provenance names the
    start ref's, a commit the artifact was never built from. A compare that
    fails (GitHub's 404 for commits with no history in common, or an outage)
    fails closed, not open and not with a bare error thrown out of the step."""
    got = _run_gate(
        history,
        tmp_path,
        event="workflow_dispatch",
        tag="v1.0.0",
        tag_commit=tag_commit,
        start=start,
    )
    assert got["failed"] and "threw" not in got, f"gate passed a {status} tag: {got}"
    assert "sha" not in got["outputs"], got
    # It names the tag, both commits and the status, and says what to do.
    for part in ("v1.0.0", history[tag_commit], history[start], status, "--ref v1.0.0"):
        assert part in got["failed"], (part, got["failed"])
    # Refused before the ci.yml lookup: nothing past this point runs.
    assert "listWorkflowRuns" not in [c[0] for c in got["calls"]], got["calls"]


# --------------------------------------------------------------------------
# The build backend comes from the hash lock, never from PyPI at build time.
# --------------------------------------------------------------------------


def _locked(name: str) -> dict[str, str]:
    """{canonical name: pinned version} of requirements/<name>.txt."""
    pins: dict[str, str] = {}
    for line in (REPO / "requirements" / f"{name}.txt").read_text().splitlines():
        m = re.match(r"([A-Za-z0-9._-]+)==([^\s;\\]+)", line)
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def test_the_release_is_built_without_build_isolation() -> None:
    """With isolation, build installs [build-system] requires fresh from PyPI,
    unpinned and unhashed, on every release."""
    (step,) = [s for s in _steps("build") if s.get("name") == "Build wheel and sdist"]
    assert step["run"].split() == ["python", "-m", "build", "--no-isolation"], step["run"]


def test_every_build_requirement_is_hash_locked_for_the_release() -> None:
    """build --no-isolation stops when a requirement is missing, so a new entry in
    [build-system] requires that is not in the lock fails the release; this
    test says so on the pull request instead."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pins = _locked("release-tools")
    for req in map(Requirement, pyproject["build-system"]["requires"]):
        name = canonicalize_name(req.name)
        assert name in pins, f"{req} is missing from requirements/release-tools.txt"
        assert req.specifier.contains(
            pins[name], prereleases=True
        ), f"{name}=={pins[name]} vs {req}"


# --------------------------------------------------------------------------
# The image holds the release's own wheel and builds nothing.
# --------------------------------------------------------------------------


def _instructions(path: Path) -> list[str]:
    """A Dockerfile's instructions, continuation lines joined, comments dropped."""
    out: list[str] = []
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#") or (not s and not buf):
            continue
        more = s.endswith("\\")
        buf.append(s[:-1].strip() if more else s)
        if not more:
            out.append(" ".join(buf))
            buf = []
    return out


def _pip_installs(path: Path) -> list[str]:
    return [
        cmd.strip()
        for ins in _instructions(path)
        if ins.startswith("RUN ")
        for cmd in re.split(r"&&|;", ins[4:])
        if "pip install" in cmd
    ]


def test_the_image_installs_the_wheel_and_builds_nothing() -> None:
    """Built from the source tree, the image fetched a fresh build backend and,
    with no .git in the context, setuptools-scm gave it version 0.0.0+unknown
    (the 0.41.0 image reports that)."""
    copies = [i for i in _instructions(REPO / "Dockerfile") if i.startswith("COPY ")]
    assert "COPY dist/*.whl /tmp/dist/" in copies, copies
    assert not [c for c in copies if "fluid_build" in c or "pyproject.toml" in c], copies
    installs = _pip_installs(REPO / "Dockerfile")
    assert installs, "no pip install found in the Dockerfile"
    for cmd in installs:
        # Either a hash-locked file or the one wheel copied from dist/.
        assert "--require-hashes -r " in cmd or cmd.endswith('"$1[${extras}]"'), cmd


def test_the_docker_job_builds_the_image_around_this_release_s_wheel() -> None:
    names = [s.get("name") for s in _steps("docker")]
    download = next(s for s in _steps("docker") if s.get("name") == "Download wheel")
    assert download["with"] == {"name": "dist", "path": "dist/"}, download
    check = names.index("Check the image holds this release's wheel")
    assert names.index("Download wheel") < names.index("Build image for scanning (no push)") < check
    assert check < names.index("Build and push Docker image"), names
    step = _steps("docker")[check]
    assert step["env"]["VERSION"] == "${{ needs.quality-gate.outputs.version }}", step
    assert "fluid_build.__version__" in step["run"] and "exit 1" in step["run"], step["run"]


# --------------------------------------------------------------------------
# Signing happens only in jobs that run no code fetched at release time.
# --------------------------------------------------------------------------

_SIGNERS = {"attest", "attest-image", "publish-testpypi", "publish-pypi"}


def test_only_jobs_that_run_no_fetched_code_can_sign() -> None:
    """A job with `id-token: write` can sign anything as this workflow. The
    jobs that build, install or run the package (whose dependencies come from
    PyPI at release time) hold no such token; the jobs that do run pinned
    actions and shell that installs and runs nothing."""
    jobs = _jobs()
    signers = {
        n for n, j in jobs.items() if (j.get("permissions") or {}).get("id-token") == "write"
    }
    assert signers == _SIGNERS, signers
    runs_code = re.compile(r"pip install|python -m build|docker (build|run)|\bfluid\b|cyclonedx")
    for name in sorted(signers):
        for step in jobs[name]["steps"]:
            assert not runs_code.search(step.get("run", "")), (name, step.get("name"))


def _record_and_check(tmp_path: Path, tamper: bool) -> subprocess.CompletedProcess:
    """Run the build job's digest step, then the attest job's check, as bash."""
    record = next(s for s in _steps("build") if s.get("id") == "digests")["run"]
    check = next(
        s
        for s in _steps("attest")
        if s.get("name") == "Check the files are the ones the build job made"
    )["run"]
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "pkg-1.0.tar.gz").write_bytes(b"sdist")
    out = tmp_path / "out"
    env = {**os.environ, "GITHUB_OUTPUT": str(out)}
    subprocess.run(["bash", "-c", record], cwd=tmp_path, env=env, check=True, capture_output=True)
    (line,) = [ln for ln in out.read_text().splitlines() if ln.startswith("sha256=")]
    if tamper:
        (dist / "pkg-1.0-py3-none-any.whl").write_bytes(b"other wheel")
    env["SHA256"] = line.removeprefix("sha256=")
    return subprocess.run(
        ["bash", "-c", check], cwd=tmp_path, env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize("tamper", [False, True])
def test_the_attest_job_signs_only_the_files_the_build_recorded(tmp_path, tamper: bool) -> None:
    """The smoke install runs the package's dependencies in the build job, after
    the digests are taken and the files uploaded; a file changed after that is
    refused before anything is signed."""
    got = _record_and_check(tmp_path, tamper)
    assert (got.returncode != 0) is tamper, got.stdout + got.stderr
    build = [s.get("name") for s in _steps("build")]
    order = [
        build.index("Build wheel and sdist"),
        build.index("Record the distribution files' digests"),
        build.index("Upload build artifacts"),
        build.index("Isolated wheel install and verify"),
    ]
    assert order == sorted(order), build
    assert _jobs()["build"]["outputs"]["sha256"] == "${{ steps.digests.outputs.sha256 }}"
    attest = _steps("attest")
    check = next(i for i, s in enumerate(attest) if s.get("env", {}).get("SHA256"))
    signing = [i for i, s in enumerate(attest) if "attest-build-provenance" in s.get("uses", "")]
    assert attest[check]["env"]["SHA256"] == "${{ needs.build.outputs.sha256 }}"
    assert signing and check < min(signing), attest


def test_the_image_attestation_signs_the_digest_the_docker_job_pushed() -> None:
    docker = _jobs()["docker"]
    assert docker["outputs"]["digest"] == "${{ steps.push.outputs.digest }}"
    (step,) = [s for s in _steps("attest-image") if "attest-build-provenance" in s.get("uses", "")]
    assert step["with"]["subject-digest"] == "${{ needs.docker.outputs.digest }}"
    assert step["with"]["push-to-registry"] is True


# --------------------------------------------------------------------------
# The dry run: a pull request rehearses the release and publishes nothing.
# --------------------------------------------------------------------------

_PUBLISHING_ACTIONS = (
    "docker/login-action",
    "pypa/gh-action-pypi-publish",
    "actions/attest-build-provenance",
    "softprops/action-gh-release",
)


def _publishes(step: dict) -> bool:
    """A step that logs in, uploads, signs, pushes or announces."""
    uses = step.get("uses", "")
    if uses.startswith(_PUBLISHING_ACTIONS):
        return True
    return uses.startswith("docker/build-push-action") and step.get("with", {}).get("push") is True


def _step_runs(job: str, step: dict, *, dry: bool) -> bool:
    cond = step.get("if")
    if cond is None:
        return True

    def lookup(path: str) -> object:
        if path in ("needs.quality-gate.outputs.publish", "steps.meta.outputs.publish"):
            return "false" if dry else "true"
        raise AssertionError(f"{job}: step condition reads unmodelled `{path}`")

    fns = {"success": lambda: True, "always": lambda: True}
    fns.update({"failure": lambda: False, "cancelled": lambda: False})
    return _condition(str(cond), {"lookup": lookup, "fn": fns})[0]


@pytest.mark.parametrize("path", list(_PATHS))
def test_a_dry_run_uploads_signs_pushes_and_announces_nothing(path: str) -> None:
    """Job conditions keep the publishing jobs out of a dry run; step conditions
    keep docker's login and push out of it. On a real run every one runs."""
    jobs = _jobs()
    dry = _PATHS[path].get("dry", False)
    ran = _simulate(jobs, **_PATHS[path])
    steps = [
        (name, step)
        for name, job in jobs.items()
        if ran[name] != "skipped"
        for step in job.get("steps", [])
        if _publishes(step)
    ]
    running = [
        f"{n}: {s.get('name') or s.get('uses')}" for n, s in steps if _step_runs(n, s, dry=dry)
    ]
    if dry:
        assert not running, running
        # Not vacuous: the dry run reaches docker, whose login and push it skips.
        assert ran["docker"] == "success" and [n for n, _ in steps] == ["docker", "docker"]
    else:
        assert len(running) == len(steps) > 0, running


def test_no_job_in_a_dry_run_holds_a_signing_token() -> None:
    """A same-repository pull request gets the token scopes a job asks for. The
    only write scope a dry run hands out is docker's, whose push is skipped."""
    jobs = _jobs()
    ran = _simulate(jobs, **_PATHS["pull request dry run"])
    writes = {
        (name, scope)
        for name, job in jobs.items()
        if ran[name] != "skipped"
        for scope, level in (job.get("permissions") or {}).items()
        if level == "write"
    }
    assert writes == {("docker", "packages")}, writes


def test_a_dry_run_never_shares_the_release_concurrency_group() -> None:
    """A group keeps one pending run and cancels the one before it, so a dry run
    in the `release` group could cancel a tag push waiting there."""
    conc = yaml.safe_load(WORKFLOW.read_text())["concurrency"]
    assert conc["group"] == (
        "${{ github.event_name == 'pull_request' && "
        "format('release-dry-run-{0}', github.ref) || 'release' }}"
    )
    assert conc["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_a_change_to_what_builds_the_release_runs_the_dry_run() -> None:
    on = yaml.safe_load(WORKFLOW.read_text())[True]  # PyYAML reads the `on` key as True
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}, set(on)
    paths = set(on["pull_request"]["paths"])
    must = {
        ".github/workflows/release.yml",
        "requirements/release-tools.txt",
        "requirements/release-image.txt",
        "Dockerfile",
        "pyproject.toml",
        "requirements/release-tools.in",
        "requirements/release-image.in",
        ".grype.yaml",
        "MANIFEST.in",
    }
    assert must <= paths, must - paths
    assert [p for p in paths if not (REPO / p).exists()] == []


@pytest.mark.parametrize(
    "event, tag, publish",
    [
        ("pull_request", "1234/merge", "false"),
        ("pull_request", "v1.2.0", "false"),  # a tag-shaped ref is still a dry run
        ("push", "v1.2.0", "true"),
        ("workflow_dispatch", "v1.2.0rc1", "true"),
    ],
)
def test_the_gate_publishes_only_outside_a_pull_request(
    tmp_path, event: str, tag: str, publish: str
) -> None:
    step = next(s for s in _steps("quality-gate") if s.get("id") == "meta")
    out = tmp_path / "out"
    env = {
        **os.environ,
        "EVENT_NAME": event,
        "TAG_INPUT": tag,
        "SKIP_TESTPYPI_INPUT": "false",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_ACTOR": "someone",
    }
    subprocess.run(["bash", "-c", step["run"]], env=env, check=True, capture_output=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs["publish"] == publish, outputs
    if publish == "false":
        assert outputs["tag"] == outputs["version"] == "", outputs
    else:
        assert outputs["tag"] == tag and outputs["version"] == tag[1:], outputs


_CHANGELOG = """# Changelog

## [Unreleased]

### Added

- **A greenhouse view.**

## [1.2.0] — 2026-01-02

### Added

- **A garden planner.**

## [1.1.0] — 2026-01-01

### Added

- **A seed list.**
"""


def _notes(
    tmp_path: Path, changelog: str, *, publish: bool, version: str = "", newer_tag: str = ""
) -> tuple:
    """Run the release-notes step under bash on a repository tagged v1.2.0."""
    step = next(s for s in _steps("release-notes") if s.get("name", "").startswith("Generate"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "tag", "v1.2.0")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    if newer_tag:
        _git(repo, "tag", newer_tag)
    summary = tmp_path / "summary.md"
    env = {
        **os.environ,
        "PUBLISH": "true" if publish else "false",
        "VERSION": version,
        "PROFILE": "stable",
        "IS_PRE": "false",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    proc = subprocess.run(
        ["bash", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True
    )
    notes = repo / "release-notes.md"
    return (
        proc,
        notes.read_text() if notes.exists() else "",
        summary.read_text() if summary.exists() else "",
    )


def test_a_dry_run_reads_the_newest_tag_s_section(tmp_path) -> None:
    proc, notes, summary = _notes(tmp_path, _CHANGELOG, publish=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "A garden planner." in notes and "A seed list." not in notes, notes
    assert "A greenhouse view." not in notes and "data-product-forge==1.2.0" in notes, notes
    assert "A garden planner." in summary, summary


def test_a_dry_run_skips_a_newer_pre_release_tag_that_has_no_section(tmp_path) -> None:
    """A pre-release's real run falls back to git log, so its section may not
    exist yet; the dry run must not go red over it."""
    proc, notes, _ = _notes(tmp_path, _CHANGELOG, publish=False, newer_tag="v1.3.0rc1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "A garden planner." in notes and "==1.2.0" in notes, notes


def test_a_dry_run_fails_when_the_newest_tag_s_section_is_not_found(tmp_path) -> None:
    """The extractor once matched nothing for every release and fell back to
    git log in silence; a dry run reads a section that exists, so it says so."""
    broken = _CHANGELOG.replace("## [1.2.0] — 2026-01-02", "## 1.2.0 - 2026-01-02")
    proc, _, _ = _notes(tmp_path, broken, publish=False)
    assert proc.returncode != 0 and "::error::" in proc.stdout, proc.stdout


def test_a_release_still_falls_back_to_git_log_with_a_warning(tmp_path) -> None:
    proc, notes, summary = _notes(tmp_path, _CHANGELOG, publish=True, version="1.3.0")
    assert proc.returncode == 0 and "::warning::" in proc.stdout, proc.stdout + proc.stderr
    assert "- second (" in notes and summary == "", notes


# --------------------------------------------------------------------------
# verify-release and verify-image check what was published, with a fake `gh`.
# --------------------------------------------------------------------------

_FAKE_GH = """#!{python}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["release", "download"]:
    out = Path(args[args.index("--dir") + 1])
    out.mkdir(exist_ok=True)
    for name in os.environ["FAKE_RELEASE_FILES"].split():
        (out / name).write_text("x", encoding="utf-8")
    sys.exit(0)
if args[:2] == ["attestation", "verify"]:
    if "--format" in args:
        digest = os.environ["FAKE_DIGEST"]
        print(json.dumps([{"verificationResult": {"statement": {"subject": [{"digest": {"sha256": digest}}]}}}]))
    sys.exit(int(os.environ.get("FAKE_VERIFY_RC", "0")))
sys.exit(2)
"""

_POLICY = [
    "--repo",
    "o/r",
    "--signer-workflow",
    "o/r/.github/workflows/release.yml",
    "--source-ref",
    "refs/tags/v1.2.0",
    "--source-digest",
    "c0ffee",
    "--deny-self-hosted-runners",
    "--predicate-type",
    "https://slsa.dev/provenance/v1",
]
_FILES = "pkg-1.2.0-py3-none-any.whl pkg-1.2.0.tar.gz sbom.cdx.json"
_BUNDLES = "pkg-1.2.0-py3-none-any.whl.intoto.jsonl pkg-1.2.0.tar.gz.intoto.jsonl"


def _verify(tmp_path: Path, job: str, **env: str) -> tuple:
    """Run a verify job's step under bash; returns (process, gh calls)."""
    if not shutil.which("jq"):
        pytest.skip("jq not installed")
    (step,) = _steps(job)
    fake = tmp_path / "bin"
    fake.mkdir()
    gh = fake / "gh"
    gh.write_text(_FAKE_GH.replace("{python}", sys.executable), encoding="utf-8")
    gh.chmod(0o700)
    log = tmp_path / "gh.log"
    run_env = {
        **os.environ,
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(log),
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_REF": "refs/tags/v1.2.0",
        "GITHUB_SHA": "c0ffee",
        **env,
    }
    proc = subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=run_env, capture_output=True, text=True
    )
    calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    return proc, calls


def _pins_the_policy(call: list[str]) -> bool:
    n = len(_POLICY)
    return any(call[i : i + n] == _POLICY for i in range(len(call) - n + 1))


def test_every_release_file_is_verified_with_its_bundle_and_online(tmp_path) -> None:
    proc, calls = _verify(
        tmp_path,
        "verify-release",
        TAG="v1.2.0",
        FAKE_RELEASE_FILES=f"{_FILES} {_BUNDLES}",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls[0][:3] == ["release", "download", "v1.2.0"], calls
    verifies = [c for c in calls if c[:2] == ["attestation", "verify"]]
    for file in ("pkg-1.2.0-py3-none-any.whl", "pkg-1.2.0.tar.gz"):
        mine = [c for c in verifies if c[2] == file]
        assert [("--bundle" in c) for c in mine] == [True, False], mine
        assert all(_pins_the_policy(c) for c in mine), mine
        assert mine[0][mine[0].index("--bundle") + 1] == f"{file}.intoto.jsonl"
    assert len(verifies) == 4, verifies


@pytest.mark.parametrize(
    "files, rc, says",
    [
        (
            f"{_FILES} pkg-1.2.0-py3-none-any.whl.intoto.jsonl",
            0,
            "no pkg-1.2.0.tar.gz.intoto.jsonl",
        ),
        (f"pkg-1.2.0-py3-none-any.whl {_BUNDLES}", 0, "exactly one wheel and one sdist"),
        (f"{_FILES} {_BUNDLES}", 1, ""),  # gh attestation verify refuses
    ],
)
def test_a_release_file_that_does_not_verify_fails_the_job(tmp_path, files, rc, says) -> None:
    proc, _ = _verify(
        tmp_path,
        "verify-release",
        TAG="v1.2.0",
        FAKE_RELEASE_FILES=files,
        FAKE_VERIFY_RC=str(rc),
    )
    assert proc.returncode != 0 and says in proc.stdout, proc.stdout + proc.stderr


_TAGS = "ghcr.io/o/r:1.2.0\nghcr.io/o/r:1.2\nghcr.io/o/r:stable-latest\nghcr.io/o/r:latest\n"


@pytest.mark.parametrize("resolves_to, ok", [("aaaa", True), ("bbbb", False)])
def test_every_moved_image_tag_must_name_the_pushed_digest(tmp_path, resolves_to, ok) -> None:
    proc, calls = _verify(
        tmp_path,
        "verify-image",
        DIGEST="sha256:aaaa",
        TAGS=_TAGS,
        FAKE_DIGEST=resolves_to,
    )
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    oci = [c for c in calls if c[:2] == ["attestation", "verify"]]
    assert all(_pins_the_policy(c) for c in oci), oci
    if ok:
        tags = [t for t in _TAGS.splitlines() if t]
        assert [c[2] for c in oci] == [f"oci://{t}" for t in tags] + [f"oci://{tags[0]}"], oci
        assert "--bundle-from-oci" in oci[-1], oci
    else:
        assert "ghcr.io/o/r:1.2.0 resolves to sha256:bbbb" in proc.stdout, proc.stdout


def test_an_image_check_with_nothing_pushed_fails(tmp_path) -> None:
    proc, calls = _verify(tmp_path, "verify-image", DIGEST="", TAGS="", FAKE_DIGEST="x")
    assert proc.returncode != 0 and not calls, proc.stdout
