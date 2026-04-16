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

"""Extended engine.py tests: _create_folder_structure, _write_contract_file, _write_provider_config, _apply_intelligent_defaults."""

import json

import pytest

from fluid_build.forge.core.engine import ForgeEngine


@pytest.fixture
def engine():
    e = ForgeEngine.__new__(ForgeEngine)
    e.project_config = {}
    e.console = None
    e.generation_context = None
    e.session_stats = {}
    return e


class TestCreateFolderStructure:
    def test_flat_dirs(self, engine, tmp_path):
        structure = {"src/": {}, "tests/": {}, "docs/": {}}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src").is_dir()
        assert (tmp_path / "tests").is_dir()
        assert (tmp_path / "docs").is_dir()

    def test_nested_dirs(self, engine, tmp_path):
        structure = {"src/": {"models/": {}, "utils/": {}}}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src" / "models").is_dir()
        assert (tmp_path / "src" / "utils").is_dir()

    def test_files_ignored(self, engine, tmp_path):
        structure = {"src/": {}, "README.md": "content"}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src").is_dir()
        # Files are not created by _create_folder_structure
        assert not (tmp_path / "README.md").exists()

    def test_deeply_nested(self, engine, tmp_path):
        structure = {"a/": {"b/": {"c/": {}}}}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "a" / "b" / "c").is_dir()


class TestWriteContractFile:
    def test_writes_yaml(self, engine, tmp_path):
        contract = {"version": "0.7.1", "kind": "fluid", "name": "test"}
        engine._write_contract_file(tmp_path, contract)
        f = tmp_path / "contract.fluid.yaml"
        assert f.exists()
        content = f.read_text()
        assert "version:" in content
        assert "test" in content

    def test_contract_fields(self, engine, tmp_path):
        contract = {"id": "btc", "name": "BTC Tracker", "domain": "finance"}
        engine._write_contract_file(tmp_path, contract)
        content = (tmp_path / "contract.fluid.yaml").read_text()
        assert "btc" in content
        assert "BTC Tracker" in content


class TestWriteProviderConfig:
    def test_writes_json(self, engine, tmp_path):
        config = {"provider": "gcp", "project": "my-proj"}
        engine._write_provider_config(tmp_path, config)
        f = tmp_path / "config" / "provider.json"
        assert f.exists()
        data = json.loads(f.read_text())
        assert data["provider"] == "gcp"

    def test_creates_config_dir(self, engine, tmp_path):
        engine._write_provider_config(tmp_path, {"a": 1})
        assert (tmp_path / "config").is_dir()


class TestApplyIntelligentDefaults:
    def test_fills_missing(self, engine):
        engine.project_config = {}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "my-data-product"
        assert engine.project_config["template"] == "starter"
        assert engine.project_config["provider"] == "local"

    def test_preserves_existing(self, engine):
        engine.project_config = {"name": "custom", "domain": "finance"}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "custom"
        assert engine.project_config["domain"] == "finance"
        # But fills in missing fields
        assert engine.project_config["template"] == "starter"

    def test_all_defaults_set(self, engine):
        engine.project_config = {}
        engine._apply_intelligent_defaults()
        expected_keys = [
            "name",
            "description",
            "domain",
            "owner",
            "template",
            "provider",
            "fluid_version",
            "target_dir",
        ]
        for key in expected_keys:
            assert key in engine.project_config
