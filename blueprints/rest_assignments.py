"""Die eingeteilte Pause mit der Crew DESSELBEN Legs teilen (Owner 2026-08-09).

Owner, wörtlich, mit Screenshot der Pausenrechner-Feed-Karte:

    „hier wurde noch keine pause eingeteilt wie weiß er wie lange? erstmal kein
     text text dann wenn pause eingeteilt einfach mit den zeiten? leute in der
     gleichen crew sehen es auch auf deren feed. sobald einer der purser die
     pause berechnet. sonst sehen sie es nicht"

Präzisierung derselben Runde:

    „und natürlich nur cabin crews und pursers.. da cockpit eine andere
     aufteilung macht.. die sehen deren.. auch nur der flug an dem tag"

Der lokale Teil (Zustand „eingeteilt", `RestEinteilung`, an die Flug-Instanz
gebunden) liegt in iOS `Tools/CabinRestCalculator.swift`. Dieses Modul ist der
serverseitige Weg dazwischen — und NICHTS darüber hinaus.


═══════════════════════════════════════════════════════════════════════════
 WAS ÜBER DIE LEITUNG GEHT: NUR ZEITEN
═══════════════════════════════════════════════════════════════════════════
`plan` ist ausschliesslich `[{nummer, start_min, end_min}]`. Keine Namen, keine
Personalnummern, keine Positionen einzelner Personen. Die Einteilung sagt
„Ruhezeit 1: 15:05–16:23" — nicht, WER darin liegt. `clean_ruhen` lässt nur
Ganzzahlen durch; alles andere (auch ein zusätzlicher Schlüssel) macht den
ganzen Plan ungültig, statt still mitzureisen. Das ist dieselbe Härte wie bei
`_hangout_audience_normalize` (unbekannte Keys werden verworfen, nicht
übernommen) — ein Client kann hier nichts einschmuggeln.

Der EINZIGE Personenbezug ist `author_token`: wer eingeteilt hat. Daraus löst
der Lese-Endpoint den Anzeigenamen aus dem Profil DES AUTORS auf („von X
eingeteilt"), damit eine Änderung nachvollziehbar ist (Owner-Vorgabe). Das ist
derselbe öffentliche Name, den `/api/user/search` liefert, und er geht
ausschliesslich an Leser, die das Roster-Gate unten passiert haben — also an
Leute, die mit dieser Person an diesem Tag auf demselben Leg sitzen.

**Es gibt keinen Namens-Abgleich in diesem Modul.** Identitäten werden nirgends
geraten: kein Fuzzy-Match, keine Ähnlichkeit, keine Ableitung aus Crew-Listen
(Lehre „Marco C." 2026-07-30). Wo im Projekt Crew-Identitäten aufgelöst werden,
läuft das exakt über `lh_pk_number` — hier wird gar keine fremde Identität
aufgelöst, weil der Zugriff am eigenen Token und am eigenen Roster hängt.


═══════════════════════════════════════════════════════════════════════════
 DREI GATES, ALLE FAIL-CLOSED — IM ZWEIFEL NICHTS
═══════════════════════════════════════════════════════════════════════════
1. ROSTER-BELEG (`_own_roster_has_leg`). Lesen darf nur, wer dieses Leg an
   diesem Tag im EIGENEN Dienstplan hat. Muster `_crew_shared_serve`
   (blueprints/lh_flightops.py): der Nachweis kommt aus EIGENER Quelle
   (`roster_snapshots` / `crew_flight_assignments`), nie aus einer Vermutung
   über fremde Daten. Kein Treffer, Tabelle weg, Supabase hustet ⇒ nichts.

2. BEREICH (`bereich_of_position`). Kabine sieht Kabine, Cockpit sieht
   Cockpit — der Bereich ist Teil des Primärschlüssels, und der Server
   erzwingt, dass der angefragte Bereich der EIGENEN Position entspricht.
   Ein Client kann sich also nicht in den anderen Bereich schreiben oder
   lesen. Position leer/unlesbar ⇒ nichts (bewusste Asymmetrie: „unbekannt"
   heißt nicht „Kabine").

3. DATUM. Der Schlüssel ist (flight, flight_date, bereich) — Flugnummer UND
   Datum. **Kein ±1-Tag-Fenster, nirgends.** Weder im Select noch im
   Roster-Beweis. Der geteilte Crew-Cache hat so eine Toleranz
   (`_crew_date_candidates`); hier wäre sie ein Fehler: am 02.08. hat die
   Flugnummer ohne Datum bei LH455 SFO→FRA die Vortags-Instanz erwischt, und
   in der Nacht auf den 07.08. ist eine ganze Fehlerfamilie daran
   hochgegangen, dass ein Schlüssel den Nachbartag mitfing (Cross-Date-Guard).
   `dep`/`arr` reisen als GEGENPROBE mit (gleiche Flugnummer zweimal am
   selben Tag), sind aber nicht Teil des Schlüssels, weil nicht jede
   Roster-Quelle beide Stationen führt.

Dazu, davor: Bearer == Pfad-Token (`_request_bearer_matches`, IDOR-Gate).


═══════════════════════════════════════════════════════════════════════════
 SCHREIBRECHT — DIE EINFACHSTE EHRLICHE REGEL
═══════════════════════════════════════════════════════════════════════════
Owner: „sobald einer der purser die pause berechnet".

  · KABINE: nur Purser (inkl. Purser 2). Das ist derselbe Detektor-Umfang wie
    in iOS `TariffHours.rolleVorschlag` (`PU`/`PUR`/`SEN`/`SPU`/`SCC`/`CSD`/
    `MDC`/`P1`/`P2` plus die ausgeschriebenen Formen). Ein Flugbegleiter LIEST
    nur — sonst könnte ein beliebiges Crew-Mitglied die Einteilung seines
    Pursers überschreiben, und die Karte behauptete etwas, das der Purser nie
    gesagt hat.
  · COCKPIT: jede Cockpit-Position. Der Dienstplan belegt Cockpit vs. Kabine,
    aber NICHT Kapitän vs. First Officer (die Position im Profil ist ein
    Freitextfeld, und die Crew-Liste nennt Kollegen abgekürzt). Eine
    „nur der Kapitän"-Regel wäre also geraten. Die einfachste Regel, die sich
    belegen lässt: wer im Cockpit sitzt, darf für das Cockpit einteilen.
  · UNLESBARE POSITION: kein Schreiben, kein Lesen.

Wer geschrieben hat, ist sichtbar (`author_name`) — eine Änderung ist damit
nachvollziehbar, ohne dass jemand gesperrt werden muss.


═══════════════════════════════════════════════════════════════════════════
 RÜCKWÄRTSKOMPATIBILITÄT (Owner: „habe viele User")
═══════════════════════════════════════════════════════════════════════════
Alte App-Builds kennen diese Endpoints nicht und verlieren NICHTS: die lokale
Einteilung auf dem Gerät des Einteilenden ist unverändert die Quelle der
Feed-Karte. Fehlt die Tabelle (Migration noch nicht angewendet), degradiert
dieses Modul lautlos auf „keine Einteilung" — kein Hard-Fail, kein 500.
Die Antwort-Form ist additiv; ein nicht vorhandener Eintrag ist `assignment:
null` mit HTTP 200, damit ein Leser ohne Berechtigung und ein Leg ohne
Einteilung von aussen ununterscheidbar sind (kein Informations-Leck).

Konto-Löschen: `rest_assignments` hängt in der DSGVO-Cascade in app.py
(`author_token`) — es bleiben keine Waisen zurück.
"""
import logging
import re
import time

from flask import Blueprint, jsonify, request

log = logging.getLogger('aerotax')
rest_assignments_bp = Blueprint('rest_assignments_bp', __name__)

TABLE = 'rest_assignments'
BEREICHE = ('kabine', 'cockpit')

# Deckel für den Plan. Mehr als zwölf Ruhegruppen gibt es in keiner Kabine und
# in keinem Cockpit; der Deckel ist ein Größen-Schutz, keine Fachaussage.
MAX_RUHEN = 12
# Minuten seit Mitternacht des START-Tages. Der Rechner arbeitet mit
# +1440-Offsets über Mitternacht (`CabinRestPlan.clock`), deshalb reicht das
# Fenster bis 48 h. Alles darüber ist kein Flug mehr.
MAX_MIN = 2880
# Eine Einteilung ist nach dem Flug Geschichte. Der Prune läuft beim Schreiben
# mit (kein neuer Cron, kein neuer Thread) — gleiches Fenster wie der lokale
# Store in iOS (`RestEinteilungStore.maxAlterTage`).
PRUNE_AFTER_DAYS = 3

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Ob die Tabelle da ist. None = noch nicht ausprobiert. Genau wie beim
# Crew-Cache: ein fehlendes Schema darf keine Fehlerflut erzeugen.
_table_ok = [None]


def _sb():
    """Test-Seam: Supabase-Client oder None (Lazy-Import wie in den anderen
    Blueprints — das Modul bleibt ohne app-Import ladbar)."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return None
        return getattr(_app, 'sb', None)
    except Exception:
        return None


def _bearer_ok(path_token):
    """Test-Seam um app._request_bearer_matches (IDOR-Gate)."""
    try:
        from app import _request_bearer_matches
        return _request_bearer_matches(path_token)
    except Exception:
        return False


def _table_missing(exc):
    """Fehlt die Tabelle? Dann EINMAL warnen und danach still degradieren."""
    msg = str(exc or '')
    gone = ('PGRST205' in msg or 'PGRST204' in msg
            or 'does not exist' in msg or 'not found' in msg.lower())
    if gone and _table_ok[0] is not False:
        _table_ok[0] = False
        log.warning('[rest-assign] Tabelle %s fehlt — Migration '
                    'supabase_migrations/20260809_rest_assignments.sql '
                    'nicht angewendet. Feature degradiert auf „keine '
                    'Einteilung".', TABLE)
    return gone


# ── Reine Helfer (test-bar ohne Netz) ───────────────────────────────────────

def norm_flight(raw):
    """'lh 462' → 'LH462'. Leer/unbrauchbar → None."""
    s = re.sub(r'[^A-Za-z0-9]', '', str(raw or '')).upper()[:12]
    return s or None


def norm_date(raw):
    """Striktes `YYYY-MM-DD` — sonst None. KEINE Toleranz, keine Nachbartage."""
    s = str(raw or '')[:10]
    return s if _DATE_RE.match(s) else None


def norm_iata(raw):
    """Dreibuchstabige Station oder None (leer ist erlaubt: nicht jede Quelle
    führt beide Stationen)."""
    s = re.sub(r'[^A-Za-z]', '', str(raw or '')).upper()
    return s if len(s) == 3 else None


def norm_bereich(raw):
    s = str(raw or '').strip().lower()
    return s if s in BEREICHE else None


# ── Rolle → Bereich → Schreibrecht ──────────────────────────────────────────
#
# Der Bereich kommt aus `app._hangout_role_of_position` — dem BESTEHENDEN
# serverseitigen Spiegel von iOS `CodeTranslations.isCockpitPosition`
# (Cockpit-Codes, Cockpit-Prosa, Kabinen-Codes, Kabinen-Prosa, Cockpit ZUERST).
# Kein zweiter Detektor: liefe der auseinander, sähe jemand die Einteilung der
# falschen Gruppe.
#
# Die Purser-Verfeinerung steht hier, weil es sie serverseitig noch nicht gab.
# Sie ist der Purser-Zweig aus iOS `TariffHours.rolleVorschlag` — und sie wird
# erst NACH dem Bereich ausgewertet, damit „Senior First Officer" nie über
# einen Purser-Marker läuft (dieselbe tragende Reihenfolge wie dort).

_PURSER_TOKENS = frozenset({'PU', 'PUR', 'SEN', 'SPU', 'SCC', 'CSD', 'MDC',
                            'P1', 'P2'})
_PURSER_PROSA = ('PURSER', 'MAITRE', 'CABINCHIEF', 'CHEFDECABINE')


def _fold(raw):
    """UPPER + Diakritika gefaltet — Test-Seam auf app._hangout_fold, mit
    lokalem Rückfall, damit die reinen Helfer ohne app testbar bleiben."""
    try:
        from app import _hangout_fold
        return _hangout_fold(raw)
    except Exception:
        s = (raw or '').strip()
        return s.upper()


def bereich_of_position(raw):
    """'kabine' | 'cockpit' | None. None = unbestimmbar ⇒ gar nichts.

    Delegiert an `app._hangout_role_of_position` (eine Quelle für die
    Positions-Klassifikation im Backend) und übersetzt nur die Vokabel:
    'cabin' → 'kabine'.
    """
    try:
        from app import _hangout_role_of_position
        rolle = _hangout_role_of_position(raw)
    except Exception:
        return None
    if rolle == 'cockpit':
        return 'cockpit'
    if rolle == 'cabin':
        return 'kabine'
    # ── „CREW" ist bei Lufthansa die KABINE (Produktvertrag 2026-08-02) ──
    # iOS kennt diesen Zweig in `TariffHours.rolleVorschlag` ausdrücklich: die
    # auswählbare Position „Crew" bedeutet Kabine/Flugbegleiter (Cockpit hat
    # eigene, eindeutige Werte und ist oben längst abgefangen).
    # `_hangout_role_of_position` kennt ihn NICHT — dort ist „Crew" unbekannt.
    # Ohne diesen Nachtrag sähe ein Kabinen-Profil mit genau diesem Eintrag
    # serverseitig gar nichts, obwohl die App ihm die Karte zeigt: die beiden
    # Seiten liefen auseinander, und zwar still.
    # Der Nachtrag steht HIER und nicht in `_hangout_role_of_position`, weil
    # diese Funktion auch die Hangout-Zielgruppen filtert — deren Verhalten
    # ändert dieser Auftrag nicht.
    # EXAKTER Wert, kein Teilstring: „Ground Crew" ist keine Kabine.
    if _fold(raw).replace(' ', '') == 'CREW':
        return 'kabine'
    return None


def is_purser_position(raw):
    """Purser oder Purser 2? Nur für den KABINEN-Bereich sinnvoll — der
    Aufrufer prüft den Bereich vorher (Cockpit zuerst)."""
    s = _fold(raw)
    if not s:
        return False
    tokens = set(re.split(r'[^A-Z0-9]+', s)) - {''}
    if tokens & _PURSER_TOKENS:
        return True
    kompakt = ''.join(tokens)
    return any(m in kompakt for m in _PURSER_PROSA)


def darf_einteilen(position, bereich):
    """Schreibrecht für GENAU DIESEN Bereich (siehe Banner „Schreibrecht")."""
    eigener = bereich_of_position(position)
    if not eigener or eigener != bereich:
        return False
    if bereich == 'cockpit':
        return True
    return is_purser_position(position)


# ── Der Plan: NUR ZEITEN ────────────────────────────────────────────────────

def clean_ruhen(raw):
    """`[{nummer,start_min,end_min}]` → geprüfte Kopie, oder None.

    ALLES-ODER-NICHTS. Ein einziger fremder Schlüssel, ein String, ein
    negativer Wert oder eine verkehrte Reihenfolge macht den ganzen Plan
    ungültig (400) — statt still einen Teil zu übernehmen. Ein stiller
    Teil-Übernehmer wäre genau der Weg, auf dem ein Name in eine Tabelle
    gerät, die keine Namen führen darf.
    """
    if not isinstance(raw, list) or not raw or len(raw) > MAX_RUHEN:
        return None
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return None
        if set(item) - {'nummer', 'start_min', 'end_min'}:
            return None
        try:
            nummer = item['nummer']
            start = item['start_min']
            ende = item['end_min']
        except KeyError:
            return None
        # bool ist in Python ein int — hier ist es keiner.
        for v in (nummer, start, ende):
            if not isinstance(v, int) or isinstance(v, bool):
                return None
        if nummer != i + 1:
            return None
        if not (0 <= start < ende <= MAX_MIN):
            return None
        out.append({'nummer': nummer, 'start_min': start, 'end_min': ende})
    return out


def build_plan(ruhen):
    """Speicher-Form. `clock` ist bewusst KEIN Zeitzonen-Bezeichner.

    Der Pausenrechner ist zeitzonen-frei: der Purser tippt die Briefing-Zeiten,
    also die WANDUHR, nach der an Bord dieses einen Legs gearbeitet wird. Für
    die Crew desselben Legs ist das dieselbe Uhr — genau deshalb ist Teilen
    hier überhaupt ehrlich. Eine Zonen-Angabe zu erfinden (‚Europe/Frankfurt')
    wäre ein synthetisierter Wert: die Startzeit kann aus dem Briefing der
    Abflugstation stammen, die Landung ist die Ortszeit der Ankunftsstation.
    Deshalb steht hier, was tatsächlich gilt — und die Karte zeigt den
    Ortszeit-Hinweis der Landung weiterhin selbst.
    """
    return {'v': 1, 'clock': 'wanduhr', 'ruhen': ruhen}


def plan_ruhen(plan):
    """Gespeicherter Plan → Ruhen-Liste (defensiv; nie werfen)."""
    if not isinstance(plan, dict):
        return []
    return clean_ruhen(plan.get('ruhen')) or []


# ── Gate 1: der Roster-Beleg ────────────────────────────────────────────────

def _own_roster_has_leg(token, flight, date):
    """Hat DIESER Nutzer GENAU DIESEN Flug an GENAU DIESEM Tag im EIGENEN
    Dienstplan? Wirft nie — jeder Zweifel ist ein Nein.

    Zwei Wege, beide aus EIGENER Quelle (der Nutzer hat seinen Dienstplan
    selbst hinterlegt — das ist die Tatsache, die ein Name nicht liefern kann,
    siehe `_roster_tokens_for_leg`):

      (a) `crew_flight_assignments` — der Flug-Index, den der Roster-Import
          ohnehin schreibt (`_crew_flight_ingest`). PK
          (self_token, flight_number, flight_date) ⇒ ein Punkt-Treffer.
          `opt_in` wird NICHT abgefragt: das Feld regelt, ob jemand in fremden
          LISTEN auftaucht — hier geht es um die eigene Anwesenheit, und
          Roster-Sharing ist ohnehin immer an.
      (b) `roster_snapshots` per jsonb-Containment auf der EIGENEN Zeile.
          Fällt (a) aus (Import lief noch nicht, Tabelle fehlt), trägt der
          Snapshot. `.eq('token', …)` macht daraus einen Einzelzeilen-Zugriff
          über den Primärschlüssel — dieser Aufruf braucht den GIN-Index
          `roster_snapshots_payload_gin` also NICHT (anders als der
          token-freie Scan in `_roster_tokens_for_leg`).

    KEINE Datums-Toleranz: exakt dieser Tag. Siehe Banner, Gate 3.
    """
    client = _sb()
    if client is None or not (token and flight and date):
        return False
    try:
        r = (client.table('crew_flight_assignments').select('self_token')
             .eq('self_token', token).eq('flight_number', flight)
             .eq('flight_date', date).limit(1).execute())
        if getattr(r, 'data', None):
            return True
    except Exception as e:
        log.info('[rest-assign] crew_flight_probe: %s', type(e).__name__)
    try:
        probe = {'tage': [{'datum': date, 'ical_sectors': [{'flight': flight}]}]}
        r = (client.table('roster_snapshots').select('token')
             .eq('token', token).contains('payload', probe)
             .limit(1).execute())
        return bool(getattr(r, 'data', None))
    except Exception as e:
        log.info('[rest-assign] roster_probe: %s', type(e).__name__)
        return False


# ── Profil (Position + Anzeigename) ─────────────────────────────────────────

def _profile_of(token):
    """{'position': …, 'name': …} — leer bei jedem Fehler."""
    try:
        import app as _app
        return (_app._profile_load(token) or {}).get('profile') or {}
    except Exception:
        return {}


def _author_name(token):
    """Anzeigename des Autors („von X eingeteilt"). Leer ⇒ die Zeile entfällt
    im Client; es wird NICHTS ersatzweise behauptet."""
    name = str(_profile_of(token).get('name') or '').strip()
    return name[:60] or None


# ── Speicher ────────────────────────────────────────────────────────────────

def _prune(client, today):
    """Alte Einteilungen wegräumen — beim Schreiben, best-effort, nie werfend."""
    try:
        import datetime as _dt
        d = _dt.date.fromisoformat(today) - _dt.timedelta(days=PRUNE_AFTER_DAYS)
        client.table(TABLE).delete().lt('flight_date', d.isoformat()).execute()
    except Exception as e:
        log.info('[rest-assign] prune: %s', type(e).__name__)


def _today():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


# ── Endpoints ───────────────────────────────────────────────────────────────

@rest_assignments_bp.route('/api/rest-assignment/<token>', methods=['PUT'])
def rest_assignment_put(token):
    """Einteilung schreiben/aktualisieren.

    Body: {flight, date, bereich, dep?, arr?, ruhen:[{nummer,start_min,end_min}]}
    """
    if not _bearer_ok(token):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    flight = norm_flight(body.get('flight'))
    date = norm_date(body.get('date'))
    bereich = norm_bereich(body.get('bereich'))
    if not flight:
        return jsonify({'ok': False, 'error': 'bad_flight'}), 400
    if not date:
        return jsonify({'ok': False, 'error': 'bad_date'}), 400
    if not bereich:
        return jsonify({'ok': False, 'error': 'bad_bereich'}), 400
    ruhen = clean_ruhen(body.get('ruhen'))
    if ruhen is None:
        return jsonify({'ok': False, 'error': 'bad_plan'}), 400
    # Gate 2 (Bereich) + Schreibrecht — beides aus der EIGENEN Position.
    position = _profile_of(token).get('position')
    if not darf_einteilen(position, bereich):
        return jsonify({'ok': False, 'error': 'not_allowed'}), 403
    # Gate 1 (Roster-Beleg) + Gate 3 (Datum steckt im Beleg und im Schlüssel).
    if not _own_roster_has_leg(token, flight, date):
        return jsonify({'ok': False, 'error': 'not_on_leg'}), 403
    client = _sb()
    if client is None:
        return jsonify({'ok': False, 'error': 'unavailable'}), 503
    row = {'flight': flight, 'flight_date': date, 'bereich': bereich,
           'dep': norm_iata(body.get('dep')), 'arr': norm_iata(body.get('arr')),
           'author_token': token, 'plan': build_plan(ruhen),
           'updated_at': time.time()}
    try:
        (client.table(TABLE)
         .upsert(row, on_conflict='flight,flight_date,bereich').execute())
        _table_ok[0] = True
    except Exception as e:
        if _table_missing(e):
            return jsonify({'ok': False, 'error': 'unavailable'}), 503
        log.warning('[rest-assign] put fail tok=%s: %s', token[:8],
                    type(e).__name__)
        return jsonify({'ok': False, 'error': 'store_failed'}), 503
    _prune(client, _today())
    return jsonify({'ok': True, 'flight': flight, 'date': date,
                    'bereich': bereich, 'updated_at': row['updated_at']})


@rest_assignments_bp.route(
    '/api/rest-assignment/<token>/<flight>/<date>/<bereich>',
    methods=['DELETE'])
def rest_assignment_delete(token, flight, date, bereich):
    """Einteilung aufheben. Dieselben Gates wie beim Schreiben — wer einteilen
    darf, darf auch aufheben (und die Änderung ist über `author_name` beim
    nächsten Schreiben wieder sichtbar).

    Die Instanz steht im PFAD, nicht im Body: der iOS-DELETE-Wrapper
    (`APIClient.delete`) schickt keinen Body mit, und ein zweiter Weg nur für
    diesen einen Aufruf wäre ein Sonderfall, den später niemand mehr kennt.
    """
    if not _bearer_ok(token):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    flight = norm_flight(flight)
    date = norm_date(date)
    bereich = norm_bereich(bereich)
    if not (flight and date and bereich):
        return jsonify({'ok': False, 'error': 'bad_request'}), 400
    if not darf_einteilen(_profile_of(token).get('position'), bereich):
        return jsonify({'ok': False, 'error': 'not_allowed'}), 403
    if not _own_roster_has_leg(token, flight, date):
        return jsonify({'ok': False, 'error': 'not_on_leg'}), 403
    client = _sb()
    if client is None:
        return jsonify({'ok': False, 'error': 'unavailable'}), 503
    try:
        (client.table(TABLE).delete()
         .eq('flight', flight).eq('flight_date', date)
         .eq('bereich', bereich).execute())
    except Exception as e:
        if not _table_missing(e):
            log.warning('[rest-assign] delete fail tok=%s: %s', token[:8],
                        type(e).__name__)
        return jsonify({'ok': False, 'error': 'store_failed'}), 503
    return jsonify({'ok': True, 'flight': flight, 'date': date,
                    'bereich': bereich})


@rest_assignments_bp.route(
    '/api/rest-assignment/<token>/<flight>/<date>/<bereich>', methods=['GET'])
def rest_assignment_get(token, flight, date, bereich):
    """Die Einteilung DIESES Legs für DIESEN Bereich — oder `assignment: null`.

    **Ein Leser ohne Berechtigung und ein Leg ohne Einteilung sehen dieselbe
    Antwort** (200 + null). Das ist Absicht: ein 403 wäre die Auskunft „es gibt
    hier etwas, das du nicht sehen darfst" — und damit selbst eine Information
    über fremde Crews.
    """
    if not _bearer_ok(token):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    leer = jsonify({'ok': True, 'assignment': None})
    f = norm_flight(flight)
    d = norm_date(date)
    b = norm_bereich(bereich)
    if not (f and d and b):
        return leer
    # Gate 2: nur den EIGENEN Bereich. Cockpit sieht die Kabinen-Einteilung
    # nicht und umgekehrt; unlesbare Position sieht gar nichts.
    if bereich_of_position(_profile_of(token).get('position')) != b:
        return leer
    # Gate 1 + 3.
    if not _own_roster_has_leg(token, f, d):
        return leer
    client = _sb()
    if client is None or _table_ok[0] is False:
        return leer
    try:
        r = (client.table(TABLE)
             .select('flight,flight_date,bereich,dep,arr,author_token,'
                     'plan,updated_at')
             .eq('flight', f).eq('flight_date', d).eq('bereich', b)
             .limit(1).execute())
        _table_ok[0] = True
        rows = getattr(r, 'data', None) or []
    except Exception as e:
        if not _table_missing(e):
            log.warning('[rest-assign] get fail: %s', type(e).__name__)
        return leer
    if not rows:
        return leer
    row = rows[0]
    plan = row.get('plan')
    ruhen = plan_ruhen(plan)
    if not ruhen:
        return leer
    author = str(row.get('author_token') or '')
    return jsonify({'ok': True, 'assignment': {
        'flight': row.get('flight'),
        'date': str(row.get('flight_date') or '')[:10],
        'bereich': row.get('bereich'),
        'dep': row.get('dep'),
        'arr': row.get('arr'),
        'ruhen': ruhen,
        'clock': plan.get('clock') if isinstance(plan, dict) else None,
        'updated_at': row.get('updated_at'),
        'author_name': _author_name(author) if author else None,
        'author_self': bool(author) and author == token,
    }})
