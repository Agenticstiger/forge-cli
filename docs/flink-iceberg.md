# Managed Flink to Iceberg (preview)

Forge can compile and reconcile an opt-in, append-only Kafka -> Flink -> Iceberg
stream. Use `fluidVersion: "0.7.7"` and
`builds[].properties.parameters.flink.management: operator`. Existing external
Flink builds and the default stable schema (0.7.6) are unchanged.

Start with [the example contract](../examples/flink-iceberg/contract.fluid.yaml)
and [its runnable Docker test](../examples/flink-iceberg/README.md).

## Supported scope

| Surface | Preview behavior |
|---|---|
| Recursive types | Struct fields, array elements, string-keyed maps, nested nullability, decimals up to precision 38, timestamps up to microseconds; maximum depth 32. |
| Kafka input | JSON with ISO-8601 timestamps, or Confluent Avro with a registry URL. Explicit topic, consumer group, startup mode and source schema. Malformed JSON fails rather than silently dropping records. |
| Transformation | One `SELECT` from `source_events`; one Iceberg output. No semicolons, including inside literals. The matching Flink planner performs semantic validation. |
| Storage | Explicit S3 warehouse URI, REST catalog, S3-compatible FileIO and state endpoints. Optional bearer/OAuth2 catalog auth through named environment variables. Authenticated catalog/token requests require HTTPS. |
| Table | Native Iceberg namespace/table creation, format v2, append-only writes, supported partition transforms, Parquet by default. Existing table schema must match before INSERT submission. |
| Runtime | Flink 1.20.1 / Java 17 / Iceberg 1.10.0 image; Kubernetes Operator resources, digest-pinned images, immutable SQL ConfigMaps, dedicated checkpoint/savepoint/HA prefixes. Operator 1.13 status semantics are used. |
| Lifecycle | `amend`, `amend-and-build`, `create-only`, dry runs, operator savepoint upgrades and suspension. Replace/reset is refused. No batch runner or scheduler submits this job. |
| Evidence | Operator spec/current reconciliation, successful recent checkpoint, recursive REST schema and recent committed snapshot. These are **not** per-record delivery receipts. |
| Publication | ODCS native object/array trees plus a `fluid.recursiveType` custom property for lossless FLUID round trips, including typed-map semantics that ODCS cannot express natively. Contract-version diffs recurse through structs, arrays and maps. |

Managed contracts contain only managed builds and their outputs. Provision Kafka,
the catalog, buckets, namespace, service account, secrets and operator separately.
One managed writer per expose; deployment identities and state prefixes cannot
overlap within the contract. Cross-contract writer/prefix coordination remains
the operator's responsibility.

## Workflow

```bash
fluid validate examples/flink-iceberg/contract.fluid.yaml
fluid generate flink examples/flink-iceberg/contract.fluid.yaml --out runtime/flink
fluid diff examples/flink-iceberg/contract.fluid.yaml --exit-on-drift --out runtime/drift.json
fluid plan examples/flink-iceberg/contract.fluid.yaml --mode amend --out runtime/plan.json
fluid apply runtime/plan.json --mode amend --yes
fluid verify examples/flink-iceberg/contract.fluid.yaml --strict --out runtime/delivery.json
```

Replace the example's all-zero image digest and development endpoints before
deployment. The image must contain the supplied Java runner and pinned connectors;
an arbitrary Flink base image is insufficient. Use matching modes for plan/apply.
Normal bundle/plan digest binding is enforced before Kubernetes writes.

`generate flink`, `plan` and `apply --mode dry-run` never contact the cluster,
catalog or registry. Offline validation cannot establish SELECT correctness;
the runtime checks it with Flink before submitting the job.

`diff` compares the live deployment against the immutable **last-applied**
specification. An intentional next-plan change is pending, not out-of-band drift.
New deployments are pending. A missing baseline or unreadable cluster is an
error, never a clean baseline. Managed drift is live-only and currently covers
the operator spec, not object-store files or Iceberg schema; `verify` checks the
latter schema and commit health.

`apply` waits for the requested reconciliation and reports running/suspended,
not delivered. `verify` additionally checks a checkpoint through the Kubernetes
REST-service proxy and loads the catalog schema/snapshot independently. An idle
stream may intentionally exceed `maxSnapshotAgeSeconds`; set the freshness
budget to the product's actual delivery expectations. Verification waits up to
120 seconds for fresh checkpoint/commit evidence (`--streaming-timeout` controls
that wait; `0` checks once). Readiness, auth and schema errors fail immediately.

## Types and transformations

```yaml
- name: lines
  type: array
  element:
    type: struct
    fields:
      - name: amount
        type: decimal(18,2)
        required: true
- name: attributes
  type: map
  key:
    type: string
    required: true
  value:
    type: string
```

Every complex type needs its child definitions; bare `struct`, `array`, `map`
or SQL type fragments are not sufficient. `required` defaults to false. Map keys
must be required strings. Names must be safe SQL identifiers and must not differ
only by case. Iceberg stores timestamps in microseconds; runtime schema checking
uses the Iceberg/Flink type conversion, not raw SQL spelling.

Source and target schemas are separate, allowing SELECT projections, casts and
nested reconstruction. Watermarks can reference a top-level timestamp through
`source.eventTime: {column: event_time, delaySeconds: 5}`. A watermark is **not**
a late-record quarantine policy.

Table creation is `CREATE TABLE IF NOT EXISTS`, followed by recursive physical
schema comparison **before** INSERT planning/submission. No automatic schema
migration is performed. Additions, renames, changes of requiredness and type
changes require an explicit Iceberg migration and review. Schema checks do not
lock the table against concurrent external DDL.

## Credentials and operations

`deployment.context`, `namespace`, and `serviceAccount` are mandatory. `kubectl`
uses that context explicitly, bounded request timeouts, ownership checks and
server-side apply without force-conflict takeover. The CLI needs CR/ConfigMap
read/write and service-proxy read permissions; the Flink service account needs
the usual native Kubernetes/HA permissions.

`deployment.secretRefs` injects existing Kubernetes Secrets through `envFrom`.
Configure object-store credentials or workload identity outside the contract.
Do not put credential values in SQL, URIs or generated resources.

```yaml
catalogAuth:
  type: oauth2
  credentialEnv: ICEBERG_CREDENTIAL
  oauth2ServerUri: https://identity.example/token
  scope: catalog
catalogProperties:
  s3.endpoint: https://objects.example
  s3.path-style-access: "true"
  client.region: us-east-1
```

The credential variable contains `client_id:client_secret`. Bearer auth instead
uses `{type: bearer, tokenEnv: ICEBERG_TOKEN}`. Iceberg resolves environment
references in the workload; CLI verification resolves its own environment.
S3-compatible endpoint/path-style/region settings also configure the checkpoint
filesystem. Workload and CLI credentials are not copied between environments.

Suspension uses `deployment.state: suspended`. Restore `running` to resume
through the operator. Savepoint-to-last-state fallback is disabled; failures
must be investigated rather than silently starting without the expected state.
The runner's `--validate-only` mode still executes setup DDL and may create a
namespace/table; it is **not** a side-effect-free dry run.

## Certification and remaining work

The opt-in Docker integration test runs the actual generated statements through
Kafka, Flink, Iceberg REST and MinIO. It checks all values for 200 unique records,
five nested structs, arrays/maps, null/empty containers, exact decimals and
timestamps after a task-manager restart. It also checks that an existing-table
nested-schema mismatch is rejected before writes. An independent PyIceberg/Arrow
reader validates the results.

The `Flink Iceberg preview` workflow enforces a 90% new-module coverage floor
and strict mypy checks on relevant pull requests. Its manual `run_live` input enables the Docker proof;
no cloud account or Kubernetes cluster is needed for that job.

This is **not Kubernetes lifecycle or production HA certification**. Operator
apply/readiness/ownership/drift paths are unit-tested; actual install, upgrade,
savepoint/resume, job-manager loss and Kubernetes failure injection remain gates.
The Docker fixture's job-manager-local checkpoints do not prove durable S3 HA.

Not implemented: CDC/upsert/delete streams, enforced late-event routing or
quarantine, schema-registry version pinning/compatibility, automatic Iceberg
schema/partition evolution, maintenance scheduling, Kafka SASL/TLS configuration,
nested policy/quality enforcement, multi-output writes, non-S3 storage, and
named-warehouse catalogs. Nonempty root governance/access policies and output/
nested quality/policy rules are rejected rather than advertised as enforced.
The existing generic stage-3 fanout remains unchanged; Flink deployment artifacts
are emitted explicitly with `generate flink`.
