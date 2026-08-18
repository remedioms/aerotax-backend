# -*- coding: utf-8 -*-
"""Owner-Mail bei Roster-Upload im Review-Status.

Fall Christoph S. (LHX/MUC, 18.08.2026): ein ``unsupported_pdf_format``-
Upload einer UNTERSTUETZTEN Airline landete still im Review — der Owner
erfuhr davon nur durch die Mail des Nutzers. Der Melder schickt jetzt eine
[AeroX Roster-Import KAPUTT]-Mail MIT dem PDF als Anhang (Owner: „damit
wir ihn uns dann zusammen angucken koennen"), gedeckelt auf 15 MB.
"""

import json
import types

import app


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a):
        return self

    def in_(self, *a):
        return self

    def execute(self):
        r = types.SimpleNamespace()
        r.data = self._rows
        return r


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == 'ax_logbook_upload'
        return _FakeTable(self._rows)


def _lauf(monkeypatch, rows):
    captured = {}

    class _FakeReq:
        def __init__(self, url, data=None, headers=None, method=None):
            captured['payload'] = json.loads(data.decode())

        def add_header(self, k, v):
            captured.setdefault('ua', v)

    monkeypatch.setattr(app, 'sb', _FakeSB(rows))
    monkeypatch.setattr(app, '_profile_load', lambda tok: {
        'profile': {'name': 'Christoph S.', 'airline': 'Lufthansa City',
                    'homebase': 'MUC'}})
    import urllib.request
    monkeypatch.setattr(urllib.request, 'Request', _FakeReq)
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda req, timeout=None: None)
    monkeypatch.setenv('RESEND_API_KEY', 'test-key')
    app._roster_pdf_review_owner_mail(
        [r['id'] for r in rows], 'unsupported_pdf_format', 'kein Marker')
    return captured['payload']


def test_mail_traegt_pdf_anhang_und_kontext(monkeypatch):
    p = _lauf(monkeypatch, [{'id': 606, 'token': 'AT-TEST',
                             'filename': 'roster.pdf', 'size_bytes': 5,
                             'data_b64': 'aGFsbG8='}])
    assert p['subject'] == '[AeroX Roster-Import KAPUTT] unsupported_pdf_format'
    assert p['attachments'] == [{'filename': 'roster.pdf',
                                 'content': 'aGFsbG8='}]
    assert 'PDF haengt an' in p['text']
    assert 'Christoph S.' in p['text']
    assert 'Lufthansa City/MUC' in p['text']


def test_zu_grosses_pdf_bleibt_in_der_tabelle(monkeypatch):
    p = _lauf(monkeypatch, [{'id': 607, 'token': 'AT-TEST',
                             'filename': 'riesig.pdf',
                             'size_bytes': 20 * 1024 * 1024,
                             'data_b64': 'x' * 10}])
    assert 'attachments' not in p
    assert 'zu gross' in p['text']
