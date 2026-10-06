# Maintainer Handbook

Operational guide for forge-cli maintainers. Covers the integration testing flow, credential rotation, release process, and triage playbooks.

For the contributor-facing view, see [`INTEGRATION_TESTING.md`](INTEGRATION_TESTING.md).

## Testing community PRs against cloud providers (pre-merge)

Tier-1 (DuckDB) integration runs automatically on every PR. For Tier-2 (Snowflake / BigQuery / AWS) validation before merging a community PR, push the PR's commits to a maintainer-controlled `staging/*` branch:

```bash
# In your local checkout of the forge-cli upstream repo:
git fetch origin pull/123/head:staging/PR-123
git push origin staging/PR-123
```

The push to `staging/PR-123` triggers `integration.yml` (because `staging/**` is in the trigger list). Watch the run in GitHub Actions — typically 10-15 minutes for all three jobs in parallel.

Once it's green, paste the run URL into the PR as a comment and approve the merge. If it's red, comment on the PR with the failing job and ask the contributor to fix.

If the contributor pushes new commits to their fork PR, repeat the `git fetch + git push` to update `staging/PR-123` with the new commits. (Or delete the stale `staging/PR-123` and create a fresh one.)

After the PR merges, delete the staging branch:

```bash
git push origin --delete staging/PR-123
```

### Why this flow instead of a `/test` bot

A comment-trigger bot (e.g. `/test integration`) requires either Prow infrastructure or a custom GitHub App. Both are operational liabilities that only pay off at very high PR volume. Two `git` commands per provider-touching community PR is acceptable maintenance overhead at forge-cli's current scale.

If maintainers find this tedious in the future, a small action that watches for a `staging` label on a PR and auto-pushes to `staging/PR-N` is a single-file follow-up.

## Triggering integration manually

Maintainers can fire `integration.yml` ad-hoc via the GitHub UI or `gh`:

```bash
# All providers
gh workflow run integration.yml --ref main

# One provider only
gh workflow run integration.yml --ref main -f providers=snowflake
```

This is useful after rotating credentials, after a known-flaky cloud outage, or to verify a hotfix without waiting for the nightly cron.

## Triage playbook for integration failures

### `integration-failure` issue auto-filed by the nightly cron

The nightly cron in `integration.yml` files an issue when any of the three provider jobs fails. The issue includes:

- Which jobs failed
- A link to the run
- Likely causes (provider API change, quota hit, leftover orphan, credentials rotated)

Triage steps in priority order:

1. **Open the failing job's logs.** Look for the actual error: `401`, `403`, `5xx`, `quota exceeded`, `assertion failed`, etc.
2. **Check whether `main` changed since the last green nightly.** If yes and the failure is a deterministic test assertion, bisect on `git log main`.
3. **If it's a credential failure**, rotate the affected secret (see "Rotating provider credentials" below).
4. **If it's a quota / billing failure**, log into the provider console, check the test account's recent usage, and either bump the quota or wait for it to reset.
5. **If it's a provider 5xx**, retry the workflow with `gh workflow run integration.yml --ref main`. If it fails twice consecutively, it's not transient — escalate to the provider's support channel.
6. **Always check the cleanup logs**, even when tests passed. A silent cleanup failure leaves orphans that drive cost.

Close the auto-filed issue with a one-line summary of the root cause once it's resolved. Pattern recognition matters; future-you and other maintainers will read these comments when triaging the next failure.

### Test asserts failed but the cloud API returned 200

This is the most informative kind of failure: the provider works, but our adapter is producing the wrong shape. Most often:

- The provider released a SDK update that changed return shapes
- A recent forge-cli change broke an assumption (e.g. expected key renamed)

Bisect on `git log main` since the last green nightly. Or if the failure is in a single test, run that test against a known-good commit to isolate.

### Cleanup script crashed mid-run

Symptom: integration job is green but the cleanup step is red, or cost is creeping up after multiple runs.

Fix:

1. Run the cleanup script manually with the same env vars to flush orphans:
   ```bash
   FORGE_CI_RUN_TAG="" python scripts/cleanup_snowflake_test_artifacts.py
   ```
   (The empty `FORGE_CI_RUN_TAG` triggers the prefix-based sweep that catches everything.)
2. Investigate why the script crashed. The cleanup scripts use `try/except` per-resource so one bad object shouldn't take down the whole sweep — if the entire script crashed, it's likely an auth or import failure in the script itself.
3. If the cause is a real bug in the cleanup script, fix it in a follow-up PR.

The Layer-3 daily orphan-sweep cron is the safety net; it catches anything older than 24h, so cost doesn't run away even if a manual sweep is delayed.

## Rotating provider credentials

### Snowflake

1. Log into the Snowflake test account as an admin.
2. Generate a new password for the `FORGE_CI_USER` user.
3. Update the `SNOWFLAKE_PASSWORD` secret in GitHub repo settings → Secrets and variables → Actions.
4. Trigger `integration.yml` manually to verify the new password works.
5. Revoke the old password.

Do this every 90 days at minimum, or immediately if you suspect compromise.

### BigQuery

The BigQuery integration uses Workload Identity Federation, so there's no static key to rotate. The short-lived OIDC token is freshly minted per run.

To revoke access, remove the GitHub Actions identity from the WIF pool's trust policy in GCP Console → IAM → Workload Identity Federation.

### AWS

The AWS integration uses OIDC role assumption, so there's no static key to rotate. The temporary credentials are freshly minted per run.

To revoke access, edit the IAM role's trust policy to remove the GitHub Actions principal.

## The release flow

forge-cli's `release.yml` is already wired with:

- Trusted Publisher OIDC for both TestPyPI and PyPI (no long-lived tokens)
- TestPyPI auto-publish on every tag, then `verify-testpypi` smoke test
- Real PyPI publish gated by the TestPyPI install check, through `environment: pypi`
  (the environment has no required reviewers today, so nothing waits for an approval)
- Sigstore attestations + SLSA build provenance on every artifact
- Pre-release tags (containing `a/b/rc/dev`) promote to PyPI too, as PEP 440 pre-releases
  that `pip install` skips unless asked for one
- The GHCR image and the GitHub Release only once PyPI has accepted the upload: when
  `publish-pypi` fails, neither is published

To cut a release:

```bash
# Pre-release for validation
git tag v0.7.8a1
git push origin v0.7.8a1
# → TestPyPI, its install check, then PyPI as a pre-release
# → then the GHCR image's alpha-latest tag and a GitHub pre-release

# Once the pre-release checks out, cut the stable
git tag v0.7.8
git push origin v0.7.8
# → TestPyPI, its install check, then PyPI; nothing waits for an approval
# → then the GHCR image (0.7.8, 0.7, stable-latest, latest) and the GitHub Release
```

The `quality-gate` job at the start of `release.yml` checks that the tag matches `vX.Y.Z` and that `ci.yml` is green on the tagged commit. On a manual run it also resolves the tag to its commit and checks that the ref the run started from contains that commit. It does NOT re-run lint or pytest — `ci.yml` is the source of truth for those.

### When TestPyPI is down

A TestPyPI incident (for example its trusted-publishing endpoint answering 503) fails
`publish-testpypi`, and nothing reaches PyPI. Re-run the failed jobs once TestPyPI is
back. If it stays down, publish the existing tag straight to PyPI from a manual run
started from the tag itself:

```bash
gh workflow run release.yml --ref vX.Y.Z -f tag=vX.Y.Z -f skip_testpypi=true
```

The run skips the TestPyPI upload and `verify-testpypi`; `verify-pypi` is then the only
install check from an index. Every job builds and publishes the tag's commit, and the
quality gate checks `ci.yml` on that commit. A tag push never skips TestPyPI.

The provenance names the commit the run started from, not the one it built. The SLSA
build provenance and PyPI's Sigstore attestations are made from the run's OIDC claims,
its `ref` and `sha`, not from the checkout. Starting from the tag makes the two agree.

A run uses the `release.yml` of the ref it starts from, so `--ref vX.Y.Z` works only for
a tag whose own `release.yml` has `skip_testpypi`: every tag after 63f3b40 (#702). For
v0.19.0 and older, start from `main`:

```bash
gh workflow run release.yml --ref main -f tag=vX.Y.Z -f skip_testpypi=true
```

The provenance then names `main` at its head when the run started, and the quality gate
says so in a warning. The gate refuses a start ref that does not contain the tag's
commit, such as `main` for a tag on another branch, or a ref older than the tag. It also
refuses a tag that does not exist, and names it.

## Provisioning a new cloud test account

When standing up Tier-2 integration for a new provider (e.g. Azure when its provider lands), follow this checklist:

1. **Create a dedicated test account** isolated from any production workload. Tag it `purpose=forge-ci`.
2. **Set a hard billing alert** at $50/month with email notifications. Configure auto-disable if available.
3. **Provision a service principal / IAM role** with the minimum privileges the integration tests need. Document the privileges in `INTEGRATION_TESTING.md`.
4. **Configure OIDC federation** if the provider supports it. Otherwise generate a long-lived credential and add it to GitHub repo secrets.
5. **Add a new job** to `integration.yml` matching the existing Snowflake / BigQuery / AWS shape: setup-python, install extras, configure auth, run tests, `if: always()` cleanup.
6. **Write a cleanup script** in `scripts/cleanup_<provider>_test_artifacts.py` modeled on the existing ones.
7. **Write a live test file** in `tests/providers/test_<provider>_live_happy_path.py` modeled on the existing ones.
8. **Update the orphan-sweep daily cron** to also clean the new provider.
9. **Update `INTEGRATION_TESTING.md`** with the new provider's local-run instructions and cost expectations.
10. **Commit, open a PR, push the PR's branch to `staging/PR-N`** to validate the new job before merge.

## Decommissioning

If forge-cli ever needs to drop integration testing for a provider (e.g. provider deprecated, account closed):

1. Remove the job from `integration.yml`.
2. Move the `tests/providers/test_<provider>_live_*.py` files out of the test path (e.g. to `tests/_archive/`).
3. Remove the cleanup script.
4. Revoke the GitHub repo secret(s) for the provider.
5. Update `INTEGRATION_TESTING.md` to reflect the change.
6. Close the test account at the provider.

Do this in the order listed — secret revocation last — so an in-flight workflow doesn't suddenly fail mid-cleanup.
