# Android-5k-Staging-Lasttest

Dieses Skript modelliert einen Launch mit **5.000 registrierten Nutzern**, nicht 5.000 gleichzeitig aktiven Geräten. Das konservative Standardprofil erreicht höchstens 500 aktive VUs: 5 Minuten bis 50, 10 Minuten bis 250, 20 Minuten Haltephase bei 500 und 5 Minuten Ramp-down. `MAX_VUS` ist auf 1–500 begrenzt. Die Account-Datei muss mindestens einen dedizierten Staging-Account pro VU enthalten, damit keine Credentials geteilt werden.

## Sicherheitsgates

Der Test verweigert bekannte Produktions-, Render- und Cloud-Run-Hosts. Er fordert `ALLOW_AEROX_STAGING_LOAD=1` sowie eine mit `BASE_URL` exakt identische Bestätigung in `AEROX_STAGING_URL`. Lokale/private Ziele brauchen zusätzlich `ALLOW_LOCAL_STAGING=1`. Diese Schutzmechanismen ersetzen keine isolierte Staging-Umgebung.

Lege außerhalb des Repos eine JSON-Datei mit ausschließlich für Staging angelegten Konten an. Labels sind nicht personenbezogen; Tokens dürfen nie committed, geloggt oder in Ergebnisdateien kopiert werden.

```json
[
  {"label":"loadtest-001","token":"<staging bearer token>"},
  {"label":"loadtest-002","token":"<staging bearer token>"}
]
```

Der Standardlauf nutzt nur authentifizierte `/api/me`-Reads für Profil, Entitlement, Friends, Friends-today und Push-Präferenzen. Er startet weder Steuer-Pipelines noch Provider-Aufrufe, Uploads, Käufe, Chats, Kommentare oder Wall-Posts.

## Sicherer Lauf

`<staging-origin>` muss ein dediziertes HTTPS-Staging-Origin sein und in beiden URL-Variablen exakt gleich stehen:

```sh
ALLOW_AEROX_STAGING_LOAD=1 \
AEROX_STAGING_URL="https://<staging-origin>" \
BASE_URL="https://<staging-origin>" \
LOAD_ACCOUNTS_FILE="$HOME/.config/aerox-loadtest/staging-accounts.json" \
MAX_VUS=500 MIN_RPS=20 \
k6 run --out json=artifacts/android-5k-staging.json \
  loadtest/k6_android_5k_staging.js
```

`--out json=...` archiviert Metriken ohne Response-Bodies. Die Ergebnisdatei gehört nicht in Git und darf keine Tokens enthalten. Dokumentiere dazu Commit-SHA, Staging-Image, Zeitfenster, VU-Profil, Account-Pool-Größe, k6-Version sowie p95/p99, Request-Rate, 2xx-/Fehlerquote, 429 und die Server-, Supabase-Pool-, Outbox- und Provider-Metriken.

Die eingebauten Gates sind: Gesamtfehler <1 %, `/api/me`-Fehler <0,5 %, Checks und Read-2xx >99 %, Gesamt-p95 <1.200 ms, Gesamt-p99 <2.500 ms, Read-p95 <1.000 ms, Read-p99 <2.000 ms sowie mindestens `MIN_RPS` (Standard 20). Eine grüne k6-Ausgabe allein ist kein Kapazitätsnachweis.

## Optionaler idempotenter Write

Standardmäßig erfolgen keine Mutationen. Für eigens angelegte Staging-Konten kann getrennt eine feste, wiederholbare `/api/me/push/prefs`-Einstellung getestet werden. Sie erzeugt keine Inhalte und keine Pushes:

```sh
ALLOW_TEST_ACCOUNT_WRITES=1 TEST_ACCOUNT_WRITE_PERCENT=2 \
ALLOW_AEROX_STAGING_LOAD=1 \
AEROX_STAGING_URL="https://<staging-origin>" \
BASE_URL="https://<staging-origin>" \
LOAD_ACCOUNTS_FILE="$HOME/.config/aerox-loadtest/staging-accounts.json" \
k6 run --out json=artifacts/android-5k-staging-write.json \
  loadtest/k6_android_5k_staging.js
```

`TEST_ACCOUNT_WRITE_PERCENT` ist auf 0–5 begrenzt. Niemals echte Nutzerkonten oder Produktion verwenden.
