# -*- coding: utf-8 -*-
"""Bereitschafts-bewusste Feed-Frische (Ivan D./LX, 18.08.2026).

Nach einer SBY-Aktivierung erschien der neue Flug in AeroX erst ~1 h
spaeter — Apple-Kalender (direktes iCal-Abo) und FollowMe hatten ihn
sofort. Ursache: der Server zieht den gespeicherten Feed nur, wenn der
letzte Import aelter als 6 h ist. Im Bereitschafts-Fenster (Standby/
Reserve ±1 Tag) gilt jetzt 15 min Frische bei 10 min Drossel.
"""

import time
import types

import app


def test_frische_entscheid_bereitschaft_gegen_normalfall():
    fuenf_stunden = 5 * 3600
    zehn_minuten = 10 * 60
    sieben_stunden = 7 * 3600
    # Der Ivan-Fall: Feed 5 h alt, heute Bereitschaft → ziehen.
    assert app._feed_refresh_wanted(fuenf_stunden, True) is True
    # Ohne Bereitschaft bleibt die 6-h-Ruhe (myTime-Schutz).
    assert app._feed_refresh_wanted(fuenf_stunden, False) is False
    # Auch im Bereitschafts-Fenster nicht haeufiger als alle 15 min.
    assert app._feed_refresh_wanted(zehn_minuten, True) is False
    # Normalfall ueber 6 h unveraendert.
    assert app._feed_refresh_wanted(sieben_stunden, False) is True
    # Unparsebares/fehlendes Alter → lieber ziehen (Altverhalten).
    assert app._feed_refresh_wanted(None, False) is True


def test_standby_erkennung_im_summary():
    assert app._feed_summary_is_standby('SBY E') is True
    assert app._feed_summary_is_standby('Bereitschaft FRA') is True
    assert app._feed_summary_is_standby('RSV') is True
    assert app._feed_summary_is_standby('Reserve Late') is True
    assert app._feed_summary_is_standby('LX 1076 ZRH-MUC') is False
    # 'SBY' nur als eigenes Token — nicht als Teil eines Worts.
    assert app._feed_summary_is_standby('PRESBYOPIE') is False
    assert app._feed_summary_is_standby(None) is False


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def in_(self, *a):
        return self

    def execute(self):
        self.calls += 1
        r = types.SimpleNamespace()
        r.data = self._rows
        return r


def _mit_fake_sb(monkeypatch, rows):
    table = _FakeTable(rows)

    class _FakeSB:
        def table(self, name):
            assert name == 'user_ical_briefings'
            return table

    monkeypatch.setattr(app, 'sb', _FakeSB())
    app._feed_refresh_sb_memo.clear()
    return table


def test_fenster_liest_briefings_und_memoisiert(monkeypatch):
    table = _mit_fake_sb(monkeypatch, [{'ical_summary': 'LX 1076 ZRH-MUC'},
                                       {'ical_summary': 'SBY E'}])
    assert app._calendar_refresh_standby_window('AT-TEST') is True
    # Zweiter Aufruf kommt aus dem Memo — kein weiterer SB-Read.
    assert app._calendar_refresh_standby_window('AT-TEST') is True
    assert table.calls == 1


def test_fenster_ohne_bereitschaft_und_sb_fehler(monkeypatch):
    _mit_fake_sb(monkeypatch, [{'ical_summary': 'LX 1076 ZRH-MUC'}])
    assert app._calendar_refresh_standby_window('AT-TEST') is False

    class _KaputtSB:
        def table(self, name):
            raise RuntimeError('sb down')

    monkeypatch.setattr(app, 'sb', _KaputtSB())
    app._feed_refresh_sb_memo.clear()
    # SB-Fehler darf den Roster-Read nie reissen → False, kein Raise.
    assert app._calendar_refresh_standby_window('AT-TEST') is False
