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
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"

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


def _simulate(jobs: dict, *, dispatch: bool, skip: bool, fail: str | None = None) -> dict[str, str]:
    """{job: success | failure | skipped}. `fail` names a job whose work fails
    if the job is allowed to run."""
    result: dict[str, str] = {}
    gate_outputs = {"skip_testpypi": "true" if dispatch and skip else "false"}
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
                return "workflow_dispatch" if dispatch else "push"
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
}


@pytest.mark.parametrize("path", list(_PATHS))
def test_a_healthy_run_publishes_everything_it_should(path: str) -> None:
    """Guards against over-gating: with nothing failing, every job runs, except
    the two TestPyPI jobs on the outage path."""
    jobs = _jobs()
    got = _simulate(jobs, **_PATHS[path])
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


@pytest.mark.parametrize("path", list(_PATHS))
def test_verify_pypi_follows_a_successful_pypi_upload(path: str) -> None:
    assert _after_pypi_failure(path)["verify-pypi"] == "skipped"


def _ok(got: dict[str, str], job: str) -> bool:
    return got[job] == "success"


# When each job must run, from the results of the jobs before it and whether
# the run skips TestPyPI. A job that runs when its rule is false acts on a step
# that failed or never ran; one skipped when its rule is true is over-gated.
_RUNS_WHEN = {
    "quality-gate": lambda got, skip: True,
    "build": lambda got, skip: _ok(got, "quality-gate"),
    "publish-testpypi": lambda got, skip: not skip and _ok(got, "build"),
    "verify-testpypi": lambda got, skip: not skip and _ok(got, "publish-testpypi"),
    "publish-pypi": lambda got, skip: _ok(got, "build") and (skip or _ok(got, "verify-testpypi")),
    "verify-pypi": lambda got, skip: _ok(got, "publish-pypi"),
    "docker": lambda got, skip: _ok(got, "publish-pypi"),
    "github-release": lambda got, skip: _ok(got, "publish-pypi"),
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
    wrong = [
        f"{job} {'ran' if got[job] != 'skipped' else 'was skipped'}"
        for job, rule in _RUNS_WHEN.items()
        if (got[job] != "skipped") is not rule(got, _PATHS[path]["skip"])
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
    if node:
        return node
    if os.environ.get("CI"):
        pytest.fail("node is required in CI: the quality-gate script runs under it")
    pytest.skip("node not installed")


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
