# Loading a contract as `fluid plan` sees it

`fluid_build.api.load_contract` returns the contract that `fluid plan` plans:
the same dict `plan.json` embeds as `contract`, after every rewrite the engine
makes on the way in. Use it when your code has to agree with the engine about
what a contract *is*: comparing a contract to the plan made from it, diffing
two revisions, rendering a contract in a UI, or checking it in CI.

Added in `fluid_build.api` **1.1**.

## Examples

### Load a contract file

```python
from fluid_build.api import load_contract

loaded = load_contract("contracts/orders/contract.fluid.yaml", env="prod")

loaded.contract   # dict, the contract `fluid plan ... --env prod` plans
loaded.digest     # "sha256:…", the plan digest's canonicalisation of that dict
loaded.files      # (contract.fluid.yaml, parts/orders.yaml, overlays/prod.yaml)
loaded.overlay    # Path(".../overlays/prod.yaml")
```

`env` is an environment *name*, never a path. The engine turns `env` into
overlay paths (`overlays/<env>.yaml`, `<env>.json`, …), so an env holding `..`
or an absolute path would merge whichever `.yaml`/`.yml`/`.json` file it named
into the returned contract. An env that is not a single path component is
refused before a file is read:

```python
load_contract("contracts/orders/contract.fluid.yaml", env="../../home/me/.docker/config")
# ContractLoadError: contract_env_invalid
```

Refused: `""` (not read as `None`), `.`, `..`, anything holding `/`, `\` or a
NUL byte (so any absolute path), and a drive-qualified name (`C:prod`). The rule
is the same on every platform: `C:prod` is refused on Linux and macOS too, so
one env never means two things. Every other string loads exactly as
`fluid plan --env` loads it, including names `fluid publish --env` would not
accept (`_staging`, `prod+eu`, a name longer than 64 characters). Pass `None`
for no env.

A `fluid bundle` archive loads the same way, and is refused for an env it was
not built for, as on the CLI:

```python
load_contract("runtime/bundle.tgz", env="prod")
```

### Is this the contract that plan was made from?

```python
import json

from fluid_build.api import load_contract_from_text
from fluid_build.forge.core.plan_digest import compute_contract_digest

plan = json.loads(open("runtime/plan.json").read())
submitted = load_contract_from_text(contract_yaml)  # text from a form, a DB row, a PR

if submitted.unresolved_refs:
    raise ValueError(f"contract references files: {submitted.unresolved_refs}")
same = submitted.digest == compute_contract_digest(plan["contract"])
```

Formatting, comments, key order, quoting, Unicode normal form, an alias beside
its canonical value (`format: bigquery-table` / `bigquery_table`) and a legacy
`build:` beside `builds:` do not change the digest. Every other value does.

Compare digests rather than dicts. `loaded.contract` keeps a YAML magic-word
or numeric key (`on:`, `no:`, `1:` in an open block such as `extensions`) as
the Python `bool` / `int` the engine plans with, while `plan.json` writes every
key as a string. The digest coerces keys the same way `plan.json` does.

`loaded.digest` is the digest of the **planned** contract: normalised,
composed and overlaid. It is not the value `fluid contract digest` prints, and
not what a federation `upstreamDigest` pins: those hash the file as parsed,
before any alias rewrite, `$ref` or overlay. The two differ whenever the file
uses one of those, so do not pin `upstreamDigest` from `loaded.digest`.

### Contract text or a parsed dict, without touching the disk

```python
from fluid_build.api import load_contract_from_dict, load_contract_from_text

loaded = load_contract_from_text(text)                    # YAML by default
loaded = load_contract_from_text(text, suffix=".json")    # or JSON
loaded = load_contract_from_dict(document)                # already parsed
```

Neither reads a file. A `$ref` is left in place and listed in
`loaded.unresolved_refs`; you decide whether that is an error.

### Text with its fragments and an overlay

```python
loaded = load_contract_from_text(
    text,
    base_dir="contracts/orders",              # resolve $ref against this directory
    overlay={"exposes": [{"binding": {"location": {"database": "prod_s"}}}]},
)
```

With `base_dir` set to a contract's directory and `overlay` set to the parsed
overlay file `--env` would select, the result equals
`load_contract(that_file, env=...)`, including the case below where the engine
drops the overlay.

Without `base_dir`, a document holding file `$ref` values cannot say whether
the engine would apply an overlay (that depends on what the fragments hold),
so passing `overlay` then raises `contract_overlay_needs_base_dir` instead of
guessing.

## What "as plan sees it" means

In the engine's order:

1. **Parse**: JSON, or YAML through the engine's billion-laughs guard.
2. **`$ref` composition**: each `{"$ref": "./file.yaml#/pointer"}` replaced by
   its target, resolved against the contract's directory (or `base_dir`) by the
   engine's resolver and under its path rules. Same-document `#/...` pointers
   are kept, as the engine keeps them, and listed in `unresolved_refs`.
3. **Overlay**: for `env`, the first of `overlays/<env>.yaml|yml|json`,
   `<env>.yaml|yml|json`, `<contract-stem>.<env>.yaml|yml|json` next to the
   contract, deep-merged over the base (objects key by key, lists of objects by
   position, anything else replaced). A bundle is never re-overlaid.
4. **Alias values**: human-friendly values rewritten to the schema's enum
   value, for example `source.kind: pg` → `postgres`, `source.mode: incremental`
   → `incremental_append`, `binding.format: kafka` → `kafka_topic`,
   `iceberg-table` → `iceberg`.
5. **Legacy `build:`**: a singular `build:` becomes `builds: [build]`; when
   both are present `builds:` wins.

Step 4 runs before step 5, exactly as in the engine, so an alias under a
legacy singular `build:` is **not** rewritten (and `fluid plan` then rejects
it at the schema gate). Write `builds:` to get alias rewriting for builds.

Validation is not part of loading: a schema-invalid contract loads, and
`fluid validate` / `fluid plan` reject it.

### Known engine behaviour: an overlay next to a `$ref`

When the overlay file, or the merged contract, still holds a `$ref` (an
overlay that references a fragment, or a same-document `#/...` pointer in
the contract), the engine's auto-bundle step reloads the contract from the
base file and the overlay is dropped: `fluid plan --env prod` plans the base
contract. `load_contract` returns what plan plans, so it returns the base too,
but its provenance does not pretend otherwise: `overlay` is `None`, the
overlay is not in `files`, and a `contract_overlay_not_applied` WARNING names
the file. The in-memory forms do the same with an `overlay` mapping: they
return the base and log the same WARNING (to `logger`, or the
`fluid.api.contract` logger). Keep `$ref` out of overlays, and use file
references rather than `#/...` pointers in a contract that has overlays.

## Reference

### `load_contract(path, *, env=None, logger=None) -> LoadedContract`

Loads a contract file or a bundle through `fluid plan`'s own loader. `path` is
resolved to an absolute path first, as `fluid plan` does. The CLI's gate on
operator-typed paths (no `..`, no symlink) is not applied to `path`; a library
caller chooses its own paths. `env` must be a single path component (see the
example above) or `None`; anything else raises `contract_env_invalid`.

### `load_contract_from_text(text, *, suffix=".yaml", base_dir=None, overlay=None, logger=None) -> LoadedContract`

Parses `text` with the engine's parser (`suffix` picks it as a file extension
would), then loads the result as `load_contract_from_dict` does.

### `load_contract_from_dict(document, *, base_dir=None, overlay=None, logger=None) -> LoadedContract`

Loads a parsed document. `document` and `overlay` are never modified. `logger`
receives the `contract_overlay_not_applied` WARNING.

### `LoadedContract`

A frozen dataclass.

| Field | Type | Meaning |
|---|---|---|
| `contract` | `dict` | The contract as planned, keys as the engine holds them (see the digest note above). A fresh dict each call; yours to mutate. |
| `origin` | `"file"` \| `"bundle"` \| `"memory"` | Which entry point and input shape produced it. |
| `source` | `Path \| None` | The resolved contract or bundle path. |
| `env` | `str \| None` | The env requested. |
| `overlay` | `Path \| None` | The overlay file merged for `env` (never set for a bundle; see the known engine behaviour above). |
| `files` | `tuple[Path, ...]` | Every file composed: the source, each `$ref` target in first-read order, the overlay. |
| `unresolved_refs` | `tuple[str, ...]` | `$ref` values left in `contract`, in document order. |
| `digest` | `str` (property) | `sha256:<hex>` of the planned contract via `compute_contract_digest`, the plan digest's canonicalisation. Not the `fluid contract digest` / `upstreamDigest` value. Raises `contract_not_serialisable` when JSON cannot represent the contract. |

### `ContractLoadError`

Every failure raises `ContractLoadError` with a stable `event`, the path when
there is one, and the engine's exception as `__cause__`:

| `event` | When |
|---|---|
| `contract_not_found` | The contract file does not exist, or `path` / `base_dir` cannot name a file (it holds a NUL byte). |
| `contract_parse_failed` | The text is not valid JSON/YAML, is not UTF-8, or trips the YAML size/anchor guard. |
| `contract_not_a_mapping` | The document (or overlay) root is not an object, from a file (with or without `env`, an overlay or not) or from text. |
| `contract_ref_unresolved` | A `$ref` target is missing, cyclic, blocked, or its pointer does not resolve. |
| `contract_env_invalid` | `env` is not a single path component: it is empty, `.` or `..`, holds `/`, `\` or NUL, or is drive-qualified (`C:prod`, on every platform). |
| `contract_overlay_needs_base_dir` | In-memory form: `overlay` given for a document with file `$ref` values but no `base_dir`. |
| `contract_not_serialisable` | Raised by `.digest`: the contract holds a value JSON cannot represent (an unquoted YAML date, a set, binary, a self-referencing alias). `fluid plan` cannot write it either; quote the value. |
| `contract_load_failed` | Any other loader failure, including a document that contains itself through a YAML alias (the engine fails on it too). |
| *engine event* | Passed through unchanged, e.g. `overlay_declared_but_missing`, `bundle_not_found` (a `.tgz` path that does not exist), `bundle_env_mismatch`, `bundle_manifest_invalid`. |

## Stability

`fluid_build.api` is governed by SemVer through `fluid_build.api.__api_version__`
(`tests/api/test_api_surface_snapshot.py` locks the exported names). The
behavioural promise: `load_contract(path, env=env).contract` is the contract
`fluid plan path --env env` plans, equal to `plan.json["contract"]` once keys
are written as strings (and always equal by `.digest`).

How each form keeps that promise:

- **The file form** (`load_contract`) calls the engine's loader itself, so a
  new step in the loader reaches it with no change here.
  `tests/api/test_contract_load.py` runs the real `fluid plan` on fixtures
  that exercise every rewrite (alias values, legacy `build:`, `$ref`, overlay,
  bundle, a magic-word key) and fails if the two differ.
- **The in-memory forms** have no file to hand the loader, so they replay a
  fixed sequence: `$ref` resolution, the overlay merge and the auto-bundle
  step's decision to drop it, then the loader's rewrites by name. The tests
  pin them to the file form on the same fixtures, and a guard test parses the
  engine loader's source (`load_contract_with_overlay`) and fails when it
  gains, loses or reorders a call that takes `contract` or a part of it
  (positionally or by keyword), changes `contract` any other way (an
  assignment that is not a known step, an item or attribute write, a method
  call, `del`, a rebinding), returns anything but `contract`, or reads
  `contract` anywhere else (an alias, a loop over it, a container holding
  it). It does not decide whether such a read changes the contract: it fails
  on it, so a new step there turns the build red until the in-memory forms
  replay it too. The guard reads that one function, outside its bundle
  branch: a change inside a function it calls (`load_with_overlay`, the `$ref`
  resolver, a rewrite itself) is caught only where the fixtures exercise it.

Do not import the helpers in `fluid_build._contract_loader` (for example
`_normalize_contract_aliases` / `_normalize_singular_build_key`) to reproduce
this: they are private, their order matters (see above), and they are not
the whole pipeline.
