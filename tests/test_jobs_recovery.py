"""Recovery semantics when the daemon (JobManager) restarts: durable
records, interrupted marking, corrupt-state tolerance, idempotency."""
from __future__ import annotations

import json
import sys
import time
import uuid

import pytest

from conftest import python_code_snippet, wait_finished

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process-group semantics are POSIX")


def test_restart_preserves_records_logs_and_truncation_flags(manager):
    job = manager.start("project-a", python_code_snippet(
        "print('persist'); import sys; sys.stderr.write('boom'); print('a' * 100)"))
    final = wait_finished(manager, "project-a", job["id"])
    assert final["status"] == "completed"

    recovered = type(manager)()
    try:
        status = recovered.status("project-a", job["id"])
        assert status["status"] == "completed"
        assert status["returncode"] == 0
        assert status["started_at"] == final["started_at"]
        assert status["finished_at"] == final["finished_at"]
        text = recovered.output("project-a", job["id"])
        assert text["text"].startswith("persist")
        assert text["size"] > 0
        err = recovered.output("project-a", job["id"], stream="stderr")
        assert "boom" in err["text"]
        page = recovered.output("project-a", job["id"], offset=4, max_bytes=8)
        assert page["next_offset"] == 12
    finally:
        recovered.shutdown()


def test_restart_marks_running_job_interrupted_and_keeps_logs(manager):
    job = manager.start("project-a", python_code_snippet("print('partial'); import time; time.sleep(30)"),
                        request_id="interrupt-replay")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "partial" in manager.output("project-a", job["id"])["text"]:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("job output never reached the log")

    # Simulate a crash: the daemon died without running shutdown().
    with manager.lock:
        manager.jobs[job["id"]]["status"] = "running"
        manager._save(manager.jobs[job["id"]])

    recovered = type(manager)()
    try:
        after = recovered.status("project-a", job["id"])
        assert after["status"] == "interrupted"
        assert after["finished_at"]
        assert "restarted" in after["error"].lower()
        text = recovered.output("project-a", job["id"])
        assert "partial" in text["text"]

        # Idempotent retry of the same request maps to the interrupted job.
        replay = recovered.start("project-a", job["argv"], job["cwd"], job["timeout"],
                                 request_id=job["request_id"])
        assert replay["id"] == job["id"]
        assert replay["status"] == "interrupted"
    finally:
        recovered.shutdown()


def test_restart_rerun_with_fresh_request_id_starts_new_job(manager):
    job = manager.start("project-a", python_code_snippet("import time; time.sleep(30)"),
                        request_id="stale-rid")
    with manager.lock:
        manager.jobs[job["id"]]["status"] = "running"
        manager._save(manager.jobs[job["id"]])
    manager.cancel("project-a", job["id"])
    wait_finished(manager, "project-a", job["id"])

    recovered = type(manager)()
    try:
        fresh = recovered.start("project-a", python_code_snippet("print('new run')"),
                                request_id="fresh-rid")
        assert fresh["id"] != job["id"]
        assert fresh["status"] == "running"
        final = wait_finished(recovered, "project-a", fresh["id"])
        assert final["status"] == "completed"
    finally:
        recovered.shutdown()


def test_restart_tolerates_corrupt_job_files(manager):
    good = manager.start("project-a", python_code_snippet("print('good')"))
    wait_finished(manager, "project-a", good["id"])

    corrupt = uuid.uuid4().hex
    (manager.root / (corrupt + ".json")).write_text("{not json", encoding="utf-8")
    truncated = uuid.uuid4().hex
    (manager.root / (truncated + ".json")).write_text('{"id": "x", "sta', encoding="utf-8")
    empty = uuid.uuid4().hex
    (manager.root / (empty + ".json")).write_text("", encoding="utf-8")
    missing_keys = uuid.uuid4().hex
    (manager.root / (missing_keys + ".json")).write_text('{"id": "y"}', encoding="utf-8")

    recovered = type(manager)()
    try:
        assert set(recovered.jobs) == {good["id"]}
        assert recovered.status("project-a", good["id"])["status"] == "completed"
    finally:
        recovered.shutdown()


def test_leftover_tmp_files_do_not_break_recovery(manager):
    good = manager.start("project-a", python_code_snippet("print('ok')"))
    wait_finished(manager, "project-a", good["id"])

    orphan = uuid.uuid4().hex
    (manager.root / (orphan + ".tmp")).write_text(json.dumps(
        {"id": orphan, "project": "project-a", "status": "running"}), encoding="utf-8")

    recovered = type(manager)()
    try:
        assert set(recovered.jobs) == {good["id"]}
        assert (manager.root / (orphan + ".tmp")).exists(), \
            "crashed-save leftovers are inert; they must not resurrect as jobs"
    finally:
        recovered.shutdown()


def test_request_id_dedup_survives_restart(manager):
    argv = python_code_snippet("print('once')")
    first = manager.start("project-a", argv, request_id="dedupe-me")
    wait_finished(manager, "project-a", first["id"])

    recovered = type(manager)()
    try:
        same = recovered.start("project-a", argv, request_id="dedupe-me")
        assert same["id"] == first["id"]
        with pytest.raises(ValueError, match="different"):
            recovered.start("project-a", python_code_snippet("print('other')"),
                            request_id="dedupe-me")
    finally:
        recovered.shutdown()


def test_restart_does_not_resurrect_process_slots(manager):
    for _ in range(3):
        job = manager.start("project-a", python_code_snippet("print('x')"))
        wait_finished(manager, "project-a", job["id"])

    recovered = type(manager)()
    try:
        assert not recovered.processes
        slots = []
        for _ in range(4):
            slots.append(recovered.start("project-a", python_code_snippet("import time; time.sleep(5)")))
        assert len(recovered.processes) == 4
        with pytest.raises(RuntimeError):
            recovered.start("project-a", python_code_snippet("import time; time.sleep(5)"))
        for job in slots:
            recovered.cancel("project-a", job["id"])
    finally:
        recovered.shutdown()


def test_prune_after_restart_keeps_limit(state_home, monkeypatch):
    from chatgpt_tun import jobs as jobs_module
    from chatgpt_tun.jobs import JobManager

    from conftest import make_project
    make_project(state_home, "prune")
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 8)
    first = JobManager()
    try:
        for _ in range(12):
            job = first.start("prune", [sys.executable, "-c", "pass"])
            wait_finished(first, "prune", job["id"])
    finally:
        first.shutdown()

    second = JobManager()
    try:
        assert len(second.jobs) == 8
        assert len(list(second.root.glob("*.json"))) == 8
        next_job = second.start("prune", python_code_snippet("print('again')"))
        assert wait_finished(second, "prune", next_job["id"])["status"] == "completed"
    finally:
        second.shutdown()


def test_interrupted_records_are_pruned_like_finished_ones(state_home, monkeypatch):
    from chatgpt_tun import jobs as jobs_module
    from chatgpt_tun.jobs import JobManager

    from conftest import make_project
    make_project(state_home, "prune2")
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 3)
    first = JobManager()
    try:
        for _ in range(5):
            job = first.start("prune2", [sys.executable, "-c", "pass"])
            wait_finished(first, "prune2", job["id"])
        # Simulate a crash that hit the two newest jobs mid-run.
        for job in sorted(first.jobs.values(), key=lambda j: j["created_at"])[-2:]:
            with first.lock:
                job["status"] = "running"
                first._save(job)
    finally:
        first.shutdown()

    recovered = JobManager()
    try:
        statuses = [j["status"] for j in recovered.jobs.values()]
        assert statuses.count("interrupted") == 2, "interrupted jobs survive as finished records"
        assert statuses.count("completed") == 1, "MAX_JOBS cap applied during recovery load"
    finally:
        recovered.shutdown()
