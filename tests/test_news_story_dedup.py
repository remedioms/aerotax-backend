"""Story-Dedup: dieselbe Meldung aus vier Quellen ist EINE Karte.

Fehlerklasse (Owner 10.08.): Die Feed-Sektion „Deine Airline" zeigte vier fast
identische Karten derselben Story (LH Starlink-WLAN) von vier Verlagen. Der
alte Dedupe verglich nur kanonische URL/ID und ROH-Titel (Jaccard >= 0,85) —
vier Wortlaute liegen weit darunter, also überlebten alle vier, wurden vier Mal
einzeln KI-umgeschrieben und vier Mal ausgespielt.

Vertrag:
- Ingest (`_dedupe_articles`): Story-Cluster auf NORMALISIERTEN Kern-Tokens
  (Verben/Füllwörter/Datum/Zahlen raus, WLAN/Wifi/Internet gefaltet).
- Serve (`/api/news/redaktion`): dieselbe geteilte Cluster-Funktion noch einmal
  über die Ausspiel-Liste — heilt den SCHON GEFÜLLTEN Store ohne Re-Ingest.
- Zwei ECHT verschiedene Stories derselben Airline dürfen NIE clustern.
- Dieselbe Schlagzeile ausserhalb des 72-h-Fensters ist eine neue Entwicklung.
- Gewinner ist der ZUERST gesehene Eintrag (stabile IDs, kein Flapping).
"""
import json

import pytest

import blueprints.news_blueprint as nb


# Die vier echten Wortlaute aus dem Feed-Screenshot vom 10.08.
STARLINK_TITEL = [
    'Lufthansa führt Starlink-WLAN ab August 2026 ein',
    'Lufthansa führt Starlink-Internet in der nächsten Woche ein',
    'Lufthansa startet Starlink-WLAN auf Airbus A320neo am 19. August',
    'Lufthansa führt Starlink-Wifi in ihrer Flotte ein',
]


def _art(i, title, published_at='2026-08-10T09:00:00+00:00', **over):
    art = {
        'id': f'story{i:04d}',
        'source': f'quelle{i}',
        'source_name': f'Quelle {i}',
        'title': title,
        'summary': f'Zusammenfassung von Quelle {i} zum Thema. ' * 2,
        'published_at': published_at,
        'article_url': f'https://verlag{i}.example/artikel/{i}',
        'image_url': None,
        'mentioned_airlines': [],   # wie im Aggregator: erst NACH dem Dedupe gefüllt
        'category': 'industry',
    }
    art.update(over)
    return art


def _titles(articles):
    return [a['title'] for a in articles]


# ── Ingest-Ebene ──────────────────────────────────────────────────

def test_vier_quellen_eine_starlink_story_ueberlebt_eine():
    arts = [_art(i, t) for i, t in enumerate(STARLINK_TITEL)]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 1, _titles(kept)
    # Gewinner = der ZUERST gesehene (stabile ID über Builds hinweg).
    assert kept[0]['id'] == 'story0000'


def test_zwei_echte_lh_stories_bleiben_beide():
    """False-Positive-Schutz: Streik und Lounge-Eröffnung sind NICHT dieselbe
    Story, obwohl beide Lufthansa nennen."""
    arts = [
        _art(1, 'Lufthansa: Streik am Montag angekündigt'),
        _art(2, 'Lufthansa eröffnet neue Lounge in München'),
    ]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 2, _titles(kept)


def test_verschiedene_stories_derselben_airline_breit():
    """Vier klar verschiedene LH-Meldungen am selben Tag — keine darf fallen."""
    arts = [
        _art(1, 'Lufthansa führt Starlink-WLAN ab August 2026 ein'),
        _art(2, 'Lufthansa: Streik am Montag angekündigt'),
        _art(3, 'Lufthansa eröffnet neue Lounge in München'),
        _art(4, 'Lufthansa bestellt zehn weitere Boeing 787'),
    ]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 4, _titles(kept)


def test_gleiche_story_ausserhalb_des_zeitfensters_bleibt():
    """Fünf Tage Abstand = neue Entwicklung, keine Dublette."""
    arts = [
        _art(1, STARLINK_TITEL[0], published_at='2026-08-05T09:00:00+00:00'),
        _art(2, STARLINK_TITEL[3], published_at='2026-08-10T09:00:00+00:00'),
    ]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 2, _titles(kept)


def test_gleiche_story_innerhalb_des_zeitfensters_faellt():
    """Gegenprobe zum Zeitfenster: 48 h Abstand clustert noch."""
    arts = [
        _art(1, STARLINK_TITEL[0], published_at='2026-08-08T09:00:00+00:00'),
        _art(2, STARLINK_TITEL[3], published_at='2026-08-10T09:00:00+00:00'),
    ]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 1, _titles(kept)


def test_gleiche_story_verschiedene_airlines_bleibt():
    """Starlink-WLAN bei Lufthansa und bei Air France sind zwei Meldungen."""
    arts = [
        _art(1, 'Lufthansa führt Starlink-WLAN in ihrer Flotte ein'),
        _art(2, 'Air France führt Starlink-WLAN in ihrer Flotte ein'),
    ]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 2, _titles(kept)


def test_alter_url_und_titel_dedupe_bleibt_wirksam():
    """Regression: die beiden bestehenden Pässe dürfen nicht verloren gehen."""
    same_url = [
        _art(1, 'Erste Meldung über den Vorfall in Frankfurt'),
        _art(2, 'Zweite Fassung derselben Meldung', id='story0001',
             article_url='https://verlag1.example/artikel/1'),
    ]
    assert len(nb._dedupe_articles(same_url)) == 1

    near_identical = [
        _art(1, 'Triebwerksschaden zwingt Boeing 777 zur Umkehr nach Dubai'),
        _art(2, 'Triebwerksschaden zwingt Boeing 777 zur Umkehr nach Dubai'),
    ]
    assert len(nb._dedupe_articles(near_identical)) == 1


# ── Token-Normalisierung ──────────────────────────────────────────

def test_kern_tokens_falten_synonyme_und_werfen_datum_raus():
    for titel in STARLINK_TITEL[:2]:
        assert nb._story_tokens(titel) == {'lufthansa', 'starlink', 'wifi'}
    # „Wi-Fi" darf nicht in zwei Zwei-Zeichen-Tokens zerfallen.
    assert 'wifi' in nb._story_tokens('Lufthansa startet Wi-Fi an Bord')
    # Reine Zahlen und Monatsnamen tragen keine Story.
    tokens = nb._story_tokens('Lufthansa startet Starlink am 19. August 2026')
    assert tokens == {'lufthansa', 'starlink'}


def test_ein_gemeinsames_wort_reicht_nie():
    """Zwei substanzarme Titel derselben Airline dürfen nicht clustern —
    sonst wäre der Jaccard auf {lufthansa} exakt 1,0."""
    arts = [
        _art(1, 'Lufthansa: Aktie legt zu'),
        _art(2, 'Lufthansa: Chef tritt ab'),
    ]
    kept = nb._dedupe_articles(arts)
    assert len(kept) == 2, _titles(kept)


# ── Serve-Ebene (heilt den schon gefüllten Store) ─────────────────

@pytest.fixture
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


def _client(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(nb.news_bp)
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)
    return app.test_client()


def _store_item(i, headline, published_at='2026-08-10T09:00:00+00:00'):
    return {
        'id': f'r{i:04d}',
        'headline': headline,
        'body': 'Ein sachlicher eigener Kurztext mit genug Substanz. ' * 2,
        'body_long': None,
        'category': 'industry',
        'source_name': f'Quelle {i}',
        'source_url': f'https://verlag{i}.example/{i}',
        'published_at': published_at,
        'mentioned_airlines': ['LH'],
        'rev': nb._REDAKTION_REV,
    }


def test_serve_liefert_aus_vier_starlink_items_genau_eins(_clean_store, monkeypatch):
    client = _client(monkeypatch)
    # Absteigend nach published_at sortiert gewinnt r0003 (neuester).
    nb._redaktion_store_merge({
        f'r{i:04d}': _store_item(
            i, t, published_at=f'2026-08-10T0{6 + i}:00:00+00:00')
        for i, t in enumerate(STARLINK_TITEL)
    })
    body = client.get('/api/news/redaktion').get_json()
    assert body['count'] == 1, json.dumps(
        [it['headline'] for it in body['items']], ensure_ascii=False)
    assert body['items'][0]['id'] == 'r0003'


def test_serve_behaelt_zwei_echte_lh_stories(_clean_store, monkeypatch):
    client = _client(monkeypatch)
    nb._redaktion_store_merge({
        'r0001': _store_item(1, 'Lufthansa: Streik am Montag angekündigt'),
        'r0002': _store_item(2, 'Lufthansa eröffnet neue Lounge in München'),
    })
    body = client.get('/api/news/redaktion').get_json()
    assert body['count'] == 2


def test_serve_respektiert_das_zeitfenster(_clean_store, monkeypatch):
    client = _client(monkeypatch)
    nb._redaktion_store_merge({
        'r0001': _store_item(1, STARLINK_TITEL[0],
                             published_at='2026-08-05T09:00:00+00:00'),
        'r0002': _store_item(2, STARLINK_TITEL[3],
                             published_at='2026-08-10T09:00:00+00:00'),
    })
    body = client.get('/api/news/redaktion').get_json()
    assert body['count'] == 2


def test_serve_vertraegt_int_epochen_im_store(_clean_store, monkeypatch):
    """Prod-Store trägt int-Epochen — das Zeitfenster muss sie lesen können."""
    client = _client(monkeypatch)
    nb._redaktion_store_merge({
        'r0001': _store_item(1, STARLINK_TITEL[0], published_at=1786000000),
        'r0002': _store_item(2, STARLINK_TITEL[3], published_at=1786010000),
    })
    body = client.get('/api/news/redaktion').get_json()
    assert body['count'] == 1
