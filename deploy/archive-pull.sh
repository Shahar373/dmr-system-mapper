#!/usr/bin/env bash
# Run on the ARCHIVE Pi (not on the mapper). Pulls the mapper's nightly snapshots over SSH with a
# read-only key and keeps a history:
#
#   archive-pull.sh SOURCE DEST [DAILY [WEEKLY]]
#     SOURCE  user@mapper-host:            (with an rrsync-restricted key, see DEPLOY.md)
#             or user@mapper-host:/path/to/dmr-system-mapper/var/snapshots/
#     DEST    local folder, e.g. /srv/dmr-mapper
#     DAILY   newest snapshots kept (default 14)
#     WEEKLY  older Sunday snapshots kept on top of those (default 8)
#
# Safe to run often (cron every few hours): the mapper Pi is often off or out of reach in the field;
# rsync copies only what changed. Each pulled file is integrity-checked; a bad one is set aside.
set -euo pipefail
SRC="${1:?source, e.g. <user>@<mapper-host>:}"
DEST="${2:?destination folder}"
DAILY="${3:-14}"
WEEKLY="${4:-8}"
KEY="${ARCHIVE_KEY:-$HOME/.ssh/dmr_mapper_archive}"

mkdir -p "$DEST"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=20"
[ -f "$KEY" ] && SSH="$SSH -i $KEY"
if ! rsync -a --timeout=120 -e "$SSH" --include='mapper-*.db' --exclude='*' "$SRC" "$DEST/"; then
  echo "$(date -Is) pull failed (mapper off or unreachable?)" >&2
  exit 1
fi

for f in "$DEST"/mapper-*.db; do
  [ -e "$f" ] || continue
  if command -v python3 >/dev/null; then
    r="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True).execute("PRAGMA quick_check").fetchone()[0])' "$f" 2>&1 || true)"
    if [ "$r" != ok ]; then
      echo "$(date -Is) $f failed its check ($r); moved aside" >&2
      mv "$f" "$f.bad"
    fi
  fi
done

# keep the newest $DAILY, plus the newest $WEEKLY Sunday snapshots among the older ones
n=0 w=0
for f in $(ls -1 "$DEST"/mapper-*.db 2>/dev/null | sort -r); do
  n=$((n + 1))
  [ "$n" -le "$DAILY" ] && continue
  d="$(basename "$f" .db)"; d="${d#mapper-}"
  if [ "$(date -d "$d" +%u 2>/dev/null || echo 0)" = 7 ] && [ "$w" -lt "$WEEKLY" ]; then
    w=$((w + 1))
    continue
  fi
  rm -f "$f"
done
echo "$(date -Is) ok: $(ls -1 "$DEST"/mapper-*.db 2>/dev/null | wc -l) snapshots in $DEST"
