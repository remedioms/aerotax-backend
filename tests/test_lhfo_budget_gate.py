"""FlightOps-Key-Budget-Gate (Quota-403-Vorfall 27.07. ~21 UTC).

Der lhfo-Key (1.000 Calls/h) wurde von Hintergrund-Syncs gesprengt → LH
403te pauschal — auch auf die Roster-Abrufe direkt nach „Neu verbinden":
die Re-Login-Heilung schlug fehl. Jetzt: Hintergrund stoppt bei 700/h,
interaktive Flows dürfen bis 950/h, darüber stoppt alles BEVOR LH 403t.
oauth_refresh (Ein-Refresher) bleibt bewusst ungegatet.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import app  # noqa: F401  (Blueprint-Registrierung)
from blueprints import lh_flightops as fo


def _with_access(**kw):
    return patch.object(fo, '_valid_access', return_value='AT-FAKE-ACCESS')


def test_background_call_blocked_at_background_ceiling():
    calls = []
    with _with_access(), \
         patch.object(fo, '_rot_hour_used',
                      return_value=fo._LHFO_HOUR_BACKGROUND_CEILING), \
         patch.object(fo, '_flightops_budget_inc',
                      side_effect=lambda p: calls.append(p)):
        assert fo._api_get('tok', '/COMMON_DUTY_EVENTS') is None
    assert calls == []          # gar nicht erst gebucht/gesendet


def test_interactive_call_allowed_in_reserved_headroom():
    sent = []
    with _with_access(), \
         patch.object(fo, '_rot_hour_used',
                      return_value=fo._LHFO_HOUR_BACKGROUND_CEILING), \
         patch.object(fo, '_flightops_budget_inc',
                      side_effect=lambda p: sent.append(p)), \
         patch.object(fo.urllib.request, 'urlopen',
                      side_effect=RuntimeError('kein echter Call im Test')):
        # Gate lässt durch (urlopen-Fake wirft danach → None, egal):
        fo._api_get('tok', '/COMMON_DUTY_EVENTS', interactive=True)
    assert sent == ['/COMMON_DUTY_EVENTS']


def test_interactive_blocked_at_hard_ceiling():
    sent = []
    with _with_access(), \
         patch.object(fo, '_rot_hour_used',
                      return_value=fo._LHFO_HOUR_INTERACTIVE_CEILING), \
         patch.object(fo, '_flightops_budget_inc',
                      side_effect=lambda p: sent.append(p)):
        assert fo._api_get('tok', '/COMMON_DUTY_EVENTS',
                           interactive=True) is None
    assert sent == []


def test_refresh_all_marks_background_and_manual_import_is_interactive():
    """flightops_import reicht die Klasse an duty_events durch: refresh-all
    setzt body.background → interactive=False; der iOS-Aufruf (leerer Body)
    → interactive=True."""
    seen = []

    def _fake_duty_events(tok, fd, td, interactive=False):
        seen.append(interactive)
        return None    # → 502, uns interessiert nur das Flag

    with patch.object(fo, 'flightops_configured', return_value=True), \
         patch.object(fo, '_access_state', return_value=('ok', 'AT-X')), \
         patch.object(app, '_validate_token', return_value=True), \
         patch.object(fo, 'duty_events', side_effect=_fake_duty_events):
        c = app.app.test_client()
        h = {'Authorization': 'Bearer AT-TEST-GATE'}
        r1 = c.post('/api/lh/flightops/import/AT-TEST-GATE', json={}, headers=h)
        r2 = c.post('/api/lh/flightops/import/AT-TEST-GATE',
                    json={'background': 1}, headers=h)
        assert r1.status_code != 401 and r2.status_code != 401, (
            r1.status_code, r2.status_code)
    assert seen == [True, False]
