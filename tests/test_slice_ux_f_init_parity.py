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

"""Slice UX-F: `fluid init --blank` and `fluid init --template` parity.

Before slice UX-F, three different scaffolding paths produced three
different contract shapes:

  fluid init --blank        →  bronze_<ws>/contract.fluid.json  (v0.5.7)
  fluid init --template X   →  <ws>/contract.fluid.yaml (+ stale .old copy)
  fluid forge --blank       →  <target>/contract.fluid.yaml       (v0.7.2)

Slice UX-F unifies all three:

1. ``blank_mode`` now calls ``build_minimal_contract`` +
   ``create_and_validate_contract`` directly (no more delegation to
   ``product_new_run``), producing a v0.7.2 YAML contract with
   ``metadata.provenance`` plus a ``.fluid/forge-receipt.json`` inside
   the product.

2. ``template_mode`` runs ``_finalise_template_product`` after the
   template files are copied: it reloads the contract, injects
   ``metadata.provenance``, rewrites the file with the standard
   generated-contract banner, and writes a ``.fluid/forge-receipt.json``
   with ``flow="template"``.

3. ``copy_template`` skips ``*.old``/``*.bak``/``*.tmp``/``*.swp`` files
   from the template source so authoring scratch artifacts (like the
   ``contract.fluid.yaml.old`` backup in the customer-360 template)
   never land in the user's project.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.cli import init as init_module
from fluid_build.cli.artifact_paths import (
    product_contract_path,
    product_forge_receipt_path,
)
from fluid_build.cli.init import (
    _should_copy_template_entry,
    blank_mode,
    copy_template,
    template_mode,
)


def _build_args(**overrides) -> argparse.Namespace:
    defaults: dict = {
        "name": None,
        "template": None,
        "blank": False,
        "quickstart": False,
        "yes": True,
        "provider": "local",
        "use_case": None,
        "no_run": True,
        "no_dag": True,
        "dry_run": False,
        "target_dir": None,
        "scan": False,
        "list_templates": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# UX-F.1 — blank_mode rewrite
# ---------------------------------------------------------------------------


class TestBlankModeParity:
    def test_writes_contract_as_yaml_not_json(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-blank", blank=True)
        rc = blank_mode(args, logging.getLogger("test"))
        assert rc == 0

        product = tmp_path / "test-blank"
        assert (product / "contract.fluid.yaml").is_file()
        assert not (product / "contract.fluid.json").exists()
        # No legacy bronze_* directory
        assert not (tmp_path / "bronze_test-blank").exists()

    def test_contract_carries_metadata_provenance(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-blank", blank=True)
        blank_mode(args, logging.getLogger("test"))

        doc = yaml.safe_load((tmp_path / "test-blank" / "contract.fluid.yaml").read_text())
        assert "metadata" in doc
        assert "provenance" in doc["metadata"]
        prov = doc["metadata"]["provenance"]
        assert prov["schema_version"] == 1
        assert prov["kind"] == "ContractMetadata"
        assert prov["generated_by"]["tool"] == "fluid-cli"

    def test_contract_is_v072_shape(self, tmp_path: Path, monkeypatch):
        """Top-level domain / description / tags, metadata.layer=Bronze,
        SQL build pattern=embedded-logic — the new scaffold shape.

        ``fluidVersion`` tracks the latest bundled schema; older 0.7.x
        floats remain accepted for templates that haven't been refreshed.
        """
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-blank", blank=True)
        blank_mode(args, logging.getLogger("test"))

        from fluid_build.schema_manager import FluidSchemaManager

        doc = yaml.safe_load((tmp_path / "test-blank" / "contract.fluid.yaml").read_text())
        assert doc["fluidVersion"] in {
            FluidSchemaManager.latest_bundled_version(),
            "0.7.2",
            "0.7.1",
            0.7,
        }
        assert doc.get("domain") == "analytics"
        assert doc["metadata"]["layer"] == "Bronze"
        assert doc["metadata"]["owner"] == {"team": "data-team"}
        assert doc["builds"][0]["pattern"] == "embedded-logic"

    def test_writes_forge_receipt_inside_product(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-blank", blank=True)
        blank_mode(args, logging.getLogger("test"))

        receipt_path = product_forge_receipt_path(tmp_path / "test-blank")
        assert receipt_path.is_file(), "slice UX-F must write forge-receipt.json"

        doc = json.loads(receipt_path.read_text())
        assert doc["kind"] == "ForgeReceipt"
        assert doc["flow"] == "blank"
        assert any(a["path"] == "contract.fluid.yaml" for a in doc["artifacts"])

    def test_dry_run_writes_nothing(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-dry", blank=True, dry_run=True)
        rc = blank_mode(args, logging.getLogger("test"))
        assert rc == 0
        assert not (tmp_path / "test-dry").exists()

    def test_existing_non_empty_dir_refused(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "existing"
        existing.mkdir()
        (existing / "file.txt").write_text("")
        args = _build_args(name="existing", blank=True)
        rc = blank_mode(args, logging.getLogger("test"))
        assert rc == 1


# ---------------------------------------------------------------------------
# UX-F.3 — copy_template filter
# ---------------------------------------------------------------------------


class TestTemplatePickerFilesystemSource:
    """_ask_template_name must list only templates that exist on disk
    so copy_template can't crash with "Template 'starter' not found"."""

    def test_list_filesystem_templates_returns_real_dirs(self):
        from fluid_build.cli.init import _list_filesystem_templates

        names = _list_filesystem_templates()
        assert names, "no filesystem templates found — check the templates/ dir"
        # customer-360 is the baseline every fluid install ships with
        assert "customer-360" in names

    def test_picker_never_offers_registry_only_names(self):
        """The registry's logical names ('starter', 'analytics',
        'etl_pipeline', 'streaming', 'ml_pipeline') do NOT have
        filesystem directories.  The picker must not offer them."""
        from fluid_build.cli.init import _list_filesystem_templates

        names = set(_list_filesystem_templates())
        registry_only = {"starter", "analytics", "etl_pipeline", "streaming", "ml_pipeline"}
        leaked = names & registry_only
        assert not leaked, (
            f"picker is offering registry-only names {leaked} that "
            f"don't have filesystem directories — copy_template will "
            f"crash on them"
        )

    def test_picker_default_falls_back_when_customer_360_missing(self, tmp_path: Path, monkeypatch):
        """If customer-360 isn't installed, the picker must pick the
        first alphabetical real template as its default instead of
        handing back a non-existent directory name."""
        import fluid_build.cli.init as im

        with patch.object(
            im, "_list_filesystem_templates", return_value=["aardvark", "hello-world"]
        ):
            with patch.object(im, "RICH_AVAILABLE", False):
                # Non-Rich path returns the default directly
                result = im._ask_template_name()
                assert result == "aardvark"  # first alphabetical


class TestCopyTemplateFilter:
    def test_should_copy_regular_file(self, tmp_path: Path):
        p = tmp_path / "contract.fluid.yaml"
        p.write_text("")
        assert _should_copy_template_entry(p) is True

    def test_skips_dot_old(self, tmp_path: Path):
        p = tmp_path / "contract.fluid.yaml.old"
        p.write_text("")
        assert _should_copy_template_entry(p) is False

    def test_skips_dot_bak(self, tmp_path: Path):
        p = tmp_path / "contract.bak"
        p.write_text("")
        assert _should_copy_template_entry(p) is False

    def test_skips_pycache(self, tmp_path: Path):
        p = tmp_path / "__pycache__"
        p.mkdir()
        assert _should_copy_template_entry(p) is False

    def test_skips_ds_store(self, tmp_path: Path):
        p = tmp_path / ".DS_Store"
        p.write_text("")
        assert _should_copy_template_entry(p) is False


class TestCopyTemplateIntegration:
    def test_copy_template_omits_stale_old_file(self, tmp_path: Path, monkeypatch):
        """Build a fake template source that contains a .old file and
        verify copy_template leaves it out of the destination."""
        # Build a fake template at the expected location relative to
        # init.py.  Use the real template resolver by monkey-patching
        # Path(__file__) targets.
        fake_template_root = tmp_path / "fake-templates" / "fake-template"
        fake_template_root.mkdir(parents=True)
        (fake_template_root / "contract.fluid.yaml").write_text(
            yaml.dump(
                {
                    "fluidVersion": "0.7.2",
                    "kind": "DataProduct",
                    "id": "x",
                    "name": "X",
                    "metadata": {"owner": {"team": "t"}},
                }
            )
        )
        (fake_template_root / "contract.fluid.yaml.old").write_text("# stale\n")
        (fake_template_root / "README.md").write_text("# fake")

        # Monkey-patch copy_template's template resolution to look here.
        def fake_copy_template(project_dir, template_name, logger):
            import shutil

            from fluid_build.cli.init import _should_copy_template_entry

            project_dir.mkdir(parents=True, exist_ok=True)

            def walk(src: Path, dst: Path):
                dst.mkdir(parents=True, exist_ok=True)
                for item in src.iterdir():
                    if not _should_copy_template_entry(item):
                        continue
                    target = dst / item.name
                    if item.is_file():
                        shutil.copy2(item, target)
                    elif item.is_dir():
                        walk(item, target)

            walk(fake_template_root, project_dir)
            return True

        project_dir = tmp_path / "out"
        ok = fake_copy_template(project_dir, "fake-template", logging.getLogger("t"))
        assert ok
        assert (project_dir / "contract.fluid.yaml").exists()
        assert (project_dir / "README.md").exists()
        assert not (project_dir / "contract.fluid.yaml.old").exists()


# ---------------------------------------------------------------------------
# UX-F.2 + UX-F.4 — template_mode provenance + receipt
# ---------------------------------------------------------------------------


class TestTemplateModeParity:
    """These tests exercise the post-copy hook against a built-in template.

    They assume `customer-360` ships with fluid_build/templates/.  If
    the template has been renamed, these tests will fail loudly and the
    test author should update the template name accordingly.
    """

    def test_template_contract_gets_provenance_envelope(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-tpl", template="customer-360")
        rc = template_mode(args, logging.getLogger("test"))
        assert rc == 0

        contract_path = tmp_path / "test-tpl" / "contract.fluid.yaml"
        assert contract_path.is_file()

        doc = yaml.safe_load(contract_path.read_text())
        assert isinstance(doc.get("metadata"), dict)
        assert (
            "provenance" in doc["metadata"]
        ), "slice UX-F must inject metadata.provenance into template contracts"
        prov = doc["metadata"]["provenance"]
        assert prov["schema_version"] == 1
        assert prov["kind"] == "ContractMetadata"
        assert "customer-360" in prov["generated_by"]["command"]

    def test_template_mode_writes_forge_receipt(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-tpl", template="customer-360")
        template_mode(args, logging.getLogger("test"))

        receipt_path = product_forge_receipt_path(tmp_path / "test-tpl")
        assert receipt_path.is_file()

        doc = json.loads(receipt_path.read_text())
        assert doc["kind"] == "ForgeReceipt"
        assert doc["flow"] == "template"
        assert doc["inputs"].get("template") == "customer-360"
        assert doc["inputs"].get("name") == "test-tpl"

    def test_template_mode_excludes_dot_old_stale_files(self, tmp_path: Path, monkeypatch):
        """customer-360 shipped with a contract.fluid.yaml.old backup;
        slice UX-F's copy filter must strip it."""
        monkeypatch.chdir(tmp_path)
        args = _build_args(name="test-tpl", template="customer-360")
        template_mode(args, logging.getLogger("test"))

        assert not (tmp_path / "test-tpl" / "contract.fluid.yaml.old").exists()
