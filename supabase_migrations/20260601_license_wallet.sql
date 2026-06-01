-- License-Wallet cross-device-sync (Worker P4, 2026-06-01).
-- Persistiert die iOS LicenseItem-SwiftData-Models in Supabase damit der
-- gleiche User auf einem zweiten Gerät (oder nach Reinstall) seine Wallet
-- wieder bekommt. Disk-Fallback (licenses_<token>.json) übernimmt wenn SB
-- down ist — Schema spiegelt deshalb 1:1 die iOS-Item-Felder.
--
-- Schema-Entscheidungen:
--  · PK auf item-id (UUID-Text) — iOS generiert die UUID lokal, beim Sync
--    landet sie unverändert in der Tabelle. Bei Mehrgerät-Konflikt gewinnt
--    Last-Writer (updated_at).
--  · user_token getrennt indiziert, weil die häufigste Query
--    "list all items for token, deleted=false" ist.
--  · category als CHECK statt enum-Tabelle — die Liste ändert sich selten,
--    iOS-LicenseCategory.rawValue ist das single-source-of-truth.
--  · item_type als freier text — iOS LicenseItemType wächst mit neuen Lizenz-
--    Klassen (z.B. neue Type-Ratings); ein DB-Constraint hier wäre ein
--    Deploy-Blocker bei jeder neuen Konstante.
--  · photo_blob_id als text (Referenz auf separates Storage, falls je
--    server-side Photo-Upload kommt). Aktuell bleibt das Foto AES-GCM
--    verschlüsselt nur auf dem Device — der Server sieht den Cipher-Blob
--    bewusst NICHT (Privacy-by-default).
--  · custom_notes als text — der User kann hier Klartext eintippen; das ist
--    persönlich, RLS verhindert Cross-User-Access.
--  · alert_window_days als jsonb mit Default [90,60,30,7] passend zum
--    iOS-Default in LicenseItem.swift.
--  · deleted als boolean für Soft-Delete — App-Side filtert deleted=false,
--    Sync-Kollisionen können so noch erkannt werden ohne Hard-Delete.
--  · metadata jsonb als Catch-all für Felder die zukünftig vom Client
--    geschickt werden ohne dass die Tabelle migriert werden muss
--    (issuing_authority-Codes, Revalidation-Daten, etc.).
create table if not exists public.user_licenses (
    id                  text         primary key,
    user_token          text         not null,
    category            text         not null,
    item_type           text         not null,
    label               text,
    issue_date          date,
    expiry_date         date,
    issuing_authority   text,
    document_number     text,
    photo_blob_id       text,
    custom_notes        text,
    alert_window_days   jsonb        not null default '[90,60,30,7]'::jsonb,
    deleted             boolean      not null default false,
    metadata            jsonb        not null default '{}'::jsonb,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    check (category in ('cockpit', 'cabin', 'general'))
);

-- Häufigste Query: "alle nicht-gelöschten Items für diesen User-Token".
create index if not exists idx_user_licenses_token
    on public.user_licenses(user_token) where deleted = false;

-- Sekundär-Query (Notification-Scheduler, Aggregat-Statistik): items die
-- in den nächsten N Tagen ablaufen.
create index if not exists idx_user_licenses_expiry
    on public.user_licenses(expiry_date) where deleted = false;

alter table public.user_licenses enable row level security;
