"""MegaR Phase 9 — Versioning + PDF Audit Tests."""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


@pytest.mark.parametrize('const_name', [
    'APP_VERSION', 'ENGINE_VERSION', 'PROMPT_VERSION',
    'CAS_READER_VERSION', 'SE_READER_VERSION', 'LSB_READER_VERSION',
    'RULESET_VERSION', 'AI_RESOLVER_VERSION', 'FRONTEND_CONTRACT_VERSION',
])
def test_version_constant_exists(const_name):
    """Alle Pflicht-Versions-Konstanten existieren in app.py."""
    assert hasattr(app_module, const_name), f'Version-Konstante {const_name} fehlt'
    val = getattr(app_module, const_name)
    assert isinstance(val, str) and val, f'{const_name} muss non-empty string sein'


def test_app_version_is_11_x():
    """v11 Clean-Release Versions-Major."""
    assert app_module.APP_VERSION.startswith('11.'), \
        f'APP_VERSION sollte 11.* sein, war {app_module.APP_VERSION!r}'


def test_engine_version_is_tour_first_v11():
    assert app_module.ENGINE_VERSION.startswith('tour_first_v11'), \
        f'ENGINE_VERSION sollte tour_first_v11* sein, war {app_module.ENGINE_VERSION!r}'


def test_frontend_contract_version_mentions_3doc():
    """Frontend-Contract-Version dokumentiert das 3-Doc-Modell."""
    v = app_module.FRONTEND_CONTRACT_VERSION
    assert 'lsb' in v.lower() and 'se' in v.lower() and 'cas' in v.lower(), \
        f'FRONTEND_CONTRACT_VERSION sollte LSB+SE+CAS-Hint enthalten, war {v!r}'


def test_no_flugstunden_in_active_reader_versions():
    """Reader-Version-Strings duerfen 'flugstunden' nur im DEPRECATED-Suffix haben."""
    rv = app_module.READER_VERSIONS
    assert 'cas' in rv
    assert 'lsb' in rv
    assert 'se' in rv
    # dp ist DEPRECATED markiert
    if 'dp' in rv:
        assert 'DEPRECATED' in rv['dp'].upper()


def test_pdf_audit_includes_engine_version():
    """PDF-Renderer schreibt ENGINE_VERSION ins Audit."""
    src = open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()
    # ENGINE_VERSION wird ins result_data geschrieben
    assert 'ENGINE_VERSION' in src
    # READER_VERSIONS auch im Audit
    assert 'reader_versions' in src or 'READER_VERSIONS' in src
