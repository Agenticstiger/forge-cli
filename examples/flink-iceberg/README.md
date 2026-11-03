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

This fixture proves task recovery and nested-value fidelity. It does not certify
Kubernetes deployment, savepoint upgrades, job-manager failover or production HA.
