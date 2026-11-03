# Nested Flink -> Iceberg example

See [the preview guide](../../docs/flink-iceberg.md) for the contract format,
operator prerequisites, workflow, limits and certification status.

The [contract](contract.fluid.yaml) preserves five nested structs, arrays of
structs, exact decimal prices, maps and event-time timestamps.

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
compares every nested value and then checks schema-drift refusal. Containers,
networks and volumes are removed in `finally`. Fixture credentials are local test
values only. Docker images and build cache are retained.

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
```

Once the image is in your registry, list its repository instead, such as
`registry.example.com/forge-flink`, or `registry.example.com/` for everything
under it. A prefix must end in `/`.

`fluid apply` and `fluid verify` also check where the catalog credential goes:
the catalog, an OAuth2 token endpoint and any `s3.endpoint` in
`catalogProperties`. A public host needs an entry in
`FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST`, and a link-local host is always
refused. A `catalogAuth` variable needs `NAME@host` in
`FLUID_ACQUISITION_SECRET_REF_ALLOWLIST` for each host that receives it, before
fluid reads it: `LAKEKEEPER_TOKEN@catalog.example.com` for a bearer token. An
OAuth2 credential needs two entries, one for the token endpoint and one for the
catalog, which receives the token minted from it. A private or loopback host
needs no host entry, and a loopback host needs no `NAME@host` entry either.

Checkpoint, savepoint, HA and warehouse locations are not on these lists. Scope
them with the permissions of the credentials in the allowed Secrets.

This fixture proves task recovery and nested-value fidelity. It does not certify
Kubernetes deployment, savepoint upgrades, job-manager failover or production HA.
