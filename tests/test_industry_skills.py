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

"""Tests for fluid_build.cli.industry_skills — loader, generator, refresh."""

from __future__ import annotations

import pytest
import yaml


class TestLoadTools:
    def test_returns_dict(self):
        from fluid_build.cli.industry_skills import load_tools

        tools = load_tools()
        assert isinstance(tools, dict)

    def test_has_platforms(self):
        from fluid_build.cli.industry_skills import load_tools

        tools = load_tools()
        assert "platforms" in tools
        names = [p["name"] for p in tools["platforms"]]
        assert "snowflake" in names
        assert "local" in names

    def test_has_transformations(self):
        from fluid_build.cli.industry_skills import load_tools

        tools = load_tools()
        assert "transformations" in tools
        names = [t["name"] for t in tools["transformations"]]
        assert "dbt" in names

    def test_has_orchestration(self):
        from fluid_build.cli.industry_skills import load_tools

        tools = load_tools()
        assert "orchestration" in tools
        names = [o["name"] for o in tools["orchestration"]]
        assert "airflow" in names

    def test_has_formats(self):
        from fluid_build.cli.industry_skills import load_tools

        tools = load_tools()
        assert "formats" in tools
        names = [f["name"] for f in tools["formats"]]
        assert "parquet" in names


class TestLoadIndustrySkills:
    @pytest.mark.parametrize("name", ["telco", "retail", "healthcare", "finance"])
    def test_loads_industry(self, name):
        from fluid_build.cli.industry_skills import load_industry_skills

        skills = load_industry_skills(name)
        assert "industry" in skills
        assert skills["industry"]["agent"] == name
        assert "canonical_model" in skills
        assert "domains" in skills
        assert len(skills["domains"]) > 0
        assert "compliance" in skills
        assert "common_data_sources" in skills

    def test_missing_industry_raises(self):
        from fluid_build.cli.industry_skills import load_industry_skills

        with pytest.raises(FileNotFoundError):
            load_industry_skills("nonexistent")

    def test_telco_has_tmf_sid(self):
        from fluid_build.cli.industry_skills import load_industry_skills

        skills = load_industry_skills("telco")
        assert skills["canonical_model"]["primary"] == "tmf_sid"

    def test_healthcare_has_hl7_fhir(self):
        from fluid_build.cli.industry_skills import load_industry_skills

        skills = load_industry_skills("healthcare")
        assert skills["canonical_model"]["primary"] == "hl7_fhir"

    def test_retail_has_nrf_arts(self):
        from fluid_build.cli.industry_skills import load_industry_skills

        skills = load_industry_skills("retail")
        assert skills["canonical_model"]["primary"] == "nrf_arts"

    def test_finance_has_iso_20022(self):
        from fluid_build.cli.industry_skills import load_industry_skills

        skills = load_industry_skills("finance")
        assert skills["canonical_model"]["primary"] == "iso_20022"


class TestListIndustries:
    def test_returns_list(self):
        from fluid_build.cli.industry_skills import list_industries

        industries = list_industries()
        assert isinstance(industries, list)
        assert len(industries) >= 5  # 4 industries + "other"

    def test_includes_other(self):
        from fluid_build.cli.industry_skills import list_industries

        industries = list_industries()
        keys = [i["key"] for i in industries]
        assert "other" in keys
        # "other" must be last
        assert industries[-1]["key"] == "other"

    def test_each_has_key_label_description(self):
        from fluid_build.cli.industry_skills import list_industries

        for ind in list_industries():
            assert "key" in ind
            assert "label" in ind
            assert "description" in ind

    @pytest.mark.parametrize("name", ["telco", "retail", "healthcare", "finance"])
    def test_includes_industry(self, name):
        from fluid_build.cli.industry_skills import list_industries

        keys = [i["key"] for i in list_industries()]
        assert name in keys


class TestGenerateSkillsFile:
    def test_generates_for_telco(self, tmp_path):
        from fluid_build.cli.industry_skills import generate_skills_file

        out = generate_skills_file("telco", tmp_path, cli_version="0.7.9")
        assert out.exists()
        assert out == tmp_path / ".fluid" / "skills.yaml"

        with out.open() as f:
            data = yaml.safe_load(f)
        assert data["_version"] == "0.7.9"
        assert data["industry"]["name"] == "telecommunications"
        assert data["canonical_model"]["primary"] == "tmf_sid"
        assert "tools" in data
        assert "platforms" in data["tools"]

    def test_generates_for_other(self, tmp_path):
        from fluid_build.cli.industry_skills import generate_skills_file

        out = generate_skills_file("other", tmp_path, cli_version="0.7.9")
        assert out.exists()

        with out.open() as f:
            data = yaml.safe_load(f)
        assert "tools" in data
        assert "industry" not in data  # no industry for "other"

    def test_generates_for_none(self, tmp_path):
        from fluid_build.cli.industry_skills import generate_skills_file

        out = generate_skills_file(None, tmp_path, cli_version="0.7.9")
        assert out.exists()

        with out.open() as f:
            data = yaml.safe_load(f)
        assert "tools" in data
        assert "industry" not in data

    def test_creates_fluid_dir(self, tmp_path):
        from fluid_build.cli.industry_skills import generate_skills_file

        generate_skills_file("retail", tmp_path)
        assert (tmp_path / ".fluid").is_dir()

    def test_has_header_comment(self, tmp_path):
        from fluid_build.cli.industry_skills import generate_skills_file

        out = generate_skills_file("finance", tmp_path)
        content = out.read_text()
        assert "Auto-generated by fluid init" in content
        assert "fluid skills update" in content

    @pytest.mark.parametrize("name", ["telco", "retail", "healthcare", "finance"])
    def test_all_industries_generate(self, tmp_path, name):
        from fluid_build.cli.industry_skills import generate_skills_file

        out = generate_skills_file(name, tmp_path)
        assert out.exists()
        with out.open() as f:
            data = yaml.safe_load(f)
        assert data["industry"]["agent"] == name
        assert len(data["domains"]) > 0
        assert len(data["tools"]["platforms"]) > 0


class TestRefreshToolsSection:
    def test_refreshes_tools(self, tmp_path):
        from fluid_build.cli.industry_skills import (
            generate_skills_file,
            load_tools,
            refresh_tools_section,
        )

        # Generate initial file
        out = generate_skills_file("telco", tmp_path, cli_version="0.7.8")
        with out.open() as f:
            old_data = yaml.safe_load(f)
        assert old_data["_version"] == "0.7.8"

        # Refresh
        refresh_tools_section(out, cli_version="0.7.9")
        with out.open() as f:
            new_data = yaml.safe_load(f)

        # Version updated
        assert new_data["_version"] == "0.7.9"
        # Industry preserved
        assert new_data["industry"]["name"] == "telecommunications"
        # Tools refreshed
        assert new_data["tools"] == load_tools()

    def test_preserves_industry_section(self, tmp_path):
        from fluid_build.cli.industry_skills import generate_skills_file, refresh_tools_section

        out = generate_skills_file("healthcare", tmp_path, cli_version="0.7.8")
        refresh_tools_section(out, cli_version="0.7.9")

        with out.open() as f:
            data = yaml.safe_load(f)
        assert data["industry"]["name"] == "healthcare"
        assert data["canonical_model"]["primary"] == "hl7_fhir"
        assert len(data["domains"]) > 0
        assert len(data["compliance"]) > 0


class TestSkillsCmd:
    def test_register(self):
        import argparse

        from fluid_build.cli.skills_cmd import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["skills", "update"])
        assert args.skills_action == "update"

    def test_run_no_action(self):
        import argparse
        import logging

        from fluid_build.cli.skills_cmd import run

        args = argparse.Namespace(skills_action=None)
        assert run(args, logging.getLogger("test")) == 1

    def test_show_no_workspace(self):
        import argparse
        import logging
        from unittest.mock import patch

        from fluid_build.cli.skills_cmd import run

        args = argparse.Namespace(skills_action="show")
        with patch("fluid_build.cli.skills_cmd.find_workspace_root", return_value=None):
            assert run(args, logging.getLogger("test")) == 1

    def test_update_no_workspace(self):
        import argparse
        import logging
        from unittest.mock import patch

        from fluid_build.cli.skills_cmd import run

        args = argparse.Namespace(skills_action="update")
        with patch("fluid_build.cli.skills_cmd.find_workspace_root", return_value=None):
            assert run(args, logging.getLogger("test")) == 1

    def test_update_no_skills_file(self, tmp_path):
        import argparse
        import logging
        from unittest.mock import patch

        from fluid_build.cli.skills_cmd import run

        args = argparse.Namespace(skills_action="update")
        with patch("fluid_build.cli.skills_cmd.find_workspace_root", return_value=tmp_path):
            assert run(args, logging.getLogger("test")) == 1

    def test_update_success(self, tmp_path):
        import argparse
        import logging
        from unittest.mock import patch

        from fluid_build.cli.industry_skills import generate_skills_file
        from fluid_build.cli.skills_cmd import run

        generate_skills_file("telco", tmp_path, cli_version="0.7.8")
        args = argparse.Namespace(skills_action="update")
        with patch("fluid_build.cli.skills_cmd.find_workspace_root", return_value=tmp_path):
            result = run(args, logging.getLogger("test"))
        assert result == 0

        # Verify version was updated
        with (tmp_path / ".fluid" / "skills.yaml").open() as f:
            data = yaml.safe_load(f)
        assert data["industry"]["name"] == "telecommunications"
