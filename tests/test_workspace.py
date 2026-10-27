from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from chatgpt_tun.registry import activate_project, deactivate_project
from chatgpt_tun.workspace import (
    list_directory,
    read_text,
    run_command,
    safe_path,
    search_text,
    write_text,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATGPT_TUN_HOME", str(tmp_path / "state"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    activate_project(root)
    return root


def test_file_roundtrip(project: Path) -> None:
    write_text("demo", "src/a.txt", "alpha\nbeta\nalpha\n")
    assert read_text("demo", "src/a.txt", start_line=2, end_line=2)["text"] == "beta"

    entries = list_directory("demo", "src")
    assert entries[0]["name"] == "a.txt"

    matches = search_text("demo", "alpha")
    assert [match["line"] for match in matches] == [1, 3]


def test_paths_cannot_escape_root(project: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        safe_path("demo", "../outside.txt")

    if hasattr(os, "symlink"):
        outside = tmp_path / "outside"
        outside.mkdir()
        (project / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="escapes"):
            safe_path("demo", "escape/secret.txt")


def test_disabled_project_is_inaccessible(project: Path) -> None:
    deactivate_project("demo")
    with pytest.raises(PermissionError, match="disabled"):
        read_text("demo", "whatever.txt")


def test_run_command_uses_project_cwd(project: Path) -> None:
    result = run_command(
        "demo",
        [sys.executable, "-c", "import pathlib; print(pathlib.Path.cwd().name)"],
        timeout=10,
    )
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "demo"
