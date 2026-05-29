#!/usr/bin/env python3
"""
LlamaShift Service Uninstaller
Stops and removes LlamaShift services across all platforms.

Usage:
  sudo python3 uninstall_service.py          # Linux (systemd)
  python3 uninstall_service.py               # Windows (NSSM/schtasks)
  python3 uninstall_service.py --all         # Remove everything (service + config + venv)
  python3 uninstall_service.py --dry-run     # Preview without making changes
"""

import sys
import os
import platform
import subprocess
import shutil
import argparse
from pathlib import Path


# ─── Color helpers ────────────────────────────────────────────────────────
def use_color():
    return sys.stdout.isatty()


def C(text, color):
    if not use_color():
        return text
    codes = {
        "red": "31", "green": "32", "yellow": "33",
        "cyan": "36", "white": "37", "bold": "1",
    }
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"


def print_ok(text):
    print(f"  {C('✓', 'green')} {text}")


def print_warn(text):
    print(f"  {C('⚠', 'yellow')} {text}")


def print_err(text):
    print(f"  {C('✗', 'red')} {text}")


def print_info(text):
    print(f"  ℹ {text}")


def print_header(text):
    print(f"\n{C('═' * 60, 'cyan')}")
    print(f"  {C(text, 'bold')}")
    print(f"{'═' * 60}\n")


def confirm(prompt):
    """Ask yes/no confirmation."""
    try:
        answer = input(f"  {prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


# ─── Environment Detection ────────────────────────────────────────────────
def detect_environment():
    """Detect runtime: linux-systemd, linux, windows, docker."""
    system = platform.system().lower()

    # Docker check
    if os.path.exists("/.dockerenv"):
        return "docker"
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read()
            if "docker" in content or "kubepods" in content:
                return "docker"
    except (FileNotFoundError, PermissionError):
        pass

    if system == "linux":
        try:
            result = subprocess.run(
                ["systemctl", "--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return "linux-systemd"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "linux"

    if system == "windows":
        return "windows"

    return "linux"  # fallback


# ─── Service Detection ────────────────────────────────────────────────────
def find_services(env):
    """Find all LlamaShift-related services on the system."""
    services = []

    if env == "linux-systemd":
        # Check systemd service
        service_file = Path("/etc/systemd/system/llamashift.service")
        if service_file.exists():
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", "llamashift"],
                    capture_output=True, text=True
                )
                active = result.stdout.strip() == "active"
                services.append({
                    "type": "systemd",
                    "name": "llamashift",
                    "active": active,
                    "file": str(service_file),
                })
            except Exception:
                services.append({
                    "type": "systemd",
                    "name": "llamashift",
                    "active": False,
                    "file": str(service_file),
                })

    elif env == "windows":
        # Check Windows Service (NSSM)
        try:
            result = subprocess.run(
                ["sc", "query", "LlamaShift"],
                capture_output=True, text=True, shell=True
            )
            if "STATE" in result.stdout:
                active = "RUNNING" in result.stdout
                services.append({
                    "type": "windows-service",
                    "name": "LlamaShift",
                    "active": active,
                })
        except Exception:
            pass

        # Check NSSM
        nssm_paths = [
            Path(__file__).parent / "nssm" / "nssm.exe",
            shutil.which("nssm"),
        ]
        nssm_path = None
        for p in nssm_paths:
            if p and Path(str(p) if p else "").exists():
                nssm_path = str(p)
                break
        if nssm_path:
            services.append({
                "type": "nssm",
                "path": nssm_path,
            })

        # Check Task Scheduler
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", "LlamaShift"],
                capture_output=True, text=True, shell=True
            )
            if result.returncode == 0:
                services.append({
                    "type": "schtasks",
                    "name": "LlamaShift",
                })
        except Exception:
            pass

    return services


# ─── Uninstall Functions ──────────────────────────────────────────────────
def uninstall_systemd(name, dry_run=False):
    """Stop and remove a systemd service."""
    if dry_run:
        print_info(f"Would stop and disable systemd service '{name}'")
        print_info(f"Would remove /etc/systemd/system/{name}.service")
        return True

    # Stop
    print_info(f"Stopping service '{name}'...")
    result = subprocess.run(["systemctl", "stop", name], capture_output=True, text=True)
    if result.returncode != 0:
        print_warn(f"Failed to stop: {result.stderr.strip()}")
    else:
        print_ok(f"Service '{name}' stopped")

    # Disable
    print_info(f"Disabling service '{name}'...")
    result = subprocess.run(["systemctl", "disable", name], capture_output=True, text=True)
    if result.returncode == 0:
        print_ok(f"Service '{name}' disabled")

    # Remove service file
    service_file = Path(f"/etc/systemd/system/{name}.service")
    if service_file.exists():
        print_info(f"Removing {service_file}...")
        service_file.unlink()
        print_ok(f"Removed {service_file}")

    # Reload daemon
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "reset-failed"], capture_output=True)

    print_ok(f"Systemd service '{name}' fully removed")
    return True


def uninstall_windows_service(name, dry_run=False):
    """Stop and remove a Windows service (NSSM or sc.exe)."""
    if dry_run:
        print_info(f"Would stop and delete Windows service '{name}'")
        return True

    # Check NSSM first
    script_dir = Path(__file__).parent
    nssm_paths = [
        script_dir / "nssm" / "nssm.exe",
        shutil.which("nssm"),
    ]
    nssm_path = None
    for p in nssm_paths:
        if p and Path(str(p) if p else "").exists():
            nssm_path = str(p)
            break

    stopped = False
    if nssm_path:
        print_info(f"Stopping service via NSSM...")
        subprocess.run([nssm_path, "stop", name], capture_output=True, shell=True)
        stopped = True

    if not stopped:
        print_info(f"Stopping service via sc.exe...")
        subprocess.run(["sc", "stop", name], capture_output=True, shell=True)
        stopped = True
        print_ok(f"Service '{name}' stopped")

    # Delete
    deleted = False
    if nssm_path:
        print_info(f"Deleting service via NSSM...")
        result = subprocess.run(
            [nssm_path, "remove", name, "confirm"],
            capture_output=True, shell=True
        )
        if result.returncode == 0:
            deleted = True

    if not deleted:
        print_info(f"Deleting service via sc.exe...")
        result = subprocess.run(
            ["sc", "delete", name],
            capture_output=True, text=True, shell=True
        )
        if "SUCCESS" in result.stdout or result.returncode == 0:
            deleted = True

    if deleted:
        print_ok(f"Windows service '{name}' removed")
    else:
        print_warn(f"Could not remove service '{name}'. Try manually: sc delete {name}")

    return deleted


def uninstall_schtasks(name, dry_run=False):
    """Remove a Task Scheduler task."""
    if dry_run:
        print_info(f"Would delete scheduled task '\\{name}'")
        return True

    print_info(f"Deleting scheduled task '\\{name}'...")
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", name, "/F"],
        capture_output=True, text=True, shell=True
    )
    if "SUCCESS" in result.stdout or result.returncode == 0:
        print_ok(f"Scheduled task '\\{name}' removed")
        return True
    else:
        print_warn(f"Failed to delete task: {result.stderr.strip()}")
        return False


def cleanup_files(app_dir, dry_run=False):
    """Remove generated files: config.json, venv, task XML."""
    items = []

    # Virtual environment
    venv_path = app_dir / ".venv"
    if venv_path.exists():
        items.append(("Virtual environment", venv_path))

    # Task XML
    task_xml = app_dir / "llamashift_task.xml"
    if task_xml.exists():
        items.append(("Task XML", task_xml))

    # Log directory
    logs_dir = app_dir / "logs"
    if logs_dir.exists():
        items.append(("Logs directory", logs_dir))

    if not items:
        print_info("No generated files to clean up")
        return True

    print()
    print_info("Files/directories that can be removed:")
    for name, path in items:
        status = "(directory)" if path.is_dir() else "(file)"
        print(f"    - {path} {status}")

    if dry_run:
        print_info("Would remove the above items")
        return True

    if not confirm("  Remove these files?"):
        print_info("Skipped file cleanup")
        return True

    for name, path in items:
        print_info(f"Removing {path}...")
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            print_ok(f"Removed {path}")
        except Exception as e:
            print_warn(f"Failed to remove {path}: {e}")

    return True


# ─── Main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Uninstall LlamaShift services")
    parser.add_argument(
        "--all", action="store_true",
        help="Remove service + generated files (config kept)"
    )
    parser.add_argument(
        "--remove-config", action="store_true",
        help="Also delete config.json (use with caution!)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview actions without making changes"
    )
    parser.add_argument(
        "--no-prompt", action="store_true",
        help="Skip confirmation prompts (use with --dry-run or for scripting)"
    )
    args = parser.parse_args()

    env = detect_environment()
    app_dir = Path(__file__).parent.resolve()

    print_header("LlamaShift Service Uninstaller")
    print_info(f"OS:       {platform.system()} {platform.release()}")
    print_info(f"Env:      {env}")
    print_info(f"App dir:  {app_dir}")
    print_info(f"Dry run:  {'yes' if args.dry_run else 'no'}")
    print()

    # Discover services
    services = find_services(env)

    if not services:
        print_warn("No LlamaShift services found on this system.")
        print_info("Nothing to uninstall.")
        return

    # Show discovered services
    print_info("Discovered services:")
    for svc in services:
        svc_type = svc["type"].upper()
        name = svc.get("name", svc.get("path", "unknown"))
        status = ""
        if "active" in svc:
            status = f" — {C('RUNNING', 'yellow') if svc['active'] else 'stopped'}"
        print(f"    [{svc_type}] {name}{status}")
    print()

    # Confirm
    if not args.dry_run and not args.no_prompt:
        if not confirm("Proceed with uninstallation?"):
            print_info("Aborted.")
            return

    # Uninstall each service
    print()
    for svc in services:
        svc_type = svc["type"]

        if svc_type == "systemd":
            uninstall_systemd(svc["name"], args.dry_run)

        elif svc_type == "windows-service":
            uninstall_windows_service(svc["name"], args.dry_run)

        elif svc_type == "schtasks":
            uninstall_schtasks(svc["name"], args.dry_run)

        # NSSM is a tool, not a service — handled during windows-service removal

    # Cleanup files
    if args.all:
        print()
        cleanup_files(app_dir, args.dry_run)

    # Remove config if requested
    if args.remove_config and not args.dry_run:
        config_path = app_dir / "config.json"
        if config_path.exists():
            if confirm("  Delete config.json?"):
                config_path.unlink()
                print_ok("Removed config.json")
        else:
            print_info("No config.json found")
    elif args.remove_config and args.dry_run:
        print_info("Would also remove config.json")

    # Summary
    print_header("Uninstallation Complete")
    print_ok("LlamaShift services have been removed.")
    print()

    if args.dry_run:
        print_info("This was a dry run. No changes were made.")
    else:
        print_info("To reinstall, run the installer again.")
        print_info("Your model files and config.json are preserved.")
        if args.remove_config:
            print_warn("config.json was deleted — you'll need to reconfigure.")

    print()
    print(C("  Goodbye! 🦙\n", "bold"))


if __name__ == "__main__":
    main()