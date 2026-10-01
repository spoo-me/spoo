# Contributing Guidelines

Thank you for considering contributing to spoo.me! Every contribution helps.

> [!NOTE]
>
> ## View the full contribution guidelines at <https://docs.spoo.me/contributing>

## Quick Start

All you need is Docker — one command sets up MongoDB, Redis, and the app with hot-reloading:

```bash
# Fork and clone the repository
git clone https://github.com/YOUR_USERNAME/spoo.git
cd spoo

# Copy environment config and start everything
cp .env.example .env
docker compose up -d
```

That's it — visit <http://localhost:8000>. The app hot-reloads on file changes.

For linting and tests, you'll also need uv:

```bash
pip install uv
uv sync
uv run pre-commit install
```

## Development Workflow

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make your changes
3. Pre-commit hooks will automatically run ruff linting and formatting on commit
4. Run tests: `uv run pytest`
5. Push and create a pull request

## Code Quality

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting, enforced via pre-commit hooks. You can also run manually:

```bash
uv run ruff check .        # Lint
uv run ruff format .       # Format
```

## Running Tests

```bash
# Run full test suite
uv run pytest

# Run specific test file
uv run pytest tests/unit/services/test_click_service.py

# Run with verbose output
uv run pytest -v
```

## Project Structure

```bash
spoo/
├── app/                        # Application package
│   ├── main.py                 # Entry point (uvicorn app.main:app)
│   ├── factory.py              # FastAPI app factory
│   ├── config.py               # Pydantic settings
│   ├── errors.py               # Domain error classes
│   ├── routes/                 # Route handlers
│   │   ├── api_v1/             # V2 JSON API (shorten, stats, exports, keys)
│   │   ├── legacy/             # V1 form-based endpoints
│   │   ├── auth_routes.py      # Authentication (register, login, refresh)
│   │   ├── oauth_routes.py     # OAuth2 providers
│   │   ├── redirect_routes.py  # Short URL redirection
│   │   └── health_routes.py    # Health check endpoint
│   ├── services/               # Business logic layer
│   ├── repositories/           # Data access layer (MongoDB)
│   ├── schemas/                # Pydantic models and DTOs
│   │   ├── models/             # Database document models
│   │   └── dto/                # Request/response schemas
│   ├── infrastructure/         # External service integrations
│   │   ├── cache/              # Redis caching (URL cache, dual cache)
│   │   ├── email/              # Email provider
│   │   ├── geoip.py            # GeoIP lookups
│   │   └── oauth_clients.py    # OAuth2 client setup
│   ├── dependencies/           # FastAPI dependency injection
│   ├── middleware/             # Error handlers, rate limiting, logging
│   ├── shared/                 # Cross-cutting utilities
│   ├── data/                   # GeoIP databases, seed lists, apps.yaml
│   ├── templates/              # Jinja2 HTML templates
│   └── static/                 # CSS, JS, images
├── tests/                      # Test suite
│   ├── unit/                   # Unit tests (mocked dependencies)
│   ├── integration/            # Integration tests (TestClient)
│   ├── shorten.py              # Smoke test (URL shortening)
│   └── stats.py                # Smoke test (statistics)
├── docker-compose.yml          # Local development stack
├── dockerfile                  # Container image
└── pyproject.toml              # Project config, dependencies, tool settings
```

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker and Docker Compose (recommended) or local MongoDB + Redis

## Pull Request Guidelines

- Keep PRs focused on a single change
- Ensure all CI checks pass (linting, tests)
- Update documentation if your change affects public behavior
- Follow [conventional commit](https://www.conventionalcommits.org/) messages

## Developer Certificate of Origin

Every commit must carry a sign-off certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):
that you wrote the change, or otherwise have the right to submit it under
the project license.

```bash
git commit -s
```

This adds a `Signed-off-by` line to the commit message.

## Getting Help

- [Discord](https://spoo.me/discord) for real-time help
- [GitHub Issues](https://github.com/spoo-me/spoo/issues) for bugs and feature requests
- [Documentation](https://docs.spoo.me) for API details and guides

## License

This project is licensed under the AGPL-3.0 License - see the [LICENSE](../LICENSE) file for details.
Versions released before the switch remain available under Apache 2.0.

The spoo.me name and logo are not covered by the code license - see [TRADEMARK.md](TRADEMARK.md).

![Contribution Charts](https://repobeats.axiom.co/api/embed/48a40934896cbcaff2812e80478ebb701ee49dd4.svg)
