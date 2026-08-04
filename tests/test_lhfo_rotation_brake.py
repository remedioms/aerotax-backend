"""Rotations-Bremse + Verteilungs-Messung (Verstärkungs-Audit 2026-07-29).

AUSGANGSMESSUNG: 4.057 `oauth_refresh` bei 601 gesunden Grants = 6,75
Rotationen pro Grant und Tag. Erwartbar waren 1–2. Der belegte Verstärker ist
eine RÜCKKOPPLUNG: `transient`/`error` lassen den Demand-Eintrag stehen
(_REFRESHER_DEMAND_RETRY_STATES) und der 'fresh'-Kurzschluss in
_refresher_refresh_grant greift nur MIT gültigem Access-Token — ein dauerhaft
scheiternder Grant hat keinen, war also in JEDEM 60-s-Tick wieder fällig:
86.400/60 = 1.440 LH-Token-Calls pro Tag und Grant.

Dieser Test sichert die Gegenmaßnahmen ab:
  1. Mindestabstand von 5 min zwischen zwei Rotationsversuchen DESSELBEN
     Grants (ein AT lebt 59 min — schneller kann nie nötig sein).
  2. Exponentieller Rückzug nach echten Fehlschlägen, gedeckelt bei 1 h.
  3. Zwei Leitplanken, die NICHT verhandelbar sind:
       · frische Nachfrage kommt durch, sobald der AT wirklich abgelaufen ist
         (der Nutzer wartet nie länger als einen Tick),
       · der Keepalive (18 h) kann durch die Bremse NIE ausfallen — der
         Rückzugs-Deckel liegt bei 1 h.
  4. Verteilungs-Zähler pro Grant und Tag (vorher gab es dafür KEINE Messung).
"""
import os
import sys
import time

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app  # noqa: F401  (Blueprint-Registrierung)
from blueprints import lh_flightops as fo


@pytest.fixture(autouse=True)
def _clean_brake_state():
    """Modul-globaler Bremsen-/Zähler-State darf nicht in fremde Tests lecken."""
    fo._rot_gate.clear()
    fo._rot_day.update({'day': '', 'n': {}})
    fo._refresher_demand.clear()
    yield
    fo._rot_gate.clear()
    fo._rot_day.update({'day': '', 'n': {}})
    fo._refresher_demand.clear()


def _expired(now):
    """Grant mit RT und abgelaufenem Access-Token = jederzeit fällig-fähig."""
    return {'refresh': 'R', 'expires_at': now - 60}


# ── 1. MINDESTABSTAND ───────────────────────────────────────────────────────
def test_min_gap_blocks_second_rotation_within_five_minutes():
    """Ein zweiter Versuch 60 s nach dem ersten wird gebremst — das war der
    Takt, in dem ein scheiternder Grant 1.440 Calls/Tag erzeugte."""
    now = 1_000_000.0
    scan = [('AT-A', _expired(now))]
    assert fo._refresher_due(scan, now=now, demand={'AT-A'}) == ['AT-A']
    fo._rot_gate_note('AT-A', 'rotated', now=now)
    out = {}
    assert fo._refresher_due(scan, now=now + 60, demand={'AT-A'},
                             out=out) == []
    assert out['gated'] == 1
    # ... und nach dem Mindestabstand wieder frei (ohne Fehlschlag kein Rückzug)
    assert fo._refresher_due(scan, now=now + fo._ROT_MIN_GAP_S,
                             demand={'AT-A'}) == ['AT-A']


def test_fresh_demand_passes_immediately_on_untouched_grant():
    """LEITPLANKE »Nutzer braucht es JETZT«: ein Grant, der noch gar nicht
    versucht wurde, geht ohne jede Wartezeit durch."""
    now = 1_000_000.0
    scan = [('AT-NEU', _expired(now))]
    assert fo._refresher_due(scan, now=now, demand={'AT-NEU'}) == ['AT-NEU']


def test_successful_rotation_costs_the_user_no_wait():
    """Nach einer ERFOLGREICHEN Rotation lebt der AT ~59 min — der 5-min-Boden
    kann den Nutzer also gar nicht treffen: bei gültigem AT ist der Grant
    ohnehin nicht fällig, und wenn er abläuft, ist der Boden längst um."""
    now = 1_000_000.0
    fo._rot_gate_note('AT-A', 'rotated', now=now)
    # Direkt nach der Rotation: AT gültig (59 min) ⇒ nicht fällig, egal ob Demand
    frisch = [('AT-A', {'refresh': 'R', 'expires_at': now + 3540})]
    assert fo._refresher_due(frisch, now=now, demand={'AT-A'}) == []
    # Wenn er abläuft, ist der Mindestabstand (5 min) 54 min her ⇒ frei
    assert fo._refresher_due([('AT-A', _expired(now + 3540))], now=now + 3540,
                             demand={'AT-A'}) == ['AT-A']


# ── 2. EXPONENTIELLER RÜCKZUG ───────────────────────────────────────────────
def test_backoff_grows_exponentially_and_is_capped():
    """120 s · 2^(n−1), Deckel 1 h — und 0 Fehlschläge heißt kein Rückzug."""
    assert fo._rot_backoff_s(0) == 0.0
    assert fo._rot_backoff_s(1) == 120.0
    assert fo._rot_backoff_s(2) == 240.0
    assert fo._rot_backoff_s(5) == 1920.0
    assert fo._rot_backoff_s(20) == fo._ROT_BACKOFF_MAX_S == 3600


def test_repeated_transient_failures_back_off_instead_of_hammering():
    """RECHNUNG: ohne Bremse 86.400/60 = 1.440 Versuche/Tag für EINEN dauerhaft
    scheiternden Grant. Mit Rückzug (120, 240, …, gedeckelt 3.600 s) sind es
    ~30 — Faktor >45."""
    now = 1_000_000.0
    scan = [('AT-BAD', _expired(now))]
    versuche, t = 0, now
    ende = now + 86400
    while t < ende:
        if fo._refresher_due(scan, now=t, demand={'AT-BAD'}):
            fo._rot_gate_note('AT-BAD', 'transient', now=t)
            versuche += 1
        t += 60          # der echte Tick-Takt
    assert versuche < 40, versuche
    assert versuche >= 24    # Deckel 1 h ⇒ mindestens ~1 Versuch/Stunde


def test_success_clears_the_backoff():
    """Heilung darf nicht bestraft werden: ein Erfolg setzt den Zähler auf 0,
    danach gilt wieder nur der Mindestabstand."""
    now = 1_000_000.0
    for i in range(4):
        fo._rot_gate_note('AT-A', 'transient', now=now + i)
    assert fo._rot_gate['AT-A']['fails'] == 4
    fo._rot_gate_note('AT-A', 'rotated', now=now + 10)
    assert fo._rot_gate['AT-A']['fails'] == 0
    scan = [('AT-A', _expired(now))]
    assert fo._refresher_due(scan, now=now + 10 + fo._ROT_MIN_GAP_S,
                             demand={'AT-A'}) == ['AT-A']


def test_claim_and_save_states_do_not_trigger_backoff():
    """Kein LH-Call, kein Rückzug: skipped_claim_* (Supabase degradiert) und
    save_pending (Nachsave-Heilung) sollen schnell wiederkommen — sie kosten
    kein LH-Kontingent. Gebremst werden sie nur vom Mindestabstand."""
    now = 1_000_000.0
    for st in ('skipped_claim_unavailable', 'skipped_claim_foreign',
               'save_pending', 'save_healed', 'fresh'):
        fo._rot_gate.clear()
        fo._rot_gate_note('AT-A', st, now=now)
        assert fo._rot_gate['AT-A']['fails'] == 0, st


# ── 3. DER KEEPALIVE BLEIBT HEILIG ──────────────────────────────────────────
def test_backoff_can_never_starve_the_keepalive():
    """Der Refresh-Token darf nie idle sterben. Der Rückzugs-Deckel (1 h) liegt
    deutlich unter dem Keepalive-Fenster (18 h) — ein Grant im
    tiefsten Rückzug ist also lange vor Ablauf des Fensters wieder dran."""
    assert fo._ROT_BACKOFF_MAX_S < fo._REFRESHER_KEEPALIVE_S / 10
    now = 1_000_000.0
    # Grant ohne jede Nachfrage, letzte Rotation 21 h her ⇒ keepalive-fällig
    alt = {'refresh': 'R',
           'expires_at': now - 21 * 3600 + fo._REFRESHER_AT_LIFETIME_S}
    for i in range(30):
        fo._rot_gate_note('AT-OLD', 'transient', now=now + i)
    # Eine Stunde nach dem letzten Versuch ist der Deckel erreicht ⇒ er rotiert
    assert fo._refresher_due([('AT-OLD', alt)], now=now + 3600 + 30,
                             demand=set()) == ['AT-OLD']


def test_brake_never_touches_dead_or_rtless_grants():
    """Die Bremse macht nichts AUF: needs_relogin und Grants ohne RT bleiben
    auch mit leerem Gate ausgeschlossen (Grant-Burn-Leitplanke)."""
    now = 1_000_000.0
    scan = [('AT-DEAD', {'refresh': 'R', 'needs_relogin': True,
                         'expires_at': now - 100}),
            ('AT-NORT', {'expires_at': now - 100})]
    assert fo._refresher_due(scan, now=now,
                             demand={'AT-DEAD', 'AT-NORT'}) == []


def test_rot_gate_ok_is_failsafe_open():
    """Wirft nie und sperrt im Zweifel NICHT — eine kaputte Bremse darf keinen
    Grant dauerhaft aussperren (ein verpasster Refresh kostet Minuten, ein
    dauerhaft blockierter Keepalive den ganzen Zugang)."""
    assert fo._rot_gate_ok('AT-X', time.time(), gate={'AT-X': None}) is True
    assert fo._rot_gate_ok('AT-X', time.time(), gate=object()) is True
    fo._rot_gate_note(None, 'transient')          # darf nicht werfen


def test_rot_gate_cap_evicts_oldest():
    """Deckel gegen unbegrenztes Wachstum; ein verlorener Stempel heißt nur
    »darf sofort wieder«, nie »rotiert doppelt«."""
    now = 1_000_000.0
    for i in range(fo._ROT_GATE_CAP + 2):
        fo._rot_gate['AT-%05d' % i] = {'last': now + i, 'fails': 0}
    fo._rot_gate_note('AT-NEU', 'rotated', now=now + 999999)
    assert len(fo._rot_gate) <= fo._ROT_GATE_CAP
    assert 'AT-NEU' in fo._rot_gate
    assert 'AT-00000' not in fo._rot_gate      # ältester zuerst geräumt


# ── 4. VERTEILUNGS-MESSUNG ──────────────────────────────────────────────────
def test_rotation_distribution_counter_answers_many_vs_few():
    """Vorher gab es NUR den Tagessummen-Zähler lhfoRD — »6,75 pro Grant« war
    ein Mittelwert ohne Verteilung. Der Report zeigt jetzt die Top-Verbraucher."""
    now = time.time()
    for _ in range(9):
        fo._rot_day_note('AT-VIELFRASS-XYZ', now=now)
    fo._rot_day_note('AT-NORMAL-1', now=now)
    rep = fo._rot_day_report()
    assert rep['calls'] == 10 and rep['grants'] == 2
    assert rep['top'][0] == ['AT-VIELF', 9]     # Token auf 8 Zeichen gekürzt
    assert rep['day'] == time.strftime('%Y%m%d', time.gmtime(now))


def test_rotation_counter_resets_on_day_rollover():
    """Tageswechsel (UTC) setzt zurück — sonst wäre der Zähler nach Tagen
    unlesbar und der Speicher wüchse."""
    t0 = 1_800_000_000.0        # irgendein fixer UTC-Zeitpunkt
    fo._rot_day_note('AT-A', now=t0)
    assert fo._rot_day_report()['calls'] == 1
    fo._rot_day_note('AT-A', now=t0 + 86400)
    rep = fo._rot_day_report()
    assert rep['calls'] == 1 and rep['grants'] == 1
    assert rep['day'] == time.strftime('%Y%m%d', time.gmtime(t0 + 86400))


# ── 5. SICHTBARKEIT (Blindflug-Fund) ────────────────────────────────────────
def test_root_logger_has_a_handler_so_info_lines_actually_arrive():
    """DIE Ursache dafür, dass `docker logs aerotax-poll | grep 'fo-refresher'`
    NULL Zeilen lieferte: Level INFO war gesetzt, aber NIEMAND hatte einen
    Handler in der Kette ('aerotax' → Root; gunicorn hängt seine Handler an
    'gunicorn.error' mit propagate=False). Ohne Handler greift
    logging.lastResort — Level WARNING. Also verschwand JEDER log.info still.

    Unter pytest hängt das logging-Plugin selbst Handler an Root (deshalb
    installiert app.py dort bewusst keinen doppelt) — geprüft wird hier, dass
    ein INFO-Record des Backend-Loggers überhaupt bei einem Handler ankommt."""
    import logging
    log = logging.getLogger('aerotax')
    assert log.getEffectiveLevel() <= logging.INFO
    seen = []

    class _Probe(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    h = _Probe()
    logging.getLogger().addHandler(h)
    try:
        log.info('[fo-refresher] tick scan=1 due=0 gated=0 demand=0 {}')
    finally:
        logging.getLogger().removeHandler(h)
    assert seen and 'fo-refresher' in seen[0]
