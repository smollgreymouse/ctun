from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
import os
import sys
import time
from pathlib import Path

from .doctor import print_diagnostics
from .ngrok import NgrokError, ngrok_version
from .process import (
    daemon_running,
    daemon_service_unit,
    ensure_daemon,
    pid_alive,
    read_log_tail,
    start_daemon,
    stop_daemon,
    wait_for_connector_url,
)
from .registry import activate_project, deactivate_project, list_projects, remove_project
from .state import (
    connector_url,
    load_config,
    load_runtime,
    log_path,
    normalize_public_url,
    state_dir,
    update_config,
)


def _print_connector_status(wait: float = 20.0) -> int:
    url = wait_for_connector_url(wait)
    runtime = load_runtime()
    if url:
        print(f"Connector URL: {url}")
        return 0
    error = runtime.get("ngrok_error") or runtime.get("error")
    if error:
        print(f"Tunnel is not ready: {error}", file=sys.stderr)
    else:
        print("Tunnel is not ready yet.", file=sys.stderr)
    print(f"Log: {log_path()}", file=sys.stderr)
    return 2


def cmd_setup(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        version = ngrok_version(str(config.get("ngrok_bin", "ngrok")))
    except NgrokError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(version)
    print(f"State directory: {state_dir()}")
    try:
        ensure_daemon()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        tail = read_log_tail(30)
        if tail:
            print(tail, file=sys.stderr)
        return 2
    return _print_connector_status(args.wait)


def cmd_up(args: argparse.Namespace) -> int:
    path = Path(args.path or ".")
    try:
        record = activate_project(path, args.name)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        ensure_daemon()
    except RuntimeError as exc:
        print(f"Project '{record['name']}' enabled, but daemon start failed: {exc}", file=sys.stderr)
        return 2

    print(f"ON  {record['name']}  {record['path']}")
    return _print_connector_status(args.wait)


def cmd_down(args: argparse.Namespace) -> int:
    try:
        record = deactivate_project(args.target)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"OFF {record['name']}  {record['path']}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    try:
        record = remove_project(args.target, force=args.force)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"REMOVED {record['name']}  {record['path']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = list_projects(active_only=args.active)
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print("No registered projects.")
        return 0
    width = max(len(str(record["name"])) for record in records)
    for record in records:
        marker = "ON " if record.get("active") else "OFF"
        print(f"{marker}  {record['name']:<{width}}  {record['path']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    config = load_config()
    daemon_pid = runtime.get("daemon_pid")
    ngrok_pid = runtime.get("ngrok_pid")
    unit = daemon_service_unit(daemon_pid)
    suffix = ""
    if daemon_pid:
        suffix = f" (pid {daemon_pid}" + (f", managed by {unit})" if unit else ")")
    elif unit:
        suffix = f" (managed by {unit})"
    print(f"daemon: {'running' if pid_alive(daemon_pid) else 'stopped'}{suffix}")
    print(f"ngrok:  {'running' if pid_alive(ngrok_pid) else 'stopped'}" + (f" (pid {ngrok_pid})" if ngrok_pid else ""))
    print(f"local:  http://{config['host']}:{config['port']}{'/' + str(config['endpoint_token']) + '/mcp'}")
    public = runtime.get("public_url") or config.get("public_url")
    print(f"public: {public or '(not discovered yet)'}")
    url = runtime.get("connector_url") if runtime.get("ngrok_ready") else connector_url(config)
    print(f"connector: {url or '(not discovered yet)'}")
    print(f"projects: {sum(1 for r in list_projects() if r.get('active'))} active / {len(list_projects())} registered")
    if runtime.get("ngrok_error"):
        print(f"ngrok error: {runtime['ngrok_error']}")
    if runtime.get("error"):
        print(f"daemon error: {runtime['error']}")
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    if runtime.get("ngrok_ready") and runtime.get("connector_url"):
        print(runtime["connector_url"])
        return 0
    config = load_config()
    url = connector_url(config)
    if url:
        print(url)
        if not daemon_running():
            print("(daemon is currently stopped)", file=sys.stderr)
        elif not runtime.get("ngrok_ready"):
            print("(ngrok has not confirmed the endpoint yet)", file=sys.stderr)
        return 0
    print("No public URL has been discovered yet. Run 'ctun setup'.", file=sys.stderr)
    return 2


def cmd_shutdown(args: argparse.Namespace) -> int:
    stopped = stop_daemon()
    print("Gateway stopped." if stopped else "Gateway was already stopped.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    stop_daemon()
    try:
        start_daemon()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("Gateway restarted.")
    return _print_connector_status(args.wait)


def cmd_logs(args: argparse.Namespace) -> int:
    path = log_path()
    if not args.follow:
        print(read_log_tail(args.lines))
        return 0

    if not path.exists():
        path.touch()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.2)
    except KeyboardInterrupt:
        return 0


def cmd_config(args: argparse.Namespace) -> int:
    changed = False

    def mutate(config: dict) -> None:
        nonlocal changed
        if args.public_url is not None:
            value = args.public_url.strip()
            config["public_url"] = normalize_public_url(value) if value else None
            changed = True
        if args.ngrok_bin is not None:
            config["ngrok_bin"] = args.ngrok_bin
            changed = True
        if args.port is not None:
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            config["port"] = args.port
            changed = True

    try:
        config = update_config(mutate)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    safe = {
        "host": config["host"],
        "port": config["port"],
        "ngrok_bin": config["ngrok_bin"],
        "public_url": config.get("public_url"),
        "connector_url": connector_url(config),
        "endpoint_token": "(hidden)",
        "state_dir": str(state_dir()),
    }
    print(json.dumps(safe, indent=2))
    if changed and daemon_running():
        print("Configuration changed. Run 'ctun restart' to apply it.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctun",
        description="One stable ngrok-backed MCP connector for many local projects.",
    )
    try:
        package_version = version("ctun")
    except PackageNotFoundError:
        package_version = "development"
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Inspect platform dependencies and tunnel state without modifying them")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=lambda args: print_diagnostics(args.json))

    setup = sub.add_parser("setup", help="Verify ngrok, start the detached gateway, and print the connector URL")
    setup.add_argument("--wait", type=float, default=25.0, help="seconds to wait for ngrok")
    setup.set_defaults(func=cmd_setup)

    up = sub.add_parser("up", aliases=["on"], help="Enable a project; defaults to the current directory")
    up.add_argument("path", nargs="?", default=".")
    up.add_argument("--name", help="override the project name (default: folder name)")
    up.add_argument("--wait", type=float, default=25.0, help="seconds to wait for ngrok")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", aliases=["off"], help="Disable a project; defaults to the current directory")
    down.add_argument("target", nargs="?", help="project name or registered path")
    down.set_defaults(func=cmd_down)

    remove = sub.add_parser("remove", aliases=["rm"], help="Remove a project from the global registry")
    remove.add_argument("target", nargs="?", help="project name or registered path; defaults to current directory")
    remove.add_argument("--force", action="store_true", help="remove even when active")
    remove.set_defaults(func=cmd_remove)

    ls = sub.add_parser("list", aliases=["ls"], help="List the global project registry")
    ls.add_argument("--active", action="store_true", help="show only enabled projects")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    status = sub.add_parser("status", help="Show daemon, tunnel and registry status")
    status.set_defaults(func=cmd_status)

    url = sub.add_parser("url", help="Print the stable ChatGPT connector URL")
    url.set_defaults(func=cmd_url)

    shutdown = sub.add_parser("shutdown", help="Stop the detached gateway and ngrok; registry is preserved")
    shutdown.set_defaults(func=cmd_shutdown)

    restart = sub.add_parser("restart", help="Restart the detached gateway and ngrok")
    restart.add_argument("--wait", type=float, default=25.0)
    restart.set_defaults(func=cmd_restart)

    logs = sub.add_parser("logs", help="Show daemon/ngrok logs")
    logs.add_argument("-n", "--lines", type=int, default=80)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    config = sub.add_parser("config", help="Show or change global configuration")
    config.add_argument("--public-url", help="pin the ngrok origin, e.g. https://name.ngrok.app; empty string clears it")
    config.add_argument("--ngrok-bin", help="ngrok executable name or path")
    config.add_argument("--port", type=int, help="local MCP port")
    config.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    if (argv if argv is not None else sys.argv[1:]) == ["--internal-daemon"]:
        from .daemon import main as daemon_main
        return daemon_main()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
