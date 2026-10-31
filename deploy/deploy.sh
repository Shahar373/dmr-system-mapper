#!/usr/bin/env bash
# Install or update dmr-system-mapper on the Pi. Run it from the clone, as the user that owns it:
#
#   deploy/deploy.sh             pull, install, (re)start the service, enable the nightly timer,
#                                and the MCP server for Claude Code when mcp.enabled is true
#   deploy/deploy.sh --force     also restart while a session is running (the session is stopped)
#   deploy/deploy.sh --no-pull   install what is checked out now
#   deploy/deploy.sh --dry-run   print what would be done; nothing is changed
#
# It asks for sudo only to install the systemd unit files and to start/stop the service.
# Nothing machine-specific is stored in the repository: user, group and paths are taken from
# this machine, the snapshot time and the MCP switch from the config (archive.snapshot_time, mcp.*).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=("$@")
SERVICE=dmr-mapper
MCP_SERVICE=dmr-mapper-mcp
DRY=0 FORCE=0 PULL=1 RENDER_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --no-pull) PULL=0 ;;
    --render-dir) RENDER_DIR="$2"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
run()  { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
if [ "$USER_NAME" = root ]; then
  if [ "$DRY" != 1 ]; then
    echo "run deploy.sh as the user who owns $REPO, not as root (it uses sudo where needed)" >&2
    exit 1
  fi
  warn "running as root: allowed for --dry-run only"
fi
cd "$REPO"

# 1. update the checkout ------------------------------------------------------------------------
if [ "$PULL" = 1 ]; then
  say "updating $REPO from git"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "local changes in tracked files; commit or stash them first (or use --no-pull):" >&2
    git status --short --untracked-files=no >&2
    exit 1
  fi
  before="$(git rev-parse HEAD:deploy/deploy.sh 2>/dev/null || true)"
  run git pull --ff-only
  if [ "$DRY" != 1 ] && [ "$(git rev-parse HEAD:deploy/deploy.sh 2>/dev/null || true)" != "$before" ]; then
    # bash goes on reading the old copy of this file: the pulled one may install more (e.g. the MCP extra)
    say "deploy.sh itself changed: running the new version"
    exec bash "$REPO/deploy/deploy.sh" --no-pull "${ARGS[@]}"
  fi
fi
echo "    at $(git log --oneline -1)"

# 2. virtual environment (system site-packages: SoapySDR's Python bindings come from the OS) ---
PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
  say "creating .venv"
  run python3 -m venv --system-site-packages "$REPO/.venv"
fi
say "installing the package"
run "$REPO/.venv/bin/pip" install --quiet --disable-pip-version-check -e "$REPO[collector,tui]"
[ -x "$PY" ] || PY=python3   # dry run on a machine without the venv

# 3. `mapper` from any folder --------------------------------------------------------------------
say "linking ~/.local/bin/mapper"
run mkdir -p "$HOME/.local/bin"
run ln -sfn "$REPO/.venv/bin/mapper" "$HOME/.local/bin/mapper"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "~/.local/bin is not on PATH in this shell; it is after the next login (Debian's ~/.profile adds it)" ;;
esac

# 4. render the systemd units --------------------------------------------------------------------
read -r SNAP_TIME MCP_ON MCP_URL <<<"$("$PY" - "$REPO" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from dmr_mapper.config import load_config
cfg = load_config()
t = str((cfg.get("archive") or {}).get("snapshot_time", "03:30"))
h, m = (int(x) for x in t.split(":"))
mcp = cfg.get("mcp") or {}
print(f"{h:02d}:{m:02d}", "yes" if mcp.get("enabled") else "no", f"http://{mcp.get('bind', '127.0.0.1')}:{mcp.get('port', 8765)}/mcp")
PYEOF
)"
if [ "$MCP_ON" = yes ]; then
  say "installing the MCP server's dependencies (mcp.enabled: true)"
  run "$REPO/.venv/bin/pip" install --quiet --disable-pip-version-check -e "$REPO[collector,tui,mcp]"
  case "$MCP_URL" in
    *//127.*|*//localhost*) warn "mcp.bind is a loopback address: the laptop cannot reach $MCP_URL; set the Pi's Tailscale address in config.local.yaml" ;;
  esac
fi
UNITS="dmr-mapper.service dmr-mapper-snapshot.service dmr-mapper-snapshot.timer"
[ "$MCP_ON" = yes ] && UNITS="$UNITS $MCP_SERVICE.service"
OUT="${RENDER_DIR:-$(mktemp -d)}"
mkdir -p "$OUT"
for f in $UNITS; do
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@GROUP@|$GROUP_NAME|g" -e "s|@REPO@|$REPO|g" -e "s|@TIME@|$SNAP_TIME|g" \
    "$REPO/deploy/$f.in" > "$OUT/$f"
  if grep -q '@[A-Z]*@' "$OUT/$f"; then echo "unfilled placeholder in $f" >&2; exit 1; fi
done
echo "    units rendered in $OUT (snapshot at $SNAP_TIME, MCP server: $MCP_ON)"

# 5. is a daemon running that systemd does not own (started by hand)? -----------------------------
STATE="$("$PY" - "$REPO" <<'PYEOF' || true
import sys
sys.path.insert(0, sys.argv[1])
from dmr_mapper.config import load_config, resolve_path
from dmr_mapper.collector import state as st
from dmr_mapper.db import Store
p = resolve_path(load_config(), "db")
if not p.exists():
    print("none 0 idle")
    raise SystemExit
with Store(p) as s:
    d = st.read_status(s)
    print(("alive" if d.alive else "none"), d.pid or 0, d.mode)
PYEOF
)"
read -r ALIVE DPID DMODE <<<"${STATE:-none 0 idle}"
MAINPID="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
if [ "$ALIVE" = alive ] && [ "$DPID" != "${MAINPID:-0}" ]; then
  echo "a daemon started by hand is running (pid $DPID). Stop it (Ctrl-C in its window, or kill $DPID), then run deploy.sh again." >&2
  [ "$DRY" = 1 ] || exit 1
fi

# 6. install units and (re)start ------------------------------------------------------------------
changed=0
for f in $UNITS; do
  if ! cmp -s "$OUT/$f" "/etc/systemd/system/$f"; then
    say "installing /etc/systemd/system/$f"
    run sudo install -m 0644 "$OUT/$f" "/etc/systemd/system/$f"
    changed=1
  fi
done
[ "$changed" = 1 ] && run sudo systemctl daemon-reload

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  if [ "$ALIVE" = alive ] && [ "$DMODE" != idle ] && [ "$FORCE" != 1 ]; then
    warn "a $DMODE session is running: the service was NOT restarted, so the new code is not live yet."
    warn "restart when it ends:  sudo systemctl restart $SERVICE   (or deploy.sh --force to stop the session now)"
  else
    say "restarting $SERVICE"
    run sudo systemctl restart "$SERVICE"
  fi
else
  say "enabling and starting $SERVICE"
  run sudo systemctl enable --now "$SERVICE"
fi
say "enabling the nightly snapshot timer"
run sudo systemctl enable --now dmr-mapper-snapshot.timer

# the MCP server never holds the receiver: restarting it is always safe
if [ "$MCP_ON" = yes ]; then
  say "(re)starting $MCP_SERVICE on $MCP_URL"
  run sudo systemctl enable "$MCP_SERVICE"
  run sudo systemctl restart "$MCP_SERVICE"
elif systemctl is-enabled --quiet "$MCP_SERVICE" 2>/dev/null || systemctl is-active --quiet "$MCP_SERVICE" 2>/dev/null; then
  say "mcp.enabled is false: stopping and disabling $MCP_SERVICE"
  run sudo systemctl disable --now "$MCP_SERVICE"
fi

# 7. summary --------------------------------------------------------------------------------------
if [ "$DRY" != 1 ]; then
  sleep 2
  systemctl --no-pager --lines=5 status "$SERVICE" || true
  echo
  "$REPO/.venv/bin/mapper" doctor || true
fi
cat <<MSG

done. The console:   mapper tui
     daemon log:     journalctl -u $SERVICE -f
     next snapshot:  systemctl list-timers dmr-mapper-snapshot.timer
MSG
[ "$MCP_ON" = yes ] && echo "     MCP server:     $MCP_URL   (journalctl -u $MCP_SERVICE -f)"
[ -z "$RENDER_DIR" ] && [ "$DRY" != 1 ] && rm -rf "$OUT"
exit 0
