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
def _clean_store():
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
        nb._REDAKTION_LAST_BUILD.update({'ts': 0.0, 'running': False})
    yield
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
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

    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE['x'] = {
            'id': 'x', 'headline': 'H' * 20, 'body': 'B' * 60,
            'category': 'industry', 'source_name': 'Q',
            'source_url': 'https://q.example', 'published_at': '2026-08-05',
            'mentioned_airlines': [], 'rev': nb._REDAKTION_REV,
        }
    r2 = client.get('/api/news/redaktion?limit=5')
    body2 = r2.get_json()
    assert body2['count'] == 1 and body2['warming'] is False
    assert 'image' not in json.dumps(body2['items'][0])


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
