<img src="ctun.png" alt="CTUN — Son of Cthulhu mascot" width="160" align="left">

# CTUN — Summon Your Local Tools

> *One tunnel to summon them all.*
>
> — Son of Cthulhu

<br clear="all">

A detached, multi-project local MCP gateway for ChatGPT.

The design is deliberately **one connector, one tunnel, many local project roots**:

```text
ChatGPT custom connector
        |
        | one stable URL, configured once
        v
https://<your-dev-domain>.ngrok.app/<secret>/mcp
        |
        v
ngrok agent on your machine
        |
        v
ctun daemon on 127.0.0.1:8765
        |
        +-- kikimora
        +-- game
        +-- sprite-processing
        +-- any other enabled folder
```

There is no tunnel per project. `ctun up` only changes the global project registry. The detached daemon and ngrok endpoint stay the same, so the ChatGPT connector URL does not change.

## Install CTUN (v0.3.4)

Choose **one installation method**. Python libraries are included in binary packages;
ngrok requires a separate account and one-time authorization.
[Latest releases](https://github.com/smollgreymouse/ctun/releases).

### Ubuntu / Debian — APT + DEB

The DEB includes Python dependencies but requires `python3.12` and `ngrok`
through APT. Ensure Python 3.12 is available in your distro's configured
repositories; if not, add a trusted Python 3.12 source or use pipx.

```bash
git clone https://github.com/smollgreymouse/ctun.git
cd ctun
# Adds official signed ngrok APT source. Once per machine:
sudo bash scripts/setup-ngrok-apt.sh
# Download the appropriate .deb from GitHub Releases, then:
sudo apt install ./ctun_0.3.1_amd64.deb
ngrok config add-authtoken 'YOUR_NGROK_TOKEN'
ctun doctor
ctun setup
```

The repository-setup script changes APT configuration, but never reads or changes
ngrok credentials. `apt install` installs declared dependencies; it does not
automatically add third-party repositories.

### Fedora / RPM-based Linux

Requires `python3.12` and ngrok installed separately. The RPM only
**recommends** ngrok because an official compatible RPM repository has not been
verified. Download the RPM from Releases, then:

```bash
sudo dnf install python3.12
sudo dnf install ./ctun-0.3.1-1.x86_64.rpm
# Install ngrok from https://ngrok.com/download/linux and put it on PATH
ngrok version
ngrok config add-authtoken 'YOUR_NGROK_TOKEN'
ctun doctor
ctun setup
```

### Linux tarball

Download `ctun_0.3.1_linux_amd64.tar.gz` from Releases.
Requires CPython 3.12 and separately installed ngrok. The tarball uses fixed
paths under `/opt/ctun` and `/usr/bin`:

```bash
sudo tar -C / -xzf ctun_0.3.1_linux_amd64.tar.gz
ctun --version
```

### macOS Apple Silicon — Homebrew (recommended)

Homebrew cask installs Python 3.12, ngrok and the CTUN PKG through a custom tap:

```sh
brew tap smollgreymouse/ctun https://github.com/smollgreymouse/ctun
brew install --cask smollgreymouse/ctun/ctun
ngrok config add-authtoken 'YOUR_NGROK_TOKEN'
ctun doctor
ctun setup
```

Alternative: download and install `ctun_0.3.1_macos_arm64.pkg`
manually, after installing Python 3.12 and ngrok. The package is currently
unsigned/not notarized; macOS may require an explicit trust decision.
macOS Intel is not packaged in this release.

### Windows x64 — graphical installer (recommended)

1. Download `ctun_0.3.1_windows_amd64.exe` from Releases and run it.
   It installs per-user, bundles Python and its libraries, and does not need admin rights.
2. Install ngrok: in PowerShell run `winget install Ngrok.Ngrok`.
3. Open a new PowerShell, then run:

```powershell
ngrok config add-authtoken YOUR_NGROK_TOKEN
& "$env:LOCALAPPDATA\Programs\CTUN\ctun.exe" doctor
& "$env:LOCALAPPDATA\Programs\CTUN\ctun.exe" setup
```

The installer does not silently change PATH. You can add
`%LOCALAPPDATA%\Programs\CTUN` to the **user** PATH if you want `ctun`
everywhere. The older portable ZIP remains available but requires Python 3.12
and does not register an application in Windows.

### Any OS — pipx from our GitHub repository

Keep this option for development and for hosts where a native package is unsuitable.
Install Python 3.11+ and pipx through your platform package manager first,
and install ngrok independently.

```bash
pipx install 'git+https://github.com/smollgreymouse/ctun.git@v0.3.1'
# Alternatively track main:
# pipx install 'git+https://github.com/smollgreymouse/ctun.git'
ctun --version
ctun doctor
```

On Windows, use PowerShell without shell-specific single-quote assumptions.
An SSH-based URL also works when GitHub SSH access is configured.

### Updating or switching installation methods

```bash
ctun shutdown         # stop the existing daemon before replacing code
# Native packages: reinstall a newer package from Releases;
# Homebrew: brew upgrade --cask smollgreymouse/ctun/ctun
# pipx: pipx upgrade ctun
ctun --version
ctun doctor
ctun up /path/to/your/project
```

Do not leave an old `pipx` `ctun` shadowing a new system package on PATH:
check with `command -v ctun` (macOS/Linux) or `Get-Command ctun`
(Windows). User state and the secret connector URL are preserved in the
per-user state directory, and packages do not rewrite ngrok credentials.

## One-time setup

Run:

```bash
ctun setup
```

On the first successful start, ngrok assigns the account's development domain. `ctun` discovers it from the local ngrok agent, stores it, and from then on starts ngrok with that same URL.

The output contains a connector URL similar to:

```text
https://example.ngrok.app/4A1v...random-secret...qQ/mcp
```

Configure the ChatGPT custom connector with that **full URL exactly once**.

The random path component is generated locally and persisted. It is intentionally part of the URL: this project exposes powerful local-development tools and does not assume ChatGPT can send a custom static Authorization header. Treat the connector URL as a secret. If it leaks, remove the local state/config or rotate the token in a future release and update the connector.

## Daily use

In any project directory:

```bash
cd ~/Projects/game
ctun up
```

The project name defaults to the folder name, so the example becomes `game`.

Open another terminal in another project:

```bash
cd ~/Projects/kikimora
ctun up
```

Both projects are now available through the **same** MCP endpoint.

The command returns after enabling the project. The daemon is detached, so that terminal can be closed.

Disable the current project from any later terminal opened in that directory:

```bash
ctun down
```

Or disable it globally by registered name from anywhere:

```bash
ctun down game
```

Re-enable it:

```bash
ctun up ~/Projects/game
```

If two different directories have the same basename, give one an explicit registry name:

```bash
ctun up ~/work/client/game --name client-game
```

## Global registry

The registry is not tied to the terminal that started a project.

```bash
ctun list
ctun list --active
ctun list --json
```

Example:

```text
ON   game               /home/me/Projects/game
ON   kikimora           /home/me/Projects/kikimora
OFF  sprite-processing  /home/me/Projects/sprite-processing
```

`down` only disables a project. It remains in the registry.

Remove a disabled record completely with:

```bash
ctun remove game
```

## Gateway lifecycle

```bash
ctun status
ctun url
ctun logs
ctun logs -f
ctun restart
ctun shutdown
```

`ctun shutdown` stops the shared MCP daemon and ngrok process but preserves the project registry. A later `ctun up` automatically starts the gateway again.

### Boot persistence and crash recovery (Linux, systemd)

Install the user-level unit to keep the tunnel up across reboots and restart
the daemon whenever its process dies:

```bash
cp packaging/linux/ctun.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ctun
sudo loginctl enable-linger $USER   # start at boot without an active login
```

The ngrok agent itself reconnects through network switches, Wi-Fi changes and
airplane mode; the daemon additionally supervises the agent and restarts it
with backoff if the process dies, reporting the outage in runtime state.

When the unit runs the daemon, the CLI detects that via the process cgroup and
routes the lifecycle commands through `systemctl --user` automatically:
`ctun shutdown` stops the unit (it stays down), `ctun restart` and `ctun up`
start it through systemd so the daemon is never a stray child. `ctun status`
shows `managed by ctun.service` in that case. If systemd is unavailable the
commands fall back to direct process management.

## Configuration and state

By default all persistent runtime state lives under:

```text
~/.local/state/chatgpt-tun/
```

The important files are:

```text
config.json    stable ngrok origin, local port and secret MCP path
registry.json  every known project and whether it is enabled
runtime.json   detached daemon/ngrok PIDs and live status
daemon.log     daemon and ngrok logs
```

For tests or isolated installations, set `CHATGPT_TUN_HOME`.

Inspect configuration:

```bash
ctun config
```

If automatic ngrok URL discovery is unavailable, pin the already-assigned development URL manually:

```bash
ctun config --public-url https://your-assigned-domain.ngrok.app
ctun restart
```

## MCP tools

The single MCP server exposes the active registry dynamically. Projects can be enabled or disabled while ChatGPT remains connected.

Current tools:

- `list_projects`
- `list_directory`
- `read_text`
- `write_text`
- `replace_text`
- `make_directory`
- `remove_path`
- `search_text`
- `run_command` (short, synchronous operations)
- `start_command` (non-blocking long operations, returns `job_id`)
- `get_command_status`
- `read_command_output` (bounded stdout/stderr chunks with byte offsets)
- `cancel_command`

Every workspace tool requires a `project` argument. File operations resolve paths against that project's root and reject `..`, absolute paths, and symlink traversal outside the root.

`run_command` does **not** invoke a shell, but it is intentionally a powerful developer tool: an executable launched inside a project can still access anything that the local OS user can access. The secret connector URL therefore must be handled like a credential.

Disabling a project with `ctun down` takes effect immediately for subsequent MCP calls.

## Why one tunnel instead of one per project?

A tunnel-per-project design forces either multiple ChatGPT connectors or changing the connector whenever the active folder changes. This project keeps the network identity stable and moves project selection into the MCP protocol itself.

The persistent objects are:

```text
one ngrok development domain
one secret MCP URL
one detached local daemon
one global project registry
```

Project activation is just registry state.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The implementation uses the current MCP Python SDK v2 and Streamable HTTP.

## Long-running commands without HTTP timeouts

Use `start_command` instead of `run_command` for builds, tests and other long
operations. It returns a `job_id` immediately; the command runs in the gateway,
independently of the ChatGPT HTTP request. Poll `get_command_status` and call
`read_command_output` separately for stdout and stderr, passing the returned
`next_offset` for subsequent calls. Poll at a reasonable interval (e.g. 5–15
seconds), not continuously. Use `cancel_command` to request cancellation.

Provide a stable `request_id` when network retries are possible. Within a project
the same request ID and arguments return the same job rather than executing twice.
A reused request ID with different arguments raises an error.

Jobs and log files reside in `~/.local/state/chatgpt-tun/jobs/` (or the
configured `CHATGPT_TUN_HOME`). Up to four commands execute concurrently. Each
output stream is capped at 8 MiB, with truncation flagged explicitly; further
output is drained and discarded to avoid child-process deadlock. The last 100
jobs are retained, removing oldest completed jobs as needed.

Metadata and completed output survive daemon restart. Commands still running
during an unexpected gateway restart are marked `interrupted`; this does not
promise that arbitrary descendant processes were killed after an OS crash.
On graceful gateway shutdown managed process groups are terminated. Commands
should be designed to be retry-safe when recovering from unexpected crashes.

The legacy `run_command` is unchanged, but it blocks the HTTP call until the
command exits: the stock MCP client read timeout and ngrok's idle timeout cut
such calls off at roughly five minutes. Anything that may run longer than a
couple of minutes must go through `start_command`. Streamable HTTP still uses
stateless JSON responses; asynchronous jobs avoid holding an HTTP call open and
do not require SSE/notification support in ChatGPT.


## Packaging and release engineering

The canonical version comes from `pyproject.toml`. Tags `vX.Y.Z` trigger
independent Linux and macOS/Windows workflows. CI tests on each OS before
publishing. The Linux `.deb`, `.rpm` and `.tar.gz` are built on Linux;
macOS `.pkg` on macOS; Windows portable `.zip` and native `.exe` on Windows.

Packages currently target Linux x86-64 (CPython 3.12), macOS arm64
(CPython 3.12), and Windows x64 (bundled Python for the EXE).
`ctun doctor` is read-only: it diagnoses dependencies and reported
tunnel readiness without starting a tunnel or modifying credentials.
A configured public URL alone is **not proof** of ngrok authorization.

Build locally with `bash scripts/build-packages.sh` (Linux),
`bash scripts/build-macos.sh` (macOS), or
`./scripts/build-windows.ps1` (Windows portable archive).
Windows native EXE is built separately in GitHub Actions using
PyInstaller and Inno Setup. Check the matching workflow status and
uploaded assets before tagging any new release.
