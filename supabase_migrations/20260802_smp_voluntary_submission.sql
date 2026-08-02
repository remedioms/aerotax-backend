-- Eigene SMP-Karten sind privat. Erst ein expliziter, protokollierter Submit
-- verschiebt sie in die Review-Queue. Bestehende pending-Zeilen stammen aus
-- dem früheren Auto-Submit-Vertrag und werden mangels Einzelnachweis privat.
alter table public.ax_smp_user_cards
    drop constraint if exists ax_smp_user_cards_status_check;

alter table public.ax_smp_user_cards
    alter column status set default 'private';

update public.ax_smp_user_cards
set status = 'private'
where status = 'pending';

alter table public.ax_smp_user_cards
    add constraint ax_smp_user_cards_status_check
    check (status in ('private', 'pending', 'approved', 'rejected'));

alter table public.ax_smp_user_cards
    add column if not exists source_community_card_id uuid
        references public.ax_smp_user_cards(id),
    add column if not exists submitted_at timestamptz,
    add column if not exists consent_version text;

create index if not exists idx_smp_user_cards_source_community
    on public.ax_smp_user_cards (source_community_card_id)
    where source_community_card_id is not null;

notify pgrst, 'reload schema';
