#!/usr/bin/env python3
"""
API module for llama-shift server.
Handles HTTP API endpoints for model management, configuration, and control.
"""

import asyncio
import http
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import websockets

from config import HAS_SYSTEMD, IS_WINDOWS, IS_DOCKER, DEFAULT_RUNTIME_PARAMS, STATIC_DIR

# Global state for server readiness
_server_ready = False
_server_ready_lock = threading.Lock()


def set_server_ready():
    """Mark the server as ready to accept requests."""
    global _server_ready
    with _server_ready_lock:
        _server_ready = True


def is_server_ready():
    """Check if the server is ready to accept requests."""
    with _server_ready_lock:
        return _server_ready


# ─── HTTP API Handler Class ────────────────────────────────────────────────
class SwitcherAPIHandler(http.server.BaseHTTPRequestHandler):
    """HTTP API handler for llama-shift server."""
    
    def log_message(self, format, *args):
        pass  # Suppress default logging
    
    def version_string(self):
        return 'llama-shift/0.1.0'
    
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
            self.handle_api_models_get()
        elif path == "/api/pin" or path == "/api/pin/status":
            self.handle_api_pin()
        elif path == "/api/devices":
            self.handle_api_devices()
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
    
    def do_DELETE(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        if path == "/api/models":
            query = urllib.parse.parse_qs(parsed_url.query)
            model_id = query.get("model", [None])[0]
            self.handle_api_model_delete(model_id)
        else:
            self.send_error_response(404, "Endpoint not found")
    
    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        if path in ["/api/start", "/api/stop", "/api/stop_all", "/api/start_all", 
                    "/api/config", "/api/mcp", "/api/restart", "/api/pin",
                    "/api/admin/reset-pin"]:
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
            elif path == "/api/models":
                self.handle_api_models_post(data)
            elif path == "/api/admin/reset-pin":
                self.handle_api_admin_reset_pin()
        else:
            self.send_error_response(404, "Endpoint not found")
    
    def serve_static(self, path):
        """Serve static files from the static directory."""
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
        """Send a JSON response."""
        response_bytes = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)
    
    def send_error_response(self, code, message):
        """Send an error JSON response."""
        response_bytes = json.dumps({"error": message}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)
    
    # ─── API Handlers ──────────────────────────────────────────────────
    
    def handle_api_config_get(self):
        """Returns current mode configuration plus app metadata and runtime info."""
        from config import _CONFIG, get_global_mode
        binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")
        expanded_data_dir = os.path.expanduser(data_dir)
        
        self.send_json_response({
            "mode": get_global_mode(),
            "modes": ["single_port", "multi_port"],
            "appName": _CONFIG.get("appName", "llamashift"),
            "serviceName": _CONFIG.get("serviceName", "llamashift"),
            "masterPort": _CONFIG.get("masterPort", 9000),
            "binaryPath": binary_path,
            "binaryExists": os.path.isfile(binary_path) and os.access(binary_path, os.X_OK),
            "dataDir": data_dir,
            "dataDirExists": os.path.isdir(expanded_data_dir),
            "dataDirModels": sorted(os.listdir(expanded_data_dir)) if os.path.isdir(expanded_data_dir) else [],
            "runtimeEnv": RUNTIME_ENV,
            "hasSystemd": HAS_SYSTEMD,
            "isDocker": IS_DOCKER,
            "isWindows": IS_WINDOWS,
        })
    
    def handle_api_config_post(self, data):
        """Toggles or sets the mode configuration."""
        from config import save_mode, get_global_mode
        new_mode = data.get("mode")
        valid_modes = ["single_port", "multi_port"]
        
        if not new_mode or new_mode not in valid_modes:
            self.send_error_response(400, f"Invalid mode. Must be one of: {', '.join(valid_modes)}")
            return
        
        old_mode = get_global_mode()
        save_mode(new_mode)
        
        from process import get_running_servers
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
        """Get current server status including model states and host stats."""
        from config import get_global_mode, MODELS
        from process import (
            get_running_servers, get_host_stats, get_process_stats,
            get_process_cmdline, _process_matches_model, _cleanup_stale_tracked_processes,
            _mcp_process, _mcp_lock
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
    
    def handle_api_devices(self):
        """Get available GPU devices for model assignment."""
        from process import get_available_devices
        devices = get_available_devices()
        self.send_json_response(devices)
    
    def handle_api_logs(self, query):
        """Get log file contents for a model."""
        from config import MODELS
        model_id = query.get("model", [None])[0]
        lines_count = int(query.get("lines", [100])[0])
        
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Invalid or missing 'model' parameter")
            return
        
        config = MODELS[model_id]
        from process import read_last_lines
        logs = read_last_lines(config["logPath"], lines_count)
        
        self.send_json_response({
            "model": model_id,
            "logs": logs
        })
    
    def handle_api_mcp_status(self):
        """Get MCP server status."""
        from process import _mcp_process, _mcp_lock
        
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
        """Start or stop MCP server."""
        action = data.get("action")
        if not action:
            self.send_error_response(400, "Missing 'action' parameter")
            return
        
        if action == "start":
            from process import start_mcp_server
            if start_mcp_server():
                self.send_json_response({"success": True, "message": "MCP server started"})
            else:
                self.send_error_response(500, "Failed to start MCP server")
        elif action == "stop":
            from process import stop_mcp_server
            stop_mcp_server()
            self.send_json_response({"success": True, "message": "MCP server stopped"})
        else:
            self.send_error_response(400, "Invalid action. Use 'start' or 'stop'")
    
    def handle_api_health(self):
        """Health check endpoint for restart flow."""
        if is_server_ready():
            self.send_json_response({
                "status": "ok",
                "message": "LlamaShift server is running"
            })
        else:
            self.send_error_response(503, "Server starting")
            response_bytes = json.dumps({"status": "starting", "message": "Server is initializing"}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)
    
    def handle_api_start(self, data):
        """Start a model."""
        from config import MODELS, _CONFIG, get_global_mode
        from process import (
            get_running_servers, kill_process, get_process_cmdline,
            _process_matches_model, _cleanup_stale_tracked_processes, is_llama_server,
            start_mcp_server, _model_processes, _proc_lock, _active_processes,
            _active_processes_lock, _log_handles, _log_lock, read_last_lines
        )
        
        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Invalid or missing 'model' parameter")
            return
        
        mode = get_global_mode()
        target_model = MODELS[model_id]
        stopped_some = []
        
        if mode == "single_port":
            master_port = _CONFIG.get("masterPort", 9000)
            
            _cleanup_stale_tracked_processes()
            running = get_running_servers()
            
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
            
            import socket
            ports_to_wait = list(running.keys())
            if ports_to_wait:
                for _wait_attempt in range(40):
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
            running = get_running_servers()
            port = target_model["port"]
            if port in running:
                for other_id, other_config in MODELS.items():
                    if other_id == model_id:
                        continue
                    if other_config["port"] == port and port in running:
                        pid = running[port]["pid"]
                        if kill_process(pid):
                            stopped_some.append(other_id)
                            break
        
        if stopped_some:
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
            else:
                print(f"[start] Port {port} still in use after 10s, proceeding anyway")
        
        binary_path = _CONFIG.get("binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")
        model_path = os.path.join(os.path.expanduser(data_dir), target_model["filename"])
        
        if not os.path.isfile(binary_path):
            self.send_error_response(400, f"Binary not found: {binary_path}. Please check config.json 'binaryPath' or build llama.cpp.")
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
            
            proc_kwargs = {
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
            }
            if not IS_WINDOWS:
                proc_kwargs["start_new_session"] = True
            
            proc = subprocess.Popen(cmd_args, **proc_kwargs)
            
            time.sleep(0.5)
            if proc.poll() is not None:
                stderr_output = proc.stderr.read() if proc.stderr else ""
                with _log_lock:
                    _log_handles.pop(model_id, None)
                with _proc_lock:
                    _model_processes.pop(model_id, None)
                    with _active_processes_lock:
                        _active_processes.discard(model_id)
                self.send_error_response(
                    500,
                    f"Process died immediately (exit code {proc.returncode}). Check logs for details."
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
        """Stop a model."""
        from config import MODELS, get_global_mode
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock
        )
        
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
        """Stop all running models."""
        from config import MODELS
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock
        )
        
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
        
        from process import _cleanup_stale_tracked_processes
        _cleanup_stale_tracked_processes()
    
    def handle_api_start_all(self, data):
        """Start all stopped models."""
        from config import MODELS, _CONFIG, get_global_mode
        from process import _model_processes, _proc_lock, _active_processes, _active_processes_lock, _log_handles, _log_lock
        
        mode = get_global_mode()
        force = data.get("force", False)
        
        if mode != "multi_port" and not force:
            self.send_error_response(400, "Start All is only available in Multi Port mode. Toggle to Multi Port or set force=true.")
            return
        
        started = []
        failed = []
        
        for model_id, config in MODELS.items():
            port = config["port"]
            
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
            
            try:
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
    
    def handle_api_models_get(self):
        """GET /api/models - list models."""
        from config import get_models
        self.send_json_response({"models": get_models()})
    
    def handle_api_models_post(self, data):
        """POST /api/models - create a new model entry."""
        from config import MODELS, _CONFIG, save_config, _models_lock, _models_cache
        import secrets
        
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
        for existing_id, existing_cfg in MODELS.items():
            if existing_cfg.get("port") == port:
                self.send_error_response(409, f"Port {port} is already assigned to model '{existing_id}'")
                return
        
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
        from config import MODELS, _CONFIG, save_config, _models_lock, _models_cache
        from process import _model_processes, _proc_lock, _log_handles, _log_lock
        
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, f"Invalid or missing model id: {model_id}")
            return
        
        from process import get_running_servers
        running = get_running_servers()
        mode = get_global_mode()
        
        if mode == "multi_port" and model_id["port"] and model_id["port"] in running:
            self.send_error_response(409, f"Model '{model_id}' is currently running. Please stop it first.")
            return
        elif mode == "single_port":
            from process import _cleanup_stale_tracked_processes, is_llama_server
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
        """Handle PIN-related API requests."""
        from security import (
            _is_rate_limited, _is_account_locked, _record_failed_attempt,
            _hash_pin, _PIN_SESSIONS, PIN_SESSION_TIMEOUT
        )
        
        client_ip = self.client_address[0]
        
        if self.command == 'GET':
            from config import _CONFIG
            pin_hash = _CONFIG.get("pinHash")
            if not pin_hash:
                self.send_json_response({
                    "success": True,
                    "pinSet": False,
                    "message": "No PIN is currently set"
                })
                return
            
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
            
            action = data.get("action", "")
            
            if action == "verify":
                if _is_account_locked(client_ip):
                    self.send_error_response(403, "Account locked due to too many failed attempts. Please try again later.")
                    return
                
                pin = data.get("pin", "")
                if not pin:
                    self.send_error_response(400, "Missing PIN")
                    return
                
                if not isinstance(pin, str):
                    self.send_error_response(400, "PIN must be a string")
                    return
                
                if not pin.isdigit():
                    self.send_error_response(400, "PIN must contain only digits")
                    return
                
                if len(pin) < 4 or len(pin) > 12:
                    self.send_error_response(400, "PIN must be between 4 and 12 digits")
                    return
                
                from config import _CONFIG
                pin_hash = _CONFIG.get("pinHash")
                if not pin_hash:
                    self.send_error_response(401, "No PIN is set")
                    return
                
                salt = _CONFIG.get("pinSalt", "")
                input_hash = _hash_pin(pin, salt)
                
                if input_hash == pin_hash:
                    _pin_failed_attempts[client_ip] = []
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
                    if _record_failed_attempt(client_ip, success=False):
                        self.send_error_response(403, "Account locked due to too many failed attempts. Please try again later.")
                    else:
                        self.send_error_response(401, "Invalid PIN")
            
            elif action == "set":
                from config import _CONFIG, _config_lock, save_config
                pin = data.get("pin", "")
                if not pin:
                    self.send_error_response(400, "Missing PIN")
                    return
                
                if len(pin) < 4:
                    self.send_error_response(400, "PIN must be at least 4 digits")
                    return
                
                import secrets
                salt = secrets.token_hex(16)
                pin_hash = _hash_pin(pin, salt)
                
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
                # Change PIN - verify current PIN first, then set new PIN
                current_pin = data.get("currentPin", "")
                new_pin = data.get("newPin", "")
                
                if not current_pin or not new_pin:
                    self.send_error_response(400, "Missing current PIN or new PIN")
                    return
                
                if len(new_pin) < 6:
                    self.send_error_response(400, "New PIN must be at least 6 digits")
                    return
                
                # Verify current PIN
                salt = _CONFIG.get("pinSalt", "")
                current_hash = _hash_pin(current_pin, salt)
                stored_hash = _CONFIG.get("pinHash", "")
                
                if current_hash != stored_hash:
                    self.send_error_response(401, "Invalid current PIN")
                    return
                
                # Set new PIN
                import secrets
                new_salt = secrets.token_hex(16)
                new_pin_hash = _hash_pin(new_pin, new_salt)
                
                from config import _config_lock, save_config
                with _config_lock:
                    _CONFIG["pinHash"] = new_pin_hash
                    _CONFIG["pinSalt"] = new_salt
                    _CONFIG["pinSetAt"] = int(time.time())
                
                save_config(_CONFIG)
                
                self.send_json_response({
                    "success": True,
                    "message": "PIN changed successfully"
                })
            
            else:
                self.send_error_response(400, "Invalid action. Use 'verify', 'set', 'verify-session', or 'change'")
    
    def handle_api_admin_reset_pin(self):
        """Reset admin PIN - requires admin PIN authentication."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            new_pin = data.get("newPin")
            if not new_pin or len(new_pin) < 4:
                self.send_error_response(400, "New PIN must be at least 4 characters long")
                return
            
            # Import and use the reset function from main
            from main import reset_admin_pin
            success, message = reset_admin_pin(new_pin)
            
            if success:
                self.send_json_response({
                    "success": True,
                    "message": message
                })
            else:
                self.send_error_response(400, message)
        except json.JSONDecodeError:
            self.send_error_response(400, "Invalid JSON in request body")
        except Exception as e:
            self.send_error_response(500, f"Reset error: {str(e)}")
    
    def handle_api_models_export(self):
        """Export all model configurations as JSON."""
        from config import get_models
        models = get_models()
        self.send_json_response({
            "success": True,
            "models": models,
            "count": len(models),
            "filename": "llamashift-models-export.json"
        })
    
    def handle_api_models_import_post(self):
        """POST handler for /api/models/import."""
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
        """Import model configurations from JSON."""
        models = data.get("models")
        if not models or not isinstance(models, dict):
            self.send_error_response(400, "Missing or invalid 'models' field. Expected a dict of model configurations.")
            return
        
        required = ["name", "filename", "port"]
        for model_id, model_cfg in models.items():
            for field in required:
                if not model_cfg.get(field):
                    self.send_error_response(400, f"Model '{model_id}': Missing required field '{field}'")
                    return
        
        ports_used = {}
        for model_id, model_cfg in models.items():
            port = model_cfg.get("port")
            if port in ports_used:
                self.send_error_response(400, f"Duplicate port {port} found in models '{ports_used[port]}' and '{model_id}'")
                return
            ports_used[port] = model_id
        
        from config import _CONFIG, _models_lock, _models_cache, save_config
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
        """PATCH /api/models - update model configuration."""
        from config import MODELS, _CONFIG, save_config, _models_lock, _models_cache, get_global_mode
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock
        )
        
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
        
        runtime_params = {"ctxSize", "nParallel", "nGpuLayers", "batchSize", "threads",
                          "extraArgs", "devices", "mmproj", "port", "filename"}
        requires_restart = bool(runtime_params & set(filtered.keys()))
        
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
            _CONFIG["models"][model_id].update(filtered)
            _models_cache = None
        save_config(_CONFIG)
        
        response_data = {
            "success": True,
            "model": model_id,
            "updated": filtered,
            "config": dict(_CONFIG["models"][model_id]),
        }
        
        if was_running and requires_restart:
            def restart_model_after_patch():
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
                    else:
                        print(f"[patch_restart] Port {port} still in use after 10s, proceeding anyway")
                    
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
        """GET /api/files - list model files in the dataDir."""
        from config import _CONFIG
        data_dir = _CONFIG.get("dataDir", "~/models")
        expanded = os.path.expanduser(data_dir)
        
        if not os.path.isdir(expanded):
            self.send_json_response({"files": [], "dataDir": data_dir, "dataDirExists": False})
            return
        
        files = []
        for root, dirs, filenames in os.walk(expanded):
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
        """Restart the server."""
        from process import stop_mcp_server, _stop_all_model_processes, _close_all_log_handles
        
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
            time.sleep(0.5)
            print(f"[restart] Restart via {restart_method}. Cleaning up...")
            stop_mcp_server()
            _stop_all_model_processes()
            _close_all_log_handles()
            
            if restart_method == "self":
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
