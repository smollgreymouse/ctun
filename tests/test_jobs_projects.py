"""Competing projects: isolation of job records, per-project idempotency
keys, shared running-slot pool and independent enable/disable."""
from __future__ import annotations

import sys
import threading

import pytest

from chatgpt_tun.platforms import stop_pid
from chatgpt_tun.registry import deactivate_project, remove_project

from conftest import make_project, python_code_snippet, wait_finished, wait_slot_freed

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process-group semantics are POSIX")


@pytest.fixture
def two_projects(state_home):
    make_project(state_home, "alpha")
    make_project(state_home, "beta")
    return "alpha", "beta"


def test_jobs_are_isolated_between_projects(manager, two_projects):
    alpha, beta = two_projects
    job = manager.start(alpha, python_code_snippet("print('alpha secret')"))
    wait_finished(manager, alpha, job["id"])

    with pytest.raises(KeyError):
        manager.status(beta, job["id"])
    with pytest.raises(KeyError):
        manager.output(beta, job["id"])
    with pytest.raises(KeyError):
        manager.cancel(beta, job["id"])

    # Unknown project names cannot probe job ids either.
    with pytest.raises((KeyError, PermissionError)):
        manager.status("ghost-project", job["id"])


def test_same_request_id_in_different_projects_is_independent(manager, two_projects):
    alpha, beta = two_projects
    argv = python_code_snippet("print('same argv, different projects')")
    in_alpha = manager.start(alpha, argv, request_id="shared-rid")
    in_beta = manager.start(beta, argv, request_id="shared-rid")
    assert in_alpha["id"] != in_beta["id"]
    assert in_alpha["project"] == alpha and in_beta["project"] == beta
    assert wait_finished(manager, alpha, in_alpha["id"])["status"] == "completed"
    assert wait_finished(manager, beta, in_beta["id"])["status"] == "completed"


def test_projects_share_the_global_running_slot_pool(manager, two_projects, monkeypatch):
    from chatgpt_tun import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_RUNNING", 4)
    alpha, beta = two_projects
    slow = python_code_snippet("import time; time.sleep(4)")

    started = []
    for project in (alpha, beta):
        for _ in range(3):
            try:
                started.append((project, manager.start(project, slow)["id"]))
            except RuntimeError:
                started.append((project, None))

    ok = [(p, j) for p, j in started if j is not None]
    rejected = [p for p, j in started if j is None]
    assert len(ok) == 4, "the pool is global, not per project"
    assert len(rejected) == 2
    assert len({p for p, _ in ok}) == 2, "both projects got slots"

    # Freeing one slot lets the next start through, for either project.
    first_alpha = [entry for entry in ok if entry[0] == alpha][0]
    manager.cancel(*first_alpha)
    wait_slot_freed(manager, first_alpha[1])
    extra = manager.start(beta, slow)
    assert extra["status"] == "running"

    for project, job_id in ok:
        manager.cancel(project, job_id)
    manager.cancel(beta, extra["id"])


def test_disabling_one_project_leaves_the_other_working(manager, two_projects):
    alpha, beta = two_projects
    in_alpha = manager.start(alpha, python_code_snippet("import time; time.sleep(30)"))
    in_beta = manager.start(beta, python_code_snippet("print('beta ok')"))
    assert wait_finished(manager, beta, in_beta["id"])["status"] == "completed"

    deactivate_project(alpha)
    with pytest.raises(PermissionError):
        manager.status(alpha, in_alpha["id"])
    with pytest.raises(PermissionError):
        manager.output(alpha, in_alpha["id"])
    with pytest.raises(PermissionError):
        manager.start(alpha, python_code_snippet("print('nope')"))

    assert manager.status(beta, in_beta["id"])["status"] == "completed"
    again = manager.start(beta, python_code_snippet("print('beta still ok')"))
    assert wait_finished(manager, beta, again["id"])["status"] == "completed"

    # Disabled projects must not expose jobs through the API; cleanup goes
    # straight to the process group so no job outlives the test.
    stop_pid(in_alpha["pid"], force=True)


def test_removed_project_cannot_reach_its_jobs(manager, two_projects):
    alpha, beta = two_projects
    job = manager.start(alpha, python_code_snippet("print('before removal')"))
    wait_finished(manager, alpha, job["id"])

    deactivate_project(alpha)
    remove_project(alpha, force=True)
    with pytest.raises(KeyError):
        manager.status(alpha, job["id"])
    with pytest.raises(KeyError):
        manager.start(alpha, python_code_snippet("print('nope')"))

    in_beta = manager.start(beta, python_code_snippet("print('beta fine')"))
    assert wait_finished(manager, beta, in_beta["id"])["status"] == "completed"


def test_cwd_cannot_escape_into_another_project(manager, two_projects):
    alpha, beta = two_projects
    with pytest.raises(ValueError):
        manager.start(alpha, python_code_snippet("print('escape')"), cwd=f"../{beta}")
    with pytest.raises(ValueError):
        manager.start(alpha, python_code_snippet("print('escape')"), cwd="/etc")


def test_many_projects_parallel_stress(manager, state_home, monkeypatch):
    from chatgpt_tun import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_RUNNING", 8)
    names = [f"para-{i}" for i in range(5)]
    for name in names:
        make_project(state_home, name)

    results: dict[str, list[str]] = {}
    errors: list = []
    barrier = threading.Barrier(len(names))

    # Each thread keeps at most one job running, so the shared pool is
    # never exhausted and any failure here is a real bug, not contention.
    def starter(name: str):
        barrier.wait()
        results[name] = []
        try:
            for _ in range(4):
                job = manager.start(name, python_code_snippet("print('parallel')"),
                                    request_id=f"{name}-rid-{_}")
                results[name].append(job["id"])
                wait_finished(manager, name, job["id"])
        except Exception as exc:  # pragma: no cover - collected below
            errors.append(exc)

    threads = [threading.Thread(target=starter, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    all_ids = []
    for name in names:
        assert len(results[name]) == 4
        for job_id in results[name]:
            status = manager.status(name, job_id)
            assert status["project"] == name
            assert status["status"] == "completed"
            text = manager.output(name, job_id)["text"]
            assert "parallel" in text
            all_ids.append(job_id)

    # No cross-project leakage: a job of one project is invisible to others.
    for name in names:
        for job_id in results[name]:
            for other in names:
                if other != name:
                    with pytest.raises(KeyError):
                        manager.status(other, job_id)
    assert len(set(all_ids)) == 20
