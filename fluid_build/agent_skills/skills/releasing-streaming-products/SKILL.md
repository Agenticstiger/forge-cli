---
name: releasing-streaming-products
description: Releases a change to a running Spark stream so it stops between batches, and traces each Iceberg commit to the release that wrote it. Use when asked to deploy, release or roll out a change to a streaming product or a kept-alive stream, to read a commit's stamps, to seal releases with fluid ship --release-dir, to find which release or run wrote a table's snapshot or was in force on a day (fluid runs status --release-dir), or when apply fails with apply_spark_artifact_mismatch.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge). The streams run on Spark and write Iceberg tables.
---

# Releasing streaming products

`fluid apply` replaces a stream whose config changed. It asks the running stream to stop
between two batches, so no batch is cut in half, and the new config resumes from the same
checkpoint.

`fluid describe --commands` prints every command and flag as JSON. Check a command
or flag there before you use one this skill does not show.

## Roll out a change

Plan and apply as the deploying-fluid-contracts-safely skill says, and stop for the user's
review. The plan's `stateImpact` says which running streams would stop between batches;
tell the user before the apply.

When the apply replaces a stream:

1. It writes a stop request at `stop/<key>` beside the build's job config
   (`_fluid/spark/<product>/<build>/`; on `runtime: local`, under `FLUID_SPARK_LOCAL_ROOT`).
2. The runner looks for it every 10 s, stops each query once no batch is running in it, and
   ends cleanly. The new config starts on the same checkpoint.
3. A run that has not stopped within `FLUID_SPARK_STOP_GRACE_SECONDS` (default 300, at most
   3600; `0` cancels at once) is cancelled. The request is removed whatever happens, Ctrl-C
   included.
4. The outcome names the config hashes the stream moved from and to, and how each old run
   stopped. Then the apply watches the new run (see Probation and rollback).

These runs are cancelled at once instead of asked:

- a run an older fluid started, with no stop key, so the first apply after a fluid upgrade
  cancels each running stream once;
- a run started on another job config (the output's location moved), which would never see
  the request;
- a second writer on the same checkpoint.

`--mode reconcile` only restarts streams that died. It never replaces a config, so it stops
nothing.

## Ask the user before you apply

- Moving `properties.stream.checkpoint`: the new run starts from `startingOffsets` unless
  the new location already holds a position. The outcome says which one happened.
- Copying a checkpoint from another stream: the runner refuses a checkpoint whose newest
  offsets name another topic, or partitions the topic no longer has, before it reads anything.
- `apply_spark_artifact_mismatch`: the Spark runs compiled now differ from the plan's (a
  plan made before a fluid upgrade, or with other Spark settings in the environment). Plan
  again, in the environment the apply runs in.

## Probation and rollback

After `fluid apply` starts a stream's new config over a previous one, it watches the new run for
`FLUID_SPARK_RELEASE_PROBATION_SECONDS` (default 300, at most 3600; `0` turns the watch off)
before it returns. It watches each changed stream in turn, so an apply of several changed streams
takes that much longer for each. Set `0` where a scheduler's timeout is shorter than the wait.

A run that fails in that time, or that its runtime retries, has failed its probation. The apply
cancels it, starts the previous job config again on its checkpoint, and fails with
`spark_release_rolled_back`. Its outcome names both runs, both configs and the cause. Tell the
user what it says the output now holds:

- On a shared checkpoint, the batches the failed run committed stay in the output, and the
  rolled-back run resumes after them.
- When the release moved the checkpoint (`properties.stream.checkpoint`) and the old config
  writes another output, the failed run's output keeps what it wrote, and the old config goes on
  writing its own.

Only the job config goes back: the rolled-back run keeps the runner and the settings this apply
compiled, so a release that changed only those rolls nothing back and says so. A stream last
started by a fluid that kept no release record cannot go back: the outcome says so and leaves the
failing run as it is. `--mode reconcile` watches nothing and rolls nothing back.

| Event | What it means | Do |
|---|---|---|
| `spark_release_rolled_back` | the new config failed its probation, and the previous one runs again | tell the user what the output holds; fix the contract, then plan and apply again |
| `apply_spark_rollback_unsafe` | the release moved the checkpoint and the old config writes the same output or dead-letter table, so nothing went back: the failed run is cancelled and the stream is down | ask the user: fix the contract and apply, or apply the previous contract on purpose and expect the records between the two checkpoints twice |
| `apply_spark_no_known_good_config` | the config the release replaced did not end well (its last run failed, was cancelled before the apply, or was lost), and the stream is down | fix the contract and apply; the failed run's outcome names its error |
| `apply_spark_release_failed_earlier` | this config failed its probation on an earlier apply, and its marker (`releases/<hash16>/failed` beside the build's job config) keeps it from starting | change the contract to fix it; only when the cause was the environment, ask the user to set `FLUID_SPARK_ACCEPT_FAILED_RELEASE=1` for one apply |
| `apply_spark_artifact_read_denied` | the identity that runs `fluid apply` cannot read the build's prefix (`_fluid/spark/<product>/<build>/`), so nothing was started | grant it `s3:GetObject` there, and `s3:ListBucket`: without it S3 answers a missing marker with AccessDenied, and a refused marker cannot be told from a missing one (when only the marker is refused, the user can set `FLUID_SPARK_ACCEPT_FAILED_RELEASE=1` for one apply instead) |

`FLUID_SPARK_ACCEPT_FAILED_RELEASE` is the operator's to give: a project `.env` cannot set it.

## What each commit records

Every commit the runner makes (a batch overwrite, a stream's append, a dead-letter table's
append) carries these keys in its Iceberg snapshot summary. The ones after `fluid.job-sha256`
are there when `fluid apply` knows them.

| Key | Value |
|---|---|
| `fluid.product`, `fluid.build` | the product and the build |
| `fluid.job-sha256` | the digest of the job config as it ran |
| `fluid.product-version` | the version of the build's output |
| `fluid.contract-digest` | the contract as loaded, before `{{ env.* }}` is resolved |
| `fluid.bundle-digest` | the bundle of the plan `fluid apply` verified |
| `fluid.release-id` | `FLUID_RELEASE_ID`, when the deploy names a release |
| `fluid.run-id` | the pipeline's run id |
| `fluid.scheduler-job`, `fluid.scheduler-run-id` | the scheduler's job and run, such as Airflow's DAG id and run id |

- Set `FLUID_RELEASE_ID` where the deploy runs: a tag, a build number, or the `release` line
  `fluid runs status` shows. It starts with a letter or a digit, holds only letters, digits
  and `._:+@/=~-`, and is at most 256 characters; any other value refuses the apply
  (`--mode reconcile` leaves it off, with a warning).
- `FLUID_RUN_ID`, when set, is the run id. Otherwise, under a scheduler, the run id is
  derived from the scheduler's run: `FLUID_SCHEDULER_JOB` and `FLUID_SCHEDULER_RUN_ID`, else
  Airflow's `AIRFLOW_CTX_DAG_ID` and `AIRFLOW_CTX_DAG_RUN_ID`. The DAG `fluid generate
  schedule` renders passes Airflow's two to fluid under the `FLUID_SCHEDULER_` names.
- The stamps are arguments of a run, not its config: a new value restarts no stream, and a
  running stream keeps the stamps it started with.

Read them back in Spark SQL:

```sql
SELECT committed_at, snapshot_id,
       summary['fluid.release-id'] AS release_id,
       summary['fluid.bundle-digest'] AS bundle,
       summary['fluid.scheduler-run-id'] AS scheduler_run
FROM glue_catalog.curated.orders.snapshots
ORDER BY committed_at DESC
```

## Which release was in force

A job that fluid does not run leaves no run record, so seal a release at each deploy, in the
CI that deploys it. `--release-dir` names a new directory, and the release is written there
only when every stage passes:

```bash
fluid ship contract.fluid.yaml --env prod --release-dir releases/<release-id>
```

For a job another tool deploys, seal it without applying:
`fluid ship contract.fluid.yaml --env prod --until validate-artifacts --release-dir releases/<release-id>`.

Then ask which release was in force, now or on a UTC day:

```bash
fluid runs status <product-id> --release-dir releases/
fluid runs status <product-id> --release-dir releases/ --on 2026-11-20 --env prod --json
```

- The answer is one per environment the releases name (or the one `--env` names): the
  release in force, the releases sealed that day, each one's versions, the CI's revision, the
  run that sealed it and its bundle. A commit whose `fluid.bundle-digest` equals a release's
  `bundle` line came from that release's bundle.
- Only an intact release is in force. A later record that no longer matches its identity
  digest is listed under "Not in force" and never stands in for the release before it.
- Exit code 0: a release was in force or sealed that day. 1: none was. 2: the input is wrong
  (a directory that cannot be read, an `--on` that is not `YYYY-MM-DD`).
