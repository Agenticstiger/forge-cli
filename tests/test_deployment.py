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

"""Tests for fluid_build.forge.core.deployment"""

from unittest.mock import patch

from fluid_build.forge.core.deployment import (
    DeploymentConfig,
    DeploymentResult,
    DeploymentStatus,
    DeploymentTarget,
    ProjectDeployer,
)

# ── Enum tests ──


class TestEnums:
    def test_deployment_targets(self):
        assert DeploymentTarget.LOCAL.value == "local"
        assert DeploymentTarget.KUBERNETES.value == "kubernetes"

    def test_deployment_status(self):
        assert DeploymentStatus.PENDING.value == "pending"
        assert DeploymentStatus.ROLLBACK.value == "rollback"


# ── Dataclass tests ──


class TestDeploymentConfig:
    def test_post_init_defaults(self):
        c = DeploymentConfig(target=DeploymentTarget.LOCAL)
        assert c.resources == {}
        assert c.env_vars == {}
        assert c.secrets == {}

    def test_explicit_values_preserved(self):
        c = DeploymentConfig(
            target=DeploymentTarget.DOCKER,
            environment="prod",
            namespace="ns",
            region="us-east-1",
            resources={"cpu": "2"},
            env_vars={"A": "B"},
            secrets={"S": "V"},
        )
        assert c.environment == "prod"
        assert c.resources == {"cpu": "2"}


class TestDeploymentResult:
    def test_post_init_defaults(self):
        r = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc",
            target=DeploymentTarget.LOCAL,
        )
        assert r.logs == []
        assert r.metrics == {}
        assert r.error is None
        assert r.endpoint is None


# ── ProjectDeployer tests ──


class TestProjectDeployer:
    def test_init_creates_deployment_dir(self, tmp_path):
        ProjectDeployer(tmp_path)
        assert (tmp_path / ".deployments").is_dir()

    def test_generate_deployment_id(self, tmp_path):
        deployer = ProjectDeployer(tmp_path)
        id1 = deployer._generate_deployment_id()
        assert isinstance(id1, str)
        assert len(id1) == 12

    def test_generate_dockerfile_python(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n")
        deployer = ProjectDeployer(tmp_path)
        dockerfile = deployer._generate_dockerfile()
        assert "python:3.9-slim" in dockerfile
        assert "pip install -r requirements.txt" in dockerfile

    def test_generate_dockerfile_node(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        deployer = ProjectDeployer(tmp_path)
        dockerfile = deployer._generate_dockerfile()
        assert "node:16-alpine" in dockerfile
        assert "npm install" in dockerfile

    def test_generate_dockerfile_dbt(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: test")
        deployer = ProjectDeployer(tmp_path)
        dockerfile = deployer._generate_dockerfile()
        assert "dbt-core" in dockerfile

    def test_generate_dockerfile_empty(self, tmp_path):
        deployer = ProjectDeployer(tmp_path)
        dockerfile = deployer._generate_dockerfile()
        assert "No dependencies found" in dockerfile

    def test_generate_docker_compose(self, tmp_path):
        deployer = ProjectDeployer(tmp_path)
        compose = deployer._generate_docker_compose()
        project_name = tmp_path.name.lower().replace("_", "-")
        assert project_name in compose
        assert "8080:8080" in compose
        assert "version:" in compose

    def test_generate_k8s_deployment(self, tmp_path):
        deployer = ProjectDeployer(tmp_path)
        k8s = deployer._generate_k8s_deployment()
        assert "kind: Deployment" in k8s
        assert "replicas:" in k8s

    def test_generate_k8s_service(self, tmp_path):
        deployer = ProjectDeployer(tmp_path)
        svc = deployer._generate_k8s_service()
        assert "kind: Service" in svc

    def test_copy_project_files_excludes(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("print('hi')")
        (src / "test.pyc").write_text("bytecode")
        cache = src / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython.pyc").write_text("bc")

        dst = tmp_path / "dst"
        dst.mkdir()

        deployer = ProjectDeployer(src)
        deployer._copy_project_files(src, dst)

        assert (dst / "main.py").exists()
        assert not (dst / "test.pyc").exists()

    def test_deploy_error_returns_failed_result(self, tmp_path):
        deployer = ProjectDeployer(tmp_path)
        config = DeploymentConfig(target=DeploymentTarget.LOCAL)
        # _prepare_deployment_package will fail in temp dir without real project
        with patch.object(
            deployer, "_prepare_deployment_package", side_effect=RuntimeError("boom")
        ):
            with patch.object(deployer, "_save_deployment_record"):
                result = deployer.deploy(config)
        assert result.status == DeploymentStatus.FAILED
        assert "boom" in result.error
