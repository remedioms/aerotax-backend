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
from datetime import datetime, timezone

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
    fulltext = _strip_html(content_raw).strip()
    # „In-App lesbar" = es gibt einen echten Volltext im RSS ODER die
    # Zusammenfassung ist lang genug für einen sinnvollen Read. Reine
    # Ein-Satz-Teaser (z.B. „Spezifisches Angebot für Kunden in China"), die
    # nur „Im Browser öffnen" anbieten, sind NICHT in-app lesbar → werden bei
    # ?readable_only=1 aus dem Feed gefiltert (User: „keine News die nicht
    # direkt in der App lesbar sind").
    in_app_readable = len(fulltext) >= 400 or full_summary_len >= 140

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
