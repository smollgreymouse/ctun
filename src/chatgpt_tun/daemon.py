from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

from .jobs import get_manager
from .mcp_server import run_server
from .ngrok import NgrokError, discover_public_url, start_ngrok
from .state import (
    clear_runtime,
    connector_url,
    load_config,
    normalize_public_url,
    update_config,
    update_runtime,
    write_runtime,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _install_sigterm_handler() -> None:
    """Turn SIGTERM into KeyboardInterrupt so the cleanup in main()'s
    finally/atexit runs when the daemon is stopped with `ctun down` or
    `ctun restart` (they signal with SIGTERM). Without this, uvicorn's
    capture-and-reraise kills the process with the default disposition
    after a graceful HTTP shutdown, skipping cleanup and leaking running
    job processes."""
    if os.name == "nt":
        return

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handler)


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def _discover_tunnel(process: subprocess.Popen[str], initial_config: dict[str, Any]) -> None:
    actual = discover_public_url(timeout=25.0)
    if actual is None:
        code = process.poll()
        message = (
            f"ngrok exited with status {code}" if code is not None else
            "ngrok public URL was not discovered within 25 seconds"
        )
        update_runtime(lambda runtime: runtime.update({"ngrok_error": message}))
        return

    expected = initial_config.get("public_url")
    if expected and normalize_public_url(str(expected)) != normalize_public_url(actual):
        update_runtime(
            lambda runtime: runtime.update(
                {
                    "ngrok_error": (
                        f"ngrok opened {actual}, but configured stable URL is {expected}"
                    )
                }
            )
        )
        return

    if not expected:
        config = update_config(lambda data: data.update({"public_url": normalize_public_url(actual)}))
    else:
        config = load_config()

    update_runtime(
        lambda runtime: runtime.update(
            {
                "public_url": normalize_public_url(actual),
                "connector_url": connector_url(config),
                "ngrok_ready": True,
            }
        )
    )


def main() -> int:
    pid = os.getpid()
    _install_sigterm_handler()
    ngrok_process: subprocess.Popen[str] | None = None
    config = load_config()

    write_runtime(
        {
            "daemon_pid": pid,
            "started_at": _now(),
            "host": config["host"],
            "port": config["port"],
            "ngrok_ready": False,
            "public_url": config.get("public_url"),
            "connector_url": connector_url(config),
        }
    )

    def cleanup() -> None:
        get_manager().shutdown()
        _terminate(ngrok_process)
        clear_runtime(expected_pid=pid)

    atexit.register(cleanup)

    try:
        ngrok_process = start_ngrok(config)
        update_runtime(lambda runtime: runtime.update({"ngrok_pid": ngrok_process.pid}))
    except NgrokError as exc:
        update_runtime(lambda runtime: runtime.update({"error": str(exc), "ngrok_error": str(exc)}))
        print(f"ngrok error: {exc}", flush=True)
        return 2
    except OSError as exc:
        update_runtime(lambda runtime: runtime.update({"error": str(exc), "ngrok_error": str(exc)}))
        print(f"ngrok start failed: {exc}", flush=True)
        return 2

    watcher = threading.Thread(
        target=_discover_tunnel,
        args=(ngrok_process, config),
        name="ngrok-discovery",
        daemon=True,
    )
    watcher.start()

    try:
        run_server()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        update_runtime(lambda runtime: runtime.update({"error": repr(exc)}))
        print(f"MCP server failed: {exc!r}", flush=True)
        return 1
    finally:
        get_manager().shutdown()
        _terminate(ngrok_process)
        clear_runtime(expected_pid=pid)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
