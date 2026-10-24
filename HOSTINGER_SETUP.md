# Setting up the system on a Hostinger VPS (KVM 1)

This guide takes you from buying the server to a running site with HTTPS on every church address,
nightly backups and a safe way to update. Plan for a few hours the first time.

The scripts in the `deploy` folder were written and syntax-checked but **not run on a real server**,
because none existed when they were written. Do the first run with test data and read each step's
output before moving on.

You will use: the Hostinger VPS, a domain name (Namecheap), a free Cloudflare account (for DNS, so the
HTTPS certificate for all church addresses can renew by itself), and your Git repository.

## 1. Before you start

- [ ] The project is committed and pushed to a **private** Git repository.
- [ ] You have chosen and bought the domain. Namecheap sells `expertchurch.com` only as a premium name, so see
      the domain options in the budget document. This guide writes the domain as `yourdomain.org`.
- [ ] You have a strong password for the superadmin: at least 10 characters, using only letters, numbers and
      `. _ @ % + = -`.
- [ ] A way to give the server access to a private repository (step 4).

## 2. Buy and prepare the server

1. On Hostinger, buy **VPS hosting, KVM 1**. Hostinger's page lists it at 1 CPU, 4 GB RAM, 50 GB disk.
2. Pick the data centre location nearest Nigeria that is offered.
3. Choose the operating system **Ubuntu 24.04**, plain, **without** a control panel (no cPanel, Plesk or CyberPanel).
4. Set a strong root password, or better, add your SSH public key.
5. Note the server's IP address from the dashboard. Turn on Hostinger's weekly backups and its firewall if
   it offers one. They are extra safety, not a replacement for section 9.
6. Connect from your computer (Windows PowerShell has `ssh`):

   ```bash
   ssh root@SERVER_IP
   ```

## 3. Domain and DNS on Cloudflare

Using Cloudflare's free DNS lets the server renew one certificate that covers every church subdomain.

1. Create a free Cloudflare account and **Add a site** with your domain. Choose the free plan.
2. Cloudflare shows two nameservers. At Namecheap, open the domain, set **Nameservers** to **Custom DNS**,
   and enter those two. This can take up to a few hours.
3. In Cloudflare DNS, add two records. Set the proxy to **DNS only** (grey cloud):

   | Type | Name | Content |
   | --- | --- | --- |
   | A | `@` | SERVER_IP |
   | A | `*` | SERVER_IP |

   The `*` record makes `rccg.yourdomain.org`, `penua.yourdomain.org` and every future church address point
   to the server, with no changes needed per church.
4. In Cloudflare, create an **API token**: My Profile, API Tokens, Create Token, use the template
   **Edit zone DNS**, limit it to your domain, and copy the token. Keep it secret.

## 4. Install the system

On the server, as root:

```bash
export REPO_URL='https://github.com/you/church.git'
export DOMAIN='yourdomain.org'
export SUPERADMIN_PASSWORD='choose-a-long-password'
```

For a **private** repository, the simplest way is a GitHub fine-grained token with read-only access to that
one repository, used in the address: `https://TOKEN@github.com/you/church.git`. Treat the token like a password.

Fetch and run the setup script:

```bash
git clone "$REPO_URL" /root/church-setup
bash /root/church-setup/deploy/setup_server.sh
```

The script installs the software, closes the firewall except SSH, HTTP and HTTPS, creates the database and
an app user, writes `/srv/church/.env` with a random secret key, installs the service, and sets up nightly
backups. At the end it should show the `church` service as **active (running)**.

If installing the Python packages fails on `sqlalchemy-sqlitecloud`, delete that line from `requirements.txt`
(it is only needed for SQLite Cloud, which you are not using), push, and run the script again.

## 5. Check DNS has reached the server

```bash
getent hosts yourdomain.org
getent hosts anything.yourdomain.org
```

Both should show `SERVER_IP`. If not, wait and try again.

## 6. HTTPS certificate and web server

1. Give certbot the Cloudflare token:

   ```bash
   mkdir -p /root/.secrets
   echo 'dns_cloudflare_api_token = PASTE_TOKEN_HERE' > /root/.secrets/cloudflare.ini
   chmod 600 /root/.secrets/cloudflare.ini
   ```

2. Request one certificate for the main address and every subdomain:

   ```bash
   certbot certonly --dns-cloudflare \
     --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
     -d yourdomain.org -d '*.yourdomain.org' \
     --deploy-hook 'systemctl reload nginx'
   ```

   Use your own email when it asks. Certbot sets up automatic renewal.

3. Turn on the nginx site (this is the command the setup script printed):

   ```bash
   sed 's/__DOMAIN__/yourdomain.org/g' /srv/church/deploy/nginx-church.conf > /etc/nginx/sites-available/church
   ln -sf /etc/nginx/sites-available/church /etc/nginx/sites-enabled/church
   rm -f /etc/nginx/sites-enabled/default
   nginx -t && systemctl reload nginx
   ```

4. Open `https://yourdomain.org` in a browser. Log in as the superadmin, using the username and password you
   set (the username defaults to `superadmin`).

## 7. First checks (use test data)

1. Change the superadmin password (key icon, top right).
2. Create a test church with a subdomain such as `test`, and tick the features you want for it.
3. Add the church's admin. Log out, then open `https://test.yourdomain.org` and log in with the temporary
   password; you must change it.
4. On that address: add a member with a photo, mark attendance, take a QR check-in and a special-program
   registration from your phone.
5. Restart the service and confirm the photo is still there:

   ```bash
   systemctl restart church
   ```

6. Delete the test church and create the real ones.

## 8. Email and SMS (optional)

Edit `/srv/church/.env` (`nano /srv/church/.env`), remove the `#` from the settings you need, fill them in,
then `systemctl restart church`.

- **Email:** any SMTP provider. With a free Gmail account use `smtp.gmail.com`, port 587 and an App Password.
  Gmail limits personal accounts to about 500 messages a day across all churches. Check that outgoing mail on
  port 587 works from your VPS before relying on it.
- **SMS:** an eBulkSMS account with credit; the username, API key and a sender name of at most 11 letters or digits.

## 9. Backups (do not skip)

The server makes a database and photo backup every night at 02:30 into `/var/backups/church`, and keeps 14 days.
**These live on the same server.** If the server is lost, so are they, so also copy them somewhere else:

1. Install `rclone` and connect a cloud drive you control: `apt-get install -y rclone`, then `rclone config`.
2. In `/usr/local/bin/church-backup`, remove the `#` before the `rclone copy` line and put your remote name in it.
3. Test a restore on a spare machine at least once:

   ```bash
   gunzip -c church-db-DATE.sql.gz | sudo -u postgres psql church
   tar -xzf church-uploads-DATE.tar.gz -C /srv/church
   ```

Check `/var/log/church-backup.log` and the backup folder weekly for the first month.

## 10. Updating the system

After pushing new code to Git, on the server:

```bash
bash /srv/church/deploy/update.sh
```

It takes a backup, pulls the code, installs packages and restarts the service. The app upgrades its own
database tables on start.

## 11. Looking after it

- **Uptime alert:** add `https://yourdomain.org/login` to a free monitor such as UptimeRobot, with email alerts.
- **Logs:** `journalctl -u church -n 100` for the app, `/var/log/nginx/error.log` for the web server.
- **Security updates:** the script turns on automatic security updates. Reboot occasionally when asked
  (`reboot`); the app starts again on its own.
- **Disk space:** `df -h` once a month. Backups older than 14 days are deleted automatically.
- **If you are locked out of the superadmin:** `cd /srv/church && sudo -u church venv/bin/flask --app app reset-superadmin`.
- **If the site is down:** `systemctl status church nginx postgresql`, then `systemctl restart church`.

## 12. Limits to remember

- One server is one point of failure. Keep the off-server backups working.
- Add churches freely, but if the server feels slow, the next Hostinger size up is a simple upgrade.
- You are responsible for the server's security and for members' personal data on it.
