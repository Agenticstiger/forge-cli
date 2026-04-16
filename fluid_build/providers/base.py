# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# fluid_build/providers/base.py
# Production-ready Provider base for FLUID Build
#
# PRIMARY TYPES are imported from the lightweight ``fluid-provider-sdk``
# package (zero dependencies).  If the SDK is not installed, a local
# fallback is used so ``fluid-forge`` remains self-contained.
#
# The canonical registry lives in fluid_build.providers.__init__.
# This module re-exports register_provider (deprecated path) for backward
# compatibility but does NOT maintain its own PROVIDERS dict.

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

# ---------------------------------------------------------------------------
# Import from fluid-provider-sdk (zero-dep) with inline fallback
# ---------------------------------------------------------------------------
try:
    from fluid_provider_sdk import (  # type: ignore[import-untyped]
        SDK_VERSION,
        ApplyResult,
        BaseProvider,
        CostEstimate,
        PlanAction,
        ProviderCapabilities,
        ProviderError,
        ProviderHookSpec,
        ProviderInternalError,
        ProviderMetadata,
        has_hook,
        invoke_hook,
    )

    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False
    SDK_VERSION = "0.0.0"

    # — inline fallback kept in sync with fluid_provider_sdk —
    from abc import ABC, abstractmethod
    from dataclasses import dataclass, field

    def _mk_logger_fb(name, passed=None):
        if passed:
            return passed
        lg = logging.getLogger(name)
        if not lg.handlers:
            h = logging.StreamHandler()
            h.setFormatter(
                logging.Formatter(
                    '{"time": "%(asctime)s", "level": "%(levelname)s", '
                    '"name": "%(name)s", "message": "%(message)s"}'
                )
            )
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
        return lg

    @dataclass
    class PlanAction:
        action_type: str
        op: str
        resource_id: str
        params: Dict[str, Any] = field(default_factory=dict)

    @dataclass
    class ApplyResult:
        provider: str
        applied: int
        failed: int
        duration_sec: float
        timestamp: str
        results: List[Dict[str, Any]] = field(default_factory=list)

        def to_json(self) -> str:
            return json.dumps(
                {
                    "provider": self.provider,
                    "applied": self.applied,
                    "failed": self.failed,
                    "duration_sec": self.duration_sec,
                    "timestamp": self.timestamp,
                    "results": self.results,
                },
                indent=None,
            )

        def get(self, key, default=None):
            return getattr(self, key, default)

        def __getitem__(self, key):
            if hasattr(self, key):
                return getattr(self, key)
            raise KeyError(key)

        def __contains__(self, key):
            return hasattr(self, key)

    class ProviderError(RuntimeError):
        pass

    class ProviderInternalError(RuntimeError):
        pass

    @dataclass
    class ProviderMetadata:
        name: str
        display_name: str = ""
        description: str = ""
        version: str = "0.0.0"
        sdk_version: str = "0.0.0"
        author: str = "Unknown"
        url: Optional[str] = None
        license: Optional[str] = None
        supported_platforms: List[str] = field(default_factory=list)
        tags: List[str] = field(default_factory=list)

        def __post_init__(self):
            if not self.display_name:
                self.display_name = self.name.replace("_", " ").title()

        def to_dict(self) -> Dict[str, Any]:
            return {
                k: getattr(self, k)
                for k in (
                    "name",
                    "display_name",
                    "description",
                    "version",
                    "sdk_version",
                    "author",
                    "url",
                    "license",
                    "supported_platforms",
                    "tags",
                )
            }

    class ProviderCapabilities:
        def __init__(
            self, planning=True, apply=True, render=False, graph=False, auth=False, **extra
        ):
            self._d = {
                "planning": planning,
                "apply": apply,
                "render": render,
                "graph": graph,
                "auth": auth,
            }
            self.extra = dict(extra)
            self._d.update(self.extra)

        def __getitem__(self, k):
            return self._d[k]

        def __contains__(self, k):
            return k in self._d

        def __iter__(self):
            return iter(self._d)

        def __len__(self):
            return len(self._d)

        def get(self, k, default=False):
            return self._d.get(k, default)

        def items(self):
            return self._d.items()

        def keys(self):
            return self._d.keys()

        def values(self):
            return self._d.values()

    class BaseProvider(ABC):
        name: str = "unknown"

        def __init__(self, *, project=None, region=None, logger=None, **kwargs):
            self.project = project
            self.region = region
            self.logger = _mk_logger_fb(self.__class__.__module__, logger)
            self.extra: Dict[str, Any] = dict(kwargs)

        def capabilities(self) -> Mapping[str, bool]:
            return ProviderCapabilities()

        @classmethod
        def get_provider_info(cls) -> ProviderMetadata:
            return ProviderMetadata(name=cls.name)

        @abstractmethod
        def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]: ...
        @abstractmethod
        def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult: ...
        def render(self, src, *, out=None, fmt=None) -> Dict[str, Any]:
            raise ProviderError(f"{self.name}: render() not supported")

        def require(self, cond, msg):
            if not cond:
                self.logger.error("precondition_failed: %s", msg)
                raise ProviderError(msg)

        def debug_kv(self, **kv):
            try:
                self.logger.debug(json.dumps(kv))
            except Exception:
                self.logger.debug(str(kv))

        def info_kv(self, **kv):
            try:
                self.logger.info(json.dumps(kv))
            except Exception:
                self.logger.info(str(kv))

        def warn_kv(self, **kv):
            try:
                self.logger.warning(json.dumps(kv))
            except Exception:
                self.logger.warning(str(kv))

        def err_kv(self, **kv):
            try:
                self.logger.error(json.dumps(kv))
            except Exception:
                self.logger.error(str(kv))

    # -- Hook fallbacks (kept in sync with fluid_provider_sdk.hooks) ------

    @dataclass
    class CostEstimate:
        currency: str = "USD"
        monthly: float = 0.0
        one_time: float = 0.0
        breakdown: List[Dict[str, Any]] = field(default_factory=list)
        notes: str = ""

        def total(self) -> float:
            return self.monthly + self.one_time

        def to_dict(self) -> Dict[str, Any]:
            return {
                "currency": self.currency,
                "monthly": self.monthly,
                "one_time": self.one_time,
                "total": self.total(),
                "breakdown": self.breakdown,
                "notes": self.notes,
            }

    class ProviderHookSpec:
        def pre_plan(self, contract):
            return contract

        def post_plan(self, actions):
            return actions

        def pre_apply(self, actions):
            return actions

        def post_apply(self, result):
            pass

        def on_error(self, error, context):
            pass

        def estimate_cost(self, actions):
            return None

        def validate_sovereignty(self, contract):
            return []

    def invoke_hook(provider, hook_name, *args, **kwargs):
        method = getattr(provider, hook_name, None)
        if method is None:
            return args[0] if args else None
        try:
            return method(*args, **kwargs)
        except Exception:
            return args[0] if args else None

    def has_hook(provider, hook_name):
        method = getattr(provider, hook_name, None)
        if method is None:
            return False
        if isinstance(provider, ProviderHookSpec):
            default = getattr(ProviderHookSpec, hook_name, None)
            return method.__func__ is not default
        return True


# ---------------------------------------------------------------------------
# Utility kept for backward compatibility
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# -----------------------------------------------------------------------------
# Backward-compatible re-exports from the canonical registry
# -----------------------------------------------------------------------------
# The canonical registry lives in fluid_build.providers.__init__.
# These thin wrappers preserve import paths that existing code may rely on,
# but all state is delegated to __init__.py.


def _canonical_registry():
    """Lazy import to avoid circular deps."""
    import fluid_build.providers as _reg

    return _reg


def register_provider(
    name: str, cls: Type[BaseProvider], logger: Optional[logging.Logger] = None
) -> None:
    """**Deprecated** — use ``from fluid_build.providers import register_provider`` instead.

    Thin wrapper that delegates to the canonical registry in
    ``fluid_build.providers.__init__``.
    """
    warnings.warn(
        "Importing register_provider from fluid_build.providers.base is deprecated. "
        "Use: from fluid_build.providers import register_provider",
        DeprecationWarning,
        stacklevel=2,
    )
    _canonical_registry().register_provider(name, cls, logger=logger, source="base_compat")


def list_providers(
    logger: Optional[logging.Logger] = None, *, lazy_discover: bool = True
) -> List[str]:
    """**Deprecated** — use ``from fluid_build.providers import list_providers`` instead."""
    warnings.warn(
        "Importing list_providers from fluid_build.providers.base is deprecated. "
        "Use: from fluid_build.providers import list_providers",
        DeprecationWarning,
        stacklevel=2,
    )
    reg = _canonical_registry()
    if lazy_discover:
        reg.discover_providers(logger)
    return reg.list_providers()


def get_provider(
    name: str, logger: Optional[logging.Logger] = None, *, lazy_discover: bool = True
) -> Type[BaseProvider]:
    """**Deprecated** — use ``from fluid_build.providers import get_provider`` instead."""
    warnings.warn(
        "Importing get_provider from fluid_build.providers.base is deprecated. "
        "Use: from fluid_build.providers import get_provider",
        DeprecationWarning,
        stacklevel=2,
    )
    reg = _canonical_registry()
    if lazy_discover:
        reg.discover_providers(logger)
    return reg.get_provider(name)


# Proxy objects that delegate to the canonical registry so legacy imports
# (``from fluid_build.providers.base import PROVIDERS``) still work.


class _RegistryProxy(dict):
    """Dict proxy that reads from the canonical registry on every access."""

    def _reg(self):
        return _canonical_registry().PROVIDERS

    def __getitem__(self, key):
        return self._reg()[key]

    def __contains__(self, key):
        return key in self._reg()

    def __iter__(self):
        return iter(self._reg())

    def __len__(self):
        return len(self._reg())

    def get(self, key, default=None):
        return self._reg().get(key, default)

    def keys(self):
        return self._reg().keys()

    def values(self):
        return self._reg().values()

    def items(self):
        return self._reg().items()

    def __repr__(self):
        return repr(self._reg())


class _ErrorsProxy(list):
    """List proxy that reads from the canonical registry on every access."""

    def _errs(self):
        return _canonical_registry().DISCOVERY_ERRORS

    def __getitem__(self, idx):
        return self._errs()[idx]

    def __iter__(self):
        return iter(self._errs())

    def __len__(self):
        return len(self._errs())

    def __repr__(self):
        return repr(self._errs())


PROVIDERS: Dict[str, Type[BaseProvider]] = _RegistryProxy()  # type: ignore[assignment]
DISCOVERY_ERRORS: List[Dict[str, str]] = _ErrorsProxy()  # type: ignore[assignment]


__all__ = [
    "BaseProvider",
    "ApplyResult",
    "PlanAction",
    "ProviderError",
    "ProviderInternalError",
    "ProviderMetadata",
    "ProviderCapabilities",
    "SDK_VERSION",
    "_HAS_SDK",
    "_utc_now_iso",
    # Deprecated re-exports (will be removed in a future release)
    "register_provider",
    "list_providers",
    "get_provider",
    "PROVIDERS",
    "DISCOVERY_ERRORS",
]
