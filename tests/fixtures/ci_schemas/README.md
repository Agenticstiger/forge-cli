# Vendored CI schemas

JSON Schemas (Draft 7) that `tests/forge/core/pipeline_systems/test_thin_wrappers.py`
validates the generated GitLab CI and CircleCI files against. Both were fetched
on 2026-11-17 and are used unmodified.

| File | Source | Licence |
|---|---|---|
| `gitlab-ci.json` | https://gitlab.com/gitlab-org/gitlab/-/raw/master/app/assets/javascripts/editor/schema/ci.json | MIT (the CE part of gitlab-org/gitlab) |
| `circleci.json` | https://raw.githubusercontent.com/CircleCI-Public/circleci-yaml-language-server/refs/heads/main/schema.json | Apache-2.0 (CircleCI-Public/circleci-yaml-language-server) |

`circleci.json` is the schema that https://json.schemastore.org/circleciconfig.json
points at with its `$ref`. SchemaStore's own file is only that one-line
pointer, so the schema it references is vendored here instead.

## Not vendored

* Azure Pipelines: the schema is 1.6 MB.
* Bitbucket Pipelines: the schema states no licence.

The Azure, Bitbucket and Tekton outputs are checked structurally instead.

## Refreshing

Download both URLs above, overwrite the two files, update the date in this
file, and run:

    python -m pytest tests/forge/core/pipeline_systems/test_thin_wrappers.py

A new schema release that rejects a generated file is a real finding: fix the
generator, not the schema.
