FROM python:3.12-slim

WORKDIR /app/backend

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY frontend/ /app/frontend/

EXPOSE 8080

# Non-SQLite databases are schema-managed by Alembic (SCALE.md D1); on SQLite
# the upgrade is a no-op beyond the version table and init_db handles DDL.
CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port 8080"]
