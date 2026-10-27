from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .jobs import get_manager
from .mcp_server import run_server
from .ngrok import NgrokError, discover_public_url, start_ngrok
from .state import (
    clear_runtime,
    connector_url,
    load_config,
    load_runtime,
    normalize_public_url,
    update_config,
    update_runtime,
    write_runtime,
)

NGROK_POLL_SECONDS = 2.0
NGROK_REDISCOVER_SECONDS = 10.0
NGROK_RESTART_BACKOFF_MAX = 30.0


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
    api_url = str(initial_config.get("ngrok_api_url") or "http://127.0.0.1:4040/api/tunnels")
    actual = discover_public_url(
        timeout=25.0, api_url=api_url,
        report=lambda exc: print("ngrok discovery:", exc, flush=True),
    )
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


def _mark_tunnel_down(process: subprocess.Popen[str]) -> None:
    code = process.poll()
    update_runtime(
        lambda runtime: runtime.update(
            {
                "ngrok_error": (
                    f"ngrok exited with status {code}; restarting"
                    if code is not None else
                    "ngrok is not running; restarting"
                ),
                "ngrok_ready": False,
                "public_url": None,
                "connector_url": None,
            }
        )
    )


def _supervise_ngrok(config: dict[str, Any], holder: dict[str, Any]) -> None:
    """Keep an ngrok agent alive for the whole daemon lifetime.

    Network switches and outages are handled by the agent itself (it
    reconnects to the same reserved domain); this loop covers the case
    the agent cannot survive on its own — its process dying. Without it
    the tunnel would stay down forever while the MCP server keeps
    claiming everything is fine. While the tunnel is not ready the loop
    also retries discovery: a slow agent API (cold machine, network
    still settling) must not leave the tunnel unreported forever. The
    holder dict carries the current process so the daemon cleanup
    always stops the live one.
    """
    _discover_tunnel(holder["process"], config)
    next_discovery = time.monotonic() + NGROK_REDISCOVER_SECONDS
    backoff = 1.0
    while True:
        process = holder["process"]
        if process is None or process.poll() is None:
            time.sleep(NGROK_POLL_SECONDS)
            if not load_runtime().get("ngrok_ready") and time.monotonic() >= next_discovery:
                _discover_tunnel(process, config)
                next_discovery = time.monotonic() + NGROK_REDISCOVER_SECONDS
            continue

        _mark_tunnel_down(process)
        time.sleep(backoff)
        backoff = min(backoff * 2, NGROK_RESTART_BACKOFF_MAX)
        try:
            replacement = start_ngrok(config)
        except (NgrokError, OSError) as exc:
            update_runtime(lambda runtime: runtime.update({"ngrok_error": str(exc)}))
            continue
        holder["process"] = replacement
        update_runtime(
            lambda runtime: runtime.update(
                {"ngrok_pid": replacement.pid, "ngrok_error": None}
            )
        )
        _discover_tunnel(replacement, config)
        next_discovery = time.monotonic() + NGROK_REDISCOVER_SECONDS
        backoff = 1.0


def main() -> int:
    pid = os.getpid()
    _install_sigterm_handler()
    ngrok_holder: dict[str, subprocess.Popen[str] | None] = {"process": None}
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
        _terminate(ngrok_holder["process"])
        clear_runtime(expected_pid=pid)

    atexit.register(cleanup)

    try:
        ngrok_holder["process"] = start_ngrok(config)
        update_runtime(
            lambda runtime: runtime.update({"ngrok_pid": ngrok_holder["process"].pid})
        )
    except NgrokError as exc:
        update_runtime(lambda runtime: runtime.update({"error": str(exc), "ngrok_error": str(exc)}))
        print(f"ngrok error: {exc}", flush=True)
        return 2
    except OSError as exc:
        update_runtime(lambda runtime: runtime.update({"error": str(exc), "ngrok_error": str(exc)}))
        print(f"ngrok start failed: {exc}", flush=True)
        return 2

    supervisor = threading.Thread(
        target=_supervise_ngrok,
        args=(config, ngrok_holder),
        name="ngrok-supervisor",
        daemon=True,
    )
    supervisor.start()

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
        _terminate(ngrok_holder["process"])
        clear_runtime(expected_pid=pid)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
