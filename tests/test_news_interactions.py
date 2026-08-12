"""Likes + Kommentare für AeroX-Redaktions-News (Owner 2026-08-11).

Vertrag, den diese Datei festnagelt:
1. Auth-Gate: ohne Bearer 401 auf ALLEN vier Endpunkten.
2. Like ist ein IDEMPOTENTER Toggle über den Story-Cluster — zweimal „an"
   erzeugt keinen zweiten Like, und ein Like bleibt sichtbar, wenn der
   Cluster-Gewinner wechselt (neuere Quelle zur selben Meldung).
3. Kommentare tragen nach außen NUR AXU-Public-Refs, nie ein AT-Token
   (ein Token IST das Bearer-Credential).
4. Fremd-Delete ⇒ 403, eigener Delete ⇒ 200.
5. /api/news/redaktion trägt like_count/liked_by_me/comment_count ADDITIV —
   bestehende Felder behalten Name UND Typ (Lehre published_at 05.08.).

Test-IDs sind absichtlich lang und eindeutig (`newsix-…`), damit parallel
laufende pytest-Prozesse sich nicht gegenseitig Zeilen wegziehen.
"""
import json as _json
import uuid

import pytest
from flask import Flask

import blueprints.news_blueprint as nb


# Reguläre AeroX-Credentials sind AT- + exakt 16 Hex — nur die werden zu AXU.
TOKEN_A = 'AT-' + '00A1B2C3D4E5F607'
TOKEN_B = 'AT-' + '11A1B2C3D4E5F607'
ART_A = 'newsix-artikel-aaa'
ART_B = 'newsix-artikel-bbb'


# ── Supabase-Fake (nur die von diesem Feature benutzten Aufrufe) ──────────
class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, rows):
        self.rows = rows
        self.eqs = {}
        self._in = None
        self._lt = None
        self._limit = None
        self.mode = None
        self.payload = None

    # -- Query-Builder ---------------------------------------------------
    def select(self, *_a, **_k):
        self.mode = 'select'
        return self

    def eq(self, key, value):
        self.eqs[key] = value
        return self

    def in_(self, key, values):
        self._in = (key, list(values))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def lt(self, key, value):
        self._lt = (key, value)
        return self

    def insert(self, row):
        self.mode = 'insert'
        self.payload = dict(row)
        return self

    def upsert(self, row, **_k):
        self.mode = 'upsert'
        self.payload = dict(row)
        return self

    def delete(self):
        self.mode = 'delete'
        return self

    # -- Ausführung ------------------------------------------------------
    def _matches(self, row):
        if any(row.get(k) != v for k, v in self.eqs.items()):
            return False
        if self._in and row.get(self._in[0]) not in self._in[1]:
            return False
        if self._lt and not (str(row.get(self._lt[0]) or '') < str(self._lt[1])):
            return False
        return True

    def execute(self):
        if self.mode == 'insert':
            self.rows.append(dict(self.payload))
            return _Result([dict(self.payload)])
        if self.mode == 'upsert':
            key = ('article_id', 'user_token')
            for row in self.rows:
                if all(row.get(k) == self.payload.get(k) for k in key):
                    row.update(self.payload)
                    return _Result([dict(row)])
            self.rows.append(dict(self.payload))
            return _Result([dict(self.payload)])
        if self.mode == 'delete':
            keep = [r for r in self.rows if not self._matches(r)]
            gone = len(self.rows) - len(keep)
            self.rows[:] = keep
            return _Result([{}] * gone)
        hits = [dict(r) for r in self.rows if self._matches(r)]
        hits.sort(key=lambda r: str(r.get('created_at') or ''), reverse=True)
        if self._limit:
            hits = hits[:self._limit]
        return _Result(hits)


class _SB:
    """Spiegelt ax_news_likes/ax_news_comments + die Zähl-RPC der Migration."""

    def __init__(self):
        self.tables = {'ax_news_likes': [], 'ax_news_comments': []}
        self.rpc_calls = []
        self.rpc_broken = False      # Funktion existiert nicht (Migration fehlt)
        self.rpc_flaky = False       # transienter Fehler (Netz/Timeout)

    def table(self, name):
        assert name in self.tables, name
        return _Table(self.tables[name])

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        sb = self

        class _RPC:
            def execute(self_inner):
                if sb.rpc_broken:
                    raise RuntimeError('function ax_news_interaction_counts_'
                                       'grouped does not exist')
                if sb.rpc_flaky:
                    raise RuntimeError('server closed the connection')
                assert name == 'ax_news_interaction_counts_grouped'
                viewer = params.get('p_viewer_token')
                out = []
                for key, ids in (params.get('p_groups') or {}).items():
                    likes = [r for r in sb.tables['ax_news_likes']
                             if r.get('article_id') in ids]
                    comments = [r for r in sb.tables['ax_news_comments']
                                if r.get('article_id') in ids]
                    out.append({
                        'group_key': key,
                        # count(distinct user_token) über den ganzen Cluster —
                        # ein Like ist ein MENSCH, keine Zeile.
                        'like_count': len({r.get('user_token')
                                           for r in likes}),
                        'comment_count': len(comments),
                        'liked_by_me': any(r.get('user_token') == viewer
                                           for r in likes),
                    })
                return _Result(out)
        return _RPC()


@pytest.fixture(autouse=True)
def _reset_rpc_flag():
    nb._NEWS_COUNTS_RPC_OK.update({'ok': True, 'warned': False,
                                   'retry_at': 0.0})
    yield
    nb._NEWS_COUNTS_RPC_OK.update({'ok': True, 'warned': False,
                                   'retry_at': 0.0})


@pytest.fixture(autouse=True)
def _clean_redaktion_store(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, '_REDAKTION_PATH', str(tmp_path / 'ix_store.json'))
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
        nb._REDAKTION_FILE_MTIME['ts'] = -1.0
        nb._REDAKTION_LAST_BUILD.update({'ts': 0.0, 'running': False})
    yield
    with nb._REDAKTION_LOCK:
        nb._REDAKTION_STORE.clear()
        nb._REDAKTION_FILE_MTIME['ts'] = -1.0
        nb._REDAKTION_LAST_BUILD.update({'ts': 0.0, 'running': False})


@pytest.fixture
def sb():
    return _SB()


def _client():
    app = Flask(__name__)
    app.register_blueprint(nb.news_bp)
    return app.test_client()


def _authed(monkeypatch, sb, token=TOKEN_A):
    """Client mit gültigem Bearer + Fake-Store, ohne Netz."""
    monkeypatch.setattr(nb, '_news_authed_token', lambda: (token, None))
    monkeypatch.setattr(nb, '_news_viewer_token', lambda: token)
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    monkeypatch.setattr(nb, '_news_rate_limited', lambda *_a, **_k: False)
    monkeypatch.setattr(nb, '_news_blocked_by', lambda _t: set())
    monkeypatch.setattr(nb, '_news_author_name', lambda t: 'Crew ' + str(t)[-4:])
    monkeypatch.setattr(nb, '_news_story_ids', lambda aid: [aid])
    return _client()


def _item(art_id, headline, published_at, **over):
    it = {'id': art_id, 'headline': headline, 'body': 'B' * 80,
          'category': 'industry', 'source_name': 'Quelle ' + art_id[-3:],
          'source_url': 'https://example.org/' + art_id,
          'published_at': published_at, 'mentioned_airlines': ['LH'],
          'rev': nb._REDAKTION_REV}
    it.update(over)
    return it


def _store(items):
    nb._redaktion_store_merge({it['id']: it for it in items})


# ── 1. Auth-Gate ─────────────────────────────────────────────────────────
def test_alle_endpunkte_ohne_bearer_sind_401():
    """Kein Authorization-Header ⇒ 401 auf allen vier Routen. Kein Endpunkt
    darf ohne Identität schreiben oder fremde Kommentare zeigen."""
    c = _client()
    assert c.post(f'/api/news/{ART_A}/like').status_code == 401
    assert c.get(f'/api/news/{ART_A}/comments').status_code == 401
    assert c.post(f'/api/news/{ART_A}/comments',
                  json={'body': 'hallo'}).status_code == 401
    assert c.delete(
        f'/api/news/{ART_A}/comments/{uuid.uuid4()}').status_code == 401


def test_guest_token_darf_nicht_schreiben(monkeypatch):
    """Demo-Modus wie im Forum: AT-GUEST- kann nicht posten."""
    stubs = {'_request_bearer_token': lambda: 'AT-GUEST-DEMO',
             '_validate_token': lambda _t: None}
    monkeypatch.setattr(nb, '_app_attr',
                        lambda name, default=None: stubs.get(name, default))
    r = _client().post(f'/api/news/{ART_A}/like')
    assert r.status_code == 403
    assert r.get_json()['error'] == 'demo_mode_cannot_post'


def test_unbekanntes_token_ist_401_store_ausfall_ist_503(monkeypatch):
    """Tri-State: ein Supabase-Hickup darf keinen Client-Logout auslösen."""
    class _State:
        def __init__(self, value):
            self.value = value

    class _Validation:
        def __init__(self, value):
            self.state = _State(value)

    for value, expected in (('invalid', 401), ('unavailable', 503)):
        stubs = {'_request_bearer_token': lambda: TOKEN_A,
                 '_validate_token': lambda _t, v=value: _Validation(v)}
        monkeypatch.setattr(nb, '_app_attr',
                            lambda name, default=None, s=stubs: s.get(name, default))
        r = _client().post(f'/api/news/{ART_A}/like')
        assert r.status_code == expected, (value, r.status_code)


# ── 2. Like: Toggle + Idempotenz ─────────────────────────────────────────
def test_like_toggle_ist_idempotent(monkeypatch, sb):
    c = _authed(monkeypatch, sb)

    r1 = c.post(f'/api/news/{ART_A}/like').get_json()
    assert r1['ok'] is True and r1['liked_by_me'] is True and r1['likes'] == 1

    # Zweites POST = Toggle AUS (nicht ein zweiter Like).
    r2 = c.post(f'/api/news/{ART_A}/like').get_json()
    assert r2['liked_by_me'] is False and r2['likes'] == 0
    assert sb.tables['ax_news_likes'] == []

    # Drittes POST = wieder AN, weiterhin genau EINE Zeile.
    r3 = c.post(f'/api/news/{ART_A}/like').get_json()
    assert r3['liked_by_me'] is True and r3['likes'] == 1
    assert len(sb.tables['ax_news_likes']) == 1


def test_like_zaehlt_pro_user_nur_einmal(monkeypatch, sb):
    _authed(monkeypatch, sb, TOKEN_A).post(f'/api/news/{ART_A}/like')
    body = _authed(monkeypatch, sb, TOKEN_B).post(
        f'/api/news/{ART_A}/like').get_json()
    assert body['likes'] == 2 and body['liked_by_me'] is True
    # B nimmt zurück — A bleibt.
    body2 = _authed(monkeypatch, sb, TOKEN_B).post(
        f'/api/news/{ART_A}/like').get_json()
    assert body2['likes'] == 1 and body2['liked_by_me'] is False


def test_like_toggle_greift_ueber_den_ganzen_story_cluster(monkeypatch, sb):
    """Der Cluster-Gewinner ist der NEUESTE Artikel — kommt eine neuere Quelle
    zur selben Meldung, wandert die ausgespielte ID. Das eigene Like liegt dann
    auf der Schwester-ID und muss von der neuen Karte aus wegnehmbar sein,
    sonst entstünde ein Geister-Like, das niemand mehr löschen kann."""
    monkeypatch.setattr(nb, '_news_authed_token', lambda: (TOKEN_A, None))
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    monkeypatch.setattr(nb, '_news_rate_limited', lambda *_a, **_k: False)
    monkeypatch.setattr(nb, '_news_story_ids',
                        lambda aid: [aid] + [x for x in (ART_A, ART_B)
                                             if x != aid])
    c = _client()
    assert c.post(f'/api/news/{ART_A}/like').get_json()['likes'] == 1
    aus = c.post(f'/api/news/{ART_B}/like').get_json()
    assert aus['liked_by_me'] is False and aus['likes'] == 0
    assert sb.tables['ax_news_likes'] == []


def test_like_ist_ueber_den_cluster_sichtbar(monkeypatch, sb):
    """Gegenprobe zum Toggle: OHNE erneutes Tippen zeigt die Schwester-ID
    denselben Zähler und dasselbe liked_by_me."""
    sb.tables['ax_news_likes'].append(
        {'article_id': ART_A, 'user_token': TOKEN_B, 'created_at': 'x'})
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    counts, ok = nb._news_counts({ART_B: [ART_B, ART_A]}, viewer_token=TOKEN_B)
    assert ok is True
    assert counts[ART_B]['like_count'] == 1
    assert counts[ART_B]['liked_by_me'] is True


def test_ungueltige_artikel_id_wird_abgewiesen(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    r = c.post('/api/news/' + ('x' * 200) + '/like')
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_article_id'


def test_like_rate_limit_greift(monkeypatch, sb):
    monkeypatch.setattr(nb, '_news_authed_token', lambda: (TOKEN_A, None))
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    monkeypatch.setattr(nb, '_news_rate_limited', lambda *_a, **_k: True)
    r = _client().post(f'/api/news/{ART_A}/like')
    assert r.status_code == 429 and r.get_json()['error'] == 'rate_limited'


# ── 3. Kommentare: Shape, AXU, Deckel ────────────────────────────────────
def test_kommentar_anlegen_und_lesen_liefert_axu_ref(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    created = c.post(f'/api/news/{ART_A}/comments',
                     json={'body': 'Guter Artikel, danke!'}).get_json()
    assert created['ok'] is True
    comment = created['comment']
    assert comment['author_public_ref'].startswith('AXU-')
    assert comment['body'] == 'Guter Artikel, danke!'

    listing = c.get(f'/api/news/{ART_A}/comments').get_json()
    assert listing['count'] == 1
    item = listing['items'][0]
    assert {'id', 'author_public_ref', 'author_name', 'body',
            'created_at'} <= set(item)
    assert item['author_public_ref'].startswith('AXU-')
    assert item['is_mine'] is True


def test_kommentar_liefert_niemals_ein_at_token(monkeypatch, sb):
    """Token = Credential. Weder Liste noch Anlege-Antwort dürfen ein
    internes AT-Token enthalten — auch nicht das eigene."""
    c = _authed(monkeypatch, sb)
    created = c.post(f'/api/news/{ART_A}/comments', json={'body': 'Hi'})
    listing = c.get(f'/api/news/{ART_A}/comments')
    for payload in (created.get_json(), listing.get_json()):
        dumped = _json.dumps(payload)
        assert TOKEN_A not in dumped
        assert 'author_token' not in dumped


def test_kommentar_ohne_darstellbare_ref_wird_nicht_ausgeliefert(monkeypatch, sb):
    """Fail-closed: lieber eine Zeile weniger als ein geleaktes Credential."""
    c = _authed(monkeypatch, sb)
    sb.tables['ax_news_comments'].append({
        'id': str(uuid.uuid4()), 'article_id': ART_A,
        'author_token': TOKEN_B, 'body': 'fremd',
        'created_at': '2026-08-11T10:00:00+00:00'})
    monkeypatch.setattr(nb, '_news_public_ref', lambda _t: '')
    listing = c.get(f'/api/news/{ART_A}/comments').get_json()
    assert listing['items'] == []


def test_leerer_und_ueberlanger_kommentar(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    assert c.post(f'/api/news/{ART_A}/comments',
                  json={'body': '   '}).status_code == 400
    zu_lang = 'x' * (nb._NEWS_COMMENT_MAX + 1)
    r = c.post(f'/api/news/{ART_A}/comments', json={'body': zu_lang})
    assert r.status_code == 413 and r.get_json()['error'] == 'too_long'
    assert sb.tables['ax_news_comments'] == []


def test_kommentar_wird_sanitisiert(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    body = c.post(f'/api/news/{ART_A}/comments',
                  json={'body': '<script>alert(1)</script>'}).get_json()
    assert '<script>' not in body['comment']['body']


def test_geblockte_autoren_verschwinden_aus_der_liste(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    sb.tables['ax_news_comments'].extend([
        {'id': str(uuid.uuid4()), 'article_id': ART_A, 'author_token': TOKEN_B,
         'body': 'geblockt', 'created_at': '2026-08-11T10:00:00+00:00'},
        {'id': str(uuid.uuid4()), 'article_id': ART_A, 'author_token': TOKEN_A,
         'body': 'sichtbar', 'created_at': '2026-08-11T11:00:00+00:00'},
    ])
    monkeypatch.setattr(nb, '_news_blocked_by', lambda _t: {TOKEN_B})
    items = c.get(f'/api/news/{ART_A}/comments').get_json()['items']
    assert [i['body'] for i in items] == ['sichtbar']


def test_kommentar_limit_und_reihenfolge(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    for i in range(5):
        sb.tables['ax_news_comments'].append({
            'id': str(uuid.uuid4()), 'article_id': ART_A,
            'author_token': TOKEN_A, 'body': f'k{i}',
            'created_at': f'2026-08-11T1{i}:00:00+00:00'})
    items = c.get(f'/api/news/{ART_A}/comments?limit=2').get_json()['items']
    assert len(items) == 2
    assert items[0]['body'] == 'k4'          # neueste zuerst
    # Keyset-Blättern: alles VOR k4.
    aelter = c.get(f'/api/news/{ART_A}/comments'
                   '?limit=2&before=2026-08-11T14:00:00%2B00:00').get_json()
    assert [i['body'] for i in aelter['items']] == ['k3', 'k2']


# ── 4. Delete: nur der eigene Kommentar ──────────────────────────────────
def test_fremder_kommentar_kann_nicht_geloescht_werden(monkeypatch, sb):
    fremd_id = str(uuid.uuid4())
    sb.tables['ax_news_comments'].append({
        'id': fremd_id, 'article_id': ART_A, 'author_token': TOKEN_B,
        'body': 'nicht meiner', 'created_at': '2026-08-11T10:00:00+00:00'})
    c = _authed(monkeypatch, sb, TOKEN_A)
    r = c.delete(f'/api/news/{ART_A}/comments/{fremd_id}')
    assert r.status_code == 403 and r.get_json()['error'] == 'not_author'
    assert len(sb.tables['ax_news_comments']) == 1


def test_eigener_kommentar_wird_geloescht(monkeypatch, sb):
    c = _authed(monkeypatch, sb, TOKEN_A)
    created = c.post(f'/api/news/{ART_A}/comments',
                     json={'body': 'weg damit'}).get_json()
    cid = created['comment']['id']
    r = c.delete(f'/api/news/{ART_A}/comments/{cid}')
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert sb.tables['ax_news_comments'] == []


def test_unbekannter_kommentar_ist_404(monkeypatch, sb):
    c = _authed(monkeypatch, sb)
    assert c.delete(
        f'/api/news/{ART_A}/comments/{uuid.uuid4()}').status_code == 404


# ── 5. /api/news/redaktion: additive Felder ──────────────────────────────
def test_redaktion_traegt_additive_interaktions_felder(monkeypatch, sb):
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    monkeypatch.setattr(nb, '_news_viewer_token', lambda: TOKEN_A)
    _store([_item(ART_A, 'Lufthansa eroeffnet eine Lounge in Muenchen',
                  '2026-08-11T09:00:00+00:00')])
    sb.tables['ax_news_likes'].extend([
        {'article_id': ART_A, 'user_token': TOKEN_A, 'created_at': 'a'},
        {'article_id': ART_A, 'user_token': TOKEN_B, 'created_at': 'b'},
    ])
    sb.tables['ax_news_comments'].append({
        'id': str(uuid.uuid4()), 'article_id': ART_A, 'author_token': TOKEN_B,
        'body': 'schoen', 'created_at': '2026-08-11T10:00:00+00:00'})

    body = _client().get('/api/news/redaktion').get_json()
    assert body['interactions_available'] is True
    item = body['items'][0]
    assert item['like_count'] == 2
    assert item['comment_count'] == 1
    assert item['liked_by_me'] is True
    # ADDITIV: bestehende Felder unverändert in Name UND Typ.
    assert item['id'] == ART_A
    assert isinstance(item['headline'], str)
    assert isinstance(item['published_at'], str)
    assert isinstance(item['mentioned_airlines'], list)
    assert isinstance(item['source_url'], str)


def test_redaktion_felder_haben_immer_denselben_typ_ohne_store(monkeypatch):
    """Swift-Decode-Sicherheit: die drei Felder sind IMMER da und IMMER vom
    gleichen Typ — auch wenn die Zähl-Quelle weg ist. Dass die Zahlen dann
    nicht echt sind, sagt `interactions_available` ehrlich dazu (keine 0,
    die wie „niemand hat geliked" aussieht)."""
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)
    monkeypatch.setattr(nb, '_news_sb', lambda: (None, False))
    _store([_item(ART_A, 'Condor erweitert das Streckennetz ab Frankfurt',
                  '2026-08-11T09:00:00+00:00', mentioned_airlines=['DE'])])
    body = _client().get('/api/news/redaktion').get_json()
    item = body['items'][0]
    assert body['interactions_available'] is False
    assert item['like_count'] == 0 and isinstance(item['like_count'], int)
    assert item['comment_count'] == 0
    assert item['liked_by_me'] is False


def test_redaktion_ohne_bearer_bleibt_abrufbar(monkeypatch, sb):
    """Der Endpoint war und bleibt OHNE Auth erreichbar (alte Builds!) —
    liked_by_me ist dann schlicht false."""
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    _store([_item(ART_A, 'Lufthansa eroeffnet eine Lounge in Muenchen',
                  '2026-08-11T09:00:00+00:00')])
    sb.tables['ax_news_likes'].append(
        {'article_id': ART_A, 'user_token': TOKEN_B, 'created_at': 'b'})
    r = _client().get('/api/news/redaktion')
    assert r.status_code == 200
    item = r.get_json()['items'][0]
    assert item['like_count'] == 1
    assert item['liked_by_me'] is False


def test_redaktion_zaehlt_ueber_den_story_cluster(monkeypatch, sb):
    """Zwei Quellen, eine Story: der Zähler der ausgespielten Karte enthält
    auch die Likes, die auf der verdrängten Schwester-ID liegen."""
    monkeypatch.setattr(nb, '_redaktion_kick_build_if_stale', lambda: True)
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    monkeypatch.setattr(nb, '_news_viewer_token', lambda: TOKEN_A)
    _store([
        _item(ART_A, 'Lufthansa startet Starlink WLAN in der Flotte',
              '2026-08-11T08:00:00+00:00'),
        _item(ART_B, 'Lufthansa bringt Starlink Internet in die Flotte',
              '2026-08-11T10:00:00+00:00'),
    ])
    # Das Like liegt auf dem ÄLTEREN Artikel; ausgespielt wird der neuere.
    sb.tables['ax_news_likes'].append(
        {'article_id': ART_A, 'user_token': TOKEN_A, 'created_at': 'a'})

    body = _client().get('/api/news/redaktion').get_json()
    assert body['count'] == 1, 'Story-Dedupe muss weiterhin EINE Karte liefern'
    item = body['items'][0]
    assert item['id'] == ART_B
    assert item['like_count'] == 1
    assert item['liked_by_me'] is True


def test_story_dedupe_indices_bleibt_kompatibel():
    """Die Gruppen-Funktion ist neu, der alte Aufrufvertrag unverändert."""
    entries = [
        ({'lufthansa', 'starlink', 'wifi'}, {'LH'}, 1000.0),
        ({'lufthansa', 'starlink', 'wifi'}, {'LH'}, 2000.0),
        ({'condor', 'streckennetz', 'frankfurt'}, {'DE'}, 3000.0),
    ]
    groups = nb._story_dedupe_groups(entries)
    assert nb._story_dedupe_indices(entries) == [g[0] for g in groups] == [0, 2]
    assert groups[0][1] == [0, 1]
    assert groups[1][1] == [2]


# ── 6. Zähl-Fallback ohne RPC ────────────────────────────────────────────
def test_select_fallback_wenn_rpc_fehlt(monkeypatch, sb):
    """Migration auf einem Origin noch nicht durch (Lehre fcm_token 01.08.):
    das Feature degradiert auf einfache SELECTs statt komplett auszufallen."""
    sb.rpc_broken = True
    sb.tables['ax_news_likes'].append(
        {'article_id': ART_A, 'user_token': TOKEN_A, 'created_at': 'a'})
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    counts, ok = nb._news_counts({ART_A: [ART_A]}, viewer_token=TOKEN_A)
    assert ok is True
    assert counts[ART_A]['like_count'] == 1
    assert counts[ART_A]['liked_by_me'] is True
    assert nb._NEWS_COUNTS_RPC_OK['ok'] is False
    # Fehlende Funktion = HARTE Sperre (kein Neuversuch bis zum Deploy).
    assert nb._NEWS_COUNTS_RPC_OK['retry_at'] == 0.0


def test_transienter_rpc_fehler_sperrt_nicht_fuer_immer(monkeypatch, sb):
    """Ein Netz-Hickup darf den exakten Zähler nicht für die Prozess-Laufzeit
    abschalten (der SELECT-Fallback gibt bei > 5000 Zeilen auf). Nur die
    FEHLENDE Funktion sperrt hart; alles andere heilt nach der Abkühlzeit."""
    sb.rpc_flaky = True
    sb.tables['ax_news_likes'].append(
        {'article_id': ART_A, 'user_token': TOKEN_A, 'created_at': 'a'})
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))
    counts, ok = nb._news_counts({ART_A: [ART_A]}, viewer_token=TOKEN_A)
    assert ok is True and counts[ART_A]['like_count'] == 1   # Fallback trägt
    assert nb._NEWS_COUNTS_RPC_OK['ok'] is False
    assert nb._NEWS_COUNTS_RPC_OK['retry_at'] > 0.0
    # Solange die Abkühlzeit läuft: kein weiterer RPC-Versuch.
    vorher = len(sb.rpc_calls)
    nb._news_counts({ART_A: [ART_A]}, viewer_token=TOKEN_A)
    assert len(sb.rpc_calls) == vorher
    # Danach wieder — und der RPC ist inzwischen gesund.
    sb.rpc_flaky = False
    nb._NEWS_COUNTS_RPC_OK['retry_at'] = 1.0
    counts2, ok2 = nb._news_counts({ART_A: [ART_A]}, viewer_token=TOKEN_A)
    assert ok2 is True and len(sb.rpc_calls) > vorher
    assert nb._NEWS_COUNTS_RPC_OK['ok'] is True


def test_ein_mensch_ist_ein_like_auch_ueber_zwei_cluster_ids(monkeypatch, sb):
    """DOPPELZÄHLUNG (Fix 13.08.): derselbe User hat zwei Artikel derselben
    Story geliked (völlig legal — der Like-PK ist (article_id, user_token)).
    Die Karte muss trotzdem EINEN Like zeigen, nicht zwei — auf BEIDEN Wegen
    (Cluster-RPC und SELECT-Fallback)."""
    for aid in (ART_A, ART_B):
        sb.tables['ax_news_likes'].append(
            {'article_id': aid, 'user_token': TOKEN_A, 'created_at': 'a'})
    sb.tables['ax_news_likes'].append(
        {'article_id': ART_B, 'user_token': TOKEN_B, 'created_at': 'b'})
    monkeypatch.setattr(nb, '_news_sb', lambda: (sb, True))

    counts, ok = nb._news_counts({ART_B: [ART_B, ART_A]}, viewer_token=TOKEN_A)
    assert ok is True
    assert counts[ART_B]['like_count'] == 2      # A (2 Zeilen) + B = 2 Menschen
    assert counts[ART_B]['liked_by_me'] is True

    sb.rpc_broken = True
    nb._NEWS_COUNTS_RPC_OK.update({'ok': True, 'warned': False,
                                   'retry_at': 0.0})
    counts2, ok2 = nb._news_counts({ART_B: [ART_B, ART_A]},
                                   viewer_token=TOKEN_A)
    assert ok2 is True and counts2[ART_B]['like_count'] == 2


def test_news_comment_ist_ein_gueltiger_meldegrund():
    """Moderation: KEIN eigener Melde-Weg — der bestehende Report-Endpoint
    kennt den neuen kind-Wert (Apple 1.4.1)."""
    import inspect

    import app as backend
    src = inspect.getsource(backend.moderation_report)
    assert "'news_comment'" in src
    assert 'invalid_kind' in src
