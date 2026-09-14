# Upstream issue draft: goccy/bigquery-emulator

Creating a dataset that already exists returns `internalError` where real
BigQuery returns `409 alreadyExists`. That breaks the
`exists_ok=True` / get-or-create idiom in every official client library,
because those implement it by catching the 409.

Found while running `tests/iac/test_iac_gcp_emulator_e2e.py` twice against
one emulator container. `google-cloud-bigquery` classifies `internalError`
as retryable, so it backed off and retried to the deadline instead of
returning the existing dataset. A multi-minute hang, then a failure.

Worked around on our side by giving each run a UUID-suffixed dataset name
(the pattern our other emulator tests already used), so this is not
blocking us. Filing it because the get-or-create idiom is ubiquitous and
the failure mode is silent and slow rather than obvious.

## To file

```bash
gh issue create \
  --repo goccy/bigquery-emulator \
  --title "Duplicate dataset create returns internalError, not 409 alreadyExists, breaking exists_ok" \
  --body-file docs/upstream-issues/gcp-emulator-duplicate-dataset-error-code.body.md
```
