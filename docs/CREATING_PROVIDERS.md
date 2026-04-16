# Creating a FLUID Provider

This guide walks you through building a custom FLUID provider — either as a
built-in contribution or a **third-party pip-installable plugin**.

---

## Quick Start (5 minutes)

### 1. Subclass `BaseProvider`

```python
# my_provider/provider.py
from fluid_build.providers.base import BaseProvider, ApplyResult, ProviderError
from typing import Any, Dict, Iterable, List, Mapping

class MyCloudProvider(BaseProvider):
    """FLUID provider for MyCloud."""

    name = "mycloud"

    def capabilities(self) -> Mapping[str, bool]:
        return {
            "planning": True,
            "apply": True,
            "render": False,
            "graph": False,
            "auth": True,
        }

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        actions = []
        for expose in contract.get("exposes", []):
            actions.append({
                "op": "create_table",
                "resource_id": expose.get("id"),
                "params": expose.get("binding", {}).get("location", {}),
            })
        return actions

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        import time
        start = time.time()
        results = []
        failed = 0

        for action in actions:
            try:
                # Your cloud SDK calls here
                results.append({"op": action["op"], "status": "ok"})
            except Exception as e:
                failed += 1
                results.append({"op": action.get("op"), "status": "error", "error": str(e)})

        return ApplyResult(
            provider=self.name,
            applied=len(results) - failed,
            failed=failed,
            duration_sec=round(time.time() - start, 3),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            results=results,
        )
```

### 2. Register via entry point

In your package's `pyproject.toml`:

```toml
[project.entry-points."fluid_build.providers"]
mycloud = "my_provider.provider:MyCloudProvider"
```

That's it! After `pip install my-fluid-provider`, FLUID will discover it
automatically:

```bash
fluid providers
# {"providers": ["aws", "gcp", "local", "mycloud", "snowflake"]}

fluid providers --debug
# Shows source: "entrypoint" for your provider
```

---

## How Discovery Works

When `fluid providers` or any CLI command runs, the discovery system loads
providers in this order:

1. **Entry-point plugins** — any package declaring
   `[project.entry-points."fluid_build.providers"]` is loaded first. This is
   how third-party providers are discovered.
2. **Built-in modules** — the curated list: `local`, `gcp`, `aws`,
   `snowflake`, `odps`.
3. **Subpackage scan** — `pkgutil.iter_modules` over
   `fluid_build/providers/*` catches any remaining built-in providers.
4. **Fallback** — if the registry is still empty after steps 1-3, a
   best-effort re-import is attempted.

The first registration wins (no overrides by default). Built-in providers
self-register on import, so entry-point plugins get priority if they register
first.

---

## BaseProvider API Reference

### Constructor

```python
def __init__(self, *, project=None, region=None, logger=None, **kwargs):
```

All providers receive `project`, `region`, and `logger` from the CLI. Extra
keyword arguments are stored in `self.extra`.

### Abstract Methods (required)

| Method | Signature | Returns |
|--------|-----------|---------|
| `plan` | `plan(contract: Mapping) -> List[Dict]` | Deterministic list of actions |
| `apply` | `apply(actions: Iterable[Mapping]) -> ApplyResult` | Execution result |

### Optional Methods

| Method | Signature | Purpose |
|--------|-----------|---------|
| `render` | `render(src, *, out=None, fmt=None) -> Dict` | Export/materialize artifacts |
| `capabilities` | `capabilities() -> Mapping[str, bool]` | Feature flags |

### Helper Methods (inherited)

| Method | Purpose |
|--------|---------|
| `self.require(cond, msg)` | Raise `ProviderError` if condition is false |
| `self.info_kv(**kv)` | Structured info logging |
| `self.warn_kv(**kv)` | Structured warning logging |
| `self.err_kv(**kv)` | Structured error logging |
| `self.debug_kv(**kv)` | Structured debug logging |

---

## Project Structure

Recommended layout for a third-party provider package:

```
fluid-provider-mycloud/
├── pyproject.toml
├── README.md
├── src/
│   └── fluid_provider_mycloud/
│       ├── __init__.py
│       └── provider.py      # MyCloudProvider class
└── tests/
    └── test_provider.py
```

### Minimal `pyproject.toml`

```toml
[project]
name = "fluid-provider-mycloud"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = [
    "fluid-forge>=0.7.1",    # for BaseProvider
    "mycloud-sdk>=1.0",       # your cloud SDK
]

[project.entry-points."fluid_build.providers"]
mycloud = "fluid_provider_mycloud.provider:MyCloudProvider"
```

---

## Testing Your Provider

```python
# tests/test_provider.py
import pytest
from fluid_provider_mycloud.provider import MyCloudProvider
from fluid_build.providers.base import BaseProvider, ApplyResult

def test_is_base_provider():
    assert issubclass(MyCloudProvider, BaseProvider)

def test_capabilities():
    p = MyCloudProvider(project="test", region="us-east-1")
    caps = p.capabilities()
    assert caps["planning"] is True
    assert caps["apply"] is True

def test_plan():
    p = MyCloudProvider(project="test")
    contract = {
        "id": "test.product",
        "exposes": [{"id": "my_table", "binding": {"platform": "mycloud"}}],
    }
    actions = p.plan(contract)
    assert len(actions) >= 1
    assert actions[0]["op"] == "create_table"

def test_apply_returns_apply_result():
    p = MyCloudProvider(project="test")
    result = p.apply([{"op": "create_table", "resource_id": "t1"}])
    assert isinstance(result, ApplyResult)
    assert result.provider == "mycloud"

def test_entry_point_discovery():
    """Verify the provider is discoverable after pip install."""
    from fluid_build.providers import discover_providers, PROVIDERS, clear_providers
    clear_providers()
    discover_providers()
    assert "mycloud" in PROVIDERS
```

---

## Advanced Topics

### Custom Constructor Parameters

If your provider needs extra config (e.g., API keys, endpoints), accept them
via `**kwargs`:

```python
class MyCloudProvider(BaseProvider):
    name = "mycloud"

    def __init__(self, *, api_key=None, endpoint=None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key or os.getenv("MYCLOUD_API_KEY")
        self.endpoint = endpoint or "https://api.mycloud.io"
```

### Lazy Imports

Defer heavyweight SDK imports to method bodies to keep CLI startup fast:

```python
def apply(self, actions):
    import mycloud_sdk  # only imported when actually executing
    client = mycloud_sdk.Client(api_key=self.api_key)
    ...
```

### Error Handling

Use the built-in error types for consistent CLI behavior:

```python
from fluid_build.providers.base import ProviderError, ProviderInternalError

# User/action error (e.g., bad config, missing permissions)
raise ProviderError("Dataset 'foo' not found in project 'bar'")

# Internal bug or environment failure
raise ProviderInternalError("MyCloud SDK returned unexpected response")
```

---

## Naming Conventions

- Provider names must be **lowercase letters, digits, or underscores** (e.g.,
  `mycloud`, `my_cloud_v2`).
- Hyphens are automatically normalized to underscores.
- The `name` class attribute should match the entry-point key.
- Banned names: `unknown`, `stub`, empty string.

---

## Migration from Duck-Typed Providers

If you have an existing provider class that doesn't subclass `BaseProvider`,
migration is straightforward:

```python
# Before (duck-typed)
class OldProvider:
    def __init__(self, project=None, region=None, logger=None):
        self.project = project
        ...

# After (BaseProvider subclass)
from fluid_build.providers.base import BaseProvider

class OldProvider(BaseProvider):
    name = "old"

    def __init__(self, *, project=None, region=None, logger=None, **kwargs):
        super().__init__(project=project, region=region, logger=logger, **kwargs)
        # your extra init...
```

Key changes:
1. Add `BaseProvider` as parent class
2. Add `name` class attribute
3. Change `__init__` to keyword-only (`*`) and call `super().__init__()`
4. Add `**kwargs` to accept future parameters
5. Ensure `apply()` returns `ApplyResult` (not a plain dict)
