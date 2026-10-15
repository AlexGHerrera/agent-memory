#!/usr/bin/env bash
# agent-memory smoke test: calls every MCP tool and the HTTP routes, prints PASS/FAIL per check.
#
# Two modes:
#   scripts/smoke.sh [--temp] [--port 18765] [--models-dir DIR] [--bin PATH] [--keep]
#       Throwaway instance: temp base dir, temp config, fake Claude Code transcript, ingestion, server on
#       127.0.0.1:<port> (default 18765), checks, then stops the server and deletes the temp dir (unless --keep).
#       --models-dir reuses an already downloaded embedding model (otherwise ~470 MB are downloaded into the temp dir).
#   scripts/smoke.sh --config FILE --url http://127.0.0.1:8765 [--bin PATH]
#       Checks an already running server with the token of that config. Saves one note "agent-memory smoke test".
# Exit code: 0 if every check passed, 1 otherwise.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG=""; URL=""; PORT="${AM_SMOKE_PORT:-18765}"; MODELS=""; KEEP=0
AM="$(command -v agent-memory || echo "$HOME/.local/share/agent-memory/venv/bin/agent-memory")"
while [ $# -gt 0 ]; do
  case "$1" in
    --temp) shift ;;
    --config) CONFIG="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --models-dir) MODELS="$2"; shift 2 ;;
    --bin) AM="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "ERROR: unknown option $1" >&2; exit 2 ;;
  esac
done
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "ERROR: --port must be a number" >&2; exit 2; }
[ -x "$AM" ] || { echo "ERROR: agent-memory not found ($AM): pass --bin" >&2; exit 2; }
PY="$(dirname "$AM")/python"

if [ -n "$CONFIG" ]; then
  [ -n "$URL" ] || { echo "ERROR: --config needs --url of the running server" >&2; exit 2; }
  TOKEN_FILE="$("$PY" -c 'import sys; from agent_memory import config; print(config.load(sys.argv[1]).mcp_token_file)' "$CONFIG")"
  exec "$PY" "$HERE/mcp_check.py" "$URL" "$TOKEN_FILE"
fi

# ---------------------------------------------------------------- throwaway instance
T="$(mktemp -d "${TMPDIR:-/tmp}/agent-memory-smoke.XXXXXX")"
SERVER_PID=""
finish() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  if [ "$KEEP" = 1 ]; then echo "kept: $T"; elif [ -d "$T" ]; then rm -r -- "$T"; fi
}
trap finish EXIT
if "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT)) == 0 else 1)"; then
  echo "ERROR: port $PORT is already in use: pass --port" >&2; exit 2
fi
[ -n "$MODELS" ] || MODELS="$T/models"
SID="5e5e5e5e-0000-4000-8000-000000000001"
mkdir -p "$T/projects/-home-demo-smoke-project"
cat > "$T/projects/-home-demo-smoke-project/$SID.jsonl" <<EOF
{"type":"user","uuid":"smoke-u1","sessionId":"$SID","timestamp":"2026-01-02T10:00:00Z","cwd":"/home/demo/smoke-project","message":{"role":"user","content":"Please configure the nightly backup of the photo archive"}}
{"type":"assistant","uuid":"smoke-a1","sessionId":"$SID","timestamp":"2026-01-02T10:00:05Z","cwd":"/home/demo/smoke-project","message":{"role":"assistant","content":[{"type":"text","text":"Done: the photo archive backup runs every night at 02:00."}]}}
EOF
cat > "$T/config.toml" <<EOF
base_dir = "$T/base"
models_dir = "$MODELS"
host = "127.0.0.1"
port = $PORT
timezone = "UTC"

[[claude_sources]]
path = "$T/projects"
machine = "smoke"
EOF
export AGENT_MEMORY_CONFIG="$T/config.toml"
echo "== temp instance in $T (port $PORT)"
"$AM" init-token >/dev/null
"$AM" download-model >/dev/null 2>&1 || { echo "FAIL embedding model download/load"; exit 1; }
"$AM" ingest 2>/dev/null | tail -1
"$AM" index >/dev/null 2>&1
HF_HUB_OFFLINE=1 "$AM" serve > "$T/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 120); do
  if "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT)) == 0 else 1)"; then
    break
  fi
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "FAIL server did not start:"; tail -20 "$T/server.log"; exit 1; }
  sleep 1
done
echo "== server up (pid $SERVER_PID)"
TOKEN_FILE="$("$PY" -c "from agent_memory import config; print(config.load().mcp_token_file)")"
set +e
"$PY" "$HERE/mcp_check.py" "http://127.0.0.1:$PORT" "$TOKEN_FILE" --expect-session "$SID" \
  --expect-text "photo archive backup"
RC=$?
set -e
if grep -q "0.0.0.0" "$T/server.log"; then echo "FAIL server log mentions 0.0.0.0"; RC=1; else echo "PASS bind: 127.0.0.1 only"; fi
exit $RC
