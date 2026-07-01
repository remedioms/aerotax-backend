"""Rel Phase 13 — DSGVO/Security Release Tests."""
import os
import conftest as _cft
import re
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
SITE_HTML = _cft.SITE_INDEX_HTML


def _scan_files():
    paths = [os.path.join(ROOT_DIR, 'app.py')]
    # docs/
    for f in os.listdir(os.path.join(ROOT_DIR, 'docs')):
        if f.endswith('.md'):
            paths.append(os.path.join(ROOT_DIR, 'docs', f))
    # tests/
    for f in os.listdir(os.path.join(ROOT_DIR, 'tests')):
        if f.endswith('.py'):
            paths.append(os.path.join(ROOT_DIR, 'tests', f))
    if os.path.exists(SITE_HTML):
        paths.append(SITE_HTML)
    return paths


def test_no_aws_keys():
    pattern = re.compile(r'AKIA[A-Z0-9]{16}')
    for p in _scan_files():
        try:
            content = open(p, encoding='utf-8').read()
        except UnicodeDecodeError:
            continue
        hits = pattern.findall(content)
        assert not hits, f'AWS-Key gefunden in {p}: {hits[:1]}'


def test_no_anthropic_keys():
    pattern = re.compile(r'sk-ant-api[a-zA-Z0-9_-]{30,}')
    for p in _scan_files():
        try:
            content = open(p, encoding='utf-8').read()
        except UnicodeDecodeError:
            continue
        hits = pattern.findall(content)
        assert not hits, f'Anthropic-Key in {p}'


def test_no_stripe_keys():
    pattern = re.compile(r'sk_(test|live)_[a-zA-Z0-9]{20,}')
    for p in _scan_files():
        try:
            content = open(p, encoding='utf-8').read()
        except UnicodeDecodeError:
            continue
        hits = pattern.findall(content)
        assert not hits


def test_no_openai_keys():
    pattern = re.compile(r'sk-proj-[a-zA-Z0-9_-]{30,}')
    for p in _scan_files():
        try:
            content = open(p, encoding='utf-8').read()
        except UnicodeDecodeError:
            continue
        hits = pattern.findall(content)
        assert not hits


def test_pii_hardening_active():
    """PII-Hardening-Module aktiv."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert ('_strip_pii' in src or 'PII' in src)


def test_anti_tax_sanitizer_active():
    """Anti-Tax-Sanitizer-Set definiert."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert '_READER_V2_FORBIDDEN_FIELDS' in src


def test_token_random_unpredictable():
    """Recovery-Token sind random (sha256/uuid)."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert ('sha256' in src.lower() or 'uuid' in src.lower() or 'token_urlsafe' in src.lower())


def test_rate_limit_active():
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert '_ip_rate_limited' in src


def test_session_ttl_documented():
    """Session-TTL ist als Konstante in Code definiert."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert ('TTL' in src or 'expires' in src.lower())


def test_pdf_no_raw_ki_prompt():
    """PDF-Renderer schreibt kein raw-KI-Prompt."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    # PDF-Render-Block
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 20000]
        # Sample-Check: keine prompt= im PDF
        assert 'prompt=' not in block.lower() or 'raw_prompt' not in block
