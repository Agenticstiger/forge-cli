# FLUID CLI Docker Image
# Multi-stage build for minimal production image
#
# Build locally:
#   docker build -t forge-cli .
#   docker build --build-arg PROFILE=alpha -t forge-cli:alpha .
#
# Run:
#   docker run --rm forge-cli --version
#   docker run --rm -v $(pwd):/workspace forge-cli validate /workspace/contract.fluid.yaml

# ============================================
# Base stage — shared dependencies
# ============================================
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Upgrade the packaging toolchain — the slim base ships older pip /
# setuptools / wheel that carry fixable advisories the release Grype
# gate would (correctly) flag. Keeping them current keeps the image clean.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# ============================================
# Build stage — install from source
# ============================================
FROM base AS builder

ARG PROFILE=stable

WORKDIR /build

# Copy only dependency files first for better layer caching
COPY pyproject.toml README.md ./
COPY fluid_build/ fluid_build/

# Install the package (non-editable)
RUN pip install --prefix=/install .

# Install provider dependencies based on profile
RUN if [ "${PROFILE}" = "experimental" ] || [ "${PROFILE}" = "alpha" ]; then \
      pip install --prefix=/install \
        ".[local,gcp,snowflake,viz]"; \
    elif [ "${PROFILE}" = "beta" ]; then \
      pip install --prefix=/install \
        ".[local,gcp,viz]"; \
    else \
      pip install --prefix=/install \
        ".[local]"; \
    fi

# ============================================
# Production stage — minimal runtime
# ============================================
FROM base AS production

# Create non-root user
RUN groupadd -r fluid && useradd -r -g fluid -m fluid

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /workspace

# Verify installation
RUN fluid --version

# Directories for user data
RUN mkdir -p /home/fluid/.fluid && chown -R fluid:fluid /home/fluid /workspace

USER fluid

ENTRYPOINT ["fluid"]
CMD ["--help"]

LABEL maintainer="Agentics Transformation <info@agentics.ai>" \
      org.opencontainers.image.title="FLUID CLI" \
      org.opencontainers.image.description="FLUID Data Products CLI — plan, apply, and visualize data products" \
      org.opencontainers.image.vendor="Agentics Transformation Pty Ltd" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/Agenticstiger/forge-cli"
