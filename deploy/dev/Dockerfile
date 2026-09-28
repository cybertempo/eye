# Synthetic demo image. Build context: repository root. Runs on: developer
# laptop (Docker), CI. Run scripts/setup.sh first: it builds web/dist and
# downloads the hash-locked runtime wheels into build/wheels.
# Base image pinned by index digest; see docs/dependencies.md to update it.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    EYE_CONTAINER=1

WORKDIR /app
# pg8000 and its dependencies, installed offline from the downloaded wheels and
# checked against the hashes in requirements/runtime.lock.
COPY requirements/runtime.lock /tmp/runtime.lock
COPY build/wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --disable-pip-version-check --no-input --no-index \
      --find-links /tmp/wheels --require-hashes --no-deps --only-binary=:all: \
      -r /tmp/runtime.lock \
    && rm -rf /tmp/runtime.lock /tmp/wheels
COPY backend/eye /app/backend/eye
COPY schemas /app/schemas
COPY migrations /app/migrations
COPY reference /app/reference
COPY web/index.html web/app.css /app/web/
COPY web/dist /app/web/dist
COPY tests/fixtures/synthetic /app/tests/fixtures/synthetic
COPY deploy/dev/eye.container.toml /app/deploy/dev/eye.container.toml

USER 65534:65534
# Prepare the disposable demo database (migrate, load synthetic fixtures,
# derive counts), then serve. deploy/dev/compose.yaml supplies the database.
CMD ["sh", "-c", "python -m eye db-prepare-demo --config /app/deploy/dev/eye.container.toml && exec python -m eye serve --config /app/deploy/dev/eye.container.toml"]
