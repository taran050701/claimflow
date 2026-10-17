#!/usr/bin/env sh
# Production start (Render). Runs on every deploy and every wake-up from sleep.
set -e  # stop at the first failing command; never serve with a half-migrated DB

.venv/bin/alembic upgrade head          # idempotent: no-op when already at head
.venv/bin/python -m scripts.demo_seed   # idempotent: existing demo claims are skipped

# exec: uvicorn REPLACES this shell, so it receives the platform's SIGTERM directly
# and shuts down gracefully. --proxy-headers: trust Render's X-Forwarded-* headers.
exec .venv/bin/uvicorn claimflow.main:app \
    --host 0.0.0.0 --port "${PORT:-8000}" \
    --proxy-headers --forwarded-allow-ips='*' \
    --no-access-log  # our middleware already logs every request as JSON
