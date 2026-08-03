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

"""BitolOdpsProvider — thin orchestrator over the :mod:`mappers` pipeline.

Phase 2 adds the **export** side: render a FLUID contract to one ODPS product
document plus N sibling ODCS contracts (one per output port). The plan
guarantees ``port.contractId == odcs_contract.id`` for every port, so the
sibling files line up with the references inside the ODPS doc.

Phase 3 will add :meth:`import_contract` and :meth:`import_directory`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fluid_build.providers._path_safety import contained_path, safe_filename_stem
from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderError
from fluid_build.providers.odcs.provider import OdcsProvider

from .io import read_input, write_output
from .mappers import EXPORT_PIPELINE, IMPORT_PIPELINE
from .mappers.base import ExportCtx, ImportCtx
from .resolver import ContractNotFound, ContractResolver, ResolvedContract
from .validation import (
    DEFAULT_API_VERSION,
    SUPPORTED_API_VERSIONS,
    load_schema,
    schema_for_document,
    validate,
)


class BitolOdpsProvider(BaseProvider):
    """Bitol Open Data Product Standard v1.0.0 — bidirectional provider.

    Phase 2 surface:
      - :meth:`render` — FLUID → ODPS product + per-port ODCS contracts.
      - :meth:`validate_product` — JSON Schema validation.

    Phase 3 will add ``import_contract`` and ``import_directory``.

    Per-port ODCS rendering is delegated to :class:`OdcsProvider` (calling
    ``render(fluid, expose_id=...)``); the per-expose scoped id it produces
    (``{product_id}.{expose_id}``) equals the ODPS port's ``contractId``
    by construction.
    """

    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        # Emit target. v1.0.0 stays the default until Bitol releases v1.1.0
        # on main: emitting an unreleased apiVersion by default would produce
        # documents that fail validation against the published schema.
        # Opt in per process via ODPS_API_VERSION=v1.1.0 (or set api_version
        # on the instance) to emit the RFC 0029 top-level ``type``.
        self.api_version = os.getenv("ODPS_API_VERSION", DEFAULT_API_VERSION)
        if self.api_version not in SUPPORTED_API_VERSIONS:
            self.logger.warning(
                "ODPS_API_VERSION=%r is not supported (%s); using %s",
                self.api_version,
                ", ".join(SUPPORTED_API_VERSIONS),
                DEFAULT_API_VERSION,
            )
            self.api_version = DEFAULT_API_VERSION
        self.spec_url = "https://github.com/bitol-io/open-data-product-standard"
        self.schema = load_schema(self.api_version)
        self.include_custom_properties = os.getenv("ODPS_INCLUDE_CUSTOM", "true").lower() == "true"
        # Strict by default — a contract that fails the schema is a bug.
        self.strict_validation = os.getenv("ODPS_STRICT", "true").lower() == "true"
        # Default version for synthesized InputPort entries (matches upstream).
        self.default_port_version = os.getenv("ODPS_DEFAULT_PORT_VERSION", "1.0.0")
        self._odcs = OdcsProvider()

    @property
    def name(self) -> str:
        return "odps_bitol"

    def capabilities(self) -> Mapping[str, bool]:
        caps = dict(super().capabilities())
        caps.update(
            {
                "planning": False,
                "apply": False,
                "render": True,
                "validate": True,
                "import": True,  # implemented in Phase 3
                "supports_batch": False,
            }
        )
        return caps

    # ---- unsupported lifecycle hooks ------------------------------------

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        raise ProviderError("Bitol ODPS provider does not support plan(). Use render() for export.")

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        raise ProviderError(
            "Bitol ODPS provider does not support apply(). Use render() for export."
        )

    # ---- render ---------------------------------------------------------

    def render(
        self,
        src: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
        *,
        out: Optional[Union[Path, str]] = None,
        fmt: Optional[str] = "yaml",
        out_dir: Optional[Union[Path, str]] = None,
    ) -> Dict[str, Any]:
        """Render a FLUID contract to one ODPS product + N per-port ODCS contracts.

        Returns ``{"product": dict, "contracts": {contractId: dict, ...}}``.

        When ``out`` is provided the product is written there; when ``out_dir``
        is provided the product **and** every contract are written there with
        the layout ``<product>.odps.<fmt>`` + ``<contractId>.odcs.<fmt>``.
        ``out`` and ``out_dir`` can be combined.
        """
        if isinstance(src, list):
            raise ProviderError(
                "Bitol ODPS export does not support batch processing. "
                "Each data product should be exported separately."
            )

        product: Dict[str, Any] = {}
        ctx = ExportCtx(
            fluid=src,
            odps=product,
            logger=self.logger,
            options={
                "include_custom_properties": self.include_custom_properties,
                "default_port_version": self.default_port_version,
                "api_version": self.api_version,
            },
        )
        for mapper in EXPORT_PIPELINE:
            mapper.to_odps(ctx)

        # Propagate strict mode to the per-port OdcsProvider dynamically so
        # late toggles (``provider.strict_validation = False`` in tests) take
        # effect for the same render call.
        self._odcs._vowl_validate_on_export = self.strict_validation

        # Emit per-port ODCS contracts, keyed by contractId.
        contracts: Dict[str, Dict[str, Any]] = {}
        seen_port_names: set[str] = set()
        for port in product.get("outputPorts") or []:
            port_name = port.get("name")
            contract_id = port.get("contractId")
            if port_name in seen_port_names:
                raise ProviderError(
                    f"Duplicate output port name '{port_name}' — each FLUID expose "
                    "must have a unique exposeId/id."
                )
            seen_port_names.add(port_name)
            odcs = self._odcs.render(src, expose_id=port_name)
            if odcs.get("id") != contract_id:
                # Defensive — these should be equal by construction
                raise ProviderError(
                    f"Linking invariant violated: port.contractId={contract_id!r} "
                    f"≠ odcs.id={odcs.get('id')!r}"
                )
            contracts[contract_id] = odcs

        # Strict-mode validation (fail-loud)
        if self.strict_validation and self.schema:
            validate(product, self.schema)
        for contract_id, odcs in contracts.items():
            if self.strict_validation and self._odcs.schema:
                self._odcs.validate_contract(odcs)

        # Write artefacts
        if out is not None and out != "-":
            write_output(product, out, fmt or "yaml")
            self.logger.info("Exported ODPS product: %s", out)
        if out_dir is not None:
            self._write_bundle(product, contracts, Path(out_dir), fmt or "yaml")

        return {"product": product, "contracts": contracts}

    def _write_bundle(
        self,
        product: Mapping[str, Any],
        contracts: Mapping[str, Mapping[str, Any]],
        out_dir: Path,
        fmt: str,
    ) -> None:
        """Emit the canonical layout: 1 ``<product>.odps.<fmt>`` + N ``<contractId>.odcs.<fmt>``.

        Filenames derive from document-controlled ids, and an imported
        document can carry a hostile one, so every stem passes through
        :func:`~fluid_build.providers._path_safety.safe_filename_stem` and the final path through
        :func:`~fluid_build.providers._path_safety.contained_path` before any write. An id that satisfies the FLUID
        identifier pattern is used verbatim, which is what preserves the
        canonical ``<contractId>.odcs.<fmt>`` sibling layout the
        :class:`ContractResolver` looks up on import.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        product_name = safe_filename_stem(product.get("id") or product.get("name"), "product")
        product_path = contained_path(out_dir, f"{product_name}.odps.{fmt}")
        write_output(dict(product), product_path, fmt)
        self.logger.info("Exported ODPS product: %s", product_path)
        for contract_id, odcs in contracts.items():
            contract_path = contained_path(
                out_dir, f"{safe_filename_stem(contract_id, 'contract')}.odcs.{fmt}"
            )
            write_output(dict(odcs), contract_path, fmt)
            self.logger.info("Exported ODCS contract: %s", contract_path)

    # ---- import (Phase 3) ----------------------------------------------

    def import_contract(
        self,
        odps_doc: Union[Mapping[str, Any], str, Path],
        *,
        base_path: Optional[Union[str, Path]] = None,
        allow_remote: bool = False,
        resolver: Optional[ContractResolver] = None,
        lenient: bool = False,
    ) -> Dict[str, Any]:
        """ODPS product (file or dict) → one FLUID contract.

        Walks every port, resolves the referenced ODCS contract via the
        :class:`ContractResolver`, and merges per-port schema/quality/qos
        back into FLUID exposes/expects. The verbatim ODPS payload is
        preserved via ``metadata.odps_passthrough.*`` so re-export reproduces
        it exactly.

        ``lenient=True`` downgrades resolution failures to warnings (the
        resulting expose/expect stub will lack a populated contract.schema).
        """
        # Load doc and capture its base path for the resolver
        odps_data, doc_base_path = self._load_odps_doc(odps_doc, base_path)

        if resolver is None:
            resolver = ContractResolver(
                base_path=doc_base_path,
                allow_remote=allow_remote,
                odcs_provider=self._odcs,
            )

        # Run the mapper pipeline to build the FLUID skeleton (metadata, team,
        # port stubs, support pass-through)
        fluid: Dict[str, Any] = {}
        ctx = ImportCtx(odps=odps_data, fluid=fluid, logger=self.logger)
        for mapper in IMPORT_PIPELINE:
            mapper.to_fluid(ctx)

        # Resolve every port's contract and merge it into the stub
        self._resolve_and_merge_ports(fluid, odps_data, resolver, lenient=lenient)

        # Preserve the original ODPS doc verbatim for round-trip
        from .mappers.base import metadata_passthrough

        metadata_passthrough(fluid)["source"] = dict(odps_data)
        return fluid

    def import_directory(
        self,
        dir_path: Union[str, Path],
        *,
        allow_remote: bool = False,
        lenient: bool = False,
    ) -> Dict[str, Any]:
        """Import a directory containing an ODPS doc + sibling ODCS files.

        Layout cases handled:

        - **Standard**: exactly one ``*.odps.yaml`` / ``*.odps.yml`` /
          ``*.odps.json`` (or any file with ``kind: DataProduct`` and
          ``apiVersion: v1.0.0``) plus N sibling ODCS files. The resolver
          gets the directory's file index, so per-port contracts always
          hit local first.
        - **ODCS-only**: no ODPS doc, just one or more ODCS files. We
          return a FLUID contract with one expose per ODCS file and no
          product wrapper.

        Raises if the directory contains zero or more than one ODPS doc.
        """
        root = Path(dir_path)
        if not root.is_dir():
            raise ProviderError(f"import_directory: {root} is not a directory")

        odps_files: List[Path] = []
        odcs_files: List[Path] = []
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            suf = child.suffix.lower()
            if suf not in (".yaml", ".yml", ".json"):
                continue
            kind = _sniff_kind(child)
            if kind == "DataProduct":
                odps_files.append(child)
            elif kind == "DataContract":
                odcs_files.append(child)

        if len(odps_files) > 1:
            raise ProviderError(
                f"import_directory: found {len(odps_files)} ODPS docs in {root}; "
                "expected exactly one (or zero for ODCS-only mode). "
                f"Found: {[str(p) for p in odps_files]}"
            )

        if not odps_files:
            return self._import_odcs_only(root, odcs_files)

        # Standard path — build a resolver pre-indexed with the dir's ODCS files
        resolver = ContractResolver(
            base_path=root,
            allow_remote=allow_remote,
            odcs_provider=self._odcs,
            additional_files=odcs_files,
        )
        resolver.index_directory(root)
        return self.import_contract(
            odps_files[0],
            base_path=root,
            allow_remote=allow_remote,
            resolver=resolver,
            lenient=lenient,
        )

    # ---- import internals ----------------------------------------------

    def _load_odps_doc(
        self,
        odps_doc: Union[Mapping[str, Any], str, Path],
        base_path: Optional[Union[str, Path]],
    ) -> tuple:
        if isinstance(odps_doc, (str, Path)):
            doc_path = Path(odps_doc)
            data = read_input(doc_path)
            if not isinstance(data, Mapping):
                raise ProviderError(f"ODPS doc {doc_path} did not parse as a mapping")
            return dict(data), Path(base_path) if base_path else doc_path.parent
        if not isinstance(odps_doc, Mapping):
            raise ProviderError("import_contract: odps_doc must be a path or a mapping")
        return dict(odps_doc), Path(base_path) if base_path else None

    def _resolve_and_merge_ports(
        self,
        fluid: Dict[str, Any],
        odps_data: Mapping[str, Any],
        resolver: ContractResolver,
        *,
        lenient: bool,
    ) -> None:
        # The mapper pipeline already created an expose stub per output port
        # and an expect stub per input port (with contractId attached via the
        # pass-through bucket). We now resolve each contractId and merge the
        # imported ODCS fields into the stub.
        #
        # Resolution policy:
        # - **output ports** must resolve. They're the data this product
        #   produces; an unresolved output is a broken bundle. The ``lenient``
        #   flag still applies here.
        # - **input ports** reference upstream contracts that often live in
        #   other repos / catalogues. Failure to find them is normal — we
        #   warn and leave the stub with just its contractId reference.
        exposes_by_port = {
            (e.get("exposeId") or e.get("id")): e
            for e in fluid.get("exposes", [])
            if isinstance(e, Mapping)
        }
        for port in odps_data.get("outputPorts") or []:
            if not isinstance(port, Mapping):
                continue
            stub = exposes_by_port.get(port.get("name"))
            if stub is None:
                continue
            cid = port.get("contractId")
            if not cid:
                continue
            resolved = self._safe_resolve(resolver, cid, lenient=lenient)
            if resolved is None:
                continue
            self._merge_odcs_into_expose(stub, resolved)

        expects_by_port = {
            e.get("id"): e for e in fluid.get("expects", []) if isinstance(e, Mapping)
        }
        for port in odps_data.get("inputPorts") or []:
            if not isinstance(port, Mapping):
                continue
            stub = expects_by_port.get(port.get("name"))
            if stub is None:
                continue
            cid = port.get("contractId")
            if not cid:
                continue
            # Always lenient for input ports — upstream contracts may live elsewhere
            resolved = self._safe_resolve(resolver, cid, lenient=True)
            if resolved is None:
                continue
            self._merge_odcs_into_expect(stub, resolved)

    def _safe_resolve(
        self, resolver: ContractResolver, contract_id: str, lenient: bool
    ) -> Optional[ResolvedContract]:
        try:
            return resolver.resolve(contract_id)
        except ContractNotFound as exc:
            if lenient:
                self.logger.warning("Skipping unresolved contract %s: %s", contract_id, exc)
                return None
            raise

    def _merge_odcs_into_expose(self, stub: Dict[str, Any], resolved: ResolvedContract) -> None:
        """Run the ODCS importer to get FLUID fields, then merge into the stub.

        The OdcsProvider returns a FLUID skeleton with one expose per
        SchemaObject. For per-port contracts we expect exactly one, so we
        merge its ``contract``, ``qos``, ``binding`` (and any extras) into
        the port stub, keeping the port's id/version/tags/pass-through.
        """
        sub_fluid = self._odcs.import_contract(resolved.odcs)
        sub_exposes = sub_fluid.get("exposes") or []
        if not sub_exposes:
            return
        primary = sub_exposes[0]
        # Carry over the contract block (schema, quality, relationships, etc.)
        if primary.get("contract"):
            stub["contract"] = primary["contract"]
        if primary.get("binding") and "binding" not in stub:
            stub["binding"] = primary["binding"]
        if primary.get("qos") and "qos" not in stub:
            stub["qos"] = primary["qos"]
        if primary.get("description") and "description" not in stub:
            stub["description"] = primary["description"]

    def _merge_odcs_into_expect(self, stub: Dict[str, Any], resolved: ResolvedContract) -> None:
        sub_fluid = self._odcs.import_contract(resolved.odcs)
        sub_exposes = sub_fluid.get("exposes") or []
        if not sub_exposes:
            return
        primary = sub_exposes[0]
        if primary.get("contract"):
            stub["contract"] = primary["contract"]
        if primary.get("binding") and "binding" not in stub:
            stub["binding"] = primary["binding"]

    def _import_odcs_only(self, root: Path, odcs_files: List[Path]) -> Dict[str, Any]:
        """Build a wrapper-less FLUID contract from a directory of ODCS files.

        Each ODCS file becomes one expose under a synthetic product. Useful
        when a team publishes raw ODCS contracts without an ODPS wrapper.
        """
        if not odcs_files:
            raise ProviderError(f"import_directory: {root} contains no ODPS doc and no ODCS files")

        fluid: Dict[str, Any] = {
            "metadata": {
                "name": root.name,
                "version": "1.0.0",
                "status": "active",
            },
            "contract": {"id": root.name},
            "exposes": [],
            "expects": [],
        }
        for odcs_file in odcs_files:
            sub = self._odcs.import_contract(odcs_file)
            for expose in sub.get("exposes") or []:
                fluid["exposes"].append(expose)

        from .mappers.base import metadata_passthrough

        metadata_passthrough(fluid)["odcs_only_directory"] = True
        metadata_passthrough(fluid)["odcs_only_warning"] = (
            f"Directory {root} contained no ODPS doc; "
            f"FLUID built from {len(odcs_files)} ODCS file(s)."
        )
        self.logger.warning(
            "import_directory: no ODPS doc in %s — building FLUID from %d ODCS file(s)",
            root,
            len(odcs_files),
        )
        return fluid

    # ---- validation -----------------------------------------------------

    def validate_product(self, odps: Mapping[str, Any]) -> None:
        # Validate against the document's OWN declared apiVersion, not the
        # provider's emit target: a v1.1.0 document is valid regardless of
        # what this process is configured to emit.
        schema = schema_for_document(odps) or self.schema
        if not schema:
            self.logger.warning("ODPS schema not available, skipping validation")
            return
        validate(odps, schema)


def _sniff_kind(path: Path) -> Optional[str]:
    """Return ``kind`` from a YAML/JSON file without raising."""
    try:
        data = read_input(path)
    except Exception:
        return None
    if not isinstance(data, Mapping):
        return None
    return data.get("kind")
