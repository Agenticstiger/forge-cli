# Agent skills eval

Do coding agents write valid, faithful FLUID contracts, and do the agent skills in
`fluid_build/agent_skills/` help? This harness answers that with numbers.

Each scenario under `scenarios/` is one request a data engineer would make. Every arm
gets the same request in a fresh workspace:

| Arm | What runs |
|---|---|
| `no-skill` | `claude -p` in the workspace as it is |
| `skill` | the same, after `fluid scaffold-ide --target claude-code` (skills, index, validate hook) |
| `forge` | `fluid forge --llm-provider claude-code` AI mode, with no outer agent |
| `codex-no-skill` | `codex exec` in the workspace as it is |
| `codex-skill` | the same, after `fluid scaffold-ide --target generic` (`.agents/skills/` and the `AGENTS.md` index; no hook) |

`run` runs the first three by default. The Codex arms are there because agents differ in
how often they use a skill (SkillsBench, arXiv 2602.12670), so whether the skills help is
measured per agent.

The contract each arm leaves behind is graded without an LLM:

- **valid**: `fluid validate --offline` exits 0 for every contract the scenario expects;
- **fidelity**: the share of the scenario's checks that pass (type and layer, bindings,
  columns and types, quality rules, schedule, `consumes[]`, what a refine must keep),
  plus two that always run: no invented email, no leftover starter values;
- **pass**: valid and every check passes.

## The report

`report.md` opens with a table of the arms: the agent, its version, the model and effort
each was pinned to, and the model the agent said it ran when that differs. Then, per arm:

| Column | Meaning |
|---|---|
| valid@1 | share of runs whose contracts are valid |
| pass@1 | the full-pass rate: share of runs that pass |
| pass@k | the chance that at least one of k runs passes |
| pass^k | the chance that all k runs pass |
| Fidelity | mean share of checks passed |

pass@k is the unbiased estimator of Chen et al. 2021 (arXiv 2107.03374), and pass^k the
one from tau-bench (Yao et al. 2024, arXiv 2406.12045). Both columns appear when every
scenario of every arm has at least two runs, with k the smallest number of runs. At k = N
they read: did any of the N runs pass, and did all of them. A gap between the two is a
flaky arm. Each rate is the mean over scenarios of that scenario's share, so a scenario
with more runs does not weigh more.

A run that timed out, whose agent errored or exited non-zero, or that hit a harness error
is a failed run. It counts in every denominator, is never valid or passed, and adds 0 to
fidelity. Its grade stays in the record for diagnosis. The report has an Errors column
and lists the reason. A timeout kills the agent's whole process tree.

Tokens, cost and turns are each agent's own counts. Claude Code reports `modelUsage` and a
list-price cost. Codex reports tokens only; its cached input is shown under Cache-read.
Cost and turns show `-` for Codex, never a zero. Used a skill counts the error-free runs
that called the Skill tool on a FLUID skill or read a FLUID skill's files.

The per-scenario table shows one run as `pass 16/16` (its checks) and several as
`2/3 pass, 97%` (runs passed, mean fidelity). Failed checks are listed per scenario and
arm, with how many of the runs failed each one.

## Negative controls

A scenario with `should_trigger: false` is a request no FLUID skill covers: a stdlib
Python script over a CSV, a README typo next to a valid contract, a DuckDB query. It runs
the `no-skill` and `skill` conditions on each agent. Instead of the contract checks, a
control run gets two checks (`grade` on a bare workspace, which has no transcript, gets only
the second):

- `no_skill_fired`: no FLUID skill was used. That means a Skill tool call naming one, or a
  read of one of its files: with the Read tool, or a shell command that names the skill's
  directory (`cd .agents/skills && cat <name>/SKILL.md` counts too). A glob over every
  `SKILL.md` fires each FLUID skill installed in the workspace. Codex reads a skill's
  `SKILL.md` with a shell command, so for Codex the shell commands are the whole record.
- `contracts_untouched`: no `*.fluid.yaml` was written, changed or deleted.

The report's negative-controls table gives, per arm, the runs that fired a FLUID skill,
the runs that touched a contract, and the runs that did the task.

## Run it

It spends real model usage, so it is opt-in. A Claude run per scenario and arm costs
about $0.08 (`skill`, `no-skill`) to $0.17 (`forge`) of Sonnet usage at list price.

```bash
claude auth status            # must exit 0
FLUID_AGENT_EVAL=1 python -m pytest -m agent_eval tests/agent_evals -s
```

Or drive the harness directly, which also narrows the run:

```bash
python tests/agent_evals/harness.py run --runs 3
python tests/agent_evals/harness.py run --scenarios sdp-local-orders --arms no-skill,skill
python tests/agent_evals/harness.py run --forge-src ../forge-cli-other-branch
python tests/agent_evals/harness.py grade <workspace> <scenario-id>
python tests/agent_evals/harness.py report tests/agent_evals/runs/<timestamp>
python tests/agent_evals/harness.py regrade tests/agent_evals/runs/<timestamp>
```

The pytest entry reads `FLUID_AGENT_EVAL_SCENARIOS`, `FLUID_AGENT_EVAL_ARMS`,
`FLUID_AGENT_EVAL_RUNS`, `FLUID_AGENT_EVAL_MODEL`, `FLUID_AGENT_EVAL_EFFORT`,
`FLUID_AGENT_EVAL_FORGE_SRC` and, for the Codex arms, `FLUID_AGENT_EVAL_CODEX_MODEL`,
`FLUID_AGENT_EVAL_CODEX_EFFORT` and `FLUID_AGENT_EVAL_CODEX_PROVIDER`.

The Codex arms need a model to name and an API key. `codex debug models` lists the model
ids. The key is read from `CODEX_API_KEY`, else `OPENAI_API_KEY`, in the harness's own
environment, so set it for that one process:

```bash
CODEX_API_KEY=... python tests/agent_evals/harness.py run --runs 3 \
    --arms no-skill,skill,codex-no-skill,codex-skill --codex-model gpt-5.5
```

`--codex-provider ollama --codex-model <local model>` runs the Codex arms on a local
Ollama model instead: no key and no spend. That checks the plumbing, not a model you
would compare. A run that cannot finish stops before any arm spends anything. That covers
an unknown arm, no `codex` on PATH, no `--codex-model`, and no key.

`regrade` grades every kept workspace of a run again against the current scenarios and
grader (agent arms with their recorded commands, the forge arm without), then rewrites
`results.json` and `report.md`. The rest of each record stays; `meta` gains `regraded_at`
and `grader_commit`. A run whose workspace is gone keeps its old grade and is listed.

`report.md` and `results.json` land in `tests/agent_evals/runs/<timestamp>/` (git-ignored).
Each run's workspace, transcript and stderr stay in the temp dir
(`<tmp>/fluid-agent-evals-<timestamp>-XXXX/<scenario>/<arm>/run-<n>/`, a fresh `mkdtemp`;
`results.json` records the path). An `--out` that already holds results keeps the runs a
new call does not repeat; a run is its scenario, arm and trial number, so redo a scenario
with the same `--runs` it had (a smaller one replaces only the first trials).

## Pinned model and effort

Every agent runs a pinned model at a pinned effort, so a new default cannot move a
result:

- Claude Code: `--model claude-sonnet-5-5 --effort medium` by default (`--model`,
  `--effort`). Medium is Sonnet 5.5's own default. The `claude -p` that forge starts gets
  the same `--effort` from the harness's shim when forge passes none.
- Codex: `--codex-model` (required) and `-c model_reasoning_effort=` from `--codex-effort`
  (default `medium`).

Each record's `run_meta` carries `agent`, `agent_version`, `model`, `effort` and the
commits, plus `model_reported` (what the agent said it ran: Claude Code's init message,
or the `modelUsage` models of forge's calls; Codex reports none) and, for Codex, `provider`.
The forge arm's `effort` is what its `claude -p` calls were launched with. The report says
"mixed" when the records of one arm disagree.

## Isolation

Every agent arm runs apart from your own setup and data. The Codex arms get the same
rules in Codex's own terms:

| Rule | Claude Code arms | Codex arms |
|---|---|---|
| Workspace | a fresh directory in the system temp dir, outside every repository (Claude Code loads each `CLAUDE.md` on the way up), and a git repository of its own | the same (Codex loads `AGENTS.md` up to that repository root, so none from above it) |
| Your setup | `--setting-sources=project,local`: no user CLAUDE.md, skills, plugins or hooks | a fresh `CODEX_HOME` and `HOME` per run: no user config, `AGENTS.md`, skills (Codex reads `$HOME/.agents/skills` too), rules or memories |
| MCP and extras | `--strict-mcp-config`; auto memory off; `--no-session-persistence` | no MCP servers configured; bundled skills, web search, history, memories, apps, plugins, browser and computer use and subagents off; `--ephemeral` |
| Environment | allowlisted (`ENV_ALLOWLIST`): no API keys, cloud credentials or tokens; cloud SDK config paths point at nothing | the same; the API key goes into the run's `CODEX_HOME` through `codex login --with-api-key` and is deleted when the run ends |
| Tools | `--permission-mode dontAsk` with `ALLOWED_TOOLS`: reads and edits in the workspace, a few read-only shell commands, only the `fluid` stages the scenarios need | a permissions profile: writes only in the workspace and the run's `home` and `tmp`; reads only those, the `fluid_build` package (never the checkout: its git-ignored files and the reference solutions), the interpreter and its venv, and Codex's `:minimal` platform set; every run's `codex-home` and the packaged skills denied; no network. execpolicy rules forbid `fluid apply`, `publish`, `scaffold-ide`, `schedule-sync` and `policy-apply` |

Codex passed a `*KEY*` variable through to its commands despite
`shell_environment_policy.exclude` (0.154.0). That is why the key is stored in
`CODEX_HOME` and kept out of every environment. Only an API key is supported: copying a
ChatGPT login would let a token refresh in the copy invalidate yours. Codex has no tool
allowlist, so its agent may run any other command; the profile keeps it inside the
workspace, offline. `fluid` runs from this checkout's interpreter with its site
directories added by a launcher, because Codex's macOS sandbox refused to exec a venv's
`bin/python` symlink.

The shared temp dirs (`/tmp`, `/var/tmp`) are denied to a Codex arm unless they hold its
run. Keep `TMPDIR` out of `/tmp` for Codex runs (the macOS default, `/var/folders`, is
fine): a work root under `/tmp` leaves the rest of `/tmp` readable to the agent.

The scenarios are trusted input: an agent still runs as you, so do not run a scenario
from someone you would not let use your shell.

`--bare` is not used: it never reads the subscription login and does not run hooks.

## Add a scenario

A directory under `scenarios/` with:

- `prompt.md`: the request, one paragraph, naming the path to write;
- `scenario.yaml`: `title`, `contracts` (paths the arm must write), `checks`, and
  optionally `arms` (conditions: `no-skill`, `skill`, `forge`; `skill` runs both skill
  arms), `forge` (`target_dir`, extra `args`) and `should_trigger: false` for a negative
  control (no contracts expected; runs `no-skill` and `skill`);
- `context/`: files copied into the workspace first;
- `reference/`: a correct solution. `test_harness.py` grades it in the default test
  lane and fails unless it is valid with every check passing.

The check kinds are documented on `run_check` in `harness.py`. Three are worth knowing:

- `unique` / `not_null` pass only for a form that really asserts the property: a `dq`
  rule needing a 100% ratio, an ODCS `duplicateValues` / `nullValues` metric capped at
  zero, or `required: true`. A loose threshold or inline SQL does not count.
- `reads_upstream: {productId, exposeId, file}` passes when the contract names `file`
  (the `parameters.inputs` path) or when `consumes[]` holds the pair and a build's SQL or
  properties mention the `exposeId`. Use it instead of `contains` for an upstream product.
- `in_file` with `matches` / `not_matches` checks a workspace file that is not a
  contract against a regex, case-insensitively; the negative controls use it.

Adapted from dbt-labs/dbt-agent-skills `evals/` (Apache-2.0); the deterministic grader
replaces its LLM grader. Repeated runs, the full-pass rate, `should_trigger`, the
`codex exec` flags and the per-run CLI version, model and effort follow
tardigrde/agent-skill-eval v0.7.0 (MIT), adapted rather than depended on. Detecting a
fired skill is this harness's own: agent-skill-eval's `should_trigger` only inverts
side-effect checks.
