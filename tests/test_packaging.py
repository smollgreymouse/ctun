"""Packaging: the wheel must build, carry the right metadata and entry
point, and the installed CLI must work in a fresh isolated venv."""
from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def project_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("dist")
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--disable-pip-version-check", "-w", str(out_dir), str(REPO)],
        capture_output=True, text=True, timeout=600, cwd=str(REPO),
    )
    if completed.returncode != 0:
        pytest.fail(f"pip wheel failed:\n{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}")
    wheels = list(out_dir.glob("ctun-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def read_wheel_member(wheel: Path, name: str) -> str:
    with zipfile.ZipFile(wheel) as zf:
        return zf.read(name).decode("utf-8")


def test_wheel_contains_package_and_metadata(wheel: Path):
    version = project_version()
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    for required in (
        "chatgpt_tun/__init__.py",
        "chatgpt_tun/cli.py",
        "chatgpt_tun/daemon.py",
        "chatgpt_tun/jobs.py",
        "chatgpt_tun/mcp_server.py",
        "chatgpt_tun/platforms/__init__.py",
        "chatgpt_tun/platforms/posix.py",
        f"ctun-{version}.dist-info/METADATA",
        f"ctun-{version}.dist-info/entry_points.txt",
    ):
        assert required in names, f"wheel is missing {required}"

    metadata = read_wheel_member(wheel, f"ctun-{version}.dist-info/METADATA")
    assert "Name: ctun" in metadata
    assert f"Version: {version}" in metadata
    mcp_req = [line for line in metadata.splitlines()
               if line.startswith("Requires-Dist: mcp")]
    assert len(mcp_req) == 1 and ">=2.1" in mcp_req[0] and "<3" in mcp_req[0], mcp_req

    entry_points = read_wheel_member(wheel, f"ctun-{version}.dist-info/entry_points.txt")
    assert "ctun = chatgpt_tun.cli:main" in entry_points


@pytest.fixture(scope="module")
def installed_venv(tmp_path_factory: pytest.TempPathFactory, wheel: Path) -> Path:
    venv_dir = tmp_path_factory.mktemp("venv")
    completed = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
    completed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps",
         "--disable-pip-version-check", str(wheel)],
        capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return bin_dir


def test_installed_entrypoint_reports_version(installed_venv: Path):
    ctun = installed_venv / ("ctun.exe" if sys.platform == "win32" else "ctun")
    assert ctun.exists(), "console script was not installed"
    completed = subprocess.run([str(ctun), "--version"],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"ctun {project_version()}"


def test_jobs_subsystem_imports_without_optional_deps(installed_venv: Path):
    """The wheel is installed --no-deps, so this proves the async jobs
    subsystem and CLI do not import anything beyond the stdlib."""
    python = installed_venv / ("python.exe" if sys.platform == "win32" else "python")
    completed = subprocess.run(
        [str(python), "-c",
         "from chatgpt_tun.jobs import JobManager, MAX_RUNNING;"
         "from chatgpt_tun import cli;"
         "from chatgpt_tun.state import state_dir;"
         "print('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "ok" in completed.stdout


def test_cli_help_lists_job_tools(installed_venv: Path):
    ctun = installed_venv / ("ctun.exe" if sys.platform == "win32" else "ctun")
    completed = subprocess.run([str(ctun), "--help"],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    for command in ("up", "down", "status", "restart"):
        assert command in completed.stdout, f"missing '{command}' in CLI help"
