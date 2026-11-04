# Nested Flink -> Iceberg example

See [the preview guide](../../docs/flink-iceberg.md) for the contract format,
operator prerequisites, workflow, limits and certification status.

The [contract](contract.fluid.yaml) preserves five nested structs, arrays of
structs, exact decimal prices, maps and event-time timestamps.

## Record shapes

A build declares the records it reads in `properties.stream`:
- the topic;
- the value's format (`json` or `avro-confluent`) and its fields, as columns;
- optionally the record key (`raw`, `json` or `avro-confluent`);
- optionally Kafka metadata (`topic`, `partition`, `offset`, `timestamp`,
  `timestamp-type`, `headers`, `leader-epoch`), each read as a column.

`properties.mapping` says how the records become the output's rows. forge-cli
writes the query from the two, and checks every output column's type and
nullability against the path it is read from before anything deploys. An output
column the mapping does not list is read from the stream column at the same
path. So a stream landed as it is, flat or nested, needs no mapping, like the
contract above.

The [shapes](shapes) directory reads one stream, kept in a
[fragment](shapes/fragments/streams/orders.yaml), into two products:
- [order-items](shapes/order-items/contract.fluid.yaml) flattens each event to
  one row per item. It lifts nested fields to columns, explodes `items`, looks
  up a map entry and a header, and keeps the record key and the Kafka position.
- [orders-nested](shapes/orders-nested/contract.fluid.yaml) writes each event as
  a nested row in a shape of its own:
  - structs built from fields that are flat or elsewhere in the record (a
    dotted name such as `order.key` maps one field);
  - a subset of `customer` that stays null where the customer is;
  - the coordinates struct moved up;
  - the Kafka position grouped in one struct.

Their `$ref`s reach the shared fragment through the
[workspace](shapes/fluid.workspace.yaml). Check a fragment on its own, naming
the schema version because the workspace has no root contract:

```bash
fluid validate --schema-version 0.7.7 --fragment streamSource shapes/fragments/streams/orders.yaml
```

`--fragment recordMapping` checks a mapping, and `--fragment schema` a column list.

A build may run its own `properties.sql` instead, read by output column name.
`fluid apply` refuses it unless the operator lists the product in
`FLUID_FLINK_CONTRACT_SQL_ALLOWLIST`. That SQL runs with the job's catalog
identity, so it can read any table that identity can read, not only the stream.

## Run the data-plane proof locally

Requirements: a working Docker daemon with Compose, Forge's test environment,
and network access to the pinned fixture images and Python packages. No cloud
resources are provisioned.

From the repository root:

```bash
docker build -t forge-flink:test examples/flink-iceberg/runtime
FLUID_TEST_FLINK_ICEBERG=1 PYTHONPATH="$PWD" \
  .venv/bin/python -m pytest -q tests/integration/test_flink_iceberg_live.py
```

For Snap Docker, put pytest's temporary directory under a non-hidden,
Docker-accessible workspace directory with `--basetemp`; Snap cannot bind-mount
normal `/tmp` or hidden home directories.

The test uses a unique Compose project and loopback-only random host ports,
produces events, restarts the task manager, produces another batch, independently
compares every nested value and then checks schema-drift refusal. A second test
runs both shapes from the same keyed records with a header, and compares their
rows with what was produced. Containers, networks and volumes are removed in
`finally`. Fixture credentials are local test values only. Docker images and
build cache are retained.

Do not run `compose up` directly without generating/mounting `job.properties`;
the test does that from the actual compiler output.

## Deploy separately to Kubernetes

Build and push the runtime to your registry, pin its resulting digest in the
contract, replace development endpoints/context, and provision the prerequisite
operator, RBAC, buckets, catalog, Kafka and secrets. The all-zero digest is an
intentional placeholder, not a deployable image.

`fluid apply` (`--dry-run` included), `fluid diff` and `fluid verify` refuse a
managed deployment until the operator lists its targets. A contract names them,
so only the operator may allow them, and an unset list allows nothing. Set these
in the shell or CI that runs fluid; a project `.env` is ignored for them:

```bash
export FLUID_FLINK_KUBE_CONTEXT_ALLOWLIST=development
export FLUID_FLINK_NAMESPACE_ALLOWLIST=data-products
export FLUID_FLINK_IMAGE_ALLOWLIST=forge-flink   # the image without :tag and @sha256:
export FLUID_FLINK_SERVICE_ACCOUNT_ALLOWLIST=data-products/flink
export FLUID_FLINK_SECRET_ALLOWLIST=data-products/flink-storage
export FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST=iceberg-rest   # the catalog the job uses
```

Once the image is in your registry, list its repository instead, such as
`registry.example.com/forge-flink`, or `registry.example.com/` for everything
under it. A prefix must end in `/`.

`fluid apply` and `fluid verify` also check where the job connects: the catalog,
an OAuth2 token endpoint and any `s3.endpoint` in `catalogProperties`. A
hostname needs an entry in `FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST`
whatever it resolves to, because the job resolves it itself, after fluid has
checked it; list a parent domain such as `svc.cluster.local` to cover a
cluster's services. An IP address needs an entry only when it is public, and a
link-local one is always refused. A `catalogAuth` variable needs `NAME@host` in
`FLUID_ACQUISITION_SECRET_REF_ALLOWLIST` for each host that receives it, before
fluid reads it: `LAKEKEEPER_TOKEN@catalog.example.com` for a bearer token. An
OAuth2 credential needs two entries, one for the token endpoint (which must be
`https://`) and one for the catalog, which receives the token minted from it.
`localhost` needs neither entry. No other catalog property may start with
`env:`, since Iceberg would resolve it from the job's environment.

Checkpoint, savepoint, HA and warehouse locations are not on these lists. Scope
them with the permissions of the credentials in the allowed Secrets.

This fixture proves task recovery and nested-value fidelity. It does not certify
Kubernetes deployment, savepoint upgrades, job-manager failover or production HA.
