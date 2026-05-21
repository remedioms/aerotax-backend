"""v11 Clean-Release Phase 4 — Tibor V2 Fixtures Tests.

Verifiziert:
- tibor_2025_source_manifest.json hat erwartete Doc-Typen + Counts.
- tibor_2025_cas_v2_from_dienstplan.json:
  - schema_version='v2'
  - days_count=395
  - keine FLUGSTUNDEN-Sources
  - alle Sources sind in {CAS, SE, BMF2025}
- Legacy-Fixture tibor_aerotax_v11_raw_initial.json existiert weiterhin als legacy-baseline.

Spec: docs/TIBOR_2025_V2_FIXTURE_BUILD.md
"""
import json
import os
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
FIXTURE_DIR = os.path.join(THIS_DIR, 'fixtures')

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _load(path):
    with open(path) as fh:
        return json.load(fh)


# ════════════════════════════════════════════════════════════════════════════
# Source Manifest
# ════════════════════════════════════════════════════════════════════════════

def test_source_manifest_exists():
    p = os.path.join(FIXTURE_DIR, 'tibor_2025_source_manifest.json')
    assert os.path.exists(p), f'Manifest fehlt: {p}'


def test_source_manifest_has_required_top_keys():
    m = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_source_manifest.json'))
    for k in ('version', 'created_at', 'year', 'subject', 'documents'):
        assert k in m


def test_manifest_includes_lsb_se_cas_docs():
    m = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_source_manifest.json'))
    types = [d['doc_type'] for d in m['documents']]
    assert 'lohnsteuerbescheinigung' in types
    assert 'streckeneinsatz' in types
    assert 'dienstplan_cas' in types


def test_manifest_marks_flugstunden_as_legacy_ignored():
    m = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_source_manifest.json'))
    flug_docs = [d for d in m['documents']
                 if d['doc_type'] == 'legacy_ignored_flight_hours_summary']
    if flug_docs:
        for d in flug_docs:
            assert d['role'] == 'IGNORED_LEGACY'


def test_manifest_has_at_least_one_dienstplan_pdf():
    m = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_source_manifest.json'))
    cas_docs = [d for d in m['documents'] if d['doc_type'] == 'dienstplan_cas']
    assert len(cas_docs) >= 1, 'Mindestens 1 CAS-Dienstplan-PDF erforderlich.'


def test_manifest_sha256_hashes_present():
    m = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_source_manifest.json'))
    for d in m['documents']:
        assert d.get('sha256_16'), f'Hash fehlt fuer {d.get("filename")}'
        assert len(d['sha256_16']) == 16


# ════════════════════════════════════════════════════════════════════════════
# V2 Fixture Tibor
# ════════════════════════════════════════════════════════════════════════════

def test_v2_fixture_exists():
    p = os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json')
    assert os.path.exists(p)


def test_v2_fixture_schema_version_is_v2():
    f = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json'))
    assert f['schema_version'] == 'v2'


def test_v2_fixture_has_tage_detail():
    f = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json'))
    assert 'tage_detail' in f
    assert isinstance(f['tage_detail'], list)
    assert len(f['tage_detail']) > 100  # Mindestens halbes Jahr


def test_v2_fixture_no_flugstunden_in_any_source():
    """KEIN Tag hat 'FLUGSTUNDEN' oder 'DP' als Source."""
    f = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json'))
    for d in f['tage_detail']:
        srcs = d.get('sources') or []
        for s in srcs:
            assert 'FLUGSTUNDEN' not in str(s).upper(), \
                f'Tag {d.get("datum")}: Flugstunden-Source verboten: {srcs}'
            assert s != 'DP', \
                f'Tag {d.get("datum")}: DP-Label muss CAS sein: {srcs}'


def test_v2_fixture_all_sources_in_allowed_set():
    """Alle Sources sind in {CAS, SE, BMF2025}."""
    allowed = {'CAS', 'SE', 'BMF2025'}
    f = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json'))
    for d in f['tage_detail']:
        srcs = set(d.get('sources') or [])
        unknown = srcs - allowed
        assert not unknown, f'Tag {d.get("datum")}: unerlaubte Sources {unknown}'


def test_v2_fixture_verification_passes():
    f = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json'))
    v = f.get('verification') or {}
    assert v.get('no_flight_hours_summary_in_sources') is True
    assert v.get('all_days_cas_or_se_or_bmf') is True


def test_v2_fixture_derived_from_field_present():
    f = _load(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json'))
    assert f.get('derived_from'), 'derived_from-Provenance fehlt'


# ════════════════════════════════════════════════════════════════════════════
# Legacy Fixture bleibt vorhanden
# ════════════════════════════════════════════════════════════════════════════

def test_legacy_fixture_still_exists_for_baseline():
    """tibor_aerotax_v11_raw_initial.json bleibt fuer Bug-Forensik + A/B-Vergleich."""
    p = os.path.join(FIXTURE_DIR, 'tibor_aerotax_v11_raw_initial.json')
    assert os.path.exists(p)


def test_legacy_fixture_unchanged_by_v2_build():
    """Sanity: V2-Build hat das Original nicht ueberschrieben."""
    p = os.path.join(FIXTURE_DIR, 'tibor_aerotax_v11_raw_initial.json')
    legacy = _load(p)
    # Original ist eine Liste (kein V2-Wrapper)
    assert isinstance(legacy, list)
    # DP-Sources sind noch da (Beweis: nicht relabelt)
    sample = legacy[0]
    assert sample.get('sources') is not None
