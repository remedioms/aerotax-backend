"""Rückkipper-Regel: zwei Quellen EINES Syncs heben sich gegenseitig auf.

BEFUND (Owner-Screen „Dienstplan-Änderungen", 2026-08-16): vier Einträge, die
sich paarweise widersprechen — „Route FRA-ATH → FRA-SFO" direkt über „Route
FRA-SFO → FRA-ATH", „Dienst entfernt · FRA-BCN-FRA" direkt über „Neuer Dienst ·
FRA-BCN-FRA".

GEMESSEN im Prod-Verlauf (`roster_changes`, Owner-Token), echte Zeitstempel:
    11:32:13.835160  2026-08-16 added     · → FRA-BCN-FRA
    11:32:13.835164  2026-08-17 modified  FRA-SFO (LH454) → FRA-ATH
    11:32:14.418051  2026-08-16 removed   FRA-BCN-FRA → ·
    11:32:14.418058  2026-08-17 modified  FRA-ATH → FRA-SFO (LH454)
583 Millisekunden Abstand → Fall (a): zwei Quellen im SELBEN Sync schreiben
nacheinander den Snapshot und diffen jeweils gegen das Ergebnis der anderen.
Dieselbe Signatur an drei weiteren Tagen desselben Tokens (09.08. 03:36:57 /
03:37:00, 12.08. 06:16:28 / 06:16:30).

Die fünf bestehenden Gatter greifen hier ALLE nicht: jedes bewertet EINEN
Vergleich, und jeder der beiden Vergleiche ist für sich korrekt und trägt
echte Dienst-Substanz. Es fehlte eine Regel über die HISTORIE.

GEGENPROBE (der wichtigste Test dieser Datei): eine ECHTE Rücknahme — die
Planung gibt den Dienst Stunden später wirklich zurück — muss vollständig
erhalten bleiben. Es entscheidet der ZEITLICHE ABSTAND, nicht die Ähnlichkeit.
"""
import json
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import app as A


# ── Die beiden echten Roster-Stände aus dem Befund ──────────────────────────
def _tag_sfo(datum):
    """Der ECHTE Stand des 17.08.: LH454 FRA-SFO mit Briefing + Layover."""
    return {
        'datum': datum, 'routing': 'FRA-SFO',
        'marker': '08:35 LT Briefing FRA · LH 454: FRA-SFO · Layover [SFO] (Tag 1/3)',
        'ical_sectors': [{'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
                          'dep_iso': f'{datum}T08:25:00Z',
                          'arr_iso': f'{datum}T19:40:00Z'}],
        'reader_facts': {'start_time': '08:35', 'end_time': '21:40',
                         'layover_ort': 'SFO',
                         'marker_raw': '08:35 LT Briefing FRA · LH 454: FRA-SFO'},
    }


def _tag_ath(datum):
    """Der KONKURRIERENDE Stand desselben Tages: LH1284 FRA-ATH."""
    return {
        'datum': datum, 'routing': 'FRA-ATH', 'marker': 'FRA - ATH',
        'ical_sectors': [{'flight': 'LH1284', 'from': 'FRA', 'to': 'ATH',
                          'dep_iso': f'{datum}T11:00:00Z',
                          'arr_iso': f'{datum}T14:00:00Z'}],
        'reader_facts': {'start_time': '12:00', 'end_time': '18:00',
                         'layover_ort': 'ATH', 'marker_raw': 'FRA - ATH'},
    }


def _tag_bcn(datum):
    """Der 16.08. aus dem Befund — im einen Lauf da, im anderen weg."""
    return {
        'datum': datum, 'routing': 'FRA-BCN-FRA', 'marker': 'FRA - BCN',
        'ical_sectors': [{'flight': 'LH1126', 'from': 'FRA', 'to': 'BCN',
                          'dep_iso': f'{datum}T04:00:00Z',
                          'arr_iso': f'{datum}T06:30:00Z'},
                         {'flight': 'LH1127', 'from': 'BCN', 'to': 'FRA',
                          'dep_iso': f'{datum}T14:00:00Z',
                          'arr_iso': f'{datum}T16:30:00Z'}],
        'reader_facts': {'start_time': '05:00', 'end_time': '19:00',
                         'marker_raw': 'FRA - BCN'},
    }


def _entry(datum, kind, old=None, new=None, ago_sec=0.583, now=None):
    """Ein bestehender Verlauf-Eintrag, `ago_sec` Sekunden alt."""
    now = now or datetime.now()
    e = {'datum': datum, 'kind': kind, 'status': 'pending',
         'detected_at': (now - timedelta(seconds=ago_sec)).isoformat()}
    if old is not None:
        e['old'] = old
    if new is not None:
        e['new'] = new
    return e


D1, D2 = '2026-08-17', '2026-08-16'


# ── 1) Der gemessene Fall: 583 ms Abstand → BEIDE Hälften fallen raus ───────
def test_modified_rueckkipper_binnen_583ms_loescht_beide_haelften():
    pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=0.583)]
    history = []
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
    ueberlebt = A._rc_cancel_reversals(neu, [pending, history])
    assert ueberlebt == [], 'die zweite Hälfte darf nicht entstehen'
    assert pending == [], 'die erste Hälfte muss mit verschwinden'


def test_removed_nach_identischem_added_loescht_beide_haelften():
    """„Dienst entfernt · FRA-BCN-FRA" über „Neuer Dienst · FRA-BCN-FRA"."""
    pending = [_entry(D2, 'added', new=_tag_bcn(D2), ago_sec=0.583)]
    neu = [{'datum': D2, 'kind': 'removed', 'old': _tag_bcn(D2)}]
    assert A._rc_cancel_reversals(neu, [pending, []]) == []
    assert pending == []


def test_added_nach_identischem_removed_loescht_beide_haelften():
    """Gleiche Regel in der anderen Reihenfolge (Quelle B schrieb zuerst)."""
    pending = [_entry(D2, 'removed', old=_tag_bcn(D2), ago_sec=1.2)]
    neu = [{'datum': D2, 'kind': 'added', 'new': _tag_bcn(D2)}]
    assert A._rc_cancel_reversals(neu, [pending, []]) == []
    assert pending == []


def test_bereits_archivierter_eintrag_wird_ebenso_aufgehoben():
    """Im Befund standen die Hälften teils als 'accepted'/'past_auto' im
    VERLAUF — die Liste zeigt sie trotzdem. Also auch dort löschen."""
    history = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=0.6)]
    history[0]['status'] = 'past_auto'
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
    assert A._rc_cancel_reversals(neu, [[], history]) == []
    assert history == []


# ── 2) GEGENPROBE: eine ECHTE Rücknahme muss erhalten bleiben ───────────────
def test_gegenprobe_echte_ruecknahme_nach_stunden_bleibt_vollstaendig():
    """Die Planung nimmt FRA-SFO, gibt ihn aber Stunden später wirklich
    zurück. Substanz und Richtung sehen GENAUSO aus wie beim Phantom-Paar —
    unterschieden wird ausschließlich über den zeitlichen Abstand."""
    pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=6 * 3600)]
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
    ueberlebt = A._rc_cancel_reversals(neu, [pending, []])
    assert len(ueberlebt) == 1, 'echte Rücknahme darf nicht verschluckt werden'
    assert len(pending) == 1, 'die ursprüngliche Änderung bleibt im Verlauf'


def test_gegenprobe_echte_ruecknahme_am_naechsten_tag_bleibt():
    pending = [_entry(D2, 'removed', old=_tag_bcn(D2), ago_sec=26 * 3600)]
    neu = [{'datum': D2, 'kind': 'added', 'new': _tag_bcn(D2)}]
    assert len(A._rc_cancel_reversals(neu, [pending, []])) == 1
    assert len(pending) == 1


def test_gegenprobe_fenstergrenze_ist_scharf():
    """Knapp innerhalb → aufgehoben. Knapp außerhalb → beide bleiben."""
    for ago, erwartet_geloescht in ((A._RC_SYNC_WINDOW_SEC - 5, True),
                                    (A._RC_SYNC_WINDOW_SEC + 5, False)):
        pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                          ago_sec=ago)]
        neu = [{'datum': D1, 'kind': 'modified',
                'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
        ueberlebt = A._rc_cancel_reversals(neu, [pending, []])
        assert (ueberlebt == []) is erwartet_geloescht
        assert (pending == []) is erwartet_geloescht


# ── 3) Die Regel darf nur EXAKTE Spiegelungen aufheben ──────────────────────
def test_weiterfuehrende_aenderung_bleibt_stehen():
    """A → B, dann B → C (nicht zurück nach A): das ist eine echte
    Folge-Änderung und muss stehen bleiben, auch binnen Sekunden."""
    pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=0.5)]
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_bcn(D1)}]
    assert len(A._rc_cancel_reversals(neu, [pending, []])) == 1
    assert len(pending) == 1


def test_anderer_tag_hebt_nicht_auf():
    pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=0.5)]
    neu = [{'datum': D2, 'kind': 'modified',
            'old': _tag_ath(D2), 'new': _tag_sfo(D2)}]
    assert len(A._rc_cancel_reversals(neu, [pending, []])) == 1
    assert len(pending) == 1


def test_ein_eintrag_hebt_nur_einen_gegenpart_auf():
    """Zwei gleiche Rückkipper dürfen nicht denselben Bestandseintrag
    zweimal verrechnen (sonst verschwände eine echte Änderung)."""
    pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=0.4)]
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)},
           {'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
    ueberlebt = A._rc_cancel_reversals(neu, [pending, []])
    assert len(ueberlebt) == 1
    assert pending == []


def test_eintrag_ohne_zeitstempel_wird_nicht_aufgehoben():
    """Ohne `detected_at` ist der Abstand unbekannt — Lücke ≠ Fakt, also
    NICHT löschen (fail-closed gegen Datenverlust)."""
    alt = _entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1))
    alt.pop('detected_at')
    pending = [alt]
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
    assert len(A._rc_cancel_reversals(neu, [pending, []])) == 1
    assert len(pending) == 1


def test_leere_eingabe_und_muell_werfen_nicht():
    assert A._rc_cancel_reversals([], [[], []]) == []
    assert A._rc_cancel_reversals(None, [None]) == []
    assert A._rc_cancel_reversals([{'kind': 'quatsch'}], [[], []]) == \
        [{'kind': 'quatsch'}]


def test_push_hysterese_bleibt_beim_aufheben_unberuehrt():
    """Die Regel räumt den VERLAUF auf, nicht das Push-Gedächtnis.

    Erster Entwurf verwarf beim Aufheben auch die `push_state`-Vermerke des
    Paares (Gedanke: der Vermerk stammt aus einem Sync-Konflikt). Ergebnis:
    `test_endpoint_flipflop_pusht_hoechstens_einmal` stieg von 2 auf 4 Pushes
    — die Flip-Flop-Dämpfung der Owner-Eskalationen 26./28.07. verlor genau
    das Gedächtnis, aus dem sie lebt. Der Vermerk bleibt deshalb stehen."""
    now = datetime.now()
    sig_ath = A._rc_state_sig(_tag_ath(D1))
    push_state = {D1: [[sig_ath, (now - timedelta(seconds=0.5)).isoformat()]]}
    vorher = json.loads(json.dumps(push_state))
    pending = [_entry(D1, 'modified', old=_tag_sfo(D1), new=_tag_ath(D1),
                      ago_sec=0.5, now=now)]
    neu = [{'datum': D1, 'kind': 'modified',
            'old': _tag_ath(D1), 'new': _tag_sfo(D1)}]
    assert A._rc_cancel_reversals(neu, [pending, []], now=now) == []
    assert pending == []
    assert push_state == vorher


# ── 4) Ende-zu-Ende am Endpoint: der Befund als Wiederholung ───────────────
def _snapshot_roundtrip(days_runs, tmp_state):
    """Spielt `days_runs` nacheinander als POST /api/user/roster-snapshot ab
    und gibt den resultierenden roster_changes-Payload zurück."""
    tok = 'AT-TEST-RC-REVERSAL'
    snap = {'payload': {}}
    changes = {'payload': None}

    def _snap_read(_t):
        return snap['payload']

    def _snap_save(_t, p):
        snap['payload'] = p
        return True

    def _ch_read(_t):
        return changes['payload']

    def _ch_save(_t, d):
        changes['payload'] = d
        return True

    with patch.object(A, '_roster_snapshot_read', side_effect=_snap_read), \
         patch.object(A, '_roster_snapshot_save', side_effect=_snap_save), \
         patch.object(A, '_roster_changes_read', side_effect=_ch_read), \
         patch.object(A, '_roster_changes_save', side_effect=_ch_save), \
         patch.object(A, '_crew_flight_ingest', return_value=None), \
         patch.object(A, '_push_notify_async', return_value=None), \
         patch.object(A, '_profile_homebase_cached', return_value='FRA'), \
         patch.object(A, '_validate_token', return_value=A._TokenValidationResult(
             A._TokenValidationState.VALID, f'{tok}@test.invalid')):
        c = A.app.test_client()
        for days in days_runs:
            r = c.post(f'/api/user/roster-snapshot/{tok}',
                       json={'tage': days},
                       headers={'Authorization': f'Bearer {tok}'})
            assert r.status_code == 200, r.get_data(as_text=True)
    return changes['payload'] or {'pending': [], 'history': []}


def _heute_plus(n):
    from datetime import date
    return (date.today() + timedelta(days=n)).isoformat()


def test_endpunkt_zwei_quellen_im_selben_sync_hinterlassen_keine_eintraege(
        tmp_path):
    """Der Befund als Wiederholung: Baseline, dann Quelle B, 583 ms später
    Quelle A — am Ende darf KEIN Eintrag in der Liste stehen, und der
    Snapshot muss wieder den echten Stand tragen."""
    # Beide Tage in der ZUKUNFT (wie das Original-Fixture 17./16.08. am
    # 16.08.): mit d2=heute kippte der Test nach Feierabend der Tagesflüge,
    # weil die Entfernung dann ehrlich als `past_auto` in den Verlauf wandert —
    # das ist past-Logik, nicht der Rückkipper, den dieser Test festnagelt.
    d1, d2 = _heute_plus(2), _heute_plus(1)
    echt = [_tag_sfo(d1), _tag_bcn(d2)]
    fremd = [_tag_ath(d1)]                     # Quelle B: anderer Tag-Stand,
    #                                            der 16.08. fehlt ihr ganz
    payload = _snapshot_roundtrip([echt, fremd, echt], tmp_path)
    offen = [e for e in (payload.get('pending') or [])]
    verlauf = [e for e in (payload.get('history') or [])]
    assert offen == [], f'Phantom-Einträge offen: {offen}'
    assert verlauf == [], f'Phantom-Einträge im Verlauf: {verlauf}'


def test_endpunkt_echte_aenderung_ueberlebt_den_sync(tmp_path):
    """Gegenprobe am Endpoint: bleibt der zweite Schreiber bei einem NEUEN
    Stand (keine Rückkehr zur Basis), muss der Eintrag stehen bleiben."""
    d1 = _heute_plus(1)
    payload = _snapshot_roundtrip(
        [[_tag_sfo(d1)], [_tag_ath(d1)]], tmp_path)
    alle = (payload.get('pending') or []) + (payload.get('history') or [])
    assert len(alle) == 1 and alle[0]['kind'] == 'modified'
    assert alle[0]['new']['routing'] == 'FRA-ATH'
