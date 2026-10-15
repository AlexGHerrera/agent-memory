# AGENTS.md — instructions for an AI agent installing or using agent-memory

You are installing (or connecting to) a long-term memory service for yourself or for another agent. Follow
[INSTALL.md](INSTALL.md) step by step, literally. This file adds the rules that apply while you do it.

## Rules

1. **Run the steps in order and compare each output with the "Expected" line.** If it does not match, stop, read
   the Troubleshooting table in INSTALL.md, apply only the fix listed there and rerun the same check. If the same
   check fails twice, stop and report to the user: the command, the output, what you tried.
2. **Never print, echo, paste or log the token.** Refer to it only by its file path. Do not put it in a
   repository, a shared config, a chat message or a note. Commands that need it read it from the file
   (`$(cat FILE)`) inside the same command.
3. **Bind address:** keep `host = "127.0.0.1"` unless the user asks for access from other machines; then use a
   the server's address on an encrypted overlay network (WireGuard, Tailscale) or a TLS proxy (INSTALL.md
   step 10). The server is plain HTTP: never a plain LAN address, never `0.0.0.0`, never a public IP.
4. **Do not delete data.** `install.sh` never deletes anything; neither should you. Uninstalling keeps the data
   unless the user explicitly asks to delete it.
5. **Ask the user before:** running with `--systemd system` (needs sudo), enabling lingering if it requires
   admin rights, editing `~/.claude/settings.json` by hand, exposing the port to a network, and running the
   nightly consolidation by hand (it uses the user's LLM quota).
6. **Do not edit the code to make a check pass.** Report the failure instead.
7. Defaults are fine for almost everyone. The only values you should ask the user for are the timezone, the
   language (`en`/`es`) and, if wanted, the name the summaries should use for them (`--user-name`).

## Minimal path (Linux, same machine as the agent)

```bash
git clone https://github.com/AlexGHerrera/agent-memory.git ~/agent-memory && cd ~/agent-memory
./install.sh --timezone <IANA/Zone> --language en
~/agent-memory/scripts/smoke.sh --config ~/.config/agent-memory/config.toml --url http://127.0.0.1:8765
```
The last command must end with `12/12 checks passed`. Then connect yourself:

- Claude Code: `scripts/register-claude-code.sh` and `scripts/install-hooks.sh` (INSTALL.md step 8).
- OpenClaw: [docs/OPENCLAW.md](docs/OPENCLAW.md) (MCP server entry with the Bearer header + `[[openclaw_sources]]`).
- Anything else: MCP streamable HTTP at `http://127.0.0.1:8765/mcp`, header `Authorization: Bearer <token>`;
  transcripts in the format of [docs/INGESTION.md](docs/INGESTION.md).

## Using the memory once connected

- "What did we do yesterday?" → `day('yesterday')`, then `session(id)` only for the detail you need.
- Something specific → `search(query, since?, until?, project?)`.
- Questions about the user's current situation (job, family, health, decisions, projects) → `facts(...)` first;
  each fact has a source id you can open with `session(id)`.
- Save with `note(text, project?)`: decisions, commitments you make ("I'll remind you…"), new facts about the
  user, and a closing summary of long tasks (what was done, what is left).
- Before saying you do not know something about the user's past, search. If it is not there, say so; do not
  invent it.
- Keep what you pull into your context small: prefer `day` → one `session`, or `search` with filters.
- Facts in sensitive categories wait for approval: `agent-memory proposals pending`; ask the user and record the
  answer with `agent-memory proposals mark <id> applied|rejected`.
- Never store secrets in notes (tokens, passwords, card data, codes). The filter is a safety net, not permission.
