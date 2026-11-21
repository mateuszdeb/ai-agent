"""Local same-origin web application with session bootstrap."""

from agent_project.web.app import create_app
from agent_project.web.settings import WebSettings

__all__ = ["WebSettings", "create_app"]
