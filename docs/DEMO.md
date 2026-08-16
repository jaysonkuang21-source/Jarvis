# NASA Hackathime Demo Runbook

Public chat-only demo: Vite on **Vercel**, FastAPI on **Render**, Auth + Postgres
on a **dedicated Supabase** project. Locked to **GPT-4o mini**, sample vault RAG.
Users bring their own OpenAI-compatible API key for chat (session only).

## Architecture

```
Browser (Vercel)  --anon key-->  Supabase Auth
       |                              ^
       +--Bearer user JWT------------>+  (API verifies via Auth /user)
       |
       +--HTTPS REST/SSE-->  Render FastAPI (DB URL; no shared chat OpenAI key)
              |                    ^
              |                    +-- per-request X-Jarvis-User-LLM-Key (BYOK)
              |
              +--> Supabase Postgres (pgvector, sample vault index)
              +--> User's OpenAI-compatible endpoint (gpt-4o-mini)
```

Hosted builds are **always** demo: `JARVIS_DEMO_MODE=true` with
`JARVIS_ALLOW_NON_LOOPBACK=true` in production. Production non-loopback binds
without demo mode refuse to start.

## Before you deploy (secrets)

1. Create a **separate Supabase project** for the demo (not personal).
2. Enable MFA on your Supabase account; GitHub 2FA if applicable; minimal owners.
3. Confirm `.env` / `frontend/.env*` are gitignored (they are). Never commit secrets.
4. Optional: keep a **capped operator** OpenAI key only for one-time sample-vault
   reindex (not for user chat). Prefer rotating it after indexing.

Frontend may only hold:

- `VITE_DEMO_MODE=true`
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_ANON_KEY` (publishable)
- `VITE_API_BASE_URL` (Render origin, no path)

Never put service-role, DB password, or OpenAI keys in Vite/`NEXT_PUBLIC_` vars.
Demo users paste their own key in the UI; it stays in browser memory and is
wiped on sign-out. Instruct users to **always rotate the key after use**.

## Supabase

1. Auth → Email enabled. Prefer confirm-email on for abuse control.
2. Run [`demo/supabase/enable_vector.sql`](../demo/supabase/enable_vector.sql).
3. Copy the **pooler** Postgres URI into Render as `JARVIS_DATABASE_URL`.
4. Copy project URL + **anon** key to Render (`JARVIS_SUPABASE_*`) and Vercel (`VITE_SUPABASE_*`).
5. Open **Security Advisor**; fix real findings — do not dismiss unread.
6. Prefer API-only DB access from Render. Do not grant anon table SELECT for index tables.

## Render (API)

1. Connect the repo; use [`Dockerfile`](../Dockerfile) / [`render.yaml`](../render.yaml).
2. Set env (see `.env.example` demo section), including:
   - `JARVIS_DEMO_MODE=true` (required for hosted)
   - `JARVIS_ALLOW_NON_LOOPBACK=true`
   - `JARVIS_CORS_ORIGINS=["https://YOUR_APP.vercel.app"]`
   - Supabase URL/anon, database URL
   - Optional `JARVIS_API_TOKEN` for operator reindex only
   - Optional `JARVIS_OPENAI_API_KEY` only if you reindex embeddings as operator
3. Health check: `GET /api/health` (should report `"environment":"demo"`).
4. After first healthy boot, call `POST /api/index/reindex` once with the
   **process API token** (not a user JWT) to index `demo/vault/`.

## Vercel (UI)

1. Root directory: `frontend`
2. Build uses [`frontend/vercel.json`](../frontend/vercel.json)
3. Env: copy from [`frontend/.env.demo.example`](../frontend/.env.demo.example)
4. Redeploy after setting `VITE_API_BASE_URL` to the Render HTTPS origin

## Local demo dry-run

```powershell
# Backend
$env:JARVIS_DEMO_MODE="true"
$env:JARVIS_SUPABASE_URL="https://....supabase.co"
$env:JARVIS_SUPABASE_ANON_KEY="eyJ..."
$env:JARVIS_DATABASE_URL="postgresql://..."
$env:JARVIS_ALLOW_NON_LOOPBACK="true"
$env:JARVIS_HOST="127.0.0.1"
# Optional operator token for reindex:
# $env:JARVIS_API_TOKEN="..."
# $env:JARVIS_OPENAI_API_KEY="sk-..."  # embeddings during operator reindex only
uv run python -m app.main

# Frontend
cd frontend
copy .env.demo.example .env.local
# edit .env.local — for local API leave VITE_API_BASE_URL empty (Vite proxies /api)
npm run dev
```

Sign in, paste a session API key in the UI, then chat. Sign out clears the key.

## Kill switches (after the event)

1. Supabase Auth → disable new signups (or pause project).
2. Render → suspend / delete the web service.
3. If you used an operator OpenAI key for reindex → revoke it.
4. Vercel → unpublish or password-protect the deployment.

## Panel checklist (two accounts)

### Secrets

- [ ] Network tab shows anon key + user JWT + (per chat) user LLM key header — no service-role / DB password
- [ ] No secrets in URLs, client logs, or SSE payloads
- [ ] Session LLM key cleared after Sign out / Clear API key
- [ ] `.env` not in git history (`git log --all -- .env` empty)

### Auth / BYOK

- [ ] Unauthenticated `POST /api/chat` → 401
- [ ] Signed-in chat without `X-Jarvis-User-LLM-Key` → 400
- [ ] Sign-in required before chat UI loads
- [ ] User A session cannot use User B’s token (logout / other browser)

### Model lock / capabilities

- [ ] Banner: GPT-4o mini / BYOK / rotate after use / rate limited
- [ ] Settings / Models TODO / timers / hub nav hidden
- [ ] `PUT /api/profile` → 403
- [ ] `POST /api/timers` / `POST /api/voice` / user `POST /api/index/reindex` → 403
- [ ] Chat body sending another model still answers as GPT-4o mini

### Rate limits

- [ ] Burst from one IP without auth → 429
- [ ] Burst as one signed-in user → 429 with user scope detail
- [ ] Global ceiling still binds under multi-user load

### Data / personal leak

- [ ] Sample vault has no real name, email, school ID, or home path
- [ ] `/api/health` and `/api/system` expose no hardware or absolute paths
- [ ] `/api/rules` vault path is basename-only in demo
- [ ] Retrieved citations only reference `demo/vault` notes
- [ ] No shared answer cache across demo users

### Hosting

- [ ] HTTPS on Vercel, Render, Supabase
- [ ] CORS restricted to the Vercel origin
- [ ] Supabase Security Advisor reviewed
- [ ] OpenAPI `/docs` disabled (`app_env=production` / demo)

## Intentionally out of scope

Enterprise Redis, WAF, multi-region, Fish TTS, Ollama, real personal vault, full hub,
user document ingest in demo.
