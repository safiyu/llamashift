#!/usr/bin/env python3
"""
Hugging Face Hub GGUF browser & resumable background downloader.

Features:
  * search_hub()      - query the HF Hub API for GGUF repos/files
  * list_repo_files() - list .gguf / .mmproj files in a repo
  * start_download()  - launch a resumable chunked download in a worker thread
  * cancel_download() - stop an in-flight download
  * get_tasks_status()- snapshot of all download tasks (for API + WebSocket)

Downloads:
  * use HTTP Range: bytes=X- headers to resume after interruption
  * verify SHA256 when the repo provides a LFS hash pointer
  * auto-register the model into config.json on completion
  * enforce a pre-flight free-disk check (110% of expected size)
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import uuid

HF_API = "https://huggingface.co/api"
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{path}"

_TASKS = {}
_TASKS_LOCK = threading.Lock()


def _http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "llama-shift/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_bytes(url, timeout=30, headers=None):
    hdrs = {"User-Agent": "llama-shift/2.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def search_hub(query, limit=20):
    """Search Hugging Face for GGUF repositories matching *query*."""
    q = (query or "").strip()
    params = urllib.parse.urlencode({
        "search": q,
        "sort": "downloads",
        "direction": "-1",
        "limit": limit,
        "filter": "gguf",
    })
    url = f"{HF_API}/models?{params}"
    results = _http_get_json(url)
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        repo_id = r.get("id") or r.get("modelId")
        if not repo_id:
            continue
        out.append({
            "repo_id": repo_id,
            "downloads": r.get("downloads", 0),
            "likes": r.get("likes", 0),
            "tags": r.get("tags", []),
            "updated_at": r.get("lastModified") or r.get("createdAt"),
        })
    return out


def list_repo_files(repo_id, pattern=("gguf", "mmproj")):
    """List downloadable files (gguf/mmproj) in a repo with size + LFS hash."""
    url = f"{HF_API}/models/{urllib.parse.quote(repo_id)}/tree/main"
    try:
        entries = _http_get_json(url)
    except Exception:
        # Try root listing endpoint variant
        entries = _http_get_json(f"{HF_API}/models/{urllib.parse.quote(repo_id)}/tree/main?recursive=true")

    files = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        path = e.get("path") or ""
        if not any(path.lower().endswith("." + ext) for ext in pattern):
            continue
        size = 0
        # size lives under lfs or directly
        lfs = e.get("lfs") or {}
        size = lfs.get("size") or e.get("size") or 0
        files.append({
            "filename": os.path.basename(path),
            "repo_path": path,
            "size": int(size),
            "size_human": _human_size(int(size)),
            "sha256": lfs.get("hash"),
            "url": HF_RESOLVE.format(repo=urllib.parse.quote(repo_id),
                                     path=urllib.parse.quote(path)),
        })
    return files


def _validate_target(data_dir, filename):
    """Sanitize filename and ensure the target stays inside data_dir."""
    safe = os.path.basename(filename or "")
    if not safe or safe.startswith(".") or not re.match(r"^[\w\.\- ]+$", safe):
        raise ValueError(f"Invalid filename: {filename!r}")
    base = os.path.abspath(os.path.expanduser(data_dir))
    target = os.path.abspath(os.path.join(base, safe))
    if os.path.commonpath([base, target]) != base:
        raise ValueError("Target path escapes dataDir")
    return base, target


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _register_model(data_dir, filename, name, repo_id):
    """Create a pre-configured entry in config.json for the downloaded file."""
    from config import _CONFIG, save_config, _models_lock, invalidate_models_cache, get_global_mode
    cfg = _CONFIG
    port = int(cfg.get("masterPort", 9000))

    # Derive a stable model id from the filename
    stem = re.sub(r"\.(gguf|mmproj)$", "", filename, flags=re.I)
    model_id = re.sub(r"[^a-zA-Z0-9_]", "_", stem)[:48].strip("_").lower()
    if not model_id:
        model_id = "model_" + uuid.uuid4().hex[:8]
    # Ensure uniqueness
    base_id = model_id
    n = 1
    while model_id in cfg.get("models", {}):
        n += 1
        model_id = f"{base_id}_{n}"

    entry = {
        "id": model_id,
        "name": name or stem,
        "desc": f"Downloaded from {repo_id}",
        "filename": filename,
        "port": port,
        "ctxSize": 8192,
        "nParallel": 1,
        "nGpuLayers": 99,
        "batchSize": 2048,
        "threads": 0,
        "temperature": 0.7,
        "maxTokens": 4096,
        "topP": 0.9,
        "devices": ["ROCm0"],
        "extraArgs": [],
        "endpoint": f"http://localhost:{port}/v1",
    }
    with _models_lock:
        cfg["models"][model_id] = entry
        invalidate_models_cache()
    save_config(cfg)
    return model_id


def _download_worker(task_id, repo_id, filename, url, sha256_expected,
                     data_dir, target, register, display_name):
    """Resumable chunked download with progress + ETA + speed tracking."""
    from config import _CONFIG
    tmp = target + ".part"
    chunk_size = 4 * 1024 * 1024
    start_bytes = 0
    try:
        if os.path.exists(tmp):
            start_bytes = os.path.getsize(tmp)

        # Pre-flight disk check
        try:
            free = shutil.disk_usage(os.path.dirname(target)).free
        except OSError:
            free = None

        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if not t or t["cancelled"]:
                return

        mode = "ab" if start_bytes > 0 else "wb"
        req_headers = {"User-Agent": "llama-shift/2.0"}
        if start_bytes > 0:
            req_headers["Range"] = f"bytes={start_bytes}-"

        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.getcode()
            if start_bytes > 0 and status != 206:
                # Server ignored the range -> restart from scratch
                start_bytes = 0
                req2 = urllib.request.Request(url, headers=req_headers)
                resp = urllib.request.urlopen(req2, timeout=60)

            total = None
            cr = resp.headers.get("Content-Range")
            if cr and "/" in cr:
                total = int(cr.split("/")[-1])
            else:
                cl = resp.headers.get("Content-Length")
                if cl:
                    total = start_bytes + int(cl)

            if free is not None and total is not None:
                if free < total * 1.1:
                    raise OSError(
                        f"Insufficient disk space: need ~{_human_size(total*1.1)}, "
                        f"have {_human_size(free)}")

            with open(tmp, mode) as f:
                downloaded = start_bytes
                last_update = time.time()
                last_downloaded = start_bytes
                last_total = total or 0
                while True:
                    with _TASKS_LOCK:
                        t = _TASKS.get(task_id)
                        if not t or t["cancelled"]:
                            break
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_update >= 0.5:
                        speed = (downloaded - last_downloaded) / max(now - last_update, 1e-6)
                        t = _TASKS.get(task_id)
                        if t:
                            t["bytes_downloaded"] = downloaded
                            if total:
                                t["total_bytes"] = total
                                t["progress"] = min(1.0, downloaded / total)
                                t["eta_seconds"] = int((total - downloaded) / max(speed, 1))
                            t["speed_mbps"] = round(speed / (1024 * 1024), 2)
                        last_update = now
                        last_downloaded = downloaded
                        last_total = total or 0

        if os.path.exists(tmp):
            downloaded = os.path.getsize(tmp)

        # SHA256 verification
        if sha256_expected:
            actual = _sha256_file(tmp)
            if actual.lower() != sha256_expected.lower():
                os.remove(tmp)
                raise ValueError(
                    f"SHA256 mismatch: expected {sha256_expected[:12]}…, "
                    f"got {actual[:12]}…")

        os.replace(tmp, target)

        registered_id = None
        if register:
            try:
                registered_id = _register_model(data_dir, filename, display_name,
                                                repo_id)
            except Exception as e:
                print(f"[hub] Auto-register failed: {e}")

        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t:
                t["status"] = "complete"
                t["progress"] = 1.0
                t["eta_seconds"] = 0
                t["finished_at"] = time.time()
                t["target_path"] = target
                t["registered_model"] = registered_id
                t["bytes_downloaded"] = os.path.getsize(target)
        print(f"[hub] Download complete: {target}"
              + (f" (registered as '{registered_id}')" if registered_id else ""))

    except Exception as e:
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t:
                if t["cancelled"]:
                    t["status"] = "cancelled"
                else:
                    t["status"] = "error"
                    t["error"] = str(e)
                t["finished_at"] = time.time()
        print(f"[hub] Download failed: {e}")


def start_download(repo_id, filename, target_dir=None, register=True,
                   url=None, sha256=None, display_name=None):
    """Start a background download. Returns the task_id."""
    from config import _CONFIG
    data_dir = target_dir or _CONFIG.get("dataDir", "~/models")
    base, target = _validate_target(data_dir, filename)
    os.makedirs(base, exist_ok=True)

    if not url:
        url = HF_RESOLVE.format(repo=urllib.parse.quote(repo_id),
                                path=urllib.parse.quote(filename))

    task_id = uuid.uuid4().hex
    task = {
        "task_id": task_id,
        "repo_id": repo_id,
        "filename": filename,
        "url": url,
        "target_path": target,
        "status": "downloading",
        "bytes_downloaded": 0,
        "total_bytes": 0,
        "progress": 0.0,
        "speed_mbps": 0.0,
        "eta_seconds": 0,
        "created_at": time.time(),
        "cancelled": False,
        "registered_model": None,
    }
    with _TASKS_LOCK:
        _TASKS[task_id] = task

    worker = threading.Thread(
        target=_download_worker,
        args=(task_id, repo_id, filename, url, sha256, base, target,
              register, display_name),
        daemon=True, name=f"download-{task_id[:8]}")
    worker.start()
    print(f"[hub] Started download {task_id} -> {target}")
    return task_id


def cancel_download(task_id):
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if not t:
            return False
        if t["status"] in ("downloading", "paused"):
            t["cancelled"] = True
            t["status"] = "cancelling"
            return True
    return False


def get_tasks_status(include_finished=True):
    with _TASKS_LOCK:
        tasks = list(_TASKS.values())
    if not include_finished:
        tasks = [t for t in tasks if t["status"] not in
                 ("complete", "error", "cancelled")]
    out = []
    for t in tasks:
        d = dict(t)
        d.pop("cancelled", None)
        if t["total_bytes"]:
            d["total_human"] = _human_size(t["total_bytes"])
        d["downloaded_human"] = _human_size(t["bytes_downloaded"])
        out.append(d)
    return sorted(out, key=lambda x: x.get("created_at", 0), reverse=True)


def _human_size(size_bytes):
    if not size_bytes:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"
