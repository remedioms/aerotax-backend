#!/usr/bin/env python3
"""Seedet die kuratierten Tipps aus dem offiziellen „Lufthansa Crew Layover
Guide" (DOCX, geparst zu JSON) als Layover-Recs nach Supabase `layover_recs`.

Warum: Die Crowdsource-Recs starten pro Station bei null; der Guide bringt
~1130 kuratierte Tipps über 221 Stationen MIT Bewertung (x/5) und Anzahl
Crew-Bewertungen. Die Review-Zahl wird als `vote_score`/`vote_count` gesetzt →
die bestehende Sortierung des Endpoints (vote_score absteigend) sortiert damit
automatisch „von der Crew am meisten bestätigt" nach oben.

Idempotent: deterministische IDs (`lhg26_<IATA>_<n>`) + Upsert
(resolution=merge-duplicates). Titel-Dedupe gegen BESTEHENDE (nicht-Guide-)
Recs, damit ein schon von Usern geposteter Tipp nicht doppelt erscheint —
die iOS-Seite dedupliziert zusätzlich per Titel gegen die Bundle-Kuration.

Aufruf (auf dem Hetzner-Host, JSON via stdin):
    docker exec -i aerotax-backend python3 - < <(cat seed_layover_guide.py; \
        echo 'DATA_JSON = r"""'; cat layover_guide.json; echo '"""'; echo 'main(DATA_JSON)')
oder lokal mit SUPABASE_URL/SUPABASE_SERVICE_KEY in der Env:
    python3 scripts/seed_layover_guide.py layover_guide.json
"""
import json
import os
import re
import sys
import unicodedata
import urllib.request

# Guide-Kategorien → App-Kategorien (LAYOVER_CATEGORIES in app.py).
_CAT_MAP = {
    'food_drink': 'food', 'tour_tipps': 'sight', 'sport': 'gym',
    'shopping': 'shopping', 'grocery': 'shopping', 'city_country': 'other',
    'language': 'other', 'money': 'other', 'airport_fb': 'other',
    'leasure': 'sight', 'aid_organisations': 'other',
}
_COFFEE_PAT = re.compile(r'caf[eé]|coffee|tea\s?room|tearoom|espresso', re.I)
# Fixer Import-Zeitstempel (2026-07-18 12:00Z) — Seeds sollen nicht „neuer"
# wirken als echte Crew-Posts vom selben Tag.
_TS = 1784548800


def _canon(s):
    s = unicodedata.normalize('NFKD', (s or '').lower())
    return re.sub(r'[^a-z0-9]', '', s)


def _headers(key):
    return {'apikey': key, 'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json'}


def _existing_titles(base, key):
    """(iata, canon(title)) aller vorhandenen Recs — paginiert."""
    seen = set()
    offset = 0
    while True:
        req = urllib.request.Request(
            base + '/rest/v1/layover_recs?select=id,iata,title'
            + f'&limit=1000&offset={offset}', headers=_headers(key))
        rows = json.loads(urllib.request.urlopen(req, timeout=20).read())
        for r in rows:
            if not str(r.get('id', '')).startswith('lhg26_'):
                seen.add((r.get('iata'), _canon(r.get('title'))))
        if len(rows) < 1000:
            return seen
        offset += 1000


def build_rows(guide):
    rows = []
    for iata, d in sorted(guide.items()):
        for idx, e in enumerate(d.get('entries') or []):
            title = (e.get('title') or '').strip()[:120]
            desc = (e.get('desc') or '').strip()[:2400]
            if not title:
                continue
            cat = _CAT_MAP.get(e.get('cat') or '', 'other')
            if cat == 'food' and _COFFEE_PAT.search(title):
                cat = 'coffee'
            reviews = int(e.get('reviews') or 0)
            rows.append({
                'id': f'lhg26_{iata}_{idx}',
                'iata': iata,
                'category': cat,
                'title': title,
                'description': desc or None,
                'rating': int(e.get('rating') or 0),
                'price_band': '',
                'location_hint': None,
                'author_token': 'lh_guide_seed',
                'author_short': 'LH Crew Guide',
                'ts': _TS,
                'vote_score': reviews,
                'vote_count': reviews,
                'deleted': False,
                'metadata': {'source': 'lh_layover_guide',
                             'guide_cat': e.get('cat'),
                             'imported': '2026-07-18'},
            })
    return rows


def main(data_json):
    key = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_KEY')
    base = os.environ['SUPABASE_URL']
    guide = json.loads(data_json)
    rows = build_rows(guide)
    existing = _existing_titles(base, key)
    fresh = [r for r in rows if (r['iata'], _canon(r['title'])) not in existing]
    skipped = len(rows) - len(fresh)
    done = 0
    for i in range(0, len(fresh), 200):
        batch = fresh[i:i + 200]
        req = urllib.request.Request(
            base + '/rest/v1/layover_recs?on_conflict=id',
            headers={**_headers(key),
                     'Prefer': 'resolution=merge-duplicates'},
            method='POST', data=json.dumps(batch).encode())
        urllib.request.urlopen(req, timeout=30).read()
        done += len(batch)
        print(f'  upserted {done}/{len(fresh)}')
    print(f'FERTIG: {done} geseedet, {skipped} Titel-Dubletten übersprungen, '
          f'{len(guide)} Stationen')


# Guard auf argv: beim stdin-Pipe (docker exec, Daten werden unten angehängt)
# ist __name__ ebenfalls '__main__', aber es gibt kein Datei-Argument.
if __name__ == '__main__' and len(sys.argv) > 1:
    main(open(sys.argv[1], encoding='utf-8').read())
