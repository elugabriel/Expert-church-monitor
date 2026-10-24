#!/usr/bin/env bash
# Deploy the latest code. Run as root on the server:  bash /srv/church/deploy/update.sh
# Takes a backup first, so a bad update can be rolled back.
set -euo pipefail

APP_DIR=/srv/church

/usr/local/bin/church-backup
sudo -u church git -C "$APP_DIR" pull --ff-only
sudo -u church "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
# The app upgrades its own database tables when it starts.
systemctl restart church
sleep 3
systemctl --no-pager --lines=10 status church
echo "Updated. Open the site and check you can log in."
