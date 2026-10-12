"""Shared pytest setup: sandbox cwd/HOME before any llamashift module loads config.json.

config.py reads ./config.json and logs under ~/logs at import time, so the sandbox is created
here, before test modules are imported, to keep the repo and the real home directory untouched.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = tempfile.mkdtemp(prefix="llamashift_tests_")

os.environ["HOME"] = os.path.join(SANDBOX, "home")
os.makedirs(os.environ["HOME"], exist_ok=True)
os.chdir(SANDBOX)
with open("config.json", "w") as _f:
    json.dump({"appName": "llamashift-test", "mode": "single_port", "masterPort": 19000,
               "serverPort": 18002, "dataDir": os.path.join(SANDBOX, "models"), "models": {}}, _f)
sys.path.insert(0, ROOT)

FAKE_SERVER_SOURCE = os.path.join(ROOT, "tests", "fake_llama_server.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_llama(tmp_path):
    """Path to an executable named llama-server that behaves like a tiny llama.cpp server."""
    target = tmp_path / "bin" / "llama-server"
    target.parent.mkdir()
    with open(FAKE_SERVER_SOURCE) as f:
        body = f.read()
    target.write_text(f"#!{sys.executable}\n{body}")
    target.chmod(0o755)
    return str(target)


def http(method, url, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    if isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


class ServerProc:
    """A real llamashift server (main.py or server.py) in its own sandbox directory."""

    def __init__(self, workdir, entry, config, env=None):
        self.workdir = str(workdir)
        self.port = config["serverPort"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_port = free_port()
        self.mcp_port = free_port()
        os.makedirs(os.path.join(self.workdir, "home"), exist_ok=True)
        with open(os.path.join(self.workdir, "config.json"), "w") as f:
            json.dump(config, f)
        self.log_path = os.path.join(self.workdir, "server.log")
        self.log = open(self.log_path, "w")
        proc_env = {**os.environ, "HOME": os.path.join(self.workdir, "home"),
                    "WS_PORT": str(self.ws_port), "MCP_PORT": str(self.mcp_port),
                    "PYTHONUNBUFFERED": "1", **(env or {})}
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, entry)], cwd=self.workdir,
                                     env=proc_env, stdout=self.log, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early (code {self.proc.returncode}):\n{self.output()}")
            try:
                if http("GET", f"{self.base}/api/health", timeout=2)[0] == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"server not ready after {timeout}s:\n{self.output()}")

    def output(self):
        self.log.flush()
        with open(self.log_path) as f:
            return f.read()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        # model servers run in their own sessions; only kill ones that belong to this sandbox
        out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
        for line in out.splitlines()[1:]:
            pid, _, args = line.strip().partition(" ")
            if self.workdir in args and "llama-server" in args:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        self.log.close()


@pytest.fixture
def start_server(tmp_path_factory, fake_llama):
    """Factory: start_server(entry="main.py", **config_overrides) -> ready ServerProc."""
    started = []

    def factory(entry="main.py", env=None, models=None, **overrides):
        workdir = tmp_path_factory.mktemp("srv")
        data_dir = workdir / "models"
        data_dir.mkdir()
        master = free_port()
        cfg_models = {}
        for mid, extra in (models or {}).items():
            (data_dir / f"{mid}.gguf").write_bytes(b"x")
            cfg_models[mid] = {"id": mid, "name": mid.upper(), "filename": f"{mid}.gguf",
                               "port": master, "ctxSize": 2048, "nParallel": 1, "devices": ["ROCm0"],
                               "dedicated": False, **extra}
        config = {"appName": "llamashift-e2e", "mode": "single_port", "masterPort": master,
                  "serverPort": free_port(), "binaryPath": fake_llama, "dataDir": str(data_dir),
                  "coldStartTimeoutSeconds": 20, "models": cfg_models, **overrides}
        srv = ServerProc(workdir, entry, config, env)
        started.append(srv)
        srv.master_port = master
        return srv.wait_ready()

    yield factory
    for srv in started:
        srv.stop()
