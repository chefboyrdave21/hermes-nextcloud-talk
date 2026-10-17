"""Nextcloud Talk real-user adapter for Hermes (user mode).

See ``adapter.py`` for the implementation. This re-export lets the
repo-root dispatcher (``register(ctx)`` at the repo root) do
``from .user import adapter`` without caring about this module's own
internals, and lets ``user/`` be deployed standalone as its own plugin
(see ``adapter.register`` docstring).
"""

from .adapter import register

__all__ = ["register"]
