**LocalStack version**: Pro Platinum, current (May 2026)
**Host OS**: macOS 14.x, Docker Desktop

### Summary

Lambda invocations inside LocalStack Pro fail when LocalStack itself is
running in Docker without a host docker socket bind-mount. The failure
is late (apply succeeds, invoke fails), which makes the root cause
non-obvious from the surface error.

### Repro

```bash
# Default Pro Platinum start command — no docker-socket bind.
localstack start -d --image localstack/localstack-pro:latest \
  --license-auth-token "$LOCALSTACK_AUTH_TOKEN"

# Create a Lambda fn via tofu, then invoke it.
aws --endpoint-url=http://localhost:4566 lambda invoke \
  --function-name my-fn /tmp/out.json
```

Expected: function executes and writes `/tmp/out.json`.
Observed:

```
{
  "FunctionError": "Unhandled",
  "ExecutedVersion": "$LATEST",
  "StatusCode": 200,
  "Payload": {"errorMessage": "Cannot connect to the Docker daemon
   at unix:///var/run/docker.sock. Is the docker daemon running?",
   "errorType": "DockerError"}
}
```

### Root cause

LocalStack Pro Lambda V2 spawns an inner Docker container per function
invocation. Without `-v /var/run/docker.sock:/var/run/docker.sock`,
that spawn fails.

### Proposal

Pre-flight check at LocalStack start: if `LAMBDA_EXECUTOR` resolves to
`docker` (the Pro Platinum default) AND the docker socket inside the
container is unreachable, log a WARNING with the mount command and
either:
- fail fast (preferred — surfaces the misconfig at start, not at invoke),
- or downgrade silently to `LAMBDA_EXECUTOR=local` with a clear log line.

The current behaviour discovers the misconfig only after a tofu apply +
client invocation, which is multiple steps and several seconds into the
workflow.

### Workaround

Bind-mount the host docker socket explicitly:

```bash
localstack start -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --image localstack/localstack-pro:latest
```

Or run LocalStack on the host directly (no outer container), which has
its own trade-offs.
