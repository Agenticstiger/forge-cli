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

"""Tests for fluid_build.forge.core.deployment."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.forge.core.deployment import (
    DeploymentConfig,
    DeploymentResult,
    DeploymentStatus,
    DeploymentTarget,
    ProjectDeployer,
    deploy_project,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deployer(tmp_path):
    """Return a ProjectDeployer whose project_path lives in tmp_path."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    return ProjectDeployer(project_dir)


# ---------------------------------------------------------------------------
# DeploymentTarget enum
# ---------------------------------------------------------------------------


class TestDeploymentTarget:
    def test_all_targets_present(self):
        values = {t.value for t in DeploymentTarget}
        for expected in ("local", "docker", "kubernetes", "gcp", "aws", "azure", "cicd"):
            assert expected in values

    def test_target_from_string(self):
        assert DeploymentTarget("docker") == DeploymentTarget.DOCKER


# ---------------------------------------------------------------------------
# DeploymentStatus enum
# ---------------------------------------------------------------------------


class TestDeploymentStatus:
    def test_all_statuses_present(self):
        values = {s.value for s in DeploymentStatus}
        for expected in ("pending", "in_progress", "success", "failed", "rollback"):
            assert expected in values


# ---------------------------------------------------------------------------
# DeploymentConfig dataclass
# ---------------------------------------------------------------------------


class TestDeploymentConfig:
    def test_defaults_are_populated(self):
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        assert cfg.environment == "development"
        assert cfg.resources == {}
        assert cfg.env_vars == {}
        assert cfg.secrets == {}
        assert cfg.namespace is None
        assert cfg.region is None

    def test_custom_values_are_stored(self):
        cfg = DeploymentConfig(
            target=DeploymentTarget.GCP,
            environment="production",
            region="us-central1",
            namespace="my-ns",
            env_vars={"KEY": "val"},
        )
        assert cfg.environment == "production"
        assert cfg.region == "us-central1"
        assert cfg.namespace == "my-ns"
        assert cfg.env_vars["KEY"] == "val"


# ---------------------------------------------------------------------------
# DeploymentResult dataclass
# ---------------------------------------------------------------------------


class TestDeploymentResult:
    def test_defaults_are_populated(self):
        res = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc123",
            target=DeploymentTarget.LOCAL,
        )
        assert res.logs == []
        assert res.metrics == {}
        assert res.endpoint is None
        assert res.error is None

    def test_custom_values_are_stored(self):
        res = DeploymentResult(
            status=DeploymentStatus.FAILED,
            deployment_id="xyz",
            target=DeploymentTarget.DOCKER,
            error="build failed",
            endpoint="http://localhost:8080",
            logs=["step 1"],
        )
        assert res.error == "build failed"
        assert res.endpoint == "http://localhost:8080"
        assert res.logs == ["step 1"]


# ---------------------------------------------------------------------------
# ProjectDeployer.__init__
# ---------------------------------------------------------------------------


class TestProjectDeployerInit:
    def test_creates_deployment_dir(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        assert deployer.deployment_dir.is_dir()

    def test_project_path_is_set_correctly(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        assert deployer.project_path == tmp_path / "my-project"

    def test_deployment_dir_is_inside_project(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        assert deployer.deployment_dir == deployer.project_path / ".deployments"


# ---------------------------------------------------------------------------
# ProjectDeployer._generate_deployment_id
# ---------------------------------------------------------------------------


class TestGenerateDeploymentId:
    def test_returns_12_char_hex_string(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        dep_id = deployer._generate_deployment_id()
        assert len(dep_id) == 12
        assert all(c in "0123456789abcdef" for c in dep_id)

    def test_returns_string_type(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        dep_id = deployer._generate_deployment_id()
        assert isinstance(dep_id, str)


# ---------------------------------------------------------------------------
# ProjectDeployer._generate_dockerfile
# ---------------------------------------------------------------------------


class TestGenerateDockerfile:
    def test_default_uses_python_slim_base(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_dockerfile()
        assert "python:3.9-slim" in content
        assert "FROM" in content

    def test_has_healthcheck_directive(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_dockerfile()
        assert "HEALTHCHECK" in content

    def test_uses_dbt_base_when_dbt_file_present(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        (deployer.project_path / "dbt_project.yml").write_text("name: my_dbt")
        content = deployer._generate_dockerfile()
        assert "dbt" in content

    def test_uses_requirements_install_when_present(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        (deployer.project_path / "requirements.txt").write_text("requests==2.28.0")
        content = deployer._generate_dockerfile()
        assert "requirements.txt" in content

    def test_uses_node_base_when_package_json_present(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        (deployer.project_path / "package.json").write_text("{}")
        content = deployer._generate_dockerfile()
        assert "node" in content


# ---------------------------------------------------------------------------
# ProjectDeployer._generate_docker_compose
# ---------------------------------------------------------------------------


class TestGenerateDockerCompose:
    def test_contains_project_name(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_docker_compose()
        assert "my-project" in content

    def test_contains_version_key(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_docker_compose()
        assert "version" in content


# ---------------------------------------------------------------------------
# ProjectDeployer._generate_k8s_deployment / _generate_k8s_service
# ---------------------------------------------------------------------------


class TestGenerateK8sManifests:
    def test_k8s_deployment_contains_project_name(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_k8s_deployment()
        assert "my-project" in content
        assert "Deployment" in content

    def test_k8s_service_contains_project_name(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_k8s_service()
        assert "my-project" in content
        assert "Service" in content

    def test_k8s_deployment_has_replicas(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        content = deployer._generate_k8s_deployment()
        assert "replicas" in content


# ---------------------------------------------------------------------------
# ProjectDeployer._deploy_local
# ---------------------------------------------------------------------------


class TestDeployLocal:
    def test_success_when_no_requirements(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        result = deployer._deploy_local("abc123", package_dir, cfg)
        assert result.status == DeploymentStatus.SUCCESS
        assert result.deployment_id == "abc123"
        assert result.endpoint is not None

    def test_success_when_requirements_install_passes(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        (package_dir / "requirements.txt").write_text("requests")
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with patch("subprocess.run", return_value=mock_proc):
            result = deployer._deploy_local("dep1", package_dir, cfg)
        assert result.status == DeploymentStatus.SUCCESS
        assert any("installed" in log.lower() for log in result.logs)

    def test_fails_when_requirements_install_fails(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        (package_dir / "requirements.txt").write_text("bad-package")
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "No matching distribution found"
        with patch("subprocess.run", return_value=mock_proc):
            result = deployer._deploy_local("dep2", package_dir, cfg)
        assert result.status == DeploymentStatus.FAILED
        assert result.error is not None

    def test_logs_contain_copy_message(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        result = deployer._deploy_local("dep_log", package_dir, cfg)
        assert any("copied" in log.lower() or "local" in log.lower() for log in result.logs)


# ---------------------------------------------------------------------------
# ProjectDeployer._deploy_docker
# ---------------------------------------------------------------------------


class TestDeployDocker:
    def _make_success_proc(self):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "sha256:abc"
        mock.stderr = ""
        return mock

    def test_success_path(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.DOCKER, env_vars={"ENV": "test"})
        with patch("subprocess.run", return_value=self._make_success_proc()):
            result = deployer._deploy_docker("dep3", package_dir, cfg)
        assert result.status == DeploymentStatus.SUCCESS
        assert result.endpoint == "http://localhost:8080"

    def test_fails_when_docker_build_fails(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.DOCKER)
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "Cannot connect to Docker daemon"
        with patch("subprocess.run", return_value=mock_fail):
            result = deployer._deploy_docker("dep4", package_dir, cfg)
        assert result.status == DeploymentStatus.FAILED
        assert result.error is not None

    def test_container_name_in_metrics(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.DOCKER)
        with patch("subprocess.run", return_value=self._make_success_proc()):
            result = deployer._deploy_docker("dep5", package_dir, cfg)
        assert "container" in result.metrics

    def test_image_name_in_metrics(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.DOCKER)
        with patch("subprocess.run", return_value=self._make_success_proc()):
            result = deployer._deploy_docker("dep6", package_dir, cfg)
        assert "image" in result.metrics


# ---------------------------------------------------------------------------
# ProjectDeployer._deploy_kubernetes
# ---------------------------------------------------------------------------


class TestDeployKubernetes:
    def test_success_when_kubectl_applies_succeed(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        k8s_dir = package_dir / "k8s"
        k8s_dir.mkdir(parents=True)
        (k8s_dir / "deployment.yaml").write_text("kind: Deployment")
        cfg = DeploymentConfig(target=DeploymentTarget.KUBERNETES, namespace="my-ns")
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_ok.stderr = ""
        with patch("subprocess.run", return_value=mock_ok):
            result = deployer._deploy_kubernetes("dep7", package_dir, cfg)
        assert result.status == DeploymentStatus.SUCCESS
        assert "my-ns" in result.endpoint

    def test_fails_when_kubectl_apply_fails(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        k8s_dir = package_dir / "k8s"
        k8s_dir.mkdir(parents=True)
        (k8s_dir / "deployment.yaml").write_text("kind: Deployment")
        cfg = DeploymentConfig(target=DeploymentTarget.KUBERNETES)
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "unauthorized"
        with patch("subprocess.run", return_value=mock_fail):
            result = deployer._deploy_kubernetes("dep8", package_dir, cfg)
        assert result.status == DeploymentStatus.FAILED

    def test_endpoint_uses_default_namespace(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        k8s_dir = package_dir / "k8s"
        k8s_dir.mkdir(parents=True)
        (k8s_dir / "svc.yaml").write_text("kind: Service")
        cfg = DeploymentConfig(target=DeploymentTarget.KUBERNETES)
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_ok.stderr = ""
        with patch("subprocess.run", return_value=mock_ok):
            result = deployer._deploy_kubernetes("dep9", package_dir, cfg)
        assert "default" in result.endpoint


# ---------------------------------------------------------------------------
# ProjectDeployer._deploy_gcp / _deploy_aws / _deploy_azure (stubs)
# ---------------------------------------------------------------------------


class TestDeployCloudStubs:
    def test_gcp_returns_failed_not_implemented(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.GCP)
        result = deployer._deploy_gcp("gcp1", tmp_path, cfg)
        assert result.status == DeploymentStatus.FAILED
        assert result.error is not None

    def test_aws_returns_failed_not_implemented(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.AWS)
        result = deployer._deploy_aws("aws1", tmp_path, cfg)
        assert result.status == DeploymentStatus.FAILED
        assert result.error is not None

    def test_azure_returns_failed_not_implemented(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.AZURE)
        result = deployer._deploy_azure("az1", tmp_path, cfg)
        assert result.status == DeploymentStatus.FAILED
        assert result.error is not None


# ---------------------------------------------------------------------------
# ProjectDeployer._deploy_cicd
# ---------------------------------------------------------------------------


class TestDeployCICD:
    def test_copies_github_actions_file_when_present(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "package"
        gh_dir = package_dir / ".github" / "workflows"
        gh_dir.mkdir(parents=True)
        (gh_dir / "deploy.yml").write_text("name: Deploy")
        cfg = DeploymentConfig(target=DeploymentTarget.CICD)
        result = deployer._deploy_cicd("cicd1", package_dir, cfg)
        assert result.status == DeploymentStatus.SUCCESS
        # The file should have been copied into the project path
        dest = deployer.project_path / ".github" / "workflows" / "deploy.yml"
        assert dest.exists()

    def test_success_even_when_no_cicd_files_found(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        package_dir = tmp_path / "empty_package"
        package_dir.mkdir()
        cfg = DeploymentConfig(target=DeploymentTarget.CICD)
        result = deployer._deploy_cicd("cicd2", package_dir, cfg)
        assert result.status == DeploymentStatus.SUCCESS


# ---------------------------------------------------------------------------
# ProjectDeployer.deploy — top-level routing & error handling
# ---------------------------------------------------------------------------


class TestProjectDeployerDeploy:
    def _success_result(self, dep_id="x"):
        return DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id=dep_id,
            target=DeploymentTarget.LOCAL,
        )

    def test_routes_to_local_target(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        with (
            patch.object(deployer, "_prepare_deployment_package", return_value=tmp_path),
            patch.object(
                deployer, "_deploy_local", return_value=self._success_result()
            ) as mock_local,
            patch.object(deployer, "_save_deployment_record"),
        ):
            result = deployer.deploy(cfg)
        mock_local.assert_called_once()
        assert result.status == DeploymentStatus.SUCCESS

    def test_routes_to_docker_target(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.DOCKER)
        docker_result = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="d",
            target=DeploymentTarget.DOCKER,
        )
        with (
            patch.object(deployer, "_prepare_deployment_package", return_value=tmp_path),
            patch.object(deployer, "_deploy_docker", return_value=docker_result) as mock_docker,
            patch.object(deployer, "_save_deployment_record"),
        ):
            result = deployer.deploy(cfg)
        mock_docker.assert_called_once()
        assert result.status == DeploymentStatus.SUCCESS

    def test_exception_produces_failed_result_with_error(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        with (
            patch.object(
                deployer, "_prepare_deployment_package", side_effect=RuntimeError("disk full")
            ),
            patch.object(deployer, "_save_deployment_record"),
        ):
            result = deployer.deploy(cfg)
        assert result.status == DeploymentStatus.FAILED
        assert "disk full" in result.error

    def test_saves_record_on_success(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        with (
            patch.object(deployer, "_prepare_deployment_package", return_value=tmp_path),
            patch.object(deployer, "_deploy_local", return_value=self._success_result()),
            patch.object(deployer, "_save_deployment_record") as mock_save,
        ):
            deployer.deploy(cfg)
        mock_save.assert_called_once()

    def test_saves_record_on_failure(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
        with (
            patch.object(deployer, "_prepare_deployment_package", side_effect=RuntimeError("boom")),
            patch.object(deployer, "_save_deployment_record") as mock_save,
        ):
            deployer.deploy(cfg)
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# ProjectDeployer list/get/_save deployment records
# ---------------------------------------------------------------------------


class TestDeploymentRecords:
    def test_list_deployments_empty_initially(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        assert deployer.list_deployments() == []

    def test_save_and_retrieve_record(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        res = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="abc123",
            target=DeploymentTarget.LOCAL,
            endpoint="file:///tmp/foo",
            logs=["all good"],
        )
        cfg = DeploymentConfig(target=DeploymentTarget.LOCAL, environment="staging")
        deployer._save_deployment_record(res, cfg)

        record = deployer.get_deployment("abc123")
        assert record is not None
        assert record["deployment_id"] == "abc123"
        assert record["status"] == "success"
        assert record["config"]["environment"] == "staging"

    def test_list_returns_saved_records(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        for dep_id in ("id_a", "id_b"):
            res = DeploymentResult(
                status=DeploymentStatus.SUCCESS,
                deployment_id=dep_id,
                target=DeploymentTarget.LOCAL,
            )
            cfg = DeploymentConfig(target=DeploymentTarget.LOCAL)
            deployer._save_deployment_record(res, cfg)

        records = deployer.list_deployments()
        assert len(records) == 2

    def test_get_deployment_returns_none_for_unknown_id(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        assert deployer.get_deployment("nonexistent_id") is None

    def test_list_deployments_sorted_newest_first(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        # Write records with manufactured timestamps
        for dep_id, ts in (("old_id", "2024-01-01T00:00:00"), ("new_id", "2025-01-01T00:00:00")):
            record = {
                "deployment_id": dep_id,
                "timestamp": ts,
                "status": "success",
                "target": "local",
                "config": {"environment": "dev", "namespace": None, "region": None},
                "result": {"endpoint": None, "error": None, "metrics": {}},
                "logs": [],
            }
            (deployer.deployment_dir / f"{dep_id}.json").write_text(json.dumps(record))

        records = deployer.list_deployments()
        assert records[0]["deployment_id"] == "new_id"
        assert records[1]["deployment_id"] == "old_id"


# ---------------------------------------------------------------------------
# ProjectDeployer.rollback_deployment
# ---------------------------------------------------------------------------


class TestRollbackDeployment:
    def test_rollback_unknown_id_returns_failed(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        result = deployer.rollback_deployment("no_such_id")
        assert result.status == DeploymentStatus.FAILED
        assert "not found" in result.error.lower()

    def test_rollback_docker_returns_rollback_status(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        record = {
            "deployment_id": "dep_docker",
            "target": "docker",
            "status": "success",
            "result": {
                "metrics": {"container": "my-container"},
                "endpoint": None,
                "error": None,
            },
            "logs": [],
        }
        (deployer.deployment_dir / "dep_docker.json").write_text(json.dumps(record))
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        with patch("subprocess.run", return_value=mock_ok):
            result = deployer.rollback_deployment("dep_docker")
        assert result.status == DeploymentStatus.ROLLBACK
        assert result.target == DeploymentTarget.DOCKER

    def test_rollback_kubernetes_succeeds(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        record = {
            "deployment_id": "dep_k8s",
            "target": "kubernetes",
            "status": "success",
            "result": {"metrics": {}, "endpoint": None, "error": None},
            "logs": [],
        }
        (deployer.deployment_dir / "dep_k8s.json").write_text(json.dumps(record))
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_ok.stderr = ""
        with patch("subprocess.run", return_value=mock_ok):
            result = deployer.rollback_deployment("dep_k8s")
        assert result.status == DeploymentStatus.ROLLBACK
        assert result.target == DeploymentTarget.KUBERNETES

    def test_rollback_kubernetes_failure(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        record = {
            "deployment_id": "dep_k8s_fail",
            "target": "kubernetes",
            "status": "success",
            "result": {"metrics": {}, "endpoint": None, "error": None},
            "logs": [],
        }
        (deployer.deployment_dir / "dep_k8s_fail.json").write_text(json.dumps(record))
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "not found"
        with patch("subprocess.run", return_value=mock_fail):
            result = deployer.rollback_deployment("dep_k8s_fail")
        assert result.status == DeploymentStatus.FAILED

    def test_rollback_local_target_not_implemented(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        record = {
            "deployment_id": "dep_local",
            "target": "local",
            "status": "success",
            "result": {"metrics": {}, "endpoint": None, "error": None},
            "logs": [],
        }
        (deployer.deployment_dir / "dep_local.json").write_text(json.dumps(record))
        result = deployer.rollback_deployment("dep_local")
        assert result.status == DeploymentStatus.FAILED
        assert "not implemented" in result.error.lower()

    def test_rollback_docker_without_container_name(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        record = {
            "deployment_id": "dep_no_container",
            "target": "docker",
            "status": "success",
            "result": {"metrics": {}, "endpoint": None, "error": None},
            "logs": [],
        }
        (deployer.deployment_dir / "dep_no_container.json").write_text(json.dumps(record))
        # No container name — should still return ROLLBACK (skips stop/rm)
        result = deployer.rollback_deployment("dep_no_container")
        assert result.status == DeploymentStatus.ROLLBACK


# ---------------------------------------------------------------------------
# deploy_project convenience function
# ---------------------------------------------------------------------------


class TestDeployProjectFunction:
    def test_creates_deployer_and_calls_deploy(self, tmp_path):
        project_path = str(tmp_path / "proj")
        Path(project_path).mkdir()
        expected = DeploymentResult(
            status=DeploymentStatus.SUCCESS,
            deployment_id="fn1",
            target=DeploymentTarget.LOCAL,
        )
        with patch.object(ProjectDeployer, "deploy", return_value=expected) as mock_deploy:
            result = deploy_project(project_path, "local", "production")
        mock_deploy.assert_called_once()
        assert result.status == DeploymentStatus.SUCCESS

    def test_passes_environment_to_config(self, tmp_path):
        project_path = str(tmp_path / "proj2")
        Path(project_path).mkdir()
        captured = []

        def capture_deploy(cfg):
            captured.append(cfg)
            return DeploymentResult(
                status=DeploymentStatus.SUCCESS,
                deployment_id="fn2",
                target=DeploymentTarget.LOCAL,
            )

        with patch.object(ProjectDeployer, "deploy", side_effect=capture_deploy):
            deploy_project(project_path, "local", "staging")

        assert captured[0].environment == "staging"


# ---------------------------------------------------------------------------
# ProjectDeployer._prepare_dependencies
# ---------------------------------------------------------------------------


class TestPrepareDependencies:
    def test_adds_health_deps_when_missing(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        req_file = pkg_dir / "requirements.txt"
        req_file.write_text("pandas>=1.0.0\n")
        deployer._prepare_dependencies(pkg_dir)
        content = req_file.read_text()
        assert "flask" in content
        assert "requests" in content

    def test_no_op_when_requirements_absent(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        # Should not raise even with no requirements.txt
        deployer._prepare_dependencies(pkg_dir)

    def test_does_not_duplicate_existing_deps(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        req_file = pkg_dir / "requirements.txt"
        req_file.write_text("flask>=2.0.0\nrequests>=2.28.0\n")
        deployer._prepare_dependencies(pkg_dir)
        content = req_file.read_text()
        # flask should appear only once
        assert content.count("flask") == 1
