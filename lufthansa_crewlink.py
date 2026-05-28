"""Lufthansa CrewLink Scraper (Option A).

⚠ HINWEIS: Web-Scraping verletzt höchstwahrscheinlich die Lufthansa-AGB.
Nutzung auf eigenes Risiko. Lufthansa kann Accounts/IP-Adressen sperren.

Encrypted Credentials Storage:
  Master-Key kommt aus env var AEROTAX_CRYPTO_KEY (base64 32 bytes).
  Wenn nicht gesetzt, generieren wir einen volatilen Key — Credentials gehen verloren beim Restart.

Selektoren-Skeleton:
  Die DOM-Selektoren in scrape_roster() sind Platzhalter. Sobald LH-CrewLink
  inspiziert wurde, müssen sie an die echten HTML-Strukturen angepasst werden.
"""
import os
import re
import json
import base64
import secrets
from datetime import datetime, timedelta
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # cryptography optional bei Tests
    AESGCM = None  # type: ignore

try:
    import requests
except Exception:
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None  # type: ignore


# ── Encryption ────────────────────────────────────────────────────────

_MEMORY_KEY: Optional[bytes] = None


def _get_master_key() -> bytes:
    """Holt den Master-Key aus env oder generiert einen volatilen."""
    global _MEMORY_KEY
    env_key = os.environ.get('AEROTAX_CRYPTO_KEY', '').strip()
    if env_key:
        try:
            k = base64.b64decode(env_key)
            if len(k) == 32:
                return k
        except Exception:
            pass
    if _MEMORY_KEY is None:
        _MEMORY_KEY = secrets.token_bytes(32)
        print('[crewlink] WARN: AEROTAX_CRYPTO_KEY nicht gesetzt — volatile in-memory key. '
              'Credentials werden beim Restart unbrauchbar.')
    return _MEMORY_KEY


def encrypt_credentials(email: str, password: str) -> dict:
    """Verschlüsselt email+password mit AES-256-GCM."""
    if AESGCM is None:
        raise RuntimeError('cryptography library nicht installiert')
    key = _get_master_key()
    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)
    payload = json.dumps({'email': email, 'password': password}).encode('utf-8')
    ct = aes.encrypt(nonce, payload, None)
    return {
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ciphertext': base64.b64encode(ct).decode('ascii'),
        'created_at': datetime.now().isoformat(),
    }


def decrypt_credentials(blob: dict) -> Optional[dict]:
    if AESGCM is None:
        return None
    try:
        key = _get_master_key()
        aes = AESGCM(key)
        nonce = base64.b64decode(blob['nonce'])
        ct = base64.b64decode(blob['ciphertext'])
        pt = aes.decrypt(nonce, ct, None)
        return json.loads(pt.decode('utf-8'))
    except Exception as e:
        print(f'[crewlink] decrypt failed: {e}')
        return None


# ── Scraper ──────────────────────────────────────────────────────────

# Skeleton — must be replaced with real CrewLink endpoints.
CREWLINK_BASE = os.environ.get('AEROTAX_CREWLINK_BASE', 'https://crewlink.lhgroup.com')
LOGIN_PATH = '/login'
ROSTER_PATH = '/roster'
DEFAULT_TIMEOUT = 30


class CrewlinkScrapeResult:
    def __init__(self):
        self.ok: bool = False
        self.events: list = []
        self.error: Optional[str] = None
        self.error_code: Optional[str] = None  # 'auth_failed', '2fa_required', 'dom_changed', 'network'
        self.fetched_at: str = datetime.now().isoformat()
        self.raw_count: int = 0


def scrape_roster(email: str, password: str, days_ahead: int = 60) -> CrewlinkScrapeResult:
    """Lädt + parsed das Roster von CrewLink.

    ⚠ Skeleton — die DOM-Selektoren müssen an die echte CrewLink-Seite angepasst werden.
    """
    result = CrewlinkScrapeResult()
    if requests is None:
        result.error = 'requests nicht installiert'
        result.error_code = 'network'
        return result

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
                      '(KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
    })

    try:
        # 1. Login-Page laden für CSRF-Token (falls vorhanden)
        login_get = session.get(CREWLINK_BASE + LOGIN_PATH, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        if login_get.status_code >= 500:
            result.error = f'login page returned {login_get.status_code}'
            result.error_code = 'network'
            return result

        # CSRF aus Hidden-Input extrahieren (Spring-Security-Pattern)
        csrf_token = None
        if BeautifulSoup is not None:
            soup = BeautifulSoup(login_get.text, 'html.parser')
            csrf_input = soup.find('input', {'name': '_csrf'}) or soup.find('input', {'name': 'csrf_token'})
            if csrf_input:
                csrf_token = csrf_input.get('value')

        # 2. Login-Form posten (Spring-Security typische Felder)
        login_data = {
            'j_username': email,           # oder 'username' / 'email' — muss inspiziert werden
            'j_password': password,        # oder 'password'
        }
        if csrf_token:
            login_data['_csrf'] = csrf_token

        login_post = session.post(CREWLINK_BASE + LOGIN_PATH,
                                   data=login_data, timeout=DEFAULT_TIMEOUT,
                                   allow_redirects=True)

        # Erkenne Login-Erfolg: Redirect auf /home oder Cookie JSESSIONID
        if 'login' in login_post.url.lower() and login_post.status_code in (200, 401, 403):
            # noch auf Login-Seite → Auth failed
            if '2fa' in login_post.text.lower() or 'verification' in login_post.text.lower():
                result.error = '2-Faktor-Authentifizierung aktiv — Web-Login nicht möglich'
                result.error_code = '2fa_required'
            else:
                result.error = 'Login fehlgeschlagen — E-Mail oder Passwort falsch'
                result.error_code = 'auth_failed'
            return result

        # 3. Roster-Seite laden
        roster_resp = session.get(CREWLINK_BASE + ROSTER_PATH, timeout=DEFAULT_TIMEOUT)
        if roster_resp.status_code != 200:
            result.error = f'Roster fetch failed: HTTP {roster_resp.status_code}'
            result.error_code = 'network'
            return result

        # 4. HTML parsen — Skeleton
        if BeautifulSoup is None:
            result.error = 'BeautifulSoup nicht installiert'
            result.error_code = 'network'
            return result

        soup = BeautifulSoup(roster_resp.text, 'html.parser')

        # ⚠ Skeleton: die echten Klassen/IDs müssen inspiziert werden.
        # Hier nur ein Pattern — passe an wenn echtes DOM bekannt.
        rows = soup.select('table.roster tr.day-row') or soup.select('div.roster-day') or []

        for row in rows:
            datum = (row.get('data-date') or '').strip()
            if not datum or not re.match(r'^\d{4}-\d{2}-\d{2}$', datum):
                continue
            event_type = (row.get('data-type') or '').strip()
            code = (row.get('data-code') or row.get('data-marker') or '').strip()
            routing = (row.get('data-routing') or '').strip()
            start = (row.get('data-start') or '').strip()
            end = (row.get('data-end') or '').strip()

            result.events.append({
                'date': datum,
                'type': event_type or 'unknown',
                'code': code,
                'routing': routing,
                'start_time': start,
                'end_time': end,
            })

        result.raw_count = len(result.events)
        result.ok = True
        return result

    except requests.exceptions.Timeout:
        result.error = 'Timeout beim Verbinden mit CrewLink'
        result.error_code = 'network'
    except requests.exceptions.ConnectionError as e:
        result.error = f'Connection-Fehler: {str(e)[:100]}'
        result.error_code = 'network'
    except Exception as e:
        result.error = f'Unerwarteter Fehler: {str(e)[:200]}'
        result.error_code = 'unknown'

    return result
