# Upstream issue draft — goccy/bigquery-emulator

`tofu apply` via `hashicorp/google` plugin crashes after Create on
read-back ("Plugin did not respond"). Documented as xfail in our
test suite (`tests/iac/test_iac_gcp_emulator_e2e.py::test_emu_tofu_apply_round_trip_xfail`).

As of the date of this draft, a search of `goccy/bigquery-emulator/issues`
returned no existing report.

## To file

```bash
gh issue create \
  --repo goccy/bigquery-emulator \
  --title "tofu apply via hashicorp/google provider crashes after dataset Create" \
  --body-file docs/upstream-issues/gcp-emulator-tofu-apply.body.md
```

The body file:

---

## Title

`tofu apply` via `hashicorp/google` provider crashes after dataset Create ("Plugin did not respond")

## What I'm doing

Using `goccy/bigquery-emulator:latest` to test OpenTofu / Terraform
configurations that target BigQuery — point the
`hashicorp/google` provider at the emulator via
`big_query_custom_endpoint = "http://localhost:9050/bigquery/v2/"` and
attempt a normal `tofu apply` of a `google_bigquery_dataset` +
`google_bigquery_table`.

## What happens

`tofu plan` succeeds. `tofu apply` reaches the Create step and the
dataset (or any other resource) is actually created in the emulator —
verified directly via `curl http://localhost:9050/bigquery/v2/projects/<p>/datasets`.
But the apply then errors with:

```
Error: Plugin did not respond
The plugin encountered an error, and failed to respond to the
plugin.(*GRPCProvider).ApplyResourceChange call. The plugin logs may
contain more details.
```

This kills the apply with a non-zero exit code and leaves tofu state
out of sync with the emulator (the resource exists but tofu doesn't
record it).

## Reproduction

`docker compose up -d` with goccy/bigquery-emulator on its default
ports (9050 HTTP / 9060 gRPC). Provider block:

```hcl
provider "google" {
  project                    = "fluid-emulator"
  big_query_custom_endpoint  = "http://localhost:9050/bigquery/v2/"
  access_token               = "emulator-dummy-token"
}

resource "google_bigquery_dataset" "test" {
  dataset_id = "test_ds"
  location   = "US"
}
```

Then `tofu init && tofu apply`. The dataset is created; tofu apply
errors out on the read-back.

## Hypothesis

The hashicorp/google provider calls a follow-up API after Create
(probably `datasets.get` or a labels read-back) and the emulator's
response shape diverges enough from real BigQuery that the provider's
unmarshalling panics. The "Plugin did not respond" wrapper is what
the tofu / terraform plugin host shows when the provider process
crashes mid-RPC.

## What I'd love

Either:

1. Documentation that the emulator's intended scope is client-library
   testing (not Terraform / OpenTofu provider testing), so users like
   me don't waste time. I'd be happy to PR this if you confirm.
2. Or a flag / response-shape fix that makes the post-Create reads
   match what hashicorp/google expects.

Workaround for now: I run `tofu init + tofu plan` against the
emulator (proves the .tf.json shape is well-formed) AND verify the
resource SHAPE against the emulator via the Python SDK
(`google-cloud-bigquery`'s `client_options.api_endpoint`). Full
apply round-trips move to a real GCP project. This works fine — I
just can't get the apply leg to round-trip on the emulator.

Happy to provide tofu plan output, provider version info, or
docker-compose file.

Thanks for the emulator — it's still hugely useful for SDK testing.

---
