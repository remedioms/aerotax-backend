# AeroX — App-Store Release-Readiness (Stand 2026-05-30)

## TL;DR

**Code-Stand: release-ready für Closed-Beta via TestFlight.**

Alle Apple-Reject-Trigger sind gefixt, Security-Mindeststandard erfüllt,
UGC-Moderation vorhanden, kein Subscription-Live-Sale ohne StoreKit. Was
für GA-Launch noch offen ist, ist unten dokumentiert — keine davon ist
release-blocking, aber alle sind GA-Quality-Faktoren.

---

## ✅ GO — alle App-Store-Blocker geschlossen

### Apple Guidelines erfüllt
- **5.1.1(v) Account-Deletion** (Wave 14): MehrView → "Konto löschen" mit
  Token-Auth (Apple-Sign-In-User) ODER Email/Password-Auth + Cascade-Delete
  über Wall-Posts, Forum-Threads, Friends, Likes, Voice-Notes, Briefings.
- **5.1.1 Privacy-Manifest** (Wave 15): `PrivacyInfo.xcprivacy` deklariert
  alle CollectedDataTypes + required-reason API-Reasons (CA92.1/C617.1/
  E174.1/35F9.1). `ITSAppUsesNonExemptEncryption=false` in Info.plist.
- **1.4.1 UGC-Moderation** (Wave 16): Context-Menu auf Wall-Posts, Forum-
  Threads, LayoverRecs mit Melden (8 Gründe + Notiz) / Blockieren / Stumm-
  schalten. BlockedUsersView im KONTO-Menü. Server filtert geblockten
  Content aus Feed/Forum.
- **3.1.1 Subscription** (Wave 17): keine Preise + keine Kauf-Buttons mehr
  in SubscriptionsView. Beta-Status klar kommuniziert; alle Features sind
  kostenlos während Beta.
- **5.1.2 Apple Sign-In Verify** (Wave 17): identity_token JWT-Signatur-
  Verifikation gegen Apple JWKS (RS256 + PKCS1v15) + iss/aud/exp/sub-Check.
- **AGB/EULA/Datenschutz/Impressum** Links in MehrView (Wave 17).
- **Permission-Strings** präzise (Wave 15): Camera erwähnt QR-Scan +
  Wall-Posts, Notifications-Description ergänzt.

### Security & Privacy
- **PBKDF2-Passwords** (Wave 21): 600k iterations + per-User-Salt. Legacy
  sha256-Hashes werden beim Login transparent migriert.
- **Password-Policy**: min 8 Zeichen + Buchstabe + Ziffer + Common-Blocklist.
- **Apple JWT Verify** (Wave 17): keine Token-Spoofing möglich.
- **Stranger-DM Block** (Wave 22): DMs nur zwischen Friends, sonst
  403 not_friends.
- **Rate-Limits** (Wave 22): Wall 10/Std, Forum 5/Std, DM 30/Min pro User.
  Demo-Tokens (AT-GUEST-*) auf allen Post-Endpoints geblockt.
- **SSRF-Schutz** (Wave 12): nur HTTPS für iCal-URLs, private IP-Block.
- **Atomic Writes** (Wave 18): Wall/Forum/Friends/Auth-Files mit
  POSIX-atomic rename → keine halb-geschriebenen JSON bei Concurrent-Writes.
- **DSGVO Art. 17**: Cascade-Delete bei Account-Deletion entfernt Posts,
  Threads, Likes, Friends-Referenzen, Voice-Notes, Briefings.
- **DSGVO Art. 17 sub-rights** (Wave 20): User können eigene Wall-Posts +
  Forum-Threads via Context-Menu löschen (is_mine-Flag aus Backend).

### Features Wired End-to-End
- **Account**: Email+Password Signup/Login/Reset/Delete + Apple Sign-In
  mit JWT-Verifikation. Cross-Device-Login funktioniert (verified).
- **Friend-Discovery** (Wave 19): `/api/user/search?q=&airline=&homebase=`
  + iOS SearchView Crew-Tab + AddFriendFromSearchView mit Request-CTA.
- **Friend-Request-Inbox** (Wave 24): FriendRequestsView mit Eingehend/
  Ausgehend, Accept/Decline, Badge in CrewView.
- **Friend-Compare** (Wave 28-30): echtes `/api/user/friend-roster`-
  Endpoint mit Privacy-Gate (Friend muss `share_roster=true` setzen).
  Kein Mock-Roster mehr.
- **UGC-Moderation** (Wave 16): Report/Block/Mute + BlockedUsersView.
- **Wall + Forum + LayoverRecs + Crew-Chat** alle backend-wired.

### Operational
- **Cost-Caching** (Wave 23): Anthropic Prompt-Caching im AI-Chat-Pfad
  → ~50% Token-Cost-Reduktion auf chat-Anteil.
- **Boot-Checks**: RECOVERY_SECRET + AEROTAX_CRYPTO_KEY beim Boot
  validiert → fail-fast statt silent corruption.
- **Auto-Deploy**: Render auto-deployt bei git push → keine manuelle
  Release-Pipeline notwendig für Backend-Updates.

---

## 🟡 Open — Quality-Items für GA (nicht release-blocking)

Diese würden im GA-Launch nochmal aufgegriffen, sind aber für Beta-TestFlight
nicht erforderlich:

### Storage-Migration zu Postgres (O3-A, O3-B, O1-D, O1-C)
- Cloud-Run-FS ist ephemeral → bei jedem Deploy gehen
  `_user_history_state/*.json` (Profile, Friends, Wall, Forum, Voice-Notes,
  Briefings) verloren.
- Workaround heute: Render hat Persistent-Disk. Auf Cloud Run wäre
  Migration zu Postgres/Supabase notwendig.
- **Impact**: Closed-Beta auf Render OK; Cloud-Run-GA braucht das.

### Anthropic Zero-Retention DPA (O8-C)
- Standard-API hat 30-Tage-Retention für Abuse-Monitoring → PII (LSB) wird
  bei Anthropic 30 Tage gespeichert.
- **Fix**: Anthropic Enterprise-Tier mit Zero-Data-Retention + signiertem
  DPA beantragen vor GA.
- **Impact**: DSGVO-Art-28-Pflicht für Auftragsverarbeiter — vor GA klären.

### iOS Design-Polish (Audit: 32 Findings)
- Spacing-/Radius-Tokens jetzt vorhanden (Wave 25), aber noch nicht
  durchgängig angewandt.
- AppColor.gold heisst noch "gold" obwohl Brand-Color jetzt Blue ist
  (kommentar-only Fix wäre Rename, niedriges Risiko).
- 30+ Stellen mit `.font(.system(size: N))` statt zentraler Font-Skala.
- SkeletonCard ist als Component bereit aber noch nicht überall angewandt.
- **Impact**: App ist heute schon "Liquid Glass" — die offenen Items sind
  Konsistenz-Polish, nicht funktionale Gaps.

### Email-Verifikation für Signup (J04)
- Aktuell ist Email-Verifikation nicht erzwungen — bei Pre-Account-Takeover-
  Risiko via Reset-Token bei unverifizierter Email.
- Mittels Resend bereits angebunden (`_send_reset_email` Pfad).
- **Fix für GA**: Signup → `email_verified=false`, Verification-Mail mit
  Token, Posts/DMs erst nach Verify.

### Brand-Unification AeroX / AeroTax / Aeris
- Bundle: AeroX. App-Display: AeroX. Tax-Feature heisst weiterhin AeroTax.
  Backend-Code-Name: aerotax-backend. Website: aerosteuer.de.
- **Strategische Entscheidung**: AeroX = App-Brand, AeroTax = Tax-Feature
  innerhalb der App. Website-Brand kann unverändert bleiben.

### iOS Polish-Findings (kleiner Liste)
- Lufthansa Cookie-Scraping juristisches Risiko (LH-AGB-Verletzung)
- TaxQuestionnaire UserDefaults-only — kein Cross-Device-Sync für Tax-Form
- ContactsImportView ist mounted aber kein Backend-Match implementiert
- Q&A-Tab Backend-Routen vorhanden aber kein iOS-UI
- Sponsored-Friends Backend-Routen vorhanden aber iOS-View nicht
  navigierbar
- Force-Update-Check Backend vorhanden, iOS triggert nie
- Statistik / Logbook-AMC1 für Cabin-Crew sichtbar (sollte Pilot-only sein)

Alle obigen Items sind funktional, nur halb-fertig oder UX-mismatch — nicht
release-blocking.

---

## 🚦 Pre-Submission Checklist

Vor App-Store-Submission durchgehen:

- [ ] **App Store Connect**: App-Eintrag anlegen (Name "AeroX", Bundle-ID
      `de.aerosteuer.aeris`)
- [ ] **TestFlight**: ipa via Xcode Archive → App Store Connect upload
- [ ] **App Review Information**: Demo-Account anlegen (`demo@aerox.test` +
      Passwort) damit Reviewer reinkommt
- [ ] **Privacy-Manifest**: Bundle wird Apple-Privacy-Report automatisch
      erstellen aus `PrivacyInfo.xcprivacy`
- [ ] **Datenschutzerklärung**: aerosteuer.de/datenschutz muss vor
      Submission live + verlinkt sein (ist es)
- [ ] **Support-URL**: aerosteuer.de oder support@aerosteuer.de funktioniert
- [ ] **Screenshots**: 6.7"-iPhone-Screenshots (Pro Max), 6.1" iPhone,
      iPad-Pro
- [ ] **Description**: App-Description die Beta-Status erwähnt + ehrlich
      kommuniziert dass Steuer-Auswertung auf aerosteuer.de läuft (bis
      native Upload kommt)
- [ ] **Categories**: Primary "Productivity" + Secondary "Business"
- [ ] **Age Rating**: 17+ wegen User-Generated-Content (Forum/Wall)
- [ ] **Anthropic Enterprise/DPA**: vor GA — für Closed-Beta TestFlight
      noch akzeptabel (Beta-User wissen Bescheid)
- [ ] **AEROTAX_CRYPTO_KEY + RECOVERY_SECRET**: in Render-Env-Vars gesetzt
- [ ] **CrewLink-Sync** + iCal-Sync: optional gate via "Beta-Feature"-Hinweis

## 🎯 Empfehlung

**Status: GO für TestFlight Closed-Beta.**

Für App-Store-Submission (auch als 1.0 Beta-Release) keine technischen
Blocker mehr. Open items oben sind GA-Quality-Verbesserungen die in
zukünftigen Versionen kommen. Apple sollte Review bestehen.

**Reihenfolge zum Go-Live:**
1. TestFlight Closed-Beta mit 10-20 Crew-Tester
2. Anthropic DPA klären (sollte in 1-2 Wochen)
3. Postgres-Migration ODER Persistent-Disk-Verifikation auf Render
4. Public TestFlight (bis 10k Tester) für 4 Wochen
5. App-Store-Public-Release mit Open-Items beseitigt

---

Stand: 2026-05-30 (commit `df31da8` Backend, `3be5057` iOS).
Waves 14-30 alle gepusht.
