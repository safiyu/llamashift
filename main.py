#!/usr/bin/env python3
"""
Main entry point for llama-shift server.
This file imports from modular components and sets up the HTTP/WebSocket servers.
"""

import asyncio
import os
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
    _hash_pin, _PIN_SESSIONS, PIN_SESSION_TIMEOUT
)
from process import (
    get_running_servers, kill_process, get_host_stats, get_process_stats,
    get_process_cmdline, _process_matches_model, _cleanup_stale_tracked_processes,
    start_mcp_server, stop_mcp_server,
    _model_processes, _proc_lock, _active_processes, _active_processes_lock,
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


def main():
    """Main entry point for the server."""
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