"""
Pixabay-Proxy — zweite Foto-Quelle NEBEN Pexels, IDENTISCHES Response-Schema.

Kritisch: Die iOS-App parst genau `{"photos":[{"src":{"landscape":…,"large":…},
"avg_color":…}]}` (siehe pexels_blueprint.py). Dieser Proxy mappt Pixabay-`hits[]`
auf DIESELBE Shape, damit iOS beide Quellen OHNE neue Structs mergen kann. Ein
früherer Schema-Bruch mit einer anderen Quelle hat genau das kaputt gemacht.

  GET /api/pixabay/search?query=<term>&per_page=15&orientation=landscape

Pixabay-REGELN (ausdrücklich verlangt):
  • 24h-Server-Cache PFLICHT → In-Process-TTL 24h pro (query,per_page,orientation)
    + `Cache-Control: public, max-age=86400`.
  • Rate-Limit 100 Requests / 60 s → eigener leichter Limiter; 429 → leere Liste.
Timeout ~6 s, jeder Fehler → `{"photos":[]}` (nie 500) → App zeigt Marken-Gradient.

Key aus Env-Var `PIXABAY_API_KEY`. Keine Fotografen-/Attribution-Pflichtfelder
(Owner: keine Attribution). Pixabay liefert keine avg_color → `null` (iOS lädt
das Bild ohnehin fürs Scoring; ohne avg_color entfällt nur der Vorab-Filter).
"""
import os
import time
import json
import threading
import urllib.parse
import urllib.request
import urllib.error
from flask import Blueprint, request, jsonify

pixabay_bp = Blueprint('pixabay', __name__)

_KEY = os.environ.get('PIXABAY_API_KEY', '')
_CACHE_TTL = 24 * 60 * 60          # 24h — Pixabay verlangt Server-Cache ausdrücklich
_cache = {}                        # key → (expires_ts, body_dict)
_cache_lock = threading.Lock()

# Pixabay-Limit ist 100 Requests / 60 s. Da wir 24h cachen, treffen echte
# Upstream-Calls nur bei Cache-Miss ein → ein einfacher 60-s-Fensterzähler
# über alle Clients reicht, um das Limit sicher zu respektieren.
_rl_lock = threading.Lock()
_rl = {'window': 0, 'count': 0}
_RL_WINDOW = 60
_RL_MAX = 100


def _rate_ok():
    now = int(time.time())
    w = now // _RL_WINDOW
    with _rl_lock:
        if _rl['window'] != w:
            _rl['window'] = w
            _rl['count'] = 0
        _rl['count'] += 1
        return _rl['count'] <= _RL_MAX


def _with_cache_headers(body):
    resp = jsonify(body)
    # 24h-Cache-Direktive (Pixabay-Pflicht + entlastet unseren Proxy).
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@pixabay_bp.route('/api/pixabay/search', methods=['GET'])
def pixabay_search():
    query = (request.args.get('query') or '').strip()
    if not query:
        return _with_cache_headers({'photos': []})
    try:
        per_page = max(3, min(200, int(request.args.get('per_page', '15'))))
    except Exception:
        per_page = 15
    # Pixabay kennt nur horizontal/vertical/all — auf Pexels-Vokabular mappen.
    orientation = request.args.get('orientation', 'landscape')
    if orientation == 'portrait':
        pb_orient = 'vertical'
    elif orientation == 'square':
        pb_orient = 'all'
    else:
        orientation = 'landscape'
        pb_orient = 'horizontal'

    ckey = f'{query.lower()}|{per_page}|{orientation}'
    now = time.time()
    with _cache_lock:
        hit = _cache.get(ckey)
        if hit and hit[0] > now:
            return _with_cache_headers(hit[1])

    if not _KEY:
        return _with_cache_headers({'photos': []})     # nicht konfiguriert → ehrlich leer
    if not _rate_ok():
        # Rate-Limit (100/60s) erreicht → ggf. abgelaufenen Cache, sonst leer.
        with _cache_lock:
            if hit:
                return _with_cache_headers(hit[1])
        return _with_cache_headers({'photos': []})

    q = urllib.parse.quote(query)
    # KEINE feste category (leer) — passt für beliebige Städte/Skylines.
    url = (
        f'https://pixabay.com/api/?key={_KEY}&q={q}'
        f'&image_type=photo&orientation={pb_orient}'
        f'&safesearch=true&per_page={per_page}&category='
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'AeroX/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read()
        obj = json.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as e:
        if getattr(e, 'code', None) == 429:
            print('[pixabay] rate_limited_upstream 429', flush=True)
        else:
            print(f'[pixabay] http_fail q={query[:40]}: {getattr(e, "code", "?")}', flush=True)
        with _cache_lock:
            if hit:
                return _with_cache_headers(hit[1])     # alter Cache besser als nichts
        return _with_cache_headers({'photos': []})
    except Exception as e:
        print(f'[pixabay] fetch_fail q={query[:40]}: {type(e).__name__}: {str(e)[:120]}', flush=True)
        with _cache_lock:
            if hit:
                return _with_cache_headers(hit[1])
        return _with_cache_headers({'photos': []})

    # Pixabay-`hits[]` → IDENTISCHE Pexels-Shape (photos[].src.{landscape,large}).
    out_photos = []
    for h in (obj.get('hits') or []):
        # largeImageURL (max 1280px) ist unser bevorzugtes „landscape"/„large";
        # webformatURL (max 640px) als Fallback, falls largeImageURL fehlt.
        large = h.get('largeImageURL') or h.get('webformatURL')
        if not large:
            continue
        out_photos.append({
            'src': {
                'landscape': large,
                'large': large,
            },
            # Pixabay liefert keine Durchschnittsfarbe → null (kein Vorab-Filter).
            'avg_color': None,
            'id': h.get('id'),
            'source': 'pixabay',
        })
    body = {'photos': out_photos}
    with _cache_lock:
        _cache[ckey] = (now + _CACHE_TTL, body)
    return _with_cache_headers(body)
