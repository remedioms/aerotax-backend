"""Link-Vorschau `/api/user/link-preview/<token>` (Owner-Auftrag 2026-08-01).

Baut auf dem bestehenden OpenGraph-Reader `_news_extract_metadata` auf (kein
zweiter Parser) — der Kern dieser Tests ist die SSRF-Härtung, weil der Server
hier eine vom USER eingegebene URL im eigenen Auftrag abruft:

  1. Scheme-/IP-Block (`_link_preview_host_blocked`) — auch für JEDEN
     Redirect-Hop, nicht nur die Erst-URL.
  2. Zeit-/Größenlimit + Content-Type-Gate (`_link_preview_fetch_html`).
  3. Der Endpoint selbst: Auth/Rate-Limit/Cache/Response-Shape.

KEIN echter Netzzugriff in diesen Tests — `_link_preview_http_get` ist die
EINE Stelle, die gemockt wird (Muster wie `_fetch_calendar_feed_text`).
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A


TOKEN = 'AT-LINKPREVIEW0000001'


def _clear_cache():
    with A._LINK_PREVIEW_LOCK:
        A._LINK_PREVIEW_MEMO.clear()


class _FakeResp:
    """Minimaler requests.Response-Stub: status_code, headers, iter_content,
    encoding, close()."""

    def __init__(self, status_code=200, headers=None, body=b'', encoding='utf-8'):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size=65536):
        b = self._body
        for i in range(0, len(b), chunk_size):
            yield b[i:i + chunk_size]

    def close(self):
        self.closed = True


# ── 1. _news_extract_metadata: additive site_name/description ─────────────

def test_news_extract_metadata_liefert_site_name_und_description():
    html = (
        '<html><head>'
        '<meta property="og:title" content="Titel">'
        '<meta property="og:site_name" content="Aero Times">'
        '<meta property="og:description" content="Kurzbeschreibung &amp; mehr">'
        '</head><body></body></html>'
    )
    out = A._news_extract_metadata(html)
    assert out['title'] == 'Titel'
    assert out['site_name'] == 'Aero Times'
    assert out['description'] == 'Kurzbeschreibung & mehr'


def test_news_extract_metadata_description_fallback_kaskade():
    html = ('<html><head>'
            '<meta name="description" content="Nur die Meta-Description">'
            '</head></html>')
    out = A._news_extract_metadata(html)
    assert out['description'] == 'Nur die Meta-Description'


def test_news_extract_metadata_ohne_og_bleibt_none():
    out = A._news_extract_metadata('<html><body>Kein Meta</body></html>')
    assert out['site_name'] is None
    assert out['description'] is None


# ── 2. SSRF-Block: Scheme + private/loopback/link-local/multicast/reserved ─

def test_blockt_falsches_scheme():
    assert A._link_preview_host_blocked('file:///etc/passwd') is True
    assert A._link_preview_host_blocked('ftp://example.com/x') is True
    assert A._link_preview_host_blocked('javascript:alert(1)') is True


def test_blockt_leere_oder_kaputte_url():
    assert A._link_preview_host_blocked('') is True
    assert A._link_preview_host_blocked('not a url') is True
    assert A._link_preview_host_blocked('https://') is True


def test_blockt_loopback_und_private_literal_ips():
    for host in ('http://127.0.0.1/x', 'https://10.0.0.5/x',
                 'https://172.16.4.4/x', 'https://192.168.1.1/x',
                 'https://169.254.169.254/latest/meta-data/',  # Cloud-Metadata
                 'https://0.0.0.0/x', 'https://[::1]/x', 'https://[fc00::1]/x',
                 'https://[fe80::1]/x'):
        assert A._link_preview_host_blocked(host) is True, host


def test_erlaubt_oeffentliche_ip_ueber_gemockte_dns():
    with patch('socket.getaddrinfo',
               return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
        assert A._link_preview_host_blocked('https://example.test/artikel') is False


def test_gemischte_a_records_werden_geblockt():
    """Ein öffentlicher UND ein privater A-Record → blocken (ALLE IPs prüfen,
    nicht nur die erste)."""
    with patch('socket.getaddrinfo', return_value=[
        (2, 1, 6, '', ('93.184.216.34', 0)),
        (2, 1, 6, '', ('169.254.169.254', 0)),
    ]):
        assert A._link_preview_host_blocked('https://example.test/x') is True


# ── 3. _link_preview_fetch_html: Redirects, Größe, Content-Type ───────────

def test_nicht_html_content_type_liefert_not_html():
    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get',
                      return_value=_FakeResp(200, {'Content-Type': 'application/pdf'})):
        html, err = A._link_preview_fetch_html('https://example.test/x.pdf')
    assert html is None
    assert err == 'not_html'


def test_redirect_wird_bis_max_gefolgt_und_dann_abgebrochen():
    calls = []

    def _fake_get(url, timeout):
        calls.append(url)
        return _FakeResp(302, {'Location': f'https://example.test/hop{len(calls)}'})

    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get', side_effect=_fake_get):
        html, err = A._link_preview_fetch_html('https://example.test/start')
    assert html is None
    assert err == 'too_many_redirects'
    # 1 Erst-Request + max 3 Redirect-Hops = 4 Requests, NICHT mehr.
    assert len(calls) == A._LINK_PREVIEW_MAX_REDIRECTS + 1


def test_redirect_ziel_wird_erneut_gegen_ssrf_geprueft():
    """Ein Redirect auf ein internes Ziel wird NICHT verfolgt — jeder Hop
    bekommt seine eigene Host-Prüfung."""
    def _blocked(url):
        return 'internal.evil' in url

    with patch.object(A, '_link_preview_host_blocked', side_effect=_blocked), \
         patch.object(A, '_link_preview_http_get',
                      return_value=_FakeResp(
                          302, {'Location': 'https://internal.evil/secret'})) as mocked_get:
        html, err = A._link_preview_fetch_html('https://example.test/start')
    assert html is None
    assert err == 'internal_host_blocked'
    # Der geblockte Redirect-Ziel-Host wurde NIE tatsächlich angefragt.
    mocked_get.assert_called_once_with('https://example.test/start', A._LINK_PREVIEW_TIMEOUT)


def test_response_ueber_limit_wird_abgebrochen_beim_lesen():
    # GEAENDERT 2026-08-01: eine zu grosse Seite wird nicht mehr verworfen,
    # sondern bis zum Limit gelesen und ausgewertet. Grund (live gemessen):
    # tagesschau.de, Wikipedia und airliners.de liegen ALLE ueber 512 KB — mit
    # dem alten Verhalten war die Vorschau fuer echte Nachrichtenseiten immer
    # leer. Die og:-Angaben stehen im <head>, also am Anfang.
    kopf = b'<html><head><meta property="og:title" content="Grosse Seite">'
    big_body = kopf + (b'x' * (A._LINK_PREVIEW_MAX_BYTES + 1000)) + b'</html>'
    resp = _FakeResp(200, {'Content-Type': 'text/html; charset=utf-8'}, body=big_body)
    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get', return_value=resp):
        html, err = A._link_preview_fetch_html('https://example.test/huge')
    assert err is None
    assert html is not None
    # Gedeckelt: wir laden NIE mehr als das Limit.
    assert len(html.encode('utf-8', errors='replace')) <= A._LINK_PREVIEW_MAX_BYTES
    # Und der Kopf ist trotzdem auswertbar.
    assert A._news_extract_metadata(html)['title'] == 'Grosse Seite'


def test_grosse_content_length_blockiert_nicht_mehr():
    """Eine angekuendigte Groesse knapp ueber dem Limit darf die Vorschau NICHT
    mehr verhindern — genau daran scheiterten die echten Nachrichtenseiten."""
    headers = {'Content-Type': 'text/html',
               'Content-Length': str(A._LINK_PREVIEW_MAX_BYTES + 1)}
    resp = _FakeResp(200, headers, body=b'<html><head><title>Da</title></head></html>')
    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get', return_value=resp):
        html, err = A._link_preview_fetch_html('https://example.test/huge')
    assert err is None and html is not None


def test_absurde_content_length_wird_weiter_abgewiesen():
    """Der Schutz bleibt — nur die Schwelle sitzt jetzt dort, wo es wirklich
    kein HTML-Dokument mehr sein kann."""
    headers = {'Content-Type': 'text/html',
               'Content-Length': str(A._LINK_PREVIEW_ABSURD_BYTES + 1)}
    resp = _FakeResp(200, headers, body=b'<html></html>')
    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get', return_value=resp):
        html, err = A._link_preview_fetch_html('https://example.test/absurd')
    assert html is None and err == 'too_large'


def test_erfolgreicher_fetch_liefert_html():
    body = b'<html><head><title>Hallo</title></head></html>'
    resp = _FakeResp(200, {'Content-Type': 'text/html; charset=utf-8'}, body=body)
    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get', return_value=resp):
        html, err = A._link_preview_fetch_html('https://example.test/ok')
    assert err is None
    assert '<title>Hallo</title>' in html


def test_fetch_exception_liefert_fetch_failed():
    with patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_http_get', side_effect=RuntimeError('boom')):
        html, err = A._link_preview_fetch_html('https://example.test/boom')
    assert html is None
    assert err == 'fetch_failed'


def test_http_get_wrapper_folgt_keinen_redirects_selbst():
    """`allow_redirects=False` ist load-bearing: sonst würde requests selbst
    einem Redirect blind folgen, BEVOR unser Host-Check je den Ziel-Host sieht."""
    with patch('requests.get') as mocked:
        mocked.return_value = _FakeResp(200)
        A._link_preview_http_get('https://example.test/x', 6.0)
        _, kwargs = mocked.call_args
        assert kwargs['allow_redirects'] is False
        assert kwargs['timeout'] == 6.0
        assert 'AeroX' in kwargs['headers']['User-Agent']


# ── 4. Der Endpoint als Ganzes ─────────────────────────────────────────────

def test_ungueltiges_token_liefert_404():
    _clear_cache()
    with A.app.test_request_context(f'/api/user/link-preview/{TOKEN}?url=https://x.test'), \
         patch.object(A, '_validate_token_exists', return_value=None):
        resp = A.get_link_preview(TOKEN)
    assert resp[1] == 404


def test_rate_limit_liefert_429():
    _clear_cache()
    with A.app.test_request_context(f'/api/user/link-preview/{TOKEN}?url=https://x.test'), \
         patch.object(A, '_validate_token_exists', return_value='mail@example.com'), \
         patch.object(A, '_token_rate_limited', return_value=True):
        resp = A.get_link_preview(TOKEN)
    assert resp[1] == 429


def test_fehlende_url_liefert_400():
    _clear_cache()
    with A.app.test_request_context(f'/api/user/link-preview/{TOKEN}'), \
         patch.object(A, '_validate_token_exists', return_value='mail@example.com'), \
         patch.object(A, '_token_rate_limited', return_value=False):
        resp = A.get_link_preview(TOKEN)
    assert resp[1] == 400


def test_geblockte_url_liefert_leere_aber_ok_vorschau_ohne_fetch():
    _clear_cache()
    with A.app.test_request_context(
            f'/api/user/link-preview/{TOKEN}?url=http://169.254.169.254/latest/meta-data/'), \
         patch.object(A, '_validate_token_exists', return_value='mail@example.com'), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_link_preview_fetch_html') as mocked_fetch:
        resp = A.get_link_preview(TOKEN)
    body = resp.get_json()
    assert body['ok'] is True
    assert body['title'] is None and body['image_url'] is None
    assert body['site_name'] is None and body['description'] is None
    mocked_fetch.assert_not_called()


def test_erfolgreiche_vorschau_nutzt_news_extract_metadata():
    _clear_cache()
    url = 'https://example.test/artikel'
    html = ('<html><head><meta property="og:title" content="Mein Titel">'
            '<meta property="og:image" content="https://example.test/bild.jpg">'
            '<meta property="og:site_name" content="Example News">'
            '<meta property="og:description" content="Beschreibung">'
            '</head></html>')
    with A.app.test_request_context(f'/api/user/link-preview/{TOKEN}?url={url}'), \
         patch.object(A, '_validate_token_exists', return_value='mail@example.com'), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_fetch_html', return_value=(html, None)):
        resp = A.get_link_preview(TOKEN)
    body = resp.get_json()
    assert body == {
        'ok': True, 'url': url, 'title': 'Mein Titel',
        'image_url': 'https://example.test/bild.jpg',
        'site_name': 'Example News', 'description': 'Beschreibung',
    }


def test_zweiter_aufruf_kommt_aus_dem_cache_ohne_erneuten_fetch():
    _clear_cache()
    url = 'https://example.test/gecacht'
    html = '<html><head><title>Cached</title></head></html>'
    with A.app.test_request_context(f'/api/user/link-preview/{TOKEN}?url={url}'), \
         patch.object(A, '_validate_token_exists', return_value='mail@example.com'), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_link_preview_host_blocked', return_value=False), \
         patch.object(A, '_link_preview_fetch_html', return_value=(html, None)) as fetch1:
        A.get_link_preview(TOKEN)
    assert fetch1.call_count == 1
    with A.app.test_request_context(f'/api/user/link-preview/{TOKEN}?url={url}'), \
         patch.object(A, '_validate_token_exists', return_value='mail@example.com'), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_link_preview_fetch_html') as fetch2:
        resp = A.get_link_preview(TOKEN)
    fetch2.assert_not_called()
    assert resp.get_json()['title'] == 'Cached'


def test_cache_deckel_leert_bei_ueberschreitung():
    _clear_cache()
    for i in range(A._LINK_PREVIEW_MAX_ENTRIES + 5):
        A._link_preview_cache_put(f'https://example.test/{i}',
                                  {'ok': True, 'url': f'https://example.test/{i}',
                                   'title': None, 'image_url': None,
                                   'site_name': None, 'description': None})
    assert len(A._LINK_PREVIEW_MEMO) <= A._LINK_PREVIEW_MAX_ENTRIES + 1


def test_route_ist_owner_scoped_registriert():
    """Der Prefix muss in _BUG004_GET_PII_PREFIXES stehen, sonst greift das
    Bearer-Binding-Gate für diesen GET-Endpoint nicht."""
    assert any(p == '/api/user/link-preview/' for p in A._BUG004_GET_PII_PREFIXES)
