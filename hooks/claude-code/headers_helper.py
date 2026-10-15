#!/usr/bin/env python3
"""headersHelper for the `memory` MCP: prints {"Authorization": "Bearer <token>"} for Claude Code.

Claude Code runs this command each time it connects (`"headersHelper"` in the MCP server entry), so the token is
never stored in ~/.claude.json nor passed on a command line. Token lookup: `--token-file PATH` if given, else
AGENT_MEMORY_TOKEN, the token file (AGENT_MEMORY_TOKEN_FILE, ~/.config/agent-memory/mcp-token or
~/.local/share/agent-memory/config/mcp-token) or the macOS keychain (service "agent-memory").
Without a token it prints {} (the server answers 401).
Usage: headers_helper.py [--token-file PATH]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import common as mc  # noqa: E402


def token(argv) -> str:
    if "--token-file" in argv:
        i = argv.index("--token-file")
        if i + 1 < len(argv):
            return mc._token_from_file(argv[i + 1])
    return mc.load_token()


if __name__ == "__main__":
    try:
        t = token(sys.argv[1:])
    except Exception:  # noqa: BLE001
        t = ""
    print(json.dumps({"Authorization": f"Bearer {t}"} if t else {}))
