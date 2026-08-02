"""Eigene SMP-Karten bleiben privat; Review nur nach explizitem Consent."""
from flask import Flask

from blueprints import smp_user_cards_blueprint as M


TOKEN = "AT-SMP-VOLUNTARY"
CARD_ID = "8b2ee84b-5c13-43c5-9e3c-8f486eb84d12"
SOURCE_ID = "d2e1621b-7f87-4320-83f3-3a6d0a188196"


class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}
        self.mode = None
        self.payload = None

    def select(self, *_a, **_k):
        self.mode = "select"
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, row):
        self.mode = "insert"
        self.payload = dict(row)
        return self

    def update(self, row):
        self.mode = "update"
        self.payload = dict(row)
        return self

    def execute(self):
        matched = [r for r in self.rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.mode == "insert":
            self.rows.append(dict(self.payload))
            return _Result([dict(self.payload)])
        if self.mode == "update":
            for row in matched:
                row.update(self.payload)
            return _Result([dict(r) for r in matched])
        return _Result([dict(r) for r in matched])


class _SB:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "ax_smp_user_cards"
        return _Table(self.rows)


def _client(monkeypatch, sb):
    monkeypatch.setattr(M, "_authed_token", lambda: (TOKEN, None))
    monkeypatch.setattr(M, "_sb_client", lambda: (sb, True))
    monkeypatch.setattr(M, "_rate_limited", lambda *_a, **_k: False)
    app = Flask(__name__)
    app.register_blueprint(M.smp_user_cards_bp)
    return app.test_client()


def _card_body(**extra):
    body = {
        "id": CARD_ID,
        "module": "bwl",
        "topic": "Ziele",
        "front": "Was bedeutet SMART?",
        "back": "Spezifisch, messbar, attraktiv, realistisch und terminiert.",
    }
    body.update(extra)
    return body


def test_normales_speichern_bleibt_privat(monkeypatch):
    sb = _SB()
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=_card_body())
    assert r.status_code == 200
    assert sb.rows[0]["status"] == "private"
    assert sb.rows[0]["submitted_at"] is None
    assert sb.rows[0]["consent_version"] is None


def test_submit_ohne_rechtebestaetigung_wird_abgelehnt(monkeypatch):
    sb = _SB()
    body = _card_body(submit_for_review=True, terms_version="2026-08")
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "rights_confirmation_required"
    assert sb.rows == []


def test_expliziter_submit_geht_in_review_queue(monkeypatch):
    sb = _SB()
    body = _card_body(submit_for_review=True, rights_confirmed=True,
                      terms_version="2026-08")
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=body)
    assert r.status_code == 200
    assert sb.rows[0]["status"] == "pending"
    assert sb.rows[0]["submitted_at"]
    assert sb.rows[0]["consent_version"] == "2026-08"



def test_submit_ohne_consent_version_wird_abgelehnt(monkeypatch):
    sb = _SB()
    body = _card_body(submit_for_review=True, rights_confirmed=True)
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "rights_confirmation_required"
    assert sb.rows == []


def test_submit_mit_leerer_consent_version_wird_abgelehnt(monkeypatch):
    sb = _SB()
    body = _card_body(submit_for_review=True, rights_confirmed=True,
                      terms_version="   ")
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "rights_confirmation_required"
    assert sb.rows == []



def test_alte_clients_mit_2026_08_bleiben_akzeptiert(monkeypatch):
    """Bestehende Zustimmungen werden nicht nachtraeglich entwertet."""
    sb = _SB()
    body = _card_body(submit_for_review=True, rights_confirmed=True,
                      terms_version="2026-08")
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=body)
    assert r.status_code == 200
    assert sb.rows[0]["consent_version"] == "2026-08"


def test_privates_speichern_braucht_keine_consent_version(monkeypatch):
    sb = _SB()
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards",
                                      json=_card_body(terms_version="x"))
    assert r.status_code == 200
    assert sb.rows[0]["status"] == "private"
    assert sb.rows[0]["consent_version"] is None


def test_herz_kopie_kann_nicht_als_eigene_karte_eingereicht_werden(monkeypatch):
    sb = _SB()
    body = _card_body(source_community_card_id=SOURCE_ID,
                      submit_for_review=True, rights_confirmed=True,
                      terms_version="2026-08")
    r = _client(monkeypatch, sb).post("/api/ax/smp/user-cards", json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "community_copy_cannot_be_submitted"

