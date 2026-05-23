# `tofu apply` via `hashicorp/google` provider crashes after dataset Create ("Plugin did not respond")

## What I'm doing

Using `goccy/bigquery-emulator:latest` to test OpenTofu / Terraform
configurations that target BigQuery — point the `hashicorp/google`
provider at the emulator via `big_query_custom_endpoint =
"http://localhost:9050/bigquery/v2/"` and attempt a normal `tofu apply`
of a `google_bigquery_dataset` + `google_bigquery_table`.

## What happens

`tofu plan` succeeds. `tofu apply` reaches the Create step and the
dataset is actually created in the emulator (verified via
`curl http://localhost:9050/bigquery/v2/projects/<p>/datasets`).
But the apply then errors with:

```
Error: Plugin did not respond
The plugin encountered an error, and failed to respond to the
plugin.(*GRPCProvider).ApplyResourceChange call. The plugin logs may
contain more details.
```

The apply exits non-zero and tofu state is left out of sync with the
emulator.

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
errors on the read-back step.

## Hypothesis

The hashicorp/google provider calls a follow-up API after Create
(probably `datasets.get` for a labels / fields read-back). The
emulator's response shape diverges enough from real BigQuery that the
provider's unmarshalling crashes the provider process. "Plugin did
not respond" is what the OpenTofu / Terraform plugin host shows when
the provider exits mid-RPC.

## What I'd love

Either:

1. Documentation that the emulator's intended scope is client-library
   testing (not OpenTofu/Terraform provider testing), so users don't
   spend a day debugging this. Happy to PR this if you confirm.
2. Or a flag / response-shape fix making the post-Create reads match
   what hashicorp/google expects.

## Workaround

`tofu init + tofu plan` works against the emulator (proves the
`.tf.json` shape is well-formed) AND the Python `google-cloud-bigquery`
SDK works against the emulator (proves the resource shape). Full apply
round-trips move to a real GCP project.

## Versions

- `ghcr.io/goccy/bigquery-emulator:latest` (image digest TBD by reporter)
- `hashicorp/google` provider `~> 6.0`
- OpenTofu `1.12.0`

Happy to provide tofu plan output, provider debug logs, or the
docker-compose file used.

Thanks for the emulator — it's still hugely useful for SDK testing.
