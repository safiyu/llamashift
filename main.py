#!/usr/bin/env python3
"""
Main entry point for llama-shift server.
This file imports from modular components and sets up the HTTP/WebSocket servers.
"""

import asyncio
import hashlib
import json
import os
import random
import signal
import socketserver
import sys
import threading
import time

import websockets

# Import from modular components
from config import (
    _CONFIG, MODELS, DEFAULT_RUNTIME_PARAMS, STATIC_DIR,
    get_server_port, get_global_mode, save_config, _models_lock, _models_cache
)
from security import (
    _is_rate_limited, _is_account_locked, _record_failed_attempt,
    _hash_pin, _PIN_SESSIONS, PIN_SESSION_TIMEOUT, _log_security_event
)
from process import (
    get_running_servers, kill_process, get_host_stats, get_process_stats,
    get_process_cmdline, _model_processes, _proc_lock, _active_processes, _active_processes_lock,
    _log_handles, _log_lock, read_last_lines,
    _stop_all_model_processes, _close_all_log_handles, get_gpu_telemetry
)
from config import _mcp_process, _mcp_lock
from telemetry import (
    _ws_clients, _telemetry_broadcast_loop
)
from api import SwitcherAPIHandler, set_server_ready, is_server_ready

# Runtime environment detection
try:
    import systemd.daemon
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False

IS_WINDOWS = sys.platform.startswith("win")
IS_DOCKER = os.path.exists("/.dockerenv") or os.path.exists("/.docker-init")

RUNTIME_ENV = "production"
if IS_WINDOWS:
    RUNTIME_ENV = "Windows"
elif IS_DOCKER:
    RUNTIME_ENV = "Docker"
else:
    RUNTIME_ENV = "Linux"


def _generate_salt():
    """Generate a random salt for PIN hashing."""
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16))


def reset_admin_pin(new_pin):
    """
    Reset the admin PIN to a new value.
    Returns (success, message) tuple.
    """
    if not new_pin or len(new_pin) < 4:
        return False, "PIN must be at least 4 characters long"
    
    try:
        # Load config first if not already loaded
        from config import load_config, _CONFIG as global_config
        cfg = load_config()
        
        with _models_lock:
            salt = _generate_salt()
            pin_hash = hashlib.sha256(f"{salt}{new_pin}".encode()).hexdigest()
            cfg["adminPinSalt"] = salt
            cfg["adminPinHash"] = pin_hash
            # Clear any existing user PIN if it's in config
            # This ensures that if a user had set a PIN, it's cleared after admin reset
            if "pinHash" in cfg:
                del cfg["pinHash"]
            if "pinSalt" in cfg:
                del cfg["pinSalt"]
            # Also clear pinSetAt to ensure proper UI state
            if "pinSetAt" in cfg:
                del cfg["pinSetAt"]
            save_config(cfg)
        
        _log_security_event("pin_reset", {"message": "Admin PIN has been reset"})
        return True, "PIN reset successfully"
    except Exception as e:
        _log_security_event("pin_reset_failed", {"error": str(e)})
        return False, f"Failed to reset PIN: {str(e)}"


def main():
    """Main entry point for the server."""
    # Handle CLI arguments for PIN reset
    if len(sys.argv) >= 2 and sys.argv[1] == "reset-pin":
        new_pin = sys.argv[2] if len(sys.argv) > 2 else None
        if not new_pin:
            print("Usage: python main.py reset-pin <new_pin>")
            sys.exit(1)
        
        success, message = reset_admin_pin(new_pin)
        if success:
            print(f"[SUCCESS] {message}")
            sys.exit(0)
        else:
            print(f"[ERROR] {message}")
            sys.exit(1)
    
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