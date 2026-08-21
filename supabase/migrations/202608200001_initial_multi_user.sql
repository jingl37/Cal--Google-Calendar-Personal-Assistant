-- Calendar Assistant multi-user data model.
-- Run this migration from the Supabase SQL editor or CLI.

create extension if not exists pgcrypto;

create table public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  timezone text not null default 'America/Toronto',
  role text not null default '',
  facts jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null default 'New conversation',
  messages jsonb not null default '[]'::jsonb,
  pending_schedule jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index conversations_user_updated_idx
  on public.conversations (user_id, updated_at desc);

-- OAuth tokens are backend-only. Authenticated browser users receive no table
-- privileges; the backend secret key is required to access these records.
create table public.google_connections (
  user_id uuid primary key references auth.users(id) on delete cascade,
  google_account_email text,
  encrypted_access_token text,
  encrypted_refresh_token text,
  token_expiry timestamptz,
  scopes text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;
alter table public.conversations enable row level security;
alter table public.google_connections enable row level security;

grant usage on schema public to authenticated;
grant select, insert, update, delete on public.profiles to authenticated;
grant select, insert, update, delete on public.conversations to authenticated;
revoke all on public.google_connections from anon, authenticated;
grant select, insert, update, delete on public.google_connections to service_role;

create policy "Users can read their profile"
  on public.profiles for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can create their profile"
  on public.profiles for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users can update their profile"
  on public.profiles for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "Users can delete their profile"
  on public.profiles for delete to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can read their conversations"
  on public.conversations for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can create their conversations"
  on public.conversations for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users can update their conversations"
  on public.conversations for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "Users can delete their conversations"
  on public.conversations for delete to authenticated
  using ((select auth.uid()) = user_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

create trigger conversations_set_updated_at
before update on public.conversations
for each row execute function public.set_updated_at();

create trigger google_connections_set_updated_at
before update on public.google_connections
for each row execute function public.set_updated_at();
