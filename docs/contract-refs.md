# Composing a contract from fragments with `$ref`

A contract can pull any object from another YAML or JSON file with `$ref`.
`fluid validate`, `plan`, `apply` and `bundle` resolve every ref before they
do anything else, so the rest of the pipeline sees one document.

```text
orders/
├── contract.fluid.yaml
├── owner.yaml
└── fragments/
    ├── builds/ingest.yaml
    └── policy.yaml
```

```yaml
# orders/contract.fluid.yaml
id: sales.orders_v1
metadata:
  owner:
    $ref: ./owner.yaml                     # the whole file
builds:
  - $ref: fragments/builds/ingest.yaml     # refs work inside lists
exposes:
  - exposeId: orders
    policy:
      $ref: fragments/policy.yaml#/gold    # file + JSON pointer
```

```bash
fluid validate orders/contract.fluid.yaml   # refs resolved transparently
fluid bundle   orders/contract.fluid.yaml   # print the single resolved document
```

A ref is resolved relative to the file that contains it, so
`fragments/builds/ingest.yaml` may itself say `$ref: ../policy.yaml`.

A `$ref` node is an object whose only key is `$ref`. Same-document refs
(`$ref: "#/definitions/x"`) are left in place as written.

---

## Where a ref may point: the ref root

Every ref must name a file inside the **ref root**. By default the ref root is
the directory that holds the root contract file (`orders/` above), and it
applies to every ref, including refs inside fragments. A fragment in
`orders/fragments/` can reach anything under `orders/` and nothing outside it.

The check runs after `..` segments and symlinks are resolved, so neither can
be used to get out:

| `$ref` written in `orders/contract.fluid.yaml` | Result |
|---|---|
| `./owner.yaml`, `fragments/policy.yaml#/gold` | resolved |
| `../shared/policy.yaml` | refused: escapes the ref root |
| `./link.yaml` where `link.yaml` is a symlink to `/home/me/x.yaml` | refused: escapes the ref root |
| `/etc/hosts`, `C:\x.yaml`, `\\server\share\x.yaml` | refused: must be a relative path |
| `file:///…`, `https://…`, `s3://…`, `//host/…` | refused: remote refs are not supported |

A refused ref fails the command with a typed error that names the ref, the
[JSON pointer](https://www.rfc-editor.org/rfc/rfc6901) of the `$ref` node, and
the file it was written in:

```text
❌ Validation error: contract_load_failed
   error: $ref '../shared/policy.yaml' at JSON pointer '/exposes/0/policy' in
   /work/orders/contract.fluid.yaml escapes the ref root /work/orders (after
   resolving '..' and symlinks); refs may only name files inside it. …
```

`fluid validate` exits 1 and `fluid bundle` exits 2. The check runs before the
target is opened, so the error is the same whether or not the target exists.

**Why.** Contracts are often untrusted input: a platform such as the FLUID
Command Center runs `fluid validate` and `fluid bundle` on contracts its users
upload. Without the root, a contract could compose any YAML or JSON file the
process can read into itself, and `fluid bundle` would print it back.

---

## Widening the root (monorepos)

To share fragments between products, set the ref root to a directory that
contains both the contracts and the shared fragments:

```text
repo/
├── shared/policy.yaml
└── products/orders/contract.fluid.yaml   # $ref: ../../shared/policy.yaml
```

```bash
FLUID_REF_ROOT=repo fluid validate repo/products/orders/contract.fluid.yaml
```

From Python, pass `ref_root=` to the loader:

```python
from fluid_build.loader import load_contract, RefConfinementError

contract = load_contract("repo/products/orders/contract.fluid.yaml", ref_root="repo")
```

`load_contract`, `load_with_overlay` and `compile_contract` all accept
`ref_root`. The argument wins over `FLUID_REF_ROOT`.

Rules for the wider root:

- It must be an existing directory that contains the root contract. If it
  does not, the command fails and names the setting it came from.
- A blank `FLUID_REF_ROOT` counts as unset: the default root applies.
- It is only consulted when the contract has a ref to another file, so a
  `FLUID_REF_ROOT` left in your shell does not affect other contracts.
- It widens the root and nothing else. URLs and absolute paths are still
  refused, and refs that resolve into system directories (`/etc`, `/proc`,
  `/private/etc` on macOS, …) are still blocked.

Set it to the narrowest directory that works. Setting it to `/` turns the
confinement off.

---

## Catching the error in Python

`RefConfinementError` is a subclass of `RefResolutionError`, so existing
`except RefResolutionError` handlers catch it. It carries the details as
attributes:

```python
from fluid_build.loader import RefConfinementError, load_contract

try:
    load_contract(path)
except RefConfinementError as err:
    print(err.ref)      # '../shared/policy.yaml'
    print(err.pointer)  # '/exposes/0/policy'
    print(err.source)   # file that contains the ref
    print(err.root)     # the ref root it escaped
```

---

## OpenAPI fragments inside a bundle

When `fluid validate <bundle>.tgz` checks the OpenAPI documents extracted into
`sources/openapi/`, the same check applies with **no** root, because a bundled
fragment has no directory. Only same-document refs
(`$ref: "#/components/schemas/Order"`) are allowed. Any other `$ref` is
reported as an `OAS-REF-EXTERNAL` error, and openapi-spec-validator is not
run on that fragment. If it were run, it would follow `file://` and
`http(s)://` refs. Inline the referenced schemas under `components` instead.
