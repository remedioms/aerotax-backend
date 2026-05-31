"""cas_integration — fertige, eigenstaendig getestete Bruecke fuer die Live-Pipeline.

Schritt 2 von "beides kombiniert", als EINE pure Funktion verpackt, damit das
Einhaengen in app.hybrid_analyze nur ein flag-gated Aufruf ist (minimaler Eingriff
in die grosse Live-Funktion).

Verwendung in app.py (an der Stelle, wo _cas_days adaptiert vorliegt und cas_bytes
noch nicht freigegeben wurde):

    from cas_integration import reconcile_cas_days
    _cas_days, _recon_audit = reconcile_cas_days(cas_bytes, _cas_days, homebase)

Das ist additiv + defensiv:
  - Nur aktiv wenn ENV AEROTAX_USE_CAS_RECONCILE in (1,true,on).
  - Jede Exception wird verschluckt → Live-Pipeline laeuft unveraendert weiter.
  - Wenn der deterministische Parser dem Layout nicht traut (confidence='none'),
    bleibt _cas_days unveraendert (LLM allein).
Returnt (moeglicherweise korrigierte cas_days, audit-dict).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


def _enabled() -> bool:
    return os.environ.get('AEROTAX_USE_CAS_RECONCILE', '') in ('1', 'true', 'on')


def reconcile_cas_days(
    cas_pdf_bytes: Optional[bytes],
    cas_days: List[Dict[str, Any]],
    homebase: str,
    force: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Gleicht die (vom LLM gelesenen) cas_days mit den deterministischen
    PDF-Fakten ab. Gibt korrigierte cas_days + Audit zurueck.

    Bei Flag aus / fehlenden Bytes / fremdem Layout / jeglichem Fehler:
    cas_days unveraendert, audit['applied']=False mit Grund.
    """
    audit: Dict[str, Any] = {'applied': False, 'reason': '', 'corrections_by_date': {},
                             'corrections_count': 0, 'det_only_dates': []}

    if not (force or _enabled()):
        audit['reason'] = 'flag_off'
        return cas_days, audit
    if not cas_pdf_bytes:
        audit['reason'] = 'no_cas_bytes'
        return cas_days, audit
    if not cas_days:
        audit['reason'] = 'no_llm_days'
        return cas_days, audit

    try:
        import cas_table_parser as ctp
        import cas_reconcile as rec
    except Exception as e:  # pragma: no cover
        audit['reason'] = f'import_failed:{type(e).__name__}'
        return cas_days, audit

    # cas_pdf_bytes kann EIN PDF (bytes) ODER eine Liste von Monats-PDFs sein.
    blobs = cas_pdf_bytes if isinstance(cas_pdf_bytes, (list, tuple)) else [cas_pdf_bytes]
    det_days_all = []
    parsed_ok = 0
    for blob in blobs:
        if not blob:
            continue
        try:
            d = ctp.parse_cas_pdf(blob)
        except Exception:
            continue
        if d.get('confidence') == 'none':
            continue
        det_days_all.extend(d.get('days') or [])
        parsed_ok += 1
    if parsed_ok == 0 or not det_days_all:
        audit['reason'] = 'layout_not_recognized'
        return cas_days, audit
    det = {'days': det_days_all, 'confidence': 'high'}
    audit['parser_files_ok'] = parsed_ok

    try:
        result = rec.reconcile_days(det.get('days') or [], cas_days, homebase)
    except Exception as e:
        audit['reason'] = f'reconcile_failed:{type(e).__name__}'
        return cas_days, audit

    audit.update({
        'applied': True,
        'reason': 'ok',
        'parser_confidence': det.get('confidence'),
        'parser_year': det.get('year'),
        'parser_homebase': det.get('homebase'),
        'corrections_count': result.get('corrections_count', 0),
        'corrections_by_date': result.get('corrections_by_date', {}),
        'det_only_dates': result.get('det_only_dates', []),
    })
    return result.get('days', cas_days), audit
