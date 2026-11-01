"""Paths and global settings. Everything is overridable through environment variables."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BLUEPRINT_DIR = PACKAGE_DIR / "blueprints"

MAX_ATTEMPTS = int(os.environ.get("IAT_MAX_ATTEMPTS", "3"))
DEFAULT_BUDGET_USD = float(os.environ.get("IAT_BUDGET_USD", "5"))          # monthly; above this a human must approve
HARD_LIMIT_USD = float(os.environ.get("IAT_HARD_LIMIT_USD", "50"))         # monthly; above this deploy is blocked
DEFAULT_TTL_HOURS = float(os.environ.get("IAT_TTL_HOURS", "4"))            # cleanup candidates after this


def home_dir() -> Path:
    d = Path(os.environ.get("IAT_HOME", Path.cwd() / ".iat")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def runs_dir() -> Path:
    d = home_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clouds_config_path() -> Path:
    return Path(os.environ.get("IAT_CLOUDS_CONFIG", Path.cwd() / "config" / "clouds.yaml")).expanduser()


def pricing_dir() -> Path:
    return Path(os.environ.get("IAT_PRICING_DIR", PACKAGE_DIR / "pricing"))
