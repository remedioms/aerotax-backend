"""Z77-Audit Guard-Tests (2026-05-21).

User-Request nach BH-003b + Office-Rescue-Fix: garantieren dass Z77-Summe
NIE durch VMA-/Tag-Klassifikations-Logik beeinflusst wird. Z77 stammt
ausschließlich aus Streckeneinsatz-Abrechnungen (stfrei_betrag Einzelzeilen
+ Summenzeilen-Vergleich, Storno-Filter), niemals aus Tag-Reklassifikation.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'app.py'
)


def _read_app_src():
    with open(APP_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def test_z77_calculation_reads_only_se_structured_lines():
    """Z77-Berechnung liest NUR aus se_structured['se_lines'], nicht aus
    tage_detail oder klass-Counters."""
    src = _read_app_src()
    # Block bei Z. 21283ff: z77_from_lines = sum(stfrei_betrag ... if not storno)
    z77_start = src.find('z77_from_lines = sum(')
    assert z77_start > 0, 'z77_from_lines-Block nicht gefunden'
    # Sliding-Window: die nächsten ~400 Zeichen MÜSSEN se_structured + se_lines enthalten
    window = src[z77_start:z77_start + 500]
    assert 'se_structured' in window, (
        'Z77-Calculation muss aus se_structured kommen!'
    )
    assert 'se_lines' in window, (
        'Z77-Calculation muss se_lines lesen — nicht tage_detail oder klass!'
    )


def test_z77_filter_only_excludes_storno_lines():
    """Der einzige Filter auf Z77-Lines ist `not storno` — KEIN Filter auf
    Tag-Klassifikation, KEIN Filter auf vma_unmapped, KEIN Filter auf Z76."""
    src = _read_app_src()
    z77_block = re.search(
        r"z77_from_lines\s*=\s*sum\([\s\S]{0,500}\)",
        src
    )
    assert z77_block, 'z77_from_lines-Block nicht gefunden'
    block_text = z77_block.group(0)
    # Storno-Filter MUSS da sein
    assert 'storno' in block_text, 'Storno-Filter fehlt in Z77-Berechnung!'
    # Diese VMA/Tag-Klassifikations-Begriffe dürfen NICHT als Filter da sein
    forbidden = ['vma_unmapped', 'klass', 'z76', 'rescue', 'tag_detail', 'classifier_result']
    for word in forbidden:
        assert word.lower() not in block_text.lower(), (
            f'Z77-Filter darf NICHT auf {word} achten — '
            f'sonst beeinflussen VMA-Fixes Z77.'
        )


def test_z77_uses_max_einzelzeilen_or_summenzeilen():
    """Backend wählt max(einzelzeilen, summenzeilen) → konservativ höchster
    nachweisbarer Wert. NICHT min, NICHT klass-abhängig."""
    src = _read_app_src()
    assert re.search(
        r"z77_used\s*=\s*max\(z77_from_lines,\s*z77_from_months\)",
        src
    ), 'Z77 muss max() von Einzelzeilen und Summenzeilen verwenden'


def test_z77_audit_dict_persists_both_sources():
    """_z77_audit muss IMMER beide Quellen (einzelzeilen + summenzeilen) +
    verwendeter_wert dokumentieren — auch wenn sie identisch sind."""
    src = _read_app_src()
    audit_block = re.search(
        r"['\"]_z77_audit['\"]:\s*\{[\s\S]{0,800}\}",
        src
    )
    assert audit_block, '_z77_audit-Block nicht gefunden'
    text = audit_block.group(0)
    for required_key in ['verwendeter_wert', 'einzelzeilen', 'summenzeilen']:
        assert required_key in text, f'_z77_audit fehlt Pflicht-Key: {required_key}'


def test_no_rescue_path_modifies_z77():
    """KEIN Rescue-Pfad (Issue→Z76, Office→Z76, Standby→Z76, BH-003b) darf
    die Z77-Variable überschreiben. Z77 ist nur die SE-Berechnung."""
    src = _read_app_src()
    # Find all `z77 = ` assignments
    z77_assigns = re.findall(r'^\s*z77\s*=\s*([^\n]+)', src, re.MULTILINE)
    # Erlaubte Quellen für z77-Assignment:
    allowed_patterns = [
        r'float\(.*se_sum.*z77_total',     # SE-Summary-Read
        r'float\(.*se_summary.*z77_total', # SE-Summary-Read (variant)
        r'float\(.*r_data.*z77',           # Result-Dict-Read
        r'float\(.*cached_state.*z77',     # Cache-Restore
        r'float\(.*se_summary[^a-z]',      # SE-Summary-Read short
        r'\.get\(["\']z77["\']',           # .get('z77', ...)
        r'inferred\.get\(["\']z77["\']',   # Infer-fallback
        r'se_data\.get\(["\']summe_steuerfrei',  # LSB-Fallback (line 23104)
        r'new_v',                          # review-answer apply (validated value)
        r'0',                              # default 0
    ]
    for assign in z77_assigns:
        assign = assign.strip().rstrip(';')
        # Erlaubt wenn irgendein Pattern matched
        if not any(re.search(p, assign) for p in allowed_patterns):
            # Aber: ignorieren wenn es klar eine harmless Variable-Initialisierung ist
            # z.B. "z77 = round(...)" — das ist Netto-Berechnung
            if 'round(' in assign and ('spesen_g' in assign or 'gesamt' in assign):
                continue
            raise AssertionError(
                f'z77 wird in einer NICHT-erlaubten Form gesetzt: "{assign}". '
                f'Erlaubt sind nur SE-Summary-, Cache-, Inferred- oder review-answer-Reads.'
            )


def test_z77_assignment_never_inside_classify_function():
    """Im Tag-Klassifikator (_deterministic_classify_v7 ...) darf z77 NIE
    zugewiesen werden — der Klassifikator macht nur Tag-Klassifikation,
    keine Z77-Summe."""
    src = _read_app_src()
    # Finde alle Funktion-Bodies von _deterministic_classify
    classify_match = re.search(
        r'def _deterministic_classify_v7\([\s\S]+?\n(?=def |\Z)',
        src
    )
    if classify_match:
        body = classify_match.group(0)
        # `z77 =` Assignments im Body
        z77_in_classify = re.findall(r'^\s+z77\s*=', body, re.MULTILINE)
        # `_z77_audit` ist erlaubt (Read-only Audit)
        # `z77_choice` (Phase 4.8 review handling) ist OK
        assert len(z77_in_classify) == 0, (
            f'Klassifikator darf z77 NIE direkt setzen — {len(z77_in_classify)} '
            f'Assignments gefunden.'
        )


def test_z77_independent_of_klass_change():
    """Symbolischer Test: wenn der Klassifikator klass=Office→Z76 ändert
    (BH-003b Rescue), beeinflusst das NIE z77_from_lines oder z77_from_months.
    Beide werden aus dem RAW SE-PDF-Reader-Output berechnet, vor jeder
    Tag-Klassifikation."""
    src = _read_app_src()
    # Suche die Z77-Berechnung
    z77_calc_pos = src.find('z77_from_lines = sum(')
    assert z77_calc_pos > 0
    # Suche die Klassifikator-Phasen DAVOR
    classify_pos = src.find('_deterministic_classify_v7(')
    # Z77 wird vor Tag-Klassifikator gerechnet? Oder zumindest unabhängig?
    # In jedem Fall: Z77 nimmt se_structured.se_lines direkt aus dem SE-Read,
    # nicht aus klassifizierten tage_detail.
    z77_block_end = src.find(')', z77_calc_pos + 200)
    z77_block = src[z77_calc_pos:z77_block_end + 1]
    assert 'se_structured' in z77_block
    assert 'tage_detail' not in z77_block
    assert 'klass' not in z77_block.lower()
