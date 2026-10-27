from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .platforms import pid_alive, spawn_flags, stop_pid
from .state import clear_runtime, daemon_lifecycle_lock, load_config, load_runtime, log_path

SYSTEMD_UNIT_NAME = "ctun.service"


def _cgroup_of(pid: int) -> str | None:
    """Linux cgroup membership string of a process, None when unavailable."""
    try:
        return Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def daemon_service_unit(pid: int | None = None) -> str | None:
    """Name of the systemd user unit owning the running daemon, if any.

    Reads the process cgroup, so this is True only when the live daemon
    was actually started by that unit — merely having the unit file
    installed does not count. POSIX/Linux only.
    """
    if os.name != "posix" or sys.platform == "darwin":
        return None
    if pid is None:
        pid = load_runtime().get("daemon_pid")
    if not pid or not pid_alive(pid):
        return None
    cgroup = _cgroup_of(int(pid))
    if cgroup and any(line.rstrip().endswith("/" + SYSTEMD_UNIT_NAME)
                      for line in cgroup.splitlines()):
        return SYSTEMD_UNIT_NAME
    return None


def systemd_unit_installed(unit: str = SYSTEMD_UNIT_NAME) -> bool:
    """Whether the user-level systemd unit exists (Linux only)."""
    if shutil.which("systemctl") is None:
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "list-unit-files", unit, "--no-legend"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return unit in (completed.stdout or "")


def systemctl_user(*args: str, timeout: float = 60.0) -> bool:
    """Run a `systemctl --user` command; True when it exits 0."""
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def daemon_running() -> bool:
    runtime = load_runtime()
    return pid_alive(runtime.get("daemon_pid"))


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def start_daemon(wait_seconds: float = 12.0) -> dict[str, Any]:
    with daemon_lifecycle_lock():
        return _start_daemon_unlocked(wait_seconds)


def _start_daemon_unlocked(wait_seconds: float) -> dict[str, Any]:
    runtime = load_runtime()
    if pid_alive(runtime.get("daemon_pid")):
        return runtime
    clear_runtime()

    if systemd_unit_installed():
        return _start_daemon_via_systemd(wait_seconds)

    log_file = log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab", buffering=0) as log:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        kwargs.update(spawn_flags())
        process = subprocess.Popen(
            ([sys.executable, "--internal-daemon"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "chatgpt_tun.daemon"]),
            **kwargs,
        )

    config = load_config()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        runtime = load_runtime()
        if runtime.get("daemon_pid") == process.pid and _port_open(str(config["host"]), int(config["port"])):
            return runtime
        if not pid_alive(process.pid):
            break
        time.sleep(0.15)

    runtime = load_runtime()
    error = runtime.get("error")
    suffix = f": {error}" if error else ""
    raise RuntimeError(
        f"chatgpt-tun daemon failed to become ready{suffix}. "
        f"See {log_file}"
    )


def ensure_daemon() -> dict[str, Any]:
    if daemon_running():
        return load_runtime()
    return start_daemon()


def _start_daemon_via_systemd(wait_seconds: float) -> dict[str, Any]:
    """Start the daemon through its systemd user unit so the unit, not
    an orphaned child, owns the process (Restart=always keeps it up)."""
    if not systemctl_user("start", SYSTEMD_UNIT_NAME):
        raise RuntimeError(
            f"Failed to start {SYSTEMD_UNIT_NAME} via systemd. "
            f"Check `systemctl --user status {SYSTEMD_UNIT_NAME}` and {log_path()}"
        )
    config = load_config()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        runtime = load_runtime()
        pid = runtime.get("daemon_pid")
        if pid and pid_alive(pid) and _port_open(str(config["host"]), int(config["port"])):
            return runtime
        time.sleep(0.15)
    runtime = load_runtime()
    error = runtime.get("error")
    suffix = f": {error}" if error else ""
    raise RuntimeError(
        f"chatgpt-tun daemon ({SYSTEMD_UNIT_NAME}) failed to become ready{suffix}. "
        f"See {log_path()}"
    )


def stop_daemon(timeout: float = 5.0) -> bool:
    with daemon_lifecycle_lock():
        return _stop_daemon_unlocked(timeout)


def _stop_daemon_unlocked(timeout: float) -> bool:
    runtime = load_runtime()
    pid = runtime.get("daemon_pid")
    if not pid_alive(pid):
        clear_runtime()
        return False

    assert isinstance(pid, int)

    # A systemd-managed daemon must be stopped through its unit, or the
    # unit would immediately restart it (Restart=always).
    if daemon_service_unit(pid):
        if systemctl_user("stop", SYSTEMD_UNIT_NAME):
            deadline = time.monotonic() + max(timeout, 10.0)
            while time.monotonic() < deadline and pid_alive(pid):
                time.sleep(0.1)
            clear_runtime(expected_pid=pid)
            return True
        # systemctl is unavailable or broken; fall through and signal
        # directly — systemd treats a dead MainPID as a clean stop.

    try:
        stop_pid(pid)
    except ProcessLookupError:
        clear_runtime()
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            clear_runtime(expected_pid=pid)
            return True
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        stop_pid(pid, force=True)
    clear_runtime(expected_pid=pid)
    return True


def wait_for_connector_url(timeout: float = 20.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runtime = load_runtime()
        if runtime.get("ngrok_ready") and runtime.get("connector_url"):
            return str(runtime["connector_url"])
        if runtime.get("ngrok_error") or runtime.get("error"):
            return None
        time.sleep(0.25)
    return None


def read_log_tail(lines: int = 80) -> str:
    path = log_path()
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-max(1, lines) :])
