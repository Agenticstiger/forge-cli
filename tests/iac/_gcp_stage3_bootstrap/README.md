# GCP Stage 3 — bootstrap

One-time setup for `tests/iac/test_iac_gcp_real_e2e.py` (Stage 3 — real GCP).

Creates the test service account the live test suite impersonates,
plus its project-level role bindings. Designed for use against an
**existing GCP project**; does NOT create the project itself.

## Pre-requisites

* A GCP project with billing linked (the suite uses BigQuery + GCS +
  Pub/Sub — the metadata APIs are free but billing must be enabled
  for actual reads/writes).
* The runner's gcloud identity needs project-level Owner (or at
  minimum `roles/resourcemanager.projectIamAdmin` +
  `roles/iam.serviceAccountAdmin` + `roles/serviceusage.serviceUsageAdmin`).
* ADC set up on the runner machine:

  ```bash
  gcloud auth application-default login
  ```

  Verify:

  ```bash
  ls ~/.config/gcloud/application_default_credentials.json
  ```

## Apply

```bash
cd tests/iac/_gcp_stage3_bootstrap
tofu init
tofu apply -var="project_id=fluidtesting" -var="user_principal=user:jeffwatson@agenticstransformation.com" -auto-approve
tofu output -json > /tmp/fluid-gcp-stage3.json
```

Then export the test-runner env vars:

```bash
eval "$(tofu output -json | python3 -c '
import json, sys
out = json.load(sys.stdin)
print(f"export FLUID_GCP_PROJECT={out[\"project_id\"][\"value\"]}")
print(f"export FLUID_GCP_TEST_SA={out[\"test_sa_email\"][\"value\"]}")
print(f"export FLUID_GCP_REGION={out[\"region\"][\"value\"]}")
')"
```

## Teardown

```bash
tofu destroy -auto-approve
```

Safe to leave in place between sessions — the SA + role bindings are
scoped to the `fluid-iactest-*` resource-name pattern the live tests
use. The service account itself does NOT incur charges; only the
ephemeral per-test resources do (BigQuery datasets/tables drop on
test teardown via fixture cleanup + session sweeper).

## Cost guard

Per-test resources are tagged `managed_by=fluid` + named with a
`fluid-iactest-<uuid>` prefix. The session sweeper in
`tests/iac/conftest.py` lists every BigQuery dataset / GCS bucket /
Pub/Sub topic matching that prefix and deletes them at session end —
even if a test crashed mid-flight. BigQuery default
`defaultTableExpirationMs` is set to ~60 minutes as belt-and-
suspenders.
