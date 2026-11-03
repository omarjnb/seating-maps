"""Gunicorn settings for the seating-maps app."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Two workers is plenty: this app is I/O bound on file sends, and SQLite
# writes only happen during uploads.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("WEB_THREADS", "4"))
worker_class = "gthread"

# Uploading 20 scanned PDFs over a slow office line can take a while.
timeout = 180
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

# Trust the platform's proxy headers so request.remote_addr / HTTPS detection
# behave on Railway, Render, Fly and friends.
forwarded_allow_ips = "*"
