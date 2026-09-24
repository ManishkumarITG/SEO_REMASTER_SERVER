"""Google PageSpeed Insights client. Works keyless at low volume; results cached for 1 hour."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import get_settings
from utils.cache import get_cache

logger = logging.getLogger("seo.pagespeed")

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_TIMEOUT_SECONDS = 60.0  # PSI runs a full Lighthouse pass; it is slow


#: The single-page audit asks for performance only, because that is what its
#: analyzer reports. A per-template run is made once for a whole group of pages,
#: so the extra categories are worth their (identical) request cost.
PERFORMANCE_ONLY: tuple[str, ...] = ("PERFORMANCE",)
FULL_REPORT: tuple[str, ...] = ("PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO")


async def fetch_pagespeed(
    url: str,
    strategy: str = "mobile",
    categories: tuple[str, ...] = PERFORMANCE_ONLY,
) -> dict[str, Any] | None:
    """Simplified Lighthouse metrics for url. Returns None on any failure.

    `categories` is part of the cache key: a performance-only result must never
    be served to a caller that asked for the full report, or the extra category
    scores would silently come back as None.
    """
    settings = get_settings()
    cache = get_cache()
    wanted = tuple(categories) or PERFORMANCE_ONLY
    cache_key = f"psi:{strategy}:{','.join(wanted)}:{url}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    # httpx repeats a sequence value as `category=A&category=B`, which is how PSI
    # wants multiple categories.
    params: dict[str, Any] = {"url": url, "strategy": strategy, "category": list(wanted)}
    if settings.pagespeed_api_key:
        params["key"] = settings.pagespeed_api_key
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(PSI_ENDPOINT, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - PSI failures must never fail the audit
        logger.warning("PageSpeed fetch failed for %s: %s", url, exc)
        return None

    metrics = _extract_metrics(payload)
    await cache.set(cache_key, metrics, ttl_seconds=settings.cache_ttl_seconds)
    return metrics


def _extract_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the performance score + LCP / INP / CLS out of the (huge) PSI payload.

    Field (real-user CrUX) data is preferred; Lighthouse lab data is the fallback.
    """
    lighthouse = payload.get("lighthouseResult") or {}
    perf_raw = ((lighthouse.get("categories") or {}).get("performance") or {}).get("score")
    audits = lighthouse.get("audits") or {}
    field = (payload.get("loadingExperience") or {}).get("metrics") or {}

    def lab(audit_id: str) -> float | None:
        value = (audits.get(audit_id) or {}).get("numericValue")
        return float(value) if isinstance(value, (int, float)) else None

    def pct(metric: str) -> float | None:
        value = (field.get(metric) or {}).get("percentile")
        return float(value) if isinstance(value, (int, float)) else None

    lcp_ms = pct("LARGEST_CONTENTFUL_PAINT_MS")
    if lcp_ms is None:
        lcp_ms = lab("largest-contentful-paint")
    cls_field = pct("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    cls = cls_field / 100.0 if cls_field is not None else lab("cumulative-layout-shift")

    def category_score(name: str) -> int | None:
        raw = ((lighthouse.get("categories") or {}).get(name) or {}).get("score")
        return round(raw * 100) if isinstance(raw, (int, float)) else None

    return {
        "performance_score": round(perf_raw * 100) if isinstance(perf_raw, (int, float)) else None,
        "lcp_ms": lcp_ms,
        "inp_ms": pct("INTERACTION_TO_NEXT_PAINT"),
        "cls": cls,
        # Present only when the caller asked for those categories; None otherwise,
        # which is why `categories` is part of the cache key.
        "accessibility_score": category_score("accessibility"),
        "best_practices_score": category_score("best-practices"),
        "seo_score": category_score("seo"),
        "tbt_ms": lab("total-blocking-time"),
        "fcp_ms": lab("first-contentful-paint"),
        "speed_index_ms": lab("speed-index"),
        "lighthouse_version": lighthouse.get("lighthouseVersion") or "",
        "strategy": (lighthouse.get("configSettings") or {}).get("formFactor") or "",
        "fetched_url": lighthouse.get("finalUrl") or lighthouse.get("requestedUrl") or "",
    }
