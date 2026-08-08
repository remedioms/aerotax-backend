# AeroX LH-Quota-Tagesreport — wird per `docker exec -i aerotax-backend python3 -`
# im Backend-Container ausgefuehrt (dort liegen die Supabase-Creds).
# Zeile 1 des stdout = Subject, Rest = Body (Konvention wie signup_digest_report).
#
# WARUM: `lh_open_api._HOUR_BUDGET`/`_hour_count` zaehlen PRO PROZESS (3
# Backend-Worker :8080 + 1 Poll-Worker :8081 + MQTT-Pfad, kein gemeinsames
# Volume) — die LH-Quota gilt aber PRO KEY (1.000/h + 5/s). Erst der
# gemeinsame Zaehler in `ax_api_budget` zeigt, was der Key wirklich sieht.
import time

try:
    from blueprints.aerox_data_blueprint import lh_quota_snapshot
except Exception as e:                                    # noqa: BLE001
    print('AeroX LH-Quota: Report fehlgeschlagen (%s)' % type(e).__name__)
    raise SystemExit(0)

HOURS = 24
QUOTA = 1000

snap = lh_quota_snapshot(HOURS)
rows = snap.get('hours') or []

FAMS = ('lhopen', 'lhopen_denied', 'lhopen_skip', 'lhfo')
tot = {f: 0 for f in FAMS}
peak = {f: (0, '-') for f in FAMS}
callers = {f: {} for f in FAMS}
over = []
for r in rows:
    st = r.get('hour_utc')
    for fam in FAMS:
        blk = (r.get('keys') or {}).get(fam) or {}
        n = int(blk.get('total') or 0)
        tot[fam] += n
        if n > peak[fam][0]:
            peak[fam] = (n, st)
        if n >= QUOTA:
            over.append((st, fam, n))
        for c, v in (blk.get('callers') or {}).items():
            callers[fam][c] = callers[fam].get(c, 0) + int(v or 0)

worst = max(peak['lhopen'][0], peak['lhfo'][0])
flag = '⚠️ ' if (over or worst > QUOTA * 0.8 or tot['lhopen_denied']) else ''
print('%sAeroX LH-Quota 24 h — Open %d (+%d abgewiesen) / FlightOps %d '
      '(Peak %d/h)' % (flag, tot['lhopen'], tot['lhopen_denied'],
                       tot['lhfo'], worst))
print('')
print('Stand %s UTC · Kontingent %d Calls/h PRO KEY (Open API und FlightOps'
      ' sind GETRENNTE Keys).' % (time.strftime('%Y-%m-%d %H:%M', time.gmtime()),
                                  QUOTA))
print('')
print('WICHTIG: „abgewiesen" = Calls, die der EIGENE Prozess-Throttle')
print('(_HOUR_BUDGET=220 pro Prozess) gestoppt hat. Gewollt = gesendet +')
print('abgewiesen. Steht dort eine grosse Zahl, ist der BEDARF das Problem,')
print('nicht das LH-Kontingent.')
print('')
print('„gar nicht erst gefragt" (seit 30.07.) ist das GEGENTEIL und eine gute')
print('Zahl: die Antwort war schon bekannt (shared_hit, final) oder die Frage')
print('war sinnlos (horizon, dedup_hour, gate_closed). Nicht mit „abgewiesen"')
print('verrechnen — sonst ist die Kennzahl unbrauchbar.')
print('')
for fam, label in (('lhopen', 'LH Open API (gesendet)'),
                   ('lhopen_denied', 'LH Open API (ABGEWIESEN vom eigenen Throttle)'),
                   ('lhopen_skip', 'LH Open API (GAR NICHT ERST GEFRAGT)'),
                   ('lhfo', 'LH FlightOps (eigener Key)')):
    print('== %s ==' % label)
    print('  Summe 24 h : %d' % tot[fam])
    print('  Spitze/h   : %d  (%s UTC)' % peak[fam])
    top = sorted(callers[fam].items(), key=lambda kv: -kv[1])[:10]
    if top:
        print('  Top-Verbraucher (24 h):')
        for c, v in top:
            print('    %-18s %6d' % (c, v))
    else:
        print('  (keine Aufrufer-Daten — Zaehler laeuft erst seit dem Deploy)')
    print('')

if over:
    print('UEBER DEM KONTINGENT:')
    for st, fam, n in over:
        print('  %s UTC  %s  %d' % (st, fam, n))
    print('')

print('Stunden-Verlauf (UTC · open / flightops):')
for r in rows:
    k = r.get('keys') or {}
    print('  %s  %5d / %5d' % (r.get('hour_utc'),
                               int((k.get('lhopen') or {}).get('total') or 0),
                               int((k.get('lhfo') or {}).get('total') or 0)))
print('')
print('Live-Abruf: GET /api/ax/lh-quota?hours=6  (Header X-Admin-Token)')
