## What I'm doing

Running the same integration test twice against one long-lived
`ghcr.io/goccy/bigquery-emulator` container, using `google-cloud-bigquery`:

```python
from google.cloud import bigquery

client = bigquery.Client(
    project="fluid-emulator",
    client_options=ClientOptions(api_endpoint="http://localhost:9050"),
    credentials=AnonymousCredentials(),
)
ds = bigquery.Dataset("fluid-emulator.sdk_probe")
ds.location = "US"
client.create_dataset(ds, exists_ok=True)   # second run hangs here
```

## What I expect

`exists_ok=True` returns the existing dataset. Against real BigQuery it
does, because the API answers a duplicate create with HTTP 409 and reason
`duplicate` / `alreadyExists`, and the client catches exactly that:

> `google/cloud/bigquery/client.py` — `except core_exceptions.Conflict: if not exists_ok: raise; return self.get_dataset(...)`

## What happens instead

The emulator logs and returns an internal error:

```
ERROR	server/handler.go:684	internalError	{"error": "internalError: dataset sdk_probe is already created"}
```

`google-api-core` treats `internalError` as a **retryable** server fault,
so the client never reaches the `Conflict` branch. It retries with
exponential backoff until the deadline and then raises. The visible
symptom is a multi-minute hang inside
`google/api_core/retry/retry_unary.py`, not an "already exists" error.

The same shape applies to `google-cloud-bigquery` for Go/Java/Node, and to
`terraform`/`tofu` refresh paths, since all of them distinguish
"already exists" from "server is unwell" by the HTTP status.

## Suggested fix

Return HTTP 409 with a Google-style error body for a duplicate dataset
create, e.g.

```json
{"error": {"code": 409, "message": "Already Exists: Dataset fluid-emulator:sdk_probe",
 "errors": [{"reason": "duplicate", "message": "Already Exists: ..."}], "status": "ALREADY_EXISTS"}}
```

`Table` creation is worth checking for the same thing — we did not get far
enough to confirm, because the dataset call fails first.

## Impact

It is not a correctness bug in the stored data, and it is easy to work
around once understood (unique names per run). It is unpleasant to
diagnose: a test that passes on a fresh container and hangs on a warm one
reads as flakiness or as a network problem, not as an API-fidelity gap.

## Version

`ghcr.io/goccy/bigquery-emulator:latest`, pulled 2026-09-14.
