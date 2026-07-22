#!/usr/bin/env python3
"""LH-MQTT-Daemon — dünner Broker-Client (eigener Compose-Service, 2026-07-22).

Hält die eine MQTT-Verbindung zum Lufthansa-Akamai-Broker und ist bewusst
DUMM: Topic-Liste kommt vom Backend (`GET /api/internal/lh-mqtt/topics`),
jedes empfangene Event geht roh zurück ans Backend
(`POST /api/internal/lh-mqtt/event`) — dort leben User-Mapping, Push-Texte
und LH-Fakten-Refresh. So bleibt dieser Prozess ohne Supabase-, Flask- und
APNs-Abhängigkeit und die ganze Logik ist offline testbar.

Broker (live verifiziert 2026-07-22): lhgopenapi.lufthansa.com:8883, TLS 1.2,
Username = clientID, Passwort = JWT aus dem Certificate-Manager
(POST api.lufthansa.com/v1/flightUpdate/credentials/JWT/<prefix>/flupSubTopic
— die LH-Doku nennt fälschlich lhgopenapi als Host; dort kommt 401
„invalid token"). JWT ist 2 Jahre gültig; wir holen trotzdem pro
Prozess-Start frisch (jeder Abruf erzeugt eine neue eindeutige clientID —
kein Kollisionsrisiko zwischen Restarts).

Env (aus /opt/aerox/env.list, geteilt mit backend+poll):
- LH_OPEN_API_KEY/SECRET (Fallback LH_KEY/SECRET) — wie Engine A
- ADSB_POLL_SECRET — Auth für die Backend-Internal-Endpoints
- LH_MQTT_BACKEND (Default http://aerotax-backend:8080 — Compose-DNS)
- LH_MQTT_REFRESH_S (Default 300) — Topic-Listen-Abgleich
Ohne LH-Keys: no-op-Schlaf (Container darf nie crash-loopen).
"""
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

_KEY = (os.environ.get('LH_OPEN_API_KEY') or os.environ.get('LH_KEY') or '').strip()
_SECRET = (os.environ.get('LH_OPEN_API_SECRET') or os.environ.get('LH_SECRET') or '').strip()
_BACKEND = (os.environ.get('LH_MQTT_BACKEND') or 'http://aerotax-backend:8080').rstrip('/')
_POLL_SECRET = (os.environ.get('ADSB_POLL_SECRET') or '').strip()
_REFRESH_S = int(os.environ.get('LH_MQTT_REFRESH_S') or 300)
_CLIENT_PREFIX = (os.environ.get('LH_MQTT_CLIENT_PREFIX') or 'aerox').strip()

_MQTT_HOST_FALLBACK = 'lhgopenapi.lufthansa.com'
_MQTT_PORT = 8883


def _log(msg):
    print(f'[lhmqtt] {msg}', flush=True)


def _http_json(url, method='GET', data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _oauth_token():
    body = urllib.parse.urlencode({
        'client_id': _KEY, 'client_secret': _SECRET,
        'grant_type': 'client_credentials'}).encode()
    d = _http_json('https://api.lufthansa.com/v1/oauth/token', 'POST', body,
                   {'Content-Type': 'application/x-www-form-urlencoded'})
    return d.get('access_token')


def fetch_mqtt_credentials():
    """(client_id, jwt, host) frisch vom Certificate Manager. Wirft bei Fehler
    (Aufrufer macht Backoff-Retry)."""
    tok = _oauth_token()
    if not tok:
        raise RuntimeError('no oauth token')
    d = _http_json('https://api.lufthansa.com/v1/flightUpdate/credentials/'
                   f'JWT/{urllib.parse.quote(_CLIENT_PREFIX)}/flupSubTopic',
                   'POST', b'', {'Authorization': 'Bearer ' + tok,
                                 'Accept': 'application/json'})
    cm = ((d.get('CertificateManagementResource') or {})
          .get('CertificateManagement') or {})
    cid, jwt = cm.get('clientID'), cm.get('javaWebToken')
    if not cid or not jwt:
        raise RuntimeError('certificate manager returned no credentials')
    return cid, jwt, (cm.get('endpoint') or _MQTT_HOST_FALLBACK)


def _backend(path, method='GET', payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'X-Poll-Secret': _POLL_SECRET}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    return _http_json(_BACKEND + path, method, data, headers, timeout=12)


def fetch_topics():
    d = _backend('/api/internal/lh-mqtt/topics')
    return set(d.get('topics') or [])


def diff_topics(current, target):
    """Pure: (subscribe, unsubscribe) als sortierte Listen."""
    return sorted(target - current), sorted(current - target)


class Daemon:
    def __init__(self):
        self.client = None
        self.subscribed = set()
        self.connected = threading.Event()
        self.msg_count = 0
        self.post_fail = 0

    # ── MQTT-Callbacks (paho v2-API) ─────────────────────────────────────
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        _log(f'connected rc={reason_code}')
        if getattr(reason_code, 'is_failure', False):
            return
        self.connected.set()
        # Nach (Re-)Connect ALLES neu subscriben — Broker-Session kann weg sein.
        topics = sorted(self.subscribed)
        self.subscribed = set()
        if topics:
            self._subscribe(topics)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        _log(f'disconnected rc={reason_code} (auto-reconnect)')
        self.connected.clear()

    def _on_message(self, client, userdata, msg):
        self.msg_count += 1
        try:
            payload = json.loads(msg.payload.decode('utf-8', 'replace'))
        except Exception:
            payload = {'raw': msg.payload.decode('utf-8', 'replace')[:500]}
        for attempt in (1, 2):
            try:
                r = _backend('/api/internal/lh-mqtt/event', 'POST',
                             {'topic': msg.topic, 'payload': payload})
                _log(f'event {msg.topic} -> kind={r.get("kind")} '
                     f'users={r.get("users")} pushed={r.get("pushed")}')
                return
            except Exception as e:
                if attempt == 2:
                    self.post_fail += 1
                    _log(f'event post FAIL {msg.topic}: {type(e).__name__}')
                else:
                    time.sleep(2)

    def _subscribe(self, topics):
        for i in range(0, len(topics), 40):
            chunk = [(t, 0) for t in topics[i:i + 40]]
            self.client.subscribe(chunk)
        self.subscribed |= set(topics)

    def _unsubscribe(self, topics):
        for i in range(0, len(topics), 40):
            self.client.unsubscribe(topics[i:i + 40])
        self.subscribed -= set(topics)

    # ── Verbindungsaufbau ────────────────────────────────────────────────
    def connect(self):
        import paho.mqtt.client as mqtt
        cid, jwt, host = fetch_mqtt_credentials()
        _log(f'credentials ok clientID={cid} broker={host}')
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                         protocol=mqtt.MQTTv311)
        cl.username_pw_set(cid, jwt)
        cl.tls_set_context(ssl.create_default_context())
        cl.on_connect = self._on_connect
        cl.on_disconnect = self._on_disconnect
        cl.on_message = self._on_message
        cl.reconnect_delay_set(min_delay=2, max_delay=120)
        cl.connect(host, _MQTT_PORT, keepalive=60)
        cl.loop_start()
        self.client = cl
        if not self.connected.wait(timeout=30):
            raise RuntimeError('CONNACK timeout')

    def refresh_topics(self):
        target = fetch_topics()
        sub, unsub = diff_topics(self.subscribed, target)
        if sub:
            self._subscribe(sub)
        if unsub:
            self._unsubscribe(unsub)
        if sub or unsub:
            _log(f'topics: +{len(sub)} -{len(unsub)} = {len(self.subscribed)}')

    def run(self):
        backoff = 5
        while True:
            try:
                self.connect()
                backoff = 5
                last_beat = 0.0
                while True:
                    try:
                        self.refresh_topics()
                    except Exception as e:
                        _log(f'topic refresh fail: {type(e).__name__}')
                    if time.time() - last_beat > 600:
                        _log(f'beat connected={self.connected.is_set()} '
                             f'topics={len(self.subscribed)} '
                             f'msgs={self.msg_count} post_fail={self.post_fail}')
                        last_beat = time.time()
                    # Verbindung tot und paho-Reconnect hilft nicht mehr →
                    # außen neu aufbauen (inkl. frischem JWT bei Auth-Probleme).
                    for _ in range(_REFRESH_S):
                        time.sleep(1)
                        if not self.connected.is_set():
                            break
                    if not self.connected.is_set():
                        if not self.connected.wait(timeout=180):
                            raise RuntimeError('reconnect stuck >3 min')
            except Exception as e:
                _log(f'session error: {type(e).__name__}: {e} — retry in {backoff}s')
                try:
                    if self.client:
                        self.client.loop_stop()
                        self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                self.connected.clear()
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


def main():
    if not _KEY or not _SECRET:
        _log('LH keys not configured — sleeping (no-op)')
        while True:
            time.sleep(3600)
    if not _POLL_SECRET:
        _log('WARN: ADSB_POLL_SECRET empty — backend calls only work on localhost')
    Daemon().run()


if __name__ == '__main__':
    main()
