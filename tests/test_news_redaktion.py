"""AeroX News-Redaktion: KI-umgeschriebene eigene Kurzartikel.

Vertrag:
- Provider-Kaskade OpenAI → Anthropic (OpenAI nur mit Key; Owner-Wunsch).
- Nur Fakten-Blöcke (id, quelle, datum, originaltitel, quelltext) gehen in
  den Prompt; Volltext wird bevorzugt und gekappt.
- Unvollständige/zu kurze KI-Ergebnisse werden verworfen (kein halber Artikel).
- Antwort-Shape trägt KEINE Bild-Felder und NICHT den Original-Wortlaut.
- Ein Artikel wird genau einmal umgeschrieben (Store-Dedupe).
- Endpoint meldet warming, solange der Store leer ist und gebaut wird.
"""
import json

import pytest

import blueprints.news_blueprint as nb


def _art(i, **over):
    art = {
        'id': f'art{i:04d}',
        'source': 'aerotelegraph',
        'source_name': 'aeroTELEGRAPH',
        'title': f'Originaltitel {i} der Quelle',
        'summary': 'Kurze Zusammenfassung mit Fakten. ' * 3,
        'fulltext': 'Langer Quelltext mit vielen Fakten. ' * 20,
        'published_at': f'2026-08-05T0{i % 10}:00:00+00:00',
        'article_url': f'https://example.org/artikel/{i}',
        'image_url': 'https://example.org/verlagsfoto.jpg',
        'category': 'industry',
        'mentioned_airlines': ['LH'],
    }
    art.update(over)
    return art


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, '_REDAKTION_PATH', str(tmp_path / 'store.json'))
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
        nb._REDAKTION_FILE_MTIME['ts'] = -1.0
        nb._REDAKTION_LAST_BUILD.update({'ts': 0.0, 'running': False})
    yield
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
        nb._REDAKTION_FILE_MTIME['ts'] = -1.0
        nb._REDAKTION_LAST_BUILD.update({'ts': 0.0, 'running': False})


def _fake_items(blocks):
    return [{'id': b['id'],
             'headline': f'Eigene Headline zu {b["id"]}',
             'body': 'Ein sachlicher, komplett eigener Kurztext. ' * 3,
             'body_long': 'Ein längerer eigener Artikel mit eigener Struktur. ' * 10}
            for b in blocks]


def test_body_long_passthrough_and_thin_drop(monkeypatch):
    monkeypatch.setattr(nb, '_redaktion_call_openai', lambda blocks: None)
    monkeypatch.setattr(nb, '_redaktion_call_anthropic', lambda blocks: [
        {'id': 'art0007', 'headline': 'Eigene lange Headline dazu',
         'body': 'Kurztext mit ausreichend Substanz und Länge. ' * 2,
         'body_long': 'Langer eigener Artikel. ' * 30},
        {'id': 'art0008', 'headline': 'Eigene Headline für dünne Quelle',
         'body': 'Kurztext mit ausreichend Substanz und Länge. ' * 2,
         'body_long': 'zu kurz'},
    ])
    out = nb._redaktion_rewrite_batch([_art(7), _art(8)])
    assert out['art0007']['body_long'].startswith('Langer eigener Artikel')
    assert out['art0008']['body_long'] is None


def test_source_block_prefers_fulltext_and_caps():
    art = _art(1, fulltext='F' * 9000)
    block = nb._redaktion_source_block(art)
    assert block['quelltext'] == 'F' * 6000
    assert block['quelle'] == 'aeroTELEGRAPH'
    assert 'image' not in json.dumps(block)


def test_provider_cascade_prefers_openai(monkeypatch):
    calls = []
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    monkeypatch.setattr(nb, '_redaktion_call_openai',
                        lambda blocks: calls.append('openai') or _fake_items(blocks))
    monkeypatch.setattr(nb, '_redaktion_call_anthropic',
                        lambda blocks: calls.append('anthropic') or _fake_items(blocks))
    out = nb._redaktion_rewrite_batch([_art(1)])
    assert calls == ['openai']
    assert 'art0001' in out


def test_provider_cascade_falls_back_to_anthropic(monkeypatch):
    monkeypatch.setattr(nb, '_redaktion_call_openai', lambda blocks: None)
    monkeypatch.setattr(nb, '_redaktion_call_anthropic',
                        lambda blocks: _fake_items(blocks))
    out = nb._redaktion_rewrite_batch([_art(2)])
    assert out['art0002']['headline'].startswith('Eigene Headline')


def test_too_short_results_are_dropped(monkeypatch):
    monkeypatch.setattr(nb, '_redaktion_call_openai', lambda blocks: None)
    monkeypatch.setattr(nb, '_redaktion_call_anthropic', lambda blocks: [
        {'id': 'art0003', 'headline': 'ok', 'body': 'zu kurz'},
    ])
    out = nb._redaktion_rewrite_batch([_art(3)])
    assert out == {}


def test_item_shape_has_no_image_and_no_original_wording(monkeypatch):
    monkeypatch.setattr(nb, '_redaktion_call_openai', lambda blocks: None)
    monkeypatch.setattr(nb, '_redaktion_call_anthropic',
                        lambda blocks: _fake_items(blocks))
    out = nb._redaktion_rewrite_batch([_art(4)])
    item = out['art0004']
    dumped = json.dumps(item)
    assert 'image' not in dumped
    # Original-Wortlaut der Quelle darf NICHT ausgeliefert werden.
    assert 'Originaltitel' not in dumped
    assert item['source_url'] == 'https://example.org/artikel/4'
    assert item['source_name'] == 'aeroTELEGRAPH'
    assert item['rev'] == nb._REDAKTION_REV


def test_parse_json_tolerates_codefences():
    raw = '```json\n{"items": [{"id": "a", "headline": "H", "body": "B"}]}\n```'
    items = nb._redaktion_parse_json(raw)
    assert items and items[0]['id'] == 'a'
    assert nb._redaktion_parse_json('kein json hier') == []


def test_build_dedupes_already_rewritten(monkeypatch):
    seen_batches = []

    def fake_batch(articles):
        seen_batches.append([a['id'] for a in articles])
        return {a['id']: {'id': a['id'], 'headline': 'H' * 20,
                          'body': 'B' * 60, 'category': 'industry',
                          'source_name': a['source_name'],
                          'source_url': a['article_url'],
                          'published_at': a['published_at'],
                          'mentioned_airlines': [], 'rev': nb._REDAKTION_REV}
                for a in articles}

    arts = [_art(i) for i in range(1, 4)]
    monkeypatch.setattr(nb, '_redaktion_base_articles', lambda: list(arts))
    monkeypatch.setattr(nb, '_redaktion_rewrite_batch', fake_batch)
    nb._redaktion_build()
    assert len(nb._REDAKTION_STORE) == 3
    # Zweiter Lauf: nichts Neues → kein weiterer KI-Call.
    nb._REDAKTION_LAST_BUILD['ts'] = 0.0
    nb._redaktion_build()
    assert len(seen_batches) == 1


def test_build_rewrites_items_with_stale_rev(monkeypatch):
    """Rev-Bump-Vertrag (Stil-Upgrade 05.08.): Artikel mit alter rev werden
    beim nächsten Build NEU geschrieben (progressiver Ersatz), Artikel mit
    aktueller rev bleiben unangetastet — und die alte Fassung bleibt bis zum
    Ersatz im Store (kein Publisher-Fallback-Fenster)."""
    seen_batches = []

    def fake_batch(articles):
        seen_batches.append(sorted(a['id'] for a in articles))
        return {a['id']: {'id': a['id'], 'headline': 'Neu ' + 'H' * 20,
                          'body': 'B' * 60, 'category': 'industry',
                          'source_name': a['source_name'],
                          'source_url': a['article_url'],
                          'published_at': a['published_at'],
                          'mentioned_airlines': [], 'rev': nb._REDAKTION_REV}
                for a in articles}

    arts = [_art(i) for i in range(1, 4)]
    monkeypatch.setattr(nb, '_redaktion_base_articles', lambda: list(arts))
    monkeypatch.setattr(nb, '_redaktion_rewrite_batch', fake_batch)
    # Vorbelegung: art0001 alte Rev, art0002 aktuelle Rev, art0003 fehlt.
    nb._redaktion_store_merge({
        'art0001': {'id': 'art0001', 'headline': 'Alt ' + 'H' * 20,
                    'body': 'B' * 60, 'category': 'industry',
                    'source_name': 'Q', 'source_url': 'u',
                    'published_at': '2026-08-05', 'mentioned_airlines': [],
                    'rev': 'aerox-redaktion-v1'},
        'art0002': {'id': 'art0002', 'headline': 'Aktuell ' + 'H' * 20,
                    'body': 'B' * 60, 'category': 'industry',
                    'source_name': 'Q', 'source_url': 'u',
                    'published_at': '2026-08-05', 'mentioned_airlines': [],
                    'rev': nb._REDAKTION_REV},
    })
    nb._redaktion_build()
    assert seen_batches == [['art0001', 'art0003']]
    snap = nb._redaktion_store_snapshot()
    assert snap['art0001']['headline'].startswith('Neu ')      # ersetzt
    assert snap['art0002']['headline'].startswith('Aktuell ')  # unangetastet
    assert snap['art0003']['rev'] == nb._REDAKTION_REV


def test_endpoint_serves_store_and_warming(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(nb.news_bp)
    client = app.test_client()

    # Build unterdrücken (kein Thread im Test).
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)

    r = client.get('/api/news/redaktion')
    body = r.get_json()
    assert r.status_code == 200 and body['ok'] is True
    assert body['items'] == [] and body['warming'] is True

    nb._redaktion_store_merge({'x': {
        'id': 'x', 'headline': 'H' * 20, 'body': 'B' * 60,
        'category': 'industry', 'source_name': 'Q',
        'source_url': 'https://q.example', 'published_at': '2026-08-05',
        'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
    }})
    r2 = client.get('/api/news/redaktion?limit=5')
    body2 = r2.get_json()
    assert body2['count'] == 1 and body2['warming'] is False
    assert 'image' not in json.dumps(body2['items'][0])


def test_endpoint_published_at_ist_immer_string(monkeypatch):
    """App-Vertrag (Vorfall 05.08.): RedaktionItem.published_at ist in Swift
    String? — ein int (Unix-Epoche aus dem Feed-Schema) lässt das GESAMTE
    Decoding platzen und die App fällt still auf den Publisher-Feed zurück.
    Der Endpoint muss int-Epochen deshalb als ISO-String ausliefern und
    gemischte Typen (int + str im selben Store) sortieren können."""
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(nb.news_bp)
    client = app.test_client()
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)

    nb._redaktion_store_merge({
        'alt': {'id': 'alt', 'headline': 'H' * 20, 'body': 'B' * 60,
                'category': 'industry', 'source_name': 'Q', 'source_url': 'u',
                'published_at': 1785913510,  # Unix-int wie im Prod-Store
                'mentioned_airlines': [], 'rev': nb._REDAKTION_REV},
        'neu': {'id': 'neu', 'headline': 'H' * 20, 'body': 'B' * 60,
                'category': 'industry', 'source_name': 'Q', 'source_url': 'u',
                'published_at': '2026-08-05T09:00:00+00:00',
                'mentioned_airlines': [], 'rev': nb._REDAKTION_REV},
        'leer': {'id': 'leer', 'headline': 'H' * 20, 'body': 'B' * 60,
                 'category': 'industry', 'source_name': 'Q', 'source_url': 'u',
                 'published_at': None,
                 'mentioned_airlines': [], 'rev': nb._REDAKTION_REV},
    })
    body = client.get('/api/news/redaktion').get_json()
    assert body['count'] == 3
    for it in body['items']:
        assert it['published_at'] is None or isinstance(it['published_at'], str)
    by_id = {it['id']: it for it in body['items']}
    # 1785913510 = 2026-08-05T07:05:10Z — als ISO-String, nicht als Zahl.
    assert by_id['alt']['published_at'] == '2026-08-05T07:05:10Z'
    # Neuester zuerst (09:00 > 07:05) trotz gemischter Typen im Store.
    assert body['items'][0]['id'] == 'neu'


def test_copyright_guard_rejects_copied_passages(monkeypatch):
    art = _art(9, fulltext=(
        'Die Lufthansa hat am Montag angekündigt, dass die beschädigte '
        'Boeing 787 nach der Reparatur in Frankfurt noch vor dem Jahresende '
        'wieder in den Liniendienst zurückkehren soll.'))
    kopiert = ('Neu formuliert vorne, aber dann: die beschädigte Boeing 787 '
               'nach der Reparatur in Frankfurt noch vor dem Jahresende '
               'wieder in den Liniendienst. Und noch ein eigener Satz dazu.')
    monkeypatch.setattr(nb, '_redaktion_call_openai', lambda blocks: None)
    monkeypatch.setattr(nb, '_redaktion_call_anthropic', lambda blocks: [
        {'id': 'art0009', 'headline': 'Eigene saubere Headline dazu',
         'body': kopiert}])
    out = nb._redaktion_rewrite_batch([art])
    assert out == {}
    assert nb._REDAKTION_REJECTED.get('art0009') == 1
    nb._REDAKTION_REJECTED.clear()


def test_copyright_guard_allows_shared_proper_nouns():
    source = ('Der Airbus A350-1000 von Lufthansa Technik landete am '
              'Flughafen Frankfurt am Main nach einem Testflug.')
    eigen = ('Nach einem Testflug ist ein Airbus A350-1000 in Frankfurt '
             'gelandet. Die Maschine wird von Lufthansa Technik betreut.')
    assert nb._redaktion_longest_shared_run(source, eigen) <= nb._REDAKTION_MAX_SHARED_RUN


def test_copyright_guard_checks_body_long_too():
    art = _art(10, fulltext='Ein sehr charakteristischer Quellsatz der '
               'wortwörtlich übernommen worden ist und lang genug dafür ist.')
    ok = nb._redaktion_copyright_ok(
        art, 'Eigene Headline', 'Eigener Kurztext mit genug Länge dabei.',
        'Langtext vorne eigen, dann ein sehr charakteristischer Quellsatz der '
        'wortwörtlich übernommen worden ist und lang genug dafür ist.')
    assert ok is False


def test_guard_retry_then_tombstone(monkeypatch):
    """Erster Guard-Fail ⇒ Artikel bleibt im todo (mit Rüge); zweiter ⇒ raus."""
    art = _art(11)
    monkeypatch.setattr(nb, '_redaktion_base_articles', lambda: [dict(art)])
    calls = []

    def fake_batch(articles):
        calls.append([bool(a.get('_redaktion_retry')) for a in articles])
        for a in articles:
            nb._REDAKTION_REJECTED[a['id']] = nb._REDAKTION_REJECTED.get(a['id'], 0) + 1
        return {}

    monkeypatch.setattr(nb, '_redaktion_rewrite_batch', fake_batch)
    nb._redaktion_build()          # Versuch 1: kein Retry-Flag
    nb._REDAKTION_LAST_BUILD['ts'] = 0.0
    nb._redaktion_build()          # Versuch 2: MIT Retry-Flag
    nb._REDAKTION_LAST_BUILD['ts'] = 0.0
    nb._redaktion_build()          # Versuch 3: tombstoned, kein Call mehr
    assert calls == [[False], [True]]
    nb._REDAKTION_REJECTED.clear()


def test_store_survives_worker_memo_loss(monkeypatch):
    """Mehrprozess-Vertrag: ein 'frischer Worker' (leeres Memo) liest die
    Artikel aus der Store-DATEI — genau das fehlte in Prod (3 Gunicorn-
    Worker, Antworten flackerten zwischen 3 und 0 Artikeln)."""
    nb._redaktion_store_merge({'a': {
        'id': 'a', 'headline': 'H' * 20, 'body': 'B' * 60,
        'category': 'industry', 'source_name': 'Q',
        'source_url': 'https://q.example', 'published_at': '2026-08-05',
        'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
    }})
    # Frischer Worker: Memo weg, Datei bleibt.
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
        nb._REDAKTION_FILE_MTIME['ts'] = -1.0
    snap = nb._redaktion_store_snapshot()
    assert 'a' in snap and snap['a']['headline'] == 'H' * 20


def test_store_cap_applies_in_file(monkeypatch):
    many = {f'id{i:03d}': {
        'id': f'id{i:03d}', 'headline': 'H' * 20, 'body': 'B' * 60,
        'category': 'industry', 'source_name': 'Q', 'source_url': 'u',
        'published_at': f'2026-08-05T{i % 24:02d}:{i % 60:02d}:00+00:00',
        'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
    } for i in range(nb._REDAKTION_STORE_MAX + 30)}
    nb._redaktion_store_merge(many)
    assert len(nb._redaktion_file_load()) == nb._REDAKTION_STORE_MAX


# ── Off-Topic-Gate + Quellen-Deckel (Owner 05.08.: „wie können so viele
#    news erscheinen??") ──────────────────────────────────────────────────────

def test_offtopic_helper_blocks_without_airline_and_exempts_with():
    # Klar fremdes Themenfeld ohne Airline-Bezug → raus.
    assert nb._redaktion_offtopic(
        'SpaceX-Rakete könnte auf dem Mond einschlagen', 'Raumfahrt-News.')
    assert nb._redaktion_offtopic(
        'US Navy sucht neues Kampfjet-Triebwerk', 'Militärbeschaffung.')
    assert nb._redaktion_offtopic(
        'Schifffahrt auf der Donau boomt bei Hitze', 'Tourismus am Fluss.')
    # Airline-Erwähnung gewinnt IMMER — als Parameter ODER aus dem Text.
    assert not nb._redaktion_offtopic(
        'Militärauftrag mit Beteiligung', 'Details.', mentioned=['LH'])
    assert not nb._redaktion_offtopic(
        'Lufthansa fliegt Satelliten-Techniker nach Kourou', 'Charter.')
    # Normale Airline-News bleiben unberührt (kein Blocklist-Treffer).
    assert not nb._redaktion_offtopic(
        'Condor startet tägliche Flüge nach Tel Aviv', 'Neue Strecke.')


def test_build_skips_offtopic_and_purges_stale_rewrite(monkeypatch):
    """Off-Topic wird weder umgeschrieben (kein KI-Call) noch weiter
    ausgeliefert: ein VOR dem Gate umgeschriebener Altbestand fliegt beim
    nächsten Build aus dem Store."""
    seen = []

    def fake_batch(articles):
        seen.extend(a['id'] for a in articles)
        return {a['id']: {'id': a['id'], 'headline': 'H' * 20,
                          'body': 'B' * 60, 'category': 'industry',
                          'source_name': a['source_name'],
                          'source_url': a['article_url'],
                          'published_at': a['published_at'],
                          'mentioned_airlines': [], 'rev': nb._REDAKTION_REV}
                for a in articles}

    good = _art(1)
    space = _art(2, title='NASA plant neue Mond-Rakete',
                 summary='Raumfahrtprogramm.', mentioned_airlines=[])
    # Altbestand: die Weltraum-Story wurde vor dem Gate schon umgeschrieben.
    nb._redaktion_store_merge({space['id']: {
        'id': space['id'], 'headline': 'Alte Weltraum-Headline ' + 'H' * 8,
        'body': 'B' * 60, 'category': 'general', 'source_name': 'Q',
        'source_url': 'u', 'published_at': space['published_at'],
        'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
    }})
    monkeypatch.setattr(nb, '_redaktion_base_articles',
                        lambda: [good, space])
    monkeypatch.setattr(nb, '_redaktion_rewrite_batch', fake_batch)
    nb._redaktion_build()
    assert seen == [good['id']]                      # Off-Topic nie zur KI
    assert space['id'] not in nb._redaktion_file_load()   # Purge greift
    assert good['id'] in nb._redaktion_file_load()


def test_endpoint_caps_items_per_source(monkeypatch):
    """Höchstens _REDAKTION_SOURCE_CAP Artikel je Quelle in einer Antwort —
    eine fleißige Quelle dominiert die Liste nicht mehr; andere Quellen
    bleiben vollständig sichtbar."""
    import time as _t
    import app as backend
    many = {}
    for i in range(nb._REDAKTION_SOURCE_CAP + 4):
        many[f'flut{i:02d}'] = {
            'id': f'flut{i:02d}', 'headline': 'H' * 20, 'body': 'B' * 60,
            'category': 'industry', 'source_name': 'Fleissige Quelle',
            'source_url': 'u',
            'published_at': f'2026-08-05T{10 + i % 12:02d}:00:00+00:00',
            'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
        }
    many['ruhig01'] = {
        'id': 'ruhig01', 'headline': 'H' * 20, 'body': 'B' * 60,
        'category': 'industry', 'source_name': 'Ruhige Quelle',
        'source_url': 'u', 'published_at': '2026-08-05T00:30:00+00:00',
        'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
    }
    nb._redaktion_store_merge(many)
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_LAST_BUILD.update({'ts': _t.time(), 'running': False})
    r = backend.app.test_client().get('/api/news/redaktion?limit=100')
    assert r.status_code == 200
    items = r.get_json()['items']
    from collections import Counter
    counts = Counter(i['source_name'] for i in items)
    assert counts['Fleissige Quelle'] == nb._REDAKTION_SOURCE_CAP
    assert counts['Ruhige Quelle'] == 1


def test_endpoint_hides_offtopic_leftovers(monkeypatch):
    """Serve-Gate: ein noch im Store liegender Off-Topic-Rewrite ist SOFORT
    unsichtbar (nicht erst nach dem nächsten Build-Purge)."""
    import time as _t
    import app as backend
    nb._redaktion_store_merge({
        'ok1': {'id': 'ok1', 'headline': 'Condor erweitert das Streckennetz',
                'body': 'B' * 60, 'category': 'industry',
                'source_name': 'Q', 'source_url': 'u',
                'published_at': '2026-08-05T09:00:00+00:00',
                'mentioned_airlines': ['DE'], 'rev': nb._REDAKTION_REV},
        'off1': {'id': 'off1', 'headline': 'SpaceX-Rakete erreicht den Mond',
                 'body': 'Raumfahrt. ' * 10, 'category': 'general',
                 'source_name': 'Q', 'source_url': 'u',
                 'published_at': '2026-08-05T10:00:00+00:00',
                 'mentioned_airlines': [], 'rev': nb._REDAKTION_REV},
    })
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_LAST_BUILD.update({'ts': _t.time(), 'running': False})
    r = backend.app.test_client().get('/api/news/redaktion?limit=100')
    ids = [i['id'] for i in r.get_json()['items']]
    assert 'ok1' in ids and 'off1' not in ids


def test_offtopic_stopcodes_never_count_as_airline_proof():
    """Live-Fall 05.08.: gespeicherte Erwähnungen ['EI']/['AM'] stammen aus
    Whole-Word-Treffern auf Funktionswörter (dt. „am", .de-Domains, engl.
    „AI") und dürfen einen Off-Topic-Artikel nicht schützen."""
    for code in ('EI', 'AM', 'DE', 'AI'):
        assert nb._redaktion_offtopic(
            'SpaceX-Rakete könnte auf dem Mond einschlagen', 'Raumfahrt.',
            mentioned=[code]), code
    assert not nb._redaktion_offtopic(
        'SpaceX-Rakete könnte auf dem Mond einschlagen', 'Raumfahrt.',
        mentioned=['LH'])
