"""Timeouts, cancellation and resource cleanup for background jobs."""
from __future__ import annotations

import sys
import time

import pytest

from chatgpt_tun.platforms import pid_alive

from conftest import (python_code_snippet, start_when_slot_free, wait_dead,
                      wait_finished, wait_pidfile, wait_slot_free)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process-group semantics are POSIX")

SPAWN_CHILD = (
    "import subprocess, sys, pathlib\n"
    "child = subprocess.Popen(['sleep', '60'])\n"
    "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
    "child.wait()\n"
)
IGNORE_SIGTERM = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('armed', flush=True)\n"
    "time.sleep(60)\n"
)


def test_timeout_kills_the_whole_process_group(manager, tmp_path):
    pidfile = tmp_path / "child-pid"
    argv = python_code_snippet(SPAWN_CHILD) + [str(pidfile)]
    job = manager.start("project-a", argv, timeout=4)
    child_pid = wait_pidfile(pidfile)
    final = wait_finished(manager, "project-a", job["id"], limit=15)
    assert final["status"] == "timed_out"
    assert wait_dead(child_pid), "grandchild survived the timeout"
    assert wait_dead(final["pid"])


def test_cancel_kills_the_whole_process_group(manager, tmp_path):
    pidfile = tmp_path / "child-pid"
    argv = python_code_snippet(SPAWN_CHILD) + [str(pidfile)]
    job = manager.start("project-a", argv)
    assert job["status"] == "running"
    child_pid = wait_pidfile(pidfile)
    cancelled = manager.cancel("project-a", job["id"])
    assert cancelled["status"] in ("cancelling", "cancelled")
    final = wait_finished(manager, "project-a", job["id"])
    assert final["status"] == "cancelled"
    assert wait_dead(child_pid), "grandchild survived cancellation"
    assert wait_dead(final["pid"])


def test_cancel_force_kills_a_sigterm_ignorer(manager):
    job = manager.start("project-a", python_code_snippet(IGNORE_SIGTERM))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "armed" in manager.output("project-a", job["id"])["text"]:
            break
        time.sleep(0.05)
    final = manager.cancel("project-a", job["id"])
    final = wait_finished(manager, "project-a", job["id"], limit=15)
    assert final["status"] == "cancelled"
    assert final["returncode"] == -9, "SIGTERM-ignorer must be finished off with SIGKILL"
    assert wait_dead(final["pid"])


def test_cancel_after_completion_is_a_safe_noop(manager):
    job = manager.start("project-a", python_code_snippet("print('fast')"))
    assert wait_finished(manager, "project-a", job["id"])["status"] == "completed"

    result = manager.cancel("project-a", job["id"])
    assert result["status"] == "completed"
    again = manager.cancel("project-a", job["id"])
    assert again["status"] == "completed"
    assert wait_dead(job["pid"], limit=0.2)


def test_double_cancel_and_unknown_job_cancel(manager):
    job = manager.start("project-a", python_code_snippet("import time; time.sleep(30)"))
    first = manager.cancel("project-a", job["id"])
    second = manager.cancel("project-a", job["id"])
    final = wait_finished(manager, "project-a", job["id"])
    assert final["status"] == "cancelled"
    assert first["status"] in ("cancelling", "cancelled")
    assert second["status"] in ("cancelling", "cancelled")

    with pytest.raises(KeyError):
        manager.cancel("project-a", "no-such-job")


def test_partial_output_survives_cancellation(manager):
    job = manager.start("project-a", python_code_snippet(
        "import time\n"
        "for i in range(300):\n"
        "    print(f'chunk-{i}', flush=True)\n"
        "    time.sleep(0.1)\n"))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if manager.output("project-a", job["id"])["text"]:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("job produced no output")
    manager.cancel("project-a", job["id"])
    final = wait_finished(manager, "project-a", job["id"])
    assert final["status"] == "cancelled"
    text = manager.output("project-a", job["id"])["text"]
    assert text.startswith("chunk-0"), "emitted output must survive cancellation"


def test_shutdown_terminates_every_running_job(manager):
    jobs = [manager.start("project-a", python_code_snippet("import time; time.sleep(60)"))
            for _ in range(3)]
    assert len(manager.processes) == 3

    manager.shutdown()
    for job in jobs:
        assert wait_dead(job["pid"]), f"job {job['id']} survived shutdown"
    wait_slot_free(manager)
    assert not manager.processes
    # Supervisors finalize asynchronously; the killed jobs must not stay
    # "running" forever in memory (a hard crash instead leaves the durable
    # record as-is and the next manager marks it interrupted).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        statuses = [manager.status("project-a", job["id"])["status"] for job in jobs]
        if all(status != "running" for status in statuses):
            break
        time.sleep(0.05)
    assert all(status != "running" for status in statuses), statuses


def test_start_arguments_are_validated(manager):
    with pytest.raises(ValueError):
        manager.start("project-a", [])
    with pytest.raises(ValueError):
        manager.start("project-a", ["echo", 42])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        manager.start("project-a", python_code_snippet("pass"), timeout=0)
    with pytest.raises(ValueError):
        manager.start("project-a", python_code_snippet("pass"), timeout=86401)
    with pytest.raises(ValueError):
        manager.start("project-a", python_code_snippet("pass"), request_id="x" * 129)
    with pytest.raises(ValueError):
        manager.start("project-a", python_code_snippet("pass"), cwd="../escape")
    with pytest.raises(ValueError):
        manager.start("project-a", python_code_snippet("pass"), cwd="no/such/dir")
    with pytest.raises(KeyError):
        manager.start("no-such-project", python_code_snippet("pass"))


def test_failed_spawn_records_error_and_frees_slot(manager):
    job = manager.start("project-a", ["/no/such/binary/anywhere"])
    assert job["status"] == "failed"
    assert job["error"]
    assert job["finished_at"]

    status = manager.status("project-a", job["id"])
    assert status["status"] == "failed"
    # The failed spawn must not hold a running slot.
    for _ in range(4):
        filler = start_when_slot_free(manager, "project-a",
                                      python_code_snippet("import time; time.sleep(5)"))
        assert filler["status"] == "running"
        manager.cancel("project-a", filler["id"])
        wait_slot_free(manager)


def test_finished_jobs_release_slots_and_state_is_consistent(manager):
    for _ in range(6):
        job = manager.start("project-a", python_code_snippet("print('cycle')"))
        final = wait_finished(manager, "project-a", job["id"])
        assert final["status"] == "completed"
        assert job["id"] not in manager.processes
        assert pid_alive(final["pid"]) is False

    running = [j for j in manager.jobs.values() if j["status"] in ("starting", "running")]
    assert not running
    ids = {j["id"] for j in manager.jobs.values()}
    on_disk = {p.stem for p in manager.root.glob("*.json")}
    assert ids == on_disk, "job state and durable files diverged"
