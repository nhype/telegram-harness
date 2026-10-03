"""Stub of hermes_constants for the runtime used by bin/harness test-plugins tests."""
import os
from pathlib import Path


def get_default_hermes_root() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser() / ".hermes"


def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or get_default_hermes_root())
