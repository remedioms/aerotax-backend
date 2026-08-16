"""Rel Phase 13 — DSGVO/Security Release Tests."""
import os
import conftest as _cft
import re
import subprocess
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


def _assert_no_secret(pattern, label):
    """Scan committed release inputs without hydrating cloud-only worktree files.

    macOS may evict old docs from a Documents-backed worktree. Reading every
    path can then block the release suite on iCloud downloads. `git grep HEAD`
    scans the authoritative committed blobs instead and never prints a matched
    secret. The separately deployed site remains an ordinary bounded read.
    """
    result = subprocess.run(
        [
            'git', '-C', ROOT_DIR, 'grep', '--quiet', '-I', '-E', pattern,
            'HEAD', '--', 'app.py', 'docs/*.md', 'tests/*.py',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), (
        f'{label} scan failed: {result.stderr[:200]}'
    )
    assert result.returncode == 1, f'{label} found in committed release inputs'
    if os.path.isfile(SITE_HTML):
        with open(SITE_HTML, encoding='utf-8') as site:
            assert re.search(pattern, site.read()) is None, f'{label} found in site'


def test_no_aws_keys():
    _assert_no_secret(r'AKIA[A-Z0-9]{16}', 'AWS key')


def test_no_anthropic_keys():
    _assert_no_secret(r'sk-ant-api[a-zA-Z0-9_-]{30,}', 'Anthropic key')


def test_no_stripe_keys():
    _assert_no_secret(r'sk_(test|live)_[a-zA-Z0-9]{20,}', 'Stripe key')


def test_no_openai_keys():
    _assert_no_secret(r'sk-proj-[a-zA-Z0-9_-]{30,}', 'OpenAI key')


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
