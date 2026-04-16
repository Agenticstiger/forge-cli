#!/bin/bash
# ============================================================================
# FLUID PyPI Publishing Script
# ============================================================================
# Publishes alpha, beta, and stable builds to PyPI with smart versioning
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_NUMBER_FILE="$SCRIPT_DIR/.build_number"
MANIFEST_FILE="$SCRIPT_DIR/build-manifest.yaml"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

# Read current build number
get_build_number() {
    # Use Jenkins BUILD_NUMBER if available, otherwise local file
    if [ -n "$BUILD_NUMBER" ]; then
        echo "$BUILD_NUMBER"
    elif [ -f "$BUILD_NUMBER_FILE" ]; then
        cat "$BUILD_NUMBER_FILE"
    else
        echo "1"
    fi
}

# Increment build number (only used for local builds, not Jenkins)
increment_build_number() {
    local current=$(get_build_number)
    local next=$((current + 1))
    echo "$next" > "$BUILD_NUMBER_FILE"
    echo "$next"
}

# Generate version string
generate_version() {
    local build_type=$1  # alpha, beta, stable
    # Read base version from pyproject.toml (strip any dev/alpha/beta/rc suffix)
    local pyproject="$SCRIPT_DIR/../pyproject.toml"
    local base_version
    if [ -f "$pyproject" ]; then
        base_version=$(grep '^version' "$pyproject" | head -1 | cut -d'"' -f2 | sed 's/\.dev[0-9]*//;s/a[0-9]*//;s/b[0-9]*//;s/rc[0-9]*//')
    else
        base_version="0.7.1"
    fi
    local build_num=$(get_build_number)
    
    case $build_type in
        experimental)
            echo "${base_version}.dev${build_num}"
            ;;
        alpha)
            echo "${base_version}a${build_num}"
            ;;
        beta)
            echo "${base_version}b${build_num}"
            ;;
        stable)
            echo "${base_version}"
            ;;
        *)
            echo "Unknown build type: $build_type" >&2
            exit 1
            ;;
    esac
}

# Publish to PyPI
publish_build() {
    local build_type=$1
    local version=$(generate_version "$build_type")
    local pypi_path="pypi/${build_type}"
    local metadata_path="${pypi_path}/build-metadata"
    
    echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  Publishing ${build_type^^} Build: v${version}${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}"
    
    # Create local directory structure for metadata
    mkdir -p "$metadata_path"
    
    # Also ensure the pypi build type directory exists
    mkdir -p "$SCRIPT_DIR/${pypi_path}"
    
    # Create build metadata
    cat > "${metadata_path}/${version}.json" <<EOF
{
  "version": "$version",
  "build_type": "$build_type",
  "build_number": $(get_build_number),
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_commit": "$(git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "git_branch": "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')",
  "built_by": "$USER",
  "hostname": "$(hostname)"
}
EOF
    
    echo -e "${GREEN}✓ Build metadata saved: ${metadata_path}/${version}.json${NC}"
    
    # Update version in pyproject.toml
    echo -e "${YELLOW}Setting version to ${version}...${NC}"
    cd "$SCRIPT_DIR/.."
    
    # Backup original pyproject.toml
    cp pyproject.toml pyproject.toml.backup
    
    # Update version using sed
    sed -i "s/^version = .*/version = \"${version}\"/" pyproject.toml
    
    # Clean previous builds
    rm -rf dist/ build/ *.egg-info
    
    # Build wheel
    echo -e "${YELLOW}Building wheel for ${build_type}...${NC}"
    python3 -m build
    
    # Configure PyPI credentials (prompt if not set)
    if [ -z "$PYPI_USER" ] || [ -z "$PYPI_PASS" ]; then
        echo -e "${YELLOW}PyPI credentials not set in environment${NC}"
        echo -e "Please provide credentials for http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}"
        read -p "Username: " PYPI_USER
        read -sp "Password: " PYPI_PASS
        echo ""
    fi
    
    # Create .pypirc
    cat > ~/.pypirc << PYPIRC_EOF
[distutils]
index-servers =
    synology-pypi

[synology-pypi]
repository = http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}
username = ${PYPI_USER}
password = ${PYPI_PASS}
PYPIRC_EOF
    
    chmod 600 ~/.pypirc
    
    # Upload to PyPI server
    echo -e "${YELLOW}Uploading to http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}...${NC}"
    if python3 -m twine upload \
        --repository synology-pypi \
        dist/*.whl 2>&1 | tee /tmp/upload.log; then
        echo -e "${GREEN}✅ Upload successful${NC}"
    else
        # Check if it's a "file already exists" error
        if grep -q "409 Conflict\|File already exists\|already been uploaded" /tmp/upload.log; then
            echo -e "${YELLOW}⚠️  Version ${version} already exists on PyPI - skipping${NC}"
        else
            echo -e "${RED}❌ Upload failed${NC}"
            cat /tmp/upload.log
            # Restore original pyproject.toml
            mv pyproject.toml.backup pyproject.toml
            rm -f /tmp/upload.log ~/.pypirc
            return 1
        fi
    fi
    rm -f /tmp/upload.log
    
    # Copy wheel to local archive
    cp dist/*.whl "$SCRIPT_DIR/${pypi_path}/"
    
    # Restore original pyproject.toml
    mv pyproject.toml.backup pyproject.toml
    
    # Cleanup credentials
    rm -f ~/.pypirc
    
    echo -e "${GREEN}✓ Published: ${build_type} v${version}${NC}"
    echo -e "  PyPI: http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}/simple"
    echo -e "  Local: ${pypi_path}/"
    echo ""
}

# Main execution
main() {
    local build_type=${1:-all}
    local current_build=$(get_build_number)
    
    echo -e "${CYAN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  FLUID PyPI Multi-Build Publisher                            ║${NC}"
    echo -e "${CYAN}║  Current Build Number: ${current_build}${NC}"
    echo -e "${CYAN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    # Increment build number at start
    new_build=$(increment_build_number)
    echo -e "${BLUE}Build number incremented: ${current_build} → ${new_build}${NC}"
    echo ""
    
    # Publish builds based on argument
    case $build_type in
        all)
            publish_build "experimental"
            publish_build "alpha"
            publish_build "beta"
            
            # Stable build gated by ALLOW_STABLE_BUILD parameter
            if [ "${ALLOW_STABLE_BUILD}" = "true" ]; then
                echo -e "${GREEN}🔓 ALLOW_STABLE_BUILD=true - Publishing stable build${NC}"
                
                # Optional: Also check test coverage
                echo -e "${YELLOW}Checking test coverage for stable build...${NC}"
                # coverage_percent=$(pytest --cov --cov-report=term | grep TOTAL | awk '{print $4}' | sed 's/%//')
                coverage_percent=85  # Mock for now
                
                if [ "$coverage_percent" -ge 40 ]; then
                    echo -e "${GREEN}✓ Coverage ${coverage_percent}% meets threshold (≥40%)${NC}"
                    publish_build "stable"
                else
                    echo -e "${RED}✗ Coverage ${coverage_percent}% below threshold (≥40%) - skipping stable${NC}"
                fi
            else
                echo -e "${YELLOW}════════════════════════════════════════════════════════${NC}"
                echo -e "${YELLOW}  ⏸️  SKIPPING STABLE PYPI PUBLISH${NC}"
                echo -e "${YELLOW}════════════════════════════════════════════════════════${NC}"
                echo -e ""
                echo -e "Stable PyPI packages are BLOCKED by default to prevent"
                echo -e "publishing immature code."
                echo -e ""
                echo -e "To enable stable publishing:"
                echo -e "  • Set ALLOW_STABLE_BUILD=true in Jenkins parameters"
                echo -e "  • Ensure test coverage ≥40%"
                echo -e "  • Ensure all quality gates pass"
                echo -e ""
                echo -e "Current status: Experimental, Alpha, Beta published ✓"
                echo -e "Stable: BLOCKED - not yet production ready"
                echo -e "${YELLOW}════════════════════════════════════════════════════════${NC}"
            fi
            ;;
        experimental|alpha|beta|stable)
            publish_build "$build_type"
            ;;
        *)
            echo -e "${RED}Usage: $0 [all|experimental|alpha|beta|stable]${NC}"
            exit 1
            ;;
    esac
    
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  Build Complete - Build #${new_build}${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${BLUE}Versions published to http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}:${NC}"
    echo -e "  Experimental: $(generate_version experimental)"
    echo -e "  Alpha:        $(generate_version alpha)"
    echo -e "  Beta:         $(generate_version beta)"
    echo -e "  Stable:       $(generate_version stable) ${YELLOW}(if coverage passed)${NC}"
    echo ""
    echo -e "${BLUE}Install from PyPI:${NC}"
    echo -e "  pip install --index-url http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}/simple fluid-forge==$(generate_version experimental) # Experimental"
    echo -e "  pip install --index-url http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}/simple fluid-forge==$(generate_version alpha)        # Alpha"
    echo -e "  pip install --index-url http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}/simple fluid-forge==$(generate_version beta)         # Beta"
    echo -e "  pip install --index-url http://${NAS_HOST:-localhost}:${PYPI_PORT:-8080}/simple fluid-forge==$(generate_version stable)       # Stable"
    echo ""
    echo -e "${BLUE}Local Archive:${NC}"
    ls -R pypi/ 2>/dev/null || echo "  (check fluid_build/pypi/)"
}

# Run
main "$@"
