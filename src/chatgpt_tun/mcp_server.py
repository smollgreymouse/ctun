from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .jobs import get_manager
from .registry import list_projects as registry_list_projects
from .state import endpoint_path, load_config
from .workspace import (
    list_directory as workspace_list_directory,
    make_directory as workspace_make_directory,
    read_text as workspace_read_text,
    remove_path as workspace_remove_path,
    replace_text as workspace_replace_text,
    run_command as workspace_run_command,
    search_text as workspace_search_text,
    write_text as workspace_write_text,
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
WRITE_NON_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
COMMAND = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


def build_server() -> MCPServer:
    mcp = MCPServer(
        "chatgpt-tun",
        instructions=(
            "A local multi-project workspace gateway. Every file or command tool requires "
            "an active project name. Call list_projects first when the intended project is unclear. "
            "Blocking run_command is cut off at about 5 minutes by MCP-client and tunnel "
            "timeouts, so any command that may run longer than ~2 minutes MUST be started "
            "with start_command and then tracked with get_command_status and "
            "read_command_output instead."
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    def list_projects() -> list[dict[str, Any]]:
        """List projects that are currently enabled in the global chatgpt-tun registry."""
        return [
            {"name": record["name"], "path": record["path"], "active": True}
            for record in registry_list_projects(active_only=True)
        ]

    @mcp.tool(annotations=READ_ONLY)
    def list_directory(project: str, path: str = ".", include_hidden: bool = False) -> list[dict[str, Any]]:
        """List one directory inside an active project."""
        return workspace_list_directory(project, path, include_hidden)

    @mcp.tool(annotations=READ_ONLY)
    def read_text(
        project: str,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 200_000,
    ) -> dict[str, Any]:
        """Read a UTF-8 text file from an active project, optionally by line range."""
        return workspace_read_text(project, path, start_line, end_line, max_chars)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def write_text(project: str, path: str, content: str, create_parents: bool = True) -> dict[str, Any]:
        """Create or replace a UTF-8 text file inside an active project."""
        return workspace_write_text(project, path, content, create_parents)

    @mcp.tool(annotations=WRITE_NON_IDEMPOTENT)
    def replace_text(project: str, path: str, old: str, new: str, count: int = 0) -> dict[str, Any]:
        """Replace exact text in a file. count=0 replaces every occurrence."""
        return workspace_replace_text(project, path, old, new, count)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def make_directory(project: str, path: str, parents: bool = True) -> dict[str, Any]:
        """Create a directory inside an active project."""
        return workspace_make_directory(project, path, parents)

    @mcp.tool(annotations=WRITE_NON_IDEMPOTENT)
    def remove_path(project: str, path: str, recursive: bool = False) -> dict[str, Any]:
        """Remove a file or directory inside an active project. The project root itself cannot be removed."""
        return workspace_remove_path(project, path, recursive)

    @mcp.tool(annotations=READ_ONLY)
    def search_text(
        project: str,
        query: str,
        path: str = ".",
        glob: str = "*",
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Search literal text recursively under an active project."""
        return workspace_search_text(project, query, path, glob, max_results)

    @mcp.tool(annotations=COMMAND)
    def run_command(
        project: str,
        argv: list[str],
        cwd: str = ".",
        timeout: int = 60,
        max_output: int = 200_000,
    ) -> dict[str, Any]:
        """Run a command without a shell inside an active project and wait for it. argv is an argument vector.

        Blocking: the HTTP call stays open until the command exits, so it is
        cut off at about 5 minutes (MCP client read timeout; tunnel idle
        timeout is the same). Use this only for commands that finish quickly;
        start anything longer with start_command instead.
        """
        return workspace_run_command(project, argv, cwd, timeout, max_output)

    @mcp.tool(annotations=COMMAND)
    def start_command(
        project: str, argv: list[str], cwd: str = ".", timeout: int = 3600,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a durable background process; return a job_id immediately.

        The only reliable way to run long commands: nothing holds the HTTP
        connection open, so the ~5-minute client and tunnel timeouts never
        apply. Survives daemon restarts. Poll get_command_status until the
        status is no longer starting/running/cancelling, and stream output
        with read_command_output (pass next_offset from the previous call).
        Pass request_id to make retries of the same logical command
        idempotent.
        """
        return get_manager().start(project, argv, cwd, timeout, request_id)

    @mcp.tool(annotations=READ_ONLY)
    def get_command_status(project: str, job_id: str) -> dict[str, Any]:
        """Check an asynchronous command by job_id without waiting for completion.

        Poll this every few seconds after start_command; statuses: starting,
        running, cancelling, completed, failed, cancelled, timed_out,
        interrupted.
        """
        return get_manager().status(project, job_id)

    @mcp.tool(annotations=READ_ONLY)
    def read_command_output(
        project: str, job_id: str, stream: str = "stdout",
        offset: int = 0, max_bytes: int = 65536,
    ) -> dict[str, Any]:
        """Read a bounded log chunk of an asynchronous command using byte offsets.

        Pass next_offset from the previous response on subsequent calls to
        stream output while the job is still running.
        """
        return get_manager().output(project, job_id, stream, offset, max_bytes)

    @mcp.tool(annotations=COMMAND)
    def cancel_command(project: str, job_id: str) -> dict[str, Any]:
        """Request termination of a running background command and its process group."""
        return get_manager().cancel(project, job_id)

    return mcp


def run_server() -> None:
    config = load_config()
    mcp = build_server()

    # The listener is loopback-only and is reached externally only via ngrok.
    # ngrok forwards the public Host header, so localhost DNS-rebinding host
    # checks would reject legitimate tunnel traffic. The unguessable endpoint
    # path remains the capability boundary for this personal development tool.
    transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    mcp.run(
        transport="streamable-http",
        host=str(config["host"]),
        port=int(config["port"]),
        streamable_http_path=endpoint_path(config),
        stateless_http=True,
        json_response=True,
        transport_security=transport_security,
    )
