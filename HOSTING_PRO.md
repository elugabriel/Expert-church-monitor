# Hosting plan: Pro setup on Render

This is the production-grade version of the hosting plan. It assumes the **Render Pro workspace** ($25 per
month plus compute). The basic plan is in `HOSTING_REQUIREMENTS.md`; this document replaces its sizing,
backup and staging advice where they differ. Prices are in US dollars, taken from render.com/pricing on
23 October 2026. Check them again before buying.

## 1. Why Pro

| Need | Hobby ($0) | Pro ($25 per month) |
|---|---|---|
| Custom domains included | 2 | 15 |
| Bandwidth included | 5 GB | 25 GB |
| Database point-in-time recovery | past 3 days | past 7 days |
| Team seats | limited | unlimited |
| Staging copy of the whole stack | single-service previews | full-stack previews |
| Horizontal autoscaling | no | yes |

For a system holding church members' personal details for many churches, the main reasons to choose Pro
are the longer recovery window, the staging environment, and room for teammates.

## 2. Recommended resources

| Resource | Plan | Price per month |
|---|---|---|
| Workspace | Pro | $25 |
| Web service | Standard (1 CPU, 2 GB) | $25 |
| Postgres | Basic 1 GB (1 GB storage included) | $19 |
| **Total** | | **about $69** |

Cheaper option: Starter web ($7) with Basic 256 MB Postgres ($6) on Pro gives about **$38**. It works for a
handful of churches, but there is little headroom.

Optional staging copy (a second web service and database used to test changes before they reach the real
churches): about **$13 more** on the cheapest plans, or less if it is only run for short periods, since
compute is billed by the second.

Other charges: extra database storage $0.30 per GB, bandwidth beyond 25 GB (price not checked), extra
custom domains beyond 15 at $0.25 each. No Cron Job is needed, because messages are sent by staff, not on a schedule.

## 3. Architecture

- One Render web service running the Flask app with gunicorn, serving every church.
- One Render Postgres database in the **same region**, connected with the **internal** connection URL
  (faster and private). Use `sslmode=require` on it.
- Custom domains: `expertchurch.com` (superadmin) and the wildcard `*.expertchurch.com` (each church).
  Render issues HTTPS certificates automatically.
- No scheduled job: birthday, first-timer and absentee messages are sent by staff pressing Send.
- All data lives in Postgres. Nothing important may be stored on the web service's disk.

## 4. Must-fix before going live

1. **Add `psycopg2-binary` to `requirements.txt`.** Without it the app cannot connect to Postgres.
2. **Move member photos into the database** (like church logos), and resize them when uploaded. Today they
   are saved to `static/uploads` and vanish on every deploy. A persistent disk is not recommended on Pro,
   because a disk ties the service to a single instance, and Render may disable zero-downtime deploys
   for services with disks (confirm in Render's docs).
3. **Trial deploy on Postgres.** Everything so far was tested on SQLite. Check member, attendance, services,
   first-timer and special-program flows on Postgres with test data first.
4. **Commit and push to a private Git repo.** Render deploys from Git.
5. **Pin the Python version** with the `PYTHON_VERSION` environment variable (the app was tested on 3.10).
6. **Remove the SQLite Cloud API key from `.env.example`** and rotate it.

## 5. Production settings

| Setting | Value |
|---|---|
| Start command | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 3 --timeout 60 --preload` |
| Health check path | `/login` |
| `SECRET_KEY` | Long random value, set once and never changed (changing it logs everyone out) |
| `BASE_DOMAIN` | `expertchurch.com` |
| `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` | Set by you, strong, changed after first login |
| `DATABASE_URL` | Internal URL from the Render database |
| `PYTHON_VERSION` | Pinned |

Notes:
- Three workers suit a 1 CPU, 2 GB service for this app. Raise the plan before raising the worker count.
- The 60 second timeout gives bulk emailing more room (it already sends in batches of about 20 seconds).
- Postgres allows 100 connections on these plans, which is far more than this app will use.

Sketch of the matching blueprint (the existing `render.yaml` only needs the plan, start command and health
check changed):

```yaml
services:
  - type: web
    name: expert-church
    runtime: python
    plan: standard
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --workers 3 --timeout 60 --preload
    healthCheckPath: /login
    envVars:
      - key: DATABASE_URL
        fromDatabase: {name: expert-church-db, property: connectionString}
      - key: SECRET_KEY
        generateValue: true
      - key: BASE_DOMAIN
        value: expertchurch.com
      - key: PYTHON_VERSION
        value: 3.10.14
      - key: SUPERADMIN_USERNAME
        sync: false
      - key: SUPERADMIN_PASSWORD
        sync: false
databases:
  - name: expert-church-db
    plan: basic-1gb
```

Check the plan and version names against Render's current plan list when you apply this.

## 6. Domain and DNS

1. Buy or confirm control of `expertchurch.com`.
2. In Render: web service, Settings, Custom Domains. Add `expertchurch.com` and `*.expertchurch.com`.
3. Create the DNS records Render shows: the root domain pointing to Render, a `*` CNAME to the service's
   `onrender.com` address, and the `_acme-challenge` and `_cf-custom-hostname` CNAMEs Render lists for
   wildcard certificates.
4. Wait until Render shows both domains as verified with HTTPS before sharing any QR code.
5. In the app, give each church its Subdomain (Superadmin, Edit Church).
6. Wildcard domains count against your included domain total. Confirm in Render that the wildcard counts as
   one domain; if not, you still have 15 included.

## 7. Email and SMS

- **Email:** a personal Gmail account is limited to about 500 messages a day, and Google Workspace to 2,000,
  counted across every church. For more than a few hundred emails a day, use a transactional email service
  (any SMTP provider works with the existing `MAIL_*` settings). Add SPF and DKIM records for the sending
  domain so mail does not land in spam.
- **SMS:** eBulkSMS with a funded account. Each church can use its own sender name (at most 11 letters or
  digits, set by the app from the church name). Numbers on the DND list cost 2 units.

## 8. Backups, monitoring and recovery

- Point-in-time recovery for the past 7 days is included with a paid database on Pro. Practise a restore
  once before launch.
- Download a logical export of the database monthly and keep it somewhere outside Render.
- Render keeps logical backups for 7 days.
- Turn on Render's deploy and failure notifications (email or Slack).
- Keep a written recovery note: where the Git repo is, where the environment variables are stored, who
  owns the domain, and how to run `flask reset-superadmin` if the superadmin password is lost.

## 9. Security checklist

- Strong superadmin password; enable two-factor sign-in on Render, GitHub and the domain registrar.
- `.env`, `church.db` and uploads stay out of Git (already in `.gitignore`).
- Admin and user passwords are temporary and must be changed at first login (built).
- Limit Render workspace seats to the people who need them.
- Members' phone numbers, addresses and birthdays are personal data. Decide who may export CSVs (admins only,
  already enforced) and agree a data-retention rule with each church. If you serve Nigerian churches, check
  your obligations under the Nigeria Data Protection Act.

## 10. Launch checklist

1. Fix the items in section 4, commit, and push.
2. Create the Render workspace on the Pro plan and add a payment card.
3. Create the services from the blueprint; set the superadmin variables.
4. Open the `onrender.com` address; log in; change the superadmin password.
5. Create a test church with a subdomain and test admin; check the temporary password flow.
6. Add the custom domains and DNS records; wait for HTTPS.
7. On the test church's address: add members, upload a photo and redeploy to confirm it stays, mark attendance
   at two services, take a QR check-in and a special-program registration from a phone, and run both reports.
8. Practise a database restore.
9. Delete the test data and create the real churches.
10. Hand each church admin their temporary password securely.

## 11. What I need from you

- Confirmation that "Pro" means the Render Pro workspace. If you meant something else (for example a bigger
  server size or another host), tell me and I will revise this.
- The domain name and where it is registered.
- Whether you want a staging copy.
- Which email option you will use, and your eBulkSMS details.
- Permission to commit, add `psycopg2-binary`, and move photos into the database.
