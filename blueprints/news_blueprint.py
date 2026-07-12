# ═══════════════════════════════════════════════════════════════
#  Aviation-News Aggregator Blueprint  (Worker P5 Backend)
#
#  Self-contained Flask Blueprint für multi-source RSS/HTML
#  Aviation-News-Aggregation mit Airline-Relevance-Filter,
#  Category-Routing und 15min in-memory cache.
#
#  Wiring in app.py:
#      from blueprints.news_blueprint import news_bp
#      app.register_blueprint(news_bp)
#
#  Endpunkte:
#      GET /api/news/feed?airline=&category=&limit=50
#      GET /api/news/sources
#
#  Pull-Strategie:
#      - ThreadPoolExecutor(max_workers=6) für parallelen Source-Pull.
#      - Pro-Source-Timeout 8s; ein Ausfall ≠ ganzer Feed kaputt.
#      - feedparser für RSS, BeautifulSoup für AvHerald-Frontpage.
#      - Dedupe via (url-hash sha256[:16]) + lowercase-title Jaccard >= 0.85.
#
#  Architektur-Hinweis:
#      Dieses Blueprint ist KEIN Datenkonsument der AeroTax-Steuer-Pipeline.
#      Es lebt isoliert, hat keine DB-Roundtrips, keine Anthropic-Calls,
#      keinen User-Context. Daher kein Auth-Required.
# ═══════════════════════════════════════════════════════════════

import hashlib
import html as html_lib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, current_app, jsonify, request

try:
    import feedparser  # type: ignore
except ImportError:  # pragma: no cover — requirements.txt sorgt für Install
    feedparser = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover — beautifulsoup4 ist bereits Pflicht-Dep
    BeautifulSoup = None  # type: ignore

news_bp = Blueprint('news', __name__)
_logger = logging.getLogger('aerotax.news')

# ── Cache ────────────────────────────────────────────────────────
# Struktur: {key: {"ts": float_unix, "payload": dict}}
# key = f"{airline}:{category}" (beide lower-case, leere Werte als '*').
# TTL bewusst 15min — News-Headlines refreshen sich nicht im Minutentakt
# und wir wollen die Upstreams (Reuters, SimpleFlying, AvHerald) nicht hämmern.
_FEED_CACHE = {}
_FEED_CACHE_TTL_SECONDS = 15 * 60
_FEED_CACHE_LOCK = threading.Lock()

# Per-Source HTTP-Timeout. feedparser nimmt das über `request_headers`+socket
# nicht direkt, deshalb fetchen wir RSS manuell via requests und reichen den
# Body in feedparser.parse(bytes) durch — so haben wir ein hartes Timeout.
_SOURCE_TIMEOUT_SECONDS = 8
_AGGREGATE_TIMEOUT_SECONDS = 12  # ThreadPool-Cap (Soft-Deadline für alle Quellen)

_USER_AGENT = 'AeroX/1.0 (news-aggregator; +https://aerosteuer.de)'

# ── Source-Definitionen ─────────────────────────────────────────
# Jede Source ist ein dict mit:
#   id            — stabiler Slug für Client-Filter
#   name          — Display-Name
#   url           — Feed/Scrape-URL
#   kind          — 'rss' | 'avherald_scrape'
#   logo_url      — kleines Brand-Icon (favicon-CDN, kein eigenes Hosting)
#   language      — 'de' | 'en' (informativ für Client-UI)
SOURCES = [
    {
        'id': 'aero_de',
        'name': 'aero.de',
        'url': 'https://feeds.aero.de/news',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=aero.de&sz=64',
        'language': 'de',
    },
    # ── Weitere deutschsprachige Luftfahrt-/Airline-Quellen ──────────
    # (User: „Mehr deutsche Airline-News-Quellen verfügbar?") Reputable
    # DE-Branchenmedien mit öffentlichem RSS. Sollte ein Feed mal 404en,
    # degradiert _pull_one_source_safe sauber (source_status=False) ohne
    # den Gesamt-Feed zu stören.
    {
        'id': 'aerotelegraph',
        'name': 'aeroTELEGRAPH',
        'url': 'https://www.aerotelegraph.com/feed',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=aerotelegraph.com&sz=64',
        'language': 'de',
    },
    {
        'id': 'aerobuzz_de',
        'name': 'AEROBUZZ',
        'url': 'https://www.aerobuzz.de/feed/',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=aerobuzz.de&sz=64',
        'language': 'de',
    },
    {
        'id': 'aviation_direct',
        'name': 'Aviation.Direct',
        'url': 'https://aviation.direct/feed/',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=aviation.direct&sz=64',
        'language': 'de',
    },
    {
        'id': 'austrian_wings',
        'name': 'Austrian Wings',
        'url': 'https://www.austrianwings.info/feed/',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=austrianwings.info&sz=64',
        'language': 'de',
    },
    {
        'id': 'reuters_aerospace',
        'name': 'Reuters Aerospace & Defense',
        'url': 'https://www.reuters.com/business/aerospace-defense/rss',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=reuters.com&sz=64',
        'language': 'en',
    },
    {
        'id': 'the_air_current',
        'name': 'The Air Current',
        'url': 'https://theaircurrent.com/feed/',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=theaircurrent.com&sz=64',
        'language': 'en',
    },
    {
        'id': 'flightradar24_squawk',
        'name': 'Flightradar24 Squawk',
        'url': 'https://www.flightradar24.com/blogs/feed/',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=flightradar24.com&sz=64',
        'language': 'en',
    },
    {
        'id': 'simple_flying',
        'name': 'Simple Flying',
        'url': 'https://simpleflying.com/feed/',
        'kind': 'rss',
        'logo_url': 'https://www.google.com/s2/favicons?domain=simpleflying.com&sz=64',
        'language': 'en',
    },
    {
        'id': 'avherald',
        'name': 'AvHerald',
        'url': 'https://avherald.com/',
        'kind': 'avherald_scrape',
        'logo_url': 'https://www.google.com/s2/favicons?domain=avherald.com&sz=64',
        'language': 'en',
    },
]

# ── Airline-Aliases ─────────────────────────────────────────────
# IATA-Code → Liste alternativer Bezeichnungen (Name, ICAO, gängige
# Schreibweisen). Match ist substring auf lowercase title+summary —
# d.h. "Lufthansa" matched genauso wie "DLH" wie "LH-Tochter".
# Achtung: 2-Buchstaben-IATA-Codes matchen nur als WHOLE-WORD (über
# Wort-Boundary-Regex), sonst gibt's false positives wie "LH" in "alle".
AIRLINE_ALIASES = {
    # Lufthansa-Group
    'LH': ['Lufthansa', 'DLH'],
    'EW': ['Eurowings', 'EWG'],
    'LX': ['Swiss International', 'Swiss Air Lines', 'SWR'],
    'OS': ['Austrian Airlines', 'AUA'],
    'SN': ['Brussels Airlines', 'BEL'],
    'EN': ['Air Dolomiti', 'DLA'],
    # IAG / British
    'BA': ['British Airways', 'BAW'],
    'IB': ['Iberia', 'IBE'],
    'AY': ['Finnair', 'FIN'],
    'EI': ['Aer Lingus', 'EIN'],
    # Air France-KLM
    'AF': ['Air France', 'AFR'],
    'KL': ['KLM', 'KLM Royal Dutch', 'KLM Royal Dutch Airlines'],
    'TO': ['Transavia France', 'TVF'],
    # US3 / US Majors
    'DL': ['Delta', 'Delta Air Lines', 'DAL'],
    'AA': ['American Airlines', 'AAL'],
    'UA': ['United Airlines', 'UAL'],
    # US Low-Cost
    'WN': ['Southwest', 'Southwest Airlines', 'SWA'],
    'B6': ['JetBlue', 'JBU'],
    'AS': ['Alaska Airlines', 'ASA'],
    'F9': ['Frontier Airlines', 'FFT'],
    'NK': ['Spirit Airlines', 'NKS'],
    'G4': ['Allegiant Air', 'AAY'],
    # Europe Low-Cost
    'U2': ['easyJet', 'EZY'],
    'FR': ['Ryanair', 'RYR'],
    'W6': ['Wizz Air', 'WZZ'],
    'VY': ['Vueling', 'VLG'],
    'DY': ['Norwegian', 'Norwegian Air Shuttle', 'NAX'],
    'PC': ['Pegasus Airlines', 'PGT'],
    # German Leisure
    'DE': ['Condor', 'CFG'],
    'X3': ['TUIfly', 'TFL'],
    # Atlantic / UK
    'VS': ['Virgin Atlantic', 'VIR'],
    # ME3
    'EK': ['Emirates', 'UAE'],
    'EY': ['Etihad', 'Etihad Airways', 'ETD'],
    'QR': ['Qatar Airways', 'QTR'],
    # Turkish
    'TK': ['Turkish Airlines', 'THY'],
    # Asia Majors
    'SQ': ['Singapore Airlines', 'SIA'],
    'CX': ['Cathay Pacific', 'CPA'],
    'NH': ['ANA', 'All Nippon Airways', 'ANA Holdings'],
    'JL': ['JAL', 'Japan Airlines'],
    'KE': ['Korean Air', 'KAL'],
    'OZ': ['Asiana', 'Asiana Airlines', 'AAR'],
    'CA': ['Air China', 'CCA'],
    'CZ': ['China Southern', 'CSN'],
    'MU': ['China Eastern', 'CES'],
    'JX': ['Starlux', 'Starlux Airlines', 'SJX'],
    'MH': ['Malaysia Airlines', 'MAS'],
    'GA': ['Garuda Indonesia', 'GIA'],
    'TG': ['Thai Airways', 'THA'],
    'BR': ['EVA Air', 'EVA'],
    'CI': ['China Airlines', 'CAL'],
    # India
    '6E': ['IndiGo', 'IGO'],
    'AI': ['Air India', 'AIC'],
    # Africa / ME extras
    'MS': ['Egyptair', 'EgyptAir', 'MSR'],
    'RJ': ['Royal Jordanian', 'RJA'],
    'ET': ['Ethiopian Airlines', 'ETH'],
    'SA': ['South African Airways', 'SAA'],
    'WB': ['RwandAir', 'RWD'],
    # Italy
    'AZ': ['ITA Airways', 'ITY', 'Alitalia'],
    # Scandinavia
    'SK': ['SAS', 'Scandinavian Airlines', 'SAS Scandinavian'],
    # Canada
    'AC': ['Air Canada', 'ACA'],
    'WS': ['WestJet', 'WJA'],
    # Latam
    'LA': ['LATAM', 'LATAM Airlines', 'LAN'],
    'AR': ['Aerolineas', 'Aerolineas Argentinas', 'ARG'],
    'AM': ['Aeromexico', 'AeroMexico', 'AMX'],
    'CM': ['Copa Airlines', 'CMP'],
    # Australia / Oceania
    'QF': ['Qantas', 'QFA'],
    'NZ': ['Air New Zealand', 'ANZ'],
    'VA': ['Virgin Australia', 'VOZ'],
    # Russia / Eastern Europe
    'SU': ['Aeroflot', 'AFL'],
    'LO': ['LOT Polish', 'LOT Polish Airlines', 'LOT'],
    'OK': ['Czech Airlines', 'CSA'],
    # Israel
    'LY': ['El Al', 'ElAl', 'ELY'],
}

# ── Category-Pattern (Regex auf title+summary, case-insensitive) ──
# Reihenfolge wichtig: erste passende Kategorie gewinnt. Safety vor allem
# anderen, damit "Boeing 737 Notlandung wegen Triebwerk" als safety klassiert
# wird, nicht als technical. labor vor industry, damit "Lufthansa-Piloten
# einigen sich auf Vertrag" als labor (nicht industry/contract) zählt.
CATEGORY_PATTERNS = [
    ('safety', re.compile(
        r'\b(crash|crashed|crashes|mayday|emergency|runway excursion|incident|'
        r'fatal|fatalities|injured|ditching|tail strike|fire on board|'
        r'depressuriz|decompress|absturz|notlandung|zwischenfall|verletzt|'
        r'evakuiert|gefahr im verzug|near miss)\b',
        re.IGNORECASE)),
    ('labor', re.compile(
        r'\b(strike|striking|walkout|union|unions|collective agreement|'
        r'pilot agreement|pilots\' contract|crew contract|industrial action|'
        r'streik|tarif|tarifvertrag|gewerkschaft|warnstreik|verdi|cockpit)\b',
        re.IGNORECASE)),
    ('regulatory', re.compile(
        r'\b(easa|faa|icao|iata regulation|bsl|baf|ntsb|bfu|directive|'
        r'aviation authority|regulator|regulators|certification|grounded by)\b',
        re.IGNORECASE)),
    ('technical', re.compile(
        r'\b(engine|engines|maintenance|airworthiness directive|\bad-\d|'
        r'a350 engine|trent xwb|leap|geared turbofan|gtf|inspection|'
        r'triebwerk|wartung|techn(?:ik|isch))\b',
        re.IGNORECASE)),
    ('industry', re.compile(
        r'\b(merger|acquisition|acquires|to acquire|new route|new routes|'
        r'launches route|delivery|delivers|first delivery|order for|orders|'
        r'aircraft order|fleet expansion|earnings|profit|loss|quarterly|'
        r'übernahme|fusion|neue strecke|auslieferung|bestellung|'
        r'quartalsergebnis)\b',
        re.IGNORECASE)),
]
_DEFAULT_CATEGORY = 'general'
_ALLOWED_CATEGORIES = {'safety', 'labor', 'regulatory', 'technical', 'industry', 'general'}


# ──────────────────────────────────────────────────────────────────
#  Public Endpoints
# ──────────────────────────────────────────────────────────────────

@news_bp.route('/api/news/feed', methods=['GET'])
def get_news_feed():
    """Aggregierter News-Feed (multi-source, dedupe, filter, sort desc).

    Query-Params:
      airline=  optional IATA-Code (LH) oder Name (Lufthansa) — BOOSTET Artikel
                die diese Airline erwähnen nach OBEN (sie kommen zuerst),
                allgemeine News bleiben danach erhalten. Case-insensitive.
                Jeder Artikel erhält `relevance` (0/1/2) und `is_own_airline`.
      category= optional safety|labor|industry|technical|regulatory|general.
                Das IST ein harter Filter.
      limit=    1..200, default 50.

    Antwort:
      { ok: true, articles: [...], count, own_airline_count, airline,
        sources_ok, sources_failed, cache_hit, generated_at }
      Jeder Artikel: + relevance:int, is_own_airline:bool, mentioned_airlines:[].
    """
    airline_raw = (request.args.get('airline') or '').strip()
    category_raw = (request.args.get('category') or '').strip().lower()
    try:
        limit = int(request.args.get('limit') or '50')
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    # ?readable_only=1 → nur Artikel, die DIREKT in der App lesbar sind (Volltext
    # oder ausreichende Zusammenfassung). Kein „Volltext nicht verfügbar / nur im
    # Browser"-Sackgassen mehr (User-Wunsch).
    readable_only = (request.args.get('readable_only') or '').strip().lower() in ('1', 'true', 'yes')

    if category_raw and category_raw not in _ALLOWED_CATEGORIES:
        return jsonify({
            'ok': False,
            'error': 'invalid_category',
            'message': f'Kategorie unbekannt — erlaubt: {sorted(_ALLOWED_CATEGORIES)}',
        }), 400

    airline_key = airline_raw.lower() or '*'
    category_key = category_raw or '*'
    cache_key = f'{airline_key}:{category_key}'

    cached = _cache_get(cache_key)
    if cached is not None:
        payload = dict(cached)
        payload['cache_hit'] = True
        # limit + readable_only im cache-hit-pfad anwenden (Cache hält die volle
        # Liste inkl. `in_app_readable`-Flag pro Artikel).
        arts = payload.get('articles', [])
        if readable_only:
            arts = [a for a in arts if a.get('in_app_readable')]
        payload['articles'] = arts[:limit]
        payload['count'] = len(payload['articles'])
        return jsonify(payload)

    started = time.time()
    aggregated, source_status = _aggregate_all_sources()

    # Per-Article Klassifikation + Airline-Tagging (vor Filter — damit der
    # Cache-Eintrag auch für andere Airline-Filter wiederverwendbar wäre,
    # falls wir später einen "all"-Cache einbauen).
    # Externe Backend-Basis EINMAL pro Request bestimmen (im Request-Context,
    # damit der Image-Proxy absolute URLs liefern kann — die Article-Konstruktion
    # selbst läuft in Worker-Threads OHNE request-Context).
    proxy_base = _news_external_base()
    enriched = []
    for art in aggregated:
        cat = _classify_category(art.get('title', ''), art.get('summary', ''))
        mentions = _extract_mentioned_airlines(art.get('title', ''), art.get('summary', ''))
        art['category'] = cat
        art['mentioned_airlines'] = mentions
        # Bild NICHT mehr direkt von der Fremdquelle hot-linken. Viele Verlage/CDNs
        # blocken Hotlinking per Referer-Check (403), lassen signierte URLs ablaufen
        # oder liefern nur über http (Mixed-Content) → in der App lädt das Bild dann
        # gar nicht. Stattdessen über den Backend-Proxy /api/news/image leiten, der
        # das Bild serverseitig zieht, kurz cached (R2 wenn verfügbar) und stabil
        # ausliefert. image_url_original bleibt als Debug/Fallback erhalten.
        orig_img = art.get('image_url')
        if orig_img and proxy_base:
            proxied = _news_proxy_url(orig_img, proxy_base)
            if proxied and proxied != orig_img:
                art['image_url'] = proxied
                art['image_url_original'] = orig_img
        enriched.append(art)

    # Persistenter Volltext-Layer (Owner: „Text soll gespeichert sein und direkt
    # voll da sein"): (a) für Artikel OHNE RSS-Volltext den gespeicherten
    # Scrape-Volltext aus Supabase anhängen (EIN chunked Roundtrip, nur auf dem
    # Cache-Miss-Pfad ≤1×/15min; Serve-TTL + Deny-Hosts, s. Copyright-
    # Leitplanken am Volltext-Layer) und (b) fehlende Volltexte im Hintergrund
    # höflich nachernten (nur NEUE Artikel, robots.txt-Gate, gedrosselt,
    # Fehler ⇒ Teaser-Fallback bleibt).
    try:
        _attach_stored_fulltexts(enriched)
    except Exception as exc:
        _log_warn(f'[news/fulltext] attach failed: {exc!r}')
    try:
        _kickoff_fulltext_harvest(enriched)
    except Exception as exc:
        _log_warn(f'[news/fulltext] harvest kickoff failed: {exc!r}')

    # Nur Category ist ein echter FILTER. Airline ist KEIN Filter mehr — sie
    # BOOSTET (airline-relevante Artikel zuerst, allgemeine News danach), damit
    # der Feed nie leer/dünn wird und der iOS-Client eine "Deine Airline"-Sektion
    # bauen kann (is_own_airline-Flag).
    filtered = _filter_articles(enriched, airline_raw='', category=category_raw)

    # Airline-Relevanz taggen (relevance-Score + is_own_airline-Bool).
    needles = _normalize_airline_input(airline_raw) if airline_raw else set()
    for art in filtered:
        rel, own = _airline_relevance(art, needles)
        art['relevance'] = rel
        art['is_own_airline'] = own

    # Sort: airline-relevant ZUERST (höchste relevance), innerhalb gleicher
    # Relevanz nach Datum desc. Ohne airline-Param: relevance überall 0 →
    # reiner Datums-Sort wie bisher.
    filtered.sort(
        key=lambda a: (a.get('relevance') or 0, a.get('published_at') or 0),
        reverse=True,
    )

    # readable_only NACH dem Cachen anwenden (der Cache unten hält die volle Liste
    # mit `in_app_readable`-Flag, damit andere Clients ohne Filter sie noch sehen).
    visible = [a for a in filtered if a.get('in_app_readable')] if readable_only else filtered

    payload = {
        'ok': True,
        'articles': visible[:limit],
        'count': min(len(visible), limit),
        'total_before_limit': len(filtered),
        # Wie viele Artikel airline-relevant sind (für "Deine Airline"-Sektion).
        'own_airline_count': sum(1 for a in filtered if a.get('is_own_airline')),
        'airline': airline_raw or None,
        'sources_ok': [s for s, ok in source_status.items() if ok],
        'sources_failed': [s for s, ok in source_status.items() if not ok],
        'cache_hit': False,
        'generated_at': int(time.time()),
        'duration_ms': int((time.time() - started) * 1000),
    }
    # Im Cache speichern wir die UNGEKAPPTE Liste, damit verschiedene
    # Limits aus demselben Cache-Hit bedient werden.
    cache_payload = dict(payload)
    cache_payload['articles'] = filtered
    cache_payload['count'] = len(filtered)
    _cache_set(cache_key, cache_payload)

    try:
        current_app.logger.info(
            '[news/feed] airline=%s category=%s articles=%d failed_sources=%s dur=%dms',
            airline_raw or '-', category_raw or '-', len(filtered),
            payload['sources_failed'], payload['duration_ms'],
        )
    except Exception:
        pass

    return jsonify(payload)


@news_bp.route('/api/news/sources', methods=['GET'])
def get_news_sources():
    """Liste der unterstützten News-Quellen mit Metadaten.

    Antwort:
      { ok: true, sources: [{id, name, url, kind, logo_url, language}, ...] }
    """
    return jsonify({
        'ok': True,
        'count': len(SOURCES),
        'sources': [
            {k: s[k] for k in ('id', 'name', 'url', 'kind', 'logo_url', 'language')}
            for s in SOURCES
        ],
    })


# ──────────────────────────────────────────────────────────────────
#  Image-Proxy  (verhindert kaputte Hotlinks im News-Feed)
#
#  Warum: image_url im Feed zeigte bisher DIREKT auf die Fremdquelle
#  (og:image / media:content / <img src>). Viele Verlage blocken
#  Hotlinking per Referer-Check (403), nutzen ablaufende signierte CDN-
#  URLs oder liefern nur http → in der iOS-App lädt das Bild nicht.
#
#  /api/news/image?u=<absolute-url> zieht das Bild serverseitig (mit
#  Browser-UA + Referer auf die Bild-Origin), validiert content-type,
#  cached es (R2 wenn verfügbar, sonst kurzlebiger In-Memory-Cache) und
#  streamt die Bytes mit langlebigem Cache-Header zurück. Schlägt der
#  Remote-Fetch fehl, 302-Redirect auf die Originalquelle (best effort).
# ──────────────────────────────────────────────────────────────────

_NEWS_IMG_FETCH_TIMEOUT = 6          # Sekunden pro Remote-Image-Fetch
_NEWS_IMG_MAX_BYTES = 6 * 1024 * 1024  # 6 MB Hard-Cap pro Bild
_NEWS_IMG_MEM_TTL = 6 * 3600         # In-Memory-Cache-TTL (R2-loser Fallback)
_NEWS_IMG_MEM_MAX = 150              # max. Einträge im In-Memory-Cache
_NEWS_IMG_MEM_CACHE = {}             # key -> {"ts": float, "ct": str, "data": bytes}
_NEWS_IMG_MEM_LOCK = threading.Lock()

# Browser-naher UA — viele Bild-CDNs liefern nichts an „Bot"-UAs aus.
_NEWS_IMG_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/17.0 Safari/605.1.15'
)


def _news_external_base():
    """Absolute Backend-Basis-URL (Schema+Host, ohne Trailing-Slash) aus dem
    aktuellen Request. '' wenn kein Request-Context — dann bleibt image_url die
    Originalquelle (degradiert sauber statt einer kaputten relativen URL)."""
    try:
        # request.host_url respektiert auf Cloud Run die X-Forwarded-Proto/Host
        # via Werkzeug-Proxy bereits; falls nicht, korrigieren wir das Schema.
        base = (request.host_url or '').rstrip('/')
        if not base:
            return ''
        proto = (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip()
        if proto in ('http', 'https') and base.startswith(('http://', 'https://')):
            rest = base.split('://', 1)[1]
            base = f'{proto}://{rest}'
        return base
    except Exception:
        return ''


def _news_proxy_url(remote_url, base):
    """Baut die absolute Proxy-URL für ein Remote-Bild. Gibt remote_url
    unverändert zurück, wenn es kein http(s)-Link ist (z.B. data:-URI)."""
    if not remote_url or not isinstance(remote_url, str):
        return remote_url
    low = remote_url.strip().lower()
    if not low.startswith(('http://', 'https://')):
        return remote_url
    if not base:
        return remote_url
    q = urllib.parse.quote(remote_url.strip(), safe='')
    return f'{base}/api/news/image?u={q}'


def _news_host_is_safe(host):
    """SSRF-Schutz: löst den Host auf und lehnt private/loopback/link-local/
    reservierte/metadata-Adressen ab. False bei Auflösungsfehler."""
    if not host:
        return False
    try:
        import socket
        import ipaddress
    except Exception:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = info[4][0]
            addr = ipaddress.ip_address(ip)
        except Exception:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _news_img_mem_get(key):
    with _NEWS_IMG_MEM_LOCK:
        entry = _NEWS_IMG_MEM_CACHE.get(key)
        if not entry:
            return None
        if (time.time() - entry['ts']) > _NEWS_IMG_MEM_TTL:
            _NEWS_IMG_MEM_CACHE.pop(key, None)
            return None
        return entry['ct'], entry['data']


def _news_img_mem_set(key, content_type, data):
    with _NEWS_IMG_MEM_LOCK:
        _NEWS_IMG_MEM_CACHE[key] = {'ts': time.time(), 'ct': content_type, 'data': data}
        if len(_NEWS_IMG_MEM_CACHE) > _NEWS_IMG_MEM_MAX:
            oldest = sorted(_NEWS_IMG_MEM_CACHE.items(), key=lambda kv: kv[1]['ts'])
            for k, _ in oldest[:len(_NEWS_IMG_MEM_CACHE) - _NEWS_IMG_MEM_MAX]:
                _NEWS_IMG_MEM_CACHE.pop(k, None)


def _news_fetch_remote_image(url):
    """Zieht ein Remote-Bild. (bytes, content_type) oder (None, None).
    Erzwingt content-type=image/*, hartes Größen-Cap und kurzes Timeout.

    SSRF-Härtung (Audit 2026-07-01): Redirects werden MANUELL verfolgt
    (max. 3 Hops) und JEDER Hop erneut gegen _news_host_is_safe geprüft —
    vorher konnte ein externer 302 auf eine interne/Metadata-Adresse
    umleiten (allow_redirects=True validierte nur den ersten Host)."""
    _MAX_REDIRECT_HOPS = 3
    current_url = url
    resp = None
    for _hop in range(_MAX_REDIRECT_HOPS + 1):
        try:
            parsed = urllib.parse.urlsplit(current_url)
        except Exception:
            return None, None
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            _log_warn(f'[news/image] bad-scheme-on-hop url={current_url[:120]}')
            return None, None
        if not _news_host_is_safe(parsed.hostname):
            _log_warn(f'[news/image] blocked-host-on-hop url={current_url[:120]}')
            return None, None
        referer = f'{parsed.scheme}://{parsed.netloc}/'
        try:
            resp = requests.get(
                current_url,
                timeout=_NEWS_IMG_FETCH_TIMEOUT,
                headers={
                    'User-Agent': _NEWS_IMG_UA,
                    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
                    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
                    # Referer = Origin des Bildes selbst → umgeht die meisten
                    # Hotlink-Schutz-Checks (die nur „fremde" Referer blocken).
                    'Referer': referer,
                },
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            _log_warn(f'[news/image] fetch-error: {exc!r}')
            return None, None
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get('Location') or ''
            try:
                resp.close()
            except Exception:
                pass
            resp = None
            if not location:
                return None, None
            # Relative Location gegen die aktuelle URL auflösen; der neue Host
            # wird am Schleifenkopf erneut gegen _news_host_is_safe geprüft.
            current_url = urllib.parse.urljoin(current_url, location)
            continue
        break
    if resp is None:
        _log_warn(f'[news/image] too-many-redirects url={url[:120]}')
        return None, None
    try:
        if resp.status_code != 200:
            _log_warn(f'[news/image] status={resp.status_code} url={url[:120]}')
            return None, None
        ct = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not ct.startswith('image/'):
            _log_warn(f'[news/image] non-image content-type={ct!r} url={url[:120]}')
            return None, None
        data = b''
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            data += chunk
            if len(data) > _NEWS_IMG_MAX_BYTES:
                _log_warn(f'[news/image] oversize >{_NEWS_IMG_MAX_BYTES}B url={url[:120]}')
                return None, None
        if not data:
            return None, None
        return data, ct
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _news_img_response(data, content_type):
    from flask import Response
    resp = Response(data, mimetype=content_type or 'image/jpeg')
    # Lange cachebar — News-Bilder ändern sich unter derselben URL praktisch nie.
    resp.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    resp.headers['Content-Length'] = str(len(data))
    return resp


@news_bp.route('/api/news/image', methods=['GET'])
def get_news_image():
    """Backend-Image-Proxy für den News-Feed (siehe Section-Header).
    Query: u=<absolute http(s)-Bild-URL> (urlencoded)."""
    raw = (request.args.get('u') or '').strip()
    if not raw:
        return jsonify({'ok': False, 'error': 'missing_url'}), 400

    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'bad_url'}), 400
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return jsonify({'ok': False, 'error': 'bad_url'}), 400
    if not _news_host_is_safe(parsed.hostname):
        return jsonify({'ok': False, 'error': 'blocked_host'}), 400

    key_hash = hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:24]

    # 1) In-Memory-Cache (überlebt nur die aktuelle Instanz, aber spart Egress).
    cached = _news_img_mem_get(key_hash)
    if cached:
        ct, data = cached
        return _news_img_response(data, ct)

    # 2) R2-Cache (durabel, instanzübergreifend) — nur wenn app.py R2 aktiv hat.
    m = _debrief_get_app_module()
    r2_enabled = bool(m and getattr(m, 'R2_AVATARS_ENABLED', False))
    r2_key = f'news/img/{key_hash}'
    if r2_enabled:
        try:
            data, ct = m._r2_get_bytes(r2_key)
        except Exception:
            data, ct = None, None
        if data:
            ct = ct or 'image/jpeg'
            _news_img_mem_set(key_hash, ct, data)
            return _news_img_response(data, ct)

    # 3) Remote ziehen.
    data, ct = _news_fetch_remote_image(raw)
    if not data:
        # SSRF/Open-Redirect-Härtung (Audit 2026-07-01): KEIN 302 mehr auf die
        # unvalidierte Roh-URL (der Fetch kann auch WEGEN blocked_host scheitern
        # — der Redirect hätte den Client dann genau dorthin geschickt).
        # 404 = die App zeigt ihren Platzhalter.
        return jsonify({'ok': False, 'error': 'image_unavailable'}), 404

    _news_img_mem_set(key_hash, ct, data)
    if r2_enabled:
        try:
            m._r2_put_bytes(r2_key, data, ct)
        except Exception as exc:
            _log_warn(f'[news/image] r2_put_fail: {exc!r}')
    return _news_img_response(data, ct)


# ──────────────────────────────────────────────────────────────────
#  Cache helpers
# ──────────────────────────────────────────────────────────────────

def _cache_get(key):
    with _FEED_CACHE_LOCK:
        entry = _FEED_CACHE.get(key)
        if not entry:
            return None
        if (time.time() - entry['ts']) > _FEED_CACHE_TTL_SECONDS:
            # Stale — wir geben None zurück; refresh erfolgt im Caller.
            return None
        return entry['payload']


def _cache_set(key, payload):
    with _FEED_CACHE_LOCK:
        _FEED_CACHE[key] = {'ts': time.time(), 'payload': payload}
        # Soft-GC: wenn zu viele Keys, werfen wir die ältesten raus.
        # Mit (airline x category)-Permutation reden wir grob über
        # 90 Airline-Werte * 6 Kategorien = 540 mögliche Schlüssel.
        # 800 als Hard-Cap ist großzügig.
        if len(_FEED_CACHE) > 800:
            oldest = sorted(_FEED_CACHE.items(), key=lambda kv: kv[1]['ts'])[:200]
            for k, _ in oldest:
                _FEED_CACHE.pop(k, None)


# ──────────────────────────────────────────────────────────────────
#  Source-Pull (parallel)
# ──────────────────────────────────────────────────────────────────

def _aggregate_all_sources():
    """Pullt alle Quellen parallel, dedupliziert und gibt eine flache
    Artikel-Liste zurück.

    Return: (articles, source_status)
        source_status = {source_id: True/False}  — True = lieferte ≥1 Artikel
    """
    raw_results = {}
    source_status = {s['id']: False for s in SOURCES}

    with ThreadPoolExecutor(max_workers=6) as pool:
        future_to_source = {
            pool.submit(_pull_one_source_safe, src): src for src in SOURCES
        }
        try:
            for fut in as_completed(future_to_source, timeout=_AGGREGATE_TIMEOUT_SECONDS):
                src = future_to_source[fut]
                try:
                    items = fut.result(timeout=1) or []
                except Exception as exc:
                    _log_warn(f'[news] source {src["id"]} exception: {exc!r}')
                    items = []
                raw_results[src['id']] = items
                source_status[src['id']] = bool(items)
        except Exception as exc:
            # Aggregate-Timeout — die noch nicht fertigen Sources zählen als failed.
            _log_warn(f'[news] aggregate timeout: {exc!r}')

    # Flatten + Dedupe
    flat = []
    for src_id, items in raw_results.items():
        flat.extend(items)

    deduped = _dedupe_articles(flat)
    return deduped, source_status


def _pull_one_source_safe(src):
    """Wrapper: ein Source-Crash darf nicht den Aggregator killen."""
    try:
        if src['kind'] == 'rss':
            return _pull_rss(src)
        if src['kind'] == 'avherald_scrape':
            return _pull_avherald(src)
        _log_warn(f'[news] unknown source kind: {src["kind"]} ({src["id"]})')
        return []
    except Exception as exc:
        _log_warn(f'[news] source {src["id"]} failed: {exc!r}')
        return []


def _pull_rss(src):
    """RSS-Feed via requests fetchen, dann feedparser.parse(bytes)."""
    if feedparser is None:
        _log_warn('[news] feedparser nicht installiert — RSS deaktiviert')
        return []
    try:
        resp = requests.get(
            src['url'],
            timeout=_SOURCE_TIMEOUT_SECONDS,
            headers={
                'User-Agent': _USER_AGENT,
                'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml',
                'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
            },
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        _log_warn(f'[news] {src["id"]} http-error: {exc!r}')
        return []
    if resp.status_code != 200:
        _log_warn(f'[news] {src["id"]} status={resp.status_code}')
        return []

    parsed = feedparser.parse(resp.content)
    items = []
    for entry in (parsed.entries or [])[:60]:
        try:
            art = _entry_to_article(entry, src)
        except Exception as exc:
            _log_warn(f'[news] {src["id"]} entry-parse-error: {exc!r}')
            continue
        if art:
            items.append(art)
    return items


def _entry_to_article(entry, src):
    """Konvertiert ein feedparser-Entry in unser Article-Schema."""
    link = (entry.get('link') or '').strip()
    title = _strip_html(entry.get('title') or '').strip()
    if not link or not title:
        return None

    summary_raw = entry.get('summary') or entry.get('description') or ''
    summary = _strip_html(summary_raw).strip()
    full_summary_len = len(summary)
    if len(summary) > 300:
        summary = summary[:297].rstrip() + '...'

    # Volltext aus RSS <content:encoded> (viele DE-Aviation-Feeds liefern den
    # ganzen Artikel) → direkt IN-APP lesbar, kein Reader/Browser nötig.
    content_raw = ''
    try:
        cont = entry.get('content')
        if cont and isinstance(cont, list):
            content_raw = cont[0].get('value') or ''
    except Exception:
        content_raw = ''
    # Absatz-erhaltend (\n\n zwischen <p>-Blöcken) statt _strip_html — sonst
    # rendert die Detail-View einen einzigen Riesen-Absatz (iOS splittet auf \n\n).
    fulltext = _html_to_paragraph_text(content_raw).strip()
    # Spenden-/Förder-Appelle aus dem RSS-Volltext entfernen, bevor er als
    # in-app-lesbar gilt oder ausgeliefert wird.
    try:
        fulltext = _strip_donation_appeals(fulltext)
    except Exception:
        pass
    # Quell-Cruft (fremde Schlagzeilen / Ticker-Label / Datum+Flug-Fußzeile) aus
    # den DE-Feeds entfernen (Owner 2026-07-10) — konservativ, saubere Feeds bleiben.
    try:
        fulltext = _strip_feed_cruft(fulltext, source_name=src.get('name'))
    except Exception:
        pass
    # „In-App lesbar" = es gibt einen ECHTEN Volltext im RSS (content:encoded).
    # NICHT mehr „lange Zusammenfassung reicht" — der frühere Summary-Fallback ließ
    # Teaser durch, die in der App nur „Volltext nicht verfügbar / Im Browser öffnen"
    # zeigen (User: „wenn kein Volltext wegen Scraping, dann die Seite nicht für News
    # benutzen"). Quellen ohne content:encoded fallen so bei ?readable_only=1 raus.
    in_app_readable = len(fulltext) >= 400

    published_at = _entry_unix_ts(entry)
    image_url = _entry_image_url(entry)
    hashtags = _entry_hashtags(entry)

    canonical = _canonicalize_url(link)
    art_id = hashlib.sha256(canonical.encode('utf-8', errors='replace')).hexdigest()[:16]

    return {
        'id': art_id,
        'source': src['id'],
        'source_name': src['name'],
        'source_logo_url': src['logo_url'],
        'title': title,
        'summary': summary,
        'published_at': published_at,
        'image_url': image_url,
        'article_url': link,
        'hashtags': hashtags,
        'fulltext': fulltext[:8000] if fulltext else None,
        'in_app_readable': in_app_readable,
        'mentioned_airlines': [],  # wird im Caller gefüllt
        'category': _DEFAULT_CATEGORY,  # wird im Caller überschrieben
    }


def _entry_unix_ts(entry):
    """Parsed published/updated parsed-time aus feedparser; fallback now.
    feedparser liefert *_parsed als UTC-struct_time → calendar.timegm
    (time.mktime würde Localtime annehmen = TZ-Shift)."""
    import calendar as _cal
    for key in ('published_parsed', 'updated_parsed', 'created_parsed'):
        struct_t = entry.get(key)
        if struct_t:
            try:
                return int(_cal.timegm(struct_t))
            except Exception:
                continue
    return int(time.time())


def _entry_image_url(entry):
    """Versucht ein passendes Image aus media_content/media_thumbnail/enclosure
    /summary-img-src zu extrahieren. None wenn keins gefunden."""
    # media:content / media:thumbnail
    for key in ('media_content', 'media_thumbnail'):
        media = entry.get(key)
        if media and isinstance(media, list):
            for m in media:
                u = (m or {}).get('url')
                if u:
                    return u
    # enclosure
    enclosures = entry.get('enclosures') or []
    for enc in enclosures:
        if (enc or {}).get('type', '').startswith('image/'):
            u = (enc or {}).get('href') or (enc or {}).get('url')
            if u:
                return u
    # <img src="..."> im summary-HTML — billige Regex, kein bs4-Roundtrip.
    summary_raw = entry.get('summary') or entry.get('description') or ''
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary_raw, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _entry_hashtags(entry):
    """Extrahiert RSS-Tags/Categories als hashtag-Liste."""
    tags = entry.get('tags') or []
    out = []
    for t in tags:
        term = (t or {}).get('term') if isinstance(t, dict) else None
        if not term:
            continue
        slug = re.sub(r'[^a-zA-Z0-9]', '', term)[:40]
        if slug:
            out.append(slug)
    return out[:6]


def _pull_avherald(src):
    """AvHerald hat kein RSS — wir scrapen die Frontpage.

    Layout (Stand 2026-06): jede Incident-Zeile ist ein <tr> in einer
    Tabelle, mit:
      - <td class="headline"><a href="article.php?opt=...">Titel</a></td>
    Wir extrahieren die ~20 sichtbaren Headlines mit absoluten URLs.
    """
    if BeautifulSoup is None:
        _log_warn('[news] beautifulsoup4 fehlt — avherald deaktiviert')
        return []
    try:
        resp = requests.get(
            src['url'],
            timeout=_SOURCE_TIMEOUT_SECONDS,
            headers={
                'User-Agent': _USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        _log_warn(f'[news] avherald http-error: {exc!r}')
        return []
    if resp.status_code != 200:
        _log_warn(f'[news] avherald status={resp.status_code}')
        return []

    try:
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as exc:
        _log_warn(f'[news] avherald parse-error: {exc!r}')
        return []

    items = []
    seen_urls = set()
    base = 'https://avherald.com/'

    # Strategie 1: Anchor-Tags die auf article.php zeigen — robust gegen
    # Layout-Umbauten (table vs. div), solange die URL-Form gleichbleibt.
    anchors = soup.find_all('a', href=re.compile(r'article\.php\?opt='))
    for a in anchors[:40]:
        href = (a.get('href') or '').strip()
        title = _strip_html(a.get_text() or '').strip()
        if not href or not title or len(title) < 8:
            continue
        if href.startswith('http://') or href.startswith('https://'):
            full_url = href
        else:
            full_url = urllib.parse.urljoin(base, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # AvHerald hat keine Per-Item-Timestamps in der Frontpage — wir
        # spreaden published_at künstlich (jetzt minus N*30min) damit die
        # Sortierung stabil bleibt aber neuere Inserts oben landen.
        published_at = int(time.time()) - (len(items) * 1800)

        canonical = _canonicalize_url(full_url)
        art_id = hashlib.sha256(canonical.encode('utf-8', errors='replace')).hexdigest()[:16]

        items.append({
            'id': art_id,
            'source': src['id'],
            'source_name': src['name'],
            'source_logo_url': src['logo_url'],
            'title': title,
            'summary': '',  # Frontpage hat kein Summary — Client kann den Vollartikel öffnen.
            'published_at': published_at,
            'image_url': None,
            'article_url': full_url,
            'hashtags': ['incident', 'avherald'],
            'mentioned_airlines': [],
            'category': _DEFAULT_CATEGORY,
        })
        if len(items) >= 20:
            break

    if not items:
        _log_warn('[news] avherald: 0 anchors matched — Layout-Change?')
    return items


# ──────────────────────────────────────────────────────────────────
#  Dedupe
# ──────────────────────────────────────────────────────────────────

def _dedupe_articles(articles):
    """Dedupliziert anhand (a) canonical-URL-hash und (b) Title-Similarity
    via Jaccard auf Token-Sets.

    Strategie:
      - Pass 1: gleiche URL (oder gleiche id) → killen.
      - Pass 2: title-token-set Jaccard >= 0.85 → killen.
      Wir behalten den jeweils ÄLTEREN Eintrag (lower published_at) wenn beide
      gleich alt sind, sonst den mit längerem Summary (mehr Kontext).
    """
    # Pass 1: URL/id
    by_id = {}
    for art in articles:
        art_id = art.get('id')
        url_key = _canonicalize_url(art.get('article_url') or '')
        key = art_id or url_key
        if not key:
            continue
        existing = by_id.get(key)
        if existing is None:
            by_id[key] = art
            continue
        # Konflikt — besseren behalten (längere Summary gewinnt).
        if len(art.get('summary') or '') > len(existing.get('summary') or ''):
            by_id[key] = art

    candidates = list(by_id.values())

    # Pass 2: Jaccard
    kept = []
    kept_token_sets = []
    for art in candidates:
        tokens = _title_tokens(art.get('title') or '')
        if not tokens:
            kept.append(art)
            kept_token_sets.append(tokens)
            continue
        dup_idx = -1
        for i, existing_tokens in enumerate(kept_token_sets):
            if not existing_tokens:
                continue
            sim = _jaccard(tokens, existing_tokens)
            if sim >= 0.85:
                dup_idx = i
                break
        if dup_idx == -1:
            kept.append(art)
            kept_token_sets.append(tokens)
        else:
            # Konflikt — den mit längerer Summary behalten.
            existing = kept[dup_idx]
            if len(art.get('summary') or '') > len(existing.get('summary') or ''):
                kept[dup_idx] = art
                kept_token_sets[dup_idx] = tokens

    return kept


_STOPWORDS = {
    'der', 'die', 'das', 'und', 'oder', 'in', 'mit', 'für', 'auf', 'an', 'zu',
    'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'on', 'at', 'for', 'with',
    'is', 'are', 'was', 'were', 'be', 'been', 'by', 'as', 'it', 'its', 'this',
    'that', 'from', 'after', 'before', 'over', 'into', 'ein', 'eine', 'einer',
    'wegen', 'nach', 'vor', 'bei', 'aus', 'um', 'am', 'im',
}


def _title_tokens(title):
    """Lower-case, strip-punct, stopword-filter — gibt set(str) zurück."""
    if not title:
        return set()
    t = title.lower()
    # Unicode-aware splitting, ohne extra Library.
    parts = re.findall(r'[a-zäöüß0-9]+', t)
    return {p for p in parts if len(p) > 2 and p not in _STOPWORDS}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


# ──────────────────────────────────────────────────────────────────
#  Airline-Relevance + Category-Classification
# ──────────────────────────────────────────────────────────────────

def _normalize_airline_input(airline_raw):
    """Mapt User-Input (IATA, ICAO, Name) auf eine Menge von Match-Tokens.

    Beispiel:
      "LH"        → {"lh", "lufthansa", "dlh"}
      "Lufthansa" → {"lh", "lufthansa", "dlh"}
      "DLH"       → {"lh", "lufthansa", "dlh"}
    """
    if not airline_raw:
        return set()
    needle = airline_raw.strip().lower()
    out = {needle}
    # IATA-Direkttreffer
    for iata, aliases in AIRLINE_ALIASES.items():
        if iata.lower() == needle:
            out.add(iata.lower())
            out.update(a.lower() for a in aliases)
            return out
    # Alias-Reverse-Lookup
    for iata, aliases in AIRLINE_ALIASES.items():
        for alias in aliases:
            if alias.lower() == needle or needle in alias.lower():
                out.add(iata.lower())
                out.update(a.lower() for a in aliases)
                return out
    # Kein bekannter Code — Input pur weitergeben (User darf nach
    # exotischer Airline filtern, wir matchen substring auf title+summary).
    return out


def _extract_mentioned_airlines(title, summary):
    """Gibt die Liste der IATA-Codes zurück, die im title+summary erwähnt
    werden. 2-letter codes nur als Whole-Word, Namen als Substring."""
    haystack = f'{title or ""}\n{summary or ""}'.lower()
    if not haystack.strip():
        return []
    out = set()
    for iata, aliases in AIRLINE_ALIASES.items():
        # 2-letter IATA-Code — Whole-Word-Match, sonst zu viele false positives.
        pattern = re.compile(rf'\b{re.escape(iata.lower())}\b')
        if pattern.search(haystack):
            out.add(iata)
            continue
        # Namen/ICAO. 3-Zeichen-Aliase (KLM/ANA/JAL/SAS/TAP/LOT) sind häufig
        # genug als Whole-Word zu treffen — wir matchen sie via \b. Längere
        # Aliase (>=4 Zeichen) erlauben wir auch als Substring (z.B. "Lufthansa"
        # in "Lufthansa-Tochter").
        matched = False
        for alias in aliases:
            alias_l = alias.lower()
            if len(alias_l) == 3:
                if re.search(rf'\b{re.escape(alias_l)}\b', haystack):
                    out.add(iata)
                    matched = True
                    break
            elif len(alias_l) >= 4 and alias_l in haystack:
                out.add(iata)
                matched = True
                break
        if matched:
            continue
    return sorted(out)


def _classify_category(title, summary):
    text = f'{title or ""}\n{summary or ""}'
    for cat, pat in CATEGORY_PATTERNS:
        if pat.search(text):
            return cat
    return _DEFAULT_CATEGORY


def _airline_relevance(art, needles):
    """Berechnet (relevance:int, is_own_airline:bool) eines Artikels für die
    gewählte Airline. `needles` ist die Menge der Match-Tokens aus
    `_normalize_airline_input` (leer = kein Airline-Filter → (0, False)).

    Scoring (höher = relevanter, kommt zuerst):
      2  — strukturierter Treffer in mentioned_airlines (IATA-Code passt)
      1  — Name/Alias als Substring im title+summary (>=4 Zeichen)
      0  — keine Airline-Relevanz (allgemeine News)
    is_own_airline ist True bei jedem Score >= 1.
    """
    if not needles:
        return 0, False
    mentioned_lower = {m.lower() for m in (art.get('mentioned_airlines') or [])}
    if mentioned_lower & needles:
        return 2, True
    blob = f'{art.get("title", "")}\n{art.get("summary", "")}'.lower()
    for needle in needles:
        if len(needle) >= 4 and needle in blob:
            return 1, True
    return 0, False


def _filter_articles(articles, airline_raw, category):
    """Wendet die echten Filter an. Category ist ein harter Filter.
    Airline ist KEIN Filter mehr (nur noch Boost/Sort im Caller via
    `_airline_relevance`) — `airline_raw` bleibt aus Kompatibilität in der
    Signatur, wird aber nur noch genutzt wenn explizit gesetzt (Legacy)."""
    out = articles

    if category:
        out = [a for a in out if a.get('category') == category]

    if airline_raw:
        needles = _normalize_airline_input(airline_raw)
        # Match wenn (a) mentioned_airlines IATA-Code passt oder (b) eine
        # der needle-Strings substring im title/summary ist.
        def _matches(art):
            rel, _own = _airline_relevance(art, needles)
            return rel > 0
        out = [a for a in out if _matches(a)]

    return out


# ──────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────

def _canonicalize_url(url):
    """Strippt UTM-Parameter und fragment, lowercased host.
    NICHT path-lowercased (case-sensitive URLs würden brechen)."""
    if not url:
        return ''
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except Exception:
        return url.strip()
    # Query: utm_* raus, fbclid raus, gclid raus.
    keep_q = []
    if parsed.query:
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False):
            kl = k.lower()
            if kl.startswith('utm_') or kl in ('fbclid', 'gclid', 'mc_cid', 'mc_eid'):
                continue
            keep_q.append((k, v))
    new_query = urllib.parse.urlencode(keep_q)
    netloc = (parsed.netloc or '').lower()
    return urllib.parse.urlunsplit((parsed.scheme.lower() or 'https', netloc, parsed.path, new_query, ''))


_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _strip_html(s):
    if not s:
        return ''
    no_tags = _TAG_RE.sub(' ', s)
    unescaped = html_lib.unescape(no_tags)
    return _WS_RE.sub(' ', unescaped).strip()


_SCRIPT_STYLE_RE = re.compile(r'<(script|style)\b[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r'</(p|div|h[1-6]|li|blockquote|figcaption|tr)\s*>', re.IGNORECASE)
_HSPACE_RE = re.compile(r'[ \t\xa0]+')


def _html_to_paragraph_text(s):
    """HTML-Fragment → Plain-Text MIT erhaltener Absatz-Struktur.

    Im Gegensatz zu `_strip_html` (kollabiert ALLES auf eine Zeile) werden
    Block-Enden (</p>, </div>, </h*>, </li>, …) zu Absatz-Trennern (\\n\\n)
    und <br> zu \\n — die iOS-Detail-View rendert Absätze via split("\\n\\n").
    Pure Funktion, wirft nie.
    """
    if not s:
        return ''
    txt = _SCRIPT_STYLE_RE.sub(' ', s)
    txt = _BR_RE.sub('\n', txt)
    txt = _BLOCK_CLOSE_RE.sub('\n\n', txt)
    txt = _TAG_RE.sub(' ', txt)
    try:
        txt = html_lib.unescape(txt)
    except Exception:
        pass
    # Zeilenweise horizontal normalisieren, vertikale Struktur behalten.
    lines = [_HSPACE_RE.sub(' ', ln).strip() for ln in txt.replace('\r', '\n').split('\n')]
    joined = '\n'.join(lines)
    joined = re.sub(r'\n{3,}', '\n\n', joined)
    return joined.strip()


# Spenden-/Solicitation-Marker (case-insensitive). "strong" = an sich schon ein
# Aufruf; "weak" = braucht zusätzlich ein Call-to-Action-Verb im selben Satz.
# Bewusst konservativ: nur der anstößige Satz fliegt, nie der ganze Artikel.
_DONATION_MARKERS_STRONG = (
    'spendenaufruf', 'unterstützen sie uns', 'unterstütze uns',
    'jetzt unterstützen', 'jetzt unterstützen sie', 'spendenkonto',
    'werde mitglied', 'werden sie mitglied', 'werde fördermitglied',
    'support us', 'donate', 'buy us a coffee', 'buy me a coffee',
    'abonnieren sie',
)
_DONATION_MARKERS_WEAK = (
    'spende', 'paypal', 'patreon', 'steady', 'iban', 'fördermitglied',
    'newsletter', 'membership',
)
_DONATION_CTA_VERBS = (
    'unterstützen sie', 'unterstütze uns', 'unterstütz uns',
    'spenden sie', 'jetzt spenden', 'spende jetzt', 'bitte spende',
    'abonnier', 'registrier', 'klicken sie', 'helfen sie',
    'werde mitglied', 'werden sie mitglied', 'jetzt unterstützen',
    'pledge', 'please support', 'support our', 'please donate',
    'subscribe', 'join us', 'become a', 'sign up',
)


def _strip_donation_appeals(text):
    """Entfernt Spenden-/Förder-/Solicitation-Sätze aus Artikel-Volltext.

    Wirkt absatz- und satzweise: nur der konkrete anstößige Satz fällt raus,
    der Rest bleibt. Ein Satz fliegt nur bei strong-Marker ODER weak-Marker +
    CTA-Verb. Reine String-Operation, wirft nie nach außen.
    """
    if not text:
        return text

    def _is_appeal(sentence):
        low = sentence.lower()
        if any(m in low for m in _DONATION_MARKERS_STRONG):
            return True
        if any(m in low for m in _DONATION_MARKERS_WEAK):
            if any(v in low for v in _DONATION_CTA_VERBS):
                return True
        return False

    out_lines = []
    for line in text.split('\n'):
        if not line.strip():
            out_lines.append(line)
            continue
        parts = re.split(r'(?<=[.!?])\s+', line)
        kept = [p for p in parts if not _is_appeal(p)]
        if not kept:
            continue
        out_lines.append(' '.join(kept).strip())

    joined = '\n'.join(out_lines)
    joined = re.sub(r'\n{3,}', '\n\n', joined)
    return joined.strip()


# Datum-only-Zeilen am Text-Ende (Quell-Metadaten): „July 10, 2026" / „10. Juli 2026".
_TRAIL_DATE_RE = re.compile(
    r'^(?:[A-Za-zÄÖÜäöü]+\s+\d{1,2},?\s+\d{4}|\d{1,2}\.\s*[A-Za-zÄÖÜäöü]+\.?\s+\d{4})$')
# „Flug AC774," / „Flight AC774" — Quell-Fußzeile.
_TRAIL_FLIGHT_RE = re.compile(r'^(?:flug|flight)\s+[A-Z0-9]{2,8},?$', re.IGNORECASE)


def _strip_feed_cruft(text, source_name=''):
    """Entfernt Quell-Cruft, den DE-Feeds im RSS-<content:encoded> VOR und NACH
    dem eigentlichen Artikel mitschicken (Owner 2026-07-10: „aero.de-Text zeigt
    fremde Schlagzeilen + Datum/Flug-Zeilen"):
      • VORNE: den Quell-Namen selbst (aeroTELEGRAPH/aero.de), Ticker-/Slug-Label
        („ticker electra aero") und einen BLOCK aus ≥2 kurzen Überschriften-Zeilen
        (verwandte Artikel) — aber NUR wenn danach ein substanzieller Absatz folgt,
        damit nie eine echte kurze Einstiegszeile geopfert wird.
      • HINTEN: reine Datums-Zeile + „Flug XX,"-Fußzeile.
    Konservativ, wirft nie; lässt saubere Feeds (simple_flying etc.) unangetastet,
    weil dort weder Label- noch Überschriften-Block-Muster greifen."""
    if not text:
        return text
    lines = text.split('\n')

    # ── VORNE strippen ────────────────────────────────────────────────
    # ALLE Header-Cruft-Zeilen VOR dem ersten echten Absatz weg — egal ob Quell-Label
    # („aeroTELEGRAPH"), Ticker-Slug („ticker-condor-grun") oder fremde Schlagzeile.
    # „Cruft" = KURZ (< 60 Zeichen) UND endet NICHT mit Satzzeichen. Stopp am ersten
    # echten Text; nur strippen, wenn danach ein substanzieller Absatz (≥100 Zeichen)
    # folgt → köpft nie einen echten kurzen Artikel. Max 6 Zeilen als Sicherheitsnetz.
    i, dropped = 0, 0
    while i < len(lines) and dropped < 6:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if len(s) >= 60 or s.endswith(('.', '!', '?')):
            break
        if not any(len(l.strip()) >= 100 for l in lines[i + 1:i + 11]):
            break
        i += 1
        dropped += 1
    lines = lines[i:]

    # ── HINTEN strippen ───────────────────────────────────────────────
    while lines:
        s = lines[-1].strip()
        if not s:
            lines.pop()
            continue
        if _TRAIL_DATE_RE.match(s) or _TRAIL_FLIGHT_RE.match(s):
            lines.pop()
            continue
        break

    out = '\n'.join(lines)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()


def _log_warn(msg):
    """Logging über current_app wenn im Request-Context, sonst logger."""
    try:
        current_app.logger.warning(msg)
    except Exception:
        _logger.warning(msg)


# ══════════════════════════════════════════════════════════════════
#  Permanenter Volltext-Layer  (Harvest + Feed-Anreicherung)
#
#  Owner-Wunsch 2026-07-10: „Nachrichten-Text ist nicht gespeichert/
#  gescrapt im Backend — damit es schneller lädt und der Text direkt
#  voll da ist." Vorher wurde der Volltext erst beim ERSTEN Öffnen
#  eines Artikels pro User via /api/news/article (app.py) gescraped
#  (langsam, konnte scheitern → nur Teaser + Link).
#
#  Jetzt:
#    1. Beim Feed-Aggregat (Cache-Miss, ≤1×/15min) hängt
#       `_attach_stored_fulltexts` den gespeicherten Volltext
#       aus Supabase `news_article_cache` an jeden Artikel OHNE
#       RSS-content:encoded — der Text ist damit DIREKT im Feed-Payload
#       (Serve-TTL + Deny-Hosts, s. Copyright-Leitplanken unten).
#    2. `_kickoff_fulltext_harvest` erntet fehlende Volltexte im
#       Hintergrund-Thread nach: NUR neue Artikel (Store-Check zuerst),
#       höflich (identifizierender UA mit Kontakt, 1s Delay, harter
#       Fetch-Cap pro Lauf), Fehler ⇒ Teaser-Fallback bleibt.
#    3. RSS-Volltexte werden ebenfalls persistiert (0 Extra-Scrapes) —
#       so überlebt der Text das Rausrotieren aus dem RSS-Feed und
#       /api/news/article (L2-Cache = dieselbe Tabelle) hat ihn warm.
#
#  Die Tabelle ist DIESELBE wie der L2-Cache von /api/news/article
#  (app.py) — dieser Layer schreibt zusätzlich `harvested_at`
#  (Migration 20260710_news_fulltext.sql).
#
#  COPYRIGHT-LEITPLANKEN (Nachfix 2026-07-10 — „Quellenangabe+Link"
#  lizenziert KEINE Volltext-Übernahme):
#    • Reuters/AvHerald sind vom Volltext-Layer ausgenommen
#      (_FULLTEXT_DENY_HOSTS): deren Terms verbieten Scraping/
#      Republikation explizit → dort bleibt es beim Teaser +
#      On-Demand-Reader-Link.
#    • Der Scrape-Harvester respektiert robots.txt (_robots_allows).
#    • KEIN Ewig-Serve: die Feed-Auslieferung liest den Store mit
#      Serve-TTL (_FULLTEXT_SERVE_TTL_DAYS) — depublizierte Artikel
#      fallen so nach Ablauf wieder auf den Teaser zurück (nur was
#      die Quelle weiterhin öffentlich hält, wird beim Re-Harvest
#      erneut lieferbar). §87f UrhG: jenseits von Snippets greift
#      das Presseverleger-Leistungsschutzrecht.
#    • Quellen-Link bleibt immer am Artikel.
# ══════════════════════════════════════════════════════════════════

_FULLTEXT_TABLE = 'news_article_cache'
# Höflicher, identifizierender UA mit Kontaktadresse (Muster Planespotters-Fix).
_FULLTEXT_HARVEST_UA = (
    'AeroX/1.0 (news-fulltext-harvester; +https://aerosteuer.de; '
    'contact: aerox@aerosteuer.de)'
)
_FULLTEXT_FETCH_TIMEOUT = 10          # Sekunden pro Quell-Fetch
_FULLTEXT_STORE_MIN_CHARS = 80        # gleiches Gate wie /api/news/article
# Ab hier gilt ein RSS-Volltext als ECHTER Volltext (nicht Teaser) → darf direkt
# gecacht werden. Darunter (kurzer Teaser wie simpleflying ~300–400) wird die volle
# Quellseite gescrapt, sonst sähe der Artikel in der App wie ein RSS-Feed aus.
_FULLTEXT_RSS_PUT_MIN = 1200
_FULLTEXT_READABLE_MIN_CHARS = 400    # ab hier gilt „in-app lesbar" (wie RSS-Pfad)
_FULLTEXT_FEED_CAP = 8000             # Payload-Cap, identisch zum RSS-Pfad
_FULLTEXT_HARVEST_MAX_FETCHES = 10    # max. NEUE Quell-Fetches pro Harvest-Lauf
_FULLTEXT_HARVEST_MAX_RSS_PUTS = 60   # max. RSS-Volltext-Upserts pro Lauf
_FULLTEXT_HARVEST_DELAY_S = 1.0       # Pause zwischen zwei Quell-Fetches
_FULLTEXT_HARVEST_MIN_GAP_S = 10 * 60  # min. Abstand zwischen zwei Läufen
_FULLTEXT_HARVEST_STATE = {'last_run': 0.0, 'running': False}
_FULLTEXT_HARVEST_LOCK = threading.Lock()
# Serve-TTL für den Feed-Attach (Copyright: KEIN Ewig-Serve nach Depublikation
# — nach Ablauf wird nur erneut lieferbar, was die Quelle noch öffentlich hält
# und der Re-Harvest frisch zieht; Feed-Artikel rotieren ohnehin binnen Tagen).
_FULLTEXT_SERVE_TTL_DAYS = 14
# Volltext-HARTE Ausnahmen (Nachfix 2026-07-10): Terms verbieten Scraping/
# Republikation explizit (Reuters ToS, AvHerald) — weder ernten noch aus dem
# Store an den Feed hängen. Diese Quellen bleiben Teaser + Quell-Link.
# Bewusst UNABHÄNGIG von app.NEWS_ARTICLE_ALLOWED_HOSTS (On-Demand-Reader),
# damit ein Whitelist-Update den Harvest nicht wieder öffnet.
_FULLTEXT_DENY_HOSTS = frozenset({
    'reuters.com', 'www.reuters.com',
    'avherald.com', 'www.avherald.com',
})

# Fallback-Whitelist falls app.py nicht ladbar (isolierte Blueprint-Tests).
# Produktiv gewinnt app.NEWS_ARTICLE_ALLOWED_HOSTS (Single Source of Truth).
_FULLTEXT_ALLOWED_HOSTS_FALLBACK = frozenset({
    'aero.de', 'www.aero.de',
    'aerotelegraph.com', 'www.aerotelegraph.com',
    'aerobuzz.de', 'www.aerobuzz.de',
    'aviation.direct', 'www.aviation.direct',
    'austrianwings.info', 'www.austrianwings.info',
    'reuters.com', 'www.reuters.com',
    'avherald.com', 'www.avherald.com',
    'simpleflying.com', 'www.simpleflying.com',
    'theaircurrent.com', 'www.theaircurrent.com',
    'flightradar24.com', 'www.flightradar24.com',
})


def _fulltext_allowed_hosts():
    m = _debrief_get_app_module()
    hosts = getattr(m, 'NEWS_ARTICLE_ALLOWED_HOSTS', None) if m else None
    return hosts or _FULLTEXT_ALLOWED_HOSTS_FALLBACK


def _fulltext_url_key(url):
    """MUSS identisch zu app._news_article_url_key bleiben (geteilte Tabelle):
    sha256(url) hex, erste 32 Zeichen."""
    return hashlib.sha256((url or '').encode('utf-8', errors='replace')).hexdigest()[:32]


def _fulltext_host(url):
    try:
        return (urllib.parse.urlsplit(url).netloc or '').lower().split(':')[0]
    except Exception:
        return ''


def _fulltext_sb_execute(label, fn, timeout_s=5):
    """Supabase-Call mit Timeout-Wrapper aus app.py wenn verfügbar (blockiert
    den Feed-Request nicht bei SB-Hängern), sonst direkter Call. None bei Fehler."""
    m = _debrief_get_app_module()
    wrapper = getattr(m, '_supabase_execute_with_timeout', None) if m else None
    if callable(wrapper):
        res, timed_out = wrapper(label, fn, timeout_s=timeout_s)
        return None if timed_out else res
    return fn()


def _fulltext_store_get_many(urls):
    """Liest gespeicherte Volltexte für viele Artikel-URLs (chunked .in_()).

    SERVE-TTL statt Ewig-Serve (Copyright-Nachfix 2026-07-10): Rows älter als
    _FULLTEXT_SERVE_TTL_DAYS (fetched_at) werden NICHT mehr geliefert — ein
    depublizierter Artikel fällt so wieder auf den Teaser zurück; nur was die
    Quelle weiterhin öffentlich hält, zieht der Re-Harvest erneut frisch.
    Deny-Hosts (Reuters/AvHerald) werden nie geliefert, auch nicht aus
    Alt-Beständen. Return: {article_url: fulltext}. Leeres dict bei
    SB-down/Fehler (wirft nie).
    """
    sb, available = _debrief_get_sb()
    if not available or sb is None or not urls:
        return {}
    key_to_url = {}
    for u in urls:
        if u and _fulltext_host(u) not in _FULLTEXT_DENY_HOSTS:
            key_to_url[_fulltext_url_key(u)] = u
    out = {}
    keys = list(key_to_url)
    serve_cutoff = (datetime.now(timezone.utc)
                    - timedelta(days=_FULLTEXT_SERVE_TTL_DAYS))
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            def _do(_chunk=chunk):
                return (sb.table(_FULLTEXT_TABLE)
                          .select('url_key, fulltext, fetched_at')
                          .in_('url_key', _chunk)
                          .execute())
            res = _fulltext_sb_execute('news_fulltext_get', _do, timeout_s=5)
            if res is None:
                # Timeout/SB-Hänger → Store als down behandeln, KEINE weiteren
                # Chunks stapeln (der Feed-Request soll nicht 3×5s warten).
                break
            for row in (getattr(res, 'data', None) or []):
                ft = row.get('fulltext')
                url = key_to_url.get(row.get('url_key'))
                if not (url and ft and len(ft) >= _FULLTEXT_STORE_MIN_CHARS):
                    continue
                # Serve-TTL: unparsebares/fehlendes fetched_at zählt als
                # abgelaufen (konservativ — lieber Teaser als Alt-Bestand).
                try:
                    fa = datetime.fromisoformat(
                        str(row.get('fetched_at') or '').replace('Z', '+00:00'))
                    if fa.tzinfo is None:
                        fa = fa.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if fa < serve_cutoff:
                    continue
                out[url] = ft
        except Exception as exc:
            _log_warn(f'[news/fulltext] store_get chunk failed: {exc!r}')
            break
    return out


def _fulltext_store_put(url, fulltext, title=None, source=None,
                        image_url=None, published_at=None):
    """Persistiert einen Artikel-Volltext (Upsert auf url_key). Best-effort.

    Schreibt `harvested_at` (Migration 20260710_news_fulltext.sql); wenn die
    Spalte noch fehlt (Migration nicht applied), Retry ohne sie — die Tabelle
    selbst existiert bereits als L2-Cache von /api/news/article.
    """
    sb, available = _debrief_get_sb()
    if not available or sb is None or not url or not fulltext:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        'url_key': _fulltext_url_key(url),
        'url': url,
        'fulltext': fulltext,
        'title': title,
        'source': source or _fulltext_host(url),
        'image_url': image_url,
        'published_at': published_at,
        'fetched_at': now_iso,
        'harvested_at': now_iso,
    }
    last_exc = None
    for attempt in (record, {k: v for k, v in record.items() if k != 'harvested_at'}):
        try:
            sb.table(_FULLTEXT_TABLE).upsert(attempt, on_conflict='url_key').execute()
            return True
        except Exception as exc:
            last_exc = exc
    _log_warn(f'[news/fulltext] store_put failed url={url[:100]} err={last_exc!r}')
    return False


# ── Extraktion ──────────────────────────────────────────────────────

_FULLTEXT_CONTENT_SELECTORS = (
    'article',
    'main article',
    'div.article-content', 'div.article-body', 'div.article__body',
    'div[itemprop="articleBody"]',
    'div.entry-content',            # WordPress (aerobuzz, aviation.direct, …)
    'div.post-content',
    'div.news-text',
    'div.story-body', 'div.story-content',
    'main',
)

_ARTICLE_TAG_RE = re.compile(r'<article\b[^>]*>(.*?)</article>', re.IGNORECASE | re.DOTALL)


def _extract_fulltext_pure(html_doc):
    """Pure Readability-Heuristik: größter <article>/<p>-Cluster → Plain-Text.

    Keine neuen Dependencies — bs4 ist bereits Pflicht-Dep des Backends; ohne
    bs4 degradiert es auf einen <article>-Regex-Fallback. Absätze werden mit
    \\n\\n getrennt (iOS-Absatz-Rendering), Spenden-Appelle entfernt.
    Deterministisch, wirft nie; '' wenn kein substanzieller Text (<200 Zeichen).
    """
    if not html_doc:
        return ''
    if BeautifulSoup is None:
        m = _ARTICLE_TAG_RE.search(html_doc)
        if not m:
            return ''
        text = _html_to_paragraph_text(m.group(1))
        return _strip_donation_appeals(text) if len(text) >= 200 else ''

    try:
        soup = BeautifulSoup(html_doc, 'html.parser')
    except Exception:
        return ''
    for junk in soup(['script', 'style', 'nav', 'header', 'footer', 'aside',
                      'form', 'noscript', 'iframe']):
        junk.decompose()

    def _paragraphs_of(el, min_len=30):
        paras = []
        for node in el.find_all(['p', 'h2', 'h3', 'li', 'blockquote']):
            t = node.get_text(' ', strip=True)
            if t and len(t) >= min_len:
                paras.append(_WS_RE.sub(' ', t))
        return paras

    # Kandidaten sammeln; es gewinnt der Container mit dem GRÖSSTEN
    # <p>-Cluster (nicht der erste Treffer — Sidebars/Teaser-Listen haben
    # zwar <article>-Tags, aber wenig Absatz-Text).
    candidates = []
    for sel in _FULLTEXT_CONTENT_SELECTORS:
        try:
            for el in soup.select(sel)[:4]:
                candidates.append(el)
        except Exception:
            continue

    best_paras, best_len = [], 0
    for el in candidates:
        paras = _paragraphs_of(el)
        total = sum(len(p) for p in paras)
        if total > best_len:
            best_paras, best_len = paras, total

    # <body> nur als FALLBACK wenn kein Selektor-Kandidat substanziell war —
    # als regulärer Kandidat würde er als Superset (inkl. Sidebar/Teaser)
    # jeden echten Content-Container "überstimmen".
    if best_len < 200 and soup.body is not None:
        paras = _paragraphs_of(soup.body)
        total = sum(len(p) for p in paras)
        if total > best_len:
            best_paras, best_len = paras, total

    text = '\n\n'.join(best_paras).strip()
    if len(text) < 200:
        return ''
    try:
        text = _strip_donation_appeals(text)
    except Exception:
        pass
    if len(text) > 20000:
        text = text[:20000].rsplit('\n\n', 1)[0]
    return text


def _extract_fulltext_for_harvest(html_doc, source_host=''):
    """Produktions-Extraktion: bevorzugt die kampferprobte Multi-Strategie aus
    app.py (`_news_extract_best_fulltext` + `_tidy_article_text`, inkl.
    Boilerplate-Strip), Fallback = `_extract_fulltext_pure`."""
    m = _debrief_get_app_module()
    if m is not None:
        best_fn = getattr(m, '_news_extract_best_fulltext', None)
        if callable(best_fn):
            try:
                ft = best_fn(html_doc, source_host=source_host) or ''
                tidy_fn = getattr(m, '_tidy_article_text', None)
                if ft and callable(tidy_fn):
                    tidied = tidy_fn(ft)
                    if tidied and len(tidied) >= _FULLTEXT_STORE_MIN_CHARS:
                        ft = tidied
                if ft and len(ft) >= _FULLTEXT_STORE_MIN_CHARS:
                    return ft
            except Exception as exc:
                _log_warn(f'[news/fulltext] app-extract failed: {exc!r}')
    return _extract_fulltext_pure(html_doc)


# robots.txt-Gate (Copyright/Politeness-Nachfix 2026-07-10): der proaktive
# Harvester ist ein Crawler und MUSS robots.txt respektieren — der bisherige
# On-Demand-Reader (1 User klickt 1 Artikel) war kein systematischer Crawl,
# der Hintergrund-Harvest ist es. Per-Host gecacht; Fehler/5xx ⇒ konservativ
# NICHT scrapen (kurze TTL, nächster Lauf probiert neu); 404 ⇒ erlaubt.
_ROBOTS_CACHE = {}                 # host → (expires_unix, True|False|RobotFileParser)
_ROBOTS_TTL_OK_S = 12 * 3600
_ROBOTS_TTL_ERR_S = 30 * 60


def _robots_allows(url):
    """Darf der Harvester diese URL laut robots.txt der Quelle ziehen?
    Best-effort, wirft nie; unklare Lage ⇒ False (höflich aussetzen)."""
    import urllib.robotparser
    host = _fulltext_host(url)
    if not host:
        return False
    now = time.time()
    hit = _ROBOTS_CACHE.get(host)
    if hit and hit[0] > now:
        verdict = hit[1]
    else:
        verdict, ttl = False, _ROBOTS_TTL_ERR_S
        try:
            scheme = urllib.parse.urlsplit(url).scheme or 'https'
            resp = requests.get(
                f'{scheme}://{host}/robots.txt', timeout=6,
                headers={'User-Agent': _FULLTEXT_HARVEST_UA})
            if resp.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse((resp.text or '').splitlines())
                verdict, ttl = rp, _ROBOTS_TTL_OK_S
            elif resp.status_code in (401, 403):
                verdict, ttl = False, _ROBOTS_TTL_OK_S   # explizit zu
            elif 400 <= resp.status_code < 500:
                verdict, ttl = True, _ROBOTS_TTL_OK_S    # kein robots.txt
        except Exception:
            pass
        _ROBOTS_CACHE[host] = (now + ttl, verdict)
    if verdict is True or verdict is False:
        return verdict
    try:
        return bool(verdict.can_fetch(_FULLTEXT_HARVEST_UA, url))
    except Exception:
        return False


def _harvest_fetch_article_html(url):
    """Zieht das Quell-HTML eines Artikels (höflich, mit Timeout). None bei Fehler."""
    try:
        resp = requests.get(
            url,
            timeout=_FULLTEXT_FETCH_TIMEOUT,
            headers={
                'User-Agent': _FULLTEXT_HARVEST_UA,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.7,en;q=0.5',
            },
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        _log_warn(f'[news/fulltext] fetch failed url={url[:100]} err={exc!r}')
        return None
    if resp.status_code != 200:
        _log_warn(f'[news/fulltext] status={resp.status_code} url={url[:100]}')
        return None
    if not resp.encoding or resp.encoding.lower() == 'iso-8859-1':
        resp.encoding = resp.apparent_encoding or 'utf-8'
    return resp.text


def _published_iso(art):
    """published_at (unix int im Feed-Schema) → ISO-String für die Tabelle."""
    try:
        ts = art.get('published_at')
        if ts:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        pass
    return None


def _harvest_run(articles):
    """Thread-Body: Volltexte für neue Artikel permanent sichern.

    Zwei Wellen: (1) RSS-Volltexte upserten (0 Scrapes), (2) für Artikel ohne
    Volltext die Quellseite höflich scrapen (Whitelist, Cap, Delay). Ein
    Fehler pro Artikel bricht nie den Lauf; der Feed-Teaser bleibt Fallback.
    """
    try:
        allowed = _fulltext_allowed_hosts()
        by_url = {}
        for art in articles:
            url = (art.get('article_url') or '').strip()
            if not url or url in by_url:
                continue
            host = _fulltext_host(url)
            # Deny-Hosts KOMPLETT raus (weder scrapen noch RSS-persistieren):
            # Reuters/AvHerald-Terms verbieten Scraping/Republikation.
            if host not in allowed or host in _FULLTEXT_DENY_HOSTS:
                continue
            by_url[url] = art

        existing = _fulltext_store_get_many(list(by_url))
        todo_rss, todo_fetch = [], []
        for url, art in by_url.items():
            if url in existing:
                continue
            ft = art.get('fulltext')
            # RSS-Volltext nur DIREKT speichern, wenn er SUBSTANZIELL ist (echter
            # Volltext-Feed wie theaircurrent/austrianwings). Kurze RSS-Teaser
            # (simpleflying ~300–400, aero.de-Teaser) NICHT als „Volltext" cachen —
            # sonst sieht der Artikel in der App wie ein RSS-Feed aus (Owner 2026-07-10).
            # Stattdessen die volle Quellseite SCRAPEN (todo_fetch).
            if ft and len(ft) >= _FULLTEXT_RSS_PUT_MIN:
                todo_rss.append((url, art))
            else:
                todo_fetch.append((url, art))

        stored_rss = 0
        for url, art in todo_rss[:_FULLTEXT_HARVEST_MAX_RSS_PUTS]:
            if _fulltext_store_put(
                url, art.get('fulltext'),
                title=art.get('title'),
                image_url=art.get('image_url_original') or art.get('image_url'),
                published_at=_published_iso(art),
            ):
                stored_rss += 1

        # Neueste zuerst ernten — die tauchen im UI oben auf.
        todo_fetch.sort(key=lambda kv: kv[1].get('published_at') or 0, reverse=True)
        stored_scrape = 0
        fetched = 0
        for url, art in todo_fetch:
            if fetched >= _FULLTEXT_HARVEST_MAX_FETCHES:
                break
            # robots.txt der Quelle respektieren (per-Host gecacht; unklare
            # Lage ⇒ auslassen, Teaser + On-Demand-Reader bleiben).
            if not _robots_allows(url):
                continue
            fetched += 1
            html_doc = _harvest_fetch_article_html(url)
            if html_doc:
                ft = _extract_fulltext_for_harvest(html_doc, source_host=_fulltext_host(url))
                if ft and len(ft) >= _FULLTEXT_STORE_MIN_CHARS:
                    if _fulltext_store_put(
                        url, ft,
                        title=art.get('title'),
                        image_url=art.get('image_url_original') or art.get('image_url'),
                        published_at=_published_iso(art),
                    ):
                        stored_scrape += 1
            time.sleep(_FULLTEXT_HARVEST_DELAY_S)

        if stored_rss or stored_scrape or fetched:
            _logger.info(
                '[news/fulltext] harvest done rss_stored=%d scraped_stored=%d '
                'fetches=%d pending=%d', stored_rss, stored_scrape, fetched,
                max(0, len(todo_fetch) - fetched))
    except Exception as exc:
        _logger.warning('[news/fulltext] harvest run failed: %r', exc)
    finally:
        with _FULLTEXT_HARVEST_LOCK:
            _FULLTEXT_HARVEST_STATE['running'] = False


def _kickoff_fulltext_harvest(articles):
    """Startet den Harvest-Thread (non-blocking). Max. 1 Lauf gleichzeitig,
    min. 10min Abstand, Kill-Switch NEWS_FULLTEXT_HARVEST=0, no-op ohne SB."""
    if (os.environ.get('NEWS_FULLTEXT_HARVEST') or '1').strip() == '0':
        return
    sb, available = _debrief_get_sb()
    if not available or sb is None:
        return
    now = time.time()
    with _FULLTEXT_HARVEST_LOCK:
        st = _FULLTEXT_HARVEST_STATE
        if st['running'] or (now - st['last_run']) < _FULLTEXT_HARVEST_MIN_GAP_S:
            return
        st['running'] = True
        st['last_run'] = now
    # Shallow-Kopien — der Request-Thread mutiert die Artikel danach weiter
    # (relevance/is_own_airline), der Harvester liest nur seine Kopien.
    snapshot = [dict(a) for a in articles if isinstance(a, dict)]
    threading.Thread(
        target=_harvest_run, args=(snapshot,),
        daemon=True, name='news-fulltext-harvest',
    ).start()


def _attach_stored_fulltexts(articles):
    """Hängt gespeicherte Volltexte an Feed-Artikel OHNE RSS-Volltext
    (Serve-TTL + Deny-Hosts erzwingt `_fulltext_store_get_many`).

    EIN chunked Supabase-Roundtrip pro Feed-Rebuild (Cache-Miss-Pfad, ≤1×/15min).
    Mutiert die Artikel in-place: fulltext (gecappt wie der RSS-Pfad) +
    in_app_readable, damit ?readable_only=1 die Artikel nicht mehr aussiebt.
    Wirft nie — SB-down ⇒ Feed unverändert (Teaser + On-Demand-Reader bleiben).
    """
    need = [a for a in articles
            if not a.get('fulltext') and (a.get('article_url') or '').strip()]
    if not need:
        return
    found = _fulltext_store_get_many([a['article_url'] for a in need])
    if not found:
        return
    for art in need:
        ft = found.get(art.get('article_url'))
        if not ft:
            continue
        # AUCH gespeicherten (evtl. vor dem Cruft-Fix gecachten) Text putzen —
        # sonst überschreibt alter Cruft-Cache den frisch gestrippten RSS-Text.
        try:
            ft = _strip_feed_cruft(ft, source_name=art.get('source_name'))
        except Exception:
            pass
        art['fulltext'] = ft[:_FULLTEXT_FEED_CAP]
        if len(ft) >= _FULLTEXT_READABLE_MIN_CHARS:
            art['in_app_readable'] = True


# ══════════════════════════════════════════════════════════════════
#  Community Debrief Board  (anonymes Incident-Debrief-Forum)
#
#  Der iOS-News-Tab ("Community Debrief") ruft drei Endpunkte:
#      GET  /api/news/debrief                 → { items: [post...] }
#      POST /api/news/debrief                 → erstellter Post (IncidentDebriefPost)
#      POST /api/news/debrief/<id>/upvote     → { upvotes, did_upvote }
#
#  Response-Shape spiegelt EXAKT die Swift-Codable IncidentDebriefPost
#  (NewsArticle.swift): id, created_at, pseudonym, poster_role, body,
#  hashtags, upvotes, did_upvote, comment_count.
#
#  Persistenz: Supabase-primär (Tabellen `debrief_posts` + `debrief_upvotes`),
#  Disk-Fallback (JSON im _USER_HISTORY_DIR) damit der Cloud-Run-Ephemeral-FS
#  nicht alles verliert. Spiegelt das Pattern aus trip_trade_blueprint.py.
#
#  Anonym: Lesen ohne Auth. Posten/Upvoten verlangt ein Bearer-Token
#  (Authorization-Header), aus dem ein deterministisches Pseudonym
#  ("Crew #NNNN") abgeleitet wird — niemals der echte Token/Username.
# ══════════════════════════════════════════════════════════════════

# Allowlist — IDENTISCH zur iOS-`DebriefHashtag`-Enum (NewsArticle.swift).
# Wenn sich eine Seite ändert, beide anpassen.
_DEBRIEF_HASHTAG_ALLOWLIST = {
    '#go-around',
    '#diversion',
    '#engine-issue',
    '#medical',
    '#tcas-ra',
    '#turbulence',
    '#bird-strike',
    '#lightning-strike',
    '#tech-stop',
    '#decompression',
    '#fuel-emergency',
    '#rejected-takeoff',
    '#smoke',
    '#unruly-pax',
    '#weather-delay',
}

# Poster-Rollen die wir akzeptieren (alles andere → None). Frei, aber kurz.
_DEBRIEF_ROLE_ALLOWLIST = {'CC', 'FO', 'CPT', 'PURSER'}

_DEBRIEF_BODY_MIN = 50
_DEBRIEF_BODY_MAX = 2000
_DEBRIEF_POST_LIMIT_PER_DAY = 3
_DEBRIEF_DEFAULT_PAGE = 50
_DEBRIEF_MAX_PAGE = 100

_DEBRIEF_DISK_LOCK = threading.Lock()


# ─── Lazy app-Module / Supabase access (Pattern aus trip_trade) ─────

def _debrief_get_app_module():
    try:
        import app as _app_module  # noqa: F401
        return _app_module
    except Exception:
        return None


def _debrief_get_sb():
    """Returns (sb_client, available_bool). (None, False) bei Importfehler."""
    m = _debrief_get_app_module()
    if m is None:
        return None, False
    return getattr(m, 'sb', None), bool(getattr(m, 'SB_AVAILABLE', False))


def _debrief_history_dir():
    m = _debrief_get_app_module()
    if m is not None:
        d = getattr(m, '_USER_HISTORY_DIR', None)
        if d:
            return d
    # TODO: Wenn app.py nicht ladbar ist (z.B. isolierter Blueprint-Test) fällt
    # das auf ein relatives Verzeichnis zurück — auf Cloud Run ist das ephemer.
    # Produktiv kommt der Pfad immer aus app._USER_HISTORY_DIR + SB ist Wahrheit.
    return '_user_history_state'


def _debrief_rate_limited(token, limit, window_sec):
    """True wenn Token sein Limit erreicht hat. Nutzt app._token_rate_limited
    wenn vorhanden, sonst lokales Sliding-Window-Bucket."""
    if not token:
        return False
    m = _debrief_get_app_module()
    if m is not None:
        fn = getattr(m, '_token_rate_limited', None)
        if callable(fn):
            try:
                return bool(fn(token, 'news_debrief_post', limit, window_sec))
            except Exception:
                pass
    now = time.time()
    cutoff = now - window_sec
    key = f'debrief:{token}'
    with _DEBRIEF_DISK_LOCK:
        bucket = _DEBRIEF_RATE_BUCKETS.setdefault(key, [])
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        if len(_DEBRIEF_RATE_BUCKETS) > 5000:
            for k in list(_DEBRIEF_RATE_BUCKETS.keys())[:2500]:
                _DEBRIEF_RATE_BUCKETS.pop(k, None)
        return False


_DEBRIEF_RATE_BUCKETS = {}


# ─── Token-Helpers ──────────────────────────────────────────────────

def _debrief_extract_token():
    """Holt das Bearer-Token aus dem Authorization-Header. None wenn keins."""
    auth = request.headers.get('Authorization', '') or ''
    if auth.lower().startswith('bearer '):
        tok = auth[7:].strip()
        return tok or None
    return None


def _debrief_valid_token(token):
    if not isinstance(token, str):
        return False
    t = token.strip()
    return bool(re.match(r'^[A-Za-z0-9_\-]{8,128}$', t))


def _debrief_pseudonym(token):
    """Deterministisches anonymes Pseudonym 'Crew #NNNN' aus Token-Hash.
    Gleiches Token → gleiches Pseudonym, aber nicht rückführbar auf das Token."""
    if not token:
        return 'Crew'
    h = hashlib.sha256(token.encode('utf-8')).hexdigest()
    num = int(h[:6], 16) % 9000 + 1000  # 1000..9999
    return f'Crew #{num}'


def _debrief_upvoter_hash(token):
    """Stabiler, nicht-rückführbarer Hash des Upvoter-Tokens für Idempotenz."""
    return hashlib.sha256(('upvote:' + (token or '')).encode('utf-8')).hexdigest()[:32]


# ─── Disk-Persistenz ────────────────────────────────────────────────

def _debrief_disk_path():
    d = _debrief_history_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, 'debrief_posts.json')


def _debrief_load_disk():
    p = _debrief_disk_path()
    try:
        with open(p) as f:
            data = json.load(f) or []
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _debrief_save_disk(posts):
    p = _debrief_disk_path()
    try:
        with _DEBRIEF_DISK_LOCK:
            with open(p, 'w') as f:
                json.dump(posts[-10000:], f, ensure_ascii=False, default=str)
        return True
    except Exception as e:
        _log_warn(f'[debrief] disk_save_fail err={type(e).__name__}: {str(e)[:200]}')
        return False


# ─── Supabase-Persistenz ────────────────────────────────────────────

def _debrief_sb_insert(row):
    # Upsert mit on_conflict='id' (idempotent wie license_wallet/layover_recs):
    # gleicher Post-Insert bleibt ohne Dupe-Fehler, und der lazy-migrate-Pfad
    # kann Disk-Rows ohne Kollision hochschreiben.
    sb, available = _debrief_get_sb()
    if not available or sb is None:
        return False
    try:
        sb.table('debrief_posts').upsert(row, on_conflict='id').execute()
        return True
    except Exception as e:
        _log_warn(f'[debrief] sb_insert_fail err={type(e).__name__}: {str(e)[:200]}')
        return False


def _debrief_sb_bulk_upsert(rows):
    """Bulk-Upsert für lazy-migrate (Disk → SB). True nur wenn der Batch hält."""
    sb, available = _debrief_get_sb()
    if not available or sb is None:
        return False
    clean = [r for r in (rows or []) if isinstance(r, dict) and r.get('id')]
    if not clean:
        return False
    try:
        sb.table('debrief_posts').upsert(clean, on_conflict='id').execute()
        return True
    except Exception as e:
        _log_warn(f'[debrief] sb_bulk_upsert_fail err={type(e).__name__}: {str(e)[:200]}')
        return False


def _debrief_sb_list(before_iso=None, limit=50):
    """Neueste zuerst. None bei SB-down → Caller fällt auf Disk zurück."""
    sb, available = _debrief_get_sb()
    if not available or sb is None:
        return None
    try:
        q = (sb.table('debrief_posts')
             .select('*')
             .eq('deleted', False)
             .order('created_at', desc=True))
        if before_iso:
            q = q.lt('created_at', before_iso)
        r = q.limit(limit).execute()
        return list(r.data or [])
    except Exception as e:
        _log_warn(f'[debrief] sb_list_fail err={type(e).__name__}: {str(e)[:200]}')
        return None


def _debrief_sb_get(post_id):
    sb, available = _debrief_get_sb()
    if not available or sb is None:
        return None
    try:
        r = sb.table('debrief_posts').select('*').eq('id', post_id).limit(1).execute()
        data = list(r.data or [])
        return data[0] if data else None
    except Exception:
        return None


def _debrief_sb_set_upvotes(post_id, upvotes, upvoters):
    sb, available = _debrief_get_sb()
    if not available or sb is None:
        return False
    try:
        sb.table('debrief_posts').update({
            'upvotes': upvotes,
            'upvoters': upvoters,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', post_id).execute()
        return True
    except Exception as e:
        _log_warn(f'[debrief] sb_upvote_fail id={post_id} err={type(e).__name__}')
        return False


# ─── Persist-Layer (SB primär, Disk-Spiegel) ────────────────────────

def _debrief_persist_new(row):
    """Insert in SB + Disk-Spiegel. True wenn mind. ein Pfad hielt."""
    sb_ok = _debrief_sb_insert(row)
    with _DEBRIEF_DISK_LOCK:
        posts = _debrief_load_disk()
        posts.append(row)
    disk_ok = _debrief_save_disk(posts)
    return sb_ok or disk_ok


def _debrief_disk_sorted(before_iso=None, limit=50):
    posts = [p for p in _debrief_load_disk()
             if isinstance(p, dict) and not p.get('deleted')]
    posts.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    if before_iso:
        posts = [p for p in posts if str(p.get('created_at') or '') < before_iso]
    return posts[:limit]


def _debrief_list(before_iso=None, limit=50):
    """SB primär, Disk-Fallback. Liefert eine sortierte (neueste zuerst) Liste.

    Lazy-Migrate (wie license_wallet): SB up, aber leer + Disk hat alte Posts
    (Hinterlassenschaft der Disk-only-Ära vor der debrief_posts-Tabelle) →
    einmalig hochschreiben, dann kanonisch aus SB re-lesen."""
    sb_rows = _debrief_sb_list(before_iso=before_iso, limit=limit)
    if sb_rows is not None:
        if not sb_rows and not before_iso:
            disk_raw = [p for p in _debrief_load_disk()
                        if isinstance(p, dict) and p.get('id')]
            if disk_raw:
                _log_warn(f'[debrief] lazy_migrate disk→sb count={len(disk_raw)}')
                if _debrief_sb_bulk_upsert(disk_raw):
                    re_read = _debrief_sb_list(before_iso=before_iso, limit=limit)
                    if re_read is not None:
                        return re_read
                # Migrate fehlgeschlagen → wenigstens die Disk-Posts ausliefern
                return _debrief_disk_sorted(before_iso=before_iso, limit=limit)
        return sb_rows
    return _debrief_disk_sorted(before_iso=before_iso, limit=limit)


def _debrief_apply_upvote(post_id, upvoter_hash):
    """Idempotenter Toggle. Returns (upvotes:int, did_upvote:bool) oder None
    wenn der Post nicht existiert."""
    # SB primär laden
    post = _debrief_sb_get(post_id)
    from_disk = False
    if post is None:
        with _DEBRIEF_DISK_LOCK:
            posts = _debrief_load_disk()
        for p in posts:
            if isinstance(p, dict) and p.get('id') == post_id and not p.get('deleted'):
                post = p
                from_disk = True
                break
    if post is None:
        return None

    upvoters = list(post.get('upvoters') or [])
    if upvoter_hash in upvoters:
        upvoters.remove(upvoter_hash)
        did_upvote = False
    else:
        upvoters.append(upvoter_hash)
        did_upvote = True
    upvotes = len(upvoters)

    _debrief_sb_set_upvotes(post_id, upvotes, upvoters)
    # Disk-Spiegel immer aktualisieren (auch wenn Quelle SB war)
    with _DEBRIEF_DISK_LOCK:
        posts = _debrief_load_disk()
        changed = False
        for p in posts:
            if isinstance(p, dict) and p.get('id') == post_id:
                p['upvotes'] = upvotes
                p['upvoters'] = upvoters
                p['updated_at'] = datetime.now(timezone.utc).isoformat()
                changed = True
                break
        if changed:
            with open(_debrief_disk_path(), 'w') as f:
                json.dump(posts[-10000:], f, ensure_ascii=False, default=str)
    return upvotes, did_upvote


# ─── Serialisierung → IncidentDebriefPost-Shape (Swift Codable) ─────

def _debrief_to_client(row, viewer_upvoter_hash=None):
    """Mapped eine Storage-Row auf das EXAKTE iOS-IncidentDebriefPost-JSON."""
    upvoters = row.get('upvoters') or []
    did_upvote = bool(viewer_upvoter_hash and viewer_upvoter_hash in upvoters)
    return {
        'id': str(row.get('id') or ''),
        'created_at': row.get('created_at') or '',
        'pseudonym': row.get('pseudonym') or 'Crew',
        'poster_role': row.get('poster_role'),  # nullable → Swift posterRole: String?
        'body': row.get('body') or '',
        'hashtags': list(row.get('hashtags') or []),
        'upvotes': int(row.get('upvotes') or len(upvoters) or 0),
        'did_upvote': did_upvote,
        'comment_count': int(row.get('comment_count') or 0),
    }


def _debrief_sanitize_hashtags(raw):
    """Nur Allowlist-Tags, dedupe, max 5. Case-insensitive normalisiert."""
    out = []
    for h in (raw or []):
        if not isinstance(h, str):
            continue
        tag = h.strip().lower()
        if not tag.startswith('#'):
            tag = '#' + tag
        if tag in _DEBRIEF_HASHTAG_ALLOWLIST and tag not in out:
            out.append(tag)
        if len(out) >= 5:
            break
    return out


# ─── Routes ─────────────────────────────────────────────────────────

@news_bp.route('/api/news/debrief', methods=['GET'])
def get_news_debrief():
    """Community-Debrief-Feed. Public-Read (kein Token erforderlich).

    Query:
      limit   — 1..100 (default 50)
      before  — ISO8601-Cursor (createdAt des letzten gesehenen Posts) für
                Infinite-Scroll; liefert nur ältere Posts.

    Antwort: { "items": [IncidentDebriefPost, ...] }  (leer → { "items": [] }).
    `did_upvote` wird gegen das optionale Bearer-Token des Viewers berechnet.
    """
    try:
        limit = int(request.args.get('limit', str(_DEBRIEF_DEFAULT_PAGE)))
    except (TypeError, ValueError):
        limit = _DEBRIEF_DEFAULT_PAGE
    limit = max(1, min(_DEBRIEF_MAX_PAGE, limit))

    before_iso = (request.args.get('before') or '').strip() or None

    viewer_token = _debrief_extract_token()
    viewer_hash = _debrief_upvoter_hash(viewer_token) if viewer_token else None

    rows = _debrief_list(before_iso=before_iso, limit=limit)
    items = [_debrief_to_client(r, viewer_hash) for r in rows if isinstance(r, dict)]
    return jsonify({'items': items}), 200


@news_bp.route('/api/news/debrief', methods=['POST'])
def post_news_debrief():
    """Erstellt einen anonymen Debrief-Post. Verlangt Bearer-Token.

    Body: { body: str (50..2000), hashtags: [allowlist], poster_role: str? }
    Rate-Limit: 3 Posts / 24h / Token.
    Antwort: der erstellte IncidentDebriefPost (Swift-Shape), HTTP 200.
    """
    token = _debrief_extract_token()
    if not _debrief_valid_token(token):
        return jsonify({'ok': False, 'error': 'Auth-Token fehlt oder ungültig.'}), 401

    if _debrief_rate_limited(token, _DEBRIEF_POST_LIMIT_PER_DAY, 86400):
        return jsonify({
            'ok': False,
            'error': 'Tageslimit erreicht (max. 3 Debrief-Posts pro Tag).',
        }), 429

    payload = request.get_json(silent=True) or {}
    body_text = payload.get('body')
    if not isinstance(body_text, str):
        body_text = ''
    body_text = body_text.strip()
    if len(body_text) < _DEBRIEF_BODY_MIN:
        return jsonify({
            'ok': False,
            'error': f'Text zu kurz (min. {_DEBRIEF_BODY_MIN} Zeichen).',
        }), 400
    if len(body_text) > _DEBRIEF_BODY_MAX:
        body_text = body_text[:_DEBRIEF_BODY_MAX]

    hashtags = _debrief_sanitize_hashtags(payload.get('hashtags'))

    poster_role = payload.get('poster_role')
    if isinstance(poster_role, str):
        poster_role = poster_role.strip().upper() or None
        if poster_role not in _DEBRIEF_ROLE_ALLOWLIST:
            poster_role = None
    else:
        poster_role = None

    now_iso = datetime.now(timezone.utc).isoformat()
    post_id = uuid.uuid4().hex
    row = {
        'id': post_id,
        'author_token_hash': _debrief_upvoter_hash(token),  # nie das Klartext-Token speichern
        'pseudonym': _debrief_pseudonym(token),
        'poster_role': poster_role,
        'body': body_text,
        'hashtags': hashtags,
        'upvotes': 0,
        'upvoters': [],
        'comment_count': 0,
        'deleted': False,
        'created_at': now_iso,
        'updated_at': now_iso,
    }
    ok = _debrief_persist_new(row)
    if not ok:
        return jsonify({'ok': False, 'error': 'Speichern fehlgeschlagen.'}), 500

    _log_warn(f'[debrief] post_created id={post_id} tags={hashtags}')
    # Viewer = Author → did_upvote ist False (hat sich selbst nicht upgevotet)
    return jsonify(_debrief_to_client(row, _debrief_upvoter_hash(token))), 200


@news_bp.route('/api/news/debrief/<post_id>/upvote', methods=['POST'])
def upvote_news_debrief(post_id):
    """Idempotenter Upvote-Toggle. Verlangt Bearer-Token.

    Antwort: { ok: true, upvotes: N, did_upvote: bool }.
    Gleiches Token zweimal → toggelt zurück (kein Doppel-Vote).
    """
    token = _debrief_extract_token()
    if not _debrief_valid_token(token):
        return jsonify({'ok': False, 'error': 'Auth-Token fehlt oder ungültig.'}), 401

    if not post_id or not re.match(r'^[A-Za-z0-9_\-]{1,64}$', post_id):
        return jsonify({'ok': False, 'error': 'Ungültige Post-ID.'}), 400

    if _debrief_rate_limited('upvote:' + token, 120, 3600):
        return jsonify({'ok': False, 'error': 'Zu viele Upvotes — kurz warten.'}), 429

    result = _debrief_apply_upvote(post_id, _debrief_upvoter_hash(token))
    if result is None:
        return jsonify({'ok': False, 'error': 'Post nicht gefunden.'}), 404

    upvotes, did_upvote = result
    return jsonify({'ok': True, 'upvotes': upvotes, 'did_upvote': did_upvote}), 200
