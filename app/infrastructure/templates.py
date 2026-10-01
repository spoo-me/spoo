"""Shared Jinja2Templates instance for the entire application.

Every module that renders templates should import ``templates`` from here
instead of creating its own ``Jinja2Templates`` object.  This guarantees
that Jinja2 environment globals (tracking IDs, feature flags, etc.) are
available in **all** templates regardless of which route renders them.
"""

from __future__ import annotations

import os

from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

templates = Jinja2Templates(directory=_TEMPLATE_DIR)


def configure_template_globals(
    *,
    clarity_id: str,
    sentry_client_key: str,
    hcaptcha_sitekey: str,
) -> None:
    """Set Jinja2 globals that are available in every rendered template."""
    templates.env.globals["clarity_id"] = clarity_id
    templates.env.globals["sentry_client_key"] = sentry_client_key
    templates.env.globals["hcaptcha_sitekey"] = hcaptcha_sitekey
