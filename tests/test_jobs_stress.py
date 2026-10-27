"""Stress tests for the async job manager: concurrency limits, churn,
log bounding and output pagination while a job is still writing."""
from __future__ import annotations

import sys
import threading
import time

import pytest

from chatgpt_tun import jobs as jobs_module
from chatgpt_tun.jobs import MAX_LOG_BYTES

from conftest import make_project, python_code_snippet, wait_finished, wait_slot_freed

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process-group semantics are POSIX")


@pytest.fixture
def churn_project(state_home):
    make_project(state_home, "churn")
    return "churn"


def test_burst_of_starts_respects_max_running(manager):
    running = []
    for _ in range(4):
        running.append(manager.start("project-a", python_code_snippet("import time; time.sleep(5)")))
    assert all(job["status"] == "running" for job in running)
    with pytest.raises(RuntimeError, match="Too many running commands"):
        manager.start("project-a", python_code_snippet("import time; time.sleep(5)"))

    manager.cancel("project-a", running[0]["id"])
    wait_slot_freed(manager, running[0]["id"])
    replacement = manager.start("project-a", python_code_snippet("import time; time.sleep(5)"))
    assert replacement["status"] == "running"
    for job in running:
        manager.cancel("project-a", job["id"])


def test_concurrent_start_threads_all_accounted(manager, churn_project, monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_RUNNING", 3)
    slow = python_code_snippet("import time; time.sleep(3)")

    results: list = []
    errors: list = []
    barrier = threading.Barrier(12)

    def starter():
        barrier.wait()
        try:
            results.append(manager.start("churn", slow))
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=starter) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 3, "exactly MAX_RUNNING jobs must acquire the slots"
    assert len(errors) == 9, "the rest must be rejected while slots are held"
    assert len({job["id"] for job in results}) == 3
    assert set(manager.processes) == {job["id"] for job in results}

    for job in results:
        manager.cancel("churn", job["id"])
    for job in results:
        wait_slot_freed(manager, job["id"])
    assert not manager.processes


def test_high_job_churn_prunes_state_and_files(state_home, churn_project, monkeypatch):
    from chatgpt_tun.jobs import JobManager

    monkeypatch.setattr(jobs_module, "MAX_JOBS", 10)
    job_manager = JobManager()
    try:
        for _ in range(15):
            job = job_manager.start("churn", [sys.executable, "-c", "pass"])
            assert wait_finished(job_manager, "churn", job["id"])["status"] == "completed"

        assert len(job_manager.jobs) == 10
        ids_on_disk = {path.stem for path in job_manager.root.glob("*.json")}
        assert ids_on_disk == set(job_manager.jobs)

        logs = {path.stem for path in job_manager.root.glob("*.stdout")}
        logs |= {path.stem for path in job_manager.root.glob("*.stderr")}
        assert logs <= ids_on_disk, "orphaned log files after pruning"

        for job in job_manager.jobs.values():
            if job.get("request_id"):
                assert job_manager.requests[(job["project"], job["request_id"])] == job["id"]

        replacement = job_manager.start("churn", python_code_snippet("print('still works')"))
        assert wait_finished(job_manager, "churn", replacement["id"])["status"] == "completed"
    finally:
        job_manager.shutdown()


def test_output_pagination_is_consistent_while_job_writes(manager):
    expected = "".join(f"line-{i:05d}-" + "x" * 60 + "\n" for i in range(4000))
    writer = python_code_snippet(
        "import sys, time\n"
        "for i in range(4000):\n"
        "    sys.stdout.write(f'line-{i:05d}-' + 'x' * 60 + '\\n')\n"
        "    if i % 500 == 0:\n"
        "        sys.stdout.flush(); time.sleep(0.05)\n"
        "sys.stdout.flush()\n"
    )
    job = manager.start("project-a", writer)
    assert job["status"] == "running"

    collected = bytearray()
    offset = 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        page = manager.output("project-a", job["id"], offset=offset, max_bytes=7000)
        assert page["next_offset"] >= offset
        collected.extend(page["text"].encode("ascii", errors="strict"))
        offset = page["next_offset"]
        if page["status"] not in ("starting", "running", "cancelling"):
            break
        time.sleep(0.01)

    final = wait_finished(manager, "project-a", job["id"])
    assert final["status"] == "completed"
    assert final["stdout_truncated"] is False
    while True:
        page = manager.output("project-a", job["id"], offset=offset, max_bytes=7000)
        collected.extend(page["text"].encode("ascii", errors="strict"))
        offset = page["next_offset"]
        if offset >= page["size"]:
            break
    assert collected.decode("ascii") == expected
    log_size = (manager.root / (job["id"] + ".stdout")).stat().st_size
    assert log_size == len(expected)


def test_flood_output_is_bounded_and_writer_not_blocked(manager):
    flood = python_code_snippet(
        "import sys\nsys.stdout.write('y' * (9 * 1024 * 1024))\n"
        "sys.stderr.write('z' * (9 * 1024 * 1024))\nprint('done')\n"
    )
    job = manager.start("project-a", flood)
    final = wait_finished(manager, "project-a", job["id"], limit=60)
    assert final["status"] == "completed", "writer must drain even past the log cap"
    assert final["stdout_truncated"] and final["stderr_truncated"]
    assert (manager.root / (job["id"] + ".stdout")).stat().st_size == MAX_LOG_BYTES
    assert (manager.root / (job["id"] + ".stderr")).stat().st_size == MAX_LOG_BYTES
    capped = (manager.root / (job["id"] + ".stdout")).read_bytes()
    assert capped == b"y" * MAX_LOG_BYTES, "log must hold exactly the first capped bytes"
    assert b"done" not in capped, "post-cap output must be discarded, not crash the writer"


def test_interleaved_streams_captured_independently(manager):
    script = python_code_snippet(
        "import sys\n"
        "for i in range(2000):\n"
        "    sys.stdout.write(f'o{i}\\n'); sys.stderr.write(f'e{i}\\n')\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
    )
    job = manager.start("project-a", script)
    final = wait_finished(manager, "project-a", job["id"])
    assert final["status"] == "completed"
    out = manager.output("project-a", job["id"], stream="stdout")
    err = manager.output("project-a", job["id"], stream="stderr")
    assert [line for line in out["text"].splitlines()] == [f"o{i}" for i in range(2000)]
    assert [line for line in err["text"].splitlines()] == [f"e{i}" for i in range(2000)]


def test_many_small_jobs_sustain_throughput(manager):
    started = time.monotonic()
    for _ in range(10):
        job = manager.start("project-a", python_code_snippet("print('tick')"))
        assert wait_finished(manager, "project-a", job["id"])["status"] == "completed"
    elapsed = time.monotonic() - started
    assert elapsed < 60, "sustained sequential jobs stalled"
    assert not manager.processes
    assert all(job["status"] not in ("starting", "running") for job in manager.jobs.values())
