"""Public health-check + dependency status blueprint.

Integration snippet (add to app.py after blueprint registrations):

    from blueprints.status_blueprint import status_bp
    app.register_blueprint(status_bp)

Routes:
    GET /status              -> minimal liveness, public, used by Cloud Run probes
    GET /status/dependencies -> per-dependency check (OpenSky, AviationWeather,
                                 Supabase, Anthropic), 2s timeout each, 30s cache

The dependency check intentionally hits external services with HEAD or a tiny
GET so the cost is negligible (one round-trip per provider per 30s).
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify

status_bp = Blueprint("status", __name__)


_BOOT_TS = time.time()
_DEP_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}  # key -> (expires_at, result)
_CACHE_TTL_SEC = 30


def _cache_get(key: str) -> Dict[str, Any] | None:
    entry = _DEP_CACHE.get(key)
    if not entry:
        return None
    exp, val = entry
    if time.time() > exp:
        return None
    return val


def _cache_put(key: str, value: Dict[str, Any]) -> None:
    _DEP_CACHE[key] = (time.time() + _CACHE_TTL_SEC, value)


def _check_supabase() -> Dict[str, Any]:
    """Cheap ping: select 1 from auth_users with LIMIT 1, service-role key."""
    cached = _cache_get("supabase")
    if cached:
        return cached
    started = time.time()
    result: Dict[str, Any] = {"status": "down", "latency_ms": None, "detail": None}
    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if not url or not key:
            result["status"] = "degraded"
            result["detail"] = "env-vars missing"
            _cache_put("supabase", result)
            return result
        import requests
        r = requests.get(
            f"{url.rstrip('/')}/rest/v1/auth_users",
            params={"select": "token", "limit": 1},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=2.0,
        )
        ms = int((time.time() - started) * 1000)
        result["latency_ms"] = ms
        if r.status_code < 300:
            result["status"] = "ok" if ms < 800 else "degraded"
        elif r.status_code in (401, 403):
            result["status"] = "degraded"
            result["detail"] = f"auth http {r.status_code}"
        else:
            result["status"] = "down"
            result["detail"] = f"http {r.status_code}"
    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
    _cache_put("supabase", result)
    return result


def _check_opensky() -> Dict[str, Any]:
    cached = _cache_get("opensky")
    if cached:
        return cached
    started = time.time()
    result: Dict[str, Any] = {"status": "down", "latency_ms": None, "detail": None}
    try:
        import requests
        # very cheap: states endpoint with a bounding-box query that returns near-nothing
        r = requests.get(
            "https://opensky-network.org/api/states/all",
            params={"lamin": 50.0, "lamax": 50.1, "lomin": 8.5, "lomax": 8.6},
            timeout=2.0,
        )
        ms = int((time.time() - started) * 1000)
        result["latency_ms"] = ms
        if r.status_code < 300:
            result["status"] = "ok" if ms < 1500 else "degraded"
        else:
            result["status"] = "degraded"
            result["detail"] = f"http {r.status_code}"
    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
    _cache_put("opensky", result)
    return result


def _check_aviationweather() -> Dict[str, Any]:
    cached = _cache_get("aviationweather")
    if cached:
        return cached
    started = time.time()
    result: Dict[str, Any] = {"status": "down", "latency_ms": None, "detail": None}
    try:
        import requests
        # METAR at KJFK is the canonical cheap probe
        r = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": "KJFK", "format": "json", "taf": "false"},
            timeout=2.0,
        )
        ms = int((time.time() - started) * 1000)
        result["latency_ms"] = ms
        if r.status_code < 300:
            result["status"] = "ok" if ms < 1500 else "degraded"
        else:
            result["status"] = "degraded"
            result["detail"] = f"http {r.status_code}"
    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
    _cache_put("aviationweather", result)
    return result


def _check_anthropic() -> Dict[str, Any]:
    """Lightweight check: HEAD on the Anthropic API root. We do not spend tokens."""
    cached = _cache_get("anthropic")
    if cached:
        return cached
    started = time.time()
    result: Dict[str, Any] = {"status": "down", "latency_ms": None, "detail": None}
    try:
        import requests
        r = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
            },
            timeout=2.0,
        )
        ms = int((time.time() - started) * 1000)
        result["latency_ms"] = ms
        # 200 OK; 401 means our key is wrong but the service is up
        if r.status_code == 200:
            result["status"] = "ok" if ms < 1500 else "degraded"
        elif r.status_code in (401, 403):
            result["status"] = "degraded"
            result["detail"] = f"auth http {r.status_code}"
        else:
            result["status"] = "degraded"
            result["detail"] = f"http {r.status_code}"
    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
    _cache_put("anthropic", result)
    return result


@status_bp.route("/status", methods=["GET"])
def status_root():
    """Liveness probe. Public. Used by Cloud Run + uptime monitors."""
    sb_check = _check_supabase()
    return jsonify({
        "service": "aeris-backend",
        "revision": os.getenv("CLOUD_RUN_REVISION", "dev"),
        "region": os.getenv("CLOUD_RUN_REGION", "unknown"),
        "sb_ok": sb_check.get("status") == "ok",
        "uptime_sec": int(time.time() - _BOOT_TS),
        "ts": int(time.time()),
    })


@status_bp.route("/status/dependencies", methods=["GET"])
def status_dependencies():
    """Per-dependency health, cached 30s. Used by the public status page."""
    deps = {
        "supabase": _check_supabase(),
        "opensky": _check_opensky(),
        "aviationweather": _check_aviationweather(),
        "anthropic": _check_anthropic(),
    }
    overall = "ok"
    for d in deps.values():
        if d.get("status") == "down":
            overall = "down"
            break
        if d.get("status") == "degraded" and overall == "ok":
            overall = "degraded"
    return jsonify({
        "overall": overall,
        "revision": os.getenv("CLOUD_RUN_REVISION", "dev"),
        "uptime_sec": int(time.time() - _BOOT_TS),
        "dependencies": deps,
        "cached_for_sec": _CACHE_TTL_SEC,
        "ts": int(time.time()),
    })
