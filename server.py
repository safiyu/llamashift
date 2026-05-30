#!/usr/bin/env python3
import http.server
import socketserver
import json
import subprocess
import os
import signal
import sys
import platform
import urllib.parse
import re
import time
import threading
import asyncio
import websockets
import uuid
import hashlib
import secrets
from collections import defaultdict

SERVER_PORT_KEY = "serverPort"
CONFIG_FILE = "config.json"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ==================== SECURITY CONFIGURATION ====================
PIN_MAX_ATTEMPTS = 5  # Maximum failed attempts before lockout
PIN_LOCKOUT_DURATION = 300  # Lockout duration in seconds (5 minutes)
PIN_SESSION_TIMEOUT = 1800  # Session timeout in seconds (30 minutes)
RATE_LIMIT_WINDOW = 60  # Rate limit window in seconds (1 minute)
RATE_LIMIT_MAX_REQUESTS = 10  # Maximum requests per window

# ==================== IN-MEMORY SECURITY STATE ====================
# Stores failed attempts: {ip_address: [(timestamp, success), ...]}
_pin_failed_attempts = defaultdict(list)
# Stores lockout times: {ip_address: lockout_until_timestamp}
_pin_lockouts = {}
# Track request timestamps for rate limiting: {ip_address: [timestamps]}
_rate_limit_requests = defaultdict(list)

def get_server_port():
    """Get the server port from config, with default fallback."""
    config = load_config()
    return config.get(SERVER_PORT_KEY, 8002)

# ─── Runtime Environment Detection ───────────────────────────────────────
def detect_environment():
    """Detect the runtime environment: linux-systemd, linux, windows, or docker."""
    system = platform.system().lower()
    
    # Check if running in Docker
    in_docker = False
    if os.path.exists("/.dockerenv"):
        in_docker = True
    else:
        try:
            with open("/proc/1/cgroup", "r") as f:
                if "docker" in f.read() or "kubepods" in f.read():
                    in_docker = True
        except (FileNotFoundError, PermissionError):
            pass
    
    if in_docker:
        return "docker"
    
    # Check if systemd is available (Linux with systemd)
    if system == "linux":
        try:
            result = subprocess.run(["systemctl", "--version"], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return "linux-systemd"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "linux"
    
    if system == "windows":
        return "windows"
    
    return "linux"  # fallback

RUNTIME_ENV = detect_environment()
IS_WINDOWS = RUNTIME_ENV == "windows"
IS_LINUX = RUNTIME_ENV.startswith("linux")
IS_DOCKER = RUNTIME_ENV == "docker"
HAS_SYSTEMD = RUNTIME_ENV == "linux-systemd"

print(f"[env] Runtime: {RUNTIME_ENV} (systemd={HAS_SYSTEMD}, docker={IS_DOCKER}, windows={IS_WINDOWS})")

# Default runtime parameters that can be overridden per-model
DEFAULT_RUNTIME_PARAMS = {
    "ctxSize": 16384,
    "nParallel": 1,
    "nGpuLayers": 99,
    "temperature": 0.7,
    "maxTokens": 4096,
    "topP": 0.9,
    "batchSize": 2048,
    "threads": 0,
}

# MCP Server process tracking
_mcp_process = None
_mcp_lock = threading.Lock()

def load_config():
    """Load the full config from config.json."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                print(f"[config] Loaded {len(cfg.get('models', {}))} models, mode={cfg.get('mode', 'single_port')}")
                return cfg
    except Exception as e:
        print(f"[config] Failed to load config: {e}, using defaults")
    return {"mode": "single_port", "models": {}}

def save_config(cfg):
    """Persist full config back to config.json."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"[config] Failed to save config: {e}")

# Global config state
_CONFIG = load_config()
_config_lock = threading.Lock()

# ─── PIN Security ──────────────────────────────────────────────────────────
# PIN session tracking: token -> {created_at, ip}
_PIN_SESSIONS = {}
_PIN_SESSION_LOCK = threading.Lock()
PIN_SESSION_TIMEOUT = 24 * 60 * 60  # 24 hours in seconds

def _hash_pin(pin, salt):
    """Hash a PIN with salt using SHA-256."""
    return hashlib.sha256(f"{salt}{pin}".encode()).hexdigest()

def _is_rate_limited(client_ip):
    """Check if client is rate limited based on request frequency."""
    current_time = time.time()
    # Clean up old requests outside the window
    _rate_limit_requests[client_ip] = [
        req_time for req_time in _rate_limit_requests[client_ip]
        if current_time - req_time < RATE_LIMIT_WINDOW
    ]
    
    # Check if limit exceeded
    if len(_rate_limit_requests[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    
    # Add current request
    _rate_limit_requests[client_ip].append(current_time)
    return False

def _is_account_locked(client_ip):
    """Check if account is locked due to failed PIN attempts."""
    current_time = time.time()
    # Clean up expired lockouts
    expired_lockouts = []
    for ip, lockout_time in _pin_lockouts.items():
        if current_time > lockout_time:
            expired_lockouts.append(ip)
    
    for ip in expired_lockouts:
        del _pin_lockouts[ip]
        # Also clear failed attempts for this IP
        _pin_failed_attempts[ip] = []
    
    # Check if account is currently locked
    if client_ip in _pin_lockouts:
        if current_time < _pin_lockouts[client_ip]:
            return True  # Still locked
        else:
            # Lockout expired, remove lockout
            del _pin_lockouts[client_ip]
            _pin_failed_attempts[client_ip] = []
            return False
    
    return False

def _record_failed_attempt(client_ip, success=False):
    """Record a failed PIN attempt."""
    current_time = time.time()
    _pin_failed_attempts[client_ip].append((current_time, success))
    
    # Clean up old attempts outside the window
    # Keep only attempts within 24 hours for account lockout logic
    _pin_failed_attempts[client_ip] = [
        (at_time, success) for at_time, success in _pin_failed_attempts[client_ip]
        if current_time - at_time < 86400  # 24 hours
    ]
    
    # Check if we should lock the account
    failed_attempts = [
        (at_time, success) for at_time, success in _pin_failed_attempts[client_ip]
        if not success
    ]
    
    if len(failed_attempts) >= PIN_MAX_ATTEMPTS:
        # Lock account for PIN_LOCKOUT_DURATION seconds
        _pin_lockouts[client_ip] = current_time + PIN_LOCKOUT_DURATION
        
        # Log security event
        _log_security_event("account_locked", {
            "ip": client_ip,
            "attempts": len(failed_attempts),
            "locked_until": _pin_lockouts[client_ip]
        })
        return True
    
    return False

def _log_security_event(event_type, details):
    """Log security events to a dedicated security log file."""
    global _security_log_file
    try:
        with _security_log_lock:
            if _security_log_file is None:
                security_log_path = os.path.join(LOG_DIR, "security.log")
                _security_log_file = open(security_log_path, "a", buffering=1)
            
            log_entry = {
                "timestamp": time.time(),
                "event": event_type,
                "details": details
            }
            
            _security_log_file.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        # Don't let logging errors break the main application
        print(f"[security] Failed to log security event {event_type}: {e}")


# ─── Server Health Check ───────────────────────────────────────────────────
# Set to True after server initialization completes
_SERVER_READY = False

def set_server_ready():
    """Mark the server as fully initialized and ready to accept requests."""
    global _SERVER_READY
    _SERVER_READY = True

def is_server_ready():
    """Check if the server is fully initialized."""
    return _SERVER_READY

# ─── WebSocket Telemetry Broadcaster ─────────────────────────────────────
# Connected WebSocket clients subscribed to telemetry updates
_ws_clients: set = set()
_ws_lock = threading.Lock()

# Background task interval for telemetry broadcast (seconds)
_TELEMETRY_INTERVAL = 2.0

async def _ws_broadcast(data: dict):
    """Broadcast telemetry data to all connected WebSocket clients."""
    if not _ws_clients:
        return
    message = json.dumps(data)
    disconnected = set()
    for client in _ws_clients:
        try:
            await client.send(message)
        except Exception:
            disconnected.add(client)
    _ws_clients -= disconnected

async def _telemetry_broadcast_loop():
    """Periodically broadcast system + GPU telemetry to all WebSocket clients."""
    while True:
        try:
            await asyncio.sleep(_TELEMETRY_INTERVAL)
            telemetry = {
                "type": "telemetry",
                "timestamp": time.time(),
                "host": get_host_stats(),
                "gpu": get_gpu_telemetry(),
                "status": _get_status_snapshot(),
            }
            await _ws_broadcast(telemetry)
        except Exception as e:
            print(f"[ws] Broadcast error: {e}")

def _get_status_snapshot() -> dict:
    """Get a lightweight status snapshot (same logic as handle_api_status but returns dict)."""
    mode = get_global_mode()
    _cleanup_stale_tracked_processes()
    running = get_running_servers()
    host_stats = get_host_stats()

    status_list = []
    for model_id, config in MODELS.items():
        port = config["port"]
        is_running = False
        pid = None

        if mode == "single_port":
            master_port = _CONFIG.get("masterPort", 9000)
            current_pid = running.get(master_port, {}).get("pid")
            
            if current_pid:
                # Get the command line of the process on the master port
                cmdline = get_process_cmdline(current_pid)
                # Check if this process's command line contains this model's filename
                # This ensures we only mark a model as "running" if its filename
                # appears in the running process's command line
                filename = config.get("filename", "")
                if filename and filename in cmdline:
                    is_running = True
                    pid = current_pid
                else:
                    is_running = False
                    pid = None
            else:
                is_running = False
                pid = None
        else:
            if port in running:
                candidate_pid = running[port]["pid"]
                cmdline = get_process_cmdline(candidate_pid)
                is_running = _process_matches_model(cmdline, config, model_id)
                pid = candidate_pid if is_running else None

        stats = {"cpu": 0.0, "mem": 0.0, "uptime": "Unknown"}
        if is_running:
            stats = get_process_stats(pid)

        status_list.append({
            **config,
            "status": "running" if is_running else "stopped",
            "pid": pid,
            "cpu": stats["cpu"],
            "memory": stats["mem"],
            "uptime": stats["uptime"]
        })

    mcp_running = False
    with _mcp_lock:
        if _mcp_process and _mcp_process.poll() is None:
            mcp_running = True

    return {
        "mode": mode,
        "models": status_list,
        "host": host_stats,
        "mcp": {"status": "running" if mcp_running else "stopped"},
    }

def get_global_mode():
    """Return the current mode setting."""
    return _CONFIG.get("mode", "single_port")

# Global log file handles — kept open for the server's lifetime
# keyed by model_id -> file handle
_log_handles = {}
_log_lock = threading.Lock()

# Security logging
_security_log_lock = threading.Lock()
_security_log_file = None

# Global process tracker — keyed by model_id -> Popen proc
_model_processes = {}
_proc_lock = threading.Lock()

# Global process tracking for cleanup
_active_processes = set()
_active_processes_lock = threading.Lock()

# Log directory — derived from config appName (set after load)
LOG_DIR = None

def _ensure_log_dir():
    """Ensure LOG_DIR is initialized from config appName."""
    global LOG_DIR
    if LOG_DIR is None:
        app_name = _CONFIG.get("appName", "llamashift")
        LOG_DIR = os.path.join(os.path.expanduser("~/logs"), str(app_name))
    os.makedirs(LOG_DIR, exist_ok=True)

# Call it once at startup
_ensure_log_dir()

# Models loaded from config.json (keyed by model_id)
_models_cache = None
_models_lock = threading.Lock()

def get_models():
    """Return the models dict from config, with logPath derived at runtime."""
    global _models_cache
    with _models_lock:
        if _models_cache is None:
            raw = _CONFIG.get("models", {})
            cache = {}
            for mid, cfg in raw.items():
                c = dict(cfg)  # shallow copy
                c["logPath"] = os.path.join(LOG_DIR, f"{mid}.log")
                cache[mid] = c
            _models_cache = cache
    return _models_cache

# Lightweight wrapper so existing code using MODELS.items(), MODELS[key],
# model_id in MODELS etc. continues to work while always reading fresh data.
class _ModelsProxy:
    """Dict-like proxy that calls get_models() on every access."""
    def __getitem__(self, key):
        return get_models()[key]
    def __contains__(self, key):
        return key in get_models()
    def items(self):
        return get_models().items()
    def keys(self):
        return get_models().keys()

MODELS = _ModelsProxy()


def reload_config():
    """Reload config from file, invalidate cache, and re-init LOG_DIR."""
    global _CONFIG, _models_cache, LOG_DIR
    with _models_lock:
        _CONFIG = load_config()
        _models_cache = None
        LOG_DIR = None  # force re-init from new appName
    _ensure_log_dir()


def save_mode(mode):
    """Update only the mode field and persist."""
    _CONFIG["mode"] = mode
    save_config(_CONFIG)
    with _models_lock:
        _models_cache = None  # invalidate cache


# ─── Cross-Platform Process Detection ──────────────────────────────────────

def get_running_servers():
    """Scans running llama-server processes on the system and maps them by port."""
    running = {}
    
    if IS_WINDOWS:
        # Windows: use tasklist + wmic or powershell
        try:
            res = subprocess.run(
                ["powershell", "-Command", 
                 "Get-Process | Where-Object { $_.ProcessName -like '*llama*' } | "
                 "Select-Object Id, Id, CommandLine | Format-List"],
                capture_output=True, text=True, timeout=10
            )
            # Fallback: simpler approach
            if res.returncode != 0:
                res = subprocess.run(
                    ["wmic", "process", "where", "name='llama-server.exe' OR name='llama-server'", 
                     "get", "ProcessId,CommandLine"],
                    capture_output=True, text=True, timeout=10
                )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines()[1:]:  # skip header
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    if len(parts) >= 2:
                        pid_str, cmd = parts
                        if "llama-server" in cmd:
                            port_match = re.search(r'(?:--port|-p)\s+(\d+)', cmd)
                            if port_match:
                                try:
                                    running[int(port_match.group(1))] = {
                                        "pid": int(pid_str), "cmd": cmd
                                    }
                                except ValueError:
                                    pass
        except Exception as e:
            print(f"[process] Windows process scan error: {e}")
    else:
        # Linux (including Docker): use ps
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,args"], 
                capture_output=True, text=True, check=True
            )
            lines = res.stdout.splitlines()
        except Exception as e:
            print(f"[process] Linux process scan error: {e}")
            return running

        for line in lines[1:]:  # skip header
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid_str, cmd = parts
            
            if "llama-server" in cmd:
                port_match = re.search(r'(?:--port|-p)\s+(\d+)', cmd)
                if port_match:
                    try:
                        pid = int(pid_str)
                        running[int(port_match.group(1))] = {"pid": pid, "cmd": cmd}
                    except ValueError:
                        pass
    return running


# ─── Cross-Platform Process Stats ──────────────────────────────────────────

def get_process_stats(pid):
    """Retrieves CPU %, MEM %, and Uptime for a given PID."""
    try:
        if IS_WINDOWS:
            res = subprocess.run(
                ["powershell", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
                 "Select-Object @{N='CPU';E={[math]::Round($_.CPU,1)}}, "
                 "@{N='WS';E={[math]::Round($_.WorkingSet/1MB,1)}}, "
                 "@{N='Elapsed';E={$_.StartTime - (Get-Date)}}"],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                # Simplified Windows stats
                return {"cpu": 0.0, "mem": 0.0, "uptime": "Running"}
        else:
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "%cpu,%mem,etime", "--no-headers"],
                capture_output=True, text=True
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = res.stdout.strip().split()
                if len(parts) >= 3:
                    return {
                        "cpu": float(parts[0]),
                        "mem": float(parts[1]),
                        "uptime": parts[2]
                    }
    except Exception:
        pass
    return {"cpu": 0.0, "mem": 0.0, "uptime": "Unknown"}


def get_host_stats():
    """Reads system stats - cross-platform compatible."""
    cpu_load = 0.0
    mem_used = 0
    mem_total = 0
    mem_pct = 0.0
    cpu_count = 0
    load_avg_1m = 0.0
    load_avg_5m = 0.0
    load_avg_15m = 0.0
    cpu_temp = 0.0

    if IS_LINUX:
        # CPU load and load averages from /proc/loadavg
        # Format: load_avg_1m load_avg_5m load_avg_15m running_threads/total_threads pid
        try:
            with open("/proc/loadavg", "r") as f:
                loadavg_data = f.read().split()
                if len(loadavg_data) >= 4:
                    load_avg_1m = float(loadavg_data[0])
                    load_avg_5m = float(loadavg_data[1])
                    load_avg_15m = float(loadavg_data[2])
                    cpu_load = load_avg_1m
        except Exception:
            pass

        # CPU count from /proc/cpuinfo
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo_data = f.read()
                # Count processor entries
                cpu_count = cpuinfo_data.count("processor\t")
                if cpu_count == 0:
                    # Fallback: try nproc
                    nproc_res = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
                    if nproc_res.returncode == 0:
                        cpu_count = int(nproc_res.stdout.strip())
        except Exception:
            try:
                # Fallback: try nproc
                nproc_res = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
                if nproc_res.returncode == 0:
                    cpu_count = int(nproc_res.stdout.strip())
            except Exception:
                cpu_count = 0

        # CPU temperature from hwmon sensors
        try:
            # Method 1: Check /sys/class/hwmon for temperature sensors
            hwmon_base = "/sys/class/hwmon"
            if os.path.isdir(hwmon_base):
                for hwmon_dir in os.listdir(hwmon_base):
                    hwmon_path = os.path.join(hwmon_base, hwmon_dir)
                    # Read name for identification
                    name_file = os.path.join(hwmon_path, "name")
                    temp_input_files = []
                    try:
                        if os.path.isdir(hwmon_path):
                            for f_name in os.listdir(hwmon_path):
                                # Match temp*_input files (e.g., temp1_input, temp16_input)
                                if f_name.startswith("temp") and f_name.endswith("_input"):
                                    temp_input_files.append(f_name)
                            # Also check for name file to identify CPU sensor
                            if os.path.exists(name_file):
                                with open(name_file, "r") as nf:
                                    hwmon_name = nf.read().strip()
                    except (IOError, OSError):
                        continue

                    # Read temperature values
                    for temp_file in sorted(temp_input_files):
                        temp_path = os.path.join(hwmon_path, temp_file)
                        try:
                            with open(temp_path, "r") as tf:
                                temp_milli_c = int(tf.read().strip())
                                temp_c = temp_milli_c / 1000.0
                                if 20.0 <= temp_c <= 100.0:  # Valid CPU temp range
                                    cpu_temp = max(cpu_temp, temp_c)  # Take highest valid reading
                        except (IOError, OSError, ValueError):
                            continue

            # Method 2: Check /sys/class/thermal for thermal zones
            if cpu_temp == 0:
                thermal_base = "/sys/class/thermal"
                if os.path.isdir(thermal_base):
                    for thermal_zone in os.listdir(thermal_base):
                        if thermal_zone.startswith("thermal_zone"):
                            temp_file = os.path.join(thermal_base, thermal_zone, "temp")
                            try:
                                if os.path.exists(temp_file):
                                    with open(temp_file, "r") as tf:
                                        temp_milli_c = int(tf.read().strip())
                                        temp_c = temp_milli_c / 1000.0
                                        if 20.0 <= temp_c <= 100.0:
                                            cpu_temp = max(cpu_temp, temp_c)
                            except (IOError, OSError, ValueError):
                                continue
            # Method 3: Check /proc/acpi/thermal_zone (older systems)
            if cpu_temp == 0:
                acpi_thermal = "/proc/acpi/thermal_zone"
                if os.path.isdir(acpi_thermal):
                    for tz in os.listdir(acpi_thermal):
                        temp_file = os.path.join(acpi_thermal, tz, "temperature")
                        try:
                            if os.path.exists(temp_file):
                                with open(temp_file, "r") as tf:
                                    for line in tf:
                                        if "temperature" in line.lower():
                                            parts = line.split(":")
                                            if len(parts) >= 2:
                                                temp_c = float(parts[1].strip().replace("C", "").strip())
                                                if 20.0 <= temp_c <= 100.0:
                                                    cpu_temp = max(cpu_temp, temp_c)
                                        break
                        except (IOError, OSError, ValueError):
                            continue
        except Exception:
            pass

        try:
            with open("/proc/meminfo", "r") as f:
                mem_info = {}
                mem_cached_kb = 0
                mem_swap_total_kb = 0
                mem_swap_used_kb = 0
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = int(parts[1].split()[0])  # in kB
                        mem_info[key] = val
                        # Track cached memory
                        if key in ("MemFree", "Buffers", "Cached", "SReclaimable"):
                            mem_cached_kb += val
                        # Track swap
                        if key == "SwapTotal":
                            mem_swap_total_kb = val
                        if key == "SwapFree":
                            mem_swap_used_kb = mem_swap_total_kb - val
                mem_total = mem_info.get("MemTotal", 0) // 1024  # MiB
                mem_free = mem_info.get("MemFree", 0) // 1024
                mem_avail = mem_info.get("MemAvailable", mem_free) // 1024
                mem_used = mem_total - mem_avail
                mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0.0
                # Store additional memory stats for frontend
                host_mem_cached = mem_cached_kb // 1024  # MiB
                host_swap_used = mem_swap_used_kb // 1024  # MiB
        except Exception:
            pass
    elif IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            c_ulong = ctypes.c_ulong
            
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", c_ulong),
                    ("dwMemoryLoad", c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            mem_total = stat.ullTotalPhys // (1024 * 1024)
            mem_avail = stat.ullAvailPhys // (1024 * 1024)
            mem_used = mem_total - mem_avail
            mem_pct = stat.dwMemoryLoad
            
            # CPU load from WMI via powershell
            cpu_res = subprocess.run(
                ["powershell", "-Command",
                 "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"],
                capture_output=True, text=True, timeout=10
            )
            if cpu_res.returncode == 0:
                cpu_load = float(cpu_res.stdout.strip()) / 100.0
        except Exception:
            pass
    else:
        # Docker fallback: try /proc, if not available use psutil-like approach
        try:
            with open("/proc/loadavg", "r") as f:
                cpu_load = float(f.read().split()[0])
        except Exception:
            cpu_load = 0.0
        try:
            with open("/proc/meminfo", "r") as f:
                mem_info = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem_info[parts[0].strip()] = int(parts[1].split()[0])
                mem_total = mem_info.get("MemTotal", 0) // 1024
                mem_avail = mem_info.get("MemAvailable", mem_info.get("MemFree", 0)) // 1024
                mem_used = mem_total - mem_avail
                mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0.0
        except Exception:
            pass

    # Format cpu_count for display (e.g., 8 → "8 Cores" or "8P+8E" for hybrid)
    cpu_count_str = f"{cpu_count} Cores" if cpu_count > 0 else "--"
    
    # Format load average string for frontend
    load_avg_str = ""
    if load_avg_1m > 0:
        load_avg_str = f"{load_avg_1m:.2f}"
        if cpu_count > 0:
            load_avg_str += f" / {cpu_count}"

    return {
        "cpu_load": round(cpu_load, 2),
        "cpu_count": cpu_count,
        "cpu_count_str": cpu_count_str,
        "load_avg_1m": round(load_avg_1m, 2),
        "load_avg_5m": round(load_avg_5m, 2),
        "load_avg_15m": round(load_avg_15m, 2),
        "cpu_temp": round(cpu_temp, 1),
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_pct": round(mem_pct, 1),
        "mem_cached": host_mem_cached if 'host_mem_cached' in dir() else 0,
        "swap_used": host_swap_used if 'host_swap_used' in dir() else 0,
    }


def get_gpu_telemetry():
    """Gathers real-time GPU statistics for NVIDIA and AMD."""
    nvidia_data = None
    amd_data = None

    # 1. NVIDIA Telemetry (works on Linux, WSL2, and Docker with GPU passthrough)
    try:
        res = subprocess.run([
            "nvidia-smi",
            "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
            "--format=csv,noheader,nounits"
        ], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.splitlines()[0].split(",")]
            if len(parts) >= 6:
                power_str = parts[5]
                power_val = 0.0
                if not power_str.startswith("[") and "N/A" not in power_str:
                    try:
                        power_val = float(power_str)
                    except ValueError:
                        pass
                
                nvidia_data = {
                    "name": parts[0],
                    "temp": float(parts[1]),
                    "util": float(parts[2]),
                    "mem_used": float(parts[3]),
                    "mem_total": float(parts[4]),
                    "power": power_val
                }
    except FileNotFoundError:
        print("[gpu] nvidia-smi not found (NVIDIA GPU may not be available)")
    except subprocess.TimeoutExpired:
        print("[gpu] nvidia-smi timed out")
    except Exception as e:
        print(f"[gpu] Error querying NVIDIA GPU: {e}")

    # 2. AMD Telemetry (Linux: rocm-smi | Windows: PowerShell WMI/CIM)
    if IS_WINDOWS:
        # Windows: query AMD GPU via PowerShell WMI
        try:
            ps_cmd = (
                "Get-CimInstance Win32_VideoController | "
                "Where-Object { $_.Name -match 'AMD|RADEON|Radeon|Graphics' -and "
                "$_.Name -notmatch 'NVIDIA|Intel' } | "
                "Select-Object -First 1 -Property Name, AdapterRAM | "
                "ConvertTo-Json -Compress"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                gpu_info = json.loads(res.stdout.strip())
                if gpu_info:
                    gpu_name = gpu_info.get("Name", "AMD Radeon GPU")
                    adapter_ram = gpu_info.get("AdapterRAM", 0)
                    amd_data = {
                        "name": gpu_name,
                        "temp": 0.0,
                        "util": 0.0,
                        "mem_used": 0.0,
                        "mem_total": adapter_ram / (1024 * 1024) if adapter_ram else 0.0,
                        "power": 0.0,
                    }
                    print(f"[gpu] Windows AMD GPU detected: {gpu_name}")
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"[gpu] Windows AMD GPU query failed: {e}")
    else:
        # Linux: use rocm-smi as primary method
        def _rocm_smi_detect():
            """Try rocm-smi for AMD GPU telemetry. Returns dict or None."""
            try:
                res = subprocess.run([
                    "rocm-smi",
                    "--showtemp",
                    "--showuse",
                    "--showpower",
                    "--showmeminfo", "vram",
                    "--json"
                ], capture_output=True, text=True, timeout=10)
                if res.returncode != 0 or not res.stdout.strip():
                    print(f"[gpu] rocm-smi returned code {res.returncode}, trying fallbacks")
                    return None
                # rocm-smi may emit WARNING lines to stdout before the JSON; find the first line that starts with '{'
                json_line = None
                for line in res.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("{"):
                        json_line = stripped
                        break
                if json_line is None:
                    print(f"[gpu] rocm-smi returned no JSON: {res.stdout[:200]}")
                    return None
                raw = json.loads(json_line)
                card = raw.get("card0", {})

                def safe_float(val, default=0.0):
                    try:
                        return float(str(val).replace("\u00b0C", "").replace("W", "").strip())
                    except (ValueError, TypeError):
                        return default

                def safe_int(val, default=0):
                    try:
                        return int(str(val).strip())
                    except (ValueError, TypeError):
                        return default

                def get_field(obj, *keys):
                    for k in keys:
                        v = obj.get(k)
                        if v is not None and str(v).lower() != 'n/a':
                            return v
                    return None

                temp_raw = get_field(card,
                    "Temperature (Sensor edge) (C)",
                    "Temperature (Tedge) (C)", "Temperature")
                util_raw = get_field(card,
                    "GPU use (%)", "GPU Utilization (%)",
                    "Average GPU Activity (%)", "Activity (%)")
                vram_used_raw = get_field(card,
                    "VRAM Total Used Memory (B)",
                    "VRAM Used (B)", "VRAM Total Used Memory")
                vram_total_raw = get_field(card,
                    "VRAM Total Memory (B)",
                    "VRAM Total (B)", "VRAM Total Memory")
                power_raw = get_field(card,
                    "Average Graphics Package Power (W)",
                    "Package Power (W)", "Average Package Power")

                # Get GPU product name from rocm-smi
                gpu_name = "AMD Radeon GPU"
                try:
                    name_res = subprocess.run([
                        "rocm-smi", "--showproductname", "--json"
                    ], capture_output=True, text=True, timeout=5)
                    if name_res.returncode == 0 and name_res.stdout.strip():
                        for nline in name_res.stdout.splitlines():
                            nstripped = nline.strip()
                            if nstripped.startswith("{"):
                                name_raw = json.loads(nstripped)
                                name_card = name_raw.get("card0", {})
                                gpu_name = name_card.get("Card Series", "AMD Radeon GPU")
                                break
                except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
                    pass  # keep default name

                return {
                    "name": gpu_name,
                    "temp": safe_float(temp_raw),
                    "util": safe_float(util_raw),
                    "mem_used": safe_int(vram_used_raw) / (1024 * 1024),
                    "mem_total": safe_int(vram_total_raw) / (1024 * 1024),
                    "power": safe_float(power_raw)
                }
            except FileNotFoundError:
                print("[gpu] rocm-smi not found, trying fallback detection")
                return None
            except subprocess.TimeoutExpired:
                print("[gpu] rocm-smi timed out, trying fallback detection")
                return None
            except Exception as e:
                print(f"[gpu] rocm-smi error: {e}, trying fallback detection")
                return None

        def _lspci_amd_detect():
            """Fallback: detect AMD GPU via lspci. Returns basic info dict or None."""
            try:
                res = subprocess.run(
                    ["lspci", "-nnv"],
                    capture_output=True, text=True, timeout=10
                )
                if res.returncode != 0:
                    return None
                # Look for AMD/ATI VGA controller
                lines = res.stdout.splitlines()
                for i, line in enumerate(lines):
                    if 'VGA compatible controller' in line and (
                        'AMD' in line.upper() or 'ATI' in line.upper()
                        or 'Advanced Micro Devices' in line
                    ):
                        # Extract GPU name (between colon and revision/PCI info)
                        gpu_name = "AMD Radeon GPU"
                        if ':' in line:
                            name_part = line.split(':', 1)[1].strip()
                            # Remove PCI class codes like [0300]
                            name_part = re.sub(r'\s*\[.*?\]\s*', ' ', name_part).strip()
                            if name_part:
                                gpu_name = name_part
                        # Try to read VRAM from /sys for this device
                        # Extract PCI address like 2d:00.0
                        pci_addr = line.split()[0] if line.split() else None
                        mem_total = 0.0
                        mem_used = 0.0
                        if pci_addr:
                            mem_total = _read_amd_vram_from_sysfs(pci_addr)
                        print(f"[gpu] AMD GPU detected via lspci: {gpu_name}")
                        return {
                            "name": gpu_name,
                            "temp": 0.0,
                            "util": 0.0,
                            "mem_used": mem_used,
                            "mem_total": mem_total,
                            "power": 0.0,
                        }
                return None
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                print(f"[gpu] lspci fallback failed: {e}")
                return None

        def _sysfs_amd_detect():
            """Fallback: detect AMD GPU via /sys/class/drm. Returns basic info dict or None."""
            try:
                import glob
                # Look for amdgpu render devices
                render_dirs = glob.glob("/sys/class/drm/renderD128*")
                card_dirs = glob.glob("/sys/class/drm/card*")
                for card_dir in sorted(card_dirs):
                    device_link = os.path.join(card_dir, "device")
                    if not os.path.isdir(device_link):
                        continue
                    # Check if this is an amdgpu device
                    driver_link = os.path.join(device_link, "driver")
                    if os.path.islink(driver_link):
                        driver_name = os.path.basename(os.path.realpath(driver_link))
                        if 'amdgpu' in driver_name.lower():
                            gpu_name = "AMD Radeon GPU"
                            # Try to read device name from uevent
                            uevent = os.path.join(card_dir, "device", "uevent")
                            if os.path.exists(uevent):
                                try:
                                    with open(uevent, 'r') as f:
                                        for uline in f:
                                            if uline.startswith("NAME="):
                                                gpu_name = uline.split("=", 1)[1].strip()
                                                break
                                except (IOError, OSError):
                                    pass
                            # Try to read VRAM
                            mem_total = _read_amd_vram_from_sysfs(os.path.basename(card_dir))
                            print(f"[gpu] AMD GPU detected via sysfs: {gpu_name}")
                            return {
                                "name": gpu_name,
                                "temp": 0.0,
                                "util": 0.0,
                                "mem_used": 0.0,
                                "mem_total": mem_total,
                                "power": 0.0,
                            }
                return None
            except Exception as e:
                print(f"[gpu] sysfs fallback failed: {e}")
                return None

        def _read_amd_vram_from_sysfs(card_or_pci):
            """Try to read AMD GPU VRAM from sysfs. Returns MiB float."""
            try:
                import glob
                # Try memory info via hwmon or direct sysfs
                # Method 1: /sys/class/drm/cardX/device/hwmon*/mem*_bytes
                paths = glob.glob(f"/sys/class/drm/{card_or_pci}*/device/hwmon*/mem*_bytes")
                for p in paths:
                    if os.path.exists(p):
                        with open(p, 'r') as f:
                            return int(f.read().strip()) / (1024 * 1024)
                # Method 2: /sys/class/drm/cardX/device/mem_info_vram_total
                mem_path = f"/sys/class/drm/{card_or_pci}*/device/mem_info_vram_total"
                for p in glob.glob(mem_path):
                    if os.path.exists(p):
                        with open(p, 'r') as f:
                            return int(f.read().strip()) / (1024 * 1024)
                # Method 3: read from /sys/class/drm/cardX/device/resource0_size
                for p in glob.glob(f"/sys/class/drm/{card_or_pci}*/device/resource0_size"):
                    if os.path.exists(p):
                        with open(p, 'r') as f:
                            return int(f.read().strip()) / (1024 * 1024)
            except Exception:
                pass
            return 0.0

        # Primary: try rocm-smi first
        amd_data = _rocm_smi_detect()
        # Fallback chain: lspci -> sysfs
        if amd_data is None:
            amd_data = _lspci_amd_detect()
        if amd_data is None:
            amd_data = _sysfs_amd_detect()

    return {
        "nvidia": nvidia_data,
        "amd": amd_data
    }

def get_process_cmdline(pid):
    """Get the command line arguments of a process as a string.
    Returns empty string on failure."""
    try:
        if IS_LINUX:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            if not raw:
                return ""
            # Null-separated args; decode and join with spaces
            cmdline = raw.decode("utf-8", errors="replace").replace("\x00", " ").strip()
            return cmdline
        elif IS_WINDOWS:
            res = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/format:list"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "CommandLine=" in line:
                        return line.split("=", 1)[1].strip('" ')
            return ""
        return ""
    except Exception:
        return ""


def _process_matches_model(cmdline, model_cfg, model_id):
    """Check if a process's command line matches this model.
    
    We verify:
    1. The command line contains 'llama-server'
    2. The command line contains the model's EXACT filename (NOT just the data dir)
    3. The command line contains this model's port
    
    This prevents models sharing the same port or GPU from incorrectly
    showing as running when only one is actually active.
    
    CRITICAL: We require EXACT filename match, not just data directory match,
    because multiple models in the same data directory would otherwise all match.
    """
    if not cmdline:
        return False
    
    cmdline_lower = cmdline.lower()
    
    # Must contain llama-server
    if "llama-server" not in cmdline_lower:
        return False
    
    # Must contain this model's port
    port_str = f"--port {model_cfg.get('port', '')}"
    port_match = port_str in cmdline or f"--port={model_cfg.get('port', '')}" in cmdline
    
    # Must contain this model's EXACT filename - this is the key discriminator
    # when multiple models share the same port or data directory
    filename = model_cfg.get("filename", "")
    if not filename:
        return False
    
    filename_lower = filename.lower()
    
    # Check for the exact filename in the command line.
    # The --model argument will contain the full path to the model file.
    # We check if the filename appears as a path component (after / or \).
    # This prevents partial matches like "Qwen" matching "Qwen3.6".
    filename_match = False
    if filename_lower in cmdline_lower:
        # Verify it's an actual file match, not a substring match
        # Check if it appears after a / or \ (i.e., as a path component)
        idx = cmdline_lower.find(filename_lower)
        if idx >= 0:
            # Check character before the filename match
            before = cmdline_lower[idx-1] if idx > 0 else ''
            # It should be preceded by / or \ (path separator) or be at a --model argument
            if before in ('/', '\\'):
                filename_match = True
            else:
                # Check if it's preceded by --model / --model=
                context_before = cmdline_lower[max(0, idx-20):idx]
                if '--model' in context_before:
                    filename_match = True
    
    return port_match and filename_match


def _cleanup_stale_tracked_processes():
    """Remove tracked model processes that are dead OR no longer match their model config."""
    stale_models = []
    with _proc_lock:
        for mid, proc in list(_model_processes.items()):
            dead = False
            # Check 1: process is dead
            if proc.poll() is not None:
                dead = True
            # Check 2: not a llama-server process anymore
            elif not is_llama_server(proc.pid):
                dead = True
            # Check 3: process cmdline no longer matches model config (model was reconfigured)
            else:
                cmdline = get_process_cmdline(proc.pid)
                if mid in MODELS:
                    if not _process_matches_model(cmdline, MODELS[mid], mid):
                        dead = True

            if dead:
                stale_models.append(mid)
                _model_processes.pop(mid, None)
                with _active_processes_lock:
                    _active_processes.discard(mid)

    if stale_models:
        with _log_lock:
            for mid in stale_models:
                handle = _log_handles.pop(mid, None)
                if handle:
                    try:
                        handle.close()
                    except OSError:
                        pass


def _human_size(bytes_val):
    """Format bytes into human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"


def read_last_lines(filepath, n=100):
    """Reads the last n lines of a file safely."""
    if not os.path.exists(filepath):
        return "Server logs will populate here once the model starts..."
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return "".join(lines[-n:])
    except Exception as e:
        return f"Error reading log file: {str(e)}"

def is_llama_server(pid):
    """Check if a PID is actually a running llama-server process."""
    try:
        if IS_LINUX:
            with open(f"/proc/{pid}/cmdline", "r") as f:
                cmdline = f.read()
            return "llama-server" in cmdline
        elif IS_WINDOWS:
            res = subprocess.run(
                ["powershell", "-Command",
                 f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).MainModule.FileName"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                return "llama-server" in res.stdout.lower()
            return False
        else:
            # Fallback: check via ps
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True
            )
            return res.returncode == 0 and "llama-server" in res.stdout
    except (FileNotFoundError, PermissionError, ProcessLookupError, subprocess.TimeoutExpired):
        return False

def get_child_processes(pid):
    """Get a list of child process PIDs for a given parent PID."""
    child_pids = []
    try:
        if IS_WINDOWS:
            # Windows: use PowerShell to get child processes
            try:
                cmd = f'(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Children | Select-Object -ExpandProperty Id'
                res = subprocess.run(["powershell", "-Command", cmd], 
                                    capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.strip().split('\n'):
                        try:
                            child_pids.append(int(line.strip()))
                        except ValueError:
                            pass
            except Exception:
                pass
        else:
            # Linux/Unix: use ps to get child processes
            try:
                res = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid=", "--no-headers"],
                                    capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.strip().split('\n'):
                        try:
                            child_pids.append(int(line.strip()))
                        except ValueError:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    return child_pids


def kill_process(pid):
    """Gracefully terminates a process by PID, including all child processes.
    Returns True if process is gone, False if still alive."""
    pid = int(pid)
    if not is_llama_server(pid):
        print(f"[!] PID {pid} is not a llama-server, skipping")
        return True

    # First, get and kill any child processes
    child_pids = get_child_processes(pid)
    if child_pids:
        print(f"[shutdown] Found {len(child_pids)} child process(es) of PID {pid}, terminating...")
        for child_pid in child_pids:
            if is_llama_server(child_pid):
                print(f"[shutdown] Terminating child process PID {child_pid}")
                try:
                    if IS_WINDOWS:
                        subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], 
                                      capture_output=True, timeout=10)
                    else:
                        os.kill(child_pid, signal.SIGTERM)
                except Exception:
                    pass
        # Wait briefly for children to terminate
        time.sleep(0.2)
    
    try:
        if IS_WINDOWS:
            # Windows: use taskkill
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], 
                          capture_output=True, timeout=10)
            time.sleep(0.5)
            # Check if still alive
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        else:
            # Linux/Unix: send SIGTERM to process group (includes children)
            # Using negative PID to signal the entire process group
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                # Process group doesn't exist, try killing the process directly
                os.kill(pid, signal.SIGTERM)
            
            for _ in range(30):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
            
            # Force kill if still running
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        return False


def start_mcp_server():
    """Start the MCP server process."""
    global _mcp_process
    
    with _mcp_lock:
        if _mcp_process and _mcp_process.poll() is None:
            print("[mcp] MCP server already running")
            return True
    
    try:
        mcp_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
        _mcp_process = subprocess.Popen(
            [sys.executable, mcp_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
        print(f"[mcp] Started MCP server with PID {_mcp_process.pid}")
        return True
    except Exception as e:
        print(f"[mcp] Failed to start MCP server: {e}")
        return False

def stop_mcp_server():
    """Stop the MCP server process."""
    global _mcp_process
    
    with _mcp_lock:
        if _mcp_process:
            try:
                _mcp_process.terminate()
                try:
                    _mcp_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _mcp_process.kill()
                    _mcp_process.wait()
                print("[mcp] MCP server stopped")
            except Exception as e:
                print(f"[mcp] Error stopping MCP server: {e}")
            finally:
                _mcp_process = None


# ─── WebSocket Handler ─────────────────────────────────────────────────────
class WSHandler(websockets.WebSocketServer):
    """Simple WebSocket server for telemetry broadcasting."""
    async def process(self, ws, request):
        # Check for upgrade header
        upgrade = request.headers.get("upgrade", "").lower()
        return upgrade == "websocket"
    
    async def handler(self, ws, path):
        """Handle a WebSocket connection."""
        _ws_clients.add(ws)
        try:
            async for message in ws:
                # Handle incoming messages (ping/pong)
                try:
                    data = json.loads(message)
                    if data.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                except (json.JSONDecodeError, Exception):
                    pass
        finally:
            _ws_clients.discard(ws)


class SwitcherAPIHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        # Add security headers
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-XSS-Protection', '1; mode=block')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
        
        origin = self.headers.get('Origin', '')
        allowed_origins = ['http://localhost:9007', 'http://127.0.0.1:9007']
        if origin in allowed_origins:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, PUT, DELETE, PATCH')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Pin-Session')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/api/health":
            self.handle_api_health()
        elif path == "/api/status":
            self.handle_api_status()
        elif path == "/api/gpu":
            self.handle_api_gpu()
        elif path == "/api/config":
            self.handle_api_config_get()
        elif path == "/api/logs":
            self.handle_api_logs(query)
        elif path == "/api/models/export":
            self.handle_api_models_export()
        elif path == "/api/models/import":
            self.handle_api_models_import_post()
        elif path == "/api/models":
            self.handle_api_models()
        elif path == "/api/pin" or path == "/api/pin/status":
            self.handle_api_pin()
        elif path == "/api/mcp":
            self.handle_api_mcp_status()
        elif path == "/api/files":
            self.handle_api_files()
        else:
            self.serve_static(path)

    def do_PUT(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        # Handle /api/models/{modelId}
        if path.startswith("/api/models/"):
            model_id = path[len("/api/models/"):]
            if model_id:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                self.handle_api_model_put(model_id, data)
            else:
                self.send_error_response(400, "Missing model id")
        else:
            self.send_error_response(404, "Endpoint not found")

    def do_PATCH(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/models":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            self.handle_api_model_patch(data)
        else:
            self.send_error_response(404, "Endpoint not found")

    def do_DELETE(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/models":
            query = urllib.parse.parse_qs(parsed_url.query)
            model_id = query.get("model", [None])[0]
            self.handle_api_model_delete(model_id)
        else:
            self.send_error_response(404, "Endpoint not found")

    def handle_api_model_put(self, model_id, data):
        """PUT /api/models/{modelId} - update model configuration (full config payload)."""
        from config import MODELS, _CONFIG, save_config, _models_lock, _models_cache, get_global_mode
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock
        )
        
        if model_id not in MODELS:
            self.send_error_response(400, f"Invalid or missing model id: {model_id}")
            return
        
        updates = {}
        allowed = {
            "name", "desc", "filename", "port", "ctxSize", "nParallel",
            "nGpuLayers", "batchSize", "threads", "temperature",
            "maxTokens", "topP", "devices", "extraArgs", "mmproj",
        }
        
        for key, value in data.items():
            if key in allowed and key != "id":
                updates[key] = value
        
        if not updates:
            self.send_error_response(400, "No valid fields to update")
            return
        
        # Validate port uniqueness if port is being changed
        if "port" in updates:
            new_port = int(updates["port"])
            for existing_id, existing_cfg in MODELS.items():
                if existing_id != model_id and existing_cfg.get("port") == new_port:
                    self.send_error_response(409, f"Port {new_port} is already assigned to model '{existing_id}'")
                    return
        
        runtime_params = {"ctxSize", "nParallel", "nGpuLayers", "batchSize", "threads",
                          "extraArgs", "devices", "mmproj", "port", "filename"}
        requires_restart = bool(runtime_params & set(updates.keys()))
        
        was_running = False
        if requires_restart:
            running = get_running_servers()
            mode = get_global_mode()
            old_model_cfg = MODELS[model_id]
            old_port = old_model_cfg.get("port")
            
            if mode == "multi_port" and old_port and old_port in running:
                was_running = True
            elif mode == "single_port":
                from process import _cleanup_stale_tracked_processes, is_llama_server
                _cleanup_stale_tracked_processes()
                with _proc_lock:
                    for mid, proc in list(_model_processes.items()):
                        if mid == model_id and is_llama_server(proc.pid):
                            was_running = True
                            break
        
        with _models_lock:
            _CONFIG["models"][model_id].update(updates)
            _models_cache = None
        save_config(_CONFIG)
        
        response_data = {
            "success": True,
            "model": model_id,
            "updated": updates,
            "config": dict(_CONFIG["models"][model_id]),
        }
        
        if was_running and requires_restart:
            def restart_model_after_put():
                time.sleep(0.5)
                try:
                    running = get_running_servers()
                    mode = get_global_mode()
                    new_model_cfg = MODELS[model_id]
                    new_port = new_model_cfg.get("port")
                    
                    if mode == "multi_port" and new_port and new_port in running:
                        pid = running[new_port]["pid"]
                        kill_process(pid)
                        with _log_lock:
                            handle = _log_handles.pop(model_id, None)
                            if handle:
                                try:
                                    handle.close()
                                except OSError:
                                    pass
                        with _proc_lock:
                            _model_processes.pop(model_id, None)
                            with _active_processes_lock:
                                _active_processes.discard(model_id)
                    elif mode == "single_port":
                        from process import _cleanup_stale_tracked_processes, is_llama_server
                        _cleanup_stale_tracked_processes()
                        master_port = _CONFIG.get("masterPort", 9000)
                        if master_port in running:
                            pid = running[master_port]["pid"]
                            kill_process(pid)
                            with _proc_lock:
                                for mid2, proc in list(_model_processes.items()):
                                    try:
                                        if proc.pid == pid:
                                            _model_processes.pop(mid2, None)
                                            with _active_processes_lock:
                                                _active_processes.discard(mid2)
                                            break
                                    except:
                                        pass
                                with _log_lock:
                                    handle = _log_handles.pop(model_id, None)
                                    if handle:
                                        try:
                                            handle.close()
                                        except OSError:
                                            pass
                    
                    port = new_port if mode == "multi_port" else _CONFIG.get("masterPort", 9000)
                    
                    import socket
                    for _wait_attempt in range(20):
                        time.sleep(0.5)
                        _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        try:
                            _sock.settimeout(1)
                            _result = _sock.connect_ex(('127.0.0.1', int(port)))
                            if _result != 0:
                                break
                        finally:
                            _sock.close()
                    
                    binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
                    data_dir = _CONFIG.get("dataDir", "~/models")
                    model_path = os.path.join(os.path.expanduser(data_dir), new_model_cfg["filename"])
                    
                    n_parallel = new_model_cfg.get("nParallel", 1)
                    n_gpu_layers = new_model_cfg.get("nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])
                    devices_list = new_model_cfg.get("devices", ["ROCm0"])
                    device_str = ",".join(devices_list)
                    
                    cmd_args = [
                        binary_path,
                        "--model", model_path,
                        "--device", device_str,
                        "-ngl", str(n_gpu_layers),
                        "--ctx-size", str(new_model_cfg["ctxSize"]),
                        "-np", str(n_parallel),
                        "--port", str(port),
                        "--host", "0.0.0.0"
                    ]
                    
                    if "mmproj" in new_model_cfg:
                        mmproj_path = os.path.join(os.path.expanduser(data_dir), new_model_cfg["mmproj"])
                        cmd_args.extend(["--mmproj", mmproj_path])
                    
                    if "extraArgs" in new_model_cfg:
                        cmd_args.extend(new_model_cfg["extraArgs"])
                    
                    log_file = open(new_model_cfg["logPath"], "w", buffering=1)
                    with _log_lock:
                        _log_handles[model_id] = log_file
                    
                    proc_kwargs = {
                        "stdout": log_file,
                        "stderr": subprocess.STDOUT,
                    }
                    if not IS_WINDOWS:
                        proc_kwargs["start_new_session"] = True
                    
                    proc = subprocess.Popen(cmd_args, **proc_kwargs)
                    with _proc_lock:
                        _model_processes[model_id] = proc
                        with _active_processes_lock:
                            _active_processes.add(model_id)
                    
                    print(f"[put_restart] Model {model_id} restarted with new config")
                except Exception as e:
                    print(f"[put_restart] Failed to restart model {model_id}: {e}")
                    with _log_lock:
                        _log_handles.pop(model_id, None)
                    with _proc_lock:
                        _model_processes.pop(model_id, None)
            
            threading.Thread(target=restart_model_after_put, daemon=True).start()
            response_data["restarted"] = True
            response_data["message"] = "Configuration saved. Model is restarting with new settings."
        
        self.send_json_response(response_data)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path in ["/api/start", "/api/stop", "/api/stop_all", "/api/start_all", "/api/config", "/api/mcp", "/api/restart", "/api/pin"]:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            
            data = {}
            if body:
                try:
                    data = json.loads(body)
                except ValueError:
                    self.send_error_response(400, "Invalid JSON body")
                    return

            if path == "/api/start":
                self.handle_api_start(data)
            elif path == "/api/stop":
                self.handle_api_stop(data)
            elif path == "/api/stop_all":
                self.handle_api_stop_all()
            elif path == "/api/start_all":
                self.handle_api_start_all(data)
            elif path == "/api/config":
                self.handle_api_config_post(data)
            elif path == "/api/mcp":
                self.handle_api_mcp_control(data)
            elif path == "/api/restart":
                self.handle_api_restart()
            elif path == "/api/pin":
                self.handle_api_pin(data)
            elif path == "/api/admin/reset-pin":
                self.handle_api_admin_reset_pin()
            elif path == "/api/models":
                self.handle_api_models_post(data)
        else:
            self.send_error_response(404, "Endpoint not found")

    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        
        local_path = os.path.abspath(os.path.join(STATIC_DIR, path.lstrip("/")))
        
        if not local_path.startswith(STATIC_DIR):
            self.send_error_response(403, "Forbidden")
            return

        if not os.path.exists(local_path) or os.path.isdir(local_path):
            self.send_error_response(404, f"File {path} not found")
            return

        ext = os.path.splitext(local_path)[1].lower()
        mime_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon"
        }
        content_type = mime_types.get(ext, "application/octet-stream")

        try:
            with open(local_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error_response(500, f"Error serving file: {str(e)}")

    def send_json_response(self, data):
        response_bytes = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def send_error_response(self, code, message):
        response_bytes = json.dumps({"error": message}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def handle_api_config_get(self):
        """Returns current mode configuration plus app metadata and runtime info."""
        binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")
        expanded_data_dir = os.path.expanduser(data_dir)
        
        self.send_json_response({
            "mode": get_global_mode(),
            "modes": ["single_port", "multi_port"],
            "appName": _CONFIG.get("appName", "llamashift"),
            "serviceName": _CONFIG.get("serviceName", "llamashift"),
            "masterPort": _CONFIG.get("masterPort", 9000),
            # Binary & data directory info
            "binaryPath": binary_path,
            "binaryExists": os.path.isfile(binary_path) and os.access(binary_path, os.X_OK),
            "dataDir": data_dir,
            "dataDirExists": os.path.isdir(expanded_data_dir),
            "dataDirModels": sorted(os.listdir(expanded_data_dir)) if os.path.isdir(expanded_data_dir) else [],
            # Runtime environment info
            "runtimeEnv": RUNTIME_ENV,
            "hasSystemd": HAS_SYSTEMD,
            "isDocker": IS_DOCKER,
            "isWindows": IS_WINDOWS,
        })

    def handle_api_config_post(self, data):
        """Toggles or sets the mode configuration."""
        new_mode = data.get("mode")
        valid_modes = ["single_port", "multi_port"]
        
        if not new_mode or new_mode not in valid_modes:
            self.send_error_response(400, f"Invalid mode. Must be one of: {', '.join(valid_modes)}")
            return
        
        old_mode = get_global_mode()
        save_mode(new_mode)
        
        running = get_running_servers()
        if running:
            self.send_json_response({
                "success": True,
                "mode": new_mode,
                "warning": f"Mode changed from {old_mode} to {new_mode}, but {len(running)} model(s) are still running."
            })
        else:
            self.send_json_response({
                "success": True,
                "mode": new_mode,
                "message": f"Mode switched from {old_mode} to {new_mode}"
            })

    def handle_api_status(self):
        mode = get_global_mode()

        # Always clean up stale tracked processes before checking status
        # This ensures models that crashed/died are marked as stopped
        _cleanup_stale_tracked_processes()

        running = get_running_servers()
        host_stats = get_host_stats()

        status_list = []
        for model_id, config in MODELS.items():
            port = config["port"]
            is_running = False
            pid = None

            if mode == "single_port":
                master_port = _CONFIG.get("masterPort", 9000)
                current_pid = running.get(master_port, {}).get("pid")
                
                if current_pid:
                    # Get the command line of the process on the master port
                    cmdline = get_process_cmdline(current_pid)
                    # Check if this process's command line contains this model's filename
                    # This ensures we only mark a model as "running" if its filename
                    # appears in the running process's command line
                    filename = config.get("filename", "")
                    if filename and filename in cmdline:
                        is_running = True
                        pid = current_pid
                    else:
                        is_running = False
                        pid = None
                else:
                    is_running = False
                    pid = None
            else:
                # Multi-port mode: check if port is in use AND the process
                # command-line matches this model (filename + port).
                # This prevents models sharing the same port from incorrectly
                # showing as running when only one is active.
                if port in running:
                    candidate_pid = running[port]["pid"]
                    # Verify this PID's process command line matches this model
                    cmdline = get_process_cmdline(candidate_pid)
                    is_running = _process_matches_model(cmdline, config, model_id)
                    pid = candidate_pid if is_running else None

            stats = {"cpu": 0.0, "mem": 0.0, "uptime": "Unknown"}
            if is_running:
                stats = get_process_stats(pid)

            status_list.append({
                **config,
                "status": "running" if is_running else "stopped",
                "pid": pid,
                "cpu": stats["cpu"],
                "memory": stats["mem"],
                "uptime": stats["uptime"]
            })

        mcp_running = False
        mcp_pid = None
        with _mcp_lock:
            if _mcp_process and _mcp_process.poll() is None:
                mcp_running = True
                mcp_pid = _mcp_process.pid

        self.send_json_response({
            "mode": mode,
            "models": status_list,
            "host": host_stats,
            "mcp": {
                "status": "running" if mcp_running else "stopped",
                "pid": mcp_pid
            }
        })

    def handle_api_gpu(self):
        telemetry = get_gpu_telemetry()
        self.send_json_response(telemetry)

    def handle_api_logs(self, query):
        model_id = query.get("model", [None])[0]
        lines_count = int(query.get("lines", [100])[0])

        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Invalid or missing 'model' parameter")
            return

        config = MODELS[model_id]
        logs = read_last_lines(config["logPath"], lines_count)
        
        self.send_json_response({
            "model": model_id,
            "logs": logs
        })

    def handle_api_mcp_status(self):
        mcp_running = False
        mcp_pid = None
        with _mcp_lock:
            if _mcp_process and _mcp_process.poll() is None:
                mcp_running = True
                mcp_pid = _mcp_process.pid

        self.send_json_response({
            "status": "running" if mcp_running else "stopped",
            "pid": mcp_pid
        })

    def handle_api_mcp_control(self, data):
        action = data.get("action")
        if not action:
            self.send_error_response(400, "Missing 'action' parameter")
            return

        if action == "start":
            if start_mcp_server():
                self.send_json_response({"success": True, "message": "MCP server started"})
            else:
                self.send_error_response(500, "Failed to start MCP server")
        elif action == "stop":
            stop_mcp_server()
            self.send_json_response({"success": True, "message": "MCP server stopped"})
        else:
            self.send_error_response(400, "Invalid action. Use 'start' or 'stop'")

    def handle_api_health(self):
        """Health check endpoint for restart flow.
        
        Returns 200 when the server is fully initialized and ready to accept requests.
        Returns 503 during server initialization (before set_server_ready() is called).
        
        This allows the frontend to poll the health endpoint after restart before
        reloading the page, ensuring the server is ready to serve requests.
        """
        if is_server_ready():
            self.send_json_response({
                "status": "ok",
                "message": "LlamaShift server is running"
            })
        else:
            self.send_error_response(503, "Server starting")
            # Also send a 200 response with body for consistency
            response_bytes = json.dumps({"status": "starting", "message": "Server is initializing"}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)

    def handle_api_start(self, data):
        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Invalid or missing 'model' parameter")
            return

        mode = get_global_mode()
        target_model = MODELS[model_id]
        stopped_some = []

        if mode == "single_port":
            master_port = _CONFIG.get("masterPort", 9000)
            
            # Step 1: Kill any existing llama-server process regardless of which model spawned it
            _cleanup_stale_tracked_processes()
            running = get_running_servers()
            
            # Step 1: Kill ALL running llama-server processes regardless of port
            # In single-port mode, only ONE model should ever run at a time,
            # even if models are configured on different ports or devices
            for _port, _info in list(running.items()):
                existing_pid = _info["pid"]
                if kill_process(existing_pid):
                    with _proc_lock:
                        for mid, p in list(_model_processes.items()):
                            try:
                                if p.pid == existing_pid:
                                    if mid not in stopped_some:
                                        stopped_some.append(mid)
                                    _model_processes.pop(mid, None)
                            except:
                                pass
            
            # Step 2: Wait for all ports to be fully released
            import socket
            ports_to_wait = list(running.keys())
            if ports_to_wait:
                for _wait_attempt in range(40):  # Up to 20 seconds
                    time.sleep(0.5)
                    all_free = True
                    for check_port in ports_to_wait:
                        _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        try:
                            _sock.settimeout(1)
                            _result = _sock.connect_ex(('127.0.0.1', int(check_port)))
                            if _result == 0:
                                all_free = False
                        finally:
                            _sock.close()
                    if all_free:
                        break
            
            # Step 3: Clean up _model_processes and _log_handles completely
            with _proc_lock:
                _model_processes.clear()
            with _log_lock:
                for handle in _log_handles.values():
                    try:
                        handle.close()
                    except OSError:
                        pass
                _log_handles.clear()

            port = master_port
        else:
            # Multi-port mode: allow multiple models simultaneously.
            # Only stop another model if it is on the SAME port (should not happen),
            # or if the user explicitly requested stop-first via query param.
            running = get_running_servers()
            port = target_model["port"]
            # Check if this exact port is already in use by another model
            if port in running:
                for other_id, other_config in MODELS.items():
                    if other_id == model_id:
                        continue
                    if other_config["port"] == port and port in running:
                        pid = running[port]["pid"]
                        if kill_process(pid):
                            stopped_some.append(other_id)
                            break

        # Wait for port to be released (up to 10 seconds) if we stopped something
        if stopped_some:
            import socket
            for _wait_attempt in range(20):
                time.sleep(0.5)
                _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    _sock.settimeout(1)
                    _result = _sock.connect_ex(('127.0.0.1', int(port)))
                    if _result != 0:
                        # Port is free
                        break
                finally:
                    _sock.close()
            else:
                print(f"[start] Port {port} still in use after 10s, proceeding anyway")

        # Build command
        binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")
        model_path = os.path.join(os.path.expanduser(data_dir), target_model["filename"])

        # Validate binary exists and is executable BEFORE starting
        if not os.path.isfile(binary_path):
            self.send_error_response(400, f"Binary not found: {binary_path}. Please check config.json 'binaryPath' or build llama.cpp. Typical path: ~/llama.cpp/build/bin/llama-server")
            return
        if not os.access(binary_path, os.X_OK):
            self.send_error_response(400, f"Binary not executable: {binary_path}. Run: chmod +x {binary_path}")
            return

        n_parallel = target_model.get("nParallel", 1)
        n_gpu_layers = target_model.get("nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])
        devices_list = target_model.get("devices", ["ROCm0"])
        device_str = ",".join(devices_list)

        cmd_args = [
            binary_path,
            "--model", model_path,
            "--device", device_str,
            "-ngl", str(n_gpu_layers),
            "--ctx-size", str(target_model["ctxSize"]),
            "-np", str(n_parallel),
            "--port", str(port),
            "--host", "0.0.0.0"
        ]

        if "mmproj" in target_model:
            mmproj_path = os.path.join(os.path.expanduser(data_dir), target_model["mmproj"])
            cmd_args.extend(["--mmproj", mmproj_path])

        if "extraArgs" in target_model:
            cmd_args.extend(target_model["extraArgs"])

        cmd_str = " ".join(cmd_args)
        print(f"[start] Model {model_id}: {cmd_str}")
        
        if not os.path.exists(model_path):
            self.send_error_response(400, f"Model file not found: {model_path}")
            return

        running = get_running_servers()
        if port in running:
            self.send_error_response(400, f"Port {port} is already in use")
            return

        try:
            log_dir = os.path.dirname(target_model["logPath"])
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)

            log_file = open(target_model["logPath"], "w", buffering=1)
            with _log_lock:
                _log_handles[model_id] = log_file

            # On Windows, don't use start_new_session (not supported the same way)
            proc_kwargs = {
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
            }
            if not IS_WINDOWS:
                proc_kwargs["start_new_session"] = True
            
            proc = subprocess.Popen(cmd_args, **proc_kwargs)

            # Brief wait + health check: ensure the process didn't immediately crash
            # (e.g. due to port already in use or OOM)
            time.sleep(0.5)
            if proc.poll() is not None:
                # Process died immediately — read its output for diagnostics
                stderr_output = proc.stderr.read() if proc.stderr else ""
                with _log_lock:
                    _log_handles.pop(model_id, None)
                with _proc_lock:
                    _model_processes.pop(model_id, None)
                    with _active_processes_lock:
                        _active_processes.discard(model_id)
                self.send_error_response(
                    500,
                    f"Process died immediately (exit code {proc.returncode}). "
                    f"Check logs for details."
                )
                return

            with _proc_lock:
                _model_processes[model_id] = proc
                with _active_processes_lock:
                    _active_processes.add(model_id)

            self.send_json_response({
                "success": True,
                "message": f"Starting {target_model['name']}...",
                "stopped": stopped_some
            })
        except Exception as e:
            with _log_lock:
                _log_handles.pop(model_id, None)
            with _proc_lock:
                _model_processes.pop(model_id, None)
                with _active_processes_lock:
                    _active_processes.discard(model_id)
            self.send_error_response(500, f"Failed to spawn model: {str(e)}")

    def handle_api_stop(self, data):
        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Invalid or missing 'model' parameter")
            return

        mode = get_global_mode()
        target_model = MODELS[model_id]
        running = get_running_servers()

        if mode == "single_port":
            if not running:
                self.send_json_response({"success": True, "message": "No model is currently running."})
                return
            pid = list(running.values())[0]["pid"]
        else:
            port = target_model["port"]
            if port not in running:
                self.send_json_response({"success": True, "message": f"Model {model_id} was already stopped."})
                return
            pid = running[port]["pid"]

        if kill_process(pid):
            actual_model_id = None
            with _log_lock:
                if mode == "single_port":
                    with _proc_lock:
                        for mid, proc in list(_model_processes.items()):
                            try:
                                if proc.pid == pid:
                                    actual_model_id = mid
                                    _model_processes.pop(mid, None)
                                    with _active_processes_lock:
                                        _active_processes.discard(mid)
                                    break
                            except Exception:
                                pass
                    log_handle = _log_handles.pop(actual_model_id or model_id, None)
                    with _proc_lock:
                        _model_processes.pop(model_id, None)
                        with _active_processes_lock:
                            _active_processes.discard(model_id)
                else:
                    log_handle = _log_handles.pop(model_id, None)
                    with _proc_lock:
                        _model_processes.pop(model_id, None)
                        with _active_processes_lock:
                            _active_processes.discard(model_id)
                if log_handle:
                    try:
                        log_handle.close()
                    except OSError:
                        pass
            self.send_json_response({
                "success": True,
                "message": f"Stopped model {actual_model_id or model_id}.",
                "stopped_model": actual_model_id or model_id
            })
        else:
            self.send_error_response(500, f"Failed to stop model {model_id}.")

    def handle_api_stop_all(self):
        running = get_running_servers()
        stopped_pids = []
        
        for port, info in running.items():
            pid = info["pid"]
            if kill_process(pid):
                stopped_pids.append(pid)

        with _log_lock:
            model_by_port = {cfg["port"]: mid for mid, cfg in MODELS.items()}
            for pid in stopped_pids:
                for port, mid in model_by_port.items():
                    try:
                        if running[port]["pid"] == pid:
                            handle = _log_handles.pop(mid, None)
                            if handle:
                                try:
                                    handle.close()
                                except OSError:
                                    pass
                            break
                    except (KeyError, TypeError):
                        pass

        self.send_json_response({
            "success": True,
            "message": f"Terminated {len(stopped_pids)} llama-server instances.",
            "stopped_pids": stopped_pids
        })

        with _proc_lock:
            model_by_port = {cfg["port"]: mid for mid, cfg in MODELS.items()}
            for pid in stopped_pids:
                for port, mid in model_by_port.items():
                    try:
                        if running[port]["pid"] == pid:
                            _model_processes.pop(mid, None)
                            break
                    except (KeyError, TypeError):
                        pass

        _cleanup_stale_tracked_processes()

    def handle_api_start_all(self, data):
        """Start all stopped models. Only works in multi_port mode (unless force=true)."""
        mode = get_global_mode()
        force = data.get("force", False)
        
        if mode != "multi_port" and not force:
            self.send_error_response(400, "Start All is only available in Multi Port mode. Toggle to Multi Port or set force=true.")
            return
        
        started = []
        failed = []
        running = get_running_servers()
        
        for model_id, config in MODELS.items():
            port = config["port"]
            # Skip already running models
            if port in running:
                continue
            
            try:
                # Reuse the same logic as handle_api_start but in a loop
                binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
                data_dir = _CONFIG.get("dataDir", "~/models")
                model_path = os.path.join(os.path.expanduser(data_dir), config["filename"])
                
                if not os.path.exists(model_path):
                    failed.append({"model": model_id, "reason": f"Model file not found: {model_path}"})
                    continue
                
                n_parallel = config.get("nParallel", 1)
                n_gpu_layers = config.get("nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])
                devices_list = config.get("devices", ["ROCm0"])
                device_str = ",".join(devices_list)
                
                cmd_args = [
                    binary_path,
                    "--model", model_path,
                    "--device", device_str,
                    "-ngl", str(n_gpu_layers),
                    "--ctx-size", str(config["ctxSize"]),
                    "-np", str(n_parallel),
                    "--port", str(port),
                    "--host", "0.0.0.0"
                ]
                
                if "mmproj" in config:
                    mmproj_path = os.path.join(os.path.expanduser(data_dir), config["mmproj"])
                    cmd_args.extend(["--mmproj", mmproj_path])
                
                if "extraArgs" in config:
                    cmd_args.extend(config["extraArgs"])
                
                print(f"[start_all] Starting {model_id}: {' '.join(cmd_args)}")
                
                log_file = open(config["logPath"], "w", buffering=1)
                with _log_lock:
                    _log_handles[model_id] = log_file
                
                proc_kwargs = {
                    "stdout": log_file,
                    "stderr": subprocess.STDOUT,
                }
                if not IS_WINDOWS:
                    proc_kwargs["start_new_session"] = True
                
                proc = subprocess.Popen(cmd_args, **proc_kwargs)
                
                with _proc_lock:
                    _model_processes[model_id] = proc
                    with _active_processes_lock:
                        _active_processes.add(model_id)
                
                started.append(model_id)
                
            except Exception as e:
                failed.append({"model": model_id, "reason": str(e)})
                with _log_lock:
                    _log_handles.pop(model_id, None)
                with _proc_lock:
                    _model_processes.pop(model_id, None)
        
        self.send_json_response({
            "success": True,
            "started": started,
            "failed": failed,
            "message": f"Started {len(started)} model(s). {len(failed)} failed." if failed else f"Started {len(started)} model(s)."
        })

    def handle_api_models(self):
        """GET /api/models - list models. POST /api/models - create a new model."""
        if self.command == 'GET':
            self.send_json_response({"models": get_models()})

    def handle_api_models_post(self, data):
        """POST /api/models - create a new model entry in config.json."""
        model_id = data.get("id")
        if not model_id or not re.match(r'^[a-zA-Z0-9_]+$', model_id):
            self.send_error_response(400, "Invalid model id. Must be alphanumeric/underscore, e.g. 'qwen3_8b'")
            return

        if model_id in MODELS:
            self.send_error_response(409, f"Model '{model_id}' already exists")
            return

        required = ["name", "filename", "port"]
        for field in required:
            if not data.get(field):
                self.send_error_response(400, f"Missing required field: {field}")
                return

        port = int(data.get("port", 0))
        # Validate port not already in use
        for existing_id, existing_cfg in MODELS.items():
            if existing_cfg.get("port") == port:
                self.send_error_response(409, f"Port {port} is already assigned to model '{existing_id}'")
                return

        # Build model config
        new_model = {
            "id": model_id,
            "name": data["name"],
            "desc": data.get("desc", ""),
            "filename": data["filename"],
            "port": port,
            "ctxSize": int(data.get("ctxSize", DEFAULT_RUNTIME_PARAMS["ctxSize"])),
            "nParallel": int(data.get("nParallel", DEFAULT_RUNTIME_PARAMS["nParallel"])),
            "nGpuLayers": int(data.get("nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])),
            "batchSize": int(data.get("batchSize", DEFAULT_RUNTIME_PARAMS["batchSize"])),
            "threads": int(data.get("threads", DEFAULT_RUNTIME_PARAMS["threads"])),
            "temperature": float(data.get("temperature", DEFAULT_RUNTIME_PARAMS["temperature"])),
            "maxTokens": int(data.get("maxTokens", DEFAULT_RUNTIME_PARAMS["maxTokens"])),
            "topP": float(data.get("topP", DEFAULT_RUNTIME_PARAMS["topP"])),
            "devices": data.get("devices", ["CPU0"]),
            "extraArgs": data.get("extraArgs", []),
            "endpoint": f"http://localhost:{port}/v1",
        }

        # Optional mmproj
        if data.get("mmproj"):
            new_model["mmproj"] = data["mmproj"]

        with _models_lock:
            _CONFIG["models"][model_id] = new_model
            _models_cache = None
        save_config(_CONFIG)

        self.send_json_response({
            "success": True,
            "model": model_id,
            "config": dict(new_model),
            "message": f"Model '{model_id}' created successfully"
        })

    def handle_api_model_delete(self, model_id):
        """DELETE /api/models?model=<id> - remove model from config."""
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, f"Invalid or missing model id: {model_id}")
            return

        # Check if model is currently running
        running = get_running_servers()
        model_cfg = MODELS[model_id]
        port = model_cfg.get("port")
        mode = get_global_mode()

        if mode == "multi_port" and port and port in running:
            self.send_error_response(409, f"Model '{model_id}' is currently running. Please stop it first.")
            return
        elif mode == "single_port":
            _cleanup_stale_tracked_processes()
            with _proc_lock:
                for mid, proc in list(_model_processes.items()):
                    if mid == model_id and is_llama_server(proc.pid):
                        self.send_error_response(409, f"Model '{model_id}' is currently running. Please stop it first.")
                        return

        with _models_lock:
            _CONFIG["models"].pop(model_id, None)
            _models_cache = None
        save_config(_CONFIG)

        # Clean up any tracked process / log handle
        with _proc_lock:
            _model_processes.pop(model_id, None)
        with _log_lock:
            handle = _log_handles.pop(model_id, None)
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass

        self.send_json_response({
            "success": True,
            "model": model_id,
            "message": f"Model '{model_id}' deleted successfully"
        })

    def handle_api_pin(self, data=None):
        """Handle PIN-related API requests.
        
        GET /api/pin/status - Check if PIN is set and verify session
        POST /api/pin/verify - Verify PIN (expects {"pin": "1234"})
        POST /api/pin/set - Set/create PIN (expects {"pin": "1234"})
        POST /api/pin/verify-session - Verify session token
        """
        # Get client IP address
        client_ip = self.client_address[0]
        
        if self.command == 'GET':
            # Check if PIN is set and verify session
            pin_hash = _CONFIG.get("pinHash")
            if not pin_hash:
                self.send_json_response({
                    "success": True,
                    "pinSet": False,
                    "message": "No PIN is currently set"
                })
                return
            
            # Check session validity
            session_token = self.headers.get("X-Pin-Session")
            session_valid = False
            if session_token and session_token in _PIN_SESSIONS:
                session_data = _PIN_SESSIONS[session_token]
                if time.time() - session_data["created_at"] < PIN_SESSION_TIMEOUT:
                    session_valid = True
            
            self.send_json_response({
                "success": True,
                "pinSet": True,
                "sessionValid": session_valid
            })
        
        elif self.command == 'POST':
            # data was already parsed in do_POST
            if data is None:
                data = {}
            
            # Check rate limiting for all POST PIN requests
            if _is_rate_limited(client_ip):
                self.send_error_response(429, "Rate limit exceeded. Please try again later.")
                return
            
            action = data.get("action", "")
            
            if action == "verify":
                # Check if account is locked
                if _is_account_locked(client_ip):
                    self.send_error_response(403, "Account locked due to too many failed attempts. Please try again later.")
                    return
                
                # Verify PIN
                pin = data.get("pin", "")
                if not pin:
                    self.send_error_response(400, "Missing PIN")
                    return
                
                # Input validation for PIN
                if not isinstance(pin, str):
                    self.send_error_response(400, "PIN must be a string")
                    return
                
                if not pin.isdigit():
                    self.send_error_response(400, "PIN must contain only digits")
                    return
                
                if len(pin) < 4 or len(pin) > 12:
                    self.send_error_response(400, "PIN must be between 4 and 12 digits")
                    return
                
                pin_hash = _CONFIG.get("pinHash")
                if not pin_hash:
                    self.send_error_response(401, "No PIN is set")
                    return
                
                # Verify PIN using the stored hash
                salt = _CONFIG.get("pinSalt", "")
                input_hash = _hash_pin(pin, salt)
                
                if input_hash == pin_hash:
                    # PIN correct - clear failed attempts for this IP
                    _pin_failed_attempts[client_ip] = []
                    # Create session token
                    import uuid
                    session_token = str(uuid.uuid4())
                    _PIN_SESSIONS[session_token] = {
                        "created_at": time.time(),
                        "ip": client_ip
                    }
                    
                    self.send_json_response({
                        "success": True,
                        "sessionValid": True,
                        "sessionToken": session_token,
                        "message": "PIN verified successfully"
                    })
                else:
                    # PIN incorrect - record failed attempt
                    if _record_failed_attempt(client_ip, success=False):
                        # Account locked
                        self.send_error_response(403, "Account locked due to too many failed attempts. Please try again later.")
                    else:
                        self.send_error_response(401, "Invalid PIN")
            
            elif action == "set":
                # Set/create PIN
                pin = data.get("pin", "")
                if not pin:
                    self.send_error_response(400, "Missing PIN")
                    return
                
                if len(pin) < 4:
                    self.send_error_response(400, "PIN must be at least 4 digits")
                    return
                
                # Generate salt and hash
                import secrets
                salt = secrets.token_hex(16)
                pin_hash = _hash_pin(pin, salt)
                
                # Update config
                with _config_lock:
                    _CONFIG["pinHash"] = pin_hash
                    _CONFIG["pinSalt"] = salt
                    _CONFIG["pinSetAt"] = int(time.time())
                
                save_config(_CONFIG)
                
                self.send_json_response({
                    "success": True,
                    "message": "PIN set successfully"
                })
            
            elif action == "verify-session":
                # Verify session token
                session_token = data.get("sessionToken")
                if not session_token:
                    self.send_error_response(400, "Missing session token")
                    return
                
                if session_token in _PIN_SESSIONS:
                    session_data = _PIN_SESSIONS[session_token]
                    if time.time() - session_data["created_at"] < PIN_SESSION_TIMEOUT:
                        self.send_json_response({
                            "success": True,
                            "sessionValid": True
                        })
                        return
                
                self.send_error_response(401, "Invalid or expired session")

            elif action == "change":
                # Change the existing PIN after verifying the current one
                current_pin = data.get("currentPin", "")
                new_pin = data.get("newPin", "")
                if not current_pin or not new_pin:
                    self.send_error_response(400, "Missing currentPin or newPin")
                    return

                if not isinstance(current_pin, str) or not isinstance(new_pin, str):
                    self.send_error_response(400, "PIN values must be strings")
                    return

                if not current_pin.isdigit() or not new_pin.isdigit():
                    self.send_error_response(400, "PIN must contain only digits")
                    return

                if len(current_pin) < 4 or len(current_pin) > 12 or len(new_pin) < 4 or len(new_pin) > 12:
                    self.send_error_response(400, "PIN must be between 4 and 12 digits")
                    return

                pin_hash = _CONFIG.get("pinHash")
                if not pin_hash:
                    self.send_error_response(401, "No PIN is set")
                    return

                salt = _CONFIG.get("pinSalt", "")
                current_hash = _hash_pin(current_pin, salt)
                if current_hash != pin_hash:
                    if _record_failed_attempt(client_ip, success=False):
                        self.send_error_response(403, "Account locked due to too many failed attempts. Please try again later.")
                    else:
                        self.send_error_response(401, "Invalid current PIN")
                    return

                # Update the PIN with a new salt and hash
                import secrets
                new_salt = secrets.token_hex(16)
                new_hash = _hash_pin(new_pin, new_salt)

                with _config_lock:
                    _CONFIG["pinHash"] = new_hash
                    _CONFIG["pinSalt"] = new_salt
                    _CONFIG["pinSetAt"] = int(time.time())

                save_config(_CONFIG)
                _pin_failed_attempts[client_ip] = []

                self.send_json_response({
                    "success": True,
                    "message": "PIN changed successfully"
                })

            else:
                self.send_error_response(400, "Invalid action. Use 'verify', 'set', 'verify-session', or 'change'")
    
    def handle_api_admin_reset_pin(self):
        """Reset the PIN to default (1234) - admin function.
        
        POST /api/admin/reset-pin
        Expects: {"adminPassword": "current_admin_password"}
        """
        client_ip = self.client_address[0]
        
        # Check rate limiting
        if _is_rate_limited(client_ip):
            self.send_error_response(429, "Rate limit exceeded. Please try again later.")
            return
        
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_error_response(400, "Invalid JSON in request body")
            return
        
        admin_password = data.get("adminPassword")
        if not admin_password:
            self.send_error_response(400, "Missing adminPassword")
            return
        
        # Verify admin password
        stored_admin_hash = _CONFIG.get("adminPasswordHash")
        stored_admin_salt = _CONFIG.get("adminPasswordSalt")
        
        if not stored_admin_hash or not stored_admin_salt:
            self.send_error_response(401, "Admin authentication not configured")
            return
        
        # Hash and compare admin password - need to check the implementation in security.py
        # For now, we'll use the standard approach - hash the input with salt and compare
        import hashlib
        input_hash = hashlib.sha256(f"{stored_admin_salt}{admin_password}".encode()).hexdigest()
        if input_hash != stored_admin_hash:
            self.send_error_response(401, "Invalid admin password")
            return
        
        # Reset PIN to default (1234)
        default_pin = "1234"
        new_salt = secrets.token_hex(16)
        new_pin_hash = _hash_pin(default_pin, new_salt)
        
        # Update config
        with _config_lock:
            _CONFIG["pinHash"] = new_pin_hash
            _CONFIG["pinSalt"] = new_salt
            _CONFIG["pinSetAt"] = int(time.time())
        
        save_config(_CONFIG)
        
        # Log security event - fix the function call to match the signature
        _log_security_event("PIN reset to default", {
            "ip": client_ip,
            "reset_by": "admin"
        })
        
        self.send_json_response({
            "success": True,
            "message": "PIN reset to default (1234). Please change it immediately."
        })
    
    def handle_api_models_export(self):
        """Export all model configurations as JSON."""
        models = get_models()
        self.send_json_response({
            "success": True,
            "models": models,
            "count": len(models),
            "filename": "llamashift-models-export.json"
        })

    def handle_api_models_import_post(self):
        """POST handler for /api/models/import - reads JSON body and calls handle_api_models_import."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            self.handle_api_models_import(data)
        except json.JSONDecodeError:
            self.send_error_response(400, "Invalid JSON in request body")
        except Exception as e:
            self.send_error_response(500, f"Import error: {str(e)}")

    def handle_api_models_import(self, data):
        """Import model configurations from JSON (overwrites existing)."""
        models = data.get("models")
        if not models or not isinstance(models, dict):
            self.send_error_response(400, "Missing or invalid 'models' field. Expected a dict of model configurations.")
            return

        # Validate each model has required fields
        required = ["name", "filename", "port"]
        for model_id, model_cfg in models.items():
            for field in required:
                if not model_cfg.get(field):
                    self.send_error_response(400, f"Model '{model_id}': Missing required field '{field}'")
                    return

        # Check for duplicate ports
        ports_used = {}
        for model_id, model_cfg in models.items():
            port = model_cfg.get("port")
            if port in ports_used:
                self.send_error_response(400, f"Duplicate port {port} found in models '{ports_used[port]}' and '{model_id}'")
                return
            ports_used[port] = model_id

        # Update config with imported models
        with _models_lock:
            _CONFIG["models"] = models
            _models_cache = None
        save_config(_CONFIG)

        self.send_json_response({
            "success": True,
            "count": len(models),
            "message": f"Successfully imported {len(models)} model(s)"
        })

    def handle_api_model_patch(self, data):
        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Invalid or missing 'model' parameter")
            return

        updates = data.get("updates")
        if not updates or not isinstance(updates, dict):
            self.send_error_response(400, "Missing or invalid 'updates' object")
            return

        allowed = {
            "ctxSize", "nParallel", "nGpuLayers", "temperature",
            "maxTokens", "topP", "batchSize", "threads", "extraArgs",
            "name", "desc", "devices", "mmproj", "port",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            self.send_error_response(400, "No valid fields to update")
            return

        # Determine if this requires a restart (runtime-affecting params)
        runtime_params = {"ctxSize", "nParallel", "nGpuLayers", "batchSize", "threads",
                          "extraArgs", "devices", "mmproj", "port", "filename"}
        requires_restart = bool(runtime_params & set(filtered.keys()))

        # Check if model is currently running BEFORE updating config
        was_running = False
        if requires_restart:
            running = get_running_servers()
            mode = get_global_mode()
            old_model_cfg = MODELS[model_id]
            old_port = old_model_cfg.get("port")

            if mode == "multi_port" and old_port and old_port in running:
                was_running = True
            elif mode == "single_port":
                _cleanup_stale_tracked_processes()
                with _proc_lock:
                    for mid, proc in list(_model_processes.items()):
                        if mid == model_id and is_llama_server(proc.pid):
                            was_running = True
                            break

        # Save updated config
        with _models_lock:
            _CONFIG["models"][model_id].update(filtered)
            _models_cache = None
        save_config(_CONFIG)

        response_data = {
            "success": True,
            "model": model_id,
            "updated": filtered,
            "config": dict(_CONFIG["models"][model_id]),
        }

        # If model was running and runtime params changed, restart it
        if was_running and requires_restart:
            def restart_model_after_patch():
                time.sleep(0.5)
                try:
                    # Stop the running process
                    running = get_running_servers()
                    mode = get_global_mode()
                    new_model_cfg = MODELS[model_id]
                    new_port = new_model_cfg.get("port")

                    if mode == "multi_port" and new_port and new_port in running:
                        pid = running[new_port]["pid"]
                        kill_process(pid)
                        with _log_lock:
                            handle = _log_handles.pop(model_id, None)
                            if handle:
                                try:
                                    handle.close()
                                except OSError:
                                    pass
                        with _proc_lock:
                            _model_processes.pop(model_id, None)
                            with _active_processes_lock:
                                _active_processes.discard(model_id)
                    elif mode == "single_port":
                        _cleanup_stale_tracked_processes()
                        master_port = _CONFIG.get("masterPort", 9000)
                        if master_port in running:
                            pid = running[master_port]["pid"]
                            kill_process(pid)
                            with _proc_lock:
                                for mid2, proc in list(_model_processes.items()):
                                    try:
                                        if proc.pid == pid:
                                            _model_processes.pop(mid2, None)
                                            with _active_processes_lock:
                                                _active_processes.discard(mid2)
                                            break
                                    except:
                                        pass
                            with _log_lock:
                                handle = _log_handles.pop(model_id, None)
                                if handle:
                                    try:
                                        handle.close()
                                    except OSError:
                                        pass
                    # Determine port to use for this model
                    port = new_port if mode == "multi_port" else _CONFIG.get("masterPort", 9000)

                    # Wait for port to be released (up to 10 seconds)
                    import socket
                    for _wait_attempt in range(20):
                        time.sleep(0.5)
                        _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        try:
                            _sock.settimeout(1)
                            _result = _sock.connect_ex(('127.0.0.1', int(port)))
                            if _result != 0:
                                # Port is free
                                break
                        finally:
                            _sock.close()
                    else:
                        print(f"[patch_restart] Port {port} still in use after 10s, proceeding anyway")

                    # Start the model with new config
                    binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
                    data_dir = _CONFIG.get("dataDir", "~/models")
                    model_path = os.path.join(os.path.expanduser(data_dir), new_model_cfg["filename"])

                    n_parallel = new_model_cfg.get("nParallel", 1)
                    n_gpu_layers = new_model_cfg.get("nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])
                    devices_list = new_model_cfg.get("devices", ["ROCm0"])
                    device_str = ",".join(devices_list)

                    cmd_args = [
                        binary_path,
                        "--model", model_path,
                        "--device", device_str,
                        "-ngl", str(n_gpu_layers),
                        "--ctx-size", str(new_model_cfg["ctxSize"]),
                        "-np", str(n_parallel),
                        "--port", str(port),
                        "--host", "0.0.0.0"
                    ]

                    if "mmproj" in new_model_cfg:
                        mmproj_path = os.path.join(os.path.expanduser(data_dir), new_model_cfg["mmproj"])
                        cmd_args.extend(["--mmproj", mmproj_path])

                    if "extraArgs" in new_model_cfg:
                        cmd_args.extend(new_model_cfg["extraArgs"])

                    log_file = open(new_model_cfg["logPath"], "w", buffering=1)
                    with _log_lock:
                        _log_handles[model_id] = log_file

                    proc_kwargs = {
                        "stdout": log_file,
                        "stderr": subprocess.STDOUT,
                    }
                    if not IS_WINDOWS:
                        proc_kwargs["start_new_session"] = True

                    proc = subprocess.Popen(cmd_args, **proc_kwargs)
                    with _proc_lock:
                        _model_processes[model_id] = proc
                        with _active_processes_lock:
                            _active_processes.add(model_id)

                    print(f"[patch_restart] Model {model_id} restarted with new config")
                except Exception as e:
                    print(f"[patch_restart] Failed to restart model {model_id}: {e}")
                    with _log_lock:
                        _log_handles.pop(model_id, None)
                    with _proc_lock:
                        _model_processes.pop(model_id, None)

            threading.Thread(target=restart_model_after_patch, daemon=True).start()
            response_data["restarted"] = True
            response_data["message"] = "Configuration saved. Model is restarting with new settings."

        self.send_json_response(response_data)

    def handle_api_files(self):
        """GET /api/files - list model files (gguf/gguf) in the dataDir."""
        data_dir = _CONFIG.get("dataDir", "~/models")
        expanded = os.path.expanduser(data_dir)

        if not os.path.isdir(expanded):
            self.send_json_response({"files": [], "dataDir": data_dir, "dataDirExists": False})
            return

        files = []
        for root, dirs, filenames in os.walk(expanded):
            # Only go 2 levels deep to avoid huge walks
            depth = root[len(expanded):].count(os.sep)
            if depth > 2:
                dirs.clear()
                continue
            for fn in sorted(filenames):
                lower = fn.lower()
                if lower.endswith(".gguf") or lower.endswith(".bin") or lower.endswith(".safetensors"):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, expanded)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    files.append({
                        "name": fn,
                        "path": rel.replace(os.sep, "/"),
                        "fullPath": full,
                        "size": size,
                        "sizeHuman": _human_size(size),
                    })

        self.send_json_response({
            "files": files,
            "dataDir": data_dir,
            "dataDirExists": True,
        })

    def handle_api_restart(self):
        """Cross-platform restart: self-restart without systemd dependency.
        
        Strategy:
        - If systemd: stop all, exit (systemd restarts us via Restart=always)
        - If Docker: stop all, exit (docker restarts via restart: always)
        - If manual (Linux/Windows): stop all models, then spawn a new instance
          of this server and exit the current one gracefully.
        """
        restart_method = "unknown"
        
        if HAS_SYSTEMD:
            restart_method = "systemd"
        elif IS_DOCKER:
            restart_method = "docker"
        else:
            restart_method = "self"
        
        self.send_json_response({
            "success": True,
            "method": restart_method,
            "message": f"Restart initiated via {restart_method}. Connection will temporarily drop."
        })
        
        def do_restart():
            time.sleep(0.5)  # allow response to transmit
            print(f"[restart] Restart via {restart_method}. Cleaning up...")
            stop_mcp_server()
            _stop_all_model_processes()
            _close_all_log_handles()
            
            if restart_method == "self":
                # Self-restart: spawn new server process and exit
                print("[restart] Self-restarting...")
                script = os.path.abspath(__file__)
                restart_cmd = [sys.executable, script]
                
                try:
                    if IS_WINDOWS:
                        subprocess.Popen(restart_cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                    else:
                        subprocess.Popen(restart_cmd, start_new_session=True)
                    print("[restart] New server process spawned.")
                except Exception as e:
                    print(f"[restart] Failed to spawn new server: {e}")
            
            print("[restart] Exiting current process...")
            os._exit(0)
            
        threading.Thread(target=do_restart, daemon=True).start()

def _close_all_log_handles():
    """Close all open log file handles on shutdown."""
    with _log_lock:
        for handle in _log_handles.values():
            try:
                handle.close()
            except OSError:
                pass
        _log_handles.clear()


def _stop_all_model_processes():
    """Gracefully stop all tracked llama-server model processes, including child processes."""
    with _proc_lock:
        pids_to_stop = list(_model_processes.items())
        _model_processes.clear()
    for mid, proc in pids_to_stop:
        try:
            # Terminate the main process
            proc.terminate()
        except Exception:
            pass
        
        try:
            # Wait up to 3 seconds for graceful shutdown
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                # Force kill if still running
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            # If wait times out, try to kill
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
    
    # Additional cleanup: scan for any stray llama-server processes
    # that might have been spawned as children
    print("[shutdown] Scanning for any remaining llama-server processes...")
    try:
        running = get_running_servers()
        for port, data in list(running.items()):
            pid = data.get("pid", 0)
            if is_llama_server(pid):
                print(f"[shutdown] Found stray process PID {pid}, terminating...")
                kill_process(pid)
    except Exception as e:
        print(f"[shutdown] Error during stray process cleanup: {e}")
    
    print("[shutdown] All model processes stopped")


def main():
    os.makedirs(STATIC_DIR, exist_ok=True)

    def handle_signal(signum, frame):
        print(f"\n[signal] Received signal {signum}. Shutting down...")
        stop_mcp_server()
        _stop_all_model_processes()
        _close_all_log_handles()
        sys.exit(0)

    if not IS_WINDOWS:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    else:
        signal.signal(signal.SIGINT, handle_signal)

    start_mcp_server()

    handler = SwitcherAPIHandler
    socketserver.TCPServer.allow_reuse_address = True

    server_port = get_server_port()
    with socketserver.TCPServer(("", server_port), handler) as httpd:
        print(f"{'='*50}")
        print(f"  LlamaShift Backend v0.1.0")
        print(f"  Runtime: {RUNTIME_ENV}")
        print(f"  URL: http://localhost:{server_port}")
        print(f"  Static: {STATIC_DIR}")
        print(f"  Restart: {'systemd' if HAS_SYSTEMD else 'docker' if IS_DOCKER else 'self'}")
        print(f"{'='*50}")

        # Set server ready - HTTP server is now fully initialized and accepting requests
        set_server_ready()

        # Start WebSocket telemetry broadcast loop alongside HTTP server
        ws_port = 28002  # separate from HTTP port

        async def _ws_handler(ws):
            _ws_clients.add(ws)
            try:
                async for _ in ws:
                    pass  # ignore incoming
            finally:
                _ws_clients.discard(ws)

        async def run_ws():
            async with websockets.serve(_ws_handler, "0.0.0.0", ws_port):
                await _telemetry_broadcast_loop()

        # Run HTTP in a background thread, WS in main asyncio loop
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        print(f"[ws] WebSocket telemetry server listening on port {ws_port}")

        try:
            asyncio.run(run_ws())
        except (KeyboardInterrupt, SystemExit):
            httpd.shutdown()

if __name__ == "__main__":
    main()
