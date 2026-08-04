# Vendored cloud-region data

`aws.csv`, `gcp.csv`, `azure.csv` are vendored verbatim from
[dgl/cloud-regions](https://github.com/dgl/cloud-regions), which maps each
cloud provider's region identifier to the country it physically sits in.

## Licence — read before editing

This data is **ODbL-1.0** (Open Database License), because the upstream project
derives locations from OpenStreetMap. That is *not* the Apache-2.0 licence the
rest of this repository uses.

ODbL covers the **database**, not software that reads it: forge-cli stays
Apache-2.0, and contract validation output is a "produced work" you may license
freely. What ODbL does require is attribution (above, and in `NOTICE`) and that
any modified or derived *database* redistributed publicly is offered under ODbL
too. So: refresh these files from upstream, don't hand-edit them into something
new.

## Why vendored rather than fetched

A residency check has to work offline and give the same answer on every machine.
Fetching at runtime would make a governance verdict depend on network reachability
and on whatever upstream happened to say that morning.

## Why only GCP and Azure are authoritative here

AWS regions are resolved from `botocore`'s own `endpoints.json` instead — the
vendor's table, shipped with the SDK and updated on every release. Measured at
vendor time, this dataset was 14 AWS regions behind botocore, missing
`eusc-de-east-1` (AWS European Sovereign Cloud) among others, and contained no
AWS region botocore lacked. `aws.csv` is kept only as a fallback for
installations without boto3.

Neither Google nor Microsoft ships an equivalent offline dataset, so for those
two this file is the best available source. Refresh with:

    curl -sfO https://raw.githubusercontent.com/dgl/cloud-regions/main/{gcp,azure,aws}/data.csv
