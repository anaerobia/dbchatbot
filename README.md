# DB Chatbot

Ask questions about your databases in plain English. Several databases can be
configured — any number of Oracle instances and PostgreSQL databases — and
you pick one from a dropdown in the UI. A Python (FastAPI) backend sends the
question to the Anthropic API to generate SQL in that database's dialect,
executes it read-only, and sends the results back to Claude to produce a
natural-language answer. A React (Vite) frontend provides the chat UI.

```
NL question ──▶ Claude (generate SQL) ──▶ read-only execute ──▶ Claude (summarize) ──▶ answer
                         │
                         └─ write request ──▶ SQL shown with a copy button (NOT executed)
```

**Example:**
- "How many rows are in AIRS_ARCHIVED_L2_BIN_MET?" → returns the count.
- "How many distinct collections are in AIRS_ARCHIVED_L2_BIN_MET?" → returns the distinct collections.

## Features

- **Multiple databases** — Oracle (by SID or service name) and PostgreSQL,
  configured in `backend/databases.json`. Each request is dispatched to the
  selected database through a common `Database` interface (`backend/db/`).
- **Dialect-correct SQL** — each backend supplies detailed dialect rules
  (row limits, date/string functions, DDL types, upsert syntax, …) and its
  detected server version, so Oracle gets `FETCH FIRST`/`MERGE`/`SYSDATE` and
  Postgres gets `LIMIT`/`ON CONFLICT`/`now()`. If a read query is rejected by
  the database, the error is sent back to Claude once for a corrected query.
- **SQL for changes, never executed** — ask for an insert/update/delete, a new
  table, a grant, etc. and the bot writes the statement(s) for you, with a
  **Copy SQL** button and a warning that the chatbot will not run it. Copy it
  and execute it yourself with an account that has write access.
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
  mode — no Oracle client install needed),
  [`psycopg`](https://www.psycopg.org/psycopg3/) 3 for PostgreSQL, `anthropic` SDK (`claude-opus-4-8`);
  deps managed by [`uv`](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`).
- **Frontend:** React 18 + Vite, `react-markdown` for answer rendering.

## Architecture: the database layer

```
backend/db/
  base.py      Database ABC: run_select(), get_schema_description(), health_check()
  oracle.py    OracleDatabase   (oracledb, SET TRANSACTION READ ONLY)
  postgres.py  PostgresDatabase (psycopg, read-only session + statement_timeout)
  guard.py     dialect-independent read-only check (assert_read_only / is_read_only)
  registry.py  loads databases.json, get_database(name) dispatches by name
```

To add another engine, subclass `Database` (set `kind`, `dialect`,
`dialect_rules`, implement `_execute_read_only` and `_fetch_columns`) and
register it in `BACKENDS` in `registry.py`.

## Safety: read-only by design

The chatbot **only ever executes single read-only queries**. When you ask for a
change, it generates the SQL and returns it for you to copy — with a warning
that it was not executed. Three layers make sure nothing else runs:

1. **Application guard** — `backend/db/guard.py` treats anything that is not a
   single `SELECT`/`WITH` statement (DML, DDL, statement chaining, Postgres
   data-modifying CTEs, `SELECT ... INTO`, `FOR UPDATE`) as a write. Writes are
   returned to the user, never executed — even if the model mislabels one as a
   read.
2. **Read-only sessions** — Oracle queries run after `SET TRANSACTION READ ONLY`;
   Postgres connections use `default_transaction_read_only=on`, a read-only
   transaction, and a `statement_timeout`.
3. **Dedicated read-only DB user (recommended).** The guard is defense in depth;
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

   Then use `chatbot_ro` in the database's config entry. Oracle schema
   introspection reads `USER_TAB_COLUMNS`; if the read-only user owns no
   tables, set `"schemas": ["OWNER_SCHEMA"]` on the entry to read
   `ALL_TAB_COLUMNS` for those owners instead.

   For PostgreSQL:

   ```sql
   CREATE ROLE chatbot_ro LOGIN PASSWORD '<strong-password>';
   GRANT CONNECT ON DATABASE analytics TO chatbot_ro;
   GRANT USAGE ON SCHEMA public TO chatbot_ro;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO chatbot_ro;
   ```

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
- The database passwords referenced by `password_env` in `databases.json`.

### Configure databases

```bash
cd backend
cp databases.example.json databases.json   # gitignored
```

Each entry needs a unique `name` and a `type` (`oracle` or `postgres`);
`default` picks the one selected when the UI loads, and `label` is the name
shown in the dropdown.

| type       | settings                                                                                      |
|------------|-----------------------------------------------------------------------------------------------|
| `oracle`   | `host`, `port` (1521), `sid` **or** `service_name`, `user`, password, optional `schemas` (owners) |
| `postgres` | `host`, `port` (5432), `dbname`, `user`, password, optional `schemas` (default `["public"]`), `sslmode`, `statement_timeout_ms` (60000) |

Give the password as `"password_env": "VAR_NAME"` (read from `.env`/the
environment — recommended) or inline as `"password"`. Set `DATABASES_CONFIG`
to use a different file path.

**Backward compatible:** if `databases.json` doesn't exist, the old
`ORACLE_HOST` / `ORACLE_PORT` / `ORACLE_SID` / `ORACLE_USER` /
`ORACLE_PASSWORD` variables in `.env` still define a single database named
`oracle`.

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
`curl http://localhost:8000/api/health` →
`{"status":"ok","databases":{"oracle_main":"ok",...}}`.

## API

- `GET /api/databases` — `{ default, databases: [{ name, label, type, dialect }] }`.
- `POST /api/ask` — request `{ "question": "...", "database": "analytics",
  "history": [{ "question": "...", "sql": "..." }] }` (`database` optional —
  defaults to the configured default; history optional, for follow-up context)
  → response `{ answer, sql, explanation, database, executed, warning, columns,
  rows, row_count, chart }`. `executed` is `false` (with a `warning`, and no
  rows) when the SQL would modify the database. `chart` is `null` or
  `{ type: "pie"|"bar"|"line", title, data: [{ label, value }] }`.
- `GET /api/health[?database=name]` — liveness + connectivity of every
  configured database (`status: "ok"|"degraded"`), or just the named one.
- `GET /api/schema[?database=name]` — the schema text the LLM is given (debugging).

## Notes

- `MAX_ROWS` (default 1000, in `.env`) hard-caps how many rows any query returns.
- The frontend "Show SQL & data" toggle reveals the generated SQL and a scrollable
  table of the returned rows for transparency and verification.
- Network access to the database host/port is required to reach the DB
  (e.g. VPN, if applicable).
