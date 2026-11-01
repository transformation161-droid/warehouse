-- ИСТОРИЧЕСКИЙ ПРОЕКТ: НЕ ПРИМЕНЯТЬ. Действующая локальная схема: server/database.py.
-- Результат аудита и ограничения прежних политик: docs/AUDIT.md.
-- ИНСТРАКТ | КСУ Контроль — схема следующего этапа.
-- НЕ подключена к автономной HTML-демоверсии. Использовать только после
-- согласования корпоративного размещения и перечня обрабатываемых данных.
-- PostgreSQL / Supabase. Миграция рассчитана на НОВЫЙ проект.

create extension if not exists pgcrypto;

create table if not exists public.ksu_members (
    user_id uuid primary key references auth.users(id) on delete cascade,
    display_name text not null,
    role text not null check (role in ('manager','inspector')),
    active boolean not null default true,
    created_at timestamptz not null default now()
);

create or replace function public.ksu_is_member()
returns boolean language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.ksu_members where user_id = auth.uid() and active)
$$;
create or replace function public.ksu_is_manager()
returns boolean language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.ksu_members where user_id = auth.uid() and active and role = 'manager')
$$;

create table if not exists public.ksu_sites (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    code text,
    storekeeper text,
    project_manager text,
    active boolean not null default true,
    created_at timestamptz not null default now()
);

create table if not exists public.ksu_questions (
    id uuid primary key default gen_random_uuid(),
    section text not null,
    title text not null,
    sort_order integer not null default 0,
    active boolean not null default true,
    created_at timestamptz not null default now()
);

create table if not exists public.ksu_inspections (
    id uuid primary key default gen_random_uuid(),
    site_id uuid not null references public.ksu_sites(id),
    inspection_date date not null,
    inspector_id uuid not null references auth.users(id),
    status text not null default 'completed' check (status in ('draft','completed')),
    created_at timestamptz not null default now()
);
create index if not exists ix_ksu_inspections_site_date on public.ksu_inspections(site_id, inspection_date desc);

create table if not exists public.ksu_inspection_answers (
    id uuid primary key default gen_random_uuid(),
    inspection_id uuid not null references public.ksu_inspections(id) on delete cascade,
    question_id uuid not null references public.ksu_questions(id),
    result text not null check (result in ('ok','fail','na')),
    comment text,
    question_title_snapshot text not null,
    section_snapshot text not null,
    unique (inspection_id, question_id)
);

create table if not exists public.ksu_findings (
    id uuid primary key default gen_random_uuid(),
    site_id uuid not null references public.ksu_sites(id),
    inspection_id uuid references public.ksu_inspections(id),
    question_id uuid references public.ksu_questions(id),
    section text not null,
    title text not null,
    description text not null default '',
    severity text not null default 'medium' check (severity in ('low','medium','high','critical')),
    owner_name text,
    due_date date,
    status text not null default 'open' check (status in ('open','inprogress','closed')),
    resolution text,
    repeat_issue boolean not null default false,
    opened_at date not null default current_date,
    closed_at date,
    author_id uuid not null default auth.uid() references auth.users(id),
    updated_at timestamptz not null default now(),
    constraint finding_closed_requires_resolution check (
      status <> 'closed' or (closed_at is not null and length(trim(coalesce(resolution,''))) > 0)
    )
);
create index if not exists ix_ksu_findings_site_status on public.ksu_findings(site_id,status,due_date);
create index if not exists ix_ksu_findings_due on public.ksu_findings(due_date) where status <> 'closed';

create table if not exists public.ksu_inventories (
    id uuid primary key default gen_random_uuid(),
    site_id uuid not null references public.ksu_sites(id),
    inventory_date date not null,
    title text not null,
    planned_positions integer not null default 0 check (planned_positions >= 0),
    status text not null default 'draft' check (status in ('draft','final')),
    author_id uuid not null default auth.uid() references auth.users(id),
    created_at timestamptz not null default now()
);
create index if not exists ix_ksu_inventories_site_date on public.ksu_inventories(site_id,inventory_date desc);

create table if not exists public.ksu_inventory_lines (
    id uuid primary key default gen_random_uuid(),
    inventory_id uuid not null references public.ksu_inventories(id) on delete cascade,
    item_name text not null,
    sku text,
    category text,
    unit text not null default 'шт.',
    qty_book numeric(16,3) not null check (qty_book>=0),
    qty_fact numeric(16,3) not null check (qty_fact>=0),
    unit_price numeric(16,2) not null default 0 check (unit_price>=0),
    responsible_name text,
    cause text,
    qty_delta numeric(16,3) generated always as (qty_fact-qty_book) stored,
    shortage_amount numeric(18,2) generated always as (greatest(qty_book-qty_fact,0)*unit_price) stored,
    surplus_amount numeric(18,2) generated always as (greatest(qty_fact-qty_book,0)*unit_price) stored
);
create index if not exists ix_ksu_inventory_lines_inventory on public.ksu_inventory_lines(inventory_id);

create table if not exists public.ksu_attachments (
    id uuid primary key default gen_random_uuid(),
    finding_id uuid references public.ksu_findings(id) on delete cascade,
    inspection_id uuid references public.ksu_inspections(id) on delete cascade,
    storage_path text not null,
    original_name text,
    uploaded_by uuid not null references auth.users(id),
    uploaded_at timestamptz not null default now(),
    check ((finding_id is not null)::int + (inspection_id is not null)::int = 1)
);

create table if not exists public.ksu_audit_log (
    id bigint generated always as identity primary key,
    source_table text not null,
    source_id uuid not null,
    operation text not null check (operation in ('INSERT','UPDATE','DELETE')),
    actor_id uuid,
    changed_at timestamptz not null default now(),
    old_record jsonb,
    new_record jsonb
);
create index if not exists ix_ksu_log_table_id on public.ksu_audit_log(source_table,source_id,changed_at desc);

-- Сотрудники не могут самостоятельно выдавать себе права руководителя.
alter table public.ksu_members enable row level security;
create policy "members can see own membership" on public.ksu_members
    for select to authenticated using (user_id=auth.uid() or public.ksu_is_manager());
-- Добавление членов команды выполняется администратором через защищённый сервер.

alter table public.ksu_sites enable row level security;
create policy "members see sites" on public.ksu_sites for select to authenticated using (public.ksu_is_member());
create policy "managers insert sites" on public.ksu_sites for insert to authenticated with check (public.ksu_is_manager());
create policy "managers update sites" on public.ksu_sites for update to authenticated using (public.ksu_is_manager()) with check (public.ksu_is_manager());

alter table public.ksu_questions enable row level security;
create policy "members see checklist" on public.ksu_questions for select to authenticated using (public.ksu_is_member());
create policy "managers add checklist" on public.ksu_questions for insert to authenticated with check (public.ksu_is_manager());
create policy "managers update checklist" on public.ksu_questions for update to authenticated using (public.ksu_is_manager()) with check (public.ksu_is_manager());

alter table public.ksu_inspections enable row level security;
create policy "members see inspections" on public.ksu_inspections for select to authenticated using (public.ksu_is_member());
create policy "members create own inspections" on public.ksu_inspections for insert to authenticated with check (public.ksu_is_member() and inspector_id=auth.uid());
create policy "manager or owner updates inspections" on public.ksu_inspections for update to authenticated using (public.ksu_is_manager() or (public.ksu_is_member() and inspector_id=auth.uid())) with check (public.ksu_is_manager() or inspector_id=auth.uid());

alter table public.ksu_inspection_answers enable row level security;
create policy "members see answers" on public.ksu_inspection_answers for select to authenticated using (public.ksu_is_member());
create policy "author inserts answers" on public.ksu_inspection_answers for insert to authenticated with check (
    exists(select 1 from public.ksu_inspections i where i.id=inspection_id and (i.inspector_id=auth.uid() or public.ksu_is_manager()))
);
create policy "author updates answers" on public.ksu_inspection_answers for update to authenticated using (
    exists(select 1 from public.ksu_inspections i where i.id=inspection_id and (i.inspector_id=auth.uid() or public.ksu_is_manager()))
);

alter table public.ksu_findings enable row level security;
create policy "members see findings" on public.ksu_findings for select to authenticated using (public.ksu_is_member());
create policy "members add findings" on public.ksu_findings for insert to authenticated with check (public.ksu_is_member() and author_id=auth.uid());
create policy "members update findings" on public.ksu_findings for update to authenticated using (public.ksu_is_member()) with check (public.ksu_is_member());

alter table public.ksu_inventories enable row level security;
create policy "members see inventories" on public.ksu_inventories for select to authenticated using (public.ksu_is_member());
create policy "members create inventories" on public.ksu_inventories for insert to authenticated with check (public.ksu_is_member() and author_id=auth.uid());
create policy "members update inventories" on public.ksu_inventories for update to authenticated using (public.ksu_is_member()) with check (public.ksu_is_member());

alter table public.ksu_inventory_lines enable row level security;
create policy "members see inventory lines" on public.ksu_inventory_lines for select to authenticated using (public.ksu_is_member());
create policy "members add inventory lines" on public.ksu_inventory_lines for insert to authenticated with check (
    public.ksu_is_member() and exists(select 1 from public.ksu_inventories i where i.id=inventory_id)
);
create policy "members update inventory lines" on public.ksu_inventory_lines for update to authenticated using (public.ksu_is_member()) with check (public.ksu_is_member());

alter table public.ksu_attachments enable row level security;
create policy "members see attachments" on public.ksu_attachments for select to authenticated using (public.ksu_is_member());
create policy "members add attachments" on public.ksu_attachments for insert to authenticated with check (public.ksu_is_member() and uploaded_by=auth.uid());

alter table public.ksu_audit_log enable row level security;
create policy "managers see audit history" on public.ksu_audit_log for select to authenticated using (public.ksu_is_manager());

create or replace function public.ksu_track_change()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
    if TG_OP = 'INSERT' then
        insert into public.ksu_audit_log(source_table,source_id,operation,actor_id,new_record)
        values (TG_TABLE_NAME,new.id,TG_OP,auth.uid(),to_jsonb(new));
        return new;
    elsif TG_OP = 'UPDATE' then
        insert into public.ksu_audit_log(source_table,source_id,operation,actor_id,old_record,new_record)
        values (TG_TABLE_NAME,new.id,TG_OP,auth.uid(),to_jsonb(old),to_jsonb(new));
        return new;
    else
        insert into public.ksu_audit_log(source_table,source_id,operation,actor_id,old_record)
        values (TG_TABLE_NAME,old.id,TG_OP,auth.uid(),to_jsonb(old));
        return old;
    end if;
end; $$;

create trigger ksu_audit_findings after insert or update or delete on public.ksu_findings for each row execute function public.ksu_track_change();
create trigger ksu_audit_inventories after insert or update or delete on public.ksu_inventories for each row execute function public.ksu_track_change();
create trigger ksu_audit_lines after insert or update or delete on public.ksu_inventory_lines for each row execute function public.ksu_track_change();
create trigger ksu_audit_inspections after insert or update or delete on public.ksu_inspections for each row execute function public.ksu_track_change();


-- План выездов (добавлено в проект схемы v1.1).
-- Назначение и права сотрудников требуют дополнительного тестирования RLS до запуска.
create table if not exists public.ksu_planned_visits (
    id uuid primary key default gen_random_uuid(),
    site_id uuid not null references public.ksu_sites(id),
    visit_date date not null,
    kind text not null check (kind in ('inspection','followup','inventory','training')),
    assigned_to uuid not null references auth.users(id),
    goal text not null default '',
    status text not null default 'planned' check (status in ('planned','completed','cancelled')),
    inspection_id uuid references public.ksu_inspections(id),
    created_by uuid not null references auth.users(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists ix_ksu_planned_visits_assigned_date on public.ksu_planned_visits(assigned_to, visit_date);
alter table public.ksu_planned_visits enable row level security;
create policy "manager and assignee see visits" on public.ksu_planned_visits
  for select to authenticated
  using (public.ksu_is_manager() or (public.ksu_is_member() and assigned_to=auth.uid()));
create policy "manager creates visits" on public.ksu_planned_visits
  for insert to authenticated
  with check (public.ksu_is_manager() and created_by=auth.uid());
create policy "manager updates visits" on public.ksu_planned_visits
  for update to authenticated
  using (public.ksu_is_manager())
  with check (public.ksu_is_manager());

-- Фотографии предполагается хранить в приватном бакете Supabase Storage,
-- с политиками доступа для сотрудников и проверкой принадлежности записи.
-- Настройка бакета и политики загрузки — отдельная задача перед запуском.
-- Пользователи создаются через Supabase Auth или корпоративный SSO.
-- Текущая версия index.html с этой схемой пока НЕ интегрирована.
