"""Orbit pre-reviews GitHub pull requests."""

import os


__version__ = "0.1.0"


def orbit_env(name: str) -> str | None:
    """Read an Orbit setting, accepting the previous prefix for existing installations."""
    return os.environ.get(f"ORBIT_{name}") or os.environ.get(f"PRBOT_{name}")
