from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "Scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_metrics
from dashboard_config import load_config


class FetchQueryConfigTests(unittest.TestCase):
    def test_bigquery_uses_configured_event_and_parameter_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "dashboard.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "app": {"name": "Query App", "appStoreId": "1234567890"},
                        "workspace": {"root": "."},
                        "firebase": {"projectId": "query-project"},
                        "analytics": {
                            "firstOpenEvent": "app_first_open",
                            "permissionPromptEvent": "camera_prompt_seen",
                            "permissionResultEvent": "camera_permission_result",
                            "permissionStatusParameter": "permission_state",
                            "permissionAcceptedValues": ["granted"],
                            "initialLoadEvent": "feed_ready",
                            "activationEvent": "signup_completed",
                            "activationValueParameter": "items_created",
                            "meaningfulActivationMinimum": 3,
                            "paywallEvent": "upgrade_viewed",
                            "primaryFeatureEvent": "share_finished",
                            "secondaryFeatureEvent": "import_finished",
                        },
                    }
                ),
                encoding="utf-8",
            )
            fetch_metrics.configure_runtime(load_config(config_path))

            daily = fetch_metrics.firebase_daily_query("`project.dataset.events_*`", date(2026, 1, 1), date(2026, 1, 2))
            activation = fetch_metrics.firebase_activation_query(
                "`project.dataset.events_*`",
                date(2026, 1, 1),
                date(2026, 1, 2),
                date(2026, 1, 9),
            )

            self.assertIn("event_name = 'signup_completed'", daily)
            self.assertIn("key = 'items_created'", daily)
            self.assertIn("event_name = 'camera_permission_result'", daily)
            self.assertIn("IN ('granted')", daily)
            self.assertIn("event_name IN ('app_first_open', 'signup_completed')", activation)
            self.assertIn("activation_value >= 3", activation)


if __name__ == "__main__":
    unittest.main()
