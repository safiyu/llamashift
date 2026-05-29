#!/usr/bin/env python3
"""
Telemetry module for llama-shift server.
Handles WebSocket broadcasting of system and GPU telemetry.
"""

import asyncio
import json
import threading
import time

# Connected WebSocket clients subscribed to telemetry updates
_ws_clients = set()
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
    from config import get_global_mode, MODELS, _CONFIG
    from process import (
        get_running_servers, get_host_stats, get_gpu_telemetry,
        get_process_cmdline, _process_matches_model, _cleanup_stale_tracked_processes,
        _mcp_process, _mcp_lock
    )
    
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
    from config import get_global_mode, MODELS, _CONFIG
    from process import (
        get_running_servers, get_host_stats, get_gpu_telemetry,
        get_process_cmdline, _process_matches_model, _cleanup_stale_tracked_processes,
        get_process_stats, _mcp_process, _mcp_lock
    )
    
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
                cmdline = get_process_cmdline(current_pid)
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


def add_ws_client(client):
    """Add a WebSocket client to the broadcast list."""
    with _ws_lock:
        _ws_clients.add(client)


def remove_ws_client(client):
    """Remove a WebSocket client from the broadcast list."""
    with _ws_lock:
        _ws_clients.discard(client)


def get_ws_client_count():
    """Get the number of connected WebSocket clients."""
    with _ws_lock:
        return len(_ws_clients)