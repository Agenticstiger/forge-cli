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

The contract each arm leaves behind is graded without an LLM:

- **valid**: `fluid validate --offline` exits 0 for every contract the scenario expects;
- **fidelity**: the share of the scenario's checks that pass (type and layer, bindings,
  columns and types, quality rules, schedule, `consumes[]`, what a refine must keep),
  plus two that always run: no invented email, no leftover starter values;
- **pass**: valid and every check passes.

The report gives valid@1, pass@1 and mean fidelity per arm, with tokens, cost, turns
and wall time taken from Claude Code's own JSON (`modelUsage`). For the `skill` arm it
also counts the runs that invoked a skill.

## Run it

It spends real model usage, so it is opt-in. One run per scenario and arm (31 runs over
the 11 scenarios) costs a few dollars of Sonnet usage.

```bash
claude auth status            # must exit 0
FLUID_AGENT_EVAL=1 python -m pytest -m agent_eval tests/agent_evals -s
```

Or drive the harness directly, which also narrows the run:

```bash
python tests/agent_evals/harness.py run --scenarios sdp-local-orders --arms no-skill,skill
python tests/agent_evals/harness.py run --forge-src ../forge-cli-other-branch
python tests/agent_evals/harness.py grade <workspace> <scenario-id>
python tests/agent_evals/harness.py report tests/agent_evals/runs/<timestamp>
python tests/agent_evals/harness.py regrade tests/agent_evals/runs/<timestamp>
```

`regrade` grades every kept workspace of a run again against the current scenarios and
grader (agent arms with their recorded commands, the forge arm without), then rewrites
`results.json` and `report.md`. The rest of each record stays; `meta` gains `regraded_at`
and `grader_commit`. A run whose workspace is gone keeps its old grade and is listed.

A run that timed out, whose agent errored or exited non-zero, or that hit a harness error
is a failed run: it counts in every denominator (valid@1, pass@1 and fidelity all divide by
every run of the arm), is never valid or passed, and adds 0 to fidelity. Its grade stays in
the record for diagnosis. The report has an Errors column and lists the reason. A timeout
kills the agent's whole process tree. Each record carries, under `run_meta`, the model alias,
commits and Claude Code version it ran with; the report header says "mixed" when a merged run
used more than one.

`report.md` and `results.json` land in `tests/agent_evals/runs/<timestamp>/` (git-ignored).
Each run's workspace, transcript and stderr stay in the temp dir
(`<tmp>/fluid-agent-evals-<timestamp>-XXXX/<scenario>/<arm>/`, a fresh `mkdtemp`;
`results.json` records the path).

## Isolation

Every agent arm runs apart from your own setup and data:

- the workspace is a fresh directory in the system temp dir, outside every repository:
  Claude Code loads each `CLAUDE.md` on the way up from its working directory;
- `--setting-sources=project,local`: no user CLAUDE.md, skills, plugins or hooks;
- `--strict-mcp-config`: no MCP servers; `--no-session-persistence`;
- auto memory off; an allowlisted environment (`ENV_ALLOWLIST` in `harness.py`): no API
  keys, cloud credentials or tokens, and the cloud SDKs' config paths point at nothing, so
  the stored subscription login is used and `fluid` cannot reach a real account;
- `--permission-mode dontAsk` with `ALLOWED_TOOLS`: reads and edits inside the workspace
  only, a few read-only shell commands, and only the `fluid` stages the scenarios need
  (no `apply`, `publish`, `scaffold-ide` or AI-mode `forge`).

The scenarios are trusted input: an agent still runs as you, so do not run a scenario
from someone you would not let use your shell.

`--bare` is not used: it never reads the subscription login and does not run hooks.

## Add a scenario

A directory under `scenarios/` with:

- `prompt.md`: the request, one paragraph, naming the path to write;
- `scenario.yaml`: `title`, `contracts` (paths the arm must write), `checks`, and
  optionally `arms` and `forge` (`target_dir`, extra `args`);
- `context/`: files copied into the workspace first;
- `reference/`: a correct solution. `test_harness.py` grades it in the default test
  lane and fails unless it is valid with every check passing.

The check kinds are documented on `run_check` in `harness.py`. Two are worth knowing:

- `unique` / `not_null` pass only for a form that really asserts the property: a `dq`
  rule needing a 100% ratio, an ODCS `duplicateValues` / `nullValues` metric capped at
  zero, or `required: true`. A loose threshold or inline SQL does not count.
- `reads_upstream: {productId, exposeId, file}` passes when the contract names `file`
  (the `parameters.inputs` path) or when `consumes[]` holds the pair and a build's SQL or
  properties mention the `exposeId`. Use it instead of `contains` for an upstream product.

Adapted from dbt-labs/dbt-agent-skills `evals/` (Apache-2.0); the deterministic grader
replaces its LLM grader.
