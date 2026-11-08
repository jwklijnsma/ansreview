"""Self-hosted web UI for ansreview (`ansreview serve`)."""

from .server import WebApp, serve

__all__ = ["WebApp", "serve"]
