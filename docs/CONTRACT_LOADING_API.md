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

loaded.contract   # dict, equal to plan.json["contract"] for `fluid plan ... --env prod`
loaded.digest     # "sha256:…", the plan digest's canonicalisation of that dict
loaded.files      # (contract.fluid.yaml, parts/orders.yaml, overlays/prod.yaml)
loaded.overlay    # Path(".../overlays/prod.yaml")
```

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
`load_contract(that_file, env=...)`.

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
the file. Keep `$ref` out of overlays, and use file references rather than
`#/...` pointers in a contract that has overlays.

## Reference

### `load_contract(path, *, env=None, logger=None) -> LoadedContract`

Loads a contract file or a bundle through `fluid plan`'s own loader. `path` is
resolved to an absolute path first, as `fluid plan` does. The CLI's gate on
operator-typed paths (no `..`, no symlink) is not applied; a library caller
chooses its own paths.

### `load_contract_from_text(text, *, suffix=".yaml", base_dir=None, overlay=None) -> LoadedContract`

Parses `text` with the engine's parser (`suffix` picks it as a file extension
would), then loads the result as `load_contract_from_dict` does.

### `load_contract_from_dict(document, *, base_dir=None, overlay=None) -> LoadedContract`

Loads a parsed document. `document` and `overlay` are never modified.

### `LoadedContract`

A frozen dataclass.

| Field | Type | Meaning |
|---|---|---|
| `contract` | `dict` | The contract as planned. A fresh dict each call; yours to mutate. |
| `origin` | `"file"` \| `"bundle"` \| `"memory"` | Which entry point and input shape produced it. |
| `source` | `Path \| None` | The resolved contract or bundle path. |
| `env` | `str \| None` | The env requested. |
| `overlay` | `Path \| None` | The overlay file merged for `env` (never set for a bundle; see the known engine behaviour above). |
| `files` | `tuple[Path, ...]` | Every file composed: the source, each `$ref` target in first-read order, the overlay. |
| `unresolved_refs` | `tuple[str, ...]` | `$ref` values left in `contract`, in document order. |
| `digest` | `str` (property) | `sha256:<hex>` via `compute_contract_digest`, the plan digest's canonicalisation. |

### `ContractLoadError`

Every failure raises `ContractLoadError` with a stable `event`, the path when
there is one, and the engine's exception as `__cause__`:

| `event` | When |
|---|---|
| `contract_not_found` | The contract file does not exist. |
| `contract_parse_failed` | The text is not valid JSON/YAML, or trips the YAML size/anchor guard. |
| `contract_not_a_mapping` | The document (or overlay) root is not an object. |
| `contract_ref_unresolved` | A `$ref` target is missing, cyclic, blocked, or its pointer does not resolve. |
| `contract_load_failed` | Any other loader failure. |
| *engine event* | Passed through unchanged, e.g. `overlay_declared_but_missing`, `bundle_env_mismatch`, `bundle_manifest_invalid`. |

## Stability

`fluid_build.api` is governed by SemVer through `fluid_build.api.__api_version__`
(`tests/api/test_api_surface_snapshot.py` locks the exported names). The
behavioural promise is the equation above: `load_contract(path, env=env).contract`
equals `plan.json["contract"]` from `fluid plan path --env env`.
`tests/api/test_contract_load.py` runs the real `fluid plan` on fixtures that
exercise every rewrite (alias values, legacy `build:`, `$ref`, overlay, bundle)
and fails if the two differ, and pins the in-memory forms to the file form.
A new rewrite in the engine's loader therefore reaches this API in the same
release, or the build is red.

Do not import the helpers in `fluid_build._contract_loader` (for example
`_normalize_contract_aliases` / `_normalize_singular_build_key`) to reproduce
this: they are private, their order matters (see above), and they are not
the whole pipeline.
