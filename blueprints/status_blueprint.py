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

import hashlib
import json
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


# ─────────────────────────────────────────────────────────────────────────────
#  Remote-Config / Kill-Switches (Owner 2026-08-04, nach dem BLR-Layover-
#  Vorfall: „kann man nicht einbauen, dass so Code direkt geändert werden
#  kann?"). Kein nachgeladener Code (Apple 2.5.2) — sondern Flags, die die
#  App holt und mit kompilierten Safe-Defaults überblendet.
#
#  Quellen-Kaskade (spätere gewinnt):
#    1. config/app_flags.json  (Repo-Defaults — Flag-Flip = Commit + Deploy)
#    2. Env AEROX_FLAGS_JSON   (JSON-Objekt — Container-Restart genügt,
#                               Notfall-Hebel ganz ohne Code-Änderung)
#  Fehlertoleranz: kaputte Datei/Env wird STILL ignoriert (Defaults gewinnen,
#  die App hat ohnehin kompilierte Fallbacks). Öffentlich lesbar; es dürfen
#  hier deshalb NIE Secrets oder personenbezogene Werte liegen.
# ─────────────────────────────────────────────────────────────────────────────

_FLAGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "app_flags.json")
_FLAGS_CACHE: Dict[str, Any] = {}          # {"exp": float, "flags": dict, "rev": str}
_FLAGS_CACHE_TTL_SEC = 60


def _load_app_flags() -> Dict[str, Any]:
    """Effektive Flags: Datei-Defaults + Env-Overlay. Nie raisen."""
    flags: Dict[str, Any] = {}
    try:
        with open(_FLAGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            flags.update({k: v for k, v in raw.items() if not k.startswith("_")})
    except Exception:
        pass  # fehlende/kaputte Datei ⇒ leere Defaults, App-Fallbacks tragen
    try:
        env_raw = os.getenv("AEROX_FLAGS_JSON", "").strip()
        if env_raw:
            env_obj = json.loads(env_raw)
            if isinstance(env_obj, dict):
                flags.update(env_obj)
    except Exception:
        pass  # kaputte Env ⇒ ignorieren, Datei-Stand gilt
    return flags


def _app_flags_cached() -> Tuple[Dict[str, Any], str]:
    now = time.time()
    if _FLAGS_CACHE.get("exp", 0) > now:
        return _FLAGS_CACHE["flags"], _FLAGS_CACHE["rev"]
    flags = _load_app_flags()
    rev = hashlib.sha256(
        json.dumps(flags, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    _FLAGS_CACHE.update({"exp": now + _FLAGS_CACHE_TTL_SEC,
                         "flags": flags, "rev": rev})
    return flags, rev


@status_bp.route("/api/app-config", methods=["GET"])
def app_config():
    """Feature-Flags/Kill-Switches für die iOS-App. Public, klein, cachebar."""
    flags, rev = _app_flags_cached()
    resp = jsonify({"ok": True, "rev": rev, "flags": flags})
    # 5 min Edge-/Client-Cache: ein Flag-Flip erreicht die Flotte in Minuten,
    # ohne dass jeder App-Start einen ungecachten Origin-Hit erzeugt.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
