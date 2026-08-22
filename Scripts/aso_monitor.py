"""Build ASO experiment monitor payload for the metrics dashboard."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dashboard_config import load_config

RATE_METRICS = {
    "tapThroughRate": ("taps", "impressions"),
    "pageConversionRate": ("firstTimeDownloads", "productPageViews"),
}
COUNT_METRICS = ("impressions", "firstTimeDownloads", "productPageViews")


def config_path() -> Path | None:
    return load_config().content_path("asoMonitor")


def asc_metadata_root() -> Path:
    config = load_config()
    raw = config.content.get("ascMetadataRoot")
    return config.resolve_from_workspace(str(raw or "asc-metadata"))


def display_config_path() -> str:
    path = config_path()
    if not path:
        return "content.asoMonitor"
    try:
        return str(path.relative_to(load_config().workspace_root))
    except ValueError:
        return str(path)


def load_aso_monitor_config() -> dict[str, Any]:
    path = config_path()
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def locale_app_info_path(locale: str) -> Path:
    return asc_metadata_root() / "app-info" / f"{locale}.json"


def locale_version_path(version: str, locale: str) -> Path:
    return asc_metadata_root() / "version" / version / f"{locale}.json"


def metadata_snapshot_for_locale(
    locale: str,
    version: str,
    baseline_meta: dict[str, Any] | None,
    *,
    use_live_subtitle: bool,
) -> dict[str, Any]:
    app_info = read_json(locale_app_info_path(locale))
    version_info = read_json(locale_version_path(version, locale))
    baseline_entry = (baseline_meta or {}).get(locale, {}) if baseline_meta else {}
    if use_live_subtitle:
        subtitle = app_info.get("subtitle") or baseline_entry.get("subtitle")
    else:
        subtitle = baseline_entry.get("subtitle") or app_info.get("subtitle")
    keywords = version_info.get("keywords") or baseline_entry.get("keywords")
    return {
        "locale": locale,
        "subtitle": subtitle,
        "keywords": keywords,
        "name": app_info.get("name"),
    }


def territory_download_share(territories: list[dict[str, Any]], names: list[str]) -> float | None:
    if not territories:
        return None
    total = sum(float(item.get("value") or 0) for item in territories)
    if total <= 0:
        return None
    wanted = {name.casefold() for name in names}
    matched = 0.0
    for item in territories:
        label = str(item.get("name") or "")
        iso = label.upper()
        if label.casefold() in wanted or iso in wanted:
            matched += float(item.get("value") or 0)
    return matched / total if total else None


def search_download_share(source_types: list[dict[str, Any]]) -> float | None:
    if not source_types:
        return None
    total = sum(float(item.get("value") or 0) for item in source_types)
    if total <= 0:
        return None
    search = 0.0
    for item in source_types:
        name = str(item.get("name") or "")
        if "search" in name.casefold():
            search += float(item.get("value") or 0)
    return search / total


def compare_to_baseline(current: float | None, baseline: float | None) -> dict[str, Any]:
    if current is None or baseline is None:
        return {"current": current, "baseline": baseline, "deltaPct": None, "direction": None}
    if baseline == 0:
        delta_pct = None if current == 0 else 100.0
    else:
        delta_pct = round(((current - baseline) / baseline) * 100, 1)
    direction = None
    if delta_pct is not None:
        if delta_pct > 0.5:
            direction = "up"
        elif delta_pct < -0.5:
            direction = "down"
        else:
            direction = "flat"
    return {"current": current, "baseline": baseline, "deltaPct": delta_pct, "direction": direction}


def window_daily_average(total: float, data_days: int) -> float | None:
    if data_days < 1:
        return None
    return total / data_days


def compare_release_count_metric(
    post: dict[str, Any],
    pre: dict[str, Any],
    key: str,
    *,
    phase: str,
) -> dict[str, Any]:
    """Compare count metrics; use per-day averages while post window is still collecting."""
    pre_total = metric_number(pre.get(key))
    post_total = metric_number(post.get(key))
    pre_days = int(pre.get("dataDays") or 0)
    post_days = int(post.get("dataDays") or 0)
    total_cmp = compare_to_baseline(post_total, pre_total)
    comparison: dict[str, Any] = {
        "current": post_total,
        "baseline": pre_total,
        "deltaPctWindowTotal": total_cmp.get("deltaPct"),
        "deltaBasis": "window_total",
        "deltaPct": total_cmp.get("deltaPct"),
        "direction": total_cmp.get("direction"),
    }
    if phase == "collecting":
        per_day_cmp = compare_to_baseline(
            window_daily_average(post_total, post_days),
            window_daily_average(pre_total, pre_days),
        )
        comparison["deltaPct"] = per_day_cmp.get("deltaPct")
        comparison["direction"] = per_day_cmp.get("direction")
        comparison["deltaBasis"] = "per_day"
        comparison["currentPerDay"] = window_daily_average(post_total, post_days)
        comparison["baselinePerDay"] = window_daily_average(pre_total, pre_days)
    return comparison


def gate_significance_for_post_window(
    significance: dict[str, Any],
    *,
    post_data_days: int,
    min_days: int,
) -> dict[str, Any]:
    if post_data_days >= min_days:
        return significance
    method = str(significance.get("method") or "welch_t")
    alpha = float(significance.get("alpha") or 0.05)
    return _significance_untested(
        method,
        alpha,
        label=f"Need ≥{min_days} post-release days ({post_data_days} loaded)",
    )


def parse_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def metric_number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normal_survival(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_proportion_significance(
    successes_a: float,
    trials_a: float,
    successes_b: float,
    trials_b: float,
    *,
    alpha: float,
) -> dict[str, Any]:
    if trials_a < 1 or trials_b < 1:
        return _significance_untested("two_proportion_z", alpha, label="Need page views or impressions in window")
    p_a = successes_a / trials_a
    p_b = successes_b / trials_b
    pooled = (successes_a + successes_b) / (trials_a + trials_b)
    variance = pooled * (1.0 - pooled) * ((1.0 / trials_a) + (1.0 / trials_b))
    if variance <= 0:
        return _significance_result(False, 1.0, "two_proportion_z", alpha)
    z = (p_a - p_b) / math.sqrt(variance)
    p_value = 2.0 * (1.0 - normal_survival(abs(z)))
    return _significance_result(p_value < alpha, p_value, "two_proportion_z", alpha)


def welch_t_significance(samples_a: list[float], samples_b: list[float], *, alpha: float) -> dict[str, Any]:
    if len(samples_a) < 2 or len(samples_b) < 2:
        short = min(len(samples_a), len(samples_b))
        return _significance_untested(
            "welch_t",
            alpha,
            label=f"Need ≥2 days per window ({short} loaded)",
        )
    mean_a = sum(samples_a) / len(samples_a)
    mean_b = sum(samples_b) / len(samples_b)
    var_a = sum((value - mean_a) ** 2 for value in samples_a) / (len(samples_a) - 1)
    var_b = sum((value - mean_b) ** 2 for value in samples_b) / (len(samples_b) - 1)
    denom = math.sqrt((var_a / len(samples_a)) + (var_b / len(samples_b)))
    if denom <= 0:
        return _significance_result(False, 1.0, "welch_t", alpha)
    t = (mean_a - mean_b) / denom
    p_value = 2.0 * (1.0 - normal_survival(abs(t)))
    return _significance_result(p_value < alpha, p_value, "welch_t", alpha)


def _significance_untested(method: str, alpha: float, *, label: str | None = None) -> dict[str, Any]:
    return {
        "testable": False,
        "significant": None,
        "pValue": None,
        "alpha": alpha,
        "method": method,
        "label": label or "Need ≥2 days per window",
    }


def _significance_result(significant: bool, p_value: float, method: str, alpha: float) -> dict[str, Any]:
    rounded = round(min(1.0, max(0.0, p_value)), 4)
    if significant:
        label = f"Significant (p={rounded:g})"
    else:
        label = f"Not significant (p={rounded:g})"
    return {
        "testable": True,
        "significant": significant,
        "pValue": rounded,
        "alpha": alpha,
        "method": method,
        "label": label,
    }


def aggregate_daily_window(daily: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    rows = [
        row
        for row in daily
        if (day := parse_day(row.get("date"))) is not None and start <= day <= end
    ]
    rows.sort(key=lambda row: row.get("date") or "")
    calendar_days = (end - start).days + 1
    if not rows:
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendarDays": calendar_days,
            "dataDays": 0,
            "complete": False,
            "impressions": None,
            "taps": None,
            "productPageViews": None,
            "firstTimeDownloads": None,
            "tapThroughRate": None,
            "pageConversionRate": None,
            "dailyImpressions": [],
            "dailyFirstTimeDownloads": [],
            "dailyProductPageViews": [],
        }

    impressions = sum(metric_number(row.get("impressions")) for row in rows)
    taps = sum(metric_number(row.get("taps")) for row in rows)
    product_page_views = sum(metric_number(row.get("productPageViews")) for row in rows)
    first_time_downloads = sum(metric_number(row.get("firstTimeDownloads")) for row in rows)
    return {
        "start": rows[0].get("date"),
        "end": rows[-1].get("date"),
        "calendarDays": calendar_days,
        "dataDays": len(rows),
        "complete": len(rows) >= calendar_days,
        "impressions": impressions,
        "taps": taps,
        "productPageViews": product_page_views,
        "firstTimeDownloads": first_time_downloads,
        "tapThroughRate": (taps / impressions) if impressions else None,
        "pageConversionRate": (first_time_downloads / product_page_views) if product_page_views else None,
        "dailyImpressions": [metric_number(row.get("impressions")) for row in rows],
        "dailyFirstTimeDownloads": [metric_number(row.get("firstTimeDownloads")) for row in rows],
        "dailyProductPageViews": [metric_number(row.get("productPageViews")) for row in rows],
    }


def last_asc_day(daily: list[dict[str, Any]]) -> date | None:
    days = [day for row in daily if (day := parse_day(row.get("date")))]
    return max(days) if days else None


def build_release_monitoring(
    asc: dict[str, Any],
    experiment: dict[str, Any],
    monitoring_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    go_live = parse_day(experiment.get("goLiveDate"))
    if not go_live:
        return None

    window_days = int(monitoring_cfg.get("windowDays") or 14)
    alpha = float(monitoring_cfg.get("significanceAlpha") or 0.05)
    min_days = int(monitoring_cfg.get("minDaysPerWindow") or 7)

    today = date.today()
    daily = asc.get("daily") or []
    asc_last = last_asc_day(daily)

    # Pre: window_days calendar days ending the day before go-live (e.g. May 15–28 when go-live is May 29).
    pre_end = go_live - timedelta(days=1)
    pre_start = pre_end - timedelta(days=window_days - 1)
    post_start = go_live
    post_end_target = go_live + timedelta(days=window_days - 1)

    if asc_last:
        post_end_effective = min(post_end_target, asc_last)
    else:
        post_end_effective = min(post_end_target, today)

    pre = aggregate_daily_window(daily, pre_start, pre_end)
    post = aggregate_daily_window(daily, post_start, post_end_effective)

    pre_discovery_days = 0
    for row in daily:
        day = parse_day(row.get("date"))
        if not day or not (pre_start <= day <= pre_end):
            continue
        if metric_number(row.get("impressions")) > 0 or metric_number(row.get("productPageViews")) > 0:
            pre_discovery_days += 1

    if today < go_live:
        phase = "pre_release"
    elif post_end_effective < post_end_target:
        phase = "collecting"
    else:
        phase = "complete"

    post_calendar_elapsed = 0
    if today >= go_live:
        elapsed_end = min(today, post_end_target)
        post_calendar_elapsed = (elapsed_end - go_live).days + 1

    comparisons: dict[str, Any] = {}
    post_data_days = int(post.get("dataDays") or 0)
    for key in COUNT_METRICS:
        daily_key = {
            "impressions": "dailyImpressions",
            "firstTimeDownloads": "dailyFirstTimeDownloads",
            "productPageViews": "dailyProductPageViews",
        }[key]
        comparison = compare_release_count_metric(post, pre, key, phase=phase)
        significance = welch_t_significance(
            post.get(daily_key) or [],
            pre.get(daily_key) or [],
            alpha=alpha,
        )
        comparison["significance"] = gate_significance_for_post_window(
            significance,
            post_data_days=post_data_days,
            min_days=min_days,
        )
        comparisons[key] = comparison

    for rate_key, (numerator, denominator) in RATE_METRICS.items():
        comparison = compare_to_baseline(post.get(rate_key), pre.get(rate_key))
        significance = two_proportion_significance(
            metric_number(post.get(numerator)),
            metric_number(post.get(denominator)),
            metric_number(pre.get(numerator)),
            metric_number(pre.get(denominator)),
            alpha=alpha,
        )
        comparison["significance"] = gate_significance_for_post_window(
            significance,
            post_data_days=post_data_days,
            min_days=min_days,
        )
        comparisons[rate_key] = comparison

    pre_ready = pre["dataDays"] >= min_days
    post_ready = post["dataDays"] >= min_days and phase == "complete"
    ready_for_decision = pre_ready and post_ready

    notes: list[str] = []
    if phase == "pre_release":
        notes.append(f"Pre-release window ends {pre_end.isoformat()}; go-live {go_live.isoformat()}.")
    elif phase == "collecting":
        notes.append(
            f"Day {post_calendar_elapsed}/{window_days} post-release · "
            f"{post['dataDays']}/{post['calendarDays']} ASC days through {post.get('end') or '—'}."
        )
        notes.append(
            f"Change % uses per-day averages until {min_days}+ post-release days; "
            f"significance needs ≥{min_days} post days."
        )
    if not pre.get("complete"):
        notes.append(f"Pre window: {pre['dataDays']}/{pre['calendarDays']} ASC days loaded.")
    elif pre_discovery_days < pre.get("calendarDays", 0):
        notes.append(
            f"Pre discovery metrics cover {pre_discovery_days}/{pre.get('calendarDays')} days "
            f"({pre_start.isoformat()}–{pre_end.isoformat()}); downloads span all {pre.get('dataDays')} days."
        )
    if phase != "pre_release" and not post.get("complete"):
        notes.append("Post window still filling.")

    days_remaining = 0
    if phase == "collecting":
        days_remaining = max(0, (post_end_target - today).days)

    return {
        "windowDays": window_days,
        "goLiveDate": go_live.isoformat(),
        "monitoringEndDate": post_end_target.isoformat(),
        "phase": phase,
        "lastAscDate": asc_last.isoformat() if asc_last else None,
        "postDaysElapsed": post_calendar_elapsed,
        "postDaysTarget": window_days,
        "postDaysRemaining": days_remaining,
        "dailyRefreshRecommended": bool(monitoring_cfg.get("dailyRefreshRecommended")),
        "preWindow": {
            **pre,
            "discoveryDays": pre_discovery_days,
            "label": f"{window_days} days before go-live",
            "versionLabel": str(experiment.get("baselineVersion") or ""),
        },
        "postWindow": {
            **post,
            "label": f"Since go-live ({window_days}-day target)",
            "versionLabel": str(experiment.get("treatmentVersion") or ""),
            "targetEnd": post_end_target.isoformat(),
        },
        "comparisons": comparisons,
        "readyForDecision": ready_for_decision,
        "minDaysPerWindow": min_days,
        "significanceAlpha": alpha,
        "note": " ".join(notes),
    }


def build_aso_monitor_payload(
    asc: dict[str, Any],
    app_store_listing: dict[str, Any],
    firebase: dict[str, Any],
) -> dict[str, Any]:
    config = load_aso_monitor_config()
    if not config:
        return {"status": "missing", "note": "Configure content.asoMonitor to track metadata experiments."}

    experiment = config.get("experiment") or {}
    baseline = config.get("baseline") or {}
    baseline_metrics = baseline.get("metrics") or {}
    baseline_metadata = baseline.get("metadata") or {}
    baseline_version = str(experiment.get("baselineVersion") or "1.11.0")
    treatment_version = str(experiment.get("treatmentVersion") or "1.12.0")
    locales = list(experiment.get("locales") or ["en-US", "es-MX", "de-DE"])
    primary_locale = str(experiment.get("primaryLocale") or "en-US")
    monitoring_cfg = config.get("monitoring") or {}

    asc_metrics = asc.get("metrics") or {}
    current_values = {
        "impressions": metric_current(asc_metrics, "impressions"),
        "tapThroughRate": metric_current(asc_metrics, "tapThroughRate"),
        "productPageViews": metric_current(asc_metrics, "productPageViews"),
        "firstTimeDownloads": metric_current(asc_metrics, "firstTimeDownloads"),
        "pageConversionRate": metric_current(asc_metrics, "pageConversionRate"),
        "appStoreSearchDownloadShare": search_download_share(asc.get("sourceType") or []),
        "usDownloadShare": territory_download_share(
            asc.get("territories") or [],
            ["United States", "US"],
        ),
    }

    listing_metrics = (app_store_listing or {}).get("metrics") or {}
    rating_metric = listing_metrics.get("averageUserRating") or {}
    firebase_metrics = (firebase or {}).get("metrics") or {}
    meaningful = firebase_metrics.get("d3MeaningfulActivationRate") or {}
    guardrail_values = {
        "averageUserRating": rating_metric.get("current"),
        "d3MeaningfulActivationRate": meaningful.get("current"),
    }

    release_monitoring = build_release_monitoring(asc, experiment, monitoring_cfg)
    release_comparisons = (release_monitoring or {}).get("comparisons") or {}

    scorecard: dict[str, Any] = {}
    for key, current in current_values.items():
        if key in release_comparisons:
            scorecard[key] = release_comparisons[key]
        else:
            scorecard[key] = compare_to_baseline(current, baseline_metrics.get(key))

    guardrails: dict[str, Any] = {}
    for key in (config.get("winRules") or {}).get("guardrails") or []:
        guardrails[key] = compare_to_baseline(guardrail_values.get(key), baseline_metrics.get(key))

    metadata_by_locale: dict[str, Any] = {}
    for locale in locales:
        metadata_by_locale[locale] = {
            "before": metadata_snapshot_for_locale(
                locale,
                baseline_version,
                baseline_metadata,
                use_live_subtitle=False,
            ),
            "after": metadata_snapshot_for_locale(
                locale,
                treatment_version,
                baseline_metadata,
                use_live_subtitle=True,
            ),
        }

    win_rules = config.get("winRules") or {}
    primary_keys = list(win_rules.get("primary") or [])
    primary_wins = [
        key
        for key in primary_keys
        if (scorecard.get(key) or {}).get("direction") == "up"
        and ((scorecard.get(key) or {}).get("significance") or {}).get("significant") is True
    ]
    primary_regressions = [
        key
        for key in primary_keys
        if (scorecard.get(key) or {}).get("direction") == "down"
        and ((scorecard.get(key) or {}).get("significance") or {}).get("significant") is True
    ]
    direction_wins = [key for key in primary_keys if (scorecard.get(key) or {}).get("direction") == "up"]
    direction_regressions = [
        key for key in primary_keys if (scorecard.get(key) or {}).get("direction") == "down"
    ]

    period = asc.get("period") or {}
    asc_days = period.get("ascDaysAvailable")
    ready_for_decision = False
    if release_monitoring:
        ready_for_decision = bool(release_monitoring.get("readyForDecision"))
    else:
        ready_for_decision = bool(asc_days and asc_days >= 14)

    return {
        "status": "ok",
        "configPath": display_config_path(),
        "experiment": experiment,
        "monitoring": monitoring_cfg,
        "releaseMonitoring": release_monitoring,
        "baseline": {
            "capturedAt": baseline.get("capturedAt"),
            "label": baseline.get("label"),
            "ascDaysAvailable": baseline.get("ascDaysAvailable"),
            "note": baseline.get("note"),
        },
        "metadata": metadata_by_locale,
        "primaryLocale": primary_locale,
        "scorecard": scorecard,
        "guardrails": guardrails,
        "sourceType": asc.get("sourceType") or [],
        "territories": asc.get("territories") or [],
        "ascPeriod": period,
        "ascDaysAvailable": asc_days,
        "publicListing": {
            "version": app_store_listing.get("version"),
            "currentVersionReleaseDate": app_store_listing.get("currentVersionReleaseDate"),
            "country": app_store_listing.get("country"),
        },
        "summary": {
            "primaryWins": primary_wins,
            "primaryRegressions": primary_regressions,
            "directionWins": direction_wins,
            "directionRegressions": direction_regressions,
            "readyForDecision": ready_for_decision,
        },
    }


def metric_current(metrics: dict[str, Any], key: str) -> float | None:
    metric = metrics.get(key)
    if not metric:
        return None
    value = metric.get("current")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
