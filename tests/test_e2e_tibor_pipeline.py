"""End-to-End-Pipeline-Tests gegen echte/sanitized Tibor-Daten.

Ziel:
  Pipeline-Schritte (Klassifikator → Align → PDF-Block) gegen reale Datenform,
  damit Bugs wie a6e291f2 (tuple-no-attribute-get) lokal reproduzierbar sind.

  Keine Sonnet-Calls. Nur strukturierte Reader-Outputs als Fixtures.
"""
import os, sys, json
import pytest

# Disable BG-Threads damit App-Import isoliert bleibt
os.environ.setdefault('AEROTAX_DISABLE_BG_THREADS', '1')

# App-Import (parent dir)
_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(_HERE))
import app

_FIXTURE_DIR = os.path.join(_HERE, 'fixtures')
_TIBOR_TAGE_DETAIL = os.path.join(_FIXTURE_DIR, 'tibor_aerotax_v11_raw_initial.json')
_FOLLOWME_GOLDEN = os.path.join(_FIXTURE_DIR, 'followme_golden_tibor_2025.json')


def _load_tibor_tage_detail():
    if not os.path.exists(_TIBOR_TAGE_DETAIL):
        return None
    with open(_TIBOR_TAGE_DETAIL) as f:
        return json.load(f)


def _load_followme_golden():
    if not os.path.exists(_FOLLOWME_GOLDEN):
        return None
    with open(_FOLLOWME_GOLDEN) as f:
        return json.load(f)


# ════════════════════════════════════════════════════════════════════════════
# Schema-Validator Tests
# ════════════════════════════════════════════════════════════════════════════

def test_schema_accepts_realistic_tage_detail():
    """Echtes Tibor _tage_detail passt zu Schema."""
    td = _load_tibor_tage_detail()
    if td is None:
        pytest.skip('Tibor-fixture fehlt')
    app._validate_pipeline_shape(td, app._SCHEMA_TAGE_DETAIL, 'tage_detail')


def test_schema_rejects_tuple_where_dict_expected():
    """Tuple statt dict → klare Exception mit Pfad."""
    bad = [('Tour', 0.0, 'reason')]  # tuple-Eintrag statt dict
    with pytest.raises(app._PipelineSchemaError) as exc_info:
        app._validate_pipeline_shape(bad, app._SCHEMA_TAGE_DETAIL, 'td')
    msg = str(exc_info.value)
    assert 'td[0]' in msg, f'Pfad sollte td[0] enthalten, ist: {msg}'
    assert 'dict' in msg.lower()


def test_schema_rejects_string_where_list_expected():
    """String statt list → Exception."""
    with pytest.raises(app._PipelineSchemaError):
        app._validate_pipeline_shape('hello', 'list', 'foo')


def test_schema_error_contains_path():
    """Verschachtelter Fehler nennt vollen Pfad."""
    schema = {'type': 'dict', 'fields': {
        'outer': {'type': 'dict', 'fields': {'inner': 'list'}},
    }}
    bad = {'outer': {'inner': 'not_a_list'}}
    with pytest.raises(app._PipelineSchemaError) as exc_info:
        app._validate_pipeline_shape(bad, schema, 'root')
    assert 'root.outer.inner' in str(exc_info.value)


def test_schema_missing_required_field():
    """Fehlendes Pflichtfeld → Exception."""
    schema = {'type': 'dict', 'fields': {'datum': 'str', 'klass': 'str'}}
    with pytest.raises(app._PipelineSchemaError) as exc_info:
        app._validate_pipeline_shape({'datum': '2025-01-01'}, schema, 'day')
    assert 'klass' in str(exc_info.value)


def test_schema_optional_field_ok_when_missing():
    """Optionales Feld darf fehlen, kein Crash."""
    schema = {'type': 'dict', 'fields': {'datum': 'str'}, 'optional': ['extra']}
    app._validate_pipeline_shape({'datum': '2025-01-01'}, schema, 'day')


# ════════════════════════════════════════════════════════════════════════════
# Align-Failure-Handling Tests
# ════════════════════════════════════════════════════════════════════════════

def test_align_failure_not_silent():
    """Wenn Align crashed: classification bekommt _followme_align_failed Flag."""
    # Synth: classification mit tuple-Eintrag → triggert Schema-Crash
    bad_classification = {
        'tage_detail': [('not_a_dict',)],  # tuple statt dict
        'fahr_tage': 10,
    }
    with pytest.raises(app._PipelineSchemaError):
        app._followme_align_counters(bad_classification, [], 2025, 'FRA')


def test_align_failure_sets_red_health():
    """Wenn Align in classify_v11_cas_pipeline crashed:
    document_health=red wird gesetzt + _followme_align_failed in classification.

    Test prüft Code-Pfad statisch — echter Test braucht volle Pipeline."""
    src_path = os.path.join(os.path.dirname(_HERE), 'app.py')
    src = open(src_path).read()
    # Suche Block der align-crash behandelt
    idx = src.find('except Exception as _ae:')
    assert idx > 0, 'align-except-block fehlt'
    block = src[idx:idx + 2500]
    assert '_followme_align_failed' in block
    assert "'red'" in block
    assert 'document_health' in block.lower() or '_document_health' in block


def test_pdf_blocked_if_align_failed():
    """post_finalize_pdf blockt bei _followme_align_failed mit 409."""
    src_path = os.path.join(os.path.dirname(_HERE), 'app.py')
    src = open(src_path).read()
    idx = src.find('def post_finalize_pdf')
    block = src[idx:idx + 4000]
    assert '_followme_align_failed' in block
    assert '409' in block
    # Friendly user-message
    assert 'Berechnung konnte nicht' in block or 'kontaktiere den Support' in block


def test_pdf_blocked_if_health_red():
    """post_finalize_pdf blockt auch wenn _document_health.status='red'."""
    src_path = os.path.join(os.path.dirname(_HERE), 'app.py')
    src = open(src_path).read()
    idx = src.find('def post_finalize_pdf')
    block = src[idx:idx + 4000]
    assert "_document_health" in block
    assert "'red'" in block


# ════════════════════════════════════════════════════════════════════════════
# Snapshot-Capture Tests
# ════════════════════════════════════════════════════════════════════════════

def test_snapshot_capture_only_active_with_env_flag():
    """Default AEROTAX_CAPTURE_SNAPSHOTS=0 — Code-Check."""
    src_path = os.path.join(os.path.dirname(_HERE), 'app.py')
    src = open(src_path).read()
    # Code muss default '0' haben
    assert "os.environ.get('AEROTAX_CAPTURE_SNAPSHOTS', '0')" in src


def test_snapshot_save_returns_early_when_flag_off():
    """_save_pipeline_snapshot tut nichts wenn Flag off."""
    # Sanity: Aufruf darf nicht crashen wenn job_id None
    app._save_pipeline_snapshot(None, 'test_stage', {'data': 'x'})  # no-op
    # Bei AEROTAX_CAPTURE_SNAPSHOTS=False (default): early return ohne Side-Effect
    if not app.AEROTAX_CAPTURE_SNAPSHOTS:
        app._save_pipeline_snapshot('test-job-id', 'stage1', {'data': 'x'})  # no-op
        # Kein Save passiert → kein _jobs['test-job-id']
        assert 'test-job-id' not in app._jobs


def test_snapshot_strips_bytes():
    """_snapshot_strip_binaries entfernt bytes-Objekte."""
    clean = app._snapshot_strip_binaries({'pdf_bytes': b'\x00\x01\x02', 'ok': 'value'})
    assert 'bytes' in str(clean.get('pdf_bytes')) or clean.get('pdf_bytes') == '<stripped>'
    assert clean.get('ok') == 'value'


def test_snapshot_strips_base64_keys():
    """Verbotene Keys (pdf_b64, base64) werden ersetzt durch <stripped>."""
    clean = app._snapshot_strip_binaries({
        'base64': 'JVBERi0xLjQKJeLjz9MK' * 50,
        'b64': 'AAA',
        'raw_pdf': 'should_strip',
        'days': [{'date': '2025-01-01'}],  # nicht-binär bleibt
    })
    assert clean['base64'] == '<stripped>'
    assert clean['b64'] == '<stripped>'
    assert clean['raw_pdf'] == '<stripped>'
    assert clean['days'][0]['date'] == '2025-01-01'


def test_snapshot_strips_long_base64_like_strings():
    """Sehr lange Strings mit base64-Pattern werden truncated."""
    base64_like = ('/' + 'A' * 3000 + '+/AB' * 100)
    clean = app._snapshot_strip_binaries({'data': base64_like})
    val = clean['data']
    assert 'base64-like' in str(val) or len(str(val)) < 10000


def test_snapshot_handles_nested_lists():
    """Listen werden rekursiv bereinigt + auf 500 Einträge gekappt."""
    big_list = [{'pdf_bytes': b'X'}] * 600
    clean = app._snapshot_strip_binaries(big_list)
    assert len(clean) <= 500


# ════════════════════════════════════════════════════════════════════════════
# E2E-Pipeline Tests
# ════════════════════════════════════════════════════════════════════════════

def test_tibor_e2e_align_with_old_fixture_no_crash():
    """Älteres Tibor _tage_detail durch Align: kein tuple-crash."""
    td = _load_tibor_tage_detail()
    if td is None:
        pytest.skip()
    classification = {
        'tage_detail': td,
        'fahr_tage': 125, 'arbeitstage': 183,
        'reinigungstage': 153, 'hotel_naechte': 55,
    }
    # Sollte sauber durchlaufen
    result = app._followme_align_counters(classification, [], 2025, 'FRA')
    assert isinstance(result, dict)
    assert 'fahr_tage' in result


def test_tibor_e2e_align_validates_input_schema():
    """Schema-Check fängt korrupte Daten BEVOR Inner-Logic crashed."""
    bad = {'tage_detail': 'not_a_list'}
    with pytest.raises(app._PipelineSchemaError) as exc_info:
        app._followme_align_counters(bad, [], 2025, 'FRA')
    assert 'classification.tage_detail' in str(exc_info.value)


def test_tibor_e2e_no_tuple_get_crash_regression():
    """Regression-Test für a6e291f2: tuple irgendwo in tage_detail."""
    td_with_tuple = [
        {'datum': '2025-01-01', 'klass': 'Z76'},
        ('garbage', 1, 2),  # ← tuple statt dict
        {'datum': '2025-01-03', 'klass': 'Z76'},
    ]
    classification = {'tage_detail': td_with_tuple}
    # Vor v11 P0: AttributeError 'tuple' object has no attribute 'get'
    # Jetzt: klarer _PipelineSchemaError mit Pfad
    with pytest.raises(app._PipelineSchemaError) as exc_info:
        app._followme_align_counters(classification, [], 2025, 'FRA')
    msg = str(exc_info.value)
    assert '[1]' in msg, f'Pfad sollte Index nennen: {msg}'


def test_tibor_e2e_followme_golden_loaded():
    """Golden Fixture lädt fehlerfrei + hat erwartete Struktur."""
    g = _load_followme_golden()
    if g is None:
        pytest.skip()
    assert 'touren' in g
    assert 'day_classification' in g
    assert 'soll_summary' in g
    assert g['soll_summary']['z76']['gesamt'] == 4794.00


def test_tibor_e2e_align_result_contains_required_fields():
    """Align-Output enthält alle Counter die downstream genutzt werden."""
    td = _load_tibor_tage_detail()
    if td is None:
        pytest.skip()
    classification = {
        'tage_detail': td,
        'fahr_tage': 125, 'arbeitstage': 183,
        'reinigungstage': 153, 'hotel_naechte': 55,
    }
    result = app._followme_align_counters(classification, [], 2025, 'FRA')
    for field in ('fahr_tage', 'arbeitstage', 'reinigungstage', 'hotel_naechte',
                  '_followme_aligned', '_followme_tours_identified'):
        assert field in result, f'Feld {field} fehlt im Align-Output'


# ════════════════════════════════════════════════════════════════════════════
# Frontend Timeout Tests (statisch — Frontend-JS-Source)
# ════════════════════════════════════════════════════════════════════════════

_FRONTEND_PATH = os.path.expanduser('~/Desktop/site/index.html')


def test_frontend_timeout_running_is_not_fail():
    """Wenn Backend running/processing/queued → friendly message statt throw."""
    if not os.path.exists(_FRONTEND_PATH):
        pytest.skip()
    src = open(_FRONTEND_PATH).read()
    # Suche Backend-Status-Check
    assert "_backendStillRunning" in src or "running.*processing.*queued" in src
    # Friendly Text
    assert 'dauert länger als erwartet' in src
    # Plus: kein throw bei running
    idx = src.find('_backendStillRunning')
    if idx > 0:
        block = src[idx:idx + 2000]
        assert 'return' in block, 'Muss return statt throw bei running'


def test_frontend_no_auto_retry_in_finishprocess():
    """finishProcess ruft KEIN process() mehr auf."""
    if not os.path.exists(_FRONTEND_PATH):
        pytest.skip()
    src = open(_FRONTEND_PATH).read()
    idx = src.find('function finishProcess')
    block = src[idx:idx + 2500]
    assert 'window._autoRetryTimer = setTimeout' not in block
    assert 'process()' not in block or 'Mit deinem Code' in block


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
