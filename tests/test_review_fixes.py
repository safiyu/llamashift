#!/usr/bin/env python3
"""Smoke tests for the cloud-reviewer critical fixes in api.py.

Run:  python3 tests/test_review_fixes.py
No live server / no real llama-server spawn / no port binding.
"""
import io
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import proxy
from api import SwitcherAPIHandler

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeHandler:
    """Bare stand-in exposing only the attrs the handlers touch."""

    # Mirror the real class constant so self.MAX_GATEWAY_BODY_BYTES resolves.
    MAX_GATEWAY_BODY_BYTES = SwitcherAPIHandler.MAX_GATEWAY_BODY_BYTES

    def __init__(self, command="GET", content_length=0, body=b""):
        self.command = command
        self.headers = {"Content-Length": str(content_length)}
        self.rfile = io.BytesIO(body)
        self.calls = []

    def send_error_response(self, code, message):
        self.calls.append((code, message))

    def send_json_response(self, data):
        self.calls.append((200, data))


def bind(method_name, handler):
    """Bind an unbound handler method to a FakeHandler."""
    fn = getattr(SwitcherAPIHandler, method_name)
    return lambda *a, **k: fn(handler, *a, **k)


def test_gateway_routing_decision():
    print("\n[1] _dispatch_gateway_proxy routing decision")
    orig = proxy.handle_request
    proxy.handle_request = lambda *a, **k: None
    try:
        for path, expected in [
            ("/v1/models", True),
            ("/v1/chat/completions", True),
            ("/v1/embeddings", True),
            ("/v1/tokenize", True),
            ("/v1/", True),
            ("/models", True),
            ("/api/health", False),
            ("/api/active", False),
            ("/", False),
            ("/api/status", False),
            ("/v1x", False),  # must NOT match (no slash)
        ]:
            h = FakeHandler(command="GET")
            dispatched = SwitcherAPIHandler._dispatch_gateway_proxy(h, path, {})
            check(f"route {path!r} -> {expected}", dispatched == expected,
                  f"got {dispatched}")
    finally:
        proxy.handle_request = orig


def test_gateway_post_body_bounded():
    print("\n[2] POST body ingestion is bounded (DoS fix)")
    captured = {}

    def fake_handle_request(handler, method, path, query, body):
        captured["method"] = method
        captured["body"] = body

    orig = proxy.handle_request
    proxy.handle_request = fake_handle_request
    try:
        # Normal POST with small body
        h = FakeHandler(command="POST", content_length=5, body=b"hello")
        SwitcherAPIHandler._dispatch_gateway_proxy(h, "/v1/chat/completions", {})
        check("POST forwards method+body",
              captured.get("method") == "POST" and captured.get("body") == b"hello",
              f"got {captured}")

        # Over-limit body -> 413, no read
        big = SwitcherAPIHandler.MAX_GATEWAY_BODY_BYTES + 1
        h = FakeHandler(command="POST", content_length=big, body=b"")
        SwitcherAPIHandler._dispatch_gateway_proxy(h, "/v1/chat/completions", {})
        check("over-limit body -> 413", h.calls and h.calls[0][0] == 413,
              f"got {h.calls}")

        # Negative Content-Length -> 400
        h = FakeHandler(command="POST", content_length=-1, body=b"")
        SwitcherAPIHandler._dispatch_gateway_proxy(h, "/v1/chat/completions", {})
        check("negative Content-Length -> 400",
              h.calls and h.calls[0][0] == 400, f"got {h.calls}")

        # Malformed Content-Length -> 400
        h = FakeHandler(command="POST")
        h.headers = {"Content-Length": "abc"}
        SwitcherAPIHandler._dispatch_gateway_proxy(h, "/v1/chat/completions", {})
        check("malformed Content-Length -> 400",
              h.calls and h.calls[0][0] == 400, f"got {h.calls}")
    finally:
        proxy.handle_request = orig


def test_gateway_proxy_exception_contained():
    print("\n[3] proxy exception -> 502, handler survives")
    def boom(handler, method, path, query, body):
        raise RuntimeError("upstream dead")

    orig = proxy.handle_request
    proxy.handle_request = boom
    try:
        h = FakeHandler(command="POST", content_length=0, body=b"")
        SwitcherAPIHandler._dispatch_gateway_proxy(h, "/v1/chat/completions", {})
        check("proxy raise -> 502", h.calls and h.calls[0][0] == 502,
              f"got {h.calls}")
    finally:
        proxy.handle_request = orig


def test_handle_api_active_no_nameerror():
    print("\n[4] handle_api_active no longer NameErrors in single_port mode")
    import config
    # config already loaded in single_port with a masterPort
    orig_mode = config.get_global_mode
    config.get_global_mode = lambda: "single_port"
    # Stub process functions so we don't touch the real running models.
    import process
    orig = (process.get_running_servers, process.get_process_cmdline,
            process.get_process_stats, process._process_matches_model,
            process._cleanup_stale_tracked_processes)
    try:
        process.get_running_servers = lambda: {}
        process.get_process_cmdline = lambda pid: ""
        process.get_process_stats = lambda pid: {"cpu": 0.0, "mem": 0.0, "uptime": "00:00"}
        process._process_matches_model = lambda *a: False
        process._cleanup_stale_tracked_processes = lambda: None

        h = FakeHandler(command="GET")
        SwitcherAPIHandler.handle_api_active(h)
        # Success means no NameError; it should have emitted a JSON response.
        got = [c for c in h.calls if isinstance(c, tuple) and c[0] == 200]
        check("handle_api_active returns JSON (no NameError)",
              len(got) == 1 and "active" in got[0][1], f"got {h.calls}")
    except NameError as e:
        check("handle_api_active returns JSON (no NameError)", False, f"NameError: {e}")
    finally:
        (process.get_running_servers, process.get_process_cmdline,
         process.get_process_stats, process._process_matches_model,
         process._cleanup_stale_tracked_processes) = orig
        config.get_global_mode = orig_mode


def test_handle_api_status_no_nameerror():
    print("\n[5] handle_api_status no longer NameErrors in single_port mode")
    import config
    import process
    orig_mode = config.get_global_mode
    config.get_global_mode = lambda: "single_port"
    orig = (process.get_running_servers, process.get_process_cmdline,
            process.get_process_stats, process.get_process_cmdline,
            process._process_matches_model, process._cleanup_stale_tracked_processes,
            process.get_host_stats)
    try:
        process.get_running_servers = lambda: {}
        process.get_process_cmdline = lambda pid: ""
        process.get_process_stats = lambda pid: {"cpu": 0.0, "mem": 0.0, "uptime": "00:00"}
        process._process_matches_model = lambda *a: False
        process._cleanup_stale_tracked_processes = lambda: None
        process.get_host_stats = lambda: {"cpu": 0.0, "mem": 0.0}
        # stub mcp globals used by status
        if not hasattr(process, "_mcp_process"):
            process._mcp_process = None
            process._mcp_lock = types.SimpleNamespace()
        h = FakeHandler(command="GET")
        SwitcherAPIHandler.handle_api_status(h)
        got = [c for c in h.calls if isinstance(c, tuple) and c[0] == 200]
        check("handle_api_status returns JSON (no NameError)",
              len(got) == 1 and "models" in got[0][1], f"got {h.calls}")
    except NameError as e:
        check("handle_api_status returns JSON (no NameError)", False, f"NameError: {e}")
    finally:
        (process.get_running_servers, process.get_process_cmdline,
         process.get_process_stats, process.get_process_cmdline,
         process._process_matches_model, process._cleanup_stale_tracked_processes,
         process.get_host_stats) = orig
        config.get_global_mode = orig_mode


def test_build_cmd_draft_resolution():
    print("\n[6] _build_cmd draft path resolution (~/ expand + isabs)")
    tmp = tempfile.mkdtemp()
    data_dir = tmp

    # a) relative draft present in data_dir
    rel = os.path.join(data_dir, "draft.gguf")
    open(rel, "w").close()
    cfg = {"filename": "main.gguf", "ctxSize": 4096,
           "draftModel": "draft.gguf", "draftGpuLayers": 12}
    open(os.path.join(data_dir, "main.gguf"), "w").close()
    cmd, _ = SwitcherAPIHandler._build_cmd(cfg, "/bin/true", data_dir, 9000)
    i = cmd.index("--model-draft")
    check("relative draft resolved into data_dir", cmd[i + 1] == rel, f"got {cmd[i+1]}")
    check("draftGpuLayers -> --gpu-layers-draft",
          "--gpu-layers-draft" in cmd and cmd[cmd.index("--gpu-layers-draft") + 1] == "12")

    # b) absolute draft present
    absd = os.path.join(tmp, "abs_draft.gguf")
    open(absd, "w").close()
    cfg2 = {"filename": "main.gguf", "ctxSize": 4096, "draftModel": absd}
    cmd2, _ = SwitcherAPIHandler._build_cmd(cfg2, "/bin/true", data_dir, 9000)
    i2 = cmd2.index("--model-draft")
    check("absolute draft used as-is", cmd2[i2 + 1] == absd, f"got {cmd2[i2+1]}")

    # c) tilde draft present in $HOME
    home_draft = os.path.join(os.path.expanduser("~"), "llamasf_test_draft.gguf")
    try:
        open(home_draft, "w").close()
        cfg3 = {"filename": "main.gguf", "ctxSize": 4096,
                "draftModel": "~/llamasf_test_draft.gguf"}
        cmd3, _ = SwitcherAPIHandler._build_cmd(cfg3, "/bin/true", data_dir, 9000)
        i3 = cmd3.index("--model-draft")
        check("~ draft expanded correctly", cmd3[i3 + 1] == home_draft,
              f"got {cmd3[i3+1]}")
    finally:
        if os.path.exists(home_draft):
            os.remove(home_draft)

    # d) missing draft -> no --model-draft flag
    cfg4 = {"filename": "main.gguf", "ctxSize": 4096,
            "draftModel": "does_not_exist.gguf"}
    cmd4, _ = SwitcherAPIHandler._build_cmd(cfg4, "/bin/true", data_dir, 9000)
    check("missing draft omits --model-draft", "--model-draft" not in cmd4)


def test_build_cmd_kv_and_flash():
    print("\n[7] _build_cmd KV-quant + flash-attn flags")
    tmp = tempfile.mkdtemp()
    open(os.path.join(tmp, "main.gguf"), "w").close()

    cfg = {"filename": "main.gguf", "ctxSize": 4096, "kvQuant": "q8_0"}
    cmd, _ = SwitcherAPIHandler._build_cmd(cfg, "/bin/true", tmp, 9000)
    check("kvQuant q8_0 -> cache-type-k/v",
          "--cache-type-k" in cmd and "--cache-type-v" in cmd)

    cfg2 = {"filename": "main.gguf", "ctxSize": 4096, "kvQuant": "f16",
            "flashAttention": True}
    cmd2, _ = SwitcherAPIHandler._build_cmd(cfg2, "/bin/true", tmp, 9000)
    check("kvQuant f16 -> no cache-type flags", "--cache-type-k" not in cmd2)
    check("flashAttention -> --flash-attn on",
          "--flash-attn" in cmd2 and cmd2[cmd2.index("--flash-attn") + 1] == "on")


def test_whitelists_include_new_keys():
    print("\n[8] PATCH/PUT whitelists + creation accept new keys")
    new_keys = {"kvQuant", "flashAttention", "draftModel", "draftGpuLayers"}

    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "api.py")).read()
    tree = ast.parse(src)

    allowed_sets = []
    runtime_sets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "allowed" and \
                        isinstance(node.value, ast.Set):
                    allowed_sets.append({e.value for e in node.value.elts
                                         if isinstance(e, ast.Constant)})
                if isinstance(t, ast.Name) and t.id == "runtime_params" and \
                        isinstance(node.value, ast.Set):
                    runtime_sets.append({e.value for e in node.value.elts
                                         if isinstance(e, ast.Constant)})

    check("found PATCH + PUT allowed sets", len(allowed_sets) >= 2,
          f"found {len(allowed_sets)}")
    for i, s in enumerate(allowed_sets):
        check(f"allowed set {i+1} includes new keys", new_keys <= s,
              f"missing {new_keys - s}")
    for i, s in enumerate(runtime_sets):
        check(f"runtime_params set {i+1} includes new keys", new_keys <= s,
              f"missing {new_keys - s}")

    # creation endpoint: function body mentions all four
    check("handle_api_models_post references draftModel",
          'data.get("draftModel")' in src)
    check("handle_api_models_post references kvQuant",
          'data.get("kvQuant")' in src)


if __name__ == "__main__":
    test_gateway_routing_decision()
    test_gateway_post_body_bounded()
    test_gateway_proxy_exception_contained()
    test_handle_api_active_no_nameerror()
    test_handle_api_status_no_nameerror()
    test_build_cmd_draft_resolution()
    test_build_cmd_kv_and_flash()
    test_whitelists_include_new_keys()
    print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(1 if FAIL else 0)
