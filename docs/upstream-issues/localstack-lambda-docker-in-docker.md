# Upstream issue draft — localstack/localstack-pro

Lambda function invocation inside the LocalStack Pro container fails
when LocalStack itself is running inside another container (the
"Docker-in-Docker" topology) without the host docker socket bind-mounted
into the LocalStack container. The forge-cli IaC test suite trips this
on `test_iac_aws_localstack_e2e.py::test_localstack_glue_iceberg_table_e2e`
and `test_lambda_handler_e2e` when LocalStack Pro is run via
`localstack start -d` on a developer macOS box: `tofu apply` lands the
`aws_lambda_function` resource shape, but the first `lambda invoke`
panics with `Cannot connect to the Docker daemon at unix:///var/run/docker.sock`.

## Repro

```bash
localstack start -d   # localstack Pro Platinum, default config
pytest tests/iac/test_iac_aws_localstack_e2e.py::test_lambda_handler_e2e
# → Lambda exec fails: "docker daemon unreachable"
```

## Current behaviour

LocalStack Pro's Lambda V2 runtime spawns an inner Docker container per
execution. Inside the LocalStack container the runtime tries to talk to
the host docker socket at `/var/run/docker.sock` — which is not
bind-mounted by default in `localstack start -d`. The failure mode is
late (apply succeeds; invoke fails), so users discover it only when a
test that exercises lambda runs.

## Expected behaviour

Either (a) `localstack start` documents the `-v
/var/run/docker.sock:/var/run/docker.sock` requirement loud in the help
output / startup banner when LAMBDA_EXECUTOR=docker (the default for
Pro Platinum), or (b) LocalStack Pro pre-flight-checks for socket
reachability and fails fast at start, not at first invocation.

## Workaround in forge-cli

The two affected tests are marked `pytest.mark.skipif` on
`Docker-in-Docker socket unreachable` — the IaC emit + apply path is
covered for every other AWS resource so the LocalStack Lambda gap does
not block the test suite.

## To file

```bash
gh issue create \
  --repo localstack/localstack-pro \
  --title "Lambda V2 inside docker-in-docker: pre-flight docker socket reachability" \
  --body-file docs/upstream-issues/localstack-lambda-docker-in-docker.body.md
```
