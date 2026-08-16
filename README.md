<img width="1243" height="1264" alt="image" src="https://github.com/user-attachments/assets/2c52a21a-4d7e-4547-8fce-1b6f331bf6d8" /># Jarvis

An Obsidian-backed personal assistant that answers from your notes, with a desktop shell and a public BYOK demo.

![Jarvis demo — paste a session API key to chat against a sample vault](docs/assets/demo.png)

## Try it

**[Live demo → https://agent15365.hackclub.app](https://agent15365.hackclub.app)**

Claim a seat (4 users per IP), paste an OpenAI-compatible API key for the session, then chat. The key stays in browser memory only — rotate it after you finish.

## Quick start

Open the demo link above. That is the whole path from “curious” to “trying it.”

## Features

- Chat grounded in vault notes with citations you can open
- Hybrid retrieval: Postgres + pgvector (ANN/HNSW) fused with full-text search via RRF, then optional LLM rerank
- Demo ingestion: upload or paste docs into `demo/vault/Inbox`, reindex, inspect chunks
- Chunking Demonstrations: Recursive, semantic, structural, claim-centered
- Machine-enforced policy from `config/rules.md` (not just prompt text)
- Desktop shell (Tauri): tray, timers/toasts, FastAPI sidecar with session API auth
- Bring-your-own LLM key in the public demo (GPT-4o mini locked; no shared chat key on the server)

## Run locally

**Requirements:** Python **3.13+**, Node **20+**, [uv](https://docs.astral.sh/uv/), and Docker Desktop if you want hybrid RAG (Postgres + pgvector).

**One command (Windows):**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-web.ps1
```

That starts the API + Vite, mints a session API token, and opens http://localhost:5173.

**Or two terminals:**

```bash
# Terminal 1 — API on 127.0.0.1:8756
uv sync
uv run python -m app.main

# Terminal 2 — Vite on 5173 (proxies /api)
cd frontend
npm install
npm run dev
```

Copy [`.env.example`](.env.example) → `.env`. For local Vite without `start-web.ps1`, either set matching `JARVIS_API_TOKEN` / `VITE_JARVIS_API_TOKEN`, or use `JARVIS_ALLOW_UNAUTHENTICATED_API=true` on loopback only.

**Postgres (hybrid RAG):**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/db-up.ps1
```

Set `JARVIS_DATABASE_URL=postgresql://jarvis:jarvis@127.0.0.1:5432/jarvis`, restart the API, then reindex from Settings. Without a DB URL the app still runs with a placeholder retrieval engine.

**Desktop (Tauri):** Rust toolchain (`rustup`), then from `frontend/`:

```bash
npm run tauri:dev
```

**Public demo dry-run:** see [`docs/DEMO.md`](docs/DEMO.md). Hosted builds use `JARVIS_DEMO_MODE=true` and seat + BYOK auth (no Supabase login).

## How it works

Retrieval is a single `RetrievalEngine` contract. The production path is **Postgres hybrid**: metadata filter → TTL result cache → vector ANN + FTS fused with reciprocal rank fusion → optional entity neighborhood → LLM rerank → answer. Chat orchestration for that path runs through **LangGraph** (`app/retrieval/graph.py`) so Local / Global / DRIFT / Auto modes and an agentic grade→rewrite→retry loop stream cleanly over SSE.

Policy is separate from prompting: `PolicyEngine` reads frontmatter in `config/rules.md` and enforces vault write paths and tool permissions. The public demo leases a short-lived seat per IP, then requires a session LLM key in the browser so the server never holds users’ chat credentials.

## Credits

Built with FastAPI, LangGraph / LangChain, Postgres + [pgvector](https://github.com/pgvector/pgvector), Vite + React + Tailwind, and Tauri. Vault integration targets [Obsidian](https://obsidian.md/) (including the Local REST API plugin for desktop workflows). Demo hosting on [Hack Club Nest](https://hackclub.app).
