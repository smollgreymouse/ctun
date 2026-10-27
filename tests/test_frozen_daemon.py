import sys
from chatgpt_tun import cli, process

def test_internal_daemon_entrypoint(monkeypatch):
    from chatgpt_tun import daemon
    monkeypatch.setattr(daemon, "main", lambda: 47)
    assert cli.main(["--internal-daemon"]) == 47

def test_frozen_start_command_uses_entrypoint(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(process, "load_runtime", lambda: {})
    monkeypatch.setattr(process, "clear_runtime", lambda: None)
    # Machines with the ctun systemd unit installed must not route this
    # test's start through a real `systemctl --user start ctun`.
    monkeypatch.setattr(process, "systemd_unit_installed", lambda: False)
    monkeypatch.setattr(process, "log_path", lambda: tmp_path / "daemon.log")
    monkeypatch.setattr(process, "load_config", lambda: {"host": "127.0.0.1", "port": 8765})
    monkeypatch.setattr(process, "pid_alive", lambda _: False)
    commands = []
    class Proc:
        pid = 999
    def popen(args, **kwargs):
        commands.append(args)
        return Proc()
    monkeypatch.setattr(process.subprocess, "Popen", popen)
    monkeypatch.setattr(process.time, "monotonic", iter([0.0, 2.0]).__next__)
    try:
        process._start_daemon_unlocked(wait_seconds=0.5)
    except RuntimeError:
        pass
    assert commands == [[sys.executable, "--internal-daemon"]]
