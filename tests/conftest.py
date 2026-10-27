"""Shared test plumbing.

Every fixture here redirects CHATGPT_TUN_HOME into a per-test temporary
directory, so the suite never reads or writes the state of a ctun daemon
that is actually running on the machine.
"""
from __future__ import annotations

import os
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
