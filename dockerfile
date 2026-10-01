FROM python:3.14-slim

# Injected by CI: release version (2.1.0) or short sha for edge builds
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Install curl for healthchecks (10MB) and clean up apt cache
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy the application into the container.
COPY . /app/

# Install the application dependencies.
WORKDIR /app
RUN uv sync --frozen --no-cache

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
