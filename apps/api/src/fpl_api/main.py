"""ASGI entry point: ``uvicorn fpl_api.main:app``."""

from __future__ import annotations

from fpl_api.app import create_app
from fpl_api.settings import Settings

app = create_app(Settings())
