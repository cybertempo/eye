# Synthetic demo image. Build context: repository root. Runs on: developer
# laptop (Docker), CI. Build web/dist first with scripts/setup.sh.
# Base image pinned by index digest; see docs/dependencies.md to update it.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    EYE_CONTAINER=1

WORKDIR /app
# Runtime code uses the standard library only, so no packages are installed.
COPY backend/eye /app/backend/eye
COPY web/index.html web/demo.css /app/web/
COPY web/dist/demo.js /app/web/dist/demo.js
COPY tests/fixtures/synthetic /app/tests/fixtures/synthetic
COPY deploy/dev/eye.container.toml /app/deploy/dev/eye.container.toml

USER 65534:65534
CMD ["python", "-m", "eye", "serve", "--config", "/app/deploy/dev/eye.container.toml"]
