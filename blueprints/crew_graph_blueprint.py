# ═══════════════════════════════════════════════════════════════
#  Crew-Graph Blueprint — Worker P6a (Backend)
#
#  Server-side Aggregation des "Who-Flew-With-Whom"-Graph als Komplement
#  zum lokalen iOS-Graph (siehe aeris-ios/.../CrewGraph/CrewGraphModel.swift).
#  Pendant zu CrewGraphIngestor.swift + CrewGraphQueryEngine.swift.
#
#  Wiring in app.py:
#      from blueprints.crew_graph_blueprint import crew_graph_bp
#      app.register_blueprint(crew_graph_bp)
#
#  Endpunkte:
#      POST /api/crew-graph/<token>/ingest        — Upsert Edges nach Tour
#      GET  /api/crew-graph/<token>/match         — Match heutige Crew gegen Graph
#      GET  /api/crew-graph/<token>/edges         — Top-N stärkste Connections
#      GET  /api/crew-graph/<token>/common        — Shared History + Mutuals
#
#  Privacy-by-Design (identisch zur iOS-Side):
#    · Keine Klarnamen. `other_display_name` ist max die CAS-Form
#      "Schumann M." — der Caller MUSS bereits truncated übergeben.
#    · Bei nicht-App-Crew (kein Token): stable other_id =
#      sha256(self_token + shortname)[:12]. Damit kollidiert "Schumann M."
#      bei zwei Usern nicht miteinander, und Schumann selbst hat keine
#      ID-Korrelation zu seinem echten Token.
#    · other_token bleibt NULL bei Nicht-App-Usern → partial index
#      idx_crew_edges_other_token greift nur für App-User.
#
#  Storage-Strategie (analog wall_posts/forum_threads):
#    · SB primary (`crew_edges`-Tabelle, siehe Migration 20260601_crew_graph.sql)
#    · Disk fallback unter _USER_HISTORY_DIR/crew_edges_<token>.json
#    · Lazy-migrate Disk → SB beim ersten Read wenn SB leer
# ═══════════════════════════════════════════════════════════════

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

crew_graph_bp = Blueprint('crew_graph', __name__)

# ── Supabase-Anbindung über app.py (try/except hält das Blueprint
#    eigenständig testbar wenn app.py noch nicht geladen ist) ───
try:
    from app import sb as _sb, SB_AVAILABLE as _SB_AVAILABLE
except ImportError:
    _sb = None
    _SB_AVAILABLE = False


def _sb_client():
    """Lazy re-resolve, damit init-Order zwischen app.py und Blueprint egal ist.
    Beim ersten Request ist app.py bereits importiert und sb live."""
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


# ── Disk-Fallback-Pfad (mirrort _USER_HISTORY_DIR aus app.py) ───
_USER_HISTORY_DIR = '_user_history_state'

# Lock für SELECT-then-UPSERT-Fallback wenn die SB-RPC nicht existiert.
# Pro Token ein Lock — schützt nur den lokalen Worker; bei mehreren Pods
# sollte die RPC `crew_edges_upsert_increment` verwendet werden (atomar).
_TOKEN_LOCKS = {}
_LOCK_REGISTRY = threading.Lock()


def _lock_for(token):
    with _LOCK_REGISTRY:
        lk = _TOKEN_LOCKS.get(token)
        if lk is None:
            lk = threading.Lock()
            _TOKEN_LOCKS[token] = lk
        return lk


# ── Limits / Tunables ───────────────────────────────────────────
SHARED_LAYOVERS_MAX = 20
SHARED_ROUTES_MAX = 20
INGEST_MAX_CREW = 50                # Schutz gegen Müll-Bodies
MATCH_DEFAULT_LIMIT = 50
EDGES_DEFAULT_LIMIT = 50
EDGES_MAX_LIMIT = 200
DISPLAY_NAME_MAX = 40


# ─── Privacy-Helpers ───────────────────────────────────────────

def _safe_token_fragment(token):
    """Sanitiert ein Token für Disk-Pfade. Erlaubt nur [A-Za-z0-9_-], 64 chars max."""
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _hash_anon_id(self_token, shortname):
    """Stabile ID für nicht-App-Crew: sha256(self_token + ":" + shortname)[:12].
    Per-Self-Token-Salt verhindert Korrelation 'Schumann M. bei User A ≡
    Schumann M. bei User B'. Wir wollen nur, dass derselbe User immer denselben
    Anker für denselben CAS-Shortname kriegt."""
    raw = f'{self_token}::{(shortname or "").strip().lower()}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def _normalize_display_name(s):
    """Truncate + strip. Caller (iOS) liefert bereits CAS-Form "Schumann M.",
    wir kappen zur Sicherheit auf DISPLAY_NAME_MAX."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    return s[:DISPLAY_NAME_MAX]


def _normalize_position(p):
    if not p:
        return None
    p = str(p).strip()
    if not p:
        return None
    return p[:32]


def _opaque_peer_id(other_id, other_token):
    """Liefert eine stabile, NICHT-zurückführbare ID für einen anderen Crew-User,
    die gefahrlos an Clients ausgeliefert werden darf.

    SECURITY (P6a): `other_token` ist bei App-Usern das ROHE Bearer-Credential des
    anderen Users — es darf NIE in einer Response erscheinen (Account-Takeover).
    Bei App-Usern ist `other_id` im Schema identisch zum Token, also auch dieser
    Wert ist roh. Wir hashen daher: peer = sha256(other_token || other_id)[:16].

    · App-User (other_token gesetzt): opaker sha256-Hash → keine Token-Leakage.
    · Anon-Crew (other_token NULL, other_id = _hash_anon_id-Form, 12 hex): bereits
      ein per-self-token gesalzener Hash, KEIN Credential. Geben wir 1:1 durch,
      damit iOS denselben Anker behält.
    Stabil über Calls hinweg (gleicher Input → gleicher Hash)."""
    if other_token:
        raw = f'{other_token}::{other_id or ""}'
        return 'peer_' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    # Kein App-Token → other_id ist schon ein anon-Hash, kein Credential.
    return other_id


def _serialize_edge(row, *, extra=None):
    """Baut die client-sichere Repräsentation einer Edge.

    SECURITY: enthält bewusst KEIN `other_token` (und keinen anderen rohen
    Fremd-Token). Korrelation läuft ausschließlich über das opake `other_id`."""
    other_token = row.get('other_token')
    raw_other_id = row.get('other_id')
    out = {
        'other_id': _opaque_peer_id(raw_other_id, other_token),
        'short_name': row.get('other_display_name'),
        'position': row.get('other_position'),
        'tour_count': int(row.get('tour_count') or 0),
        'last_flown_date': row.get('last_flown_date'),
        'shared_layovers': row.get('shared_layovers') or [],
        'shared_routes': row.get('shared_routes') or [],
        'strength': _classify_strength(int(row.get('tour_count') or 0)),
    }
    if extra:
        out.update(extra)
    return out


def _resolve_other_id(self_token, member):
    """Bestimmt other_id + other_token aus einem crew_list-Member.
    Returns (other_id: str, other_token: Optional[str], display_name: Optional[str]).
    """
    other_token_raw = member.get('token')
    other_token = None
    if isinstance(other_token_raw, str):
        ot = other_token_raw.strip()
        if ot and ot != self_token:
            other_token = ot

    short_name = _normalize_display_name(member.get('short_name'))

    if other_token:
        # App-User: other_id ist das Token selbst. display_name optional als Cache.
        return (other_token, other_token, short_name)

    # Kein App-Token → anon-Hash. Ohne shortname können wir nichts indizieren.
    if not short_name:
        return (None, None, None)
    anon_id = _hash_anon_id(self_token, short_name)
    return (anon_id, None, short_name)


# ─── Disk-Fallback ─────────────────────────────────────────────

def _disk_path(token):
    safe = _safe_token_fragment(token)
    if not safe:
        return None
    os.makedirs(_USER_HISTORY_DIR, exist_ok=True)
    return os.path.join(_USER_HISTORY_DIR, f'crew_edges_{safe}.json')


def _disk_load(token):
    """Returns dict {other_id -> edge-row} oder {}."""
    p = _disk_path(token)
    if not p:
        return {}
    try:
        with open(p) as f:
            data = json.load(f) or {}
            if isinstance(data, dict):
                return data
            return {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _disk_save(token, edges_dict):
    p = _disk_path(token)
    if not p:
        return False
    try:
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(edges_dict, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


# ─── Supabase IO ───────────────────────────────────────────────

_SB_KNOWN_COLS = {
    'self_token', 'other_id', 'other_token', 'other_display_name',
    'other_position', 'tour_count', 'last_flown_date',
    'shared_layovers', 'shared_routes', 'created_at', 'updated_at',
}


def _sb_select_edges(token, limit=None, order_desc=True):
    """List edges for self_token sortiert nach tour_count desc. None bei SB-down."""
    sb, ok = _sb_client()
    if not ok or not token:
        return None
    try:
        q = sb.table('crew_edges').select('*').eq('self_token', token)
        if order_desc:
            q = q.order('tour_count', desc=True)
        if limit:
            q = q.limit(int(limit))
        r = q.execute()
        return r.data or []
    except Exception as e:
        current_app.logger.warning(
            f'[crew-graph] sb_select_fail tok={token[:8]} err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


def _sb_select_one(self_token, other_id):
    """Holt genau eine Edge. None bei SB-down oder not-found."""
    sb, ok = _sb_client()
    if not ok or not self_token or not other_id:
        return None
    try:
        r = (sb.table('crew_edges').select('*')
             .eq('self_token', self_token)
             .eq('other_id', other_id)
             .limit(1).execute())
        rows = r.data or []
        return rows[0] if rows else None
    except Exception as e:
        current_app.logger.warning(
            f'[crew-graph] sb_select_one_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


def _sb_upsert_increment(self_token, other_id, other_token, display_name,
                         position, tour_date, new_layovers, new_routes):
    """Versucht atomare RPC `crew_edges_upsert_increment`. Fallback: SELECT +
    Upsert unter Token-Lock (race-arm aber nicht atomar zwischen Pods).
    Returns (ok: bool, used_rpc: bool)."""
    sb, ok = _sb_client()
    if not ok:
        return (False, False)

    # 1) RPC versuchen (atomar in der DB).
    try:
        sb.rpc('crew_edges_upsert_increment', {
            'p_self_token': self_token,
            'p_other_id': other_id,
            'p_other_token': other_token,
            'p_other_display_name': display_name,
            'p_other_position': position or '',
            'p_tour_date': tour_date,
            'p_new_layovers': new_layovers or [],
            'p_new_routes': new_routes or [],
        }).execute()
        return (True, True)
    except Exception as e:
        current_app.logger.info(
            f'[crew-graph] rpc_unavailable_fallback err={type(e).__name__}: {str(e)[:120]}'
        )

    # 2) SELECT-then-UPSERT-Fallback mit Lock.
    lk = _lock_for(f'{self_token}::{other_id}')
    with lk:
        existing = _sb_select_one(self_token, other_id) or {}
        old_lay = existing.get('shared_layovers') or []
        old_rt = existing.get('shared_routes') or []
        merged_lay = _merge_capped(old_lay, new_layovers or [], SHARED_LAYOVERS_MAX)
        merged_rt = _merge_capped(old_rt, new_routes or [], SHARED_ROUTES_MAX)
        old_count = int(existing.get('tour_count') or 0)
        new_count = old_count + 1 if existing else 1

        # last_flown_date: max(old, new). Beide ISO yyyy-MM-dd ⇒ lex-compare ok.
        old_date = existing.get('last_flown_date')
        last_date = tour_date
        if old_date and isinstance(old_date, str) and old_date > (tour_date or ''):
            last_date = old_date

        # other_position: niemals mit leer überschreiben.
        eff_pos = position or existing.get('other_position')
        eff_token = other_token or existing.get('other_token')
        eff_name = display_name or existing.get('other_display_name')

        row = {
            'self_token': self_token,
            'other_id': other_id,
            'other_token': eff_token,
            'other_display_name': eff_name,
            'other_position': eff_pos,
            'tour_count': new_count,
            'last_flown_date': last_date,
            'shared_layovers': merged_lay,
            'shared_routes': merged_rt,
            'updated_at': datetime.utcnow().isoformat() + 'Z',
        }
        if not existing:
            row['created_at'] = row['updated_at']
        try:
            sb.table('crew_edges').upsert(row, on_conflict='self_token,other_id').execute()
            return (True, False)
        except Exception as e:
            current_app.logger.error(
                f'[crew-graph] sb_upsert_fail tok={self_token[:8]} '
                f'err={type(e).__name__}: {str(e)[:120]}'
            )
            return (False, False)


def _lazy_migrate_disk_to_sb(token):
    """Wenn SB-leer für diesen Token aber Disk-Daten vorhanden: einmalig bulk-
    upserten. Idempotent (Server-Counter wird NICHT erhöht, Disk-Counter wird
    1:1 übernommen)."""
    sb, ok = _sb_client()
    if not ok:
        return False
    disk = _disk_load(token) or {}
    if not disk:
        return False
    rows = []
    for other_id, edge in disk.items():
        if not isinstance(edge, dict):
            continue
        rows.append({
            'self_token': token,
            'other_id': other_id,
            'other_token': edge.get('other_token'),
            'other_display_name': edge.get('other_display_name'),
            'other_position': edge.get('other_position'),
            'tour_count': int(edge.get('tour_count') or 1),
            'last_flown_date': edge.get('last_flown_date'),
            'shared_layovers': edge.get('shared_layovers') or [],
            'shared_routes': edge.get('shared_routes') or [],
        })
    if not rows:
        return False
    try:
        for i in range(0, len(rows), 500):
            sb.table('crew_edges').upsert(
                rows[i:i+500], on_conflict='self_token,other_id'
            ).execute()
        current_app.logger.info(
            f'[crew-graph] lazy_migrated tok={token[:8]} n={len(rows)}'
        )
        return True
    except Exception as e:
        current_app.logger.warning(
            f'[crew-graph] lazy_migrate_fail tok={token[:8]} '
            f'err={type(e).__name__}: {str(e)[:120]}'
        )
        return False


# ─── Read-helpers (SB primary, Disk fallback) ─────────────────

def _load_edges(token, limit=None):
    """Returns list[dict] mit Edge-Rows. SB primary, Disk fallback, lazy-migrate."""
    sb_rows = _sb_select_edges(token, limit=limit)
    if sb_rows is not None:
        if not sb_rows:
            # SB leer → versuche Lazy-Migrate
            disk = _disk_load(token)
            if disk:
                if _lazy_migrate_disk_to_sb(token):
                    # Re-fetch nach Migration
                    sb_rows = _sb_select_edges(token, limit=limit) or []
                else:
                    # SB nicht migriert (z.B. RLS-Fehler) → Disk durchreichen
                    return _sorted_disk(disk, limit=limit)
        return sb_rows
    # SB down → Disk-Only
    disk = _disk_load(token)
    return _sorted_disk(disk, limit=limit)


def _sorted_disk(disk_dict, limit=None):
    if not disk_dict:
        return []
    rows = list(disk_dict.values())
    rows.sort(key=lambda r: -int(r.get('tour_count') or 0))
    if limit:
        rows = rows[:int(limit)]
    return rows


def _load_one_edge(self_token, other_id):
    """Single-edge-lookup. SB primary, Disk fallback."""
    sb_row = _sb_select_one(self_token, other_id)
    if sb_row is not None:
        return sb_row
    disk = _disk_load(self_token)
    return disk.get(other_id)


# ─── Merge-Helper ───────────────────────────────────────────────

def _merge_capped(existing, adding, limit):
    """Insertion-Order-erhaltend, dedupliziert, gecapped. Werte werden gestrippt
    und uppercased (Airport/Route-Codes sind case-insensitive)."""
    seen = set()
    result = []
    for s in (existing or []):
        if not isinstance(s, str):
            continue
        v = s.strip().upper()
        if not v or v in seen:
            continue
        seen.add(v)
        result.append(v)
    for s in (adding or []):
        if not isinstance(s, str):
            continue
        v = s.strip().upper()
        if not v or v in seen:
            continue
        seen.add(v)
        result.append(v)
        if len(result) >= limit:
            break
    return result[:limit]


# ─── Disk-Side Upsert (kompatibel zur SB-Form) ─────────────────

def _disk_upsert_increment(token, other_id, other_token, display_name,
                           position, tour_date, new_layovers, new_routes):
    lk = _lock_for(f'disk::{token}::{other_id}')
    with lk:
        d = _disk_load(token)
        existing = d.get(other_id) or {}
        old_lay = existing.get('shared_layovers') or []
        old_rt = existing.get('shared_routes') or []
        merged_lay = _merge_capped(old_lay, new_layovers or [], SHARED_LAYOVERS_MAX)
        merged_rt = _merge_capped(old_rt, new_routes or [], SHARED_ROUTES_MAX)
        old_count = int(existing.get('tour_count') or 0)
        new_count = old_count + 1 if existing else 1
        old_date = existing.get('last_flown_date')
        last_date = tour_date
        if old_date and isinstance(old_date, str) and old_date > (tour_date or ''):
            last_date = old_date
        eff_pos = position or existing.get('other_position')
        eff_token = other_token or existing.get('other_token')
        eff_name = display_name or existing.get('other_display_name')

        now_iso = datetime.utcnow().isoformat() + 'Z'
        d[other_id] = {
            'self_token': token,
            'other_id': other_id,
            'other_token': eff_token,
            'other_display_name': eff_name,
            'other_position': eff_pos,
            'tour_count': new_count,
            'last_flown_date': last_date,
            'shared_layovers': merged_lay,
            'shared_routes': merged_rt,
            'created_at': existing.get('created_at') or now_iso,
            'updated_at': now_iso,
        }
        return _disk_save(token, d)


# ════════════════════════════════════════════════════════════════
#   Derived crew-graph: Same-Flight-Overlap → crew_edges
# ────────────────────────────────────────────────────────────────
#   REALITÄT (2026-07): LH-myTime-iCal-Roster listen KEINE Mitflieger.
#   Es gibt also KEINE echte "wer-war-als-Crew-mit-mir"-Quelle aus dem
#   Roster-Import — der /ingest-Endpoint (crew_list) hätte nie einen
#   Speiser bekommen, darum blieb crew_edges leer (0 Rows in prod).
#
#   Ableitbare Quelle, die die Daten DOCH hergeben: zwei AeroX-User mit
#   DERSELBEN Flugnummer am DEMSELBEN Datum sind denselben Flug geflogen.
#   Die Cross-User-Tabelle crew_flight_assignments (gespeist beim Roster-
#   Snapshot) hat genau diesen Index. Wir aggregieren daraus symmetrische
#   crew_edges. Semantik ehrlich: "denselben Flug geflogen", NICHT
#   "als Crew zusammen eingeteilt".
#
#   Idempotenz: tour_count wird auf den ABSOLUTEN Wert (# gemeinsamer Flüge)
#   GESETZT — nicht inkrementiert. Mehrfaches Re-Ingest desselben Rosters
#   (BackgroundSync pusht regelmäßig) ändert die Kante nicht. Deterministisch:
#   gleicher Snapshot-Stand → gleiche Kanten. Self-direction only — jeder User
#   baut beim EIGENEN Push seinen eigenen Graph (kein Schreiben in fremde Rows).
# ════════════════════════════════════════════════════════════════

def _upsert_edge_absolute(self_token, other_id, other_token, display_name,
                          position, tour_count, last_flown_date, layovers, routes):
    """Idempotenter SET-Upsert einer Edge (kein +1-Increment). Schreibt SB
    (primary) + Disk-Mirror (best-effort). Returns True bei mind. einem Write."""
    now_iso = datetime.utcnow().isoformat() + 'Z'
    row = {
        'self_token': self_token,
        'other_id': other_id,
        'other_token': other_token,
        'other_display_name': display_name,
        'other_position': position or '',
        'tour_count': int(tour_count),
        'last_flown_date': last_flown_date,
        'shared_layovers': layovers or [],
        'shared_routes': routes or [],
        'updated_at': now_iso,
    }
    wrote = False
    sb, ok = _sb_client()
    if ok:
        try:
            sb.table('crew_edges').upsert(
                row, on_conflict='self_token,other_id'
            ).execute()
            wrote = True
        except Exception as e:
            current_app.logger.warning(
                f'[crew-graph] overlap_upsert_fail tok={self_token[:8]} '
                f'err={type(e).__name__}: {str(e)[:120]}'
            )
    # Disk-Mirror (SET, nicht increment) — hält same-worker-Reads konsistent.
    try:
        lk = _lock_for(f'disk::{self_token}::{other_id}')
        with lk:
            d = _disk_load(self_token)
            prev = d.get(other_id) or {}
            drow = dict(row)
            drow['created_at'] = prev.get('created_at') or now_iso
            d[other_id] = drow
            if _disk_save(self_token, d):
                wrote = True
    except Exception:
        pass
    return wrote


def rebuild_overlap_edges(self_token, tage):
    """Leitet crew_edges für `self_token` aus Same-Flight-Overlap ab.

    tage: roster-snapshot tage_detail (jeder Tag mit reader_facts.flight_numbers
    + optional reader_facts.layover_ort + routing). Best-effort, wirft NIE.
    Returns Anzahl geschriebener Kanten (0 wenn keine Co-Flieger / SB-down).
    """
    try:
        safe = _safe_token_fragment(self_token)
        if not safe or not tage:
            return 0
        sb, ok = _sb_client()
        if not ok:
            # Cross-User-Overlap braucht die SB-Tabelle crew_flight_assignments —
            # ohne SB gibt es keine fremden Roster zum Matchen.
            return 0

        # 1) Eigene (flugnr, datum) + Layover/Route-Kontext einsammeln.
        my_flights = {}   # (fn, date) -> {'layover': str|None, 'route': str|None}
        for t in tage:
            if not isinstance(t, dict):
                continue
            datum = (t.get('datum') or '')[:10]
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', datum):
                continue
            rf = t.get('reader_facts') or {}
            # Nightstop = letzte Tages-Ankunft (geteilte Ableitung mit
            # friends-today/Family); reines reader_facts.layover_ort trug bei
            # Multi-Leg-Turnaround-Tagen einen Vortags-/Kontext-Wert. Fallback
            # auf das rohe Feld, falls app.py (noch) nicht bindbar ist.
            try:
                from app import _feed_nightstop_ort as _ns
                lay = (_ns(t) or '').upper().strip() or None
            except Exception:
                lay = (rf.get('layover_ort') or '').upper().strip() or None
            rt = t.get('routing')
            rt = rt.strip().upper() if isinstance(rt, str) and rt.strip() else None
            for fn in (rf.get('flight_numbers') or []):
                fn_norm = re.sub(r'[^A-Za-z0-9]', '', str(fn or '').upper())[:12]
                if not fn_norm:
                    continue
                my_flights[(fn_norm, datum)] = {'layover': lay, 'route': rt or fn_norm}
        if not my_flights:
            return 0

        my_fns = sorted({fn for (fn, _d) in my_flights})

        # 2) Co-Flieger aus crew_flight_assignments (Index auf flight_number),
        #    gechunkt. Nur opt_in. Match strikt auf (flight_number, flight_date)
        #    gegen die eigenen Flüge.
        others = {}   # other_token -> aggregate
        for i in range(0, len(my_fns), 100):
            chunk = my_fns[i:i + 100]
            try:
                r = (sb.table('crew_flight_assignments')
                     .select('self_token,flight_number,flight_date,'
                             'display_name,base,position,opt_in')
                     .in_('flight_number', chunk)
                     .eq('opt_in', True)
                     .execute())
            except Exception as e:
                current_app.logger.warning(
                    f'[crew-graph] overlap_query_fail tok={safe[:8]} '
                    f'err={type(e).__name__}: {str(e)[:120]}'
                )
                return 0
            for row in (r.data or []):
                ot = row.get('self_token')
                if not ot or ot == self_token:
                    continue
                fn = row.get('flight_number')
                fd = (row.get('flight_date') or '')[:10]
                ctx = my_flights.get((fn, fd))
                if not ctx:
                    continue   # nicht derselbe Flug am selben Tag
                agg = others.get(ot)
                if agg is None:
                    agg = {'count': 0, 'layovers': set(), 'routes': set(),
                           'last': None, 'name': row.get('display_name'),
                           'pos': row.get('position')}
                    others[ot] = agg
                agg['count'] += 1
                if ctx['layover']:
                    agg['layovers'].add(ctx['layover'])
                if ctx['route']:
                    agg['routes'].add(ctx['route'])
                if agg['last'] is None or (fd and fd > agg['last']):
                    agg['last'] = fd
                if not agg['name'] and row.get('display_name'):
                    agg['name'] = row.get('display_name')
                if not agg['pos'] and row.get('position'):
                    agg['pos'] = row.get('position')

        if not others:
            return 0

        written = 0
        for ot, agg in others.items():
            display = _normalize_display_name(agg['name'])
            pos = _normalize_position(agg['pos'])
            ok_w = _upsert_edge_absolute(
                self_token=safe,
                other_id=ot,
                other_token=ot,
                display_name=display,
                position=pos,
                tour_count=agg['count'],
                last_flown_date=agg['last'],
                layovers=sorted(agg['layovers'])[:SHARED_LAYOVERS_MAX],
                routes=sorted(agg['routes'])[:SHARED_ROUTES_MAX],
            )
            if ok_w:
                written += 1

        current_app.logger.info(
            f'[crew-graph] overlap_rebuild tok={safe[:8]} '
            f'flights={len(my_flights)} peers={len(others)} written={written}'
        )
        return written
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[crew-graph] overlap_rebuild_fail err={type(e).__name__}: {str(e)[:120]}'
            )
        except Exception:
            pass
        return 0


# ════════════════════════════════════════════════════════════════
#                          E N D P O I N T S
# ════════════════════════════════════════════════════════════════

@crew_graph_bp.route('/api/crew-graph/<token>/ingest', methods=['POST'])
def crew_graph_ingest(token):
    """Upsert Edges für eine Tour.

    Body:
        {
          "tour_id": "<opaque>",          # informativ, nicht gespeichert
          "date": "YYYY-MM-DD",
          "routing": ["FRA-SIN", "SIN-FRA"],   # optional
          "layovers": ["SIN"],                 # optional
          "crew_list": [
            {"token": "<other-app-token>", "short_name": "Anna K.", "position": "FA"},
            {"short_name": "Schumann M.",  "position": "Purser"}   # nicht-App-User
          ]
        }

    Antwort 200:
        {"ok": true, "ingested": N, "skipped": M, "edges": [...]}
    """
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400

    body = request.get_json(silent=True) or {}
    tour_id = (body.get('tour_id') or '').strip() or None
    tour_date = (body.get('date') or '').strip()
    routing = body.get('routing') or []
    layovers = body.get('layovers') or []
    crew_list = body.get('crew_list') or []

    if not isinstance(crew_list, list) or not crew_list:
        return jsonify({'ok': False, 'error': 'crew_list fehlt oder ist leer.'}), 400
    if not isinstance(routing, list):
        routing = []
    if not isinstance(layovers, list):
        layovers = []
    if not tour_date or not re.match(r'^\d{4}-\d{2}-\d{2}$', tour_date):
        return jsonify({'ok': False, 'error': 'date muss im Format YYYY-MM-DD sein.'}), 400

    if len(crew_list) > INGEST_MAX_CREW:
        current_app.logger.warning(
            f'[crew-graph] ingest_capped tok={safe_tok[:8]} got={len(crew_list)}'
        )
        crew_list = crew_list[:INGEST_MAX_CREW]

    sb, sb_on = _sb_client()
    ingested = 0
    skipped = 0
    result_edges = []

    for m in crew_list:
        if not isinstance(m, dict):
            skipped += 1
            continue
        other_id, other_token, display_name = _resolve_other_id(safe_tok, m)
        if not other_id:
            skipped += 1
            continue
        position = _normalize_position(m.get('position'))

        # Schreibe zu SB (primary) UND Disk (best-effort mirror).
        sb_ok = False
        if sb_on:
            sb_ok, _used_rpc = _sb_upsert_increment(
                safe_tok, other_id, other_token, display_name, position,
                tour_date, layovers, routing,
            )
        disk_ok = _disk_upsert_increment(
            safe_tok, other_id, other_token, display_name, position,
            tour_date, layovers, routing,
        )

        if sb_ok or disk_ok:
            ingested += 1
            # Read-after-Write nur lokal (Disk) — vermeidet zusätzlichen SB-Roundtrip.
            d = _disk_load(safe_tok)
            edge = d.get(other_id)
            if edge:
                # SECURITY: niemals die rohe Disk-Row (enthält other_token)
                # zurückgeben — nur die client-sichere Serialisierung.
                result_edges.append(_serialize_edge(edge))
        else:
            skipped += 1
            current_app.logger.warning(
                f'[crew-graph] ingest_skip_persist tok={safe_tok[:8]} oid={other_id[:8]}'
            )

    current_app.logger.info(
        f'[crew-graph] ingest tok={safe_tok[:8]} tour={tour_id or "?"} '
        f'date={tour_date} n_in={len(crew_list)} ok={ingested} skip={skipped}'
    )
    return jsonify({
        'ok': True,
        'ingested': ingested,
        'skipped': skipped,
        'edges': result_edges,
    })


@crew_graph_bp.route('/api/crew-graph/<token>/match', methods=['GET'])
def crew_graph_match(token):
    """Match die Crew eines konkreten Datums gegen den Graph.

    Datenquelle für "today_crew": Der Aufrufer (iOS) bestimmt die Crew-Liste
    typischerweise selbst (CAS-View hat sie eh in der Hand). Für serverseitige
    Bequemlichkeit ziehen wir hilfsweise den `roster_snapshot_<token>.json`
    aus _USER_HISTORY_DIR — wenn vorhanden — und filtern auf das gewünschte
    Datum. Wenn nichts gefunden wird, kommt today_crew leer zurück — der iOS-
    Client soll dann seinen eigenen CAS-State durchgeben und die Match-Logik
    lokal ausführen (CrewGraphQueryEngine.match(...)).

    Query:
        date=YYYY-MM-DD

    Antwort 200:
        {
          "ok": true,
          "date": "...",
          "today_crew": [
            {"other_id": "<opaque>", "short_name": "...",
             "position": "...", "tour_count": N, "last_flown_date": "...",
             "shared_layovers": [...], "shared_routes": [...],
             "strength": "occasional"}
          ],
          "suggested_match_count": N
        }
    """
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400

    date_q = (request.args.get('date') or '').strip()
    if not date_q or not re.match(r'^\d{4}-\d{2}-\d{2}$', date_q):
        return jsonify({'ok': False, 'error': 'date-Parameter muss YYYY-MM-DD sein.'}), 400

    # 1) Versuche today_crew aus roster_snapshot zu lesen (best-effort).
    today_crew_members = _resolve_today_crew_from_snapshot(safe_tok, date_q)

    # 2) Hole vollständigen Edge-Index für diesen Self-Token einmal — match-Lookup
    #    läuft dann in-memory (kein N+1).
    edges = _load_edges(safe_tok, limit=None) or []
    by_id = {}
    by_token = {}
    for e in edges:
        oid = e.get('other_id')
        if oid:
            by_id[oid] = e
        ot = e.get('other_token')
        if ot:
            by_token[ot] = e

    today_crew_out = []
    matched = 0
    for m in today_crew_members:
        other_id, other_token, display_name = _resolve_other_id(safe_tok, m)
        if not other_id:
            continue
        edge = by_id.get(other_id) or (by_token.get(other_token) if other_token else None)
        if edge:
            matched += 1
            # short_name/position aus dem heutigen Member nachziehen wenn die Edge
            # leer ist — Serializer baut den Rest (inkl. opakem other_id, ohne Token).
            row = dict(edge)
            if not row.get('other_display_name'):
                row['other_display_name'] = display_name
            if not row.get('other_position'):
                row['other_position'] = _normalize_position(m.get('position'))
            today_crew_out.append(_serialize_edge(row))
        else:
            today_crew_out.append(_serialize_edge({
                'other_id': other_id,
                'other_token': other_token,
                'other_display_name': display_name,
                'other_position': _normalize_position(m.get('position')),
                'tour_count': 0,
                'last_flown_date': None,
                'shared_layovers': [],
                'shared_routes': [],
            }))

    return jsonify({
        'ok': True,
        'date': date_q,
        'today_crew': today_crew_out,
        'suggested_match_count': matched,
        'snapshot_used': bool(today_crew_members),
    })


@crew_graph_bp.route('/api/crew-graph/<token>/edges', methods=['GET'])
def crew_graph_edges(token):
    """Top-N stärkste Connections (tour_count desc).

    Query:
        limit=<int>   default 50, hard-cap 200
        offset=<int>  default 0
    """
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400

    try:
        limit = int(request.args.get('limit', EDGES_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = EDGES_DEFAULT_LIMIT
    try:
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(EDGES_MAX_LIMIT, limit))
    offset = max(0, offset)

    # Wir laden bewusst limit+offset zusammen — Supabase-Client unterstützt
    # `.range(offset, offset+limit-1)`, aber via _load_edges holen wir
    # nur die Top-(offset+limit) und slicen danach. Bei 200er-Cap akzeptabel.
    rows_all = _load_edges(safe_tok, limit=offset + limit) or []
    paged = rows_all[offset:offset + limit]

    out = [_serialize_edge(r) for r in paged]
    return jsonify({
        'ok': True,
        'edges': out,
        'count': len(out),
        'limit': limit,
        'offset': offset,
        'has_more': len(rows_all) > offset + limit,
    })


@crew_graph_bp.route('/api/crew-graph/<token>/common', methods=['GET'])
def crew_graph_common(token):
    """Shared History mit einem konkreten Other.

    Query:
        other=<other_token_OR_other_id>

    Antwort:
        {
          "ok": true,
          "edge": { ... shared_layovers, shared_routes, tour_count, last_flown_date ... },
          "mutuals": [ ... heuristisch: gemeinsame Layover-Schnittmenge ... ]
        }

    Mutual-Friends (App-User-Schnittmenge) wird zusätzlich berechnet wenn der
    andere ein App-User ist und seine eigenen Friends in user_friends bekannt
    sind — Schnittmenge mit den eigenen Friends.
    """
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400

    other_param = (request.args.get('other') or '').strip()
    if not other_param:
        return jsonify({'ok': False, 'error': 'other-Parameter fehlt.'}), 400

    # `other` kann entweder ein opakes App-Token sein ODER ein anon-Hash (other_id).
    # Wir versuchen zuerst direkten other_id-Match, fallback auf other_token-Lookup.
    edge = _load_one_edge(safe_tok, other_param)
    if not edge:
        # vielleicht ist `other_param` ein App-Token → other_id im Schema = Token
        # bei App-Usern, also ist das identisch und das obige hat schon getroffen.
        # Wenn nicht: einmal die ganze Liste durchsuchen.
        all_edges = _load_edges(safe_tok, limit=None) or []
        for e in all_edges:
            if e.get('other_token') == other_param or e.get('other_id') == other_param:
                edge = e
                break

    if not edge:
        return jsonify({
            'ok': True,
            'edge': None,
            'mutuals': [],
            'mutual_friends': [],
            'message': 'Keine gemeinsame Flughistorie gefunden.',
        })

    # Heuristik-Mutuals: gleiche Layover-Schnittmenge.
    anchor_layovers = set(edge.get('shared_layovers') or [])
    anchor_other_token = edge.get('other_token')
    anchor_other_id = edge.get('other_id')

    mutuals = []
    if anchor_layovers:
        all_edges = _load_edges(safe_tok, limit=None) or []
        for e in all_edges:
            if e.get('other_id') == anchor_other_id:
                continue
            inter = anchor_layovers & set(e.get('shared_layovers') or [])
            if inter:
                mutuals.append(_serialize_edge(e, extra={
                    'shared_layovers_with_anchor': sorted(inter),
                }))
        # Sortiere nach Anzahl Schnittmenge (mehr = besser), dann tour_count.
        mutuals.sort(key=lambda m: (-len(m['shared_layovers_with_anchor']),
                                    -int(m.get('tour_count') or 0)))
        mutuals = mutuals[:6]

    # Mutual-Friends (echte App-User-Schnittmenge), nur möglich wenn der Other
    # ein App-User ist (other_token gesetzt).
    mutual_friends = []
    if anchor_other_token:
        mutual_friends = _compute_mutual_friends(safe_tok, anchor_other_token)

    return jsonify({
        'ok': True,
        'edge': _serialize_edge(edge),
        'mutuals': mutuals,
        'mutual_friends': mutual_friends,
    })


# ─── Helper: today_crew aus roster_snapshot lesen ───────────────

def _resolve_today_crew_from_snapshot(self_token, date_q):
    """Best-effort: liest roster_snapshot_<token>.json und extrahiert die
    Crew-Member-Liste für `date_q` falls vorhanden. Returns [] bei Miss —
    der iOS-Client soll dann seinen lokalen CAS-State durchgeben.
    """
    safe = _safe_token_fragment(self_token)
    if not safe:
        return []
    path = os.path.join(_USER_HISTORY_DIR, f'roster_snapshot_{safe}.json')
    try:
        with open(path) as f:
            snap = json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    tage = snap.get('tage') or []
    out = []
    for t in tage:
        if not isinstance(t, dict):
            continue
        if t.get('datum') != date_q:
            continue
        rf = t.get('reader_facts') or {}
        # Crew-Liste kann unter mehreren Schlüsseln liegen — wir prüfen die
        # häufigsten Varianten.
        crew = (rf.get('crew') or rf.get('crew_list')
                or t.get('crew') or t.get('crew_list') or [])
        if not isinstance(crew, list):
            continue
        for c in crew:
            if not isinstance(c, dict):
                continue
            short = (c.get('short_name') or c.get('shortName')
                     or c.get('name') or '')
            pos = c.get('position') or c.get('pos')
            tok = c.get('token')
            if not short and not tok:
                continue
            out.append({
                'token': tok,
                'short_name': short,
                'position': pos,
            })
    return out


# ─── Helper: Mutual-Friends (App-User-Schnittmenge) ─────────────

def _compute_mutual_friends(self_token, other_token):
    """Schnittmenge der akzeptierten Friends beider App-User. Liest user_friends
    aus SB direkt — keine Disk-Variante, weil bei SB-down die Berechnung eh
    unzuverlässig wäre.

    SECURITY (P6a): `friend_token` ist das ROHE Bearer-Credential eines dritten
    App-Users. Es darf NIE an den Client. Wir liefern opake `_opaque_peer_id`-
    Hashes der Schnittmenge zurück (stabil korrelierbar, nicht zurückführbar)."""
    sb, ok = _sb_client()
    if not ok:
        return []
    try:
        r1 = (sb.table('user_friends').select('friend_token,status')
              .eq('owner_token', self_token).eq('status', 'accepted').execute())
        r2 = (sb.table('user_friends').select('friend_token,status')
              .eq('owner_token', other_token).eq('status', 'accepted').execute())
        my_friends = {row.get('friend_token') for row in (r1.data or [])
                      if row.get('friend_token')}
        their_friends = {row.get('friend_token') for row in (r2.data or [])
                         if row.get('friend_token')}
        inter = my_friends & their_friends
        # Self und Other entfernen — können nicht "mit sich selbst befreundet" sein.
        inter.discard(self_token)
        inter.discard(other_token)
        # Rohe Friend-Tokens NIE ausliefern → opake Peer-IDs.
        return sorted(_opaque_peer_id(ft, ft) for ft in inter)
    except Exception as e:
        current_app.logger.warning(
            f'[crew-graph] mutual_friends_fail tok={self_token[:8]}/'
            f'{other_token[:8]} err={type(e).__name__}: {str(e)[:120]}'
        )
        return []


# ─── Strength-Klassifikation (spiegelt CrewConnectionStrength iOS) ─

def _classify_strength(tour_count):
    if tour_count < 2:
        return 'firstFlight'
    if tour_count < 5:
        return 'occasional'
    if tour_count < 10:
        return 'familiar'
    if tour_count < 20:
        return 'regular'
    return 'core'
