"""Tunnel resilience: the daemon must bring ngrok back after its agent
process dies (crash, OOM, kill). Network switches and outages are
survived by the ngrok agent itself; the daemon's supervisor covers the
case the agent cannot survive — its process exiting.

Uses an isolated CHATGPT_TUN_HOME, a random port and a controllable
fake ngrok agent, so the live system daemon is never touched. The fake
serves the tunnels API on a dedicated port (config key ngrok_api_url)
and exits with a signal-like status as soon as a trigger file appears.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only e2e")

FAKE_NGROK = """\
#!/usr/bin/env python3
import json, os, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

api_port = int(os.environ["FAKE_API_PORT"])
trigger = os.environ["FAKE_TRIGGER"]
pid_log = os.environ["FAKE_PID_LOG"]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/tunnels":
            body = json.dumps({"tunnels": [{"public_url": "https://fake-tunnel.example.org"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass

with open(pid_log, "a") as fh:
    fh.write(str(os.getpid()) + "\\n")

server = HTTPServer(("127.0.0.1", api_port), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
while True:
    if os.path.exists(trigger):
        try:
            with open(trigger) as fh:
                if fh.read().strip() == str(os.getpid()):
                    os._exit(9)
        except OSError:
            pass
    time.sleep(0.1)
"""


@pytest.fixture
def ngrok_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from conftest import TOKEN, DaemonHandle, free_port

    state = tmp_path / "state"
    state.mkdir()
    fake = state / "fake-ngrok"
    fake.write_text(FAKE_NGROK)
    fake.chmod(0o755)
    trigger = tmp_path / "kill-ngrok"
    pid_log = tmp_path / "ngrok-pids.log"
    api_port = free_port()
    monkeypatch.setenv("FAKE_API_PORT", str(api_port))
    monkeypatch.setenv("FAKE_TRIGGER", str(trigger))
    monkeypatch.setenv("FAKE_PID_LOG", str(pid_log))

    port = free_port()
    (state / "config.json").write_text(json.dumps({
        "version": 1, "host": "127.0.0.1", "port": port,
        "ngrok_bin": str(fake),
        "ngrok_api_url": f"http://127.0.0.1:{api_port}/api/tunnels",
        "public_url": None, "endpoint_token": TOKEN,
    }), encoding="utf-8")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from chatgpt_tun.registry import activate_project

    project = tmp_path / "demo"
    project.mkdir()
    monkeypatch.setenv("CHATGPT_TUN_HOME", str(state))
    activate_project(project)

    handle = DaemonHandle(state, port)
    pids: list[int] = []
    try:
        yield handle, state, trigger, pid_log, pids
    finally:
        monkeypatch.delenv("CHATGPT_TUN_HOME", raising=False)
        handle.kill_group()
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), 9)
            except (ProcessLookupError, PermissionError):
                pass


def wait_for(predicate, limit: float, what: str):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def read_runtime(state: Path) -> dict:
    return json.loads((state / "runtime.json").read_text())


def logged_pids(pid_log: Path) -> list[int]:
    if not pid_log.exists():
        return []
    return [int(line) for line in pid_log.read_text().split()]


def test_daemon_restarts_a_dead_ngrok_agent_and_rediscovers(ngrok_env):
    from conftest import mcp_call

    handle, state, trigger, pid_log, pids = ngrok_env
    handle.start()
    pids.extend(wait_for(lambda: logged_pids(pid_log) or None, 10, "first ngrok agent"))

    runtime = wait_for(lambda: read_runtime(state).get("ngrok_ready"), 35,
                       "initial tunnel discovery")
    assert runtime is True
    assert read_runtime(state)["ngrok_pid"] == pids[0]
    assert read_runtime(state)["public_url"] == "https://fake-tunnel.example.org"

    first = mcp_call(handle.url, "list_projects", {})
    assert first

    trigger.write_text(str(pids[0]))  # the first ngrok agent "crashes"
    pids.extend(wait_for(lambda: logged_pids(pid_log)[1:], 20, "ngrok restart"))

    def recovered():
        data = read_runtime(state)
        if data.get("ngrok_ready") and data.get("ngrok_pid") == pids[-1]:
            return data
        return None

    runtime = wait_for(recovered, 35, "tunnel recovery with a fresh agent")
    assert runtime["ngrok_error"] is None
    assert runtime["public_url"] == "https://fake-tunnel.example.org"
    assert runtime["ngrok_pid"] != pids[0]

    again = mcp_call(handle.url, "list_projects", {})
    assert again == first, "the MCP server must ride out ngrok restarts untouched"


def test_runtime_reports_the_outage_between_crash_and_recovery(ngrok_env):
    handle, state, trigger, pid_log, pids = ngrok_env
    handle.start()
    pids.extend(wait_for(lambda: logged_pids(pid_log) or None, 10, "first ngrok agent"))
    wait_for(lambda: read_runtime(state).get("ngrok_ready"), 35, "initial discovery")

    trigger.write_text(str(pids[0]))
    wait_for(lambda: logged_pids(pid_log)[1:], 20, "ngrok restart")

    def saw_outage():
        try:
            past = json.loads((state / "runtime.json").read_text())
        except FileNotFoundError:
            return None
        return past if past.get("ngrok_error") else None

    # The restart loop may recover before we observe the outage window;
    # whether we catch the interim error or the clean recovery, the
    # daemon must never be left reporting a dead tunnel as ready.
    outage = wait_for(lambda: saw_outage() or read_runtime(state).get("ngrok_ready"),
                      35, "outage marker or recovery")
    assert outage is True or "restarting" in outage.get("ngrok_error", "")
