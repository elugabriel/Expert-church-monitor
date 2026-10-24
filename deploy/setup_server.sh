#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VPS for the Expert Church Monitoring System.
#
# Run as root, giving three values:
#   REPO_URL=https://github.com/you/church.git DOMAIN=yourdomain.org SUPERADMIN_PASSWORD='a-long-password' \
#     bash setup_server.sh
#
# It installs the software, creates the database and the app user, writes the settings file,
# and starts the app. It stops before HTTPS: follow HOSTINGER_SETUP.md section 6 for the
# certificate and the nginx site. Safe to read first; it is not magic.
set -euo pipefail

: "${REPO_URL:?Set REPO_URL to your git repository address}"
: "${DOMAIN:?Set DOMAIN to your domain, for example yourdomain.org}"
: "${SUPERADMIN_PASSWORD:?Set SUPERADMIN_PASSWORD to a strong password}"
SUPERADMIN_USERNAME="${SUPERADMIN_USERNAME:-superadmin}"

# The password goes into a settings file that systemd and python-dotenv both read, so keep it
# to characters neither of them treats specially (no spaces, quotes, # or $).
if ! [[ "$SUPERADMIN_PASSWORD" =~ ^[A-Za-z0-9._@%+=-]{10,}$ ]]; then
    echo "SUPERADMIN_PASSWORD must be 10+ characters using only letters, numbers and . _ @ % + = -" >&2
    exit 1
fi

APP_USER=church
APP_DIR=/srv/church
DB_NAME=church
DB_USER=church

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root." >&2
    exit 1
fi

echo "== Installing packages =="
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y upgrade
DEBIAN_FRONTEND=noninteractive apt-get -y install \
    python3-venv python3-pip python3-dev libpq-dev build-essential \
    nginx postgresql ufw fail2ban unattended-upgrades git curl \
    certbot python3-certbot-dns-cloudflare

echo "== Firewall: SSH, HTTP and HTTPS only =="
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
systemctl enable --now fail2ban

echo "== App user and code =="
id -u "$APP_USER" >/dev/null 2>&1 || adduser --system --group --home "$APP_DIR" "$APP_USER"
if [ ! -d "$APP_DIR/.git" ]; then
    # The directory may already exist (it is the app user's home), so clone into a temp place.
    TMP_CLONE=$(mktemp -d)
    git clone "$REPO_URL" "$TMP_CLONE"
    cp -a "$TMP_CLONE"/. "$APP_DIR"/
    rm -rf "$TMP_CLONE"
    chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
fi

echo "== Python environment =="
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
mkdir -p "$APP_DIR/static/uploads"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/static/uploads"

echo "== Database and settings file =="
if [ ! -f "$APP_DIR/.env" ]; then
    DB_PASS=$(openssl rand -hex 16)
    sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
    ELSE
        ALTER ROLE ${DB_USER} PASSWORD '${DB_PASS}';
    END IF;
END
\$\$;
SQL
    sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
        || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"

    umask 077
    cat > "$APP_DIR/.env" <<ENV
SECRET_KEY=$(openssl rand -hex 32)
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}
BASE_DOMAIN=${DOMAIN}
SUPERADMIN_USERNAME=${SUPERADMIN_USERNAME}
SUPERADMIN_PASSWORD=${SUPERADMIN_PASSWORD}
# Optional, for absentee emails (see HOSTINGER_SETUP.md):
# MAIL_SERVER=smtp.gmail.com
# MAIL_PORT=587
# MAIL_USERNAME=
# MAIL_PASSWORD=
# MAIL_FROM=
# Optional, for absentee SMS:
# EBULKSMS_USERNAME=
# EBULKSMS_API_KEY=
# EBULKSMS_SENDER=
ENV
    chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
else
    echo "$APP_DIR/.env already exists, so the database and settings were left alone."
fi

echo "== Service =="
install -m 644 "$APP_DIR/deploy/church.service" /etc/systemd/system/church.service
systemctl daemon-reload
systemctl enable --now church
sleep 3
systemctl --no-pager --lines=15 status church || true

echo "== Backups =="
mkdir -p /var/backups/church
chown "$APP_USER":"$APP_USER" /var/backups/church
install -m 755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/church-backup
install -m 644 "$APP_DIR/deploy/church-backup.cron" /etc/cron.d/church-backup

cat <<DONE

Done. The app is running behind a socket but is not reachable from the internet yet.
Next, in HOSTINGER_SETUP.md: point DNS at this server (section 5), get the HTTPS
certificate (section 6), then enable the nginx site:

  sed 's/__DOMAIN__/${DOMAIN}/g' $APP_DIR/deploy/nginx-church.conf > /etc/nginx/sites-available/church
  ln -sf /etc/nginx/sites-available/church /etc/nginx/sites-enabled/church
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
DONE
