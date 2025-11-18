"""Remo-GPU package."""

from __future__ import annotations

from importlib import metadata

from .cli import main  # noqa: F401

try:
    __version__ = metadata.version("remo-gpu")
except metadata.PackageNotFoundError:  # pragma: no cover - during local dev
    __version__ = "0.0.0"

__all__ = ["main", "__version__"]

