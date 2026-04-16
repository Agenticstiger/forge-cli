## Description

<!-- What provider does this PR add or modify? -->

Closes #

## Provider Details

- **Provider name:** <!-- e.g. databricks, azure -->
- **Change type:**
  - [ ] New provider
  - [ ] Enhancement to existing provider
  - [ ] Bug fix for existing provider

## Documentation

<!-- Every provider change needs documentation. Choose ONE of the options below. -->

- [ ] **Docs PR linked:**
  - Docs PR: <!-- e.g. https://github.com/Agenticstiger/forge_docs/pull/123 -->
- [ ] **Docs TODO** — I will create a PR in [forge_docs](https://github.com/Agenticstiger/forge_docs) before this is merged

## Provider Checklist

- [ ] I have read the [Contributing Guide](../../CONTRIBUTING.md) and [Creating Providers](../../docs/CREATING_PROVIDERS.md)
- [ ] Provider subclasses `BaseProvider` from `fluid_provider_sdk`
- [ ] Provider is registered via entry points in `pyproject.toml`
- [ ] At least one working example contract is included
- [ ] Unit tests are added under `tests/providers/`
- [ ] All new and existing tests pass (`pytest`)
- [ ] I have run `ruff check` and `black` with no errors
- [ ] New maintained Python files include license headers (`python scripts/add_license_headers.py`; `examples/**` exempt)

## Testing

<!-- How did you test this provider? Which contracts did you run? Include provider-specific setup steps. -->

## Screenshots / Output

<!-- Paste CLI output showing the provider in action. -->
