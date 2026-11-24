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
# Python 3.14, not 3.13. The release Grype gate (--fail-on high --only-fixed)
# failed v0.16.0 on CVE-2026-82049, High, in the 3.13 interpreter, with the fix
# listed only from 3.14. The gate ran before the push, so no vulnerable image was
# published, and therefore no 0.16.0 image was published at all. 3.14 is already
# in pyproject's classifiers and in ci.yml's test matrix.
#
# The base is pinned by the digest of the multi-architecture index (CPython
# 3.14.10 at the time of pinning), so a retag of python:3.14-slim upstream changes
# nothing here until the digest is bumped in a reviewed change. The tag stays
# beside the digest so Dependabot (see .github/dependabot.yml) proposes the next
# digest. To refresh by hand: `crane digest python:3.14-slim`. The apt-get upgrade
# below still takes the current Debian security patches at build time.
FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# `apt-get upgrade` pulls the latest Debian security patches for base-image
# packages (e.g. liblzma5) so fixable OS-package CVEs are actually resolved at
# build time rather than shipped — the release Grype gate scans OS + library
# CVEs, so keeping the base current keeps the surface small.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
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

# Install provider dependencies based on profile. Every profile names
# [standard] (litellm + mcp) and [lineage], so the image keeps AI,
# `fluid mcp serve` and OpenLineage emission now that they are out of the base
# install (0.28.0).
RUN if [ "${PROFILE}" = "experimental" ] || [ "${PROFILE}" = "alpha" ]; then \
      pip install --prefix=/install \
        ".[local,gcp,snowflake,viz,standard,lineage]"; \
    elif [ "${PROFILE}" = "beta" ]; then \
      pip install --prefix=/install \
        ".[local,gcp,viz,standard,lineage]"; \
    else \
      pip install --prefix=/install \
        ".[local,standard,lineage]"; \
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
      org.opencontainers.image.vendor="Agentics Transformation Limited" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/Agenticstiger/forge-cli"
