from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class NgrokError(RuntimeError):
    pass


def resolve_ngrok(binary: str = "ngrok") -> str:
    if "/" in binary or "\\" in binary:
        path = Path(binary).expanduser()
        if not path.exists():
            raise NgrokError(f"ngrok binary not found: {path}")
        return str(path)
    resolved = shutil.which(binary)
    if not resolved:
        raise NgrokError(
            "ngrok was not found in PATH. Install it, then run "
            "'ngrok config add-authtoken <TOKEN>' once."
        )
    return resolved


def ngrok_version(binary: str = "ngrok") -> str:
    executable = resolve_ngrok(binary)
    completed = subprocess.run(
        [executable, "version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise NgrokError(completed.stdout.strip() or "ngrok version failed")
    return completed.stdout.strip()


def start_ngrok(config: dict[str, Any]) -> subprocess.Popen[str]:
    executable = resolve_ngrok(str(config.get("ngrok_bin", "ngrok")))
    target = f"http://{config['host']}:{int(config['port'])}"
    argv = [
        executable,
        "http",
        target,
        "--log",
        "stdout",
        "--log-format",
        "json",
    ]
    if config.get("public_url"):
        argv.extend(["--url", str(config["public_url"])])

    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=subprocess.STDOUT,
        text=True,
        close_fds=True,
    )


def discover_public_url(timeout: float = 20.0, api_url: str = "http://127.0.0.1:4040/api/tunnels") -> str | None:
    # The agent API is loopback-only: system or environment proxies (macOS
    # reads them from System Configuration as well) must never intercept it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(api_url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for tunnel in payload.get("tunnels", []):
                public_url = str(tunnel.get("public_url", ""))
                if public_url.startswith("https://"):
                    return public_url.rstrip("/")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    return None
