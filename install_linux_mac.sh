#!/usr/bin/env bash
# LlamaShift - Linux/macOS Installer Launcher
# Auto-checks prerequisites and runs the Python installer with sudo

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================================"
echo "  LlamaShift - Linux/macOS Installer"
echo "============================================================"
echo ""

# Color helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Detect OS
OS_TYPE="$(uname -s)"
if [[ "$OS_TYPE" == "Linux" ]]; then
    PLATFORM="Linux"
elif [[ "$OS_TYPE" == "Darwin" ]]; then
    PLATFORM="macOS"
else
    echo -e "${RED}[!] Unsupported OS: $OS_TYPE${NC}"
    exit 1
fi

echo -e "${CYAN}[i] Detected platform: $PLATFORM${NC}"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[!] Python 3 not found.${NC}"
    if [[ "$PLATFORM" == "Linux" ]]; then
        echo "    Install with: sudo apt install python3 python3-pip  # Debian/Ubuntu"
        echo "                 sudo dnf install python3 python3-pip   # Fedora/RHEL"
        echo "                 sudo pacman -S python python-pip        # Arch"
    else
        echo "    Install with: brew install python"
    fi
    echo ""
    exit 1
fi

PYTHON_VER=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}[OK] Python 3 found: $PYTHON_VER${NC}"

# Check Python version >= 3.10
PYTHON_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [[ "$PYTHON_MAJOR" -lt 3 ]] || ([[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 10 ]]); then
    echo -e "${RED}[!] Python 3.10+ required (found $PYTHON_MAJOR.$PYTHON_MINOR).${NC}"
    exit 1
fi

echo -e "${GREEN}[OK] Python version >= 3.10 satisfied${NC}"
echo ""

# Check pip
if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null; then
    echo -e "${YELLOW}[!] pip not found. Installing...${NC}"
    if command -v pip3 &>/dev/null; then
        PIP_CMD="pip3"
    else
        PIP_CMD="python3 -m pip"
    fi
else
    if command -v pip3 &>/dev/null; then
        PIP_CMD="pip3"
    else
        PIP_CMD="python3 -m pip"
    fi
fi

# Install dependencies
echo -e "${CYAN}[i] Installing Python dependencies...${NC}"
$PIP_CMD install "flask>=3.0.0" "requests>=2.28.0" "psutil>=5.9.0" 2>&1 | tail -1
if [[ $? -ne 0 ]]; then
    echo -e "${RED}[!] Failed to install dependencies.${NC}"
    echo "    Try: $PIP_CMD install flask requests psutil"
    exit 1
fi

echo -e "${GREEN}[OK] Dependencies installed${NC}"
echo ""

# Check for sudo (Linux only)
if [[ "$PLATFORM" == "Linux" ]]; then
    if ! command -v sudo &>/dev/null; then
        echo -e "${RED}[!] sudo is required but not installed.${NC}"
        echo "    Please run this script as root or install sudo."
        exit 1
    fi

    # Test sudo access
    if ! sudo -n true 2>/dev/null; then
        echo -e "${YELLOW}[!] Administrator access required for service installation.${NC}"
        echo "    The installer will prompt for your password."
        echo ""
    fi
fi

# Run the Python installer
echo -e "${CYAN}[i] Starting LlamaShift installer...${NC}"
echo ""

if [[ "$PLATFORM" == "Linux" ]]; then
    sudo python3 "${SCRIPT_DIR}/install_linux_mac.py"
else
    python3 "${SCRIPT_DIR}/install_linux_mac.py"
fi

EXIT_CODE=$?

echo ""
if [[ $EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}[OK] Installation complete!${NC}"
    echo "    Access the UI at: http://localhost:8002"
else
    echo -e "${RED}[!] Installer exited with error code $EXIT_CODE${NC}"
fi

exit $EXIT_CODE