"""Resolve credentials and local environment for app-metrics-dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from dashboard_config import load_config


DEFAULT_ASC_TIMEOUT = "120s"


def resolve_google_credentials_path() -> str | None:
    """Return an existing credentials JSON path, or None."""
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path:
        path = Path(env_path).expanduser()
        if path.is_file():
            return str(path.resolve())

    config = load_config()
    for pattern in config.credential_globs:
        resolved_pattern = config.resolve_from_config(pattern)
        parent = resolved_pattern.parent
        if not parent.is_dir():
            continue
        matches = sorted(parent.glob(resolved_pattern.name))
        if matches:
            return str(matches[0].resolve())
    return None


def apply_growth_dashboard_env_file() -> bool:
    """Load the configured env file without overriding shell exports."""
    env_file = load_config().env_file
    if not env_file.is_file():
        return False
    loaded = False
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded = True
    return loaded


def apply_google_credentials_default() -> str | None:
    """Set GOOGLE_APPLICATION_CREDENTIALS when unset and a default file exists."""
    apply_growth_dashboard_env_file()
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return resolve_google_credentials_path()
    resolved = resolve_google_credentials_path()
    if resolved:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = resolved
    return resolved


def growth_subprocess_env() -> dict[str, str]:
    """Environment for ASC and dashboard fetch subprocesses."""
    apply_growth_dashboard_env_file()
    env = os.environ.copy()
    env.setdefault("ASC_BYPASS_KEYCHAIN", "1")
    env.setdefault("ASC_TIMEOUT", DEFAULT_ASC_TIMEOUT)
    config = load_config()
    if config.firebase_project_id:
        env.setdefault("FIREBASE_BQ_PROJECT_ID", config.firebase_project_id)
    if config.firebase_dataset:
        env.setdefault("FIREBASE_BQ_DATASET", config.firebase_dataset)
    return env
