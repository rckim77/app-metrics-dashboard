"""Load and validate an app-metrics-dashboard configuration file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ENV_VAR = "APP_METRICS_DASHBOARD_CONFIG"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class DashboardConfig:
    path: Path
    data: dict[str, Any]

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def app(self) -> dict[str, Any]:
        return self.data.get("app") or {}

    @property
    def app_name(self) -> str:
        return str(self.app.get("name") or "App")

    @property
    def app_slug(self) -> str:
        raw = str(self.app.get("slug") or self.app_name).lower()
        slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
        return slug or "app"

    @property
    def app_store_id(self) -> str:
        value = str(self.app.get("appStoreId") or "").strip()
        if not value:
            raise ValueError("app.appStoreId is required.")
        return value

    @property
    def platform(self) -> str:
        return str(self.app.get("platform") or "IOS").upper()

    @property
    def live_status_enabled(self) -> bool:
        return bool(self.app.get("liveStatus", True))

    @property
    def configured_version(self) -> str | None:
        value = str(self.app.get("version") or "").strip()
        return value or None

    @property
    def configured_version_state(self) -> str:
        return str(self.app.get("versionState") or "unknown").upper()

    @property
    def workspace_root(self) -> Path:
        raw = str((self.data.get("workspace") or {}).get("root") or ".")
        return self.resolve_from_config(raw)

    @property
    def metrics_root(self) -> Path:
        raw = str((self.data.get("workspace") or {}).get("metricsDirectory") or "build/AppMetrics")
        return self.resolve_from_workspace(raw)

    @property
    def metrics_path(self) -> Path:
        filename = str((self.data.get("workspace") or {}).get("metricsFile") or "metrics.json")
        if Path(filename).name != filename:
            raise ValueError("workspace.metricsFile must be a filename, not a path.")
        return self.metrics_root / filename

    @property
    def project_file(self) -> Path | None:
        raw = (self.data.get("workspace") or {}).get("projectFile")
        return self.resolve_from_workspace(str(raw)) if raw else None

    @property
    def firebase(self) -> dict[str, Any]:
        return self.data.get("firebase") or {}

    @property
    def firebase_project_id(self) -> str:
        return str(self.firebase.get("projectId") or "").strip()

    @property
    def firebase_dataset(self) -> str | None:
        value = str(self.firebase.get("dataset") or "").strip()
        return value or None

    @property
    def credential_globs(self) -> list[str]:
        values = self.firebase.get("credentialFiles") or []
        return [str(value) for value in values if str(value).strip()]

    @property
    def analytics(self) -> dict[str, Any]:
        return self.data.get("analytics") or {}

    @property
    def content(self) -> dict[str, Any]:
        return self.data.get("content") or {}

    @property
    def ui(self) -> dict[str, Any]:
        return self.data.get("ui") or {}

    @property
    def env_file(self) -> Path:
        raw = str(self.data.get("envFile") or "metrics-dashboard.env")
        return self.resolve_from_config(raw)

    def analytics_identifier(self, key: str, default: str) -> str:
        value = str(self.analytics.get(key) or default)
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError(f"analytics.{key} must contain only letters, numbers, and underscores.")
        return value

    def analytics_values(self, key: str, default: list[str]) -> list[str]:
        values = self.analytics.get(key)
        resolved = values if isinstance(values, list) and values else default
        output: list[str] = []
        for value in resolved:
            item = str(value)
            if not IDENTIFIER_PATTERN.fullmatch(item):
                raise ValueError(f"analytics.{key} values must contain only letters, numbers, and underscores.")
            output.append(item)
        return output

    def content_path(self, key: str) -> Path | None:
        value = self.content.get(key)
        return self.resolve_from_config(str(value)) if value else None

    def read_content(self, key: str) -> str | None:
        path = self.content_path(key)
        if not path or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def resolve_from_config(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.directory / path
        return path.resolve()

    def resolve_from_workspace(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    def public_payload(self) -> dict[str, Any]:
        feature_labels = self.ui.get("featureLabels") or ["Primary feature", "Secondary feature"]
        labels = [str(item) for item in feature_labels][:2]
        while len(labels) < 2:
            labels.append("Feature")
        return {
            "app": {
                "name": self.app_name,
                "slug": self.app_slug,
                "appStoreId": self.app_store_id,
                "platform": self.platform,
            },
            "analytics": {
                "activationLabel": str(self.ui.get("activationLabel") or "Activation"),
                "activationValueLabel": str(self.ui.get("activationValueLabel") or "Activation value"),
                "activationValueUnit": str(self.ui.get("activationValueUnit") or "number"),
                "meaningfulActivationMinimum": int(self.analytics.get("meaningfulActivationMinimum") or 0),
                "featureLabels": labels,
            },
            "behavior": {
                "autoRefresh": bool(self.ui.get("autoRefresh", True)),
            },
        }


def resolve_config_path(value: str | Path | None = None) -> Path:
    raw = value or os.environ.get(CONFIG_ENV_VAR)
    if not raw:
        raise ValueError(
            "Pass --config /path/to/dashboard.json or set "
            f"{CONFIG_ENV_VAR}."
        )
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Dashboard config not found: {path}")
    return path


def load_config(value: str | Path | None = None) -> DashboardConfig:
    path = resolve_config_path(value)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Dashboard config must contain a JSON object.")
    if int(data.get("schemaVersion") or 0) != 1:
        raise ValueError("Dashboard config schemaVersion must be 1.")
    config = DashboardConfig(path=path, data=data)
    _ = config.app_store_id
    _ = config.analytics_identifier("firstOpenEvent", "first_open")
    _ = config.analytics_identifier("activationEvent", "activation_completed")
    _ = config.analytics_identifier("activationValueParameter", "value")
    return config
