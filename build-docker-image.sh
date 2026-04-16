#!/bin/bash
# Build FLUID CLI Docker Image
# Builds a Docker image containing a specific version of the FLUID CLI
# and publishes it to a Docker registry (GHCR by default)

set -e

# Configuration
REGISTRY="localhost:5000"
IMAGE_NAME="fluid-forge-cli"
DEFAULT_PROFILE="stable"
CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

show_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Build FLUID CLI Docker image from a specific build profile

OPTIONS:
    -p, --profile PROFILE    Build profile to use (experimental, alpha, beta, stable)
                            Default: stable
    -v, --version VERSION    CLI version to install (e.g., 0.7.1a42, 0.7.1dev1)
                            If not specified, uses latest from profile
    -t, --tag TAG           Additional Docker tag (default: same as version)
    -r, --registry REGISTRY  Docker registry (default: ghcr.io/agentics-rising/fluid-forge-cli)
    --no-push               Build only, don't push to registry
    --pypi-url URL          PyPI server URL (default: http://\$NAS_HOST:\$PYPI_PORT/simple)
    --pypi-user USER        PyPI username for authentication
    --pypi-pass PASS        PyPI password for authentication
    -h, --help              Show this help message

EXAMPLES:
    # Build latest stable version
    $0 --profile stable

    # Build specific alpha version
    $0 --profile alpha --version 0.7.1a42

    # Build experimental with custom tag
    $0 --profile experimental --tag latest-dev

    # Build without pushing
    $0 --profile beta --no-push

PROFILES:
    experimental    Kitchen sink - ALL commands and providers
    alpha           Bleeding edge features
    beta            Feature complete preview
    stable          Production ready
EOF
}

# Parse command line arguments
PROFILE="$DEFAULT_PROFILE"
VERSION=""
TAG=""
PUSH=true
NO_CACHE=false
PYPI_URL="http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}/simple"
PYPI_USER=""
PYPI_PASS=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--profile)
            PROFILE="$2"
            shift 2
            ;;
        -v|--version)
            VERSION="$2"
            shift 2
            ;;
        -t|--tag)
            TAG="$2"
            shift 2
            ;;
        -r|--registry)
            REGISTRY="$2"
            shift 2
            ;;
        --no-push)
            PUSH=false
            shift
            ;;
        --no-cache)
            NO_CACHE=true
            shift
            ;;
        --pypi-url)
            PYPI_URL="$2"
            shift 2
            ;;
        --pypi-user)
            PYPI_USER="$2"
            shift 2
            ;;
        --pypi-pass)
            PYPI_PASS="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Validate profile
if [[ ! "$PROFILE" =~ ^(experimental|alpha|beta|stable)$ ]]; then
    print_error "Invalid profile: $PROFILE"
    echo "Valid profiles: experimental, alpha, beta, stable"
    exit 1
fi

print_header "FLUID CLI Docker Build"
echo "Profile:  $PROFILE"
echo "Registry: $REGISTRY"
echo "PyPI:     $PYPI_URL"
echo ""

# Determine version if not specified
if [ -z "$VERSION" ]; then
    print_warning "No version specified, will install latest from $PROFILE profile"
    VERSION_SPEC="fluid-forge"
    if [ "$TAG" = "" ]; then
        TAG="$PROFILE-latest"
    fi
else
    print_success "Building with version: $VERSION"
    VERSION_SPEC="fluid-forge==$VERSION"
    if [ "$TAG" = "" ]; then
        TAG="$VERSION"
    fi
fi

# Full image name
FULL_IMAGE_NAME="${REGISTRY}/${IMAGE_NAME}:${TAG}"

print_header "Building Docker Image"
echo "Image: $FULL_IMAGE_NAME"
echo "CLI:   $VERSION_SPEC"
echo ""

# Create Dockerfile
DOCKERFILE=$(cat << 'DOCKERFILE_END'
# FLUID CLI Docker Image
# Multi-stage build for minimal production image

FROM python:3.11-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# Production Stage
# ============================================
FROM base as production

# Create app user
RUN groupadd -r fluid && useradd -r -g fluid fluid

# Set working directory
WORKDIR /app

# Install FLUID CLI from PyPI
ARG PYPI_URL
ARG PYPI_USER
ARG PYPI_PASS
ARG VERSION_SPEC
ARG PROFILE

# Build authenticated PyPI URL if credentials provided
RUN if [ -n "${PYPI_USER}" ] && [ -n "${PYPI_PASS}" ]; then \
      PYPI_HOST=$(echo ${PYPI_URL} | sed 's|http://||;s|/.*||'); \
      AUTH_PYPI_URL="http://${PYPI_USER}:${PYPI_PASS}@${PYPI_HOST}/simple"; \
      pip install --index-url "${AUTH_PYPI_URL}" --trusted-host ${PYPI_HOST} ${VERSION_SPEC}; \
    else \
      pip install --index-url ${PYPI_URL} --trusted-host $(echo ${PYPI_URL} | sed 's|http://||;s|/.*||') ${VERSION_SPEC}; \
    fi

# Install provider dependencies based on profile
# experimental/alpha: All providers for seamless deployments
# beta/stable: Core providers only
RUN if [ "${PROFILE}" = "experimental" ] || [ "${PROFILE}" = "alpha" ]; then \
      echo "Installing all provider dependencies for ${PROFILE} profile..."; \
      pip install --no-cache-dir \
        google-cloud-bigquery \
        google-cloud-storage \
        google-cloud-functions \
        google-cloud-scheduler \
        google-auth \
        boto3 \
        azure-storage-blob \
        azure-identity \
        snowflake-connector-python; \
    elif [ "${PROFILE}" = "beta" ]; then \
      echo "Installing core provider dependencies for ${PROFILE} profile..."; \
      pip install --no-cache-dir \
        google-cloud-bigquery \
        google-cloud-storage \
        boto3 \
        snowflake-connector-python; \
    else \
      echo "Stable profile - providers installed on-demand"; \
    fi

# Verify installation
RUN fluid --version && \
    fluid --help | grep -qi "fluid" && \
    echo "✅ FLUID CLI installed successfully"

# Create directories for user data
RUN mkdir -p /app/.fluid /app/workspace && \
    chown -R fluid:fluid /app

# Switch to non-root user
USER fluid

# Set HOME for writable config
ENV HOME=/app

# Default command
ENTRYPOINT ["fluid"]
CMD ["--help"]

# Labels
LABEL maintainer="DUSTLabs <info@dustlabs.com>" \
      org.opencontainers.image.title="FLUID CLI" \
      org.opencontainers.image.description="FLUID Data Products Command-Line Interface" \
      org.opencontainers.image.vendor="DUSTLabs / Agentics Transformation LTD Ireland" \
      org.opencontainers.image.licenses="Apache-2.0"
DOCKERFILE_END
)

# Write Dockerfile to temp location
TEMP_DOCKERFILE=$(mktemp)
echo "$DOCKERFILE" > "$TEMP_DOCKERFILE"

# Build the image
print_success "Building Docker image..."

# Add --no-cache flag if requested
CACHE_FLAG=""
if [ "$NO_CACHE" = true ]; then
    CACHE_FLAG="--no-cache"
    print_warning "Building with --no-cache (fresh build, no layer caching)"
fi

docker build \
    $CACHE_FLAG \
    --build-arg PYPI_URL="$PYPI_URL" \
    --build-arg PYPI_USER="$PYPI_USER" \
    --build-arg PYPI_PASS="$PYPI_PASS" \
    --build-arg VERSION_SPEC="$VERSION_SPEC" \
    --build-arg PROFILE="$PROFILE" \
    --tag "$FULL_IMAGE_NAME" \
    --file "$TEMP_DOCKERFILE" \
    .

# Clean up temp Dockerfile
rm "$TEMP_DOCKERFILE"

print_success "Image built: $FULL_IMAGE_NAME"

# Test the image
print_header "Testing Docker Image"
echo "Running: docker run --rm $FULL_IMAGE_NAME --version"
INSTALLED_VERSION=$(docker run --rm "$FULL_IMAGE_NAME" --version)
print_success "Installed version: $INSTALLED_VERSION"

echo ""
echo "Running: docker run --rm $FULL_IMAGE_NAME providers"
docker run --rm "$FULL_IMAGE_NAME" providers

# Push to registry if requested
if [ "$PUSH" = true ]; then
    print_header "Pushing to Registry"
    
    # Check if registry is accessible
    if ! curl -s "http://${REGISTRY#*://}/v2/" > /dev/null 2>&1; then
        print_error "Cannot access registry at $REGISTRY"
        print_warning "Make sure registry is running: docker run -d -p 5000:5000 --name registry registry:2"
        exit 1
    fi
    
    print_success "Pushing $FULL_IMAGE_NAME..."
    docker push "$FULL_IMAGE_NAME"
    
    print_success "Image pushed successfully!"
    
    # Also tag with profile name for easy access
    if [ "$TAG" != "$PROFILE" ]; then
        PROFILE_TAG="${REGISTRY}/${IMAGE_NAME}:${PROFILE}"
        print_success "Tagging as $PROFILE_TAG"
        docker tag "$FULL_IMAGE_NAME" "$PROFILE_TAG"
        docker push "$PROFILE_TAG"
    fi
else
    print_warning "Skipping push (--no-push specified)"
fi

print_header "Build Complete"
cat << EOF

✅ Docker image ready: $FULL_IMAGE_NAME

🚀 Usage Examples:

  # Validate a contract
  docker run --rm -v \$(pwd):/workspace $FULL_IMAGE_NAME validate /workspace/contract.fluid.yaml

  # Generate Airflow DAG
  docker run --rm -v \$(pwd):/workspace $FULL_IMAGE_NAME \\
    generate-airflow /workspace/contract.fluid.yaml \\
    --output /workspace/dag.py

  # Interactive shell
  docker run --rm -it -v \$(pwd):/workspace --entrypoint /bin/bash $FULL_IMAGE_NAME

  # Run in Jenkins pipeline
  docker {
      image '$FULL_IMAGE_NAME'
      args '-v \$WORKSPACE:/workspace'
  }

📦 Available on registry: $REGISTRY

EOF

if [ "$PUSH" = true ]; then
    echo "🔍 View on registry:"
    echo "  curl http://${REGISTRY#*://}/v2/${IMAGE_NAME}/tags/list"
    echo ""
fi
