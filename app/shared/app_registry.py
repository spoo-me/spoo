"""
App registry loader.

Reads apps.yaml at startup, validates each entry with Pydantic,
and returns a typed dict of AppEntry models.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.infrastructure.logging import get_logger
from app.schemas.models.app import AppEntry, AppStatus

log = get_logger(__name__)

_PACKAGE_DIR = Path(__file__).parent.parent
_APPS_YAML = _PACKAGE_DIR / "data" / "apps.yaml"
_STATIC_APPS_DIR = _PACKAGE_DIR / "static" / "images" / "apps"


def load_app_registry(config_path: Path | None = None) -> dict[str, AppEntry]:
    """Load and validate the app registry from YAML.

    Returns a dict mapping app_id -> validated AppEntry model.
    Invalid entries are skipped with a warning.
    """
    path = config_path or _APPS_YAML
    if not path.exists():
        log.warning("app_registry_not_found", path=str(path))
        return {}

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        log.error("app_registry_parse_error", path=str(path), error=str(exc))
        return {}

    if not data or not isinstance(data, dict):
        log.warning("app_registry_invalid_format", path=str(path))
        return {}

    raw_apps = data.get("apps", {})
    if not raw_apps:
        log.warning("app_registry_empty", path=str(path))
        return {}

    registry: dict[str, AppEntry] = {}
    for app_id, app_data in raw_apps.items():
        try:
            entry = AppEntry.model_validate(app_data)
        except ValidationError as exc:
            log.warning(
                "app_registry_entry_invalid",
                app_id=app_id,
                error=str(exc),
            )
            continue

        # Fail-safe: a live device app must declare the scopes it will be
        # granted. Skipping (not defaulting) means a misconfigured entry
        # can never mint an unrestricted grant.
        if entry.is_live_device_app() and not entry.scopes:
            log.warning(
                "app_registry_live_app_missing_scopes",
                app_id=app_id,
            )
            continue

        # Validate icon files exist for live apps
        if entry.status == AppStatus.LIVE and entry.icon:
            icon_path = _STATIC_APPS_DIR / entry.icon
            if not icon_path.exists():
                log.warning(
                    "app_icon_missing",
                    app_id=app_id,
                    icon=entry.icon,
                    expected_path=str(icon_path),
                )

        registry[app_id] = entry

    log.info("app_registry_loaded", app_count=len(registry))
    return registry
