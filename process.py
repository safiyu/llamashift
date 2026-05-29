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
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "%cpu,%mem,etime", "--no-headers"],
                capture_output=True, text=True
            )
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
            if len(parts) >= 6:
                power_str = parts[5]
                power_val = 0.0
                if not power_str.startswith("[") and "N/A" not in power_str:
                    try:
                        power_val = float(power_str)
                    except ValueError:
                        pass
                
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
        return ""
    except Exception:
        return ""


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
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
            
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        return False


def start_mcp_server():
    """Start the MCP server process."""
    global _mcp_process
    
    with _mcp_lock:
        if _mcp_process and _mcp_process.poll() is None:
            print("[mcp] MCP server already running")
            return True
    
    try:
        mcp_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
        _mcp_process = subprocess.Popen(
            [sys.executable, mcp_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
        print(f"[mcp] Started MCP server with PID {_mcp_process.pid}")
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
                proc = proc_info.get("process")
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