"""Privacy-preserving learning state for AI-assisted document parsers.

The runtime never writes Python or promotes free-form model output to a
deterministic parser.  Instead it learns a stable *format contract* (a salted
structural fingerprint, verification generation and audit cadence):

* unknown format: two independent model reads plus deterministic evidence
  checks;
* two different, fully verified documents: format becomes active;
* active format: one model read plus the same deterministic checks;
* every tenth successful use: two independent reads again;
* any semantic disagreement: quarantine and require two fresh documents.

Only the SHA-256 fingerprint and source hashes are stored.  No user token,
document text, extracted flight facts or filename enters the learning tables.
Known first-party parsers remain deterministic and do not use this module.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata


FINGERPRINT_VERSION = 1
PROMPT_VERSION = "source-evidence-v1"
AUDIT_INTERVAL = 10

_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?<!\w)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\w)")
_TIME_RE = re.compile(r"(?<!\w)\d{1,3}:[0-5]\d(?!\w)")
_ALNUM_ID_RE = re.compile(
    r"\b(?=[A-Z0-9_-]{6,}\b)(?=[A-Z0-9_-]*[A-Z])"
    r"(?=[A-Z0-9_-]*\d)[A-Z0-9_-]+\b")
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")
_MONTH_RE = re.compile(
    r"\b(?:JAN(?:UAR(?:Y)?)?|FEB(?:RUAR(?:Y)?)?|"
    r"M(?:AR(?:CH)?|AERZ|ÄRZ)|APR(?:IL)?|MAY|MAI|JUN(?:E|I)?|"
    r"JUL(?:Y|I)?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|"
    r"O(?:CT(?:OBER)?|KT(?:OBER)?)|NOV(?:EMBER)?|"
    r"D(?:EC(?:EMBER)?|EZ(?:EMBER)?)|"
    r"JANVIER|FEVRIER|FÉVRIER|MARS|AVRIL|JUIN|JUILLET|AOUT|AOÛT|"
    r"OCTOBRE|DECEMBRE|DÉCEMBRE|"
    r"ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|"
    r"SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)\b",
    re.IGNORECASE,
)

# These words select label/header lines and deliberately cover the formats
# already observed in the import inbox.  Unknown languages fall back to the
# first value-masked lines, which can cause a harmless false negative (two
# reads again), never a less-verified import.
_HEADER_WORDS = {
    "ROSTER", "SCHEDULE", "DUTY", "PLAN", "DATE", "DAY", "MONTH",
    "PERIOD", "REPORT", "ACTIVITY", "FROM", "TO", "START", "END",
    "FLIGHT", "BLOCK", "TIME", "BASE", "CREW", "AIRCRAFT", "TYPE",
    "REGISTRATION", "DEPARTURE", "ARRIVAL", "ORIGIN", "DESTINATION",
    "LOGBOOK", "DURATION", "LANDING", "CAPTAIN", "OFFICER",
    "DIENSTPLAN", "EINSATZPLAN", "DATUM", "TAG", "MONAT", "ZEIT",
    "FLUG", "VON", "NACH", "ABFLUG", "ANKUNFT", "BLOCKZEIT",
}
_VARIABLE_LABEL_RE = re.compile(
    r"^(?:CREW(?:\s+(?:MEMBER|NAME))?|EMPLOYEE|PERSONNEL|STAFF|USER|"
    r"NAME|ID|BASE|HOMEBASE)\s*:\s*.+$",
    re.IGNORECASE,
)


def _masked_line(value: str) -> str:
    line = unicodedata.normalize("NFKC", str(value or ""))
    line = " ".join(line.upper().split())[:240]
    if not line:
        return ""
    line = _EMAIL_RE.sub("<EMAIL>", line)
    line = _URL_RE.sub("<URL>", line)
    line = _DATE_RE.sub("<DATE>", line)
    line = _TIME_RE.sub("<TIME>", line)
    line = _MONTH_RE.sub("<MONTH>", line)
    line = _ALNUM_ID_RE.sub("<ID>", line)
    line = _NUMBER_RE.sub("<N>", line)
    if _VARIABLE_LABEL_RE.match(line):
        line = line.split(":", 1)[0] + ":<VALUE>"
    return " ".join(line.split())


def _line_shape(value: str) -> str:
    """Coarse token grammar; values disappear but column layout remains."""
    line = _masked_line(value)
    line = re.sub(r"<[A-Z]+>", "V", line)
    line = re.sub(r"[A-ZÀ-ÖØ-Þ]+", "A", line)
    line = re.sub(r"\d+", "N", line)
    line = re.sub(r"\s+", " ", line)
    # Consecutive same-class words do not add format identity.
    line = re.sub(r"\bA(?: A)+\b", "A+", line)
    return line[:180]


def document_format_fingerprint(source: str, kind: str) -> str:
    """Return a stable SHA-256 contract for a roster/logbook layout.

    The input is intentionally reduced before hashing.  The resulting value
    cannot be used to reconstruct document text and remains useful across
    months/users whose dates, times, IDs and names differ.
    """
    if kind not in ("roster", "logbook"):
        raise ValueError("unsupported parser-learning kind")
    raw_lines = [" ".join(line.split()) for line in str(source or "").splitlines()]
    raw_lines = [line for line in raw_lines if line][:1600]
    if not raw_lines:
        raise ValueError("empty parser-learning source")

    # Delimited logs have an authoritative column header.  Include the first
    # two label rows verbatim-after-masking so two different column maps do not
    # accidentally share a contract.
    delimited = [line for line in raw_lines[:12]
                 if line.count("\t") >= 2 or line.count(",") >= 3
                 or line.count(";") >= 3]

    headers = []
    for line in raw_lines[:100]:
        masked = _masked_line(line)
        without_placeholders = re.sub(r"<[A-Z]+>", "", masked)
        words = set(re.findall(r"[A-ZÀ-ÖØ-Þ]+", without_placeholders))
        if words & _HEADER_WORDS:
            headers.append(masked)
        if len(headers) >= 24:
            break
    if not headers:
        headers = [_masked_line(line) for line in raw_lines[:12]]
    headers = [line for line in headers if line]

    shapes = sorted({shape for line in raw_lines[:500]
                     if len((shape := _line_shape(line))) >= 3})[:48]
    basis = "\n".join([
        f"v={FINGERPRINT_VERSION}",
        f"kind={kind}",
        "delimited=" + "|".join(_masked_line(line) for line in delimited[:2]),
        "headers=" + "|".join(headers),
        "shapes=" + "|".join(shapes),
    ])
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


def source_evidence_hash(source: str, server_secret: str = "") -> str:
    """Stable private document ID; production uses a server-side HMAC key."""
    payload = str(source or "").encode("utf-8", "replace")
    secret = str(server_secret or "").encode("utf-8", "replace")
    if secret:
        return hmac.new(secret, payload, hashlib.sha256).hexdigest()
    # Local/unit environments have no learning DB; an ordinary hash keeps the
    # pure helper usable without inventing a test secret.
    return hashlib.sha256(payload).hexdigest()


def learning_read_count(state: dict | None) -> int:
    """Return 1 only for an active, not-currently-due format contract."""
    if not isinstance(state, dict) or state.get("status") != "active":
        return 2
    try:
        successful_uses = max(0, int(state.get("successful_uses") or 0))
    except (TypeError, ValueError):
        return 2
    # Audit the 10th, 20th, ... successful import.  Training documents count
    # as verified uses, so a just-promoted format still receives seven cheap
    # single-read imports before its first recurring audit.
    return 2 if (successful_uses + 1) % AUDIT_INTERVAL == 0 else 1


def learning_mode(state: dict | None) -> str:
    reads = learning_read_count(state)
    if not isinstance(state, dict) or state.get("status") != "active":
        return "training_double_read"
    return "recurring_audit" if reads == 2 else "active_single_read"
