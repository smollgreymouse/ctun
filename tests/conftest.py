"""Shared test plumbing.

Every fixture here redirects CHATGPT_TUN_HOME into a per-test temporary
directory, so the suite never reads or writes the state of a ctun daemon
that is actually running on the machine.
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

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chatgpt_tun.platforms import pid_alive, stop_pid  # noqa: E402
from chatgpt_tun.registry import activate_project  # noqa: E402


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated state directory; the real user state is never touched."""
    root = tmp_path / "state"
    monkeypatch.setenv("CHATGPT_TUN_HOME", str(root))
    return root


@pytest.fixture
def manager(state_home, project):
    from chatgpt_tun.jobs import JobManager

    job_manager = JobManager()
    yield job_manager
    job_manager.shutdown()


@pytest.fixture
def project(state_home: Path) -> Path:
    root = state_home.parent / "project-a"
    root.mkdir(parents=True, exist_ok=True)
    activate_project(root)
    return root


def make_project(state_home: Path, name: str) -> Path:
    root = state_home.parent / name
    root.mkdir(parents=True, exist_ok=True)
    activate_project(root, name)
    return root


def wait_finished(manager, project: str, job_id: str, limit: float = 15.0) -> dict:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        item = manager.status(project, job_id)
        if item["status"] not in ("starting", "running", "cancelling"):
            return item
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {limit}s")


def wait_slot_free(manager, limit: float = 5.0) -> None:
    """All running slots are released asynchronously, when the supervisor
    threads finalize (e.g. after shutdown)."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not manager.processes:
            return
        time.sleep(0.02)
    raise AssertionError(f"{len(manager.processes)} slots still held after {limit}s")


def wait_slot_freed(manager, job_id: str, limit: float = 5.0) -> None:
    """One cancelled/finished job releases its slot asynchronously."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if job_id not in manager.processes:
            return
        time.sleep(0.02)
    raise AssertionError(f"slot for {job_id} still held after {limit}s")


def start_when_slot_free(manager, project: str, argv: list[str], **kwargs) -> dict:
    """Start a job, tolerating the async slot release after a cancel."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return manager.start(project, argv, **kwargs)
        except RuntimeError as exc:
            if "Too many running commands" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def wait_pidfile(path, limit: float = 10.0) -> int:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        time.sleep(0.02)
    raise AssertionError(f"helper process never wrote its pid to {path}")
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        item = manager.status(project, job_id)
        if item["status"] not in ("starting", "running", "cancelling"):
            return item
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {limit}s")


def wait_dead(pid: int | None, limit: float = 5.0) -> bool:
    """Wait until a process (and its group) is gone; zombies count as gone."""
    if not pid:
        return True
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        stop_pid(pid, force=True)
    except ProcessLookupError:
        return True
    return False


def python_code_snippet(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


# --- Shared end-to-end daemon helpers (used by test_daemon_e2e and long-wait tests) ---

REPO = Path(__file__).resolve().parents[1]
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


