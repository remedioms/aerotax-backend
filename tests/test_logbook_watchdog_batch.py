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
        self.pushes = []            # ("completed"|"unsupported", token, anchor)
        monkeypatch.setattr(w, "_set_status",
                            lambda ids, status, processed=None:
                            self.status_calls.append(
                                (sorted(ids), status, processed)))
        monkeypatch.setattr(w, "_download", lambda rid: self.files[rid][0])
        monkeypatch.setattr(w, "_try_parsers", self._parse)
        monkeypatch.setattr(w, "_rest", self._rest)
        monkeypatch.setattr(w, "_upsert_import",
                            lambda token, legs, sims, meta:
                            self.upserts.append((token, legs, sims, meta)))
        monkeypatch.setattr(w, "_push_completed",
                            lambda token, anchor:
                            self.pushes.append(("completed", token, anchor)))
        monkeypatch.setattr(w, "_push_unsupported",
                            lambda token, anchor:
                            self.pushes.append(("unsupported", token, anchor)))

    def _parse(self, path):
        blob = open(path, "rb").read()
        for _blob, outcome in self.files.values():
            if _blob == blob:
                if outcome == "unsupported":
                    return "unsupported", None, None, None
                if outcome == "control":
                    raise ValueError("Effektiv 0 != PDF 123 min")
                return "lh_flugstunden", list(outcome), [], dict(REPORT)
        raise AssertionError("unbekannter Blob im Test")

    def _rest(self, method, path, payload=None, headers=None,
              expect_json=True):
        assert method == "GET" and path.startswith("ax_logbook_import")
        return ([{"legs": self.existing, "sim": [], "meta": {}}]
                if self.existing is not None else [])

    def statuses_for(self, rid):
        return [(s, p) for ids, s, p in self.status_calls if rid in ids]


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


def test_dup_of_unsupported_inherits_failed(monkeypatch):
    # Regression #286: Kopie einer unbrauchbaren Datei muss `failed` sein.
    h = Harness(monkeypatch, {5: (b"same", "unsupported"),
                              6: (b"same", "unsupported")})
    events = []
    w.process_token_batch("AT-TEST", [_row(5, b"same"), _row(6, b"same")], events)
    assert h.statuses_for(5)[-1] == (w.STATUS_FAILED, True)
    assert h.statuses_for(6)[-1] == (w.STATUS_FAILED, True)
    assert h.pushes == [("unsupported", "AT-TEST", 5)]
    assert not h.upserts


def test_merge_conflict_sends_whole_batch_to_review(monkeypatch):
    clash = dict(LEG_A, block_min=999)
    h = Harness(monkeypatch, {7: (b"pdf-7", [clash])},
                existing_import=[LEG_A])
    events = []
    w.process_token_batch("AT-TEST", [_row(7, b"pdf-7")], events)
    assert h.statuses_for(7)[-1] == (w.STATUS_REVIEW, None)
    assert not h.upserts and not h.pushes
    assert events == [("review", "AT-TEST", [7],
                       events[0][3])] and "Merge-Konflikt" in events[0][3]


def test_control_violation_goes_to_review_not_user_failed(monkeypatch):
    h = Harness(monkeypatch, {8: (b"pdf-8", "control")})
    events = []
    w.process_token_batch("AT-TEST", [_row(8, b"pdf-8")], events)
    assert h.statuses_for(8)[-1] == (w.STATUS_REVIEW, None)
    assert not h.pushes and not h.upserts
    assert events[0][0] == "review"


def test_sha_mismatch_goes_to_review(monkeypatch):
    wrong = hashlib.sha256(b"anderes").hexdigest()
    h = Harness(monkeypatch, {9: (b"pdf-9", [LEG_A])})
    events = []
    w.process_token_batch("AT-TEST", [_row(9, sha=wrong)], events)
    assert h.statuses_for(9)[-1] == (w.STATUS_REVIEW, None)
    assert not h.upserts and not h.pushes


def test_mixed_batch_imports_good_and_fails_unsupported(monkeypatch):
    h = Harness(monkeypatch, {10: (b"pdf-10", [LEG_A]),
                              11: (b"pdf-11", "unsupported")})
    events = []
    w.process_token_batch("AT-TEST", [_row(10, b"pdf-10"), _row(11, b"pdf-11")], events)
    assert h.statuses_for(10)[-1] == (w.STATUS_COMPLETED, True)
    assert h.statuses_for(11)[-1] == (w.STATUS_FAILED, True)
    kinds = {p[0] for p in h.pushes}
    assert kinds == {"completed", "unsupported"}
