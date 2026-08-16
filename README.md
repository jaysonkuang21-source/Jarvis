# Jarvis

Personal assistant backed by an Obsidian vault. The desktop shell is Tauri;
the backend is FastAPI. Retrieval is engine-agnostic so LightRAG / GraphRAG /
Neo4j can be swapped behind one contract.

## Quick start (web UI + backend)

**Desktop shortcut (Windows):** one-time install, then double-click **Jarvis** on the Desktop (starts API + Vite and opens the UI):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-desktop-shortcut.ps1
```

Or two terminals:

```bash
# Terminal 1 — API on 127.0.0.1:8756
uv sync
uv run python -m app.main

# Terminal 2 — Vite on 5173, proxies /api to the backend
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Set your vault path under Settings → Rules.

**Local API auth (fail closed):** Protected `/api/*` routes (all except
`/api/health`) require `JARVIS_API_TOKEN` unless you explicitly set
`JARVIS_ALLOW_UNAUTHENTICATED_API=true` (lab/pytest on loopback only). Prefer
`scripts/start-web.ps1`, which mints a session token and injects it into both
the backend and Vite (`VITE_JARVIS_API_TOKEN`). Or set the same value in `.env`
(`JARVIS_API_TOKEN`) and `frontend/.env` (`VITE_JARVIS_API_TOKEN`). The Tauri
shell always mints/injects a token for the sidecar.
`JARVIS_ALLOW_NON_LOOPBACK=true` requires a token and cannot be combined with
unauthenticated mode.

## Desktop shell (Tauri)

Requires a Rust toolchain (`rustup`). From `frontend/`:

```bash
npm run tauri:dev
```

The shell starts the FastAPI sidecar (`uv run python -m app.main`), owns the
system tray, and raises native Windows toasts for timers. Closing the window
hides to the tray; Quit from the tray exits both processes. The shell mints a
session API token, sets `JARVIS_API_TOKEN` on the child, and delivers it to the
webview via `get_api_token` (sidecar stdio is discarded and is not the channel).

## Security

Secrets (`JARVIS_OPENAI_API_KEY`, `JARVIS_OBSIDIAN_API_KEY`,
`JARVIS_LANGSMITH_API_KEY`, `JARVIS_API_TOKEN`) live only in `.env` /
`SecretStr` settings — never hardcoded and never returned by API JSON
(`/api/health`, `/api/options`, `/api/profile`, etc.).

**Auth:** Fail closed when no token is configured. Opt into
`JARVIS_ALLOW_UNAUTHENTICATED_API` only for local lab/pytest. Production
(`JARVIS_APP_ENV=production`) disables OpenAPI `/docs` and `/redoc`. Responses
include baseline security headers (`X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`).

**Rate limits** (in-process): `JARVIS_RATE_LIMIT` (per IP),
`JARVIS_RATE_LIMIT_PER_USER` (per validated API token hash),
`JARVIS_RATE_LIMIT_GLOBAL` (process-wide). Loopback clients (desktop
sidecar, Vite proxy) and `/api/health` are fully exempt. Validated API
tokens skip the per-IP bucket (still pay per-user + global on remote peers).
Over-limit requests get `429`.

**Tokens:** Prefer the Tauri- or start-web-minted session token. Avoid baking
`VITE_JARVIS_API_TOKEN` into shipped frontend builds; if you use it for local
Vite, keep the same value in `.env` as `JARVIS_API_TOKEN` and do not log or
persist it in the browser.

**If a key may have been exposed** (OpenAI, Obsidian, LangSmith, or API token),
rotate it in that provider’s dashboard / Obsidian plugin settings, update
`.env`, and restart Jarvis. Do not paste keys into chat or commits.

### Obsidian Local REST API key

1. Obsidian → Settings → Community plugins → Local REST API → API Key → generate
   or replace the key.
2. Put the new value in `.env` as `JARVIS_OBSIDIAN_API_KEY` and restart Jarvis.
3. Treat the old key as burned; never commit it.

RAG messages put policy text in the system role and retrieved notes in a
separate non-system block. That prompt split is **not** the enforcement
boundary — `PolicyEngine` (`config/rules.md` frontmatter) is.

## Postgres + pgvector (hybrid RAG)

Chunks, HNSW vectors, and FTS live in **Postgres + pgvector**, configured with
`JARVIS_DATABASE_URL`. That is separate from **`data/jarvis.db`** (SQLite), which
is only for scheduler jobs.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/db-up.ps1
```

That starts `docker-compose.yml` (image `pgvector/pgvector:pg16`, published on
`127.0.0.1:5432` only), waits until healthy, and prints the URL line to set.
Lab defaults are user/password/db `jarvis` (override with `POSTGRES_*` in `.env`).
Stop with `scripts/db-down.ps1` (named volume is kept).

1. Ensure `.env` has `JARVIS_DATABASE_URL=postgresql://jarvis:jarvis@127.0.0.1:5432/jarvis`
2. Restart the API (`uv run python -m app.main` or the desktop shell)
3. Reindex from Settings so chunks land in Postgres

Without the URL (or if the DB is down), the app still runs with
`PlaceholderRetrievalEngine`. See [docs/retrieval-plan.md](docs/retrieval-plan.md).

## Public demo (NASA Hackathime)

Chat-only hosted demo (Vercel + Render + Supabase, GPT-4o mini locked, sample
vault RAG). Users paste a session OpenAI-compatible API key (wiped on sign-out;
rotate after use). See [docs/DEMO.md](docs/DEMO.md).
## Layout

| Path | Role |
|------|------|
| `app/` | FastAPI backend, policy engine, retrieval |
| `config/rules.md` | Machine-enforced assistant policy + prompt body |
| `frontend/` | Vite + React + Tailwind + shadcn UI |
| `src-tauri/` | Tray, autostart, notifications, sidecar spawn |
| `docker-compose.yml` | Local Postgres + pgvector (RAG); not SQLite |
| `data/jarvis.db` | SQLite — scheduler jobs only (not vectors) |
| `.cursor/rules/dependencies.mdc` | Blocks unapproved dependency changes |

## Regenerating TypeScript types

After editing `app/models.py`, `app/security.py`, or `app/scheduler.py`:

```bash
uv run python scripts/generate_types.py
```
