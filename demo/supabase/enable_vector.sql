-- Enable pgvector for the Jarvis demo hybrid index (Supabase SQL editor).
-- Run once in the dedicated demo project. Jarvis connects via JARVIS_DATABASE_URL
-- from Render using the DB password / pooler URI — not the browser anon key.

create extension if not exists vector;

-- Jarvis creates its own application tables via try_ensure_schema().
-- Do not expose those tables to anon/authenticated via PostgREST for the demo.
-- Prefer API-only DB access from Render.

-- Defense in depth: revoke Data API privileges if tables land in public.
-- Adjust after first successful reindex once table names exist:
--   revoke all on all tables in schema public from anon, authenticated;
--   revoke all on all sequences in schema public from anon, authenticated;
