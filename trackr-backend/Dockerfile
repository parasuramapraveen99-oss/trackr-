FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir -e ".[db,storage,pdf]"

EXPOSE 8000

CMD python -c "import os, psycopg; from app.db.migrate import migrate; conn = psycopg.connect(os.environ['DATABASE_URL']); migrate(conn); conn.commit(); conn.close()" \
    && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
