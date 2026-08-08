"""Kalender-Logik-Sweep: alle Freunde x letzte 7 + naechste 3 Tage.
Laeuft IM aerotax-backend-Container (docker exec). Gibt Verstoesse auf stdout aus.
R1-R4 + R6: echte Fehler; R5: Hinweise (Datenluecken, kein Fehler).
Exit-Code immer 0 (Cron-Rauschen vermeiden).

R2-NACHZUG 2026-08-09 (Owner-Befund): die alte R2 hiess „zukunft-mit-ist" und
meldete JEDE est_dep/est_arr an einem Flug, dessen Abflug > 1 h in der Zukunft
liegt. Das ist falsch — Verspaetungen werden voellig legitim im Voraus
angekuendigt. BELEG aus der Produktion (`airport_delay_obs`, gelesen
2026-08-08T20:00Z): Zeile `CGN#ARR EW593`, Verkehrstag 2026-08-09,
sched 01:15, esti 2026-08-09T02:37 (+82 min), `esti_changed_at`
2026-08-08T18:20:24Z — die Schaetzung stand rund 7 h VOR dem Ereignis in der
Datenbank. Die alte Regel haette sie als Verstoss gemeldet.
Ein Alarm, der staendig falsch anschlaegt, wird ignoriert; dann geht der
echte unter.

WAS R2 SCHUETZEN SOLL: eine MESSUNG an einem Flug, der noch nicht stattgefunden
hat. Eine angekuendigte SCHAETZUNG ist etwas anderes als eine gemessene
IST-Zeit — und die Daten geben diese Unterscheidung her:

  • `arr_measured` (bool, gesetzt in app._enrich_leg_delays): True NUR, wenn
    eine Ankunftszeit MIT terminalem Status UND plausiblem `esti_changed_at`
    belegt ist. Genau das ist „gemessen". Auf der ABFLUG-Seite existiert kein
    Gegenstueck (kein `dep_measured`) — das ist die bekannte Schwaeche, s.u.
  • `status` — terminale/airborne Meldungen an einem noch nicht gestarteten
    Flug sind per Definition unmoeglich.
  • Physik: eine Schaetzung zeigt in die ZUKUNFT. Eine „Ist"-Zeit, die bereits
    VERGANGEN ist, waehrend der Flug erst noch abfliegt, kann nur die Instanz
    eines anderen Tages sein (genau der Fall LH757 BOM->FRA am 08.08.2026).

SCHWAECHE, ehrlich benannt: fuer die Abflug-Seite gibt es kein `dep_measured`
im Sektor-Payload — `est_dep_iso` traegt Prognose UND Ist im selben Feld (das
Board fuellt seine `esti`-Spalte vor dem Abflug mit der Vorhersage und danach
mit der Messung). Deshalb ist die Abflug-Seite hier nur ueber Physik
abgesichert (R2c/R6), nicht ueber ein Herkunfts-Flag. Ein Board, das eine
verfruehte Prognose meldet (est_dep vor NOW an einem Flug, der erst spaeter
abfliegt), erzeugt einen R2c-Fehlalarm — das ist bewusst in Kauf genommen,
weil dieselbe Signatur den echten 24-h-Nachbarn traegt.

R6 (neu, 2026-08-09): das Spiegelbild von R4 fuer die ABFLUG-Seite, und
bewusst OHNE Zukunfts-Bedingung. Der 24-h-Nachbar-Fehler trifft ueberwiegend
VERGANGENE Legs (gemessen am 08.08.2026: 40 gespeicherte Sektoren mit einer um
~24 h verschobenen est_dep bei korrekter est_arr — u.a. LH099 MUC->FRA,
LH1801 MAD->MUC, LH488 MUC->SEA). Die alte Regelmenge sah davon KEINEN
einzigen, weil R2 nur in die Zukunft und R4 nur auf die Ankunfts-Seite schaute.
"""
import os, sys, json, urllib.request
from datetime import datetime, timedelta, timezone

# ═══ PURE REGEL-SCHICHT ══════════════════════════════════════════════════════
# Ohne I/O, ohne Env, importierbar — damit die Regeln des Waechters GETESTET
# werden koennen (tests/test_sweep_kalender_rules.py). Bis 2026-08-09 lag das
# Skript nur auf dem Server, war in keinem Repo und in keinem Test; eine
# Aenderung daran war unwiederbringlich.

LANDED   = ('landed', 'gelandet', 'arrived')
AIRBORNE = ('airborne', 'abgeflogen', 'departed', 'gestartet', 'en route', 'im flug')

# ── Physik-Schranken (Stunden), 1:1 dieselben Zahlen wie im Backend ──────────
# EARLY: kein Flug geht 6 h VOR Plan raus / landet 6 h vor Plan
#        (app._DEP_EARLY_MARGIN_H, app._OVERNIGHT_ARR_MARGIN_H).
# LATE:  jenseits davon ist es keine Schaetzung mehr, sondern eine Umplanung
#        (app._DEP_LATE_MARGIN_H). Dazwischen bleibt JEDE reale Verspaetung
#        unbeanstandet — auch eine sehr grosse.
EARLY_MARGIN_H = 6
LATE_MARGIN_H  = 20
# Wie weit muss der Abflug in der Zukunft liegen, damit R2 ueberhaupt urteilt?
# Schuetzt die Sekunden um den Abflug herum (Uhren-Versatz Scraper/Tafel,
# frueher Pushback) vor Fehlalarmen.
FUTURE_GUARD_H = 1


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


def pruefe_sektor(s, tag, now):
    """Alle Invarianten EINES Sektors. Rein, ohne I/O, wirft nie.

    Gibt ``(verstoesse, hinweise)`` zurueck — zwei Listen von Strings.
    `s` ist ein `ical_sectors`-Eintrag der friend-roster-Antwort, `tag` das
    Praefix fuer die Meldung, `now` der Messzeitpunkt (aware, UTC).
    """
    viol, hinweis = [], []
    st    = (s.get('status') or '').lower()
    dep_t = parse_iso(s.get('dep_iso') or s.get('dep_utc') or s.get('dep'))
    arr_t = parse_iso(s.get('arr_iso') or s.get('arr_utc'))
    ed    = parse_iso(s.get('est_dep_iso'))
    ea    = parse_iso(s.get('est_arr_iso'))

    # R1: Plan-Ankunft >6h vorbei → Status muss landed sein
    if arr_t and arr_t < now - timedelta(hours=6):
        if any(k in st for k in AIRBORNE):
            viol.append(f'R1 stale-airborne: {tag} status={st!r}')
        elif st in ('', 'geplant', 'scheduled', 'grounded', 'boarding') and (ea or ed):
            viol.append(f'R1 stale-status: {tag} status={st!r} trotz est vorhanden')

    # ── R2: an einem Flug, der noch nicht stattgefunden hat, darf keine
    # MESSUNG haengen. Eine angekuendigte Verspaetung (est in der
    # Zukunft, hinter dem Plan) ist ausdruecklich KEIN Verstoss.
    if dep_t and dep_t > now + timedelta(hours=FUTURE_GUARD_H):
        # R2a — das Herkunfts-Flag selbst. `arr_measured=True` heisst
        # „terminaler Status + Zeitstempel nach der Landung": eine
        # belegte Messung. An einem Flug, der noch nicht weg ist, kann
        # es die nicht geben. Das ist die schaerfste Aussage, die die
        # Daten hergeben.
        if s.get('arr_measured') is True:
            viol.append(
                f'R2a zukunft-gemessen: {tag} arr_measured=True '
                f'est_arr={s.get("est_arr_iso")}'
            )
        # R2b — der Fall, fuer den R2 gebaut wurde: „gelandet" (oder
        # „in der Luft") an einem Flug in der Zukunft. Bleibt scharf.
        if any(k in st for k in LANDED):
            viol.append(f'R2b zukunft-landed: {tag} status={st!r}')
        elif any(k in st for k in AIRBORNE):
            viol.append(f'R2b zukunft-airborne: {tag} status={st!r}')
        # R2c — Physik statt Schwellwert: eine SCHAETZUNG zeigt nach
        # vorn. Ein est-Zeitpunkt, der schon vorbei ist, waehrend der
        # Flug erst abfliegt, ist keine Vorhersage, sondern die Instanz
        # eines anderen Tages (LH757 BOM->FRA, 08.08.2026: est_dep
        # 2026-08-07T21:41Z an einem Leg mit Abflug 2026-08-08T21:10Z).
        for _lbl, _t in (('est_dep', ed), ('est_arr', ea)):
            if _t and _t < now:
                viol.append(
                    f'R2c zukunft-ist-vergangen: {tag} '
                    f'{_lbl}={_t.isoformat()} liegt VOR jetzt, '
                    f'Abflug erst {dep_t.isoformat()}'
                )

    # R3: est_arr physikalisch vor est_dep
    if ed and ea and ea < ed:
        viol.append(
            f'R3 arr-vor-dep: {tag} {s.get("est_dep_iso")} > {s.get("est_arr_iso")}'
        )

    # R4: est_arr ausserhalb des Physik-Fensters um die Plan-Ankunft
    # → Wrong-Day-Leiche. Frueher nur die EARLY-Seite; die 24-h-Nachbarn
    # traten am 08.08.2026 nachweislich in BEIDE Richtungen auf
    # (LH495 YYZ->MUC: est_arr +26,95 h).
    if ea and arr_t:
        if (arr_t - ea) > timedelta(hours=EARLY_MARGIN_H):
            viol.append(
                f'R4 wrongday-est-arr-frueh: {tag} '
                f'est_arr={s.get("est_arr_iso")} sched_arr={s.get("arr_iso")}'
            )
        elif (ea - arr_t) > timedelta(hours=LATE_MARGIN_H):
            viol.append(
                f'R4 wrongday-est-arr-spaet: {tag} '
                f'est_arr={s.get("est_arr_iso")} sched_arr={s.get("arr_iso")}'
            )

    # R6: est_dep ausserhalb des Physik-Fensters um den Plan-Abflug.
    # Spiegelbild zu R4 — und BEWUSST ohne Zukunfts-Bedingung: der
    # 24-h-Nachbar traf ueberwiegend bereits geflogene Legs, die R2 nie
    # ansieht. Angekuendigte wie eingetretene Verspaetungen (0…+20 h)
    # bleiben unbeanstandet.
    if ed and dep_t:
        if (dep_t - ed) > timedelta(hours=EARLY_MARGIN_H):
            viol.append(
                f'R6 wrongday-est-dep-frueh: {tag} '
                f'est_dep={s.get("est_dep_iso")} sched_dep={s.get("dep_iso")}'
            )
        elif (ed - dep_t) > timedelta(hours=LATE_MARGIN_H):
            viol.append(
                f'R6 wrongday-est-dep-spaet: {tag} '
                f'est_dep={s.get("est_dep_iso")} sched_dep={s.get("dep_iso")}'
            )

    # R5: Vergangenheits-Leg (>24h) ohne est, aber status=landed → Hinweis
    if (arr_t and arr_t < now - timedelta(hours=24)
            and not ea and not ed
            and any(k in st for k in LANDED)):
        hinweis.append(f'R5 hinweis-keine-ist: {tag}')

    return viol, hinweis


# ═══ I/O-SCHICHT ═════════════════════════════════════════════════════════════
SB = KEY = None
NOW = datetime.now(timezone.utc)

def sb(p):
    r = urllib.request.Request(
        SB + p,
        headers={'apikey': KEY, 'Authorization': 'Bearer ' + KEY,
                 'User-Agent': 'AeroX-Sweep/2.0'}
    )
    return json.load(urllib.request.urlopen(r, timeout=30))

def api(p, bearer):
    r = urllib.request.Request(
        'http://127.0.0.1:8080' + p,
        headers={'Authorization': 'Bearer ' + bearer,
                 'User-Agent': 'AeroX-Sweep/2.0'}
    )
    return json.load(urllib.request.urlopen(r, timeout=120))

def main():
    global SB, KEY
    try:
        SB  = os.environ['SUPABASE_URL']
        KEY = os.environ['SUPABASE_SERVICE_KEY']
    except KeyError as e:
        print(f'FATAL: Env-Variable {e} fehlt — laeuft der Sweep im richtigen Container?')
        return 0

    # ── Nutzer-Token holen ──────────────────────────────────────────────────
    try:
        tok_row = sb('/rest/v1/auth_users?email=eq.miguel.schumann%40icloud.com&select=token')
        if not tok_row:
            print('FATAL: miguel.schumann@icloud.com nicht in auth_users gefunden')
            return 0
        tok = tok_row[0]['token']
    except Exception as ex:
        print(f'FATAL: auth_users-Abfrage fehlgeschlagen: {ex}')
        return 0

    # ── Freundes-Kanten laden ───────────────────────────────────────────────
    try:
        edges = sb(f'/rest/v1/user_friends?owner_token=eq.{tok}'
                   f'&select=friend_token&status=eq.accepted')
        if not edges:
            edges = sb(f'/rest/v1/user_friends?owner_token=eq.{tok}&select=friend_token')
    except Exception as ex:
        print(f'FATAL: user_friends-Abfrage fehlgeschlagen: {ex}')
        return 0

    window = {(NOW.date() + timedelta(days=o)).isoformat() for o in range(-7, 4)}

    viol_r1r4 = []  # echte Fehler
    viol_r5   = []  # Hinweise

    for e in edges:
        ft = e['friend_token']
        try:
            ro = api(f'/api/user/friend-roster/{tok}/{ft}', tok)
        except Exception as ex:
            viol_r1r4.append(f'R1 friend-roster FEHLER: {ft[:9]}… → {str(ex)[:80]}')
            continue
        seq = (ro if isinstance(ro, list)
               else next((ro[k] for k in ('days', 'data', 'roster')
                          if isinstance(ro.get(k), list)), []))
        for d in seq:
            dat = d.get('datum') or d.get('date') or ''
            if dat not in window:
                continue
            for s in (d.get('ical_sectors') or []):
                fl  = s.get('flight') or '?'
                frm = s.get('from')   or s.get('dep') or '?'
                to  = s.get('to')     or s.get('arr') or '?'
                tag = f'{ft[:9]}… {dat} {fl} {frm}->{to}'
                _v, _h = pruefe_sektor(s, tag, NOW)
                viol_r1r4.extend(_v)
                viol_r5.extend(_h)

    # ── Ausgabe ─────────────────────────────────────────────────────────────
    print(f'Sweep {NOW.isoformat()[:16]}Z — {len(edges)} Freunde, '
          f'Fenster {min(window)}..{max(window)}')

    if not viol_r1r4:
        print('R1-R4+R6: ALLES SAUBER — 0 Verstoesse')
    else:
        print(f'R1-R4+R6: {len(viol_r1r4)} Verstoesse:')
        for v in viol_r1r4:
            print(' ', v)

    print(f'R5-Hinweise: {len(viol_r5)}')
    for v in viol_r5:
        print(' ', v)

    # ── Maschinenlesbare Zusammenfassung fuer wrapper.sh ────────────────────
    # NAME UNVERAENDERT: sweep_wrapper.sh greppt exakt diesen Schluessel
    # (`SWEEP_VIOLATIONS_R1R4=`) — umbenennen wuerde den Mail-Versand still
    # auf den Sentinel „ERR" schalten.
    print(f'SWEEP_VIOLATIONS_R1R4={len(viol_r1r4)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
