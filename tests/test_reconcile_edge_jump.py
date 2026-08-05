"""Edge-Jump im Reconcile-Fenster (Dominik Ift, Condor-FA, 2026-08-05).

Meldung: „krankgemeldet, Plan aktualisiert sich nicht mehr, Flug steht als
abgeflogen drin". Forensik: `_reconcile_month_briefings` räumte nur im Fenster
[fmin..fmax] mit fmin = min(feed_dates). Eine Krankmeldung BLANKT im
monatsverankerten Condor-Feed die führenden Monatstage → der Unterrand sprang
von 01.08. auf 06.08. → der gestrichene Flugtag 03.08. lag UNTER fmin und wurde
nie geräumt (575 Ghost-Blockminuten in FTL/Fatigue). Der Vormonats-Clamp hebt
fmin nur AN, er senkt nie.

Fix: den Unterrand beim Import mitschreiben (`feed_min`/`feed_min_at`) und beim
nächsten Import prüfen, ob er SCHNELLER gewandert ist als die Zeit vergangen ist.
Rollende Feeds (LH: ≤1 Tag pro Tag) bleiben unangetastet, der Vormonats-Clamp
bleibt hart (Flugbuch-Beweisgate-Regression 2026-08-05).
"""
import logging
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as backend


def _d(offset):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y-%m-%d')


def _month_start():
    return datetime.now().replace(day=1).strftime('%Y-%m-%d')


def _previous_month_start():
    first = datetime.now().replace(day=1)
    return (first - timedelta(days=1)).replace(day=1).strftime('%Y-%m-%d')


def _event(day, summary='DE 1234 FRA-PMI'):
    return {
        'summary': summary,
        'location': 'FRA',
        'start': day,
        'start_iso': day + 'T08:00:00',
        '_multiday_dates': [day],
    }


def _flight_day(day, flight='DE1234'):
    """Gespeicherter Tagessatz mit Sektoren — genau die Form, die der
    FTL-/Fatigue-Rechner als Blockminuten liest."""
    return {
        'ical_summary': f'{flight} FRA-PMI',
        'ical_start_iso': day + 'T08:00:00',
        'block_minutes': 575,
        'ical_sectors': [{
            'flight': flight,
            'from': 'FRA',
            'to': 'PMI',
            'dep_iso': day + 'T08:00:00Z',
            'arr_iso': day + 'T10:20:00Z',
        }],
        'legs': [{'flight': flight, 'from': 'FRA', 'to': 'PMI'}],
    }


def test_blanked_month_head_is_cleared_after_edge_jump(caplog):
    """KERNFALL: der Unterrand springt 6 Tage nach vorn, obwohl seit dem letzten
    Import nur 1 Tag vergangen ist → geblankt, nicht gerollt. Das Fenster sinkt
    auf den alten Rand zurück und der Ghost-Flugtag verschwindet."""
    prev_edge = _d(-5)
    ghost_day = _d(-2)
    briefings = {ghost_day: _flight_day(ghost_day)}
    feed = [_event(_d(1)), _event(_d(2)), _event(_d(3))]

    with caplog.at_level(logging.INFO):
        dbg = backend._reconcile_month_briefings(
            'TESTTOKEN_EDGEJUMP', briefings, feed,
            prev_feed_min=prev_edge,
            prev_feed_min_at=(datetime.now() - timedelta(days=1)).isoformat())

    assert ghost_day not in briefings, \
        'Der gestrichene Flugtag unter dem neuen Feed-Rand muss geräumt werden'
    assert ghost_day in (dbg.get('removed_dates') or []), \
        f'removed_dates muss den geräumten Tag melden, got {dbg}'
    rendered = '\n'.join(r.getMessage() for r in caplog.records)
    assert '[ical-reconcile] edge-jump:' in rendered, rendered
    assert f'fenster-gesenkt-auf={max(prev_edge, _previous_month_start())}' \
        in rendered, rendered
    assert dbg['window'].startswith(max(prev_edge, _previous_month_start()))


def test_rolling_feed_edge_is_not_lowered():
    """Ein normal ROLLENDER Feed (LH/myTime) wandert höchstens einen Tag pro Tag.
    8 Tage Sprung bei 10 Tagen Pause ist kein Sprung → Fenster unverändert, alte
    Tage unter fmin bleiben stehen."""
    prev_edge = _d(-20)
    new_edge = _d(-12)
    below_edge = _d(-15)
    briefings = {below_edge: _flight_day(below_edge, 'LH400')}
    feed = [_event(new_edge, 'LH401 FRA-JFK'), _event(_d(2), 'LH402 JFK-FRA')]

    dbg = backend._reconcile_month_briefings(
        'TESTTOKEN_ROLLING', briefings, feed,
        prev_feed_min=prev_edge,
        prev_feed_min_at=(datetime.now() - timedelta(days=10)).isoformat())

    expected_fmin = max(new_edge, _previous_month_start())
    assert dbg['window'].startswith(expected_fmin), \
        f'Rollender Feed darf das Fenster nicht senken: {dbg}'
    assert 'edge_jump' not in dbg
    assert below_edge in briefings and briefings[below_edge].get('ical_sectors'), \
        'Tage unter dem rollenden Rand bleiben Historie'
    assert not (dbg.get('removed_dates') or [])


def test_edge_jump_never_digs_below_previous_month_start():
    """Vormonats-Clamp bleibt HART: ein uralter gespeicherter Rand darf das
    Räumfenster nicht in eingefrorene Historie ziehen (Flugbuch-Beweisgate)."""
    prev_month_start = _previous_month_start()
    ancient_edge = (datetime.strptime(prev_month_start, '%Y-%m-%d')
                    - timedelta(days=40)).strftime('%Y-%m-%d')
    frozen_day = (datetime.strptime(prev_month_start, '%Y-%m-%d')
                  - timedelta(days=1)).strftime('%Y-%m-%d')
    briefings = {frozen_day: _flight_day(frozen_day, 'DE9999')}
    feed = [_event(_d(0)), _event(_d(1))]

    dbg = backend._reconcile_month_briefings(
        'TESTTOKEN_CLAMP', briefings, feed,
        prev_feed_min=ancient_edge,
        prev_feed_min_at=(datetime.now() - timedelta(days=1)).isoformat())

    assert dbg['window'].startswith(prev_month_start), \
        f'Fenster darf nie unter den Vormonatsanfang: {dbg}'
    assert frozen_day in briefings, 'ältere Historie bleibt eingefroren'
    assert briefings[frozen_day].get('ical_sectors')


def test_full_clean_lowers_window_to_current_month_start():
    """`full_clean=True` erklärt den Feed zur Autorität für den GANZEN laufenden
    Monat — auch ohne bekannten Vorrand (Alt-Profile, iOS-Geräte-Pfad)."""
    month_start = _month_start()
    feed_first = (datetime.strptime(month_start, '%Y-%m-%d')
                  + timedelta(days=10)).strftime('%Y-%m-%d')
    feed_last = (datetime.strptime(month_start, '%Y-%m-%d')
                 + timedelta(days=12)).strftime('%Y-%m-%d')
    feed = [_event(feed_first), _event(feed_last)]

    # Ohne full_clean: der Monatsanfang liegt UNTER fmin → unangetastet.
    briefings_plain = {month_start: _flight_day(month_start)}
    dbg_plain = backend._reconcile_month_briefings(
        'TESTTOKEN_NOFULL', briefings_plain, feed)
    assert dbg_plain['window'].startswith(feed_first)
    assert month_start in briefings_plain

    # Mit full_clean: Fenster sinkt auf den Monatsanfang → Ghost weg.
    briefings_full = {month_start: _flight_day(month_start)}
    dbg_full = backend._reconcile_month_briefings(
        'TESTTOKEN_FULL', briefings_full, feed, full_clean=True)
    assert dbg_full['window'].startswith(month_start), dbg_full
    assert month_start not in briefings_full
    assert month_start in (dbg_full.get('removed_dates') or [])


def test_past_day_guard_skips_freshly_reconciled_days():
    """PFLICHT-BEGLEITFIX: ohne `skip_dates` restauriert die Vergangenheits-
    Planke den gerade geräumten Tag sofort wieder aus `existing` — der Ghost
    wäre zurück, bevor der Save ihn los ist. Ein NICHT geräumter
    Vergangenheits-Tag mit Sektoren bleibt weiterhin geschützt."""
    cleared_day = _d(-2)
    protected_day = _d(-3)
    existing = {cleared_day: _flight_day(cleared_day, 'DE1234'),
                protected_day: _flight_day(protected_day, 'DE5678')}
    briefings = {
        cleared_day: {'ical_summary': 'Krank'},
        protected_day: {'ical_summary': 'Layover [PMI] (Tag 2/2)'},
    }

    kept = backend._preserve_past_flown_days(
        briefings, existing, skip_dates=[cleared_day])

    assert not (briefings[cleared_day].get('ical_sectors') or []), \
        'Geräumter Tag darf NICHT aus der Historie wiederbelebt werden'
    assert [s.get('flight') for s in briefings[protected_day]['ical_sectors']] \
        == ['DE5678'], 'Bestandsverhalten: ungeräumte geflogene Tage bleiben'
    assert kept == 1


def test_feed_min_stamp_only_moves_timestamp_when_edge_moves():
    """`feed_min_at` beantwortet „seit wann steht der Rand hier?" — ein Re-Import
    mit UNVERÄNDERTEM Rand darf den Zeitstempel nicht erneuern, sonst hätte ein
    häufig pollender Feed nie „verstrichene Tage" vorzuweisen."""
    feed = [_event(_d(0)), _event(_d(3))]
    obj = {}
    backend._calendar_feed_stamp_feed_min(obj, feed)
    assert obj['feed_min'] == _d(0)
    first_stamp = obj['feed_min_at']

    backend._calendar_feed_stamp_feed_min(obj, feed)
    assert obj['feed_min_at'] == first_stamp, 'gleicher Rand → gleicher Stempel'

    backend._calendar_feed_stamp_feed_min(obj, [_event(_d(2)), _event(_d(3))])
    assert obj['feed_min'] == _d(2)
    assert obj['feed_min_at'] != first_stamp, 'neuer Rand → neuer Stempel'


def _ics(days):
    """Minimaler Direkt-ICS-Text mit je einem Termin pro Tag."""
    rows = ['BEGIN:VCALENDAR', 'VERSION:2.0']
    for day in days:
        d8 = day.replace('-', '')
        rows.extend([
            'BEGIN:VEVENT',
            f'UID:edge-{d8}@example.test',
            f'DTSTART:{d8}T080000Z',
            f'DTEND:{d8}T090000Z',
            'SUMMARY:OFF',
            'END:VEVENT',
        ])
    rows.append('END:VCALENDAR')
    return '\r\n'.join(rows)


def _stateful_persistence(monkeypatch):
    """Profil + Briefings überleben zwischen zwei Import-Requests — nur so ist
    die Edge-Jump-Erkennung überhaupt prüfbar (sie liest den beim VORIGEN Import
    geschriebenen `feed_min`)."""
    store = {'profile': {}, 'briefings': {}}
    monkeypatch.setattr(backend, '_profile_load',
                        lambda _t: {'profile': dict(store['profile'])})
    monkeypatch.setattr(backend, '_profile_load_from_disk',
                        lambda _t: {'profile': dict(store['profile'])})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda _t, profile, full_disk_payload=None:
            store.update({'profile': dict(profile)}) or True)
    monkeypatch.setattr(
        backend, '_ical_briefings_load',
        lambda _t: {k: dict(v) for k, v in store['briefings'].items()})
    monkeypatch.setattr(
        backend, '_ical_briefings_save',
        lambda _t, b: store.update(
            {'briefings': {k: dict(v) for k, v in b.items()}}) or True)
    monkeypatch.setattr(backend, '_manual_briefings_load', lambda _t: {})
    monkeypatch.setattr(backend, '_manual_briefings_save', lambda _t, m: True)
    return store


def test_import_endpoint_persists_edge_and_clears_blanked_days(monkeypatch):
    """END-TO-END über den Direkt-ICS-Import: Runde 1 schreibt den Feed-Rand ins
    Profil, Runde 2 (geblankter Monatsanfang) erkennt den Sprung und räumt den
    gestrichenen Flugtag — ohne dass die Vergangenheits-Planke ihn zurückholt."""
    store = _stateful_persistence(monkeypatch)
    tok = 'AT-TEST-EDGEJUMP-000000'
    monkeypatch.setattr(
        backend, '_validate_token',
        lambda t: backend._TokenValidationResult(
            backend._TokenValidationState.VALID))
    client = backend.app.test_client()
    headers = {'Authorization': f'Bearer {tok}'}

    r1 = client.post(f'/api/user/calendar-feed/{tok}/import', headers=headers,
                     json={'ics_text': _ics([_d(i) for i in range(-4, 4)])})
    assert r1.status_code == 200 and r1.get_json().get('ok'), r1.get_json()
    feed = store['profile'].get('calendar_feed') or {}
    assert feed.get('feed_min') == _d(-4), feed
    assert feed.get('feed_min_at')

    # Gestrichener Flugtag aus einem früheren Import (Sektoren = Blockminuten).
    # Das Wetter-Feld ist Absicht: der Tagessatz bleibt nach dem Prune der
    # ical_*-Felder BESTEHEN (nur eben leer) — genau die Konstellation, in der
    # `_preserve_past_flown_days` den Ghost ohne `skip_dates` sofort wieder aus
    # der Historie zurückholen würde.
    ghost = _d(-2)
    store['briefings'][ghost] = dict(_flight_day(ghost),
                                     weather_summary='Sonnig, 24 °C')

    # Krankmeldung: der Feed blankt alles bis einschließlich heute.
    r2 = client.post(f'/api/user/calendar-feed/{tok}/import', headers=headers,
                     json={'ics_text': _ics([_d(i) for i in range(1, 4)])})
    assert r2.status_code == 200 and r2.get_json().get('ok'), r2.get_json()
    rec = r2.get_json().get('reconcile') or {}
    assert ghost in (rec.get('removed_dates') or []), rec
    assert not (store['briefings'].get(ghost, {}).get('ical_sectors') or []), \
        'Ghost-Blockminuten müssen nach dem Reconcile weg sein'
    assert store['briefings'].get(ghost, {}).get('weather_summary'), \
        'Nicht-iCal-Felder des Tages bleiben erhalten'
    assert (store['profile']['calendar_feed'].get('feed_min')) == _d(1)


def test_feed_min_stamp_reads_raw_eventkit_events():
    """Der iOS-EKEventStore-Push stempelt VOR der Adaption — die Roh-Events
    tragen nur `start_iso`. Ohne den ISO-Fallback bliebe `feed_min` dort leer
    und die Edge-Jump-Erkennung wäre auf dem Geräte-Pfad tot."""
    raw = [{'summary': 'DE 1234 FRA-PMI', 'location': 'FRA',
            'start_iso': _d(4) + 'T08:00:00', 'end_iso': _d(4) + 'T10:20:00'},
           {'summary': 'DE 1235 PMI-FRA', 'location': 'PMI',
            'start_iso': _d(1) + 'T11:00:00', 'end_iso': _d(1) + 'T13:20:00'}]
    obj = {}
    backend._calendar_feed_stamp_feed_min(obj, raw, iso_fallback=True)
    assert obj['feed_min'] == _d(1)

    # Ohne Fallback (Reconcile-Semantik) decken diese Roh-Events keinen Tag ab.
    assert backend._feed_covered_dates(raw) == set()
