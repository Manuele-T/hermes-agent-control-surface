# Optional packaging, two uses:
# 1. Self-hosting against a real Hermes (see README's Docker section for the
#    host.docker.internal / volume-mount setup that needs — ./run.sh in WSL is
#    the recommended path for that case instead).
# 2. The public synthetic-data demo (HERMES_DATA_SOURCE=synthetic, no real
#    Hermes needed at all) — this is what render.yaml builds and deploys.

FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

EXPOSE 8123
# Bind 0.0.0.0 here because container-internal loopback isn't reachable from
# the host — for self-hosting, publish with `-p 127.0.0.1:8123:8123` so it's
# loopback-only on the host (see README). $PORT (set by Render and similar
# platforms) overrides the default for the public demo deploy.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8123}"]
