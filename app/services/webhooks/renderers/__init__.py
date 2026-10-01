"""Flavor registry — adding a flavor is a new module + one entry here."""

from app.services.webhooks.renderers.discord import DiscordRenderer
from app.services.webhooks.renderers.protocol import Renderer
from app.services.webhooks.renderers.raw import RawRenderer
from app.services.webhooks.renderers.slack import SlackRenderer


def default_renderers() -> dict[str, Renderer]:
    return {
        RawRenderer.flavor: RawRenderer(),
        DiscordRenderer.flavor: DiscordRenderer(),
        SlackRenderer.flavor: SlackRenderer(),
    }


__all__ = [
    "DiscordRenderer",
    "RawRenderer",
    "Renderer",
    "SlackRenderer",
    "default_renderers",
]
