#!/usr/bin/env python3
"""R15 — Kontrollierte Live-Validation V1 vs V2 gegen Tibors 2025-Daten.

Aufruf:
    ANTHROPIC_API_KEY=sk-ant-... python3 scripts/r15_live_validation.py

Optional:
    --tibor-dir /pfad/zu/Tibor/2025/        (default: /Users/miguelschumann/Desktop/Tibor/2025)
    --homebase FRA
    --skip-v1                               (nur V2 messen, V1-Baseline aus Datei laden)
    --skip-v2                               (nur V1 messen)

Was passiert:
1) Liest LSB + SE + alle Dienstplan-PDFs aus dem Tibor-Verzeichnis.
2) Baut das Files-Dict im hybrid_analyze-Format.
3) Run #1: AEROTAX_CAS_READER_V2 NICHT gesetzt -> V1-Baseline.
4) Run #2: AEROTAX_CAS_READER_V2=1 -> V2-Pfad.
5) Vergleicht KPIs deterministisch + kritische Tage (BLR-Heimkehr etc.).
6) Schreibt:
   - R15_VALIDATION_OUTPUT.json (Rohdaten beider Runs)
   - CAS_READER_V2_R15_LIVE_VALIDATION.md (Markdown-Report)

Kosten: voller End-to-End-Run zweimal. Mit Tibors 13 CAS-PDFs sind das
~30 Sonnet-Calls pro Run = ~60 Calls insgesamt + LSB+SE-Calls.

KEIN Deploy. KEIN Default-Switch. Reine Mess-Operation lokal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Repo-Root im sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_TIBOR_DIR = Path('/Users/miguelschumann/Desktop/Tibor/2025')


def _load_pdf(path: Path) -> bytes:
    return path.read_bytes()


def _collect_files(tibor_dir: Path) -> dict:
    """Findet LSB, SE, Dienstplan-PDFs und liefert das files-Dict fuer
    hybrid_analyze. Schliesst die Flugstundenuebersicht aus (Legacy)."""
    files = {'lsb': [], 'se': [], 'cas': [], 'dp': []}

    for entry in tibor_dir.iterdir():
        if entry.is_dir():
            continue
        name = entry.name.lower()
        if name.startswith('.'):
            continue
        if 'lohnsteuer' in name:
            files['lsb'].append((_load_pdf(entry), entry.name))
        elif 'strecken' in name:
            files['se'].append((_load_pdf(entry), entry.name))
        # Flugstundenuebersicht bewusst ignorieren (Legacy, nicht Pflichtdoc)

    # CAS aus Dienstplan-Unterordner
    cas_dir = tibor_dir / 'Dienstplan'
    if cas_dir.is_dir():
        for entry in sorted(cas_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() == '.pdf':
                files['cas'].append((_load_pdf(entry), entry.name))

    return files


def _build_form(homebase: str) -> dict:
    return {
        'year': '2025',
        'base': homebase,
        'entfernung_km': '40',     # synthetisch — beeinflusst nur Pendlerpauschale, nicht Z76
        'fahrzeit_min': '45',      # synthetisch
        'anfahrt_min': '45',       # ueber commute_min_for_cas-Filter relevant
    }


def _extract_kpis(result: dict) -> dict:
    """Extrahiert KPIs aus dem hybrid_analyze-Result-Dict. Aggregiert Z-Buckets
    aus tage_detail (klass-Counts + eur-Summe), weil classification kein
    summary-Substruktur hat."""
    if not isinstance(result, dict):
        return {'error': 'result_not_dict'}

    out = {
        'gesamt_eur':      None,
        'z72_tage': 0, 'z72_eur': 0.0,
        'z73_tage': 0, 'z73_eur': 0.0,
        'z74_tage': 0, 'z74_eur': 0.0,
        'z76_tage': 0, 'z76_eur': 0.0,
        'fahrtage':        None,
        'arbeitstage':     None,
        'reinigungstage':  None,
        'hotel_naechte':   None,
        'trinkgeld_eur':   None,
        'classification_keys': sorted(list(
            (result.get('classification') or {}).keys()
        ))[:60] if isinstance(result.get('classification'), dict) else [],
    }
    cls = result.get('classification') or {}
    if isinstance(cls, dict):
        for k_src, k_dst in [
            ('gesamt_wiso', 'gesamt_eur'),
            ('gesamt_eur', 'gesamt_eur'),
            ('fahr_tage', 'fahrtage'),
            ('arbeitstage', 'arbeitstage'),
            ('reinigungstage', 'reinigungstage'),
            ('hotel_naechte', 'hotel_naechte'),
            ('trinkgeld_eur', 'trinkgeld_eur'),
        ]:
            if k_src in cls and out.get(k_dst) is None:
                out[k_dst] = cls.get(k_src)
        # Z-Buckets aus tage_detail aggregieren (klass-Counts + eur-Summen)
        tage_detail = cls.get('tage_detail') or []
        for td in tage_detail:
            if not isinstance(td, dict):
                continue
            klass = (td.get('klass') or td.get('classification') or '').upper().strip()
            eur = float(td.get('eur') or td.get('vma_eur') or 0)
            for bucket in ('Z72', 'Z73', 'Z74', 'Z76'):
                if klass == bucket:
                    out[f'{bucket.lower()}_tage'] += 1
                    out[f'{bucket.lower()}_eur'] += eur
                    break

    # normalized_tours_audit (Parallelpfad) als zweite Quelle
    nta = (result.get('normalized_tours_audit')
           or cls.get('normalized_tours_audit')
           or {})
    if isinstance(nta, dict):
        out['normalized_tours_kpis'] = {
            'fahrtage':       nta.get('fahrtage'),
            'arbeitstage':    nta.get('arbeitstage'),
            'hotel_naechte':  nta.get('hotel_naechte'),
            'reinigungstage': nta.get('reinigungstage'),
            'z72':            nta.get('z72'),
            'z73':            nta.get('z73'),
            'z74':            nta.get('z74'),
            'z76':            nta.get('z76'),
        }
    # Runden
    for b in ('z72', 'z73', 'z74', 'z76'):
        out[f'{b}_eur'] = round(out[f'{b}_eur'], 2)
    return out


CRITICAL_DATES = [
    ('2025-01-06', 'BLR Heimkehr — X darf NICHT als Frei verloren gehen'),
    ('2025-01-04', 'BLR Mid-Tour — X innerhalb Layover'),
    ('2025-01-05', 'BLR Mid-Tour — X innerhalb Layover'),
    ('2025-04-08', 'Pattern A residual — target_iata-Risiko'),
    ('2025-10-05', 'Pattern A residual — target_iata-Risiko'),
]


def _extract_critical_days(result: dict) -> list:
    """Sammelt fuer kritische Daten Tag-Audit-Zeilen."""
    found = []
    cls = result.get('classification') or {}
    tage_detail = (cls.get('tage_detail')
                   or result.get('tage_detail')
                   or [])
    nta_bydate = ((result.get('normalized_tours_audit') or {}).get('by_date')
                  or (cls.get('normalized_tours_audit') or {}).get('by_date')
                  or {})

    for ds, note in CRITICAL_DATES:
        row = {'datum': ds, 'note': note}
        for t in tage_detail:
            if isinstance(t, dict) and (t.get('datum') == ds or t.get('date') == ds):
                row['legacy'] = {
                    'klass':   t.get('klass') or t.get('classification'),
                    'marker':  t.get('marker'),
                    'routing': t.get('routing'),
                    'layover_ort': t.get('layover_ort'),
                    'overnight': t.get('overnight_after_day'),
                    'eur':     t.get('eur') or t.get('vma_eur'),
                    'reason_counted': t.get('reason_counted'),
                    'why_suspicious': t.get('why_suspicious'),
                }
                break
        if ds in nta_bydate:
            entry = nta_bydate[ds] or {}
            row['normalized'] = {
                'bucket':      entry.get('bucket'),
                'eur':         entry.get('eur'),
                'country':     entry.get('country'),
                'rate':        entry.get('rate'),
                'source':      entry.get('source'),
                'tour_id':     entry.get('tour_id'),
            }
        found.append(row)
    return found


def _extract_phantom_z76(result: dict) -> list:
    """BH-003c: SE-only Z76-Tage finden (klass=Z76, reason mentioning SE-Override)."""
    cls = result.get('classification') or {}
    tage_detail = cls.get('tage_detail') or []
    phantoms = []
    for t in tage_detail:
        if not isinstance(t, dict):
            continue
        klass = (t.get('klass') or '').upper()
        reason = (t.get('reason_counted') or '')
        if klass == 'Z76' and 'SE-Override' in reason:
            phantoms.append({
                'datum':  t.get('datum'),
                'marker': t.get('marker'),
                'eur':    t.get('eur'),
                'reason': reason[:160],
            })
    return phantoms[:30]


def _extract_blr_jan_window(result: dict) -> list:
    """Extrahiert 2025-01-03..06 fuer BLR-Audit."""
    cls = result.get('classification') or {}
    tage_detail = cls.get('tage_detail') or []
    out = []
    for t in tage_detail:
        if not isinstance(t, dict):
            continue
        ds = t.get('datum') or t.get('date')
        if ds and ds.startswith('2025-01-0') and ds <= '2025-01-08':
            out.append({
                'datum': ds,
                'klass': t.get('klass'),
                'marker': t.get('marker'),
                'routing': t.get('routing'),
                'layover_ort': t.get('layover_ort'),
                'overnight': t.get('overnight_after_day'),
                'eur': t.get('eur'),
                'reason': t.get('reason_counted'),
                'why_suspicious': t.get('why_suspicious'),
            })
    return out


def _run_once(label: str, files: dict, form: dict) -> dict:
    """Ein End-to-End-Run. Importiert app FRISCH, damit env-Flag wirkt."""
    # Cleane Import-Reset, damit AEROTAX_CAS_READER_V2 zur Import-Zeit greift,
    # wo das relevant ist.
    for mod in list(sys.modules.keys()):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    import importlib
    app_mod = importlib.import_module('app')

    t0 = time.time()
    try:
        result = app_mod.hybrid_analyze(form=form, files=files, job_id=f'r15-{label}')
        ok = True
        err = None
    except Exception as e:
        result = {}
        ok = False
        err = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
    elapsed = time.time() - t0

    return {
        'label':         label,
        'ok':            ok,
        'error':         err,
        'wallclock_s':   round(elapsed, 1),
        'kpis':          _extract_kpis(result),
        'critical_days': _extract_critical_days(result),
        'phantom_z76':   _extract_phantom_z76(result),
        'blr_jan_window': _extract_blr_jan_window(result),
        'errors_field':  result.get('errors', []) if isinstance(result, dict) else [],
        '_v2_active_seen': bool(
            (result.get('cas_reader_v2_active')
             or any((c.get('_v2_active') for c in (result.get('cas_runs') or [])
                     if isinstance(c, dict))))
        ),
    }


def _diff_kpi(v1: dict, v2: dict) -> dict:
    out = {}
    keys = sorted(set(v1.keys()) | set(v2.keys()))
    for k in keys:
        a = v1.get(k); b = v2.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[k] = {'v1': a, 'v2': b, 'diff': round(b - a, 2)}
        elif isinstance(a, dict) and isinstance(b, dict):
            out[k] = _diff_kpi(a, b)
        else:
            out[k] = {'v1': a, 'v2': b}
    return out


def _markdown_report(payload: dict, out_path: Path) -> None:
    runs = payload['runs']
    v1 = runs.get('v1') or {}
    v2 = runs.get('v2') or {}
    v1_kpis = v1.get('kpis') or {}
    v2_kpis = v2.get('kpis') or {}
    diff = _diff_kpi(v1_kpis, v2_kpis)

    rows = []
    for key in ['z72_tage', 'z72_eur', 'z73_tage', 'z73_eur',
                'z74_tage', 'z74_eur', 'z76_tage', 'z76_eur',
                'fahrtage', 'arbeitstage', 'reinigungstage',
                'hotel_naechte', 'trinkgeld_eur', 'gesamt_eur']:
        d = diff.get(key, {})
        rows.append(
            f"| {key} | {v1_kpis.get(key)} | {v2_kpis.get(key)} | "
            f"{d.get('diff') if isinstance(d, dict) else ''} |"
        )

    crit_lines = []
    for r in (v2.get('critical_days') or []):
        crit_lines.append(
            f"- **{r.get('datum')}** — {r.get('note')}\n"
            f"  - legacy: {r.get('legacy')}\n"
            f"  - normalized: {r.get('normalized')}"
        )

    decision_block = payload.get('decision', 'PENDING')

    md = f"""# CAS Reader V2 — R15 Live-Validation Report

**Stand:** {payload.get('timestamp')}
**Branch:** `{payload.get('git_branch')}`
**Tibor-Dir:** `{payload.get('tibor_dir')}`
**Skip:** v1={payload.get('skip_v1', False)}, v2={payload.get('skip_v2', False)}

## 1. Setup

| Run | Flag | Wallclock | OK | _v2_active gesehen |
| --- | --- | --- | --- | --- |
| V1  | AEROTAX_CAS_READER_V2 not set | {v1.get('wallclock_s')}s | {v1.get('ok')} | {v1.get('_v2_active_seen')} |
| V2  | AEROTAX_CAS_READER_V2=1       | {v2.get('wallclock_s')}s | {v2.get('ok')} | {v2.get('_v2_active_seen')} |

Errors V1: `{v1.get('error') or '—'}`
Errors V2: `{v2.get('error') or '—'}`

## 2. KPI Vergleich V1 vs V2

| KPI | V1 | V2 | Diff |
| --- | --- | --- | --- |
""" + "\n".join(rows) + f"""

### Normalized-Tours-Audit (Parallelpfad)
V1: `{v1_kpis.get('normalized_tours_kpis')}`
V2: `{v2_kpis.get('normalized_tours_kpis')}`

## 3. Bekannte Problemfaelle (V2-Run)

""" + ("\n".join(crit_lines) if crit_lines else "_keine kritischen Tage gefunden in tage_detail/normalized_tours_audit_") + f"""

## 4. Audit-Auszug

Volle Daten in `R15_VALIDATION_OUTPUT.json` (Roh-JSON beider Runs).

## 5. Entscheidung

**{decision_block}**

## 6. Wenn NEEDS_FIX

Konkrete Abweichungen werden hier nach Analyse manuell ergaenzt (Datum, erwarteter Wert, tatsaechlicher Wert, vermutete Ursache, Funktion, Fix-Vorschlag).
"""
    out_path.write_text(md, encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tibor-dir', default=str(DEFAULT_TIBOR_DIR))
    ap.add_argument('--homebase', default='FRA')
    ap.add_argument('--skip-v1', action='store_true')
    ap.add_argument('--skip-v2', action='store_true')
    ap.add_argument('--out-json', default=str(ROOT / 'R15_VALIDATION_OUTPUT.json'))
    ap.add_argument('--out-md', default=str(ROOT / 'CAS_READER_V2_R15_LIVE_VALIDATION.md'))
    args = ap.parse_args()

    if not os.environ.get('ANTHROPIC_API_KEY'):
        print("FEHLER: ANTHROPIC_API_KEY nicht gesetzt. Abbruch.")
        print("Aufruf:  ANTHROPIC_API_KEY=sk-ant-... python3 scripts/r15_live_validation.py")
        sys.exit(2)

    tibor = Path(args.tibor_dir)
    if not tibor.is_dir():
        print(f"FEHLER: Tibor-Verzeichnis nicht gefunden: {tibor}")
        sys.exit(2)

    # Pre-flight check
    probe = _collect_files(tibor)
    print(f"Gefunden: LSB={len(probe['lsb'])} SE={len(probe['se'])} CAS={len(probe['cas'])}")
    if not probe['lsb'] or not probe['se'] or not probe['cas']:
        print("FEHLER: Pflichtdokumente unvollstaendig. Abbruch.")
        sys.exit(2)

    form = _build_form(args.homebase)

    runs = {}
    if not args.skip_v1:
        # FRISCH laden — hybrid_analyze setzt files[k]=None nach Verbrauch
        files = _collect_files(tibor)
        os.environ.pop('AEROTAX_CAS_READER_V2', None)
        print("=== V1-Run startet (Flag off) ===")
        runs['v1'] = _run_once('v1', files, form)
        print(f"V1 done in {runs['v1']['wallclock_s']}s ok={runs['v1']['ok']}")

    if not args.skip_v2:
        # FRISCH laden — V1 hat das vorige Dict geleert
        files = _collect_files(tibor)
        os.environ['AEROTAX_CAS_READER_V2'] = '1'
        print("=== V2-Run startet (Flag on) ===")
        runs['v2'] = _run_once('v2', files, form)
        print(f"V2 done in {runs['v2']['wallclock_s']}s ok={runs['v2']['ok']}")
        os.environ.pop('AEROTAX_CAS_READER_V2', None)

    git_branch = os.popen('git -C "%s" rev-parse --abbrev-ref HEAD 2>/dev/null' % ROOT).read().strip()

    payload = {
        'timestamp':  datetime.now().isoformat(),
        'git_branch': git_branch or 'unknown',
        'tibor_dir':  str(tibor),
        'homebase':   args.homebase,
        'skip_v1':    args.skip_v1,
        'skip_v2':    args.skip_v2,
        'runs':       runs,
        'decision':   'PENDING — bitte KPI-Diff + critical days pruefen und manuell PASS_FOR_STAGING oder NEEDS_FIX setzen.',
    }
    Path(args.out_json).write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
    _markdown_report(payload, Path(args.out_md))
    print(f"JSON: {args.out_json}")
    print(f"MD:   {args.out_md}")


if __name__ == '__main__':
    main()
