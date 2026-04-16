#!/bin/bash

# FLUID Build - macOS/Linux Setup Script
# This script provides one-command setup for Unix-like systems

set -e  # Exit on any error

echo ""
echo "========================================="
echo "   FLUID Build - macOS/Linux Setup"
echo "========================================="
echo ""

# Color codes for better output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}✅${NC} $1"
}

print_error() {
    echo -e "${RED}❌${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ️${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠️${NC} $1"
}

# Check if make is available
if ! command -v make &> /dev/null; then
    print_error "Make command not found."
    echo ""
    echo "To install make:"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "  macOS: Install Xcode Command Line Tools:"
        echo "    xcode-select --install"
        echo "  Or install via Homebrew:"
        echo "    brew install make"
    else
        echo "  Ubuntu/Debian: sudo apt-get install build-essential"
        echo "  CentOS/RHEL: sudo yum groupinstall 'Development Tools'"
        echo "  Arch Linux: sudo pacman -S base-devel"
    fi
    echo ""
    echo "Or run the setup manually:"
    echo "  python3 -m venv .venv"
    echo "  source .venv/bin/activate"
    echo "  pip install -e \".[dev,gcp,snowflake,viz]\""
    echo ""
    exit 1
fi

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 not found. Please install Python 3.8+ from https://python.org"
    echo ""
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macOS installation options:"
        echo "  1. Download from https://python.org"
        echo "  2. Install via Homebrew: brew install python"
        echo "  3. Install via pyenv: pyenv install 3.11"
    else
        echo "Linux installation options:"
        echo "  Ubuntu/Debian: sudo apt-get install python3 python3-venv python3-pip"
        echo "  CentOS/RHEL: sudo yum install python3 python3-venv python3-pip"
        echo "  Arch Linux: sudo pacman -S python python-pip"
    fi
    echo ""
    exit 1
fi

print_status "Python found: $(python3 --version)"
print_status "Make found: $(make --version | head -n1)"
echo ""

# Check if we're in the right directory
if [[ ! -f "pyproject.toml" ]] || [[ ! -f "Makefile" ]]; then
    print_error "Please run this script from the FLUID Build root directory"
    echo "Expected files: pyproject.toml, Makefile"
    exit 1
fi

# Check for common issues
if [[ "$OSTYPE" == "darwin"* ]] && command -v brew &> /dev/null; then
    print_info "macOS with Homebrew detected"
    
    # Check for common macOS development issues
    if ! brew list python &> /dev/null && ! brew list python@3.11 &> /dev/null; then
        print_warning "Consider installing Python via Homebrew for better compatibility:"
        echo "  brew install python"
    fi
fi

# Run the setup
print_info "Running setup..."
echo ""

if make setup; then
    echo ""
    echo "========================================"
    echo "   🎉 Setup Complete!"
    echo "========================================"
    echo ""
    echo "To activate the environment and start using FLUID Build:"
    echo "  source .venv/bin/activate"
    echo "  python -m fluid_build.cli --help"
    echo ""
    echo "Example commands:"
    echo "  python -m fluid_build.cli version"
    echo "  python -m fluid_build.cli validate examples/customer360/contract.fluid.yaml"
    echo ""
    echo "For documentation:"
    echo "  make docs-dev     # Start documentation server"
    echo "  make demo         # Run demo workflow"
    echo ""
else
    echo ""
    print_error "Setup failed. Please check the error messages above."
    echo ""
    echo "For manual setup:"
    echo "  python3 -m venv .venv"
    echo "  source .venv/bin/activate"
    echo "  pip install --upgrade pip wheel"
    echo "  pip install -e \".[dev,gcp,snowflake,viz]\""
    echo ""
    exit 1
fi