#!/usr/bin/env python3
"""Fetch and summarize metrics for a configured mobile app."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
import requests

from aso_monitor import build_aso_monitor_payload
from credentials import apply_google_credentials_default, growth_subprocess_env
from dashboard_config import CONFIG_ENV_VAR, DashboardConfig, load_config


APP_ID = ""
FIREBASE_PROJECT_ID = ""
REVENUECAT_API_BASE = "https://api.revenuecat.com/v2"
REPORTS = {
    "downloads": "App Downloads Standard",
    "install_performance": "App Install Performance",
    "install_delete": "App Store Installation and Deletion Standard",
    "sessions": "App Sessions Standard",
    "purchases": "App Store Purchases Standard",
    "discovery_engagement": "App Store Discovery and Engagement Standard",
    "web_preview_engagement": "App Store Web Preview Engagement Standard",
    "subscription_events": "App Store Subscription Event Report Standard",
    "subscription_state": "App Store Subscription State Report Standard",
    "retention_messaging": "Retention Messaging",
    "platform_installs": "Platform App Installs",
}
DEFAULT_OUTPUT = Path("build/AppMetrics/metrics.json")
DEFAULT_METRICS_PATH = DEFAULT_OUTPUT
RAW_DIR = Path("build/AppMetrics/raw")
REFRESH_PROGRESS_PATH = Path("build/AppMetrics/refresh-progress.json")
MEANINGFUL_ACTIVATION_VALUE = 1
FIRST_OPEN_EVENT = "first_open"
PERMISSION_PROMPT_EVENT = "permission_prompt_viewed"
PERMISSION_RESULT_EVENT = "permission_result"
PERMISSION_STATUS_PARAMETER = "status"
PERMISSION_ACCEPTED_VALUES = ["authorized"]
INITIAL_LOAD_EVENT = "initial_load_completed"
ACTIVATION_EVENT = "activation_completed"
ACTIVATION_VALUE_PARAMETER = "value"
PAYWALL_EVENT = "paywall_presented"
PRIMARY_FEATURE_EVENT = "primary_feature_used"
SECONDARY_FEATURE_EVENT = "secondary_feature_used"
DEFAULT_CHART_DAYS = 90
DEFAULT_COMPARISON_DAYS = 28
PROCESSING_DATE_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ASC_CACHE_MARKER = ".asc-cache.json"
# Minimum reports required before a processing-date slice is treated as complete.
REQUIRED_ONGOING_REPORT_KEYS = ("downloads", "discovery_engagement")
REQUIRED_SNAPSHOT_REPORT_KEYS = ("downloads",)
ALL_REPORT_KEYS = tuple(REPORTS.keys())


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch app growth reports and build dashboard JSON.")
    parser.add_argument("--config", type=Path, help="Path to an app dashboard JSON config.")
    parser.add_argument("--app-id", help="Override the configured App Store app id.")
    parser.add_argument("--request-id")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_COMPARISON_DAYS,
        help="Comparison window for KPI deltas (current vs prior period).",
    )
    parser.add_argument(
        "--chart-days",
        type=int,
        default=DEFAULT_CHART_DAYS,
        help="Maximum ASC/GA4 daily points to retain for charts (uses all CSV history up to this cap).",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=14,
        help="Try ASC downloads for each processing date in the last N days (merges into daily series).",
    )
    parser.add_argument(
        "--include-snapshot",
        action="store_true",
        default=True,
        help="Also fetch ONE_TIME_SNAPSHOT ASC reports when instances are available.",
    )
    parser.add_argument(
        "--no-include-snapshot",
        action="store_false",
        dest="include_snapshot",
        help="Skip ONE_TIME_SNAPSHOT ASC fetch.",
    )
    parser.add_argument("--skip-fetch", action="store_true", help="Only parse existing CSV files.")
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--firebase-json", type=Path, help="Optional Firebase/GA4 export to merge.")
    parser.add_argument("--firebase-source", choices=["auto", "bigquery", "json", "off"], default="auto")
    parser.add_argument("--skip-revenuecat", action="store_true", help="Skip RevenueCat API calls.")
    parser.add_argument("--skip-reviews", action="store_true", help="Load App Store ratings summary only (skip written reviews).")
    parser.add_argument(
        "--reviews-only",
        action="store_true",
        help="Fetch written App Store reviews into an existing metrics JSON file (see --output).",
    )
    parser.add_argument("--bigquery-project")
    parser.add_argument("--bigquery-dataset")
    parser.add_argument("--processing-date", default=date.today().isoformat())
    parser.add_argument(
        "--refresh-source",
        choices=["asc", "firebase", "revenueCat"],
        help="When set, writes refresh-progress.json steps for the dashboard while this source runs.",
    )
    parser.add_argument(
        "--force-asc-cache",
        action="store_true",
        help="Re-download ASC processing-date slices even when a complete local cache exists.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    configure_runtime(config)
    args.app_id = args.app_id or APP_ID
    args.raw_dir = args.raw_dir or RAW_DIR
    args.output = args.output or DEFAULT_OUTPUT
    args.bigquery_project = args.bigquery_project or os.environ.get("FIREBASE_BQ_PROJECT_ID") or FIREBASE_PROJECT_ID
    args.bigquery_dataset = args.bigquery_dataset or os.environ.get("FIREBASE_BQ_DATASET") or config.firebase_dataset

    apply_google_credentials_default()

    if args.reviews_only:
        if not args.output.exists():
            raise SystemExit(f"Metrics file not found: {args.output}")
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        payload.setdefault("sources", {})
        payload["sources"]["appStoreReviews"] = summarize_app_store_reviews(args.app_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(str(args.output))
        return 0

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_fetch:
        if args.refresh_source == "asc":
            write_refresh_progress("asc", "prepare", "Preparing App Store Connect analytics refresh…")
        interim_path = DEFAULT_METRICS_PATH if args.refresh_source == "asc" else None
        fetch_all_asc_reports(
            args.raw_dir,
            args.app_id,
            args.request_id,
            args.processing_date,
            args.backfill_days,
            args.include_snapshot,
            refresh_source=args.refresh_source,
            comparison_days=args.days,
            chart_days=args.chart_days,
            interim_metrics_path=interim_path,
            force_cache_refresh=args.force_asc_cache,
        )

    if args.refresh_source == "asc":
        write_refresh_progress("asc", "parse", "Parsing ASC reports and building daily metrics…")
    elif args.refresh_source == "firebase":
        write_refresh_progress("firebase", "query", "Querying Firebase / GA4 from BigQuery…")
    elif args.refresh_source == "revenueCat":
        write_refresh_progress("revenueCat", "fetch", "Fetching RevenueCat overview and charts…")

    payload = build_payload(
        args.raw_dir,
        args.days,
        args.chart_days,
        args.firebase_json,
        args.firebase_source,
        args.bigquery_project,
        args.bigquery_dataset,
        args.skip_revenuecat,
        args.skip_reviews,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(args.output))
    return 0


def configure_runtime(config: DashboardConfig) -> None:
    """Apply one app config to this fetch process."""
    global APP_ID, FIREBASE_PROJECT_ID
    global DEFAULT_OUTPUT, DEFAULT_METRICS_PATH, RAW_DIR, REFRESH_PROGRESS_PATH
    global MEANINGFUL_ACTIVATION_VALUE
    global FIRST_OPEN_EVENT, PERMISSION_PROMPT_EVENT, PERMISSION_RESULT_EVENT
    global PERMISSION_STATUS_PARAMETER, PERMISSION_ACCEPTED_VALUES, INITIAL_LOAD_EVENT
    global ACTIVATION_EVENT, ACTIVATION_VALUE_PARAMETER, PAYWALL_EVENT
    global PRIMARY_FEATURE_EVENT, SECONDARY_FEATURE_EVENT

    os.environ[CONFIG_ENV_VAR] = str(config.path)
    APP_ID = config.app_store_id
    FIREBASE_PROJECT_ID = config.firebase_project_id
    DEFAULT_OUTPUT = config.metrics_path
    DEFAULT_METRICS_PATH = config.metrics_path
    RAW_DIR = config.metrics_root / "raw"
    REFRESH_PROGRESS_PATH = config.metrics_root / "refresh-progress.json"
    MEANINGFUL_ACTIVATION_VALUE = int(config.analytics.get("meaningfulActivationMinimum") or 0)
    FIRST_OPEN_EVENT = config.analytics_identifier("firstOpenEvent", "first_open")
    PERMISSION_PROMPT_EVENT = config.analytics_identifier("permissionPromptEvent", "permission_prompt_viewed")
    PERMISSION_RESULT_EVENT = config.analytics_identifier("permissionResultEvent", "permission_result")
    PERMISSION_STATUS_PARAMETER = config.analytics_identifier("permissionStatusParameter", "status")
    PERMISSION_ACCEPTED_VALUES = config.analytics_values("permissionAcceptedValues", ["authorized"])
    INITIAL_LOAD_EVENT = config.analytics_identifier("initialLoadEvent", "initial_load_completed")
    ACTIVATION_EVENT = config.analytics_identifier("activationEvent", "activation_completed")
    ACTIVATION_VALUE_PARAMETER = config.analytics_identifier("activationValueParameter", "value")
    PAYWALL_EVENT = config.analytics_identifier("paywallEvent", "paywall_presented")
    PRIMARY_FEATURE_EVENT = config.analytics_identifier("primaryFeatureEvent", "primary_feature_used")
    SECONDARY_FEATURE_EVENT = config.analytics_identifier("secondaryFeatureEvent", "secondary_feature_used")


def ensure_asc_request(app_id: str, access_type: str = "ONGOING") -> str | None:
    command = [
        "asc",
        "analytics",
        "request",
        "--app",
        app_id,
        "--access-type",
        access_type,
        "--reuse-existing",
        "--output",
        "json",
        "--pretty",
    ]
    last_error: RuntimeError | None = None
    for attempt in range(2):
        try:
            result = run_json(command)
            request_id = result.get("requestId")
            if not request_id:
                return None
            return str(request_id)
        except RuntimeError as error:
            last_error = error
            detail = str(error).lower()
            if attempt == 0 and ("deadline exceeded" in detail or "context canceled" in detail):
                continue
            raise
    if last_error:
        raise last_error
    return None


def publish_interim_asc_metrics(
    raw_dir: Path,
    days: int,
    chart_days: int,
    metrics_path: Path = DEFAULT_METRICS_PATH,
) -> None:
    """Merge freshly parsed ASC rows into the dashboard metrics file while a refresh is running."""
    asc = summarize_asc(raw_dir, days, chart_days)
    if asc.get("status") != "ok":
        return
    listing = summarize_app_store_listing(APP_ID)
    existing: dict[str, Any] = {}
    if metrics_path.exists():
        try:
            existing = json.loads(metrics_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    firebase = (existing.get("sources") or {}).get("firebase") or {"status": "off", "metrics": {}}
    firebase_for_aso = firebase if firebase.get("status") == "ok" else {"metrics": {}}
    aso = build_aso_monitor_payload(asc, listing, firebase_for_aso)
    current = existing or {"sources": {}}
    current.setdefault("sources", {})
    current["generatedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if asc.get("period"):
        current["period"] = asc["period"]
    current["sources"]["appStoreConnect"] = asc
    current["asoMonitor"] = aso
    if listing.get("status") == "ok":
        current["sources"]["appStoreListing"] = listing
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_refresh_progress(source: str, step: str, detail: str, **extra: Any) -> None:
    REFRESH_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    existing: dict[str, Any] = {}
    if REFRESH_PROGRESS_PATH.exists():
        try:
            existing = json.loads(REFRESH_PROGRESS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    started_at = now if step in {"starting", "prepare"} else existing.get("startedAt") or now
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


def processing_date_cache_dir(label_dir: Path, processing_date: str) -> Path:
    return label_dir / processing_date


def required_report_keys_for_label(label: str) -> tuple[str, ...]:
    return REQUIRED_ONGOING_REPORT_KEYS if label == "ongoing" else REQUIRED_SNAPSHOT_REPORT_KEYS


def report_export_paths(cache_dir: Path, key: str) -> list[Path]:
    return sorted(cache_dir.glob(f"{key}*.csv")) + sorted(cache_dir.glob(f"{key}*.csv.gz"))


def is_report_cached(cache_dir: Path, key: str) -> bool:
    return any(path.is_file() and path.stat().st_size > 0 for path in report_export_paths(cache_dir, key))


def reports_present_in_dir(cache_dir: Path) -> list[str]:
    return [key for key in ALL_REPORT_KEYS if is_report_cached(cache_dir, key)]


def is_processing_cache_complete(
    cache_dir: Path,
    *,
    require_discovery: bool = False,
    label: str | None = None,
) -> bool:
    if not cache_dir.is_dir():
        return False
    resolved_label = label or ("ongoing" if require_discovery else "snapshot")
    for key in required_report_keys_for_label(resolved_label):
        if not is_report_cached(cache_dir, key):
            return False
    marker = cache_dir / ASC_CACHE_MARKER
    if not marker.exists():
        return True
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    reports = payload.get("reports")
    if not isinstance(reports, list) or not reports:
        return True
    if require_discovery and "discovery_engagement" not in reports:
        return False
    for key in reports:
        if key in ALL_REPORT_KEYS and not is_report_cached(cache_dir, key):
            return False
    return True


def write_processing_cache_marker(cache_dir: Path, processing_date: str, reports: list[str]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "processingDate": processing_date,
        "reports": sorted(set(reports)),
        "cachedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    (cache_dir / ASC_CACHE_MARKER).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def list_asc_processing_dirs(raw_dir: Path) -> list[Path]:
    """All complete ASC cache slices, newest processing date first."""
    dirs: list[Path] = []
    for label in ("ongoing", "snapshot"):
        label_dir = raw_dir / label
        if not label_dir.is_dir():
            continue
        for item in label_dir.iterdir():
            require_discovery = label == "ongoing"
            if item.is_dir() and PROCESSING_DATE_DIR_PATTERN.match(item.name) and is_processing_cache_complete(
                item, require_discovery=require_discovery, label=label
            ):
                dirs.append(item)
    return sorted(dirs, key=lambda item: item.name, reverse=True)


def asc_report_roots(raw_dir: Path) -> list[Path]:
    """Backward-compatible alias: all cached processing slices."""
    roots = list_asc_processing_dirs(raw_dir)
    return roots or [raw_dir]


def read_rows_from_roots(roots: list[Path], pattern: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for root in roots:
        rows.extend(read_rows(root, pattern))
    return rows


ASC_SLICE_ROW_GROUPS = (
    ("downloads", "downloads*.csv"),
    ("install_performance", "install_performance*.csv"),
    ("install_delete", "install_delete*.csv"),
    ("sessions", "sessions*.csv"),
    ("purchases", "purchases*.csv"),
    ("discovery_engagement", "discovery_engagement*.csv"),
    ("web_preview_engagement", "web_preview_engagement*.csv"),
    ("subscription_events", "subscription_events*.csv"),
    ("subscription_state", "subscription_state*.csv"),
    ("retention_messaging", "retention_messaging*.csv"),
    ("platform_installs", "platform_installs*.csv"),
)

ASC_DAILY_MERGE_FIELDS = (
    "firstTimeDownloads",
    "downloads",
    "installs",
    "firstTimeInstallSuccess",
    "installFailures",
    "deletions",
    "sessions",
    "sessionSeconds",
    "impressions",
    "uniqueImpressions",
    "taps",
    "uniqueTaps",
    "productPageViews",
    "uniqueProductPageViews",
    "webProductPageViews",
    "purchases",
    "salesUsd",
    "proceedsUsd",
    "subscriptionEvents",
    "subscriptionStateRows",
    "platformInstalls",
)


def read_asc_slice_row_groups(cache_dir: Path) -> dict[str, list[dict[str, str]]]:
    return {key: read_rows(cache_dir, pattern) for key, pattern in ASC_SLICE_ROW_GROUPS}


def populate_daily_from_row_groups(
    rows: dict[str, list[dict[str, str]]],
    daily: dict[str, dict[str, Any]],
) -> None:
    for row in rows["downloads"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        value = number(row.get("Counts"))
        entry = daily_entry(daily, day)
        entry["downloads"] += value
        if row.get("Download Type") == "First-time download":
            entry["firstTimeDownloads"] += value

    for row in rows["install_delete"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        value = number(row.get("Counts"))
        entry = daily_entry(daily, day)
        if row.get("Event") == "Install":
            entry["installs"] += value
        elif row.get("Event") == "Delete":
            entry["deletions"] += value

    for row in rows["install_performance"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        value = number(row.get("Counts"))
        entry = daily_entry(daily, day)
        if row.get("Download Type") == "First-time download" and row.get("Install Status") == "Success":
            entry["firstTimeInstallSuccess"] += value
        elif row.get("Install Status") and row.get("Install Status") != "Success":
            entry["installFailures"] += value

    for row in rows["sessions"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        entry = daily_entry(daily, day)
        entry["sessions"] += number(row.get("Sessions"))
        entry["sessionSeconds"] += number(row.get("Total Session Duration"))

    for row in rows["purchases"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        entry = daily_entry(daily, day)
        entry["purchases"] += number(row.get("Purchases"))
        entry["salesUsd"] += number(row.get("Sales in USD"))
        entry["proceedsUsd"] += number(row.get("Proceeds in USD"))

    for row in rows["discovery_engagement"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        value = number(row.get("Counts"))
        unique_value = number(row.get("Unique Counts"))
        entry = daily_entry(daily, day)
        event = row.get("Event")
        if event == "Impression":
            entry["impressions"] += value
            entry["uniqueImpressions"] += unique_value
        elif event == "Tap":
            entry["taps"] += value
            entry["uniqueTaps"] += unique_value
        elif event == "Page view":
            entry["productPageViews"] += value
            entry["uniqueProductPageViews"] += unique_value

    for row in rows["web_preview_engagement"]:
        day = parse_day(row.get("Date"))
        if day and row.get("Event") == "Page view":
            daily_entry(daily, day)["webProductPageViews"] += number(row.get("Counts"))

    for row in rows["subscription_events"]:
        day = parse_day(row.get("Date"))
        if not day:
            continue
        entry = daily_entry(daily, day)
        event = (row.get("Event") or row.get("Subscription Event") or "").lower()
        value = number(row.get("Counts") or row.get("Subscriptions") or row.get("Events"))
        if "start" in event or "new" in event or "trial" in event:
            entry["subscriptionEvents"] += value

    for row in rows["subscription_state"]:
        day = parse_day(row.get("Date"))
        if day:
            daily_entry(daily, day)["subscriptionStateRows"] += 1

    for row in rows["platform_installs"]:
        day = parse_day(row.get("Install Day"))
        if day:
            daily_entry(daily, day)["platformInstalls"] += number(row.get("Installs"))


def build_daily_from_slice(cache_dir: Path) -> dict[str, dict[str, Any]]:
    daily: dict[str, dict[str, Any]] = {}
    populate_daily_from_row_groups(read_asc_slice_row_groups(cache_dir), daily)
    return daily


def merge_slice_dailies(slices: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Merge calendar days across ASC processing slices, keeping the largest value per metric."""
    merged: dict[str, dict[str, Any]] = {}
    for slice_daily in slices:
        for day, entry in slice_daily.items():
            target = merged.setdefault(day, {"date": day})
            for field in ASC_DAILY_MERGE_FIELDS:
                value = number(entry.get(field))
                if value > number(target.get(field)):
                    target[field] = value
    return merged


def merge_download_attribution(
    slice_row_groups: list[dict[str, list[dict[str, str]]]],
    start: date,
    end: date,
) -> tuple[Counter[str], Counter[str]]:
    source_split: Counter[str] = Counter()
    territory_split: Counter[str] = Counter()
    filled: set[tuple[str, str, str]] = set()
    for rows in slice_row_groups:
        for row in rows.get("downloads", []):
            day = parse_day(row.get("Date"))
            if not day or not (start <= day <= end):
                continue
            if row.get("Download Type") != "First-time download":
                continue
            slot = (day.isoformat(), row.get("Source Type") or "Unknown", row.get("Territory") or "Unknown")
            if slot in filled:
                continue
            value = number(row.get("Counts"))
            source_split[slot[1]] += value
            territory_split[slot[2]] += value
            filled.add(slot)
    return source_split, territory_split


def fetch_all_asc_reports(
    raw_dir: Path,
    app_id: str,
    request_id: str | None,
    processing_date: str,
    backfill_days: int,
    include_snapshot: bool,
    refresh_source: str | None = None,
    *,
    comparison_days: int = DEFAULT_COMPARISON_DAYS,
    chart_days: int = DEFAULT_CHART_DAYS,
    interim_metrics_path: Path | None = None,
    force_cache_refresh: bool = False,
) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    request_ids: list[tuple[str, str]] = []
    ongoing_id = request_id or ensure_asc_request(app_id, "ONGOING")
    if ongoing_id:
        request_ids.append(("ongoing", ongoing_id))
    if include_snapshot:
        snapshot_id = ensure_asc_request(app_id, "ONE_TIME_SNAPSHOT")
        if snapshot_id and snapshot_id != ongoing_id:
            request_ids.append(("snapshot", snapshot_id))

    ongoing_rid = request_ids[0][1] if request_ids else ongoing_id or ""
    if refresh_source == "asc":
        write_refresh_progress("asc", "discover_dates", "Resolving ASC processing dates (cache + API)…")
    processing_dates, skipped_discover = resolve_processing_dates(
        raw_dir,
        "ongoing",
        ongoing_rid,
        processing_date,
        backfill_days,
        force_cache_refresh=force_cache_refresh,
    )
    total_dates = len(processing_dates)
    if refresh_source == "asc":
        discover_note = (
            f" ({skipped_discover} from cache, {total_dates - skipped_discover} checked via API)"
            if skipped_discover
            else ""
        )
        write_refresh_progress(
            "asc",
            "discover_dates",
            f"Found {total_dates} ASC processing date(s) to check{discover_note}…",
            processingTotal=total_dates,
            cachedSlices=skipped_discover,
        )
    skipped_cached = 0
    for label, rid in request_ids:
        label_dir = raw_dir / label
        require_discovery = label == "ongoing"
        for index, date_value in enumerate(processing_dates, start=1):
            cache_dir = processing_date_cache_dir(label_dir, date_value)
            if not force_cache_refresh and is_processing_cache_complete(
                cache_dir,
                require_discovery=require_discovery,
                label=label,
            ):
                skipped_cached += 1
                if refresh_source == "asc":
                    write_refresh_progress(
                        "asc",
                        "fetch_reports",
                        f"Using cached ASC slice for {date_value} ({index}/{total_dates})…",
                        processingDate=date_value,
                        processingIndex=index,
                        processingTotal=total_dates,
                        requestLabel=label,
                        cachedSlices=skipped_cached,
                    )
                continue
            if refresh_source == "asc":
                write_refresh_progress(
                    "asc",
                    "fetch_reports",
                    f"Downloading ASC reports for {date_value} ({index}/{total_dates})…",
                    processingDate=date_value,
                    processingIndex=index,
                    processingTotal=total_dates,
                    requestLabel=label,
                    cachedSlices=skipped_cached,
                )
            try:
                downloaded_reports = fetch_asc_reports(
                    cache_dir,
                    rid,
                    date_value,
                    refresh_source=refresh_source,
                )
                present_reports = sorted(set(reports_present_in_dir(cache_dir) + downloaded_reports))
                if present_reports:
                    write_processing_cache_marker(cache_dir, date_value, present_reports)
            except RuntimeError as error:
                if "no instances found" in str(error).lower():
                    continue
                if "output file already exists" in str(error).lower():
                    continue
                raise
            if refresh_source == "asc" and interim_metrics_path:
                publish_interim_asc_metrics(
                    raw_dir,
                    comparison_days,
                    chart_days,
                    interim_metrics_path,
                )
    if refresh_source == "asc" and skipped_cached:
        write_refresh_progress(
            "asc",
            "fetch_reports",
            f"Skipped {skipped_cached} cached ASC slice(s); downloaded only missing days.",
            cachedSlices=skipped_cached,
        )


def backfill_candidate_dates(processing_date: str, backfill_days: int) -> list[str]:
    anchor = parse_day(processing_date) or date.today()
    return [(anchor - timedelta(days=offset)).isoformat() for offset in range(max(backfill_days, 1))]


def resolve_processing_dates(
    raw_dir: Path,
    label: str,
    request_id: str,
    processing_date: str,
    backfill_days: int,
    *,
    force_cache_refresh: bool = False,
) -> tuple[list[str], int]:
    """Return processing dates in the backfill window, skipping API discovery for complete cache slices."""
    label_dir = raw_dir / label
    require_discovery = label == "ongoing"
    from_cache: list[str] = []
    need_api: list[str] = []
    for date_value in backfill_candidate_dates(processing_date, backfill_days):
        cache_dir = processing_date_cache_dir(label_dir, date_value)
        if not force_cache_refresh and is_processing_cache_complete(
            cache_dir,
            require_discovery=require_discovery,
            label=label,
        ):
            from_cache.append(date_value)
        else:
            need_api.append(date_value)
    discovered = discover_processing_dates_from_api(request_id, need_api)
    all_dates = sorted(set(from_cache + discovered), reverse=True)
    return all_dates, len(from_cache)


def discover_processing_dates(request_id: str, processing_date: str, backfill_days: int) -> list[str]:
    """Backward-compatible wrapper: always queries ASC for every backfill candidate."""
    candidates = backfill_candidate_dates(processing_date, backfill_days)
    discovered = discover_processing_dates_from_api(request_id, candidates)
    return discovered or [processing_date]


def discover_processing_dates_from_api(request_id: str, candidates: list[str]) -> list[str]:
    if not request_id:
        return list(candidates)
    discovered: list[str] = []
    for candidate in candidates:
        try:
            view = run_json([
                "asc",
                "analytics",
                "view",
                "--request-id",
                request_id,
                "--date",
                candidate,
                "--output",
                "json",
                "--pretty",
                "--paginate",
            ])
            has_instances = any(
                (item.get("instances") or [])
                for item in view.get("data", [])
                if isinstance(item, dict)
            )
            if has_instances:
                discovered.append(candidate)
        except RuntimeError:
            continue
    return discovered


def fetch_asc_reports(
    raw_dir: Path,
    request_id: str,
    processing_date: str,
    refresh_source: str | None = None,
) -> list[str]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloaded_reports: list[str] = []
    view = run_json([
        "asc",
        "analytics",
        "view",
        "--request-id",
        request_id,
        "--date",
        processing_date,
        "--output",
        "json",
        "--pretty",
        "--paginate",
    ])
    by_name = {item.get("name"): item for item in view.get("data", []) if isinstance(item, dict)}
    report_items = [(key, report_name) for key, report_name in REPORTS.items() if by_name.get(report_name)]
    if not report_items:
        return downloaded_reports
    for report_index, (key, report_name) in enumerate(report_items, start=1):
        report = by_name.get(report_name)
        if not report:
            continue
        instances = report.get("instances") or []
        if not instances:
            continue
        instance_id = instances[0].get("id")
        if not instance_id:
            continue
        if refresh_source == "asc":
            write_refresh_progress(
                "asc",
                "download_report",
                f"Downloading {report_name} for {processing_date} ({report_index}/{len(report_items)})…",
                reportName=report_name,
                reportIndex=report_index,
                reportTotal=len(report_items),
                processingDate=processing_date,
            )
        links = run_json([
            "asc",
            "analytics",
            "instances",
            "links",
            "--instance-id",
            str(instance_id),
            "--output",
            "json",
            "--pretty",
            "--paginate",
        ])
        for index, segment in enumerate(links.get("data", []), start=1):
            segment_id = segment.get("id")
            if not segment_id:
                continue
            suffix = f"-{index}" if len(links.get("data", [])) > 1 else ""
            output_csv = raw_dir / f"{key}{suffix}.csv"
            gz_path = Path(f"{output_csv}.gz")
            if (output_csv.exists() and output_csv.stat().st_size > 0) or (
                gz_path.exists() and gz_path.stat().st_size > 0
            ):
                if key not in downloaded_reports:
                    downloaded_reports.append(key)
                continue
            remove_existing_report(output_csv)
            run_json([
                "asc",
                "analytics",
                "download",
                "--request-id",
                request_id,
                "--instance-id",
                str(instance_id),
                "--segment-id",
                str(segment_id),
                "--decompress",
                "--output",
                str(output_csv),
                "--output-format",
                "json",
                "--pretty",
            ])
        if key not in downloaded_reports:
            downloaded_reports.append(key)
    return downloaded_reports


def remove_existing_report(output_csv: Path) -> None:
    for candidate in (output_csv, Path(f"{output_csv}.gz")):
        if candidate.exists():
            candidate.unlink()


def run_json(command: list[str]) -> dict[str, Any]:
    env = growth_subprocess_env() if command and command[0] == "asc" else os.environ.copy()
    if command and command[0] != "asc":
        env.setdefault("ASC_BYPASS_KEYCHAIN", "1")
    completed = subprocess.run(command, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{' '.join(command)} returned non-JSON output.") from exc


def build_payload(
    raw_dir: Path,
    days: int,
    chart_days: int,
    firebase_json: Path | None,
    firebase_source: str,
    bigquery_project: str,
    bigquery_dataset: str | None,
    skip_revenuecat: bool = False,
    skip_reviews: bool = False,
) -> dict[str, Any]:
    asc = summarize_asc(raw_dir, days, chart_days)
    firebase = summarize_firebase(firebase_json, days, chart_days, firebase_source, bigquery_project, bigquery_dataset)
    revenue_cat = {"status": "off", "note": "RevenueCat fetching is disabled.", "metrics": {}} if skip_revenuecat else summarize_revenuecat(days, chart_days)
    app_store_listing = summarize_app_store_listing(APP_ID)
    app_store_reviews = (
        summarize_app_store_reviews_ratings_only(APP_ID)
        if skip_reviews
        else summarize_app_store_reviews(APP_ID)
    )
    return {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "period": asc["period"],
        "asoMonitor": build_aso_monitor_payload(asc, app_store_listing, firebase),
        "sources": {
            "appStoreConnect": asc,
            "firebase": firebase,
            "revenueCat": revenue_cat,
            "appStoreListing": app_store_listing,
            "appStoreReviews": app_store_reviews,
        },
    }


def summarize_app_store_listing(app_id: str, country: str = "us") -> dict[str, Any]:
    try:
        response = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": app_id, "country": country},
            timeout=15,
        )
        response.raise_for_status()
        result = (response.json().get("results") or [])[0]
    except Exception as error:
        return {
            "status": "error",
            "note": f"App Store public listing lookup failed: {error}",
            "metrics": {},
        }
    if not result:
        return {"status": "missing", "note": "App Store public listing lookup returned no app.", "metrics": {}}
    rating = result.get("averageUserRating")
    rating_count = result.get("userRatingCount")
    return {
        "status": "ok",
        "note": f"Loaded public App Store listing data for {country.upper()}.",
        "country": country.upper(),
        "appId": app_id,
        "trackName": result.get("trackName"),
        "version": result.get("version"),
        "currentVersionReleaseDate": result.get("currentVersionReleaseDate"),
        "formattedPrice": result.get("formattedPrice"),
        "genres": result.get("genres") or [],
        "metrics": {
            "averageUserRating": {"current": tidy(number(rating)) if rating not in (None, "") else None},
            "userRatingCount": {"current": tidy(number(rating_count)) if rating_count not in (None, "") else None},
        },
    }


def summarize_asc(raw_dir: Path, days: int, chart_days: int) -> dict[str, Any]:
    processing_dirs = list_asc_processing_dirs(raw_dir)
    slice_dailies = [build_daily_from_slice(cache_dir) for cache_dir in processing_dirs]
    slice_row_groups = [read_asc_slice_row_groups(cache_dir) for cache_dir in processing_dirs]
    rows = {
        key: read_rows_from_roots(processing_dirs, pattern)
        for key, pattern in ASC_SLICE_ROW_GROUPS
    }
    all_dates = [parse_day(row.get("Date") or row.get("Install Day")) for group in rows.values() for row in group]
    all_dates = [value for value in all_dates if value]
    end = max(all_dates) if all_dates else date.today()
    chart_span = max(chart_days, days)
    chart_start = end - timedelta(days=chart_span - 1)
    start = end - timedelta(days=days - 1)
    prior_start = start - timedelta(days=days)
    prior_end = start - timedelta(days=1)

    daily = merge_slice_dailies(slice_dailies)
    source_split, territory_split = merge_download_attribution(slice_row_groups, start, end)
    available_dates: set[date] = {parse_day(key) for key in daily if parse_day(key)}

    subscription_state_rows: list[dict[str, str]] = []
    for row_group in slice_row_groups:
        if row_group.get("subscription_state"):
            subscription_state_rows = row_group["subscription_state"]
            break
    active_subscription_breakdown = build_asc_active_subscription_breakdown(subscription_state_rows)

    series = [tidy_daily(daily[key]) for key in sorted(daily)]
    for item in series:
        item["pageConversionRate"] = ratio(number(item.get("firstTimeDownloads")), number(item.get("productPageViews")))
        item["tapThroughRate"] = ratio(number(item.get("taps")), number(item.get("impressions")))
    chart_series = [item for item in series if chart_start.isoformat() <= item["date"] <= end.isoformat()]
    current = [item for item in series if start.isoformat() <= item["date"] <= end.isoformat()]
    prior = [item for item in series if prior_start.isoformat() <= item["date"] <= prior_end.isoformat()]
    asc_days_available = len({item["date"] for item in chart_series})

    metric_keys = [
        "firstTimeDownloads",
        "downloads",
        "installs",
        "firstTimeInstallSuccess",
        "installFailures",
        "deletions",
        "sessions",
        "impressions",
        "taps",
        "productPageViews",
        "webProductPageViews",
        "purchases",
        "salesUsd",
        "proceedsUsd",
        "platformInstalls",
        "subscriptionEvents",
    ]
    metrics = {key: compare(sum_metric(current, key), sum_metric(prior, key)) for key in metric_keys}
    metrics["pageConversionRate"] = {
        "current": ratio(sum_metric(current, "firstTimeDownloads"), sum_metric(current, "productPageViews")),
        "prior": ratio(sum_metric(prior, "firstTimeDownloads"), sum_metric(prior, "productPageViews")),
    }
    metrics["tapThroughRate"] = {
        "current": ratio(sum_metric(current, "taps"), sum_metric(current, "impressions")),
        "prior": ratio(sum_metric(prior, "taps"), sum_metric(prior, "impressions")),
    }
    stability = summarize_asc_stability()

    note = "ASC Analytics only returned completed report slices currently available for the ongoing request."
    if asc_days_available and asc_days_available < chart_span:
        note = (
            f"ASC has {asc_days_available} day(s) of daily data through {end.isoformat()} "
            f"(ongoing export; longer ranges fill in as Apple completes more days)."
        )

    return {
        "status": "ok" if any(rows.values()) else "missing",
        "note": note,
        "period": {
            "current": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
            "prior": {"start": prior_start.isoformat(), "end": prior_end.isoformat(), "days": days},
            "chartDays": chart_span,
            "ascDaysAvailable": asc_days_available,
            "availableDays": sorted(day.isoformat() for day in available_dates),
        },
        "metrics": metrics,
        "daily": chart_series,
        "sourceType": top_counter(source_split),
        "territories": top_counter(territory_split),
        "activeSubscriptionBreakdown": active_subscription_breakdown,
        "stability": stability,
        "reports": {key: {"rows": len(value)} for key, value in rows.items()},
    }


def summarize_asc_stability() -> dict[str, Any]:
    try:
        payload = run_json(
            [
                "asc",
                "performance",
                "metrics",
                "list",
                "--app",
                APP_ID,
                "--platform",
                "IOS",
                "--metric-type",
                "TERMINATION,HANG",
                "--output",
                "json",
            ]
        )
    except Exception as error:
        return {
            "status": "error",
            "source": "app_store_connect_performance",
            "note": f"ASC performance metrics unavailable: {error}",
            "metrics": {},
        }

    product_data = payload.get("productData") if isinstance(payload, dict) else None
    numeric_paths = extract_numeric_paths(product_data or [])
    metrics = {
        "crashes": {"current": metric_from_paths(numeric_paths, ("crash",), ("count", "total"))},
        "terminations": {"current": metric_from_paths(numeric_paths, ("termination",), ("count", "total"))},
        "hangs": {"current": metric_from_paths(numeric_paths, ("hang",), ("count", "total"))},
        "hangRate": {"current": metric_from_paths(numeric_paths, ("hang",), ("rate", "percent", "ratio"))},
    }
    has_metric = any(value.get("current") is not None for value in metrics.values())
    if not product_data:
        note = "ASC Performance returned no stability product data yet."
        status = "empty"
    elif not has_metric:
        note = "ASC Performance data loaded, but no crash, termination, or hang values were found."
        status = "partial"
    else:
        note = "Loaded App Store Connect performance stability metrics."
        status = "ok"
    return {
        "status": status,
        "source": "app_store_connect_performance",
        "note": note,
        "productDataCount": len(product_data or []),
        "metrics": metrics,
    }


def extract_numeric_paths(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, float]]:
    if isinstance(value, dict):
        result: list[tuple[str, float]] = []
        for key, child in value.items():
            result.extend(extract_numeric_paths(child, (*path, str(key))))
        return result
    if isinstance(value, list):
        result: list[tuple[str, float]] = []
        for index, child in enumerate(value):
            result.extend(extract_numeric_paths(child, (*path, str(index))))
        return result
    if isinstance(value, bool) or value in (None, ""):
        return []
    if isinstance(value, (int, float)):
        return [(".".join(path), float(value))]
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        try:
            return [(".".join(path), float(stripped))]
        except ValueError:
            return []
    return []


def metric_from_paths(
    paths: list[tuple[str, float]],
    required_keywords: tuple[str, ...],
    preferred_keywords: tuple[str, ...] = (),
) -> int | float | None:
    candidates = [
        (path, value)
        for path, value in paths
        if any(keyword in path.lower() for keyword in required_keywords)
    ]
    if preferred_keywords:
        preferred = [
            (path, value)
            for path, value in candidates
            if any(keyword in path.lower() for keyword in preferred_keywords)
        ]
        if preferred:
            candidates = preferred
    if not candidates:
        return None
    return tidy(sum(value for _, value in candidates))


def summarize_firebase(
    firebase_json: Path | None,
    days: int,
    chart_days: int,
    source: str,
    bigquery_project: str,
    bigquery_dataset: str | None,
) -> dict[str, Any]:
    if source in ("auto", "bigquery"):
        bigquery = summarize_firebase_bigquery(days, chart_days, bigquery_project, bigquery_dataset)
        if bigquery["status"] == "ok" or source == "bigquery" or not firebase_json:
            return bigquery

    if source == "off":
        return {"status": "off", "note": "Firebase/GA4 fetching is disabled.", "metrics": {}}

    if not firebase_json:
        return {
            "status": "not_loaded",
            "note": "Set GOOGLE_APPLICATION_CREDENTIALS and FIREBASE_BQ_DATASET for BigQuery, or load a Firebase/GA4 JSON export.",
            "metrics": {},
        }
    if not firebase_json.exists():
        return {"status": "missing", "note": f"{firebase_json} does not exist.", "metrics": {}}
    data = json.loads(firebase_json.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "metrics" in data:
        return {"status": "ok", "note": "Loaded Firebase/GA4 metrics JSON.", "metrics": data["metrics"]}

    events = data.get("events", []) if isinstance(data, dict) else []
    daily: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        day = parse_day(event.get("date") or event.get("event_date"))
        name = event.get("event_name") or event.get("name")
        if not day or not name:
            continue
        aliases = {
            FIRST_OPEN_EVENT: "first_open",
            ACTIVATION_EVENT: "activation_completed",
        }
        key = aliases.get(str(name), str(name))
        entry = daily.setdefault(day.isoformat(), {"date": day.isoformat()})
        entry[key] = entry.get(key, 0) + number(event.get("count") or event.get("event_count"))

    end = max((parse_day(key) for key in daily), default=date.today())
    start = end - timedelta(days=days - 1)
    current = [item for key, item in sorted(daily.items()) if start.isoformat() <= key <= end.isoformat()]
    first_open = sum_metric(current, "first_open")
    cleanup = sum_metric(current, "activation_completed")
    activation = (cleanup / first_open) if first_open else None
    return {
        "status": "ok",
        "note": "Loaded Firebase/GA4 event export.",
        "metrics": {
            "firstOpen": {"current": first_open},
            "activationCompleted": {"current": cleanup},
            "activationRate": {"current": activation},
        },
        "daily": current,
    }


def summarize_revenuecat(days: int, chart_days: int) -> dict[str, Any]:
    api_key = os.environ.get("REVENUECAT_API_KEY") or os.environ.get("REVENUECAT_SECRET_API_KEY")
    project_id = os.environ.get("REVENUECAT_PROJECT_ID")
    if not api_key and not project_id:
        return {
            "status": "needs_setup",
            "note": "Add REVENUECAT_API_KEY and REVENUECAT_PROJECT_ID to the configured env file, then restart the dashboard.",
            "metrics": {},
        }
    if not api_key or not project_id:
        return {
            "status": "error",
            "note": "RevenueCat setup is incomplete. Set both REVENUECAT_API_KEY and REVENUECAT_PROJECT_ID.",
            "projectId": project_id,
            "metrics": {},
        }
    end = date.today()
    start = end - timedelta(days=days - 1)
    prior_start = start - timedelta(days=days)
    chart_start = end - timedelta(days=chart_days - 1)
    query_start = min(prior_start, chart_start)
    headers = {"Authorization": f"Bearer {api_key}"}
    base_path = f"/projects/{project_id}"
    chart_names = [
        "revenue",
        "mrr",
        "customers_new",
        "customers_active",
        "trials_new",
        "trial_conversion_rate",
        "churn",
        "actives",
    ]
    errors: dict[str, str] = {}
    charts: dict[str, Any] = {}
    try:
        overview = revenuecat_get(headers, f"{base_path}/metrics/overview")
    except Exception as error:
        return {
            "status": "error",
            "note": f"RevenueCat overview fetch failed: {error}",
            "projectId": project_id,
            "metrics": {},
        }

    for chart_name in chart_names:
        try:
            charts[chart_name] = revenuecat_get(
                headers,
                f"{base_path}/charts/{chart_name}",
                {
                    "start_date": query_start.isoformat(),
                    "end_date": end.isoformat(),
                    "resolution": "0",
                },
            )
        except Exception as error:
            errors[chart_name] = str(error)

    product_catalog: list[dict[str, Any]] = []
    active_by_product: list[dict[str, Any]] = []
    try:
        product_catalog = build_revenuecat_product_catalog(headers, project_id)
    except Exception as error:
        errors["products"] = str(error)

    try:
        actives_product_chart = revenuecat_get(
            headers,
            f"{base_path}/charts/actives",
            {
                "start_date": end.isoformat(),
                "end_date": end.isoformat(),
                "resolution": "0",
                "segment": "product",
                "limit_num_segments": "20",
            },
        )
        measure_name = revenuecat_chart_measure_name(actives_product_chart, "Actives")
        active_by_product = revenuecat_segment_totals(actives_product_chart, measure_name)
    except Exception as error:
        errors["actives_product"] = str(error)

    country_charts: dict[str, Any] = {}
    for chart_name in ("revenue", "customers_new"):
        try:
            country_charts[chart_name] = revenuecat_get(
                headers,
                f"{base_path}/charts/{chart_name}",
                {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "resolution": "0",
                    "segment": "country",
                    "limit_num_segments": "6",
                },
            )
        except Exception as error:
            errors[f"{chart_name}_country"] = str(error)

    daily = normalize_revenuecat_daily(charts)
    chart_series = [item for item in daily if chart_start.isoformat() <= item["date"] <= end.isoformat()]
    current = [item for item in daily if start.isoformat() <= item["date"] <= end.isoformat()]
    prior = [item for item in daily if prior_start.isoformat() <= item["date"] < start.isoformat()]
    overview_metrics = normalize_revenuecat_overview(overview)
    computed_metrics = revenuecat_computed_metrics(current, prior)
    active_subscription_product_metrics = build_active_subscription_product_metrics(product_catalog, active_by_product)
    duration_metrics = build_active_subscription_duration_metrics(product_catalog, active_by_product)
    metrics = {**overview_metrics, **computed_metrics, **duration_metrics, **active_subscription_product_metrics}
    status = "ok" if not errors else "partial"
    note = "Loaded RevenueCat overview and chart metrics."
    if errors:
        note = "Loaded RevenueCat with some chart gaps: " + "; ".join(f"{key}: {value}" for key, value in list(errors.items())[:3])
    return {
        "status": status,
        "note": note,
        "projectId": project_id,
        "period": {
            "current": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
            "prior": {"start": prior_start.isoformat(), "end": (start - timedelta(days=1)).isoformat(), "days": days},
            "chartDays": chart_days,
            "revenueCatDaysAvailable": len(chart_series),
        },
        "metrics": metrics,
        "daily": chart_series,
        "products": product_catalog,
        "activeByProduct": active_by_product,
        "countries": {
            "revenue": revenuecat_segment_totals(country_charts.get("revenue"), "Revenue"),
            "customers": revenuecat_segment_totals(country_charts.get("customers_new"), "New Customers"),
        },
        "charts": {
            key: {
                "displayName": value.get("display_name"),
                "lastComputedAt": value.get("last_computed_at"),
                "summary": value.get("summary"),
            }
            for key, value in charts.items()
        },
        "errors": errors,
    }


def revenuecat_get(headers: dict[str, str], path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.get(
        f"{REVENUECAT_API_BASE}{path}",
        headers=headers,
        params=params,
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("type") or response.text
        except ValueError:
            message = response.text
        raise RuntimeError(message.strip())
    return response.json()


REVENUECAT_DAILY_KEYS = {
    ("revenue", "Revenue"): "revenueUsd",
    ("revenue", "Transactions"): "transactions",
    ("mrr", "MRR"): "mrrUsd",
    ("customers_new", "New Customers"): "newCustomers",
    ("customers_active", "Active Customers"): "activeCustomers",
    ("trials_new", "New Trials"): "newTrials",
    ("trial_conversion_rate", "Trial Starts"): "trialStarts",
    ("trial_conversion_rate", "Conversions"): "trialConversions",
    ("trial_conversion_rate", "Expirations"): "trialExpirations",
    ("trial_conversion_rate", "Pending"): "trialPending",
    ("trial_conversion_rate", "Conversion Rate"): "trialConversionRate",
    ("churn", "Actives"): "churnActives",
    ("churn", "Churned Actives"): "churnedActives",
    ("churn", "Churn Rate"): "churnRate",
    ("actives", "Actives"): "activeSubscriptions",
}


def normalize_revenuecat_daily(charts: dict[str, Any]) -> list[dict[str, Any]]:
    daily: dict[str, dict[str, Any]] = {}
    for chart_name, payload in charts.items():
        measures = payload.get("measures") or []
        measure_names = [str(item.get("display_name") or "") for item in measures]
        for point in payload.get("values") or []:
            if not isinstance(point, dict) or "segment" in point:
                continue
            day = day_from_epoch(point.get("cohort"))
            if not day:
                continue
            measure_index = int(number(point.get("measure")))
            measure_name = measure_names[measure_index] if measure_index < len(measure_names) else ""
            key = REVENUECAT_DAILY_KEYS.get((chart_name, measure_name))
            if not key:
                continue
            entry = daily.setdefault(day.isoformat(), revenuecat_daily_entry(day))
            entry[key] = revenuecat_daily_measure_value(key, point.get("value"))
    return [daily[key] for key in sorted(daily)]


REVENUECAT_PERCENT_RATIO_KEYS = {"churnRate", "trialConversionRate"}


def revenuecat_daily_measure_value(key: str, value: Any) -> int | float:
    amount = number(value)
    if key in REVENUECAT_PERCENT_RATIO_KEYS:
        if amount > 1:
            return tidy(amount / 100)
        return tidy(amount)
    return tidy(amount)


def revenuecat_daily_entry(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "revenueUsd": 0,
        "transactions": 0,
        "mrrUsd": 0,
        "newCustomers": 0,
        "activeCustomers": 0,
        "newTrials": 0,
        "trialStarts": 0,
        "trialConversions": 0,
        "trialExpirations": 0,
        "trialPending": 0,
        "trialConversionRate": 0,
        "churnActives": 0,
        "churnedActives": 0,
        "churnRate": 0,
        "activeSubscriptions": 0,
    }


def normalize_revenuecat_overview(payload: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    key_map = {
        "active_trials": "activeTrials",
        "active_subscriptions": "activeSubscriptionsOverview",
        "mrr": "mrrOverviewUsd",
        "revenue": "revenueOverviewUsd",
        "new_customers": "newCustomersOverview",
        "active_users": "activeUsersOverview",
    }
    for item in payload.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        key = key_map.get(str(item.get("id") or ""))
        if not key:
            continue
        metrics[key] = {
            "current": tidy(number(item.get("value"))),
            "unit": item.get("unit"),
            "name": item.get("name"),
            "description": item.get("description"),
            "period": item.get("period"),
            "lastUpdatedAt": item.get("last_updated_at_iso8601"),
        }
    return metrics


def revenuecat_computed_metrics(current: list[dict[str, Any]], prior: list[dict[str, Any]]) -> dict[str, Any]:
    latest_current = current[-1] if current else {}
    latest_prior = prior[-1] if prior else {}
    return {
        "revenueUsd": compare(sum_metric(current, "revenueUsd"), sum_metric(prior, "revenueUsd")),
        "transactions": compare(sum_metric(current, "transactions"), sum_metric(prior, "transactions")),
        "newCustomers": compare(sum_metric(current, "newCustomers"), sum_metric(prior, "newCustomers")),
        "activeCustomers": compare(sum_metric(current, "activeCustomers"), sum_metric(prior, "activeCustomers")),
        "newTrials": compare(sum_metric(current, "newTrials"), sum_metric(prior, "newTrials")),
        "trialStarts": compare(sum_metric(current, "trialStarts"), sum_metric(prior, "trialStarts")),
        "trialConversions": compare(sum_metric(current, "trialConversions"), sum_metric(prior, "trialConversions")),
        "trialConversionRate": {
            "current": ratio(sum_metric(current, "trialConversions"), sum_metric(current, "trialStarts")),
            "prior": ratio(sum_metric(prior, "trialConversions"), sum_metric(prior, "trialStarts")),
        },
        "churnRate": {
            "current": percent_to_ratio(latest_current.get("churnRate")),
            "prior": percent_to_ratio(latest_prior.get("churnRate")),
        },
        "mrrUsd": {
            "current": tidy(number(latest_current.get("mrrUsd"))),
            "prior": tidy(number(latest_prior.get("mrrUsd"))),
        },
        "activeSubscriptions": {
            "current": tidy(number(latest_current.get("activeSubscriptions"))),
            "prior": tidy(number(latest_prior.get("activeSubscriptions"))),
        },
    }


def revenuecat_list_paginated(headers: dict[str, str], path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_path = path
    while next_path:
        payload = revenuecat_get(headers, next_path)
        items.extend(item for item in (payload.get("items") or []) if isinstance(item, dict))
        next_path = payload.get("next_page") or None
    return items


def revenuecat_chart_measure_name(payload: dict[str, Any] | None, preferred: str) -> str:
    if not payload:
        return preferred
    measures = payload.get("measures") or []
    names = [str(item.get("display_name") or "") for item in measures if isinstance(item, dict)]
    if preferred in names:
        return preferred
    return names[0] if names else preferred


def revenuecat_product_duration_class(duration: str) -> str:
    normalized = str(duration or "").upper()
    if normalized in {"P1Y"} or normalized.endswith("Y"):
        return "annual"
    if normalized in {"P1M", "P1W"} or normalized.endswith("M") or normalized.endswith("W"):
        return "monthly"
    return "other"


def build_revenuecat_product_catalog(headers: dict[str, str], project_id: str) -> list[dict[str, Any]]:
    base_path = f"/projects/{project_id}"
    catalog: dict[str, dict[str, Any]] = {}

    for item in revenuecat_list_paginated(headers, f"{base_path}/products"):
        store_identifier = str(item.get("store_identifier") or "").strip()
        if not store_identifier:
            continue
        subscription = item.get("subscription") if isinstance(item.get("subscription"), dict) else {}
        duration = str(subscription.get("duration") or "")
        catalog[store_identifier] = {
            "id": str(item.get("id") or ""),
            "storeIdentifier": store_identifier,
            "displayName": str(item.get("display_name") or "").strip() or store_identifier,
            "duration": duration,
            "durationClass": revenuecat_product_duration_class(duration),
            "type": str(item.get("type") or ""),
        }

    for offering in revenuecat_list_paginated(headers, f"{base_path}/offerings"):
        if not offering.get("is_current"):
            continue
        offering_id = str(offering.get("id") or "").strip()
        if not offering_id:
            continue
        packages = revenuecat_list_paginated(headers, f"{base_path}/offerings/{offering_id}/packages")
        for package in packages:
            package_name = str(package.get("display_name") or package.get("lookup_key") or "").strip()
            products = package.get("products") if isinstance(package.get("products"), dict) else {}
            for entry in products.get("items") or []:
                if not isinstance(entry, dict):
                    continue
                product = entry.get("product") if isinstance(entry.get("product"), dict) else entry
                store_identifier = str(product.get("store_identifier") or "").strip()
                if not store_identifier:
                    continue
                existing = catalog.setdefault(
                    store_identifier,
                    {
                        "id": str(product.get("id") or ""),
                        "storeIdentifier": store_identifier,
                        "displayName": store_identifier,
                        "duration": "",
                        "durationClass": "other",
                        "type": str(product.get("type") or ""),
                    },
                )
                if package_name:
                    existing["displayName"] = package_name
                subscription = product.get("subscription") if isinstance(product.get("subscription"), dict) else {}
                duration = str(subscription.get("duration") or existing.get("duration") or "")
                if duration:
                    existing["duration"] = duration
                    existing["durationClass"] = revenuecat_product_duration_class(duration)

    return sorted(catalog.values(), key=lambda item: item.get("displayName") or item.get("storeIdentifier") or "")


def build_active_subscription_duration_metrics(
    product_catalog: list[dict[str, Any]],
    active_by_product: list[dict[str, Any]],
) -> dict[str, Any]:
    catalog_by_store = {
        str(item.get("storeIdentifier") or ""): item
        for item in product_catalog
        if item.get("storeIdentifier")
    }
    catalog_by_id = {
        str(item.get("id") or ""): item
        for item in product_catalog
        if item.get("id")
    }
    catalog_by_name = {
        str(item.get("displayName") or ""): item
        for item in product_catalog
        if item.get("displayName")
    }

    buckets: dict[str, dict[str, Any]] = {
        "monthly": {"current": 0.0, "products": []},
        "annual": {"current": 0.0, "products": []},
    }

    for segment in active_by_product:
        segment_name = str(segment.get("name") or "").strip()
        value = number(segment.get("value"))
        if value <= 0:
            continue
        product = (
            catalog_by_store.get(segment_name)
            or catalog_by_id.get(segment_name)
            or catalog_by_name.get(segment_name)
        )
        duration_class = "other"
        display_name = segment_name
        if product:
            duration_class = str(product.get("durationClass") or "other")
            display_name = str(product.get("displayName") or segment_name)
        else:
            lowered = segment_name.lower()
            if "annual" in lowered or "year" in lowered:
                duration_class = "annual"
            elif "month" in lowered or "weekly" in lowered:
                duration_class = "monthly"

        bucket_key = duration_class if duration_class in buckets else "other"
        if bucket_key == "other":
            continue
        buckets[bucket_key]["current"] += value
        buckets[bucket_key]["products"].append(
            {
                "name": display_name,
                "segment": segment_name,
                "value": tidy(value),
            }
        )

    metrics: dict[str, Any] = {}
    for bucket_key, metric_key in (("monthly", "activeSubscriptionsMonthly"), ("annual", "activeSubscriptionsAnnual")):
        bucket = buckets[bucket_key]
        products = sorted(bucket["products"], key=lambda item: number(item.get("value")), reverse=True)
        label = products[0]["name"] if products else ("Active monthly" if bucket_key == "monthly" else "Active annual")
        metrics[metric_key] = {
            "current": tidy(bucket["current"]),
            "label": label,
            "products": products,
        }
    return metrics


def build_active_subscription_product_metrics(
    product_catalog: list[dict[str, Any]],
    active_by_product: list[dict[str, Any]],
) -> dict[str, Any]:
    catalog_by_store = {
        str(item.get("storeIdentifier") or ""): item
        for item in product_catalog
        if item.get("storeIdentifier")
    }
    catalog_by_id = {
        str(item.get("id") or ""): item
        for item in product_catalog
        if item.get("id")
    }
    catalog_by_name = {
        str(item.get("displayName") or ""): item
        for item in product_catalog
        if item.get("displayName")
    }

    products: list[dict[str, Any]] = []
    total = 0.0
    for segment in active_by_product:
        segment_name = str(segment.get("name") or "").strip()
        value = number(segment.get("value"))
        if not segment_name or value <= 0:
            continue
        product = (
            catalog_by_store.get(segment_name)
            or catalog_by_id.get(segment_name)
            or catalog_by_name.get(segment_name)
        )
        display_name = str((product or {}).get("displayName") or segment_name).strip()
        duration = str((product or {}).get("duration") or "").strip()
        total += value
        products.append(
            {
                "name": display_name,
                "segment": segment_name,
                "value": tidy(value),
                "storeIdentifier": str((product or {}).get("storeIdentifier") or segment_name),
                "productId": str((product or {}).get("id") or ""),
                "duration": duration,
                "type": str((product or {}).get("type") or ""),
            }
        )

    products = sorted(products, key=lambda item: number(item.get("value")), reverse=True)
    for product in products:
        product["share"] = None if total == 0 else round(number(product.get("value")) / total, 3)

    return {
        "activeSubscriptionProducts": {
            "current": tidy(total),
            "products": products,
            "source": "RevenueCat actives chart segmented by product, matched to the RevenueCat product catalog.",
        }
    }


def revenuecat_segment_totals(payload: dict[str, Any] | None, measure_name: str) -> list[dict[str, Any]]:
    if not payload:
        return []
    totals = (payload.get("summary") or {}).get("total") or {}
    total_value = number((totals.get("Total") or {}).get(measure_name))
    items: list[dict[str, Any]] = []
    for name, values in totals.items():
        if name == "Total" or not isinstance(values, dict):
            continue
        value = number(values.get(measure_name))
        items.append({
            "name": name,
            "value": tidy(value),
            "share": None if total_value == 0 else round(value / total_value, 3),
        })
    return sorted(items, key=lambda item: number(item["value"]), reverse=True)


def day_from_epoch(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def percent_to_ratio(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return round(number(value) / 100, 4)


def summarize_firebase_bigquery(
    days: int,
    chart_days: int,
    project_id: str,
    dataset_id: str | None,
) -> dict[str, Any]:
    credentials = apply_google_credentials_default()
    if not credentials:
        return {
            "status": "not_configured",
            "note": (
                "Set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON with BigQuery read access, "
                "or add a matching firebase.credentialFiles path to the dashboard config. "
                "FIREBASE_BQ_DATASET is optional if the account can list datasets."
            ),
            "metrics": {},
        }
    if not Path(credentials).exists():
        return {"status": "missing", "note": f"GOOGLE_APPLICATION_CREDENTIALS does not exist: {credentials}", "metrics": {}}

    try:
        validate_bigquery_identifier(project_id, "project")
        token = service_account_access_token(Path(credentials))
        if not dataset_id:
            dataset_id = discover_analytics_dataset(token, project_id)
        validate_bigquery_identifier(dataset_id, "dataset")
        chart_span = max(chart_days, days)
        end = date.today() - timedelta(days=1)
        chart_start = end - timedelta(days=chart_span - 1)
        start = end - timedelta(days=days - 1)
        prior_start = start - timedelta(days=days)
        prior_end = start - timedelta(days=1)
        scan_end = end + timedelta(days=7)
        query_start = min(chart_start, prior_start)
        table = f"`{project_id}.{dataset_id}.events_*`"
        daily_rows = run_bigquery_query(
            token,
            project_id,
            firebase_daily_query(table, query_start, scan_end),
        )
        funnel_rows = run_bigquery_query(
            token,
            project_id,
            firebase_activation_query(table, start, end, scan_end),
        )
        activation_cohort_rows = run_bigquery_query(
            token,
            project_id,
            firebase_activation_cohort_query(table, chart_start, end, scan_end),
        )
        deleted_total_rows = run_bigquery_query(
            token,
            project_id,
            firebase_activation_value_total_query(table),
        )
        engagement_rows = run_bigquery_query(
            token,
            project_id,
            firebase_engagement_overview_query(table, start, end),
        )
        dau_rows = run_bigquery_query(
            token,
            project_id,
            firebase_dau_daily_query(table, query_start, end),
        )
        current_mau_rows = run_bigquery_query(
            token,
            project_id,
            firebase_mau_query(table, start, end),
        )
        prior_mau_rows = run_bigquery_query(
            token,
            project_id,
            firebase_mau_query(table, prior_start, prior_end),
        )
    except Exception as error:
        return {"status": "error", "note": str(error), "metrics": {}}

    daily = merge_firebase_dau_daily(normalize_firebase_daily(daily_rows), dau_rows)
    activation_cohorts = normalize_firebase_activation_cohorts(activation_cohort_rows)
    chart_series = [item for item in daily if chart_start.isoformat() <= item["date"] <= end.isoformat()]
    current = [item for item in daily if start.isoformat() <= item["date"] <= end.isoformat()]
    prior = [item for item in daily if prior_start.isoformat() <= item["date"] <= prior_end.isoformat()]
    funnel = funnel_rows[0] if funnel_rows else {}
    deleted_total = deleted_total_rows[0] if deleted_total_rows else {}
    engagement = engagement_rows[0] if engagement_rows else {}

    first_open = number(funnel.get("firstOpenUsers"))
    engagement_first_open = number(engagement.get("firstOpenUsers")) or first_open
    paywall_users = number(engagement.get("paywallSeenUsers"))
    primary_feature_users = number(engagement.get("primaryFeatureUsers"))
    secondary_feature_users = number(engagement.get("secondaryFeatureUsers"))
    current_mau = number((current_mau_rows[0] if current_mau_rows else {}).get("mau"))
    prior_mau = number((prior_mau_rows[0] if prior_mau_rows else {}).get("mau"))
    d0 = number(funnel.get("d0ActivationUsers"))
    d3 = number(funnel.get("d3ActivationUsers"))
    d5 = number(funnel.get("d5ActivationUsers"))
    d7 = number(funnel.get("d7ActivationUsers"))
    d0_base = number(funnel.get("d0EligibleFirstOpenUsers"))
    d3_base = number(funnel.get("d3EligibleFirstOpenUsers"))
    d5_base = number(funnel.get("d5EligibleFirstOpenUsers"))
    d7_base = number(funnel.get("d7EligibleFirstOpenUsers"))
    d0_meaningful = number(funnel.get("d0MeaningfulActivationUsers"))
    d3_meaningful = number(funnel.get("d3MeaningfulActivationUsers"))
    d5_meaningful = number(funnel.get("d5MeaningfulActivationUsers"))
    d7_meaningful = number(funnel.get("d7MeaningfulActivationUsers"))
    total_activation_value = number(deleted_total.get("totalActivationValue"))

    return {
        "status": "ok",
        "note": f"Loaded Firebase/GA4 events from BigQuery dataset {project_id}.{dataset_id}.",
        "period": {
            "current": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
            "prior": {"start": prior_start.isoformat(), "end": prior_end.isoformat(), "days": days},
            "chartDays": chart_span,
            "ga4DaysAvailable": len(chart_series),
        },
        "metrics": {
            "firstOpen": compare(sum_metric(current, "first_open"), sum_metric(prior, "first_open")),
            "permissionViewed": compare(sum_metric(current, "permission_viewed"), sum_metric(prior, "permission_viewed")),
            "permissionAccepted": compare(sum_metric(current, "permission_accepted"), sum_metric(prior, "permission_accepted")),
            "initialLoadCompleted": compare(sum_metric(current, "initial_load_completed"), sum_metric(prior, "initial_load_completed")),
            "activationCompleted": compare(sum_metric(current, "activation_completed"), sum_metric(prior, "activation_completed")),
            "activationValue": compare(sum_metric(current, "activation_value"), sum_metric(prior, "activation_value")),
            "totalActivationValue": {"current": tidy(total_activation_value)},
            "d0ActivationRate": {"current": ratio(d0, d0_base), "users": tidy(d0), "base": tidy(d0_base)},
            "d3ActivationRate": {"current": ratio(d3, d3_base), "users": tidy(d3), "base": tidy(d3_base)},
            "d5ActivationRate": {"current": ratio(d5, d5_base), "users": tidy(d5), "base": tidy(d5_base)},
            "d7ActivationRate": {"current": ratio(d7, d7_base), "users": tidy(d7), "base": tidy(d7_base)},
            "d0MeaningfulActivationRate": {
                "current": ratio(d0_meaningful, d0_base),
                "users": tidy(d0_meaningful),
                "base": tidy(d0_base),
            },
            "d3MeaningfulActivationRate": {
                "current": ratio(d3_meaningful, d3_base),
                "users": tidy(d3_meaningful),
                "base": tidy(d3_base),
            },
            "d5MeaningfulActivationRate": {
                "current": ratio(d5_meaningful, d5_base),
                "users": tidy(d5_meaningful),
                "base": tidy(d5_base),
            },
            "d7MeaningfulActivationRate": {
                "current": ratio(d7_meaningful, d7_base),
                "users": tidy(d7_meaningful),
                "base": tidy(d7_base),
            },
            "activationRate": {
                "current": ratio(d3_meaningful, d3_base),
                "users": tidy(d3_meaningful),
                "base": tidy(d3_base),
            },
            "paywallExposureRate": {
                "current": ratio(paywall_users, current_mau),
                "users": tidy(paywall_users),
                "base": tidy(current_mau),
            },
            "paywallSeenUserRate": {
                "current": ratio(paywall_users, current_mau),
                "users": tidy(paywall_users),
                "base": tidy(current_mau),
            },
            "primaryFeatureUserRate": {
                "current": ratio(primary_feature_users, engagement_first_open),
                "users": tidy(primary_feature_users),
                "base": tidy(engagement_first_open),
            },
            "secondaryFeatureUserRate": {
                "current": ratio(secondary_feature_users, engagement_first_open),
                "users": tidy(secondary_feature_users),
                "base": tidy(engagement_first_open),
            },
            "dau": compare(avg_metric(current, "dau"), avg_metric(prior, "dau")),
            "mau": compare(current_mau, prior_mau),
        },
        "daily": chart_series,
        "activationCohorts": activation_cohorts,
        "funnel": {
            "firstOpenUsers": tidy(first_open),
            "d0EligibleFirstOpenUsers": tidy(d0_base),
            "d3EligibleFirstOpenUsers": tidy(d3_base),
            "d5EligibleFirstOpenUsers": tidy(d5_base),
            "d7EligibleFirstOpenUsers": tidy(d7_base),
            "d0ActivationUsers": tidy(d0),
            "d3ActivationUsers": tidy(d3),
            "d5ActivationUsers": tidy(d5),
            "d7ActivationUsers": tidy(d7),
            "d0MeaningfulActivationUsers": tidy(d0_meaningful),
            "d3MeaningfulActivationUsers": tidy(d3_meaningful),
            "d5MeaningfulActivationUsers": tidy(d5_meaningful),
            "d7MeaningfulActivationUsers": tidy(d7_meaningful),
            "meaningfulActivationMinimum": MEANINGFUL_ACTIVATION_VALUE,
        },
    }


def firebase_daily_query(table: str, start: date, end: date) -> str:
    accepted_values = ", ".join(f"'{value}'" for value in PERMISSION_ACCEPTED_VALUES)
    return f"""
SELECT
  PARSE_DATE('%Y%m%d', event_date) AS date,
  COUNTIF(event_name = '{FIRST_OPEN_EVENT}') AS first_open,
  COUNTIF(event_name = '{PERMISSION_PROMPT_EVENT}') AS permission_viewed,
  COUNTIF(event_name = '{PERMISSION_RESULT_EVENT}'
    AND (SELECT value.string_value FROM UNNEST(event_params) WHERE key = '{PERMISSION_STATUS_PARAMETER}') IN ({accepted_values})) AS permission_accepted,
  COUNTIF(event_name = '{INITIAL_LOAD_EVENT}') AS initial_load_completed,
  COUNTIF(event_name = '{ACTIVATION_EVENT}') AS activation_completed,
  SUM(IF(event_name = '{ACTIVATION_EVENT}',
    COALESCE((SELECT value.int_value FROM UNNEST(event_params) WHERE key = '{ACTIVATION_VALUE_PARAMETER}'), 0), 0)) AS activation_value,
  COUNTIF(event_name = '{PAYWALL_EVENT}') AS paywall_presented,
  COUNTIF(event_name = '{PRIMARY_FEATURE_EVENT}') AS primary_feature_used,
  COUNTIF(event_name = '{SECONDARY_FEATURE_EVENT}') AS secondary_feature_used
FROM {table}
WHERE _TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  AND event_name IN (
    '{FIRST_OPEN_EVENT}',
    '{PERMISSION_PROMPT_EVENT}',
    '{PERMISSION_RESULT_EVENT}',
    '{INITIAL_LOAD_EVENT}',
    '{ACTIVATION_EVENT}',
    '{PAYWALL_EVENT}',
    '{PRIMARY_FEATURE_EVENT}',
    '{SECONDARY_FEATURE_EVENT}'
  )
GROUP BY date
ORDER BY date
"""


def firebase_activation_query(table: str, start: date, end: date, scan_end: date) -> str:
    meaningful_bytes = MEANINGFUL_ACTIVATION_VALUE
    data_complete_ts = f"{(end + timedelta(days=1)).isoformat()} 00:00:00+00"
    return f"""
WITH scoped_events AS (
  SELECT
    user_pseudo_id,
    event_name,
    event_timestamp,
    event_date,
    COALESCE((SELECT value.int_value FROM UNNEST(event_params) WHERE key = '{ACTIVATION_VALUE_PARAMETER}'), 0) AS activation_value
  FROM {table}
  WHERE _TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{scan_end:%Y%m%d}'
    AND event_name IN ('{FIRST_OPEN_EVENT}', '{ACTIVATION_EVENT}')
),
first_opens AS (
  SELECT
    user_pseudo_id,
    MIN(TIMESTAMP_MICROS(event_timestamp)) AS first_open_ts
  FROM scoped_events
  WHERE event_name = '{FIRST_OPEN_EVENT}'
    AND event_date BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  GROUP BY user_pseudo_id
),
activations AS (
  SELECT
    user_pseudo_id,
    MIN(TIMESTAMP_MICROS(event_timestamp)) AS activation_ts
  FROM scoped_events
  WHERE event_name = '{ACTIVATION_EVENT}'
  GROUP BY user_pseudo_id
),
meaningful_activations AS (
  SELECT
    user_pseudo_id,
    MIN(TIMESTAMP_MICROS(event_timestamp)) AS activation_ts
  FROM scoped_events
  WHERE event_name = '{ACTIVATION_EVENT}'
    AND activation_value >= {meaningful_bytes}
  GROUP BY user_pseudo_id
)
SELECT
  COUNT(*) AS firstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 24 HOUR)) AS d0EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 72 HOUR)) AS d3EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 5 DAY)) AS d5EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 7 DAY)) AS d7EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 24 HOUR)
    AND activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 24 HOUR)) AS d0ActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 72 HOUR)
    AND activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 72 HOUR)) AS d3ActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 5 DAY)
    AND activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 5 DAY)) AS d5ActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 7 DAY)
    AND activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 7 DAY)) AS d7ActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 24 HOUR)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 24 HOUR)) AS d0MeaningfulActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 72 HOUR)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 72 HOUR)) AS d3MeaningfulActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 5 DAY)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 5 DAY)) AS d5MeaningfulActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 7 DAY)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 7 DAY)) AS d7MeaningfulActivationUsers
FROM first_opens
LEFT JOIN activations USING (user_pseudo_id)
LEFT JOIN (
  SELECT user_pseudo_id, activation_ts AS meaningful_activation_ts
  FROM meaningful_activations
) USING (user_pseudo_id)
    """


def firebase_activation_cohort_query(table: str, start: date, end: date, scan_end: date) -> str:
    meaningful_bytes = MEANINGFUL_ACTIVATION_VALUE
    data_complete_ts = f"{(end + timedelta(days=1)).isoformat()} 00:00:00+00"
    return f"""
WITH scoped_events AS (
  SELECT
    user_pseudo_id,
    event_name,
    event_timestamp,
    event_date,
    COALESCE((SELECT value.int_value FROM UNNEST(event_params) WHERE key = '{ACTIVATION_VALUE_PARAMETER}'), 0) AS activation_value
  FROM {table}
  WHERE _TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{scan_end:%Y%m%d}'
    AND event_name IN ('{FIRST_OPEN_EVENT}', '{ACTIVATION_EVENT}')
),
first_opens AS (
  SELECT
    user_pseudo_id,
    MIN(TIMESTAMP_MICROS(event_timestamp)) AS first_open_ts
  FROM scoped_events
  WHERE event_name = '{FIRST_OPEN_EVENT}'
    AND event_date BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  GROUP BY user_pseudo_id
),
activations AS (
  SELECT
    user_pseudo_id,
    MIN(TIMESTAMP_MICROS(event_timestamp)) AS activation_ts
  FROM scoped_events
  WHERE event_name = '{ACTIVATION_EVENT}'
  GROUP BY user_pseudo_id
),
meaningful_activations AS (
  SELECT
    user_pseudo_id,
    MIN(TIMESTAMP_MICROS(event_timestamp)) AS meaningful_activation_ts
  FROM scoped_events
  WHERE event_name = '{ACTIVATION_EVENT}'
    AND activation_value >= {meaningful_bytes}
  GROUP BY user_pseudo_id
),
joined AS (
  SELECT
    DATE(first_open_ts) AS cohort_date,
    first_open_ts,
    activation_ts,
    meaningful_activation_ts
  FROM first_opens
  LEFT JOIN activations USING (user_pseudo_id)
  LEFT JOIN meaningful_activations USING (user_pseudo_id)
)
SELECT
  cohort_date AS date,
  COUNT(*) AS firstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 24 HOUR)) AS d0EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 72 HOUR)) AS d3EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 5 DAY)) AS d5EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 7 DAY)) AS d7EligibleFirstOpenUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 24 HOUR)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 24 HOUR)) AS d0MeaningfulActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 72 HOUR)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 72 HOUR)) AS d3MeaningfulActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 5 DAY)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 5 DAY)) AS d5MeaningfulActivationUsers,
  COUNTIF(first_open_ts <= TIMESTAMP_SUB(TIMESTAMP('{data_complete_ts}'), INTERVAL 7 DAY)
    AND meaningful_activation_ts BETWEEN first_open_ts AND TIMESTAMP_ADD(first_open_ts, INTERVAL 7 DAY)) AS d7MeaningfulActivationUsers
FROM joined
GROUP BY cohort_date
ORDER BY cohort_date
"""


def firebase_activation_value_total_query(table: str) -> str:
    return f"""
SELECT
  SUM(COALESCE((SELECT value.int_value FROM UNNEST(event_params) WHERE key = '{ACTIVATION_VALUE_PARAMETER}'), 0)) AS totalActivationValue
FROM {table}
WHERE event_name = '{ACTIVATION_EVENT}'
  AND REGEXP_CONTAINS(_TABLE_SUFFIX, r'^\\d{{8}}$')
"""


def firebase_dau_daily_query(table: str, start: date, end: date) -> str:
    return f"""
SELECT
  PARSE_DATE('%Y%m%d', event_date) AS date,
  COUNT(DISTINCT user_pseudo_id) AS dau
FROM {table}
WHERE _TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  AND event_date BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
GROUP BY date
ORDER BY date
"""


def firebase_mau_query(table: str, start: date, end: date) -> str:
    return f"""
SELECT
  COUNT(DISTINCT user_pseudo_id) AS mau
FROM {table}
WHERE _TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  AND event_date BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
"""


def merge_firebase_dau_daily(daily: list[dict[str, Any]], dau_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dau_by_date = {}
    for row in dau_rows:
        day = parse_day(row.get("date"))
        if day:
            dau_by_date[day.isoformat()] = tidy(number(row.get("dau")))
    for item in daily:
        item["dau"] = dau_by_date.get(item["date"], 0)
    return daily


def avg_metric(items: list[dict[str, Any]], key: str) -> float:
    values = [float(item.get(key) or 0) for item in items if float(item.get(key) or 0) > 0]
    if not values:
        return 0.0
    return sum(values) / len(values)


def firebase_engagement_overview_query(table: str, start: date, end: date) -> str:
    return f"""
SELECT
  COUNT(DISTINCT IF(event_name = '{FIRST_OPEN_EVENT}', user_pseudo_id, NULL)) AS firstOpenUsers,
  COUNT(DISTINCT IF(event_name = '{PAYWALL_EVENT}', user_pseudo_id, NULL)) AS paywallSeenUsers,
  COUNT(DISTINCT IF(event_name = '{PRIMARY_FEATURE_EVENT}', user_pseudo_id, NULL)) AS primaryFeatureUsers,
  COUNT(DISTINCT IF(event_name = '{SECONDARY_FEATURE_EVENT}', user_pseudo_id, NULL)) AS secondaryFeatureUsers
FROM {table}
WHERE _TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  AND event_date BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'
  AND event_name IN (
    '{FIRST_OPEN_EVENT}',
    '{PAYWALL_EVENT}',
    '{PRIMARY_FEATURE_EVENT}',
    '{SECONDARY_FEATURE_EVENT}'
  )
"""


def read_rows(raw_dir: Path, pattern: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(raw_dir.rglob(pattern)):
        if path.suffix == ".gz":
            continue
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle, delimiter="\t"))
    return rows


def probe_ga4_export(project_id: str, dataset_id: str | None) -> dict[str, Any]:
    """Lightweight GA4/BigQuery availability check for the dashboard status API."""
    credentials = apply_google_credentials_default()
    if not credentials:
        return {
            "status": "not_configured",
            "available": False,
            "note": "No service-account JSON found. Configure firebase.credentialFiles or GOOGLE_APPLICATION_CREDENTIALS.",
        }
    if not Path(credentials).exists():
        return {
            "status": "missing",
            "available": False,
            "note": f"Credentials file missing: {credentials}",
        }
    try:
        token = service_account_access_token(Path(credentials))
        resolved_dataset = dataset_id
        if not resolved_dataset:
            resolved_dataset = discover_analytics_dataset(token, project_id)
        validate_bigquery_identifier(resolved_dataset, "dataset")
        return {
            "status": "ok",
            "available": True,
            "dataset": resolved_dataset,
            "note": f"GA4 BigQuery export is available at {project_id}.{resolved_dataset}.",
        }
    except Exception as error:
        message = str(error)
        return {
            "status": "unavailable",
            "available": False,
            "dataset": dataset_id,
            "note": message,
        }


def service_account_access_token(credentials_path: Path) -> str:
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    now = int(datetime.now(timezone.utc).timestamp())
    assertion = jwt.encode(
        {
            "iss": credentials["client_email"],
            "scope": "https://www.googleapis.com/auth/bigquery",
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        },
        credentials["private_key"],
        algorithm="RS256",
    )
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=30,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def run_bigquery_query(token: str, project_id: str, query: str) -> list[dict[str, Any]]:
    response = requests.post(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "useLegacySql": False, "timeoutMs": 30000},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    job_reference = payload.get("jobReference", {})
    while not payload.get("jobComplete", True):
        job_id = job_reference.get("jobId")
        location = job_reference.get("location")
        params = {"location": location} if location else {}
        response = requests.get(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
    if "errors" in payload:
        message = "; ".join(error.get("message", str(error)) for error in payload["errors"])
        raise RuntimeError(message)
    return decode_bigquery_rows(payload)


def discover_analytics_dataset(token: str, project_id: str) -> str:
    response = requests.get(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/datasets",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    datasets = response.json().get("datasets", [])
    analytics = [
        item.get("datasetReference", {}).get("datasetId")
        for item in datasets
        if str(item.get("datasetReference", {}).get("datasetId", "")).startswith("analytics_")
    ]
    analytics = [dataset for dataset in analytics if dataset]
    if not analytics:
        raise RuntimeError("No analytics_* BigQuery dataset found. Set FIREBASE_BQ_DATASET explicitly after the GA4 export appears.")
    if len(analytics) > 1:
        raise RuntimeError("Multiple analytics_* datasets found. Set FIREBASE_BQ_DATASET to the correct GA4 export dataset.")
    return str(analytics[0])


def decode_bigquery_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = payload.get("schema", {}).get("fields", [])
    names = [field["name"] for field in fields]
    decoded = []
    for row in payload.get("rows", []):
        values = [cell.get("v") for cell in row.get("f", [])]
        decoded.append({name: coerce_bigquery_value(value) for name, value in zip(names, values)})
    return decoded


def coerce_bigquery_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if text == "":
        return ""
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def normalize_firebase_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        day = parse_day(row.get("date"))
        if not day:
            continue
        normalized.append({
            "date": day.isoformat(),
            "first_open": tidy(number(row.get("first_open"))),
            "permission_viewed": tidy(number(row.get("permission_viewed"))),
            "permission_accepted": tidy(number(row.get("permission_accepted"))),
            "initial_load_completed": tidy(number(row.get("initial_load_completed"))),
            "activation_completed": tidy(number(row.get("activation_completed"))),
            "activation_value": tidy(number(row.get("activation_value"))),
            "paywall_presented": tidy(number(row.get("paywall_presented"))),
            "primary_feature_used": tidy(number(row.get("primary_feature_used"))),
            "secondary_feature_used": tidy(number(row.get("secondary_feature_used"))),
            "dau": tidy(number(row.get("dau"))),
        })
    return normalized


def normalize_firebase_activation_cohorts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        day = parse_day(row.get("date"))
        if not day:
            continue
        d0_base = number(row.get("d0EligibleFirstOpenUsers"))
        d3_base = number(row.get("d3EligibleFirstOpenUsers"))
        d5_base = number(row.get("d5EligibleFirstOpenUsers"))
        d7_base = number(row.get("d7EligibleFirstOpenUsers"))
        d0_users = number(row.get("d0MeaningfulActivationUsers"))
        d3_users = number(row.get("d3MeaningfulActivationUsers"))
        d5_users = number(row.get("d5MeaningfulActivationUsers"))
        d7_users = number(row.get("d7MeaningfulActivationUsers"))
        normalized.append({
            "date": day.isoformat(),
            "firstOpenUsers": tidy(number(row.get("firstOpenUsers"))),
            "d0MeaningfulActivationRate": ratio(d0_users, d0_base),
            "d0MeaningfulActivationUsers": tidy(d0_users),
            "d0EligibleFirstOpenUsers": tidy(d0_base),
            "d3MeaningfulActivationRate": ratio(d3_users, d3_base),
            "d3MeaningfulActivationUsers": tidy(d3_users),
            "d3EligibleFirstOpenUsers": tidy(d3_base),
            "d5MeaningfulActivationRate": ratio(d5_users, d5_base),
            "d5MeaningfulActivationUsers": tidy(d5_users),
            "d5EligibleFirstOpenUsers": tidy(d5_base),
            "d7MeaningfulActivationRate": ratio(d7_users, d7_base),
            "d7MeaningfulActivationUsers": tidy(d7_users),
            "d7EligibleFirstOpenUsers": tidy(d7_base),
        })
    return normalized


def daily_entry(daily: dict[str, dict[str, Any]], day: date) -> dict[str, Any]:
    key = day.isoformat()
    if key not in daily:
        daily[key] = {
            "date": key,
            "firstTimeDownloads": 0,
            "downloads": 0,
            "installs": 0,
            "firstTimeInstallSuccess": 0,
            "installFailures": 0,
            "deletions": 0,
            "sessions": 0,
            "sessionSeconds": 0,
            "impressions": 0,
            "uniqueImpressions": 0,
            "taps": 0,
            "uniqueTaps": 0,
            "productPageViews": 0,
            "uniqueProductPageViews": 0,
            "webProductPageViews": 0,
            "purchases": 0,
            "salesUsd": 0,
            "proceedsUsd": 0,
            "subscriptionEvents": 0,
            "subscriptionStateRows": 0,
            "platformInstalls": 0,
        }
    return daily[key]


def tidy_daily(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: tidy(value) if isinstance(value, float) else value for key, value in entry.items()}


def compare(current: float, prior: float) -> dict[str, Any]:
    return {
        "current": tidy(current),
        "prior": tidy(prior),
        "delta": tidy(current - prior),
        "deltaPct": None if prior == 0 else round(((current - prior) / prior) * 100, 1),
    }


def sum_metric(items: list[dict[str, Any]], key: str) -> float:
    return sum(float(item.get(key) or 0) for item in items)


def top_counter(counter: Counter[str], limit: int = 8) -> list[dict[str, Any]]:
    total = sum(counter.values())
    return [
        {"name": key, "value": tidy(value), "share": None if total == 0 else round(value / total, 3)}
        for key, value in counter.most_common(limit)
    ]


def build_asc_active_subscription_breakdown(rows: list[dict[str, str]]) -> dict[str, Any]:
    latest_day = max((parse_day(row.get("Date")) for row in rows), default=None)
    if not latest_day:
        return {"date": None, "total": 0, "products": [], "source": "App Store Connect Subscription State Report"}

    products: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    total = 0.0
    for row in rows:
        day = parse_day(row.get("Date"))
        if day != latest_day or not is_active_paid_subscription_state(row.get("State Metric")):
            continue
        value = number(row.get("Counts"))
        if value <= 0:
            continue
        name = str(row.get("Subscription Name") or "Unknown subscription").strip()
        duration = str(row.get("Subscription Duration") or "").strip()
        group = str(row.get("Subscription Group") or "").strip()
        identifier = str(row.get("Subscription Identifier") or "").strip()
        key = (name, duration, group, identifier)
        item = products.setdefault(
            key,
            {
                "name": name,
                "duration": duration,
                "group": group,
                "identifier": identifier,
                "value": 0.0,
                "stateMetrics": set(),
            },
        )
        item["value"] += value
        item["stateMetrics"].add(str(row.get("State Metric") or "").strip())
        total += value

    product_list: list[dict[str, Any]] = []
    for item in products.values():
        value = number(item["value"])
        product_list.append(
            {
                "name": item["name"],
                "duration": item["duration"],
                "group": item["group"],
                "identifier": item["identifier"],
                "value": tidy(value),
                "share": None if total == 0 else round(value / total, 3),
                "stateMetrics": sorted(metric for metric in item["stateMetrics"] if metric),
            }
        )

    return {
        "date": latest_day.isoformat(),
        "total": tidy(total),
        "products": sorted(product_list, key=lambda item: number(item["value"]), reverse=True),
        "source": "App Store Connect Subscription State Report active paid states, grouped by subscription name, duration, and group.",
    }


def is_active_paid_subscription_state(value: str | None) -> bool:
    state = str(value or "").strip().lower()
    if not state:
        return False
    inactive_terms = ("churn", "cancel", "refund")
    if any(term in state for term in inactive_terms):
        return False
    if "trial" in state:
        return False
    return True


def validate_bigquery_identifier(value: str, label: str) -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not value or any(character not in allowed for character in value):
        raise ValueError(f"Invalid BigQuery {label} identifier: {value}")


def ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def parse_day(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def number(value: Any) -> float:
    if value in (None, ""):
        return 0
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return 0


def tidy(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 2)


TERRITORY_META: dict[str, tuple[str, str, str]] = {
    "USA": ("US", "United States", "en"),
    "GBR": ("GB", "United Kingdom", "en"),
    "CAN": ("CA", "Canada", "en"),
    "AUS": ("AU", "Australia", "en"),
    "DEU": ("DE", "Germany", "de"),
    "FRA": ("FR", "France", "fr"),
    "ESP": ("ES", "Spain", "es"),
    "ITA": ("IT", "Italy", "it"),
    "NLD": ("NL", "Netherlands", "nl"),
    "SWE": ("SE", "Sweden", "sv"),
    "NOR": ("NO", "Norway", "no"),
    "DNK": ("DK", "Denmark", "da"),
    "FIN": ("FI", "Finland", "fi"),
    "PRT": ("PT", "Portugal", "pt"),
    "BRA": ("BR", "Brazil", "pt"),
    "MEX": ("MX", "Mexico", "es"),
    "JPN": ("JP", "Japan", "ja"),
    "KOR": ("KR", "South Korea", "ko"),
    "CHN": ("CN", "China", "zh"),
    "TWN": ("TW", "Taiwan", "zh"),
    "HKG": ("HK", "Hong Kong", "zh"),
    "SGP": ("SG", "Singapore", "en"),
    "IND": ("IN", "India", "en"),
    "ISR": ("IL", "Israel", "he"),
    "POL": ("PL", "Poland", "pl"),
    "CHE": ("CH", "Switzerland", "de"),
    "AUT": ("AT", "Austria", "de"),
    "BEL": ("BE", "Belgium", "nl"),
    "IRL": ("IE", "Ireland", "en"),
    "NZL": ("NZ", "New Zealand", "en"),
}

LANGUAGE_LABELS = {
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "he": "Hebrew",
    "pl": "Polish",
}

POSITIVE_REVIEW_THEME_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("simple", "clean", "minimal", "intuitive"), "Simple, clean interface"),
    (("easy", "easier", "quick", "fast"), "Easy day-to-day use"),
    (("useful", "helpful", "handy", "valuable"), "Useful core experience"),
    (("design", "beautiful", "polished", "thoughtful"), "Strong product design"),
    (("reliable", "smooth", "works well"), "Reliable performance"),
    (("developer", "support", "responsive", "well done"), "Positive developer experience"),
    (("recommend", "love", "great", "excellent", "amazing", "fantastic"), "Strong overall satisfaction"),
]

NEGATIVE_REVIEW_THEME_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("crash", "crashes", "bug", "bugs", "freeze"), "Stability issues"),
    (("expensive", "overpriced", "too expensive", "paywall", "subscription"), "Pricing or subscription concerns"),
    (("instructions", "tutorial", "how to", "confusing"), "Onboarding or guidance gaps"),
    (("slow", "lag", "loading", "hang"), "Performance complaints"),
    (("permission", "access denied", "privacy"), "Permission or privacy friction"),
    (("doesn't work", "does not work", "not working", "broken"), "Reliability issues"),
    (("missing", "wish it", "would like", "need a"), "Missing features"),
]


def summarize_app_store_reviews_ratings_only(app_id: str) -> dict[str, Any]:
    """Lightweight ratings + histogram for overview; defers written reviews to the Reviews tab."""
    try:
        ratings_payload = run_json([
            "asc",
            "reviews",
            "ratings",
            "--app",
            app_id,
            "--all",
            "--output",
            "json",
        ])
    except RuntimeError as error:
        return {
            "status": "error",
            "note": f"App Store ratings fetch failed: {error}",
            "reviews": [],
            "countries": [],
            "insights": {"positiveThemes": [], "negativeThemes": []},
            "ratings": {},
            "reviewsLoaded": False,
        }

    ratings = normalize_review_ratings(ratings_payload)
    countries = [
        {
            "code": item.get("country"),
            "name": item.get("countryName") or item.get("country"),
            "reviewCount": 0,
        }
        for item in ratings.get("byCountry", [])
        if item.get("country")
    ]
    return {
        "status": "ok",
        "note": "Ratings summary loaded. Open Reviews to fetch written reviews.",
        "reviewCount": 0,
        "ratings": ratings,
        "insights": {"positiveThemes": [], "negativeThemes": []},
        "countries": countries,
        "reviews": [],
        "reviewsLoaded": False,
    }


def summarize_app_store_reviews(app_id: str) -> dict[str, Any]:
    try:
        reviews_payload = run_json([
            "asc",
            "reviews",
            "--app",
            app_id,
            "--paginate",
            "--output",
            "json",
        ])
        ratings_payload = run_json([
            "asc",
            "reviews",
            "ratings",
            "--app",
            app_id,
            "--all",
            "--output",
            "json",
        ])
    except RuntimeError as error:
        return {
            "status": "error",
            "note": f"App Store review fetch failed: {error}",
            "reviews": [],
            "countries": [],
            "insights": {},
            "ratings": {},
        }

    ratings = normalize_review_ratings(ratings_payload)
    country_lookup = {
        item["country"]: item["countryName"]
        for item in ratings.get("byCountry", [])
        if item.get("country")
    }
    raw_reviews = reviews_payload.get("data") or []
    reviews = [normalize_customer_review(item, country_lookup) for item in raw_reviews if isinstance(item, dict)]
    reviews.sort(key=lambda item: item.get("createdDate") or "")
    attach_review_app_versions(app_id, reviews)
    attach_developer_responses(reviews)
    for review in reviews:
        apply_review_translation(review)
    insights = build_review_insights(reviews)
    countries = build_review_countries(reviews, ratings)
    return {
        "status": "ok",
        "note": f"Loaded {len(reviews)} App Store review(s) from App Store Connect.",
        "reviewCount": len(reviews),
        "ratings": ratings,
        "insights": insights,
        "countries": countries,
        "reviews": reviews,
        "reviewsLoaded": True,
    }


def normalize_review_ratings(payload: dict[str, Any]) -> dict[str, Any]:
    by_country = []
    for item in payload.get("byCountry") or []:
        if not isinstance(item, dict):
            continue
        by_country.append(
            {
                "country": item.get("country"),
                "countryName": item.get("countryName") or item.get("country"),
                "averageRating": tidy(number(item.get("averageRating"))),
                "ratingCount": tidy(number(item.get("ratingCount"))),
                "histogram": item.get("histogram") or {},
            }
        )
    histogram = payload.get("histogram") or {}
    return {
        "averageRating": tidy(number(payload.get("averageRating"))),
        "totalCount": tidy(number(payload.get("totalCount"))),
        "countryCount": tidy(number(payload.get("countryCount"))),
        "histogram": {str(key): tidy(number(value)) for key, value in histogram.items()},
        "byCountry": by_country,
    }


def normalize_customer_review(item: dict[str, Any], country_lookup: dict[str, str]) -> dict[str, Any]:
    attributes = item.get("attributes") or {}
    territory = str(attributes.get("territory") or "").upper()
    country_code, country_name, language_code = territory_meta(territory, country_lookup)
    title = str(attributes.get("title") or "").strip()
    body = str(attributes.get("body") or "").strip()
    return {
        "id": item.get("id"),
        "rating": tidy(number(attributes.get("rating"))),
        "title": title,
        "body": body,
        "displayTitle": title,
        "displayBody": body,
        "originalTitle": None,
        "originalBody": None,
        "originalLanguage": None,
        "originalLanguageLabel": None,
        "translated": False,
        "reviewerNickname": attributes.get("reviewerNickname"),
        "createdDate": attributes.get("createdDate"),
        "territory": territory,
        "countryCode": country_code,
        "countryName": country_name,
        "languageCode": language_code,
        "appVersion": None,
    }


def attach_review_app_versions(app_id: str, reviews: list[dict[str, Any]]) -> None:
    """Map each review to its App Store version via per-version customer review listings."""
    if not reviews:
        return
    review_versions: dict[str, str] = {}
    try:
        versions_payload = run_json([
            "asc",
            "versions",
            "list",
            "--app",
            app_id,
            "--platform",
            "IOS",
            "--paginate",
            "--output",
            "json",
        ])
    except RuntimeError:
        versions_payload = {"data": []}

    for version_item in versions_payload.get("data") or []:
        if not isinstance(version_item, dict):
            continue
        version_id = version_item.get("id")
        attributes = version_item.get("attributes") or {}
        version_string = str(attributes.get("versionString") or "").strip()
        if not version_id or not version_string:
            continue
        try:
            version_reviews_payload = run_json([
                "asc",
                "versions",
                "customer-reviews",
                "list",
                "--version-id",
                str(version_id),
                "--paginate",
                "--output",
                "json",
            ])
        except RuntimeError:
            continue
        for item in version_reviews_payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            review_id = item.get("id")
            if review_id:
                review_versions[str(review_id)] = version_string

    for review in reviews:
        review_id = review.get("id")
        review["appVersion"] = review_versions.get(str(review_id)) if review_id else None


def territory_meta(territory: str, country_lookup: dict[str, str]) -> tuple[str, str, str]:
    if territory in TERRITORY_META:
        country_code, country_name, language_code = TERRITORY_META[territory]
        return country_code, country_lookup.get(country_code, country_name), language_code
    country_code = territory[:2] if len(territory) == 3 else territory
    country_name = country_lookup.get(country_code, country_code or territory or "Unknown")
    return country_code, country_name, "en"


def attach_developer_responses(reviews: list[dict[str, Any]]) -> None:
    for review in reviews:
        review_id = review.get("id")
        if not review_id:
            review["developerResponse"] = None
            continue
        language_code = str(review.get("languageCode") or "en")
        review["developerResponse"] = fetch_developer_review_response(str(review_id), language_code)


def fetch_developer_review_response(review_id: str, language_code: str) -> dict[str, Any] | None:
    try:
        payload = run_json([
            "asc",
            "reviews",
            "response",
            "for-review",
            "--review-id",
            review_id,
            "--output",
            "json",
        ])
    except RuntimeError:
        return None
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    attributes = data.get("attributes") or {}
    body = str(attributes.get("responseBody") or "").strip()
    if not body:
        return None
    response: dict[str, Any] = {
        "body": body,
        "displayBody": body,
        "createdDate": attributes.get("lastModifiedDate") or attributes.get("createdDate"),
        "translated": False,
        "originalBody": None,
        "originalLanguageLabel": None,
    }
    if language_code != "en":
        translated_body, translated = translate_text_to_english(body, language_code)
        if translated:
            response["originalBody"] = body
            response["displayBody"] = translated_body
            response["translated"] = True
            response["originalLanguageLabel"] = LANGUAGE_LABELS.get(language_code, language_code.upper())
    return response


def apply_review_translation(review: dict[str, Any]) -> None:
    language_code = review.get("languageCode") or "en"
    combined = f"{review.get('title', '')}\n{review.get('body', '')}".strip()
    if not combined or language_code == "en":
        return
    translated_title, title_translated = translate_text_to_english(review.get("title") or "", language_code)
    translated_body, body_translated = translate_text_to_english(review.get("body") or "", language_code)
    if not title_translated and not body_translated:
        return
    review["originalTitle"] = review.get("title")
    review["originalBody"] = review.get("body")
    review["displayTitle"] = translated_title or review.get("title")
    review["displayBody"] = translated_body or review.get("body")
    review["originalLanguage"] = language_code
    review["originalLanguageLabel"] = LANGUAGE_LABELS.get(language_code, language_code.upper())
    review["translated"] = True


def translate_text_to_english(text: str, source_lang: str) -> tuple[str, bool]:
    cleaned = text.strip()
    if not cleaned or source_lang == "en":
        return text, False
    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": cleaned[:500], "langpair": f"{source_lang}|en"},
            timeout=20,
        )
        response.raise_for_status()
        translated = str((response.json().get("responseData") or {}).get("translatedText") or "").strip()
        if translated and translated.lower() != cleaned.lower():
            return translated, True
    except Exception:
        pass
    return text, False


def build_review_countries(reviews: list[dict[str, Any]], ratings: dict[str, Any]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for review in reviews:
        code = review.get("countryCode")
        if not code:
            continue
        counts[code] += 1
        names[code] = review.get("countryName") or names.get(code) or code
    for item in ratings.get("byCountry") or []:
        code = item.get("country")
        if not code:
            continue
        names[code] = item.get("countryName") or names.get(code) or code
    countries = []
    for code, count in counts.most_common():
        countries.append({"code": code, "name": names.get(code, code), "reviewCount": count})
    return countries


def build_review_insights(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    if not reviews:
        return {
            "positiveSummary": "No written reviews yet.",
            "negativeSummary": "No negative themes detected.",
            "positiveThemes": [],
            "negativeThemes": [],
        }
    # Scan all written reviews so mixed 3–4★ feedback still surfaces in sentiment bullets.
    positive_themes = extract_review_themes(reviews, POSITIVE_REVIEW_THEME_HINTS)
    negative_themes = extract_review_themes(reviews, NEGATIVE_REVIEW_THEME_HINTS)
    high_ratings = [review for review in reviews if number(review.get("rating")) >= 4]
    low_and_mixed = [review for review in reviews if number(review.get("rating")) <= 4]
    return {
        "positiveSummary": summarize_review_themes(
            positive_themes,
            reviews,
            fallback="No strong positive themes repeated across reviews yet.",
        ),
        "negativeSummary": summarize_review_themes(
            negative_themes,
            reviews,
            fallback="No recurring concerns called out across reviews.",
            negative=True,
        ),
        "positiveThemes": positive_themes,
        "negativeThemes": negative_themes,
        "positiveReviewCount": len(high_ratings),
        "negativeReviewCount": len(low_and_mixed),
        "reviewCount": len(reviews),
    }


def extract_review_themes(reviews: list[dict[str, Any]], hints: list[tuple[tuple[str, ...], str]]) -> list[str]:
    if not reviews:
        return []
    review_count = len(reviews)
    minimum_mentions = 1 if review_count <= 6 else 2
    scores: Counter[str] = Counter()
    for review in reviews:
        text = review_text_for_insights(review)
        matched_labels: set[str] = set()
        for keywords, label in hints:
            if label in matched_labels:
                continue
            if theme_matches_text(text, keywords):
                scores[label] += 1
                matched_labels.add(label)
    return [label for label, count in scores.most_common(5) if count >= minimum_mentions]


def theme_matches_text(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if " " in keyword:
            if keyword in text:
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            return True
    return False


def review_text_for_insights(review: dict[str, Any]) -> str:
    title = str(review.get("displayTitle") or review.get("title") or "")
    body = str(review.get("displayBody") or review.get("body") or "")
    return f"{title} {body}".lower()


def summarize_review_themes(
    themes: list[str],
    reviews: list[dict[str, Any]],
    fallback: str,
    negative: bool = False,
) -> str:
    if not reviews:
        return fallback
    if not themes:
        return fallback
    lead = "Main concerns" if negative else "What users praise"
    return f"{lead}: {', '.join(themes)}."


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
