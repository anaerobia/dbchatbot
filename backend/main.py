"""FastAPI backend for the natural-language database chatbot.

Several databases (Oracle instances, Postgres databases, ...) can be
configured; each request names the one to use (see :mod:`db.registry`).

Flow for POST /api/ask:
  question -> Anthropic (generate SQL for the target dialect) ->
    read query:  read-only execute -> Anthropic (summarize) -> answer
                 (+ the SQL and rows, for transparency)
    write/other: NOT executed -> SQL returned for the user to copy, with a
                 warning that the chatbot cannot run it.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # load backend/.env before anything reads os.environ

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import db  # noqa: E402
import llm  # noqa: E402

app = FastAPI(title="DB Chatbot")

_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HistoryTurn(BaseModel):
    """A prior turn: the user's question and the SQL that answered it."""

    question: str
    sql: str


class AskRequest(BaseModel):
    """A natural-language question, plus optional prior conversation turns."""

    question: str
    # Name of the configured database to query; empty means the default.
    database: str | None = None
    # Oldest first. The frontend sends the recent turns so follow-up questions
    # can resolve references (e.g. an omitted table name) from context.
    history: list[HistoryTurn] = []


class Chart(BaseModel):
    """A normalized chart the frontend can render directly."""

    type: str  # "pie" | "bar" | "line"
    title: str
    data: list[dict]  # [{"label": str, "value": float}, ...]


class AskResponse(BaseModel):
    """The answer plus the SQL and results used to produce it.

    When ``executed`` is False the SQL modifies the database: it was not run,
    and ``warning`` tells the user to copy and execute it themselves.
    """

    answer: str
    sql: str
    explanation: str
    database: str
    executed: bool = True
    warning: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    chart: Chart | None = None


class DatabaseInfo(BaseModel):
    """A configured database, as offered to the UI."""

    name: str
    label: str
    type: str
    dialect: str


class DatabasesResponse(BaseModel):
    """All configured databases and which one is the default."""

    default: str
    databases: list[DatabaseInfo]


# How many times a read query rejected by the database is sent back to the
# LLM, with the error, for a corrected version.
SQL_REPAIR_ATTEMPTS = 1

NOT_EXECUTED_WARNING = (
    "This statement modifies the database, so the chatbot did not execute it. "
    "Review it carefully, then copy it and run it yourself with a tool and "
    "account that have write access."
)


def _build_chart(spec: dict, columns: list[str], rows: list[list]) -> Chart | None:
    """Turn the LLM chart spec + result rows into a renderable Chart, or None.

    Maps the spec's label/value column names (case-insensitively) to row
    indices and coerces values to float. Returns None if the spec asks for no
    chart, the columns don't match, or no numeric data results.
    """
    if not spec or spec.get("type", "none") == "none":
        return None

    upper = [c.upper() for c in columns]
    try:
        li = upper.index(spec["label_column"].upper())
        vi = upper.index(spec["value_column"].upper())
    except (ValueError, KeyError, AttributeError):
        return None

    data: list[dict] = []
    for row in rows:
        try:
            value = float(row[vi])
        except (TypeError, ValueError):
            continue  # skip non-numeric values
        data.append({"label": str(row[li]), "value": value})

    if not data:
        return None
    return Chart(type=spec["type"], title=spec.get("title", ""), data=data)


def _resolve_database(name: str | None) -> db.Database:
    """Return the named (or default) database, mapping errors to HTTP codes."""
    try:
        return db.get_database(name)
    except db.UnknownDatabaseError:
        raise HTTPException(status_code=404, detail=f"Unknown database '{name}'.")
    except db.ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Database config error: {exc}")


@app.get("/api/databases", response_model=DatabasesResponse)
def databases() -> DatabasesResponse:
    """List the configured databases so the UI can offer a selector."""
    try:
        registry = db.get_registry()
    except db.ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Database config error: {exc}")
    return DatabasesResponse(
        default=registry.default,
        databases=[
            DatabaseInfo(name=d.name, label=d.label, type=d.kind, dialect=d.dialect)
            for d in registry.all()
        ],
    )


@app.get("/api/health")
def health(database: str | None = None) -> dict:
    """Liveness check that also confirms the DB connection(s) work.

    With ``?database=name`` only that database is checked (503 if it is
    down). Otherwise every configured database is checked and reported; the
    overall status is ``"degraded"`` if any of them is unreachable.
    """
    if database:
        target = _resolve_database(database)
        try:
            target.health_check()
        except Exception as exc:  # noqa: BLE001 - surface any connectivity issue
            raise HTTPException(
                status_code=503, detail=f"Database unavailable: {exc}"
            )
        return {"status": "ok", "databases": {target.name: "ok"}}

    try:
        targets = db.list_databases()
    except db.ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Database config error: {exc}")

    statuses: dict[str, str] = {}
    for target in targets:
        try:
            target.health_check()
            statuses[target.name] = "ok"
        except Exception as exc:  # noqa: BLE001
            statuses[target.name] = f"unavailable: {exc}"
    healthy = all(v == "ok" for v in statuses.values())
    return {"status": "ok" if healthy else "degraded", "databases": statuses}


@app.get("/api/schema")
def schema(database: str | None = None) -> dict:
    """Return the schema description the LLM is given (useful for debugging)."""
    target = _resolve_database(database)
    try:
        return {"database": target.name, "schema": target.get_schema_description()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc))


def _generate(
    target: db.Database,
    question: str,
    history: list[dict],
    failed_attempt: dict | None,
) -> dict:
    """Ask the LLM for SQL in ``target``'s dialect; map failures to HTTP 502."""
    try:
        generated = llm.generate_sql(
            question,
            target.get_schema_description(),
            dialect_context=target.dialect_context(),
            history=history,
            failed_attempt=failed_attempt,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to generate SQL: {exc}")
    if not generated.get("sql", "").strip():
        raise HTTPException(status_code=502, detail="Model returned empty SQL.")
    return generated


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    """Answer a natural-language question about the database."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")

    max_rows = int(os.environ.get("MAX_ROWS", "1000"))
    target = _resolve_database(req.database)

    history = [t.model_dump() for t in req.history]
    failed_attempt: dict | None = None

    # A read query that the database rejects (typically a dialect slip, e.g.
    # LIMIT on Oracle) gets one repair attempt with the error fed back.
    for attempt in range(1 + SQL_REPAIR_ATTEMPTS):
        # 1. Natural language -> SQL in the target database's dialect
        generated = _generate(target, question, history, failed_attempt)
        sql = generated["sql"].strip()
        explanation = generated.get("explanation", "")

        # 2a. Anything that is not a safe read-only query -- whether the model
        # labelled it a write or the guard rejects it -- is handed back to the
        # user to run themselves, never executed here.
        if generated.get("statement_type") == "write" or not db.is_read_only(sql):
            return AskResponse(
                answer=explanation or "Here is the SQL statement for your request.",
                sql=sql,
                explanation=explanation,
                database=target.name,
                executed=False,
                warning=NOT_EXECUTED_WARNING,
            )

        # 2b. Execute (the read-only guard is enforced again inside run_select)
        try:
            columns, rows = target.run_select(sql, max_rows=max_rows)
            break
        except db.UnsafeQueryError as exc:
            raise HTTPException(
                status_code=400, detail=f"Rejected unsafe query: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - driver/database errors
            if attempt == SQL_REPAIR_ATTEMPTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Query failed: {exc}\n\nGenerated SQL:\n{sql}",
                )
            print(f"[ask] {target.name}: query failed, retrying: {exc}", flush=True)
            failed_attempt = {"sql": sql, "error": str(exc)}

    # Build the chart first so the summary knows a graphic is being shown.
    chart = _build_chart(generated.get("chart"), columns, rows)

    # 3. Results -> natural language
    try:
        answer = llm.summarize_answer(
            question,
            sql,
            columns,
            rows,
            has_chart=chart is not None,
            dialect=target.dialect,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to summarize answer: {exc}")

    return AskResponse(
        answer=answer,
        sql=sql,
        explanation=explanation,
        database=target.name,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        chart=chart,
    )
