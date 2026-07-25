# Oracle DB Chatbot

Ask questions about your Oracle database in plain English. A Python (FastAPI)
backend sends the question to the Anthropic API to generate an Oracle `SELECT`
query, executes it read-only, and sends the results back to Claude to produce a
natural-language answer. A React (Vite) frontend provides the chat UI.

```
NL question ──▶ Claude (generate SQL) ──▶ read-only execute ──▶ Claude (summarize) ──▶ answer
```

**Example:**
- "How many rows are in AIRS_ARCHIVED_L2_BIN_MET?" → returns the count.
- "How many distinct collections are in AIRS_ARCHIVED_L2_BIN_MET?" → returns the distinct collections.

## Features

- **Natural-language → SQL → answer** with the generated SQL and result rows
  shown for transparency ("Show SQL & data").
- **Conversational memory** — follow-up questions resolve references from prior
  turns (e.g. ask about `SNDR_J1_L2_MET`, then "how many distinct collections?"
  infers the same table). The frontend sends the recent turns with each request.
- **Charts** — ask to "plot / chart / graph …" and get a pie, bar, or line chart
  (e.g. "plot STAGEDFORGES in SNDR_SNPP_L2_MET as a pie"). Pies fold the long
  tail into an "Other" slice; colors are a colorblind-safe palette.
- **Rich answers** — responses render as Markdown (real tables, bold, lists).
  Inline tables are capped at 5 rows (full data is in "Show SQL & data").
- **Prompt caching** — the DB schema is cached on the Anthropic side, so repeat
  questions are cheaper.

## Stack

- **Backend:** FastAPI, [`oracledb`](https://python-oracledb.readthedocs.io/) (thin
  mode — no Oracle client install needed), `anthropic` SDK (`claude-opus-4-8`);
  deps managed by [`uv`](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`).
- **Frontend:** React 18 + Vite, `react-markdown` for answer rendering.

## Safety: read-only by design

Two layers protect the database:

1. **Application guard** — `backend/db.py` refuses anything that is not a single
   `SELECT`/`WITH` statement (no `INSERT/UPDATE/DELETE/DDL`, no statement
   chaining), and issues `SET TRANSACTION READ ONLY` per query.
2. **Dedicated read-only DB user (recommended).** The guard is defense in depth;
   the real boundary should be a database account that *cannot* write. Ask your
   DBA to create one:

   ```sql
   -- Run as a DBA
   CREATE USER chatbot_ro IDENTIFIED BY "<strong-password>";
   GRANT CREATE SESSION TO chatbot_ro;
   -- Grant SELECT only on the tables the bot should see, e.g.:
   GRANT SELECT ON <owner_schema>.<table_name> TO chatbot_ro;
   -- ...repeat per table, or use a role.
   ```

   Then point `ORACLE_USER`/`ORACLE_PASSWORD` at `chatbot_ro`. The schema
   introspection in `db.py` reads `USER_TAB_COLUMNS`; if the read-only user owns
   no tables, grant `SELECT` and adjust the introspection query to
   `ALL_TAB_COLUMNS` filtered to the owning schema.

## First-time setup

The backend uses [`uv`](https://docs.astral.sh/uv/) for Python dependency and
environment management. Install it if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, do this once:

```bash
# Backend — uv reads pyproject.toml, creates .venv, and installs deps
cd backend
uv sync
cp .env.example .env        # then edit .env with your API key

# Frontend
cd ../frontend
npm install
```

Edit `backend/.env`:
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com/ (or run `ant auth
  login` and leave it unset).
- Set the Oracle connection values (`ORACLE_HOST`, `ORACLE_PORT`, `ORACLE_SID`,
  `ORACLE_USER`, `ORACLE_PASSWORD`) for your database — ideally the read-only
  account above.

## Running

### Easiest: one command (starts both servers)

```bash
./start.sh
```

This launches the backend and the Vite frontend together, prints the URLs, and
stops both when you press **Ctrl+C**. It creates `backend/.env` from the example
and runs `npm install` on first run if needed.

Then open **http://localhost:5173**.

### Or run each server manually (two terminals)

```bash
# Terminal 1 — backend (uv run uses the project env; no activation needed)
cd backend && uv run uvicorn main:app --port 8000

# Terminal 2 — frontend
cd frontend && npm run dev
```

The dev server proxies `/api` to the backend on port 8000, so no CORS config is
needed for local use. Check connectivity:
`curl http://localhost:8000/api/health` → `{"status":"ok"}`.

## API

- `POST /api/ask` — request `{ "question": "...", "history": [{ "question": "...",
  "sql": "..." }] }` (history optional, for follow-up context) → response
  `{ answer, sql, explanation, columns, rows, row_count, chart }`, where `chart`
  is `null` or `{ type: "pie"|"bar"|"line", title, data: [{ label, value }] }`.
- `GET /api/health` — liveness + DB connectivity.
- `GET /api/schema` — the schema text the LLM is given (debugging).

## Notes

- `MAX_ROWS` (default 1000, in `.env`) hard-caps how many rows any query returns.
- The frontend "Show SQL & data" toggle reveals the generated SQL and a scrollable
  table of the returned rows for transparency and verification.
- Network access to the database host/port is required to reach the DB
  (e.g. VPN, if applicable).
