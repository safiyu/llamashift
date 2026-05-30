#!/usr/bin/env python3
"""
Configuration management module for llama-shift server.
Handles config loading/saving, environment detection, and model management.
"""

import json
import os
import platform
import subprocess
import threading
from collections import defaultdict

# ==================== CONSTANTS ====================
SERVER_PORT_KEY = "serverPort"
CONFIG_FILE = "config.json"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

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

# Global config state
_CONFIG = None
_config_lock = threading.Lock()

# Models loaded from config.json (keyed by model_id)
_models_cache = None
_models_lock = threading.Lock()

# Log directory — derived from config appName (set after load)
LOG_DIR = None


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


# ─── Config Management ─────────────────────────────────────────────────────
def get_server_port():
    """Get the server port from config, with default fallback."""
    config = load_config()
    return config.get(SERVER_PORT_KEY, 8002)


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
    global _CONFIG
    _CONFIG["mode"] = mode
    save_config(_CONFIG)
    with _models_lock:
        _models_cache = None  # invalidate cache


# ─── Model Management ──────────────────────────────────────────────────────
def _ensure_log_dir():
    """Ensure LOG_DIR is initialized from config appName."""
    global LOG_DIR
    if LOG_DIR is None:
        app_name = load_config().get("appName", "llamashift")
        LOG_DIR = os.path.join(os.path.expanduser("~/logs"), str(app_name))
    os.makedirs(LOG_DIR, exist_ok=True)


def get_models():
    """Return the models dict from config, with logPath derived at runtime."""
    global _models_cache
    with _models_lock:
        if _models_cache is None:
            raw = load_config().get("models", {})
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


def get_global_mode():
    """Return the current mode setting."""
    return _CONFIG.get("mode", "single_port")


# Call it once at startup (after _CONFIG is initialized)
def init_config():
    """Initialize global config state - call this after module load."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    _ensure_log_dir()


def _human_size(size_bytes):
    """Convert bytes to human-readable size (e.g., 1.5 GB)."""
    if size_bytes == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def reset_config_for_testing():
    """Reset config state - useful for testing."""
    global _CONFIG, _models_cache, LOG_DIR
    _CONFIG = None
    _models_cache = None
    LOG_DIR = None


# Initialize config at module load so LOG_DIR and _CONFIG are set before any request
init_config()
