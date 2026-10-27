"""End-to-end daemon restart recovery.

Boots the real daemon (``python -m chatgpt_tun.daemon``) against an
isolated CHATGPT_TUN_HOME and a free localhost port, drives it over MCP
HTTP, kills it and checks what survives the restart. The system-wide
ctun daemon is never touched: everything lives in tmp_path.
"""
from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("mcp")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")

from conftest import (TOKEN, DaemonHandle, free_port, kill_job_group,
                      mcp_call, poll_status)


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
