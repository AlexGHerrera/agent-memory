#!/usr/bin/env bash
# Registers (or re-registers) the `memory` MCP server in Claude Code (user scope).
#
# Default (recommended): a `headersHelper` entry. Claude Code runs hooks/claude-code/headers_helper.py (copied to
# ~/.claude/hooks/agent-memory/) on each connection; the helper reads the token file and prints the
# Authorization header. The token is never stored in ~/.claude.json and never appears on a command line.
# Verified with Claude Code 2.1.300; needs a Claude Code version that supports `headersHelper` for HTTP servers.
#
# Fallback (--header-mode, for older Claude Code): `claude mcp add --header "Authorization: Bearer <token>"`.
# Then the token is visible for a moment in the process list (argv of `claude`) to other users of this machine,
# and Claude Code stores it in plain text in ~/.claude.json (this script makes that file 600).
#
# Usage: scripts/register-claude-code.sh [--config FILE] [--bin PATH-TO-agent-memory] [--url http://HOST:PORT]
#                                        [--header-mode]
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/hooks/claude-code"
CONFIG="${AGENT_MEMORY_CONFIG:-$HOME/.config/agent-memory/config.toml}"
AM="$HOME/.local/share/agent-memory/venv/bin/agent-memory"
URL=""
MODE=helper
DEST="$HOME/.claude/hooks/agent-memory"
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --bin) AM="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --header-mode) MODE=header; shift ;;
    *) echo "ERROR: unknown option $1" >&2; exit 2 ;;
  esac
done
command -v claude >/dev/null || { echo "ERROR: claude (Claude Code CLI) not found in PATH" >&2; exit 1; }
PY="$(dirname "$AM")/python"
read -r HOST PORT TOKEN_FILE < <("$PY" -c '
import sys
from agent_memory import config
c = config.load(sys.argv[1]); print(c.host, c.port, c.mcp_token_file)' "$CONFIG")
[ -n "$URL" ] || URL="http://$HOST:$PORT"
[ -s "$TOKEN_FILE" ] || { echo "ERROR: token file missing: $TOKEN_FILE (run agent-memory init-token)" >&2; exit 1; }
claude mcp remove memory -s user >/dev/null 2>&1 || true

if [ "$MODE" = helper ]; then
  case "$DEST$TOKEN_FILE" in *[[:space:]]*)
    echo "ERROR: paths with spaces are not supported in headersHelper mode; use --header-mode" >&2; exit 1 ;;
  esac
  install -d -m 700 "$DEST"
  install -m 700 "$SRC/common.py" "$SRC/headers_helper.py" "$DEST/"
  PY3="$(command -v python3)"
  JSON="$("$PY" -c '
import json, sys
url, py, helper, tok = sys.argv[1:]
print(json.dumps({"type": "http", "url": url + "/mcp", "headersHelper": f"{py} {helper} --token-file {tok}"}))
' "$URL" "$PY3" "$DEST/headers_helper.py" "$TOKEN_FILE")"
  claude mcp add-json --scope user memory "$JSON" >/dev/null
else
  echo "WARNING: --header-mode: the token is briefly visible in the process list and stored in plain text in" \
       "~/.claude.json (600)." >&2
  TOKEN="$(cat "$TOKEN_FILE")"
  claude mcp add --transport http --scope user memory "$URL/mcp" --header "Authorization: Bearer $TOKEN" >/dev/null
  unset TOKEN
fi
[ -f "$HOME/.claude.json" ] && chmod 600 "$HOME/.claude.json"
claude mcp list 2>/dev/null | grep '^memory' | sed -E 's/Bearer [^ ]+/Bearer ****/' || true
echo "registered ($MODE): memory -> $URL/mcp"
