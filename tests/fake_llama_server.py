"""Tiny stand-in for llama-server used by the tests (copied next to a `llama-server` shebang line).

Flags understood: --port N, --model PATH, --fake-load SECONDS (503 on /health until loaded),
--fake-stream SECONDS (length of a streamed chat reply), --fake-exit CODE (exit immediately).
"""
import http.server
import json
import os
import socketserver
import sys
import time


def _arg(name, default=None, cast=str):
    if name in sys.argv:
        return cast(sys.argv[sys.argv.index(name) + 1])
    return default


PORT = _arg("--port", cast=int)
MODEL = os.path.basename(_arg("--model", "model.gguf"))
LOAD = _arg("--fake-load", 0.2, float)
STREAM = _arg("--fake-stream", 0.0, float)
EXIT = _arg("--fake-exit", None, int)
STARTED = time.time()

if EXIT is not None:
    print("fake llama-server: simulated startup failure", flush=True)
    sys.exit(EXIT)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            if time.time() - STARTED < LOAD:
                self._json(503, {"status": "loading"})
            else:
                self._json(200, {"status": "ok"})
        elif self.path in ("/v1/models", "/models"):
            self._json(200, {"data": [{"id": MODEL}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not body.get("stream"):
            self._json(200, {"model": MODEL, "choices": [{"message": {"role": "assistant", "content": "ok"}}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        deadline = time.time() + STREAM
        n = 0
        while True:
            self.wfile.write(f'data: {{"model": "{MODEL}", "n": {n}}}\n\n'.encode())
            self.wfile.flush()
            n += 1
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


print(f"fake llama-server {MODEL} listening on {PORT}", flush=True)
Server(("0.0.0.0", PORT), Handler).serve_forever()
