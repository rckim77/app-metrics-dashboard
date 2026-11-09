from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts"))

import serve_dashboard


class ProjectVersionTests(unittest.TestCase):
    def read_version(self, name: str, contents: str) -> str | None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / name
            project.write_text(contents)
            with patch.object(serve_dashboard, "CONFIG", SimpleNamespace(project_file=project)):
                return serve_dashboard.project_marketing_version()

    def test_legacy_project_prefers_highest_version(self):
        self.assertEqual(self.read_version("project.pbxproj", '''
            MARKETING_VERSION = "2.9.0";
            MARKETING_VERSION = 2.10.0;
            MARKETING_VERSION = 1.0;
        '''), "2.10.0")

    def test_xcproj_with_trailing_commas_and_configuration_overrides(self):
        self.assertEqual(self.read_version("project.xcproj", '''
        {
          "targets": [
            { "build-settings": {
              "MARKETING_VERSION[config=Debug]": "2.9.0",
              "MARKETING_VERSION[config=Release]": "2.10.0",
            } },
            { "build-settings": { "MARKETING_VERSION": "1.0", } },
          ],
        }
        '''), "2.10.0")

    def test_missing_version(self):
        self.assertIsNone(self.read_version("project.xcproj", '{ "targets": [], }'))

    def test_missing_project(self):
        with patch.object(serve_dashboard, "CONFIG", SimpleNamespace(project_file=Path("/nonexistent/project.xcproj"))):
            self.assertIsNone(serve_dashboard.project_marketing_version())


if __name__ == "__main__":
    unittest.main()
