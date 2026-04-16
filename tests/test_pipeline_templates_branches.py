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

"""Branch-coverage tests for fluid_build.forge.core.pipeline_templates"""

import pytest

from fluid_build.forge.core.pipeline_templates import (
    BasePipelineTemplate,
    GitHubActionsTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

# ── Enum tests ──────────────────────────────────────────────────────


class TestPipelineProvider:
    @pytest.mark.parametrize(
        "member,value",
        [
            ("GITHUB_ACTIONS", "github_actions"),
            ("GITLAB_CI", "gitlab_ci"),
            ("AZURE_DEVOPS", "azure_devops"),
            ("JENKINS", "jenkins"),
            ("BITBUCKET", "bitbucket"),
            ("CIRCLE_CI", "circle_ci"),
            ("TEKTON", "tekton"),
        ],
    )
    def test_values(self, member, value):
        assert PipelineProvider[member].value == value


class TestPipelineComplexity:
    @pytest.mark.parametrize(
        "member,value",
        [
            ("BASIC", "basic"),
            ("STANDARD", "standard"),
            ("ADVANCED", "advanced"),
            ("ENTERPRISE", "enterprise"),
        ],
    )
    def test_values(self, member, value):
        assert PipelineComplexity[member].value == value


# ── PipelineConfig tests ────────────────────────────────────────────


class TestPipelineConfig:
    def test_basic_sets_dev_only(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.environments == ["dev"]

    def test_standard_sets_dev_staging(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.STANDARD,
        )
        assert cfg.environments == ["dev", "staging"]

    def test_advanced_sets_all_envs(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ADVANCED,
        )
        assert cfg.environments == ["dev", "staging", "prod"]

    def test_enterprise_sets_all_envs(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ENTERPRISE,
        )
        assert cfg.environments == ["dev", "staging", "prod"]

    def test_custom_environments_preserved(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
            environments=["qa", "prod"],
        )
        assert cfg.environments == ["qa", "prod"]

    def test_notification_channels_default_empty(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.notification_channels == []

    def test_custom_steps_default_empty(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.custom_steps == []

    def test_enable_flags_defaults(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.enable_approvals is False
        assert cfg.enable_security_scan is True
        assert cfg.enable_performance_monitoring is True
        assert cfg.enable_marketplace_publishing is False


# ── BasePipelineTemplate tests ──────────────────────────────────────


class TestBasePipelineTemplate:
    def test_init_defaults(self):
        t = BasePipelineTemplate()
        assert t.provider_name == "unknown"
        assert t.file_extensions == [".yml"]

    def test_generate_raises_not_implemented(self):
        t = BasePipelineTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        with pytest.raises(NotImplementedError):
            t.generate(config)

    def test_get_features(self):
        t = BasePipelineTemplate()
        features = t.get_features()
        assert features["multi_environment"] is True
        assert features["approvals"] is True
        assert features["security_scanning"] is True
        assert features["artifact_management"] is True
        assert features["notifications"] is True
        assert features["parallel_execution"] is True
        assert features["matrix_builds"] is True

    def test_get_fluid_commands(self):
        t = BasePipelineTemplate()
        cmds = t._get_fluid_commands()
        assert "validate" in cmds
        assert "plan" in cmds
        assert "apply" in cmds
        assert "test" in cmds
        assert "contract_test" in cmds
        assert "visualize" in cmds
        assert "publish_opds" in cmds
        assert "marketplace_publish" in cmds
        assert "doctor" in cmds
        assert "fluid" in cmds["validate"]

    def test_get_common_environment_vars(self):
        t = BasePipelineTemplate()
        env = t._get_common_environment_vars()
        assert "FLUID_LOG_LEVEL" in env
        assert "FLUID_CONFIG_PATH" in env
        assert "PYTHONPATH" in env
        assert "PIP_CACHE_DIR" in env


# ── PipelineTemplateGenerator tests ─────────────────────────────────


class TestPipelineTemplateGenerator:
    def test_init_populates_templates(self):
        gen = PipelineTemplateGenerator()
        assert PipelineProvider.GITHUB_ACTIONS in gen.templates
        assert PipelineProvider.GITLAB_CI in gen.templates
        assert PipelineProvider.AZURE_DEVOPS in gen.templates
        assert PipelineProvider.JENKINS in gen.templates
        assert PipelineProvider.BITBUCKET in gen.templates
        assert PipelineProvider.CIRCLE_CI in gen.templates
        assert PipelineProvider.TEKTON in gen.templates

    def test_list_available_providers(self):
        gen = PipelineTemplateGenerator()
        providers = gen.list_available_providers()
        assert "github_actions" in providers
        assert "jenkins" in providers

    def test_generate_unsupported_provider(self):
        gen = PipelineTemplateGenerator()
        # Remove a provider to trigger error
        del gen.templates[PipelineProvider.TEKTON]
        config = PipelineConfig(
            provider=PipelineProvider.TEKTON,
            complexity=PipelineComplexity.BASIC,
        )
        with pytest.raises(ValueError, match="Unsupported provider"):
            gen.generate_pipeline(config)

    def test_get_provider_features_valid(self):
        gen = PipelineTemplateGenerator()
        features = gen.get_provider_features(PipelineProvider.GITHUB_ACTIONS)
        assert "multi_environment" in features

    def test_get_provider_features_invalid(self):
        gen = PipelineTemplateGenerator()
        del gen.templates[PipelineProvider.TEKTON]
        features = gen.get_provider_features(PipelineProvider.TEKTON)
        assert features == {}


# ── GitHubActionsTemplate generate branches ─────────────────────────


class TestGitHubActionsGenerate:
    def test_init(self):
        t = GitHubActionsTemplate()
        assert t.provider_name == "GitHub Actions"
        assert ".yml" in t.file_extensions

    def test_basic_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        result = t.generate(config)
        assert isinstance(result, dict)
        assert any("github/workflows" in k for k in result)

    def test_standard_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.STANDARD,
        )
        result = t.generate(config)
        assert isinstance(result, dict)

    def test_advanced_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ADVANCED,
        )
        result = t.generate(config)
        assert isinstance(result, dict)

    def test_enterprise_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ENTERPRISE,
        )
        result = t.generate(config)
        assert isinstance(result, dict)


# ── Generate pipeline through generator ─────────────────────────────


class TestGeneratePipeline:
    @pytest.mark.parametrize("provider", list(PipelineProvider))
    def test_all_providers_basic(self, provider):
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(
            provider=provider,
            complexity=PipelineComplexity.BASIC,
        )
        result = gen.generate_pipeline(config)
        assert isinstance(result, dict)
        assert len(result) > 0

    @pytest.mark.parametrize("complexity", list(PipelineComplexity))
    def test_github_all_complexities(self, complexity):
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=complexity,
        )
        result = gen.generate_pipeline(config)
        assert isinstance(result, dict)
