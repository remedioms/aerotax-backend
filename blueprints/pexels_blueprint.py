"""
Pexels-Proxy — hält den API-Key server-seitig.

Vorher lag der Pexels-Key HARDCODED im iOS-Binary (DestinationPhoto.swift,
NewsHeadlinePhoto.swift) → aus der App extrahierbar, Quota-Missbrauch möglich.
Dieser Proxy ruft Pexels mit dem Key aus der Env-Var `PEXELS_API_KEY` auf und
reicht die JSON-Antwort 1:1 durch — die iOS-App parst unverändert `photos[].src`.

  GET /api/pexels/search?query=<term>&per_page=15&orientation=landscape

Leichter Server-Cache (15 min) + ein einfacher In-Process-Rate-Limiter dämpfen
Missbrauch unseres Proxys. Ohne Key/bei Pexels-Fehler: leeres `photos`-Array
(die App zeigt dann ihren Marken-Gradient, nie eine falsche Stadt).
"""
import os
import time
import json
import threading
import urllib.parse
import urllib.request
from flask import Blueprint, request, jsonify

pexels_bp = Blueprint('pexels', __name__)

_KEY = os.environ.get('PEXELS_API_KEY', '')
_CACHE_TTL = 15 * 60
_cache = {}            # key → (expires_ts, body_dict)
_cache_lock = threading.Lock()

# Sehr einfacher Token-Bucket pro Prozess (dämpft Missbrauch, ohne legitime
# Foto-Loads zu blocken): max ~600 Pexels-Hits / 10 min über alle Clients.
_rl_lock = threading.Lock()
_rl = {'window': 0, 'count': 0}
_RL_WINDOW = 600
_RL_MAX = 600


def _rate_ok():
    now = int(time.time())
    w = now // _RL_WINDOW
    with _rl_lock:
        if _rl['window'] != w:
            _rl['window'] = w
            _rl['count'] = 0
        _rl['count'] += 1
        return _rl['count'] <= _RL_MAX


@pexels_bp.route('/api/pexels/search', methods=['GET'])
def pexels_search():
    query = (request.args.get('query') or '').strip()
    if not query:
        return jsonify({'photos': []})
    try:
        per_page = max(1, min(30, int(request.args.get('per_page', '15'))))
    except Exception:
        per_page = 15
    orientation = request.args.get('orientation', 'landscape')
    if orientation not in ('landscape', 'portrait', 'square'):
        orientation = 'landscape'

    ckey = f'{query.lower()}|{per_page}|{orientation}'
    now = time.time()
    with _cache_lock:
        hit = _cache.get(ckey)
        if hit and hit[0] > now:
            return jsonify(hit[1])

    if not _KEY:
        return jsonify({'photos': []})            # nicht konfiguriert → ehrlich leer
    if not _rate_ok():
        # Rate-Limit erreicht → ggf. abgelaufenen Cache liefern, sonst leer.
        with _cache_lock:
            if hit:
                return jsonify(hit[1])
        return jsonify({'photos': []})

    q = urllib.parse.quote(query)
    url = f'https://api.pexels.com/v1/search?query={q}&per_page={per_page}&orientation={orientation}'
    req = urllib.request.Request(url, headers={'Authorization': _KEY})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
        obj = json.loads(raw.decode('utf-8'))
    except Exception as e:
        print(f'[pexels] fetch_fail q={query[:40]}: {type(e).__name__}: {str(e)[:120]}', flush=True)
        with _cache_lock:
            if hit:
                return jsonify(hit[1])           # alter Cache besser als nichts
        return jsonify({'photos': []})

    # Nur die für die App relevanten Felder durchreichen (kleinere Payload),
    # aber im SELBEN Schema (`photos[].src.{landscape,large}`) das iOS schon parst.
    out_photos = []
    for p in (obj.get('photos') or []):
        src = p.get('src') or {}
        out_photos.append({'src': {
            'landscape': src.get('landscape'),
            'large': src.get('large'),
        }})
    body = {'photos': out_photos}
    with _cache_lock:
        _cache[ckey] = (now + _CACHE_TTL, body)
    return jsonify(body)
