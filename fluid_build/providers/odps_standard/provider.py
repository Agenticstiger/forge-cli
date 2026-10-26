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

import copy
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

from fluid_build.providers._path_safety import contained_path, safe_filename_stem
from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderError

# The ODCS leg solved the same problem first, so its normaliser owns the shared
# answers: which keys FLUID ``metadata`` accepts for a given schema version,
# which schema version a source document is allowed to make us declare, whether
# a version can hold root ``extensions`` at all, how an ODCS/ODPS team object
# becomes a FLUID ``metadata.owner``, and which FLUID enum a status string or a
# physical object type lands on. Private names, imported deliberately rather
# than copied — a second table would drift from the schema the moment one of
# them is extended. (The ODPS team mapper already reaches into the ODCS team
# mapper the same way.)
from fluid_build.providers.odcs.mappers.extras import FLUID_EXTRAS_PROPERTY
from fluid_build.providers.odcs.mappers.normalize import (
    EXTENSIONS_KEY,
    _normalize_owner,
    _supports_extensions,
    metadata_keys_for,
    resolve_declared_version,
)
from fluid_build.providers.odcs.mappers.types import (
    binding_format,
    odcs_to_fluid_status,
    physical_type_to_expose_kind,
)
from fluid_build.providers.odcs.provider import OdcsProvider

from .io import read_input, write_output
from .mappers import EXPORT_PIPELINE, IMPORT_PIPELINE
from .mappers.base import PASSTHROUGH_KEY, ExportCtx, ImportCtx
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
    ``render(fluid, expose_id=...)``); the per-expose id it produces equals the
    ODPS port's ``contractId`` because both sides resolve it through
    :func:`fluid_build.util.contract.expose_contract_id`, reading the same
    contract. The derived ``{product_id}.{expose_id}`` is only what that lookup
    falls back to: an id the expose pins (``contract.id``) or one an import
    recorded is used verbatim, by both sides alike.

    Agreeing on the lookup is not on its own enough to make the two ids the
    same string, and this class does not rely on it being: two exposes can
    resolve to one id, and then one per-port contract overwrites the other. The
    ``contractId``-collision refusal and the ``odcs.id != port.contractId``
    check in :meth:`render` are what actually hold the invariant, and they hold
    it by refusing an export that would not satisfy it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        # Emit target. Bitol released v1.1.0 on 2026-09-08, so both supported
        # apiVersions are published standards. v1.0.0 stays the default because
        # changing the emitted apiVersion changes what every existing consumer
        # parses; the default moves with a FLUID schema bump, not silently.
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

        # An imported contract keeps its ODPS round-trip state in root
        # ``extensions.odps`` because that is the only place the FLUID schema
        # accepts it. Push it back down into the inline buckets the export
        # mappers read, so they stay unchanged and the product doc still
        # round-trips byte-for-byte. A hand-written contract has no bucket and
        # goes through untouched.
        src = _rehydrate_document(src)

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
        # Which port claimed each contract id. ``contracts`` is keyed by that
        # id, so a second port resolving to one already taken used to overwrite
        # the first entry: the bundle came out with N output ports and fewer
        # than N ODCS contracts, the losing port's schema absent altogether,
        # and a consumer resolving it got the surviving port's schema instead.
        # ``fluid validate`` now refuses the contract that causes it, but
        # neither ``fluid odps export`` nor ``fluid generate artifacts`` runs
        # validate, so the exporter has to refuse it too — and refuse rather
        # than warn: a sealed bundle that is silently missing a schema is worse
        # than an export that did not happen.
        claimed_by: Dict[str, Any] = {}
        for port in product.get("outputPorts") or []:
            port_name = port.get("name")
            contract_id = port.get("contractId")
            if port_name in seen_port_names:
                raise ProviderError(
                    f"Duplicate output port name '{port_name}' — each FLUID expose "
                    "must have a unique exposeId/id."
                )
            seen_port_names.add(port_name)
            first_port = claimed_by.get(contract_id)
            if first_port is not None:
                raise ProviderError(
                    f"Output ports '{first_port}' and '{port_name}' both resolve to "
                    f"contract id '{contract_id}' — one per-port ODCS contract would "
                    f"overwrite the other and '{first_port}' would have no schema in "
                    "the exported bundle. Give each expose a distinct "
                    "exposes[].contract.id, or drop the pin so the id is derived "
                    "from the exposeId."
                )
            claimed_by[contract_id] = port_name
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
        """ODPS product (file or dict) → one **schema-valid** FLUID contract.

        Walks every port, resolves the referenced ODCS contract via the
        :class:`ContractResolver`, and merges per-port schema/quality/qos
        back into FLUID exposes/expects. The verbatim ODPS payload is
        preserved via ``extensions.odps.metadata.source`` so re-export
        reproduces it exactly.

        The result is normalised into the FLUID document shape by
        :func:`_to_fluid_document` — without that pass the returned contract
        was rejected by ``fluid validate`` with ten errors (no root
        ``fluidVersion``/``kind``/``id``/``name``, a root ``contract``/
        ``expects``/``owner`` block, exposes with no ``kind``, and six keys
        ``metadata`` does not accept). It is *not* schema-validated here: the
        caller decides what an invalid document means, and ``fluid odps
        import`` refuses to write one unless ``--lenient`` is passed.

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

        # Resolve every port's contract and merge it into the stub. The
        # resolved ODCS documents are also the only place a FLUID → ODPS export
        # recorded the source contract's own ``fluidVersion``, so the merge
        # hands back what they claimed.
        recorded_versions = self._resolve_and_merge_ports(
            fluid, odps_data, resolver, lenient=lenient
        )

        # Preserve the original ODPS doc verbatim for round-trip
        from .mappers.base import metadata_passthrough

        metadata_passthrough(fluid)["source"] = dict(odps_data)
        return _to_fluid_document(fluid, odps_data, recorded_versions=recorded_versions)

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

        Raises if the directory contains more than one ODPS doc, or none
        while a file that may be it could not be read (it did not parse, or
        the YAML caps refused it). Such a file next to a readable ODPS doc
        is skipped with a warning.
        """
        root = Path(dir_path)
        if not root.is_dir():
            raise ProviderError(f"import_directory: {root} is not a directory")

        odps_files, odcs_files, unreadable = _classify_bundle_files(root, self.logger)
        if unreadable and not odps_files:
            # An unreadable file may be the product: importing the rest as an
            # ODCS-only directory would drop it without an error.
            raise ProviderError(
                f"import_directory: found no readable ODPS doc in {root}, and could not "
                f"read {len(unreadable)} file(s) that may be it: {unreadable}"
            )

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
    ) -> List[Any]:
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
        #
        # Only the OUTPUT ports' contracts are collected as fluidVersion
        # evidence. An input port names an upstream product authored by someone
        # else, whose schema version says nothing about the version THIS product
        # was written against.
        recorded_versions: List[Any] = []
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
            if cid is None or cid == "":
                continue
            resolved = self._safe_resolve(resolver, cid, lenient=lenient)
            if resolved is None:
                continue
            recorded = _recorded_fluid_version(resolved.odcs)
            if recorded is not None:
                recorded_versions.append(recorded)
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
            if cid is None or cid == "":
                continue
            # Always lenient for input ports — upstream contracts may live elsewhere
            resolved = self._safe_resolve(resolver, cid, lenient=True)
            if resolved is None:
                continue
            self._merge_odcs_into_expect(stub, resolved)

        return recorded_versions

    def _safe_resolve(
        self, resolver: ContractResolver, contract_id: str, lenient: bool
    ) -> Optional[ResolvedContract]:
        try:
            return resolver.resolve(contract_id)
        except ContractNotFound as exc:
            if lenient:
                # %r: contractId is document text and can carry a NUL byte.
                self.logger.warning("Skipping unresolved contract %r: %s", contract_id, exc)
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
        # FLUID requires ``kind`` on every expose. The resolved contract's
        # ``physicalType`` is the authoritative signal for it — far better than
        # the guess :func:`_normalize_imported_expose` has to make from the
        # port's ``type`` when no contract resolved.
        if primary.get("kind") and "kind" not in stub:
            stub["kind"] = primary["kind"]

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
        # Each sub-import already resolved its own document's recorded
        # fluidVersion (or defaulted it), so its root field is the version
        # evidence here — there is no ODPS product doc to read one from.
        recorded_versions: List[Any] = []
        for odcs_file in odcs_files:
            sub = self._odcs.import_contract(odcs_file)
            if sub.get("fluidVersion"):
                recorded_versions.append(sub["fluidVersion"])
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
        # Same intermediate shape as the ODPS path (root ``contract``,
        # ``metadata.status``/``version``), so it needs the same normalisation
        # to come out as a contract ``fluid validate`` accepts.
        return _to_fluid_document(fluid, {}, recorded_versions=recorded_versions)

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


def _classify_bundle_files(
    root: Path, logger: logging.Logger
) -> Tuple[List[Path], List[Path], List[str]]:
    """Sort a directory's YAML/JSON files into ODPS docs, ODCS contracts and
    files that could not be read, which are skipped with a warning."""
    odps_files: List[Path] = []
    odcs_files: List[Path] = []
    unreadable: List[str] = []
    for child in sorted(root.rglob("*")):
        if not child.is_file() or child.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        # A file that does not parse, or that the YAML caps refuse, may be the
        # product doc, so it is reported, not mistaken for an unrelated file.
        try:
            data = read_input(child)
        # ValueError: UnsafeYamlError, UnicodeDecodeError, JSONDecodeError.
        except (OSError, ValueError, RecursionError, yaml.YAMLError) as exc:
            logger.warning("Skipping %r: %s", str(child), exc)
            unreadable.append(str(child))
            continue
        kind = data.get("kind") if isinstance(data, Mapping) else None
        if kind == "DataProduct":
            odps_files.append(child)
        elif kind == "DataContract":
            odcs_files.append(child)
    return odps_files, odcs_files, unreadable


# ---- import normalisation / export rehydration ---------------------------
#
# The import mappers each write their slice in whatever shape reads most
# naturally for it: ``metadata.status``/``version``/``name``, a root
# ``contract``/``owner``/``expects`` block, and an expose carrying both ``id``
# and its inline ``odps_passthrough`` bucket. That intermediate shape is NOT a
# valid FLUID contract — the schema sets ``additionalProperties: false`` on the
# root, on ``metadata`` and on every expose, requires
# ``fluidVersion``/``kind``/``id``/``name`` at the root, ``metadata.owner``, and
# ``kind``/``binding``/``contract`` on every expose — so ``fluid odps import``
# printed its success tick and wrote a document its own validator rejected with
# ten errors.
#
# These two functions are the seam between the shapes, mirroring
# :mod:`fluid_build.providers.odcs.mappers.normalize` for the ODCS leg:
#
# ``_to_fluid_document``
#     runs once at the end of the import, restructures the root into the FLUID
#     layout and hoists everything with no schema home into root
#     ``extensions.odps`` — the one object the FLUID schema declares open
#     (``additionalProperties: true``) and therefore the only legal place for
#     vendor round-trip state.
#
# ``_rehydrate_document``
#     the inverse, run at the top of every :meth:`BitolOdpsProvider.render`. It
#     pushes the bucket back down into the inline buckets the export mappers
#     already read, so those mappers stay unchanged and
#     ``export(import(export(x))) == export(x)`` still holds.
#
# Both copy their input and never mutate it.

EXTENSION_NAMESPACE = "odps"

# Module-level: the import normalisation below is module functions, not provider
# methods, and the version decisions they make have to be visible to the user
# whose contract came back declaring something other than what it declared.
LOG = logging.getLogger(__name__)

# Bitol ODPS ``OutputPort.type`` → the kind of object it names, spelled in the
# ODCS ``physicalType`` vocabulary so the ODCS importer's own
# ``physical_type_to_expose_kind`` / ``binding_format`` tables stay the single
# source of truth for the two closed FLUID enums they feed.
_PORT_TYPE_TO_PHYSICAL_TYPE: Mapping[str, str] = {
    "kafka": "topic",
    "s3": "file",
}

# ``OutputPort.type`` → FLUID ``binding.platform``: the inverse of the
# exporter's ``provider_to_port_type``. A port type with no FLUID platform
# (``mysql``, ``custom``) resolves to the enum's honest escape hatch rather
# than to a platform the document never named.
_PORT_TYPE_TO_PLATFORM: Mapping[str, str] = {
    "bigquery": "gcp",
    "snowflake": "snowflake",
    "s3": "aws",
    "azure": "azure",
    "databricks": "databricks",
    "postgres": "postgres",
    "kafka": "kafka",
    "local": "local",
}


def _to_fluid_document(
    fluid: Mapping[str, Any],
    odps: Mapping[str, Any],
    *,
    recorded_versions: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Restructure the import pipeline's output into a valid FLUID contract.

    ``recorded_versions`` are the ``fluidVersion`` values the source documents
    recorded for themselves, in no particular order; see
    :func:`_declared_fluid_version` for what is done with them.
    """
    work = copy.deepcopy(dict(fluid))
    bucket: Dict[str, Any] = {}

    metadata = dict(work.pop("metadata", None) or {})
    metadata_pt = dict(metadata.pop(PASSTHROUGH_KEY, {}) or {})

    # The root ``contract`` block the product mapper writes carries nothing but
    # the product id, which is a root field in FLUID.
    contract_block = work.pop("contract", None)
    contract_block = dict(contract_block) if isinstance(contract_block, Mapping) else {}
    contract_id = contract_block.pop("id", None)
    if contract_block:
        bucket["contract_extra"] = contract_block

    # The schema version the imported contract declares. Everything below that
    # asks "does the schema have a home for this?" asks about THIS version.
    # Deliberately not named ``version``: that name is taken further down by the
    # product's OWN version string, a different fact entirely, and the shadowing
    # silently handed ``1.0.0`` to every schema lookup here.
    declared_version = _declared_fluid_version(recorded_versions)

    doc: Dict[str, Any] = {
        # The ODPS product document carries no FLUID schema version —
        # ``apiVersion`` names the Bitol spec, not this one — but its sibling
        # ODCS contracts do, in the ``fluidExtras`` custom property a FLUID
        # export writes. Pinning every import to the current stable version
        # regardless was the bug: a contract exported at 0.7.6 came back
        # declaring 0.7.5, whose ``exposeContract`` is closed, so the
        # validate-before-write gate in ``fluid odps import`` refused it and
        # ``export`` followed by ``import`` exited 1.
        "fluidVersion": declared_version,
        "kind": "DataProduct",
        "id": odps.get("id") or contract_id or "imported.odps.product",
    }

    # FLUID requires ``name``; the import mapper already falls back to the
    # product id when the ODPS document omits it.
    metadata_name = metadata.pop("name", None)
    doc["name"] = odps.get("name") or metadata_name or doc["id"]

    description = metadata.pop("description", None)
    if description:
        doc["description"] = description

    domain = metadata.pop("domain", None)
    if domain:
        doc["domain"] = domain

    # ODPS ``status`` → FLUID ``lifecycle.state``, which is a closed enum
    # (preview/active/deprecated/retired): the Bitol vocabulary's ``draft`` and
    # ``proposed`` both have to land on ``preview``, so the raw spelling is kept
    # in the bucket and replayed on export rather than derived back.
    metadata_status = metadata.pop("status", None)
    raw_status = odps.get("status") or metadata_status
    if raw_status:
        doc["lifecycle"] = {"state": odcs_to_fluid_status(raw_status)}
    if odps.get("status"):
        bucket["status"] = odps["status"]

    # The product's own version string: a Bitol top-level field with no FLUID
    # root home (``fluidVersion`` is the schema's version, not the product's).
    version = metadata.pop("version", None)
    if version:
        bucket["version"] = version

    tenant = metadata.pop("tenant", None)
    if tenant:
        bucket["tenant"] = tenant

    # Anything else ODPS put in ``metadata`` that the closed object rejects.
    # Asked per declared version: the team roster the team mapper writes has a
    # home only from 0.7.6, so an older document's roster is bucketed here (and
    # exports verbatim from the bucket) rather than promoting the document to a
    # preview schema just to fit it.
    accepted_metadata = metadata_keys_for(declared_version)
    stray = {k: v for k, v in metadata.items() if k not in accepted_metadata}
    for key in stray:
        metadata.pop(key)
    if stray:
        bucket["metadata_extra"] = stray

    # The team mapper writes a root ``owner``; FLUID's home for it is
    # ``metadata.owner``, which is required and accepts only
    # {team,email,slack,oncall} — the rest (``name``, ``role``) goes to the
    # bucket so re-export rebuilds the same ODPS ``team`` object.
    derived_owner = work.pop("owner", None)
    metadata["owner"] = _normalize_owner(metadata.get("owner") or derived_owner, bucket)
    doc["metadata"] = metadata

    if metadata_pt:
        bucket["metadata"] = metadata_pt

    port_types = {
        port.get("name"): port.get("type")
        for port in odps.get("outputPorts") or []
        if isinstance(port, Mapping)
    }
    doc["exposes"] = [
        _normalize_imported_expose(expose, port_types, bucket, declared_version)
        for expose in (work.pop("exposes", None) or [])
        if isinstance(expose, Mapping)
    ]

    # ``expects`` is not a FLUID root key. The modern spelling is ``consumes``,
    # which requires a productId/exposeId pair a Bitol ``InputPort`` cannot
    # supply (it names a contractId and nothing else), so the list rides in the
    # bucket verbatim — lossless, and the export side's legacy ``expects[]``
    # path reads it back unchanged.
    expects = work.pop("expects", None)
    if expects:
        bucket["expects"] = expects

    for key, value in work.items():
        if key.startswith("_"):  # in-memory scoping/marker keys, never on disk
            continue
        doc.setdefault(key, value)

    if bucket:
        extensions = dict(doc.get(EXTENSIONS_KEY) or {})
        extensions[EXTENSION_NAMESPACE] = bucket
        doc[EXTENSIONS_KEY] = extensions
    return doc


def _normalize_imported_expose(
    expose: Mapping[str, Any],
    port_types: Mapping[Any, Any],
    bucket: Dict[str, Any],
    declared_version: str,
) -> Dict[str, Any]:
    work = dict(expose)
    expose_pt = dict(work.pop(PASSTHROUGH_KEY, {}) or {})
    # The port stub carries both spellings; ``exposeId`` is the one FLUID
    # declares, and a leftover ``id`` is an additional property the closed
    # expose object rejects.
    legacy_id = work.pop("id", None)
    expose_id = work.get("exposeId") or legacy_id or "dataset"
    work["exposeId"] = expose_id

    port_type = expose_pt.get("port_type") or port_types.get(expose_id)
    physical_type = _port_physical_type(port_type)

    # FLUID requires ``kind``, ``binding`` and ``contract`` on every expose;
    # a Bitol output port supplies none of them directly, and an unresolved
    # port (``--lenient``) supplies nothing at all. Note every key invented
    # here so ``_rehydrate_expose`` can take it back off — otherwise a
    # re-export would claim the source document stated something it never did.
    synthesized: Dict[str, Any] = {}
    if "kind" not in work:
        work["kind"] = physical_type_to_expose_kind(physical_type)
        synthesized["kind"] = True
    binding, binding_synthesized = _normalize_port_binding(work.get("binding"), port_type)
    work["binding"] = binding
    if binding_synthesized:
        synthesized["binding"] = binding_synthesized
    contract = dict(work.get("contract") or {})
    if "schema" not in contract and "openapiRef" not in contract:
        # ``exposeContract`` requires one of those two, so an empty object
        # fails the anyOf just as a missing block fails the expose's
        # ``required``. An unresolved port has neither to offer.
        contract["schema"] = []
        synthesized["contract"] = ["schema"]
    # The contractId the imported port already answered to belongs in
    # ``contract.id``, the field that exists for exactly this: an id a catalog
    # or a consumer knows the port by, which must not be re-derived as
    # ``{product id}.{exposeId}`` on the next export. It is written only when
    # the declared schema has the field (0.7.6 added it), and the field is asked
    # for rather than the version compared, so a later version that keeps it
    # needs no edit here. At an older declared version the id stays where it
    # already is — the per-expose pass-through, which
    # :func:`fluid_build.util.contract.explicit_expose_contract_id` reads as its
    # fallback — because writing it would fail the closed ``exposeContract``
    # and the whole import with it. Not recorded in ``synthesized``: this is
    # the source document's own value, not a repair, so a re-export may state
    # it, and an edit to it must win over the stale pass-through copy.
    # ``isinstance`` and not ``str()``: the port's ``contractId`` is document
    # text and may be any YAML scalar. ``exposeContract.id`` is a non-empty
    # string, so a number or a mapping stays in the pass-through rather than
    # being coerced into a field it does not belong in.
    imported_contract_id = expose_pt.get("contract_id")
    if _supports_expose_contract_id(declared_version):
        if isinstance(imported_contract_id, str) and imported_contract_id and "id" not in contract:
            contract["id"] = imported_contract_id
    elif "id" in contract:
        # The mirror case: the declared version has no such field, yet the
        # contract block carries one. The ODCS extras blob restores a pinned id
        # verbatim, so an expose arrives here with one whenever the recorded
        # version could not be honoured. It moves to the pass-through the
        # exporter reads as its fallback — lossless — rather than staying in a
        # closed object where it would fail the whole import.
        expose_pt.setdefault("contract_id", contract.pop("id"))
    work["contract"] = contract

    entry: Dict[str, Any] = {}
    # A Bitol port version is a free-form string — the FLUID exporter itself
    # emits ``version: '1'`` — while FLUID ``expose.version`` is semver. One
    # that is not semver has no schema home, so it rides in the bucket instead
    # of failing the whole contract.
    raw_version = work.get("version")
    if raw_version is not None and not _is_semver(raw_version, declared_version):
        entry["version"] = work.pop("version")

    if expose_pt:
        entry["expose"] = expose_pt
    if synthesized:
        entry["synthesized"] = synthesized
    if entry:
        bucket.setdefault("exposes", {})[expose_id] = entry
    return work


def _normalize_port_binding(
    binding: Any, port_type: Optional[str]
) -> Tuple[Dict[str, Any], List[str]]:
    """Build a FLUID ``binding`` — platform/format/location are all required.

    Anything already present came from the port's resolved ODCS contract, whose
    ``servers[]`` entry is the authoritative platform signal; derivation from
    the port's ``type`` only fills the gaps.
    """
    restored = dict(binding) if isinstance(binding, Mapping) else {}
    platform = _PORT_TYPE_TO_PLATFORM.get(str(port_type or "").lower(), "other")
    derived = {
        "platform": platform,
        "format": binding_format(platform, _port_physical_type(port_type)),
        # A Bitol output port names no physical object, so there is nothing
        # honest to put in ``location``; the resolved ODCS contract's servers
        # are what normally fill it.
        "location": {},
    }
    synthesized: List[str] = []
    for key, value in derived.items():
        if key not in restored:
            restored[key] = value
            synthesized.append(key)
    return restored, synthesized


def _port_physical_type(port_type: Optional[str]) -> Optional[str]:
    """A port ``type`` in the ODCS ``physicalType`` vocabulary, or None.

    A port with no ``type`` yields None, which the ODCS tables answer with
    ``other`` — the honest result for a document that named no object kind.
    """
    if not port_type:
        return None
    return _PORT_TYPE_TO_PHYSICAL_TYPE.get(str(port_type).lower(), "table")


def _default_fluid_version() -> str:
    """The current stable FLUID schema version.

    Function-local import: ``schema_manager`` pulls in ``jsonschema``, which
    ``tests/perf/test_startup_budget.py`` forbids on the ``fluid --help`` path.
    ``cli.plan._default_fluid_version`` is the same one-line lookup, but
    importing it from here would drag the whole plan module (and ``rich``) into
    the provider.
    """
    from fluid_build.schema_manager import SchemaManager

    return SchemaManager.latest_bundled_version()


def _recorded_fluid_version(odcs: Mapping[str, Any]) -> Any:
    """The FLUID schema version an ODCS document records for its source contract.

    The ODPS product document has no field for it: Bitol ``apiVersion`` names
    the Bitol spec. The one place a FLUID → ODPS export writes it is the sibling
    ODCS contract, in the ``fluidExtras`` entry under ``customProperties`` that
    the ODCS extras mapper owns — which is why a bare product import has no
    version to honour and a bundle import does.

    Returned raw, exactly as the document stated it. Deciding whether a foreign
    document's value may be honoured is :func:`_declared_fluid_version`'s job,
    not this lookup's.
    """
    for prop in odcs.get("customProperties") or []:
        if not isinstance(prop, Mapping) or prop.get("property") != FLUID_EXTRAS_PROPERTY:
            continue
        value = prop.get("value")
        root = value.get("root") if isinstance(value, Mapping) else None
        if isinstance(root, Mapping) and "fluidVersion" in root:
            return root["fluidVersion"]
    return None


def _declared_fluid_version(recorded: Sequence[Any]) -> str:
    """The version an ODPS import declares, given what the source documents recorded.

    ``resolve_declared_version`` applies the allowlist that keeps a foreign or
    hostile document from selecting a schema this build does not ship; it warns
    on each value it refuses and falls back to the stable default, which is also
    the answer when nothing was recorded at all.

    Two further rules are this leg's own:

    * **The highest accepted version wins** when sibling contracts disagree.
      They came from one FLUID contract and normally agree; if they do not, the
      merged document may carry a field only the newer schema declares, and the
      lower version would reject it.
    * **A version too old to hold the import's own state is refused.** Every
      ODPS import parks round-trip state (at minimum the verbatim source
      document) in root ``extensions``, which arrived in FLUID 0.7.3 while every
      version closes the root to additional properties. Honouring a recorded
      0.7.1 would produce a contract the validate-before-write gate rejects —
      the same failure the old blanket pin caused, with the versions swapped.
    """
    from fluid_build.schema_manager import FluidSchemaManager

    default = _default_fluid_version()
    if not recorded:
        return default

    bundled = list(FluidSchemaManager.BUNDLED_VERSIONS)
    accepted = [resolve_declared_version(value) for value in recorded]
    version = max(accepted, key=lambda v: bundled.index(v) if v in bundled else -1)

    if not _supports_extensions(version):
        LOG.warning(
            "ODPS import: not declaring the recorded fluidVersion %s. An imported "
            "contract carries this provider's round-trip state in root `extensions`, "
            "added in FLUID 0.7.3; %s closes the root to additional properties and "
            "cannot hold it. The imported contract declares %s instead.",
            version,
            version,
            default,
        )
        return default
    return version


@lru_cache(maxsize=None)
def _supports_expose_contract_id(version: str) -> bool:
    """Does this bundled FLUID schema declare ``exposeContract.id``?

    Asked of the schema rather than compared against the version string that
    introduced the field (0.7.6), so promoting that version to stable, or adding
    a later one, needs no edit here.
    """
    defs = _schema_defs(version)
    contract = defs.get("exposeContract")
    properties = contract.get("properties") if isinstance(contract, Mapping) else None
    return isinstance(properties, Mapping) and "id" in properties


@lru_cache(maxsize=None)
def _semver_pattern(version: str) -> Optional[re.Pattern]:
    """The ``$defs/semver`` pattern from the schema version we emit.

    Read from the bundled schema rather than restated here, so it cannot drift
    from the definition that will judge the document. ``None`` when the schema
    cannot be read — then nothing is stripped and the validator reports the
    version as the error it is, which beats dropping a field on a guess.
    """
    semver = _schema_defs(version).get("semver")
    pattern = semver.get("pattern") if isinstance(semver, Mapping) else None
    return re.compile(pattern) if pattern else None


def _schema_defs(version: str) -> Mapping[str, Any]:
    """``$defs`` of one bundled FLUID schema, or empty when it cannot be read."""
    from fluid_build.schema_manager import FluidSchemaManager

    schema = FluidSchemaManager().get_schema(version, offline_only=True)
    if not isinstance(schema, Mapping):
        return {}
    defs = schema.get("$defs")
    return defs if isinstance(defs, Mapping) else {}


def _is_semver(value: Any, version: str) -> bool:
    pattern = _semver_pattern(version)
    return pattern is None or bool(pattern.match(str(value)))


def _rehydrate_document(fluid: Mapping[str, Any]) -> Mapping[str, Any]:
    """Inverse of :func:`_to_fluid_document` — no-op when there is no ODPS bucket."""
    extensions = fluid.get(EXTENSIONS_KEY)
    bucket = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
    if not isinstance(bucket, Mapping) or not bucket:
        return fluid

    work = copy.deepcopy(dict(fluid))
    metadata = dict(work.get("metadata") or {})

    if isinstance(bucket.get("metadata"), Mapping):
        metadata[PASSTHROUGH_KEY] = dict(bucket["metadata"])
    if isinstance(bucket.get("metadata_extra"), Mapping):
        metadata.update(bucket["metadata_extra"])
    for key in ("version", "tenant"):
        if bucket.get(key):
            metadata[key] = bucket[key]

    # Replay the source document's verbatim Bitol status spelling, but only
    # while it still agrees with ``lifecycle.state``: ``draft`` and
    # ``proposed`` both import as ``preview``, so the verbatim value is what
    # keeps the round-trip exact — yet the moment someone edits
    # ``lifecycle.state`` the edit has to win, or the exporter would publish a
    # status the contract no longer claims. ``resolve_status`` reads
    # ``metadata.status`` ahead of ``lifecycle.state`` for exactly this.
    lifecycle = work.get("lifecycle")
    state = lifecycle.get("state") if isinstance(lifecycle, Mapping) else None
    verbatim = bucket.get("status")
    if verbatim and (state is None or odcs_to_fluid_status(verbatim) == state):
        metadata["status"] = verbatim
    elif state:
        metadata["status"] = state

    for root_key in ("name", "description", "domain"):
        if work.get(root_key):
            metadata[root_key] = work[root_key]

    owner = dict(metadata.get("owner") or {})
    if isinstance(bucket.get("owner_extra"), Mapping):
        owner.update(bucket["owner_extra"])
    if bucket.get("owner_synthesized"):
        # FLUID required an owner the ODPS document never named; re-exporting
        # the invented one would mint a ``team`` object out of nothing.
        metadata.pop("owner", None)
    elif owner:
        metadata["owner"] = owner
        work["owner"] = owner
    work["metadata"] = metadata

    if isinstance(bucket.get("contract_extra"), Mapping):
        work["contract"] = dict(bucket["contract_extra"])
    if work.get("id"):
        work.setdefault("contract", {})["id"] = work["id"]
    if bucket.get("expects"):
        work["expects"] = copy.deepcopy(bucket["expects"])

    exposes_bucket = bucket.get("exposes") if isinstance(bucket.get("exposes"), Mapping) else {}
    work["exposes"] = [
        _rehydrate_expose(expose, exposes_bucket)
        for expose in work.get("exposes") or []
        if isinstance(expose, Mapping)
    ]
    return work


def _rehydrate_expose(
    expose: Mapping[str, Any], exposes_bucket: Mapping[str, Any]
) -> Dict[str, Any]:
    work = dict(expose)
    expose_id = work.get("exposeId") or work.get("id")
    entry = exposes_bucket.get(expose_id) if expose_id else None
    entry = entry if isinstance(entry, Mapping) else {}

    if isinstance(entry.get("expose"), Mapping):
        work[PASSTHROUGH_KEY] = dict(entry["expose"])
    if "version" in entry:
        work["version"] = entry["version"]

    # Undo the schema-required repairs, so the export pipeline sees the expose
    # exactly as the source document described it: a port that named no type
    # must not come back out carrying the ``other``/``custom`` placeholder a
    # synthesized binding would have handed it.
    synthesized = entry.get("synthesized")
    if isinstance(synthesized, Mapping):
        if synthesized.get("kind"):
            work.pop("kind", None)
        for block in ("binding", "contract"):
            keys = synthesized.get(block)
            if not isinstance(keys, list):
                continue
            inner = dict(work.get(block) or {})
            for key in keys:
                inner.pop(key, None)
            # A block whose every key was invented was itself absent from the
            # source document.
            if inner:
                work[block] = inner
            else:
                work.pop(block, None)
    return work
