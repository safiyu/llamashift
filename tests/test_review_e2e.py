#!/usr/bin/env python3
"""End-to-end HTTP test of the cloud-reviewer gateway fixes.

Boots an isolated instance of the real api.SwitcherAPIHandler in a temp dir
with an EMPTY model registry (so no real llama-server spawn can happen), on a
free port, then issues real HTTP requests.

Key assertion (Fix 1.1): POST /v1/chat/completions must reach the proxy —
i.e. return the proxy's OpenAI-formatted error — instead of the old
do_POST fallthrough `404 {"error": "Endpoint not found"}`.

Run:  python3 tests/test_review_e2e.py
"""
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(method, url, body=None, headers=None):
    data = body.encode() if isinstance(body, str) else body
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="llamasf_e2e_")

    # Isolated config: empty registry, no models -> no spawn possible.
    cfg = {
        "appName": "llamashift-test",
        "mode": "single_port",
        "masterPort": 9100,
        "serverPort": port,
        "dataDir": os.path.join(tmp, "models"),
        "models": {},
    }
    os.makedirs(os.path.join(tmp, "models"), exist_ok=True)
    with open(os.path.join(tmp, "config.json"), "w") as f:
        json.dump(cfg, f)

    os.chdir(tmp)
    sys.path.insert(0, PROJECT)

    # Import AFTER chdir so config.init_config() reads our temp config.json.
    import config
    import api
    from api import SwitcherAPIHandler, set_server_ready

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), SwitcherAPIHandler)
    set_server_ready()
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"
    PASS = FAIL = 0

    def check(name, cond, detail=""):
        nonlocal PASS, FAIL
        if cond:
            PASS += 1
            print(f"  PASS  {name}")
        else:
            FAIL += 1
            print(f"  FAIL  {name}  {detail}")

    # wait for socket to accept
    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    print(f"\nE2E server on {base} (empty registry)\n")

    # 1. GET /v1/models -> 200 OpenAI list (was working via do_GET before)
    st, body = req("GET", f"{base}/v1/models")
    check("GET /v1/models -> 200", st == 200, f"got {st}")
    try:
        check("  body is OpenAI list", json.loads(body).get("object") == "list", body[:80])
    except Exception as e:
        check("  body is OpenAI list", False, str(e))

    # 2. POST /v1/chat/completions (THE critical fix) -> must reach proxy.
    #    Empty registry: proxy returns OpenAI error, NOT 'Endpoint not found'.
    st, body = req("POST", f"{base}/v1/chat/completions",
                   body='{"model":"nope","messages":[{"role":"user","content":"hi"}]}',
                   headers={"Content-Type": "application/json"})
    check("POST /v1/chat/completions NOT 'Endpoint not found' (Fix 1.1)",
          "Endpoint not found" not in body, body[:120])
    check("  reached proxy (OpenAI error shape)",
          st in (400, 404) and ("not found in registry" in body
                                or "Missing 'model'" in body
                                or '"type"' in body),
          f"status={st} body={body[:120]}")

    # 3. POST /v1/embeddings -> also routed to proxy, not 404 fallthrough
    st, body = req("POST", f"{base}/v1/embeddings",
                   body='{"model":"nope","input":"x"}',
                   headers={"Content-Type": "application/json"})
    check("POST /v1/embeddings routed to proxy (Fix 1.1)",
          "Endpoint not found" not in body, body[:120])

    # 4. GET /api/active -> 200 (Fix 1.2 NameError)
    st, body = req("GET", f"{base}/api/active")
    check("GET /api/active -> 200 (Fix 1.2 no NameError)", st == 200, f"{st} {body[:80]}")
    try:
        check("  returns active list", "active" in json.loads(body), body[:80])
    except Exception:
        check("  returns active list", False, body[:80])

    # 5. GET /api/status -> 200 (bonus: same NameError fixed)
    st, body = req("GET", f"{base}/api/status")
    check("GET /api/status -> 200 (NameError fixed)", st == 200, f"{st} {body[:80]}")

    # 6. Hub / VRAM endpoints exist (Fix 1.3) — no AttributeError/500.
    st, body = req("GET", f"{base}/api/hub/downloads")
    check("GET /api/hub/downloads -> 200 (Fix 1.3)", st == 200, f"{st} {body[:80]}")

    st, body = req("GET", f"{base}/api/hub/search")  # missing q
    check("GET /api/hub/search (no q) -> 400 (method exists)",
          st == 400, f"{st} {body[:80]}")

    st, body = req("GET", f"{base}/api/vram/estimate")  # missing model
    check("GET /api/vram/estimate (no model) -> 400 (method exists)",
          st == 400, f"{st} {body[:80]}")

    # 7. DoS bound (Fix 2.1): huge Content-Length -> 413, not OOM/500.
    st, body = req("POST", f"{base}/v1/chat/completions",
                   body="x",
                   headers={"Content-Type": "application/json",
                            "Content-Length": str(api.SwitcherAPIHandler.MAX_GATEWAY_BODY_BYTES + 1)})
    check("over-limit POST body -> 413 (Fix 2.1)", st == 413, f"{st} {body[:80]}")

    print(f"\n{'='*50}\nE2E RESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
    httpd.shutdown()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
