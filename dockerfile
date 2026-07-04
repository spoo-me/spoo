# ── Builder: resolve and install dependencies only ──────────────────────
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer keyed on the lockfile inputs alone — code changes never
# invalidate it, so rebuilds after app edits skip the entire install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache --compile-bytecode


# ── Runtime: venv + code, no build machinery, non-root ──────────────────
FROM python:3.14-slim

# Injected by CI: release version (2.1.0) or short sha for edge builds
ARG APP_VERSION=dev
# UV_NO_SYNC: `uv run` entrypoints must never mutate the baked venv at
# container start (the dev-dependency group isn't installed, and a sync
# would try to pull it).
ENV APP_VERSION=${APP_VERSION} \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:${PATH}"

# curl for healthchecks (10MB) and clean up apt cache
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home appuser

# uv stays available so `uv run ...` compose commands keep working
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY . /app/

USER appuser

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
