# Contributing to FLUID Forge

Thank you for your interest in contributing to FLUID Forge! This guide will help you get started.

## Code of Conduct

By participating in this project you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

## How to Contribute

### Reporting Bugs

1. Search [existing issues](https://github.com/Agenticstiger/forge-cli/issues) to avoid duplicates.
2. Open a new issue using the **Bug Report** template.
3. Include:
   - FLUID Forge version (`fluid version`)
   - Python version (`python3 --version`)
   - Operating system
   - Steps to reproduce
   - Expected vs actual behaviour
   - Contract YAML (redact any secrets)

### Suggesting Features

1. Open an issue using the **Feature Request** template.
2. Describe the problem you'd like to solve, not just the solution.
3. If the feature involves a new provider, use the **Provider Request** template instead.

### Branching Strategy

All contributions target the `main` branch. Use the following branch name prefixes:

| Prefix | Use for | Example |
|--------|---------|---------|
| `feat/` | New features | `feat/databricks-provider` |
| `fix/` | Bug fixes | `fix/validate-empty-contract` |
| `docs/` | Documentation changes (in this repo) | `docs/update-quickstart` |
| `provider/` | New or updated providers | `provider/azure-support` |
| `refactor/` | Code cleanup, no behaviour change | `refactor/simplify-loader` |
| `chore/` | CI, dependencies, maintenance | `chore/bump-ruff-version` |
| `test/` | Test improvements | `test/add-provider-registry-tests` |

### CI Gates on `main`

All PRs to `main` must pass these checks before merge:

| Check | What it does |
|-------|-------------|
| **Lint & Format** | `ruff check` + `black --check` (Python 3.12) |
| **Test Matrix** | `pytest` on Python 3.10, 3.11, 3.12, 3.13, 3.14 (randomized order) |
| **DuckDB Integration** | Full `fluid init → validate → plan` end-to-end against the local DuckDB provider — runs on every PR including from forks (free, no secrets). See [`docs/INTEGRATION_TESTING.md`](docs/INTEGRATION_TESTING.md). |
| **Coverage Gates** | Core 80%, local providers 50%, cloud providers 20% (Python 3.12) |
| **Security Scan** | `bandit` with medium severity threshold |
| **Build Smoke Test** | Wheel build + install verification |
| **License Headers** | All maintained `.py` files except `examples/**` must have Apache 2.0 header |
| **actionlint** | Validates `.github/workflows/*.yml` and refuses any change that would let fork PRs read cloud secrets |
| **Docs Reminder** | Soft check — adds `needs-docs` label if no docs reference |

PRs also require at least **1 approving review** from a [CODEOWNER](https://github.com/Agenticstiger/forge-cli/blob/main/.github/CODEOWNERS).

### Cloud-touching changes (Snowflake / BigQuery / AWS)

Cloud-provider integration tests do **not** run on community PRs — running them would let any contributor incur cloud cost on the project's accounts. Instead:

- **Pre-merge**: a maintainer pushes the PR's commits to a `staging/PR-N` branch on the upstream repo, which triggers `integration.yml` against real cloud accounts. The maintainer comments the run result on the PR.
- **Post-merge**: `integration.yml` re-runs automatically on `main` to catch any regression that slipped through review.
- **Nightly**: a cron runs the full integration suite to detect external API drift.

If you're contributing changes to `fluid_build/providers/{snowflake,gcp,aws}/`, you can validate them locally against your own cloud account before opening the PR. See [`docs/INTEGRATION_TESTING.md`](docs/INTEGRATION_TESTING.md) for the env vars and cost expectations per provider.

Local DuckDB integration runs on every PR automatically — no maintainer action needed.

### Submitting Code

1. **Fork** the repository and create a branch from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Install** in development mode:
   ```bash
   pip install -e ".[dev,local]"
   ```

3. **Make your changes.** Follow the coding standards below.

4. **Add tests.** All new code must include tests. Run the suite:
   ```bash
   pytest
   ```

5. **Lint and format:**
   ```bash
   ruff check fluid_build/ tests/
   black fluid_build/ tests/
   ```

6. **Add license headers** to any new maintained Python files (`fluid_build/`, `tests/`, `scripts/`, `tools/`; `examples/**` is exempt illustrative code):
   ```bash
   python scripts/add_license_headers.py
   ```

7. **Commit** with a clear message following [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat(provider): add Databricks provider skeleton
   fix(cli): handle empty contract in validate
   docs: update quickstart for v0.8
   ```

8. **Address documentation** — see [Documentation Requirements](#documentation-requirements) below.

9. **Push** and open a Pull Request against `main`.

## Documentation Requirements

We maintain documentation in a **separate repository**: [Agenticstiger/forge_docs](https://github.com/Agenticstiger/forge_docs).

Every PR that changes user-facing behaviour must be paired with a documentation update. When you open a PR, the template asks you to choose one of:

1. **Link a docs PR** — open a companion PR in [forge_docs](https://github.com/Agenticstiger/forge_docs) and paste the link in your CLI PR description.
2. **No docs needed** — check the box and provide a justification (e.g. internal refactor, test-only change, CI config update).
3. **Docs TODO** — acknowledge that docs are needed and commit to creating the docs PR before your CLI PR is merged.

A GitHub Actions workflow (`docs-reminder`) will automatically label your PR with `needs-docs` if none of these are addressed.

### What counts as needing docs?

- New CLI commands or flags
- Changed command behaviour or output
- New or updated providers
- Configuration changes
- Breaking changes
- New environment variables or setup steps

### What does NOT need docs?

- Internal refactors with no behaviour change
- Test-only changes
- CI/CD configuration updates
- Dependency bumps

### How to create a docs PR

The documentation site lives in a separate repo. To create a companion docs PR:

1. Fork and clone [Agenticstiger/forge_docs](https://github.com/Agenticstiger/forge_docs).
2. Create a branch with the same name as your CLI branch (e.g. `feat/databricks-provider`).
3. Make your documentation changes (Markdown files under `docs/`).
4. Preview locally: `pip install mkdocs-material && mkdocs serve`
5. Push and open a PR against `main` in forge_docs.
6. Paste the docs PR link in your CLI PR description under the "Documentation" section.

**Tip:** open the docs PR as a draft first, then finalize it alongside your CLI PR review.

## Coding Standards

- **Python 3.10+** — no walrus operators in hot paths, use `from __future__ import annotations` sparingly.
- **Type hints** on all public function signatures.
- **Logging** — use `logging.getLogger(__name__)` in production code, never bare `print()`.
- **No bare `except:`** — always catch specific exceptions.
- **Tests** — use `pytest`. Place unit tests in `tests/`, provider integration tests under `tests/providers/`.
- **Imports** — standard library → third-party → local, separated by blank lines. Use `ruff` to auto-sort.

## Testing Best Practices

### Coverage Gates

CI enforces a **three-tier coverage strategy**:

| Gate | Threshold | What's included |
|------|-----------|-----------------|
| **Core framework** | 80% | All `fluid_build/` except providers and `provider_action_executor.py` |
| **Local providers** | 50% | Providers that don't need cloud credentials (local, catalogs, ODCS, etc.) |
| **Cloud providers** | 20% | Providers requiring cloud credentials (AWS, GCP, Snowflake, etc.) |

Run coverage locally before pushing:

```bash
# Full test suite with coverage
pytest --cov=fluid_build --cov-report=term-missing -q

# Check core gate
coverage report --fail-under=80 \
  --omit="fluid_build/providers/*,fluid_build/cli/provider_action_executor.py"
```

### Writing Good Tests

**Test behaviour, not implementation.** A test should verify *what* a function produces, not *how* it does it internally. If a test breaks when you refactor without changing behaviour, the test is too tightly coupled.

```python
# BAD: tests implementation details (which internal function was called)
def test_validate_calls_schema_checker(self):
    with patch("fluid_build.cli.validate._check_schema") as mock:
        validate(contract)
    mock.assert_called_once()

# GOOD: tests observable behaviour (return value, side effects)
def test_validate_returns_errors_for_invalid_contract(self):
    result = validate(invalid_contract)
    assert result.is_valid is False
    assert "missing required field" in result.errors[0].message
```

**Use `@pytest.mark.parametrize` for variant testing.** If you're writing multiple test methods that differ only by input/output, combine them:

```python
# BAD: 5 copy-paste methods
def test_infer_bool(self):
    assert infer_type("bool") == "boolean"
def test_infer_int(self):
    assert infer_type("int64") == "integer"

# GOOD: 1 parametrized test
@pytest.mark.parametrize("input_type,expected", [
    ("bool", "boolean"),
    ("int64", "integer"),
    ("float32", "number"),
    ("timestamp", "datetime"),
    ("utf8", "string"),
])
def test_infer_type(self, input_type, expected):
    assert infer_type(input_type) == expected
```

**Mock only at boundaries.** Use real objects when they're cheap (dataclasses, simple classes). Reserve mocking for:
- Network calls (HTTP, gRPC)
- File system operations
- External SDKs (boto3, google-cloud, snowflake-connector)
- Time-dependent logic (`time.time()`, `datetime.now()`)

**Use helper factories for test data:**

```python
def _make_contract(name="test", version="1.0", **overrides):
    defaults = {"id": name, "version": version, "spec": "v1"}
    defaults.update(overrides)
    return defaults
```

### Async Tests

Use this manual async handling pattern for compatibility with the lightweight test helpers:

```python
import asyncio

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

def test_async_function(self):
    result = _run(my_async_function())
    assert result == expected
```

### Test Isolation

Tests run in **randomized order** (via `pytest-randomly`). Every test must:
- Clean up any files it creates (use `tmp_path` fixture)
- Not depend on execution order
- Not leak global state (environment variables, module-level caches)

If a test fails only when run in the full suite, it has a hidden dependency on test ordering.

## Provider Contributions

If you're building a new provider:

1. Read the [Provider SDK documentation](https://agenticstiger.github.io/forge_docs/providers/).
2. Subclass `BaseProvider` from `fluid_provider_sdk`.
3. Register via entry points in your `pyproject.toml`:
   ```toml
   [project.entry-points."fluid_build.providers"]
   my_provider = "my_package:MyProvider"
   ```
4. Include at least one working example contract.
5. See the [provider documentation](https://agenticstiger.github.io/forge_docs/providers/) for internals.

## Adding a Catalog Adapter

Catalog adapters are the **source-side** complement to providers:
they pull metadata FROM an existing catalog (Snowflake Horizon,
Databricks Unity, BigQuery, Glue, DataHub, etc.) and feed it into
forge-cli's staged pipeline. Each adapter is roughly 200 LOC and
follows nine reusable patterns documented in
`fluid_build/copilot/catalog/_patterns.py`.

A community contributor with a weekend can ship a new adapter.
Here's the path.

### 1. Decide what to wrap

Pick a catalog with a Python SDK (e.g. Apache Atlas's `atlasclient`,
Alation's REST API, OpenMetadata's `python-client`). The adapter is
typically a thin wrapper around 4-6 SDK calls.

### 2. Implement the ABC

The interface is `fluid_build.copilot.catalog.base.CatalogAdapter`:

```python
from fluid_build.copilot.catalog.base import CatalogAdapter
from fluid_build.copilot.catalog.models import (
    CatalogTable, CatalogColumn, CatalogLineage, GlossaryTerm, CatalogScope,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver, AtlasCredentials,    # add to credentials.py
)


class AtlasCatalogAdapter(CatalogAdapter):
    name = "atlas"

    def __init__(self, *, credentials: AtlasCredentials) -> None:
        self.credentials = credentials
        self._client = None  # lazy-initialised in _client()

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: str | None = None,
        inline_credentials: dict | None = None,
    ) -> "AtlasCatalogAdapter":
        creds = resolver.resolve(
            "atlas",
            credential_id=credential_id,
            inline=inline_credentials,
            credential_type=AtlasCredentials,
        )
        return cls(credentials=creds)

    def _client(self):
        # Lazy SDK import — keeps `fluid --help` fast and lets users
        # `pip install data-product-forge` without atlasclient.
        if self._client is None:
            try:
                import atlasclient  # type: ignore[import-not-found]
            except ImportError as exc:
                raise CatalogConfigError(
                    "atlasclient missing. Install with: "
                    'pip install "data-product-forge[atlas]"',
                    suggestions=['pip install "data-product-forge[atlas]"'],
                ) from exc
            self._client = atlasclient.Atlas(...)
        return self._client

    def list_tables(self, scope: CatalogScope) -> list[CatalogTable]: ...
    def get_table(self, fqn: str) -> CatalogTable: ...
    def get_lineage(self, fqn: str) -> CatalogLineage: ...
    def list_glossary_terms(self, scope: CatalogScope) -> list[GlossaryTerm]: ...

    def audit_context(self) -> dict:
        # Non-secret fields only — credentials NEVER appear here.
        return {
            "catalog_name": self.name,
            "host": self.credentials.host,
        }
```

### 3. Honour the nine patterns

Read `fluid_build/copilot/catalog/_patterns.py`. Reuse the helpers:

- `validate_and_quote_identifier(...)` — defends against SQL injection
  in identifier strings.
- `safe_metadata_call(...)` — soft-fails on optional reads
  (lineage / glossary / tags) so a forge isn't blocked when the
  user lacks one privilege.
- `translate_permission_or_connection_error(...)` — maps
  vendor-specific exceptions into typed `CatalogPermissionError` /
  `CatalogConnectionError` with `suggestions: list[str]` carrying
  the next-action.

### 4. Add the credential class

Edit `fluid_build/copilot/catalog/credentials.py`:

```python
class AtlasCredentials(BaseModel):
    host: str                                 # non-sensitive
    auth_method: Literal["pat", "basic"] = "pat"
    token: SecretStr | None = None            # SecretStr for safety
    username: str | None = None
    password: SecretStr | None = None
```

The `CredentialResolver` chain (inline → keyring → sources.yaml →
env vars) is built-in; just register the env-var names in the
resolver's per-catalog table.

### 5. Register the optional extra

Edit `pyproject.toml`:

```toml
[project.optional-dependencies]
atlas = ["atlasclient>=1.2"]
catalogs = [..., "atlasclient>=1.2"]
```

### 6. Wire the dispatch

Two register sites — both already have an `_SOURCE_ADAPTERS` dict:

- `fluid_build/cli/forge_data_model.py::_build_catalog_adapter` —
  dispatches `--source-type atlas` from the CLI.
- `fluid_build/cli/mcp.py::_SOURCE_ADAPTERS` — dispatches the MCP
  `forge_from_source` tool.

Add one entry to each:

```python
"atlas": AtlasCatalogAdapter,
```

### 7. Write the test file

`tests/copilot/catalog/test_catalog_adapter_atlas.py`. Stub the SDK
via `sys.modules` so the adapter can be tested without
`atlasclient` installed:

```python
@pytest.fixture
def atlas_sdk_stub(monkeypatch):
    fake = ModuleType("atlasclient")
    fake.Atlas = MagicMock(name="atlasclient.Atlas")
    monkeypatch.setitem(sys.modules, "atlasclient", fake)
    yield fake.Atlas
```

The 5 existing adapter test files
(`test_catalog_adapter_{bigquery,dataplex,glue,datahub,dmm}.py`)
are templates — copy the closest one and edit. Every adapter
ships with classes for: `TestFromResolver`, `TestLazyImport`,
`TestAuditContext`, `TestListTables`, `TestErrorTranslation`,
plus catalog-specific tests.

### 8. Pin the public API

Add the new class to `tests/test_public_api_stability.py`:

```python
("fluid_build.copilot.catalog.atlas", "AtlasCatalogAdapter"),
("fluid_build.copilot.catalog", "AtlasCredentials"),
```

This stops a future refactor from silently renaming or removing
your adapter.

### 9. Document it

Add a page to the `forge_docs` repo:
`forge_docs/docs/cli/catalogs/atlas.md`. Use any of the existing
catalog pages as a template — they share the same structure
(install, privileges, auth methods, setup, end-to-end demo, what
lands where, common errors).

Then add a row to the catalog index table at
`forge_docs/docs/cli/catalogs/README.md`.

### 10. Submit a PR

Branch name: `feat/atlas-catalog-adapter`.

The PR template asks for:
- 5+ tests passing in `tests/copilot/catalog/`
- Public API entry added (test_public_api_stability.py)
- Docs page in `forge_docs/`
- A redacted INFORMATION_SCHEMA snippet showing what your adapter
  reads (helps reviewers verify the read-only contract).

CI runs the full forge-cli test suite plus the per-adapter tests
you wrote. Coverage gate is ≥80% on `fluid_build/copilot/catalog/`.

## Development Setup

```bash
# Clone
git clone https://github.com/Agenticstiger/forge-cli.git
cd forge-cli

# Create virtualenv (recommended)
python3 -m venv .venv && source .venv/bin/activate

# Install with all dev + provider extras
pip install -e ".[dev,local,gcp,snowflake,viz]"

# Run tests
pytest

# Run a single test file
pytest tests/providers/test_registry.py -v
```

## Contributor License Agreement (CLA)

By submitting a pull request, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE) and that you have the right to license them.

## Your First Contribution

New to FLUID Forge? Here's how to get started:

1. Browse issues labelled [`good first issue`](https://github.com/Agenticstiger/forge-cli/labels/good%20first%20issue) for beginner-friendly tasks.
2. Comment on the issue to let maintainers know you'd like to work on it.
3. Fork the repo, create a branch following the [naming conventions](#branching-strategy), and make your changes.
4. Open a PR — our welcome bot will guide you through the checklist.
5. A maintainer will review your PR and provide feedback.

### What makes a good first issue?

Maintainers tag issues as `good first issue` when they meet these criteria:
- Scope is limited to 1–2 files
- No deep domain knowledge required (e.g. provider internals, policy engine)
- Clear acceptance criteria in the issue description
- Existing tests can be used as a pattern

If you find a bug or improvement that fits these criteria, feel free to suggest the `good first issue` label in a comment.

Don't be afraid to ask questions! Open a [Discussion](https://github.com/Agenticstiger/forge-cli/discussions) if you need help.

## Recognition

We value every contribution. Contributors are recognised in:

- **Release notes** — your name and PR are included in the [CHANGELOG](CHANGELOG.md) for each release.
- **Git history** — all commits preserve author attribution.
- **GitHub contributors page** — [see all contributors](https://github.com/Agenticstiger/forge-cli/graphs/contributors).

Repeat contributors may be invited to join a maintainer team with write access.

## Getting Help

- **Discussions:** [GitHub Discussions](https://github.com/Agenticstiger/forge-cli/discussions)
- **Bugs:** [Issue Tracker](https://github.com/Agenticstiger/forge-cli/issues)
- **Docs:** [agenticstiger.github.io/forge_docs](https://agenticstiger.github.io/forge_docs/)

Thank you for helping make FLUID Forge better!
