# Using agent-memory with OpenClaw

Two independent parts: OpenClaw **reads** the memory through MCP, and agent-memory **ingests** OpenClaw's
transcripts so they become searchable and get summarized every night.

## 1. Give the OpenClaw agent the five tools (MCP over HTTP)

The server speaks MCP streamable HTTP:

- URL: `http://127.0.0.1:8765/mcp` (or, if OpenClaw runs on another machine, the server's address on an
  encrypted overlay network such as WireGuard/Tailscale: the server is plain HTTP, never use a plain LAN address)
- Header on every request: `Authorization: Bearer <contents of the token file>`
  (token file: `agent-memory doctor` shows where it is; default `~/.local/share/agent-memory/config/mcp-token`)
- Server name: `memory`. Tools: `search`, `day`, `session`, `note`, `facts`.

Add it wherever your OpenClaw version configures remote MCP servers (an HTTP/streamable-HTTP server entry with a
URL and custom headers). The exact configuration key depends on the OpenClaw version; check its documentation for
"MCP servers". Keep the token out of any file that is committed or shared: if the configuration supports reading a
header from an environment variable or a file, use that.

Verify from the OpenClaw machine with the repo's stdlib checker (PASS/FAIL per tool; never prints the token;
saves one note "agent-memory smoke test"):

```
python3 scripts/mcp_check.py http://127.0.0.1:8765 /path/to/mcp-token
```

Suggested instructions for the agent's system prompt / AGENTS.md:

```
You have a long-term memory (MCP server "memory"):
- day('yesterday' | 'today' | 'YYYY-MM-DD') -> what happened that day, with session ids
- session(id) -> raw transcript of one session (paginated: id#2, id#3...)
- search(query, since?, until?, project?) -> hybrid search across everything
- facts(query?, category?, include_history?) -> what is true today about the user, with source ids
- note(text, project?) -> save decisions, commitments and the closing summary of long tasks
Before saying you do not know something about the user's past, search the memory. Never store secrets in notes.
```

## 2. Ingest OpenClaw transcripts

Enable the built-in importer in `config.toml` (install.sh adds it automatically when `~/.openclaw/agents` exists):

```toml
[[openclaw_sources]]
path = "~/.openclaw/agents"   # reads <path>/<agent>/sessions/*.jsonl
machine = "openclaw"
agents = []                   # empty = every agent folder; or e.g. ["main"]
primary_agents = ["main"]     # agents a human talks to directly
```

Then `agent-memory ingest` (or wait for the 15-minute timer). Check with `agent-memory stats` and
`agent-memory day today`.

What the importer keeps: what the human wrote (without the channel metadata envelopes OpenClaw adds, e.g.
"Conversation info (untrusted metadata)" blocks) and the assistant's replies in turns a human started, including
messages sent with the `message` tool. What it drops: cron, heartbeat, boot and sub-agent prompts, tool calls and
results, reasoning, images (kept as `[image]`), and messages that are mostly a secret. For agents not listed in
`primary_agents`, a "user" message without a sender envelope only counts as human if it is short (≤300 chars),
because their user is usually another agent.

Stored as: source `openclaw`, project `openclaw-<agent>`, sessions `oc:<agent>:<session id>`.

### If the transcript format differs

The importer understands OpenClaw's "pi" transcript records (`{"type": "session", ...}` and
`{"type": "message", "message": {"role", "content": [text|toolCall|thinking|image]}}`). If your OpenClaw
version stores transcripts differently (other folder, a database, another record shape), do not patch the
database: write a small adapter that converts them to the generic JSONL format in [INGESTION.md](INGESTION.md)
and list its output folder in `[[claude_sources]]`. The tests in `tests/test_openclaw.py` show the exact records
the importer was written against.

## 3. Optional: approvals of sensitive facts

The nightly consolidation extracts facts. Health, work, decision and personal facts taken from conversations wait
for approval. An OpenClaw agent (or a cron job) can show them to the user and record the answer:

```
agent-memory proposals pending                    # JSON: [{"id", "kind": "fact", "title", "section", ...}]
agent-memory proposals mark <id> applied          # user said yes
agent-memory proposals mark <id> rejected         # user said no
```
