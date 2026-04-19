#!/usr/bin/env bash
set -euo pipefail

# Wait for Postgres if configured.
if [[ "${DATABASE_URL:-}" == *"@db:"* ]]; then
    echo "[entrypoint] waiting for postgres..."
    for i in $(seq 1 30); do
        python -c "import asyncio,asyncpg,os; url=os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://'); asyncio.run(asyncpg.connect(url).close() if False else (lambda: None)())" 2>/dev/null && break || true
        python - <<'PY' && break || sleep 2
import asyncio, os, asyncpg
url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
async def main():
    conn = await asyncpg.connect(url)
    await conn.close()
asyncio.run(main())
PY
    done
fi

# Run alembic upgrade, falling back to SQLAlchemy create_all (belt-and-braces).
alembic -c /app/config/alembic.ini upgrade head || echo "[entrypoint] alembic skipped (create_all will cover)"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
