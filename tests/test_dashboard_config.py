from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "Scripts"
sys.path.insert(0, str(SCRIPTS))

from dashboard_config import CONFIG_ENV_VAR, load_config


class DashboardConfigTests(unittest.TestCase):
    def write_config(self, root: Path, **overrides: object) -> Path:
        payload = {
            "schemaVersion": 1,
            "app": {
                "name": "Test App",
                "appStoreId": "1234567890",
                "platform": "ios",
            },
            "workspace": {
                "root": "..",
                "projectFile": "TestApp.xcodeproj/project.pbxproj",
                "metricsDirectory": "build/Metrics",
            },
            "firebase": {
                "projectId": "test-project",
                "credentialFiles": ["credentials/*.json"],
            },
            "analytics": {
                "activationEvent": "signup_completed",
                "activationValueParameter": "items_created",
            },
        }
        payload.update(overrides)
        path = root / "dashboard.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_resolves_app_and_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir()
            config = load_config(self.write_config(config_dir))

            self.assertEqual(config.app_name, "Test App")
            self.assertEqual(config.app_slug, "test-app")
            self.assertEqual(config.platform, "IOS")
            self.assertEqual(config.workspace_root, Path(directory).resolve())
            self.assertEqual(config.metrics_path, Path(directory).resolve() / "build/Metrics/metrics.json")
            self.assertEqual(config.analytics_identifier("activationEvent", "activation_completed"), "signup_completed")

    def test_loads_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(root)
            previous = os.environ.get(CONFIG_ENV_VAR)
            os.environ[CONFIG_ENV_VAR] = str(path)
            try:
                self.assertEqual(load_config().path, path.resolve())
            finally:
                if previous is None:
                    os.environ.pop(CONFIG_ENV_VAR, None)
                else:
                    os.environ[CONFIG_ENV_VAR] = previous

    def test_rejects_unsafe_analytics_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(
                root,
                analytics={
                    "activationEvent": "event' OR TRUE --",
                    "activationValueParameter": "value",
                },
            )
            with self.assertRaisesRegex(ValueError, "letters, numbers, and underscores"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
