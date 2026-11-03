"""
Exam Hall Seating Maps — a small Flask app for distributing seating-map PDFs.

Two sides:
  * Public  : pick an exam date, browse maps grouped by session / hall / subject,
              download or print them. No login.
  * Admin   : password-protected bulk upload, replace and delete.

Run locally:   python app.py
Run in prod:   gunicorn -c gunicorn.conf.py app:app
"""

from __future__ import annotations

import io
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
import zipfile
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

#: Where PDFs and the SQLite file live. On a hosted platform this MUST point at
#: a persistent volume, otherwise every redeploy wipes the uploads.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "seating_maps.db"

#: Sessions an exam date can be split into. Edit to taste.
SESSIONS = ["Morning", "Afternoon", "Evening"]

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "64"))
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECONDS = 300

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Served over HTTPS in production; relaxed locally so dev login works.
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
)

#: Prefer ADMIN_PASSWORD_HASH (a werkzeug hash). ADMIN_PASSWORD is the plain
#: fallback for quick local runs.
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "32768:8:1$GupNwFCFIplJpmoD$efdca8d9643aff3ba6548cb19627d7178df35db6dc734d6d9e3663d14449093504a1db464cf088756aeab4ca0d05767eb293207b6c2f4d42c54f1c0566d17087").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# In-memory brute-force throttle: {ip: (failed_count, first_failure_ts)}
_login_attempts: dict[str, tuple[int, float]] = {}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_date     TEXT    NOT NULL,          -- YYYY-MM-DD
    session_name  TEXT    NOT NULL,
    hall          TEXT    NOT NULL,
    subject       TEXT    NOT NULL,
    subject_code  TEXT    NOT NULL DEFAULT '',
    stored_name   TEXT    NOT NULL UNIQUE,   -- uuid.pdf on disk
    original_name TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    uploaded_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maps_date ON maps (exam_date);
CREATE INDEX IF NOT EXISTS idx_maps_date_hall ON maps (exam_date, hall);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_storage() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def admin_password_configured() -> bool:
    return bool(ADMIN_PASSWORD_HASH or ADMIN_PASSWORD)


def verify_admin_password(candidate: str) -> bool:
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, candidate)
    if ADMIN_PASSWORD:
        return secrets.compare_digest(ADMIN_PASSWORD, candidate)
    return False


def login_locked_out(ip: str) -> int:
    """Return remaining lockout seconds, or 0 if the caller may try again."""
    record = _login_attempts.get(ip)
    if not record:
        return 0
    count, first_ts = record
    elapsed = time.time() - first_ts
    if elapsed > LOGIN_LOCKOUT_SECONDS:
        _login_attempts.pop(ip, None)
        return 0
    if count >= LOGIN_MAX_ATTEMPTS:
        return int(LOGIN_LOCKOUT_SECONDS - elapsed)
    return 0


def record_login_failure(ip: str) -> None:
    count, first_ts = _login_attempts.get(ip, (0, time.time()))
    if time.time() - first_ts > LOGIN_LOCKOUT_SECONDS:
        count, first_ts = 0, time.time()
    _login_attempts[ip] = (count + 1, first_ts)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-"


def is_pdf(stream) -> bool:
    """Check the magic bytes, not just the extension."""
    head = stream.read(5)
    stream.seek(0)
    return head == PDF_MAGIC


def parse_date(value: str | None) -> str:
    """Validate a YYYY-MM-DD string, falling back to today."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return date.today().isoformat()


def pretty_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%A, %d %B %Y")
    except ValueError:
        return iso


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def safe_component(text: str, fallback: str = "file") -> str:
    """Turn arbitrary text into something safe for a download filename."""
    normalized = unicodedata.normalize("NFKD", text)
    cleaned = re.sub(r"[^\w\-. ]+", "", normalized, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or fallback


def download_name(row: sqlite3.Row) -> str:
    """A self-describing filename, e.g. 2026-11-09_Morning_Hall-A_MATH101.pdf"""
    parts = [
        row["exam_date"],
        safe_component(row["session_name"], "session"),
        safe_component(row["hall"], "hall"),
        safe_component(row["subject_code"] or row["subject"], "subject"),
    ]
    return "_".join(p for p in parts if p) + ".pdf"


def session_sort_key(name: str) -> tuple[int, str]:
    try:
        return (SESSIONS.index(name), "")
    except ValueError:
        return (len(SESSIONS), name.lower())


def fetch_maps(exam_date: str) -> list[sqlite3.Row]:
    return (
        get_db()
        .execute(
            "SELECT * FROM maps WHERE exam_date = ? ORDER BY hall, subject",
            (exam_date,),
        )
        .fetchall()
    )


def group_by_session(rows) -> list[tuple[str, list[tuple[str, list]]]]:
    """[(session, [(hall, [rows]), ...]), ...] in display order."""
    by_session: dict[str, dict[str, list]] = {}
    for row in rows:
        by_session.setdefault(row["session_name"], {}).setdefault(
            row["hall"], []
        ).append(row)

    result = []
    for session_name in sorted(by_session, key=session_sort_key):
        halls = by_session[session_name]
        hall_list = [
            (hall, halls[hall]) for hall in sorted(halls, key=str.casefold)
        ]
        result.append((session_name, hall_list))
    return result


def build_zip(rows, archive_label: str):
    """Bundle the given maps into an in-memory zip."""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            path = UPLOAD_DIR / row["stored_name"]
            if not path.exists():
                continue
            name = download_name(row)
            counter = 2
            while name in used:
                name = f"{download_name(row)[:-4]}_{counter}.pdf"
                counter += 1
            used.add(name)
            archive.write(path, arcname=name)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_component(archive_label, 'seating_maps')}.zip",
    )


# --------------------------------------------------------------------------
# Jinja helpers
# --------------------------------------------------------------------------

app.jinja_env.filters["human_size"] = human_size
app.jinja_env.filters["pretty_date"] = pretty_date


@app.context_processor
def inject_globals():
    return {
        "today": date.today().isoformat(),
        "sessions": SESSIONS,
        "is_admin": bool(session.get("is_admin")),
    }


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    exam_date = parse_date(request.args.get("date"))
    rows = fetch_maps(exam_date)

    # Dates that actually have maps, for the "jump to" list.
    upcoming = (
        get_db()
        .execute(
            """SELECT exam_date, COUNT(*) AS n
               FROM maps
               WHERE exam_date >= ?
               GROUP BY exam_date
               ORDER BY exam_date
               LIMIT 8""",
            (date.today().isoformat(),),
        )
        .fetchall()
    )

    return render_template(
        "index.html",
        exam_date=exam_date,
        grouped=group_by_session(rows),
        total=len(rows),
        upcoming=upcoming,
    )


@app.route("/map/<int:map_id>")
def view_map(map_id: int):
    """Open the PDF inline — this is what the Print button uses."""
    row = get_db().execute("SELECT * FROM maps WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        abort(404)
    return send_from_directory(
        UPLOAD_DIR,
        row["stored_name"],
        mimetype="application/pdf",
        as_attachment=False,
        download_name=download_name(row),
    )


@app.route("/download/<int:map_id>")
def download_map(map_id: int):
    row = get_db().execute("SELECT * FROM maps WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        abort(404)
    if not (UPLOAD_DIR / row["stored_name"]).exists():
        abort(404)
    return send_from_directory(
        UPLOAD_DIR,
        row["stored_name"],
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name(row),
    )


@app.route("/download-bundle")
def download_bundle():
    """Zip every map for a date, optionally narrowed to one session or hall."""
    exam_date = parse_date(request.args.get("date"))
    session_name = request.args.get("session", "").strip()
    hall = request.args.get("hall", "").strip()

    query = "SELECT * FROM maps WHERE exam_date = ?"
    params: list[str] = [exam_date]
    label_parts = [exam_date]

    if session_name:
        query += " AND session_name = ?"
        params.append(session_name)
        label_parts.append(session_name)
    if hall:
        query += " AND hall = ?"
        params.append(hall)
        label_parts.append(hall)

    query += " ORDER BY hall, subject"
    rows = get_db().execute(query, params).fetchall()
    if not rows:
        abort(404)

    return build_zip(rows, "_".join(label_parts))


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_password_configured():
        return render_template("login.html", unconfigured=True), 503

    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
        ip = ip.split(",")[0].strip()

        remaining = login_locked_out(ip)
        if remaining:
            flash(f"Too many attempts. Try again in {remaining} seconds.", "error")
            return render_template("login.html"), 429

        if verify_admin_password(request.form.get("password", "")):
            _login_attempts.pop(ip, None)
            session.clear()
            session["is_admin"] = True
            session.permanent = False
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") else url_for("admin"))

        record_login_failure(ip)
        flash("Incorrect password.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Admin routes
# --------------------------------------------------------------------------


@app.route("/admin")
@admin_required
def admin():
    exam_date = parse_date(request.args.get("date"))
    rows = fetch_maps(exam_date)
    all_dates = (
        get_db()
        .execute(
            """SELECT exam_date, COUNT(*) AS n
               FROM maps GROUP BY exam_date ORDER BY exam_date DESC LIMIT 30"""
        )
        .fetchall()
    )
    known_halls = [
        r["hall"]
        for r in get_db()
        .execute("SELECT DISTINCT hall FROM maps ORDER BY hall")
        .fetchall()
    ]
    return render_template(
        "admin.html",
        exam_date=exam_date,
        maps=rows,
        all_dates=all_dates,
        known_halls=known_halls,
        max_upload_mb=MAX_UPLOAD_MB,
    )


@app.route("/admin/upload", methods=["POST"])
@admin_required
def upload():
    exam_date = parse_date(request.form.get("exam_date"))
    files = request.files.getlist("maps")

    if not files or all(not f.filename for f in files):
        flash("No files selected.", "error")
        return redirect(url_for("admin", date=exam_date))

    db = get_db()
    saved, skipped = 0, []

    for index, file in enumerate(files):
        if not file or not file.filename:
            continue

        # Per-file metadata arrives as maps-0-hall, maps-0-subject, ...
        hall = (request.form.get(f"hall-{index}") or "").strip()
        subject = (request.form.get(f"subject-{index}") or "").strip()
        code = (request.form.get(f"code-{index}") or "").strip()
        session_name = (request.form.get(f"session-{index}") or "").strip()

        if not hall:
            skipped.append(f"{file.filename} (no hall)")
            continue
        if not subject:
            subject = Path(file.filename).stem
        if not session_name:
            session_name = SESSIONS[0]

        if not secure_filename(file.filename).lower().endswith(".pdf"):
            skipped.append(f"{file.filename} (not a .pdf)")
            continue
        if not is_pdf(file.stream):
            skipped.append(f"{file.filename} (not a real PDF)")
            continue

        stored_name = f"{uuid.uuid4().hex}.pdf"
        destination = UPLOAD_DIR / stored_name
        file.save(destination)

        db.execute(
            """INSERT INTO maps
                 (exam_date, session_name, hall, subject, subject_code,
                  stored_name, original_name, size_bytes, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exam_date,
                session_name,
                hall,
                subject,
                code,
                stored_name,
                file.filename[:255],
                destination.stat().st_size,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        saved += 1

    db.commit()

    if saved:
        flash(f"Uploaded {saved} map{'s' if saved != 1 else ''}.", "ok")
    if skipped:
        flash("Skipped: " + "; ".join(skipped), "error")

    return redirect(url_for("admin", date=exam_date))


@app.route("/admin/delete/<int:map_id>", methods=["POST"])
@admin_required
def delete_map(map_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM maps WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        abort(404)

    (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
    db.execute("DELETE FROM maps WHERE id = ?", (map_id,))
    db.commit()

    flash(f"Deleted {row['hall']} — {row['subject']}.", "ok")
    return redirect(url_for("admin", date=row["exam_date"]))


@app.route("/admin/edit/<int:map_id>", methods=["POST"])
@admin_required
def edit_map(map_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM maps WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        abort(404)

    db.execute(
        """UPDATE maps
              SET exam_date = ?, session_name = ?, hall = ?,
                  subject = ?, subject_code = ?
            WHERE id = ?""",
        (
            parse_date(request.form.get("exam_date") or row["exam_date"]),
            (request.form.get("session_name") or row["session_name"]).strip(),
            (request.form.get("hall") or row["hall"]).strip(),
            (request.form.get("subject") or row["subject"]).strip(),
            (request.form.get("subject_code") or "").strip(),
            map_id,
        ),
    )
    db.commit()
    flash("Details updated.", "ok")
    return redirect(url_for("admin", date=request.form.get("exam_date")))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="Not found."), 404


@app.errorhandler(413)
def too_large(_e):
    return (
        render_template(
            "error.html",
            code=413,
            message=f"That upload is larger than the {MAX_UPLOAD_MB} MB limit.",
        ),
        413,
    )


init_storage()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_ENV") == "development",
    )
