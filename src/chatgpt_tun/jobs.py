"""Durable, bounded asynchronous commands, independent from MCP HTTP request lifetime."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .platforms import stop_pid
from .state import state_dir
from .workspace import safe_path

MAX_RUNNING = 4
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_JOBS = 100
CHUNK = 65536


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self) -> None:
        self.root = state_dir() / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.requests: dict[tuple[str, str], str] = {}
        for path in self.root.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["status"] in ("running", "starting"):
                    job["status"] = "interrupted"
                    job["finished_at"] = _now()
                    job["error"] = "Gateway restarted; process exit status is unknown"
                    self._save(job)
                self.jobs[job["id"]] = job
                if job.get("request_id"):
                    self.requests[(job["project"], job["request_id"])] = job["id"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self._prune()

    def _save(self, job: dict[str, Any]) -> None:
        path = self.root / (job["id"] + ".json")
        tmp = self.root / (job["id"] + ".tmp")
        tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _prune(self) -> None:
        finished = sorted((j for j in self.jobs.values() if j["status"] not in ("starting", "running")),
                          key=lambda j: j["created_at"])
        while len(self.jobs) > MAX_JOBS and finished:
            job = finished.pop(0)
            self.jobs.pop(job["id"], None)
            if job.get("request_id"):
                self.requests.pop((job["project"], job["request_id"]), None)
            for suffix in (".json", ".stdout", ".stderr"):
                (self.root / (job["id"] + suffix)).unlink(missing_ok=True)

    def _lookup(self, project: str, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job or job["project"] != project:
            raise KeyError("Unknown job for this project")
        # Disabled projects must not expose their past command output.
        from .registry import get_active_project
        get_active_project(project)
        return job

    def start(self, project: str, argv: list[str], cwd: str = ".",
              timeout: int = 3600, request_id: str | None = None) -> dict[str, Any]:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        if not 1 <= timeout <= 86400:
            raise ValueError("timeout must be between 1 and 86400 seconds")
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ValueError("request_id must contain 1-128 characters")
        workdir = safe_path(project, cwd)
        if not workdir.is_dir():
            raise ValueError("cwd is not a directory")
        with self.lock:
            key = (project, request_id) if request_id is not None else None
            if key and key in self.requests:
                old = self.jobs[self.requests[key]]
                if old["argv"] != argv or old["cwd"] != cwd or old["timeout"] != timeout:
                    raise ValueError("request_id already used with different command arguments")
                return self._public(old)
            if len(self.processes) >= MAX_RUNNING:
                raise RuntimeError("Too many running commands")
            ident = uuid.uuid4().hex
            job = {"id": ident, "project": project, "argv": argv, "cwd": cwd,
                   "timeout": timeout, "request_id": request_id, "status": "starting",
                   "created_at": _now(), "started_at": None, "finished_at": None,
                   "returncode": None, "error": None, "pid": None,
                   "stdout_truncated": False, "stderr_truncated": False}
            self.jobs[ident] = job
            if key:
                self.requests[key] = ident
            self._save(job)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=os.environ.copy(), start_new_session=os.name != "nt",
                                        creationflags=flags)
            except Exception as exc:
                job.update(status="failed", error=str(exc), finished_at=_now())
                self._save(job)
                return self._public(job)
            self.processes[ident] = proc
            job.update(status="running", started_at=_now(), pid=proc.pid)
            self._save(job)
            threading.Thread(target=self._supervise, args=(ident, proc), daemon=True).start()
            self._prune()
            return self._public(job)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return dict(job)

    def status(self, project: str, job_id: str) -> dict[str, Any]:
        with self.lock:
            return self._public(self._lookup(project, job_id))

    def output(self, project: str, job_id: str, stream: str = "stdout",
               offset: int = 0, max_bytes: int = 65536) -> dict[str, Any]:
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream must be stdout or stderr")
        if offset < 0 or not 1 <= max_bytes <= 262144:
            raise ValueError("invalid offset or max_bytes")
        with self.lock:
            job = self._public(self._lookup(project, job_id))
        path = self.root / (job_id + "." + stream)
        if path.exists():
            with path.open("rb") as fh:
                fh.seek(offset)
                data = fh.read(max_bytes)
                next_offset = fh.tell()
                size = path.stat().st_size
        else:
            data, next_offset, size = b"", offset, 0
        return {"job_id": job_id, "stream": stream, "offset": offset,
                "next_offset": next_offset, "size": size,
                "text": data.decode("utf-8", errors="replace"),
                "truncated": job[stream + "_truncated"],
                "status": job["status"]}

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            stop_pid(proc.pid)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                stop_pid(proc.pid, force=True)
            except ProcessLookupError:
                pass

    def cancel(self, project: str, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self._lookup(project, job_id)
            proc = self.processes.get(job_id)
            if proc is not None and job["status"] == "running":
                job["status"] = "cancelling"
                self._save(job)
        if proc is not None:
            self._terminate(proc)
        return self.status(project, job_id)

    def _pump(self, ident: str, stream: str, pipe: Any) -> None:
        written = 0
        with (self.root / (ident + "." + stream)).open("wb") as fh:
            try:
                while True:
                    # read1 returns as soon as any bytes are available; read(n)
                    # would block until the full chunk or EOF, so a long-running
                    # command's progress would only appear in 64 KiB bursts.
                    data = pipe.read1(CHUNK)
                    if not data:
                        break
                    allowed = max(0, MAX_LOG_BYTES - written)
                    if allowed:
                        fh.write(data[:allowed])
                        fh.flush()
                        written += min(len(data), allowed)
                    if len(data) > allowed:
                        with self.lock:
                            self.jobs[ident][stream + "_truncated"] = True
            finally:
                pipe.close()

    def _supervise(self, ident: str, proc: subprocess.Popen[bytes]) -> None:
        pumps = [threading.Thread(target=self._pump, args=(ident, stream, pipe), daemon=True)
                 for stream, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for worker in pumps:
            worker.start()
        timed_out = False
        try:
            proc.wait(timeout=self.jobs[ident]["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(proc)
        for worker in pumps:
            worker.join()
        with self.lock:
            job = self.jobs[ident]
            job["returncode"] = proc.returncode
            if job["status"] == "cancelling":
                job["status"] = "cancelled"
            elif timed_out:
                job["status"] = "timed_out"
            else:
                job["status"] = "completed" if proc.returncode == 0 else "failed"
            job["finished_at"] = _now()
            self._save(job)
            self.processes.pop(ident, None)
            self._prune()

    def shutdown(self) -> None:
        with self.lock:
            ids = list(self.processes)
        for ident in ids:
            with self.lock:
                proc = self.processes.get(ident)
            if proc is not None:
                self._terminate(proc)


_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        return _manager
