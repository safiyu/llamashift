#!/usr/bin/env python3
"""
LlamaShift Universal Installer
Auto-detects OS and guides user through setup with questions.
Requires admin/root access.

Usage:
  sudo python3 install.py          # Linux/macOS
  python install.py                # Windows (run as Administrator)
"""

import sys
import os
import platform
import subprocess
import json
import shutil
from pathlib import Path

# ─── Color helpers (fallback to plain if no tty) ───────────────────────────
def use_color():
    return sys.stdout.isatty()

def C(text, color):
    if not use_color():
        return text
    codes = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "white": "37", "bold": "1"}
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"

def print_header(text):
    print(f"\n{C('═' * 60, 'cyan')}")
    print(f"  {C(text, 'bold')}")
    print(f"{'═' * 60}\n")

def print_ok(text):
    print(f"  {C('✓', 'green')} {text}")

def print_warn(text):
    print(f"  {C('⚠', 'yellow')} {text}")

def print_err(text):
    print(f"  {C('✗', 'red')} {text}")

def print_info(text):
    print(f"  ℹ {text}")

def ask(question, default=None):
    """Ask a yes/no question."""
    if default:
        prompt = f"  {question} [Y/n]: "
    else:
        prompt = f"  {question} [y/N]: "
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if default and not answer:
        return True
    return answer in ("y", "yes")

def ask_text(question, default=None):
    """Ask a text question."""
    if default is not None:
        prompt = f"  {question} [{default}]: "
    else:
        prompt = f"  {question}: "
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    return answer if answer else (default if default is not None else "")

def ask_choice(question, options):
    """Ask user to pick from numbered options."""
    print(f"\n{C(question, 'bold')}")
    for i, opt in enumerate(options, 1):
        print(f"    {i}. {opt}")
    print()
    while True:
        try:
            val = input("  Select [1-{}]: ".format(len(options))).strip()
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except (ValueError, EOFError):
            pass

def require_admin():
    """Ensure script runs with admin/root privileges."""
    if os.geteuid() != 0:
        print_err("This installer requires root/admin privileges.")
        print_err("Please run with: sudo python3 install.py")
        print_err("(On Windows, run PowerShell as Administrator)")
        sys.exit(1)

# ─── OS Detection ──────────────────────────────────────────────────────────
def detect_os():
    """Return one of: 'linux', 'macos', 'windows'."""
    family = platform.system()
    if family == "Linux":
        return "linux"
    elif family == "Darwin":
        return "macos"
    elif family == "Windows":
        return "windows"
    return "unknown"

# ─── GPU Detection ─────────────────────────────────────────────────────────
def detect_gpu():
    """Detect available GPUs and return list of devices + driver info."""
    os_type = detect_os()
    result = {
        "type": "cpu",
        "drivers": {},
        "devices": [],       # Array of {name, backend, index, memory}
        "gpus": [],          # Human-readable names
    }

    # Check NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version",
                 "--format=csv,noheader"],
                stderr=subprocess.DEVNULL, timeout=5)
            nvidia_devices = []
            for line in out.decode().strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    idx = parts[0]
                    name = parts[1]
                    mem = parts[2]
                    nvidia_devices.append({
                        "name": name,
                        "backend": "CUDA",
                        "index": int(idx),
                        "memory": mem,
                    })
            if nvidia_devices:
                result["type"] = "nvidia"
                result["devices"].extend(nvidia_devices)
                result["gpus"] = [d["name"] for d in nvidia_devices]
                result["drivers"]["cuda"] = True
        except Exception:
            pass

    # Check AMD ROCm
    amd_devices = []
    if os.path.exists("/dev/kfd") or os.path.exists("/dev/dri"):
        result["drivers"]["rocm"] = True
        if shutil.which("rocm-smi"):
            try:
                out = subprocess.check_output(
                    ["rocm-smi", "--showproductname", "--json"],
                    stderr=subprocess.DEVNULL, timeout=5)
                data = json.loads(out.decode())
                # rocm-smi returns {"card0": {...}, "card1": {...}} or {"cards": [...]}
                card_keys = []
                if "cards" in data:
                    card_keys = enumerate(data["cards"])
                else:
                    # Collect card0, card1, ... keys
                    for k in sorted(data.keys(), key=lambda x: int(x.replace("card", "")) if x.startswith("card") and x[4:].isdigit() else 999):
                        if k.startswith("card") and k[4:].isdigit():
                            card_keys.append((int(k[4:]), data[k]))

                # Now fetch VRAM info to attach memory totals
                vram_info = {}
                try:
                    vram_out = subprocess.check_output(
                        ["rocm-smi", "--showmeminfo", "vram", "--json"],
                        stderr=subprocess.DEVNULL, timeout=5)
                    vram_data = json.loads(vram_out.decode())
                    for vk in vram_data:
                        if vk.startswith("card") and vk[4:].isdigit():
                            vram_info[int(vk[4:])] = vram_data[vk]
                except Exception:
                    pass

                for i, card in card_keys:
                    # Try multiple field names for GPU name
                    name = (
                        card.get("Card Series")
                        or card.get("Card SKU")
                        or card.get("product_name")
                        or card.get("Name")
                        or f"AMD GPU {i}"
                    )
                    # Resolve VRAM total from the separate query
                    mem_str = "unknown"
                    vram_card = vram_info.get(i, {})
                    vram_total_b = vram_card.get("VRAM Total Memory (B)")
                    if vram_total_b:
                        try:
                            total_mb = int(str(vram_total_b).replace('"', '')) // (1024 * 1024)
                            mem_str = f"{total_mb} MiB"
                        except (ValueError, TypeError):
                            pass
                    amd_devices.append({
                        "name": name,
                        "backend": "ROCm",
                        "index": i,
                        "memory": mem_str,
                    })
            except Exception:
                pass

        if not amd_devices:
            # Fallback: try lshw or lspci
            try:
                out = subprocess.check_output(
                    ["lspci", "-nn"], stderr=subprocess.DEVNULL, timeout=5)
                for line in out.decode().split("\n"):
                    if "VGA" in line or "Display" in line:
                        if "AMD" in line or "Advanced Micro Devices" in line:
                            name = line.split(":")[1].split("@")[0].strip() if ":" in line else f"AMD GPU"
                            amd_devices.append({
                                "name": name,
                                "backend": "ROCm",
                                "index": len(amd_devices),
                                "memory": "unknown",
                            })
            except Exception:
                pass

        if amd_devices:
            if result["type"] == "cpu":
                result["type"] = "amd"
            result["devices"].extend(amd_devices)
            result["gpus"].extend([d["name"] for d in amd_devices])
        elif result["drivers"].get("rocm"):
            # ROCm drivers exist but no GPU detected via tools
            result["type"] = "amd" if result["type"] == "cpu" else result["type"]

    # Check Vulkan (secondary backend)
    if shutil.which("vulkaninfo"):
        try:
            out = subprocess.check_output(
                ["vulkaninfo", "--summary"], stderr=subprocess.DEVNULL, timeout=5)
            for line in out.decode().split("\n"):
                if "deviceName" in line:
                    name = line.split(":")[1].strip()
                    # Skip if already detected by NVIDIA/ROCm
                    existing = [d["name"] for d in result["devices"]]
                    if name not in existing:
                        result["devices"].append({
                            "name": name,
                            "backend": "Vulkan",
                            "index": len(result["devices"]),
                            "memory": "unknown",
                        })
        except Exception:
            pass

    # Try to get device IDs from llama-server --list-devices
    # This gives us the exact format llama-server expects (e.g. "ROCm0", "Vulkan0", "CUDA:0")
    binary = find_llama_server_binary()
    llama_devices = enumerate_llama_devices(binary)
    result["_llama_devices"] = llama_devices

    # Now set device_id on each detected device based on llama-server output
    for dev in result["devices"]:
        backend = dev.get("backend", "")
        idx = dev.get("index", 0)
        # Look up the device_id from llama-server --list-devices
        if (backend, idx) in llama_devices:
            dev["device_id"] = llama_devices[(backend, idx)]
        elif backend == "CUDA":
            dev["device_id"] = f"CUDA:{idx}"  # CUDA uses colon format
        elif backend == "ROCm":
            dev["device_id"] = f"ROCm{idx}"  # ROCm uses no separator (e.g. "ROCm0")
        elif backend == "Vulkan":
            dev["device_id"] = f"Vulkan{idx}"  # Vulkan uses no separator (e.g. "Vulkan0")
        else:
            dev["device_id"] = f"{backend}:{idx}"

    if not result["devices"]:
        result["devices"] = [{"name": "CPU", "backend": "CPU", "index": 0, "memory": "system", "device_id": "CPU"}]

    return result


def find_llama_server_binary():
    """Search common locations for llama-server binary."""
    common_paths = [
        os.path.expanduser("~/bin/llama-server"),
        os.path.expanduser("~/.llama/llama-server"),
        "/usr/local/bin/llama-server",
        "/usr/bin/llama-server",
        os.path.expanduser("./llama-server"),
    ]
    for p in common_paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    if shutil.which("llama-server"):
        return shutil.which("llama-server")
    return None


def enumerate_llama_devices(binary_path=None):
    """Run llama-server --list-devices and parse output into device_id list.
    Returns dict mapping (backend, index) -> device_id string.
    Example --list-devices output:
      ROCm0:  AMD Radeon Pro W7900
      Vulkan0: AMD Radeon Pro W7900
      Vulkan1: AMD Radeon Pro W7900
      Vulkan2: AMD Radeon Pro W7900
    """
    devices = {}
    binary = binary_path or find_llama_server_binary()
    if not binary:
        return devices

    try:
        out = subprocess.check_output([binary, "--list-devices"],
                                       stderr=subprocess.DEVNULL, timeout=10)
        for line in out.decode().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Format is "BackendN: description" or "Backend:N: description"
            # Match "Backend" + optional ":" + index at start
            import re
            m = re.match(r'^([A-Za-z]+)(\d+):', line)
            if m:
                backend = m.group(1)
                idx = int(m.group(2))
                device_id = f"{backend}{idx}"  # e.g. "ROCm0", "Vulkan0"
                devices[(backend, idx)] = device_id
    except Exception:
        pass

    return devices

# ─── Model Path Setup ──────────────────────────────────────────────────────
def find_gguf_files(base_dir=None):
    """Find available GGUF model files."""
    if base_dir and os.path.isdir(base_dir):
        candidates = [base_dir]
    else:
        candidates = [
            os.path.expanduser("~/models"),
            os.path.expanduser("~/.cache/huggingface/hub/"),
            "/opt/models",
            "./models",
        ]
    files = []
    for d in candidates:
        if os.path.isdir(d):
            for root, _, fnames in os.walk(d):
                for f in fnames:
                    if f.endswith(".gguf"):
                        files.append(os.path.join(root, f))
    return files[:50]  # Cap at 50

# ─── Install Steps ─────────────────────────────────────────────────────────
def check_llama_server_binary(path):
    """Check if a path is a valid llama-server binary and return version info."""
    if not path:
        return None
    if not os.path.isfile(path):
        return None
    if not os.access(path, os.X_OK):
        return None
    try:
        out = subprocess.check_output([path, "--help"],
                                       stderr=subprocess.DEVNULL, timeout=5)
        out_str = out.decode(errors="replace").lower()
        version = ""
        for line in out_str.split("\n"):
            if "llama-server" in line or "version" in line:
                version = line.strip()[:80]
                break
        return {"path": path, "version": version, "valid": True}
    except Exception:
        return None

def step_prerequisites(os_type, app_dir):
    """Check and install required system packages."""
    print_header("Step 1: Checking Prerequisites")

    # Python
    if sys.version_info < (3, 10):
        print_err(f"Python 3.10+ required (found {sys.version_info.major}.{sys.version_info.minor})")
        sys.exit(1)
    print_ok(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # pip packages (PEP 668 aware)
    print_ok("Checking Python dependencies...")
    requirements_file = app_dir / "requirements.txt"
    
    # Check if all required packages are available
    missing_packages = []
    for pkg_name in ["flask"]:
        try:
            __import__(pkg_name.lower())
        except ImportError:
            missing_packages.append(pkg_name)
    
    used_venv = False
    venv_path = app_dir / ".venv"
    
    if missing_packages:
        print_warn(f"Missing packages: {', '.join(missing_packages)}")
        
        # Check if we're in an externally-managed environment (PEP 668)
        is_pep668 = os.path.exists("/usr/lib/python3/dist-packages/externally-managed")
        
        if is_pep668:
            print_info("Detected externally-managed Python environment (PEP 668).")
            if venv_path.exists():
                print_ok(f"Using existing virtual environment: {venv_path}")
                used_venv = True
            elif ask("Create a virtual environment for dependencies?", True):
                print_info(f"Creating virtual environment at {venv_path}...")
                try:
                    subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
                    print_ok("Virtual environment created.")
                    used_venv = True
                except subprocess.CalledProcessError:
                    print_err("Failed to create virtual environment.")
                    print_info("Falling back to --break-system-packages flag...")
            else:
                print_info("Falling back to --break-system-packages flag...")
        
        if used_venv:
            # Use venv pip
            if os_type == "windows":
                pip_exe = venv_path / "Scripts" / "pip.exe"
            else:
                pip_exe = venv_path / "bin" / "pip"
            
            if pip_exe.exists():
                print_info("Installing dependencies into virtual environment...")
                result = subprocess.run([str(pip_exe), "install", "-r", str(requirements_file)],
                                       check=False)
                if result.returncode == 0:
                    print_ok("Dependencies installed in virtual environment.")
                else:
                    print_err("Failed to install dependencies in venv.")
                    used_venv = False
                    print_info("Falling back to system-wide install with --break-system-packages...")
        
        if not used_venv:
            # Fallback: try with --break-system-packages
            print_warn("Installing system-wide with --break-system-packages flag...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--break-system-packages", "-r", str(requirements_file)],
                check=False
            )
            if result.returncode == 0:
                print_ok("Dependencies installed system-wide.")
            else:
                print_err("Failed to install dependencies.")
                print_info("Please install manually:")
                if os_type == "linux":
                    print_info("  sudo apt install python3-flask  # Debian/Ubuntu")
                    print_info("  sudo pip3 install --break-system-packages -r requirements.txt")
                elif os_type == "macos":
                    print_info("  pip3 install -r requirements.txt")
                print()

    # llama-server binary — required prerequisite
    print()
    print_info("▎ llama-server binary (REQUIRED)")
    print_info("   This is the core inference engine from llama.cpp.")
    print_info("   Download from: https://github.com/ggerganov/llama.cpp/releases")
    print_info("   Look for: llama-server with CUDA/ROCm support")
    print()

    # Auto-detect common paths
    common_paths = []
    if os_type == "linux":
        common_paths = [
            os.path.expanduser("~/bin/llama-server"),
            os.path.expanduser("~/.llama/llama-server"),
            "/usr/local/bin/llama-server",
            "/usr/bin/llama-server",
            os.path.expanduser("./llama-server"),
        ]
    elif os_type == "macos":
        common_paths = [
            os.path.expanduser("~/bin/llama-server"),
            os.path.expanduser("./llama-server"),
        ]
    elif os_type == "windows":
        common_paths = [
            r"C:\Program Files\llama.cpp\llama-server.exe",
            r"C:\Users\Public\llama-server.exe",
            os.path.expanduser(".\\llama-server.exe"),
        ]

    auto_detected = []
    for p in common_paths:
        if os.path.isfile(p):
            result = check_llama_server_binary(p)
            if result:
                auto_detected.append((p, result["version"]))

    if auto_detected:
        print_ok(f"Auto-detected {len(auto_detected)} llama-server binary(ies):")
        for i, (path, ver) in enumerate(auto_detected, 1):
            ver_str = f" ({ver})" if ver else ""
            print(f"      {i}. {path}{ver_str}")
        print()
        selected_idx = int(ask_text("  Select binary [1-{}] or 0 to skip".format(len(auto_detected)),
                                      "1"))
        if selected_idx > 0 and selected_idx <= len(auto_detected):
            binary_path = auto_detected[selected_idx - 1][0]
            print_ok(f"Using: {binary_path}")
        else:
            binary_path = ""
            print_warn("Skipping auto-detected binaries.")
    else:
        print_warn("No llama-server binary found in common locations.")
        print_info("Download from: https://github.com/ggerganov/llama.cpp/releases")
        print_info("Look for the llama-server binary (with CUDA/ROCm if you have a GPU).")
        if os_type == "linux":
            print_info("Example paths: /home/user/llama.cpp/build/bin/llama-server")
            print_info("               /usr/local/bin/llama-server")
        elif os_type == "macos":
            print_info("Example paths: /Users/user/llama.cpp/build/bin/llama-server")
        else:
            print_info("Example paths: C:\\llama.cpp\\build\\bin\\Release\\llama-server.exe")
        print()
        binary_path = ask_text("  Path to llama-server binary", default="")
        if binary_path:
            result = check_llama_server_binary(binary_path)
            if result:
                print_ok(f"llama-server valid: {binary_path}")
                if result["version"]:
                    print_info(f"   Version info: {result['version']}")
            else:
                print_warn(f"Binary not found or not executable at: {binary_path}")
                binary_path = ""

    # Return tuple: (binary_path, used_venv, venv_path_or_none)
    return binary_path, used_venv, (venv_path if used_venv else None)

def step_gpu_config(gpu_info):
    """Configure GPU settings, show detected devices, ask for web UI port."""
    print_header("Step 2: GPU & Network Configuration")

    # ── Show detected devices ──
    print_info("▎ Detected devices:")
    devices = gpu_info.get("devices", [])
    for i, dev in enumerate(devices, 1):
        backend = dev.get("backend", "Unknown")
        name = dev.get("name", f"Device {i}")
        mem = dev.get("memory", "unknown")
        marker = " ← default" if i == 1 else ""
        print(f"      {i}. {name} [{backend}] ({mem}){marker}")
    print()

    # If multi-GPU, let user pick which device(s) to use
    available_devices = []
    if len(devices) > 1:
        # Show device selection as multi-choice
        device_str = ask_text(
            "Device numbers to use (comma-separated, 'all' for all)",
            default="all"
        ).strip().lower()
        if device_str == "all" or not device_str:
            available_devices = devices
        else:
            try:
                indices = [int(x.strip()) - 1 for x in device_str.split(",")]
                available_devices = [devices[i] for i in indices if 0 <= i < len(devices)]
            except ValueError:
                available_devices = devices
    else:
        available_devices = devices

    if gpu_info["type"] == "cpu" and not available_devices[0].get("backend") == "CPU":
        print_warn("No GPU detected. Models will run in CPU mode (slower).")
        if ask("Continue anyway?", True):
            available_devices = [{"name": "CPU", "backend": "CPU", "index": 0, "memory": "system"}]

    # ── Mode selection ──
    mode = ask_choice(
        "Operation mode:",
        ["Single-port (one model at a time, ideal for Open WebUI)",
         "Multi-port (multiple models simultaneously)"]
    )
    single_port = (mode == "Single-port (one model at a time, ideal for Open WebUI)")

    config = {"mode": "single_port" if single_port else "multi_port"}
    config["gpu_type"] = gpu_info["type"]
    config["cuda"] = gpu_info["drivers"].get("cuda", False)
    config["rocm"] = gpu_info["drivers"].get("rocm", False)
    config["available_devices"] = available_devices

    # Build devices array matching llama.cpp --list-devices format
    # llama.cpp --list-devices shows: "ROCm0", "Vulkan0", "CUDA:0", etc.
    # For --device arg: CUDA uses "CUDA:0", ROCm uses "ROCm:0", Vulkan uses "Vulkan:0"
    # But newer llama.cpp builds use "Backend:index" without colon for ROCm/Vulkan
    # Store the llama.cpp device identifier directly in the device object during detection
    config["devices_array"] = [d.get("device_id", f"{d['backend']}:{d['index']}") for d in available_devices]

    # ── GPU layer offload ──
    if gpu_info["type"] in ("nvidia", "amd"):
        all_layers = ask("Offload all layers to GPU? (recommended)", True)
        if all_layers:
            config["nGpuLayers_val"] = 999
        else:
            config["nGpuLayers_val"] = int(ask_text("  How many layers to offload to GPU?", "99"))
    else:
        config["nGpuLayers_val"] = 0

    if gpu_info["type"] == "amd":
        config["rocm_device"] = "/dev/kfd"

    # ── Web UI port ──
    print()
    print_info("▎ Web UI settings:")
    config["webUIPort"] = int(ask_text("  Web UI port (management dashboard)", "8002"))
    config["masterPort"] = int(ask_text("  Master API port (model endpoint)", "9000"))

    if single_port:
        print_info("  (In single-port mode, all models share the master API port)")
    else:
        print_info("  (In multi-port mode, each model gets its own port)")

    return config

def _configure_model(chosen, gpu_config, data_dir=None):
    """Configure a single model. Returns model_config dict."""
    filename = os.path.basename(chosen)
    model_id = filename.replace(".gguf", "").replace("-", "").replace("_", "")[:20].lower()

    print()
    print_info("▎ Model details:")
    model_name = ask_text("  Display name", default=filename.replace(".gguf", ""))
    model_desc = ask_text("  Description (purpose/use-case)", default="")

    print()
    print_info("▎ Model runtime settings:")
    ctx_size = int(ask_text("  Context size (tokens)", "16384"))
    n_parallel = int(ask_text("  Parallel requests", "2"))

    # Port
    master_port = gpu_config.get("masterPort", 9000)
    if gpu_config["mode"] == "multi_port":
        model_port = int(ask_text("  Model port", "9001"))
    else:
        model_port = master_port
        print_info(f"  (Single-port mode: using master port {master_port})")

    # GPU device assignment for this model
    devices_array = gpu_config.get("devices_array", [])
    gpu_label = ""
    if gpu_config.get("cuda"):
        gpu_label = "CUDA (NVIDIA)"
    elif gpu_config.get("rocm"):
        gpu_label = "ROCm (AMD)"
    else:
        gpu_label = "CPU"

    # Multi-GPU: let user pick which devices for this model
    if len(gpu_config.get("available_devices", [])) > 1:
        dev_names = [f"{d['name']} [{d['backend']}]" for d in gpu_config["available_devices"]]
        dev_str = ask_text(
            f"  Assign devices (comma-separated, available: {', '.join(dev_names)})",
            default="all"
        ).strip().lower()
        if dev_str == "all" or not dev_str:
            model_devices = list(devices_array)
            gpu_label = "Multi-GPU"
        else:
            try:
                indices = [int(x.strip()) - 1 for x in dev_str.split(",")]
                model_devices = [devices_array[i] for i in indices if 0 <= i < len(devices_array)]
                gpu_label = ", ".join([gpu_config["available_devices"][int(x.strip())-1]["name"]
                                       for x in dev_str.split(",") if x.strip().isdigit()])
            except (ValueError, IndexError):
                model_devices = list(devices_array)
    else:
        model_devices = list(devices_array)

    # MMProj file check (multimodal models)
    mmproj = ""
    model_dir = os.path.dirname(chosen)
    base_name = filename.replace(".gguf", "")
    potential_mmproj = os.path.join(model_dir, f"mmproj-{base_name}.gguf")
    if os.path.exists(potential_mmproj):
        print_info(f"  Found matching mmproj: mmproj-{base_name}.gguf")
        if ask("  Use this multimodal projector file?", True):
            mmproj = os.path.basename(potential_mmproj)
    else:
        mmproj_input = ask_text("  Multimodal projector file (mmproj) name (leave empty if N/A)", default="")
        if mmproj_input:
            mmproj = mmproj_input

    # nGpuLayers override per model
    n_gpu_layers = gpu_config.get("nGpuLayers_val", 0)
    n_gpu_layers_override = ask_text("  GPU layers override (leave empty for global setting)", default="")
    if n_gpu_layers_override:
        n_gpu_layers = int(n_gpu_layers_override)

    # Additional llama-server params
    additional_params = ask_text("  Additional llama-server flags (space-separated, leave empty if N/A)", default="")

    # Calculate size
    size_bytes = os.path.getsize(chosen)
    if size_bytes > 1024**3:
        size_str = f"{size_bytes / 1024**3:.1f}GB"
    else:
        size_str = f"{size_bytes / 1024**2:.0f}MB"

    model_config = {
        "id": model_id,
        "name": model_name,
        "desc": model_desc,
        "size": size_str,
        "gpu": gpu_label,
        "devices": model_devices,
        "filename": filename,
        "port": model_port,
        "ctxSize": ctx_size,
        "nParallel": n_parallel,
        "nGpuLayers": n_gpu_layers,
        "endpoint": f"http://localhost:{model_port}/v1",
    }

    if mmproj:
        model_config["mmproj"] = mmproj
    if additional_params:
        model_config["additionalParams"] = additional_params

    return model_config


def step_model_setup(binary_path, gpu_config):
    """Set up model files. Returns (model_config, data_dir)."""
    print_header("Step 3: Model Configuration")

    gguf_files = find_gguf_files()
    data_dir = None

    if gguf_files:
        print_ok(f"Found {len(gguf_files)} GGUF model(s):")
        for i, f in enumerate(gguf_files[:10], 1):
            size_mb = os.path.getsize(f) / (1024 * 1024)
            print(f"      {i}. {os.path.basename(f)} ({size_mb:.0f}MB)")
        if len(gguf_files) > 10:
            print(f"      ... and {len(gguf_files) - 10} more")
        print()

        if ask("Configure one of the found models?", True):
            while True:
                try:
                    idx = int(ask_text("  Model number", "1"))
                    if 1 <= idx <= len(gguf_files):
                        break
                except ValueError:
                    pass

            chosen = gguf_files[idx - 1]
            model_config = _configure_model(chosen, gpu_config, data_dir)
            # Determine data_dir from chosen model
            data_dir = os.path.dirname(chosen)
            return model_config, data_dir
    else:
        print_warn("No GGUF models found in default locations.")
        print_info("Default locations searched: ~/models, ~/.cache/huggingface/hub/, /opt/models, ./models")
        print()

        data_dir = ask_text(
            "Path to directory containing your GGUF model files",
            default=os.path.expanduser("~/models")
        )

        if not os.path.isdir(data_dir):
            print_warn(f"Directory does not exist: {data_dir}")
            if ask("Create this directory?", True):
                try:
                    os.makedirs(data_dir, exist_ok=True)
                    print_ok(f"Created directory: {data_dir}")
                except Exception as e:
                    print_err(f"Failed to create directory: {e}")
            print_info("You can add models to this directory and re-run the installer.")
            print_info("You can also configure models later by editing config.json.")
            return None, data_dir

        gguf_in_dir = [
            os.path.join(root, f)
            for root, _, fnames in os.walk(data_dir)
            for f in fnames
            if f.endswith(".gguf")
        ]

        if not gguf_in_dir:
            print_warn(f"No .gguf files found in {data_dir}")
            print_info("Download GGUF models from HuggingFace or similar sources.")
            print_info("You can configure models later by editing config.json.")
            return None, data_dir

        gguf_files = gguf_in_dir
        print_ok(f"Found {len(gguf_files)} GGUF model(s) in {data_dir}:")
        for i, f in enumerate(gguf_files[:10], 1):
            size_mb = os.path.getsize(f) / (1024 * 1024)
            print(f"      {i}. {os.path.basename(f)} ({size_mb:.0f}MB)")
        if len(gguf_files) > 10:
            print(f"      ... and {len(gguf_files) - 10} more")
        print()

        if ask("Configure one of the found models?", True):
            while True:
                try:
                    idx = int(ask_text("  Model number", "1"))
                    if 1 <= idx <= len(gguf_files):
                        break
                except ValueError:
                    pass

            chosen = gguf_files[idx - 1]
            model_config = _configure_model(chosen, gpu_config, data_dir)
            return model_config, data_dir

    return None, data_dir

def step_install_service(os_type, app_dir, venv_path=None):
    """Install as systemd service (Linux) or winget service (Windows)."""
    print_header("Step 4: Service Installation")

    install = ask_choice(
        "How should LlamaShift run?",
        ["As a background service (auto-start on boot)",
         "Manual only (run with python3 server.py)"]
    )

    if install == "As a background service (auto-start on boot)":
        if os_type == "linux":
            return install_systemd(app_dir, venv_path)
        elif os_type == "windows":
            return install_windows_service(app_dir)
        else:
            print_warn(f"No native service installer for {os_type}. Use manual mode.")
            return {"method": "manual"}
    else:
        print_ok("Manual mode selected. Run 'python3 server.py' to start.")
        return {"method": "manual"}

def install_systemd(app_dir, venv_path=None):
    """Create systemd service for Linux."""
    service_name = "llamashift"
    service_file = Path("/etc/systemd/system") / f"{service_name}.service"

    user = os.environ.get("SUDO_USER", "root")

    # Use venv python if available, otherwise system python
    if venv_path and (venv_path / "bin" / "python3").exists():
        python_exe = str(venv_path / "bin" / "python3")
    else:
        python_exe = "/usr/bin/env python3"

    content = f"""\
[Unit]
Description=LlamaShift LLM Workstation Manager
After=network.target

[Service]
User={user}
Group={user}
WorkingDirectory={app_dir}
ExecStart={python_exe} {app_dir}/server.py
Restart=always
RestartSec=3
Environment=PATH=/usr/bin:/usr/local/bin
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""

    print_info(f"Creating systemd service at {service_file}...")
    with open(service_file, "w") as f:
        f.write(content)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", service_name], check=True)
    subprocess.run(["systemctl", "start", service_name], check=True)

    print_ok(f"Service '{service_name}' installed and started.")
    print_info(f"Check status: sudo systemctl status {service_name}")
    return {"method": "systemd", "service": service_name, "user": user}

def install_windows_service(app_dir):
    """Install as Windows service using sc.exe or NSSM."""
    service_name = "LlamaShift"
    python_exe = sys.executable

    # Check for NSSM (recommended for Python services)
    nssm_path = shutil.which("nssm")
    if nssm_path:
        print_info("Using NSSM to install Windows service...")
        subprocess.run([nssm_path, "install", service_name, python_exe], check=True)
        subprocess.run([nssm_path, "set", service_name, "AppDirectory", str(app_dir)], check=True)
        subprocess.run([nssm_path, "set", service_name, "AppStdout", str(app_dir / "logs\\llamashift.log")], check=True)
        subprocess.run([nssm_path, "set", service_name, "AppStderr", str(app_dir / "logs\\llamashift.log")], check=True)
        subprocess.run([nssm_path, "start", service_name], check=True)
        print_ok(f"Windows service '{service_name}' installed via NSSM.")
        return {"method": "windows-nssm", "service": service_name}

    # Fallback: try schtasks (less reliable for interactive services)
    print_warn("NSSM not found. Using Windows Task Scheduler as fallback.")
    task_xml = f"""\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>LlamaShift LLM Workstation Manager</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>0</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{app_dir / 'server.py'}"</Arguments>
      <WorkingDirectory>{app_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    task_name = "\\LlamaShift"
    subprocess.run(["schtasks", "/Create", "/TN", task_name, "/XML", task_xml, "/F"], check=True)
    print_ok(f"Scheduled task '{task_name}' created.")
    return {"method": "windows-schtasks", "service": task_name}

# ─── Generate config.json ──────────────────────────────────────────────────
def generate_config(app_dir, binary_path, gpu_config, model_config, data_dir=None):
    """Generate config.json from user answers."""
    # Use provided data_dir, or fall back to app_dir/models
    if data_dir is None:
        data_dir = str(app_dir / "models")
    else:
        # Expand ~ if present
        data_dir = os.path.expanduser(data_dir)

    config = {
        "appName": "llamashift",
        "serviceName": "llamashift",
        "masterPort": model_config.get("masterPort", 9000) if model_config else 9000,
        "binaryPath": binary_path or "",
        "dataDir": data_dir,
        "mode": gpu_config["mode"],
        "models": {}
    }

    if model_config:
        # Remove internal-only fields
        clean = {k: v for k, v in model_config.items()
                 if k not in ("filepath", "masterPort")}
        config["models"][model_config["id"]] = clean

    config_path = app_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    print_ok(f"config.json written to {config_path}")
    return config

# ─── Main ──────────────────────────────────────────────────────────────────
def get_app_dir():
    """Get directory where this script resides."""
    return Path(__file__).parent.resolve()

def main():
    print_header("LlamaShift Universal Installer")
    print_info(f"OS: {platform.system()} {platform.release()}")
    print_info(f"Python: {sys.version}")
    print_info(f"Working dir: {get_app_dir()}")
    print()

    # Check admin
    require_admin()
    print_ok("Admin/root privileges confirmed.")

    os_type = detect_os()
    app_dir = get_app_dir()

    # Step 1: Prerequisites (returns tuple: binary_path, used_venv, venv_path)
    binary_path, used_venv, venv_path = step_prerequisites(os_type, app_dir)

    # Step 2: GPU config
    gpu_info = detect_gpu()
    gpu_config = step_gpu_config(gpu_info)

    # Step 3: Model setup
    model_config, data_dir = step_model_setup(binary_path, gpu_config)

    # Step 4: Install service
    service_config = step_install_service(os_type, app_dir, venv_path)

    # Generate config
    config = generate_config(app_dir, binary_path, gpu_config, model_config, data_dir)

    # Final summary
    print_header("Installation Complete!")

    print_ok("LlamaShift is configured and ready.")
    print()
    print_info("Configuration:  config.json")
    print_info(f"Server URL:     http://localhost:{config['masterPort'] if model_config else 9000} (UI on :8002)")

    if service_config["method"] != "manual":
        print_info(f"Service:        {service_config['method']} — {service_config.get('service', '')}")
        print_info(f"Status:         Run 'sudo systemctl status llamashift' (Linux) or check Services (Windows)")
    else:
        print_info(f"Start:          cd {app_dir} && python3 server.py")

    print()
    print_info("For more options, edit config.json or see setup.md")
    print()
    print_info("💡 You can reconfigure model parameters at any time from the Web UI:")
    print_info("   - Click the gear icon (⚙️) on each model card to adjust context size,")
    print_info("     parallel requests, GPU layers, ports, and device assignments.")
    print_info("   - Toggle between Single-port and Multi-port mode in the header.")
    print()
    print(C("  Thanks for using LlamaShift! 🦙\n", "bold"))

if __name__ == "__main__":
    main()