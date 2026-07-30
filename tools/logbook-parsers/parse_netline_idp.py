#!/usr/bin/env python3
"""Condor/NetLine-Crew „Individual duty plan" (CREWLINK-PDF) → AeroX-Import-Legs.

  python3 parse_netline_idp.py <quelle.pdf|quelle.txt> <ziel.json>

Quelle darf ein PDF sein (wird per pdfplumber extrahiert) oder ein bereits
extrahierter Text (.txt) — das Ergebnis ist identisch, weil ausschließlich
auf dem Textstrom gearbeitet wird.

Regeln aus ~/Desktop/AeroX-Feature-Docs/Flugbuch.md, Abschnitte 2d/2e/2f/3.

FORMAT (verifiziert an 20-Elisa Franz-…idp.pdf, Period 01Jul26-31Jul26)
──────────────────────────────────────────────────────────────────────
* Kopf: „Individual duty plan for <PersNr> NACHNAME, VORNAME" +
  „Period: 01Jul26 - 31Jul26" → Monat/Jahr für die Tages-Tokens.
* Detailteil in DREI Monats-Spalten nebeneinander. pdfplumber liest
  zeilenweise QUER über die Spalten — die Textreihenfolge ist deshalb NICHT
  chronologisch und einzelne Zeilen sind sogar zeichenweise ineinander
  verschränkt („[ [ F R D T P 8 1 8 2 : : 5 3 9 6 ] ]"). Wir ankern deshalb
  an den Tages-Tokens (`Sat11`, `Mon13`, …) und an einer strengen Leg-Regex,
  nie an Zeilennummern.
* Duty-Leg:  `DE 2080 R FRA 1015 2214 LAX 339 JU`
              ^flug   ^Request  ^dep ^ab  ^an ^arr ^AC ^info
  Das `R` (Request-Marker) und der Info-Code am Ende werden ignoriert.
* Es gibt WEITERE Zeilen mit fast identischem Aussehen, die KEINE eigenen
  Legs sind, sondern Echos aus den Tabellen „Crew Information on Leg":
      `Sat11 DE 2080 FRA 1015 2214 LAX PREX, MANUELA`
  Ihnen fehlt der AC-Typ → die strenge Regex verlangt ihn und schluckt sie
  nicht. Sie werden separat gezählt und im Report ausgewiesen (sonst hätte
  dieses PDF 9 statt 6 Legs).
* Alle Zeiten sind UTC. arr <= dep ⇒ Ankunft am Folgetag (Red-Eye).
* Ground-Codes (Sby/SB90/SBH30/RE10/SBX/Off/Sic/Vac/K/U/X/H1/H2/-/C/I/C/O/
  Pick Up …) sind keine Legs.

KONTROLLSUMME: der Plan druckt selbst „Flight time HH:MM" (Summe aller
Blockzeiten des Zeitraums). Der Parser rechnet dagegen — das ist eine
quellenunabhängige Prüfung, kein Selbstgespräch.
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

# ── Rang → Rolle (Konvention wie parse_duties_v5.ROLES) ──────────────────
# NetLine-Ränge: CP=Captain, FO=First Officer, PU=Purser, ST=Steward/ess,
# JU=Junior. Kabine ⇒ 'FB' (Flugbegleiter) — für Kabine werden KEINE
# Landungen/Starts geführt (FCL.050 ist Cockpit-Recht), also bleiben die
# ldg_*/to_*-Felder weg (Doku 2d: „Felder weglassen statt null").
RANK_ROLE = {'CP': 'PIC', 'CAP': 'PIC', 'FO': 'FO', 'SFO': 'SFO',
             'PU': 'FB', 'ST': 'FB', 'JU': 'FB', 'FB': 'FB'}
COCKPIT_RANKS = {'CP', 'CAP', 'FO', 'SFO'}

MONTHS = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
          'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
WDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

# Tages-Anker im Detailteil: „Sat11", „Mon13" …
RE_DAY = re.compile(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\d{2})\b')
# STRENGE Leg-Regex — AC-Typ ist PFLICHT (unterscheidet Duty-Leg vom
# Crew-Info-Echo). Info-Code am Ende optional.
#
# TAGES-SUFFIX (Upload #24 Hendrik Neubert, 2026-07-30): Condor hängt an die
# Flugnummer den Abflugtag des Umlaufs an — im IDP mit LEERZEICHEN vor dem
# Slash: „DE 2144 /17 SDQ 0240 1144 FRA 339". Ohne das optionale `/\d{1,2}`
# bricht die Regex genau vor `SDQ` ab und das Leg fällt STILL raus (hier
# 4 statt 5 Legs, 1806 statt 2350 min — nur die Plan-Kontrollsumme
# „Flight time" hat es gefangen). Dieselbe Konvention wie in den
# OffBlock-CSVs („DE 2199 /14", dort ohne Leerzeichen) → Suffix strippen,
# nicht in die Flugnummer übernehmen.
RE_LEG = re.compile(
    r'\b([A-Z]{2})\s?(\d{2,4})\s*(?:/\s?\d{1,2})?\s+(?:R\s+)?'  # Flugnr (+Tages-Suffix, +Request)
    r'([A-Z]{3})\s+(\d{4})\s+(\d{4})\s+([A-Z]{3})'   # dep ab an arr
    r'\s+([0-9][0-9A-Z]{2})\b')                  # AC-Typ (339, 32Q, 76W …)
# LOCKERE Regex ohne AC — findet zusätzlich die Crew-Info-Echos.
RE_LEG_LOOSE = re.compile(
    r'\b([A-Z]{2})\s?(\d{2,4})\s*(?:/\s?\d{1,2})?\s+(?:R\s+)?'
    r'([A-Z]{3})\s+(\d{4})\s+(\d{4})\s+([A-Z]{3})\b')
RE_PERIOD = re.compile(r'Period:\s*(\d{2})([A-Za-z]{3})(\d{2})\s*-\s*'
                       r'(\d{2})([A-Za-z]{3})(\d{2})')
RE_HEADER_NAME = re.compile(r'Individual duty plan for\s+\S+\s+'
                            r'([A-ZÄÖÜ\'\- ]+),\s*([A-ZÄÖÜ\'\- ]+?)\s+NetLine')
RE_TIMEPAIR = re.compile(r'\b\d{4}\s+\d{4}\b')

# Bekannte Boden-/Abwesenheits-Codes. Nur für den ehrlichen „verdächtige
# Zeile"-Zähler — geparst wird davon nichts.
GROUND_CODES = {
    'SBY', 'SB', 'SB90', 'SB30', 'SBH', 'SBH30', 'SBX', 'RE10', 'RE',
    'OFF', 'SIC', 'VAC', 'ABS', 'K', 'K_ORT', 'U', 'X', 'H1', 'H2', 'H3',
    'C/I', 'C/O', 'PICK', 'OFFD', 'FLD', 'CRM', 'IFR', 'RT', 'EQ', 'BZW',
}


def read_source(path):
    """PDF → Text (pdfplumber) oder Textdatei 1:1."""
    if path.lower().endswith('.pdf'):
        import pdfplumber
        pages = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or '')
        return '\n'.join(pages)
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def parse_period(text):
    """„Period: 01Jul26 - 31Jul26" → (start_date, end_date)."""
    m = RE_PERIOD.search(text)
    if not m:
        return None, None
    d1, mo1, y1, d2, mo2, y2 = m.groups()
    try:
        a = datetime(2000 + int(y1), MONTHS[mo1.upper()], int(d1))
        b = datetime(2000 + int(y2), MONTHS[mo2.upper()], int(d2))
    except (KeyError, ValueError):
        return None, None
    return a, b


def build_daymap(start, end):
    """(Wochentag, Tagesnummer) → Datum, über den ganzen Zeitraum.

    Der Wochentag im Token ist eine echte Prüfsumme: mangled-Text-Artefakte
    wie „Sep26" oder falsch zusammengelaufene Spalten ergeben keinen
    gültigen (Wochentag, Tag)-Treffer und werden dadurch aussortiert.
    Mehrdeutig kann es nur werden, wenn derselbe Tag im Zeitraum zweimal auf
    denselben Wochentag fällt (Feb/Mär im Nicht-Schaltjahr) — dann gewinnt
    das frühere Datum und der Report meldet es.
    """
    daymap, ambiguous = {}, []
    cur = start
    while cur <= end:
        key = (WDAYS[cur.weekday()], cur.day)
        if key in daymap:
            ambiguous.append(key)
        else:
            daymap[key] = cur
        cur += timedelta(days=1)
    return daymap, ambiguous


def own_rank(text):
    """Eigenen Rang aus den Crew-Listen des Plans lesen (belegbasiert).

    Der Plan nennt im Kopf „… for 377477C FRANZ, ELISA"; in den Crew-Listen
    der eigenen Legs steht dieselbe Person mit Rangkürzel: „ST FRANZ, ELISA".
    Fallback: die Rangspalte der Monatsübersicht („ST 0755 2210 …").
    """
    m = RE_HEADER_NAME.search(text)
    last = m.group(1).strip() if m else None
    if last:
        hits = re.findall(r'\b(CP|CAP|FO|SFO|PU|ST|JU)\s+' + re.escape(last)
                          + r'\s*,', text)
        if hits:
            return Counter(hits).most_common(1)[0][0], f'Crew-Liste ({last})'
    m2 = re.search(r'^(CP|CAP|FO|SFO|PU|ST|JU)\s+\d{4}\b', text, re.M)
    if m2:
        return m2.group(1), 'Rangspalte Monatsübersicht'
    return None, 'nicht gefunden'


def to_min(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[2:])


def selftest():
    """Regressions-Fälle für RE_LEG (ohne PDF lauffähig): python3 … --selftest"""
    cases = [
        # (Zeile, erwartet: None ODER (flug, von, ab, an, nach, ac))
        ('DE 2080 R FRA 1015 2214 LAX 339 JU',
         ('DE', '2080', 'FRA', '1015', '2214', 'LAX', '339')),
        ('DE 2455 YVR 0037 1028 FRA 339',
         ('DE', '2455', 'YVR', '0037', '1028', 'FRA', '339')),
        # Upload #24: Condor-Tages-Suffix MIT Leerzeichen vor dem Slash
        ('DE 2144 /17 SDQ 0240 1144 FRA 339',
         ('DE', '2144', 'SDQ', '0240', '1144', 'FRA', '339')),
        # OffBlock-Schreibweise ohne Leerzeichen
        ('DE2199/14 FRA 0800 1000 PMI 32Q',
         ('DE', '2199', 'FRA', '0800', '1000', 'PMI', '32Q')),
        # Crew-Info-Echo: KEIN AC-Typ ⇒ darf NICHT matchen
        ('Sat06 DE 2454 FRA 1252 2236 YVR', None),
        # Bodenaktivität: keine Flugnummer ⇒ kein Leg
        ('Sat13 DL4_330 FRA 0700 1500', None),
    ]
    bad = 0
    for line, want in cases:
        m = RE_LEG.search(line)
        got = m.groups() if m else None
        ok = got == want
        bad += 0 if ok else 1
        print(f'  [{"ok " if ok else "FAIL"}] {line}\n         -> {got}')
    print('SELFTEST', 'OK' if not bad else f'{bad} FEHLER')
    sys.exit(0 if not bad else 1)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--selftest':
        selftest()
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    src, dst = sys.argv[1], sys.argv[2]
    text = read_source(src)

    start, end = parse_period(text)
    if not start:
        print('FEHLER: „Period: DDMonYY - DDMonYY" nicht gefunden — ohne '
              'Monat/Jahr lässt sich kein Datum bilden. Abbruch.')
        sys.exit(1)
    daymap, ambiguous = build_daymap(start, end)

    rank, rank_src = own_rank(text)
    role = RANK_ROLE.get((rank or '').upper())
    is_cockpit = (rank or '').upper() in COCKPIT_RANKS

    # ── Tages-Anker einsammeln: Offset im Gesamttext → Datum ─────────────
    anchors = []            # [(offset, datum, token)]
    bad_day_tokens = []
    for m in RE_DAY.finditer(text):
        key = (m.group(1), int(m.group(2)))
        d = daymap.get(key)
        if d:
            anchors.append((m.start(), d, m.group(0)))
        else:
            bad_day_tokens.append(m.group(0))

    def day_for(offset):
        """Letzter gültiger Tages-Anker VOR dieser Textposition.

        Gilt auch zeilenintern: steht der Tag als Präfix in derselben Zeile
        („Fri31 DE 2455 …"), gewinnt er gegen den Tag der Zeile darüber.
        """
        best = None
        for off, d, tok in anchors:
            if off < offset:
                best = (d, tok)
            else:
                break
        return best if best else (None, None)

    # ── Legs parsen ──────────────────────────────────────────────────────
    legs = []
    strict_spans = []
    for m in RE_LEG.finditer(text):
        carrier, num, dep, dep_t, arr_t, arr, ac = m.groups()
        d, tok = day_for(m.start())
        if d is None:
            bad_day_tokens.append(f'{carrier}{num} ohne Tages-Anker')
            continue
        strict_spans.append(m.start())

        dep_m, arr_m = to_min(dep_t), to_min(arr_t)
        dep_dt = d + timedelta(minutes=dep_m)
        arr_dt = d + timedelta(minutes=arr_m)
        if arr_m <= dep_m:                    # Red-Eye ⇒ Ankunft am Folgetag
            arr_dt += timedelta(days=1)
        block = int((arr_dt - dep_dt).total_seconds() // 60)

        leg = {'date': d.strftime('%Y-%m-%d'),
               'flight': f'{carrier}{num}',
               'from': dep, 'to': arr,
               'dep_iso': dep_dt.strftime('%Y-%m-%dT%H:%M:00Z'),
               'arr_iso': arr_dt.strftime('%Y-%m-%dT%H:%M:00Z')}
        if 0 < block < 1200:                  # Invariante 5: keine Fantasie-Blöcke
            leg['block_min'] = block
        # AC-Typ ROH übernehmen. Doku 2f: Typ-Vereinheitlichung nur
        # BELEGBASIERT („339"→„A339" nur, wenn „A339" im selben Logbuch
        # vorkommt). Dieser Plan kennt nur die NetLine-Kürzel (339, 32Q) —
        # also kein Mapping, sonst wäre es erfunden.
        leg['type'] = ac
        if role:
            leg['role'] = role
        # Landungen/Starts nur für Cockpit — und auch dort nennt der
        # Duty-Plan sie nicht. Nichts erfinden.
        if is_cockpit:
            pass
        legs.append((m.start(), tok, leg))

    legs.sort(key=lambda t: (t[2]['date'], t[2]['dep_iso']))
    anchors_used = {id(l[2]): l[1] for l in legs}
    legs = [l[2] for l in legs]

    # ── Crew-Info-Echos & verdächtige Zeilen ehrlich zählen ──────────────
    echo, unknown_cand = [], []
    key_of = {(l['flight'], l['from'], l['to'], l['dep_iso'][11:16])
              for l in legs}
    for m in RE_LEG_LOOSE.finditer(text):
        if m.start() in strict_spans:
            continue
        carrier, num, dep, dep_t, arr_t, arr = m.groups()
        k = (f'{carrier}{num}', dep, arr, f'{dep_t[:2]}:{dep_t[2:]}')
        (echo if k in key_of else unknown_cand).append(m.group(0).strip())

    # Zeilen, die ein Zeitpaar tragen, aber weder Leg noch bekannter
    # Boden-Code sind — die will ich sehen, nicht stillschweigend schlucken.
    suspicious = []
    for line in text.splitlines():
        if not RE_TIMEPAIR.search(line):
            continue
        if RE_LEG_LOOSE.search(line):
            continue
        toks = line.split()
        upper = {t.upper().strip('[]') for t in toks}
        if upper & GROUND_CODES or 'Pick Up' in line:
            continue
        # Kopfzeilen der Monatsübersicht: ein Leit-Token (Rang bzw. Basis)
        # gefolgt von lauter HHMM — das sind die Dienstbeginn-/Dienstende-
        # Reihen über dem Kalenderraster, keine Leg-Kandidaten.
        if len(toks) > 3 and all(re.fullmatch(r'\d{4}', t) for t in toks[1:]):
            continue
        suspicious.append(line.strip())

    # ── Kollisions-Schutz (Doku 2e) ──────────────────────────────────────
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from legkeys import dedupe_keys
    collisions = dedupe_keys(legs)

    out = {'legs': legs, 'sim': []}
    with open(dst, 'w') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    # ── Report / Selbst-Verifikation ─────────────────────────────────────
    block_sum = sum(l.get('block_min', 0) for l in legs)
    print(f'Quelle      : {src}')
    print(f'Ziel        : {dst}')
    print(f'Zeitraum    : {start:%d.%m.%Y} – {end:%d.%m.%Y}')
    print(f'Rang/Rolle  : {rank} → role={role}   (Quelle: {rank_src})')
    print(f'Legs        : {len(legs)}   Block gesamt '
          f'{block_sum // 60}:{block_sum % 60:02d} ({block_sum} min)')
    print('')
    print('  datum       flug     strecke    dep–arr        block   AC   tag')
    for l in legs:
        b = l.get('block_min', 0)
        print(f'  {l["date"]}  {l["flight"]:<8} {l["from"]}→{l["to"]}    '
              f'{l["dep_iso"][11:16]}–{l["arr_iso"][11:16]}'
              f'{"(+1)" if l["arr_iso"][:10] != l["dep_iso"][:10] else "    "}  '
              f'{b // 60}:{b % 60:02d} ({b:>3})  {l.get("type", "—"):<4} '
              f'{anchors_used.get(id(l), "?")}')
    print('')

    # Kontrollsumme aus dem Plan selbst
    m = re.search(r'Flight time\s+(\d{1,3}):(\d{2})', text)
    if m:
        want = int(m.group(1)) * 60 + int(m.group(2))
        ok = 'OK' if want == block_sum else 'ABWEICHUNG'
        print(f'KONTROLLE   : Plan-Summe „Flight time" {m.group(1)}:{m.group(2)}'
              f' = {want} min vs. geparst {block_sum} min → {ok}')
    else:
        print('KONTROLLE   : „Flight time"-Summe im Plan nicht gefunden')

    # Chronologie-Plausibilität (Überlappungen wären ein Datums-Fehler)
    bad_seq = [f'{b["flight"]} ({b["date"]})'
               for a, b in zip(legs, legs[1:]) if b['dep_iso'] < a['arr_iso']]
    print(f'Chronologie : {"lückenlos aufsteigend" if not bad_seq else "ÜBERLAPP: " + ", ".join(bad_seq)}')

    print(f'Key-Kollisionen aufgeloest: {len(collisions)}')
    for c in collisions:
        print(f'   {c[0]}  {c[1]}  {c[2]}  dep {c[3]}  -> Suffix ({c[4]})')

    print(f'Übersprungene Kandidaten:')
    print(f'   Crew-Info-Echos (identisch zu geparstem Leg, ohne AC): '
          f'{len(echo)}')
    for e in echo:
        print(f'      {e}')
    print(f'   leg-ähnlich, aber unbekannt: {len(unknown_cand)}')
    for u in unknown_cand:
        print(f'      {u}')
    print(f'   Zeilen mit Zeitpaar ohne bekannten Boden-Code: '
          f'{len(suspicious)}')
    for s_ in suspicious:
        print(f'      {s_[:120]}')
    if bad_day_tokens:
        print(f'   ungültige/ankerlose Tages-Tokens: '
              f'{len(bad_day_tokens)} {sorted(set(bad_day_tokens))[:10]}')
    if ambiguous:
        print(f'   ACHTUNG mehrdeutige (Wochentag,Tag)-Paare: {ambiguous}')


if __name__ == '__main__':
    main()
