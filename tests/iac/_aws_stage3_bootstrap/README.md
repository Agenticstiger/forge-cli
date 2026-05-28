# Stage 3 IAM bootstrap

One-time setup for `tests/iac/test_iac_aws_real_e2e.py` (Stage 3 — real AWS).

Creates four IAM roles the AWS plugin's contracts attach to Lambda / Step
Functions / Glue ETL / Redshift Spectrum at apply time, plus the matching
managed-policy attachments. Outputs the four role ARNs the live test suite
reads from `FLUID_AWS_*_ROLE_ARN` env vars.

## Usage

```bash
cd tests/iac/_aws_stage3_bootstrap
tofu init
tofu apply -auto-approve
tofu output -json > /tmp/fluid-stage3-roles.json
```

Then export the ARNs into the calling shell:

```bash
eval "$(tofu output -json | python3 -c '
import json, sys
out = json.load(sys.stdin)
for k, v in out.items():
    env = "FLUID_AWS_" + k.replace("_role_arn", "").upper() + "_ROLE_ARN"
    print(f"export {env}={v[\"value\"]}")
')"
```

## Teardown

```bash
tofu destroy -auto-approve
```

Safe to leave in place between sessions — the roles are scoped to the
`fluid-iactest-*` resource-name pattern the live tests use, and carry the
`managed_by=fluid` + `purpose=stage3-iac-testing` tags for filtering.
