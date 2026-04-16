# Snowflake Account Identifiers

`forge-cli` accepts the common Snowflake account identifier forms that users tend to paste from the Snowflake UI, connector docs, or browser URLs, then normalizes them before building connector settings.

Accepted inputs:

- `org-account`
- `xy12345`
- `xy12345.us-east-1`
- `xy12345.us-east-1.aws`
- `xy12345.eu-central-1.gcp`
- `xy12345.west-us-2.azure`
- `org-account.privatelink`
- `xy12345.us-east-1.privatelink`
- `https://org-account.snowflakecomputing.com`
- `https://xy12345.us-east-1.aws.snowflakecomputing.com`
- `https://xy12345.us-east-1.privatelink.snowflakecomputing.com`
- Snowsight-style hostnames such as `https://app-org-account.privatelink.snowflakecomputing.com/...`

Canonical output examples:

- `org-account` -> `org-account`
- `xy12345.us-east-1.aws` -> `xy12345.us-east-1`
- `https://xy12345.us-east-1.aws.snowflakecomputing.com` -> `xy12345.us-east-1`
- `https://xy12345.us-east-1.privatelink.snowflakecomputing.com` -> `xy12345.us-east-1.privatelink`

Normalization behavior:

- strips `https://`
- strips `.snowflakecomputing.com`
- strips trailing cloud suffixes such as `.aws`, `.gcp`, and `.azure`
- preserves `.privatelink` when it is part of the effective connector account identifier
- strips a leading `app-` prefix from browser hostnames

Invalid values raise a `ValueError` with guidance instead of silently misparsing. For example, arbitrary hostnames such as `https://example.com` are rejected.
