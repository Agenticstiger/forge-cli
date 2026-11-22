---
name: publishing-to-data-mesh-manager
description: Publishes FLUID data products to Data Mesh Manager (Entropy Data) as one batch in dependency order, as drafts first, promotes the drafts whose fluid verify report is clean, and reports where a migration's products stand. Use when asked to publish, register or catalogue data products in Data Mesh Manager or Entropy Data, to move drafts to active, to check how far a migration has got, or when fluid datamesh-manager fails with a dmm_batch_ or dmm_promote_ error.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge) and an Entropy Data API key in DMM_API_KEY.
---

# Publishing to Data Mesh Manager

`fluid datamesh-manager` (or `fluid dmm`) writes to a catalog other teams read. Read before you
write, publish only what the user asked for, and publish drafts first.

`fluid describe --commands` prints every command and flag as JSON. Check a command
or flag there before you use one this skill does not show.

## Where the products stand

`status` is read-only: it reads the product list once and sends nothing else.

```bash
fluid datamesh-manager status products/
```

Each product the contracts declare is `active`, `draft`, `deprecated`, `retired`, `unknown`
(listed with no status) or `missing`, and the summary says `active N of M`. A contract that
cannot be read, or declares no id, is a row of its own (`unreadable`, `unnamed`).
`--format json` prints the rows and the summary. `--strict` exits 1 unless every product is
active: a migration is finished only when they all are.

## Publish a batch as drafts

```bash
fluid datamesh-manager publish products/ --status draft --dry-run
fluid datamesh-manager publish products/ --status draft
```

- A folder (searched for `*.fluid.yaml` at any depth), a glob or several contracts publish as
  one batch, each product after the products it consumes (`consumes[].productId`). One file
  named on its own is a single publish.
- The dry run reads each product's status and says what the real run would skip or keep.
  Show it to the user before the real run.
- A product that fails blocks only the products that consume it. The others go on, and the
  command exits 1. Fix the failure and run the same command again: it resumes.
- A status only moves forward. `draft` never demotes a product that is active, a deprecated
  product keeps its status, and a retired one is skipped in a batch (an error for one file).
  A product whose status cannot be read is an error, never a guess.
- `--format json` prints one report for the batch.

## Promote what passed verification

```bash
fluid verify products/orders/contract.fluid.yaml --strict --require-every-port --out reports/orders.json
fluid datamesh-manager promote products/ --verify-report reports/ --dry-run
fluid datamesh-manager promote products/ --verify-report reports/
```

`--verify-report` takes `fluid verify --out` reports, or a folder of them. It promotes a draft
only when its newest report is clean: no error or mismatch, at least one port checked, every
output port the contract declares covered, none left unverified under `--require-every-port`,
and no drift or failed refresh. The other products stay drafts and the command exits 1.
`--id <product-id>` names a product without its contract.

## Ask the user first

- Before every publish or promote that is not a dry run, and before `--status active`, which
  skips the draft review.
- Before `--auto-approve-access`: it approves the access agreements generated from
  `consumes[]`, which otherwise wait as pending.
- Before `delete`, `wipe` or `delete-contract`: they remove products or contracts from the
  catalog.

Keep the key in `DMM_API_KEY` (and a self-hosted URL in `DMM_API_URL`). A key passed with
`--api-key` lands in the shell history and the process list.

## When a command fails

| Event | What it means | Do |
|---|---|---|
| `dmm_batch_no_contracts` | the folder holds no `*.fluid.yaml`, `*.fluid.yml` or `*.fluid.json` | check the path, or name the files or a glob |
| `dmm_batch_duplicate_product` | two files declare one product id | give each contract its own id, or leave the copy out |
| `dmm_batch_dependency_cycle` | products consume each other in a loop | remove one `consumes[]` entry of the loop |
| `dmm_verify_report_unreadable` | a `--verify-report` file is not a verify report | pass a `fluid verify --out` file, or the folder that holds them |
| `dmm_promote_nothing_named` | no contract and no `--id` was given | name the contracts or the product ids |
| `dmm_promote_contract_without_id` | a contract declares no top-level `id` | add the id, then run `fluid validate` on the contract |

Nothing was sent when a `dmm_batch_` error stops the batch: the contracts are checked before
the first request.
