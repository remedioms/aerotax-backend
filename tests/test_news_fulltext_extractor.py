"""Tests für den News-Volltext-Extraktor (blueprints/news_blueprint.py).

Reine Funktionen, keine Netz-/DB-Zugriffe:
  * _extract_fulltext_pure     — größter <article>/<p>-Cluster → Plain-Text
  * _html_to_paragraph_text    — HTML-Fragment → Text MIT Absatz-Struktur
  * _attach_stored_fulltexts   — Feed-Anreicherung (Store gemockt)

Kontext: permanenter Volltext-Layer 2026-07-10 (Owner: „Text soll gespeichert
sein und direkt voll da sein").
"""
import blueprints.news_blueprint as nb


# ── Fixtures ────────────────────────────────────────────────────────

_PARA = (
    'Die Lufthansa hat am Donnerstag angekündigt, ihre Langstreckenflotte '
    'weiter zu modernisieren und zusätzliche Maschinen zu bestellen.'
)

# Fixture 1: WordPress-artige Artikel-Seite mit Nav/Sidebar/Footer-Boilerplate.
FIXTURE_WORDPRESS = f"""
<html><head><title>Test</title>
<script>var tracking = 'x';</script>
<style>.a {{ color: red; }}</style>
</head><body>
<nav><ul><li>Home</li><li>News</li><li>Flotte</li></ul></nav>
<header><h1>Seiten-Header</h1></header>
<article>
  <h2>Lufthansa modernisiert Flotte</h2>
  <p>{_PARA}</p>
  <p>Der Konzern begründet den Schritt mit der stark gestiegenen Nachfrage auf
     Nordatlantik-Strecken und den höheren Treibstoffkosten der Bestandsflotte,
     die im Sommerflugplan deutlich sichtbar wurden.</p>
  <p>Die ersten neuen Maschinen sollen bereits im kommenden Jahr ausgeliefert
     werden und zunächst ab Frankfurt sowie München zum Einsatz kommen.</p>
</article>
<aside><p>Werbung: Jetzt Kreditkarte bestellen und Meilen sammeln bei unserem
   Partner mit vielen exklusiven Vorteilen für Vielflieger!</p></aside>
<footer><p>Impressum</p></footer>
</body></html>
"""

# Fixture 2: kein <article>-Tag — der größte <p>-Cluster (Content-Div) muss
# gegen den kleineren Sidebar-Cluster gewinnen.
FIXTURE_P_CLUSTER = """
<html><body>
<div class="sidebar">
  <p>Kurzer Teaser eins der Sidebar, knapp über dreissig Zeichen lang.</p>
  <p>Kurzer Teaser zwei der Sidebar, ebenfalls etwas länger gemacht.</p>
</div>
<div class="entry-content">
  <p>Der Flughafen Wien hat im Juni deutlich mehr Passagiere abgefertigt als im
     Vorjahresmonat, wie die Betreibergesellschaft am Montag mitteilte und dabei
     auf die starke Nachfrage im Sommerreiseverkehr verwies.</p>
  <p>Besonders die Strecken nach Südeuropa legten zweistellig zu, während der
     Transferverkehr über das Drehkreuz stabil blieb und die Pünktlichkeit sich
     gegenüber dem chaotischen Vorsommer spürbar verbesserte.</p>
  <p>Für das Gesamtjahr rechnet der Vorstand mit einem neuen Passagierrekord,
     sofern keine externen Störungen wie Streiks oder Wetterlagen dazwischen
     kommen, hieß es weiter.</p>
</div>
</body></html>
"""

# Fixture 3: Artikel mit Spenden-Appell — der Appell-Satz muss rausfliegen.
FIXTURE_DONATION = """
<html><body>
<article>
  <p>Der Verband der Flugbegleiter fordert bessere Ruhezeiten auf
     Langstreckenrotationen und verweist auf eine aktuelle Befragung unter
     mehreren tausend Crew-Mitgliedern in ganz Europa.</p>
  <p>Die Airlines lehnen die Forderung bislang ab und verweisen auf die
     bestehenden gesetzlichen Regelungen, die aus ihrer Sicht ausreichend
     Schutz für die Besatzungen bieten würden.</p>
  <p>Unterstützen Sie uns mit einer Spende, damit wir weiter unabhängig
     berichten können.</p>
</article>
</body></html>
"""


# ── _extract_fulltext_pure ──────────────────────────────────────────

def test_extract_wordpress_article_keeps_body_drops_chrome():
    text = nb._extract_fulltext_pure(FIXTURE_WORDPRESS)
    assert len(text) >= 200
    assert 'Langstreckenflotte' in text
    assert 'Sommerflugplan' in text
    # Nav/Script/Style/Aside-Chrome darf nicht im Volltext landen.
    assert 'tracking' not in text
    assert 'color: red' not in text
    assert 'Kreditkarte bestellen' not in text
    # Absatz-Struktur bleibt erhalten (iOS rendert via split("\n\n")).
    assert '\n\n' in text
    assert len([p for p in text.split('\n\n') if p.strip()]) >= 3


def test_extract_largest_p_cluster_beats_sidebar():
    text = nb._extract_fulltext_pure(FIXTURE_P_CLUSTER)
    assert len(text) >= 200
    assert 'Flughafen Wien' in text
    assert 'Passagierrekord' in text
    # Der kleinere Sidebar-Cluster darf nicht gewinnen …
    assert 'Sidebar' not in text


def test_extract_strips_donation_appeal_sentence():
    text = nb._extract_fulltext_pure(FIXTURE_DONATION)
    assert 'Ruhezeiten' in text
    assert 'gesetzlichen Regelungen' in text
    # … aber der Spenden-Satz fliegt raus, der Rest bleibt 1:1.
    assert 'Spende' not in text
    assert 'Unterstützen Sie uns' not in text


def test_extract_empty_and_thin_input():
    assert nb._extract_fulltext_pure('') == ''
    assert nb._extract_fulltext_pure(None) == ''
    # Zu dünner Text (<200 Zeichen) → '' statt Müll-Snippet.
    assert nb._extract_fulltext_pure('<html><body><p>kurz</p></body></html>') == ''


# ── _html_to_paragraph_text (RSS content:encoded-Pfad) ─────────────

def test_html_to_paragraph_text_preserves_paragraph_breaks():
    html = ('<p>Erster Absatz mit etwas Inhalt.</p>'
            '<p>Zweiter Absatz, klar getrennt.</p>'
            '<p>Dritter&nbsp;Absatz mit Entity.</p>')
    text = nb._html_to_paragraph_text(html)
    parts = [p for p in text.split('\n\n') if p.strip()]
    assert parts == [
        'Erster Absatz mit etwas Inhalt.',
        'Zweiter Absatz, klar getrennt.',
        'Dritter Absatz mit Entity.',
    ]
    # Kein Tag/Script-Rest.
    assert '<' not in text


def test_html_to_paragraph_text_never_throws_on_junk():
    assert nb._html_to_paragraph_text(None) == ''
    assert nb._html_to_paragraph_text('') == ''
    assert 'Hallo' in nb._html_to_paragraph_text('<div>Hallo<br>Welt</div>')


# ── _attach_stored_fulltexts (Store gemockt — kein SB nötig) ───────

def test_attach_stored_fulltexts_sets_fulltext_and_readable(monkeypatch):
    long_text = 'Absatz eins des gespeicherten Volltexts. ' * 20  # > 400 Zeichen
    arts = [
        {'article_url': 'https://www.aero.de/news-1', 'fulltext': None,
         'in_app_readable': False},
        {'article_url': 'https://www.aero.de/news-2', 'fulltext': 'schon da',
         'in_app_readable': True},
        {'article_url': '', 'fulltext': None, 'in_app_readable': False},
    ]
    monkeypatch.setattr(
        nb, '_fulltext_store_get_many',
        lambda urls: {'https://www.aero.de/news-1': long_text})
    nb._attach_stored_fulltexts(arts)
    # .rstrip(): gespeicherter Text läuft durch _strip_feed_cruft, das am Ende
    # bewusst `out.strip()` macht — der Test-String endet auf ein Leerzeichen,
    # das der Cruft-Putz korrekt wegtrimmt (kein Inhaltsverlust).
    assert arts[0]['fulltext'] == long_text[:nb._FULLTEXT_FEED_CAP].rstrip()
    assert arts[0]['in_app_readable'] is True
    # Artikel mit vorhandenem RSS-Volltext bleibt unangetastet.
    assert arts[1]['fulltext'] == 'schon da'


def test_attach_stored_fulltexts_store_down_is_noop(monkeypatch):
    arts = [{'article_url': 'https://www.aero.de/news-1', 'fulltext': None,
             'in_app_readable': False}]
    monkeypatch.setattr(nb, '_fulltext_store_get_many', lambda urls: {})
    nb._attach_stored_fulltexts(arts)
    assert arts[0]['fulltext'] is None
    assert arts[0]['in_app_readable'] is False


# ── Copyright-Leitplanken (Nachfix 2026-07-10) ─────────────────────

def test_deny_hosts_cover_reuters_and_avherald():
    # Terms verbieten Scraping/Republikation → HART vom Volltext-Layer
    # ausgenommen (unabhängig von der app-Whitelist des On-Demand-Readers).
    for h in ('reuters.com', 'www.reuters.com',
              'avherald.com', 'www.avherald.com'):
        assert h in nb._FULLTEXT_DENY_HOSTS


class _FakeResp:
    def __init__(self, status_code, text=''):
        self.status_code = status_code
        self.text = text


def test_robots_allows_honors_disallow(monkeypatch):
    nb._ROBOTS_CACHE.clear()
    monkeypatch.setattr(
        nb.requests, 'get',
        lambda *a, **k: _FakeResp(200, 'User-agent: *\nDisallow: /news/\n'))
    assert nb._robots_allows('https://example.com/news/artikel-1') is False
    assert nb._robots_allows('https://example.com/blog/artikel-2') is True
    nb._ROBOTS_CACHE.clear()


def test_robots_allows_conservative_on_error_permissive_on_404(monkeypatch):
    nb._ROBOTS_CACHE.clear()
    # 404 = kein robots.txt → erlaubt.
    monkeypatch.setattr(nb.requests, 'get', lambda *a, **k: _FakeResp(404))
    assert nb._robots_allows('https://example.org/a') is True
    nb._ROBOTS_CACHE.clear()
    # Netz-Fehler → konservativ NICHT scrapen (nächster Lauf probiert neu).
    def _boom(*a, **k):
        raise nb.requests.RequestException('down')
    monkeypatch.setattr(nb.requests, 'get', _boom)
    assert nb._robots_allows('https://example.org/a') is False
    nb._ROBOTS_CACHE.clear()
