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

"""
Deployment system for FLUID Forge projects

This module manages the complete lifecycle of generated projects:
1. Project packaging and preparation
2. Environment setup and dependency management
3. Deployment to various targets (local, cloud, CI/CD)
4. Health checks and monitoring
5. Rollback and recovery
"""

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint


class DeploymentTarget(Enum):
    """Supported deployment targets"""

    LOCAL = "local"
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    GCP = "gcp"
    AWS = "aws"
    AZURE = "azure"
    CICD = "cicd"


class DeploymentStatus(Enum):
    """Deployment status"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLBACK = "rollback"


@dataclass
class DeploymentConfig:
    """Configuration for deployment"""

    target: DeploymentTarget
    environment: str = "development"
    namespace: Optional[str] = None
    region: Optional[str] = None
    resources: Optional[Dict[str, Any]] = None
    env_vars: Optional[Dict[str, str]] = None
    secrets: Optional[Dict[str, str]] = None

    def __post_init__(self):
        if self.resources is None:
            self.resources = {}
        if self.env_vars is None:
            self.env_vars = {}
        if self.secrets is None:
            self.secrets = {}


@dataclass
class DeploymentResult:
    """Result of deployment operation"""

    status: DeploymentStatus
    deployment_id: str
    target: DeploymentTarget
    endpoint: Optional[str] = None
    logs: List[str] = None
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.logs is None:
            self.logs = []
        if self.metrics is None:
            self.metrics = {}


class ProjectDeployer:
    """Manages project deployment lifecycle"""

    def __init__(self, project_path: Path):
        self.project_path = Path(project_path)
        self.deployment_dir = self.project_path / ".deployments"
        self.deployment_dir.mkdir(exist_ok=True)

    def deploy(self, config: DeploymentConfig) -> DeploymentResult:
        """Deploy project to specified target"""

        deployment_id = self._generate_deployment_id()

        try:
            # Prepare project for deployment
            package_path = self._prepare_deployment_package()

            # Deploy to target
            if config.target == DeploymentTarget.LOCAL:
                result = self._deploy_local(deployment_id, package_path, config)
            elif config.target == DeploymentTarget.DOCKER:
                result = self._deploy_docker(deployment_id, package_path, config)
            elif config.target == DeploymentTarget.KUBERNETES:
                result = self._deploy_kubernetes(deployment_id, package_path, config)
            elif config.target == DeploymentTarget.GCP:
                result = self._deploy_gcp(deployment_id, package_path, config)
            elif config.target == DeploymentTarget.AWS:
                result = self._deploy_aws(deployment_id, package_path, config)
            elif config.target == DeploymentTarget.AZURE:
                result = self._deploy_azure(deployment_id, package_path, config)
            elif config.target == DeploymentTarget.CICD:
                result = self._deploy_cicd(deployment_id, package_path, config)
            else:
                raise ValueError(f"Unsupported deployment target: {config.target}")

            # Save deployment record
            self._save_deployment_record(result, config)

            return result

        except Exception as e:
            error_result = DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=config.target,
                error=str(e),
                logs=[f"Deployment failed: {str(e)}"],
            )
            self._save_deployment_record(error_result, config)
            return error_result

    def _generate_deployment_id(self) -> str:
        """Generate unique deployment ID"""
        import hashlib
        import time

        timestamp = str(int(time.time()))
        project_name = self.project_path.name
        return hashlib.md5(f"{project_name}-{timestamp}".encode()).hexdigest()[:12]

    def _prepare_deployment_package(self) -> Path:
        """Prepare project for deployment"""

        # Create temporary package directory
        package_dir = tempfile.mkdtemp(prefix="fluid_deploy_")
        package_path = Path(package_dir)

        # Copy project files (excluding .git, __pycache__, etc.)
        self._copy_project_files(self.project_path, package_path)

        # Generate deployment manifests
        self._generate_deployment_manifests(package_path)

        # Install dependencies
        self._prepare_dependencies(package_path)

        return package_path

    def _copy_project_files(self, source: Path, dest: Path):
        """Copy project files excluding development artifacts"""

        exclude_patterns = {
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".venv",
            "venv",
            "*.pyc",
            "*.pyo",
            ".DS_Store",
            "node_modules",
            ".idea",
            ".vscode",
            "*.log",
            "runtime",
            ".deployments",
        }

        def should_exclude(path: Path) -> bool:
            for pattern in exclude_patterns:
                if pattern.startswith("*"):
                    if path.name.endswith(pattern[1:]):
                        return True
                elif path.name == pattern:
                    return True
            return False

        # Copy files
        for item in source.rglob("*"):
            if should_exclude(item):
                continue

            rel_path = item.relative_to(source)
            dest_path = dest / rel_path

            if item.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest_path)

    def _generate_deployment_manifests(self, package_path: Path):
        """Generate deployment manifests (Dockerfile, k8s manifests, etc.)"""

        # Generate Dockerfile
        dockerfile_content = self._generate_dockerfile()
        (package_path / "Dockerfile").write_text(dockerfile_content)

        # Generate docker-compose.yml
        docker_compose_content = self._generate_docker_compose()
        (package_path / "docker-compose.yml").write_text(docker_compose_content)

        # Generate Kubernetes manifests
        k8s_dir = package_path / "k8s"
        k8s_dir.mkdir(exist_ok=True)

        deployment_yaml = self._generate_k8s_deployment()
        (k8s_dir / "deployment.yaml").write_text(deployment_yaml)

        service_yaml = self._generate_k8s_service()
        (k8s_dir / "service.yaml").write_text(service_yaml)

        # Generate CI/CD configuration
        self._generate_cicd_configs(package_path)

    def _generate_dockerfile(self) -> str:
        """Generate Dockerfile for the project"""

        # Try to detect project type
        has_requirements = (self.project_path / "requirements.txt").exists()
        has_package_json = (self.project_path / "package.json").exists()
        has_dbt = (self.project_path / "dbt_project.yml").exists()

        if has_dbt:
            base_image = "python:3.9-slim"
            install_cmd = "pip install dbt-core dbt-bigquery"
        elif has_requirements:
            base_image = "python:3.9-slim"
            install_cmd = "pip install -r requirements.txt"
        elif has_package_json:
            base_image = "node:16-alpine"
            install_cmd = "npm install"
        else:
            base_image = "python:3.9-slim"
            install_cmd = "# No dependencies found"

        return f"""
FROM {base_image}

WORKDIR /app

# Copy dependency files first for better caching
{"COPY requirements.txt ." if has_requirements else ""}
{"COPY package.json package-lock.json ./" if has_package_json else ""}
{"COPY dbt_project.yml ." if has_dbt else ""}

# Install dependencies
RUN {install_cmd}

# Copy application code
COPY . .

# Set default command
CMD ["python", "-m", "fluid_build.cli.main", "apply"]

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\
    CMD python -c "import sys; sys.exit(0)"

# Labels
LABEL maintainer="FLUID Build"
LABEL version="1.0"
LABEL description="FLUID Forge generated project"
""".strip()

    def _generate_docker_compose(self) -> str:
        """Generate docker-compose.yml"""

        project_name = self.project_path.name.lower().replace("_", "-")

        return f"""
version: '3.8'

services:
  {project_name}:
    build: .
    container_name: {project_name}
    restart: unless-stopped
    environment:
      - ENVIRONMENT=development
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    ports:
      - "8080:8080"
    networks:
      - {project_name}-network

networks:
  {project_name}-network:
    driver: bridge

volumes:
  data:
  logs:
""".strip()

    def _generate_k8s_deployment(self) -> str:
        """Generate Kubernetes deployment manifest"""

        project_name = self.project_path.name.lower().replace("_", "-")

        return f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {project_name}
  labels:
    app: {project_name}
    version: v1
spec:
  replicas: 2
  selector:
    matchLabels:
      app: {project_name}
  template:
    metadata:
      labels:
        app: {project_name}
        version: v1
    spec:
      containers:
      - name: {project_name}
        image: {project_name}:latest
        ports:
        - containerPort: 8080
        env:
        - name: ENVIRONMENT
          value: "production"
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 5
""".strip()

    def _generate_k8s_service(self) -> str:
        """Generate Kubernetes service manifest"""

        project_name = self.project_path.name.lower().replace("_", "-")

        return f"""
apiVersion: v1
kind: Service
metadata:
  name: {project_name}-service
  labels:
    app: {project_name}
spec:
  selector:
    app: {project_name}
  ports:
  - name: http
    port: 80
    targetPort: 8080
    protocol: TCP
  type: ClusterIP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {project_name}-ingress
  labels:
    app: {project_name}
spec:
  rules:
  - host: {project_name}.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {project_name}-service
            port:
              number: 80
""".strip()

    def _generate_cicd_configs(self, package_path: Path):
        """Generate CI/CD configuration files"""

        # GitHub Actions
        github_dir = package_path / ".github" / "workflows"
        github_dir.mkdir(parents=True, exist_ok=True)

        github_workflow = self._generate_github_workflow()
        (github_dir / "deploy.yml").write_text(github_workflow)

        # GitLab CI
        gitlab_ci = self._generate_gitlab_ci()
        (package_path / ".gitlab-ci.yml").write_text(gitlab_ci)

        # Azure DevOps
        azure_pipelines = self._generate_azure_pipelines()
        (package_path / "azure-pipelines.yml").write_text(azure_pipelines)

    def _generate_github_workflow(self) -> str:
        """Generate GitHub Actions workflow"""

        return """
name: Deploy

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.9'
    
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt
    
    - name: Run tests
      run: |
        python -m pytest tests/
    
    - name: Validate project
      run: |
        python -m fluid_build.forge.core.validation .

  build:
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
    - name: Checkout
      uses: actions/checkout@v3
    
    - name: Set up Docker Buildx
      uses: docker/setup-buildx-action@v2
    
    - name: Log in to Container Registry
      uses: docker/login-action@v2
      with:
        registry: ${{ env.REGISTRY }}
        username: ${{ github.actor }}
        password: ${{ secrets.GITHUB_TOKEN }}
    
    - name: Build and push Docker image
      uses: docker/build-push-action@v4
      with:
        context: .
        push: true
        tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:latest
        cache-from: type=gha
        cache-to: type=gha,mode=max

  deploy:
    needs: build
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
    - name: Deploy to production
      run: |
        echo "Deploying to production..."
        # Add deployment steps here
""".strip()

    def _generate_gitlab_ci(self) -> str:
        """Generate GitLab CI configuration"""

        return """
stages:
  - test
  - build
  - deploy

variables:
  DOCKER_DRIVER: overlay2
  DOCKER_TLS_CERTDIR: "/certs"

before_script:
  - python -m pip install --upgrade pip

test:
  stage: test
  image: python:3.9
  script:
    - pip install -r requirements.txt
    - python -m pytest tests/
    - python -m fluid_build.forge.core.validation .

build:
  stage: build
  image: docker:20.10.16
  services:
    - docker:20.10.16-dind
  script:
    - docker build -t $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA .
    - docker push $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
  only:
    - main
    - develop

deploy:
  stage: deploy
  image: bitnami/kubectl:latest
  script:
    - kubectl set image deployment/app app=$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
    - kubectl rollout status deployment/app
  only:
    - main
  when: manual
""".strip()

    def _generate_azure_pipelines(self) -> str:
        """Generate Azure DevOps pipeline"""

        return """
trigger:
  branches:
    include:
    - main
    - develop

pool:
  vmImage: 'ubuntu-latest'

variables:
  imageName: 'fluid-project'
  containerRegistry: 'myregistry.azurecr.io'

stages:
- stage: Test
  displayName: 'Test and Validate'
  jobs:
  - job: Test
    steps:
    - task: UsePythonVersion@0
      inputs:
        versionSpec: '3.9'
    
    - script: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt
      displayName: 'Install dependencies'
    
    - script: |
        python -m pytest tests/
      displayName: 'Run tests'
    
    - script: |
        python -m fluid_build.forge.core.validation .
      displayName: 'Validate project'

- stage: Build
  displayName: 'Build and Push'
  condition: and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))
  jobs:
  - job: Build
    steps:
    - task: Docker@2
      displayName: 'Build and push image'
      inputs:
        containerRegistry: '$(containerRegistry)'
        repository: '$(imageName)'
        command: 'buildAndPush'
        Dockerfile: '**/Dockerfile'
        tags: |
          $(Build.BuildId)
          latest

- stage: Deploy
  displayName: 'Deploy to Production'
  condition: and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))
  jobs:
  - deployment: Deploy
    environment: 'production'
    strategy:
      runOnce:
        deploy:
          steps:
          - script: echo "Deploying to production..."
            displayName: 'Deploy'
""".strip()

    def _prepare_dependencies(self, package_path: Path):
        """Prepare dependencies for deployment"""

        requirements_file = package_path / "requirements.txt"
        if requirements_file.exists():
            # Add any missing deployment dependencies
            with open(requirements_file) as f:
                requirements = f.read()

            # Add health check dependencies if not present
            health_deps = ["flask", "requests"]
            for dep in health_deps:
                if dep not in requirements:
                    requirements += f"\n{dep}>=1.0.0"

            with open(requirements_file, "w") as f:
                f.write(requirements)

    def _deploy_local(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Deploy project locally"""

        logs = []

        try:
            # Create local deployment directory
            local_deploy_path = self.deployment_dir / f"local_{deployment_id}"
            shutil.copytree(package_path, local_deploy_path)

            logs.append(f"Copied project to {local_deploy_path}")

            # Install dependencies
            if (local_deploy_path / "requirements.txt").exists():
                result = subprocess.run(
                    ["pip", "install", "-r", str(local_deploy_path / "requirements.txt")],
                    capture_output=True,
                    text=True,
                )

                if result.returncode == 0:
                    logs.append("Dependencies installed successfully")
                else:
                    raise Exception(f"Failed to install dependencies: {result.stderr}")

            # Start local service
            logs.append("Local deployment completed")

            return DeploymentResult(
                status=DeploymentStatus.SUCCESS,
                deployment_id=deployment_id,
                target=config.target,
                endpoint=f"file://{local_deploy_path}",
                logs=logs,
            )

        except Exception as e:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=config.target,
                error=str(e),
                logs=logs,
            )

    def _deploy_docker(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Deploy project using Docker"""

        logs = []

        try:
            # Build Docker image
            image_name = f"fluid-{self.project_path.name.lower()}:{deployment_id}"

            result = subprocess.run(
                ["docker", "build", "-t", image_name, str(package_path)],
                capture_output=True,
                text=True,
                cwd=package_path,
            )

            if result.returncode != 0:
                raise Exception(f"Docker build failed: {result.stderr}")

            logs.append(f"Built Docker image: {image_name}")

            # Run container
            container_name = f"fluid-{self.project_path.name.lower()}-{deployment_id}"
            run_cmd = ["docker", "run", "-d", "--name", container_name, "-p", "8080:8080"]

            # Add environment variables
            for key, value in config.env_vars.items():
                run_cmd.extend(["-e", f"{key}={value}"])

            run_cmd.append(image_name)

            result = subprocess.run(run_cmd, capture_output=True, text=True)

            if result.returncode != 0:
                raise Exception(f"Failed to run container: {result.stderr}")

            logs.append(f"Started container: {container_name}")

            return DeploymentResult(
                status=DeploymentStatus.SUCCESS,
                deployment_id=deployment_id,
                target=config.target,
                endpoint="http://localhost:8080",
                logs=logs,
                metrics={"image": image_name, "container": container_name},
            )

        except Exception as e:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=config.target,
                error=str(e),
                logs=logs,
            )

    def _deploy_kubernetes(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Deploy project to Kubernetes"""

        logs = []

        try:
            # Apply Kubernetes manifests
            k8s_dir = package_path / "k8s"

            for manifest in k8s_dir.glob("*.yaml"):
                result = subprocess.run(
                    ["kubectl", "apply", "-f", str(manifest)], capture_output=True, text=True
                )

                if result.returncode != 0:
                    raise Exception(f"Failed to apply {manifest.name}: {result.stderr}")

                logs.append(f"Applied manifest: {manifest.name}")

            # Wait for deployment to be ready
            app_name = self.project_path.name.lower().replace("_", "-")
            result = subprocess.run(
                ["kubectl", "rollout", "status", f"deployment/{app_name}"],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                logs.append("Deployment is ready")
            else:
                logs.append("Deployment may not be fully ready")

            return DeploymentResult(
                status=DeploymentStatus.SUCCESS,
                deployment_id=deployment_id,
                target=config.target,
                endpoint=f"http://{app_name}.{config.namespace or 'default'}.svc.cluster.local",
                logs=logs,
            )

        except Exception as e:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=config.target,
                error=str(e),
                logs=logs,
            )

    def _deploy_gcp(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Deploy project to Google Cloud Platform"""

        logs = []
        logs.append("GCP deployment not yet implemented")

        return DeploymentResult(
            status=DeploymentStatus.FAILED,
            deployment_id=deployment_id,
            target=config.target,
            error="GCP deployment not implemented",
            logs=logs,
        )

    def _deploy_aws(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Deploy project to Amazon Web Services"""

        logs = []
        logs.append("AWS deployment not yet implemented")

        return DeploymentResult(
            status=DeploymentStatus.FAILED,
            deployment_id=deployment_id,
            target=config.target,
            error="AWS deployment not implemented",
            logs=logs,
        )

    def _deploy_azure(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Deploy project to Microsoft Azure"""

        logs = []
        logs.append("Azure deployment not yet implemented")

        return DeploymentResult(
            status=DeploymentStatus.FAILED,
            deployment_id=deployment_id,
            target=config.target,
            error="Azure deployment not implemented",
            logs=logs,
        )

    def _deploy_cicd(
        self, deployment_id: str, package_path: Path, config: DeploymentConfig
    ) -> DeploymentResult:
        """Set up CI/CD deployment"""

        logs = []

        try:
            # Copy CI/CD configurations to project
            cicd_files = [".github/workflows/deploy.yml", ".gitlab-ci.yml", "azure-pipelines.yml"]

            for cicd_file in cicd_files:
                source = package_path / cicd_file
                dest = self.project_path / cicd_file

                if source.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    logs.append(f"Created CI/CD config: {cicd_file}")

            return DeploymentResult(
                status=DeploymentStatus.SUCCESS,
                deployment_id=deployment_id,
                target=config.target,
                logs=logs,
                metrics={"configs_created": len(cicd_files)},
            )

        except Exception as e:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=config.target,
                error=str(e),
                logs=logs,
            )

    def _save_deployment_record(self, result: DeploymentResult, config: DeploymentConfig):
        """Save deployment record for tracking"""

        record = {
            "deployment_id": result.deployment_id,
            "timestamp": self._get_timestamp(),
            "status": result.status.value,
            "target": result.target.value,
            "config": {
                "environment": config.environment,
                "namespace": config.namespace,
                "region": config.region,
            },
            "result": {
                "endpoint": result.endpoint,
                "error": result.error,
                "metrics": result.metrics,
            },
            "logs": result.logs,
        }

        record_file = self.deployment_dir / f"{result.deployment_id}.json"
        with open(record_file, "w") as f:
            json.dump(record, f, indent=2)

    def _get_timestamp(self) -> str:
        """Get current timestamp"""
        import datetime

        return datetime.datetime.now().isoformat()

    def list_deployments(self) -> List[Dict[str, Any]]:
        """List all deployments for this project"""

        deployments = []

        for record_file in self.deployment_dir.glob("*.json"):
            try:
                with open(record_file) as f:
                    record = json.load(f)
                deployments.append(record)
            except Exception:
                continue

        # Sort by timestamp (newest first)
        deployments.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        return deployments

    def get_deployment(self, deployment_id: str) -> Optional[Dict[str, Any]]:
        """Get specific deployment record"""

        record_file = self.deployment_dir / f"{deployment_id}.json"
        if record_file.exists():
            with open(record_file) as f:
                return json.load(f)
        return None

    def rollback_deployment(self, deployment_id: str) -> DeploymentResult:
        """Rollback a deployment"""

        record = self.get_deployment(deployment_id)
        if not record:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=DeploymentTarget.LOCAL,
                error="Deployment record not found",
            )

        # Implementation depends on deployment target
        target = DeploymentTarget(record["target"])

        if target == DeploymentTarget.DOCKER:
            return self._rollback_docker(deployment_id, record)
        elif target == DeploymentTarget.KUBERNETES:
            return self._rollback_kubernetes(deployment_id, record)
        else:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=target,
                error=f"Rollback not implemented for {target.value}",
            )

    def _rollback_docker(self, deployment_id: str, record: Dict[str, Any]) -> DeploymentResult:
        """Rollback Docker deployment"""

        logs = []

        try:
            container_name = record["result"]["metrics"].get("container")
            if container_name:
                # Stop container
                subprocess.run(["docker", "stop", container_name], capture_output=True)
                logs.append(f"Stopped container: {container_name}")

                # Remove container
                subprocess.run(["docker", "rm", container_name], capture_output=True)
                logs.append(f"Removed container: {container_name}")

            return DeploymentResult(
                status=DeploymentStatus.ROLLBACK,
                deployment_id=deployment_id,
                target=DeploymentTarget.DOCKER,
                logs=logs,
            )

        except Exception as e:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=DeploymentTarget.DOCKER,
                error=str(e),
                logs=logs,
            )

    def _rollback_kubernetes(self, deployment_id: str, record: Dict[str, Any]) -> DeploymentResult:
        """Rollback Kubernetes deployment"""

        logs = []

        try:
            app_name = self.project_path.name.lower().replace("_", "-")

            # Rollback deployment
            result = subprocess.run(
                ["kubectl", "rollout", "undo", f"deployment/{app_name}"],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                logs.append(f"Rolled back deployment: {app_name}")
            else:
                raise Exception(f"Rollback failed: {result.stderr}")

            return DeploymentResult(
                status=DeploymentStatus.ROLLBACK,
                deployment_id=deployment_id,
                target=DeploymentTarget.KUBERNETES,
                logs=logs,
            )

        except Exception as e:
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                deployment_id=deployment_id,
                target=DeploymentTarget.KUBERNETES,
                error=str(e),
                logs=logs,
            )


def deploy_project(
    project_path: str, target: str, environment: str = "development"
) -> DeploymentResult:
    """Convenience function to deploy a project"""

    deployer = ProjectDeployer(Path(project_path))
    config = DeploymentConfig(target=DeploymentTarget(target), environment=environment)

    return deployer.deploy(config)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        cprint("Usage: python deployment.py <project_path> <target> [environment]")
        cprint("Targets: local, docker, kubernetes, gcp, aws, azure, cicd")
        sys.exit(1)

    project_path = sys.argv[1]
    target = sys.argv[2]
    environment = sys.argv[3] if len(sys.argv) > 3 else "development"

    result = deploy_project(project_path, target, environment)

    cprint(f"Deployment {result.deployment_id}: {result.status.value}")
    if result.endpoint:
        cprint(f"Endpoint: {result.endpoint}")
    if result.error:
        cprint(f"Error: {result.error}")

    cprint("\nLogs:")
    for log in result.logs:
        cprint(f"  {log}")
