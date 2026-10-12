#!/usr/bin/env python3
"""
Process management module for llama-shift server.
Handles cross-platform process detection, stats, GPU telemetry, and process lifecycle.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from config import IS_WINDOWS, IS_LINUX, IS_MACOS

# Global process tracker — keyed by model_id -> Popen proc
_model_processes = {}
_proc_lock = threading.Lock()

# Global process tracking for cleanup
_active_processes = set()
_active_processes_lock = threading.Lock()

# Log directory — derived from config appName (set after load)
LOG_DIR = None

# MCP server process tracking
_mcp_process = None
_mcp_lock = threading.Lock()

# Log file handles for streaming logs to clients
_log_handles = {}
_log_lock = threading.Lock()

# Security log lock
_security_log_lock = threading.Lock()

# ─── In-flight gateway requests and swap draining ──────────────────────────
# Gateway requests are counted per upstream port for their whole exchange
# (streams included) so a single_port swap can wait for them before killing
# the old model. Requests sent straight to a model port are not tracked.
_flight_cv = threading.Condition()
_inflight = {}   # port -> number of in-flight gateway requests
_swapping = {}   # port -> (incoming model id, reject_all) while a swap is draining or loading


def try_begin_request(port, model_id):
    """Count a request on *port*; False when the port is being swapped to another model."""
    with _flight_cv:
        swap = _swapping.get(port)
        if swap is not None and (swap[1] or swap[0] != model_id):
            return False
        _inflight[port] = _inflight.get(port, 0) + 1
        return True


def end_request(port):
    with _flight_cv:
        n = _inflight.get(port, 0) - 1
        if n > 0:
            _inflight[port] = n
        else:
            _inflight.pop(port, None)
        _flight_cv.notify_all()


def inflight_count(port):
    with _flight_cv:
        return _inflight.get(port, 0)


def is_swapped_out(port, model_id):
    """True when *port* is mid-swap and *model_id* is not the model coming in."""
    with _flight_cv:
        swap = _swapping.get(port)
        return swap is not None and (swap[1] or swap[0] != model_id)


def begin_swap(port, incoming_model_id, restart=False):
    """Mark *port* as swapping; new gateway requests for other models get 503 model_swapping."""
    with _flight_cv:
        _swapping[port] = (incoming_model_id, bool(restart))


def end_swap(port):
    with _flight_cv:
        _swapping.pop(port, None)
        _flight_cv.notify_all()


def drain_port(port, timeout=None):
    """Wait until no gateway request is in flight on *port*; False if the timeout expired."""
    if timeout is None:
        from config import get_drain_timeout
        timeout = get_drain_timeout()
    deadline = time.monotonic() + float(timeout)
    with _flight_cv:
        while _inflight.get(port, 0) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"[drain] Port {port}: {_inflight.get(port, 0)} request(s) still in flight "
                      f"after {float(timeout):.0f}s, proceeding with the swap")
                return False
            _flight_cv.wait(min(remaining, 0.5))
    return True


def drain_and_kill(port, pid, incoming_model_id, restart=False):
    """Mark the swap, let in-flight requests finish (bounded), then kill the old model."""
    begin_swap(port, incoming_model_id, restart)
    if inflight_count(port):
        print(f"[drain] Waiting for {inflight_count(port)} in-flight request(s) on port {port}...")
    drain_port(port)
    return kill_process(pid)


def end_swap_when_healthy(swap_ports, health_port, timeout):
    """Release swap marks once the new model answers /health (or after *timeout*)."""
    def _run():
        import http.client
        deadline = time.monotonic() + float(timeout)
        try:
            while time.monotonic() < deadline:
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", int(health_port), timeout=2)
                    conn.request("GET", "/health")
                    ok = conn.getresponse().status == 200
                    conn.close()
                    if ok:
                        return
                except Exception:
                    pass
                time.sleep(0.5)
        finally:
            for p in swap_ports:
                end_swap(p)
    threading.Thread(target=_run, daemon=True, name="swap-release").start()


# ─── Per-model activity tracking (idle reaper) ─────────────────────────────
# model_id -> last activity timestamp (monotonic-ish via time.time()).
# A model is "active" when it has served a proxied inference request recently;
# the idle reaper terminates models that stay quiet past idleTimeoutSeconds.
_model_last_activity = {}
_model_activity_lock = threading.Lock()

# Idle-reaper worker thread
_idle_reaper_thread = None
_idle_reaper_stop = threading.Event()


def touch_model_activity(model_id, port=None):
    """Record activity for *model_id* (optionally recording its port).

    Called on every proxied inference request and on manual start, so the
    idle reaper only reclaims models that are genuinely unused.
    """
    now = time.time()
    with _model_activity_lock:
        _model_last_activity[model_id] = now
        if port is not None:
            _model_last_activity[f"{model_id}__port"] = port


def get_model_activity(model_id):
    """Return (last_activity_ts, port) for *model_id* or (None, None)."""
    with _model_activity_lock:
        ts = _model_last_activity.get(model_id)
        port = _model_last_activity.get(f"{model_id}__port")
    return ts, port


def wait_for_port_free(port, timeout=10):
    """Block until *port* is no longer accepting connections (up to *timeout* s)."""
    import socket
    for _ in range(max(1, int(timeout) * 2)):  # 0.5 s steps
        _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _sock.settimeout(1)
            if _sock.connect_ex(('127.0.0.1', int(port))) != 0:
                return
        finally:
            _sock.close()
        time.sleep(0.5)
    print(f"[port] Port {port} still in use after {timeout}s, proceeding anyway")


def reap_idle_models(idle_timeout_seconds):
    """Terminate gateway-tracked models idle longer than *idle_timeout_seconds*.

    Only models whose activity was recorded via touch_model_activity()
    (proxied requests or UI starts) are considered. Returns a list of the
    model ids that were reaped (for WebSocket notifications).
    """
    if not idle_timeout_seconds or idle_timeout_seconds <= 0:
        return []

    from config import get_global_mode, MODELS, _CONFIG
    _cleanup_stale_tracked_processes()
    running = get_running_servers()
    if not running:
        return []

    mode = get_global_mode()
    master_port = _CONFIG.get("masterPort", 9000)
    now = time.time()

    # Map model_id -> pid of a currently-running process matching that model.
    candidates = {}
    for port, info in running.items():
        pid = info.get("pid")
        if not pid:
            continue
        cmdline = get_process_cmdline(pid)
        for mid, cfg in MODELS.items():
            if mid in candidates:
                continue
            if mode == "single_port" and not cfg.get("dedicated"):
                if port == master_port and cfg.get("filename", "") in cmdline:
                    candidates[mid] = (pid, port)
            else:
                if _process_matches_model(cmdline, cfg, mid):
                    candidates[mid] = (pid, port)
            if len(candidates) == len(MODELS):
                break

    reap_dedicated = bool(_CONFIG.get("idleReapDedicated", False))
    reaped = []
    for mid, (pid, port) in list(candidates.items()):
        ts, _ = get_model_activity(mid)
        if ts is None:
            continue  # never touched via proxy/UI — leave it alone
        if now - ts < idle_timeout_seconds:
            continue
        if MODELS[mid].get("dedicated") and not reap_dedicated:
            continue
        if inflight_count(port):
            continue  # a long stream is still being served
        print(f"[reaper] Model {mid} idle for {int(now - ts)}s "
              f"(> {int(idle_timeout_seconds)}s), unloading")
        reaped.append(mid)
        with _proc_lock:
            _model_processes.pop(mid, None)
            with _active_processes_lock:
                _active_processes.discard(mid)
        with _log_lock:
            handle = _log_handles.pop(mid, None)
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass
        try:
            kill_process(pid)
        except Exception as e:
            print(f"[reaper] Failed to stop {mid}: {e}")
    return reaped


def start_idle_reaper(interval_seconds=15):
    """Start (once) the background idle-reaper daemon thread."""
    global _idle_reaper_thread
    if _idle_reaper_thread is not None and _idle_reaper_thread.is_alive():
        return
    _idle_reaper_stop.clear()
    _idle_reaper_thread = threading.Thread(
        target=_idle_reaper_loop, args=(interval_seconds,),
        daemon=True, name="idle-reaper")
    _idle_reaper_thread.start()
    print(f"[reaper] Idle auto-unload worker started (checks every {interval_seconds}s)")


def stop_idle_reaper():
    """Stop the background idle-reaper thread (idempotent)."""
    global _idle_reaper_thread
    _idle_reaper_stop.set()
    if _idle_reaper_thread:
        _idle_reaper_thread.join(timeout=3)
        _idle_reaper_thread = None


def _idle_reaper_loop(interval_seconds=15):
    """Worker loop: reap idle models, push state updates via WebSocket."""
    import config
    while not _idle_reaper_stop.is_set():
        try:
            timeout_s = float((config._CONFIG or {}).get("idleTimeoutSeconds", 0) or 0)
            if timeout_s > 0:
                reaped = reap_idle_models(timeout_s)
                if reaped:
                    print(f"[reaper] Unloaded idle model(s): {', '.join(reaped)}")
                    _notify_state_changed(reaped)
        except Exception as e:
            print(f"[reaper] Error: {e}")
        _idle_reaper_stop.wait(interval_seconds)


def _notify_state_changed(reaped_model_ids):
    """Push an immediate WebSocket state update after an unload event."""
    try:
        import asyncio
        from telemetry import _ws_broadcast
        from config import get_server_port

        def _payload():
            return {
                "type": "state",
                "timestamp": time.time(),
                "reason": "idle_unload",
                "unloaded": reaped_model_ids,
                "gateway": f"http://localhost:{get_server_port()}/v1",
            }

        sent = {"done": False}

        def _run():
            try:
                asyncio.run(_ws_broadcast(_payload()))
            except Exception:
                pass
            finally:
                sent["done"] = True

        threading.Thread(target=_run, daemon=True, name="ws-unload-notify").start()
    except Exception:
        pass


# ─── Cross-Platform Process Detection ──────────────────────────────────────
def get_running_servers():
    """Scans running llama-server processes on the system and maps them by port."""
    running = {}
    
    if IS_WINDOWS:
        # Windows: use tasklist + wmic or powershell
        try:
            res = subprocess.run(
                ["powershell", "-Command", 
                 "Get-Process | Where-Object { $_.ProcessName -like '*llama*' } | "
                 "Select-Object Id, Id, CommandLine | Format-List"],
                capture_output=True, text=True, timeout=10
            )
            # Fallback: simpler approach
            if res.returncode != 0:
                res = subprocess.run(
                    ["wmic", "process", "where", "name='llama-server.exe' OR name='llama-server'", 
                     "get", "ProcessId,CommandLine"],
                    capture_output=True, text=True, timeout=10
                )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines()[1:]:  # skip header
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    if len(parts) >= 2:
                        pid_str, cmd = parts
                        if "llama-server" in cmd:
                            port_match = re.search(r'(?:--port|-p)\s+(\d+)', cmd)
                            if port_match:
                                try:
                                    running[int(port_match.group(1))] = {
                                        "pid": int(pid_str), "cmd": cmd
                                    }
                                except ValueError:
                                    pass
        except Exception as e:
            print(f"[process] Windows process scan error: {e}")
    else:
        # Linux (including Docker): use ps
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,args"], 
                capture_output=True, text=True, check=True
            )
            lines = res.stdout.splitlines()
        except Exception as e:
            print(f"[process] Linux process scan error: {e}")
            return running

        for line in lines[1:]:  # skip header
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid_str, cmd = parts
            
            if "llama-server" in cmd:
                port_match = re.search(r'(?:--port|-p)\s+(\d+)', cmd)
                if port_match:
                    try:
                        pid = int(pid_str)
                        running[int(port_match.group(1))] = {"pid": pid, "cmd": cmd}
                    except ValueError:
                        pass
    return running


# ─── Cross-Platform Process Stats ──────────────────────────────────────────
def get_process_stats(pid):
    """Retrieves CPU %, MEM %, and Uptime for a given PID."""
    try:
        if IS_WINDOWS:
            res = subprocess.run(
                ["powershell", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
                 "Select-Object @{N='CPU';E={[math]::Round($_.CPU,1)}}, "
                 "@{N='WS';E={[math]::Round($_.WorkingSet/1MB,1)}}, "
                 "@{N='Elapsed';E={$_.StartTime - (Get-Date)}}"],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                # Simplified Windows stats
                return {"cpu": 0.0, "mem": 0.0, "uptime": "Running"}
        else:
            # BSD ps (macOS) has no --no-headers; "=" suppresses each header instead
            ps_args = (["ps", "-p", str(pid), "-o", "%cpu=,%mem=,etime="] if IS_MACOS
                       else ["ps", "-p", str(pid), "-o", "%cpu,%mem,etime", "--no-headers"])
            res = subprocess.run(ps_args, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                parts = res.stdout.strip().split()
                if len(parts) >= 3:
                    return {
                        "cpu": float(parts[0]),
                        "mem": float(parts[1]),
                        "uptime": parts[2]
                    }
    except Exception:
        pass
    return {"cpu": 0.0, "mem": 0.0, "uptime": "Unknown"}


def get_host_stats():
    """Reads system stats - cross-platform compatible."""
    cpu_load = 0.0
    mem_used = 0
    mem_total = 0
    mem_pct = 0.0
    cpu_count = 0
    load_avg_1m = 0.0
    load_avg_5m = 0.0
    load_avg_15m = 0.0
    cpu_temp = 0.0

    if IS_LINUX:
        # CPU load and load averages from /proc/loadavg
        try:
            with open("/proc/loadavg", "r") as f:
                loadavg_data = f.read().split()
                if len(loadavg_data) >= 4:
                    load_avg_1m = float(loadavg_data[0])
                    load_avg_5m = float(loadavg_data[1])
                    load_avg_15m = float(loadavg_data[2])
                    cpu_load = load_avg_1m
        except Exception:
            pass

        # CPU count from /proc/cpuinfo
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo_data = f.read()
                cpu_count = cpuinfo_data.count("processor\t")
                if cpu_count == 0:
                    nproc_res = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
                    if nproc_res.returncode == 0:
                        cpu_count = int(nproc_res.stdout.strip())
        except Exception:
            try:
                nproc_res = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
                if nproc_res.returncode == 0:
                    cpu_count = int(nproc_res.stdout.strip())
            except Exception:
                cpu_count = 0

        # CPU temperature from hwmon sensors
        try:
            hwmon_base = "/sys/class/hwmon"
            if os.path.isdir(hwmon_base):
                for hwmon_dir in os.listdir(hwmon_base):
                    hwmon_path = os.path.join(hwmon_base, hwmon_dir)
                    temp_input_files = []
                    try:
                        if os.path.isdir(hwmon_path):
                            for f_name in os.listdir(hwmon_path):
                                if f_name.startswith("temp") and f_name.endswith("_input"):
                                    temp_input_files.append(f_name)
                            if os.path.exists(os.path.join(hwmon_path, "name")):
                                with open(os.path.join(hwmon_path, "name"), "r") as nf:
                                    hwmon_name = nf.read().strip()
                    except (IOError, OSError):
                        continue

                    for temp_file in sorted(temp_input_files):
                        temp_path = os.path.join(hwmon_path, temp_file)
                        try:
                            with open(temp_path, "r") as tf:
                                temp_milli_c = int(tf.read().strip())
                                temp_c = temp_milli_c / 1000.0
                                if 20.0 <= temp_c <= 100.0:
                                    cpu_temp = max(cpu_temp, temp_c)
                        except (IOError, OSError, ValueError):
                            continue
        except Exception:
            pass

        try:
            with open("/proc/meminfo", "r") as f:
                mem_info = {}
                mem_cached_kb = 0
                mem_swap_total_kb = 0
                mem_swap_used_kb = 0
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = int(parts[1].split()[0])
                        mem_info[key] = val
                        if key in ("MemFree", "Buffers", "Cached", "SReclaimable"):
                            mem_cached_kb += val
                        if key == "SwapTotal":
                            mem_swap_total_kb = val
                        if key == "SwapFree":
                            mem_swap_used_kb = mem_swap_total_kb - val
                mem_total = mem_info.get("MemTotal", 0) // 1024
                mem_free = mem_info.get("MemFree", 0) // 1024
                mem_avail = mem_info.get("MemAvailable", mem_free) // 1024
                mem_used = mem_total - mem_avail
                mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0.0
        except Exception:
            pass
    elif IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            c_ulong = ctypes.c_ulong
            
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", c_ulong),
                    ("dwMemoryLoad", c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            mem_total = stat.ullTotalPhys // (1024 * 1024)
            mem_avail = stat.ullAvailPhys // (1024 * 1024)
            mem_used = mem_total - mem_avail
            mem_pct = stat.dwMemoryLoad
            
            cpu_res = subprocess.run(
                ["powershell", "-Command",
                 "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"],
                capture_output=True, text=True, timeout=10
            )
            if cpu_res.returncode == 0:
                cpu_load = float(cpu_res.stdout.strip()) / 100.0
        except Exception:
            pass
    else:
        # Docker fallback
        try:
            with open("/proc/loadavg", "r") as f:
                cpu_load = float(f.read().split()[0])
        except Exception:
            cpu_load = 0.0
        try:
            with open("/proc/meminfo", "r") as f:
                mem_info = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem_info[parts[0].strip()] = int(parts[1].split()[0])
                mem_total = mem_info.get("MemTotal", 0) // 1024
                mem_avail = mem_info.get("MemAvailable", mem_info.get("MemFree", 0)) // 1024
                mem_used = mem_total - mem_avail
                mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0.0
        except Exception:
            pass

    cpu_count_str = f"{cpu_count} Cores" if cpu_count > 0 else "--"
    load_avg_str = ""
    if load_avg_1m > 0:
        load_avg_str = f"{load_avg_1m:.2f}"
        if cpu_count > 0:
            load_avg_str += f" / {cpu_count}"

    return {
        "cpu_load": round(cpu_load, 2),
        "cpu_count": cpu_count,
        "cpu_count_str": cpu_count_str,
        "load_avg_1m": round(load_avg_1m, 2),
        "load_avg_5m": round(load_avg_5m, 2),
        "load_avg_15m": round(load_avg_15m, 2),
        "cpu_temp": round(cpu_temp, 1),
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_pct": round(mem_pct, 1),
    }


_nvml_lib = None
_nvml_initialized = False


def _get_nvidia_power_fallback():
    """Fallback power reader for NVIDIA GPUs when power.draw reports N/A (e.g. RTX 40-series)."""
    global _nvml_lib, _nvml_initialized
    try:
        import ctypes
        import ctypes.util
        if _nvml_lib is None:
            lib_name = ctypes.util.find_library('nvidia-ml') or 'libnvidia-ml.so.1' or 'libnvidia-ml.so'
            if lib_name:
                _nvml_lib = ctypes.CDLL(lib_name)
        if _nvml_lib:
            if not _nvml_initialized:
                if _nvml_lib.nvmlInit() == 0:
                    _nvml_initialized = True
            if _nvml_initialized:
                dev = ctypes.c_void_p()
                if _nvml_lib.nvmlDeviceGetHandleByIndex(0, ctypes.byref(dev)) == 0:
                    p_mw = ctypes.c_uint()
                    if _nvml_lib.nvmlDeviceGetPowerUsage(dev, ctypes.byref(p_mw)) == 0 and p_mw.value > 0:
                        return round(p_mw.value / 1000.0, 1)

                    class _NvmlVal(ctypes.Union):
                        _fields_ = [
                            ('dVal', ctypes.c_double),
                            ('uiVal', ctypes.c_uint),
                            ('ulVal', ctypes.c_ulong),
                            ('ullVal', ctypes.c_ulonglong),
                            ('sval', ctypes.c_char * 8),
                        ]
                    class _NvmlSample(ctypes.Structure):
                        _fields_ = [
                            ('timeStamp', ctypes.c_ulonglong),
                            ('sampleValue', _NvmlVal)
                        ]
                    cnt = ctypes.c_uint(5)
                    samples = (_NvmlSample * 5)()
                    stype = ctypes.c_uint()
                    if _nvml_lib.nvmlDeviceGetSamples(dev, 0, 0, ctypes.byref(stype), ctypes.byref(cnt), samples) == 0 and cnt.value > 0:
                        latest = samples[cnt.value - 1]
                        if stype.value == 1 and latest.sampleValue.uiVal > 0:
                            return round(latest.sampleValue.uiVal / 1000.0, 1)
                        elif stype.value == 0 and latest.sampleValue.dVal > 0:
                            return round(latest.sampleValue.dVal, 1)
    except Exception:
        pass

    try:
        res = subprocess.run(['nvidia-smi', '-q', '-d', 'POWER'], capture_output=True, text=True, timeout=3)
        if res.returncode == 0 and res.stdout:
            m = re.search(r'Power Samples.*?Avg\s*:\s*([\d\.]+)\s*W', res.stdout, re.DOTALL)
            if m:
                return float(m.group(1))
    except Exception:
        pass

    return 0.0


def get_free_vram_mib():
    """Free VRAM in MiB summed over the NVIDIA and AMD GPUs seen by telemetry; None if unknown."""
    try:
        tele = get_gpu_telemetry() or {}
        free = 0.0
        for vendor in ("nvidia", "amd"):
            gpu = tele.get(vendor)
            if not isinstance(gpu, dict):
                continue
            total = float(gpu.get("mem_total", 0) or 0)
            used = float(gpu.get("mem_used", 0) or 0)
            if total > 0:
                free += max(0.0, total - used)
        return free if free > 0 else None
    except Exception:
        return None


def get_gpu_telemetry():
    """Gathers real-time GPU statistics for NVIDIA and AMD."""
    nvidia_data = None
    amd_data = None

    # 1. NVIDIA Telemetry
    try:
        res = subprocess.run([
            "nvidia-smi",
            "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
            "--format=csv,noheader,nounits"
        ], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.splitlines()[0].split(",")]
            if len(parts) >= 5:
                power_val = 0.0
                if len(parts) >= 6:
                    power_str = parts[5]
                    if not power_str.startswith("[") and "N/A" not in power_str:
                        try:
                            power_val = float(power_str)
                        except ValueError:
                            pass
                if power_val <= 0.0:
                    power_val = _get_nvidia_power_fallback()

                nvidia_data = {
                    "name": parts[0],
                    "temp": float(parts[1]),
                    "util": float(parts[2]),
                    "mem_used": float(parts[3]),
                    "mem_total": float(parts[4]),
                    "power": power_val
                }
    except FileNotFoundError:
        print("[gpu] nvidia-smi not found (NVIDIA GPU may not be available)")
    except subprocess.TimeoutExpired:
        print("[gpu] nvidia-smi timed out")
    except Exception as e:
        print(f"[gpu] Error querying NVIDIA GPU: {e}")

    # 2. AMD Telemetry
    if IS_WINDOWS:
        try:
            ps_cmd = (
                "Get-CimInstance Win32_VideoController | "
                "Where-Object { $_.Name -match 'AMD|RADEON|Radeon|Graphics' -and "
                "$_.Name -notmatch 'NVIDIA|Intel' } | "
                "Select-Object -First 1 -Property Name, AdapterRAM | "
                "ConvertTo-Json -Compress"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                gpu_info = json.loads(res.stdout.strip())
                if gpu_info:
                    gpu_name = gpu_info.get("Name", "AMD Radeon GPU")
                    adapter_ram = gpu_info.get("AdapterRAM", 0)
                    amd_data = {
                        "name": gpu_name,
                        "temp": 0.0,
                        "util": 0.0,
                        "mem_used": 0.0,
                        "mem_total": adapter_ram / (1024 * 1024) if adapter_ram else 0.0,
                        "power": 0.0,
                    }
                    print(f"[gpu] Windows AMD GPU detected: {gpu_name}")
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"[gpu] Windows AMD GPU query failed: {e}")
    else:
        # Linux AMD detection
        def _rocm_smi_detect():
            try:
                res = subprocess.run([
                    "rocm-smi",
                    "--showtemp",
                    "--showuse",
                    "--showpower",
                    "--showmeminfo", "vram",
                    "--json"
                ], capture_output=True, text=True, timeout=10)
                if res.returncode != 0 or not res.stdout.strip():
                    return None
                json_line = None
                for line in res.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("{"):
                        json_line = stripped
                        break
                if json_line is None:
                    return None
                raw = json.loads(json_line)
                card = raw.get("card0", {})

                def safe_float(val, default=0.0):
                    try:
                        return float(str(val).replace("\u00b0C", "").replace("W", "").strip())
                    except (ValueError, TypeError):
                        return default

                def safe_int(val, default=0):
                    try:
                        return int(str(val).strip())
                    except (ValueError, TypeError):
                        return default

                def get_field(obj, *keys):
                    for k in keys:
                        v = obj.get(k)
                        if v is not None and str(v).lower() != 'n/a':
                            return v
                    return None

                temp_raw = get_field(card, "Temperature (Sensor edge) (C)", "Temperature (Tedge) (C)", "Temperature")
                util_raw = get_field(card, "GPU use (%)", "GPU Utilization (%)", "Average GPU Activity (%)", "Activity (%)")
                vram_used_raw = get_field(card, "VRAM Total Used Memory (B)", "VRAM Used (B)", "VRAM Total Used Memory")
                vram_total_raw = get_field(card, "VRAM Total Memory (B)", "VRAM Total (B)", "VRAM Total Memory")
                power_raw = get_field(card, "Average Graphics Package Power (W)", "Package Power (W)", "Average Package Power")

                gpu_name = "AMD Radeon GPU"
                try:
                    name_res = subprocess.run([
                        "rocm-smi", "--showproductname", "--json"
                    ], capture_output=True, text=True, timeout=5)
                    if name_res.returncode == 0 and name_res.stdout.strip():
                        for nline in name_res.stdout.splitlines():
                            nstripped = nline.strip()
                            if nstripped.startswith("{"):
                                name_raw = json.loads(nstripped)
                                name_card = name_raw.get("card0", {})
                                gpu_name = name_card.get("Card Series", "AMD Radeon GPU")
                                break
                except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
                    pass

                return {
                    "name": gpu_name,
                    "temp": safe_float(temp_raw),
                    "util": safe_float(util_raw),
                    "mem_used": safe_int(vram_used_raw) / (1024 * 1024),
                    "mem_total": safe_int(vram_total_raw) / (1024 * 1024),
                    "power": safe_float(power_raw)
                }
            except FileNotFoundError:
                return None
            except subprocess.TimeoutExpired:
                return None
            except Exception as e:
                print(f"[gpu] rocm-smi error: {e}")
                return None

        amd_data = _rocm_smi_detect()

    return {
        "nvidia": nvidia_data,
        "amd": amd_data
    }


def get_available_devices():
    """Enumerates available GPU devices for selection."""
    devices = []
    
    # Check for NVIDIA GPUs via nvidia-smi
    try:
        res = subprocess.run([
            "nvidia-smi",
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader,nounits"
        ], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    devices.append({
                        "id": f"nvidia-{parts[0].strip()}",
                        "name": parts[1].strip(),
                        "type": "nvidia",
                        "vram": float(parts[2]) if len(parts) > 2 else 0.0
                    })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass
    
    # Check for AMD GPUs via rocm-smi
    try:
        res = subprocess.run([
            "rocm-smi",
            "--showid",
            "--showproductname",
            "--json"
        ], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            json_line = None
            for line in res.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("{"):
                    json_line = stripped
                    break
            if json_line:
                raw = json.loads(json_line)
                card = raw.get("card0", {})
                gpu_name = card.get("Card Series", "AMD Radeon GPU")
                # rocm-smi doesn't provide VRAM directly, query separately
                vram_mb = 0.0
                try:
                    vram_res = subprocess.run([
                        "rocm-smi", "--showmeminfo", "vram", "--json"
                    ], capture_output=True, text=True, timeout=10)
                    if vram_res.returncode == 0 and vram_res.stdout.strip():
                        for vline in vram_res.stdout.splitlines():
                            vstripped = vline.strip()
                            if vstripped.startswith("{"):
                                vram_raw = json.loads(vstripped)
                                vram_card = vram_raw.get("card0", {})
                                vram_total = vram_card.get("VRAM Total Memory (B)", 0)
                                vram_mb = float(vram_total) / (1024 * 1024) if vram_total else 0.0
                                break
                except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
                    pass
                
                devices.append({
                    "id": f"amd-0",
                    "name": gpu_name,
                    "type": "amd",
                    "vram": vram_mb
                })
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    except Exception:
        pass
    
    # Fallback for Windows AMD detection (via PowerShell)
    if IS_WINDOWS and not devices:
        try:
            ps_cmd = (
                "Get-CimInstance Win32_VideoController | "
                "Where-Object { $_.Name -match 'AMD|RADEON|Radeon|Graphics' -and "
                "$_.Name -notmatch 'NVIDIA|Intel' } | "
                "Select-Object Name, AdapterRAM | "
                "ConvertTo-Json -Compress"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                gpu_info = json.loads(res.stdout.strip())
                if gpu_info:
                    gpu_name = gpu_info.get("Name", "AMD Radeon GPU")
                    adapter_ram = gpu_info.get("AdapterRAM", 0)
                    devices.append({
                        "id": f"amd-0",
                        "name": gpu_name,
                        "type": "amd",
                        "vram": adapter_ram / (1024 * 1024) if adapter_ram else 0.0
                    })
        except Exception:
            pass
    
    return devices


def get_process_cmdline(pid):
    """Get the command line arguments of a process as a string."""
    try:
        if IS_LINUX:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            if not raw:
                return ""
            cmdline = raw.decode("utf-8", errors="replace").replace("\x00", " ").strip()
            return cmdline
        elif IS_WINDOWS:
            res = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/format:list"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "CommandLine=" in line:
                        return line.split("=", 1)[1].strip('" ')
            return ""
        elif IS_MACOS:
            res = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True, timeout=5)
            return res.stdout.strip() if res.returncode == 0 else ""
        return ""
    except Exception:
        return ""


def get_process_env(binary_path):
    """Return an execution environment with appropriate LD_LIBRARY_PATH for the given binary.

    Prevents dynamic library collisions between ROCm and CUDA builds of llama-server.
    """
    proc_env = dict(os.environ)
    if not binary_path:
        return proc_env
    bin_dir = os.path.dirname(os.path.abspath(binary_path))
    old_ld = proc_env.get("LD_LIBRARY_PATH", "")
    # Strip any competing llama.cpp build dirs so ROCm and CUDA builds don't cross-link
    ld_parts = [p for p in old_ld.split(":") if p and not ("llama.cpp" in p and p != bin_dir)]
    proc_env["LD_LIBRARY_PATH"] = ":".join([bin_dir] + ld_parts)
    return proc_env


def _process_matches_model(cmdline, model_cfg, model_id):
    """Check if a process's command line matches this model."""
    if not cmdline:
        return False
    
    cmdline_lower = cmdline.lower()
    
    if "llama-server" not in cmdline_lower:
        return False
    
    port_str = f"--port {model_cfg.get('port', '')}"
    port_match = port_str in cmdline or f"--port={model_cfg.get('port', '')}" in cmdline
    
    filename = model_cfg.get("filename", "")
    if not filename:
        return False
    
    filename_lower = filename.lower()
    
    filename_match = False
    if filename_lower in cmdline_lower:
        idx = cmdline_lower.find(filename_lower)
        if idx >= 0:
            before = cmdline_lower[idx-1] if idx > 0 else ''
            if before in ('/', '\\'):
                filename_match = True
            else:
                context_before = cmdline_lower[max(0, idx-20):idx]
                if '--model' in context_before:
                    filename_match = True
    
    return port_match and filename_match


def _cleanup_stale_tracked_processes():
    """Remove tracked model processes that are dead or no longer match their config."""
    from config import MODELS
    stale_models = []
    with _proc_lock:
        for mid, proc in list(_model_processes.items()):
            dead = False
            if proc.poll() is not None:
                dead = True
            elif not is_llama_server(proc.pid):
                dead = True
            else:
                cmdline = get_process_cmdline(proc.pid)
                if mid in MODELS:
                    if not _process_matches_model(cmdline, MODELS[mid], mid):
                        dead = True

            if dead:
                stale_models.append(mid)
                _model_processes.pop(mid, None)
                with _active_processes_lock:
                    _active_processes.discard(mid)

    return stale_models


def _human_size(bytes_val):
    """Format bytes into human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"


def read_last_lines(filepath, n=100):
    """Reads the last n lines of a file safely."""
    if not os.path.exists(filepath):
        return "Server logs will populate here once the model starts..."
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return "".join(lines[-n:])
    except Exception as e:
        return f"Error reading log file: {str(e)}"


def is_llama_server(pid):
    """Check if a PID is actually a running llama-server process."""
    try:
        if IS_LINUX:
            with open(f"/proc/{pid}/cmdline", "r") as f:
                cmdline = f.read()
            return "llama-server" in cmdline
        elif IS_WINDOWS:
            res = subprocess.run(
                ["powershell", "-Command",
                 f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).MainModule.FileName"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                return "llama-server" in res.stdout.lower()
            return False
        else:
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True
            )
            return res.returncode == 0 and "llama-server" in res.stdout
    except (FileNotFoundError, PermissionError, ProcessLookupError, subprocess.TimeoutExpired):
        return False


def get_child_processes(pid):
    """Get a list of child process PIDs for a given parent PID."""
    child_pids = []
    try:
        if IS_WINDOWS:
            try:
                cmd = f'(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Children | Select-Object -ExpandProperty Id'
                res = subprocess.run(["powershell", "-Command", cmd], 
                                    capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.strip().split('\n'):
                        try:
                            child_pids.append(int(line.strip()))
                        except ValueError:
                            pass
            except Exception:
                pass
        elif IS_MACOS:
            try:
                res = subprocess.run(["pgrep", "-P", str(pid)],
                                    capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.split():
                        try:
                            child_pids.append(int(line.strip()))
                        except ValueError:
                            pass
            except Exception:
                pass
        else:
            try:
                res = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid=", "--no-headers"],
                                    capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.strip().split('\n'):
                        try:
                            child_pids.append(int(line.strip()))
                        except ValueError:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    return child_pids


def _pid_gone(pid):
    """True once *pid* no longer exists; reaps our own child first, since a zombie still answers kill(pid, 0)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def kill_process(pid):
    """Gracefully terminates a process by PID, including all child processes."""
    pid = int(pid)
    if not is_llama_server(pid):
        print(f"[!] PID {pid} is not a llama-server, skipping")
        return True

    child_pids = get_child_processes(pid)
    if child_pids:
        print(f"[shutdown] Found {len(child_pids)} child process(es) of PID {pid}, terminating...")
        for child_pid in child_pids:
            if is_llama_server(child_pid):
                print(f"[shutdown] Terminating child process PID {child_pid}")
                try:
                    if IS_WINDOWS:
                        subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], 
                                      capture_output=True, timeout=10)
                    else:
                        os.kill(child_pid, signal.SIGTERM)
                except Exception:
                    pass
        time.sleep(0.2)
    
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], 
                          capture_output=True, timeout=10)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                os.kill(pid, signal.SIGTERM)
            
            for _ in range(30):
                time.sleep(0.1)
                if _pid_gone(pid):
                    return True

            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
            return _pid_gone(pid)
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        return False


def _port_accepting(port):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def start_mcp_server():
    """Start the MCP server process; returns False (with a clear log line) if it dies at launch."""
    global _mcp_process

    with _mcp_lock:
        if _mcp_process and _mcp_process.poll() is None:
            print("[mcp] MCP server already running")
            return True

    import config
    mcp_port = config.get_mcp_port()
    if mcp_port == config.get_ws_port():
        print(f"[mcp] ERROR: MCP port {mcp_port} equals the WebSocket telemetry port; "
              f"set MCP_PORT or WS_PORT to different values. MCP not started.")
        return False

    if _port_accepting(mcp_port):
        print(f"[mcp] ERROR: port {mcp_port} is already in use; set MCP_PORT to a free port. MCP not started.")
        return False

    try:
        mcp_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
        config._ensure_log_dir()
        log_path = os.path.join(config.LOG_DIR, "mcp.log")
        env = {**os.environ, "MCP_PORT": str(mcp_port)}
        # Point MCP at the real HTTP port, and let it authenticate when a management key is set
        env.setdefault("MAIN_SERVER_URL", f"http://localhost:{(config._CONFIG or {}).get('serverPort', 8002)}")
        if (config._CONFIG or {}).get("managementApiKey"):
            env.setdefault("MCP_AUTH_TOKEN", str(config._CONFIG["managementApiKey"]))
        with open(log_path, "ab") as log_file:
            proc = subprocess.Popen(
                [sys.executable, mcp_script],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )

        deadline = time.monotonic() + 6.0  # the MCP SDK takes a few seconds to import
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = read_last_lines(log_path, 5).strip()
                print(f"[mcp] ERROR: MCP server exited at startup (code {proc.returncode}); "
                      f"port {mcp_port} may be in use. Last log lines:\n{tail}")
                return False
            if _port_accepting(mcp_port):
                break
            time.sleep(0.1)
        else:
            print(f"[mcp] WARNING: MCP server is running but not accepting connections on port "
                  f"{mcp_port} after 6s; check {log_path}")

        with _mcp_lock:
            _mcp_process = proc
        print(f"[mcp] Started MCP server with PID {proc.pid} on port {mcp_port}")
        return True
    except Exception as e:
        print(f"[mcp] Failed to start MCP server: {e}")
        return False


def stop_mcp_server():
    """Stop the MCP server process."""
    global _mcp_process
    
    with _mcp_lock:
        if _mcp_process:
            try:
                _mcp_process.terminate()
                try:
                    _mcp_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _mcp_process.kill()
                    _mcp_process.wait()
                print("[mcp] MCP server stopped")
            except Exception as e:
                print(f"[mcp] Error stopping MCP server: {e}")
            finally:
                _mcp_process = None


def _stop_all_model_processes():
    """Stop all model processes and clean up their log handles."""
    global _model_processes, _active_processes, _log_handles
    
    with _proc_lock:
        for model_id, proc_info in list(_model_processes.items()):
            try:
                proc = proc_info if hasattr(proc_info, "poll") else proc_info.get("process")
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
            except Exception as e:
                print(f"[cleanup] Error stopping process for {model_id}: {e}")
    
    with _active_processes_lock:
        _active_processes.clear()
    
    with _log_lock:
        _log_handles.clear()


def _close_all_log_handles():
    """Close all log file handles."""
    global _log_handles
    
    with _log_lock:
        for handle in _log_handles.values():
            try:
                handle.close()
            except Exception:
                pass
        _log_handles.clear()


# ─── WebSocket Handler ─────────────────────────────────────────────────────
# This will be handled in telemetry.py