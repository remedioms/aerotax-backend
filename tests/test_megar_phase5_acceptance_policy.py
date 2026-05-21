"""MegaR Phase 5 — Acceptance Policy Tests.

Verifiziert:
- Alle xfails in test_tibor_2025_golden_acceptance haben documented_reference_disagreement reason
- Alle xfails referenzieren mindestens ein Audit-Doc
- User-facing PDF darf nicht FollowMe erwaehnen
- result_data interne audit-spur darf FollowMe-Reference enthalten

Spec: docs/GOLDEN_ACCEPTANCE_POLICY.md
"""
import os
import re
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _read_acceptance_file():
    p = os.path.join(THIS_DIR, 'test_tibor_2025_golden_acceptance.py')
    return open(p, encoding='utf-8').read()


def test_all_xfails_have_documented_reason():
    """Jeder xfail muss 'documented_reference_disagreement' im reason haben.

    Acceptance-File nutzt eine Konstante `_BELEGTE_ABWEICHUNG = pytest.mark.xfail(reason=...)`
    plus parametrize-`pytest.param(..., marks=pytest.mark.xfail(reason=...))`. Beide
    Patterns muessen documented_reference_disagreement im reason haben.
    """
    src = _read_acceptance_file()
    reason_blocks = re.findall(r"xfail\(\s*reason=['\"]([^'\"]+)['\"]", src)
    # Mindestens 5 xfail-mit-reason (1 Konstanter + 5 parametrize)
    assert len(reason_blocks) >= 5, \
        f'Erwarte mindestens 5 xfail mit explizitem reason, fand {len(reason_blocks)}'
    # Alle reasons muessen documented_reference_disagreement enthalten
    for r in reason_blocks:
        assert 'documented_reference_disagreement' in r, \
            f'xfail-reason ohne documented_reference_disagreement: {r[:80]}'


def test_xfails_reference_audit_docs():
    """xfail-reasons referenzieren mindestens eine Audit-Doc."""
    src = _read_acceptance_file()
    # Audit-docs die referenziert werden sollten
    docs = [
        'FINAL_DISAGREEMENT_DECISION.md',
        'FIX10_PHANTOM_BEWEIS.md',
        'CLOSEOUT1_DISAGREEMENT_AUDIT.md',
        'FINAL_KPI_REST_DECISION.md',
    ]
    found_refs = sum(src.count(d) for d in docs)
    assert found_refs >= 2, \
        f'xfail-reasons sollten mindestens 2 audit-doc-References enthalten, fand {found_refs}'


def test_no_strict_true_xfail_in_acceptance():
    """xfails in Acceptance haben strict=False (oder kein strict, default ist False).

    strict=True würde False-Negative produzieren wenn Pipeline-Fix später greift.
    """
    src = _read_acceptance_file()
    # Kein strict=True in den xfails
    assert 'strict=True' not in src, 'strict=True in xfail nicht erlaubt'


def test_user_facing_pdf_no_followme_reference():
    """User-facing PDF-Texte dürfen NICHT FollowMe erwaehnen."""
    app_src = open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()
    # PDF-Renderer-Funktionen
    pdf_functions = ['def render_pdf', 'def create_pdf', 'def _build_pdf']
    pdf_blocks = []
    for fn in pdf_functions:
        idx = app_src.find(fn)
        if idx > 0:
            pdf_blocks.append(app_src[idx:idx + 15000])

    # FollowMe darf in PDF-User-Text nicht vorkommen
    user_facing_followme_pattern = re.compile(
        r'(?:c\.drawString|p\.drawString|story\.append|Paragraph\()\s*[^,]*FollowMe',
        re.IGNORECASE
    )
    for block in pdf_blocks:
        hits = user_facing_followme_pattern.findall(block)
        assert not hits, f'User-facing PDF erwaehnt FollowMe: {hits[:2]}'


def test_internal_audit_log_may_reference_followme():
    """Internal audit/log darf FollowMe-Reference enthalten (kein Verbot)."""
    app_src = open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()
    # FollowMe in print/log ist OK
    log_followme = app_src.count('FollowMe') + app_src.count('followme')
    # Wir VERLANGEN nicht zwingend, dass FollowMe-Refs da sind — nur dass sie
    # ERLAUBT sind (kein blocker-Pattern).
    assert log_followme >= 0  # Sanity (immer wahr)


def test_acceptance_file_has_belegte_abweichung_marker():
    """Konstantes _BELEGTE_ABWEICHUNG-Marker im File."""
    src = _read_acceptance_file()
    assert '_BELEGTE_ABWEICHUNG' in src, 'Konstanter xfail-Marker fehlt'


def test_acceptance_categories_defined_in_policy_doc():
    """GOLDEN_ACCEPTANCE_POLICY.md definiert 4 Kategorien."""
    p = os.path.join(ROOT_DIR, 'docs', 'GOLDEN_ACCEPTANCE_POLICY.md')
    assert os.path.exists(p)
    content = open(p, encoding='utf-8').read()
    for cat in ('PASS', 'ACCEPTED_DIFFERENCE', 'NEEDS_REVIEW', 'FAIL'):
        assert cat in content, f'Kategorie {cat} fehlt in Policy-Doc'
