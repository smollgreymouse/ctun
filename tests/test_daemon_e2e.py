"""End-to-end daemon restart recovery.

Boots the real daemon (``python -m chatgpt_tun.daemon``) against an
isolated CHATGPT_TUN_HOME and a free localhost port, drives it over MCP
HTTP, kills it and checks what survives the restart. The system-wide
ctun daemon is never touched: everything lives in tmp_path.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TOKEN = "e2e-token"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DaemonHandle:
    def __init__(self, state: Path, port: int):
        self.state = state
        self.port = port
        self.url = f"http://127.0.0.1:{port}/{TOKEN}/mcp"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ,
                   CHATGPT_TUN_HOME=str(self.state),
                   PYTHONPATH=str(SRC))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "chatgpt_tun.daemon"],
            env=env, cwd=str(REPO),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"daemon exited early with {self.proc.returncode}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise AssertionError("daemon never opened its port")

    def kill_hard(self) -> None:
        assert self.proc
        os.killpg(os.getpgid(self.proc.pid), 9)
        self.proc.wait(timeout=10)

    def stop_graceful(self) -> int:
        assert self.proc
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.proc.pid), 9)
            self.proc.wait(timeout=10)
            raise AssertionError("daemon ignored SIGTERM")
        return self.proc.returncode

    def kill_group(self) -> None:
        """Last-resort cleanup for whatever is still alive."""
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 9)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=10)


def mcp_call(url: str, tool: str, arguments: dict, timeout: float = 30.0) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run() -> dict:
        async with streamable_http_client(url) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments, read_timeout_seconds=timeout)
                if result.is_error:
                    texts = [getattr(c, "text", str(c)) for c in result.content]
                    raise AssertionError(f"{tool} failed: {texts}")
                if result.structured_content is not None:
                    return result.structured_content
                return {"_content": [c.model_dump(mode="json") for c in result.content]}

    return asyncio.run(run())


def poll_status(url: str, project: str, job_id: str, limit: float = 20.0) -> dict:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        status = mcp_call(url, "get_command_status", {"project": project, "job_id": job_id})
        if status["status"] not in ("starting", "running", "cancelling"):
            return status
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not settle within {limit}s")


def kill_job_group(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except (ProcessLookupError, PermissionError):
        pass


@pytest.fixture
def daemon_env(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "fake-ngrok").write_text("#!/bin/sh\nexec sleep 300\n")
    (state / "fake-ngrok").chmod(0o755)
    port = free_port()
    (state / "config.json").write_text(json.dumps({
        "version": 1, "host": "127.0.0.1", "port": port,
        "ngrok_bin": str(state / "fake-ngrok"),
        "public_url": None, "endpoint_token": TOKEN,
    }), encoding="utf-8")

    sys.path.insert(0, str(SRC))
    from chatgpt_tun.registry import activate_project

    project = tmp_path / "demo"
    project.mkdir()
    os.environ["CHATGPT_TUN_HOME"] = str(state)
    activate_project(project)

    orphans: list[int] = []
    handle = DaemonHandle(state, port)
    try:
        yield handle, state, orphans
    finally:
        os.environ.pop("CHATGPT_TUN_HOME", None)
        handle.kill_group()
        for pid in orphans:
            kill_job_group(pid)


def test_hard_crash_then_restart_recovers_job_state(daemon_env):
    handle, state, orphans = daemon_env
    handle.start()

    done = mcp_call(handle.url, "start_command", {
        "project": "demo", "argv": [sys.executable, "-c", "print('before crash')"],
        "timeout": 60, "request_id": "e2e-done",
    })
    assert poll_status(handle.url, "demo", done["id"])["status"] == "completed"

    long_job = mcp_call(handle.url, "start_command", {
        "project": "demo",
        "argv": [sys.executable, "-u", "-c",
                 "print('from the first daemon', flush=True); import time; time.sleep(120)"],
        "timeout": 600, "request_id": "e2e-long",
    })
    assert long_job["status"] == "running"
    orphans.append(long_job["pid"])

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if mcp_call(handle.url, "read_command_output",
                    {"project": "demo", "job_id": long_job["id"]})["text"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("no output before the crash")

    handle.kill_hard()  # the orphaned job keeps running in its own session

    handle.start()  # same state dir and port, like a real daemon restart
    recovered = mcp_call(handle.url, "get_command_status",
                         {"project": "demo", "job_id": long_job["id"]})
    assert recovered["status"] == "interrupted"
    assert recovered["finished_at"]
    text = mcp_call(handle.url, "read_command_output",
                    {"project": "demo", "job_id": long_job["id"]})
    assert "from the first daemon" in text["text"]

    # Completed jobs survive the crash untouched.
    still_done = mcp_call(handle.url, "get_command_status",
                          {"project": "demo", "job_id": done["id"]})
    assert still_done["status"] == "completed"
    assert "before crash" in mcp_call(handle.url, "read_command_output",
                                      {"project": "demo", "job_id": done["id"]})["text"]

    # The durable request_id still deduplicates against the interrupted job.
    replay = mcp_call(handle.url, "start_command", {
        "project": "demo",
        "argv": [sys.executable, "-u", "-c",
                 "print('from the first daemon', flush=True); import time; time.sleep(120)"],
        "timeout": 600, "request_id": "e2e-long",
    })
    assert replay["id"] == long_job["id"]
    assert replay["status"] == "interrupted"

    # A fresh request runs normally on the restarted daemon.
    fresh = mcp_call(handle.url, "start_command", {
        "project": "demo", "argv": [sys.executable, "-c", "print('after restart')"],
        "timeout": 60, "request_id": "e2e-after",
    })
    assert poll_status(handle.url, "demo", fresh["id"])["status"] == "completed"


def test_graceful_restart_terminates_running_jobs(daemon_env):
    handle, state, orphans = daemon_env
    handle.start()

    job = mcp_call(handle.url, "start_command", {
        "project": "demo",
        "argv": [sys.executable, "-u", "-c",
                 "print('graceful', flush=True); import time; time.sleep(120)"],
        "timeout": 600, "request_id": "e2e-graceful",
    })
    assert job["status"] == "running"
    orphans.append(job["pid"])

    exit_code = handle.stop_graceful()
    assert exit_code == 0, "graceful SIGTERM must shut the daemon down cleanly"
    assert not (state / "runtime.json").exists(), "runtime state must be cleared on exit"
    kill_job_group(job["pid"])

    handle.start()
    after = mcp_call(handle.url, "get_command_status",
                     {"project": "demo", "job_id": job["id"]})
    # The old daemon SIGTERMed the job during cleanup; whether its
    # supervisor thread managed to record the kill before the process
    # exit is a race, but the durable record must never claim the job
    # is still running.
    assert after["status"] in ("failed", "interrupted")
    assert "graceful" in mcp_call(handle.url, "read_command_output",
                                  {"project": "demo", "job_id": job["id"]})["text"]

    follow_up = mcp_call(handle.url, "start_command", {
        "project": "demo", "argv": [sys.executable, "-c", "print('works again')"],
        "timeout": 60, "request_id": "e2e-next",
    })
    assert poll_status(handle.url, "demo", follow_up["id"])["status"] == "completed"
