-- The encrypted Google token table is inaccessible to browsers. Only the
-- backend's secret service role may read or update it.
revoke all on public.google_connections from anon, authenticated;
grant select, insert, update, delete on public.google_connections to service_role;
