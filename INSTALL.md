# INSTALL — step by step (written to be followed literally, also by an AI agent)

Every step has a command and the expected result. Run the steps in order. If a check does not match, stop and go
to [Troubleshooting](#troubleshooting); do not improvise workarounds. Nothing here needs root, except the
optional `--systemd system` mode.

Conventions used below (defaults of `install.sh`, user mode):

| Name | Value |
|---|---|
| `REPO` | where the repository is cloned, e.g. `~/agent-memory` |
| `BASE` | `~/.local/share/agent-memory` (data, venv, model, token) |
| `CONFIG` | `~/.config/agent-memory/config.toml` |
| `AM` | `~/.local/share/agent-memory/venv/bin/agent-memory` |
| URL | `http://127.0.0.1:8765` |

Never print, paste or log the token. Refer to it by its file path only.

## 1. Prerequisites

```bash
uname -s
```
Expected: `Linux` (systemd services) or `Darwin` (macOS: see [macOS notes](#macos-notes)).

```bash
python3 -c 'import sys; print(sys.version_info >= (3, 11))' ; command -v uv
```
Expected: `True`, or a path for `uv` (the installer then fetches Python 3.12). If both fail, install Python 3.11+
or uv first.

```bash
systemctl --user is-system-running
```
Expected: `running` or `degraded`. Anything else (e.g. `Failed to connect to bus`): use `--systemd none` in step 3
and see Troubleshooting.

```bash
(ss -ltn 2>/dev/null || netstat -ltn) | grep -c ':8765 '
```
Expected: `0` (port free). Otherwise pick another port and pass `--port N` in step 3 (and use it everywhere below).

```bash
df -Pk ~ | awk 'NR==2 {print ($4 > 2000000) ? "ok" : "low"}'
```
Expected: `ok` (at least 2 GB free).

## 2. Get the code

```bash
git clone https://github.com/AlexGHerrera/agent-memory.git ~/agent-memory && cd ~/agent-memory && ls install.sh config.example.toml
```
Expected: `config.example.toml  install.sh`.

## 3. Install

Pick the timezone of the user (IANA name) and the language of summaries (`en` or `es`):

```bash
cd ~/agent-memory && ./install.sh --timezone Europe/Madrid --language en
```

Useful options (all optional; `./install.sh --help` lists them):
`--user-name "Sam"` (how prompts call the user), `--port N`, `--claude-projects DIR|none`,
`--openclaw DIR|none` (default: `~/.openclaw/agents` if it exists), `--systemd user|system|none`,
`--register-claude` (register the MCP server in Claude Code), `--hooks` (install the Claude Code hooks).

The installer downloads ~470 MB (embedding model) once. Expected last lines:

```
== done
config:     /home/<you>/.config/agent-memory/config.toml
database:   /home/<you>/.local/share/agent-memory/data/memory.db
token file: /home/<you>/.local/share/agent-memory/config/mcp-token (600)
MCP URL:    http://127.0.0.1:8765/mcp   (header: Authorization: Bearer <contents of the token file>)
```

Re-running `install.sh` is safe: it updates the code and units and keeps config, token and data.

## 4. Check files and permissions

```bash
stat -c '%a %n' ~/.config/agent-memory/config.toml ~/.local/share/agent-memory/config/mcp-token ~/.local/share/agent-memory
```
Expected: `600 …config.toml`, `600 …mcp-token`, `700 …agent-memory`.

```bash
~/.local/share/agent-memory/venv/bin/agent-memory doctor
```
Expected JSON with `"bind_address": "ok (loopback)"`, `"token_file": "ok (600)"`, `"model_cached": true`.
`"llm"` says whether the Claude Code CLI was found (needed only for the nightly summaries).

## 5. Check the services

```bash
systemctl --user is-active agent-memory.service agent-memory-ingest.timer agent-memory-consolidate.timer
```
Expected: three lines `active`.

```bash
journalctl --user -u agent-memory.service -n 20 --no-pager | grep -c 'Uvicorn running on http://127.0.0.1:8765'
```
Expected: `1` or more (the server takes a few seconds to load the model after starting).

Keep the services running after logout (needed on servers):

```bash
loginctl enable-linger "$(id -un)" && loginctl show-user "$(id -un)" -p Linger
```
Expected: `Linger=yes`. If it is refused, ask the machine's administrator to run it.

## 6. Smoke test (calls every tool)

```bash
~/agent-memory/scripts/smoke.sh --config ~/.config/agent-memory/config.toml --url http://127.0.0.1:8765
```
Expected: 12 lines starting with `PASS` and `12/12 checks passed`; exit code 0. It saves one note
"agent-memory smoke test …" in the memory.

Optional, independent of the installed service (throwaway instance on port 18765, reuses the downloaded model):

```bash
~/agent-memory/scripts/smoke.sh --bin ~/.local/share/agent-memory/venv/bin/agent-memory --models-dir ~/.local/share/agent-memory/models
```
Expected: `14/14 checks passed` and `PASS bind: 127.0.0.1 only`.

## 7. First ingestion

The timer ingests every 15 minutes. To do it now (it can take a while with a long history):

```bash
systemctl --user start agent-memory-ingest.service && ~/.local/share/agent-memory/venv/bin/agent-memory stats
```
Expected: JSON like `{"messages": N, "sessions": M, "vectors": V, ...}` with N > 0 if there were transcripts.

```bash
~/.local/share/agent-memory/venv/bin/agent-memory day today
```
Expected: `# Day YYYY-MM-DD` followed by today's sessions (if any).

## 8. Connect the agent

### Claude Code

```bash
~/agent-memory/scripts/register-claude-code.sh && claude mcp list | grep '^memory'
```
Expected: `registered (helper): memory -> http://127.0.0.1:8765/mcp` and a `memory:` line marked
`✔ Connected`.

By default the script registers a `headersHelper` (verified with Claude Code 2.1.300): Claude Code runs
`~/.claude/hooks/agent-memory/headers_helper.py --token-file <token file>` on each connection to get the header,
so the token stays only in its 600 file: it is not stored in `~/.claude.json` and never appears on a command line.
If your Claude Code is too old for `headersHelper` (the line shows `Failed to connect … 401`), rerun with
`--header-mode`. Then, be aware: the token is passed on the `claude mcp add` command line (visible for a moment
to other users of the machine in the process list) and Claude Code stores it in plain text in `~/.claude.json`
(the script makes that file 600). Never commit or share `~/.claude.json`.

Hooks (context at session start, notes before compaction, summary at the end):

```bash
~/agent-memory/scripts/install-hooks.sh && python3 -c "import json,os; h=json.load(open(os.path.expanduser('~/.claude/settings.json')))['hooks']; print(sorted(k for k in h if k in ('SessionStart','PreCompact','SessionEnd')))"
```
Expected: `['PreCompact', 'SessionEnd', 'SessionStart']`. A backup `settings.json.bak-<timestamp>` is kept.
Optional hook settings (persona/preferences files to inject, a task list, caps) are environment variables or
`~/.config/agent-memory/hooks.json`; see the header of `hooks/claude-code/common.py`.

Claude Code on another machine than the server: copy the token file over a secure channel to
`~/.config/agent-memory/mcp-token` (mode 600) on that machine, register with
`scripts/register-claude-code.sh --url http://<server-private-ip>:8765`, install the hooks there and set
`AGENT_MEMORY_URL=http://<server-private-ip>:8765` and `AGENT_MEMORY_UPLOAD=1` (or the same keys in
`~/.config/agent-memory/hooks.json`) so transcripts are uploaded at SessionEnd. The server must bind to that
private IP: see step 10.

### OpenClaw

Follow [docs/OPENCLAW.md](docs/OPENCLAW.md): add the MCP server (URL + Bearer header) and enable
`[[openclaw_sources]]` for ingestion.

### Any other agent

MCP streamable HTTP at `http://127.0.0.1:8765/mcp` with `Authorization: Bearer <token>`. Feed transcripts in the
generic JSONL format: [docs/INGESTION.md](docs/INGESTION.md).

## 9. Optional: check the nightly consolidation now

Uses the LLM (default: `claude -p --model haiku` with the Claude Code login of this user; set `llm_command` in the
config for another model).

```bash
~/.local/share/agent-memory/venv/bin/agent-memory consolidate --include-active | tail -1
```
Expected: JSON with `"calls": N, "errors": 0`. With errors: `claude` is not logged in or not in the PATH of the
service (`claude_bin` in the config takes an absolute path).

## 10. Optional: expose over a private network

> **Warning: the server speaks plain HTTP.** The Bearer token and every memory result travel unencrypted. Bind to
> a non-loopback address **only** if that address belongs to an encrypted overlay network (WireGuard,
> Tailscale), or put a TLS-terminating reverse proxy in front of it. **Never** bind it to a plain LAN address
> (home or office Wi-Fi, shared networks) and never to a public network: anyone on that network could read the
> token and then the whole memory.

1. Find the server's address on the encrypted network (e.g. `tailscale ip -4`, or the WireGuard interface IP).
2. In `CONFIG` set `host = "<that IP>"` and, if clients use a hostname, `allowed_hosts = ["<name>:8765", "<name>"]`.
3. `systemctl --user restart agent-memory.service`, then rerun step 6 with `--url http://<that IP>:8765`.
4. Restrict the port to your own devices with the network's ACLs or a firewall rule. Never use `0.0.0.0` (the
   server refuses it) and never expose the port to the internet.

## Uninstall

Stops and removes the services and the code; **keeps your data** unless you delete it explicitly.

```bash
systemctl --user disable --now agent-memory.service agent-memory-ingest.timer agent-memory-consolidate.timer
mkdir -p ~/agent-memory-removed
mv ~/.config/systemd/user/agent-memory*.service ~/.config/systemd/user/agent-memory*.timer ~/agent-memory-removed/
systemctl --user daemon-reload
claude mcp remove memory -s user          # if it was registered in Claude Code
```
Remove the three agent-memory entries from the `hooks` section of `~/.claude/settings.json` (or restore the
`settings.json.bak-*` backup) and move `~/.claude/hooks/agent-memory` away.

Data (conversations, facts, token) lives in `~/.local/share/agent-memory` and the config in
`~/.config/agent-memory`. Deleting them is irreversible: back up `data/memory.db` first if in doubt, and only do
it if the user explicitly asks.

System mode (`--systemd system`): same with `sudo systemctl …` and the units in `/etc/systemd/system/` (plus their
`*.service.d/` drop-ins); data in `/opt/agent-memory`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `install.sh`: `Python >= 3.11 not found and uv not installed` | Install python3.11+ (`apt install python3.11 python3.11-venv`) or uv, rerun. |
| `systemctl --user`: `Failed to connect to bus` | No user session (e.g. a non-login shell). Try `export XDG_RUNTIME_DIR=/run/user/$(id -u)`; enable lingering (step 5); or install with `--systemd none` and run `agent-memory serve` under your own supervisor. |
| A unit fails with `status=226/NAMESPACE` or `Failed to set up mount namespacing` | The kernel/container does not allow the sandboxing drop-ins (in user mode they need unprivileged user namespaces). Rerun `install.sh` with the same options plus `--no-hardening`. |
| Server exits: `Listening on 0.0.0.0/:: is forbidden` or `is a public address` | `host` must be 127.0.0.1 or a private-network IP. |
| Server exits: `… must have mode 600` | `chmod 600` the token file. |
| Server exits: `MCP token too short` / file missing | `agent-memory init-token` (creates it if missing; never overwrites). |
| Server log: model download error with `HF_HUB_OFFLINE=1` | The model was not downloaded: `agent-memory download-model` (needs internet once), restart the service. |
| Client gets `401` | Wrong or missing `Authorization: Bearer` header; the token must be the exact file content (no newline). |
| Client gets `421`/`Invalid Host header` | Clients use a hostname: add it to `allowed_hosts` (with and without `:port`), restart. |
| Client gets `403 Invalid Origin header` | The request carried an `Origin` header (browser-style client). Refused by design; call it from a non-browser MCP client. |
| Port already in use | Another process listens on 8765: reinstall with `--port N` or edit `port` in the config and restart. |
| `search` finds nothing right after install | Nothing ingested yet: step 7. Check `claude_sources` paths with `agent-memory doctor`. |
| Consolidation `errors` > 0 | LLM CLI missing or not logged in for that user; see step 9. Summaries are retried the next night. |
| Hooks do nothing | Check `~/.local/state/agent-memory-hooks/hooks.log`. They fail open by design; `AGENT_MEMORY_NO_HOOKS=1` disables them. |
| A secret got stored | Extend the filter or add a literal hash (`scripts/make-literal-hashes.py`), then `agent-memory refilter` (dry run) and `agent-memory refilter --apply`. |

## macOS notes

- `./install.sh --systemd none` works (Python 3.11+ or uv required). Start the server with
  `AGENT_MEMORY_CONFIG=~/.config/agent-memory/config.toml ~/.local/share/agent-memory/venv/bin/agent-memory serve`,
  or create a launchd agent (`~/Library/LaunchAgents`) running that command with `KeepAlive`, plus two more for
  `agent-memory ingest` (every 900 s) and `agent-memory consolidate` (daily).
- The hooks can read the token from the keychain instead of a file:
  `security add-generic-password -s agent-memory -a mcp -w` (prompts for the value).
- `stat -c` in step 4 is `stat -f '%Lp %N'` on macOS.
