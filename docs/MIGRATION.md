# Migration Notes

## v1.0 — Staged forge data-model pipeline

forge-cli v1.0 introduces the staged forge pipeline (`fluid forge
data-model`). The migration touches three on-disk locations.

### `~/.fluid/store/` is the new memory + cache root

Pre-v1.0 the copilot wrote a single `.fluid/copilot-memory.json`
file inside each workspace. v1.0 moves to a unified store at
`~/.fluid/store/` with namespaced subdirectories:

```
~/.fluid/store/
├── llm/
│   ├── logical/<cache_key>.json
│   ├── builder/<cache_key>.json
│   ├── transformation/<cache_key>.json
│   └── readme/<cache_key>.json
├── memory/
│   ├── project/<workspace_fingerprint>.json
│   ├── team/<team_slug>.json          (when shared)
│   ├── episodic/<iso_ts>.json         (decay-ranked timeline)
│   └── semantic/<entity_hash>.json    (vector-indexed)
├── discovery/<workspace_hash>.json
├── skills/<skill_hash>.json
└── history/<contract_hash>/<n>.json   (per-artefact versioning)
```

**No migration command is required.** The first time forge-cli reads
a workspace that has the legacy `.fluid/copilot-memory.json` file,
it logs a one-shot stderr notice and continues to read from the
legacy path until the user explicitly clears it. New writes always
land in `~/.fluid/store/memory/project/` from v1.0 onward.

The notice looks like:

```
[fluid] Reading legacy memory from /path/to/workspace/.fluid/copilot-memory.json;
        new writes go to ~/.fluid/store/memory/project/.
```

If you're running on a clean workstation with no legacy file the
notice never fires.

### Opt-in env-vars for the new behaviour

| Env var | Default | What it changes |
|---|---|---|
| `FLUID_COPILOT_PARALLEL_PHYSICAL` | `1` (on) | Set to `0` to force sequential builder/readme/transformation runs (debugging only). |
| `FLUID_COPILOT_SEMANTIC_MEMORY` | `0` (off) | Set to `1` (or `true`/`yes`/`on`) to write every successful forge to `memory/semantic`. Off by default for privacy on multi-tenant workstations. |
| `FLUID_QUIET` | unset | Set to `1` to suppress the v2-preview banner. Stacks with `FLUID_NONINTERACTIVE=1` and the `--quiet` / `-q` CLI flag. |
| `FLUID_NONINTERACTIVE` | unset | Same effect as `FLUID_QUIET=1` for CI orchestrators. |
| `FLUID_BANNER_TODAY` | unset | Override the banner's "today" check (test-only; accepts `YYYY-MM-DD`). |
| `FLUID_LLM_TEMPERATURE` | `0.0` | Override the per-stage sampling temperature (clamped to `[0, 2]`; garbage falls back to `0.0`). |
| `FLUID_LLM_PROVIDER` | from `~/.fluid/ai_config.json` | Pin the active provider for the run. |
| `FLUID_LLM_MODEL` | from saved config | Pin a specific model (forces single-model mode). |
| `FLUID_OLLAMA_MODEL` | unset | Required when `FLUID_LLM_PROVIDER=ollama`. |
| `FLUID_STORE_BACKEND` | `file` | One of `null`, `file`, `sqlite`, `postgres`, `vector`. |
| `FLUID_STORE_DSN` | unset | Required when `FLUID_STORE_BACKEND=postgres`. |
| `FLUID_STORE_MAX_MB` | unset | Optional size cap on `~/.fluid/store/`; LRU eviction on overflow. |
| `FLUID_SECRETS_FILE` | unset | Optional path to a launchpad-style `.env` file hydrated at CLI start. |

### CLI surface changes

* New: `fluid forge data-model {from-intent,from-ddl,validate,diff,dump-ddl}`
* New: `fluid memory {show,save,clear,search}`
* New: `fluid mcp serve` — MCP stdio server exposing the staged
  pipeline as tools for external agents.
* New: `fluid roadmap` — print the embedded roadmap (colorised on
  TTY, plain when piped).
* New `--quiet` / `-q` flag at every banner-emitting surface (`forge
  data-model *`, `generate speed-transformation`, `ai setup`, `ai
  status`).
* New `--tiered` opt-in for per-stage tiered model selection within
  the active provider.
* New `--deterministic` flag — temperature 0, seed 42, cache off,
  audit metadata on.

### `--quiet` parsed at every surface

In v0.7 `--quiet` was advertised by the banner text but only parsed
on `viz-graph`. v1.0 parses it on every banner-emitting CLI surface
(see the table in the README). The env-var paths
(`FLUID_QUIET=1`, `FLUID_NONINTERACTIVE=1`) keep working too.

### Coverage gate

A V1 coverage gate at ≥80% lands in CI for
`fluid_build/copilot/*` + `fluid_build/forge_datamodel/*` (excluding
deprecated/optional modules — see
`.github/workflows/ci.yml` for the full omit list). The pre-existing
70% core gate is unchanged.

### Banner auto-expiry

The v2-preview banner stops printing on **2026-05-07** regardless of
suppression env vars or flags. No code change required to remove it
— it's gated on `date.today()` against an embedded constant. After
the date the file stays in the codebase as a no-op so the
suppression test surface remains stable for v2 launch banners.

## Pre-v1.0 → v1.0 cheat sheet

| You used to do | You now do |
|---|---|
| Edit `.fluid/copilot-memory.json` by hand | `fluid memory show project; fluid memory save --scope project` |
| Run `fluid forge` and hope the contract was right | `fluid forge data-model from-intent … --review` (opens editor on the logical sidecar) |
| Re-run an entire forge to tweak SQL | Use the targeted repair loop — the validator's `field` hint maps to the single agent that gets re-run |
| Edit JSON sidecars and re-run downstream | `fluid forge data-model diff old.model.json new.model.json` |
| `export FLUID_QUIET=1` for non-interactive CI | Same env var still works, OR pass `--quiet` to any forge subcommand |

## Removing the legacy file safely

Once you're on v1.0 and have confirmed `~/.fluid/store/memory/project/`
holds your project memory, the legacy file is unused and can be
deleted:

```bash
rm .fluid/copilot-memory.json
```

The next forge run will skip the legacy-read path entirely.
