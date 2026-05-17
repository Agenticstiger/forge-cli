# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Provider-agnostic assembly of a complete OpenTofu ``.tf.json`` module.

A plugin's ``emit`` returns only its ``resource`` sub-tree; this module
wraps it in the central ``terraform {}`` block (with an optional remote
``backend``), folds in any brownfield ``import {}`` blocks, and renders
canonical, byte-stable JSON. No ``provider {}`` or ``variable {}`` blocks
are emitted — the OpenTofu providers configure themselves from the
environment, keeping the artifact secret-free and portable.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from .base import IacProviderPlugin
from .importer import ImportBlock, import_section
from .versions import REQUIRED_TOFU_VERSION


def assemble_tofu_document(
    *,
    required_providers: Dict[str, Dict[str, str]],
    resources: Dict[str, Any],
    backend: Optional[Dict[str, Any]] = None,
    imports: Optional[List[ImportBlock]] = None,
) -> Dict[str, Any]:
    """Assemble a full ``.tf.json`` document from a plugin's resource sub-tree.

    ``backend`` is an optional ``terraform.backend`` block for remote
    state (see ``iac.backend``); ``imports`` are optional brownfield
    ``import {}`` blocks (see ``iac.importer``).
    """
    terraform_block: Dict[str, Any] = {
        "required_version": REQUIRED_TOFU_VERSION,
        "required_providers": required_providers,
    }
    if backend is not None:
        terraform_block["backend"] = backend
    document: Dict[str, Any] = {"terraform": terraform_block, "resource": resources}
    if imports:
        document.update(import_section(imports))
    return document


def render_tofu_json(document: Mapping[str, Any]) -> str:
    """Serialize a document to canonical ``.tf.json`` text.

    ``sort_keys`` makes the output byte-stable across runs — reviewable
    diffs and a hashable artifact.
    """
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def build_module(
    plugin: IacProviderPlugin,
    contract: Mapping[str, Any],
    *,
    backend: Optional[Dict[str, Any]] = None,
    imports: Optional[List[ImportBlock]] = None,
) -> str:
    """Compile a contract through one plugin into rendered ``.tf.json`` text."""
    document = assemble_tofu_document(
        required_providers=plugin.required_providers,
        resources=plugin.emit(contract),
        backend=backend,
        imports=imports,
    )
    return render_tofu_json(document)
