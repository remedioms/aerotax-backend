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
     Batch aussortieren, dann Format-Erkennung über die STRIKTEN Parser
     (LH-Flugstundenübersicht Cockpit+Kabine, Condor/CFG). Jeder Parser prüft
     seine Monatssummen selbst und bricht bei Abweichung ab — der Wächter
     erbt diese Garantien, statt eigene Leseheuristik zu erfinden.
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
  * KEIN Parser erkennt das Format → ehrliches `failed` + Push an den Nutzer
    („Datei enthält keine einzelnen Flüge / Format unbekannt") + Owner-Mail.
  * Jede unerwartete Exception → Zeile bleibt `pending` (nächster Lauf
    versucht es erneut), Owner-Mail mit Traceback-Kopf.

Idempotenz: Alle Schritte sind wiederholbar — der Merge ist eine Union, der
Push läuft über `enqueue_push_outbox` mit fester Idempotenz-Key
(`logbook-import-completed:<upload_id>` wie beim manuellen Werkzeug).
"""

import base64
import hashlib
import json
import os
import sys
import tempfile
import traceback
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROSTER_MARKER = "AEROX_ROSTER_PDF_V1"
ALERT_TO = "aerox@aerosteuer.de"
ALERT_FROM = "noreply@aerosteuer.de"
MAX_BATCH_FILES = 40          # Schutz vor Amok-Uploads in einem Lauf
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REVIEW = "review"      # wartet auf Menschen; App pollt weiter „queued"
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"


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


def _set_status(ids, status, processed=None):
    if not ids:
        return
    payload = {"status": status}
    if processed is not None:
        payload["processed"] = processed
    if status in (STATUS_COMPLETED, STATUS_FAILED):
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    id_list = ",".join(str(i) for i in ids)
    _rest("PATCH", f"ax_logbook_upload?id=in.({id_list})", payload,
          expect_json=False)


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
    import parse_lh_flugstunden
    import parse_cfg_flugstunden
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not parse_lh_flugstunden.RE_HEADER.search(text):
        return "unsupported", None, None, None
    if "Condor" in text:
        name, mod = "cfg_flugstunden", parse_cfg_flugstunden
    else:
        name, mod = "lh_flugstunden", parse_lh_flugstunden
    legs, sims, report = mod.parse_pdf(path)
    return name, legs, sims, report


# ── Merge mit bestehendem Import ────────────────────────────────────────────

def _leg_key(leg):
    return (leg.get("date"), leg.get("flight"), leg.get("from"),
            leg.get("to"), leg.get("dep_iso") or f"block:{leg.get('block_min')}")


def _sim_key(sim):
    return (sim.get("date"), sim.get("code"), sim.get("duration_min"))


def merge_legs(existing, new):
    """Union über Leg-Schlüssel. Bestehende Zeilen gewinnen unverändert;
    identische Schlüssel mit ABWEICHENDER Blockzeit sind ein Konflikt →
    ValueError (Aufrufer schickt den Batch in `review`)."""
    by_key = {}
    for leg in existing or []:
        by_key[_leg_key(leg)] = leg
    added = 0
    for leg in new:
        key = _leg_key(leg)
        if key in by_key:
            if by_key[key].get("block_min") != leg.get("block_min"):
                raise ValueError(
                    f"Merge-Konflikt {key}: block {by_key[key].get('block_min')}"
                    f" != {leg.get('block_min')}")
            continue
        by_key[key] = leg
        added += 1
    merged = sorted(by_key.values(),
                    key=lambda l: (l.get("date") or "", l.get("dep_iso") or "",
                                   l.get("flight") or ""))
    return merged, added


def merge_sims(existing, new):
    by_key = {_sim_key(s): s for s in (existing or [])}
    for sim in new or []:
        by_key.setdefault(_sim_key(sim), sim)
    return sorted(by_key.values(),
                  key=lambda s: (s.get("date") or "", s.get("code") or ""))


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


def _push_unsupported(token, anchor_upload_id):
    payload = {
        "title": "Flugbuch-Import: Datei nicht geeignet",
        "body": ("Die hochgeladene Datei enthält keine einzelnen Flüge "
                 "(oder ein unbekanntes Format). Bitte lade einen Export mit "
                 "Datum, Flugnummer und Strecke pro Flug hoch."),
        "data": {"type": "logbook_import_failed",
                 "job_id": anchor_upload_id,
                 "deep_link": "aerox://more/logbook"},
    }
    _rest("POST", "rpc/enqueue_push_outbox", {
        "p_idempotency_key": f"logbook-import-failed:{anchor_upload_id}",
        "p_user_token": token, "p_payload": payload,
    }, expect_json=False)


# ── Batch-Verarbeitung ──────────────────────────────────────────────────────

def process_token_batch(token, rows, events):
    ids = [r["id"] for r in rows]
    _set_status(ids, STATUS_PROCESSING)
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
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
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

    if review:
        # Ein einziger unklarer Fall reißt den ganzen Batch in die manuelle
        # Prüfung — Teilimporte neben offenen Fragen sind schwerer zu
        # entwirren als ein sauber liegender Batch.
        _set_status(ids, STATUS_REVIEW)
        events.append(("review", token, ids,
                       "; ".join(f"#{i}: {m}" for i, m in review)))
        return

    real = [f for f in parsed_files if "parser" in f]
    dups = [f for f in parsed_files if "dup_of" in f]

    if real:
        new_legs = [leg for f in real for leg in f["legs"]]
        new_sims = [sim for f in real for sim in f["sims"]]
        existing = _rest("GET", f"ax_logbook_import?token=eq.{token}"
                                "&select=legs,sim,meta") or []
        old_legs = existing[0]["legs"] if existing else []
        old_sims = (existing[0].get("sim") or []) if existing else []
        try:
            merged_legs, added = merge_legs(old_legs, new_legs)
        except ValueError as ex:
            _set_status(ids, STATUS_REVIEW)
            events.append(("review", token, ids, str(ex)))
            return
        merged_sims = merge_sims(old_sims, new_sims)
        months = sorted({f["report"]["month"] for f in real})
        label = (f"Watchdog: {real[0]['parser']} {months[0]}–{months[-1]}"
                 if len(months) > 1 else
                 f"Watchdog: {real[0]['parser']} {months[0]}")
        prev_meta = (existing[0].get("meta") or {}) if existing else {}
        if prev_meta.get("source"):
            label = f"{prev_meta['source']} + {label}"
        meta = _meta_for(merged_legs, merged_sims, label, {
            "watchdog": {"upload_ids": [f["id"] for f in real],
                         "duplicates_skipped": [f["id"] for f in dups],
                         "added_legs": added,
                         "ts": datetime.now(timezone.utc).isoformat()},
        })
        _upsert_import(token, merged_legs, merged_sims, meta)
        _set_status([f["id"] for f in real], STATUS_COMPLETED, processed=True)
        _push_completed(token, max(f["id"] for f in real))
        events.append(("imported", token, [f["id"] for f in real],
                       f"+{added} Legs (gesamt {len(merged_legs)})"))

    if unsupported:
        _set_status(unsupported, STATUS_FAILED, processed=True)
        _push_unsupported(token, max(unsupported))
        events.append(("unsupported", token, unsupported, "Format unbekannt"))

    # Byte-Dubletten erben das Schicksal ihres ORIGINALS — eine Kopie einer
    # unbrauchbaren Datei ist genauso `failed`, nicht still `completed`
    # (Erst-Einsatz 12.08.: #286 = Kopie der unsupported #285). Original
    # außerhalb dieses Laufs (früher verarbeitet) ⇒ Inhalt ist erledigt ⇒
    # completed ohne Push.
    if dups:
        real_ids = {f["id"] for f in real}
        failed_dups = [f["id"] for f in dups if f["dup_of"] in set(unsupported)]
        done_dups = [f["id"] for f in dups
                     if f["dup_of"] in real_ids or f["dup_of"] not in
                     set(unsupported) | real_ids]
        if failed_dups:
            _set_status(failed_dups, STATUS_FAILED, processed=True)
        if done_dups:
            _set_status(done_dups, STATUS_COMPLETED, processed=True)
        events.append(("dups", token,
                       {"failed": failed_dups, "completed": done_dups}, ""))


def main():
    _recover_stale_processing()
    rows = _pending_rows()
    if not rows:
        _log("nichts offen")
        return
    rows = rows[:MAX_BATCH_FILES]
    by_token = defaultdict(list)
    for row in rows:
        by_token[row["token"]].append(row)
    _log(f"{len(rows)} offene Datei(en) von {len(by_token)} Nutzer(n)")

    events = []
    for token, batch in by_token.items():
        try:
            process_token_batch(token, batch, events)
        except Exception:  # noqa: BLE001
            # Unerwartet → Zeilen zurück auf pending (nächster Lauf), Alarm.
            _set_status([r["id"] for r in batch], STATUS_PENDING)
            head = traceback.format_exc(limit=6)
            events.append(("error", token, [r["id"] for r in batch], head))

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
