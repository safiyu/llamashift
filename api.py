#!/usr/bin/env python3
"""
API module for llama-shift server.
Handles HTTP API endpoints for model management, configuration, and control.
"""

import hmac
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

import proxy
from version import __version__, API_VERSION

from config import (
    DEFAULT_RUNTIME_PARAMS, HAS_SYSTEMD, IS_DOCKER, IS_WINDOWS,
    RUNTIME_ENV, STATIC_DIR,
)

# ─── Global state ──────────────────────────────────────────────────────────
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


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One thread per request, so a streaming /v1 call never blocks health checks or other requests."""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


# ─── HTTP API Handler Class ────────────────────────────────────────────────
class SwitcherAPIHandler(http.server.BaseHTTPRequestHandler):
    """HTTP API handler for llama-shift server."""

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def version_string(self):
        return 'llama-shift/0.1.0'

    def end_headers(self):
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
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Pin-Session, Authorization, X-API-Key')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # Routes that stay open when managementApiKey is set: liveness and the PIN login flow.
    AUTH_EXEMPT_PATHS = ("/api/health", "/api/version", "/api/pin", "/api/pin/status")

    def _management_auth_ok(self, path):
        """Gate /api/* behind the optional managementApiKey (Bearer, X-API-Key or a valid PIN session)."""
        from config import _CONFIG
        if not path.startswith("/api/") or path in self.AUTH_EXEMPT_PATHS:
            return True
        key = _CONFIG.get("managementApiKey")
        if not key:
            return True

        presented = self.headers.get("X-API-Key", "")
        auth = self.headers.get("Authorization", "")
        if not presented and auth.startswith("Bearer "):
            presented = auth[len("Bearer "):].strip()
        if presented and hmac.compare_digest(presented.encode("utf-8"), str(key).encode("utf-8")):
            return True

        from security import _PIN_SESSIONS, PIN_SESSION_TIMEOUT
        token = self.headers.get("X-Pin-Session")
        session = _PIN_SESSIONS.get(token) if token else None
        if session and time.time() - session["created_at"] < PIN_SESSION_TIMEOUT:
            return True

        body = json.dumps({"error": "Unauthorized: a valid management API key or PIN session is required"}).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    # Maximum request body accepted on the /v1/* gateway path. OpenAI
    # chat payloads are tiny; this exists purely to stop an unbounded
    # Content-Length from exhausting gateway memory.
    MAX_GATEWAY_BODY_BYTES = 64 * 1024 * 1024  # 64 MB

    def _dispatch_gateway_proxy(self, path, query):
        """Route /v1/* (and legacy /models) through the OpenAI proxy.

        Called from BOTH do_GET and do_POST — BaseHTTPRequestHandler
        dispatches strictly by method, so the proxy must be reachable
        from each one. Returns True when the request was handled.
        """
        if not (path in ("/v1/models", "/models") or path.startswith("/v1/")):
            return False

        body = b""
        if self.command in ("POST", "PUT", "PATCH"):
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                self.send_error_response(400, "Invalid Content-Length header")
                return True
            if content_length < 0:
                self.send_error_response(400, "Negative Content-Length header")
                return True
            if content_length > self.MAX_GATEWAY_BODY_BYTES:
                self.send_error_response(
                    413,
                    f"Payload exceeds {self.MAX_GATEWAY_BODY_BYTES} byte limit")
                return True
            if content_length > 0:
                body = self.rfile.read(content_length)

        try:
            proxy.handle_request(self, self.command, path, query, body)
        except Exception as e:
            # Never let a proxy failure kill the handler thread without
            # a client-visible response.
            print(f"[gateway] proxy error: {e}")
            try:
                self.send_error_response(502, f"Gateway error: {e}")
            except Exception:
                pass
        return True

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # ── Unified OpenAI gateway: /v1/* (and legacy /models) ──
        if self._dispatch_gateway_proxy(path, query):
            return

        if not self._management_auth_ok(path):
            return

        if path == "/api/health":
            self.handle_api_health()
        elif path == "/api/version":
            self.handle_api_version()
        elif path == "/api/active":
            self.handle_api_active()
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
        elif path == "/api/hub/search":
            self.handle_api_hub_search(query)
        elif path == "/api/hub/files":
            self.handle_api_hub_files(query)
        elif path == "/api/hub/downloads":
            self.handle_api_hub_downloads()
        elif path == "/api/vram/estimate":
            self.handle_api_vram_estimate(query)
        else:
            self.serve_static(path)

    def do_PUT(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if not self._management_auth_ok(path):
            return

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

        if not self._management_auth_ok(path):
            return

        if path == "/api/models":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
            except ValueError:
                self.send_error_response(400, "Invalid JSON body")
                return
            self.handle_api_model_patch(data)
        else:
            self.send_error_response(404, "Endpoint not found")

    def do_DELETE(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if not self._management_auth_ok(path):
            return

        if path == "/api/models":
            query = urllib.parse.parse_qs(parsed_url.query)
            model_id = query.get("model", [None])[0]
            self.handle_api_model_delete(model_id)
        else:
            self.send_error_response(404, "Endpoint not found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # ── Unified OpenAI gateway: POST /v1/chat/completions, /v1/completions,
        #    /v1/embeddings, /v1/tokenize, ... ──
        if self._dispatch_gateway_proxy(path, query):
            return

        if not self._management_auth_ok(path):
            return

        if path in ["/api/start", "/api/stop", "/api/stop_all", "/api/start_all",
                    "/api/config", "/api/mcp", "/api/restart", "/api/pin",
                    "/api/admin/reset-pin", "/api/models"]:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')

            data = {}
            if body:
                try:
                    data = json.loads(body)
                except ValueError:
                    self.send_error_response(400, "Invalid JSON body")
                    return

            handler_map = {
                "/api/start": lambda d: self.handle_api_start(d),
                "/api/stop": lambda d: self.handle_api_stop(d),
                "/api/stop_all": lambda d: self.handle_api_stop_all(),
                "/api/start_all": lambda d: self.handle_api_start_all(d),
                "/api/config": lambda d: self.handle_api_config_post(d),
                "/api/mcp": lambda d: self.handle_api_mcp_control(d),
                "/api/restart": lambda d: self.handle_api_restart(),
                "/api/pin": lambda d: self.handle_api_pin(d),
                "/api/models": lambda d: self.handle_api_models_post(d),
                "/api/admin/reset-pin": lambda d: self.handle_api_admin_reset_pin(),
            }
            handler = handler_map.get(path)
            if handler:
                handler(data)
        else:
            self.send_error_response(404, "Endpoint not found")

    # ─── Static file serving ─────────────────────────────────────────────
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
            ".ico": "image/x-icon",
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

    # ─── JSON helpers ────────────────────────────────────────────────────
    def send_json_response(self, data, code=200):
        response_bytes = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def send_error_response(self, code, message, extra=None):
        response_bytes = json.dumps({"error": message, **(extra or {})}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    # ─── Shared helpers (extracted from duplicated code) ─────────────────

    @staticmethod
    def _build_cmd(model_cfg, binary_path, data_dir, port, master_port=None):
        """Build the command-line list for launching a model."""
        mp = master_port if master_port is not None else port
        model_path = os.path.join(os.path.expanduser(data_dir), model_cfg["filename"])

        n_parallel = model_cfg.get("nParallel", 1)
        n_gpu_layers = model_cfg.get("nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])
        devices_list = model_cfg.get("devices", ["ROCm0"])
        device_str = ",".join(devices_list)

        cmd_args = [
            binary_path,
            "--model", model_path,
            "--device", device_str,
            "-ngl", str(n_gpu_layers),
            "--ctx-size", str(model_cfg["ctxSize"]),
            "-np", str(n_parallel),
            "--port", str(port),
            "--host", "0.0.0.0",
        ]

        # ── KV-cache quantization (per-model kvQuant: f16 | q8_0 | q4_0) ──
        kv_quant = (model_cfg.get("kvQuant") or "").strip().lower()
        if kv_quant in ("q8_0", "q4_0", "q8_1", "q5_0", "q5_1", "bf16", "f16"):
            if kv_quant != "f16":
                cmd_args.extend([
                    "--cache-type-k", kv_quant,
                    "--cache-type-v", kv_quant,
                ])

        # ── Flash attention (per-model flashAttention: bool) ──
        if model_cfg.get("flashAttention"):
            cmd_args.extend(["--flash-attn", "on"])

        # ── Speculative decoding: draft model + draft GPU layers ──
        draft = model_cfg.get("draftModel")
        if draft:
            expanded_draft = os.path.expanduser(str(draft).strip())
            if os.path.isabs(expanded_draft):
                draft_path = expanded_draft
            else:
                draft_path = os.path.join(
                    os.path.expanduser(data_dir), expanded_draft)
            if os.path.isfile(draft_path):
                cmd_args.extend(["--model-draft", draft_path])
                ngld = model_cfg.get("draftGpuLayers")
                if ngld is not None:
                    try:
                        cmd_args.extend(["--gpu-layers-draft", str(int(ngld))])
                    except (TypeError, ValueError):
                        pass
            else:
                print(f"[start] Warning: draft model not found at {draft_path} "
                      f"(skipping speculative decoding)")

        if "mmproj" in model_cfg:
            mmproj_path = os.path.join(os.path.expanduser(data_dir), model_cfg["mmproj"])
            cmd_args.extend(["--mmproj", mmproj_path])

        if "extraArgs" in model_cfg:
            cmd_args.extend(model_cfg["extraArgs"])

        return cmd_args, model_path

    @staticmethod
    def _wait_for_port_free(port, timeout=10):
        """Block until *port* is no longer bound, up to *timeout* seconds."""
        for _ in range(timeout * 2):  # 0.5 s steps
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                _sock.settimeout(1)
                _result = _sock.connect_ex(('127.0.0.1', int(port)))
                if _result != 0:
                    return
            finally:
                _sock.close()
            time.sleep(0.5)
        print(f"[port] Port {port} still in use after {timeout}s, proceeding anyway")

    @staticmethod
    def _cleanup_model_state(model_id, running, mode, master_port):
        """Kill a model process and remove it from all tracked state dicts.

        Returns the pid that was killed (or None).
        """
        from process import _cleanup_stale_tracked_processes, get_running_servers, is_llama_server
        from process import _model_processes, _proc_lock, _active_processes, _active_processes_lock

        _cleanup_stale_tracked_processes()
        running = running if running is not None else get_running_servers()

        pid = None
        if mode == "multi_port":
            port = model_id  # caller passes model_id as key into `running`
            if port in running:
                pid = running[port]["pid"]
        else:
            # single_port: find pid matching model_id
            with _proc_lock:
                for mid, proc in list(_model_processes.items()):
                    if mid == model_id and is_llama_server(proc.pid):
                        pid = proc.pid
                        break

        if pid and is_llama_server(pid):
            try:
                if IS_WINDOWS:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=10)
                else:
                    os.kill(pid, signal.SIGTERM)
                time.sleep(0.3)
                try:
                    os.kill(pid, 0)
                    if IS_WINDOWS:
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                       capture_output=True, timeout=10)
                except ProcessLookupError:
                    pass
            except Exception:
                pass

        with _proc_lock:
            _model_processes.pop(model_id, None)
            with _active_processes_lock:
                _active_processes.discard(model_id)
        return pid

    def _spawn_model(self, model_id, target_model, binary_path, data_dir, port,
                     is_single_port):
        """Spawn a single model process.

        Returns the Popen proc and log file handle on success. On failure it sends a
        JSON error and returns (None, None); a process that launched but could not be
        registered is killed so it never runs unmanaged.
        """
        from process import (
            _log_handles, _log_lock, _proc_lock, _active_processes_lock,
            _model_processes, _active_processes, get_process_env,
            read_last_lines,
        )

        cmd_args, model_path = self._build_cmd(
            target_model, binary_path, data_dir, port,
        )
        cmd_str = " ".join(cmd_args)
        print(f"[start] Model {model_id}: {cmd_str}")

        if not os.path.exists(model_path):
            self.send_error_response(400, f"Model file not found: {model_path}")
            return None, None

        running = self._get_running_servers()
        if port in running:
            self.send_error_response(400, f"Port {port} is already in use")
            return None, None

        def _forget():
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

        proc = None
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
                "env": get_process_env(binary_path),
            }
            if not IS_WINDOWS:
                proc_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd_args, **proc_kwargs)

            time.sleep(0.5)
            if proc.poll() is not None:
                _forget()
                tail = read_last_lines(target_model["logPath"], 8).strip()
                self.send_error_response(
                    500,
                    f"Process died immediately (exit code {proc.returncode}). "
                    f"Last log lines: {tail}",
                )
                return None, None

            with _proc_lock:
                _model_processes[model_id] = proc
                with _active_processes_lock:
                    _active_processes.add(model_id)

            return proc, log_file
        except Exception as e:
            if proc is not None:
                self._stop_owned_process(proc)
            _forget()
            self.send_error_response(500, f"Failed to spawn model: {str(e)}")
            return None, None

    @staticmethod
    def _stop_owned_process(proc):
        """Terminate and reap a process we just launched (its group on POSIX, since it has its own session)."""
        if proc.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    @staticmethod
    def _get_running_servers():
        """Lazy import + call to process.get_running_servers."""
        from process import get_running_servers
        return get_running_servers()

    # ─── API Handlers ────────────────────────────────────────────────────

    def handle_api_config_get(self):
        from config import _CONFIG, get_global_mode, get_ws_port, get_mcp_port
        binary_path = _CONFIG.get("binaryPath",
                                  "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")
        expanded_data_dir = os.path.expanduser(data_dir)

        self.send_json_response({
            "mode": get_global_mode(),
            "modes": ["single_port", "multi_port"],
            "appName": _CONFIG.get("appName", "llamashift"),
            "serviceName": _CONFIG.get("serviceName", "llamashift"),
            "masterPort": _CONFIG.get("masterPort", 9000),
            "wsPort": get_ws_port(),
            "mcpPort": get_mcp_port(),
            "managementAuthRequired": bool(_CONFIG.get("managementApiKey")),
            "binaryPath": binary_path,
            "binaryExists": os.path.isfile(binary_path)
                            and os.access(binary_path, os.X_OK),
            "dataDir": data_dir,
            "dataDirExists": os.path.isdir(expanded_data_dir),
            "dataDirModels": (sorted(os.listdir(expanded_data_dir))
                              if os.path.isdir(expanded_data_dir) else []),
            "runtimeEnv": RUNTIME_ENV,
            "hasSystemd": HAS_SYSTEMD,
            "isDocker": IS_DOCKER,
            "isWindows": IS_WINDOWS,
        })

    def handle_api_config_post(self, data):
        from config import save_mode, get_global_mode
        new_mode = data.get("mode")
        valid_modes = ["single_port", "multi_port"]

        if not new_mode or new_mode not in valid_modes:
            self.send_error_response(
                400, f"Invalid mode. Must be one of: {', '.join(valid_modes)}")
            return

        old_mode = get_global_mode()
        save_mode(new_mode)

        from process import get_running_servers
        running = get_running_servers()
        if running:
            self.send_json_response({
                "success": True,
                "mode": new_mode,
                "warning": (f"Mode changed from {old_mode} to {new_mode}, "
                            f"but {len(running)} model(s) are still running."),
            })
        else:
            self.send_json_response({
                "success": True,
                "mode": new_mode,
                "message": f"Mode switched from {old_mode} to {new_mode}",
            })

    def handle_api_active(self):
        """GET /api/active - return only currently running models with their endpoints.

        Designed for external apps (e.g. IDE extensions, scripts, companion UIs) to
        discover which models are live and which endpoint to route requests to.

        Response shape:
        {
            "active": [
                {
                    "id": "qwen36",
                    "name": "Qwen 3.6 35B A3B Vision",
                    "gpu": "AMD R9700",
                    "port": 9000,
                    "endpoint": "http://localhost:9000/v1",
                    "dedicated": false,
                    "pid": 13427,
                    "uptime": "00:05"
                },
                ...
            ]
        }
        """
        from config import get_global_mode, MODELS, _CONFIG
        from process import (
            get_running_servers, get_process_stats, get_process_cmdline,
            _process_matches_model, _cleanup_stale_tracked_processes,
        )

        mode = get_global_mode()
        _cleanup_stale_tracked_processes()
        running = get_running_servers()
        master_port = _CONFIG.get("masterPort", 9000)

        active = []
        for model_id, cfg in MODELS.items():
            is_running = False
            pid = None

            if mode == "single_port" and not cfg.get("dedicated"):
                # Shared model: only reachable via the master port.
                effective_port = master_port
                current_pid = running.get(master_port, {}).get("pid")
                if current_pid:
                    cmdline = get_process_cmdline(current_pid)
                    filename = cfg.get("filename", "")
                    if filename and filename in cmdline:
                        is_running = True
                        pid = current_pid
            else:
                effective_port = cfg.get("port", master_port)
                if effective_port in running:
                    candidate_pid = running[effective_port]["pid"]
                    cmdline = get_process_cmdline(candidate_pid)
                    is_running = _process_matches_model(cmdline, cfg, model_id)
                    pid = candidate_pid if is_running else None

            if not is_running:
                continue

            stats = get_process_stats(pid)
            active.append({
                "id":        model_id,
                "name":      cfg.get("name", model_id),
                "gpu":       cfg.get("gpu", ""),
                "devices":   cfg.get("devices", []),
                "port":      effective_port,
                "endpoint":  f"http://localhost:{effective_port}/v1",
                "dedicated": cfg.get("dedicated", False),
                "pid":       pid,
                "cpu":       stats["cpu"],
                "memory":    stats["mem"],
                "uptime":    stats["uptime"],
            })

        self.send_json_response({"active": active})

    def handle_api_status(self):
        from config import get_global_mode, MODELS, _CONFIG
        from process import (
            get_running_servers, get_host_stats, get_process_stats,
            get_process_cmdline, _process_matches_model,
            _cleanup_stale_tracked_processes, _mcp_process, _mcp_lock,
        )

        mode = get_global_mode()
        _cleanup_stale_tracked_processes()
        running = get_running_servers()
        host_stats = get_host_stats()

        status_list = []
        for model_id, cfg in MODELS.items():
            port = cfg["port"]
            is_running = False
            pid = None

            if mode == "single_port" and not cfg.get("dedicated"):
                master_port = _CONFIG.get("masterPort", 9000)
                current_pid = running.get(master_port, {}).get("pid")
                if current_pid:
                    cmdline = get_process_cmdline(current_pid)
                    filename = cfg.get("filename", "")
                    if filename and filename in cmdline:
                        is_running = True
                        pid = current_pid
            else:
                if port in running:
                    candidate_pid = running[port]["pid"]
                    cmdline = get_process_cmdline(candidate_pid)
                    is_running = _process_matches_model(cmdline, cfg, model_id)
                    pid = candidate_pid if is_running else None

            stats = {"cpu": 0.0, "mem": 0.0, "uptime": "Unknown"}
            if is_running:
                stats = get_process_stats(pid)

            shared = mode == "single_port" and not cfg.get("dedicated")
            status_list.append({
                **cfg,
                "effectivePort": _CONFIG.get("masterPort", 9000) if shared else port,
                "status": "running" if is_running else "stopped",
                "pid": pid,
                "cpu": stats["cpu"],
                "memory": stats["mem"],
                "uptime": stats["uptime"],
            })

        mcp_running = False
        mcp_pid = None
        with _mcp_lock:
            if _mcp_process and _mcp_process.poll() is None:
                mcp_running = True
                mcp_pid = _mcp_process.pid

        self.send_json_response({
            "mode": mode,
            "masterPort": _CONFIG.get("masterPort", 9000),
            "models": status_list,
            "host": host_stats,
            "mcp": {
                "status": "running" if mcp_running else "stopped",
                "pid": mcp_pid,
            },
        })

    def handle_api_gpu(self):
        from process import get_gpu_telemetry
        try:
            telemetry = get_gpu_telemetry()
        except Exception as e:
            print(f"[gpu] telemetry failed: {e}")
            telemetry = None
        self.send_json_response(telemetry if isinstance(telemetry, dict) else {"nvidia": None, "amd": None})

    def handle_api_devices(self):
        from process import get_available_devices
        devices = get_available_devices()
        self.send_json_response(devices)

    def handle_api_logs(self, query):
        from config import MODELS
        model_id = query.get("model", [None])[0]
        lines_count = int(query.get("lines", [100])[0])

        if not model_id or model_id not in MODELS:
            self.send_error_response(400,
                                     "Invalid or missing 'model' parameter")
            return

        config = MODELS[model_id]
        from process import read_last_lines
        logs = read_last_lines(config["logPath"], lines_count)

        self.send_json_response({"model": model_id, "logs": logs})

    def handle_api_mcp_status(self):
        from process import _mcp_process, _mcp_lock

        mcp_running = False
        mcp_pid = None
        with _mcp_lock:
            if _mcp_process and _mcp_process.poll() is None:
                mcp_running = True
                mcp_pid = _mcp_process.pid

        self.send_json_response({
            "status": "running" if mcp_running else "stopped",
            "pid": mcp_pid,
        })

    def handle_api_mcp_control(self, data):
        action = data.get("action")
        if not action:
            self.send_error_response(400, "Missing 'action' parameter")
            return

        if action == "start":
            from process import start_mcp_server
            if start_mcp_server():
                self.send_json_response(
                    {"success": True, "message": "MCP server started"})
            else:
                self.send_error_response(500, "Failed to start MCP server")
        elif action == "stop":
            from process import stop_mcp_server
            stop_mcp_server()
            self.send_json_response(
                {"success": True, "message": "MCP server stopped"})
        else:
            self.send_error_response(400, "Invalid action. Use 'start' or 'stop'")

    def handle_api_version(self):
        """Release and management-API versions; clients check apiVersion for compatibility."""
        self.send_json_response({"version": __version__, "apiVersion": API_VERSION})

    def handle_api_health(self):
        if is_server_ready():
            self.send_json_response({
                "status": "ok",
                "message": "LlamaShift server is running",
                "version": __version__,
                "apiVersion": API_VERSION,
            })
        else:
            self.send_json_response({
                "status": "starting",
                "message": "Server is initializing",
                "error": "Server starting",
                "version": __version__,
                "apiVersion": API_VERSION,
            }, code=503)

    # ─── Model lifecycle handlers ────────────────────────────────────────

    @staticmethod
    def _predict_outgoing_ids(mode, target_model, running):
        """Ids of the running models a start of *target_model* will stop (matched by filename)."""
        from config import MODELS, _CONFIG
        from process import get_process_cmdline

        master_port = _CONFIG.get("masterPort", 9000)
        if mode == "single_port":
            if target_model.get("dedicated", False):
                ports = [target_model.get("port", master_port)]
            else:
                ports = [p for p in running
                         if not any(m.get("dedicated") and m.get("port") == p for m in MODELS.values())]
        else:
            ports = [target_model["port"]]

        ids = []
        for p in ports:
            if p not in running:
                continue
            cmdline = get_process_cmdline(running[p]["pid"])
            for mid, cfg in MODELS.items():
                fname = cfg.get("filename", "")
                if fname and fname in cmdline:
                    ids.append(mid)
                    break
        return ids

    @staticmethod
    def _vram_check(model_id, target_model, outgoing_ids):
        """Estimate whether the model fits once the outgoing models stop; never raises.

        Returns (warnings, details). Telemetry or estimator failures yield no warnings,
        so they can never block a start.
        """
        try:
            import vram
            from config import MODELS, _CONFIG
            from process import get_free_vram_mib

            free = get_free_vram_mib()
            if not free:
                return [], {}
            data_dir = _CONFIG.get("dataDir", "~/models")
            cfg = dict(target_model)
            cfg.setdefault("dataDir", data_dir)
            outgoing = 0.0
            for mid in outgoing_ids:
                try:
                    ocfg = dict(MODELS[mid])
                    ocfg.setdefault("dataDir", data_dir)
                    outgoing += vram.estimate_vram_usage(ocfg)["total_mib"]
                except Exception:
                    pass
            result = vram.fit_warnings(model_id, cfg, free, outgoing)
            return result["warnings"], result
        except Exception as e:
            print(f"[vram] Fit check skipped for {model_id}: {e}")
            return [], {}

    def handle_api_start(self, data):
        """POST /api/start: swap in a model; swap marks are released once the new model is healthy."""
        from process import end_swap
        swap_ports = []
        handoff = []
        try:
            self._start_model(data, swap_ports, handoff)
        finally:
            if not handoff:
                for p in swap_ports:
                    end_swap(p)

    def _start_model(self, data, swap_ports, handoff):
        from config import MODELS, _CONFIG, get_global_mode
        from process import (
            get_process_cmdline, _process_matches_model,
            _cleanup_stale_tracked_processes, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, start_mcp_server,
            _log_handles, _log_lock, drain_and_kill, end_swap_when_healthy,
        )

        def kill_for_swap(swap_port, pid):
            swap_ports.append(swap_port)
            return drain_and_kill(swap_port, pid, model_id, restart)

        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(400,
                                     "Invalid or missing 'model' parameter")
            return

        mode = get_global_mode()
        target_model = MODELS[model_id]
        stopped_some = []

        binary_path = target_model.get(
            "binaryPath") or _CONFIG.get(
            "binaryPath",
            "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")

        # Validate before stopping anything, so a bad request never kills the running model
        if not os.path.isfile(binary_path):
            self.send_error_response(
                400,
                f"Binary not found: {binary_path}. Please check config.json "
                f"'binaryPath' or build llama.cpp.")
            return
        if not os.access(binary_path, os.X_OK):
            self.send_error_response(
                400,
                f"Binary not executable: {binary_path}. "
                f"Run: chmod +x {binary_path}")
            return
        model_path = os.path.join(os.path.expanduser(data_dir), target_model["filename"])
        if not os.path.exists(model_path):
            self.send_error_response(400, f"Model file not found: {model_path}")
            return

        pre_running = self._get_running_servers()
        outgoing_ids = self._predict_outgoing_ids(mode, target_model, pre_running)
        restart = model_id in outgoing_ids
        warnings, vram_info = self._vram_check(model_id, target_model, outgoing_ids)
        if warnings:
            print(f"[vram] {' '.join(warnings)}")
            if _CONFIG.get("strictVram", False) and not data.get("force", False):
                self.send_error_response(
                    409,
                    warnings[0] + " Send \"force\": true to start anyway, or set strictVram to false.",
                    extra={"warnings": warnings, **vram_info},
                )
                return

        if mode == "single_port":
            master_port = _CONFIG.get("masterPort", 9000)
            is_dedicated = target_model.get("dedicated", False)
            target_port = target_model.get("port", master_port) if is_dedicated else master_port

            _cleanup_stale_tracked_processes()
            running = self._get_running_servers()

            if is_dedicated:
                if target_port in running:
                    existing_pid = running[target_port]["pid"]
                    if kill_for_swap(target_port, existing_pid):
                        with _proc_lock:
                            for mid, p in list(_model_processes.items()):
                                try:
                                    if p.pid == existing_pid:
                                        if mid not in stopped_some:
                                            stopped_some.append(mid)
                                        _model_processes.pop(mid, None)
                                except Exception:
                                    pass
                ports_to_wait = [target_port]
            else:
                for _port, _info in list(running.items()):
                    is_other_dedicated = False
                    for mid, mcfg in MODELS.items():
                        if mcfg.get("dedicated") and mcfg.get("port") == _port:
                            is_other_dedicated = True
                            break
                    if is_other_dedicated:
                        continue  # preserve dedicated models on other GPUs

                    existing_pid = _info["pid"]
                    if kill_for_swap(_port, existing_pid):
                        with _proc_lock:
                            for mid, p in list(_model_processes.items()):
                                try:
                                    if p.pid == existing_pid:
                                        if mid not in stopped_some:
                                            stopped_some.append(mid)
                                        _model_processes.pop(mid, None)
                                except Exception:
                                    pass
                ports_to_wait = [p for p in running.keys() if not any(m.get("dedicated") and m.get("port") == p for m in MODELS.values())]

            if ports_to_wait:
                for p in ports_to_wait:
                    self._wait_for_port_free(p)

            if not is_dedicated:
                with _proc_lock:
                    for mid in list(_model_processes.keys()):
                        if not MODELS.get(mid, {}).get("dedicated"):
                            _model_processes.pop(mid, None)
                with _log_lock:
                    for mid in list(_log_handles.keys()):
                        if not MODELS.get(mid, {}).get("dedicated"):
                            handle = _log_handles.pop(mid, None)
                            if handle:
                                try:
                                    handle.close()
                                except OSError:
                                    pass

            port = target_port
        else:
            running = self._get_running_servers()
            port = target_model["port"]
            if port in running:
                for other_id, other_cfg in MODELS.items():
                    if other_id == model_id:
                        continue
                    if other_cfg["port"] == port and port in running:
                        pid = running[port]["pid"]
                        if kill_for_swap(port, pid):
                            stopped_some.append(other_id)
                            break

        if stopped_some:
            self._wait_for_port_free(port)

        proc, _ = self._spawn_model(model_id, target_model, binary_path,
                                    data_dir, port, mode == "single_port")
        if proc is None:
            return

        if swap_ports:
            end_swap_when_healthy(
                list(swap_ports), port, int(_CONFIG.get("coldStartTimeoutSeconds", 120)))
            handoff.append(True)

        self.send_json_response({
            "success": True,
            "message": f"Starting {target_model['name']}...",
            "stopped": stopped_some,
            "warnings": warnings,
        })

    def handle_api_stop(self, data):
        from config import MODELS, _CONFIG, get_global_mode
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock,
        )

        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(400,
                                     "Invalid or missing 'model' parameter")
            return

        mode = get_global_mode()
        target_model = MODELS[model_id]
        running = self._get_running_servers()

        if mode == "single_port":
            if target_model.get("dedicated"):
                port = target_model.get("port", 9000)
                if port not in running:
                    self.send_json_response(
                        {"success": True,
                         "message": f"Model {model_id} was already stopped."})
                    return
                pid = running[port]["pid"]
            else:
                master_port = _CONFIG.get("masterPort", 9000)
                if master_port in running:
                    pid = running[master_port]["pid"]
                else:
                    non_ded_pids = [info["pid"] for p, info in running.items() if not any(m.get("dedicated") and m.get("port") == p for m in MODELS.values())]
                    if not non_ded_pids:
                        self.send_json_response(
                            {"success": True,
                             "message": "No switched model is currently running."})
                        return
                    pid = non_ded_pids[0]
        else:
            port = target_model["port"]
            if port not in running:
                self.send_json_response(
                    {"success": True,
                     "message": f"Model {model_id} was already stopped."})
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
                    log_handle = _log_handles.pop(
                        actual_model_id or model_id, None)
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
                "stopped_model": actual_model_id or model_id,
            })
        else:
            self.send_error_response(500, f"Failed to stop model {model_id}.")

    def handle_api_stop_all(self):
        from config import MODELS
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock,
        )

        running = self._get_running_servers()
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
            "stopped_pids": stopped_pids,
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
        from config import MODELS, _CONFIG, get_global_mode
        from process import (
            _model_processes, _proc_lock, _active_processes,
            _active_processes_lock, _log_handles, _log_lock,
        )

        mode = get_global_mode()
        force = data.get("force", False)

        if mode != "multi_port" and not force:
            self.send_error_response(
                400,
                "Start All is only available in Multi Port mode. "
                "Toggle to Multi Port or set force=true.")
            return

        started = []
        failed = []

        binary_path = _CONFIG.get(
            "binaryPath",
            "/home/safiyu/llama.cpp/build/bin/llama-server")
        data_dir = _CONFIG.get("dataDir", "~/models")

        # Validate binary once
        if not os.path.isfile(binary_path):
            self.send_error_response(
                400,
                f"Binary not found: {binary_path}. "
                f"Please check config.json 'binaryPath' or build llama.cpp.")
            return

        for model_id, cfg in MODELS.items():
            port = cfg["port"]
            model_path = os.path.join(
                os.path.expanduser(data_dir), cfg["filename"])

            if not os.path.exists(model_path):
                failed.append(
                    {"model": model_id,
                     "reason": f"Model file not found: {model_path}"})
                continue

            model_binary = cfg.get("binaryPath") or binary_path
            cmd_args, _ = self._build_cmd(cfg, model_binary, data_dir, port)

            try:
                log_file = open(cfg["logPath"], "w", buffering=1)
                with _log_lock:
                    _log_handles[model_id] = log_file

                from process import get_process_env
                proc_kwargs = {
                    "stdout": log_file,
                    "stderr": subprocess.STDOUT,
                    "env": get_process_env(model_binary),
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
            "message": (f"Started {len(started)} model(s). "
                        f"{len(failed)} failed." if failed
                        else f"Started {len(started)} model(s)."),
        })

    # ─── Model CRUD handlers ─────────────────────────────────────────────

    def handle_api_models_get(self):
        from config import get_models
        self.send_json_response({"models": get_models()})

    def handle_api_models_post(self, data):
        from config import MODELS, _CONFIG, save_config, _models_lock, invalidate_models_cache
        model_id = data.get("id")
        if not model_id or not re.match(r'^[a-zA-Z0-9_]+$', model_id):
            self.send_error_response(
                400,
                "Invalid model id. Must be alphanumeric/underscore, "
                "e.g. 'qwen3_8b'")
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
                self.send_error_response(
                    409,
                    f"Port {port} is already assigned to model "
                    f"'{existing_id}'")
                return

        new_model = {
            "id": model_id,
            "name": data["name"],
            "desc": data.get("desc", ""),
            "filename": data["filename"],
            "port": port,
            "ctxSize": int(data.get(
                "ctxSize", DEFAULT_RUNTIME_PARAMS["ctxSize"])),
            "nParallel": int(data.get(
                "nParallel", DEFAULT_RUNTIME_PARAMS["nParallel"])),
            "nGpuLayers": int(data.get(
                "nGpuLayers", DEFAULT_RUNTIME_PARAMS["nGpuLayers"])),
            "batchSize": int(data.get(
                "batchSize", DEFAULT_RUNTIME_PARAMS["batchSize"])),
            "threads": int(data.get(
                "threads", DEFAULT_RUNTIME_PARAMS["threads"])),
            "temperature": float(data.get(
                "temperature", DEFAULT_RUNTIME_PARAMS["temperature"])),
            "maxTokens": int(data.get(
                "maxTokens", DEFAULT_RUNTIME_PARAMS["maxTokens"])),
            "topP": float(data.get(
                "topP", DEFAULT_RUNTIME_PARAMS["topP"])),
            "devices": data.get("devices", ["CPU0"]),
            "extraArgs": data.get("extraArgs", []),
            "endpoint": f"http://localhost:{port}/v1",
        }
        if data.get("mmproj"):
            new_model["mmproj"] = data["mmproj"]
        if data.get("binaryPath"):
            new_model["binaryPath"] = data["binaryPath"]
        if data.get("gpu"):
            new_model["gpu"] = data["gpu"]
        if data.get("size"):
            new_model["size"] = data["size"]
        if data.get("dedicated") is not None:
            new_model["dedicated"] = bool(data["dedicated"])

        # ── Optional inference knobs (KV-quant, flash-attn, speculative) ──
        kv_quant = str(data.get("kvQuant") or "").strip().lower()
        if kv_quant:
            new_model["kvQuant"] = kv_quant
        if data.get("flashAttention") is not None:
            new_model["flashAttention"] = bool(data["flashAttention"])
        if data.get("draftModel"):
            new_model["draftModel"] = str(data["draftModel"]).strip()
        if data.get("draftGpuLayers") is not None:
            try:
                new_model["draftGpuLayers"] = int(data["draftGpuLayers"])
            except (TypeError, ValueError):
                pass

        with _models_lock:
            _CONFIG["models"][model_id] = new_model
            invalidate_models_cache()
        save_config(_CONFIG)

        self.send_json_response({
            "success": True,
            "model": model_id,
            "config": dict(new_model),
            "message": f"Model '{model_id}' created successfully",
        })

    def handle_api_model_delete(self, model_id):
        from config import MODELS, _CONFIG, save_config, _models_lock, invalidate_models_cache
        from process import (
            _model_processes, _proc_lock, _log_handles, _log_lock,
            get_running_servers, _cleanup_stale_tracked_processes,
            is_llama_server, get_global_mode,
        )

        if not model_id or model_id not in MODELS:
            self.send_error_response(400,
                                     f"Invalid or missing model id: {model_id}")
            return

        running = get_running_servers()
        mode = get_global_mode()

        if mode == "multi_port" and model_id["port"] and model_id["port"] in running:
            self.send_error_response(
                409, f"Model '{model_id}' is currently running. "
                     f"Please stop it first.")
            return
        elif mode == "single_port":
            _cleanup_stale_tracked_processes()
            with _proc_lock:
                for mid, proc in list(_model_processes.items()):
                    if mid == model_id and is_llama_server(proc.pid):
                        self.send_error_response(
                            409, f"Model '{model_id}' is currently running. "
                                 f"Please stop it first.")
                        return

        with _models_lock:
            _CONFIG["models"].pop(model_id, None)
            invalidate_models_cache()
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
            "message": f"Model '{model_id}' deleted successfully",
        })

    # ─── PIN handlers ────────────────────────────────────────────────────

    def handle_api_pin(self, data=None):
        from security import (
            _is_rate_limited, _is_account_locked, _record_failed_attempt,
            _hash_pin, _PIN_SESSIONS, PIN_SESSION_TIMEOUT, _pin_failed_attempts,
        )

        client_ip = self.client_address[0]

        if self.command == 'GET':
            from config import _CONFIG
            pin_hash = _CONFIG.get("pinHash")
            if not pin_hash:
                self.send_json_response({
                    "success": True, "pinSet": False,
                    "message": "No PIN is currently set",
                })
                return

            session_token = self.headers.get("X-Pin-Session")
            session_valid = False
            if session_token and session_token in _PIN_SESSIONS:
                session_data = _PIN_SESSIONS[session_token]
                if (time.time() - session_data["created_at"]
                        < PIN_SESSION_TIMEOUT):
                    session_valid = True

            self.send_json_response({
                "success": True, "pinSet": True,
                "sessionValid": session_valid,
            })

        elif self.command == 'POST':
            if _is_rate_limited(client_ip):
                self.send_error_response(
                    429,
                    "Rate limit exceeded. Please try again later.")
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
                    self.send_error_response(
                        403,
                        "Account locked due to too many failed attempts. "
                        "Please try again later.")
                    return

                pin = data.get("pin", "")
                if not pin:
                    self.send_error_response(400, "Missing PIN")
                    return
                if not isinstance(pin, str):
                    self.send_error_response(400, "PIN must be a string")
                    return
                if not pin.isdigit():
                    self.send_error_response(
                        400, "PIN must contain only digits")
                    return
                if len(pin) < 4 or len(pin) > 12:
                    self.send_error_response(
                        400, "PIN must be between 4 and 12 digits")
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
                    session_token = str(uuid.uuid4())
                    _PIN_SESSIONS[session_token] = {
                        "created_at": time.time(), "ip": client_ip,
                    }
                    self.send_json_response({
                        "success": True, "sessionValid": True,
                        "sessionToken": session_token,
                        "message": "PIN verified successfully",
                    })
                else:
                    if _record_failed_attempt(client_ip, success=False):
                        self.send_error_response(
                            403,
                            "Account locked due to too many failed attempts. "
                            "Please try again later.")
                    else:
                        self.send_error_response(401, "Invalid PIN")

            elif action == "set":
                from config import _CONFIG, _config_lock, save_config
                pin = data.get("pin", "")
                if not pin:
                    self.send_error_response(400, "Missing PIN")
                    return
                if len(pin) < 4:
                    self.send_error_response(
                        400, "PIN must be at least 4 digits")
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
                    "message": "PIN set successfully",
                })

            elif action == "verify-session":
                session_token = data.get("sessionToken")
                if not session_token:
                    self.send_error_response(
                        400, "Missing session token")
                    return

                if session_token in _PIN_SESSIONS:
                    session_data = _PIN_SESSIONS[session_token]
                    if (time.time() - session_data["created_at"]
                            < PIN_SESSION_TIMEOUT):
                        self.send_json_response({
                            "success": True, "sessionValid": True})
                        return

                self.send_error_response(
                    401, "Invalid or expired session")

            elif action == "change":
                current_pin = data.get("currentPin", "")
                new_pin = data.get("newPin", "")

                if not current_pin or not new_pin:
                    self.send_error_response(
                        400, "Missing current PIN or new PIN")
                    return
                if len(new_pin) < 6:
                    self.send_error_response(
                        400, "New PIN must be at least 6 digits")
                    return

                salt = _CONFIG.get("pinSalt", "")
                current_hash = _hash_pin(current_pin, salt)
                stored_hash = _CONFIG.get("pinHash", "")

                if current_hash != stored_hash:
                    self.send_error_response(401, "Invalid current PIN")
                    return

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
                    "message": "PIN changed successfully",
                })

            else:
                self.send_error_response(
                    400,
                    "Invalid action. Use 'verify', 'set', "
                    "'verify-session', or 'change'")

    def handle_api_admin_reset_pin(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            new_pin = data.get("newPin")
            if not new_pin or len(new_pin) < 4:
                self.send_error_response(
                    400, "New PIN must be at least 4 characters long")
                return

            from main import reset_admin_pin
            success, message = reset_admin_pin(new_pin)

            if success:
                self.send_json_response(
                    {"success": True, "message": message})
            else:
                self.send_error_response(400, message)
        except json.JSONDecodeError:
            self.send_error_response(
                400, "Invalid JSON in request body")
        except Exception as e:
            self.send_error_response(500, f"Reset error: {str(e)}")

    # ─── Model import / export ───────────────────────────────────────────

    def handle_api_models_export(self):
        from config import get_models
        models = get_models()
        self.send_json_response({
            "success": True, "models": models,
            "count": len(models),
            "filename": "llamashift-models-export.json",
        })

    def handle_api_models_import_post(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            self.handle_api_models_import(data)
        except json.JSONDecodeError:
            self.send_error_response(
                400, "Invalid JSON in request body")
        except Exception as e:
            self.send_error_response(500, f"Import error: {str(e)}")

    def handle_api_models_import(self, data):
        models = data.get("models")
        if not models or not isinstance(models, dict):
            self.send_error_response(
                400,
                "Missing or invalid 'models' field. "
                "Expected a dict of model configurations.")
            return

        required = ["name", "filename", "port"]
        for model_id, model_cfg in models.items():
            for field in required:
                if not model_cfg.get(field):
                    self.send_error_response(
                        400,
                        f"Model '{model_id}': "
                        f"Missing required field '{field}'")
                    return

        ports_used = {}
        for model_id, model_cfg in models.items():
            port = model_cfg.get("port")
            if port in ports_used:
                self.send_error_response(
                    400,
                    f"Duplicate port {port} found in models "
                    f"'{ports_used[port]}' and '{model_id}'")
                return
            ports_used[port] = model_id

        from config import _CONFIG, _models_lock, invalidate_models_cache, save_config
        with _models_lock:
            _CONFIG["models"] = models
            invalidate_models_cache()
        save_config(_CONFIG)

        self.send_json_response({
            "success": True,
            "count": len(models),
            "message": f"Successfully imported {len(models)} model(s)",
        })

    # ─── PATCH /api/models ───────────────────────────────────────────────

    def handle_api_model_patch(self, data):
        from config import (
            MODELS, _CONFIG, save_config, _models_lock, invalidate_models_cache,
            get_global_mode,
        )
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock,
        )

        model_id = data.get("model")
        if not model_id or model_id not in MODELS:
            self.send_error_response(
                400, "Invalid or missing 'model' parameter")
            return

        updates = data.get("updates")
        if not updates or not isinstance(updates, dict):
            self.send_error_response(
                400, "Missing or invalid 'updates' object")
            return

        allowed = {
            "ctxSize", "nParallel", "nGpuLayers", "temperature",
            "maxTokens", "topP", "batchSize", "threads", "extraArgs",
            "name", "desc", "devices", "mmproj", "port",
            "binaryPath", "gpu", "dedicated",
            "kvQuant", "flashAttention", "draftModel", "draftGpuLayers",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            self.send_error_response(400, "No valid fields to update")
            return

        runtime_params = {
            "ctxSize", "nParallel", "nGpuLayers", "batchSize", "threads",
            "extraArgs", "devices", "mmproj", "port", "filename", "binaryPath",
            "kvQuant", "flashAttention", "draftModel", "draftGpuLayers",
        }
        requires_restart = bool(runtime_params & set(filtered.keys()))

        was_running = self._model_was_running(
            model_id, MODELS[model_id],
            "PATCH", requires_restart)

        with _models_lock:
            _CONFIG["models"][model_id].update(filtered)
            invalidate_models_cache()
        save_config(_CONFIG)

        response_data = {
            "success": True,
            "model": model_id,
            "updated": filtered,
            "config": dict(_CONFIG["models"][model_id]),
        }

        if was_running and requires_restart:
            self._schedule_restart(
                model_id, MODELS[model_id],
                "patch", response_data)

        self.send_json_response(response_data)

    # ─── PUT /api/models/{modelId} ───────────────────────────────────────

    def handle_api_model_put(self, model_id, data):
        from config import (
            MODELS, _CONFIG, save_config, _models_lock, invalidate_models_cache,
            get_global_mode,
        )
        from process import (
            get_running_servers, kill_process, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock, _log_handles, _log_lock,
        )

        if model_id not in MODELS:
            self.send_error_response(
                400, f"Invalid or missing model id: {model_id}")
            return

        allowed = {
            "name", "desc", "filename", "port", "ctxSize", "nParallel",
            "nGpuLayers", "batchSize", "threads", "temperature",
            "maxTokens", "topP", "devices", "extraArgs", "mmproj",
            "binaryPath", "gpu", "dedicated",
            "kvQuant", "flashAttention", "draftModel", "draftGpuLayers",
        }
        updates = {
            k: v for k, v in data.items()
            if k in allowed and k != "id"
        }

        if not updates:
            self.send_error_response(400, "No valid fields to update")
            return

        # Validate port uniqueness
        if "port" in updates:
            new_port = int(updates["port"])
            for existing_id, existing_cfg in MODELS.items():
                if (existing_id != model_id
                        and existing_cfg.get("port") == new_port):
                    self.send_error_response(
                        409,
                        f"Port {new_port} is already assigned to model "
                        f"'{existing_id}'")
                    return

        runtime_params = {
            "ctxSize", "nParallel", "nGpuLayers", "batchSize", "threads",
            "extraArgs", "devices", "mmproj", "port", "filename", "binaryPath",
            "kvQuant", "flashAttention", "draftModel", "draftGpuLayers",
        }
        requires_restart = bool(runtime_params & set(updates.keys()))

        was_running = self._model_was_running(
            model_id, MODELS[model_id],
            "PUT", requires_restart)

        with _models_lock:
            _CONFIG["models"][model_id].update(updates)
            invalidate_models_cache()
        save_config(_CONFIG)

        response_data = {
            "success": True,
            "model": model_id,
            "updated": updates,
            "config": dict(_CONFIG["models"][model_id]),
        }

        if was_running and requires_restart:
            self._schedule_restart(
                model_id, MODELS[model_id],
                "put", response_data)

        self.send_json_response(response_data)

    # ─── File listing ────────────────────────────────────────────────────

    def handle_api_files(self):
        from config import _CONFIG
        data_dir = _CONFIG.get("dataDir", "~/models")
        expanded = os.path.expanduser(data_dir)

        if not os.path.isdir(expanded):
            self.send_json_response({
                "files": [], "dataDir": data_dir,
                "dataDirExists": False,
            })
            return

        files = []
        for root, dirs, filenames in os.walk(expanded):
            depth = root[len(expanded):].count(os.sep)
            if depth > 2:
                dirs.clear()
                continue
            for fn in sorted(filenames):
                lower = fn.lower()
                if (lower.endswith(".gguf")
                        or lower.endswith(".bin")
                        or lower.endswith(".safetensors")):
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
            "files": files, "dataDir": data_dir,
            "dataDirExists": True,
        })

    # ─── Hugging Face hub + VRAM estimator ───────────────────────────────

    def handle_api_hub_search(self, query):
        """GET /api/hub/search?q=...&limit=N - search the HF hub for GGUF repos."""
        import downloader
        q = (query.get("q", [""])[0] or "").strip()
        if not q:
            self.send_error_response(400, "Missing 'q' search query")
            return
        try:
            limit = int(query.get("limit", ["20"])[0])
        except (ValueError, TypeError):
            limit = 20
        limit = max(1, min(limit, 100))
        try:
            results = downloader.search_hub(q, limit)
        except Exception as e:
            print(f"[hub] search failed: {e}")
            self.send_error_response(502, f"Hub search failed: {e}")
            return
        self.send_json_response({"results": results, "count": len(results)})

    def handle_api_hub_files(self, query):
        """GET /api/hub/files?repo=owner/name - list gguf/mmproj files in a repo."""
        import downloader
        repo = (query.get("repo", [""])[0] or "").strip()
        if not repo:
            self.send_error_response(400, "Missing 'repo' parameter")
            return
        try:
            files = downloader.list_repo_files(repo)
        except Exception as e:
            print(f"[hub] file listing failed for {repo}: {e}")
            self.send_error_response(502, f"File listing failed: {e}")
            return
        self.send_json_response({"repo": repo, "files": files, "count": len(files)})

    def handle_api_hub_downloads(self):
        """GET /api/hub/downloads - report in-flight/recent download tasks."""
        import downloader
        try:
            tasks = downloader.get_tasks_status(include_finished=True)
        except Exception as e:
            print(f"[hub] download status failed: {e}")
            self.send_error_response(502, f"Download status failed: {e}")
            return
        self.send_json_response({"downloads": tasks, "count": len(tasks)})

    def handle_api_vram_estimate(self, query):
        """GET /api/vram/estimate?model=<id>&ctx=N&ngl=N - VRAM footprint breakdown.

        ctx/ngl are optional overrides; they default to the model's stored
        values. Returns a MiB breakdown (weights / KV cache / overhead) plus
        a recommended layer count for the currently free VRAM.
        """
        import vram
        from config import MODELS, _CONFIG
        from process import get_gpu_telemetry

        model_id = (query.get("model", [""])[0] or "").strip()
        if not model_id or model_id not in MODELS:
            self.send_error_response(400, "Missing or unknown 'model' id")
            return

        cfg = dict(MODELS[model_id])
        cfg.setdefault("dataDir", _CONFIG.get("dataDir", "~/models"))

        ctx_size = None
        n_gpu_layers = None
        try:
            if query.get("ctx"):
                ctx_size = int(query["ctx"][0])
            if query.get("ngl"):
                n_gpu_layers = int(query["ngl"][0])
        except (ValueError, TypeError):
            self.send_error_response(400, "ctx and ngl must be integers")
            return

        try:
            estimate = vram.estimate_vram_usage(
                cfg, ctx_size, n_gpu_layers)
        except Exception as e:
            print(f"[vram] estimate failed for {model_id}: {e}")
            self.send_error_response(502, f"VRAM estimate failed: {e}")
            return

        # Attach an auto-tune suggestion when we can read GPU memory.
        free_mb = 0.0
        try:
            tele = get_gpu_telemetry() or {}
            for vendor in ("nvidia", "amd"):
                gpu = tele.get(vendor)
                if not isinstance(gpu, dict):
                    continue
                total = float(gpu.get("mem_total", 0) or 0)
                used = float(gpu.get("mem_used", 0) or 0)
                if total > 0:
                    free_mb += max(0.0, total - used)
        except Exception:
            free_mb = 0.0

        recommendation = None
        if free_mb > 0:
            try:
                recommendation = vram.calculate_optimal_configuration(
                    cfg, free_mb, free_mb)
            except Exception as e:
                print(f"[vram] optimize failed for {model_id}: {e}")

        self.send_json_response({
            "model": model_id,
            "free_vram_mib": round(free_mb, 1),
            "estimate": estimate,
            "recommendation": recommendation,
        })

    # ─── Restart ─────────────────────────────────────────────────────────

    def handle_api_restart(self):
        from process import (
            stop_mcp_server, _stop_all_model_processes,
            _close_all_log_handles,
        )

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
            "message": (f"Restart initiated via {restart_method}. "
                        f"Connection will temporarily drop."),
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
                        subprocess.Popen(
                            restart_cmd,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                    else:
                        subprocess.Popen(
                            restart_cmd, start_new_session=True)
                    print("[restart] New server process spawned.")
                except Exception as e:
                    print(f"[restart] Failed to spawn new server: {e}")

            print("[restart] Exiting current process...")
            os._exit(0)

        threading.Thread(target=do_restart, daemon=True).start()

    # ─── Private helpers (shared between patch/put) ──────────────────────

    def _model_was_running(self, model_id, model_cfg, method, requires_restart):
        """Return True if *model_id* was running and needs a restart."""
        from config import get_global_mode
        from process import (
            get_running_servers, _cleanup_stale_tracked_processes,
            is_llama_server, _model_processes, _proc_lock,
        )

        if not requires_restart:
            return False

        running = get_running_servers()
        mode = get_global_mode()
        old_port = model_cfg.get("port")

        if mode == "multi_port" and old_port and old_port in running:
            return True
        elif mode == "single_port":
            _cleanup_stale_tracked_processes()
            with _proc_lock:
                for mid, proc in list(_model_processes.items()):
                    if mid == model_id and is_llama_server(proc.pid):
                        return True
        return False

    def _schedule_restart(self, model_id, new_model_cfg, method_tag, response_data):
        """Schedule a model restart in a background thread after config save."""
        from config import MODELS, _CONFIG, get_global_mode
        from process import (
            get_running_servers, kill_process, _cleanup_stale_tracked_processes,
            is_llama_server, _model_processes, _proc_lock,
            _active_processes, _active_processes_lock,
            _log_handles, _log_lock,
        )

        def do_restart():
            time.sleep(0.5)
            try:
                running = get_running_servers()
                mode = get_global_mode()
                new_port = new_model_cfg.get("port")

                # Stop existing process
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
                                except Exception:
                                    pass
                        with _log_lock:
                            handle = _log_handles.pop(model_id, None)
                            if handle:
                                try:
                                    handle.close()
                                except OSError:
                                    pass

                port = new_port if mode == "multi_port" else _CONFIG.get(
                    "masterPort", 9000)

                self._wait_for_port_free(port)

                binary_path = new_model_cfg.get(
                    "binaryPath") or _CONFIG.get(
                    "binaryPath",
                    "/home/safiyu/llama.cpp/build/bin/llama-server")
                data_dir = _CONFIG.get("dataDir", "~/models")
                cmd_args, model_path = self._build_cmd(
                    new_model_cfg, binary_path, data_dir, port)

                log_file = open(new_model_cfg["logPath"], "w", buffering=1)
                with _log_lock:
                    _log_handles[model_id] = log_file

                from process import get_process_env
                proc_kwargs = {
                    "stdout": log_file,
                    "stderr": subprocess.STDOUT,
                    "env": get_process_env(binary_path),
                }
                if not IS_WINDOWS:
                    proc_kwargs["start_new_session"] = True

                proc = subprocess.Popen(cmd_args, **proc_kwargs)
                with _proc_lock:
                    _model_processes[model_id] = proc
                    with _active_processes_lock:
                        _active_processes.add(model_id)

                print(f"[{method_tag}_restart] "
                      f"Model {model_id} restarted with new config")
            except Exception as e:
                print(f"[{method_tag}_restart] "
                      f"Failed to restart model {model_id}: {e}")
                with _log_lock:
                    _log_handles.pop(model_id, None)
                with _proc_lock:
                    _model_processes.pop(model_id, None)

        threading.Thread(target=do_restart, daemon=True).start()
        response_data["restarted"] = True
        response_data["message"] = (
            "Configuration saved. Model is restarting with new settings.")


# ─── Top-level imports that need module-level state ────────────────────────
from http.server import BaseHTTPRequestHandler
import signal


def _human_size(size_bytes):
    """Convert bytes to human-readable size (e.g., 1.5 GB)."""
    if size_bytes == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
