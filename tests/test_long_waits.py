"""Long synchronous waits over MCP HTTP: where do connections actually break?

The stock MCP HTTP client (``create_mcp_http_client``) applies
``httpx.Timeout(30, read=300)``: any single tool call that blocks for
more than five minutes is cut by the *client* regardless of the server,
which is exactly why long work must go through async jobs instead of a
blocking run_command. These tests pin the observed ceilings on a real
daemon: uvicorn itself imposes no response timeout, keep-alive idle
gaps reconnect transparently, and the start/poll/cancel jobs pattern
stays stable over HTTP for as long as needed.
"""
from __future__ import annotations

import sys
import time

import pytest

from conftest import TOKEN, DaemonHandle, free_port, mcp_call, poll_status

pytest.importorskip("mcp")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only e2e")


@pytest.fixture
def daemon_env(tmp_path):
    """Reuse the shared isolated-daemon fixture machinery."""
    import json
    import os
    from pathlib import Path

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

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from chatgpt_tun.registry import activate_project

    project = tmp_path / "demo"
    project.mkdir()
    os.environ["CHATGPT_TUN_HOME"] = str(state)
    activate_project(project)

    handle = DaemonHandle(state, port)
    try:
        yield handle
    finally:
        os.environ.pop("CHATGPT_TUN_HOME", None)
        handle.kill_group()


def test_default_mcp_client_cuts_calls_after_five_minutes():
    """Documents the ceiling that makes blocking calls unreliable: the
    stock client's read timeout is 300 s (with 30 s connect/write/pool).
    Anything longer is dropped client-side before the server even
    finishes."""
    from mcp.shared._httpx_utils import create_mcp_http_client

    client = create_mcp_http_client()
    timeout = client.timeout
    assert timeout.read == 300.0, "stock client must expose the 5-minute read ceiling"
    assert timeout.connect == 30.0
    assert timeout.write == 30.0


def test_sixty_second_synchronous_call_completes_locally(daemon_env):
    """uvicorn/mcp-server impose no response timeout of their own: a
    blocking call crossing the classic one-minute proxy boundary is fine
    on the direct path."""
    daemon_env.start()
    started = time.monotonic()
    result = mcp_call(daemon_env.url, "run_command", {
        "project": "demo", "argv": ["/bin/sleep", "65"], "timeout": 120,
    }, timeout=180)
    elapsed = time.monotonic() - started
    assert result["returncode"] == 0
    assert 63 <= elapsed < 100, elapsed


def test_default_client_tolerates_calls_over_thirty_seconds(daemon_env):
    """The 30 s value in the stock timeout is connect/write/pool, not the
    operative cap for a blocking response — the read ceiling (300 s) is
    what governs. A 35 s call must succeed with fully default settings,
    like ChatGPT's own connector would send them."""
    daemon_env.start()
    result = mcp_call(daemon_env.url, "run_command", {
        "project": "demo", "argv": ["/bin/sleep", "35"], "timeout": 90,
    }, timeout=120)
    assert result["returncode"] == 0


def test_next_call_survives_idle_keepalive_gap(daemon_env):
    """uvicorn closes idle keep-alive connections after ~5 s; the next
    call after a long thinking pause must reconnect transparently
    instead of erroring on a stale socket."""
    daemon_env.start()
    first = mcp_call(daemon_env.url, "list_projects", {})
    assert first
    time.sleep(8)  # > uvicorn's default 5 s keep-alive timeout
    second = mcp_call(daemon_env.url, "list_projects", {})
    assert second == first


def test_async_job_pattern_stays_stable_over_http(daemon_env):
    """The recommended pattern for long work: start returns immediately,
    status/output polls keep flowing while the job runs, incremental
    output actually grows between polls (streaming), and cancellation
    lands cleanly without orphans."""
    daemon_env.start()
    job = mcp_call(daemon_env.url, "start_command", {
        "project": "demo",
        "argv": [sys.executable, "-u", "-c",
                 "import time\n"
                 "for i in range(120):\n"
                 "    print(f'tick-{i}', flush=True)\n"
                 "    time.sleep(0.5)\n"],
        "timeout": 600, "request_id": "long-wait-poll",
    })
    assert job["status"] == "running"

    seen_sizes = []
    for _ in range(5):
        time.sleep(2)
        status = mcp_call(daemon_env.url, "get_command_status",
                          {"project": "demo", "job_id": job["id"]})
        assert status["status"] == "running"
        page = mcp_call(daemon_env.url, "read_command_output",
                        {"project": "demo", "job_id": job["id"]})
        seen_sizes.append(page["size"])
    assert seen_sizes == sorted(seen_sizes) and len(set(seen_sizes)) > 1, \
        f"output must stream between polls, got {seen_sizes}"
    assert "tick-0" in mcp_call(daemon_env.url, "read_command_output",
                                {"project": "demo", "job_id": job["id"]})["text"]

    cancelled = mcp_call(daemon_env.url, "cancel_command",
                         {"project": "demo", "job_id": job["id"]})
    assert cancelled["status"] in ("cancelling", "cancelled")
    final = poll_status(daemon_env.url, "demo", job["id"])
    assert final["status"] == "cancelled"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            import os
            os.kill(final["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        import os
        os.killpg(os.getpgid(final["pid"]), 9)
        raise AssertionError("cancelled job process outlived the daemon cleanup")
