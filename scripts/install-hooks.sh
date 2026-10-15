#!/usr/bin/env bash
# Installs the agent-memory Claude Code hooks for the current user. Idempotent.
#  1. Copies hooks/claude-code/*.py to ~/.claude/hooks/agent-memory/ (overwrites older copies of these files only).
#  2. Merges the three hook entries of hooks/claude-code/settings.example.json into ~/.claude/settings.json,
#     keeping every existing setting and hook. A backup settings.json.bak-<timestamp> is written first; nothing is
#     changed if the entries are already there.
# Usage: scripts/install-hooks.sh [--settings FILE] [--dest DIR]
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/hooks/claude-code"
SETTINGS="$HOME/.claude/settings.json"
DEST="$HOME/.claude/hooks/agent-memory"
while [ $# -gt 0 ]; do
  case "$1" in
    --settings) SETTINGS="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    *) echo "ERROR: unknown option $1" >&2; exit 2 ;;
  esac
done
command -v python3 >/dev/null || { echo "ERROR: python3 not found" >&2; exit 1; }
install -d -m 700 "$DEST"
install -m 700 "$SRC"/common.py "$SRC"/session_start.py "$SRC"/pre_compact.py "$SRC"/session_end.py \
  "$SRC"/headers_helper.py "$DEST/"
echo "hooks copied to $DEST"
python3 - "$SRC/settings.example.json" "$SETTINGS" "$DEST" <<'EOF'
import json, os, shutil, sys, time
example, settings, dest = sys.argv[1:]
want = json.load(open(example))["hooks"]
cur = {}
if os.path.exists(settings):
    with open(settings) as f:
        cur = json.load(f)
hooks = cur.setdefault("hooks", {})
changed = False
for event, groups in want.items():
    for g in groups:
        for h in g["hooks"]:
            h["command"] = h["command"].replace("$HOME/.claude/hooks/agent-memory", dest)
            present = any(x.get("command") == h["command"] for grp in hooks.get(event, []) for x in grp.get("hooks", []))
            if not present:
                hooks.setdefault(event, []).append({"hooks": [h]})
                changed = True
if changed:
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    if os.path.exists(settings):
        shutil.copy2(settings, f"{settings}.bak-{time.strftime('%Y%m%d%H%M%S')}")
    tmp = settings + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings)
    print(f"hooks added to {settings}")
else:
    print(f"hooks already present in {settings}")
EOF
