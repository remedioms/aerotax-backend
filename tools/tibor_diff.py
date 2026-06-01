"""Offline-Diff: AeroTax PRODUKTIV-Pfad (normalized_tours.py) vs FollowMe-Golden.

Kein Sonnet, kein Netz, <2s. Bildet EXAKT den Live-Produktiv-Switch aus
hybrid_analyze (app.py ~29569ff) nach:
    reader_facts (aus Fixture) → _adapt_cas_reader_to_builder
    → normalized_tours.build_normalized_tours
    → normalized_tours.calculate_allowances_from_normalized_tours
Das ist der Pfad, der live das PDF erzeugt (AEROTAX_USE_NORMALIZED_TOURS=1,
productive switch bei tours>0). NICHT _classify_days_from_normalized_tours —
die läuft nur in Tests.

Diff gegen tests/fixtures/followme_golden_tibor_2025.json. Steuerjahr-Filter
(nur 2025) ist anwendbar via --year. Aufruf:
    python3 tools/tibor_diff.py [--days] [--year 2025]
"""
import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
FIXTURE_DIR = os.path.join(ROOT_DIR, 'tests', 'fixtures')
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module
import normalized_tours as nt

CAS_DIR = '/Users/miguelschumann/Desktop/Steuer 25/CAS'


def _load_cas_pdf_bytes():
    """Alle CAS-Monats-PDFs als bytes-Liste (für den deterministischen
    cas_reconcile-Schritt = zeitbasierte overnight/hotel-Wahrheit)."""
    blobs = []
    if not os.path.isdir(CAS_DIR):
        return blobs
    for fn in sorted(os.listdir(CAS_DIR)):
        if fn.lower().endswith('.pdf'):
            with open(os.path.join(CAS_DIR, fn), 'rb') as f:
                blobs.append(f.read())
    return blobs


def _build_bmf_table(year):
    """Nachbau des Produktiv-BMF-Tabellenbaus (app.py ~29605)."""
    bmf_year = app_module.BMF_AUSLAND_BY_YEAR.get(year, {}) or {}
    table = {}
    for iata, country in (app_module.IATA_TO_BMF or {}).items():
        entry = bmf_year.get(country)
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            table[iata] = {
                'voll_24h':   float(entry[0]),
                'an_abreise': float(entry[1]),
                'country':    country,
            }
    return table


SE_PDF = '/Users/miguelschumann/Desktop/Tibor/2025/2025 Streckeneinsatzabrechnungen.pdf'


def _real_se_rows():
    """Echte SE-Rows aus der Streckeneinsatz-Abrechnung (deterministischer
    Parser, kein Sonnet). Das ist FollowMe's Z76-Wahrheit: stfrei-Ort-Spalte.
    Verifiziert: 114 Ausland-stfrei (Golden Z76=113), 17 Inland (Golden 17)."""
    sys.path.insert(0, THIS_DIR)
    from se_parser_det import parse_se_pdf
    rows = parse_se_pdf(SE_PDF)
    return [r for r in rows if not r['storno']]


def run_pipeline(year=2025, homebase='FRA'):
    v2 = json.load(open(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json')))
    # reader_facts ist das Reader-Output-Format, das der Builder erwartet
    # reader_facts trägt bereits alle Keys, die build_normalized_tours liest
    # (marker_raw, routing, activity_type, overnight_after_day, starts/ends_at_homebase,
    # layover_ort, has_fl, duty_duration_minutes). Der V1-Adapter würde sie zerstören
    # (erwartet location/flights), also direkt füttern.
    cas_days = [t['reader_facts'] for t in v2['tage_detail'] if t.get('reader_facts')]
    se_rows = _real_se_rows()

    # cas_reconcile: zeitbasierte overnight/hotel-Wahrheit aus den harten PDF-
    # Fakten (UTC-Zeiten + Flughafen-TZ), exakt wie der Live-Pfad VOR dem Builder.
    if '--no-reconcile' not in sys.argv:
        from cas_integration import reconcile_cas_days
        blobs = _load_cas_pdf_bytes()
        cas_days, recon_audit = reconcile_cas_days(blobs, cas_days, homebase, force=True)
        if '--verbose' in sys.argv:
            print(f"[reconcile] applied={recon_audit.get('applied')} "
                  f"corrections={recon_audit.get('corrections_count')} "
                  f"files_ok={recon_audit.get('parser_files_ok')} "
                  f"reason={recon_audit.get('reason')}")

    tours = nt.build_normalized_tours(
        cas_days=cas_days, se_rows=se_rows, year=year,
        employee_context=None, homebase=homebase, rules=None,
    )
    bmf_table = _build_bmf_table(year)
    result = nt.calculate_allowances_from_normalized_tours(
        tours, bmf_table, rules=None,
        iata_to_bmf=app_module.IATA_TO_BMF, se_rows=se_rows, homebase=homebase,
        cas_days=cas_days,
    )
    return result, tours


def load_golden():
    return json.load(open(os.path.join(FIXTURE_DIR, 'followme_golden_tibor_2025.json')))


def main():
    show_days = '--days' in sys.argv
    r, tours = run_pipeline()
    gold = load_golden()
    ss = gold['soll_summary']
    z76_soll = ss['z76']['gesamt']
    vma_soll = z76_soll + ss['z73']['gesamt'] + ss['z72']['gesamt'] + ss['z74']['gesamt']

    rows = [
        ('Z76 €',         round(r.z76_eur, 2),    z76_soll),
        ('Z76 Tage',      r.z76_tage,             None),
        ('Z73 € / Tage',  f'{round(r.z73_eur,2)} / {r.z73_tage}', f"{ss['z73']['gesamt']} / {ss['z73']['tage']}"),
        ('Z72 € / Tage',  f'{round(r.z72_eur,2)} / {r.z72_tage}', f"{ss['z72']['gesamt']} / {ss['z72']['tage']}"),
        ('Z74 € / Tage',  f'{round(r.z74_eur,2)} / {r.z74_tage}', f"{ss['z74']['gesamt']} / {ss['z74']['tage']}"),
        ('VMA gesamt €',  round(r.z72_eur+r.z73_eur+r.z74_eur+r.z76_eur, 2), vma_soll),
        ('Hotelnächte',   r.hotel_naechte,        ss['hotelaufenthalte']),
        ('Fahrtage',      r.fahrtage,             ss['fahrten']['total']),
        ('Arbeitstage',   r.arbeitstage,          ss['arbeitstage']),
        ('Reinigungstage', r.reinigungstage,      ss['reinigung']['tage']),
    ]
    print(f"\n{'Metrik':<16}{'AeroTax':>16}{'FollowMe':>16}{'Δ':>10}")
    print('-' * 58)
    for name, got, soll in rows:
        delta = ''
        try:
            if soll is not None and '/' not in str(got):
                d = round(float(got) - float(soll), 2)
                delta = f'{d:+}' + ('' if d == 0 else '  <—')
        except (TypeError, ValueError):
            delta = ''
        print(f"{name:<16}{got!s:>16}{soll!s:>16}{delta:>10}")

    if show_days:
        gdc = gold.get('day_classification', {})
        by_date = r.by_date or {}
        Y = '2025-'
        g_z76 = {d for d, v in gdc.items() if v.get('klass') == 'Z76' and d.startswith(Y)}
        a_z76 = {d for d, v in by_date.items()
                 if (v.get('klass') or v.get('bucket') or '').upper() == 'Z76' and d.startswith(Y)}
        print(f"\n=== Z76-Diff 2025: AeroTax={len(a_z76)} Golden={len(g_z76)} net={len(a_z76)-len(g_z76):+} ===")
        print(f"-- {len(a_z76 - g_z76)} EXTRA (Phantom) --")
        for d in sorted(a_z76 - g_z76):
            gd = gdc.get(d)
            gk = gd.get('klass') if gd else 'FREI/NO_VMA'
            print(f"   {d}  golden={gk}")
        print(f"-- {len(g_z76 - a_z76)} FEHLEND --")
        for d in sorted(g_z76 - a_z76):
            av = by_date.get(d) or {}
            print(f"   {d}  aerotax={(av.get('klass') or av.get('bucket') or '—')}")
        spill = [d for d in by_date
                 if (by_date[d].get('klass') or by_date[d].get('bucket') or '').upper() == 'Z76'
                 and not d.startswith(Y)]
        if spill:
            print(f"\n(+{len(spill)} Z76-Tage außerhalb 2025 = Spillover, gehören NICHT ins Steuerjahr)")

    return r, gold


if __name__ == '__main__':
    main()
