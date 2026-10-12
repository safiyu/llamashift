"""Unit tests for the gateway hardening fixes (no real model servers except a tiny fake)."""
import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

import api
import config
import process
import proxy
import vram
from api import SwitcherAPIHandler
from conftest import ROOT, free_port


class Headers(dict):
    """Case-insensitive stand-in for the request headers."""

    def __init__(self, items=None):
        super().__init__({k.lower(): v for k, v in (items or {}).items()})

    def get(self, key, default=None):
        return super().get(key.lower(), default)


class H(SwitcherAPIHandler):
    """Real handler class without a socket; records what it would send."""

    def __init__(self, headers=None, command="GET"):
        self.headers = Headers(headers)
        self.command = command
        self.client_address = ("127.0.0.1", 0)
        self.sent = []
        self.raw_headers = []
        self.status = None
        self.wfile = io.BytesIO()

    def send_json_response(self, data, code=200):
        self.sent.append((code, data))

    def send_error_response(self, code, message, extra=None):
        self.sent.append((code, {"error": message, **(extra or {})}))

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.raw_headers.append((key, value))

    def end_headers(self):
        pass


@pytest.fixture
def models(monkeypatch):
    """Install a model registry (the cache is what get_models() serves) plus config overrides."""
    def install(registry, **cfg):
        cache = {mid: {**c, "logPath": c.get("logPath") or os.path.join(config.LOG_DIR, f"{mid}.log")}
                 for mid, c in registry.items()}
        monkeypatch.setattr(config, "_models_cache", cache)
        monkeypatch.setitem(config._CONFIG, "models", registry)
        for key, value in cfg.items():
            monkeypatch.setitem(config._CONFIG, key, value)
    return install


def model_cfg(mid, port, **extra):
    return {"id": mid, "name": mid.upper(), "filename": f"{mid}.gguf", "port": port, "ctxSize": 2048,
            "nParallel": 1, "devices": ["ROCm0"], "dedicated": False, "gpu": "G",
            "endpoint": f"http://localhost:{port}/v1", **extra}


def find_pids(port):
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return [int(line.split(None, 1)[0]) for line in out.splitlines()[1:]
            if f"--port {port}" in line and "llama-server" in line]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


# ── 1/2: undefined names and spawn failure cleanup ───────────────────────

def test_no_undefined_names_in_runtime_modules():
    res = subprocess.run([sys.executable, "-m", "pyflakes", *(os.path.join(ROOT, f) for f in
                          ("api.py", "proxy.py", "process.py", "main.py", "config.py", "telemetry.py", "server.py"))],
                         capture_output=True, text=True)
    if "No module named pyflakes" in res.stderr:
        pytest.skip("pyflakes not installed")
    bad = [line for line in res.stdout.splitlines() if "undefined name" in line or "referenced before assignment" in line]
    assert bad == []


def spawn_args(tmp_path, fake_llama, mid="m1", **extra):
    data_dir = tmp_path / "models"
    data_dir.mkdir(exist_ok=True)
    (data_dir / f"{mid}.gguf").write_bytes(b"x")
    port = free_port()
    cfg = model_cfg(mid, port, logPath=str(tmp_path / f"{mid}.log"), **extra)
    return mid, cfg, fake_llama, str(data_dir), port


def test_spawn_registers_process_and_returns_it(tmp_path, fake_llama):
    mid, cfg, binary, data_dir, port = spawn_args(tmp_path, fake_llama)
    h = H()
    proc, log = h._spawn_model(mid, cfg, binary, data_dir, port, True)
    try:
        assert proc is not None and h.sent == []
        assert process._model_processes[mid] is proc and mid in process._active_processes
    finally:
        H._stop_owned_process(proc)
        process._model_processes.pop(mid, None)
        process._active_processes.discard(mid)
        process._log_handles.pop(mid, None)
        log.close()


def test_spawn_dies_immediately_gives_json_error_with_log_tail(tmp_path, fake_llama):
    mid, cfg, binary, data_dir, port = spawn_args(tmp_path, fake_llama, extraArgs=["--fake-exit", "3"])
    h = H()
    assert h._spawn_model(mid, cfg, binary, data_dir, port, True) == (None, None)
    code, body = h.sent[-1]
    assert code == 500 and "died immediately" in body["error"] and "simulated startup failure" in body["error"]
    assert mid not in process._model_processes and mid not in process._log_handles


def test_spawn_failure_after_launch_kills_the_process_it_started(tmp_path, fake_llama, monkeypatch):
    mid, cfg, binary, data_dir, port = spawn_args(tmp_path, fake_llama)

    def boom(_seconds):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "time", types.SimpleNamespace(sleep=boom, time=time.time))
    h = H()
    assert h._spawn_model(mid, cfg, binary, data_dir, port, True) == (None, None)
    code, body = h.sent[-1]
    assert code == 500 and "Failed to spawn model: boom" in body["error"]
    assert mid not in process._model_processes and mid not in process._log_handles
    deadline = time.time() + 5
    while time.time() < deadline and any(alive(p) for p in find_pids(port)):
        time.sleep(0.1)
    assert not any(alive(p) for p in find_pids(port))


def test_stop_single_port_shared_model_has_config_in_scope(models, monkeypatch):
    models({"a": model_cfg("a", 19005)}, mode="single_port", masterPort=19000)
    monkeypatch.setattr(SwitcherAPIHandler, "_get_running_servers", staticmethod(lambda: {}))
    h = H()
    h.handle_api_stop({"model": "a"})
    code, body = h.sent[-1]
    assert code == 200 and "No switched model" in body["message"]


# ── 4: ports ─────────────────────────────────────────────────────────────

def test_default_ports_and_env_overrides(monkeypatch):
    monkeypatch.delenv("MCP_PORT", raising=False)
    monkeypatch.delenv("WS_PORT", raising=False)
    assert config.get_mcp_port() == 28003
    assert config.get_ws_port() == 28002
    monkeypatch.setenv("WS_PORT", "29111")
    monkeypatch.setenv("MCP_PORT", "29222")
    assert config.get_ws_port() == 29111 and config.get_mcp_port() == 29222
    monkeypatch.setenv("WS_PORT", "not-a-port")
    assert config.get_ws_port() == 28002


def test_mcp_server_default_port_is_28003():
    env = {k: v for k, v in os.environ.items() if k != "MCP_PORT"}
    env["PYTHONPATH"] = ROOT
    res = subprocess.run([sys.executable, "-c", "import mcp_server; print(mcp_server.MCP_PORT)"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert res.stdout.strip().splitlines()[-1] == "28003", res.stderr


def test_start_mcp_refuses_when_it_would_clash_with_websocket(monkeypatch, capsys):
    monkeypatch.setenv("WS_PORT", "29333")
    monkeypatch.setenv("MCP_PORT", "29333")
    assert process.start_mcp_server() is False
    assert "equals the WebSocket telemetry port" in capsys.readouterr().out


def test_start_mcp_reports_a_port_that_is_already_taken(monkeypatch, capsys):
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        monkeypatch.setenv("MCP_PORT", str(port))
        monkeypatch.setenv("WS_PORT", str(free_port()))
        assert process.start_mcp_server() is False
        assert f"port {port} is already in use" in capsys.readouterr().out
    finally:
        blocker.close()


def test_start_mcp_reports_immediate_exit_with_log_tail(monkeypatch, capsys):
    monkeypatch.setenv("MCP_PORT", str(free_port()))
    monkeypatch.setenv("WS_PORT", str(free_port()))
    monkeypatch.setenv("MCP_HOST", "256.256.256.256")  # invalid bind address: the server dies at startup
    assert process.start_mcp_server() is False
    out = capsys.readouterr().out
    assert "MCP server exited at startup" in out


# ── 5: drain and swap marks ──────────────────────────────────────────────

def test_drain_waits_for_in_flight_request_then_proceeds():
    port = 41001
    assert process.try_begin_request(port, "a")
    threading.Timer(0.6, process.end_request, args=(port,)).start()
    t0 = time.monotonic()
    assert process.drain_port(port, timeout=5) is True
    assert time.monotonic() - t0 >= 0.5
    assert process.inflight_count(port) == 0


def test_drain_timeout_proceeds_and_logs(capsys):
    port = 41002
    assert process.try_begin_request(port, "a")
    try:
        t0 = time.monotonic()
        assert process.drain_port(port, timeout=0.4) is False
        assert 0.35 <= time.monotonic() - t0 < 3
        assert "still in flight" in capsys.readouterr().out
    finally:
        process.end_request(port)


def test_swap_rejects_other_models_but_admits_the_incoming_one():
    port = 41003
    process.begin_swap(port, "incoming")
    try:
        assert process.try_begin_request(port, "old") is False
        assert process.is_swapped_out(port, "old") and not process.is_swapped_out(port, "incoming")
        assert process.try_begin_request(port, "incoming") is True
        process.end_request(port)
    finally:
        process.end_swap(port)
    assert process.try_begin_request(port, "old") is True
    process.end_request(port)


def test_restart_swap_rejects_everyone():
    port = 41004
    process.begin_swap(port, "same", restart=True)
    try:
        assert process.try_begin_request(port, "same") is False
    finally:
        process.end_swap(port)


def test_gateway_returns_503_model_swapping_with_retry_after(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_target_model", lambda name: ("old", {"port": 41005}))
    monkeypatch.setattr(proxy, "_get_port_for_model", lambda mid, cfg: 41005)
    process.begin_swap(41005, "incoming")
    try:
        h = H(command="POST")
        proxy.handle_completion_proxy(h, "/v1/chat/completions", b'{"model": "old"}')
    finally:
        process.end_swap(41005)
    body = json.loads(h.wfile.getvalue())
    assert h.status == 503 and body["error"]["code"] == "model_swapping"
    assert ("Retry-After", "5") in h.raw_headers


def test_gateway_counts_the_request_for_its_whole_exchange(monkeypatch):
    seen = {}
    monkeypatch.setattr(proxy, "resolve_target_model", lambda name: ("a", {"port": 41006}))
    monkeypatch.setattr(proxy, "_get_port_for_model", lambda mid, cfg: 41006)
    monkeypatch.setattr(proxy, "ensure_model_running", lambda mid, cfg: 41006)
    monkeypatch.setattr(proxy, "_forward", lambda h, path, body, port, mid: seen.update(count=process.inflight_count(port)))
    proxy.handle_completion_proxy(H(command="POST"), "/v1/chat/completions", b'{"model": "a"}')
    assert seen["count"] == 1 and process.inflight_count(41006) == 0


def test_gateway_releases_the_count_when_forwarding_fails(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_target_model", lambda name: ("a", {"port": 41007}))
    monkeypatch.setattr(proxy, "_get_port_for_model", lambda mid, cfg: 41007)
    monkeypatch.setattr(proxy, "ensure_model_running", lambda mid, cfg: 41007)

    def explode(*a, **k):
        raise OSError("client went away")

    monkeypatch.setattr(proxy, "_forward", explode)
    with pytest.raises(OSError):
        proxy.handle_completion_proxy(H(command="POST"), "/v1/chat/completions", b'{"model": "a"}')
    assert process.inflight_count(41007) == 0


def test_idle_reaper_skips_dedicated_and_in_flight_models(monkeypatch, models):
    models({"ded": model_cfg("ded", 19101, dedicated=True), "busy": model_cfg("busy", 19102, dedicated=True)},
           mode="multi_port")
    running = {19101: {"pid": 9101}, 19102: {"pid": 9102}}
    monkeypatch.setattr(process, "get_running_servers", lambda: running)
    monkeypatch.setattr(process, "_cleanup_stale_tracked_processes", lambda: [])
    monkeypatch.setattr(process, "get_process_cmdline",
                        lambda pid: f"llama-server --model /m/{'ded' if pid == 9101 else 'busy'}.gguf --port {19101 if pid == 9101 else 19102}")
    killed = []
    monkeypatch.setattr(process, "kill_process", lambda pid: killed.append(pid) or True)
    for mid in ("ded", "busy"):
        process._model_last_activity[mid] = time.time() - 1000

    assert process.reap_idle_models(60) == []  # dedicated models are left alone by default
    monkeypatch.setitem(config._CONFIG, "idleReapDedicated", True)
    assert process.try_begin_request(19102, "busy")
    try:
        assert process.reap_idle_models(60) == ["ded"]  # "busy" still has a stream open
    finally:
        process.end_request(19102)
    assert killed == [9101]


# ── 6a: VRAM fit ─────────────────────────────────────────────────────────

def test_vram_check_warns_when_model_does_not_fit(monkeypatch):
    monkeypatch.setattr(process, "get_free_vram_mib", lambda: 4000.0)
    monkeypatch.setattr(vram, "estimate_vram_usage", lambda cfg, *a, **k: {"total_mib": 9000.0})
    warnings, info = SwitcherAPIHandler._vram_check("big", {"filename": "big.gguf"}, [])
    assert len(warnings) == 1 and "9000" in warnings[0] and "4000" in warnings[0]
    assert info["needed_mib"] == 9000.0 and info["available_mib"] == 4000.0


def test_vram_check_counts_memory_freed_by_outgoing_models(monkeypatch, models):
    models({"old": model_cfg("old", 19000)})
    monkeypatch.setattr(process, "get_free_vram_mib", lambda: 4000.0)
    monkeypatch.setattr(vram, "estimate_vram_usage", lambda cfg, *a, **k: {"total_mib": 6000.0})
    warnings, info = SwitcherAPIHandler._vram_check("new", {"filename": "new.gguf"}, ["old"])
    assert warnings == [] and info["available_mib"] == 10000.0


@pytest.mark.parametrize("free", [None, 0])
def test_vram_check_is_silent_without_telemetry(monkeypatch, free):
    monkeypatch.setattr(process, "get_free_vram_mib", lambda: free)
    assert SwitcherAPIHandler._vram_check("m", {"filename": "m.gguf"}, []) == ([], {})


def test_vram_estimator_failure_never_blocks(monkeypatch):
    monkeypatch.setattr(process, "get_free_vram_mib", lambda: 1000.0)

    def explode(*a, **k):
        raise RuntimeError("bad gguf")

    monkeypatch.setattr(vram, "estimate_vram_usage", explode)
    assert SwitcherAPIHandler._vram_check("m", {"filename": "m.gguf"}, []) == ([], {})


@pytest.fixture
def startable(tmp_path, fake_llama, models, monkeypatch):
    """A registry with one startable model and a start flow that never launches a process."""
    data_dir = tmp_path / "models"
    data_dir.mkdir()
    (data_dir / "m1.gguf").write_bytes(b"x")
    models({"m1": model_cfg("m1", 19000, logPath=str(tmp_path / "m1.log"))},
           mode="single_port", masterPort=19000, binaryPath=fake_llama, dataDir=str(data_dir))
    monkeypatch.setattr(SwitcherAPIHandler, "_get_running_servers", staticmethod(lambda: {}))
    monkeypatch.setattr(process, "_cleanup_stale_tracked_processes", lambda: [])
    monkeypatch.setattr(process, "get_free_vram_mib", lambda: 1000.0)
    monkeypatch.setattr(vram, "estimate_vram_usage", lambda cfg, *a, **k: {"total_mib": 5000.0})
    monkeypatch.setattr(SwitcherAPIHandler, "_spawn_model", lambda self, *a, **k: (object(), None))


def start(data):
    h = H(command="POST")
    h.handle_api_start(data)
    return h.sent[-1]


def test_start_adds_warnings_by_default(startable):
    code, body = start({"model": "m1"})
    assert code == 200 and body["success"] and len(body["warnings"]) == 1


def test_strict_vram_refuses_with_409_unless_forced(startable, monkeypatch):
    monkeypatch.setitem(config._CONFIG, "strictVram", True)
    code, body = start({"model": "m1"})
    assert code == 409 and "force" in body["error"] and body["warnings"] and body["needed_mib"] == 5000.0
    code, body = start({"model": "m1", "force": True})
    assert code == 200 and body["warnings"]


def test_start_without_a_fit_problem_has_empty_warnings(startable, monkeypatch):
    monkeypatch.setattr(process, "get_free_vram_mib", lambda: 50000.0)
    monkeypatch.setitem(config._CONFIG, "strictVram", True)
    code, body = start({"model": "m1"})
    assert code == 200 and body["warnings"] == []


def test_start_validates_before_stopping_anything(startable, monkeypatch):
    monkeypatch.setitem(config._CONFIG, "binaryPath", "/nonexistent/llama-server")
    killed = []
    monkeypatch.setattr(process, "kill_process", lambda pid: killed.append(pid) or True)
    code, body = start({"model": "m1"})
    assert code == 400 and "Binary not found" in body["error"] and killed == []


# ── 6c: management auth ──────────────────────────────────────────────────

def auth(path, headers=None, key="sekret"):
    saved = config._CONFIG.get("managementApiKey")
    config._CONFIG["managementApiKey"] = key or ""
    try:
        h = H(headers)
        return h._management_auth_ok(path), h
    finally:
        if saved is None:
            config._CONFIG.pop("managementApiKey", None)
        else:
            config._CONFIG["managementApiKey"] = saved


def test_auth_is_off_when_no_key_is_configured():
    ok, _ = auth("/api/status", key=None)
    assert ok


def test_auth_required_when_key_is_set():
    ok, h = auth("/api/status")
    assert not ok and h.status == 401
    assert ("WWW-Authenticate", "Bearer") in h.raw_headers
    assert "Unauthorized" in json.loads(h.wfile.getvalue())["error"]


def test_auth_accepts_bearer_and_x_api_key_and_rejects_wrong_key():
    assert auth("/api/status", {"Authorization": "Bearer sekret"})[0]
    assert auth("/api/status", {"X-API-Key": "sekret"})[0]
    assert not auth("/api/status", {"Authorization": "Bearer nope"})[0]
    assert not auth("/api/status", {"X-API-Key": "sekre"})[0]


def test_auth_exempts_health_pin_and_non_api_paths():
    for path in ("/api/health", "/api/pin", "/api/pin/status", "/", "/index.js", "/v1/models"):
        assert auth(path)[0], path


def test_auth_accepts_a_valid_pin_session_and_rejects_expired_or_unknown():
    import security
    security._PIN_SESSIONS["live"] = {"created_at": time.time()}
    security._PIN_SESSIONS["stale"] = {"created_at": time.time() - security.PIN_SESSION_TIMEOUT - 5}
    try:
        assert auth("/api/status", {"X-Pin-Session": "live"})[0]
        assert not auth("/api/status", {"X-Pin-Session": "stale"})[0]
        assert not auth("/api/status", {"X-Pin-Session": "unknown"})[0]
    finally:
        security._PIN_SESSIONS.pop("live", None)
        security._PIN_SESSIONS.pop("stale", None)


# ── 6d / 6f: active and status ───────────────────────────────────────────

def stub_running(monkeypatch, port=19000):
    cmd = f"llama-server --model /m/a.gguf --port {port}"
    monkeypatch.setattr(process, "get_running_servers", lambda: {port: {"pid": 1234, "cmd": cmd}})
    monkeypatch.setattr(process, "get_process_cmdline", lambda pid: cmd)
    monkeypatch.setattr(process, "get_process_stats", lambda pid: {"cpu": 0.0, "mem": 0.0, "uptime": "00:01"})
    monkeypatch.setattr(process, "_cleanup_stale_tracked_processes", lambda: [])


def test_active_reports_the_effective_port_and_endpoint(models, monkeypatch):
    models({"a": model_cfg("a", 19005, endpoint="http://localhost:19005/v1")}, mode="single_port", masterPort=19000)
    stub_running(monkeypatch)
    h = H()
    h.handle_api_active()
    entry = h.sent[-1][1]["active"][0]
    assert entry["port"] == 19000 and entry["endpoint"] == "http://localhost:19000/v1"


def test_active_dedicated_model_keeps_its_own_port(models, monkeypatch):
    models({"a": model_cfg("a", 19005, dedicated=True)}, mode="single_port", masterPort=19000)
    stub_running(monkeypatch, port=19005)
    h = H()
    h.handle_api_active()
    entry = h.sent[-1][1]["active"][0]
    assert entry["port"] == 19005 and entry["endpoint"].endswith(":19005/v1")


def test_status_includes_master_port_and_effective_port(models, monkeypatch):
    models({"a": model_cfg("a", 19005)}, mode="single_port", masterPort=19000)
    stub_running(monkeypatch)
    h = H()
    h.handle_api_status()
    body = h.sent[-1][1]
    assert body["masterPort"] == 19000
    assert body["models"][0]["effectivePort"] == 19000 and body["models"][0]["port"] == 19005


# ── 6e: single health response ───────────────────────────────────────────

def raw_get(port, path):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode()


def test_health_503_sends_exactly_one_response(monkeypatch):
    monkeypatch.setattr(api, "_server_ready", False)
    httpd = api.ThreadedServer(("127.0.0.1", 0), SwitcherAPIHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        raw = raw_get(httpd.server_address[1], "/api/health")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert raw.count("HTTP/1.") == 1 and raw.startswith("HTTP/1.0 503")
    body = json.loads(raw.split("\r\n\r\n", 1)[1])
    assert body["status"] == "starting"


def test_threaded_server_serves_requests_while_another_is_open(monkeypatch):
    monkeypatch.setattr(api, "_server_ready", True)
    httpd = api.ThreadedServer(("127.0.0.1", 0), SwitcherAPIHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    idle = socket.create_connection(("127.0.0.1", port))  # a client holding a connection open
    try:
        idle.sendall(b"GET /api/health HTTP/1.0\r\n")  # incomplete request: handler thread blocks reading
        assert raw_get(port, "/api/health").startswith("HTTP/1.0 200")
    finally:
        idle.close()
        httpd.shutdown()
        httpd.server_close()


# ── misc regressions ─────────────────────────────────────────────────────

def test_save_mode_invalidates_the_models_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
    config.get_models()
    assert config._models_cache is not None
    config.save_mode("multi_port")
    try:
        assert config._models_cache is None
    finally:
        config._CONFIG["mode"] = "single_port"


def test_ws_broadcast_drops_dead_clients_without_crashing():
    import telemetry

    class Good:
        sent = []

        async def send(self, msg):
            self.sent.append(msg)

    class Dead:
        async def send(self, msg):
            raise ConnectionError("gone")

    good, dead = Good(), Dead()
    telemetry._ws_clients.update({good, dead})
    try:
        asyncio.run(telemetry._ws_broadcast({"type": "telemetry"}))
        assert good.sent and dead not in telemetry._ws_clients
    finally:
        telemetry._ws_clients.clear()


def test_patch_models_route_exists_on_the_api_handler():
    assert hasattr(SwitcherAPIHandler, "do_PATCH")


# ── 6g: macOS process control ────────────────────────────────────────────

def test_kill_process_works_with_macos_code_paths(tmp_path, monkeypatch):
    script = tmp_path / "llama-server"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script), "--port", "41999"], start_new_session=True)
    time.sleep(0.3)
    monkeypatch.setattr(process, "IS_MACOS", True)
    monkeypatch.setattr(process, "IS_LINUX", False)
    monkeypatch.setattr(process, "IS_WINDOWS", False)
    try:
        assert "llama-server" in process.get_process_cmdline(proc.pid)
        assert process.is_llama_server(proc.pid) is True
        assert process.kill_process(proc.pid) is True
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_macos_stats_and_children_use_portable_commands(monkeypatch):
    monkeypatch.setattr(process, "IS_MACOS", True)
    monkeypatch.setattr(process, "IS_LINUX", False)
    monkeypatch.setattr(process, "IS_WINDOWS", False)
    stats = process.get_process_stats(os.getpid())
    assert stats["uptime"] != "Unknown"
    assert isinstance(process.get_child_processes(os.getpid()), list)


def test_darwin_is_its_own_posix_environment():
    code = ("import platform; platform.system = lambda: 'Darwin'; import config; "
            "print(config.RUNTIME_ENV, config.IS_MACOS, config.IS_LINUX, config.IS_WINDOWS)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": ROOT})
    assert res.stdout.strip().splitlines()[-1] == "macos True False False", res.stderr


# ── /api/gpu handler ─────────────────────────────────────────────────────

GPU = {"name": "RTX", "temp": 40.0, "util": 3.0, "mem_used": 1024.0, "mem_total": 8192.0, "power": 20.0}


def test_gpu_handler_returns_the_telemetry_shape(monkeypatch):
    monkeypatch.setattr(process, "get_gpu_telemetry", lambda: {"nvidia": GPU, "amd": None})
    h = H()
    h.handle_api_gpu()
    assert h.sent == [(200, {"nvidia": GPU, "amd": None})]


def test_gpu_handler_returns_nulls_when_telemetry_raises(monkeypatch):
    def boom():
        raise RuntimeError("smi exploded")
    monkeypatch.setattr(process, "get_gpu_telemetry", boom)
    h = H()
    h.handle_api_gpu()
    assert h.sent == [(200, {"nvidia": None, "amd": None})]


def test_gpu_route_is_behind_the_management_key():
    ok, h = auth("/api/gpu")
    assert not ok and h.status == 401
    ok, _ = auth("/api/gpu", {"Authorization": "Bearer sekret"})
    assert ok


def test_every_handler_called_in_api_py_is_defined():
    import re
    src = open(os.path.join(ROOT, "api.py"), encoding="utf-8").read()
    defined = set(re.findall(r"^\s+def (\w+)\(", src, re.M))
    called = set(re.findall(r"self\.(handle_\w+|send_json_response|send_error_response)\(", src))
    assert sorted(c for c in called if c not in defined) == []
