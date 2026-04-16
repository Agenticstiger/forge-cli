# FLUID Build - PowerShell Setup Script
# This script provides one-command setup for Windows PowerShell users

# Enable strict mode for better error handling
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Colors for better output
$Red = "Red"
$Green = "Green"
$Yellow = "Yellow"
$Blue = "Cyan"

function Write-Status {
    param([string]$Message)
    Write-Host "✅ $Message" -ForegroundColor $Green
}

function Write-Error-Message {
    param([string]$Message)
    Write-Host "❌ $Message" -ForegroundColor $Red
}

function Write-Info {
    param([string]$Message)
    Write-Host "ℹ️ $Message" -ForegroundColor $Blue
}

function Write-Warning-Message {
    param([string]$Message)
    Write-Host "⚠️ $Message" -ForegroundColor $Yellow
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor $Blue
Write-Host "   FLUID Build - PowerShell Setup" -ForegroundColor $Blue
Write-Host "=========================================" -ForegroundColor $Blue
Write-Host ""

# Check if Python is available
try {
    $pythonVersion = python --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python not found"
    }
    Write-Status "Python found: $pythonVersion"
} catch {
    Write-Error-Message "Python not found. Please install Python 3.8+ from https://python.org"
    Write-Host ""
    Write-Host "Installation steps:"
    Write-Host "  1. Download Python from https://python.org"
    Write-Host "  2. Make sure to check 'Add Python to PATH' during installation"
    Write-Host "  3. Restart PowerShell after installation"
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# Check if make is available
$makeAvailable = $false
try {
    $null = Get-Command make -ErrorAction Stop
    $makeAvailable = $true
    Write-Status "Make found"
} catch {
    Write-Warning-Message "Make command not found"
    Write-Host ""
    Write-Host "To install make on Windows:"
    Write-Host "  • Via Chocolatey: choco install make"
    Write-Host "  • Via Scoop: scoop install make"
    Write-Host "  • Via winget: winget install GnuWin32.Make"
    Write-Host "  • Install Git for Windows (includes make in Git Bash)"
    Write-Host ""
    Write-Info "Falling back to manual setup..."
}

# Check if we're in the right directory
if (-not (Test-Path "pyproject.toml") -or -not (Test-Path "Makefile")) {
    Write-Error-Message "Please run this script from the FLUID Build root directory"
    Write-Host "Expected files: pyproject.toml, Makefile"
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Info "Starting setup..."
Write-Host ""

try {
    if ($makeAvailable) {
        # Use make if available
        Write-Info "Using make for setup..."
        make setup
        
        if ($LASTEXITCODE -ne 0) {
            throw "Make setup failed"
        }
    } else {
        # Manual setup
        Write-Info "Running manual setup..."
        
        # Create virtual environment
        Write-Host "📦 Creating virtual environment..."
        python -m venv .venv
        
        # Activate and install
        Write-Host "📦 Installing packages..."
        & ".venv\Scripts\Activate.ps1"
        python -m pip install --upgrade pip wheel
        python -m pip install -e ".[dev,gcp,snowflake,viz]"
        
        # Test installation
        Write-Host "🔍 Testing installation..."
        python -m fluid_build.cli --version
    }
    
    Write-Host ""
    Write-Host "========================================" -ForegroundColor $Green
    Write-Host "   🎉 Setup Complete!" -ForegroundColor $Green
    Write-Host "========================================" -ForegroundColor $Green
    Write-Host ""
    Write-Host "To activate the environment and start using FLUID Build:"
    Write-Host "  .venv\Scripts\Activate.ps1" -ForegroundColor $Yellow
    Write-Host "  python -m fluid_build.cli --help" -ForegroundColor $Yellow
    Write-Host ""
    Write-Host "Example commands:"
    Write-Host "  python -m fluid_build.cli version"
    Write-Host "  python -m fluid_build.cli validate examples\customer360\contract.fluid.yaml"
    Write-Host ""
    Write-Host "For more help:"
    Write-Host "  • Documentation: make docs-dev"
    Write-Host "  • Run demo: make demo"
    Write-Host "  • See SETUP.md for detailed guide"
    Write-Host ""
    
} catch {
    Write-Host ""
    Write-Error-Message "Setup failed: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "For manual setup, try:"
    Write-Host "  python -m venv .venv"
    Write-Host "  .venv\Scripts\Activate.ps1"
    Write-Host "  pip install --upgrade pip wheel"
    Write-Host "  pip install -e `".[dev,gcp,snowflake,viz]`""
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Read-Host "Press Enter to exit"