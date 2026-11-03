# Exam Hall Seating Maps

A small Flask app for publishing exam seating maps as PDFs and handing them to
invigilators on exam day.

- **Public side** — pick an exam date, see every map grouped by session and
  hall, download or print it. No sign in, no folder hunting.
- **Admin side** — one password. Upload any number of PDFs for a date in a
  single batch, label each one with its session, hall and subject, and fix or
  delete them later.

Maps are organised as **date → session → hall → subject**, and the number of
maps per hall is unlimited.

---

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit it, see below
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export ADMIN_PASSWORD=choose-something
export FLASK_ENV=development

python app.py
```

Open http://127.0.0.1:5000 — the public page. Sign in at `/login` to upload.

---

## Configuration

Everything is set through environment variables (see `.env.example`).

| Variable | Required | What it does |
| --- | --- | --- |
| `SECRET_KEY` | **Yes, in production** | Signs the admin session cookie. A random value is generated at startup if unset, which logs every admin out on each restart. |
| `ADMIN_PASSWORD_HASH` | **Yes, in production** | Werkzeug hash of the admin password. Preferred over the plain-text variable. |
| `ADMIN_PASSWORD` | No | Plain-text fallback for local testing. Leave blank in production. |
| `DATA_DIR` | **Yes, in production** | Where the SQLite database and uploaded PDFs are written. See the warning below. |
| `MAX_UPLOAD_MB` | No | Size cap for one upload batch. Default 64. |
| `PORT` | No | Set automatically by most hosts. |

Generate the password hash:

```bash
python -c "from werkzeug.security import generate_password_hash as h; print(h(input('password: ')))"
```

Paste the output into `ADMIN_PASSWORD_HASH`. The plain password is never
stored anywhere.

---

## ⚠️ Storage: read this before deploying

Uploaded PDFs and the SQLite database both live under `DATA_DIR`. **Most
hosting platforms give each deploy a fresh, empty filesystem.** If `DATA_DIR`
points at ordinary app storage, every redeploy silently erases all your maps.

Attach a persistent volume and point `DATA_DIR` at it:

- **Railway** — add a Volume to the service, mount at `/data`, set `DATA_DIR=/data`.
- **Render** — add a Disk, mount at `/data`, set `DATA_DIR=/data`.
- **Fly.io** — `fly volumes create maps_data --size 1`, mount at `/data` in `fly.toml`.
- **Own VPS** — any directory you back up, e.g. `/srv/seating-maps/data`.

One gigabyte handles thousands of seating maps.

---

## Deploying

The repo already contains `Procfile`, `gunicorn.conf.py` and
`requirements.txt`, so most platforms need no extra build configuration.

1. Push this directory to a Git repository.
2. Create a new web service from that repo on your host.
3. Attach a persistent volume mounted at `/data`.
4. Set the environment variables: `SECRET_KEY`, `ADMIN_PASSWORD_HASH`,
   `DATA_DIR=/data`.
5. Deploy. The start command is `gunicorn -c gunicorn.conf.py app:app`.

`GET /healthz` returns `{"status": "ok"}` for uptime checks.

Because the site is public, the admin login is the only thing standing between
the internet and your upload form — use a long password. Logins are throttled
to 8 attempts per IP per 5 minutes.

---

## Filename convention (optional but handy)

The upload page parses filenames to pre-fill the hall, subject and code
columns, so a well-named batch needs almost no typing. It recognises:

| Filename | Hall | Subject | Code | Session |
| --- | --- | --- | --- | --- |
| `HallA_MATH101_Morning.pdf` | Hall A | — | MATH101 | Morning |
| `Hall-B_Organic_Chemistry_CHEM204.pdf` | Hall B | Organic Chemistry | CHEM204 | *default* |
| `Room12_Physics_PM.pdf` | Room 12 | Physics | — | Afternoon |

Recognised place words: `hall`, `room`, `block`, `lab`, `theatre`, `gym`,
`auditorium`. Recognised session words: `morning`, `afternoon`, `evening`,
`am`, `pm`. Anything the parser gets wrong is editable right there in the
table before you submit, and `Copy first row's hall & session down` fills a
whole batch for one hall in a click.

---

## How invigilators use it

Send one URL to the exam halls. On the day:

1. The page opens on today's date already.
2. Filter by hall name if the date is busy.
3. **Download** saves the PDF; **Open / print** shows it in the browser's PDF
   viewer, ready for Ctrl+P.
4. `Download all for Hall A` grabs every map for that hall as one zip.

Downloaded files are named self-descriptively, e.g.
`2026-11-09_Morning_Hall-A_MATH101.pdf`, so they stay sorted in the
Downloads folder.

---

## Project layout

```
app.py                  Routes, SQLite schema, auth, upload/download logic
gunicorn.conf.py        Production server settings
requirements.txt        Flask, Werkzeug, gunicorn
Procfile                Start command for Railway / Render / Heroku
templates/
  base.html             Shared shell, nav, flash messages
  index.html            Public date view + client-side hall filter
  admin.html            Bulk upload queue + inline edit/delete
  login.html            Admin sign in
  error.html            404 / 413 pages
static/style.css        All styling
data/                   Created at runtime: seating_maps.db + uploads/
```

The database has a single `maps` table; there is no migration system, so if
you add a column, add it with `ALTER TABLE`.

---

## Things you might want next

- **Multiple admin accounts** with an audit trail of who uploaded what.
- **Per-hall access links** so each hall only sees its own maps.
- **Student lookup** — type a student number, get the hall and seat, which
  needs the seating data as rows rather than as flat PDFs.
- **Automatic cleanup** of maps older than a set number of months.
