#!/usr/bin/env bash
# Daily backup of the database and uploaded photos. Installed as /usr/local/bin/church-backup
# and run every night by /etc/cron.d/church-backup. Keeps the last 14 days on this server.
#
# IMPORTANT: copies on the same server do not protect you if the server is lost. Also copy
# /var/backups/church off the server (see HOSTINGER_SETUP.md section 9).
set -euo pipefail

BACKUP_DIR=/var/backups/church
STAMP=$(date +%Y-%m-%d_%H%M)
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

sudo -u postgres pg_dump church | gzip > "$BACKUP_DIR/church-db-$STAMP.sql.gz"
tar -czf "$BACKUP_DIR/church-uploads-$STAMP.tar.gz" -C /srv/church static/uploads

find "$BACKUP_DIR" -name 'church-*' -mtime +"$KEEP_DAYS" -delete

# Optional off-server copy, once rclone is set up (HOSTINGER_SETUP.md section 9):
# rclone copy "$BACKUP_DIR" remote:church-backups --max-age 2d

echo "Backup finished: $STAMP"
