# Expert Church Monitoring System

A Flask + SQL attendance system for churches: super admin manages
churches and church admins; each church admin manages members, marks
attendance by member number, registers first timers, runs absentee reports,
and gets a birthday reminder pop-up for the current month.

All application code (models, routes, config) lives in a single
[`app.py`](app.py) for a simple deploy; HTML templates live in
[`templates/`](templates) and CSS/JS/images live in [`static/`](static) so
the front end is easy to find and edit.

## Features

- **Super admin**: create/delete churches (with logo + contact info), create/remove church admins.
- **Church admin**: add members, register first timers (with a short form), mark attendance by typing a member's unique number.
- **Bulk import**: upload a spreadsheet of existing members at once (Members → Import from CSV) instead of adding them one by one — CSV and Excel (`.xlsx`) both work, a downloadable template shows the expected columns, and bad rows are skipped and listed while the rest still import.
- **Unique member numbers**: auto-generated per church, e.g. `STP-0001`.
- **Birthdays are day + month only, never a year**: people are often not comfortable sharing their birth year, and the app only needs day/month for reminders anyway — the member form uses two dropdowns (no year field exists anywhere, including in CSV/Excel import).
- **Edit lock**: each member record starts locked; an admin must click "Enable Editing" before changing details, then can lock it again.
- **Delete member**: admins can remove a member who has left the church (from the Members list or the member's own page) — this also clears their attendance history and asks for confirmation first.
- **Absentee report**: pick a date range, see everyone with no attendance in that window (with their last-attended date), export to CSV for follow-up.
- **Birthday pop-up**: on login, admins see a modal of members whose birthday falls in the current month.

## Local setup

```bash
python -m venv venv
venv\Scripts\activate          # on Windows PowerShell: venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env         # edit as needed
python app.py
```

Visit `http://localhost:5000`. On first run the app prints a **super admin
username/password** to the console (or uses `SUPERADMIN_USERNAME` /
`SUPERADMIN_PASSWORD` from your `.env` if set) — log in with those, then:

1. Create a church (name, short code, logo, pastor, contact info).
2. Add an admin for that church.
3. Log in as that admin to add members and start marking attendance.

## Database

Uses SQLAlchemy against whatever `DATABASE_URL` you set:

- **Default / local testing**: `sqlite:///church.db` — a single SQLite file, zero setup.
- **SQLite Cloud** (good for a shared testing environment before you deploy):
  1. `pip install sqlalchemy-sqlitecloud` (already in `requirements.txt`).
  2. Create a database at [sqlitecloud.io](https://sqlitecloud.io) and grab its connection string + API key.
  3. Set `DATABASE_URL=sqlitecloud://<host>.sqlite.cloud:8860/<dbname>.sqlite?apikey=<key>`.
     (Source: [SQLite Cloud SQLAlchemy quick start](https://docs.sqlitecloud.io/docs/quick-start-sqlalchemy-orm))
- **Postgres** (recommended for real production use on Render — see caveat below): set `DATABASE_URL` to the Postgres URL Render gives you; the app auto-fixes the `postgres://` → `postgresql://` scheme.

> **Important caveat about SQLite on Render:** Render's web service filesystem
> is ephemeral — it resets on every deploy/restart. A plain local SQLite file
> will **not** persist unless you point `DATABASE_URL` at SQLite Cloud/Postgres
> instead. For anything beyond testing, use SQLite Cloud or Postgres.
>
> Church logos and member photos are stored **as bytes inside the database**
> itself (not as files on disk), specifically so they survive restarts too —
> as long as `DATABASE_URL` points at persistent storage, images persist
> right along with everything else. No separate disk or file storage setup
> is needed.

## Deploying to Render

1. Push this folder to a Git repo (GitHub/GitLab).
2. On Render: **New → Web Service**, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app` (already in the `Procfile`, Render should pick it up automatically).
5. Add environment variables in the Render dashboard:
   - `SECRET_KEY` — a long random string.
   - `DATABASE_URL` — your SQLite Cloud or Postgres connection string.
   - `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` — set these so you know the login instead of hunting through logs.

No persistent disk is needed for uploaded images — they're stored in the
database itself, so as long as `DATABASE_URL` points at SQLite Cloud or
Postgres, logos and photos survive restarts along with everything else.

## Super admin login keeps resetting on Render — why, and the fix

Render's web service disk is **ephemeral**: it's wiped on every deploy/restart.
If `DATABASE_URL` is left at the default local SQLite file, your whole
database (not just the super admin) resets each time, and a fresh random
password gets generated and printed only to that run's logs.

Fix both parts:

1. **Point `DATABASE_URL` at persistent storage** so your data survives
   deploys — Render's free managed Postgres is the easiest (a few clicks in
   the same dashboard; this app already handles `postgres://` URLs), or use
   SQLite Cloud as described above.
2. **Set `SUPERADMIN_USERNAME` and `SUPERADMIN_PASSWORD`** in Render's
   environment variables to a login you choose, instead of relying on the
   auto-generated one in the logs.

If you're ever locked out anyway, recover access without wiping any data:

```bash
flask reset-superadmin --username superadmin --password "YourNewPassword"
```

Run this either from Render's **Shell** tab (if your plan includes it), or
locally — set your local `DATABASE_URL` to the production connection string
first, then run the command; it talks straight to the production database.
Omit `--password` to have one generated and printed for you.

## Project layout

```
app.py                       # models, routes, config — the whole backend
requirements.txt
Procfile                     # for Render/gunicorn
.env.example                 # copy to .env for local config
templates/
  base.html                  # shared layout (navbar, flash messages, footer)
  auth_login.html
  superadmin/                # church & admin management pages
  admin/                     # dashboard, members, reports, church profile
  errors/                    # 403 / 404 pages
static/
  css/style.css              # custom styles (layered on Bootstrap)
  js/app.js                  # shared front-end behaviour (birthday modal)
  branding/company_logo.png  # Expert Media Solutions logo, shown in the navbar
```

Church logos and member photos aren't stored under `static/` — they're saved
as bytes in the database and served through the `/church-logo/<id>` and
`/member-photo/<id>` routes.

## Notes / next steps you may want

- Passwords for church admins are set by the super admin when creating the account; there's no self-service "forgot password" flow yet.
- Attendance is one record per member per day (re-scanning the same number the same day just shows "already marked present").
- The absentee report works off any date range you pick, so it works whether your church meets weekly, midweek, or on an irregular schedule.
- There's no migration tool (no Flask-Migrate/Alembic) — `db.create_all()` only creates tables that don't exist yet, it won't alter an existing one. If you have an old local `church.db` (or SQLite Cloud database) from before images moved into the database, delete/recreate it so the new columns get created; there's no real cost to this pre-launch, since it just means starting the church/member data fresh.
