@echo off
REM FLUID Build - Windows Setup Script
REM This script provides one-command setup for Windows users

echo.
echo =========================================
echo   FLUID Build - Windows Setup
echo =========================================
echo.

REM Check if make is available
where make >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ Error: 'make' command not found.
    echo.
    echo To install make on Windows, you have several options:
    echo   1. Install via Chocolatey: choco install make
    echo   2. Install via Scoop: scoop install make
    echo   3. Install Git for Windows (includes make in Git Bash)
    echo   4. Use WSL (Windows Subsystem for Linux)
    echo.
    echo Or run the setup manually:
    echo   python -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -e ".[dev,gcp,snowflake,viz]"
    echo.
    pause
    exit /b 1
)

REM Check if Python is available
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ Error: Python not found. Please install Python 3.8+ from https://python.org
    pause
    exit /b 1
)

echo ✅ Python found
echo ✅ Make found
echo.

REM Run the setup
echo 🚀 Running setup...
make setup

if %ERRORLEVEL% equ 0 (
    echo.
    echo ========================================
    echo   🎉 Setup Complete!
    echo ========================================
    echo.
    echo To activate the environment and start using FLUID Build:
    echo   .venv\Scripts\activate
    echo   python -m fluid_build.cli --help
    echo.
    echo Example commands:
    echo   python -m fluid_build.cli version
    echo   python -m fluid_build.cli validate examples\customer360\contract.fluid.yaml
    echo.
) else (
    echo.
    echo ❌ Setup failed. Please check the error messages above.
    echo.
)

pause