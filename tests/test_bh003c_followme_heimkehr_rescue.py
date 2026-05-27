"""BH-003c FollowMe Mischfall-Heimkehr-Rescue (2026-05-22).

Klassifikator-Verbesserung: wenn der Vortag ein Auslands-Layover (Z76) war
UND heute als Mischfall-Issue klassifiziert wurde, übernehmen wir die
FollowMe-Heuristik — heute ist der An-/Abreise-Tag des Vortag-Lands.

User-Beweis (Tibor 2025): 10 Mischfall-Tage standen ohne VMA, FollowMe
gibt ~200€ Z76 An-/Abreise des jeweiligen Lands."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')


def _read():
    with open(APP_PATH, encoding='utf-8') as f:
        return f.read()


def test_bh003c_rescue_block_exists():
    src = _read()
    assert 'BH-003c 2026-05-22' in src
    assert 'FollowMe Mischfall-Heimkehr-Rescue' in src


def test_bh003c_fires_only_after_bh003b_fails():
    """BH-003c-Block sitzt im `if klass != 'Z76'` Branch nach BH-003b."""
    src = _read()
    idx_b = src.find('BH-003b 2026-05-21')
    idx_c = src.find('BH-003c 2026-05-22')
    assert idx_b > 0 and idx_c > idx_b, 'BH-003c muss NACH BH-003b stehen'


def test_bh003c_requires_foreign_layover_yesterday():
    """H1+H2: prev.layover_ort nicht leer und kein Inland-Code."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    assert '_bh003c_prev_layover' in block
    assert 'not _is_inland_code(_bh003c_prev_layover)' in block


def test_bh003c_requires_layover_not_homebase():
    """H3: kein Heimat-Zirkel (prev_layover != homebase)."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    assert '_bh003c_prev_layover != _bh003c_hb_up' in block


def test_bh003c_uses_bmf_an_abreise_not_voll():
    """H4: konservativ — an_abreise (8h-Satz), kein voll_24h."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    assert "_bh003c_bmf.get('an_abreise', 0)" in block
    assert "_bh003c_bmf.get('voll_24h'" not in block


def test_bh003c_writes_audit_rescue_entry():
    """Audit-Trail: Eintrag in `rescues` mit rescue_type='bh003c_followme_heimkehr'."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    assert "'bh003c_followme_heimkehr'" in block
    assert "rescues.append(" in block


def test_bh003c_inactive_when_layover_is_homebase():
    """H3-Edge-Case: wenn prev.layover_ort == homebase, KEIN Z76-Rescue (Tour-Ende intern)."""
    # Statisch prüfen: der Code-Pfad ohne H3 fired nicht
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    # H3-Check ist Pflichtteil der if-Bedingung
    h3_line = re.search(r'_bh003c_prev_layover\s*!=\s*_bh003c_hb_up', block)
    assert h3_line is not None


def test_bh003c_skipped_when_bmf_missing():
    """H4: Wenn BMF-Mapping kein an_abreise liefert (Land unbekannt), kein Rescue."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    # if _bh003c_eur > 0 ist die Gate-Bedingung
    assert 'if _bh003c_eur > 0:' in block


def test_bh003c_keeps_issue_fallback_when_no_layover():
    """Wenn keine der H1-H4 Bedingungen greift, bleibt es Issue (kein VMA)."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    # Nach dem BH-003c-Block kommt der finale Issue-Fallback
    after = src[idx:idx + 6000]
    assert "klass = 'Issue'" in after
    assert "Heimkehr aus Vortag-Tour — separater Tour-Abschluss" in after


def test_bh003c_reason_string_mentions_followme():
    """Audit-Reason erwähnt FollowMe-Heimkehr explizit (Auditability)."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    assert 'FollowMe-Heimkehr' in block


def test_bh003c_print_log_present():
    """Log-Print für Operations-Audit (datum + prev_layover + eur)."""
    src = _read()
    idx = src.find('BH-003c 2026-05-22')
    block = src[idx:idx + 5000]
    assert '[bh003c-rescue]' in block
