#!/bin/bash
# install-fluid-cli.sh
# Easy installation script for FLUID CLI from build artifacts
#
# Usage:
#   ./install-fluid-cli.sh              # Install latest build
#   ./install-fluid-cli.sh 0.8.0        # Install specific version

set -e

# Configuration - override via environment variables
NAS_HOST="${NAS_HOST:-localhost}"
NAS_SSH_USER="${NAS_SSH_USER:-khyana_ai}"
ARTIFACT_REPO="ssh://${NAS_SSH_USER}@${NAS_HOST}/volume1/git-server/fluid-cli-builds.git"
ARTIFACT_DIR="builds"
PACKAGE_NAME="fluid-forge"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Version to install (default: latest)
VERSION="${1:-latest}"

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}   FLUID CLI Installer${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 is not installed${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo -e "${GREEN}✓${NC} Python ${PYTHON_VERSION} detected"

# Create temp directory
TEMP_DIR=$(mktemp -d)
trap "rm -rf ${TEMP_DIR}" EXIT

echo ""
echo -e "${YELLOW}📥 Downloading FLUID CLI from artifact repository...${NC}"

# Clone artifact repository
if git clone --depth 1 "${ARTIFACT_REPO}" "${TEMP_DIR}" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Artifact repository cloned"
else
    echo -e "${RED}❌ Failed to clone artifact repository${NC}"
    echo "   Make sure you have access to: ${ARTIFACT_REPO}"
    echo "   Repository: fluid-cli-builds.git"
    exit 1
fi

cd "${TEMP_DIR}/${ARTIFACT_DIR}"

# Find wheel file
if [ "${VERSION}" = "latest" ]; then
    WHEEL_FILE=$(ls -t ${PACKAGE_NAME}-*.whl 2>/dev/null | head -1)
    echo -e "${GREEN}✓${NC} Found latest build: ${WHEEL_FILE}"
else
    WHEEL_FILE=$(ls ${PACKAGE_NAME}-${VERSION}*.whl 2>/dev/null | head -1)
    if [ -z "${WHEEL_FILE}" ]; then
        echo -e "${RED}❌ Version ${VERSION} not found${NC}"
        echo ""
        echo "Available versions:"
        ls -1 ${PACKAGE_NAME}-*.whl | sed 's/.*-\([0-9.]*\)-.*/  - \1/' | sort -u
        exit 1
    fi
    echo -e "${GREEN}✓${NC} Found version: ${WHEEL_FILE}"
fi

# Show build metadata if available
METADATA_FILE="${WHEEL_FILE%.whl}.json"
if [ -f "${METADATA_FILE}" ]; then
    echo ""
    echo -e "${BLUE}📋 Build Information:${NC}"
    cat "${METADATA_FILE}" | python3 -m json.tool | grep -E "(version|build_date|git_commit)" | sed 's/^/   /'
fi

echo ""
echo -e "${YELLOW}📦 Installing FLUID CLI...${NC}"

# Ask for installation preference
echo ""
echo "Choose installation method:"
echo "  1) User install (recommended)"
echo "  2) System-wide install (requires sudo)"
echo "  3) Virtual environment (isolated)"
read -p "Enter choice [1-3]: " INSTALL_METHOD

case ${INSTALL_METHOD} in
    1)
        echo -e "${BLUE}Installing for current user...${NC}"
        pip3 install --user --upgrade "${WHEEL_FILE}"
        INSTALLED_PATH="${HOME}/.local/bin"
        ;;
    2)
        echo -e "${BLUE}Installing system-wide...${NC}"
        sudo pip3 install --upgrade "${WHEEL_FILE}"
        INSTALLED_PATH="/usr/local/bin"
        ;;
    3)
        echo -e "${BLUE}Creating virtual environment...${NC}"
        VENV_DIR="${HOME}/.fluid-cli-venv"
        python3 -m venv "${VENV_DIR}"
        source "${VENV_DIR}/bin/activate"
        pip install --upgrade "${WHEEL_FILE}"
        INSTALLED_PATH="${VENV_DIR}/bin"
        
        # Create activation helper
        cat > "${HOME}/.fluid-cli-activate" << 'EOF'
#!/bin/bash
source ${HOME}/.fluid-cli-venv/bin/activate
export PS1="(fluid-cli) ${PS1}"
EOF
        chmod +x "${HOME}/.fluid-cli-activate"
        
        echo ""
        echo -e "${YELLOW}To use FLUID CLI, run:${NC}"
        echo "   source ~/.fluid-cli-activate"
        echo "   # or add to ~/.bashrc for automatic activation"
        ;;
    *)
        echo -e "${RED}Invalid choice${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Installation Complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Verify installation
if command -v fluid &> /dev/null; then
    FLUID_VERSION=$(fluid version 2>/dev/null | head -1 || echo "unknown")
    echo -e "${GREEN}✓${NC} FLUID CLI installed: ${FLUID_VERSION}"
    echo ""
    echo -e "${BLUE}Try these commands:${NC}"
    echo "   fluid --help"
    echo "   fluid version"
    echo "   fluid blueprint list"
    echo "   fluid forge --mode copilot"
else
    echo -e "${YELLOW}⚠️  'fluid' command not found in PATH${NC}"
    echo ""
    echo "Add to your PATH by running:"
    echo "   export PATH=\"${INSTALLED_PATH}:\$PATH\""
    echo ""
    echo "Or add to ~/.bashrc:"
    echo "   echo 'export PATH=\"${INSTALLED_PATH}:\$PATH\"' >> ~/.bashrc"
fi

echo ""
echo -e "${BLUE}📚 Documentation:${NC}"
echo "   https://your-docs-url.com/fluid-cli"
echo ""
echo -e "${BLUE}🐛 Issues & Support:${NC}"
echo "   Contact: fluid-team@yourcompany.com"
