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
     Nur wenn alle Parser explizit ``unsupported`` melden, darf Sol/xhigh eine
     textlesbare Quelle zweimal unabhängig lesen. Jedes Feld wird danach gegen
     genau eine Quellzeile geprüft; Abweichung/Teilbeleg → Review, kein Import.
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
  * KEIN Parser erkennt das Format → `review` + Owner-Mail. Die Originaldatei
    bleibt 14 Tage erhalten, damit wir den Parser ergänzen und denselben Upload
    nachverarbeiten können. KEIN Nutzer-„failed" und KEINE Aufforderung zum
    erneuten Upload: die Datei ist angekommen, nur unser Parser fehlt noch.
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
ROSTER_AI_LEARN_MARKER = "AEROX_ROSTER_AI_LEARN_V1"
ALERT_TO = "aerox@aerosteuer.de"
ALERT_FROM = "noreply@aerosteuer.de"
MAX_BATCH_FILES = 40          # Schutz vor Amok-Uploads in einem Lauf
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REVIEW = "review"      # wartet auf Menschen; terminal, App pollt nicht
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"
MS_MAM_ENCRYPTED_PREFIX = b"\x00MSMAMARPCRYPT\x00"
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
# Zwei Exporte desselben physischen Legs können die Blockzeit an einer
# Sekunden-/Minutengrenze unterschiedlich runden. Bei ansonsten identischem
# Schlüssel ist genau eine Minute deshalb dieselbe Messung; größere
# Abweichungen bleiben ein harter Konflikt und gehen weiterhin in `review`.
MAX_BLOCK_MERGE_DRIFT_MIN = 1

# Unknown, text-readable logbooks get one quality-first extraction attempt
# after the deterministic parser cascade has explicitly returned
# ``unsupported``.  Each chunk is read twice from scratch by Sol.  Nothing is
# persisted unless both source-validated reads are byte-for-byte equivalent.
LOGBOOK_AI_MODEL_DEFAULT = "gpt-5.6-sol"
LOGBOOK_AI_EFFORT_DEFAULT = "xhigh"
LOGBOOK_AI_TIMEOUT_SECONDS = 180
LOGBOOK_AI_MAX_SOURCE_CHARS = 80000
LOGBOOK_AI_CHUNK_CHARS = 24000
LOGBOOK_AI_MAX_CHUNKS = 4
LOGBOOK_AI_MAX_OUTPUT_TOKENS = 48000
LOGBOOK_AI_MAX_FILES_PER_TOKEN_RUN = 3

LOGBOOK_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "legs": {
            "type": "array",
            "maxItems": 400,
            "items": {
                "type": "object",
                "properties": {
                    "date_text": {"type": "string"},
                    "flight_no": {"type": "string"},
                    "from_iata": {"type": "string"},
                    "to_iata": {"type": "string"},
                    "block_time": {"type": "string"},
                    "aircraft_type": {"type": ["string", "null"]},
                    "registration": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "landings_day_text": {"type": ["string", "null"]},
                    "landings_night_text": {"type": ["string", "null"]},
                    "night_time": {"type": ["string", "null"]},
                    "source_evidence": {"type": "string"},
                },
                "required": [
                    "date_text", "flight_no", "from_iata", "to_iata",
                    "block_time", "aircraft_type", "registration", "role",
                    "landings_day_text", "landings_night_text",
                    "night_time", "source_evidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["legs"],
    "additionalProperties": False,
}

LOGBOOK_AI_SYSTEM = """You extract operated flight legs from a crew logbook.
Return raw source facts only; never infer, translate, repair, or calculate a
value. Every returned value must appear literally in the same single source
line copied to source_evidence. Return one item per operated flight row.

Required acceptance facts: an exact date token, flight number, three-letter
origin, three-letter destination, and an exact block/duration token. If any of
those is absent from a row, omit that row. Do not return totals, positioning as
a passenger, private trips, duties, standby, off days, simulators, headings,
or summary lines. Optional aircraft, registration, role, landing counts, and
night time stay null unless printed in that same line. Keep source order.

block_time and night_time must be copied exactly (for example 08:15, 1:32, or
1.5). landing count fields are exact printed integer tokens as strings. Copy
source_evidence as the shortest complete, unchanged source line supporting all
fields. Output only the requested JSON schema."""


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
    # `neq` würde NULL-notes verwerfen. Roster-Quellen UND die erfolgreichen
    # KI-Lernbeispiele gehören nicht in die Flugbuch-Pipeline: das eine ist
    # die Airline-Retry-Queue, das andere bewusst dauerhaftes Lernmaterial.
    q = ("ax_logbook_upload?processed=is.false&status=eq.pending"
         f"&or=(note.is.null,and(note.neq.{ROSTER_MARKER},"
         f"note.neq.{ROSTER_AI_LEARN_MARKER}))"
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


def _is_supported_image(filename, blob):
    """Nur belegte Screenshot-/Bildformate in die manuelle Prüfung geben.

    Die Endung allein ist kein Typbeweis (Upload-Dateinamen sind Nutzereingabe),
    deshalb müssen Endung UND Magic Bytes passen. Bilder werden absichtlich
    nicht zu Flügen halluziniert: sie bleiben 14 Tage im privaten Review-Store,
    bis der Inhalt kontrolliert übernommen wurde.
    """
    name = str(filename or '').lower()
    if name.endswith(('.jpg', '.jpeg')):
        return blob.startswith(b'\xff\xd8\xff')
    if name.endswith('.png'):
        return blob.startswith(b'\x89PNG\r\n\x1a\n')
    if name.endswith(('.heic', '.heif')):
        return (len(blob) >= 12 and blob[4:8] == b'ftyp'
                and any(brand in blob[8:32]
                        for brand in (b'heic', b'heix', b'hevc', b'hevx',
                                      b'heif', b'mif1', b'msf1')))
    if name.endswith('.webp'):
        return (len(blob) >= 12 and blob[:4] == b'RIFF'
                and blob[8:12] == b'WEBP')
    return False


# ── Guarded Sol fallback for unknown logbook layouts ───────────────────────

def _logbook_ai_source_text(path):
    """Return a bounded, line-oriented source representation.

    The evidence guard below accepts facts from one physical source line only.
    PDF text, delimited text and spreadsheet rows can provide that guarantee;
    images and genuinely binary formats deliberately stay in manual review.
    """
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        return None, f"source_read_{type(exc).__name__}"

    try:
        if blob.startswith(b"%PDF-"):
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = [page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                         for page in pdf.pages]
            source = "\n".join(pages)
        elif blob.startswith(b"PK\x03\x04"):
            import openpyxl
            workbook = openpyxl.load_workbook(
                path, read_only=True, data_only=True)
            lines = []
            for sheet in workbook.worksheets:
                for row_number, row in enumerate(
                        sheet.iter_rows(values_only=True), 1):
                    values = []
                    for value in row:
                        if value is None:
                            values.append("")
                        elif isinstance(value, datetime):
                            values.append(value.isoformat(sep=" "))
                        else:
                            values.append(str(value).replace("\n", " ").strip())
                    if any(values):
                        lines.append(
                            f"[{sheet.title}!R{row_number}]\t" + "\t".join(values))
            workbook.close()
            source = "\n".join(lines)
        else:
            source = None
            for encoding in ("utf-8-sig", "utf-16", "cp1252"):
                try:
                    candidate = blob.decode(encoding)
                except UnicodeError:
                    continue
                if "\x00" not in candidate:
                    source = candidate
                    break
            if source is None:
                return None, "source_not_text_readable"
    except Exception as exc:  # format/OCR failures stay private and fail closed
        return None, f"source_extract_{type(exc).__name__}"

    source = "\n".join(line.rstrip() for line in (source or "").splitlines())
    if not source.strip():
        return None, "source_has_no_text"
    if len(source) > LOGBOOK_AI_MAX_SOURCE_CHARS:
        return None, "source_too_large_for_complete_read"
    if max((len(line) for line in source.splitlines()), default=0) > \
            LOGBOOK_AI_CHUNK_CHARS:
        return None, "source_line_too_large"
    return source, None


def _logbook_ai_chunks(source):
    """Split only between lines; later chunks receive a small header copy."""
    lines = source.splitlines()
    header = lines[:12]
    chunks, current = [], []
    current_chars = 0
    for line in lines:
        extra = len(line) + 1
        if current and current_chars + extra > LOGBOOK_AI_CHUNK_CHARS:
            chunks.append("\n".join(current))
            current = list(header)
            current_chars = sum(len(value) + 1 for value in current)
        current.append(line)
        current_chars += extra
    if current:
        chunks.append("\n".join(current))
    return chunks if len(chunks) <= LOGBOOK_AI_MAX_CHUNKS else []


_LOGBOOK_AI_MONTHS = {
    "JAN": 1, "JANUARY": 1, "JANUAR": 1,
    "FEB": 2, "FEBRUARY": 2, "FEBRUAR": 2,
    "MAR": 3, "MARCH": 3, "MAERZ": 3, "MÄRZ": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5, "MAI": 5,
    "JUN": 6, "JUNE": 6, "JUNI": 6,
    "JUL": 7, "JULY": 7, "JULI": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10, "OKT": 10, "OKTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12, "DEZ": 12, "DEZEMBER": 12,
}


def _logbook_ai_parse_date(value, source):
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if match:
        parts = tuple(int(item) for item in match.groups())
        try:
            return datetime(parts[0], parts[1], parts[2]).date().isoformat()
        except ValueError:
            return None

    match = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{4})", raw)
    if match:
        first, separator, second, year = match.groups()
        first, second, year = int(first), int(second), int(year)
        header = source.upper().replace(" ", "")
        month_first = bool(re.search(r"M{1,2}[/.-]D{1,2}[/.-]Y{2,4}", header))
        day_first = bool(re.search(r"D{1,2}[/.-]M{1,2}[/.-]Y{2,4}", header))
        if first > 12:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        elif month_first and not day_first:
            month, day = first, second
        elif day_first and not month_first:
            day, month = first, second
        else:
            return None  # ambiguous numeric date: never guess locale
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    match = re.fullmatch(
        r"(\d{1,2})\s+([A-Za-zÄÖÜäöü]+)\s+(\d{4})", raw)
    if match:
        day, month_name, year = match.groups()
        folded = month_name.upper().replace("Ä", "AE")
        month = (_LOGBOOK_AI_MONTHS.get(month_name.upper())
                 or _LOGBOOK_AI_MONTHS.get(folded))
        if month:
            try:
                return datetime(int(year), month, int(day)).date().isoformat()
            except ValueError:
                return None
    return None


def _logbook_ai_parse_duration(value, source, allow_zero=False):
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)", raw)
    if match:
        minutes = int(match.group(1)) * 60 + int(match.group(2))
    elif re.fullmatch(r"\d{1,2}[.,]\d{1,4}", raw):
        exact = float(raw.replace(",", ".")) * 60
        if abs(exact - round(exact)) > 1e-8:
            return None
        minutes = int(round(exact))
    elif (re.fullmatch(r"\d{1,4}", raw)
          and re.search(r"(?i)\b(?:block|duration|flight\s*time)\b[^\n]{0,30}\bmin",
                        source)):
        minutes = int(raw)
    else:
        return None
    lower = 0 if allow_zero else 1
    return minutes if lower <= minutes <= 24 * 60 else None


def _logbook_ai_response_text(payload):
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if (isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)):
                parts.append(content["text"])
    return "".join(parts)


def _logbook_ai_call_once(source_chunk, token):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("openai_not_configured")
    model = (os.environ.get("AEROX_LOGBOOK_OPENAI_MODEL", "").strip()
             or os.environ.get("AEROX_ROSTER_OPENAI_MODEL", "").strip()
             or LOGBOOK_AI_MODEL_DEFAULT)
    effort = (os.environ.get("AEROX_LOGBOOK_OPENAI_EFFORT", "").strip()
              or os.environ.get("AEROX_ROSTER_OPENAI_EFFORT", "").strip()
              or LOGBOOK_AI_EFFORT_DEFAULT)
    body = {
        "model": model,
        "store": False,
        "safety_identifier": "ax-logbook-" + hashlib.sha256(
            str(token or "").encode()).hexdigest()[:24],
        "reasoning": {"effort": effort},
        "max_output_tokens": LOGBOOK_AI_MAX_OUTPUT_TOKENS,
        "input": [
            {"role": "system", "content": LOGBOOK_AI_SYSTEM},
            {"role": "user", "content": (
                "Read this complete line-bounded logbook chunk independently. "
                "Some document header lines may be repeated between chunks; "
                "do not turn headers into flights.\n\n" + source_chunk)},
        ],
        "text": {"format": {
            "type": "json_schema",
            "name": "aerox_logbook_legs",
            "strict": True,
            "schema": LOGBOOK_AI_SCHEMA,
        }},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(
            request, timeout=LOGBOOK_AI_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    raw = _logbook_ai_response_text(payload)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [], model
    legs = parsed.get("legs") if isinstance(parsed, dict) else None
    return (legs if isinstance(legs, list) else []), model


def _logbook_ai_validate_items(items, source):
    """Validate every returned fact against one exact source line."""
    if not re.search(
            r"(?i)\b(?:block(?:zeit)?|blk|duration|flight\s*time|flugzeit)\b",
            source):
        return [], len(items or [])
    line_positions = {}
    for index, line in enumerate(source.splitlines()):
        normalized = " ".join(line.split())
        if normalized:
            line_positions.setdefault(normalized, index)

    clean, dropped = [], 0
    for raw in (items or [])[:400]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        evidence = " ".join(str(raw.get("source_evidence") or "").split())
        if not evidence or evidence not in line_positions:
            dropped += 1
            continue
        evidence_upper = evidence.upper()
        evidence_tokens = re.findall(r"[A-Z0-9]+", evidence_upper)
        joined_tokens = set(evidence_tokens)
        joined_tokens.update(
            evidence_tokens[index] + evidence_tokens[index + 1]
            for index in range(len(evidence_tokens) - 1))

        exact_fields = ("date_text", "block_time")
        if any(str(raw.get(field) or "").strip() not in evidence
               for field in exact_fields):
            dropped += 1
            continue
        codes = {}
        valid = True
        for field in ("flight_no", "from_iata", "to_iata"):
            value = re.sub(r"\s+", "", str(raw.get(field) or "").upper())
            if not value:
                valid = False
                break
            codes[field] = value
        if not valid:
            dropped += 1
            continue
        if (not re.fullmatch(r"[A-Z0-9]{1,10}", codes["flight_no"])
                or not re.search(r"[A-Z]", codes["flight_no"])
                or not re.search(r"\d", codes["flight_no"])
                or codes["flight_no"] not in joined_tokens
                or not re.fullmatch(r"[A-Z]{3}", codes["from_iata"])
                or codes["from_iata"] not in evidence_tokens
                or not re.fullmatch(r"[A-Z]{3}", codes["to_iata"])
                or codes["to_iata"] not in evidence_tokens
                or codes["from_iata"] == codes["to_iata"]):
            dropped += 1
            continue

        date_iso = _logbook_ai_parse_date(raw.get("date_text"), source)
        block_min = _logbook_ai_parse_duration(raw.get("block_time"), source)
        if not date_iso or block_min is None:
            dropped += 1
            continue

        leg = {
            "date": date_iso,
            "flight": codes["flight_no"],
            "from": codes["from_iata"],
            "to": codes["to_iata"],
            "block_min": block_min,
            "_source_line": line_positions[evidence],
            "_source_evidence": evidence,
        }
        for source_field, target_field in (
                ("aircraft_type", "type"),
                ("registration", "reg"),
                ("role", "role")):
            value = str(raw.get(source_field) or "").strip()
            if value:
                pattern = re.escape(" ".join(value.upper().split()))
                pattern = pattern.replace(r"\ ", r"\s+")
                if not re.search(
                        rf"(?<![A-Z0-9]){pattern}(?![A-Z0-9])",
                        evidence_upper):
                    valid = False
                    break
                leg[target_field] = value.upper()[:32]
        if not valid:
            dropped += 1
            continue

        for source_field, target_field in (
                ("landings_day_text", "ldg_day"),
                ("landings_night_text", "ldg_night")):
            value = str(raw.get(source_field) or "").strip()
            if value:
                if (not re.fullmatch(r"\d{1,3}", value)
                        or not re.search(rf"(?<!\d){re.escape(value)}(?!\d)",
                                         evidence)):
                    valid = False
                    break
                leg[target_field] = int(value)
        if not valid:
            dropped += 1
            continue

        night = str(raw.get("night_time") or "").strip()
        if night:
            if night not in evidence:
                dropped += 1
                continue
            night_min = _logbook_ai_parse_duration(
                night, source, allow_zero=True)
            if night_min is None or night_min > block_min:
                dropped += 1
                continue
            leg["night_min"] = night_min
        clean.append(leg)

    # Without departure timestamps, equal identity keys would collapse during
    # the normal merge. Reject rather than silently lose a repeated sector.
    identities = [(leg["date"], leg["flight"], leg["from"], leg["to"])
                  for leg in clean]
    if len(identities) != len(set(identities)):
        return [], dropped + 1
    return clean, dropped


def _logbook_ai_public_leg(item):
    return {key: value for key, value in item.items()
            if not key.startswith("_source_")}


def _try_openai_logbook(path, token):
    """Return a normal parser tuple or ``(None, private_error_code)``."""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return None, "openai_not_configured"
    source, error = _logbook_ai_source_text(path)
    if error:
        return None, error
    chunks = _logbook_ai_chunks(source)
    if not chunks:
        return None, "source_requires_too_many_chunks"

    combined, model = [], LOGBOOK_AI_MODEL_DEFAULT
    try:
        for chunk in chunks:
            first, model = _logbook_ai_call_once(chunk, token)
            second, _ = _logbook_ai_call_once(chunk, token)
            first_clean, first_dropped = _logbook_ai_validate_items(first, source)
            second_clean, second_dropped = _logbook_ai_validate_items(second, source)
            if first_dropped or second_dropped:
                return None, "source_evidence_rejected"
            first_facts = sorted(json.dumps(item, sort_keys=True)
                                 for item in first_clean)
            second_facts = sorted(json.dumps(item, sort_keys=True)
                                  for item in second_clean)
            if first_facts != second_facts:
                return None, "independent_reads_disagree"
            combined.extend(first_clean)
    except Exception as exc:  # network/provider details stay out of row data
        _log(f"Sol-Flugbuchaufruf fehlgeschlagen: {type(exc).__name__}")
        return None, f"openai_call_{type(exc).__name__}"

    unique = {}
    for item in combined:
        fact = json.dumps(item, sort_keys=True)
        unique[fact] = item
    verified = sorted(unique.values(), key=lambda item: item["_source_line"])
    if not verified:
        return None, "no_verified_flight_legs"
    identities = [(item["date"], item["flight"], item["from"], item["to"])
                  for item in verified]
    if len(identities) != len(set(identities)):
        return None, "duplicate_leg_identity_without_departure_time"

    legs = [_logbook_ai_public_leg(item) for item in verified]
    dates = sorted(leg["date"] for leg in legs)
    first_month, last_month = dates[0][:7], dates[-1][:7]
    report = {
        "month": (first_month if first_month == last_month
                  else f"{first_month}–{last_month}"),
        "document_type": "openai_verified_logbook",
        "model": model,
        "reasoning_effort": (os.environ.get(
            "AEROX_LOGBOOK_OPENAI_EFFORT", "").strip()
            or os.environ.get("AEROX_ROSTER_OPENAI_EFFORT", "").strip()
            or LOGBOOK_AI_EFFORT_DEFAULT),
        "independent_reads": 2,
        "chunks": len(chunks),
        "source_evidence_guard": True,
        "store": False,
        "legs": len(legs),
        "block_min": sum(leg["block_min"] for leg in legs),
        "control": "OPENAI_DOUBLE_READ_SOURCE_VERIFIED",
    }
    return ("openai_verified_logbook", legs, [], report), None


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
    import parse_emirates_cabin_log
    import parse_lh_flugstunden
    import parse_cfg_flugstunden
    import parse_roster_logbook
    import parse_swiss_historical

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
    with open(path, "rb") as handle:
        magic = handle.read(5)
    # openpyxl is intentionally loaded only after the ZIP/XLSX signature was
    # established. A missing optional spreadsheet runtime must never prevent
    # unrelated CSV/PDF imports from reaching their own parser.
    if magic.startswith(b"PK\x03\x04"):
        import parse_edw_xlsx
        if parse_edw_xlsx.matches_workbook(path):
            result, controls = parse_edw_xlsx.parse_workbook(path)
            legs = result["legs"]
            first, last = legs[0]["date"], legs[-1]["date"]
            report = dict(controls)
            report["month"] = (first[:7] if first[:7] == last[:7]
                               else f"{first[:7]}–{last[:7]}")
            return "edelweiss_xlsx", legs, result["sim"], report
        return "unsupported", None, None, None
    if not magic.startswith(b"%PDF-"):
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
    if parse_swiss_historical.matches_pdf(path):
        legs, sims, report = parse_swiss_historical.parse_pdf(path)
        parser_name = ("informational_pdf"
                       if report.get("document_type") == "zero_flight_roster"
                       else "swiss_historical_roster")
        return parser_name, legs, sims, report
    if parse_emirates_cabin_log.matches_pdf(path):
        legs, sims, report = parse_emirates_cabin_log.parse_pdf(path)
        return "emirates_cabin_log", legs, sims, report
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
    """Identität eines Legs: Datum + Flugnummer + Strecke (+ Abflugzeit).

    Für Legs OHNE `dep_iso` (Alt-Importe, FAA-Layouts, Condor-Historie) stand
    hier früher die BLOCKZEIT im Schlüssel. Damit war jede Minute Rundungs-
    differenz eine neue Identität: derselbe Flug mit 500 statt 501 Minuten kam
    beim nächsten Upload als ZWEITES Leg dazu — still, ohne Konflikt, mit
    doppelten Landungen im Flugbuch. Die Blockzeit ist eine MESSUNG, keine
    Identität. Sie gehört deshalb in die Toleranzprüfung von `merge_legs`
    (MAX_BLOCK_MERGE_DRIFT_MIN), nicht in den Schlüssel: kleine Drift heißt
    „dasselbe Leg, Bestand gewinnt", große Drift heißt Konflikt → `review`.
    """
    return (leg.get("date"), _base_flight(leg.get("flight")), leg.get("from"),
            leg.get("to"), leg.get("dep_iso") or "")


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
    identische Schlüssel mit mehr als einer Minute Blockzeit-Abweichung sind
    ein Konflikt → ValueError (Aufrufer schickt den Batch in `review`)."""
    by_key = {}
    facts = defaultdict(list)
    for leg in existing or []:
        by_key[_leg_key(leg)] = leg
        facts[_fact_key(leg)].append(leg)
    added = 0
    for leg in new:
        key = _leg_key(leg)
        if key in by_key:
            old_block = by_key[key].get("block_min")
            new_block = leg.get("block_min")
            same_measurement = (
                isinstance(old_block, int)
                and isinstance(new_block, int)
                and abs(old_block - new_block) <= MAX_BLOCK_MERGE_DRIFT_MIN
            )
            if old_block != new_block and not same_measurement:
                raise ValueError(
                    f"Merge-Konflikt {key}: block {old_block} != {new_block}")
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
    parsed_files, unsupported, encrypted, seen_sha = [], [], [], {}
    unsupported_reasons = {}
    ai_deferred = []
    ai_attempts = 0
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
        if blob.startswith(MS_MAM_ENCRYPTED_PREFIX):
            # Microsoft Intune/MAM company protection wraps an otherwise
            # ordinary-looking '.pdf' in ciphertext. This is permanent for
            # the server (the employer key is intentionally unavailable), so
            # retaining it in operator review cannot unlock a parser fix.
            encrypted.append(rid)
            continue
        if _is_supported_image(row.get("filename"), blob):
            review.append((
                rid,
                "Screenshot/Bild ist angekommen und wartet auf manuelle "
                "Flugbuch-Prüfung."))
            continue
        # Format-Routing arbeitet mit Content-Signaturen. Die neutrale Endung
        # verhindert zusätzlich, dass Aufrufer sie versehentlich als Typbeleg
        # missverstehen.
        with tempfile.NamedTemporaryFile(suffix=".upload", delete=False) as tmp:
            tmp.write(blob)
            path = tmp.name
        try:
            name, legs, sims, report = _try_parsers(path)
            if name == "unsupported":
                if ai_attempts >= LOGBOOK_AI_MAX_FILES_PER_TOKEN_RUN:
                    ai_deferred.append(rid)
                else:
                    ai_attempts += 1
                    ai_result, ai_error = _try_openai_logbook(path, token)
                    if ai_result:
                        ai_name, ai_legs, ai_sims, ai_report = ai_result
                        parsed_files.append({
                            "id": rid, "parser": ai_name, "legs": ai_legs,
                            "sims": ai_sims, "report": ai_report,
                        })
                    else:
                        unsupported.append(rid)
                        unsupported_reasons[rid] = ai_error or "unknown"
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
        messages = {}
        for rid in unsupported:
            reason = unsupported_reasons.get(rid, "unknown")
            message = (
                "Dateiformat wird noch nicht unterstützt; die Sol-"
                "Doppelprüfung konnte keine vollständig belegten Legs "
                f"freigeben ({reason}). Originaldatei wurde für die Parser-"
                "Erweiterung aufbewahrt."
            )
            messages[rid] = message
            _status([rid], STATUS_REVIEW, processed=False,
                    error_code="unsupported_format", error_message=message)
        review_by_id.update(messages)
        events.append(("review", token, unsupported,
                       "; ".join(f"#{rid}: {messages[rid]}"
                                 for rid in unsupported)))

    if ai_deferred:
        _status(ai_deferred, STATUS_PENDING, processed=False)
        events.append(("deferred", token, ai_deferred,
                       "Sol-Kostenlimit dieses Laufs; nächster Cron-Lauf"))

    # Byte-Dubletten erben das Schicksal ihres ORIGINALS — eine Kopie eines
    # noch unbekannten Formats wartet ebenfalls auf `review`, statt fälschlich
    # `completed` oder endgültig `failed` zu werden. Original
    # außerhalb dieses Laufs (früher verarbeitet) ⇒ Inhalt ist erledigt ⇒
    # completed ohne Push.
    failed_ids = set(encrypted)
    failed_dups = []
    if dups:
        real_ids = {f["id"] for f in real + informational}
        review_ids = set(review_by_id)
        deferred_ids = set(ai_deferred)
        failed_dups = [f["id"] for f in dups
                       if f["dup_of"] in failed_ids]
        review_dups = [f["id"] for f in dups if f["dup_of"] in review_ids]
        deferred_dups = [f["id"] for f in dups
                         if f["dup_of"] in deferred_ids]
        done_dups = [f["id"] for f in dups
                     if f["dup_of"] in real_ids or f["dup_of"] not in
                     set(unsupported) | real_ids | review_ids | failed_ids
                     | deferred_ids]
        if review_dups:
            _status(review_dups, STATUS_REVIEW, processed=False,
                    error_code="needs_review",
                    error_message="Identische Datei wartet bereits auf Prüfung.")
        if deferred_dups:
            _status(deferred_dups, STATUS_PENDING, processed=False)
        if done_dups:
            _status(done_dups, STATUS_COMPLETED, processed=True)
        events.append(("dups", token,
                       {"failed": failed_dups, "review": review_dups,
                        "deferred": deferred_dups,
                        "completed": done_dups}, ""))

    failed = sorted(encrypted + failed_dups)
    if failed:
        message = (
            "Die Datei ist durch Microsoft-Unternehmensschutz verschlüsselt. "
            "Bitte eine ungeschützte PDF aus Dateien/Downloads exportieren "
            "und erneut hochladen."
        )
        _status(failed, STATUS_FAILED, processed=True,
                error_code="encrypted_document", error_message=message)
        _push_failed(token, max(failed))
        events.append(("failed", token, failed, message))

    # Unbekannte Formate sind kein Nutzerfehler. Sie bleiben samt Payload im
    # Review und erzeugen deshalb bewusst keinen „bitte erneut hochladen“-Push.


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
