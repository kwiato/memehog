#!/usr/bin/env bash
# Nightly Memehog backup: consistent SQLite snapshot + the media library.
#
# The VPS holds the only copy of your collection — run this from cron, e.g.:
#   30 4 * * *  /srv/memehog-app/scripts/backup.sh /srv/memehog /srv/backups
#
# Then ship $BACKUP_DIR off-machine with restic/rclone (Hetzner Storage Box,
# Backblaze B2, or back home to the Pi), e.g.:
#   restic -r sftp:u123@u123.your-storagebox.de:memehog backup "$BACKUP_DIR"
set -euo pipefail

DATA_DIR="${1:?usage: backup.sh <data-dir> <backup-dir>}"
BACKUP_DIR="${2:?usage: backup.sh <data-dir> <backup-dir>}"

mkdir -p "$BACKUP_DIR"

# .backup gives a consistent snapshot even mid-write (WAL-aware) — never
# plain-copy a live SQLite file.
sqlite3 "$DATA_DIR/memehog.db" ".backup '$BACKUP_DIR/memehog.db'"

# Media files are content-addressed (hash names) — rsync only moves new ones.
rsync -a --delete "$DATA_DIR/library/" "$BACKUP_DIR/library/"
rsync -a --delete "$DATA_DIR/thumbs/" "$BACKUP_DIR/thumbs/"

echo "backup done: $(du -sh "$BACKUP_DIR" | cut -f1) in $BACKUP_DIR"
