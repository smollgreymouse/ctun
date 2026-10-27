"""systemd-aware daemon management.

When the daemon runs under the ctun.service user unit, the CLI must
route shutdown/restart/start through systemctl instead of fighting
Restart=always with raw signals. Ownership is detected from the live
process cgroup, so these tests fake the cgroup reader and the systemctl
runner — nothing here touches the real unit or the live daemon.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from chatgpt_tun import process
from chatgpt_tun.state import write_runtime

UNIT_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ctun.service\n"
PLAIN_CGROUP = "0::/user.slice/session-2.scope\n"


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_TUN_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    yield tmp_path / "state"


def test_detection_by_cgroup(monkeypatch):
    monkeypatch.setattr(process, "pid_alive", lambda pid: True)
    monkeypatch.setattr(process, "_cgroup_of", lambda pid: UNIT_CGROUP)
    assert process.daemon_service_unit(4242) == "ctun.service"

    monkeypatch.setattr(process, "_cgroup_of", lambda pid: PLAIN_CGROUP)
    assert process.daemon_service_unit(4242) is None

    monkeypatch.setattr(process, "pid_alive", lambda pid: False)
    monkeypatch.setattr(process, "_cgroup_of", lambda pid: UNIT_CGROUP)
    assert process.daemon_service_unit(4242) is None, "dead processes are not managed"


def test_real_non_systemd_process_is_not_detected(isolated_state):
    proc = subprocess.Popen(["sleep", "5"])
    try:
        assert process.daemon_service_unit(proc.pid) is None
    finally:
        proc.terminate()
        proc.wait()
    assert process.daemon_service_unit(None) is None


def test_stop_daemon_routes_through_systemd(isolated_state, monkeypatch):
    write_runtime({"daemon_pid": 4711})
    calls = []
    stopped = []

    def fake_systemctl(*args, **kwargs):
        calls.append(args)
        stopped.append(True)
        return True

    monkeypatch.setattr(process, "daemon_service_unit", lambda pid: "ctun.service")
    monkeypatch.setattr(process, "systemctl_user", fake_systemctl)
    monkeypatch.setattr(process, "pid_alive", lambda pid: not stopped)

    assert process.stop_daemon() is True
    assert calls == [("stop", "ctun.service")]
    assert not (isolated_state / "runtime.json").exists()


def test_stop_daemon_falls_back_when_systemctl_fails(isolated_state, monkeypatch):
    write_runtime({"daemon_pid": 4711})
    killed = []
    monkeypatch.setattr(process, "daemon_service_unit", lambda pid: "ctun.service")
    monkeypatch.setattr(process, "systemctl_user", lambda *args, **kwargs: False)
    monkeypatch.setattr(process, "pid_alive", lambda pid: not killed)
    monkeypatch.setattr(process, "stop_pid", lambda pid, force=False: killed.append(pid))

    assert process.stop_daemon() is True
    assert killed == [4711], "fallback must signal the process directly"


def test_start_goes_through_systemd_when_unit_installed(isolated_state, monkeypatch):
    runtime_box = {"daemon_pid": None}
    calls = []

    def fake_systemctl(*args, **kwargs):
        calls.append(args)
        runtime_box["daemon_pid"] = os.getpid()  # the unit started the daemon
        return True

    monkeypatch.setattr(process, "systemd_unit_installed", lambda: True)
    monkeypatch.setattr(process, "load_config",
                        lambda: {"host": "127.0.0.1", "port": 59999})
    monkeypatch.setattr(process, "systemctl_user", fake_systemctl)
    monkeypatch.setattr(process, "load_runtime", lambda: dict(runtime_box))
    monkeypatch.setattr(process, "_port_open", lambda host, port: True)

    runtime = process.start_daemon(wait_seconds=5)
    assert calls == [("start", "ctun.service")]
    assert runtime["daemon_pid"] == os.getpid()


def test_start_fails_loudly_when_systemctl_cannot_start(isolated_state, monkeypatch):
    monkeypatch.setattr(process, "systemd_unit_installed", lambda: True)
    monkeypatch.setattr(process, "systemctl_user", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="systemd"):
        process.start_daemon(wait_seconds=1)


def test_start_ignores_systemd_when_daemon_already_running(isolated_state, monkeypatch):
    write_runtime({"daemon_pid": os.getpid()})
    calls = []
    monkeypatch.setattr(process, "systemd_unit_installed",
                        lambda: calls.append("checked") or True)
    monkeypatch.setattr(process, "pid_alive", lambda pid: True)

    runtime = process.start_daemon(wait_seconds=1)
    assert runtime["daemon_pid"] == os.getpid()
    assert not calls, "running daemon must short-circuit before any systemd call"
