# ═══════════════════════════════════════════════════════════════
#  Aircraft-Info Blueprint — ⚠️ DEPRECATED (Sweep 2026-07-10)
#
#  Die OpenSky-Metadata-API ist 410 Gone (live verifiziert: /api/metadata/
#  aircraft/registration/<reg> UND …/icao/<hex> → 410) — der Upstream-Pfad
#  dieses Blueprints ist damit dauerhaft tot; es liefert nur noch SB-Cache-
#  und Static-Fleet-Treffer. Kanonischer Ersatz ist die eigene Referenz-DB
#  unter /api/ax/aircraft (aerox_data_blueprint, 520k-baked-SQLite:
#  reg/type/built — live ok); iOS-Konsument ist Tracking/AircraftInfoCard
#  (Alt-Welt, kanonisch ist AircraftDetailView).
#
#  Der Endpoint BLEIBT registriert (Alt-Builds rufen ihn weiter), wird aber
#  NICHT mehr gepflegt: keine neuen Quellen, keine Fleet-Updates. Toter
#  Static-Fleet-/OpenSky-Code bewusst NICHT ausgebaut (Alt-Build-Kompat,
#  nur markiert). Neue Features gehen ausschließlich auf /api/ax/aircraft.
#
#  Ursprüngliche Beschreibung:
#  Liefert Metadaten (Manufacturer, Model, Build-Year, Seats, Operator,
#  Country) zu einer Aircraft-Registration. Im Gegensatz zum Live-State-
#  Blueprint (`adsb_blueprint.py`) ändert sich diese Information selten —
#  wir cachen sie auf SB für 30 Tage und greifen nur bei Cache-Miss auf
#  Upstream zu.
#
#  Wiring in app.py:
#      from blueprints.aircraft_info_blueprint import aircraft_info_bp
#      app.register_blueprint(aircraft_info_bp)
#
#  Endpunkte:
#      GET /api/aircraft-info/<reg>   → {reg, hex24, manufacturer, model,
#                                        type_code, build_year, seats,
#                                        operator, country, last_seen_date}
#
#  Chain:
#      1) SB-Cache (TTL 30 Tage) → sofort raus.
#      2) OpenSky `/api/metadata/aircraft/icao/<hex24>` (3s timeout).
#         FREE, kein Rate-Limit für Metadata-Lookups.
#      3) Static Fleet-Lookup (~50 D-Ax* + HB-Jx* + OE-Lx* + OO-Sx* aus
#         in-process Tabelle als Last-Line-of-Defense).
#      4) Wenn nichts gefunden → 404, KEIN Cache (sonst würden wir
#         "unbekannt" persistieren, verhindert spätere Recovery).
#
#  Privacy:
#    · Keine User-Token-Authentifizierung — Aircraft-Metadaten sind
#      öffentliche Daten (planespotters, flightradar etc).
#    · Wir loggen die Reg, nicht den Caller — kein PII-Issue.
# ═══════════════════════════════════════════════════════════════

import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from flask import Blueprint, jsonify

aircraft_info_bp = Blueprint('aircraft_info', __name__)

# ── Reg→Hex Lookup (delegiert an adsb_blueprint) ────────────────
try:
    from blueprints.adsb_blueprint import resolve_reg_to_hex
except ImportError:
    # Standalone-Test/Dev — Fallback auf empty resolver damit der Blueprint
    # nicht beim Boot dies-en wenn die Import-Order kaputt geht.
    def resolve_reg_to_hex(_reg):
        return None

# ── SB-Anbindung (lazy-resolve wie aircraft_health_blueprint) ──
try:
    from app import sb as _sb, SB_AVAILABLE as _SB_AVAILABLE
except ImportError:
    _sb = None
    _SB_AVAILABLE = False


def _sb_client():
    """Lazy re-resolve, damit init-Order zwischen app.py und Blueprint egal ist."""
    global _sb, _SB_AVAILABLE
    if _sb is not None and _SB_AVAILABLE:
        return _sb, True
    try:
        from app import sb as live_sb, SB_AVAILABLE as live_av
        _sb = live_sb
        _SB_AVAILABLE = bool(live_av)
        return _sb, _SB_AVAILABLE
    except ImportError:
        return None, False


# ── Constants ──────────────────────────────────────────────────
USER_AGENT = "AeroTax-Backend/1.1 (Aircraft-Metadata; mailto:ops@aerotax.de)"
OPENSKY_METADATA_TIMEOUT = 3
SB_CACHE_TTL_DAYS = 30
REG_PATTERN = re.compile(r'^[A-Z0-9]{1,3}-[A-Z0-9]{1,5}$')


# ── Static Fleet-DB (Last-Line-of-Defense) ──────────────────────
#
# ⚠️ DEPRECATED/eingefroren (Sweep 2026-07-10): wird NICHT mehr gepflegt —
# seit OpenSky 410 Gone ist das faktisch die einzige Upstream-„Quelle" dieses
# Blueprints. Bewusst NICHT ausgebaut (Alt-Builds), aber keine neuen Regs
# hier eintragen; die Referenz-DB /api/ax/aircraft kennt sie ohnehin.
#
# Identisch zur iOS-Side `AircraftRegistryLookup.swift`-Tabelle — wenn beide
# auseinanderdriften, gewinnt was im Backend ist (wegen TTL-Cache landet
# Backend-Wert eh nach ~30 Tagen im Client). Stand 2026-05.
#
# Format: reg → (type_code, manufacturer, model, build_year, seats, operator, country)
# build_year und seats sind "typische" Werte für die Variante — bei
# Re-Configuration (z.B. LH-Premium-Eco-Retrofit) sind sie nicht exakt,
# aber als Fallback ausreichend.
_STATIC_FLEET = {
    # Lufthansa A320-Family
    "D-AIPA": ("A320", "Airbus", "A320-200", 1989, 168, "Lufthansa", "Germany"),
    "D-AIPB": ("A320", "Airbus", "A320-200", 1989, 168, "Lufthansa", "Germany"),
    "D-AIPC": ("A320", "Airbus", "A320-200", 1989, 168, "Lufthansa", "Germany"),
    "D-AIPD": ("A320", "Airbus", "A320-200", 1989, 168, "Lufthansa", "Germany"),
    "D-AIPE": ("A320", "Airbus", "A320-200", 1989, 168, "Lufthansa", "Germany"),
    "D-AIPF": ("A320", "Airbus", "A320-200", 1990, 168, "Lufthansa", "Germany"),
    "D-AIPH": ("A320", "Airbus", "A320-200", 1990, 168, "Lufthansa", "Germany"),
    "D-AIPK": ("A320", "Airbus", "A320-200", 1990, 168, "Lufthansa", "Germany"),
    "D-AIPL": ("A320", "Airbus", "A320-200", 1990, 168, "Lufthansa", "Germany"),
    "D-AIQA": ("A320", "Airbus", "A320-200", 1991, 168, "Lufthansa", "Germany"),
    "D-AIQB": ("A320", "Airbus", "A320-200", 1991, 168, "Lufthansa", "Germany"),
    "D-AIQC": ("A320", "Airbus", "A320-200", 1991, 168, "Lufthansa", "Germany"),
    "D-AIUA": ("A320", "Airbus", "A320-200", 2014, 180, "Lufthansa", "Germany"),
    "D-AIUB": ("A320", "Airbus", "A320-200", 2014, 180, "Lufthansa", "Germany"),
    # Lufthansa A330/A340
    "D-AIKA": ("A330", "Airbus", "A330-300", 2004, 255, "Lufthansa", "Germany"),
    "D-AIKB": ("A330", "Airbus", "A330-300", 2004, 255, "Lufthansa", "Germany"),
    "D-AIHA": ("A340", "Airbus", "A340-600", 2003, 297, "Lufthansa", "Germany"),
    "D-AIHB": ("A340", "Airbus", "A340-600", 2003, 297, "Lufthansa", "Germany"),
    # Lufthansa A350-900
    "D-AIXA": ("A359", "Airbus", "A350-900", 2017, 293, "Lufthansa", "Germany"),
    "D-AIXB": ("A359", "Airbus", "A350-900", 2017, 293, "Lufthansa", "Germany"),
    "D-AIXC": ("A359", "Airbus", "A350-900", 2017, 293, "Lufthansa", "Germany"),
    "D-AIXD": ("A359", "Airbus", "A350-900", 2018, 293, "Lufthansa", "Germany"),
    "D-AIXE": ("A359", "Airbus", "A350-900", 2018, 293, "Lufthansa", "Germany"),
    # Lufthansa A380
    "D-AIMA": ("A388", "Airbus", "A380-800", 2010, 509, "Lufthansa", "Germany"),
    "D-AIMB": ("A388", "Airbus", "A380-800", 2010, 509, "Lufthansa", "Germany"),
    "D-AIMC": ("A388", "Airbus", "A380-800", 2010, 509, "Lufthansa", "Germany"),
    # Lufthansa 747-8
    "D-ABYA": ("B748", "Boeing", "747-8", 2012, 364, "Lufthansa", "Germany"),
    "D-ABYB": ("B748", "Boeing", "747-8", 2012, 364, "Lufthansa", "Germany"),
    "D-ABYC": ("B748", "Boeing", "747-8", 2013, 364, "Lufthansa", "Germany"),
    # Eurowings A320
    "D-AEWA": ("A320", "Airbus", "A320-200", 2015, 180, "Eurowings", "Germany"),
    "D-AEWB": ("A320", "Airbus", "A320-200", 2015, 180, "Eurowings", "Germany"),
    "D-AIZA": ("A320", "Airbus", "A320-200", 2009, 180, "Eurowings", "Germany"),
    "D-AIZB": ("A320", "Airbus", "A320-200", 2009, 180, "Eurowings", "Germany"),
    # SWISS A220 + A330
    "HB-JCA": ("BCS3", "Airbus", "A220-300", 2017, 145, "SWISS", "Switzerland"),
    "HB-JCB": ("BCS3", "Airbus", "A220-300", 2017, 145, "SWISS", "Switzerland"),
    "HB-JHA": ("A333", "Airbus", "A330-300", 2009, 236, "SWISS", "Switzerland"),
    "HB-JHB": ("A333", "Airbus", "A330-300", 2010, 236, "SWISS", "Switzerland"),
    # Austrian A320
    "OE-LBA": ("A320", "Airbus", "A320-200", 2003, 168, "Austrian Airlines", "Austria"),
    "OE-LBB": ("A320", "Airbus", "A320-200", 2003, 168, "Austrian Airlines", "Austria"),
    "OE-LBC": ("A320", "Airbus", "A320-200", 2003, 168, "Austrian Airlines", "Austria"),
    # Brussels A320
    "OO-SNA": ("A320", "Airbus", "A320-200", 2005, 174, "Brussels Airlines", "Belgium"),
    "OO-SNB": ("A320", "Airbus", "A320-200", 2005, 174, "Brussels Airlines", "Belgium"),
}


# ─── /api/aircraft-info/<reg> ────────────────────────────────────

@aircraft_info_bp.route('/api/aircraft-info/<reg>', methods=['GET'])
def get_aircraft_info(reg):
    """
    ⚠️ DEPRECATED (Sweep 2026-07-10, s. Header): bleibt nur für Alt-Builds
    online — OpenSky-Upstream ist 410 Gone, Neues geht auf /api/ax/aircraft.

    Liefert Metadaten zu einer Aircraft-Registration.

    Returns:
        200 mit {reg, hex24, manufacturer, model, type_code, build_year,
                 seats, operator, country, last_seen_date, source, fetched_at}
        400 wenn Reg-Format ungültig
        404 wenn alle Quellen leer

    Source-Werte:
        'cache'    — aus SB cache (innerhalb TTL)
        'opensky'  — frisch von OpenSky Metadata-API
        'static'   — Last-Line-of-Defense aus in-process Tabelle
    """
    reg_norm = (reg or '').strip().upper()
    if not reg_norm or not REG_PATTERN.match(reg_norm):
        return jsonify({
            "error": "invalid_registration",
            "detail": f"expected pattern XX-XXXXX, got {reg!r}",
        }), 400

    # ─── Step 1: SB-Cache ───
    cached = _sb_cache_get(reg_norm)
    if cached is not None:
        cached["source"] = "cache"
        return jsonify(cached), 200

    # ─── Step 2: OpenSky Metadata API ───
    hex24 = resolve_reg_to_hex(reg_norm)
    # Wir können auch ohne hex24 weiter — OpenSky hat /api/metadata/
    # aircraft/registration/<reg>, das nutzen wir wenn hex unbekannt.
    opensky_info = _fetch_opensky_metadata(reg_norm, hex24)
    if opensky_info is not None:
        opensky_info["reg"] = reg_norm
        opensky_info["hex24"] = opensky_info.get("hex24") or hex24
        opensky_info["source"] = "opensky"
        opensky_info["fetched_at"] = _now_iso()
        _sb_cache_put(reg_norm, opensky_info)
        return jsonify(opensky_info), 200

    # ─── Step 3: Static Fleet-Lookup ───
    static_info = _static_lookup(reg_norm)
    if static_info is not None:
        static_info["reg"] = reg_norm
        static_info["hex24"] = hex24
        static_info["source"] = "static"
        static_info["fetched_at"] = _now_iso()
        # Static-Hits cachen wir auch in SB damit wiederholte Lookups dem
        # Static-Pfad nicht jedes Mal durchlaufen — schadet nicht, weil
        # bei Static-Drift wir den Cache eh manuell flushen müssten.
        _sb_cache_put(reg_norm, static_info)
        return jsonify(static_info), 200

    # ─── Nichts gefunden ───
    return jsonify({
        "error": "not_found",
        "reg": reg_norm,
        "hex24": hex24,
        "detail": "OpenSky metadata empty and no static fleet entry",
    }), 404


# ─── /api/aircraft-info/health ──────────────────────────────────

@aircraft_info_bp.route('/api/aircraft-info/health', methods=['GET'])
def aircraft_info_health():
    """Sanity-check für Boot-Verification + Static-Fleet-Größe."""
    sb_client, sb_ok = _sb_client()
    return jsonify({
        "ok": True,
        "static_fleet_size": len(_STATIC_FLEET),
        "sb_cache_available": sb_ok,
        "cache_ttl_days": SB_CACHE_TTL_DAYS,
    }), 200


# ── SB Cache Helpers ────────────────────────────────────────────

def _sb_cache_get(reg):
    """Liest aus aircraft_info_cache wenn innerhalb TTL. Bei SB-Outage
    silently None — Caller fällt auf Upstream zurück."""
    sb_client, sb_ok = _sb_client()
    if not sb_ok or sb_client is None:
        return None
    try:
        res = (sb_client.table('aircraft_info_cache')
               .select('*')
               .eq('reg', reg)
               .limit(1)
               .execute())
        rows = getattr(res, 'data', None) or []
        if not rows:
            return None
        row = rows[0]
        # TTL-Check: fetched_at < now - 30d → stale, ignorieren.
        fetched_at_str = row.get('fetched_at') or ''
        try:
            fetched_dt = datetime.fromisoformat(fetched_at_str.replace('Z', '+00:00'))
            age_seconds = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
            if age_seconds > SB_CACHE_TTL_DAYS * 86400:
                return None
        except ValueError:
            pass  # ungültiges Datum → cache trotzdem nutzen (besser als nichts)

        out = {
            "reg": row.get('reg'),
            "hex24": row.get('hex24'),
            "manufacturer": row.get('manufacturer'),
            "model": row.get('model'),
            "type_code": row.get('type_code'),
            "build_year": row.get('build_year'),
            "seats": row.get('seats'),
            "operator": row.get('operator'),
            "country": row.get('country'),
            "fetched_at": fetched_at_str,
        }
        # `payload` enthält zusätzliche optionale Felder (z.B. last_seen_date)
        # die wir aus OpenSky bekommen aber nicht alle als Top-Level-Spalten
        # vorhalten.
        payload = row.get('payload') or {}
        if isinstance(payload, dict):
            for k in ('last_seen_date', 'icao_aircraft_type', 'serial_number',
                      'engines', 'first_flight_date'):
                if k in payload and payload[k]:
                    out[k] = payload[k]
        return out
    except Exception:
        return None


def _sb_cache_put(reg, info):
    """Persistiert in aircraft_info_cache (upsert by reg). Silent-fail bei
    SB-Outage — wir wollen den Lookup-Response nicht blockieren weil das
    Caching down ist."""
    sb_client, sb_ok = _sb_client()
    if not sb_ok or sb_client is None:
        return
    try:
        payload_extra = {
            k: info[k] for k in (
                'last_seen_date', 'icao_aircraft_type', 'serial_number',
                'engines', 'first_flight_date',
            ) if k in info and info[k] is not None
        }
        row = {
            'reg': info.get('reg'),
            'hex24': info.get('hex24'),
            'manufacturer': info.get('manufacturer'),
            'model': info.get('model'),
            'type_code': info.get('type_code'),
            'build_year': info.get('build_year'),
            'seats': info.get('seats'),
            'operator': info.get('operator'),
            'country': info.get('country'),
            'payload': payload_extra,
            'fetched_at': _now_iso(),
        }
        sb_client.table('aircraft_info_cache').upsert(row, on_conflict='reg').execute()
    except Exception:
        pass  # silent — Cache ist Best-Effort


# ── Upstream-Fetchers ───────────────────────────────────────────

def _fetch_opensky_metadata(reg, hex24):
    """
    ⚠️ TOTER PFAD (2026-07-10): beide OpenSky-Metadata-URLs antworten 410 Gone —
    liefert praktisch immer None. Bewusst nicht ausgebaut (deprecated Blueprint).

    Holt Metadaten von OpenSky. Bevorzugt `/api/metadata/aircraft/icao/<hex>`
    wenn hex24 bekannt — sonst Fallback auf `/api/metadata/aircraft/registration/<reg>`.

    Returns dict mit {manufacturer, model, type_code, build_year, seats,
                       operator, country, last_seen_date, hex24} oder None.
    """
    urls = []
    if hex24:
        urls.append(("hex",
                     f"https://opensky-network.org/api/metadata/aircraft/icao/{urllib.parse.quote(hex24)}"))
    urls.append(("reg",
                 f"https://opensky-network.org/api/metadata/aircraft/registration/"
                 f"{urllib.parse.quote(reg)}"))

    for kind, url in urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=OPENSKY_METADATA_TIMEOUT) as resp:
                data = resp.read()
        except urllib.error.HTTPError:
            continue  # 404 oder 5xx — nächste URL probieren
        except urllib.error.URLError:
            continue
        except Exception:
            continue

        try:
            obj = json.loads(data)
        except (ValueError, json.JSONDecodeError):
            continue

        if not isinstance(obj, dict):
            continue

        # Heuristik: OpenSky liefert "manufacturerName"/"manufacturerIcao"/
        # "model"/"typecode"/"built"/"operatorIconame"/"country"/"icao24"/
        # "lastSeen". Felder können fehlen wenn die Aircraft nicht
        # vollständig registriert ist.
        info = _normalize_opensky_metadata(obj)
        # Wenn alle Pflichtfelder leer sind, ignorieren wir die Antwort —
        # macht keinen Sinn als positive Hit.
        if not info.get('manufacturer') and not info.get('model') and not info.get('type_code'):
            continue
        return info

    return None


def _normalize_opensky_metadata(obj):
    """Maps OpenSky-Metadata-Feldnamen auf unsere konsistente Output-Form."""
    manufacturer = (obj.get('manufacturerName') or
                    obj.get('manufacturerIcao') or
                    obj.get('manufacturer') or '')
    model = (obj.get('model') or '')
    type_code = (obj.get('typecode') or obj.get('icaoAircraftClass') or '')
    operator = (obj.get('operator') or obj.get('operatorCallsign') or
                obj.get('owner') or '')
    country = obj.get('country') or obj.get('registeredCountry') or ''
    hex24 = (obj.get('icao24') or '').lower() or None

    # build_year: OpenSky hat "built" als YYYY-MM-DD oder YYYY String.
    build_year = None
    built_raw = obj.get('built')
    if built_raw:
        m = re.match(r'^(\d{4})', str(built_raw))
        if m:
            try:
                build_year = int(m.group(1))
            except ValueError:
                pass

    # seats: nicht in OpenSky-Standard-Response. Wenn vorhanden in
    # `categoryDescription` oder Provider-Extension, parsen wir später.
    seats = None
    if isinstance(obj.get('seats'), int):
        seats = obj['seats']

    last_seen_date = obj.get('lastSeen') or obj.get('last_seen') or None

    return {
        'manufacturer': manufacturer or None,
        'model': model or None,
        'type_code': type_code or None,
        'build_year': build_year,
        'seats': seats,
        'operator': operator or None,
        'country': country or None,
        'hex24': hex24,
        'last_seen_date': last_seen_date,
    }


def _static_lookup(reg):
    """Last-Line-of-Defense: in-process Fleet-Tabelle."""
    entry = _STATIC_FLEET.get(reg)
    if entry is None:
        return None
    type_code, manufacturer, model, build_year, seats, operator, country = entry
    return {
        'manufacturer': manufacturer,
        'model': model,
        'type_code': type_code,
        'build_year': build_year,
        'seats': seats,
        'operator': operator,
        'country': country,
    }


def _now_iso():
    return datetime.now(timezone.utc).isoformat()
