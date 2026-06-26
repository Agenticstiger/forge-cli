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

"""Discovery + handling of plugin-contributed ``contract.extensions.<key>`` blocks.

Plugins advertise the JSON-Schema for their extension block under the
``fluid_build.extension_schemas`` entry-point group, keyed by the extension
sub-key (complementing the existing ``fluid_build.extension_validators`` group)::

    [project.entry-points."fluid_build.extension_schemas"]
    customScaffold = "data_product_forge_custom_scaffold.validation:get_extension_schema"

The provider has the signature ``get_extension_schema(fluid_version=None) -> dict``
and returns a draft-07 JSON Schema describing the data *under* the extension key.

These helpers let the ``fluid forge`` copilot (a) ground contract generation on
the installed extension schemas and (b) validate the generated blocks before
emit — so any plugin that advertises a schema is handled natively, with no
per-extension change to this CLI. The mechanism is extension-agnostic: the
entry-point group is the entire contract.

Discovery mirrors ``cli/validate.py::_run_extension_validators`` — the same
``importlib.metadata`` walk, the Python<3.10 fallback, per-plugin isolation, and
``redact_secret_text`` pre-redaction. The SDK ships an equivalent reference
implementation (``fluid_sdk.iter_extension_schemas``); the CLI keeps its own copy
here so the copilot has no hard dependency on the SDK and so every
plugin-invocation site in this package shares one audited idiom.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

_SCHEMAS_GROUP = "fluid_build.extension_schemas"
_VALIDATORS_GROUP = "fluid_build.extension_validators"


def iter_extension_schemas(
    fluid_version: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{extension_key: json_schema}`` for every installed provider.

    Per-plugin error isolation: a provider that fails to load, raises, or
    returns a non-dict is skipped (logged, redacted); it never drops the other
    providers and never raises to the caller. Returns ``{}`` when nothing is
    installed (the backward-compatible no-op path).
    """
    log = logger or logging.getLogger(__name__)
    from fluid_build.observability.secret_redactor import redact_secret_text

    try:
        import importlib.metadata as _md

        try:
            eps = _md.entry_points(group=_SCHEMAS_GROUP)
        except TypeError:  # Python < 3.10
            eps = _md.entry_points().get(_SCHEMAS_GROUP, [])
    except Exception as e:  # discovery itself failed — fail open
        log.warning("Extension schema discovery failed: %s", redact_secret_text(str(e)))
        return {}

    from fluid_build.plugin_manager import is_allowed

    schemas: Dict[str, Dict[str, Any]] = {}
    for ep in eps:
        if not is_allowed(ep.name):
            log.debug("extension schema provider %r skipped by allow/block policy", ep.name)
            continue
        try:
            provider = ep.load()
            try:
                schema = provider(fluid_version)
            except TypeError:
                # Provider declared no parameter — call it with none.
                schema = provider()
        except Exception as e:
            log.warning(
                "%s",
                redact_secret_text(f"extension schema provider {ep.name!r} failed: {e}"),
            )
            continue
        if not isinstance(schema, dict):
            log.warning("extension schema provider %r returned a non-dict; skipping", ep.name)
            continue
        schemas[ep.name] = schema
    return schemas


def run_extension_validators(
    contract: Mapping[str, Any],
    logger: Optional[logging.Logger] = None,
) -> List[str]:
    """Return redacted ``extensions.<key>`` validation errors (or ``[]``).

    Return-based core shared by ``fluid validate`` (which folds these into its
    ``ValidationResult``) and the copilot's pre-emit conformance pass (which
    turns them into error-severity findings). A broken validator yields a single
    redacted error rather than crashing; one plugin never drops the others. Each
    plugin receives a deep copy of the extensions block so it cannot mutate the
    contract the rest of the run relies on.
    """
    log = logger or logging.getLogger(__name__)
    extensions = contract.get("extensions") if isinstance(contract, Mapping) else None
    if not isinstance(extensions, Mapping):
        return []
    import copy

    from fluid_build.observability.secret_redactor import redact_secret_text

    try:
        import importlib.metadata as _md

        try:
            eps = _md.entry_points(group=_VALIDATORS_GROUP)
        except TypeError:  # Python < 3.10
            eps = _md.entry_points().get(_VALIDATORS_GROUP, [])
    except Exception as e:
        log.warning("Extension validator discovery failed: %s", redact_secret_text(str(e)))
        return []

    errors: List[str] = []
    from fluid_build.plugin_manager import is_allowed

    for ep in eps:
        if not is_allowed(ep.name):
            log.debug("extension validator %r skipped by allow/block policy", ep.name)
            continue
        plugin_errors: List[str] = []
        try:
            validator = ep.load()
            validator(copy.deepcopy(dict(extensions)), plugin_errors)
        except Exception as e:
            errors.append(redact_secret_text(f"extensions: validator {ep.name!r} raised: {e}"))
            continue
        for msg in plugin_errors:
            errors.append(redact_secret_text(f"extensions.{ep.name}: {msg}"))
    return errors


def build_extension_prompt_fragment(schemas: Mapping[str, Dict[str, Any]]) -> str:
    """Build an LLM system-prompt fragment describing installed extensions.

    Returns ``""`` when no schemas are installed, so the modeler system prompt
    is byte-identical to today in the no-plugin case (backward-compat).
    """
    if not schemas:
        return ""
    import json

    lines = [
        "## Contract extensions (plugin-contributed)",
        (
            "The following third-party `contract.extensions.<key>` blocks are "
            "available. If — and only if — the user's intent or source clearly "
            "calls for one, propose a value that conforms to its JSON Schema. "
            "Put each proposed block under "
            "`source_summary.proposed_extensions.<key>`. Omit any extension you "
            "are unsure about; an absent extension is always valid."
        ),
    ]
    for key, schema in sorted(schemas.items()):
        lines.append(f"### extensions.{key}\n```json\n{json.dumps(schema, indent=2)}\n```")
    return "\n".join(lines)


def assemble_proposed_extensions(
    proposed: Any,
    *,
    fluid_version: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Promote LLM-proposed extension blocks that pass their plugin schema.

    Reads the free-form ``logical.source_summary['proposed_extensions']`` map and
    keeps only sub-blocks whose key has an installed schema AND that validate
    against it. This is a best-effort pre-filter so the emitted contract starts
    clean; the pre-emit extension validators + repair loop remain the
    authoritative gate. Returns ``{}`` when nothing qualifies, so the emitted
    contract is unchanged in the no-extension / no-plugin case.
    """
    if not isinstance(proposed, Mapping) or not proposed:
        return {}
    schemas = iter_extension_schemas(fluid_version, logger=logger)
    if not schemas:
        return {}
    try:
        from jsonschema import Draft7Validator
    except ImportError:  # pragma: no cover — jsonschema is a hard dependency
        Draft7Validator = None  # type: ignore[assignment]

    out: Dict[str, Any] = {}
    for key, block in proposed.items():
        schema = schemas.get(key)
        if schema is None:
            continue
        if Draft7Validator is not None:
            if next(iter(Draft7Validator(schema).iter_errors(block)), None) is not None:
                continue  # invalid — let the repair loop surface it; don't emit junk
        out[key] = block
    return out
