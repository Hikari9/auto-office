"""Auto Office runtime: the `office` CLI and its single runs.db state authority."""
from __future__ import annotations

__all__ = ["__version__"]


def __getattr__(name: str):
    if name == "__version__":
        from office.version import current
        return current()
    raise AttributeError(name)
