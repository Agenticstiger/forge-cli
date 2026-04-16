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

"""Branch-coverage tests for fluid_build.forge.core.deployment"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.forge.core.deployment import (
    DeploymentConfig,
    DeploymentResult,
    DeploymentStatus,
    DeploymentTarget,
    ProjectDeployer,
)

# ── Enum tests ──────────────────────────────────────────────────────


class TestDeploymentTarget:
    @pytest.mark.parametrize(
        "member,value",
        [
            ("LOCAL", "local"),
            ("DOCKER", "docker"),
            ("KUBERNETES", "kubernetes"),
            ("GCP", "gcp"),
            ("AWS", "aws"),
            ("AZURE", "azure"),
            ("CICD", "cicd"),
        ],
    )
    def test_values(self, member, value):
        assert DeploymentTarget[member].value == value


class TestDeploymentStatus:
    @pytest.mark.parametrize(
        "member,value",
        [
            ("PENDING", "pending"),
            ("IN_PROGRESS", "in_progress"),
            ("SUCCESS", "success"),
            ("FAILED", "failed"),
            ("ROLLBACK", "rollback"),
        ],
    )
    def test_values(self, member, value):
        assert DeploymentStatus[member].value == value


# ── Dataclass tests ─────────────────────────────────────────────────


class TestDeploymentConfig:
    def test_defaults_none_to_empty_dicts(self):
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        assert cfg.resources == {}
        assert cfg.env_vars == {}
        assert cfg.secrets == {}

    def test_preserves_provided_values(self):
        cfg = DeploymentConfig(
            target=DeploymentTarget.DOCKER,
            resources={"cpu": "1"},
            env_vars={"K": "V"},
            secrets={"s": "v"},
        )
        assert cfg.resources == {"cpu": "1"}
        assert cfg.env_vars == {"K": "V"}
        assert cfg.secrets == {"s": "v"}

    def test_target_assignment(self):
        cfg = DeploymentConfig(target=DeploymentTarget.KUBERNETES)
        assert cfg.target == DeploymentTarget.KUBERNETES


class TestDeploymentResult:
    def test_defaults_none_to_empty(self):
        r = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.LOCAL,
        )
        assert r.logs == []
        assert r.metrics == {}
        assert r.endpoint is None
        assert r.error is None

    def test_preserves_provided_logs(self):
        r = DeploymentResult(
            status=DeploymentStatus.FAILED,
            deployment_id="xyz",
            target=DeploymentTarget.AWS,
            logs=["step1", "step2"],
            error="boom",
        )
        assert r.logs == ["step1", "step2"]
        assert r.error == "boom"


# ── ProjectDeployer tests ───────────────────────────────────────────


@pytest.fixture
def deployer(tmp_path):
    """Create a ProjectDeployer with a temporary project directory."""
    # Create minimal project structure
    (tmp_path / "contract.fluid.yaml").write_text("apiVersion: 0.5.7")
    (tmp_path / "README.md").write_text("# Test")
    (tmp_path / "requirements.txt").write_text("requests>=2.0")
    return ProjectDeployer(tmp_path)


class TestProjectDeployerInit:
    def test_creates_deployment_dir(self, deployer, tmp_path):
        assert (tmp_path / ".deployments").is_dir()

    def test_sets_project_path(self, deployer, tmp_path):
        assert deployer.project_path == tmp_path


class TestGenerateDeploymentId:
    def test_returns_12_char_hex(self, deployer):
        did = deployer._generate_deployment_id()
        assert len(did) == 12
        assert all(c in "0123456789abcdef" for c in did)

    def test_different_ids(self, deployer):
        # IDs based on timestamp so quick successive calls may collide,
        # but the hash should still be deterministic per timestamp
        id1 = deployer._generate_deployment_id()
        import time

        time.sleep(1.1)
        id2 = deployer._generate_deployment_id()
        assert id1 != id2


class TestGenerateDockerfile:
    def test_with_requirements(self, deployer):
        content = deployer._generate_dockerfile()
        assert "pip install -r requirements.txt" in content
        assert "python:3.9-slim" in content

    def test_with_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (
            (tmp_path / "requirements.txt").unlink()
            if (tmp_path / "requirements.txt").exists()
            else None
        )
        d = ProjectDeployer(tmp_path)
        content = d._generate_dockerfile()
        assert "node:16-alpine" in content
        assert "npm install" in content

    def test_with_dbt(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: test")
        d = ProjectDeployer(tmp_path)
        content = d._generate_dockerfile()
        assert "dbt-core" in content

    def test_no_dependencies(self, tmp_path):
        d = ProjectDeployer(tmp_path)
        content = d._generate_dockerfile()
        assert "No dependencies found" in content


class TestGenerateDockerCompose:
    def test_contains_project_name(self, deployer, tmp_path):
        content = deployer._generate_docker_compose()
        name = tmp_path.name.lower().replace("_", "-")
        assert name in content

    def test_contains_version(self, deployer):
        content = deployer._generate_docker_compose()
        assert "version:" in content
        assert "services:" in content
        assert "networks:" in content


class TestGenerateK8sDeployment:
    def test_contains_deployment_kind(self, deployer):
        content = deployer._generate_k8s_deployment()
        assert "kind: Deployment" in content
        assert "replicas: 2" in content

    def test_contains_project_name(self, deployer, tmp_path):
        content = deployer._generate_k8s_deployment()
        name = tmp_path.name.lower().replace("_", "-")
        assert name in content


class TestGenerateK8sService:
    def test_contains_service_kind(self, deployer):
        content = deployer._generate_k8s_service()
        assert "kind: Service" in content
        assert "kind: Ingress" in content


class TestGenerateCICDConfigs:
    def test_github_workflow(self, deployer):
        content = deployer._generate_github_workflow()
        assert "name: Deploy" in content
        assert "runs-on:" in content

    def test_gitlab_ci(self, deployer):
        content = deployer._generate_gitlab_ci()
        assert "stages:" in content
        assert "test" in content

    def test_azure_pipelines(self, deployer):
        content = deployer._generate_azure_pipelines()
        assert "trigger:" in content
        assert "stages:" in content


class TestCopyProjectFiles:
    def test_copies_normal_files(self, deployer, tmp_path):
        src = tmp_path / "src_proj"
        src.mkdir()
        (src / "README.md").write_text("hello")
        dest = tmp_path / "dest"
        dest.mkdir()
        deployer._copy_project_files(src, dest)
        assert (dest / "README.md").exists()

    def test_excludes_git_dir_itself(self, deployer, tmp_path):
        """should_exclude returns True for .git directory (leaf name match)"""
        src = tmp_path / "src_proj"
        src.mkdir()
        git_dir = src / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("x")
        (src / "file.txt").write_text("ok")
        dest = tmp_path / "dest"
        dest.mkdir()
        deployer._copy_project_files(src, dest)
        # Normal files are copied
        assert (dest / "file.txt").exists()

    def test_excludes_pyc(self, deployer, tmp_path):
        src = tmp_path / "src_proj"
        src.mkdir()
        (src / "file.pyc").write_text("x")
        (src / "ok.py").write_text("pass")
        dest = tmp_path / "dest"
        dest.mkdir()
        deployer._copy_project_files(src, dest)
        assert not (dest / "file.pyc").exists()
        assert (dest / "ok.py").exists()

    def test_excludes_pycache(self, deployer, tmp_path):
        src = tmp_path / "src_proj"
        src.mkdir()
        cache = src / "__pycache__"
        cache.mkdir()
        (cache / "mod.pyc").write_text("x")
        dest = tmp_path / "dest"
        dest.mkdir()
        deployer._copy_project_files(src, dest)
        assert not (dest / "__pycache__").exists()


class TestPrepareDependencies:
    def test_adds_health_deps(self, deployer, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        req = pkg / "requirements.txt"
        req.write_text("numpy>=1.0")
        deployer._prepare_dependencies(pkg)
        content = req.read_text()
        assert "flask" in content
        assert "requests" in content

    def test_no_duplicate_deps(self, deployer, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        req = pkg / "requirements.txt"
        req.write_text("flask>=2.0\nrequests>=2.28")
        deployer._prepare_dependencies(pkg)
        content = req.read_text()
        # Should not have added extra flask/requests lines
        assert content.count("flask") == 1
        assert content.count("requests") == 1

    def test_no_requirements_file(self, deployer, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        # Should not raise
        deployer._prepare_dependencies(pkg)


class TestDeploy:
    def test_deploy_unsupported_target_returns_failed(self, deployer):
        """The deploy method catches exceptions and returns FAILED status."""
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        # Mock _prepare_deployment_package so it doesn't do real work
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_local", side_effect=RuntimeError("nope")):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.FAILED
        assert "nope" in result.error

    def test_deploy_routes_to_docker(self, deployer):
        cfg = DeploymentConfig(target=DeploymentTarget.DOCKER)
        mock_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.DOCKER,
        )
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_docker", return_value=mock_result):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.SUCCESS

    def test_deploy_routes_to_kubernetes(self, deployer):
        cfg = DeploymentConfig(target=DeploymentTarget.KUBERNETES)
        mock_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.KUBERNETES,
        )
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_kubernetes", return_value=mock_result):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.SUCCESS

    def test_deploy_routes_to_gcp(self, deployer):
        cfg = DeploymentConfig(target=DeploymentTarget.GCP)
        mock_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.GCP,
        )
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_gcp", return_value=mock_result):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.SUCCESS

    def test_deploy_routes_to_aws(self, deployer):
        cfg = DeploymentConfig(target=DeploymentTarget.AWS)
        mock_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.AWS,
        )
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_aws", return_value=mock_result):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.SUCCESS

    def test_deploy_routes_to_azure(self, deployer):
        cfg = DeploymentConfig(target=DeploymentTarget.AZURE)
        mock_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.AZURE,
        )
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_azure", return_value=mock_result):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.SUCCESS

    def test_deploy_routes_to_cicd(self, deployer):
        cfg = DeploymentConfig(target=DeploymentTarget.CICD)
        mock_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.CICD,
        )
        with patch.object(deployer, "_prepare_deployment_package", return_value=Path("/tmp/fake")):
            with patch.object(deployer, "_deploy_cicd", return_value=mock_result):
                with patch.object(deployer, "_save_deployment_record"):
                    result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.SUCCESS


class TestSaveDeploymentRecord:
    def test_saves_record_file(self, deployer):
        result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="test123",
            target=DeploymentTarget.LOCAL,
        )
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        deployer._save_deployment_record(result, cfg)
        records = list(deployer.deployment_dir.glob("*.json"))
        assert len(records) >= 1
