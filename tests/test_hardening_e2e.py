"""End-to-end tests: real llamashift servers (main.py and server.py) with a fake llama-server."""
import json
import os
import signal
import socket
import subprocess
import threading
import time

import pytest
from http import client as httpclient

from conftest import http


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for(predicate, timeout=20, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def chat(port, model, stream=False, timeout=60):
    return http("POST", f"http://127.0.0.1:{port}/v1/chat/completions",
                {"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}, timeout=timeout)


def pids_on_port(port):
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return [int(line.split(None, 1)[0]) for line in out.splitlines()[1:]
            if f"--port {port}" in line and "llama-server" in line]


MODELS = {"a": {"extraArgs": ["--fake-load", "0.3", "--fake-stream", "4"]},
          "b": {"extraArgs": ["--fake-load", "0.3"]}}


@pytest.mark.parametrize("entry", ["main.py", "server.py"])
def test_every_entrypoint_serves_the_same_api(start_server, entry):
    srv = start_server(entry, models=MODELS)
    status, body, _ = http("GET", f"{srv.base}/v1/models")
    assert status == 200 and {m["id"] for m in json.loads(body)["data"]} == {"a", "b"}
    assert http("GET", f"{srv.base}/api/vram/estimate")[0] == 400  # route exists, model id is required
    status, body, _ = http("GET", f"{srv.base}/api/status")
    assert status == 200 and json.loads(body)["masterPort"] == srv.master_port
    assert json.loads(http("GET", f"{srv.base}/api/active")[1]) == {"active": []}
    assert http("PATCH", f"{srv.base}/api/models", "{")[0] == 400  # PATCH is routed on every entrypoint
    assert "Traceback" not in srv.output() and "NameError" not in srv.output()


def test_websocket_and_mcp_listen_together_on_their_own_ports(start_server):
    srv = start_server(models=MODELS)
    assert wait_for(lambda: listening(srv.ws_port), 10), srv.output()
    assert wait_for(lambda: listening(srv.mcp_port), 10), srv.output()
    out = srv.output()
    assert "Started MCP server" in out and f"on port {srv.mcp_port}" in out and "ERROR" not in out
    cfg = json.loads(http("GET", f"{srv.base}/api/config")[1])
    assert cfg["wsPort"] == srv.ws_port and cfg["mcpPort"] == srv.mcp_port


def test_idle_reaper_starts_only_when_enabled(start_server):
    off = start_server(models=MODELS, idleTimeoutSeconds=0)
    on = start_server(models=MODELS, idleTimeoutSeconds=30)
    assert "Idle auto-unload worker started" not in off.output()
    assert "Idle auto-unload worker started" in on.output()


def test_management_api_key_end_to_end(start_server):
    srv = start_server(models=MODELS, managementApiKey="s3cret")
    assert http("GET", f"{srv.base}/api/status")[0] == 401
    assert http("GET", f"{srv.base}/api/status", headers={"Authorization": "Bearer s3cret"})[0] == 200
    assert http("GET", f"{srv.base}/api/status", headers={"X-API-Key": "s3cret"})[0] == 200
    assert http("GET", f"{srv.base}/api/status", headers={"X-API-Key": "wrong"})[0] == 401
    assert http("POST", f"{srv.base}/api/stop_all", {})[0] == 401
    assert http("GET", f"{srv.base}/api/health")[0] == 200
    assert http("GET", f"{srv.base}/v1/models")[0] == 200
    assert http("GET", f"{srv.base}/")[0] == 200  # dashboard assets stay reachable


def test_start_returns_warnings_and_active_matches_status(start_server):
    srv = start_server(models=MODELS)
    status, body, _ = http("POST", f"{srv.base}/api/start", {"model": "b"})
    data = json.loads(body)
    assert status == 200 and data["success"] and data["warnings"] == []
    assert wait_for(lambda: json.loads(http("GET", f"{srv.base}/api/active")[1])["active"], 15)
    entry = json.loads(http("GET", f"{srv.base}/api/active")[1])["active"][0]
    assert entry["port"] == srv.master_port and entry["endpoint"] == f"http://localhost:{srv.master_port}/v1"
    status_model = [m for m in json.loads(http("GET", f"{srv.base}/api/status")[1])["models"] if m["id"] == "b"][0]
    assert status_model["effectivePort"] == srv.master_port and status_model["status"] == "running"


def test_bad_start_request_does_not_kill_the_running_model(start_server):
    srv = start_server(models=MODELS)
    assert chat(srv.port, "b")[0] == 200
    os.unlink(os.path.join(srv.workdir, "models", "a.gguf"))
    status, body, _ = http("POST", f"{srv.base}/api/start", {"model": "a"})
    assert status == 400 and "Model file not found" in json.loads(body)["error"]
    active = json.loads(http("GET", f"{srv.base}/api/active")[1])["active"]
    assert [m["id"] for m in active] == ["b"]


def test_swap_drains_the_active_stream_and_rejects_the_old_model(start_server):
    srv = start_server(models=MODELS)
    stream = {"chunks": 0, "done": False, "status": None}
    first_chunk = threading.Event()

    def read_stream():
        conn = httpclient.HTTPConnection("127.0.0.1", srv.port, timeout=60)
        conn.request("POST", "/v1/chat/completions",
                     json.dumps({"model": "a", "stream": True, "messages": []}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        stream["status"] = resp.status
        buf = b""
        while True:
            data = resp.read1(4096)
            if not data:
                break
            buf += data
            stream["chunks"] = buf.count(b"data: {")
            stream.setdefault("first_at", time.time())
            first_chunk.set()
        stream["done"] = b"[DONE]" in buf
        stream["ended"] = time.time()

    reader = threading.Thread(target=read_stream)
    reader.start()
    assert first_chunk.wait(30), srv.output()  # model "a" is up and streaming (about 4s long)

    swap = {}

    def request_b():
        swap["result"] = chat(srv.port, "b")
        swap["at"] = time.time()

    swapper = threading.Thread(target=request_b)
    swapper.start()
    time.sleep(1.0)  # the swap is now draining "a"

    started = time.time()
    assert http("GET", f"{srv.base}/api/health")[0] == 200
    assert time.time() - started < 2  # the server stays responsive while a stream is open
    status, body, headers = chat(srv.port, "a")
    assert status == 503 and json.loads(body)["error"]["code"] == "model_swapping", srv.output()
    assert headers.get("Retry-After") == "5"

    reader.join(30)
    swapper.join(60)
    assert stream["status"] == 200 and stream["done"] and stream["chunks"] >= 10  # never cut off mid-stream
    status, body, _ = swap["result"]
    assert status == 200 and json.loads(body)["model"] == "b.gguf"
    assert stream["ended"] - stream["first_at"] > 2  # tokens arrive as they are produced, not all at the end
    assert swap["at"] >= stream["ended"]  # "b" only answered after "a" finished
    assert "Waiting for 1 in-flight request" in srv.output()


def test_drain_timeout_lets_the_swap_proceed(start_server):
    srv = start_server(models=MODELS, drainTimeoutSeconds=1)
    result = {}
    first_chunk = threading.Event()

    def read_stream():
        conn = httpclient.HTTPConnection("127.0.0.1", srv.port, timeout=60)
        conn.request("POST", "/v1/chat/completions", json.dumps({"model": "a", "stream": True, "messages": []}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        try:
            while resp.read1(4096):
                first_chunk.set()
            result["ended_cleanly"] = True
        except (OSError, httpclient.HTTPException):
            result["ended_cleanly"] = False

    reader = threading.Thread(target=read_stream)
    reader.start()
    assert first_chunk.wait(30), srv.output()
    status, body, _ = chat(srv.port, "b")
    reader.join(30)
    assert status == 200 and json.loads(body)["model"] == "b.gguf"
    assert "still in flight after 1s, proceeding with the swap" in srv.output()


def test_shutdown_stops_the_model_processes(start_server):
    srv = start_server(models=MODELS)
    assert chat(srv.port, "b")[0] == 200
    assert wait_for(lambda: pids_on_port(srv.master_port), 5)
    srv.proc.send_signal(signal.SIGTERM)
    srv.proc.wait(timeout=20)
    assert wait_for(lambda: not pids_on_port(srv.master_port), 10), "model process outlived the server"
