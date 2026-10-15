# Ingestion: input formats and how to feed any agent

agent-memory reads three kinds of sources, all configured in `config.toml` and read by `agent-memory ingest`
(the `agent-memory-ingest.timer` runs it every 15 minutes):

| Source | Config | Format |
|---|---|---|
| Claude Code transcripts, or **any agent through the generic format below** | `[[claude_sources]]` | JSONL, one record per line |
| OpenClaw transcripts | `[[openclaw_sources]]` | OpenClaw "pi" JSONL (see [OPENCLAW.md](OPENCLAW.md)) |
| Markdown vault (read-only) | `vault_path` | `*.md` files |

Plus notes saved with the `note` MCP tool, which go straight to the database.

## The generic format (Claude Code JSONL subset)

To feed transcripts from an agent that is not Claude Code, write one `.jsonl` file per conversation (or per day)
into a folder and list the folder as a source:

```toml
[[claude_sources]]
path = "~/my-agent/memory-export"   # any depth below this folder; every *.jsonl file is read
machine = "my-agent"                 # free label stored with every message
```

Each line is one JSON object. Only these fields are used:

| Field | Required | Meaning |
|---|---|---|
| `type` | yes | `"user"` or `"assistant"`. Any other value is ignored. |
| `uuid` | recommended | Unique, stable id of the message. It makes ingestion idempotent: re-reading the file never duplicates. Without it a hash of the whole record is used (then do not change records after writing them). |
| `sessionId` | recommended | Conversation id. Default: the file name without `.jsonl`. Use something stable, e.g. `chat-2026-10-03` or your agent's session uuid. |
| `timestamp` | recommended | ISO 8601 with timezone, e.g. `2026-10-03T08:00:00Z`. Default: the file mtime. |
| `message.content` | yes | A string, or a list of blocks: `{"type": "text", "text": "..."}`, `{"type": "tool_use", "name": "...", "input": {...}}`, `{"type": "tool_result", "content": "..."}`, `{"type": "image"}`. Text blocks are what gets summarized and embedded; tool blocks are stored abbreviated and ranked lower in search. |
| `message.role` | no | Ignored (the role comes from `type`). |
| `cwd` | no | Working directory; its basename becomes the `project`. Default project: the first folder under the source path. |
| `isMeta` | no | `true` = metadata line, its text is skipped. |
| `isSidechain` + `agentId` | no | Marks a sub-agent's message: stored as session `<sessionId>/agent-<agentId>` linked to its parent. |
| `isCompactSummary` | no | `true` = a summary produced by context compaction (stored with role `compact_summary`). |

Minimal example (two lines of `~/my-agent/memory-export/support/chat-2026-10-03.jsonl`):

```json
{"type":"user","uuid":"m-0001","sessionId":"chat-2026-10-03","timestamp":"2026-10-03T08:00:00Z","cwd":"/srv/agents/support","message":{"content":"Remind me to renew the domain on Friday"}}
{"type":"assistant","uuid":"m-0002","sessionId":"chat-2026-10-03","timestamp":"2026-10-03T08:00:04Z","message":{"content":[{"type":"text","text":"Noted: renew the domain on Friday."}]}}
```

### Rules for adapters

1. **Append only.** Ingestion reads each file incrementally from the last offset; only complete lines (ending in
   `\n`) are read, so a half-written last line waits for the next run. If a file shrinks it is re-read from the
   start (the `uuid`s prevent duplicates).
2. **Write the file atomically or append whole lines.** Do not rewrite earlier lines with different content.
3. **One message per line**, UTF-8, no pretty printing.
4. **Do not include secrets on purpose.** The secrets filter runs on everything, but it is a safety net, not a
   guarantee (see the limits in `agent_memory/secrets_filter.py`). Sessions that must never be stored can be
   listed in `exclude_sessions`.
5. The adapter does not need to run on the server: write the files on the agent's machine and upload them with
   `PUT /inbox/<folder>/<session>.jsonl` (below), or sync the folder with any tool.

Summaries and facts are produced for the sources listed in `consolidate_sources` (default `["claude",
"openclaw"]`); everything written in this generic format is stored with source `claude`, so it is included.

## Uploading transcripts from another machine (`PUT /inbox`)

```
curl -sS -X PUT --data-binary @chat-2026-10-03.jsonl \
  -H "Authorization: Bearer $(cat ~/.config/agent-memory/mcp-token)" \
  -H "Content-Type: application/x-ndjson" \
  http://SERVER:8765/inbox/my-agent/chat-2026-10-03.jsonl
```

- Path: 1 to 4 segments of `[A-Za-z0-9._-]` (no leading dot, no `..`), ending in `.jsonl`; body ≤ `upload_max_mb`.
- Reply: `{"ok": true, "bytes": N, "result": "...", "path": "..."}` with `result` one of `new`, `same` (identical,
  untouched), `contained` (an older prefix, untouched), `extended` (the stored file was a prefix: replaced) or
  `copy` (different content: stored next to it with a suffix; the original is never overwritten).
- The upload lands in `inbox_dir`; `install.sh` lists that folder as a `[[claude_sources]]` entry (machine
  `remote`), so the next ingestion picks it up.

## What is never ingested

- Projects listed in `exclude_projects` (folder names under a source) and sessions in `exclude_sessions` (with
  their sub-agents).
- In the vault: `vault_exclude_dirs`, symlinks, files outside the vault and files over `vault_max_file_kb`.
- Empty texts. Messages longer than `max_message_chars` are truncated.
