"""Batch-Orchestrierung des Flugbuch-Wächters — netzwerkfrei durchgespielt.

`process_token_batch` entscheidet über Import / `review` / `failed` und
darüber, WER einen Push bekommt. Der Erst-Einsatz am 12.08. fand genau hier
einen echten Fehler (Byte-Dublette einer unbrauchbaren Datei wurde
`completed` statt `failed`) — diese Tests spielen die vier Ausgänge komplett
im Speicher durch, mit denselben Funktionen, die live laufen.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

import logbook_watchdog as w  # noqa: E402


LEG_A = {"date": "2026-02-06", "flight": "LH1642", "from": "MUC", "to": "GDN",
         "dep_iso": "2026-02-06T10:26:00Z", "block_min": 92}
LEG_B = {"date": "2026-02-06", "flight": "LH1643", "from": "GDN", "to": "MUC",
         "dep_iso": "2026-02-06T12:28:00Z", "block_min": 96}
REPORT = {"month": "2026-02"}


class Harness:
    """Fängt alle Außenwelt-Aufrufe des Wächters ab und protokolliert sie."""

    def __init__(self, monkeypatch, files, existing_import=None):
        # upload_id → bytes | ("parsed", legs) | "unsupported" | "control"
        self.files = files
        self.existing = existing_import
        self.status_calls = []      # (ids, status, processed)
        self.upserts = []           # (token, legs, sims, meta)
        self.pushes = []            # ("completed"|"failed", token, anchor)
        self.cache_busts = []       # Token, deren Lese-Cache gelöscht wurde
        monkeypatch.setattr(w, "_bust_import_cache",
                            lambda token: self.cache_busts.append(token))
        monkeypatch.setattr(w, "_set_status",
                            lambda ids, status, processed=None,
                            error_code=None, error_message=None:
                            self.status_calls.append(
                                (sorted(ids), status, processed,
                                 error_code, error_message)))
        monkeypatch.setattr(w, "_download", lambda rid: self.files[rid][0])
        monkeypatch.setattr(w, "_try_parsers", self._parse)
        monkeypatch.setattr(w, "_rest", self._rest)
        monkeypatch.setattr(w, "_upsert_import",
                            lambda token, legs, sims, meta:
                            self.upserts.append((token, legs, sims, meta)))
        monkeypatch.setattr(w, "_push_completed",
                            lambda token, anchor:
                            self.pushes.append(("completed", token, anchor)))
        monkeypatch.setattr(w, "_push_failed",
                            lambda token, anchor:
                            self.pushes.append(("failed", token, anchor)))

    def _parse(self, path):
        blob = open(path, "rb").read()
        for _blob, outcome in self.files.values():
            if _blob == blob:
                if outcome == "unsupported":
                    return "unsupported", None, None, None
                if outcome == "control":
                    raise ValueError("Effektiv 0 != PDF 123 min")
                if outcome == "informational":
                    return ("informational_pdf", [], [],
                            {"month": "flight-time-statistics"})
                return "lh_flugstunden", list(outcome), [], dict(REPORT)
        raise AssertionError("unbekannter Blob im Test")

    def _rest(self, method, path, payload=None, headers=None,
              expect_json=True):
        assert method == "GET" and path.startswith("ax_logbook_import")
        return ([{"legs": self.existing, "sim": [], "meta": {}}]
                if self.existing is not None else [])

    def statuses_for(self, rid):
        return [(s, p) for ids, s, p, *_ in self.status_calls if rid in ids]


import hashlib


def _row(rid, blob=None, sha=None):
    """Upload-Zeile mit ECHTER Prüfsumme des Blobs — der Wächter prüft sie
    (Fail-safe 3), eine Fantasie-Prüfsumme schickt sonst alles in `review`."""
    if sha is None:
        sha = hashlib.sha256(blob).hexdigest() if blob is not None else None
    return {"id": rid, "token": "AT-TEST", "sha256": sha, "filename": "f.pdf"}


def test_happy_path_imports_and_completes(monkeypatch):
    h = Harness(monkeypatch, {1: (b"pdf-1", [LEG_A]), 2: (b"pdf-2", [LEG_B])})
    events = []
    w.process_token_batch("AT-TEST", [_row(1, b"pdf-1"), _row(2, b"pdf-2")], events)
    assert len(h.upserts) == 1
    _, legs, _, meta = h.upserts[0]
    assert len(legs) == 2 and meta["watchdog"]["added_legs"] == 2
    assert h.statuses_for(1)[-1] == (w.STATUS_COMPLETED, True)
    assert h.pushes == [("completed", "AT-TEST", 2)]
    assert events[0][0] == "imported"
    # Ohne Cache-Löschen zeigt die App nach dem Push bis zu 6 h den alten Stand.
    assert h.cache_busts == ["AT-TEST"]


def test_dup_of_unsupported_inherits_failed(monkeypatch):
    # Regression #286: Kopie einer unbrauchbaren Datei muss `failed` sein.
    h = Harness(monkeypatch, {5: (b"same", "unsupported"),
                              6: (b"same", "unsupported")})
    events = []
    w.process_token_batch("AT-TEST", [_row(5, b"same"), _row(6, b"same")], events)
    assert h.statuses_for(5)[-1] == (w.STATUS_FAILED, True)
    assert h.statuses_for(6)[-1] == (w.STATUS_FAILED, True)
    # Datei + Byte-Kopie sind EIN Problem → EIN Fehler-Push, Anker = höchste ID.
    assert h.pushes == [("failed", "AT-TEST", 6)]
    assert not h.upserts


def test_merge_conflict_sends_whole_batch_to_review(monkeypatch):
    clash = dict(LEG_A, block_min=999)
    h = Harness(monkeypatch, {7: (b"pdf-7", [clash])},
                existing_import=[LEG_A])
    events = []
    w.process_token_batch("AT-TEST", [_row(7, b"pdf-7")], events)
    assert h.statuses_for(7)[-1] == (w.STATUS_REVIEW, False)
    assert not h.upserts and not h.pushes
    assert events == [("review", "AT-TEST", [7],
                       events[0][3])] and "Merge-Konflikt" in events[0][3]


def test_control_violation_goes_to_review_not_user_failed(monkeypatch):
    h = Harness(monkeypatch, {8: (b"pdf-8", "control")})
    events = []
    w.process_token_batch("AT-TEST", [_row(8, b"pdf-8")], events)
    assert h.statuses_for(8)[-1] == (w.STATUS_REVIEW, False)
    assert not h.pushes and not h.upserts
    assert events[0][0] == "review"


def test_informational_pdf_completes_without_fake_import_or_push(monkeypatch):
    h = Harness(monkeypatch, {18: (b"stats", "informational")})
    events = []
    w.process_token_batch("AT-TEST", [_row(18, b"stats")], events)
    assert h.statuses_for(18)[-1] == (w.STATUS_COMPLETED, True)
    assert not h.upserts and not h.pushes
    assert events == [("informational", "AT-TEST", [18],
                       "erkannt; enthält keine einzelnen Flugbuch-Legs")]


def test_fcl_carryover_is_persisted_in_import_meta(monkeypatch):
    h = Harness(monkeypatch, {40: (b"fcl", [LEG_A])})
    monkeypatch.setattr(
        w, "_try_parsers",
        lambda _path: ("offblock_fcl050", [LEG_A], [],
                       {"month": "2012-01–2026-08",
                        "carryover_min": 634764}))
    events = []
    w.process_token_batch("AT-TEST", [_row(40, b"fcl")], events)
    assert h.upserts[0][3]["carryover_min"] == 634764


def test_faa_landing_carryover_is_persisted_in_import_meta(monkeypatch):
    h = Harness(monkeypatch, {42: (b"faa", [LEG_A])})
    monkeypatch.setattr(
        w, "_try_parsers",
        lambda _path: ("offblock_faa", [LEG_A], [], {
            "month": "2012-01–2026-08",
            "carryover_min": 634764,
            "carryover_ldg_day": 1050,
            "carryover_ldg_night": 0,
            "carryover_landings": 1050,
        }))
    events = []
    w.process_token_batch("AT-TEST", [_row(42, b"faa")], events)
    meta = h.upserts[0][3]
    assert meta["carryover_min"] == 634764
    assert meta["carryover_ldg_day"] == 1050
    assert meta["carryover_ldg_night"] == 0
    assert meta["carryover_landings"] == 1050


def test_fcl_carryover_conflict_goes_to_review(monkeypatch):
    h = Harness(monkeypatch, {41: (b"fcl", [LEG_A])},
                existing_import=[])
    monkeypatch.setattr(
        w, "_try_parsers",
        lambda _path: ("offblock_fcl050", [LEG_A], [],
                       {"month": "2012-01–2026-08",
                        "carryover_min": 634764}))
    monkeypatch.setattr(
        w, "_rest",
        lambda *_args, **_kwargs: [{"legs": [], "sim": [],
                                    "meta": {"carryover_min": 1}}])
    events = []
    w.process_token_batch("AT-TEST", [_row(41, b"fcl")], events)
    assert h.statuses_for(41)[-1] == (w.STATUS_REVIEW, False)
    assert not h.upserts and not h.pushes
    assert "FCL.050" in events[0][3]


def test_sha_mismatch_goes_to_review(monkeypatch):
    wrong = hashlib.sha256(b"anderes").hexdigest()
    h = Harness(monkeypatch, {9: (b"pdf-9", [LEG_A])})
    events = []
    w.process_token_batch("AT-TEST", [_row(9, sha=wrong)], events)
    assert h.statuses_for(9)[-1] == (w.STATUS_REVIEW, False)
    assert not h.upserts and not h.pushes


def test_reader_key_collision_is_numbered_before_upsert(monkeypatch):
    # Zwei Legs, die sich NUR in der Abflugzeit unterscheiden: der Leser im
    # Backend schlüsselt gröber (date|flight|from|to) und behielte nur das
    # letzte — die Landungen des ersten wären still weg.
    twin = dict(LEG_A, dep_iso="2026-02-06T18:26:00Z", block_min=94)
    h = Harness(monkeypatch, {12: (b"pdf-12", [LEG_A, twin])})
    events = []
    w.process_token_batch("AT-TEST", [_row(12, b"pdf-12")], events)
    _, legs, _, meta = h.upserts[0]
    assert [l["flight"] for l in legs] == ["LH1642", "LH1642(2)"]
    assert meta["watchdog"]["dedupe_suffixes"] == 1


def test_cache_bust_removes_the_positive_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "_USER_HISTORY_DIR", str(tmp_path))
    cached = tmp_path / "logbook_import_AT-TEST.json"
    cached.write_text("{}")
    w._bust_import_cache("AT-TEST")
    assert not cached.exists()
    w._bust_import_cache("AT-TEST")     # fehlende Datei ist kein Fehler


def test_crash_after_completion_leaves_finished_rows_alone(monkeypatch):
    # Die Notbremse setzte ALLE Zeilen des Batches auf `pending` zurück —
    # auch schon `completed`+`processed`. Die holt `_pending_rows` nie wieder
    # (processed=is.false), die App zeigte sie für immer als „in Arbeit".
    resets = []
    monkeypatch.setattr(w, "_recover_stale_processing", lambda: None)
    monkeypatch.setattr(w, "_purge_expired_payloads", lambda: None)
    monkeypatch.setattr(w, "_pending_rows",
                        lambda: [_row(1, b"a"), _row(2, b"b")])
    monkeypatch.setattr(w, "_set_status",
                        lambda ids, status, processed=None,
                        error_code=None, error_message=None:
                        resets.append((sorted(ids), status, processed)))
    monkeypatch.setattr(w, "_alert", lambda *args: None)

    def _boom(token, rows, events, terminal=None):
        terminal.add(1)                 # #1 ist verifiziert abgeschlossen
        raise RuntimeError("Push kaputt")

    monkeypatch.setattr(w, "process_token_batch", _boom)
    w.main()
    assert resets == [([2], w.STATUS_PENDING, None)]


def test_batch_cap_never_splits_one_users_upload_group(monkeypatch):
    # Mitten in einer Token-Gruppe zu kappen verteilt EINEN Upload-Schwung auf
    # zwei Läufe — der Nutzer bekäme zwei „Import fertig"-Pushes.
    rows = ([dict(_row(i), token="AT-A") for i in range(1, 31)]
            + [dict(_row(i), token="AT-B") for i in range(31, 51)])
    seen = []
    monkeypatch.setattr(w, "_recover_stale_processing", lambda: None)
    monkeypatch.setattr(w, "_purge_expired_payloads", lambda: None)
    monkeypatch.setattr(w, "_pending_rows", lambda: rows)
    monkeypatch.setattr(w, "_alert", lambda *args: None)
    monkeypatch.setattr(w, "process_token_batch",
                        lambda token, batch, events, terminal=None:
                        seen.append((token, len(batch))))
    w.main()
    assert seen == [("AT-A", 30)]       # AT-B kommt komplett im nächsten Lauf


def test_batch_cap_still_runs_a_single_oversized_group(monkeypatch):
    rows = [dict(_row(i), token="AT-A") for i in range(1, 60)]
    seen = []
    monkeypatch.setattr(w, "_recover_stale_processing", lambda: None)
    monkeypatch.setattr(w, "_purge_expired_payloads", lambda: None)
    monkeypatch.setattr(w, "_pending_rows", lambda: rows)
    monkeypatch.setattr(w, "_alert", lambda *args: None)
    monkeypatch.setattr(w, "process_token_batch",
                        lambda token, batch, events, terminal=None:
                        seen.append((token, len(batch))))
    w.main()
    assert seen == [("AT-A", 59)]       # sonst bliebe die Gruppe ewig liegen


def test_mixed_batch_imports_good_and_fails_unsupported(monkeypatch):
    h = Harness(monkeypatch, {10: (b"pdf-10", [LEG_A]),
                              11: (b"pdf-11", "unsupported")})
    events = []
    w.process_token_batch("AT-TEST", [_row(10, b"pdf-10"), _row(11, b"pdf-11")], events)
    assert h.statuses_for(10)[-1] == (w.STATUS_COMPLETED, True)
    assert h.statuses_for(11)[-1] == (w.STATUS_FAILED, True)
    kinds = {p[0] for p in h.pushes}
    assert kinds == {"completed", "failed"}


def test_mixed_batch_imports_good_while_bad_file_waits_for_review(monkeypatch):
    """Ein einzelner defekter Monat blockiert nicht mehr den ganzen Upload."""
    h = Harness(monkeypatch, {13: (b"good", [LEG_A]),
                              14: (b"bad", "control")})
    events = []
    w.process_token_batch(
        "AT-TEST", [_row(13, b"good"), _row(14, b"bad")], events)
    assert len(h.upserts) == 1
    assert h.upserts[0][1] == [LEG_A]
    assert h.statuses_for(13)[-1] == (w.STATUS_COMPLETED, True)
    assert h.statuses_for(14)[-1] == (w.STATUS_REVIEW, False)
    assert h.pushes == [("completed", "AT-TEST", 13)]


def test_duplicate_of_merge_conflict_inherits_review(monkeypatch):
    clash = dict(LEG_A, block_min=999)
    h = Harness(monkeypatch, {15: (b"same-clash", [clash]),
                              16: (b"same-clash", [clash])},
                existing_import=[LEG_A])
    events = []
    w.process_token_batch(
        "AT-TEST", [_row(15, b"same-clash"), _row(16, b"same-clash")],
        events)
    assert h.statuses_for(15)[-1] == (w.STATUS_REVIEW, False)
    assert h.statuses_for(16)[-1] == (w.STATUS_REVIEW, False)
    assert not h.upserts and not h.pushes


# ── Fehler-Push („bitte nochmal hochladen") ────────────────────────────────

def test_unsupported_batch_gets_exactly_one_failure_push(monkeypatch):
    """Drei unlesbare Dateien in einem Schub = EIN Push, Anker = höchste ID."""
    h = Harness(monkeypatch, {20: (b"a", "unsupported"),
                              21: (b"b", "unsupported"),
                              22: (b"c", "unsupported")})
    events = []
    w.process_token_batch("AT-TEST", [_row(20, b"a"), _row(21, b"b"),
                                      _row(22, b"c")], events)
    assert h.pushes == [("failed", "AT-TEST", 22)]
    for rid in (20, 21, 22):
        assert h.statuses_for(rid)[-1] == (w.STATUS_FAILED, True)


def test_review_never_pushes_upload_again(monkeypatch):
    """`review` heißt: der Betreiber schaut. „Bitte nochmal hochladen" wäre
    dort schlicht falsch — die Datei ist womöglich völlig in Ordnung."""
    h = Harness(monkeypatch, {30: (b"pdf-30", "control")})
    events = []
    w.process_token_batch("AT-TEST", [_row(30, b"pdf-30")], events)
    assert h.statuses_for(30)[-1] == (w.STATUS_REVIEW, False)
    assert h.pushes == []


def test_failure_push_payload_is_short_keyed_and_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "_rest",
                        lambda method, path, payload=None, headers=None,
                        expect_json=True: calls.append((method, path, payload)))
    w._push_failed("AT-TEST", 42)
    assert len(calls) == 1
    method, path, payload = calls[0]
    assert (method, path) == ("POST", "rpc/enqueue_push_outbox")
    assert payload["p_idempotency_key"] == "logbook-import-failed:42"
    assert payload["p_user_token"] == "AT-TEST"
    body = payload["p_payload"]
    assert body["title"] == "Flugbuch-Import fehlgeschlagen"
    assert body["body"] == "Bitte lade die Datei noch einmal hoch."
    assert body["data"] == {"type": "logbook_import_failed",
                            "localization_key": "logbook_import_failed",
                            "deep_link": "aerox://more/logbook"}
    # Keine wandernden Felder im `data` — sonst hebelt der Hash in
    # app._push_outbox_key die Dedupe aus, sobald der Payload dort durchläuft.
    assert "job_id" not in body["data"]


def test_completed_push_is_unchanged_and_separate(monkeypatch):
    """Regel 3 der Nacharbeit: den Fertig-Push nur verifizieren, nicht doppeln."""
    calls = []
    monkeypatch.setattr(w, "_rest",
                        lambda method, path, payload=None, headers=None,
                        expect_json=True: calls.append(payload))
    w._push_completed("AT-TEST", 7)
    assert calls[0]["p_idempotency_key"] == "logbook-import-completed:7"
    assert calls[0]["p_payload"]["data"]["localization_key"] == (
        "logbook_import_completed")


def test_retention_query_uses_url_safe_utc_timestamp(monkeypatch):
    calls = []
    monkeypatch.setattr(
        w, "_rest",
        lambda method, path, payload=None, headers=None, expect_json=True:
        calls.append((method, path, payload)),
    )

    w._purge_expired_payloads()

    assert len(calls) == 2
    review_path = calls[1][1]
    assert "purge_after=lt." in review_path
    assert "+" not in review_path
    assert review_path.endswith("Z")
