"""The long-wait contract must be visible to MCP clients.

Blocking run_command is cut at ~5 minutes by the stock MCP client read
timeout and ngrok's idle timeout (see tests/test_long_waits.py), so the
tool descriptions themselves have to steer long work to the jobs
pattern — they are what the LLM sees. These tests pin that guidance so
it cannot silently disappear from the schema.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from chatgpt_tun.mcp_server import build_server


def tool_descriptions() -> dict[str, str]:
    mcp = build_server()
    return {tool.name: tool.description or "" for tool in asyncio.run(mcp.list_tools())}


def test_run_command_warns_about_the_blocking_ceiling():
    description = tool_descriptions()["run_command"]
    assert "5 minutes" in description, "clients must know blocking calls get cut"
    assert "start_command" in description, "clients must be pointed at the escape hatch"


def test_start_command_teaches_the_polling_pattern():
    description = tool_descriptions()["start_command"]
    assert "job_id" in description
    assert "get_command_status" in description
    assert "read_command_output" in description
    assert "5-minute" in description, "why the jobs pattern is the reliable path"


def test_status_tool_lists_the_terminal_statuses():
    description = tool_descriptions()["get_command_status"]
    for status in ("starting", "running", "completed", "failed", "cancelled",
                   "timed_out", "interrupted"):
        assert status in description, f"status '{status}' missing from the contract"


def test_output_tool_documents_incremental_streaming():
    description = tool_descriptions()["read_command_output"]
    assert "next_offset" in description
    assert "while the job is still running" in description


def test_server_instructions_carry_the_rule():
    mcp = build_server()
    instructions = mcp.instructions or ""
    assert "~2 minutes" in instructions
    assert "start_command" in instructions
    assert "get_command_status" in instructions
