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

"""Provider-agnostic assembly of a complete OpenTofu ``.tf.json`` module.

A plugin's ``emit`` returns only its ``resource`` sub-tree; this module
wraps it in the central ``terraform {}`` block (with an optional remote
``backend``), folds in any brownfield ``import {}`` blocks, and renders
canonical, byte-stable JSON. A ``provider {}`` block is emitted only when a
plugin needs static, non-secret configuration (e.g. the Snowflake provider's
``preview_features_enabled``); credentials are never emitted — providers
read those from the environment, keeping the artifact secret-free.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .base import IacProviderPlugin
from .importer import ImportBlock, import_section
from .naming import TofuExpr
from .versions import REQUIRED_TOFU_VERSION


def assemble_tofu_document(
    *,
    required_providers: Dict[str, Dict[str, str]],
    resources: Dict[str, Any],
    data: Optional[Dict[str, Any]] = None,
    backend: Optional[Dict[str, Any]] = None,
    imports: Optional[List[ImportBlock]] = None,
    provider: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a full ``.tf.json`` document from a plugin's sub-trees.

    ``data`` is an optional ``data`` sub-tree (e.g. ``archive_file`` —
    see ``IacProviderPlugin.emit_data``); ``backend`` is an optional
    ``terraform.backend`` block for remote state (see ``iac.backend``);
    ``imports`` are optional brownfield ``import {}`` blocks (see
    ``iac.importer``); ``provider`` is the optional ``provider`` sub-tree —
    static, non-secret settings keyed by provider local name (see
    ``IacProviderPlugin.provider_block``).
    """
    terraform_block: Dict[str, Any] = {
        "required_version": REQUIRED_TOFU_VERSION,
        "required_providers": required_providers,
    }
    if backend is not None:
        terraform_block["backend"] = backend
    document: Dict[str, Any] = {"terraform": terraform_block}
    # OpenTofu rejects an empty ``"resource": {}`` object ("at least one
    # object property is required") — omit the key entirely when the
    # contract produced no resources, leaving a valid, empty module.
    if resources:
        document["resource"] = resources
    if provider:
        document["provider"] = provider
    if data:
        document["data"] = data
    if imports:
        document.update(import_section(imports))
    return document


def _escape_tofu_literals(obj: Any) -> Any:
    """Neutralise OpenTofu interpolation in every contract-derived string.

    OpenTofu parses ``${...}`` / ``%{...}`` sequences inside ``.tf.json``
    string values as template expressions — and ``${file(...)}`` reads the
    apply host's filesystem. Every string the emitter produced from contract
    content is escaped here (``${`` → ``$${``, ``%{`` → ``%%{`` — OpenTofu's
    literal-escape), so a malicious contract cannot smuggle an interpolation
    into the emitted module. ``TofuExpr`` values are the emitter's own
    deliberate resource cross-references and pass through unescaped.
    """
    if isinstance(obj, TofuExpr):
        return str(obj)
    if isinstance(obj, str):
        return obj.replace("${", "$${").replace("%{", "%%{")
    if isinstance(obj, Mapping):
        return {k: _escape_tofu_literals(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_escape_tofu_literals(v) for v in obj]
    return obj


def render_tofu_json(document: Mapping[str, Any]) -> str:
    """Serialize a document to canonical ``.tf.json`` text.

    ``sort_keys`` makes the output byte-stable across runs — reviewable
    diffs and a hashable artifact. Contract-derived strings are escaped
    against OpenTofu interpolation injection first (see
    :func:`_escape_tofu_literals`).
    """
    return json.dumps(_escape_tofu_literals(document), indent=2, sort_keys=True) + "\n"


def build_module(
    plugin: IacProviderPlugin,
    contract: Mapping[str, Any],
    *,
    actions: Iterable[Mapping[str, Any]] = (),
    backend: Optional[Dict[str, Any]] = None,
    imports: Optional[List[ImportBlock]] = None,
) -> str:
    """Compile a contract through one plugin into rendered ``.tf.json`` text.

    ``actions`` is the native ``provider.plan()`` output for the contract;
    the plugin uses it to emit the schedule / orchestration resources that
    have no clean declarative form in ``exposes[]`` (see ``iac.base``).
    """
    provider_cfg = plugin.provider_block()
    document = assemble_tofu_document(
        required_providers=plugin.required_providers,
        resources=plugin.emit(contract, actions),
        data=plugin.emit_data(contract, actions),
        backend=backend,
        imports=imports,
        # `.tf.json` keys the provider block by the provider's local name.
        provider={plugin.name: provider_cfg} if provider_cfg else None,
    )
    return render_tofu_json(document)
