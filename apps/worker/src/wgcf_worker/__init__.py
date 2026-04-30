"""Worker entrypoint package for the Workspace Governance Control Fabric."""

from .main import build_parser, main, render_worker_status_human

__all__ = ["build_parser", "main", "render_worker_status_human"]
