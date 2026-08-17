"""GIF-Suche — ANBIETER-NEUTRALER Proxy (Owner-Go „GIF-Feature", 2026-08-12).

Warum ein Proxy und keine direkte Client-Anbindung:

1. **Der Key bleibt auf dem Server.** Ein API-Key im iOS-Binary ist
   extrahierbar (die Lehre steckt schon in `pexels_blueprint.py`: der Pexels-
   Key lag hardcoded in der App). `GIPHY_API_KEY` kommt ausschließlich aus der
   Server-Env und taucht nirgends im Code, in Tests oder in Logs auf.
2. **Der Anbieter ist austauschbar.** Der Client kennt NUR unsere Shape
   (`items[].{id, preview_url, gif_url, width, height}` + `attribution`).
   Tenor ist am 30.06.2026 abgeschaltet worden — genau dagegen ist das gebaut:
   ein Anbieterwechsel ist ein Backend-Deploy, kein App-Update.
3. **Ohne Key: 503 `gif_search_unavailable`.** Ehrlich statt leerer Liste —
   der Client kann den GIF-Knopf dann ausblenden, statt eine tote Suche
   anzubieten (Owner-Regel „Fehler darf nicht wie Leere aussehen").

## Der Fluss (und wo wirklich gecacht wird)

    Suche  → dieser Proxy (Query-Cache 1 h / Trending 15 min)
    Auswahl→ POST /api/gif-search/<token>/import {gif_url}
    Import → Server lädt die Datei GENAU EINMAL, prüft sie wie einen Upload
             (echtes GIF-Magic, ≤ 10 MB, ≤ 1200 px) und legt sie in UNSERE
             Medien-Ablage (dieselbe wie jedes Foto: R2 `wall/…`)
    Senden → media_url/image_url zeigt auf UNS

Ab dem Import wird der Anbieter für dieses GIF **nie wieder** gerufen — weder
beim Anzeigen im Chat, noch im Forum, noch bei den News-Kommentaren. Das ist
die wichtigste Cache-Ebene: sie kostet einmal Traffic und liefert danach aus
eigener Ablage, ohne fremden Host und ohne Tracking-Beacon beim Leser.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, request

gif_search_bp = Blueprint('gif_search', __name__)


# ── Konfiguration ───────────────────────────────────────────────────────────
# Key NUR aus der Env lesen, NIE loggen und nie in eine Antwort schreiben.
_ENV_KEY = 'GIPHY_API_KEY'
_PROVIDER = 'giphy'
_ATTRIBUTION = 'GIPHY'          # Anbieter verlangt sichtbare Namensnennung
_API_BASE = 'https://api.giphy.com/v1/gifs'
_RATING = 'pg-13'
_LANG = 'de'
_HTTP_TIMEOUT = 10

_SEARCH_TTL = 60 * 60           # 1 h
_TRENDING_TTL = 15 * 60         # 15 min
_CACHE_MAX = 500                # Deckel wie `_cache_soft_cap`-Muster
_CACHE_DROP = 200

_LIMIT_DEFAULT = 24
_LIMIT_MAX = 50

# Suchen pro Nutzer und Minute — schützt das Kontingent des Anbieters.
_SEARCH_RATE = (30, 60)
# Importe pro Nutzer und Stunde — spiegelt den Wall-Upload (20/h).
_IMPORT_RATE = (20, 3600)

# Der Import lädt eine vom CLIENT genannte URL — das ist ein SSRF-Vektor.
# Deshalb hart auf die MEDIEN-Hosts des Anbieters begrenzt (Codex 13.08.:
# `.giphy.com` erlaubte auch api.giphy.com und beliebige Ports): nur https,
# nur `media*.giphy.com`, nur Port 443/leer. Alles andere wird abgelehnt,
# bevor ein Socket aufgeht.
import re as _re_ssrf
_ALLOWED_MEDIA_HOST_RE = _re_ssrf.compile(r'^media[0-9]*\.giphy\.com$')
# Harte Abbruchgrenze beim Herunterladen (der Deckel selbst liegt bei 10 MB;
# ein Byte mehr reicht, um „zu groß" sicher zu erkennen).
_DOWNLOAD_HARD_STOP = 10 * 1024 * 1024 + 1
_GIF_MAX_BYTES = 10 * 1024 * 1024
_GIF_MAX_DIMENSION = 1200

_cache = {}                     # cache_key → (expires_ts, body_dict)
_cache_lock = threading.Lock()

# Zähler für Tests/Betrieb: wie oft ging ein echter Anbieter-Request raus?
_stats = {'provider_calls': 0, 'cache_hits': 0}


def _app_attr(name, default=None):
    """Lazy-Zugriff auf app.py (zirkulärer Import zur Ladezeit vermeiden) —
    gleiches Muster wie news_blueprint._app_attr."""
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _api_key():
    return (os.environ.get(_ENV_KEY) or '').strip()


def _authed_token():
    """(token, None) bei gültigem Bearer, sonst (None, (response, status)).

    Dieselbe Tri-State-Mechanik wie news_blueprint: Store-Ausfall ⇒ 503, nie
    401 — ein Supabase-Hickup darf keinen Client-Logout auslösen.
    """
    bearer_fn = _app_attr('_request_bearer_token')
    validate_fn = _app_attr('_validate_token')
    if not callable(bearer_fn) or not callable(validate_fn):
        return None, (jsonify({'ok': False, 'error': 'auth_unavailable'}), 503)
    bearer = bearer_fn()
    if not bearer:
        return None, (jsonify({'ok': False, 'error': 'auth_required'}), 401)
    validation = validate_fn(bearer)
    state = getattr(getattr(validation, 'state', None), 'value', None)
    if state == 'unavailable':
        fn = _app_attr('_auth_store_unavailable_response')
        if callable(fn):
            return None, fn()
        return None, (jsonify({'ok': False,
                               'error': 'auth_store_unavailable'}), 503)
    if state != 'valid':
        return None, (jsonify({'ok': False, 'error': 'unauthorized'}), 401)
    return bearer, None


def _rate_limited(token, endpoint, rate):
    fn = _app_attr('_token_rate_limited')
    if not callable(fn):
        return False
    try:
        return bool(fn(token, endpoint, rate[0], rate[1]))
    except Exception:
        return False


def _cache_get(key):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            _stats['cache_hits'] += 1
            return hit[1]
    return None


def _cache_put(key, body, ttl):
    with _cache_lock:
        _cache[key] = (time.time() + ttl, body)
        cap = _app_attr('_cache_soft_cap')
        if callable(cap):
            cap(_cache, max_items=_CACHE_MAX, drop=_CACHE_DROP)
        elif len(_cache) > _CACHE_MAX:
            for k in list(_cache.keys())[:_CACHE_DROP]:
                _cache.pop(k, None)


def cache_clear():
    """Nur für Tests/Betrieb — leert Cache und Zähler."""
    with _cache_lock:
        _cache.clear()
    _stats.update({'provider_calls': 0, 'cache_hits': 0})


def _fetch_json(url):
    """Roh-JSON vom Anbieter. None bei jedem Fehler (nie werfen)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroX/1.0'})
    try:
        _stats['provider_calls'] += 1
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        # NIE die URL loggen — sie trägt den Key im Query-String.
        print(f'[gif-search] provider_fail: {type(e).__name__}: '
              f'{str(e)[:120]}', flush=True)
        return None


def _int_or_none(v):
    try:
        n = int(str(v).strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _variant(images, names, *, require_import_limits=False):
    """Erste brauchbare GIF-Variante aus `names` als (dict, url).

    Fuer den Import muessen die von GIPHY gemeldeten Bytes und Masse bereits
    in unseren Upload-Grenzen liegen. Der Import validiert die echte Datei
    trotzdem noch einmal — Metadaten sind nur die Vorauswahl, nie Vertrauen.
    """
    for name in names:
        candidate = images.get(name) or {}
        if not isinstance(candidate, dict):
            continue
        url = (candidate.get('url') or '').split('?')[0]
        if not url or not url.lower().endswith('.gif'):
            continue
        if require_import_limits:
            size = _int_or_none(candidate.get('size'))
            width = _int_or_none(candidate.get('width'))
            height = _int_or_none(candidate.get('height'))
            if (size is None or width is None or height is None
                    or size > _GIF_MAX_BYTES
                    or max(width, height) > _GIF_MAX_DIMENSION):
                continue
        return candidate, url
    return None, ''


def _normalize(raw):
    """Anbieter-Antwort → UNSERE Shape.

    Nur Einträge mit vollständigem Datensatz kommen durch: ohne `gif_url` gäbe
    es nichts zu importieren, ohne `preview_url` nichts anzuzeigen. Lieber ein
    Treffer weniger als eine Kachel, die beim Antippen ins Leere läuft.
    """
    items = []
    for entry in (raw.get('data') or []):
        if not isinstance(entry, dict):
            continue
        images = entry.get('images') or {}
        full = images.get('original') or {}

        # Raster: die downsampled-Varianten bewegen sich, enthalten aber
        # deutlich weniger Frames/Bytes. Beim realen „hallo"-Treffer waren es
        # 170 KB statt 4,46 MB fuer fixed_width — mal 24 sichtbare Kacheln ist
        # das der Unterschied zwischen flüssigem Sheet und grauen Platzhaltern.
        _, preview_url = _variant(images, (
            'fixed_width_downsampled',
            'fixed_height_downsampled',
            'preview_gif',
            'fixed_width_small',
            'fixed_height_small',
            'fixed_width',
            'fixed_height',
            'original',
        ))

        # Auswahl/Import: Original nur, wenn es bereits sicher unter 10 MB und
        # 1200 px liegt. Sonst die qualitativ beste vollständige GIPHY-
        # Downsized-Variante nehmen. So scheitert ein sichtbarer Treffer nicht
        # erst nach dem Tap (der reale Treffer hatte 13,17 MB im Original,
        # aber 7,27 MB als downsized_large).
        chosen, gif_url = _variant(images, (
            'original',
            'downsized_large',
            'downsized_medium',
            'downsized',
            'fixed_width',
            'fixed_height',
        ), require_import_limits=True)
        if not gif_url or not preview_url:
            continue
        dimensions = full if isinstance(full, dict) else (chosen or {})
        items.append({
            'id': str(entry.get('id') or ''),
            'preview_url': preview_url,
            'gif_url': gif_url,
            'width': _int_or_none(dimensions.get('width')),
            'height': _int_or_none(dimensions.get('height')),
        })
    return {'items': items, 'attribution': _ATTRIBUTION,
            'provider': _PROVIDER}


@gif_search_bp.route('/api/gif-search/<token>', methods=['GET'])
def gif_search(token):
    """GIF-Suche (mit `q`) bzw. Trending (ohne `q`).

    Query: ?q=<suche>&limit=<n>  (limit 1..50, Default 24)
    Antwort: {ok, items:[{id, preview_url, gif_url, width, height}],
              attribution:"GIPHY", provider:"giphy", cached:bool}

    `<token>` steht im Pfad wie bei den anderen Owner-Routen; maßgeblich ist
    aber der BEARER (GETs deckt das `before_request`-Auth-Gate nicht ab).
    """
    bearer, err = _authed_token()
    if err:
        return err

    q = (request.args.get('q') or '').strip()[:80]
    limit = _int_or_none(request.args.get('limit')) or _LIMIT_DEFAULT
    limit = max(1, min(limit, _LIMIT_MAX))

    cache_key = f'{_PROVIDER}|{"s" if q else "t"}|{q.lower()}|{limit}'
    cached = _cache_get(cache_key)
    if cached is not None:
        # Cache-Treffer kostet den Anbieter NICHTS — deshalb auch kein
        # Rate-Limit-Verbrauch dafür.
        body = dict(cached)
        body['cached'] = True
        return jsonify(body)

    if not _api_key():
        # Ehrlich: der Client blendet den GIF-Knopf aus, statt eine tote Suche
        # anzubieten. KEINE leere Trefferliste vortäuschen.
        return jsonify({'ok': False, 'error': 'gif_search_unavailable'}), 503

    if _rate_limited(bearer, 'gif_search', _SEARCH_RATE):
        return jsonify({'ok': False, 'error': 'rate_limited',
                        'message': 'Zu viele Suchen. Kurz durchatmen.'}), 429

    params = {'api_key': _api_key(), 'limit': str(limit),
              'rating': _RATING, 'lang': _LANG}
    if q:
        params['q'] = q
        url = f'{_API_BASE}/search?' + urllib.parse.urlencode(params)
    else:
        url = f'{_API_BASE}/trending?' + urllib.parse.urlencode(params)

    raw = _fetch_json(url)
    if raw is None:
        return jsonify({'ok': False, 'error': 'gif_search_failed'}), 502

    body = _normalize(raw)
    body['ok'] = True
    _cache_put(cache_key, body, _SEARCH_TTL if q else _TRENDING_TTL)
    out = dict(body)
    out['cached'] = False
    return jsonify(out)


@gif_search_bp.route('/api/me/gif-search', methods=['GET'])
def me_gif_search():
    """Bearer-only alias; the mature handler already authenticates the header."""
    return gif_search('')


def _allowed_media_url(u):
    """Nur https auf die Medien-Domains des Anbieters. Alles andere ⇒ None.

    Ohne diese Klammer wäre der Import ein offener SSRF-Proxy: ein Client
    könnte den Server interne Adressen abrufen lassen.
    """
    s = str(u or '').strip()
    if not s or len(s) > 500:
        return None
    try:
        p = urllib.parse.urlsplit(s)
        # p.port wirft ValueError bei kaputtem Port (":abc", ausserhalb 0-65535)
        # — MUSS im try stehen (Codex-Zweitpass: sonst HTTP 500 statt sauberer
        # Ablehnung).
        port = p.port
    except Exception:
        return None
    if p.scheme != 'https' or not p.hostname:
        return None
    host = p.hostname.lower()
    if not _ALLOWED_MEDIA_HOST_RE.match(host):
        return None
    if port not in (None, 443):
        return None
    return s


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Der Import folgt KEINEM Redirect (Gegenprüfung 13.08.).

    `_allowed_media_url` prüft nur die START-URL. `urlopen` folgt 30x aber
    per Default auf JEDEN Host — ein Redirect von *.giphy.com auf eine
    interne Adresse (oder http://) hätte die SSRF-Allowlist ausgehebelt.
    Die echten Medien-URLs (media*.giphy.com/media/…) liefern direkt aus;
    ein 30x ist hier also nie legitim ⇒ Fehler statt folgen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _download_limited(url):
    """Lädt höchstens `_DOWNLOAD_HARD_STOP` Bytes. (bytes, None) oder
    (None, fehlercode). Folgt keinem Redirect (siehe _NoRedirectHandler)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroX/1.0'})
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=_HTTP_TIMEOUT) as r:
            data = r.read(_DOWNLOAD_HARD_STOP)
    except Exception as e:
        print(f'[gif-search] import_fetch_fail: {type(e).__name__}: '
              f'{str(e)[:120]}', flush=True)
        return None, 'gif_import_failed'
    if not data:
        return None, 'gif_import_failed'
    return data, None


@gif_search_bp.route('/api/gif-search/<token>/import', methods=['POST'])
def gif_import(token):
    """Body: {gif_url}. Holt das gewählte GIF EINMAL und legt es in UNSERE
    Ablage. Antwort: {ok, url} — dieselbe Form wie `upload_wall_image`.

    Ab hier ist der Anbieter aus dem Spiel: `url` zeigt auf unseren Serve-Pfad,
    und genau dieser Wert darf als `image_url`/`media_url` in Chat, Forum,
    Wall und News-Kommentar (alle prüfen auf eigene Pfade).

    Geprüft wird wie bei jedem Upload: echtes GIF-Magic, höchstens 10 MB und
    1200 px Kantenlänge. Ein Treffer, der darüber liegt, wird ABGELEHNT und
    nicht etwa verkleinert — eine re-kodierte Animation wäre ein Standbild.
    """
    bearer, err = _authed_token()
    if err:
        return err
    if _rate_limited(bearer, 'gif_import', _IMPORT_RATE):
        return jsonify({'ok': False, 'error': 'rate_limited',
                        'message': 'Zu viele GIFs. Bitte später erneut.'}), 429

    payload = request.get_json(silent=True) or {}
    src = _allowed_media_url(payload.get('gif_url'))
    if not src:
        return jsonify({'ok': False, 'error': 'invalid_gif_url',
                        'message': 'Diese GIF-Quelle ist nicht erlaubt.'}), 400

    data, dl_err = _download_limited(src)
    if dl_err:
        return jsonify({'ok': False, 'error': dl_err}), 502

    store = _app_attr('wall_store_media_bytes')
    if not callable(store):
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503
    # Der Speicher-Helfer validiert Magic-Bytes und Deckel — GIF-fremde oder
    # zu große Dateien fallen hier mit demselben Fehlercode wie beim Upload.
    url, store_err = store(bearer, data)
    if store_err is not None:
        return jsonify(store_err[0]), store_err[1]
    return jsonify({'ok': True, 'url': url, 'attribution': _ATTRIBUTION})


@gif_search_bp.route('/api/me/gif-search/import', methods=['POST'])
def me_gif_import():
    """Bearer-only import alias; `gif_import` authenticates the header."""
    return gif_import('')
