#!/usr/bin/env bash
# agent-memory installer. Idempotent and non-interactive: run it again to update the code; it never deletes data,
# never overwrites an existing config or token, and never prints the token.
#
# Usage: ./install.sh [options]          (every option also has an environment variable)
#   --systemd user|system|none   AM_SYSTEMD    default: user (systemctl --user); system needs sudo
#   --prefix DIR                 AM_PREFIX     base dir. default: ~/.local/share/agent-memory (system: /opt/agent-memory)
#   --config FILE                AM_CONFIG     default: ~/.config/agent-memory/config.toml (system: <prefix>/config.toml)
#   --host IP                    AM_HOST       default: 127.0.0.1 (private-network IPs allowed; never 0.0.0.0)
#   --port N                     AM_PORT       default: 8765
#   --timezone TZ                AM_TIMEZONE   default: system timezone, else UTC
#   --language en|es             AM_LANGUAGE   default: en
#   --user-name NAME             AM_USER_NAME  default: "the user"
#   --claude-projects DIR|none   AM_CLAUDE_PROJECTS  default: ~/.claude/projects
#   --openclaw DIR|none          AM_OPENCLAW   default: ~/.openclaw/agents if it exists, else none
#   --python BIN                 AM_PYTHON     Python >= 3.11 (default: auto; uses uv if available)
#   --no-model                   AM_NO_MODEL=1 skip the embedding model download (~470 MB); the server needs it
#   --no-start                   AM_NO_START=1 install the units but do not enable/start them
#   --no-hardening               AM_NO_HARDENING=1 skip the systemd sandboxing drop-ins (if units fail 226/NAMESPACE)
#   --register-claude            AM_REGISTER_CLAUDE=1  register the MCP server in Claude Code (user scope)
#   --hooks                      AM_HOOKS=1    install the Claude Code hooks (scripts/install-hooks.sh)
set -euo pipefail
umask 077

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD="${AM_SYSTEMD:-user}"
PREFIX="${AM_PREFIX:-}"
CONFIG="${AM_CONFIG:-}"
HOST="${AM_HOST:-127.0.0.1}"
PORT="${AM_PORT:-8765}"
TIMEZONE="${AM_TIMEZONE:-}"
LANGUAGE="${AM_LANGUAGE:-en}"
USER_NAME="${AM_USER_NAME:-the user}"
CLAUDE_PROJECTS="${AM_CLAUDE_PROJECTS:-$HOME/.claude/projects}"
OPENCLAW="${AM_OPENCLAW:-}"
PYTHON="${AM_PYTHON:-}"
NO_MODEL="${AM_NO_MODEL:-0}"
NO_START="${AM_NO_START:-0}"
NO_HARDENING="${AM_NO_HARDENING:-0}"
REGISTER_CLAUDE="${AM_REGISTER_CLAUDE:-0}"
HOOKS="${AM_HOOKS:-0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --systemd) SYSTEMD="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --language) LANGUAGE="$2"; shift 2 ;;
    --user-name) USER_NAME="$2"; shift 2 ;;
    --claude-projects) CLAUDE_PROJECTS="$2"; shift 2 ;;
    --openclaw) OPENCLAW="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --no-model) NO_MODEL=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --no-hardening) NO_HARDENING=1; shift ;;
    --register-claude) REGISTER_CLAUDE=1; shift ;;
    --hooks) HOOKS=1; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown option $1" >&2; exit 2 ;;
  esac
done

step() { echo; echo "== $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

case "$SYSTEMD" in user|system|none) ;; *) die "--systemd must be user, system or none" ;; esac
case "$LANGUAGE" in en|es) ;; *) die "--language must be en or es" ;; esac
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be a number"
if [ -z "$PREFIX" ]; then
  if [ "$SYSTEMD" = system ]; then PREFIX=/opt/agent-memory; else PREFIX="$HOME/.local/share/agent-memory"; fi
fi
if [ -z "$CONFIG" ]; then
  if [ "$SYSTEMD" = system ]; then CONFIG="$PREFIX/config.toml"; else CONFIG="$HOME/.config/agent-memory/config.toml"; fi
fi
if [ -z "$TIMEZONE" ]; then
  TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$TIMEZONE" ] || TIMEZONE="$(cat /etc/timezone 2>/dev/null || true)"
  [ -n "$TIMEZONE" ] || TIMEZONE=UTC
fi
if [ -z "$OPENCLAW" ]; then
  if [ -d "$HOME/.openclaw/agents" ]; then OPENCLAW="$HOME/.openclaw/agents"; else OPENCLAW=none; fi
fi

step "prerequisites"
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
USE_UV=0
if [ -z "$PYTHON" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$(command -v "$c")"; then PYTHON="$(command -v "$c")"; break; fi
  done
fi
if [ -z "$PYTHON" ]; then
  if command -v uv >/dev/null 2>&1; then
    USE_UV=1
    echo "no Python >= 3.11 found: uv will provide 3.12"
  else
    die "Python >= 3.11 not found and uv not installed (install python3.11+ or https://docs.astral.sh/uv/)"
  fi
else
  py_ok "$PYTHON" || die "$PYTHON is older than 3.11"
  echo "python: $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"
fi
if [ "$SYSTEMD" = user ]; then command -v systemctl >/dev/null || die "systemctl not found: use --systemd none"; fi
if [ "$SYSTEMD" = system ]; then command -v sudo >/dev/null || die "--systemd system needs sudo"; fi

step "directories ($PREFIX)"
if [ "$SYSTEMD" = system ] && [ ! -w "$(dirname "$PREFIX")" ]; then
  sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 700 "$PREFIX"
fi
install -d -m 700 "$PREFIX" "$PREFIX/data" "$PREFIX/config" "$PREFIX/work" "$PREFIX/inbox" "$PREFIX/models"
install -d -m 700 "$(dirname "$CONFIG")"

step "Python environment and package"
VENV="$PREFIX/venv"
if [ ! -x "$VENV/bin/python" ]; then
  if [ "$USE_UV" = 1 ]; then
    uv venv --quiet --python 3.12 "$VENV"
  else
    "$PYTHON" -m venv "$VENV"
  fi
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$VENV/bin/python" --reinstall-package agent-memory "$SRC"
else
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$SRC"
  "$VENV/bin/python" -m pip install --quiet "$SRC"
fi
"$VENV/bin/python" - <<'EOF'
import sqlite3, sqlite_vec
c = sqlite3.connect(":memory:")
c.enable_load_extension(True); sqlite_vec.load(c)
c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
print("sqlite", sqlite3.sqlite_version, "with FTS5 and sqlite-vec", c.execute("select vec_version()").fetchone()[0])
EOF
BIN="$VENV/bin"
AM="$BIN/agent-memory"

step "configuration ($CONFIG)"
CLAUDE_BIN="$(command -v claude || true)"
[ -n "$CLAUDE_BIN" ] || CLAUDE_BIN="claude"
WORKDIR_PROJECT="$(printf '%s' "$PREFIX/work" | sed 's/[^A-Za-z0-9-]/-/g')"
ARGS=("base_dir=$PREFIX" "timezone=$TIMEZONE" "language=$LANGUAGE" "user_name=$USER_NAME" "host=$HOST"
      "port=$PORT" "claude_bin=$CLAUDE_BIN" "exclude_projects=[\"$WORKDIR_PROJECT\"]")
if [ "$CLAUDE_PROJECTS" != none ]; then ARGS+=(--claude-source "$CLAUDE_PROJECTS:$(hostname -s 2>/dev/null || echo local)"); fi
ARGS+=(--claude-source "$PREFIX/inbox:remote")
if [ "$OPENCLAW" != none ]; then ARGS+=(--openclaw-source "$OPENCLAW"); fi
"$VENV/bin/python" "$SRC/scripts/render_config.py" "$SRC/config.example.toml" "$CONFIG" "${ARGS[@]}"
chmod 600 "$CONFIG"
"$AM" --config "$CONFIG" doctor >/dev/null || die "the config does not load: check $CONFIG"

step "MCP Bearer token (600, never shown)"
"$AM" --config "$CONFIG" init-token

step "embedding model"
if [ "$NO_MODEL" = 1 ]; then
  echo "skipped (--no-model): run '$AM --config $CONFIG download-model' before starting the server"
else
  "$AM" --config "$CONFIG" download-model
fi

step "INDEX.md"
# The first ingestion is left to the timer (every 15 min) or to a manual `agent-memory ingest`: with a long
# transcript history it can take a while (embeddings run on the CPU).
"$AM" --config "$CONFIG" index

render_unit() {  # template destination (values passed as arguments: safe for any path without whitespace)
  "$VENV/bin/python" "$SRC/scripts/render_template.py" "$1" "$2" "CONFIG=$CONFIG" "BIN=$BIN" "BASE=$PREFIX" \
    "WANTEDBY=$WANTEDBY" "PATH=$BIN:$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin" "USER=$(id -un)" \
    "GROUP=$(id -gn)" "HOME=$HOME"
}
UNITS=(agent-memory.service agent-memory-ingest.service agent-memory-ingest.timer
       agent-memory-consolidate.service agent-memory-consolidate.timer)
TMPU="$(mktemp -d)"
cleanup() { [ -n "${TMPU:-}" ] && [ -d "$TMPU" ] && rm -r -- "$TMPU"; return 0; }
trap cleanup EXIT
install -d -m 700 "$PREFIX/work/cache" "$PREFIX/models/hf-home"
# Renders units + drop-ins into $TMPU/<unit> and $TMPU/<unit>.d/
render_all() {
  for u in "${UNITS[@]}"; do render_unit "$SRC/systemd/$u" "$TMPU/$u"; done
  for u in agent-memory.service agent-memory-ingest.service agent-memory-consolidate.service; do
    install -d -m 755 "$TMPU/$u.d"
    if [ "$SYSTEMD" = system ]; then render_unit "$SRC/systemd/system-user.conf" "$TMPU/$u.d/10-user.conf"; fi
  done
  if [ "$NO_HARDENING" != 1 ]; then
    for u in agent-memory.service agent-memory-ingest.service; do
      render_unit "$SRC/systemd/hardening.conf" "$TMPU/$u.d/20-hardening.conf"
    done
    render_unit "$SRC/systemd/hardening-consolidate.conf" "$TMPU/agent-memory-consolidate.service.d/20-hardening.conf"
  fi
}
# Copies the rendered tree into $1 with the given command prefix ("" or "sudo"); with --no-hardening an existing
# hardening drop-in is moved aside (renamed .disabled), never deleted.
install_units() {
  local dest="$1" run="${2:-}"
  for u in "${UNITS[@]}"; do $run install -m 644 "$TMPU/$u" "$dest/$u"; done
  for u in agent-memory.service agent-memory-ingest.service agent-memory-consolidate.service; do
    $run install -d -m 755 "$dest/$u.d"
    for f in "$TMPU/$u.d"/*.conf; do [ -e "$f" ] && $run install -m 644 "$f" "$dest/$u.d/"; done
    if [ "$NO_HARDENING" = 1 ] && [ -e "$dest/$u.d/20-hardening.conf" ]; then
      $run mv "$dest/$u.d/20-hardening.conf" "$dest/$u.d/20-hardening.conf.disabled"
    fi
  done
}
case "$SYSTEMD" in
  user)
    step "systemd user units"
    UDIR="$HOME/.config/systemd/user"; WANTEDBY=default.target
    install -d -m 700 "$UDIR"
    render_all
    install_units "$UDIR"
    systemctl --user daemon-reload
    if [ "$NO_START" != 1 ]; then
      systemctl --user enable --now agent-memory.service agent-memory-ingest.timer agent-memory-consolidate.timer
      systemctl --user restart agent-memory.service
    fi
    if ! loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q yes; then
      echo "NOTE: lingering is off: the services stop when you log out. Enable with: loginctl enable-linger $(id -un)"
    fi
    ;;
  system)
    step "systemd system units (sudo)"
    WANTEDBY=multi-user.target
    render_all
    install_units /etc/systemd/system sudo
    sudo systemctl daemon-reload
    if [ "$NO_START" != 1 ]; then
      sudo systemctl enable --now agent-memory.service agent-memory-ingest.timer agent-memory-consolidate.timer
      sudo systemctl restart agent-memory.service
    fi
    ;;
  none)
    step "systemd skipped (--systemd none)"
    echo "start the server yourself: AGENT_MEMORY_CONFIG=$CONFIG $AM serve"
    ;;
esac

if [ "$REGISTER_CLAUDE" = 1 ]; then
  step "Claude Code registration"
  "$SRC/scripts/register-claude-code.sh" --config "$CONFIG" --bin "$AM"
fi
if [ "$HOOKS" = 1 ]; then
  step "Claude Code hooks"
  "$SRC/scripts/install-hooks.sh"
fi

step "done"
TOKEN_FILE="$("$VENV/bin/python" -c 'import sys; from agent_memory import config; print(config.load(sys.argv[1]).mcp_token_file)' "$CONFIG")"
echo "config:     $CONFIG"
echo "database:   $PREFIX/data/memory.db"
echo "token file: $TOKEN_FILE (600)"
echo "MCP URL:    http://$HOST:$PORT/mcp   (header: Authorization: Bearer <contents of the token file>)"
echo "CLI:        $AM --config $CONFIG {stats|search|day|facts|doctor}"
echo "smoke test: $SRC/scripts/smoke.sh --config $CONFIG --url http://$HOST:$PORT"
