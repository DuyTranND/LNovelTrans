FROM node:22-alpine AS frontend
WORKDIR /frontend
RUN corepack enable && corepack prepare pnpm@11.19.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data

# Install gosu to safely drop privileges from root to non-root UID 1000 at runtime
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY fonts/ ./fonts/
COPY --from=frontend /frontend/dist ./frontend/dist

RUN useradd --create-home --uid 1000 translator && mkdir -p /app/data && chown translator:translator /app/data

# Robust startup: ensure Railway root-owned persistent volume is writable, then drop to non-root UID 1000
RUN printf '%s\n' \
  '#!/bin/sh' \
  'set -e' \
  'DATA_DIR="${DATA_DIR:-/app/data}"' \
  'PORT="${PORT:-8000}"' \
  'mkdir -p "$DATA_DIR"' \
  'if [ "$(id -u)" = "0" ]; then' \
  '  chown translator:translator "$DATA_DIR"' \
  '  if ! gosu translator test -w "$DATA_DIR"; then' \
  '    echo "ERROR: DATA_DIR ($DATA_DIR) is not writable by translator (UID 1000)" >&2' \
  '    exit 1' \
  '  fi' \
  '  if [ "$#" -eq 0 ]; then' \
  '    exec gosu translator uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" --workers 1' \
  '  fi' \
  '  exec gosu translator "$@"' \
  'fi' \
  'if ! test -w "$DATA_DIR"; then' \
  '  echo "ERROR: DATA_DIR ($DATA_DIR) is not writable by user (UID $(id -u))" >&2' \
  '  exit 1' \
  'fi' \
  'if [ "$#" -eq 0 ]; then' \
  '  exec uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" --workers 1' \
  'fi' \
  'exec "$@"' \
  > /entrypoint.sh && chmod 755 /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
