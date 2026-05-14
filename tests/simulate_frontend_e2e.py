"""End-to-end Frontend-Simulator — findet runtime-only Bugs durch Re-Implementation
der JS-Logik gegen echtes Backend.

User-Request 2026-05-14 nach mehreren übersehenen Bugs: „alles simulieren".
Vorher waren Audits code-reading-only → runtime-Bugs (default-values, race-conditions,
state-bleed) blieben unentdeckt. Dieser Simulator führt die Frontend-Logik aus.

Was simuliert wird:
- _autoResume flow (localStorage token → fetch → render)
- _recallSubmit flow (typing token → fetch → render)
- deriveUiState pro state
- canShowPdfDownload
- DOM-Mutation-Sequence (welche Elements werden display:block/none gesetzt)
- Lock-Indicator-Visibility tracking
"""
import urllib.request
import json
import re
import sys


BACKEND_URL = 'https://aerotax-backend-443401186607.europe-west3.run.app'
SITE_HTML = '/Users/miguelschumann/Desktop/site/index.html'


def fetch_session(token):
    """Wie window.fetch('/api/session/<token>')"""
    req = urllib.request.Request(f'{BACKEND_URL}/api/session/{token}')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code
    except Exception as e:
        return {'error': str(e)}, 0


# ─── JS-Logik in Python re-implementiert ─────────────────────────────────────

def can_show_pdf_download(api_state):
    """Re-impl von window.canShowPdfDownload (index.html ~Z. 1592+)."""
    if not isinstance(api_state, dict):
        return (False, 'not dict')
    if api_state.get('_isDemo') == True and not api_state.get('job_id'):
        return (False, '_isDemo+no_job_id')
    if api_state.get('fetch_error') == True:
        return (False, 'fetch_error')
    if api_state.get('result_stale') == True:
        return (False, 'result_stale')
    dh = api_state.get('document_health')
    if isinstance(dh, dict) and dh.get('status') == 'red':
        return (False, 'doc_health red')
    if dh == 'red':
        return (False, 'doc_health red string')
    if api_state.get('canonical_state') != 'done':
        return (False, f'canonical_state={api_state.get("canonical_state")}')
    if api_state.get('pdf_allowed') == False:
        return (False, 'pdf_allowed=False')
    ri = api_state.get('review_items') or api_state.get('_review_items') or []
    if isinstance(ri, list):
        pending = [x for x in ri if isinstance(x, dict) and x.get('status') == 'pending']
        if pending:
            return (False, f'{len(pending)} pending review_items')
    if not api_state.get('download_url'):
        return (False, 'no download_url')
    return (True, 'ok')


def derive_ui_state(api_state):
    """Re-impl von window.deriveUiState (index.html ~Z. 1690+).
    Returns dict {banner_title, show_pdf_download, show_pdf_locked, pdf_locked_reason, status_kind}.
    """
    s = api_state or {}
    cs = s.get('canonical_state') or 'unknown'
    pdf_ok, _ = can_show_pdf_download(s)
    fetch_err = s.get('fetch_error') == True
    status_kind = 'fetch_error' if fetch_err else cs

    out = {
        'status_kind': status_kind,
        'canonical_state': cs,
        'banner_title': '',
        'show_pdf_download': pdf_ok,
        'show_pdf_locked': not pdf_ok,
        'pdf_locked_reason': '',
        'show_retry': False,
        'show_support': False,
        'show_refresh_status': False,
        'chat_mode': 'gated',
    }
    if fetch_err:
        out.update(banner_title='Verbindung kurz unterbrochen',
                   pdf_locked_reason='Status nicht erreichbar',
                   show_refresh_status=True, show_support=True)
        return out
    if cs in ('processing', 'queued', 'pending'):
        out.update(banner_title='Auswertung läuft', show_refresh_status=True,
                   pdf_locked_reason='Auswertung noch nicht abgeschlossen', chat_mode='processing')
        return out
    if cs == 'needs_review':
        out.update(banner_title='Auswertung vorbereitet — kurze Klärung nötig',
                   show_pdf_locked=True, pdf_locked_reason='Offene Punkte — bitte im Chat klären',
                   chat_mode='needs_review', show_support=True)
        return out
    if cs == 'done':
        out.update(banner_title='Auswertung fertig', show_pdf_download=pdf_ok,
                   show_pdf_locked=not pdf_ok, chat_mode='done')
        if not pdf_ok:
            out['pdf_locked_reason'] = 'PDF wird vorbereitet'
        return out
    if cs == 'failed_retryable':
        out.update(banner_title='Auswertung unterbrochen', show_retry=True, show_support=True,
                   pdf_locked_reason='Auswertung unterbrochen', chat_mode='failed_retryable')
        return out
    if cs == 'failed_support':
        out.update(banner_title='Auswertung konnte nicht sicher abgeschlossen werden',
                   show_support=True, show_retry=False,
                   pdf_locked_reason='Berechnung nicht vollständig geprüft', chat_mode='failed_support')
        return out
    if cs == 'expired':
        out.update(banner_title='Code abgelaufen', pdf_locked_reason='Code abgelaufen', chat_mode='expired')
        return out
    if cs == 'deleted':
        out.update(banner_title='Auswertung gelöscht', pdf_locked_reason='Auswertung gelöscht', chat_mode='deleted')
        return out
    # unknown / fallback
    out.update(banner_title='Status wird geprüft', show_refresh_status=True,
               pdf_locked_reason='Status unklar')
    return out


def simulate_auto_resume_render_data(j):
    """Simuliert was _autoResume an window.render() übergibt.
    Re-impl von index.html ~Z. 6420+ (initial branch).
    """
    rd = j.get('result_data') or {}
    d = dict(rd)  # Spread rd
    # Then explicit fields override
    d['download_url'] = j.get('download_url')
    d['notes'] = j.get('notes') or []
    d['canonical_state'] = j.get('canonical_state')
    d['reason_code'] = j.get('reason_code')
    d['pdf_allowed'] = j.get('pdf_allowed')
    d['result_stale'] = j.get('result_stale')
    d['document_health'] = j.get('document_health')
    # JS: rd._review_items || j.review_items  → [] is truthy in JS!
    # Python equivalent: prefer rd._review_items if it's a list (even empty)
    rd_ri = rd.get('_review_items')
    if isinstance(rd_ri, list):
        d['_review_items'] = rd_ri
    else:
        d['_review_items'] = j.get('review_items')
    d['user_title'] = j.get('user_title')
    d['user_message'] = j.get('user_message')
    d['next_actions'] = j.get('next_actions')
    return d


def simulate_recall_render_data(j):
    """Simuliert was _recallSubmit an window.render() übergibt (Z. 7950+)."""
    rd = j.get('result_data') or {}
    d = dict(rd)
    d['canonical_state'] = j.get('canonical_state')
    d['reason_code'] = j.get('reason_code')
    d['pdf_allowed'] = j.get('pdf_allowed')
    d['download_url'] = j.get('download_url')
    d['notes'] = j.get('notes') or []
    d['job_id'] = j.get('job_id')
    return d


# ─── Static-Audit: alle pdf-locked-indicator-Mutationen finden ───────────────

def find_lock_indicator_mutations(html_path):
    """Sucht ALLE Stellen die pdf-locked-indicator's display setzen."""
    src = open(html_path).read()
    findings = []
    # Direkte Element-Mutations
    for m in re.finditer(r"document\.getElementById\(['\"]pdf-locked-indicator['\"]\)", src):
        line_num = src[:m.start()].count('\n') + 1
        # Grab next 5 lines as context
        end_line = line_num + 5
        lines = src.split('\n')
        ctx = '\n'.join(lines[line_num-1:end_line])
        findings.append((line_num, ctx[:200]))
    # CSS-Klassen-Mutations
    for m in re.finditer(r"lockHost\.style\.display", src):
        line_num = src[:m.start()].count('\n') + 1
        lines = src.split('\n')
        ctx = '\n'.join(lines[line_num-1:line_num+2])
        findings.append((line_num, '[via lockHost] ' + ctx[:200]))
    return findings


# ─── Main Simulation ────────────────────────────────────────────────────────

TOKENS = {
    'done': 'AT-5E7C27E28BD0DB8E',
    'needs_review': 'AT-6FB9EDE4C51F428E',
    'fake': 'AT-COMPLETELY-FAKE-XXX',
}


def main():
    print('═══ End-to-End Frontend Simulator ═══\n')

    for label, token in TOKENS.items():
        print(f'--- Token: {label} ({token}) ---')
        j, http_status = fetch_session(token)
        print(f'  Backend HTTP: {http_status}')

        if http_status not in (200, 404):
            print(f'  ❌ unexpected status {http_status}')
            continue

        # Top-level state
        cs = j.get('canonical_state')
        pdf_a = j.get('pdf_allowed')
        dl = j.get('download_url')
        print(f'  Backend cs={cs}, pdf_allowed={pdf_a}, download_url={"set" if dl else "null"}')

        # Simulate Auto-Resume render-data
        d_ar = simulate_auto_resume_render_data(j)
        ui_ar = derive_ui_state(d_ar)
        can_pdf_ar, why_ar = can_show_pdf_download(d_ar)
        print(f'  _autoResume → render(d):')
        print(f'    canShowPdfDownload={can_pdf_ar} ({why_ar})')
        print(f'    show_pdf_locked={ui_ar["show_pdf_locked"]}, reason="{ui_ar["pdf_locked_reason"]}"')

        # Simulate Recall render-data
        d_rc = simulate_recall_render_data(j)
        ui_rc = derive_ui_state(d_rc)
        can_pdf_rc, why_rc = can_show_pdf_download(d_rc)
        print(f'  _recallSubmit → render(d):')
        print(f'    canShowPdfDownload={can_pdf_rc} ({why_rc})')
        print(f'    show_pdf_locked={ui_rc["show_pdf_locked"]}, reason="{ui_rc["pdf_locked_reason"]}"')

        # Konsistenz-Check
        if can_pdf_ar != can_pdf_rc:
            print(f'  ⚠️ INCONSISTENT: AutoResume und Recall liefern unterschiedliche PDF-Status!')

        print()

    print('═══ Static-Audit: ALLE pdf-locked-indicator Mutationen ═══\n')
    mutations = find_lock_indicator_mutations(SITE_HTML)
    for line_num, ctx in mutations:
        print(f'Line {line_num}:')
        print(f'  {ctx[:300]}')
        print()


if __name__ == '__main__':
    main()
