FROM node:22-alpine AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY public ./public
COPY src ./src
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SC_DATA_DIR=/data

# requirements.lock.txt is a fully pinned, resolved snapshot (pip freeze) of
# requirements.txt's version ranges — reproducible builds without a
# surprise transitive-dependency bump landing straight in production.
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && apt-get update && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend ./backend
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh
COPY --from=frontend /app/dist ./frontend_dist

# /data is the only place this process ever writes (LocalFilesystemStorage,
# SQLite dev database) — both are development/testing-only in production
# (S3 storage + PostgreSQL), so a production deployment can run this image
# with a read-only root filesystem plus a tmpfs mount for /tmp and this
# directory left as the sole writable volume, or omitted entirely.
RUN groupadd --system stranger_club && useradd --system --gid stranger_club --home-dir /data stranger_club \
    && mkdir -p /data/uploads && chown -R stranger_club:stranger_club /data /app
USER stranger_club

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
