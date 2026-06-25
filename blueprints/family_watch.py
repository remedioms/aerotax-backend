# ═══════════════════════════════════════════════════════════════
#  Family-Watcher / Family-Share Blueprint  (Worker F, 2026-06-02)
#
#  Backend für den iOS-FamilyMode (FamilyWatchClient.swift).
#  Crew gewährt einer Family-Person Lese-Rechte auf ausgewählte Status-Felder
#  (Layover-Stadt, current_city, landed-Status, nächster Flug, Photos).
#
#  Architektur:
#      SB-Primary  →  family_shares-Tabelle (crew_token + family_token + fields jsonb)
#      Disk-Fallback → _USER_HISTORY_DIR/family_shares.json
#      Lazy-Migrate bei SB-leerem Read.
#
#  Privacy-Garantien:
#      - Family-User sieht NIE Geld-Daten, FTL-Stunden, Roster-Original
#      - Server filtert Response strikt nach `fields_granted`-Liste
#      - Felder die nicht explizit gegranted sind = nicht in der Response
#
#  Endpunkte (matched iOS FamilyWatchClient.swift):
#      GET    /api/family-watch/<token>/feed
#               → für family-token, liefert alle Crews die dieser Family
#                 was gegranted haben, gefiltert nach erlaubten Feldern
#      GET    /api/family-share/<token>/list
#               → für crew-token, liefert alle Family-Grants
#      POST   /api/family-share/<token>/grant
#               body: {family_token, relation, fields: [...]}
#      DELETE /api/family-share/<token>/revoke/<family_token>
#
#  Wiring in app.py:
#      from blueprints.family_watch import family_watch_bp
#      app.register_blueprint(family_watch_bp)
# ═══════════════════════════════════════════════════════════════

import os
import re
import json
import time
import hashlib
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify, current_app

family_watch_bp = Blueprint('family_watch', __name__)

# Late-binding helper: greift bei jedem call frisch auf app-module-Attribute zu.
# Vorteil: am module-import-Zeitpunkt ist app.py noch nicht fertig initialisiert,
# Top-Level `from app import X` würde nur die Fallback-Werte einfangen. Wir
# resolven also bei Bedarf zur Request-Zeit, wenn app.py voll geladen ist.
def _app_attr(name, default=None):
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _atomic_write_json(path, data, max_items=None, **json_kwargs):
    fn = _app_attr('_atomic_write_json')
    if fn is not None and fn is not _atomic_write_json:  # avoid infinite recursion
        return fn(path, data, max_items=max_items, **json_kwargs)
    # Fallback
    json_kwargs.setdefault('ensure_ascii', False)
    target_dir = os.path.dirname(path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, **json_kwargs)


def _user_profile_path(token):
    fn = _app_attr('_user_profile_path')
    if fn is not None and fn is not _user_profile_path:
        return fn(token)
    return None


def _load_crew_profile(token):
    """Lädt das Crew-Profil-Dict ({name, homebase, ...}) GENAU so wie der
    Endpoint GET /api/user/profile/<token> es liest:
      Supabase-primary (user_profiles) → Disk-Fallback (_user_profile_path).
    Vorher lasen die Family-Helper NUR die Disk-Datei — auf Render/Cloud-Run
    liegt das Profil aber in Supabase, die Disk-Datei ist ephemeral/leer, daher
    kamen Token-Slice als Name + None als Homebase zurück.
    Returns dict (kann leer sein), nie None."""
    if not token:
        return {}
    # 1) Bevorzugt _profile_load aus app.py (SB-primary, Disk-Fallback) — exakt
    #    der Pfad den GET /api/user/profile nutzt.
    fn = _app_attr('_profile_load')
    if callable(fn):
        try:
            doc = fn(token) or {}
            prof = doc.get('profile')
            if isinstance(prof, dict):
                return prof
        except Exception as e:
            _log().info(f'[family-pair] profile_load_skip {type(e).__name__}')
    # 2) Disk-only Fallback (falls _profile_load nicht verfügbar).
    try:
        pp = _user_profile_path(token)
        if pp and os.path.exists(pp):
            with open(pp) as f:
                doc = json.load(f) or {}
            prof = doc.get('profile')
            if isinstance(prof, dict):
                return prof
    except Exception:
        pass
    return {}


def _get_sb():
    return _app_attr('SB_AVAILABLE', False), _app_attr('sb', None)


def _get_history_dir():
    return _app_attr('_USER_HISTORY_DIR', '_user_history_state')


# Whitelist gegen FamilyShareField-Enum (iOS-Side).
ALLOWED_FIELDS = {
    'layover_place', 'current_city', 'landed_status', 'next_flight',
    'photos', 'voice_notes', 'aircraft_reg',
}

ALLOWED_RELATIONS = {'partner', 'mama', 'papa', 'freund', 'kind', 'family'}


def _log():
    try:
        return current_app.logger
    except RuntimeError:
        return logging.getLogger('family_watch')


def _safe_token(token):
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _shares_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_shares.json')


def _shares_load_from_disk():
    p = _shares_disk_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f) or []
    except Exception:
        return []


# ── Family-Watch-ANFRAGEN (Suche statt Code) ─────────────────────────────────
# Familie sucht die Crew-Person (searchUsers → token), schickt eine Anfrage; die
# Crew bestätigt → daraus entsteht der normale family_shares-Grant. Disk-Store
# (klein, kurzlebig); SB optional. Kein Code mehr nötig.
def _requests_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_requests.json')


def _requests_load_from_disk():
    p = _requests_disk_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f) or []
    except Exception:
        return []


def _requests_load_from_sb():
    """SB-Read der offenen Family-Anfragen. None wenn SB nicht verfügbar/Tabelle
    fehlt → Caller fällt auf Disk zurück."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return None
    try:
        out = []
        r = sb.table('family_requests').select('*').execute()
        for row in (r.data or []):
            out.append({
                'crew_token': row.get('crew_token'),
                'family_token': row.get('family_token'),
                'relation': row.get('relation'),
                'requester_name': row.get('requester_name'),
                'requester_avatar': row.get('requester_avatar'),
                'created_at': row.get('created_at'),
            })
        return out
    except Exception as e:
        _log().warning(f'[family-req] sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        return None


def _requests_save_to_sb(reqs):
    """UPSERT-ONLY: upsertet die vorhandenen (crew,family)-Paare in SB.
    KEIN Reconcile-Delete mehr (#7/#19): auf Cloud Run multi-instance hätte das
    Löschen von "fehlenden" Keys aus einem stalen In-Memory-Snapshot Anfragen
    anderer User/Container weggewischt (Instance A löscht was Instance B gerade
    angelegt hat). Echte Deletes (Zurückziehen/Approve/Reject) machen die Caller
    jetzt per gezieltem .delete().eq(...)."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        want = {}
        for r in (reqs or []):
            ct, ft = r.get('crew_token'), r.get('family_token')
            if ct and ft:
                want[(ct, ft)] = r
        rows = [{
            'crew_token': r.get('crew_token'),
            'family_token': r.get('family_token'),
            'relation': r.get('relation') or 'family',
            'requester_name': r.get('requester_name'),
            'requester_avatar': r.get('requester_avatar'),
            'created_at': r.get('created_at') or _dt.datetime.now().isoformat(),
        } for r in want.values()]
        for i in range(0, len(rows), 500):
            sb.table('family_requests').upsert(
                rows[i:i+500], on_conflict='crew_token,family_token').execute()
        return True
    except Exception as e:
        _log().warning(f'[family-req] sb_save_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _requests_load():
    """SB-primary + Disk-merge, dedupliziert nach (crew_token, family_token).
    KRITISCH auf Cloud Run: die Disk ist ephemer/multi-instance → eine nur auf Disk
    gespeicherte Anfrage sah die Crew-Person (anderer Container) NIE („Anfrage von
    der Familie poppt nicht auf"). Jetzt SB-primary."""
    sb_data = _requests_load_from_sb()
    disk_data = _requests_load_from_disk()
    if sb_data is None:
        return disk_data or []
    merged = {}
    for r in (sb_data or []):
        key = (r.get('crew_token'), r.get('family_token'))
        if key[0] and key[1]:
            merged[key] = r
    for r in (disk_data or []):
        key = (r.get('crew_token'), r.get('family_token'))
        if key[0] and key[1] and key not in merged:
            merged[key] = r
    return list(merged.values())


def _requests_save(reqs):
    sb_ok = _requests_save_to_sb(reqs)
    disk_ok = False
    try:
        _atomic_write_json(_requests_disk_path(), reqs, max_items=2000)
        disk_ok = True
    except Exception:
        pass
    return bool(sb_ok or disk_ok)


def _shares_load_from_sb():
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return None
    try:
        out = []
        offset = 0
        page = 1000
        while True:
            r = (sb.table('family_shares').select('*')
                 .eq('deleted', False)
                 .range(offset, offset + page - 1).execute())
            rows = r.data or []
            for row in rows:
                out.append({
                    'crew_token': row.get('crew_token'),
                    'family_token': row.get('family_token'),
                    'relation': row.get('relation'),
                    'fields': row.get('fields') or [],
                    'created_at': row.get('created_at'),
                })
            if len(rows) < page:
                break
            offset += page
        return out
    except Exception as e:
        _log().warning(f'[family-share] sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        return None


def _shares_save_to_sb(shares):
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        rows = []
        for s in (shares or []):
            if not isinstance(s, dict):
                continue
            ct = s.get('crew_token')
            ft = s.get('family_token')
            if not ct or not ft:
                continue
            rows.append({
                'crew_token': ct,
                'family_token': ft,
                'relation': s.get('relation') or '',
                'fields': s.get('fields') or [],
                'created_at': s.get('created_at') or _dt.datetime.now().isoformat(),
                'deleted': False,
            })
        if not rows:
            return True
        for i in range(0, len(rows), 500):
            sb.table('family_shares').upsert(
                rows[i:i+500], on_conflict='crew_token,family_token').execute()
        return True
    except Exception as e:
        _log().warning(f'[family-share] sb_save_fail {type(e).__name__}: {str(e)[:200]}')
        return False


def _shares_load():
    """SB+Disk merge, dedupliziert nach (crew_token, family_token)."""
    sb_data = _shares_load_from_sb()
    disk_data = _shares_load_from_disk()
    if sb_data is None:
        return disk_data or []
    merged = {}
    for s in (sb_data or []):
        if not isinstance(s, dict):
            continue
        key = (s.get('crew_token'), s.get('family_token'))
        if key[0] and key[1]:
            merged[key] = s
    for s in (disk_data or []):
        if not isinstance(s, dict):
            continue
        key = (s.get('crew_token'), s.get('family_token'))
        if not (key[0] and key[1]):
            continue
        if key not in merged:
            merged[key] = s
    return list(merged.values())


def _shares_save(shares):
    sb_ok = _shares_save_to_sb(shares)
    disk_ok = False
    try:
        _atomic_write_json(_shares_disk_path(), shares)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[family-share] disk_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _iso_or_none(v):
    """Roher DB-Zeitwert → getrimmter ISO-String (oder None). NICHT abschneiden —
    der Offset (+00:00)/Mikrosekunden müssen erhalten bleiben, sonst parst die
    Zeitzone falsch."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_iso(s):
    """Toleranter ISO→aware-UTC-Parser. Naive (ohne Zone) wird als UTC gewertet."""
    if not s:
        return None
    try:
        t = str(s).strip().replace('Z', '+00:00')
        d = _dt.datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone(_dt.timezone.utc)
    except Exception:
        return None


def _load_crew_status_for_family(crew_token, allowed_fields):
    """Liest aus dem Crew-Profile + briefing-state nur die erlaubten Felder.
    Returns dict mit den status-feldern fuer die WatchedCrew.CrewStatus
    iOS struct (alle felder Optional)."""
    if not crew_token:
        return {}
    status = {
        'layover_place': None,
        'current_city': None,
        'landed': None,
        'next_flight_no': None,
        'next_flight_dep_iata': None,
        'next_flight_arr_iata': None,
        'next_flight_etd_iso': None,
        'photo_count_today': None,
        'last_seen_iso': None,
        # „Fliegt gerade"-Block (User 2026-06-25): heute aktiver Flugtag → die
        # Family sieht ein Radar-Widget mit interpoliertem Flieger statt „In <Abflug>".
        # iOS rechnet Position/Animation selbst aus dep/arr-IATA + den Zeiten.
        'flying_now': None,
        'today_dep_iata': None,
        'today_arr_iata': None,
        'today_dep_iso': None,
        'today_arr_iso': None,
        # Zuhause/Feierabend: heute an der Homebase (reiner Heimtag) ODER nach
        # Landung an der Homebase (Dienst vorbei) — die Card zeigt „Feierabend"
        # statt eines falschen Layovers (User 2026-06-25).
        'home_now': None,
    }
    try:
        # SB-primary statt Disk: auf Cloud Run ist die Profil-Disk-Datei ephemer/
        # leer → die Family-Watcher sahen weder current_city noch last_seen
        # („selbst wenn verbunden, sieht sie nichts vom Plan"). _load_crew_profile
        # + _profile_load lesen Supabase-first.
        prof = _load_crew_profile(crew_token) or {}
        if 'current_city' in allowed_fields:
            status['current_city'] = prof.get('current_city')
        # #36: landed/photo_count aus dem bereits geladenen Crew-Profil wiren —
        # vorher blieben beide selbst bei Freigabe IMMER None. Privacy-gegated:
        # nur setzen wenn das jeweilige Feld in allowed_fields ist.
        if 'landed_status' in allowed_fields:
            status['landed'] = prof.get('landed')
        if 'photos' in allowed_fields:
            # Kein dedizierter Foto-Zähler-Quell-Tisch vorhanden; wir wiren nur
            # einen ggf. existierenden Profilschlüssel. Fehlt er → bleibt None
            # (keine erfundenen Daten).
            status['photo_count_today'] = prof.get('photo_count_today')
        # last_seen_iso ist selbst eine Aktivitäts-Info ("zuletzt online/aktiv")
        # und darf NUR geleakt werden wenn der Crew ein relevantes Live-Feld
        # freigegeben hat. Vorher wurde es bedingungslos gesetzt (ausserhalb des
        # allowed_fields-Gates) → Family sah Aktivität auch ohne jede Freigabe.
        _ls_allowed = bool(allowed_fields & {'current_city', 'last_seen', 'landed_status', 'next_flight'})
        if _ls_allowed:
            _pl = _app_attr('_profile_load')
            if callable(_pl):
                full = _pl(crew_token) or {}
                status['last_seen_iso'] = full.get('_updated_at')
    except Exception as e:
        _log().info(f'[family-watch] profile_read_skip {type(e).__name__}')
    # next_flight: nur wenn 'next_flight' in allowed_fields. Best-effort read aus
    # briefings/roster state via SB. Wenn nicht ladbar → bleibt None.
    sb_avail, sb = _get_sb()
    # FIX (Bug-Hunt #3): die Tabelle 'briefings' EXISTIERT NICHT — die echten
    # Briefing-Daten liegen in user_ical_briefings (ical_summary „FRA-JFK 10:30-
    # 13:20, …", ical_location „JFK, FRA-JFK-FRA", ical_start). Vorher las dieser
    # Code eine Phantom-Tabelle → next_flight/layover blieben IMMER leer, auch
    # wenn die Crew die Felder freigegeben hatte.
    if 'next_flight' in allowed_fields and sb_avail and sb is not None:
        try:
            today = _dt.datetime.now().date().isoformat()
            r = (sb.table('user_ical_briefings')
                 .select('datum,ical_summary,ical_location,ical_start')
                 .eq('token', crew_token)
                 .gte('datum', today)
                 .order('datum', desc=False)
                 .limit(1).execute())
            rows = r.data or []
            if rows:
                br = rows[0]
                summ = br.get('ical_summary') or ''
                mleg = re.search(r'\b([A-Z]{3})-([A-Z]{3})\b', summ)
                if mleg:
                    status['next_flight_dep_iata'] = mleg.group(1)
                    status['next_flight_arr_iata'] = mleg.group(2)
                st = br.get('ical_start')
                if st:
                    status['next_flight_etd_iso'] = str(st)[:25]
        except Exception as e:
            _log().info(f'[family-watch] briefing_read_skip {type(e).__name__}')
    # Roster-derived Layover-Stadt + Reconcile von current_city.
    #
    # THEME-B FIX (Family-Watch zeigt falsche Stadt): `current_city` (oben, Z.389)
    # ist NUR ein vom iOS-LocationStore gepushter, reverse-geocodeter GPS-String
    # (POST /api/user/location, ~1×/h) bzw. ein PUT-Wert. Er wird vom Roster-/
    # Briefing-Import NIE aktualisiert oder gelöscht → ein alter GPS-Sample
    # („München", früher „San Francisco") friert ein und widerspricht dem echten
    # Plan (z.B. OPO-FRA). Der Roster (user_ical_briefings.ical_location) ist die
    # zuverlässige, plan-verankerte Quelle. Die iOS-Family-Card bevorzugt ohnehin
    # `layover_place ?? current_city` (CrewStatusBigCard.swift) — wir machen die
    # layover_place-Ableitung robuster und unterdrücken eine stale current_city,
    # die dem Roster widerspricht.
    hb = (prof.get('homebase') or prof.get('home_base') or '').strip().upper()
    roster_layover = None   # IATA des heutigen Layover-Orts (≠ Homebase), wenn ermittelbar
    roster_today_home = False  # heutiger Roster-Tag liegt POSITIV an der Homebase
    flying_now = False          # NOW im heutigen Dienst-Zeitfenster → „Fliegt gerade"
    today_dep = today_arr = None
    today_dep_iso = today_arr_iso = None
    if sb_avail and sb is not None:
        # ical_location-Format: „JFK, FRA-JFK-FRA". Erstes Token = Aufenthaltsort
        # des Tages. An einem HOMEBASE-Tag (Tag-Trip FRA-LUX-FRA → erstes Token =
        # FRA) ist man NICHT im Layover, sondern zuhause → nicht als Layover werten.
        try:
            today = _dt.datetime.now().date().isoformat()
            r = (sb.table('user_ical_briefings')
                 .select('ical_location,ical_summary,ical_start,ical_end')
                 .eq('token', crew_token).eq('datum', today).limit(1).execute())
            rows = r.data or []
            if rows:
                row = rows[0]
                loc = (row.get('ical_location') or '').strip()
                first = loc.split(',')[0].strip().upper()
                summ = row.get('ical_summary') or ''
                # Heutige Flug-Legs: erste DEP, letzte ARR. Bevorzugt aus dem Summary
                # („FRA-MUC-BIO 14:30-…" / mehrere „XXX-YYY"), sonst die Routing-Kette
                # aus ical_location (Teil nach dem Komma, z.B. „BCN-BIO-MUC").
                legs = re.findall(r'\b([A-Z]{3})-([A-Z]{3})\b', summ)
                chain = None
                if legs:
                    chain = [legs[0][0]] + [b for _, b in legs]
                else:
                    mchain = re.search(r'\b([A-Z]{3}(?:-[A-Z]{3})+)\b', loc)
                    if mchain:
                        chain = mchain.group(1).split('-')
                is_flight_today = bool(chain) and len(chain) >= 2
                if is_flight_today:
                    today_dep, today_arr = chain[0], chain[-1]
                    today_dep_iso = _iso_or_none(row.get('ical_start'))
                    today_arr_iso = _iso_or_none(row.get('ical_end'))
                    # In-Flight-Fenster: NOW zwischen Dienst-Start und -Ende. Beide ISO
                    # mit Zone (Upload speichert Europe/Berlin→UTC). Fehlt das Ende →
                    # grobes Fenster Start … Start+10h (Langstrecke abgedeckt).
                    st = _parse_iso(today_dep_iso)
                    en = _parse_iso(today_arr_iso)
                    now = _dt.datetime.now(_dt.timezone.utc)
                    if st and not en:
                        en = st + _dt.timedelta(hours=10)
                    if st and en and st <= now <= en:
                        flying_now = True
                    # Nach der Landung (now > Ende) ist die Crew am ZIEL → das ist der
                    # echte Layover-Ort, nicht der Abflug. Vor/während Flug KEIN
                    # „In <Abflug>"-Layover (der Bug). Homebase-Ziel = zuhause.
                    if en and now > en and today_arr:
                        if today_arr == hb and hb:
                            roster_today_home = True
                        else:
                            roster_layover = today_arr
                elif len(first) == 3 and first.isalpha():
                    # Kein Flugtag → erstes Token ist der echte Aufenthaltsort.
                    if first == hb and hb:
                        roster_today_home = True
                    elif first != hb:
                        roster_layover = first
        except Exception:
            pass
    if 'next_flight' in allowed_fields:
        status['flying_now'] = flying_now
        status['today_dep_iata'] = today_dep
        status['today_arr_iata'] = today_arr
        status['today_dep_iso'] = today_dep_iso
        status['today_arr_iso'] = today_arr_iso
    if 'layover_place' in allowed_fields:
        status['layover_place'] = roster_layover
        # Feierabend/Zuhause: heute an der Homebase und NICHT gerade in der Luft.
        status['home_now'] = bool(roster_today_home) and not flying_now
    # Reconcile current_city gegen den Roster: wenn der heutige Plan POSITIV an
    # der Homebase liegt (roster_today_home — erstes ical_location-Token == hb),
    # ist die Crew laut Plan zuhause — eine widersprechende GPS-Stadt (anderer
    # Ort) ist dann ein veralteter Sample und wird NICHT an die Family geleakt.
    # Wir erfinden keine Stadt, wir entfernen nur eine nachweislich plan-widrige.
    # WICHTIG: nur bei POSITIVEM Home-Signal — ein OFF-/Rest-Tag OHNE IATA-Code
    # (z.B. mehrtägiger Layover-Ruhetag fern der Base) lässt current_city stehen,
    # sonst würden wir eine legitime Layover-GPS-Stadt fälschlich unterdrücken.
    # Hat der Roster einen echten Layover (roster_layover gesetzt), ist DER die
    # Wahrheit; eine current_city, die diese IATA nicht erkennbar enthält (ein
    # einfacher Contains scheitert für ausgeschriebene Städtenamen wie „München"),
    # wird unterdrückt, weil die Family bereits layover_place sieht und zwei
    # widersprüchliche Orte nur verwirren.
    if 'current_city' in allowed_fields and status.get('current_city'):
        if roster_today_home:
            # Plan = Homebase-Tag → keine widersprechende GPS-Stadt servieren.
            status['current_city'] = None
        elif roster_layover is not None:
            # Plan = Layover an roster_layover → wenn die GPS-Stadt diesen Ort
            # nicht erkennbar enthält, ist sie stale → unterdrücken (layover_place
            # trägt die korrekte Info).
            cc = str(status['current_city']).strip().upper()
            if roster_layover not in cc:
                status['current_city'] = None
    # Felder die NICHT in allowed_fields sind: explicit auf None setzen
    # (Privacy-Garantie: Server filtert, Client kann nicht durchgeben was nicht gegranted).
    if 'current_city' not in allowed_fields:
        status['current_city'] = None
    if 'next_flight' not in allowed_fields:
        status['next_flight_no'] = None
        status['next_flight_dep_iata'] = None
        status['next_flight_arr_iata'] = None
        status['next_flight_etd_iso'] = None
    if 'layover_place' not in allowed_fields:
        status['layover_place'] = None
    if 'landed_status' not in allowed_fields:
        status['landed'] = None
    if 'photos' not in allowed_fields:
        status['photo_count_today'] = None
    if not (allowed_fields & {'current_city', 'last_seen', 'landed_status', 'next_flight'}):
        status['last_seen_iso'] = None
    return status


def _crew_short_name(crew_token):
    """Display-Name für die Family-Card. Privacy: kein Email-Leak, nur
    profile.name (kann selbst-gewählt sein). Liest SB-primary via
    _load_crew_profile (gleicher Pfad wie GET /api/user/profile).
    Fallback NIE der rohe Token (der ist ein Auth-Slice, kein Anzeigename),
    sondern ein neutraler Platzhalter."""
    prof = _load_crew_profile(crew_token)
    n = prof.get('name')
    if isinstance(n, str) and n.strip():
        return n.strip()
    return 'AeroX-Crew'


def _crew_avatar(crew_token):
    """Profilfoto-URL der Crew für die Family-Avatare (User 2026-06-25: „Avatars
    mit Profilfoto oben, ein Klick wechselt zwischen mehreren Crew"). Nur die
    öffentliche avatar_url — kein Token/PII."""
    prof = _load_crew_profile(crew_token) or {}
    a = prof.get('avatar_url')
    return a if (isinstance(a, str) and a.strip()) else None


def _crew_homebase(crew_token):
    """Homebase-IATA aus dem Crew-Profil (für die Redeem-Bestätigung der
    Family-Person). Liest SB-primary via _load_crew_profile (gleicher Pfad wie
    GET /api/user/profile). None wenn nicht gesetzt."""
    prof = _load_crew_profile(crew_token)
    hb = prof.get('homebase') or prof.get('home_base')
    if isinstance(hb, str) and hb.strip():
        return hb.strip()
    return None


# ════════════════════════════════════════════════════════════════════
#  Pairing-Code + Scoped Family-Token  (Security-Fix 2026-06-05)
#
#  Vorher teilte die iOS-App den ROHEN Crew-Bearer (appState.token) als
#  "Verbindungs-Code" — ein App-weites Auth-Credential. Wer es abfing,
#  konnte sich als der Crew-Account ausgeben.
#
#  Neuer Flow:
#    1) Crew ruft  POST /api/family/pair-code/<crew_token>/create
#       → kurzer Code (6 Zeichen, A-Z2-9 ohne Ambiguität), TTL 30 min,
#         regenerieren invalidiert den vorherigen Code dieses Crews.
#    2) Family ruft POST /api/family/pair-code/redeem  body={code,family_name}
#       → erzeugt einen SCOPED, read-only family_token (NICHT der Crew-Bearer),
#         der nur die family-watch-Read-Pfade für diesen Crew freischaltet.
#         Returns {family_token, crew_name, crew_homebase}.
#
#  Der scoped family_token (Prefix AT-FAM-) wird in einer eigenen Tabelle
#  family_token -> crew_token (read-only scope) gehalten. Der bestehende
#  /api/family-watch/<token>/feed akzeptiert BEIDE: den scoped Token (neu,
#  bevorzugt) und — back-compat — jeden family_token der via grant existiert.
# ════════════════════════════════════════════════════════════════════

_PAIR_CODE_TTL_SEC = 30 * 60          # 30 Minuten
_PAIR_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # ohne I/O/0/1
_PAIR_CODE_LEN = 6


def _pair_codes_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_pair_codes.json')


def _scoped_tokens_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_scoped_tokens.json')


# --- SB-Persistenz für Pair-Codes + Scoped-Tokens (#31) ------------------------
# Vorher waren beide NUR auf Disk → auf Cloud Run (ephemer, multi-instance, bei
# jedem Deploy gewiped) verloren sich Pair-Codes (Redeem schlug fehl) und
# Scoped-Family-Tokens (Family-Watcher sah „seinen" Plan nicht mehr). Jetzt
# SB-primary mit Disk-Fallback. SICHER: existieren die SB-Tabellen noch nicht
# (User hat PASTE_ME_IN_SUPABASE.sql noch nicht ausgeführt), werfen die SB-Calls
# → None/False → es bleibt beim bisherigen Disk-Verhalten (keine Regression).
# `data` ist ein jsonb-Blob (der komplette Record), Key ist code bzw. family_token.
def _kv_load_from_sb(table, key_col):
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return None
    try:
        out = {}
        r = sb.table(table).select('*').execute()
        for row in (r.data or []):
            k = row.get(key_col)
            if k:
                out[k] = row.get('data') or {}
        return out
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        return None


def _kv_save_to_sb(table, key_col, data):
    """UPSERT-ONLY: upsertet die vorhandenen Keys in SB. KEIN Reconcile-Delete
    mehr (#7/#19): auf Cloud Run multi-instance hätte das Löschen von Keys, die
    in einem stalen In-Memory-Snapshot fehlen, Pair-Codes/Scoped-Tokens anderer
    Crews weggewischt (Race: Instance A löscht was Instance B gerade angelegt
    hat → fremder Redeem brach). Echte Deletes (konsumierter/abgelaufener Code)
    machen die Caller jetzt per gezieltem .delete().eq(...)."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        rows = [{key_col: k, 'data': v} for k, v in (data or {}).items() if k]
        for i in range(0, len(rows), 500):
            sb.table(table).upsert(rows[i:i+500], on_conflict=key_col).execute()
        return True
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_save_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _kv_delete_from_sb(table, key_col, key):
    """Gezieltes Löschen eines einzelnen Keys (#7/#19): ersetzt das frühere
    blanket Reconcile-Delete. Guarded, wirft nie."""
    if not key:
        return False
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        sb.table(table).delete().eq(key_col, key).execute()
        return True
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_delete_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _requests_delete_from_sb(crew_token, family_token):
    """Gezieltes Löschen einer Anfrage in SB (#7): ersetzt das Reconcile-Delete
    in _requests_save_to_sb. Guarded, wirft nie."""
    if not crew_token or not family_token:
        return False
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        (sb.table('family_requests').delete()
         .eq('crew_token', crew_token).eq('family_token', family_token).execute())
        return True
    except Exception as e:
        _log().warning(f'[family-req] sb_delete_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _pair_codes_load_from_disk():
    p = _pair_codes_disk_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _pair_codes_load():
    sb = _kv_load_from_sb('family_pair_codes', 'code')
    disk = _pair_codes_load_from_disk()
    if sb is None:
        return disk
    merged = dict(disk); merged.update(sb)   # SB-primary
    return merged


def _pair_codes_save(codes):
    sb_ok = _kv_save_to_sb('family_pair_codes', 'code', codes)
    disk_ok = False
    try:
        _atomic_write_json(_pair_codes_disk_path(), codes)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[family-pair] codes_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _scoped_tokens_load_from_disk():
    p = _scoped_tokens_disk_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _scoped_tokens_load():
    sb = _kv_load_from_sb('family_scoped_tokens', 'family_token')
    disk = _scoped_tokens_load_from_disk()
    if sb is None:
        return disk
    merged = dict(disk); merged.update(sb)   # SB-primary
    return merged


def _scoped_tokens_save(toks):
    sb_ok = _kv_save_to_sb('family_scoped_tokens', 'family_token', toks)
    disk_ok = False
    try:
        _atomic_write_json(_scoped_tokens_disk_path(), toks)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[family-pair] scoped_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _scoped_token_crew(family_token):
    """Returns crew_token wenn family_token ein gültiger scoped read-only
    Family-Token ist (Prefix AT-FAM- in family_scoped_tokens), sonst None."""
    if not family_token:
        return None
    toks = _scoped_tokens_load()
    rec = toks.get(family_token)
    if isinstance(rec, dict):
        return rec.get('crew_token')
    return None


def _gen_pair_code():
    import secrets
    return ''.join(secrets.choice(_PAIR_CODE_ALPHABET) for _ in range(_PAIR_CODE_LEN))


def _gen_scoped_family_token():
    import secrets
    return 'AT-FAM-' + secrets.token_urlsafe(18)


def _normalize_code(raw):
    """Uppercase, Whitespace/Bindestriche weg, dann tolerant gegen die typischen
    Tipp-Verwechsler mappen (0→O, 1→I, I→? ...). Da das Generator-Alphabet
    weder I/O/0/1 enthält, mappen wir die wahrscheinlichen Verwechsler auf ihr
    Alphabet-Pendant: 0→O ist NICHT im Alphabet, also nutzen wir die andere
    Richtung — wir behandeln O als 0-Tippfehler? Beides ambig. Einfacher: wir
    werfen alles raus was NICHT im Alphabet ist (A-HJ-NP-Z + 2-9)."""
    if not raw or not isinstance(raw, str):
        return ''
    s = re.sub(r'[^A-Za-z0-9]', '', raw).upper()
    # Häufige Tippfehler auf gültige Alphabet-Zeichen korrigieren:
    #   0 (Null)  → O ist NICHT im Alphabet → verwerfen
    #   1 (Eins)  → I ist NICHT im Alphabet → verwerfen
    # Wir verwerfen daher schlicht alle Nicht-Alphabet-Zeichen.
    s = re.sub(r'[^A-HJ-NP-Z2-9]', '', s)
    return s[:_PAIR_CODE_LEN]


# ════════════════════════════════════════════════════════════════════
#  Family-Side
# ════════════════════════════════════════════════════════════════════

@family_watch_bp.route('/api/family-watch/<token>/feed', methods=['GET'])
def family_watch_feed(token):
    """Family-User holt feed: alle Crews die ihm was gegranted haben."""
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    shares = _shares_load()
    # Scoped read-only Family-Token (neu, bevorzugt): er ist NICHT der Crew-Bearer,
    # sondern ein per Pairing-Code gemünzter Token der genau auf einen Crew zeigt.
    # Wenn das eingehende Token ein solcher ist, verwenden wir die scoped-Tabelle
    # als zusätzliche Quelle (ein Crew-Eintrag, auch wenn noch kein grant existiert).
    scoped_crew = _scoped_token_crew(token)
    # Filter: alle grants mit family_token == this token (back-compat)
    relevant = [s for s in shares if s.get('family_token') == token]
    if scoped_crew:
        # Sicherstellen dass der gepairte Crew im Feed auftaucht, auch falls der
        # Crew noch keine Felder explizit gegranted hat → dann mit leerer
        # allowed_fields-Liste (Card zeigt "wartet auf Freigabe").
        if not any(s.get('crew_token') == scoped_crew for s in relevant):
            relevant.append({'crew_token': scoped_crew, 'family_token': token,
                             'fields': []})
    out = []
    for s in relevant:
        crew_token = s.get('crew_token')
        if not crew_token:
            continue
        fields = list(s.get('fields') or [])
        # nur erlaubte Felder durchlassen
        fields_clean = [f for f in fields if f in ALLOWED_FIELDS]
        status = _load_crew_status_for_family(crew_token, set(fields_clean))
        # SECURITY (2026-06 Audit): NIE das echte Crew-Bearer-Token an die
        # Family-Seite ausliefern — damit wäre voller Account-Zugriff möglich
        # (Roster, DMs, Grants widerrufen, Account löschen). Der Client nutzt
        # das Feld nur als stabile Identifiable-ID, also liefern wir unter dem
        # alten Key einen opaken, pro Pairing stabilen Hash (Salt = Family-
        # Token, damit IDs nicht über Familien hinweg korrelierbar sind).
        opaque_id = hashlib.sha256(
            f'{crew_token}:{token}'.encode('utf-8')
        ).hexdigest()[:16]
        out.append({
            'crew_token': opaque_id,
            'crew_short_name': _crew_short_name(crew_token),
            'crew_avatar_url': _crew_avatar(crew_token),
            'status': status,
            'allowed_fields': fields_clean,
        })
    return jsonify({'watched': out, 'count': len(out)})


# ════════════════════════════════════════════════════════════════════
#  Crew-Side
# ════════════════════════════════════════════════════════════════════

@family_watch_bp.route('/api/family-share/<token>/list', methods=['GET'])
def family_share_list(token):
    """Crew-User holt seine eigenen Grants (wer sieht mich + welche Felder)."""
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    shares = _shares_load()
    own = [s for s in shares if s.get('crew_token') == token]
    grants = []
    for s in own:
        ft = s.get('family_token')
        grants.append({
            'family_token': ft,
            'family_short_name': (ft or '')[:8] if ft else None,
            'family_relation': s.get('relation'),
            'fields': [f for f in (s.get('fields') or []) if f in ALLOWED_FIELDS],
            'created_at': s.get('created_at'),
        })
    return jsonify({'grants': grants, 'count': len(grants)})


@family_watch_bp.route('/api/family-share/<token>/grant', methods=['POST'])
def family_share_grant(token):
    """Crew-User gewährt Family-Person Lese-Zugriff auf bestimmte Felder."""
    # Token-Auth: vom before_request-Hook in app.py gecheckt (auth-required).
    # Hier nur form-validation.
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    family_token = (body.get('family_token') or '').strip()
    if not family_token:
        return jsonify({'ok': False, 'error': 'missing_family_token'}), 400
    if family_token == token:
        return jsonify({'ok': False, 'error': 'cannot_grant_self'}), 400
    relation = (body.get('relation') or '').strip().lower() or 'family'
    if relation not in ALLOWED_RELATIONS:
        relation = 'family'
    raw_fields = body.get('fields') or []
    if not isinstance(raw_fields, list):
        return jsonify({'ok': False, 'error': 'fields_must_be_list'}), 400
    fields = [f for f in raw_fields if isinstance(f, str) and f in ALLOWED_FIELDS]
    if not fields:
        return jsonify({'ok': False, 'error': 'no_valid_fields'}), 400

    shares = _shares_load()
    # Existing grant? → update statt duplizieren
    found = False
    for s in shares:
        if s.get('crew_token') == token and s.get('family_token') == family_token:
            s['fields'] = fields
            s['relation'] = relation
            s['updated_at'] = _dt.datetime.now().isoformat()
            found = True
            break
    if not found:
        shares.append({
            'crew_token': token,
            'family_token': family_token,
            'relation': relation,
            'fields': fields,
            'created_at': _dt.datetime.now().isoformat(),
        })
    if not _shares_save(shares):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    return jsonify({'ok': True, 'fields': fields, 'relation': relation})


@family_watch_bp.route('/api/family-request/<family_token>', methods=['POST'])
def family_request_create(family_token):
    """Familie schickt einer GESUCHTEN Crew-Person eine Beobachtungs-Anfrage.
    Body: {crew_token, relation}. Ersetzt den Pair-Code-Flow."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    crew_token = (body.get('crew_token') or '').strip()
    if not crew_token or crew_token == family_token:
        return jsonify({'ok': False, 'error': 'bad_crew_token'}), 400
    relation = (body.get('relation') or 'family').strip().lower()
    if relation not in ALLOWED_RELATIONS:
        relation = 'family'
    fam_prof = _load_crew_profile(family_token) or {}
    reqs = _requests_load()
    if any(r.get('crew_token') == crew_token and r.get('family_token') == family_token
           for r in reqs):
        return jsonify({'ok': True, 'already': True})
    reqs.append({
        'crew_token': crew_token, 'family_token': family_token, 'relation': relation,
        'requester_name': (fam_prof.get('name') or 'Familie'),
        'requester_avatar': fam_prof.get('avatar_url'),
        'created_at': _dt.datetime.now().isoformat(),
    })
    _requests_save(reqs)
    return jsonify({'ok': True})


@family_watch_bp.route('/api/family-request/<crew_token>/pending', methods=['GET'])
def family_request_pending(crew_token):
    """Crew-Person: offene Familie-Anfragen zum Bestätigen/Ablehnen."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    reqs = _requests_load()
    out = [{
        'family_token': r['family_token'],
        'requester_name': r.get('requester_name') or 'Familie',
        'requester_avatar': r.get('requester_avatar'),
        'relation': r.get('relation') or 'family',
        'created_at': r.get('created_at'),
    } for r in reqs if r.get('crew_token') == crew_token]
    return jsonify({'ok': True, 'requests': out})


@family_watch_bp.route('/api/family-request/<crew_token>/approve', methods=['POST'])
def family_request_approve(crew_token):
    """Crew bestätigt eine Familie-Anfrage → erstellt den family_shares-Grant.
    Body: {family_token, fields?}. fields default = alle erlaubten."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    family_token = (body.get('family_token') or '').strip()
    if not family_token:
        return jsonify({'ok': False, 'error': 'missing_family_token'}), 400
    reqs = _requests_load()
    match = next((r for r in reqs
                  if r.get('crew_token') == crew_token
                  and r.get('family_token') == family_token), None)
    if not match:
        return jsonify({'ok': False, 'error': 'no_request'}), 404
    raw_fields = body.get('fields') or list(ALLOWED_FIELDS)
    fields = [f for f in raw_fields if f in ALLOWED_FIELDS] or list(ALLOWED_FIELDS)
    relation = match.get('relation') or 'family'
    shares = _shares_load()
    if not any(s.get('crew_token') == crew_token and s.get('family_token') == family_token
               for s in shares):
        shares.append({
            'crew_token': crew_token, 'family_token': family_token,
            'relation': relation, 'fields': fields,
            'created_at': _dt.datetime.now().isoformat(),
        })
        _shares_save(shares)
    # Genuine Delete: die genehmigte Anfrage gezielt aus SB entfernen (#7).
    # _requests_save upsertet nur noch (kein Reconcile-Delete), darum hier
    # explizit. Disk wird über _requests_save mitgeschrieben.
    _requests_delete_from_sb(crew_token, family_token)
    _requests_save([r for r in reqs
                    if not (r.get('crew_token') == crew_token
                            and r.get('family_token') == family_token)])
    return jsonify({'ok': True, 'fields': fields})


@family_watch_bp.route('/api/family-request/<crew_token>/reject', methods=['POST'])
def family_request_reject(crew_token):
    """Crew lehnt eine Familie-Anfrage ab (Anfrage entfernen). Body: {family_token}."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    family_token = (body.get('family_token') or '').strip()
    reqs = _requests_load()
    # Genuine Delete: die abgelehnte Anfrage gezielt aus SB entfernen (#7).
    _requests_delete_from_sb(crew_token, family_token)
    _requests_save([r for r in reqs
                    if not (r.get('crew_token') == crew_token
                            and r.get('family_token') == family_token)])
    return jsonify({'ok': True})


@family_watch_bp.route('/api/family-share/<token>/revoke/<family_token>',
                       methods=['DELETE'])
def family_share_revoke(token, family_token):
    """Crew-User widerruft Grant für eine Family-Person."""
    safe = _safe_token(token)
    safe_ft = _safe_token(family_token)
    if not safe or not safe_ft:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    shares = _shares_load()
    new_shares = [s for s in shares
                  if not (s.get('crew_token') == token
                          and s.get('family_token') == family_token)]
    if len(new_shares) == len(shares):
        # War nie gegranted → idempotent return ok
        return jsonify({'ok': True, 'revoked': False, 'message': 'no_grant_found'})
    if not _shares_save(new_shares):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    # SB-soft-delete: wir haben oben replacing-upsert gemacht. Für SB den
    # konkreten record markieren als deleted.
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            (sb.table('family_shares')
             .update({'deleted': True})
             .eq('crew_token', token)
             .eq('family_token', family_token)
             .execute())
        except Exception as e:
            _log().warning(f'[family-share] sb_revoke_skip {type(e).__name__}')
    return jsonify({'ok': True, 'revoked': True})


# ════════════════════════════════════════════════════════════════════
#  Pairing-Code-Endpunkte
# ════════════════════════════════════════════════════════════════════

def _resolve_crews_for_family(family_token):
    """ALLE Crew-Tokens zu einem Family-Token (scoped + alle Grants), dedupliziert."""
    out = []
    seen = set()
    scoped = _scoped_token_crew(family_token)
    if scoped and scoped not in seen:
        seen.add(scoped); out.append(scoped)
    try:
        for s in _shares_load():
            if s.get('family_token') == family_token:
                ct = s.get('crew_token')
                if ct and ct not in seen:
                    seen.add(ct); out.append(ct)
    except Exception:
        pass
    return out


def _resolve_crew_for_family(family_token, opaque_id=None):
    """Findet den Crew-Token zu einem Family-Token. Mit `opaque_id` (der pro
    Pairing stabile sha256(crew:family)[:16]-Hash aus dem Watch-Feed) wird GENAU
    der gemeinte Crew aufgelöst → Family kann mehreren Crew folgen. Ohne id:
    der primäre (scoped) Crew."""
    crews = _resolve_crews_for_family(family_token)
    if opaque_id:
        for ct in crews:
            h = hashlib.sha256(f'{ct}:{family_token}'.encode('utf-8')).hexdigest()[:16]
            if h == opaque_id:
                return ct
    return crews[0] if crews else None


def _load_crew_roster_days(crew_token, days_limit):
    """Lädt das Roster der Crew als Tag-Detail-Liste — EXAKT die gleiche Quelle
    wie GET /api/user/friend-roster (in-memory _store, Fallback roster_snapshot).
    Privacy: liefert KEINE Geld-Felder (eur), nur Plan-Infos (Klasse, Routing,
    Layover, Zeiten) — Family sieht den Plan, nicht die Steuer."""
    from datetime import date as _date, timedelta as _td
    _store = _app_attr('_store', {}) or {}
    sess = _store.get(crew_token) or {}
    tage = (sess.get('result_data') or {}).get('_tage_detail') or []
    if not tage:
        snap_fn = _app_attr('_roster_snapshot_read')
        if callable(snap_fn):
            try:
                tage = (snap_fn(crew_token) or {}).get('tage') or []
            except Exception:
                tage = []
    today = _date.today()
    cutoff = today + _td(days=days_limit)
    out = []
    for day in tage:
        if not isinstance(day, dict):
            continue
        d = day.get('datum')
        if not d:
            continue
        try:
            day_date = _date.fromisoformat(d[:10])
            if day_date > cutoff or day_date < today - _td(days=45):
                continue
        except Exception:
            continue
        rf = day.get('reader_facts') or {}
        out.append({
            'datum': d,
            'klass': day.get('klass'),
            'marker': day.get('marker'),
            'routing': day.get('routing'),
            # bewusst KEIN 'eur' — Family sieht keine Geld-Daten
            'layover_ort': rf.get('layover_ort'),
            'start_time': rf.get('start_time'),
            'end_time': rf.get('end_time'),
        })

    # FALLBACK (User: „bin bei Family drin und kann den Kalender nicht sehen"):
    # kein Tax-_tage_detail und kein roster_snapshot → den Plan DIREKT aus
    # user_ical_briefings bauen (gleiche Quelle wie der Live-Status). So sieht die
    # Family den Kalender auch ohne dass die Crew je eine Steuer-Auswertung lief.
    if not out:
        sb_avail, sb = _get_sb()
        if sb_avail and sb is not None:
            try:
                start = (today - _td(days=45)).isoformat()
                end = cutoff.isoformat()
                # Nur EXISTIERENDE Spalten selektieren (ical_klass gibt es NICHT —
                # ein Select darauf wirft 42703 → Fallback lieferte leer).
                r = (sb.table('user_ical_briefings')
                     .select('datum,ical_summary,ical_location,ical_start,ical_end')
                     .eq('token', crew_token)
                     .gte('datum', start).lte('datum', end)
                     .order('datum').limit(150).execute())

                def _hhmm(x):
                    m = re.search(r'T(\d{2}:\d{2})', str(x or ''))
                    return m.group(1) if m else None

                for row in (r.data or []):
                    d = row.get('datum')
                    if not d:
                        continue
                    summ = (row.get('ical_summary') or '')
                    up = summ.upper()
                    klass = 'OFF' if 'OFF DAY' in up else ('Z76' if 'LAYOVER' in up else None)
                    codes = re.findall(r'\b[A-Z]{3}\b', (row.get('ical_location') or '').upper())
                    dedup = []
                    for c in codes:
                        if not dedup or dedup[-1] != c:
                            dedup.append(c)
                    routing = '-'.join(dedup) if len(dedup) >= 2 else None
                    out.append({
                        'datum': d,
                        'klass': klass,
                        'marker': summ,
                        'routing': routing,
                        'layover_ort': None,
                        'start_time': _hhmm(row.get('ical_start')),
                        'end_time': _hhmm(row.get('ical_end')),
                    })
            except Exception as e:
                _log().info(f'[family-roster] ical_fallback_skip {type(e).__name__}')
    return out


@family_watch_bp.route('/api/family-roster/<family_token>', methods=['GET'])
def family_roster(family_token):
    """Family-Person holt den (read-only) Kalender/Plan der gepairten Crew.
    Query: ?days=60 (default 60, max 120).
    Privacy: nur Plan-Infos, keine Geld-/Steuer-Daten. Respektiert ein explizites
    share_roster=False als Opt-Out (honest empty)."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token', 'days': []}), 400
    try:
        days_limit = min(max(int(request.args.get('days') or 60), 1), 120)
    except Exception:
        days_limit = 60
    opaque_id = (request.args.get('crew') or '').strip() or None
    crew_token = _resolve_crew_for_family(family_token, opaque_id=opaque_id)
    if not crew_token:
        return jsonify({'ok': False, 'shared': False,
                        'error': 'not_paired', 'days': []}), 404
    # KEIN Opt-Out mehr (Produkt-Entscheidung 2026-06-25): das Annehmen einer
    # Familie-Anfrage IST die Zustimmung, den Plan zu teilen. Der share_roster-
    # Aus-Schalter ist im Client entfernt — eine bestätigte Familie sieht den
    # Plan immer (read-only, weiterhin OHNE Geld-/Steuer-Daten).
    days = _load_crew_roster_days(crew_token, days_limit)
    return jsonify({
        'ok': True, 'shared': True, 'count': len(days), 'days': days,
        'crew_name': _crew_short_name(crew_token),
        'crew_homebase': _crew_homebase(crew_token),
    })


@family_watch_bp.route('/api/family/pair-code/<token>/create', methods=['POST'])
def family_pair_code_create(token):
    """Crew erzeugt einen kurzen, kurzlebigen Pairing-Code.

    Auth: der Crew-Bearer steht im Pfad (<token>) → der zentrale
    before_request-Gate (_bug004_token_auth_gate) validiert ihn gegen
    auth_users, weil das AT-...-Pattern matched und es ein POST ist.

    Regenerieren invalidiert den vorherigen Code dieses Crews (1 aktiver Code
    pro Crew). Returns {code, expires_in}.
    """
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400

    codes = _pair_codes_load()
    now = time.time()
    # Alte/abgelaufene Codes ausmisten + jeden vorhandenen Code DIESES Crews
    # entfernen (Regenerate invalidiert den vorigen).
    codes = {c: rec for c, rec in codes.items()
             if isinstance(rec, dict)
             and (now - float(rec.get('created_at', 0))) < _PAIR_CODE_TTL_SEC
             and rec.get('crew_token') != token}

    # Neuen, kollisionsfreien Code generieren.
    code = _gen_pair_code()
    tries = 0
    while code in codes and tries < 10:
        code = _gen_pair_code()
        tries += 1

    codes[code] = {
        'crew_token': token,
        'created_at': now,
        'consumed': False,
    }
    _pair_codes_save(codes)
    return jsonify({'ok': True, 'code': code, 'expires_in': _PAIR_CODE_TTL_SEC})


@family_watch_bp.route('/api/family/pair-code/redeem', methods=['POST'])
def family_pair_code_redeem():
    """Family-Person löst einen Pairing-Code ein.

    Public (kein Auth-Gate): der Family-User hat noch keinen Crew-Token; der
    kurze Code IST das Geheimnis. body={code, family_name?}.

    Bei gültigem, nicht-abgelaufenem Code wird ein SCOPED, read-only
    family_token (Prefix AT-FAM-) gemünzt der nur auf diesen einen Crew zeigt —
    NICHT der Crew-Bearer. Returns {family_token, crew_name, crew_homebase}.
    """
    body = request.get_json(silent=True) or {}
    code = _normalize_code(body.get('code') or '')
    if not code or len(code) != _PAIR_CODE_LEN:
        return jsonify({'ok': False, 'error': 'invalid_code'}), 400

    codes = _pair_codes_load()
    now = time.time()
    rec = codes.get(code)
    if not isinstance(rec, dict):
        return jsonify({'ok': False, 'error': 'code_not_found'}), 404
    if (now - float(rec.get('created_at', 0))) >= _PAIR_CODE_TTL_SEC:
        # Abgelaufen → aufräumen. Gezieltes SB-Delete (#7/#19): _pair_codes_save
        # upsertet nur noch, darum den Key hier explizit aus SB entfernen.
        codes.pop(code, None)
        _kv_delete_from_sb('family_pair_codes', 'code', code)
        _pair_codes_save(codes)
        return jsonify({'ok': False, 'error': 'code_expired'}), 410

    # Single-Use: ein bereits eingelöster Code mintet KEIN zweites Family-Token.
    # (Security-Audit 2026-06-07: vorher blieb ein konsumierter Code bis TTL
    # einlösbar → wer den Code abfängt, könnte nach dem legitimen User ein
    # eigenes Family-Token münzen.)
    if rec.get('consumed'):
        return jsonify({'ok': False, 'error': 'code_already_used'}), 409

    crew_token = rec.get('crew_token')
    if not crew_token:
        return jsonify({'ok': False, 'error': 'code_invalid'}), 400

    family_name = (body.get('family_name') or '').strip()[:60] or None

    # --- TOCTOU-Schutz (#20): Single-Use atomar durchsetzen, BEVOR ein Token
    # gemünzt wird. Vorher war read→check→mint→mark-consumed nicht atomar: zwei
    # parallele Redeems desselben Codes konnten beide zwei Family-Tokens münzen.
    # Strategie:
    #   1) Wenn SB verfügbar: atomares conditional update
    #        update(data=consumed) WHERE code=? AND data->>consumed='false'
    #      Nur wenn das genau diese Zeile getroffen hat (r.data nicht leer),
    #      haben WIR das Rennen gewonnen → mint erlaubt. Trifft es 0 Zeilen,
    #      hat ein anderer Redeemer den Code schon konsumiert (oder der Code
    #      existiert nur auf Disk) → wir verweigern bzw. fallen auf Disk zurück.
    #   2) Ohne SB: consumed=True setzen und ZUERST speichern, DANN minten —
    #      das verkleinert das Fenster auf die Disk-Schreiblatenz.
    consumed_rec = dict(rec)
    consumed_rec['consumed'] = True
    consumed_rec['consumed_at'] = now

    sb_avail, sb = _get_sb()
    won_via_sb = False
    if sb_avail and sb is not None:
        try:
            r = (sb.table('family_pair_codes')
                 .update({'data': consumed_rec})
                 .eq('code', code)
                 .eq('data->>consumed', 'false')
                 .execute())
            if r.data:
                won_via_sb = True
            else:
                # 0 Zeilen getroffen. Existiert der Code in SB bereits als
                # consumed → eindeutig schon eingelöst → ablehnen. Existiert er
                # gar nicht in SB (nur Disk) → unten auf Disk-Pfad zurückfallen.
                exists = (sb.table('family_pair_codes').select('code')
                          .eq('code', code).limit(1).execute())
                if exists.data:
                    return jsonify({'ok': False, 'error': 'code_already_used'}), 409
        except Exception as e:
            _log().warning(f'[family-pair] redeem_sb_consume_fail {type(e).__name__}: {str(e)[:140]}')

    if not won_via_sb:
        # Disk-Pfad (SB nicht verfügbar / Code nur auf Disk): consumed FIRST
        # speichern, DANN minten — verkleinert das TOCTOU-Fenster maximal ohne
        # echte Atomarität zu garantieren.
        codes[code] = consumed_rec
        _pair_codes_save(codes)

    # Scoped, read-only Family-Token münzen und persistieren.
    family_token = _gen_scoped_family_token()
    toks = _scoped_tokens_load()
    toks[family_token] = {
        'crew_token': crew_token,
        'scope': 'family_read',
        'family_name': family_name,
        'created_at': now,
    }
    _scoped_tokens_save(toks)

    if won_via_sb:
        # SB ist bereits konsumiert (atomar). Disk-Spiegel best-effort
        # nachziehen, damit ein späterer Disk-only-Read nicht „unconsumed" sieht.
        codes[code] = consumed_rec
        try:
            _atomic_write_json(_pair_codes_disk_path(), codes)
        except Exception:
            pass

    return jsonify({
        'ok': True,
        'family_token': family_token,
        'crew_name': _crew_short_name(crew_token),
        'crew_homebase': _crew_homebase(crew_token),
    })
