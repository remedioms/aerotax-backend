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
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        # limit auch im cache-hit-pfad anwenden (verschiedene Clients,
        # verschiedene Limits — der Cache hält die volle Liste).
        payload['articles'] = payload.get('articles', [])[:limit]
        payload['count'] = len(payload['articles'])
        return jsonify(payload)

    started = time.time()
    aggregated, source_status = _aggregate_all_sources()

    # Per-Article Klassifikation + Airline-Tagging (vor Filter — damit der
    # Cache-Eintrag auch für andere Airline-Filter wiederverwendbar wäre,
    # falls wir später einen "all"-Cache einbauen).
    enriched = []
    for art in aggregated:
        cat = _classify_category(art.get('title', ''), art.get('summary', ''))
        mentions = _extract_mentioned_airlines(art.get('title', ''), art.get('summary', ''))
        art['category'] = cat
        art['mentioned_airlines'] = mentions
        enriched.append(art)

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

    payload = {
        'ok': True,
        'articles': filtered[:limit],
        'count': min(len(filtered), limit),
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
    if len(summary) > 300:
        summary = summary[:297].rstrip() + '...'

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
        'mentioned_airlines': [],  # wird im Caller gefüllt
        'category': _DEFAULT_CATEGORY,  # wird im Caller überschrieben
    }


def _entry_unix_ts(entry):
    """Parsed published/updated parsed-time aus feedparser; fallback now."""
    for key in ('published_parsed', 'updated_parsed', 'created_parsed'):
        struct_t = entry.get(key)
        if struct_t:
            try:
                return int(time.mktime(struct_t))
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


def _log_warn(msg):
    """Logging über current_app wenn im Request-Context, sonst logger."""
    try:
        current_app.logger.warning(msg)
    except Exception:
        _logger.warning(msg)
