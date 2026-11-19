---
name: deploying-fluid-contracts-safely
description: Runs the fluid delivery pipeline (validate, bundle, generate artifacts, plan, apply, verify) in the safe order, stops for a human to review the plan, and refuses the flags that skip its safety gates. Use when asked to plan, deploy, apply, ship, release, roll out or publish a FLUID contract, or when apply asks for --allow-data-loss or fails a plan-binding, plan-mode or digest check.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge).
---

# Deploying FLUID contracts safely

`fluid apply` changes real tables and files. It must only run a plan a person has read.

## Prepare the plan

Run these from the contract's directory and stop at the first failure:

```bash
fluid validate contract.fluid.yaml --offline
fluid bundle contract.fluid.yaml --format tgz --out runtime/bundle.tgz
fluid validate runtime/bundle.tgz
fluid generate artifacts runtime/bundle.tgz --out dist/artifacts
fluid validate-artifacts dist/artifacts
fluid plan runtime/bundle.tgz --env dev --mode amend --out runtime/plan.json --html
```

Then stop. Summarise the plan (`runtime/plan.json`, or `plan.html` for a person) and ask
the user to approve this plan, in this environment and mode, before you apply it.

## Apply the reviewed plan

```bash
fluid apply runtime/plan.json --bundle runtime/bundle.tgz --env dev --mode amend
fluid verify contract.fluid.yaml --strict --env dev
```

- The plan is bound to its bundle and its mode: apply the same `runtime/plan.json`,
  with the same `--bundle`, `--env` and `--mode`. A changed contract needs a new bundle
  and a new plan.
- For a rehearsal, plan with `--mode dry-run` and apply with `--mode dry-run`.
- Start in `dev`. Apply to another environment only when the user names it.

## Modes

| Mode | Existing data |
|---|---|
| `dry-run` | untouched; renders only |
| `create-only` | untouched; fails if the object exists |
| `amend` (default) | kept; new columns are added |
| `amend-and-build` | kept; transforms run again |
| `replace`, `replace-and-build` | dropped and rebuilt |
| `reconcile` | untouched; restarts Spark streams that died, deploys nothing |

## Never without the user's explicit yes for this run

Ask, in the chat, before any of these, and say what each one risks:

- `--allow-data-loss`, or a `replace` or `replace-and-build` mode: they drop data.
- `--no-verify-plan-binding` or `--no-verify-federation`: they skip the checks that
  the plan, its bundle and its upstreams are the ones that were reviewed.
- `--adopt-shared-container`: it takes ownership of a shared container.
- `--yes` on apply, or an environment other than `dev`.

## When a gate fails

| Event | What it means | Do |
|---|---|---|
| `apply_plan_mode_mismatch` | the plan was made for another mode | plan again with the mode you will apply |
| `apply_plan_digest_bundle_mismatch` | the bundle changed after the plan | bundle and plan again; never bypass |
| `apply_plan_digest_packet_mismatch` | the bundle holds the planned contract in other bytes (rebuilt) | apply the bundle the plan was made from, or plan again |
| `apply_plan_digest_packet_missing` | the plan records no packet digest (an older fluid made it) | plan again on the bundle |
| `apply_plan_digest_plan_tamper` | the plan file was edited | plan again; never edit `plan.json` |
| `apply_plan_digest_tofu_plan_target_mismatch` | the credentials or region reach another account, project or region than the plan's | use the plan's credentials and region, or plan again for this target |
| `apply_plan_digest_tofu_plan_toolchain` | another OpenTofu or provider version than the plan's | apply with the plan's versions, or plan again here |
| `apply_plan_digest_tofu_plan_target_unverified` | this run cannot read a target field the plan bound (no credentials or SDK) | give this run the plan's credentials and SDK; ask the user before `--no-verify-plan-binding` |
| `apply_plan_digest_tofu_plan_target_missing` / `…_lock_missing` / `…_lock_mismatch` | the plan has no target record, or its lock file was not handed over or was changed | plan again, or hand over every file `fluid plan --tofu` wrote, unchanged |
| `apply_plan_digest_tofu_plan_target` | another state backend than the plan's | apply with the plan's `--state-backend` |
| `apply_plan_digest_tofu_plan_state_missing` | the plan was made on state this runner does not have (local state) | apply where it was planned, or use a remote state backend |
| data-loss refusal | the change would drop data | stop and ask the user |

A `…target_field_unbound` or `…target_provider_unbound` warning is not a failure: the plan is
not bound to that account, project, region or host. Tell the user which one before you apply.
