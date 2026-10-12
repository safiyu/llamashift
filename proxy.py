#!/usr/bin/env python3
"""
Unified OpenAI-compatible reverse proxy for llama-shift.

Exposes /v1/* on the gateway port (default 8002):
  * GET  /v1/models            - list all configured models
  * POST /v1/chat/completions  - proxy to the target model (auto cold-start)
  * POST /v1/completions       - legacy completions proxy
  * POST /v1/embeddings        - embeddings proxy
  * POST /v1/tokenize / /v1/untokenize / /v1/pass-through - llama.cpp extras

Cold-start behaviour:
  * If the requested model is not running, the incoming request is buffered
    while the gateway spawns it (respecting single/multi-port mode), then
    health-polls http://127.0.0.1:<port>/health with exponential backoff
    before piping the request through.
  * Concurrent requests for the same model are serialized via a per-model
    spawn lock + Event, so only one spawn/health-check happens at a time.
  * SSE responses are streamed in fixed chunks straight from the upstream
    socket to the client (no full-response buffering).
"""

import base64
import hashlib
import http.client
import json
import threading
import time
import urllib.parse

# ─── Per-model spawn serialization ────────────────────────────────────────
_spawn_locks = {}          # model_id -> threading.Lock
_spawn_locks_guard = threading.Lock()
_spawn_events = {}         # model_id -> threading.Event (set when spawn done)
_spawn_errors = {}         # model_id -> last spawn error message
_spawn_inflight = {}       # model_id -> True while a spawn is in flight


def _get_spawn_lock(model_id):
    with _spawn_locks_guard:
        lk = _spawn_locks.get(model_id)
        if lk is None:
            lk = threading.Lock()
            _spawn_locks[model_id] = lk
            _spawn_events[model_id] = threading.Event()
        return lk


def _spawn_error(model_id):
    return _spawn_errors.get(model_id)


def _clear_spawn_error(model_id):
    _spawn_errors.pop(model_id, None)


# ─── Public API ───────────────────────────────────────────────────────────

class ProxyError(Exception):
    """Proxy error with an HTTP status code."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    return handler.rfile.read(length)


def _auth_ok(handler):
    """Validate the optional openAiApiKey against the Authorization header."""
    from config import _CONFIG
    api_key = _CONFIG.get("openAiApiKey")
    if not api_key:
        return True
    header = handler.headers.get("Authorization", "")
    token = ""
    if header.startswith("Bearer "):
        token = header[len("Bearer "):].strip()
    elif header:
        token = header.strip()
    # Constant-time compare
    a = hashlib.sha256(token.encode()).digest()
    b = hashlib.sha256(api_key.encode()).digest()
    ok = hashlib.sha256(a + b).digest()
    return ok == hashlib.sha256(b + a).digest()


def _auth_error_response(handler):
    body = json.dumps({
        "error": {
            "message": "Invalid API key",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }).encode("utf-8")
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("WWW-Authenticate", 'Bearer')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _openai_error(handler, status, message, etype="invalid_request_error", code=None, headers=None):
    err = {"message": message, "type": etype, "param": None}
    if code:
        err["code"] = code
    body = json.dumps({"error": err}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def resolve_target_model(model_name):
    """Map an OpenAI 'model' string to (model_id, config).

    Matches by: exact model id, case-insensitive id, 'name', 'filename'
    (with or without the .gguf extension), or the /v1/models id alias.
    """
    from config import get_models
    models = get_models()
    if not model_name:
        raise ProxyError(400, "Missing 'model' field in request body")

    name = model_name.strip()
    name_l = name.lower()

    # 1. exact id
    if name in models:
        return name, models[name]
    # 2. case-insensitive id
    for mid, cfg in models.items():
        if mid.lower() == name_l:
            return mid, cfg
    # 3. name / filename / basename
    for mid, cfg in models.items():
        if cfg.get("name", "").lower() == name_l:
            return mid, cfg
        fname = cfg.get("filename", "")
        if fname:
            base = fname.rsplit("/", 1)[-1]
            if base.lower() == name_l or base.lower().removesuffix(".gguf") == name_l:
                return mid, cfg
    raise ProxyError(404, f"Model '{model_name}' not found in registry. "
                          "Known models: " + ", ".join(models.keys()))


def _get_port_for_model(model_id, model_cfg):
    """Port the model will listen on, given the global mode."""
    from config import _CONFIG, get_global_mode
    mode = get_global_mode()
    master_port = _CONFIG.get("masterPort", 9000)
    if mode == "single_port" and not model_cfg.get("dedicated"):
        return master_port
    return int(model_cfg.get("port") or master_port)


def _is_model_running(model_id, model_cfg):
    from config import _CONFIG, get_global_mode
    from process import (
        get_running_servers, get_process_cmdline, _process_matches_model,
        _cleanup_stale_tracked_processes,
    )
    _cleanup_stale_tracked_processes()
    running = get_running_servers()
    mode = get_global_mode()
    master_port = _CONFIG.get("masterPort", 9000)

    if mode == "single_port" and not model_cfg.get("dedicated"):
        current_pid = running.get(master_port, {}).get("pid")
        if not current_pid:
            return False
        cmdline = get_process_cmdline(current_pid)
        fname = model_cfg.get("filename", "")
        return bool(fname) and fname in cmdline
    port = int(model_cfg.get("port") or master_port)
    if port not in running:
        return False
    pid = running[port]["pid"]
    cmdline = get_process_cmdline(pid)
    return _process_matches_model(cmdline, model_cfg, model_id)


def _wait_healthy(port, timeout):
    """Poll http://127.0.0.1:port/health until 200 or timeout. Exponential backoff."""
    delay = 0.25
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
    return False


def ensure_model_running(model_id, model_cfg, timeout=None):
    """Make sure the model is up; returns the port it is serving on.

    Raises ProxyError(503/400) on failure. Concurrent calls for the same
    model serialize on a per-model lock; waiters then re-check health.
    """
    from config import _CONFIG
    from process import _model_last_activity, touch_model_activity

    if timeout is None:
        timeout = int(_CONFIG.get("coldStartTimeoutSeconds", 120))

    port = _get_port_for_model(model_id, model_cfg)

    if _is_model_running(model_id, model_cfg):
        touch_model_activity(model_id, port)
        return port

    lock = _get_spawn_lock(model_id)
    with lock:
        # Re-check inside the lock (another request may have finished the spawn).
        if _is_model_running(model_id, model_cfg):
            touch_model_activity(model_id, port)
            return port

        _spawn_inflight[model_id] = True
        _clear_spawn_error(model_id)
        ev = _spawn_events[model_id]
        ev.clear()

        def _do_spawn():
            from api import SwitcherAPIHandler
            from config import _CONFIG as C, get_global_mode
            from process import (
                _model_processes, _proc_lock, _active_processes,
                _active_processes_lock, _log_handles, _log_lock,
                get_process_env, _cleanup_stale_tracked_processes,
                begin_swap, end_swap, drain_and_kill,
            )
            import os
            import socket
            import subprocess

            swap_marks = []
            try:
                mode = get_global_mode()
                master_port = C.get("masterPort", 9000)
                target_port = int(model_cfg.get("port") or master_port) if model_cfg.get("dedicated") else (
                    master_port if mode == "single_port" else int(model_cfg.get("port") or master_port))

                _cleanup_stale_tracked_processes()

                # Tear down anything occupying the target port, after in-flight requests drain.
                from process import get_running_servers, kill_process, wait_for_port_free
                running = get_running_servers()

                try:
                    outgoing = SwitcherAPIHandler._predict_outgoing_ids(mode, model_cfg, running)
                    advice, _ = SwitcherAPIHandler._vram_check(model_id, model_cfg, outgoing)
                    if advice:
                        print(f"[vram] {' '.join(advice)}")
                except Exception:
                    pass

                begin_swap(target_port, model_id)
                swap_marks.append(target_port)
                if target_port in running:
                    drain_and_kill(target_port, running[target_port]["pid"], model_id)
                # single-port: also free the shared master port if different
                if mode == "single_port" and target_port == master_port:
                    for p, info in list(running.items()):
                        if p != target_port:
                            is_other_dedicated = any(
                                m.get("dedicated") and m.get("port") == p
                                for m in get_models_snapshot().values())
                            if not is_other_dedicated:
                                swap_marks.append(p)
                                drain_and_kill(p, info["pid"], model_id)
                                break
                wait_for_port_free(target_port)

                binary_path = model_cfg.get("binaryPath") or C.get(
                    "binaryPath", "/home/safiyu/llama.cpp/build/bin/llama-server")
                data_dir = C.get("dataDir", "~/models")

                # Build command via the shared static helper (includes draft/KV flags)
                cmd_args, model_path = SwitcherAPIHandler._build_cmd(
                    model_cfg, binary_path, data_dir, target_port)

                if not os.path.exists(model_path):
                    raise ProxyError(400, f"Model file not found: {model_path}")
                if not os.path.isfile(binary_path):
                    raise ProxyError(400, f"Binary not found: {binary_path}")

                log_dir = os.path.dirname(model_cfg.get("logPath", ""))
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
                log_file = open(model_cfg["logPath"], "w", buffering=1)
                with _log_lock:
                    _log_handles[model_id] = log_file

                proc_kwargs = {
                    "stdout": log_file,
                    "stderr": subprocess.STDOUT,
                    "env": get_process_env(binary_path),
                }
                if not _is_windows():
                    proc_kwargs["start_new_session"] = True

                proc = subprocess.Popen(cmd_args, **proc_kwargs)
                time.sleep(0.5)
                if proc.poll() is not None:
                    raise ProxyError(
                        500, f"Model process died immediately (exit {proc.returncode}). "
                             f"Check logs for details.")

                with _proc_lock:
                    _model_processes[model_id] = proc
                    with _active_processes_lock:
                        _active_processes.add(model_id)

                # Health poll (blocking; runs in this worker thread).
                if not _wait_healthy(target_port, timeout):
                    kill_process(proc.pid)
                    with _proc_lock:
                        _model_processes.pop(model_id, None)
                        with _active_processes_lock:
                            _active_processes.discard(model_id)
                    raise ProxyError(
                        503, f"Model did not become healthy within {timeout}s. "
                             "It may be OOM or still loading weights.")

                touch_model_activity(model_id, target_port)
                print(f"[proxy] Model {model_id} is healthy on port {target_port}")
            except ProxyError as e:
                _spawn_errors[model_id] = e.message
                raise
            except Exception as e:
                _spawn_errors[model_id] = str(e)
                raise
            finally:
                for p in swap_marks:
                    end_swap(p)
                ev.set()
                _spawn_inflight[model_id] = False

        worker = threading.Thread(target=_do_spawn, daemon=True,
                                  name=f"cold-start-{model_id}")
        worker.start()
        from config import get_drain_timeout
        if not ev.wait(timeout + get_drain_timeout() + 5):
            _spawn_errors[model_id] = "Cold start timed out"

    err = _spawn_error(model_id)
    if err:
        status = 400 if "not found" in err.lower() else 503
        raise ProxyError(status, f"Cold start failed: {err}")

    if not _is_model_running(model_id, model_cfg):
        raise ProxyError(503, "Model failed to start")

    touch_model_activity(model_id, port)
    return port


def _is_windows():
    import sys
    return sys.platform.startswith("win")


# Clients retry after this many seconds when a swap rejects their request
SWAP_RETRY_AFTER_SECONDS = 5


def _swap_rejection(handler, model_id):
    """Reject a request whose model is being swapped out: 503 model_swapping + Retry-After."""
    _openai_error(
        handler, 503,
        f"Model '{model_id}' is being swapped out for another model; retry shortly.",
        etype="server_error", code="model_swapping",
        headers={"Retry-After": str(SWAP_RETRY_AFTER_SECONDS)},
    )


def get_models_snapshot():
    """Snapshot of the model registry (used by spawn cleanup logic)."""
    from config import get_models
    return get_models()


def touch_activity_for_model(model_id):
    """Update last-activity for a model regardless of running state."""
    from process import touch_model_activity
    try:
        _, cfg = resolve_target_model(model_id)
        touch_model_activity(model_id, _get_port_for_model(model_id, cfg))
    except Exception:
        pass


# ─── Request routing ─────────────────────────────────────────────────────

def handle_request(handler, method, path, query, body):
    """Route a /v1/* request. *body* must be the raw request bytes."""
    if not _auth_ok(handler):
        _auth_error_response(handler)
        return

    if path == "/v1/models" or path == "/models":
        if method != "GET":
            _openai_error(handler, 405, "Method not allowed")
            return
        handle_models_list(handler)
        return

    if method != "POST":
        _openai_error(handler, 405, "Method not allowed for this endpoint")
        return

    # Body endpoints
    if path in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings",
                "/v1/tokenize", "/v1/untokenize", "/v1/pass-through",
                "/chat/completions", "/completions", "/embeddings"):
        handle_completion_proxy(handler, path, body)
        return

    _openai_error(handler, 404, f"Unknown OpenAI endpoint: {path}",
                  etype="invalid_request_error")


def handle_models_list(handler):
    """GET /v1/models - OpenAI-compatible model list."""
    from config import get_models
    models = get_models()
    data = []
    for mid, cfg in models.items():
        data.append({
            "id": mid,
            "object": "model",
            "created": 0,
            "owned_by": "llamashift",
            "name": cfg.get("name", mid),
            "meta": {
                "filename": cfg.get("filename", ""),
                "port": cfg.get("port"),
                "gpu": cfg.get("gpu", ""),
            },
        })
    payload = {"object": "list", "data": data}
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _normalize_upstream_path(path):
    """Map gateway /v1/* paths to the upstream llama-server path."""
    p = path
    if p.startswith("/v1/"):
        return "/" + p[len("/v1/"):]
    return p


def handle_completion_proxy(handler, path, body):
    """Buffer the request, cold-start if needed, then pipe the response (SSE-aware)."""
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        _openai_error(handler, 400, "Invalid JSON body")
        return

    model_name = payload.get("model")
    try:
        model_id, model_cfg = resolve_target_model(model_name)
    except ProxyError as e:
        _openai_error(handler, e.status, e.message,
                      code="model_not_found" if e.status == 404 else None)
        return

    from process import is_swapped_out, try_begin_request, end_request, touch_model_activity
    if is_swapped_out(_get_port_for_model(model_id, model_cfg), model_id):
        _swap_rejection(handler, model_id)
        return

    try:
        port = ensure_model_running(model_id, model_cfg)
    except ProxyError as e:
        _openai_error(handler, e.status, e.message, etype="server_error")
        return

    # Counted for the whole upstream exchange so a swap can drain active streams
    if not try_begin_request(port, model_id):
        _swap_rejection(handler, model_id)
        return
    try:
        _forward(handler, path, body, port, model_id)
    finally:
        end_request(port)
        touch_model_activity(model_id, port)


def _forward(handler, path, body, port, model_id):
    touch_activity_for_model(model_id)

    # ── Forward to upstream ──
    upstream_path = _normalize_upstream_path(path)
    headers = {
        "Content-Type": "application/json",
    }
    # Pass through an upstream API key if configured
    from config import _CONFIG
    if _CONFIG.get("upstreamApiKey"):
        headers["Authorization"] = f"Bearer {_CONFIG['upstreamApiKey']}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=None)
        conn.request("POST", upstream_path, body=body, headers=headers)
        resp = conn.getresponse()
    except (http.client.HTTPException, OSError, ConnectionError) as e:
        _openai_error(handler, 502, f"Upstream request failed: {e}",
                      etype="server_error")
        return

    _pipe_response(handler, resp, conn)


def _pipe_response(handler, upstream_resp, conn):
    """Stream the upstream response to the client without full buffering."""
    is_stream = (upstream_resp.getheader("Transfer-Encoding", "") == "chunked"
                 or (upstream_resp.getheader("Content-Type", "")
                     .startswith("text/event-stream")))
    content_type = upstream_resp.getheader("Content-Type", "application/json")

    handler.send_response(upstream_resp.status)
    handler.send_header("Content-Type", content_type)
    if is_stream:
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
    else:
        cl = upstream_resp.getheader("Content-Length")
        if cl:
            handler.send_header("Content-Length", cl)
    handler.end_headers()

    try:
        while True:
            # read1 returns what has arrived; read(4096) would hold SSE tokens until 4 KB piled up
            chunk = upstream_resp.read1(4096)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
