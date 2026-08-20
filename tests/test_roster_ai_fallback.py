"""KI-Lese-Fallback für den Roster-Import (Owner-Auftrag 2026-08-06).

Wenn ein User einen Dienstplan hochlädt, den KEIN deterministischer Parser
versteht (unbekannte Airline / neues Format), liest der konfigurierte
Modellanbieter die Roh-Fakten strukturiert — OpenAI zweimal unabhängig —
statt dass der User „0 Einträge" bekommt.

Geprüft werden genau die Leitplanken:
  * DETERMINISTIC-FIRST — bekanntes Format ⇒ kein KI-Call (Spy),
  * KEINE FAKE-WERTE — jede HH:MM und jeder Tag-im-Monat muss wörtlich im
    Quelltext stehen, sonst fliegt das Event raus,
  * Tagesdeckel (3/Token/Tag),
  * API-Fehler ⇒ Verhalten exakt wie vorher, kein Crash,
  * Lern-Warteschlange: erfolgreicher Lauf wird EINMAL archiviert.

Die Modell-API ist IMMER gemockt (`requests.post`) — es geht in keinem Test ein
Byte nach draußen. Fixture ist SYNTHETISCH (erfundene Airline, keine
Personendaten).
"""
import io
import json
import os
import sys
import base64

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


# Erfundenes Format — von keinem deterministischen Parser abgedeckt.
UNKNOWN_TEXT = """SKYREGIO AIRWAYS - Crew Duty Plan
Monat: 2026-09
Crew: MUSTER, Test
Base: DUS
01 SBY DUS 06:00 14:00
02 OFF
03 SR101 DUS PMI 07:30 10:05
04 URLAUB
"""

# Was ein braves Modell auf UNKNOWN_TEXT antworten würde: jede Zeit und jeder
# Tag steht wörtlich im Quelltext.
GOOD_ITEMS = [
    {'day': 1, 'month_hint': '2026-09', 'summary': 'SBY', 'location': 'DUS',
     'start_hhmm': '06:00', 'end_hhmm': '14:00', 'from_iata': 'DUS',
     'to_iata': 'DUS', 'flight_no': None, 'all_day': False},
    {'day': 2, 'month_hint': '2026-09', 'summary': 'OFF', 'location': None,
     'start_hhmm': None, 'end_hhmm': None, 'from_iata': None,
     'to_iata': None, 'flight_no': None, 'all_day': True},
    {'day': 3, 'month_hint': '2026-09', 'summary': 'SR101 DUS PMI',
     'location': None, 'start_hhmm': '07:30', 'end_hhmm': '10:05',
     'from_iata': 'DUS', 'to_iata': 'PMI', 'flight_no': 'SR101',
     'all_day': False},
]

OCR_CREWACCESS_TEXT = """Roster Preview
Planning period: September 2026
MUSTER, Test Crew
Rank: JC Base: FRA
Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID
30 Wed 11:40 JC 1144 FRA BIO 13:00 15:10 32N 834204
01 Thu 07:50 JC 1087 MRS FRA 08:50 10:35 32N
01Sep-30Sep2026 Jan - Sep
OFF Days 0 0
Block time 2:10
Created 16Aug2026 16:17 (UTC) by 000000X 1 ( 1)
"""


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


def _answer(items, wrap_codefence=False):
    body = json.dumps({'items': items}, ensure_ascii=False)
    if wrap_codefence:
        body = f'```json\n{body}\n```'
    return {'content': [{'type': 'text', 'text': body}]}


@pytest.fixture(autouse=True)
def _reset_ai_state(monkeypatch):
    """Tagesdeckel ist ein Modul-Global — zwischen Tests leeren."""
    backend._ROSTER_AI_CALLS.clear()
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test-not-a-real-key')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('AEROX_ROSTER_AI_MODEL', raising=False)
    monkeypatch.delenv('AEROX_ROSTER_AI_EFFORT', raising=False)
    monkeypatch.delenv('AEROX_ROSTER_OPENAI_MODEL', raising=False)
    monkeypatch.delenv('AEROX_ROSTER_OPENAI_EFFORT', raising=False)
    # Die Lern-Warteschlange schreibt sonst in den echten Import-Inbox-Pfad.
    monkeypatch.setattr(backend, '_logbook_upload_store',
                        lambda *a, **k: True)
    monkeypatch.setattr(backend, 'SB_AVAILABLE', False)
    yield
    backend._ROSTER_AI_CALLS.clear()


def _mock_post(monkeypatch, payload, calls=None):
    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        if calls is not None:
            calls.append({'url': url, 'headers': headers or {},
                          'body': json or {}, 'timeout': timeout})
        return _FakeResponse(payload)
    monkeypatch.setattr(requests, 'post', fake_post)


def _openai_answer(items):
    evidence_by_day = {
        1: '01 SBY DUS 06:00 14:00',
        2: '02 OFF',
        3: '03 SR101 DUS PMI 07:30 10:05',
        4: '04 URLAUB',
    }
    enriched = [dict(item, source_evidence=(
        item.get('source_evidence')
        or evidence_by_day.get(item.get('day'), ''))) for item in items]
    return {
        'output': [{
            'type': 'message',
            'content': [{
                'type': 'output_text',
                'text': json.dumps({'items': enriched}, ensure_ascii=False),
            }],
        }],
    }


# ── 1. Unbekanntes Format + gemockte Antwort → Events, valides ICS, Marker ──
def test_unknown_format_ai_fallback_builds_marked_ics(monkeypatch):
    calls = []
    _mock_post(monkeypatch, _answer(GOOD_ITEMS, wrap_codefence=True), calls)

    ics = backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-1', 'unsupported_pdf_format')
    assert ics, 'KI-Fallback muss ein ICS liefern'

    # RAW-HTTP-Muster: x-api-key + anthropic-version, EIN Versuch, Timeout 90s.
    assert len(calls) == 1
    assert calls[0]['url'] == 'https://api.anthropic.com/v1/messages'
    assert calls[0]['headers']['x-api-key'] == 'sk-ant-test-not-a-real-key'
    assert calls[0]['headers']['anthropic-version'] == '2023-06-01'
    assert calls[0]['timeout'] == 90
    assert calls[0]['body']['model'] == 'claude-opus-5'
    assert calls[0]['body']['max_tokens'] == 16000
    # Tabellen-Ablesen braucht keine tiefe Denk-Runde → Kosten-/Latenz-Bremse.
    assert calls[0]['body']['output_config'] == {'effort': 'low'}

    events = backend._parse_ics_to_events_v2(ics)
    assert len(events) == 3
    by_day = {ev['start']: ev for ev in events}
    assert set(by_day) == {'2026-09-01', '2026-09-02', '2026-09-03'}
    # Marker im Event-Dict (additives Feld) für Diagnose/Forensik.
    assert all(ev.get('ax_source') == 'ki_fallback' for ev in events)
    assert 'X-AEROX-SOURCE:ki_fallback' in ics
    # Flug-Leg trägt die Route aus validierten IATA-Codes.
    assert by_day['2026-09-03']['summary'] == 'SR101 DUS - PMI'
    # Stations-lokal wie beim Discover-Parser: DTSTART mit TZID, nicht UTC-Z.
    assert 'DTSTART;TZID=Europe/Berlin:20260903T073000' in ics
    # Ganztags-Marker bleibt ganztägig und wörtlich.
    assert by_day['2026-09-02']['summary'] == 'OFF'


def test_ai_model_is_overridable_via_env(monkeypatch):
    calls = []
    monkeypatch.setenv('AEROX_ROSTER_AI_MODEL', 'claude-sonnet-5')
    _mock_post(monkeypatch, _answer(GOOD_ITEMS), calls)
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-MODEL', 'unsupported_pdf_format')
    assert calls[0]['body']['model'] == 'claude-sonnet-5'


def test_effort_can_be_switched_off_for_legacy_models(monkeypatch):
    """Ältere Modelle kennen `output_config.effort` nicht — leeres Env-Feld
    lässt es weg, statt den einzigen Versuch mit einem 400 zu verbrennen."""
    calls = []
    monkeypatch.setenv('AEROX_ROSTER_AI_EFFORT', '')
    _mock_post(monkeypatch, _answer(GOOD_ITEMS), calls)
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-EFFORT', 'unsupported_pdf_format')
    assert 'output_config' not in calls[0]['body']


def test_openai_sol_xhigh_requires_two_identical_structured_reads(monkeypatch):
    calls = []
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-openai-not-real')
    _mock_post(monkeypatch, _openai_answer(GOOD_ITEMS), calls)

    ics = backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-OPENAI-DOUBLE', 'unsupported_pdf_format')

    assert ics
    assert len(calls) == 2
    for call in calls:
        assert call['url'] == 'https://api.openai.com/v1/responses'
        assert call['headers']['Authorization'] == \
            'Bearer sk-test-openai-not-real'
        assert call['timeout'] == 180
        assert call['body']['model'] == 'gpt-5.6-sol'
        assert call['body']['reasoning'] == {'effort': 'xhigh'}
        assert call['body']['store'] is False
        response_format = call['body']['text']['format']
        assert response_format['type'] == 'json_schema'
        assert response_format['strict'] is True
        assert response_format['schema']['additionalProperties'] is False


def test_openai_double_read_disagreement_fails_closed(monkeypatch, caplog):
    calls = []
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-openai-not-real')
    answers = [_openai_answer(GOOD_ITEMS), _openai_answer(GOOD_ITEMS[:-1])]

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append(url)
        return _FakeResponse(answers[len(calls) - 1])

    monkeypatch.setattr(requests, 'post', fake_post)
    caplog.set_level('WARNING')

    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-OPENAI-MISMATCH',
        'unsupported_pdf_format') is None
    assert len(calls) == 2
    assert 'double-read-mismatch' in caplog.text


def test_openai_double_read_accepts_same_facts_in_different_order(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-openai-not-real')
    answers = [_openai_answer(GOOD_ITEMS),
               _openai_answer(list(reversed(GOOD_ITEMS)))]
    calls = []

    def fake_post(*args, **kwargs):
        answer = answers[len(calls)]
        calls.append(answer)
        return _FakeResponse(answer)

    monkeypatch.setattr(requests, 'post', fake_post)
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-OPENAI-ORDER',
        'unsupported_pdf_format')
    assert len(calls) == 2


def test_openai_source_evidence_must_exist_and_contain_facts(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-openai-not-real')
    bad = [dict(GOOD_ITEMS[0], source_evidence='01 SBY DUS 06:00 18:00')]
    _mock_post(monkeypatch, _openai_answer(bad))

    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-OPENAI-EVIDENCE',
        'unsupported_pdf_format') is None


# ── 1b. Bild-PDF: visuelle Transkription, danach deterministischer Parser ─
def test_image_only_pdf_ocr_requires_known_parser_and_checksum(monkeypatch):
    calls = []
    pdf = b'%PDF-1.4\nsynthetic-image-only'
    _mock_post(monkeypatch, {
        'content': [{'type': 'text', 'text': OCR_CREWACCESS_TEXT}],
    }, calls)

    transcript = backend._roster_ai_ocr_pdf_text(
        pdf, 'AT-TEST-AI-OCR', 'pdf_no_text')
    assert transcript == OCR_CREWACCESS_TEXT.strip()
    assert len(calls) == 1
    content = calls[0]['body']['messages'][0]['content']
    assert content[0]['type'] == 'document'
    assert content[0]['source']['media_type'] == 'application/pdf'
    assert base64.b64decode(content[0]['source']['data']) == pdf
    # Folgemonatszeile ist nicht Teil der gedruckten September-Blockzeit.
    ics, err = backend._crewaccess_text_to_ics(transcript, carrier='VL')
    assert err is None
    assert backend._crewaccess_ocr_block_time_matches(transcript, ics)
    assert 'DTSTART:20261001T085000Z' in ics


def test_image_only_pdf_ocr_rejects_checksum_mismatch(monkeypatch, caplog):
    _mock_post(monkeypatch, {
        'content': [{'type': 'text', 'text':
                     OCR_CREWACCESS_TEXT.replace('Block time 2:10',
                                                 'Block time 9:59')}],
    })
    caplog.set_level('WARNING')
    assert backend._roster_ai_ocr_pdf_text(
        b'%PDF-1.4\nsynthetic', 'AT-TEST-AI-OCR-BAD') is None
    assert 'checksum-reject' in caplog.text


def test_endpoint_image_only_pdf_uses_checked_visual_transcript(monkeypatch):
    from unittest.mock import patch

    _mock_post(monkeypatch, {
        'content': [{'type': 'text', 'text': OCR_CREWACCESS_TEXT}],
    })
    pdf_bytes = _text_to_pdf('')
    token = 'AT-TEST-AI-OCR-ENDPOINT'
    _clear_user_state(token)
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'ai-ocr@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store',
                         return_value=True):
        rv = client.post(
            f'/api/user/roster-pdf/{token}/import',
            data={'pdf': (io.BytesIO(pdf_bytes), 'scan.pdf'),
                  'airline': 'Lufthansa City'},
            content_type='multipart/form-data')
    payload = rv.get_json() or {}
    assert rv.status_code == 200, payload
    assert payload.get('ok') is True
    assert payload.get('source') == 'ki_fallback'
    assert payload.get('events_count') == 2


# ── 2. Halluzinations-Guard ────────────────────────────────────────────────
def test_hallucinated_time_is_dropped(monkeypatch, caplog):
    """Eine Zeit, die NICHT im Quelltext steht, verwirft das Event."""
    bad = [dict(GOOD_ITEMS[0]),
           {'day': 5, 'month_hint': '2026-09', 'summary': 'FLT',
            'location': None, 'start_hhmm': '23:45', 'end_hhmm': '23:59',
            'from_iata': None, 'to_iata': None, 'flight_no': None,
            'all_day': False}]
    _mock_post(monkeypatch, _answer(bad))
    caplog.set_level('WARNING')

    ics = backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-2', 'unsupported_pdf_format')
    events = backend._parse_ics_to_events_v2(ics)
    assert [ev['start'] for ev in events] == ['2026-09-01']
    assert 'hallucination-drop' in caplog.text


def test_hallucination_guard_unit_rules():
    """Direkt am Guard: Tag, Zeit, Jahr und Ganztags-Summary müssen belegt sein."""
    items = [
        # ok
        {'day': 1, 'month_hint': '2026-09', 'summary': 'SBY',
         'start_hhmm': '06:00', 'end_hhmm': '14:00', 'from_iata': 'DUS',
         'to_iata': 'DUS', 'flight_no': None, 'location': None,
         'all_day': False},
        # Tag steht nicht im Quelltext
        {'day': 27, 'month_hint': '2026-09', 'summary': 'OFF',
         'all_day': True},
        # Jahr steht nicht im Quelltext
        {'day': 1, 'month_hint': '2019-09', 'summary': 'OFF',
         'all_day': True},
        # erfundene Zeit
        {'day': 3, 'month_hint': '2026-09', 'summary': 'SR101',
         'start_hhmm': '05:55', 'end_hhmm': '10:05', 'all_day': False},
        # kaputtes Zeitformat
        {'day': 3, 'month_hint': '2026-09', 'summary': 'SR101',
         'start_hhmm': '7:30', 'end_hhmm': '10:05', 'all_day': False},
        # Ganztags-Marker, der so nirgends im Plan steht
        {'day': 4, 'month_hint': '2026-09', 'summary': 'Bereitschaftsdienst',
         'all_day': True},
    ]
    valid, dropped = backend._roster_ai_validate_items(items, UNKNOWN_TEXT)
    assert dropped == 5
    assert [v['day'] for v in valid] == [1]

    # Unbelegte OPTIONALE Felder killen den Tag nicht, sondern nur sich selbst.
    partial, dropped_partial = backend._roster_ai_validate_items(
        [{'day': 2, 'month_hint': '2026-09', 'summary': 'OFF',
          'from_iata': 'JFK', 'to_iata': 'LAX', 'flight_no': 'XX9999',
          'location': 'JFK', 'all_day': True}], UNKNOWN_TEXT)
    assert dropped_partial == 0
    assert partial[0]['from_iata'] is None and partial[0]['to_iata'] is None
    assert partial[0]['flight_no'] is None and partial[0]['location'] is None


def test_start_without_end_stays_all_day(monkeypatch):
    """Eine fehlende Endzeit wird NICHT erfunden — der Tag reist ganztägig."""
    valid, dropped = backend._roster_ai_validate_items(
        [{'day': 2, 'month_hint': '2026-09', 'summary': 'OFF',
          'start_hhmm': '06:00', 'end_hhmm': None, 'all_day': False}],
        UNKNOWN_TEXT)
    assert dropped == 0 and valid[0]['all_day'] is True


# ── 3. DETERMINISTIC-FIRST ─────────────────────────────────────────────────
def test_known_discover_format_never_calls_ai(monkeypatch):
    """Discover-Text wird deterministisch geparst — der Fallback bleibt kalt."""
    spy = []
    monkeypatch.setattr(backend, '_roster_ai_fallback_ics',
                        lambda *a, **k: spy.append(a) or None)

    def boom(*a, **k):  # pragma: no cover — darf nie erreicht werden
        raise AssertionError('kein HTTP-Call im deterministischen Pfad')
    monkeypatch.setattr(requests, 'post', boom)

    from tests.test_discover_roster_pdf import SYN_TEXT
    ics, err = backend._discover_roster_text_to_ics(SYN_TEXT, carrier='4Y')
    assert err is None and 'BEGIN:VEVENT' in ics
    assert spy == []


def test_endpoint_known_format_does_not_reach_ai(monkeypatch):
    """End-to-End-Spy: ein verstandenes PDF erreicht den KI-Pfad nicht."""
    from unittest.mock import patch
    from tests.test_discover_roster_pdf import SYN_TEXT

    spy = []
    monkeypatch.setattr(backend, '_roster_ai_fallback_ics',
                        lambda *a, **k: spy.append(a) or None)
    monkeypatch.setattr(requests, 'post', _never_called)

    pdf_bytes = _text_to_pdf(SYN_TEXT)
    token = 'AT-TEST-AI-DETERMINISTIC-1'
    _clear_user_state(token)
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'ai-det@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store',
                         return_value=True):
        rv = client.post(f'/api/user/roster-pdf/{token}/import',
                         data={'pdf': (io.BytesIO(pdf_bytes), 'roster.pdf'),
                               'airline': 'Discover'},
                         content_type='multipart/form-data')
    payload = rv.get_json() or {}
    assert rv.status_code == 200, payload
    assert payload.get('source') == 'pdf'
    assert 'ki_hint' not in payload
    assert spy == []


# ── 4. Tagesdeckel ─────────────────────────────────────────────────────────
def test_daily_cap_blocks_fourth_run(monkeypatch, caplog):
    calls = []
    _mock_post(monkeypatch, _answer(GOOD_ITEMS), calls)
    caplog.set_level('WARNING')
    token = 'AT-TEST-AI-CAP'

    for _ in range(backend._ROSTER_AI_DAILY_MAX):
        assert backend._roster_ai_fallback_ics(
            UNKNOWN_TEXT, token, 'unsupported_pdf_format')
    assert len(calls) == 3

    # Vierter Lauf am selben Tag: KEIN API-Call mehr.
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, token, 'unsupported_pdf_format') is None
    assert len(calls) == 3
    assert 'daily-cap' in caplog.text

    # Der Deckel gilt pro Token, nicht global.
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-CAP-OTHER', 'unsupported_pdf_format')
    assert len(calls) == 4


def test_missing_api_key_costs_no_budget(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setattr(requests, 'post', _never_called)
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-NOKEY', 'unsupported_pdf_format') is None
    assert backend._ROSTER_AI_CALLS == {}


# ── 5. API-Fehler → Verhalten identisch zu vorher ──────────────────────────
@pytest.mark.parametrize('payload,label', [
    (None, 'http-500'),
    ({'content': [{'type': 'text', 'text': 'kein JSON hier'}]}, 'garbage'),
    ({'content': []}, 'empty'),
])
def test_api_failure_is_silent(monkeypatch, payload, label):
    if payload is None:
        def fake_post(*a, **k):
            return _FakeResponse({}, status=500)
        monkeypatch.setattr(requests, 'post', fake_post)
    else:
        _mock_post(monkeypatch, payload)
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, f'AT-TEST-AI-ERR-{label}',
        'unsupported_pdf_format') is None


def test_network_exception_is_swallowed(monkeypatch):
    def fake_post(*a, **k):
        raise requests.ConnectionError('boom')
    monkeypatch.setattr(requests, 'post', fake_post)
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-NET', 'unsupported_pdf_format') is None


def test_endpoint_api_failure_keeps_previous_error(monkeypatch):
    """Der Fallback darf einen Import NIE schlechter machen: bei API-Fehler
    kommt exakt die bisherige 422-Antwort zurück."""
    from unittest.mock import patch

    def fake_post(*a, **k):
        raise requests.Timeout('slow')
    monkeypatch.setattr(requests, 'post', fake_post)

    pdf_bytes = _text_to_pdf(UNKNOWN_TEXT)
    token = 'AT-TEST-AI-ERR-ENDPOINT'
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'ai-err@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store',
                         return_value=True):
        rv = client.post(f'/api/user/roster-pdf/{token}/import',
                         data={'pdf': (io.BytesIO(pdf_bytes), 'plan.pdf')},
                         content_type='multipart/form-data')
    assert rv.status_code == 422
    payload = rv.get_json() or {}
    assert payload == {'ok': False, 'error': 'unsupported_pdf_format',
                       'monitoring_queued': True}


# ── End-to-End: Antwort trägt source=ki_fallback + ki_hint ─────────────────
def test_endpoint_marks_ai_read_import_honestly(monkeypatch):
    from unittest.mock import patch
    _mock_post(monkeypatch, _answer(GOOD_ITEMS))

    pdf_bytes = _text_to_pdf(UNKNOWN_TEXT)
    token = 'AT-TEST-AI-ENDPOINT-OK'
    _clear_user_state(token)
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'ai-ok@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store',
                         return_value=True):
        rv = client.post(f'/api/user/roster-pdf/{token}/import',
                         data={'pdf': (io.BytesIO(pdf_bytes), 'plan.pdf')},
                         content_type='multipart/form-data')
    payload = rv.get_json() or {}
    assert rv.status_code == 200, payload
    assert payload.get('ok') is True
    assert payload.get('source') == 'ki_fallback'
    assert payload.get('ki_hint') == backend._ROSTER_AI_HINT
    assert payload.get('events_count', 0) >= 3


def test_endpoint_restores_previous_calendar_if_second_display_check_differs(
        monkeypatch):
    """A post-persist mismatch must never leave the rejected plan visible."""
    from unittest.mock import patch

    _mock_post(monkeypatch, _answer(GOOD_ITEMS))
    real_contract = backend._airline_display_contract
    contract_calls = []

    def disagree_after_persist(events):
        result = real_contract(events)
        contract_calls.append(result)
        if len(contract_calls) == 2:
            result = dict(result)
            result['sector_count'] = int(result.get('sector_count') or 0) + 1
        return result

    monkeypatch.setattr(
        backend, '_airline_display_contract', disagree_after_persist)
    pdf_bytes = _text_to_pdf(UNKNOWN_TEXT)
    token = 'AT-TEST-AI-DISPLAY-ROLLBACK'
    _clear_user_state(token)
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'ai-rollback@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store',
                         return_value=True):
        rv = client.post(f'/api/user/roster-pdf/{token}/import',
                         data={'pdf': (io.BytesIO(pdf_bytes), 'plan.pdf')},
                         content_type='multipart/form-data')

    assert rv.status_code == 422
    assert len(contract_calls) == 2
    assert backend._airline_request_profile_feed(token) == {}
    assert backend._ical_briefings_load(token) == {}


# ── 6. Lern-Warteschlange (Owner-Zusatz 2026-08-06) ────────────────────────
def test_learn_queue_row_uses_dedicated_note_marker(monkeypatch):
    captured = []
    monkeypatch.setattr(
        backend, '_logbook_upload_store',
        lambda token, filename, blob, note: captured.append(
            (token, filename, blob, note)) or True)
    _mock_post(monkeypatch, _answer(GOOD_ITEMS))

    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-LEARN', 'unsupported_pdf_format')
    assert len(captured) == 1
    token, filename, blob, note = captured[0]
    assert note == 'AEROX_ROSTER_AI_LEARN_V1'
    assert filename.startswith('roster-ai-learn-') and filename.endswith('.json')
    body = json.loads(blob.decode('utf-8'))
    # Lernmaterial: gekappter Roh-Quelltext + validierte KI-Events + Modell.
    assert body['source_text'] == UNKNOWN_TEXT
    assert body['model'] == 'claude-opus-5'
    assert len(body['events']) == 3
    assert body['source_sha256'][:16] in filename


def test_learn_queue_dedupes_identical_source(monkeypatch):
    """Zweiter identischer Lauf → keine zweite Zeile (sha256 des Quelltexts)."""
    stored = []
    monkeypatch.setattr(
        backend, '_logbook_upload_store',
        lambda token, filename, blob, note: stored.append(filename) or True)
    _mock_post(monkeypatch, _answer(GOOD_ITEMS))
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)

    class _Res:
        def __init__(self, data):
            self.data = data

    def fake_exec(name, fn, **kwargs):
        # Erster Lauf: Inbox leer. Danach: die Zeile aus Lauf 1 ist da.
        return _Res([{'id': 1}] if stored else []), False
    monkeypatch.setattr(backend, '_supabase_execute_with_timeout', fake_exec)

    token = 'AT-TEST-AI-LEARN-DEDUPE'
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, token, 'unsupported_pdf_format')
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, token, 'unsupported_pdf_format')
    assert len(stored) == 1


def test_learn_queue_only_on_successful_read(monkeypatch):
    """Kein valides Event ⇒ kein Lernmaterial (nichts zu lernen)."""
    stored = []
    monkeypatch.setattr(
        backend, '_logbook_upload_store',
        lambda *a, **k: stored.append(a) or True)
    _mock_post(monkeypatch, _answer(
        [{'day': 27, 'month_hint': '2026-09', 'summary': 'OFF',
          'all_day': True}]))
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-LEARN-EMPTY', 'unsupported_pdf_format') is None
    assert stored == []


# ── Datenschutz: nie Inhalt im Log ─────────────────────────────────────────
def test_logs_never_contain_source_text(monkeypatch, caplog):
    _mock_post(monkeypatch, _answer(GOOD_ITEMS))
    caplog.set_level('INFO')
    assert backend._roster_ai_fallback_ics(
        UNKNOWN_TEXT, 'AT-TEST-AI-PRIVACY', 'unsupported_pdf_format')
    text = caplog.text
    assert 'SKYREGIO' not in text and 'MUSTER' not in text
    assert 'chars=' in text and 'sha=' in text


# ── Helfer ────────────────────────────────────────────────────────────────
def _never_called(*args, **kwargs):  # pragma: no cover
    raise AssertionError('Anthropic-API darf hier nicht gerufen werden')


def _text_to_pdf(text):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 820
    for line in text.splitlines():
        if y < 40:
            c.showPage()
            y = 820
        c.drawString(30, y, line)
        y -= 12
    c.save()
    return buf.getvalue()


def _clear_user_state(token):
    for path in (backend._user_profile_path(token),
                 os.path.join(backend._USER_HISTORY_DIR, 'briefings',
                              f'{token}.json')):
        try:
            os.remove(path)
        except OSError:
            pass
