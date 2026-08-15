#!/usr/bin/env python3
"""Flugbuch-Upload-Wächter — verarbeitet neue `ax_logbook_upload`-Zeilen.

Läuft IM Backend-Container (App-venv hat pdfplumber; die Parser liegen im
Image unter tools/logbook-parsers). Aufruf per Host-Cron-Wrapper mit flock —
es gibt dadurch garantiert höchstens EINE Instanz, das Skript selbst braucht
keine verteilte Sperre. DB-Zugriff ausschließlich über PostgREST
(SUPABASE_URL/SUPABASE_SERVICE_KEY aus dem Container-Env, kein psycopg2).

Ablauf pro Lauf:
  1. Recovery: `processing`-Leichen eines abgestürzten Laufs → zurück auf
     `pending` (NUR processing; `review`/`failed` bleiben liegen).
  2. Offene Zeilen holen (ohne Roster-PDFs — die gehören der Roster-Pipeline)
     und pro Token bündeln: EIN Nutzer-Batch = EIN Import + EIN Push.
  3. Je Datei: sha256 gegen die gespeicherte Prüfsumme, Byte-Dubletten im
     Batch aussortieren, dann inhaltsbasierte Format-Erkennung über die
     STRIKTEN Parser (OffBlock-Duties-CSV und FCL.050, LH-/CAS-/Cargo-Roster,
     LH-Flugstundenübersicht Cockpit+Kabine, Condor/CFG). Jeder Parser prüft
     seine verfügbaren Kontrollen selbst und bricht bei Abweichung ab — der
     Wächter erbt diese Garantien, statt eigene Leseheuristik zu erfinden.
  4. Legs werden mit einem BESTEHENDEN Import VERSCHMOLZEN (Union über
     Leg-Schlüssel) — `ax_logbook_import` ist eine Zeile pro Token, ein
     naives Upsert würde die Historie des Nutzers löschen.
  5. Rücklesen und verifizieren, ERST DANN Uploads abschließen + ein Push.

FAIL-SAFE (Owner 2026-08-12: „wenn nicht 100 % sicher → nochmal überprüfen"):
  * Format erkannt, aber Kontrolle/Parse verletzt → Zeile auf `review`,
    Owner-Mail. KEIN Nutzer-„failed" für ein Problem, das unseres sein kann.
  * Gleicher Leg-Schlüssel mit ABWEICHENDER Blockzeit beim Merge → kompletter
    Batch auf `review`, nichts geschrieben. Lieber liegen lassen als raten.
  * sha256-Mismatch → `review` (Datei unterwegs beschädigt?).
  * KEIN Parser erkennt das Format → ehrliches `failed` + kurzer Push an den
    Nutzer („Flugbuch-Import fehlgeschlagen — bitte lade die Datei noch einmal
    hoch.") + Owner-Mail. `review` pusht NICHTS: dort prüft der Betreiber,
    ein zweiter Upload würde nicht helfen.
  * Jede unerwartete Exception → Zeile bleibt `pending` (nächster Lauf
    versucht es erneut), Owner-Mail mit Traceback-Kopf.

Idempotenz: Alle Schritte sind wiederholbar — der Merge ist eine Union, der
Push läuft über `enqueue_push_outbox` mit fester Idempotenz-Key
(`logbook-import-completed:<upload_id>` wie beim manuellen Werkzeug,
`logbook-import-failed:<upload_id>` für den Fehlerfall).
"""

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import traceback
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from legkeys import dedupe_keys  # noqa: E402

ROSTER_MARKER = "AEROX_ROSTER_PDF_V1"
ALERT_TO = "aerox@aerosteuer.de"
ALERT_FROM = "noreply@aerosteuer.de"
MAX_BATCH_FILES = 40          # Schutz vor Amok-Uploads in einem Lauf
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REVIEW = "review"      # wartet auf Menschen; terminal, App pollt nicht
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"
# Endzustände: einmal erreicht, NIE von der Notbremse in main() zurückgedreht.
TERMINAL_STATUS = (STATUS_COMPLETED, STATUS_FAILED, STATUS_REVIEW)

# Positiv-Cache des Backends (`logbook_import_<token>.json`, 6 h TTL). app.py
# bildet den Pfad relativ zum Arbeitsverzeichnis (`_user_history_state`); der
# Wächter läuft im selben Container auf demselben Volume, leitet den Pfad aber
# aus der eigenen Dateilage ab — der Cron-Wrapper startet mit fremdem cwd.
_USER_HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "_user_history_state")
RE_LEG_SUFFIX = re.compile(r"\(\d+\)$")
RE_SOURCE_ELLIPSIS = re.compile(r"^…\s*\(\+(\d+)\)$")
SOURCE_KEEP = 3               # meta.source wächst sonst mit jedem Import


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"env {name} fehlt — Wächter läuft nur im Container")
    return value


# Lazy statt Modulebene: die reinen Merge-Funktionen sind so auch in Tests
# importierbar, ohne dass Container-Env vorhanden sein muss.
_SB = {}


def _sb():
    if not _SB:
        _SB["url"] = _env("SUPABASE_URL").rstrip("/")
        _SB["key"] = _env("SUPABASE_SERVICE_KEY")
    return _SB["url"], _SB["key"]


def _rest(method, path, payload=None, headers=None, expect_json=True):
    sb_url, SB_KEY = _sb()
    req = urllib.request.Request(
        f"{sb_url}/rest/v1/{path}",
        data=(json.dumps(payload).encode() if payload is not None else None),
        method=method,
        headers={
            "apikey": SB_KEY,
            "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    if not expect_json or not body:
        return None
    return json.loads(body)


def _log(msg):
    print(f"[logbook-watchdog] {datetime.now(timezone.utc).isoformat()} {msg}",
          flush=True)


def _alert(subject, body):
    """Owner-Mail via Resend. Best-effort — ein Mailfehler darf den Lauf nie
    stoppen, er landet nur im Cron-Log. GOTCHA aus dem Analytics-Wrapper:
    Cloudflare blockt Pythons Default-User-Agent (403/1010) → curl-UA."""
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        _log(f"ALERT (keine RESEND_API_KEY): {subject}")
        return
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({
                "from": ALERT_FROM, "to": [ALERT_TO],
                "subject": f"[logbook-watchdog] {subject}",
                "text": body,
            }).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": "curl/8.0"},
            method="POST")
        urllib.request.urlopen(req, timeout=30).read()
        _log(f"alert mail raus: {subject}")
    except Exception as ex:  # noqa: BLE001 — bewusst breit, nur loggen
        _log(f"alert mail FEHLGESCHLAGEN ({type(ex).__name__}): {subject}")


# ── Upload-Zeilen ───────────────────────────────────────────────────────────

def _recover_stale_processing():
    rows = _rest("PATCH",
                 "ax_logbook_upload?status=eq.processing&processed=is.false",
                 {"status": STATUS_PENDING},
                 headers={"Prefer": "return=representation"}) or []
    if rows:
        _log(f"recovery: {[r['id'] for r in rows]} processing→pending")


def _pending_rows():
    # `neq` würde NULL-notes verwerfen — deshalb explizites or=(is.null,neq).
    q = ("ax_logbook_upload?processed=is.false&status=eq.pending"
         f"&or=(note.is.null,note.neq.{ROSTER_MARKER})"
         "&select=id,token,name,airline,filename,sha256,size_bytes,created_at"
         "&order=id&limit=200")
    return _rest("GET", q) or []


def _set_status(ids, status, processed=None, error_code=None,
                error_message=None):
    if not ids:
        return
    payload = {"status": status}
    if processed is not None:
        payload["processed"] = processed
    payload["error_code"] = error_code
    payload["error_message"] = (str(error_message)[:1000]
                                if error_message else None)
    if status in (STATUS_COMPLETED, STATUS_FAILED):
        now = datetime.now(timezone.utc).isoformat()
        payload.update({
            "completed_at": now,
            "data_b64": None,
            "payload_purged_at": now,
            "purge_after": None,
        })
    elif status == STATUS_REVIEW:
        payload["purge_after"] = (
            datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    id_list = ",".join(str(i) for i in ids)
    _rest("PATCH", f"ax_logbook_upload?id=in.({id_list})", payload,
          expect_json=False)


def _purge_expired_payloads():
    """Delete sensitive source bytes after their useful processing window.

    Terminal rows are metadata/audit records only.  Review rows retain their
    bytes for 14 days; after that the reason remains visible but the document
    itself is removed automatically.
    """
    # Embedded into a PostgREST query below. An unescaped `+00:00` is decoded
    # as a space and makes the timestamp filter invalid; `Z` is URL-safe UTC.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"data_b64": None, "payload_purged_at": now}
    _rest("PATCH",
          "ax_logbook_upload?data_b64=not.is.null"
          "&status=in.(completed,failed)", payload, expect_json=False)
    _rest("PATCH",
          "ax_logbook_upload?data_b64=not.is.null&status=eq.review"
          f"&purge_after=lt.{now}", payload, expect_json=False)


def _download(upload_id):
    rows = _rest("GET",
                 f"ax_logbook_upload?id=eq.{upload_id}&select=data_b64")
    if not rows or not rows[0].get("data_b64"):
        raise ValueError(f"Upload #{upload_id}: data_b64 fehlt")
    return base64.b64decode(rows[0]["data_b64"])


# ── Parser-Erkennung ────────────────────────────────────────────────────────

def _try_parsers(path):
    """(parser_name, legs, sims, report) | ('unsupported', ...) | wirft
    ValueError für „Format erkannt, aber Kontrolle verletzt".

    EXPLIZITES Routing statt Parser-Reihenfolge: LH- und Condor-Variante
    tragen DENSELBEN Kopf („Flugstunden - Übersicht … für Monat"). Probierte
    man erst den LH-Parser, scheiterte ein Condor-PDF nicht am Kopf, sondern
    erst an der LH-Summenzeile — und DER Fehler ist kein „falsches Format",
    die Datei wäre fälschlich in `review` gelandet statt beim Condor-Parser.
    Deshalb entscheidet der Dokumenttext, welcher Parser zuständig ist; seine
    ValueErrors sind danach ausnahmslos echte Kontroll-Verletzungen."""
    import pdfplumber
    from pdfminer.pdfparser import PDFSyntaxError
    try:
        # pdfplumber >=0.11 kapselt pdfminer-Fehler; die Produktionsversion
        # reicht PDFSyntaxError noch direkt durch. Beide Varianten abdecken,
        # ohne einen nur in neuem pdfminer vorhandenen Modulpfad zu importieren.
        from pdfplumber.utils.exceptions import PdfminerException
        pdf_errors = (PDFSyntaxError, PdfminerException)
    except ImportError:  # pragma: no cover - Produktions-Altversion
        pdf_errors = (PDFSyntaxError,)
    import parse_duties_v8
    import parse_faa_logbook
    import parse_fcl050_v2
    import parse_foreflight_easa
    import parse_foreflight_csv
    import parse_simple_flights_csv
    import parse_edw_xlsx
    import parse_lh_flugstunden
    import parse_cfg_flugstunden
    import parse_roster_logbook

    # Der Upload-Endpunkt nimmt CSV/Excel/PDF/JSON/ZIP an. Vorher bekam jede
    # Datei trotzdem die Endung .pdf und lief blind in pdfplumber. Ein valides
    # Duties-CSV endete dadurch als unerwarteter ``No /Root object``-Crash,
    # wurde auf pending zurückgesetzt und erzeugte alle zehn Minuten dieselbe
    # Owner-Mail. Deshalb zuerst die belegte CSV-Signatur prüfen und PDFs nur
    # anhand ihrer Magic Bytes öffnen; die Dateiendung ist nicht vertrauenswürdig.
    if parse_duties_v8.matches_csv(path):
        legs, sims, report = parse_duties_v8.parse_csv(path)
        return "offblock_duties", legs, sims, report
    if parse_foreflight_csv.matches_csv(path):
        legs, sims, report = parse_foreflight_csv.parse_csv(path)
        return "foreflight_csv", legs, sims, report
    if parse_simple_flights_csv.matches_csv(path):
        legs, sims, report = parse_simple_flights_csv.parse_csv(path)
        return "simple_flight_history", legs, sims, report
    if parse_edw_xlsx.matches_workbook(path):
        result, controls = parse_edw_xlsx.parse_workbook(path)
        legs = result["legs"]
        first, last = legs[0]["date"], legs[-1]["date"]
        report = dict(controls)
        report["month"] = (first[:7] if first[:7] == last[:7]
                           else f"{first[:7]}–{last[:7]}")
        return "edelweiss_xlsx", legs, result["sim"], report
    with open(path, "rb") as handle:
        if not handle.read(5).startswith(b"%PDF-"):
            return "unsupported", None, None, None
    if parse_fcl050_v2.matches_pdf(path):
        legs, sims, report = parse_fcl050_v2.parse_pdf(path)
        return "offblock_fcl050", legs, sims, report
    if parse_faa_logbook.matches_pdf(path):
        legs, sims, report = parse_faa_logbook.parse_pdf(path)
        return "offblock_faa", legs, sims, report
    if parse_foreflight_easa.matches_pdf(path):
        legs, sims, report = parse_foreflight_easa.parse_pdf(path)
        return "foreflight_easa", legs, sims, report
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except pdf_errors + (OSError,):
        # Beschädigte/unvollständige PDFs sind ein endgültiger Dateifehler,
        # kein transienter Backendfehler. ``unsupported`` setzt processed=true
        # und bittet den Nutzer idempotent einmal um einen erneuten Upload.
        return "unsupported", None, None, None
    # Valid documents that contain useful accounting/aggregate information,
    # but no individual flight facts.  They are accepted deliberately without
    # inventing routes, flight numbers or block times.  The tax-document and
    # roster paths can consume them in their proper context; the logbook job
    # must not loop/error-mail merely because they were uploaded here.
    if (re.search(r"Streckeneinsatz\s*-?\s*Abrechnung", text,
                  re.IGNORECASE)
            and re.search(r"\bDatum\b.*\bAb\b.*\bAn\b.*\bstfrei\b", text,
                          re.IGNORECASE | re.DOTALL)):
        return "informational_pdf", [], [], {
            "month": "tax-expense-statement",
            "document_type": "streckeneinsatzabrechnung",
        }
    if (re.search(r"(?im)^\s*Flight Time and Landings\s*$", text)
            and re.search(r"(?im)^\s*Total since entry:\s*\d+:\d{2}\s+\d+",
                          text)):
        return "informational_pdf", [], [], {
            "month": "flight-time-statistics",
            "document_type": "aggregate_flight_time_statistics",
        }
    if (parse_roster_logbook.ACK_KIND.search(text[:500])
            or ("Crew Assignment System" in text[:1500]
                and ("Einsatzplan" in text[:1500]
                     or "Dienstplan" in text[:1500]))
            or ("Duty plan requested at" in text[:1000]
                and "All times: Local FRA" in text[:1000])
            or ("Individual duty plan" in text[:1000]
                and "NetLine/Crew(CFG)" in text[:1000])):
        payload = parse_roster_logbook.parse_sources(
            [path], completed_at=datetime.now(timezone.utc),
            preserve_source_month=True)
        return ("roster_logbook", payload["legs"], payload["sim"],
                payload["report"])
    if not parse_lh_flugstunden.RE_HEADER.search(text):
        return "unsupported", None, None, None
    if "Condor" in text:
        name, mod = "cfg_flugstunden", parse_cfg_flugstunden
    else:
        name, mod = "lh_flugstunden", parse_lh_flugstunden
    # Die beiden Parser geben BEWUSST verschieden zurück: LH liefert
    # (legs, sims, report), die Condor/CFG-Variante nur (legs, report) — sie
    # bricht bei Simulator-Minuten ohnehin ab, hat also keine Sim-Liste. Ein
    # festes Drei-Entpacken sprengte jeden Condor-Upload mit einem ValueError,
    # der hier als „Kontrolle verletzt" gedeutet wurde → Datei in `review`.
    out = mod.parse_pdf(path)
    if len(out) == 3:
        legs, sims, report = out
    else:
        legs, report = out
        sims = []
    return name, legs, sims, report


# ── Merge mit bestehendem Import ────────────────────────────────────────────

def _base_flight(flight):
    """Flugnummer ohne Kollisions-Suffix „(2)". Der Suffix ist reine
    Lese-Disambiguierung (legkeys), keine Identität — ohne dieses Abziehen
    gälte dasselbe Leg beim nächsten Upload derselben Datei als NEU."""
    return RE_LEG_SUFFIX.sub("", (flight or "").upper().strip())


def _leg_key(leg):
    return (leg.get("date"), _base_flight(leg.get("flight")), leg.get("from"),
            leg.get("to"), leg.get("dep_iso") or f"block:{leg.get('block_min')}")


def _sim_key(sim):
    return (sim.get("date"), sim.get("code"), sim.get("duration_min"))


def _fact_key(leg):
    """Physical-fact identity for exports without flight number/timestamps."""
    compact = lambda value: re.sub(r"\s+", "", str(value or "")).upper()
    return (leg.get("date"), leg.get("from"), leg.get("to"),
            leg.get("block_min"), compact(leg.get("reg")),
            compact(leg.get("type")), leg.get("ldg_day"),
            leg.get("ldg_night"), leg.get("night_min"))


def merge_legs(existing, new):
    """Union über Leg-Schlüssel. Bestehende Zeilen gewinnen unverändert;
    identische Schlüssel mit ABWEICHENDER Blockzeit sind ein Konflikt →
    ValueError (Aufrufer schickt den Batch in `review`)."""
    by_key = {}
    facts = defaultdict(list)
    for leg in existing or []:
        by_key[_leg_key(leg)] = leg
        facts[_fact_key(leg)].append(leg)
    added = 0
    for leg in new:
        key = _leg_key(leg)
        if key in by_key:
            if by_key[key].get("block_min") != leg.get("block_min"):
                raise ValueError(
                    f"Merge-Konflikt {key}: block {by_key[key].get('block_min')}"
                    f" != {leg.get('block_min')}")
            continue
        # FAA Logbook Pro omits flight number and departure clock. The same
        # user can upload its EASA twin, which carries the same physical facts
        # plus UTC clocks. If one side lacks a clock and every printed fact
        # agrees, it is one leg, not a second flight. Consume candidates so
        # repeated identical shuttles remain count-preserving.
        candidates = facts.get(_fact_key(leg)) or []
        match_index = next((index for index, candidate in enumerate(candidates)
                            if bool(candidate.get("dep_iso"))
                            != bool(leg.get("dep_iso"))), None)
        if match_index is not None:
            candidates.pop(match_index)
            continue
        by_key[key] = leg
        facts[_fact_key(leg)].append(leg)
        added += 1
    merged = sorted(by_key.values(),
                    key=lambda l: (l.get("date") or "", l.get("dep_iso") or "",
                                   l.get("flight") or ""))
    return merged, added


def dedupe_for_reader(legs):
    """Leg-Keys auf den GRÖBEREN Schlüssel des Lesers eindeutig machen.

    Der Wächter unterscheidet Legs zusätzlich über `dep_iso`/Blockzeit, das
    Backend liest sie aber nur als `date|flight|from|to` (`_logbook_leg_key`)
    in ein Dict — zwei hier verschiedene Zeilen fielen dort STILL zusammen,
    die zuerst gelesene verlor ihre Landungen. Nachbearbeitung wie in allen
    Parsern: die zweite Belegung bekommt „(2)" (legkeys.dedupe_keys).

    Vorher werden bestehende Suffixe abgezogen, damit die Nummerierung bei
    jedem Lauf über die GANZE (sortierte) Liste neu und stabil vergeben wird —
    sonst bekäme eine dritte Belegung erneut „(2)"."""
    for leg in legs:
        if leg.get("flight"):
            leg["flight"] = _base_flight(leg["flight"])
    return dedupe_keys(legs)


def merge_sims(existing, new):
    by_key = {_sim_key(s): s for s in (existing or [])}
    for sim in new or []:
        by_key.setdefault(_sim_key(sim), sim)
    return sorted(by_key.values(),
                  key=lambda s: (s.get("date") or "", s.get("code") or ""))


def remove_generic_faa_sim_twins(sims):
    """Drop FAA's generic FSTD copy when a descriptive EASA row exists.

    Logbook Pro exports the same simulator session twice across its EASA and
    FAA layouts: EASA retains the course code (for example ``RC25_1``), while
    FAA reduces it to ``FSTD``.  ``merge_sims`` cannot normally collapse
    those because its code field deliberately distinguishes two genuine
    same-day sessions.  Pair generic rows one-for-one with descriptive rows
    that have the exact same date and duration; unpaired FSTD sessions remain.
    """
    descriptive = defaultdict(int)
    for sim in sims or []:
        if str(sim.get("code") or "").upper() != "FSTD":
            descriptive[(sim.get("date"), sim.get("duration_min"))] += 1
    kept, removed = [], 0
    for sim in sims or []:
        fact = (sim.get("date"), sim.get("duration_min"))
        if (str(sim.get("code") or "").upper() == "FSTD"
                and descriptive.get(fact, 0) > 0):
            descriptive[fact] -= 1
            removed += 1
            continue
        kept.append(sim)
    return kept, removed


def remove_fcl_sim_leg_artifacts(legs, sims):
    """Remove old zero-time A-page copies after the fixed FCL parser runs."""
    sim_dates = {sim.get("date") for sim in sims or [] if sim.get("date")}
    kept, removed = [], 0
    for leg in legs or []:
        is_artifact = (
            leg.get("date") in sim_dates
            and leg.get("from") and leg.get("from") == leg.get("to")
            and not leg.get("block_min")
            and not leg.get("ldg_day") and not leg.get("ldg_night"))
        if is_artifact:
            removed += 1
        else:
            kept.append(leg)
    return kept, removed


def resolve_roster_revisions(parsed_files):
    """Drop older complete monthly roster revisions inside one upload batch.

    This is deliberately done before the append-only import merge. A later
    Acknowledged roster can replace a Released roster with an entirely
    different flight/routing; leg-key dedupe alone cannot recognize that the
    older assignment was cancelled. A zero-flight newer roster is also a
    meaningful complete replacement for its month.
    """
    roster_files = [item for item in parsed_files
                    if item.get("parser") == "roster_logbook"]
    month_winners = {}
    for item in roster_files:
        report = item.get("report") or {}
        created = report.get("source_created_at") or ""
        rank = (created, int(item.get("id") or 0))
        for month in report.get("coverage_months") or []:
            previous = month_winners.get(month)
            if previous is None or rank > previous[0]:
                month_winners[month] = (rank, item["id"])
    superseded = 0
    for item in roster_files:
        kept = []
        for leg in item.get("legs") or []:
            month = leg.pop("_roster_month", None)
            winner = month_winners.get(month)
            if month and winner and winner[1] != item["id"]:
                superseded += 1
                continue
            kept.append(leg)
        item["legs"] = kept
    return superseded


def _capped_source(prev, label, keep=SOURCE_KEEP):
    """`meta.source` begrenzen. Jeder Import hängte bisher „ + <Label>" an —
    die Zeichenkette wächst unbegrenzt und steht in der App als Dateiname.
    Es bleiben die letzten `keep` Bausteine, davor „… (+N)" für den Rest."""
    parts = [p.strip() for p in (prev or "").split(" + ") if p.strip()]
    dropped = 0
    if parts:
        match = RE_SOURCE_ELLIPSIS.match(parts[0])
        if match:
            dropped = int(match.group(1))
            parts = parts[1:]
    parts.append(label)
    if len(parts) > keep:
        dropped += len(parts) - keep
        parts = parts[-keep:]
    return (f"… (+{dropped}) + " if dropped else "") + " + ".join(parts)


def _bust_import_cache(token):
    """Positiv-Cache-Datei des Backends für diesen Token löschen.

    Ohne das zeigt die App nach dem „fertig"-Push bis zu 6 h den ALTEN Blob
    (app.py `_logbook_import_load`, TTL 6 h). Best effort — ein Cache-Fehler
    darf einen verifizierten Import nie zurückrollen.

    TODO/GRENZE: Das ZWEITE Origin (NAS, https://nas-api.aerosteuer.de) hat
    einen eigenen Datenträger und keine erreichbare Invalidierungs-Route; dort
    bleibt der alte Blob bis zu 6 h warm. Bewusst KEIN neuer HTTP-Endpoint in
    app.py — solange die Lücke nur diesen Cache betrifft, ist sie zeitlich
    begrenzt und selbstheilend."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", token or "")[:64]
    if not safe:
        return
    path = os.path.join(_USER_HISTORY_DIR, f"logbook_import_{safe}.json")
    try:
        os.unlink(path)
        _log(f"Import-Cache gelöscht: {os.path.basename(path)}")
    except FileNotFoundError:
        pass
    except OSError as ex:  # noqa: BLE001 — nur loggen
        _log(f"Import-Cache-Löschen fehlgeschlagen ({type(ex).__name__}): {ex}")


def _meta_for(legs, sims, label, extra):
    meta = {
        "source": label,
        "legs": len(legs),
        "block_min": sum(l.get("block_min", 0) for l in legs),
        "landings": sum(l.get("ldg_day", 0) + l.get("ldg_night", 0)
                        for l in legs),
        "sim_sessions": len(sims),
        "sim_min": sum(s.get("duration_min") or 0 for s in sims),
        "first_date": legs[0]["date"] if legs else None,
        "last_date": legs[-1]["date"] if legs else None,
    }
    meta.update(extra)
    return meta


def _upsert_import(token, legs, sims, meta):
    _rest("POST", "ax_logbook_import?on_conflict=token", {
        "token": token,
        "filename": meta["source"],
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "legs": legs, "sim": sims, "meta": meta,
    }, headers={"Prefer": "resolution=merge-duplicates"}, expect_json=False)
    # Rücklesen als Verifikation — dem Schreiben wird nicht geglaubt.
    rows = _rest("GET", f"ax_logbook_import?token=eq.{token}"
                        "&select=legs,sim") or []
    if not rows:
        raise RuntimeError("Rücklesen: Import-Zeile fehlt nach Upsert")
    got_legs, got_sims = rows[0]["legs"], rows[0].get("sim") or []
    if len(got_legs) != len(legs) or len(got_sims) != len(sims):
        raise RuntimeError(
            f"Rücklesen: legs {len(got_legs)}!={len(legs)} "
            f"oder sim {len(got_sims)}!={len(sims)}")
    got_keys = {_leg_key(l) for l in got_legs}
    want_keys = {_leg_key(l) for l in legs}
    if got_keys != want_keys:
        raise RuntimeError("Rücklesen: Leg-Schlüsselmengen weichen ab")


def _push_completed(token, anchor_upload_id):
    payload = json.dumps({
        "title": "Flugbuch-Import fertig",
        "body": "Deine importierten Flüge und Stunden sind jetzt im Flugbuch.",
        "data": {"type": "logbook_import_completed",
                 "localization_key": "logbook_import_completed",
                 "job_id": anchor_upload_id,
                 "deep_link": "aerox://more/logbook"},
    }, ensure_ascii=False)
    _rest("POST", "rpc/enqueue_push_outbox", {
        "p_idempotency_key": f"logbook-import-completed:{anchor_upload_id}",
        "p_user_token": token, "p_payload": json.loads(payload),
    }, expect_json=False)


def _push_failed(token, anchor_upload_id):
    """Endgültig gescheitert → EINE kurze, handlungsfähige Nachricht.

    Owner 12.08.: „wenn es nicht ging sagen bitte nochmal hochladen". Der
    alte Text erklärte das Dateiformat in drei Zeilen — auf dem Sperrbildschirm
    liest das niemand. Die Übersetzungen hängen am `localization_key`
    (_PUSH_SYSTEM_COPY in app.py), `data` bleibt deshalb frei von variablen
    Feldern: der Ankunfts-Push hat gezeigt, dass eine wandernde job_id im
    `data` die Dedupe der Outbox aushebelt, sobald der Payload je über
    `_push_outbox_enqueue` läuft.

    NUR für Endzustand `failed`. `review` bekommt bewusst nichts: dort prüft
    der Betreiber, und „bitte nochmal hochladen" wäre schlicht falsch.
    """
    payload = {
        "title": "Flugbuch-Import fehlgeschlagen",
        "body": "Bitte lade die Datei noch einmal hoch.",
        "data": {"type": "logbook_import_failed",
                 "localization_key": "logbook_import_failed",
                 "deep_link": "aerox://more/logbook"},
    }
    _rest("POST", "rpc/enqueue_push_outbox", {
        "p_idempotency_key": f"logbook-import-failed:{anchor_upload_id}",
        "p_user_token": token, "p_payload": payload,
    }, expect_json=False)


# ── Batch-Verarbeitung ──────────────────────────────────────────────────────

def process_token_batch(token, rows, events, terminal=None):
    ids = [r["id"] for r in rows]

    def _status(target_ids, status, processed=None, error_code=None,
                error_message=None):
        """Statuswechsel + Merkzettel `terminal`: Zeilen in einem Endzustand
        darf die Notbremse in main() NICHT auf `pending` zurücksetzen — sonst
        hängt eine längst fertige Datei in der App ewig als „in Arbeit"."""
        _set_status(target_ids, status, processed, error_code, error_message)
        if terminal is None:
            return
        if status in TERMINAL_STATUS:
            terminal.update(target_ids)
        else:
            terminal.difference_update(target_ids)

    _status(ids, STATUS_PROCESSING)
    parsed_files, unsupported, seen_sha = [], [], {}
    review = []

    for row in rows:
        rid = row["id"]
        try:
            blob = _download(rid)
        except Exception as ex:  # noqa: BLE001
            review.append((rid, f"download: {ex}"))
            continue
        sha = hashlib.sha256(blob).hexdigest()
        if row.get("sha256") and row["sha256"] != sha:
            review.append((rid, "sha256-Mismatch gegen gespeicherte Prüfsumme"))
            continue
        if sha in seen_sha:
            # Byte-identische Doppel-Uploads desselben Batches: die Kopie ist
            # mit dem Original erledigt (Stefan-Muster #278/#280).
            parsed_files.append({"id": rid, "dup_of": seen_sha[sha]})
            continue
        seen_sha[sha] = rid
        # Format-Routing arbeitet mit Content-Signaturen. Die neutrale Endung
        # verhindert zusätzlich, dass Aufrufer sie versehentlich als Typbeleg
        # missverstehen.
        with tempfile.NamedTemporaryFile(suffix=".upload", delete=False) as tmp:
            tmp.write(blob)
            path = tmp.name
        try:
            name, legs, sims, report = _try_parsers(path)
            if name == "unsupported":
                unsupported.append(rid)
            else:
                parsed_files.append({"id": rid, "parser": name, "legs": legs,
                                     "sims": sims, "report": report})
        except ValueError as ex:
            review.append((rid, f"Kontrolle/Parse: {ex}"))
        finally:
            os.unlink(path)

    # Parser-/Kontrollfehler sind dateibezogen.  Ein einzelner korrigierter
    # Monat darf nicht elf andere, vollständig validierte Monatsdateien
    # blockieren.  Die betroffene Datei bleibt mit ihrem konkreten Grund im
    # Review; die unabhängigen guten Dateien laufen unten normal weiter.
    review_by_id = {rid: message for rid, message in review}
    for rid, message in review:
        _status([rid], STATUS_REVIEW, processed=False,
                error_code="needs_review", error_message=message)
    if review:
        events.append(("review", token, sorted(review_by_id),
                       "; ".join(f"#{i}: {m}" for i, m in review)))

    informational = [f for f in parsed_files
                     if f.get("parser") == "informational_pdf"]
    real = [f for f in parsed_files if "parser" in f
            and f.get("parser") != "informational_pdf"]
    dups = [f for f in parsed_files if "dup_of" in f]

    if real:
        roster_superseded = resolve_roster_revisions(real)
        new_legs = [leg for f in real for leg in f["legs"]]
        new_sims = [sim for f in real for sim in f["sims"]]
        existing = _rest("GET", f"ax_logbook_import?token=eq.{token}"
                                "&select=legs,sim,meta") or []
        old_legs = existing[0]["legs"] if existing else []
        old_sims = (existing[0].get("sim") or []) if existing else []
        fcl_cleanup = 0
        if any(f["parser"] == "offblock_fcl050" for f in real):
            old_legs, fcl_cleanup = remove_fcl_sim_leg_artifacts(
                old_legs, old_sims)
        try:
            merged_legs, added = merge_legs(old_legs, new_legs)
        except ValueError as ex:
            real_ids = [f["id"] for f in real]
            review_by_id.update({rid: str(ex) for rid in real_ids})
            _status(real_ids, STATUS_REVIEW, processed=False,
                    error_code="merge_conflict", error_message=str(ex))
            events.append(("review", token, real_ids, str(ex)))
            real = []
            merged_legs = merged_sims = []
            added = 0
        if real:
            merged_sims = merge_sims(old_sims, new_sims)
        faa_sim_twins = 0
        if real and any(f["parser"] == "offblock_faa" for f in real):
            merged_sims, faa_sim_twins = remove_generic_faa_sim_twins(
                merged_sims)
        collisions = dedupe_for_reader(merged_legs) if real else []
        if not real:
            # Informational/unsupported/review siblings still need their own
            # terminal handling below.
            pass
        else:
            months = sorted({f["report"]["month"] for f in real})
            label = (f"Watchdog: {real[0]['parser']} {months[0]}–{months[-1]}"
                     if len(months) > 1 else
                     f"Watchdog: {real[0]['parser']} {months[0]}")
        prev_meta = (existing[0].get("meta") or {}) if existing else {}
        report_carryovers = {
            int(f["report"]["carryover_min"])
            for f in real
            if isinstance(f.get("report"), dict)
            and isinstance(f["report"].get("carryover_min"), int)
        }
        prev_carryover = prev_meta.get("carryover_min")
        if real and (len(report_carryovers) > 1
                or (report_carryovers and isinstance(prev_carryover, int)
                    and prev_carryover not in report_carryovers)):
            real_ids = [f["id"] for f in real]
            message = "widersprüchliche FCL.050-Überträge"
            review_by_id.update({rid: message for rid in real_ids})
            _status(real_ids, STATUS_REVIEW, processed=False,
                    error_code="carryover_conflict",
                    error_message=message)
            events.append(("review", token, real_ids, message))
            real = []
        carryover_min = (next(iter(report_carryovers))
                         if report_carryovers else prev_carryover)
        carryover_landing_fields = {}
        for field in ("carryover_ldg_day", "carryover_ldg_night",
                      "carryover_landings"):
            report_values = {
                int(f["report"][field])
                for f in real
                if isinstance(f.get("report"), dict)
                and isinstance(f["report"].get(field), int)
            }
            previous = prev_meta.get(field)
            if real and (len(report_values) > 1
                    or (report_values and isinstance(previous, int)
                        and previous not in report_values)):
                real_ids = [f["id"] for f in real]
                message = "widersprüchliche FAA-Landungsüberträge"
                review_by_id.update({rid: message for rid in real_ids})
                _status(real_ids, STATUS_REVIEW, processed=False,
                        error_code="carryover_conflict",
                        error_message=message)
                events.append(("review", token, real_ids, message))
                real = []
                break
            value = (next(iter(report_values))
                     if report_values else previous)
            if isinstance(value, int) and value >= 0:
                carryover_landing_fields[field] = value
        if real:
            label = _capped_source(prev_meta.get("source"), label)
        extra_meta = {
            "watchdog": {"upload_ids": [f["id"] for f in real],
                         "duplicates_skipped": [f["id"] for f in dups],
                         "added_legs": added,
                         "roster_revision_legs_superseded": roster_superseded,
                         "fcl_sim_leg_artifacts_removed": fcl_cleanup,
                         "faa_sim_twins_removed": faa_sim_twins,
                         "dedupe_suffixes": len(collisions),
                         "ts": datetime.now(timezone.utc).isoformat()},
        }
        if isinstance(carryover_min, int) and carryover_min >= 0:
            extra_meta["carryover_min"] = carryover_min
        extra_meta.update(carryover_landing_fields)
        if real:
            meta = _meta_for(merged_legs, merged_sims, label, extra_meta)
            _upsert_import(token, merged_legs, merged_sims, meta)
            _bust_import_cache(token)
            _status([f["id"] for f in real], STATUS_COMPLETED, processed=True)
            _push_completed(token, max(f["id"] for f in real))
            events.append(("imported", token, [f["id"] for f in real],
                           f"+{added} Legs (gesamt {len(merged_legs)})"))

    if informational:
        info_ids = [f["id"] for f in informational]
        _status(info_ids, STATUS_COMPLETED, processed=True)
        events.append(("informational", token, info_ids,
                       "erkannt; enthält keine einzelnen Flugbuch-Legs"))

    if unsupported:
        _status(unsupported, STATUS_FAILED, processed=True,
                error_code="unsupported_format",
                error_message="Dateiformat wird noch nicht unterstützt.")
        events.append(("unsupported", token, unsupported, "Format unbekannt"))

    # Byte-Dubletten erben das Schicksal ihres ORIGINALS — eine Kopie einer
    # unbrauchbaren Datei ist genauso `failed`, nicht still `completed`
    # (Erst-Einsatz 12.08.: #286 = Kopie der unsupported #285). Original
    # außerhalb dieses Laufs (früher verarbeitet) ⇒ Inhalt ist erledigt ⇒
    # completed ohne Push.
    failed_dups = []
    if dups:
        real_ids = {f["id"] for f in real + informational}
        review_ids = set(review_by_id)
        failed_dups = [f["id"] for f in dups if f["dup_of"] in set(unsupported)]
        review_dups = [f["id"] for f in dups if f["dup_of"] in review_ids]
        done_dups = [f["id"] for f in dups
                     if f["dup_of"] in real_ids or f["dup_of"] not in
                     set(unsupported) | real_ids | review_ids]
        if failed_dups:
            _status(failed_dups, STATUS_FAILED, processed=True,
                    error_code="unsupported_format",
                    error_message="Dateiformat wird noch nicht unterstützt.")
        if review_dups:
            _status(review_dups, STATUS_REVIEW, processed=False,
                    error_code="needs_review",
                    error_message="Identische Datei wartet bereits auf Prüfung.")
        if done_dups:
            _status(done_dups, STATUS_COMPLETED, processed=True)
        events.append(("dups", token,
                       {"failed": failed_dups, "review": review_dups,
                        "completed": done_dups}, ""))

    # Fehler-Push ganz zum Schluss und über ALLE `failed`-Zeilen des Laufs:
    # eine Datei und ihre Byte-Kopie sind EIN Problem, nicht zwei. Anker ist
    # die höchste betroffene Upload-ID — derselbe Batch ergibt in jedem Lauf
    # denselben Idempotenz-Key, ein Wiederholungslauf pusht also nicht erneut.
    failed_ids = sorted(set(unsupported) | set(failed_dups))
    if failed_ids:
        _push_failed(token, max(failed_ids))


def main():
    _recover_stale_processing()
    _purge_expired_payloads()
    rows = _pending_rows()
    if not rows:
        _log("nichts offen")
        return
    by_token = defaultdict(list)
    for row in rows:
        by_token[row["token"]].append(row)
    if len(rows) > MAX_BATCH_FILES:
        # Kappen NUR an Token-Grenzen: mitten in einer Nutzer-Gruppe zu
        # schneiden verteilt EINEN Upload-Schwung auf zwei Läufe — der Nutzer
        # bekäme zwei „Flugbuch-Import fertig"-Pushes. Die erste Gruppe läuft
        # immer, auch wenn sie allein über der Grenze liegt (sonst bliebe sie
        # für immer liegen).
        kept, taken = {}, 0
        for token, batch in by_token.items():
            if taken and taken + len(batch) > MAX_BATCH_FILES:
                break
            kept[token] = batch
            taken += len(batch)
        by_token = kept
        rows = [r for batch in kept.values() for r in batch]
    _log(f"{len(rows)} offene Datei(en) von {len(by_token)} Nutzer(n)")

    events = []
    for token, batch in by_token.items():
        terminal = set()
        try:
            process_token_batch(token, batch, events, terminal)
        except Exception:  # noqa: BLE001
            # Unerwartet → Zeilen zurück auf pending (nächster Lauf), Alarm.
            # ABER nur die, die KEINEN Endzustand erreicht haben: eine bereits
            # `completed`+`processed`-Zeile auf `pending` zurückzudrehen holt
            # sie nie wieder (`_pending_rows` filtert processed=is.false) und
            # zeigt dem Nutzer den fertigen Import ewig als „in Arbeit".
            rest = [r["id"] for r in batch if r["id"] not in terminal]
            if rest:
                _set_status(rest, STATUS_PENDING)
            head = traceback.format_exc(limit=6)
            events.append(("error", token, rest, head))

    lines = []
    for kind, token, ids, detail in events:
        line = f"{kind}: tok={token[:10]}… ids={ids} {detail}"
        _log(line)
        lines.append(line)
    if any(k in ("review", "unsupported", "error") for k, *_ in events):
        _alert("Flugbuch-Uploads brauchen Aufmerksamkeit",
               "\n".join(lines) or "(leer)")


if __name__ == "__main__":
    main()
