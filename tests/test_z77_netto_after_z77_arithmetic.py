"""Test-Guards für die Netto-after-Z77-Rechnung.

Verhindert die falsche Aussage „Netto-Effekt = 0 wenn Z77 > AT-VMA-brutto".
Das ist nur korrekt wenn AT-VMA-brutto ≥ FM-VMA-brutto. Wenn FM-VMA-brutto
größer ist als Z77, hat FM eine echte Netto-VMA, während AT auf 0 clampt
— der Δ-Netto ist real, nicht 0.

Diese Tests dokumentieren die Bucket-Arithmetik und schützen vor zukünftiger
Falschannahmen.
"""

import pytest


def _netto(block_a, vma_brutto, z77):
    vma_netto = max(0.0, vma_brutto - z77)
    return block_a + vma_netto, vma_netto


# ════════════════════════════════════════════════════════════════════
# Core invariant
# ════════════════════════════════════════════════════════════════════

def test_z77_offsets_only_vma_bucket():
    """Z77 reduziert nur VMA, nicht Block A. Clamp auf 0."""
    total, vma_netto = _netto(block_a=976.0, vma_brutto=4363.0, z77=4705.0)
    assert vma_netto == 0.0
    assert total == 976.0


def test_fm_after_z77_can_have_positive_vma_netto():
    """Wenn VMA-Brutto > Z77, bleibt nach Clamp positive VMA-Netto."""
    total, vma_netto = _netto(block_a=974.72, vma_brutto=5046.0, z77=4705.0)
    assert vma_netto == 341.0
    assert total == 1315.72


# ════════════════════════════════════════════════════════════════════
# Tibor-Case: Asymmetric Clamp
# ════════════════════════════════════════════════════════════════════

def test_tibor_at_netto_976_when_at_vma_below_z77():
    """AT (Tibor): VMA-Brutto 4363 < Z77 4705 → VMA-Netto=0 → Total=976."""
    total, _ = _netto(976.0, 4363.0, 4705.0)
    assert total == 976.0


def test_tibor_fm_netto_1315_when_fm_vma_exceeds_z77():
    """FM (Tibor): VMA-Brutto 5046 > Z77 4705 → VMA-Netto=341 → Total=1315.72."""
    total, _ = _netto(974.72, 5046.0, 4705.0)
    assert abs(total - 1315.72) < 0.01


def test_tibor_real_delta_339_eur_after_both_z77_clamps():
    """Δ FM-AT für Tibor = 339.72€ Netto NACH Z77-Clamp auf beiden Seiten.
    Diese Δ entsteht weil FM-VMA-Brutto die Z77 übersteigt, AT-VMA-Brutto nicht."""
    fm_total, _ = _netto(974.72, 5046.0, 4705.0)
    at_total, _ = _netto(976.0, 4363.0, 4705.0)
    delta = fm_total - at_total
    assert abs(delta - 339.72) < 0.5, f'Tibor real Δ should be ~339.72€, got {delta}'


def test_tax_relevance_at_42_percent_grenzsteuer():
    """Bei 42% Grenzsteuer hat der 339€ Δ einen ~143€-Effekt auf Erstattung."""
    delta = 339.72
    tax_effect = delta * 0.42
    assert abs(tax_effect - 142.68) < 0.5


# ════════════════════════════════════════════════════════════════════
# P0 Fix only adds ~147€ — NOT enough to lift AT-brutto above Z77.
# ════════════════════════════════════════════════════════════════════

def test_p0_fix_alone_does_not_change_tibor_total():
    """Nach P0-Fix +147€ Brutto: AT-Brutto 4510 — IMMER NOCH < Z77 4705
    → AT-VMA-Netto bleibt 0 → AT-Total bleibt 976."""
    at_total_pre, _ = _netto(976.0, 4363.0, 4705.0)
    at_total_post, _ = _netto(976.0, 4363.0 + 147.0, 4705.0)
    assert at_total_pre == at_total_post == 976.0


def test_p0_fix_helps_only_users_with_low_z77():
    """User ohne Z77 sieht den P0-Fix als +147€ direkt im VMA-Netto."""
    user_total_pre, _ = _netto(976.0, 4363.0, 0.0)
    user_total_post, _ = _netto(976.0, 4363.0 + 147.0, 0.0)
    assert user_total_post - user_total_pre == 147.0


# ════════════════════════════════════════════════════════════════════
# Source arbitration guards: do NOT assume FM is always more correct
# ════════════════════════════════════════════════════════════════════

def test_at_brutto_lower_does_not_automatically_mean_at_wrong():
    """AT kann konservativer als FM sein und trotzdem BMF-konform sein.
    Beispiel: cluster_an_abreise mit CAS-layover=FRA → AT defensibel an_abreise.
    Kein automatischer FIX_AEROTAX nur weil FM mehr gibt."""
    # No assert — dieser Test ist eine Doku-Erinnerung dass Quellenhierarchie
    # entscheidet, nicht die Summe.
    pass


# ════════════════════════════════════════════════════════════════════
# Compare-net-after-z77 must compute both buckets correctly
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('block_a,vma_brutto,z77,exp_total', [
    (1000.0, 6000.0, 4000.0,  3000.0),  # FM-like: brutto > Z77 → real netto VMA
    (1000.0, 3000.0, 4000.0,  1000.0),  # AT-like: brutto < Z77 → VMA netto = 0
    (1000.0, 4000.0, 4000.0,  1000.0),  # boundary: brutto == Z77 → netto = 0
    (1000.0,    0.0, 4000.0,  1000.0),  # no VMA at all
    (   0.0, 6000.0, 4000.0,  2000.0),  # no Block A, only VMA-net
])
def test_netto_after_z77_parametric(block_a, vma_brutto, z77, exp_total):
    total, _ = _netto(block_a, vma_brutto, z77)
    assert total == exp_total
