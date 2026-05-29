@echo off
REM LlamaShift Windows Installer Launcher
REM Auto-detects and runs the Python installer or opens PowerShell with admin rights

echo ============================================================
echo   LlamaShift - Windows Installer
echo ============================================================
echo.

REM Check if running as administrator
net session >nul 2>&1
if %errorLevel% == 0 (
    echo [OK] Running as Administrator
) else (
    echo [!] Not running as Administrator.
    echo     Requesting elevated privileges...
    echo.
    
    REM Relaunch as admin using PowerShell
    powershell -Command "Start-Process '%~f0' -Verb RunAs"; exit
)

REM Check Python
python --version 2>nul
if %errorLevel% neq 0 (
    echo [!] Python 3.10+ not found.
    echo     Please install Python from https://www.python.org/downloads/
    echo     Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Python found. Installing dependencies...
echo.

REM Install Python dependencies with pinned versions
pip install "flask>=3.0.0" "requests>=2.28.0" "psutil>=5.9.0"
if %errorLevel% neq 0 (
    echo [!] Failed to install Python dependencies.
    echo     Try running: pip install flask requests psutil
    echo.
    pause
    exit /b 1
)

echo.
echo Dependencies installed successfully.
echo.

REM Run the Python installer
echo Starting LlamaShift installer...
echo.
python "%~dp0install.py"

echo.
if %errorLevel% == 0 (
    echo.
    echo Installation complete!
    echo.
    echo TIP: You can reconfigure model parameters at any time from the Web UI:
    echo      - Click the gear icon on each model card to adjust context size,
    echo        parallel requests, GPU layers, ports, and device assignments.
    echo      - Toggle between Single-port and Multi-port mode in the header.
) else (
    echo.
    echo Installer exited with error code %errorLevel%
)
echo.
pause
