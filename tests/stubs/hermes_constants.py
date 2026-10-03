"""Minimal stand-in for Hermes's hermes_constants, for running plugin tests without Hermes."""
import os
from pathlib import Path


def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
