#!/usr/bin/env python3
"""Serve the metrics dashboard as a local mini-app."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from credentials import (
    apply_google_credentials_default,
    growth_subprocess_env,
    resolve_google_credentials_path,
)
from dashboard_config import CONFIG_ENV_VAR, DashboardConfig, TOOL_ROOT, load_config

CONFIG: DashboardConfig | None = None
REPO_ROOT = TOOL_ROOT
ASO_MONITOR_CONFIG_PATH: Path | None = None
METRICS_PATH = TOOL_ROOT / "build/AppMetrics/metrics.json"
DAILY_METRICS_CACHE_DIR = TOOL_ROOT / "build/AppMetrics/daily"
REFRESH_PROGRESS_PATH = TOOL_ROOT / "build/AppMetrics/refresh-progress.json"
FETCH_SCRIPT = Path(__file__).resolve().parent / "fetch_metrics.py"
VENV_PYTHON = TOOL_ROOT / ".venv" / "bin" / "python"
DASHBOARD_URL_PATH = "/dashboard.html"
LEGACY_DASHBOARD_URL_PATHS = {
    "/metrics-dashboard/metrics-dashboard.html",
    "/growth-dashboard/metrics-dashboard.html",
    "/growth-dashboard/growth-visualizer.html",
    "/metrics-dashboard/growth-visualizer.html",
}
METRICS_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
SOURCE_NAMES = {"asc", "firebase", "revenueCat"}
ACTIVE_REFRESH_PROCESSES: dict[str, subprocess.Popen[str]] = {}
CANCELLED_REFRESH_SOURCES: set[str] = set()


def fetch_python() -> str:
    if VENV_PYTHON.is_file():
        return str(VENV_PYTHON)
    return sys.executable


def fetch_command() -> list[str]:
    if not CONFIG:
        raise RuntimeError("Dashboard is not configured.")
    return [fetch_python(), str(FETCH_SCRIPT), "--config", str(CONFIG.path)]


def configure_runtime(config: DashboardConfig) -> None:
    """Point the generic server at one consuming app workspace."""
    global CONFIG, REPO_ROOT, ASO_MONITOR_CONFIG_PATH
    global METRICS_PATH, DAILY_METRICS_CACHE_DIR, REFRESH_PROGRESS_PATH

    CONFIG = config
    os.environ[CONFIG_ENV_VAR] = str(config.path)
    REPO_ROOT = config.workspace_root
    ASO_MONITOR_CONFIG_PATH = config.content_path("asoMonitor")
    METRICS_PATH = config.metrics_path
    DAILY_METRICS_CACHE_DIR = config.metrics_root / "daily"
    REFRESH_PROGRESS_PATH = config.metrics_root / "refresh-progress.json"
    apply_google_credentials_default()
    growth_env = growth_subprocess_env()
    os.environ.update(
        {
            key: value
            for key, value in growth_env.items()
            if key in ("ASC_BYPASS_KEYCHAIN", "ASC_TIMEOUT", "FIREBASE_BQ_PROJECT_ID", "FIREBASE_BQ_DATASET")
        }
    )


def config_payload() -> dict[str, Any]:
    if not CONFIG:
        return {"status": "error", "message": "Dashboard is not configured."}
    return {
        "status": "ok",
        "config": CONFIG.public_payload(),
        "context": CONFIG.read_content("appContext"),
        "tasks": CONFIG.read_content("growthTasks"),
    }


class GrowthDashboardHandler(SimpleHTTPRequestHandler):
    server_version = "AppMetricsDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(TOOL_ROOT), **kwargs)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path in LEGACY_DASHBOARD_URL_PATHS:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", DASHBOARD_URL_PATH)
            self.end_headers()
            return
        if path in {"/api/metrics", "/api/growth-metrics"}:
            self.send_metrics()
            return
        if path == "/api/config":
            self.send_json(config_payload())
            return
        if path == "/api/status":
            self.send_json(status_payload())
            return
        if path == "/api/refresh-progress":
            self.send_json(read_refresh_progress())
            return
        if path == "/api/aso-monitor":
            self.send_aso_monitor()
            return
        if path == "/api/aso-monitor-config":
            self.send_aso_monitor_config()
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path in LEGACY_DASHBOARD_URL_PATHS:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", DASHBOARD_URL_PATH)
            self.end_headers()
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/refresh",
            "/api/refresh-source",
            "/api/refresh-cancel",
            "/api/refresh-reviews",
            "/api/aso-monitor-config",
        }:
            self.send_json(
                {"status": "error", "message": f"Unknown API path: {path}"},
                HTTPStatus.NOT_FOUND,
            )
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
            if path == "/api/aso-monitor-config":
                payload = save_aso_monitor_config(body)
            elif path == "/api/refresh-cancel":
                payload = cancel_refresh(body)
            elif path == "/api/refresh-source":
                payload = refresh_source(body)
            elif path == "/api/refresh-reviews":
                payload = refresh_reviews(body)
            else:
                payload = refresh_metrics(body)
            self.send_json(payload)
        except Exception as error:
            self.send_json({"status": "error", "message": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def send_aso_monitor_config(self) -> None:
        if not ASO_MONITOR_CONFIG_PATH or not ASO_MONITOR_CONFIG_PATH.exists():
            self.send_json(
                {"status": "missing", "message": "No content.asoMonitor file is configured."},
                HTTPStatus.NOT_FOUND,
            )
            return
        try:
            config = json.loads(ASO_MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            self.send_json(
                {"status": "error", "message": f"Invalid aso-monitor.json: {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.send_json({"status": "ok", "config": config})

    def send_aso_monitor(self) -> None:
        metrics = read_metrics_file()
        if not metrics:
            self.send_json(
                {"status": "missing", "note": "Load metrics before building the experiment archive."},
                HTTPStatus.NOT_FOUND,
            )
            return
        sources = metrics.get("sources") or {}
        asc = sources.get("appStoreConnect") or {}
        if asc.get("status") != "ok":
            self.send_json(
                {"status": "missing", "note": "App Store Connect metrics are not loaded yet."},
                HTTPStatus.NOT_FOUND,
            )
            return
        sys.path.insert(0, str(FETCH_SCRIPT.parent))
        from aso_monitor import build_aso_monitor_payload

        payload = build_aso_monitor_payload(
            asc,
            sources.get("appStoreListing") or {},
            sources.get("firebase") or {},
        )
        self.send_json(payload)

    def send_metrics(self) -> None:
        if not METRICS_PATH.exists():
            self.send_json(
                {
                    "status": "missing",
                    "message": "Run /api/refresh or Scripts/fetch_metrics.py first.",
                },
                HTTPStatus.NOT_FOUND,
            )
            return
        try:
            self.send_json(json.loads(METRICS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as error:
            self.send_json({"status": "error", "message": f"Invalid metrics JSON: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def refresh_metrics(options: dict[str, Any]) -> dict[str, Any]:
    command = [*fetch_command(), "--skip-reviews"]
    if options.get("skipAsc"):
        command.append("--skip-fetch")
    days = int(options.get("days") or 28)
    chart_days = int(options.get("chartDays") or 90)
    command.extend(["--days", str(days), "--chart-days", str(chart_days)])
    if backfill_days := options.get("backfillDays"):
        command.extend(["--backfill-days", str(int(backfill_days))])
    if options.get("includeSnapshot") is False:
        command.append("--no-include-snapshot")
    if firebase_source := options.get("firebaseSource"):
        command.extend(["--firebase-source", str(firebase_source)])
    if bigquery_project := options.get("bigqueryProject"):
        command.extend(["--bigquery-project", str(bigquery_project)])
    if bigquery_dataset := options.get("bigqueryDataset"):
        command.extend(["--bigquery-dataset", str(bigquery_dataset)])

    cache_existing_metrics_snapshot()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=growth_subprocess_env(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown refresh failure"
        raise RuntimeError(detail)
    if not METRICS_PATH.exists():
        raise RuntimeError("Refresh completed but metrics JSON was not written.")
    return {
        "status": "ok",
        "message": completed.stdout.strip(),
        "metrics": json.loads(METRICS_PATH.read_text(encoding="utf-8")),
    }


def save_aso_monitor_config(updates: dict[str, Any]) -> dict[str, Any]:
    if not ASO_MONITOR_CONFIG_PATH or not ASO_MONITOR_CONFIG_PATH.exists():
        raise RuntimeError("No content.asoMonitor file is configured.")
    current = json.loads(ASO_MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
    experiment_updates = updates.get("experiment") if isinstance(updates.get("experiment"), dict) else {}
    baseline_updates = updates.get("baseline") if isinstance(updates.get("baseline"), dict) else {}

    experiment = current.setdefault("experiment", {})
    if "goLiveDate" in experiment_updates:
        value = experiment_updates.get("goLiveDate")
        experiment["goLiveDate"] = value or None
    if label := experiment_updates.get("label"):
        experiment["label"] = str(label).strip()
    if locale := experiment_updates.get("primaryLocale"):
        experiment["primaryLocale"] = str(locale).strip()
    if locales := experiment_updates.get("locales"):
        if isinstance(locales, list) and locales:
            experiment["locales"] = [str(item).strip() for item in locales if str(item).strip()]

    baseline = current.setdefault("baseline", {})
    if "note" in baseline_updates:
        baseline["note"] = str(baseline_updates.get("note") or "").strip()

    ASO_MONITOR_CONFIG_PATH.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "ok", "config": current}


def register_refresh_process(source: str, process: subprocess.Popen[str]) -> None:
    with PROCESS_LOCK:
        ACTIVE_REFRESH_PROCESSES[source] = process


def unregister_refresh_process(source: str, process: subprocess.Popen[str]) -> None:
    with PROCESS_LOCK:
        if ACTIVE_REFRESH_PROCESSES.get(source) is process:
            ACTIVE_REFRESH_PROCESSES.pop(source, None)


def terminate_refresh_process(source: str) -> bool:
    with PROCESS_LOCK:
        process = ACTIVE_REFRESH_PROCESSES.get(source)
    if not process or process.poll() is not None:
        return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)
    return True


def cancel_refresh(options: dict[str, Any]) -> dict[str, Any]:
    source = str(options.get("source") or "")
    if source not in SOURCE_NAMES:
        raise RuntimeError("source must be one of asc, firebase, or revenueCat.")
    CANCELLED_REFRESH_SOURCES.add(source)
    terminated = terminate_refresh_process(source)
    write_refresh_progress(
        source,
        "cancelled",
        f"{source_label(source)} refresh cancelled.",
        status="error",
    )
    return {
        "status": "ok",
        "source": source,
        "cancelled": True,
        "terminated": terminated,
        "message": "Refresh cancelled." if terminated else "Refresh was not running on the server.",
    }


def refresh_source(options: dict[str, Any]) -> dict[str, Any]:
    source = str(options.get("source") or "")
    if source not in SOURCE_NAMES:
        raise RuntimeError("source must be one of asc, firebase, or revenueCat.")
    CANCELLED_REFRESH_SOURCES.discard(source)
    write_refresh_progress(source, "starting", f"Starting {source} refresh…")
    days = int(options.get("days") or 28)
    chart_days = int(options.get("chartDays") or 90)
    if not CONFIG:
        raise RuntimeError("Dashboard is not configured.")
    temp_output = CONFIG.metrics_root / f"metrics-{source}.json"
    command = [
        *fetch_command(),
        "--days",
        str(days),
        "--chart-days",
        str(chart_days),
        "--output",
        str(temp_output),
        "--refresh-source",
        source,
    ]

    if source == "asc":
        if backfill_days := options.get("backfillDays"):
            command.extend(["--backfill-days", str(int(backfill_days))])
        if options.get("includeSnapshot") is False:
            command.append("--no-include-snapshot")
        if options.get("forceAscCache"):
            command.append("--force-asc-cache")
        command.extend(["--firebase-source", "off", "--skip-revenuecat", "--skip-reviews"])
    elif source == "firebase":
        command.extend(["--skip-fetch", "--firebase-source", str(options.get("firebaseSource") or "auto"), "--skip-revenuecat"])
        if bigquery_project := options.get("bigqueryProject"):
            command.extend(["--bigquery-project", str(bigquery_project)])
        if bigquery_dataset := options.get("bigqueryDataset"):
            command.extend(["--bigquery-dataset", str(bigquery_dataset)])
    else:
        command.extend(["--skip-fetch", "--firebase-source", "off"])

    timeout_seconds = 3000 if source == "asc" else 900
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=growth_subprocess_env(),
    )
    register_refresh_process(source, process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            terminate_refresh_process(source)
            process.communicate(timeout=5)
            write_refresh_progress(
                source,
                "error",
                f"{source_label(source)} refresh timed out after {timeout_seconds // 60} minutes.",
                status="error",
            )
            raise RuntimeError(
                f"{source_label(source)} refresh timed out after {timeout_seconds // 60} minutes. "
                "Partial data may already be in the configured metrics file."
            ) from error

        if source in CANCELLED_REFRESH_SOURCES:
            CANCELLED_REFRESH_SOURCES.discard(source)
            raise RuntimeError(f"{source_label(source)} refresh cancelled.")

        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip() or "unknown refresh failure"
            if source in CANCELLED_REFRESH_SOURCES:
                CANCELLED_REFRESH_SOURCES.discard(source)
                raise RuntimeError(f"{source_label(source)} refresh cancelled.") from None
            raise RuntimeError(detail)

        if not temp_output.exists():
            raise RuntimeError(f"Refresh completed but {display_path(temp_output)} was not written.")
        partial = json.loads(temp_output.read_text(encoding="utf-8"))
        merged = merge_source_metrics(source, partial)
        write_refresh_progress(source, "complete", f"{source_label(source)} refresh finished.", status="ok")
        return {
            "status": "ok",
            "source": source,
            "message": stdout.strip(),
            "metrics": merged,
        }
    except Exception as error:
        if source in CANCELLED_REFRESH_SOURCES:
            CANCELLED_REFRESH_SOURCES.discard(source)
            write_refresh_progress(source, "cancelled", f"{source_label(source)} refresh cancelled.", status="error")
            raise RuntimeError(f"{source_label(source)} refresh cancelled.") from error
        write_refresh_progress(source, "error", str(error), status="error")
        raise
    finally:
        unregister_refresh_process(source, process)


def refresh_reviews(options: dict[str, Any]) -> dict[str, Any]:
    command = [*fetch_command(), "--reviews-only", "--output", str(METRICS_PATH)]
    cache_existing_metrics_snapshot()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=growth_subprocess_env(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown review refresh failure"
        raise RuntimeError(detail)
    if not METRICS_PATH.exists():
        raise RuntimeError("Review refresh completed but metrics JSON was not written.")
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    return {
        "status": "ok",
        "message": completed.stdout.strip(),
        "metrics": metrics,
    }


def merge_source_metrics(source: str, partial: dict[str, Any]) -> dict[str, Any]:
    key_map = {
        "asc": "appStoreConnect",
        "firebase": "firebase",
        "revenueCat": "revenueCat",
    }
    source_key = key_map[source]
    with METRICS_LOCK:
        current = read_metrics_file() or {
            "generatedAt": partial.get("generatedAt"),
            "period": partial.get("period", {}),
            "sources": {},
        }
        cache_metrics_snapshot(current)
        current.setdefault("sources", {})
        current["generatedAt"] = partial.get("generatedAt") or current.get("generatedAt")
        if source == "asc" and partial.get("period"):
            current["period"] = partial["period"]
        current["sources"][source_key] = (partial.get("sources") or {}).get(source_key, {})
        if (partial.get("sources") or {}).get("appStoreListing"):
            current["sources"]["appStoreListing"] = partial["sources"]["appStoreListing"]
        if (partial.get("sources") or {}).get("appStoreReviews"):
            current["sources"]["appStoreReviews"] = partial["sources"]["appStoreReviews"]
        if partial.get("asoMonitor"):
            current["asoMonitor"] = partial["asoMonitor"]
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        METRICS_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        cache_metrics_snapshot(current)
        return current


def cache_existing_metrics_snapshot() -> None:
    metrics = read_metrics_file()
    if metrics:
        cache_metrics_snapshot(metrics)


def cache_metrics_snapshot(metrics: dict[str, Any]) -> None:
    cache_date = metrics_snapshot_date(metrics)
    if not cache_date:
        return
    snapshot_dir = DAILY_METRICS_CACHE_DIR / cache_date
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / "metrics.json"
    snapshot_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def metrics_snapshot_date(metrics: dict[str, Any]) -> str | None:
    generated_at = str(metrics.get("generatedAt") or "")
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", generated_at)
    if match:
        return match.group(1)
    return None


def status_payload() -> dict[str, Any]:
    if not CONFIG:
        return {"status": "error", "message": "Dashboard is not configured."}
    credentials = resolve_google_credentials_path()
    dataset = os.environ.get("FIREBASE_BQ_DATASET") or CONFIG.firebase_dataset
    project_id = os.environ.get("FIREBASE_BQ_PROJECT_ID") or CONFIG.firebase_project_id
    metrics = read_metrics_file()
    ga4_probe: dict[str, Any] = {"status": "not_checked"}
    if credentials and Path(credentials).exists():
        sys.path.insert(0, str(FETCH_SCRIPT.parent))
        from fetch_metrics import probe_ga4_export

        ga4_probe = probe_ga4_export(project_id, dataset)
    asc_source = (metrics.get("sources", {}) if metrics else {}).get("appStoreConnect", {})
    firebase_source = (metrics.get("sources", {}) if metrics else {}).get("firebase", {})
    revenuecat_source = (metrics.get("sources", {}) if metrics else {}).get("revenueCat", {})
    revenuecat_status = revenuecat_source.get("status", "needs_setup")
    app_version = latest_asc_app_version_status()
    return {
        "status": "ok",
        "app": CONFIG.public_payload()["app"],
        "metricsPath": display_path(METRICS_PATH),
        "metricsExists": METRICS_PATH.exists(),
        "dashboardPath": DASHBOARD_URL_PATH,
        "asc": {
            "appId": CONFIG.app_store_id,
            "usesAscCli": True,
            "bypassKeychain": True,
            "ready": asc_source.get("status") == "ok",
            "note": asc_source.get("note"),
            "reports": asc_source.get("reports", {}),
        },
        "firebase": {
            "projectId": project_id,
            "dataset": dataset or ga4_probe.get("dataset"),
            "credentialsPath": credentials,
            "credentialsExists": bool(credentials and Path(credentials).exists()),
            "ready": bool(ga4_probe.get("available")),
            "datasetAutoDiscovery": dataset is None,
            "ga4": ga4_probe,
            "sourceStatus": firebase_source.get("status"),
            "note": firebase_source.get("note"),
        },
        "revenueCat": {
            "ready": revenuecat_status in {"ok", "partial", "connected"},
            "status": revenuecat_status,
            "projectId": os.environ.get("REVENUECAT_PROJECT_ID") or revenuecat_source.get("projectId"),
            "credentialsPresent": bool(os.environ.get("REVENUECAT_API_KEY") or os.environ.get("REVENUECAT_SECRET_API_KEY")),
            "note": revenuecat_source.get("note") or "Add RevenueCat API credentials to track subscription health.",
        },
        "appVersion": app_version,
    }


def read_metrics_file() -> dict[str, Any]:
    if not METRICS_PATH.exists():
        return {}
    try:
        return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def project_marketing_version() -> str | None:
    project_file = CONFIG.project_file if CONFIG else None
    if not project_file or not project_file.exists():
        return None
    text = project_file.read_text(encoding="utf-8", errors="ignore")
    if project_file.suffix == ".xcproj":
        versions = re.findall(r'"MARKETING_VERSION(?:\[[^\]"\n]+\])*"\s*:\s*"([^"\n]+)"', text)
    else:
        versions = re.findall(r"MARKETING_VERSION = ([^;]+);", text)
    cleaned = [value.strip().strip('"') for value in versions if value.strip()]
    if not cleaned:
        return None
    # Prefer the highest marketing version in the project (main app target is usually newest).
    return max(cleaned, key=version_sort_key)


def version_sort_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in re.split(r"[^\d]+", value):
        if piece.isdigit():
            parts.append(int(piece))
    return tuple(parts or [0])


def latest_asc_app_version_status() -> dict[str, Any]:
    project_version = project_marketing_version()
    if CONFIG and not CONFIG.live_status_enabled:
        state = CONFIG.configured_version_state
        return {
            "version": CONFIG.configured_version or project_version,
            "state": state,
            "label": human_state(state),
            "source": "config",
        }
    payload: dict[str, Any] = {
        "version": project_version,
        "state": "unknown",
        "label": "Unknown",
        "source": "project",
    }
    try:
        completed = subprocess.run(
            [
                "asc",
                "versions",
                "list",
                "--app",
                CONFIG.app_store_id if CONFIG else "",
                "--platform",
                CONFIG.platform if CONFIG else "IOS",
                "--output",
                "json",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            env=growth_subprocess_env(),
            check=False,
        )
        if completed.returncode != 0:
            payload["note"] = completed.stderr.strip() or completed.stdout.strip() or "ASC version lookup failed."
            return payload
        data = json.loads(completed.stdout or "{}").get("data", [])
        if not data:
            payload["label"] = "No ASC versions"
            return payload
        item = latest_asc_version_item(data)
        attrs = item.get("attributes", {}) if item else {}
        state = attrs.get("appStoreState") or attrs.get("appVersionState") or "unknown"
        version_string = attrs.get("versionString") or project_version
        payload.update(
            {
                "version": version_string,
                "state": state,
                "label": human_state(state),
                "source": "app_store_connect",
                "createdDate": attrs.get("createdDate"),
                "releaseType": attrs.get("releaseType"),
                "projectVersion": project_version,
            }
        )
    except Exception as error:
        payload["note"] = str(error)
    return payload


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def latest_asc_version_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None

    def sort_key(item: dict[str, Any]) -> tuple[str, tuple[int, ...]]:
        attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
        created = str(attrs.get("createdDate") or "")
        version = str(attrs.get("versionString") or "")
        return (created, version_sort_key(version))

    return max(items, key=sort_key)


def source_label(source: str) -> str:
    if source == "asc":
        return "App Store Connect"
    if source == "firebase":
        return "Firebase / BigQuery"
    return "RevenueCat"


def read_refresh_progress() -> dict[str, Any]:
    if not REFRESH_PROGRESS_PATH.exists():
        return {"status": "idle", "processRunning": False}
    try:
        payload = json.loads(REFRESH_PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "idle", "processRunning": False}
    if not isinstance(payload, dict):
        return {"status": "idle", "processRunning": False}
    payload.setdefault("status", "running")
    source = str(payload.get("source") or "")
    with PROCESS_LOCK:
        process = ACTIVE_REFRESH_PROCESSES.get(source)
    process_running = bool(process and process.poll() is None)
    payload["processRunning"] = process_running
    if payload.get("status") == "running" and not process_running:
        from datetime import datetime, timezone

        updated_at = payload.get("updatedAt")
        stale = True
        if isinstance(updated_at, str):
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                stale = (datetime.now(timezone.utc) - updated).total_seconds() > 120
            except ValueError:
                stale = True
        if stale:
            payload["status"] = "stale"
    return payload


def write_refresh_progress(source: str, step: str, detail: str, **extra: Any) -> None:
    from datetime import datetime, timezone

    REFRESH_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    existing: dict[str, Any] = {}
    if REFRESH_PROGRESS_PATH.exists():
        try:
            existing = json.loads(REFRESH_PROGRESS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    started_at = now if step == "starting" else existing.get("startedAt") or now
    payload = {
        "source": source,
        "step": step,
        "detail": detail,
        "startedAt": started_at,
        "updatedAt": now,
        "status": extra.pop("status", "running"),
        **extra,
    }
    REFRESH_PROGRESS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_refresh_progress() -> None:
    if REFRESH_PROGRESS_PATH.exists():
        REFRESH_PROGRESS_PATH.unlink()


def human_state(state: str) -> str:
    normalized = str(state or "unknown").upper()
    known = {
        "IN_REVIEW": "In Review",
        "WAITING_FOR_REVIEW": "Waiting for Review",
        "PENDING_DEVELOPER_RELEASE": "Pending Developer Release",
        "PENDING_APPLE_RELEASE": "Pending Apple Release",
        "READY_FOR_SALE": "Ready for Sale",
        "READY_FOR_DISTRIBUTION": "Ready for Distribution",
        "PREPARE_FOR_SUBMISSION": "Prepare for Submission",
        "DEVELOPER_REJECTED": "Developer Rejected",
        "REJECTED": "Rejected",
        "METADATA_REJECTED": "Metadata Rejected",
        "INVALID_BINARY": "Invalid Binary",
        "REPLACED_WITH_NEW_VERSION": "Replaced with New Version",
        "PROCESSING_FOR_DISTRIBUTION": "Processing for Distribution",
    }
    if normalized in known:
        return known[normalized]
    words = normalized.replace("_", " ").lower().split()
    minor = {"for", "of", "the", "and", "or"}
    return " ".join(word if index and word in minor else word.capitalize() for index, word in enumerate(words))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve an app metrics dashboard locally.")
    parser.add_argument("--config", type=Path, required=True, help="Path to an app dashboard JSON config.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_METRICS_DASHBOARD_PORT", "8765")))
    args = parser.parse_args()

    config = load_config(args.config)
    configure_runtime(config)
    port = args.port
    server = ThreadingHTTPServer(("127.0.0.1", port), GrowthDashboardHandler)
    credentials = resolve_google_credentials_path()
    url = f"http://127.0.0.1:{port}{DASHBOARD_URL_PATH}"
    print(f"Serving {config.app_name} Metrics Dashboard at {url}")
    print("API: /api/config, /api/metrics, /api/status, /api/refresh-source, …")
    if credentials:
        print(f"BigQuery credentials: {credentials}")
    else:
        print("BigQuery credentials: not found (configure firebase.credentialFiles or GOOGLE_APPLICATION_CREDENTIALS)")
    if VENV_PYTHON.is_file():
        print(f"Using Python: {VENV_PYTHON}")
    else:
        print(
            "Tip: python3 -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
