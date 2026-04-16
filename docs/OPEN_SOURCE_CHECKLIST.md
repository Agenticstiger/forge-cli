# Open-Sourcing Fluid Forge CLI — Step-by-Step Checklist

This document walks through every step required to take the `forge-cli`
project from its current private state to a public repository under the
**Agenticstiger** GitHub organisation.

**Target repository:** `https://github.com/Agenticstiger/forge-cli`

---

## Table of Contents

1. [GitHub Organisation Setup](#1-github-organisation-setup)
2. [Create the Repository](#2-create-the-repository)
3. [Update All Internal References](#3-update-all-internal-references)
4. [Secret & Credential Audit](#4-secret--credential-audit)
5. [License & Legal Review](#5-license--legal-review)
6. [Configure Branch Protection](#6-configure-branch-protection)
7. [Set Up GitHub Actions CI](#7-set-up-github-actions-ci)
8. [Set Up PyPI Publishing (Trusted Publisher)](#8-set-up-pypi-publishing-trusted-publisher)
9. [Set Up Docker Publishing (GHCR)](#9-set-up-docker-publishing-ghcr)
10. [GitHub Environments](#10-github-environments)
11. [Community Files & Templates](#11-community-files--templates)
12. [Publish First Release](#12-publish-first-release)
13. [Post-Launch Checklist](#13-post-launch-checklist)

---

## 1. GitHub Organisation Setup

> If `Agenticstiger` already exists, skip to step 2.

- [ ] Go to <https://github.com/organizations/plan> and create the **Agenticstiger** organisation.
- [ ] Under **Settings → Member privileges**, set default repo permission to **Read**.
- [ ] Create teams (optional but recommended):

  | Team | Purpose | Members |
  |------|---------|---------|
  | `core` | Full write to all repos | Maintainers |
  | `providers` | Write to provider directories | Provider contributors |
  | `security` | Reviews of auth / secrets code | Security reviewers |

- [ ] Add a profile README at `Agenticstiger/.github/profile/README.md`.
- [ ] Set org-level Actions permissions: **Settings → Actions → General**
  - Allow GitHub Actions for all repositories
  - Allow actions created by GitHub and verified creators

---

## 2. Create the Repository

- [ ] Create **`forge-cli`** under `Agenticstiger`:
  - **Visibility:** Public
  - **Description:** `Fluid Forge CLI — plan, apply, and visualize data products across providers`
  - **No** initialise with README (we're pushing existing code)
  - License: Apache 2.0 (already in the codebase)

- [ ] Add from local machine:
  ```bash
  cd forge-cli

  # Add the new public remote
  git remote add public git@github.com:Agenticstiger/forge-cli.git

  # Push main branch
  git push public main

  # Push tags (if any)
  git push public --tags
  ```

- [ ] Set **`main`** as the default branch.
- [ ] **Topics** (for discoverability): `data-products`, `cli`, `python`, `gitops`, `data-engineering`, `duckdb`, `gcp`, `snowflake`, `dbt`

---

## 3. Update All Internal References

Before pushing, do a find-and-replace across the codebase. Every reference to
the old org/repo name needs updating.

### 3a. Files to update

| File | What to change |
|------|----------------|
| `pyproject.toml` | `[project.urls]` — Repository, Issues |
| `README.md` | Badge URLs, discussion links, clone URLs |
| `CONTRIBUTING.md` | Issue/PR links |
| `SECURITY.md` | Reporting URL (if GitHub-based) |
| `.github/CODEOWNERS` | Team references → `@jeffwatson-ai` etc. |
| `.github/workflows/*.yml` | Any hardcoded repo references |
| `Dockerfile` | `org.opencontainers.image.source` label |
| `Jenkinsfile` | Leave as-is (internal CI) or remove from public repo |

### 3b. Automated rename

```bash
# Preview changes first
grep -rn "Agenticstiger/forge-cli" --include="*.py" --include="*.yml" \
  --include="*.yaml" --include="*.toml" --include="*.md" --include="Dockerfile"

# Apply (macOS/Linux)
find . -type f \( -name "*.py" -o -name "*.yml" -o -name "*.yaml" \
  -o -name "*.toml" -o -name "*.md" -o -name "Dockerfile" \) \
  -exec sed -i 's|Agenticstiger/forge-cli|Agenticstiger/forge-cli|g' {} +
```

### 3c. pyproject.toml URLs (target state)

```toml
[project.urls]
Homepage = "https://agenticstiger.github.io/forge_docs/"
Repository = "https://github.com/Agenticstiger/forge-cli"
Issues = "https://github.com/Agenticstiger/forge-cli/issues"
Documentation = "https://agenticstiger.github.io/forge_docs/"
```

### 3d. CODEOWNERS (target state)

```
*                               @jeffwatson-ai
fluid_build/cli/                @jeffwatson-ai
fluid_build/providers/aws/      @jeffwatson-ai
fluid_build/providers/gcp/      @jeffwatson-ai
fluid_build/providers/snowflake/ @jeffwatson-ai
fluid_build/providers/local/    @jeffwatson-ai
fluid_build/credentials/        @jeffwatson-ai
fluid_build/secrets.py          @jeffwatson-ai
fluid_build/auth.py             @jeffwatson-ai
```

---

## 4. Secret & Credential Audit

**This is the most critical step.** Public repos expose full git history.

- [ ] Run `detect-secrets` against the repo:
  ```bash
  pip install detect-secrets
  detect-secrets scan --all-files > .secrets.baseline
  detect-secrets audit .secrets.baseline
  ```
- [ ] Verify `.env` and `.env.example` are in `.gitignore` (they are).
- [ ] Search history for leaked secrets:
  ```bash
  # Check for common patterns in git history
  git log --all -p | grep -iE "(password|secret|api[_-]?key|token)" | head -50
  ```
- [ ] If secrets are found in history, consider:
  - Rewriting history with `git filter-repo` (preferred), or
  - Starting a clean repo from a squashed commit
- [ ] Rotate any credentials that may have been committed.
- [ ] Remove or `.gitignore` files that shouldn't be public:

  | File | Action |
  |------|--------|
  | `.env` | Already in `.gitignore` — verify |
  | `Jenkinsfile` | Remove from public repo (internal CI) |
  | `.gitlab-ci.yml` | Remove from public repo (internal CI) |
  | `.secrets.baseline` | Keep (detect-secrets config, no actual secrets) |
  | `runtime/` | Add to `.gitignore` (local apply state) |
  | `.coverage` | Add to `.gitignore` |

---

## 5. License & Legal Review

- [ ] Confirm Apache 2.0 `LICENSE` file is present and correct.
- [ ] Verify `NOTICE` file lists the correct copyright holder:
  ```
  Copyright 2024-2026 Agentics Transformation Pty Ltd
  ```
- [ ] Run the license header check:
  ```bash
  python scripts/add_license_headers.py
  ```
- [ ] Review `THIRD_PARTY_LICENSES.md` — ensure all dependencies are listed.
- [ ] Confirm `pyproject.toml` specifies `license = { text = "Apache-2.0" }`.

---

## 6. Configure Branch Protection

In **Settings → Branches → Add rule** for `main`:

- [ ] **Require a pull request before merging**
  - Required approving reviews: 1
  - Dismiss stale reviews on new pushes
- [ ] **Require status checks to pass**
  - Required checks: `Lint & Format`, `Test (Python 3.12)`, `Security Scan`, `Build Smoke Test`
- [ ] **Require conversation resolution**
- [ ] **Do not allow bypassing the above settings** (even for admins)
- [ ] **Restrict pushes** — only `core` team can push to `main`

---

## 7. Set Up GitHub Actions CI

The following workflow files should already be in `.github/workflows/`:

| File | Trigger | Purpose |
|------|---------|---------|
| `ci.yml` | Push/PR to `main` | Lint, test matrix (3.9-3.12), security scan, license check, build smoke test |
| `build-profiles.yml` | Push to `main` | Build alpha/beta/stable wheels, upload as artifacts |
| `release.yml` | Tag `v*.*.*` or manual | Publish to PyPI/TestPyPI, GitHub Release, Docker to GHCR |

**Verify workflows reference the correct repo:**
```bash
grep -rn "Agenticstiger\|fluid-forge-cli" .github/workflows/
```

All references should point to `Agenticstiger/forge-cli`.

---

## 8. Set Up PyPI Publishing (Trusted Publisher)

This uses OIDC — **no API tokens needed**. GitHub Actions authenticates directly
with PyPI.

### 8a. Register on PyPI

- [ ] Go to <https://pypi.org> → log in → **Your projects** → **Publishing** →
      **Add a new pending publisher** (if the package doesn't exist yet).

  | Field | Value |
  |-------|-------|
  | PyPI project name | `data-product-forge` |
  | Owner | `Agenticstiger` |
  | Repository | `forge-cli` |
  | Workflow name | `release.yml` |
  | Environment name | `pypi` |

- [ ] Click **Add**.

### 8b. Register on TestPyPI

- [ ] Go to <https://test.pypi.org> → same process:

  | Field | Value |
  |-------|-------|
  | PyPI project name | `data-product-forge` |
  | Owner | `Agenticstiger` |
  | Repository | `forge-cli` |
  | Workflow name | `release.yml` |
  | Environment name | `testpypi` |

- [ ] Click **Add**.

### 8c. How it works

When `release.yml` runs, the `pypa/gh-action-pypi-publish` action uses GitHub's
OIDC token to authenticate with PyPI. No secrets to manage or rotate.

```
Tag push (v0.7.7) → release.yml → quality-gate → build → publish-pypi
                                                        → github-release
                                                        → docker (GHCR)
```

---

## 9. Set Up Docker Publishing (GHCR)

GHCR uses `GITHUB_TOKEN` — no additional setup needed for public repos.

- [ ] Verify the `Dockerfile` exists in the repo root.
- [ ] First time only: after the first image push, go to the package settings
      and set visibility to **Public**:
  - `https://github.com/orgs/Agenticstiger/packages/container/forge-cli/settings`
- [ ] Users will pull with:
  ```bash
  docker pull ghcr.io/agenticstiger/forge-cli:latest
  docker pull ghcr.io/agenticstiger/forge-cli:0.7.7
  docker pull ghcr.io/agenticstiger/forge-cli:alpha-latest
  ```

---

## 10. GitHub Environments

Create two environments for release gating:

### Environment: `pypi`
- [ ] **Settings → Environments → New environment** → `pypi`
- [ ] Add **required reviewers** (at least 1 maintainer must approve)
- [ ] Set **deployment branch rule**: only `main` branch / tags matching `v*`

### Environment: `testpypi`
- [ ] **Settings → Environments → New environment** → `testpypi`
- [ ] No required reviewers (pre-releases can publish freely)
- [ ] Deployment branch rule: only `main` branch / tags matching `v*`

---

## 11. Community Files & Templates

These should already exist — verify they're up to date:

- [ ] `README.md` — updated badges pointing to new repo
  ```markdown
  [![CI](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml)
  [![PyPI](https://img.shields.io/pypi/v/data-product-forge)](https://pypi.org/project/fluid-forge/)
  [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
  ```
- [ ] `CONTRIBUTING.md` — links to new repo issues/PRs
- [ ] `CODE_OF_CONDUCT.md` — already present (Contributor Covenant)
- [ ] `SECURITY.md` — verify contact email / reporting process
- [ ] `.github/ISSUE_TEMPLATE/` — already has:
  - `bug_report.yml`
  - `feature_request.yml`
  - `provider_request.yml`
- [ ] `.github/pull_request_template.md` — already present
- [ ] **Discussions** — enable via **Settings → General → Features → Discussions**

---

## 12. Publish First Release

Once everything above is done:

### 12a. Test the pipeline end-to-end

```bash
# 1. Push a pre-release tag to test the full pipeline
git tag v0.7.7a1
git push public v0.7.7a1

# 2. Watch the release workflow:
#    https://github.com/Agenticstiger/forge-cli/actions/workflows/release.yml

# 3. Verify TestPyPI:
pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ data-product-forge==0.7.7a1

# 4. Verify GHCR:
docker pull ghcr.io/agenticstiger/forge-cli:0.7.7a1

# 5. Verify GitHub Release exists with wheel attached
```

### 12b. Publish the stable release

```bash
# Only after the pre-release pipeline succeeds:
git tag v0.7.7
git push public v0.7.7

# This will:
#   - Run quality gate (lint, test, security)
#   - Build wheel + sdist
#   - Publish to PyPI (requires reviewer approval via pypi environment)
#   - Create GitHub Release with changelog
#   - Build and push Docker image tagged 0.7.7 + latest
```

### 12c. Verify

```bash
# PyPI
pip install data-product-forge
fluid --version

# Docker
docker run --rm ghcr.io/agenticstiger/forge-cli:latest --version
```

---

## 13. Post-Launch Checklist

- [ ] **Announce** — post to relevant channels (blog, social, HN, Reddit r/dataengineering)
- [ ] **Codecov** — connect at <https://app.codecov.io/gh/Agenticstiger/forge-cli>
- [ ] **PyPI project page** — add description, URLs, and classifiers (pulled from `pyproject.toml`)
- [ ] **GitHub repo settings**:
  - Add social preview image (use the Fluid Forge logo)
  - Pin the repo to the organisation profile
- [ ] **Dependabot** — create `.github/dependabot.yml`:
  ```yaml
  version: 2
  updates:
    - package-ecosystem: pip
      directory: /
      schedule:
        interval: weekly
    - package-ecosystem: github-actions
      directory: /
      schedule:
        interval: weekly
  ```
- [ ] **Remove internal-only files** from the public repo if not already done:
  - `Jenkinsfile` (internal CI)
  - `.gitlab-ci.yml` (internal CI)
  - `runtime/` contents (local state)
  - `daily_context_store/` (build logs)
- [ ] **Set up branch protection** for release tags (optional):
  - Tag protection rule: `v*` — only `core` team can create

---

## Quick Reference: Before & After

| Item | Before (private) | After (public) |
|------|-------------------|----------------|
| **Org** | `Agenticstiger` | `Agenticstiger` |
| **Repo** | `fluid-forge-cli` | `forge-cli` |
| **Full URL** | `github.com/Agenticstiger/forge-cli` | `github.com/Agenticstiger/forge-cli` |
| **CI** | Jenkins + GitHub Actions | GitHub Actions only |
| **PyPI** | Private NAS PyPI | pypi.org (Trusted Publisher) |
| **Docker** | `localhost:5000` | `ghcr.io/agenticstiger/forge-cli` |
| **Artifacts** | Git-based NAS repo | GitHub Actions artifacts + Releases |
| **Package name** | `data-product-forge` | `data-product-forge` (unchanged) |
| **CLI command** | `fluid` | `fluid` (unchanged) |
